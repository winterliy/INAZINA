"""Company capital accounts: a company invests, banks and borrows like anyone else.

A company is not a person and never logs in. Its capital lives in a system
account ``company:<code>`` that the existing managers treat like any holder:
it can place orders, own shares (including its own: treasury stock, or another
company's: a subsidiary), open bank accounts and take loans.

The account's cash *is* the issuer's cash ledger (``issuer_cash_accounts``):
database triggers keep the two equal, so dividends funded by the issuer,
offerings and these trades all move the same money. Changes that bypass the
corporate-action journal are booked to the company's financial statements by
:meth:`CompanyAccounts.reconcile`.
"""
from functions.exact_money import ExactDecimal
import threading
from decimal import Decimal

from functions.money import base_currency_code
from functions.time_utils import MAX_SUPPORTED_EPOCH

PREFIX = "company:"


def _trace_effective_owners(shares_by_holder, votes_by_holder, equity_by_holder, equity_total_by_org,
                            votes_total_by_org, share_supply_by_org, issuer_accounts, account_issuers,
                            treasury_ids, organization_names, user_names):
    effective_owners_by_org = {}
    # Trace each issuer's ownership through corporate accounts to terminal
    # holders. Simple paths prevent circular cross-holdings from multiplying
    # control indefinitely; different valid paths to the same terminal owner
    # are summed, matching the organization graph's path attribution model.
    for root in shares_by_holder:
        totals = {}
        equity_denominator = equity_total_by_org.get(root, Decimal(0))
        vote_denominator = votes_total_by_org.get(root, Decimal(0))
        pending = []
        for holder in shares_by_holder.get(root, {}):
            if holder in treasury_ids or holder == issuer_accounts.get(root):
                continue
            equity_ratio = (equity_by_holder.get(root, {}).get(holder, Decimal(0)) / equity_denominator
                            if equity_denominator else Decimal(0))
            vote_ratio = (votes_by_holder.get(root, {}).get(holder, Decimal(0)) / vote_denominator
                          if vote_denominator else Decimal(0))
            if equity_ratio or vote_ratio:
                pending.append((holder, equity_ratio, vote_ratio, frozenset({root})))
        traversed = 0
        while pending and traversed < 50000:
            holder, equity_path, vote_path, ancestry = pending.pop()
            traversed += 1
            intermediary = account_issuers.get(holder)
            if not intermediary or intermediary in ancestry:
                node = ("company", intermediary) if intermediary else ("user", holder)
                amount = totals.setdefault(node, [Decimal(0), Decimal(0)])
                amount[0] += equity_path
                amount[1] += vote_path
                continue
            equity_denominator = equity_total_by_org.get(intermediary, Decimal(0))
            vote_denominator = votes_total_by_org.get(intermediary, Decimal(0))
            owners = shares_by_holder.get(intermediary, {})
            if not owners:
                node = ("company", intermediary)
                amount = totals.setdefault(node, [Decimal(0), Decimal(0)])
                amount[0] += equity_path
                amount[1] += vote_path
                continue
            for next_holder in owners:
                if next_holder in treasury_ids or next_holder == issuer_accounts.get(intermediary):
                    continue
                equity_ratio = (equity_by_holder.get(intermediary, {}).get(next_holder, Decimal(0)) / equity_denominator
                                if equity_denominator else Decimal(0))
                vote_ratio = (votes_by_holder.get(intermediary, {}).get(next_holder, Decimal(0)) / vote_denominator
                              if vote_denominator else Decimal(0))
                if not equity_ratio and not vote_ratio:
                    continue
                next_equity_path = equity_path * equity_ratio
                next_vote_path = vote_path * vote_ratio
                next_ancestry = ancestry | {intermediary}
                if len(next_ancestry) >= 8 and account_issuers.get(next_holder) not in next_ancestry:
                    terminal_code = account_issuers.get(next_holder)
                    node = ("company", terminal_code) if terminal_code else ("user", next_holder)
                    amount = totals.setdefault(node, [Decimal(0), Decimal(0)])
                    amount[0] += next_equity_path
                    amount[1] += next_vote_path
                else:
                    pending.append((next_holder, next_equity_path, next_vote_path, next_ancestry))
        if totals:
            (kind, holder), (equity_ratio, vote_ratio) = max(
                totals.items(), key=lambda pair: (pair[1][1], pair[1][0], pair[0][1]))
            name = (organization_names.get(holder, holder) if kind == "company"
                    else user_names.get(holder, holder))
            effective_owners_by_org[root] = {
                "id": holder, "kind": kind, "name": name,
                "shares": str(equity_ratio * share_supply_by_org.get(root, Decimal(0))),
                "percent": str(vote_ratio * 100),
            }
    return effective_owners_by_org


_OWNER_TRACE_CACHE = {"key": None, "value": None}
_OWNER_TRACE_LOCK = threading.Lock()


def _cached_effective_owners(shares_by_holder, votes_by_holder, equity_by_holder, equity_total_by_org,
                             votes_total_by_org, share_supply_by_org, issuer_accounts, account_issuers,
                             treasury_ids, organization_names, user_names):
    def frozen(nested):
        return tuple(sorted((k, tuple(sorted(v.items()))) for k, v in nested.items()))
    key = (frozen(shares_by_holder), frozen(votes_by_holder), frozen(equity_by_holder),
           tuple(sorted(equity_total_by_org.items())), tuple(sorted(votes_total_by_org.items())),
           tuple(sorted(share_supply_by_org.items())), tuple(sorted(issuer_accounts.items())),
           tuple(sorted(account_issuers.items())), tuple(sorted(treasury_ids)),
           tuple(sorted(organization_names.items())), tuple(sorted(user_names.items())))
    with _OWNER_TRACE_LOCK:
        if _OWNER_TRACE_CACHE["key"] == key:
            return _OWNER_TRACE_CACHE["value"]
    value = _trace_effective_owners(
        shares_by_holder, votes_by_holder, equity_by_holder, equity_total_by_org, votes_total_by_org,
        share_supply_by_org, issuer_accounts, account_issuers, treasury_ids, organization_names, user_names)
    with _OWNER_TRACE_LOCK:
        _OWNER_TRACE_CACHE["key"], _OWNER_TRACE_CACHE["value"] = key, value
    return value


class CompanyAccounts:
    @staticmethod
    def is_company(conn, user_id):
        return bool(conn.execute("SELECT 1 FROM company_accounts WHERE user_id=?", (str(user_id),)).fetchone())

    @staticmethod
    def issuer_of(conn, user_id):
        row = conn.execute("SELECT company_code FROM company_accounts WHERE user_id=?", (str(user_id),)).fetchone()
        return str(row[0]) if row else None

    @staticmethod
    def account_id(conn, code):
        """The company's capital account, opened on first use."""
        code = str(code).strip()
        row = conn.execute("SELECT user_id FROM company_accounts WHERE company_code=?", (code,)).fetchone()
        if row:
            return str(row[0])
        company = conn.execute("SELECT * FROM company WHERE company_code=? AND status='active'", (code,)).fetchone()
        if not company or company["share_class"] == "preferred":
            raise ValueError("거래 중인 보통주 법인 코드를 입력해주세요. 우선주는 소속 법인의 자본을 사용합니다.")
        # The issuer's cash is 기준 통화, so the account lives in an 기준 통화 country:
        # the listing country when it uses 기준 통화, otherwise 낙원.
        listing = conn.execute("SELECT e.nationality FROM market_exchanges e JOIN country_currencies c "
                               "ON c.nationality=e.nationality WHERE e.exchange_id=? AND c.currency_code=base_currency_code()",
                               (company["exchange_id"],)).fetchone()
        nationality = listing[0] if listing else "낙원"
        conn.execute("INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)", (code,))
        cash = conn.execute("SELECT balance FROM issuer_cash_accounts WHERE company_code=?", (code,)).fetchone()[0]
        user_id = PREFIX + code  # non-decimal: can never be a Discord or web login
        conn.execute("INSERT INTO users(user_id,nickname,nationality,cash,status,role) VALUES (?,?,?,?,'active','company')",
                     (user_id, company['name'], nationality, int(cash)))
        conn.execute("INSERT INTO company_accounts(company_code,user_id) VALUES (?,?)", (code, user_id))
        from managers.financial_manager import FinancialManager
        FinancialManager.ensure(conn, code)
        return user_id

    @staticmethod
    def _nwx(conn, amount, currency):
        """A native amount in ``currency`` valued in whole 기준 통화."""
        from managers.currency_manager import CurrencyManager, SCALE
        currency = str(currency or base_currency_code(conn)).upper()
        if currency == base_currency_code(conn) or not amount:
            return ExactDecimal(amount or 0)
        units = int(round(ExactDecimal(amount) * SCALE))
        sign = -1 if units < 0 else 1
        return ExactDecimal(sign * CurrencyManager.convert(conn, abs(units), currency, base_currency_code(conn))) / SCALE

    @staticmethod
    def _nwx_decimal(conn, amount, currency):
        """Convert a Decimal native-currency amount to 기준 통화 without float."""
        from managers.currency_manager import CurrencyManager, SCALE
        native = Decimal(str(amount or 0))
        currency = str(currency or base_currency_code(conn)).upper()
        units = int(native * SCALE)
        sign = -1 if units < 0 else 1
        if currency == base_currency_code(conn):
            return Decimal(units) / SCALE
        converted = CurrencyManager.convert(
            conn, abs(units), currency, base_currency_code(conn)
        )
        return Decimal(sign * converted) / SCALE

    @staticmethod
    def _currency_of(conn, code):
        row = conn.execute(
            "SELECT quote_currency FROM company WHERE company_code=? UNION ALL "
            "SELECT quote_currency FROM investment WHERE product_code=? UNION ALL "
            "SELECT quote_currency FROM market_assets WHERE code=? UNION ALL "
            "SELECT quote_currency FROM treasury_bond_products WHERE product_code=? LIMIT 1",
            (code, code, code, code)).fetchone()
        return (row[0] if row else None) or base_currency_code(conn)

    @classmethod
    def positions(cls, conn, account, issuer):
        """Cost basis of what the account holds and owes, in 기준 통화.

        investments: securities at cost, deposits at balance, cash locked in orders
        and collateral; treasury: the company's own shares at cost; debt: loans.
        """
        investments = treasury = Decimal(0)
        from functions.money import currency_precision, from_minor_units
        for lot in conn.execute("SELECT holding_id,asset_code,quantity,average_cost_minor_units FROM user_assets WHERE user_id=?", (account,)):
            currency = cls._currency_of(conn, lot["asset_code"])
            places = currency_precision(conn, currency)
            from managers.asset_cost_manager import AssetCostManager
            cost = AssetCostManager.decimal_minor(AssetCostManager.total_cost_minor(conn, lot['holding_id'])) / (10 ** places)
            value = cls._nwx_decimal(conn, cost, currency)
            if cls.own_shares(conn, account, lot["asset_code"]):
                treasury += value
            else:
                investments += value
        from managers.currency_manager import SCALE
        for row in conn.execute("SELECT balance_units,quote_currency FROM accounts WHERE user_id=?", (account,)):
            investments += Decimal(str(cls._nwx(
                conn, ExactDecimal(row["balance_units"] or 0) / SCALE, row["quote_currency"]
            )))
        for row in conn.execute("SELECT reserved_cash,quote_currency FROM market_orders WHERE user_id=? "
                                "AND status IN ('scheduled','open','partial') AND reserved_cash>0", (account,)):
            investments += cls._nwx_decimal(
                conn, row["reserved_cash"], row["quote_currency"]
            )
        for query in ("SELECT COALESCE(SUM(acquisition_cost),0) FROM treasury_bond_holdings WHERE user_id=? AND status='active'",
                      "SELECT COALESCE(SUM(collateral),0) FROM futures_positions WHERE user_id=?",
                      "SELECT COALESCE(SUM(amount),0) FROM option_collateral_cash_lots WHERE writer_id=?",
                      "SELECT COALESCE(SUM(principal),0) FROM els_subscriptions WHERE user_id=? AND status IN ('subscribed','issued')"):
            investments += Decimal(str(conn.execute(query, (account,)).fetchone()[0] or 0))
        debt = sum((cls._nwx_decimal(
            conn, row["outstanding_balance"], row["quote_currency"]
        ) for row in conn.execute(
            "SELECT outstanding_balance,quote_currency FROM loans WHERE user_id=? AND status IN ('active','defaulted')", (account,))), Decimal(0))
        return {"investments": investments, "treasury": treasury, "debt": debt}

    @classmethod
    def reconcile(cls, conn, code, residual=None):
        """Bring the statements in line with the live account, classifying the difference.

        Cash, investments (at cost), treasury stock and loans move to their live
        values. What remains is income or expense: logged dividends are 영업외수익
        (with their withholding tax), the rest is the gain or loss on investments,
        unless ``residual`` names an account (e.g. other_expense for a transfer).
        """
        from managers.financial_manager import FinancialManager
        issuer = FinancialManager.ensure(conn, code)
        entity = FinancialManager.entity(conn, issuer)
        if entity["currency"] != base_currency_code(conn):
            return 0
        account = conn.execute("SELECT user_id FROM company_accounts WHERE company_code=?", (issuer,)).fetchone()
        scale = 10 ** int(entity["decimal_places"])
        books = FinancialManager.balances(conn, issuer, MAX_SUPPORTED_EPOCH)
        live_cash = FinancialManager.cash_state(conn, issuer)[0] * scale
        held = cls.positions(conn, account[0], issuer) if account else {"investments": 0, "treasury": 0, "debt": 0}
        logs = conn.execute("SELECT * FROM company_income_log WHERE company_code=? AND booked=0", (issuer,)).fetchall()
        total = lambda kind, field="gross_nwx": sum(int(row[field]) for row in logs if row["kind"] == kind) * scale
        dividends, taxes = total("dividend"), total("dividend", "tax_nwx")
        received, sent = total("transfer_in"), total("transfer_out")
        posted = []
        # 1) Operating: 배당금수익 (with withholding tax) and transfers in/out (영업외수익·비용).
        operating_cash = dividends - taxes + received - sent
        operating = [("cash", operating_cash), ("other_income", -(dividends + received)),
                     ("income_tax", taxes), ("other_expense", sent)]
        operating = [(name, value) for name, value in operating if value]
        if operating:
            FinancialManager.event(conn, issuer, "법인 배당금수익·송금 정산", operating, "operating")
            posted += operating
        # 2) Investing/financing: holdings at cost, treasury stock and loans move to their
        #    live values; what is left is the realised gain or loss on investments.
        lines = [("cash", live_cash - int(books.get("cash", 0)) - operating_cash)]
        if account:
            lines += [("short_investments", int(round(held["investments"] * scale)) - int(books.get("short_investments", 0))),
                      ("treasury_stock", int(round(held["treasury"] * scale)) - int(books.get("treasury_stock", 0))),
                      ("short_debt", -int(round(held["debt"] * scale)) - int(books.get("short_debt", 0)))]
        # Deposit interest (with its withholding tax) and loan interest are 이자수익/이자비용.
        lines += [("interest_income", -total("interest_income")), ("income_tax", total("interest_income", "tax_nwx")),
                  ("interest_expense", total("interest_expense")), ("overdue_interest", total("overdue_interest"))]
        remainder = -sum(value for _, value in lines)
        if remainder:
            lines.append((residual or ("investment_gain" if remainder < 0 else "investment_loss"), remainder))
        lines = [(name, value) for name, value in lines if value]
        if lines:
            FinancialManager.event(conn, issuer, "법인 계정 투자·차입 정산 (이자·투자손익 포함)", lines, "investing")
            posted += lines
        conn.execute("UPDATE company_income_log SET booked=1 WHERE company_code=? AND booked=0", (issuer,))
        return posted

    @classmethod
    def refresh_statements(cls, db, code):
        """Reconcile source positions and explicit income without guessed profit."""
        from managers.financial_manager import FinancialManager, month
        with db.atomic() as conn:
            issuer = FinancialManager.ensure(conn, code)
            if conn.execute('SELECT 1 FROM financial_periods WHERE company_code=? AND period>=?',
                            (issuer, month())).fetchone():
                return None
            ent = FinancialManager.entity(conn, issuer)
            for row in conn.execute('SELECT * FROM company_income_log WHERE company_code=? AND booked=0 ORDER BY entry_id', (issuer,)).fetchall():
                gross, tax = int(row['gross_nwx']), int(row['tax_nwx'])
                kind = row['kind']
                if kind in ('dividend', 'transfer_in', 'interest_income'):
                    income = 'interest_income' if kind == 'interest_income' else 'other_income'
                    lines = [('cash', gross-tax), (income, -gross), ('income_tax', tax)]
                else:
                    expense = {'transfer_out': 'other_expense', 'interest_expense': 'interest_expense', 'overdue_interest': 'overdue_interest'}[kind]
                    lines = [('cash', -gross), (expense, gross)]
                FinancialManager.event(conn, issuer, '원본 수익·비용 기록 자동 반영', lines, 'operating',
                    key='income-log:' + str(row['entry_id']), currency=base_currency_code(conn))
                conn.execute('UPDATE company_income_log SET booked=1 WHERE entry_id=?', (row['entry_id'],))
            account = conn.execute('SELECT user_id FROM company_accounts WHERE company_code=?', (issuer,)).fetchone()
            positions = cls.positions(conn, account[0], issuer) if account else None
            return FinancialManager.reconcile_sources(conn, issuer, positions)

    @classmethod
    def financial_report(cls, db, code, period=None):
        from managers.financial_manager import FinancialManager, month
        if period is None or period == month():
            cls.refresh_statements(db, code)
        return FinancialManager(db).report(code, period)

    @staticmethod
    def log_income(conn, account, kind, gross_nwx, tax_nwx=0, counterparty=None):
        """Remember a dividend or intra-group transfer for classification and consolidation."""
        issuer = CompanyAccounts.issuer_of(conn, account)
        if issuer:
            import time
            conn.execute("INSERT INTO company_income_log(company_code,kind,counterparty,gross_nwx,tax_nwx,recorded_epoch) "
                         "VALUES (?,?,?,?,?,?)", (issuer, kind, counterparty, int(round(gross_nwx)), int(round(tax_nwx)), int(time.time())))

    @staticmethod
    def own_shares(conn, user_id, code):
        """Treasury stock: shares a company holds in itself (no dividend, no vote)."""
        issuer = CompanyAccounts.issuer_of(conn, user_id)
        if not issuer:
            return False
        row = conn.execute("SELECT COALESCE(parent_company_code,company_code) FROM company WHERE company_code=?",
                           (str(code),)).fetchone()
        return bool(row and row[0] == issuer)

    @staticmethod
    def summary(conn):
        rows = conn.execute(
            "SELECT c.company_code,c.name,c.exchange_id,c.status,c.total_shares,c.listed_shares,c.share_price,c.capital,c.par_value_minor_units,c.quote_currency,"
            "COALESCE(i.balance,0) cash,COALESCE(i.locked_amount,0) locked,a.user_id account "
            "FROM company c LEFT JOIN issuer_cash_accounts i ON i.company_code=c.company_code "
            "LEFT JOIN company_accounts a ON a.company_code=c.company_code "
            "WHERE (c.status='active' OR EXISTS(SELECT 1 FROM organizations o "
            "WHERE o.organization_code=c.company_code AND o.status='active')) "
            "AND COALESCE(c.share_class,'common')='common' ORDER BY c.company_code").fetchall()
        from database.user_db import TREASURY_USER_ID
        from decimal import Decimal, InvalidOperation
        from functions.money import base_currency_code, currency_precision, round_money
        from managers.currency_manager import CurrencyManager

        base_currency = base_currency_code(conn)
        legal_currencies = {
            str(row[0]): str(row[1]) for row in conn.execute(
                "SELECT o.organization_code,n.currency_code FROM organizations o "
                "JOIN country_currencies n ON n.nationality=o.country_code "
                "JOIN currencies c ON c.code=n.currency_code AND c.status='active'"
            )
        }

        def convert_cash(amount, target_currency):
            if target_currency == base_currency:
                return amount
            quote = CurrencyManager.quote(conn, base_currency, target_currency)
            converted = Decimal(str(amount or 0)) * Decimal(quote.numerator) / Decimal(quote.denominator)
            return round_money(converted, currency_precision(conn, target_currency))

        issuer_accounts = {str(row[0]): str(row[1]) for row in conn.execute(
            "SELECT company_code,user_id FROM company_accounts")}
        account_issuers = {user_id: code for code, user_id in issuer_accounts.items()}
        organization_names = {str(row[0]): str(row[1]) for row in conn.execute(
            "SELECT organization_code,name FROM organizations")}
        user_names = {str(row[0]): str(row[1] or row[0]) for row in conn.execute(
            "SELECT user_id,nickname FROM users")}
        # National accounts are real shareholders, unlike the internal system treasury.
        treasury_ids = {str(TREASURY_USER_ID)}
        issues = conn.execute(
            "SELECT s.organization_code,c.company_code,c.total_shares,s.voting_rights_per_share,s.economic_interest_per_share "
            "FROM share_issuances s JOIN company c ON c.share_issuance_id=s.issuance_id "
            "WHERE c.status='active' OR EXISTS(SELECT 1 FROM organizations o "
            "WHERE o.organization_code=s.organization_code AND o.status='active') "
            "ORDER BY s.organization_code,c.company_code").fetchall()
        holdings_by_issue = {}
        for holding in conn.execute(
            "SELECT u.asset_code,u.user_id,SUM(u.quantity) quantity FROM user_assets u "
            "JOIN company c ON c.company_code=u.asset_code "
            "JOIN share_issuances s ON s.issuance_id=c.share_issuance_id "
            "WHERE c.status='active' OR EXISTS(SELECT 1 FROM organizations o "
            "WHERE o.organization_code=s.organization_code AND o.status='active') "
            "GROUP BY u.asset_code,u.user_id"):
            holdings_by_issue.setdefault(str(holding[0]), []).append((str(holding[1]), Decimal(str(holding[2] or 0))))
        parents_by_org = {}
        shares_by_holder = {}
        votes_by_holder = {}
        equity_by_holder = {}
        votes_total_by_org = {}
        equity_total_by_org = {}
        share_supply_by_org = {}
        votes_by_org = {}
        for issue in issues:
            organization, issue_code = str(issue[0]), str(issue[1])
            try:
                supply = Decimal(str(issue[2] or 0))
                votes_per_share = Decimal(str(issue[3] or 0))
                equity_per_share = Decimal(str(issue[4] or 0))
            except InvalidOperation:
                continue
            if (not supply.is_finite() or supply <= 0 or not votes_per_share.is_finite()
                    or votes_per_share < 0 or not equity_per_share.is_finite() or equity_per_share < 0):
                continue
            issuer_user = issuer_accounts.get(organization)
            amounts = holdings_by_issue.get(issue_code, ())
            excluded = treasury_ids | ({issuer_user} if issuer_user else set())
            excluded_amount = sum((quantity for holder, quantity in amounts if holder in excluded), Decimal(0))
            votes_total = max(Decimal(0), supply - excluded_amount) * votes_per_share
            equity_total_by_org[organization] = equity_total_by_org.get(organization, Decimal(0)) + max(Decimal(0), supply - excluded_amount) * equity_per_share
            share_supply_by_org[organization] = share_supply_by_org.get(organization, Decimal(0)) + supply
            if votes_total <= 0:
                votes_total = Decimal(0)
            else:
                votes_total_by_org[organization] = votes_total_by_org.get(organization, Decimal(0)) + votes_total
            issue_shares = shares_by_holder.setdefault(organization, {})
            issue_votes = votes_by_holder.setdefault(organization, {})
            issue_equity = equity_by_holder.setdefault(organization, {})
            votes_by_company = votes_by_org.setdefault(organization, {})
            for holder, quantity in amounts:
                if issuer_user and holder == issuer_user:
                    continue
                issue_shares[holder] = issue_shares.get(holder, Decimal(0)) + quantity
                if holder in excluded:
                    continue
                voting_power = quantity * votes_per_share
                issue_votes[holder] = issue_votes.get(holder, Decimal(0)) + voting_power
                issue_equity[holder] = issue_equity.get(holder, Decimal(0)) + quantity * equity_per_share
                shareholder = account_issuers.get(holder)
                if shareholder and shareholder != organization:
                    votes_by_company[shareholder] = votes_by_company.get(shareholder, Decimal(0)) + voting_power
        largest_shareholder_by_org = {}
        max_voting_holder_by_org = {}
        for organization, holders in shares_by_holder.items():
            if holders:
                holder, shares = max(holders.items(), key=lambda pair: (pair[1], pair[0]))
                code = account_issuers.get(holder)
                largest_shareholder_by_org[organization] = {
                    "id": holder,
                    "name": organization_names.get(code, code) if code else user_names.get(holder, holder),
                    "shares": str(shares),
                }
        for organization, holders in votes_by_holder.items():
            votes_total = votes_total_by_org.get(organization, Decimal(0))
            if holders and votes_total > 0:
                holder, votes = max(holders.items(), key=lambda pair: (pair[1], pair[0]))
                code = account_issuers.get(holder)
                max_voting_holder_by_org[organization] = {
                    "id": holder,
                    "name": organization_names.get(code, code) if code else user_names.get(holder, holder),
                    "percent": str(votes / votes_total * 100),
                }
        for organization, votes_by_company in votes_by_org.items():
            votes_total = votes_total_by_org.get(organization, Decimal(0))
            controlling = [(holder, votes / votes_total) for holder, votes in votes_by_company.items()
                           if votes_total and votes / votes_total > Decimal("0.5")]
            if controlling:
                holder, ratio = max(controlling, key=lambda pair: pair[1])
                parents_by_org[organization] = {"code": holder, "percent": float(ratio * 100)}

        # The ownership trace below is a pure function of these inputs and explodes
        # combinatorially with cross-holdings, so an unchanged ledger reuses the last result.
        effective_owners_by_org = _cached_effective_owners(
            shares_by_holder, votes_by_holder, equity_by_holder, equity_total_by_org, votes_total_by_org,
            share_supply_by_org, issuer_accounts, account_issuers, treasury_ids, organization_names, user_names)

        # One grouped query per table instead of three per company.
        holders = sorted({row["account"] for row in rows if row["account"]})
        holdings_by_holder, account_counts, loan_totals = {}, {}, {}
        for start in range(0, len(holders), 500):
            chunk = holders[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for h in conn.execute(
                    f"SELECT user_id,asset_code,SUM(quantity) quantity FROM user_assets WHERE user_id IN ({marks}) "
                    "GROUP BY user_id,asset_code ORDER BY user_id,asset_code", chunk):
                holdings_by_holder.setdefault(h[0], []).append({"asset_code": h[1], "quantity": h[2]})
            for h in conn.execute(
                    f"SELECT user_id,COUNT(*) FROM accounts WHERE user_id IN ({marks}) GROUP BY user_id", chunk):
                account_counts[h[0]] = h[1]
            for h in conn.execute(
                    f"SELECT user_id,COALESCE(SUM(outstanding_balance),0) FROM loans WHERE user_id IN ({marks}) "
                    "AND status='active' GROUP BY user_id", chunk):
                loan_totals[h[0]] = h[1]

        result = []
        for row in rows:
            item = dict(row)
            item["cash_base"] = item["cash"]
            item["base_currency"] = base_currency
            currency = legal_currencies.get(str(item["company_code"]), base_currency)
            try:
                cash = convert_cash(item["cash"], currency)
                locked = convert_cash(item["locked"], currency)
            except (ArithmeticError, InvalidOperation, TypeError, ValueError):
                # Keep the ledger currency visible if a live FX quote is unavailable.
                currency = base_currency
            else:
                item["cash"] = cash
                item["locked"] = locked
            item["currency"] = currency
            item["share_decimal_places"] = currency_precision(conn, item["quote_currency"] or item["currency"])
            holder = item["account"]
            item["holdings"] = holdings_by_holder.get(holder, []) if holder else []
            item["accounts"] = account_counts.get(holder, 0) if holder else 0
            item["loans"] = loan_totals.get(holder, 0) if holder else 0
            parent = parents_by_org.get(str(item["company_code"]))
            item["parents"] = [parent] if parent else []
            item["largest_shareholder"] = largest_shareholder_by_org.get(str(item["company_code"]))
            item["max_voting_holder"] = max_voting_holder_by_org.get(str(item["company_code"]))
            controller = effective_owners_by_org.get(str(item["company_code"]))
            item["ultimate_controller"] = dict(controller) if controller else controller
            result.append(item)
        return result
