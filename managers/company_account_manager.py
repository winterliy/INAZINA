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
PREFIX = "company:"


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
        # The issuer's cash is NWX, so the account lives in an NWX country:
        # the listing country when it uses NWX, otherwise 낙원.
        listing = conn.execute("SELECT e.nationality FROM market_exchanges e JOIN country_currencies c "
                               "ON c.nationality=e.nationality WHERE e.exchange_id=? AND c.currency_code='NWX'",
                               (company["exchange_id"],)).fetchone()
        nationality = listing[0] if listing else "낙원"
        conn.execute("INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)", (code,))
        cash = conn.execute("SELECT balance FROM issuer_cash_accounts WHERE company_code=?", (code,)).fetchone()[0]
        user_id = PREFIX + code  # non-decimal: can never be a Discord or web login
        conn.execute("INSERT INTO users(user_id,nickname,nationality,cash,status,role) VALUES (?,?,?,?,'active','company')",
                     (user_id, f"{company['name']} 법인", nationality, int(cash)))
        conn.execute("INSERT INTO company_accounts(company_code,user_id) VALUES (?,?)", (code, user_id))
        from managers.financial_manager import FinancialManager
        FinancialManager.ensure(conn, code)
        return user_id

    @staticmethod
    def _nwx(conn, amount, currency):
        """A native amount in ``currency`` valued in whole NWX."""
        from managers.currency_manager import CurrencyManager, SCALE
        currency = str(currency or "NWX").upper()
        if currency == "NWX" or not amount:
            return float(amount or 0)
        units = int(round(float(amount) * SCALE))
        sign = -1 if units < 0 else 1
        return sign * CurrencyManager.convert(conn, abs(units), currency, "NWX") / SCALE

    @staticmethod
    def _currency_of(conn, code):
        row = conn.execute(
            "SELECT quote_currency FROM company WHERE company_code=? UNION ALL "
            "SELECT quote_currency FROM investment WHERE product_code=? UNION ALL "
            "SELECT quote_currency FROM market_assets WHERE code=? UNION ALL "
            "SELECT quote_currency FROM treasury_bond_products WHERE product_code=? LIMIT 1",
            (code, code, code, code)).fetchone()
        return (row[0] if row else None) or "NWX"

    @classmethod
    def positions(cls, conn, account, issuer):
        """Cost basis of what the account holds and owes, in NWX.

        investments: securities at cost, deposits at balance, cash locked in orders
        and collateral; treasury: the company's own shares at cost; debt: loans.
        """
        investments = treasury = 0.0
        for lot in conn.execute("SELECT asset_code,quantity,average_cost FROM user_assets WHERE user_id=?", (account,)):
            value = cls._nwx(conn, float(lot["quantity"]) * float(lot["average_cost"] or 0), cls._currency_of(conn, lot["asset_code"]))
            if cls.own_shares(conn, account, lot["asset_code"]):
                treasury += value
            else:
                investments += value
        from managers.currency_manager import SCALE
        for row in conn.execute("SELECT balance_units,quote_currency FROM accounts WHERE user_id=?", (account,)):
            investments += cls._nwx(conn, int(row["balance_units"] or 0) / SCALE, row["quote_currency"])
        for row in conn.execute("SELECT reserved_cash,quote_currency FROM market_orders WHERE user_id=? "
                                "AND status IN ('scheduled','open','partial') AND reserved_cash>0", (account,)):
            investments += cls._nwx(conn, row["reserved_cash"], row["quote_currency"])
        for query in ("SELECT COALESCE(SUM(acquisition_cost),0) FROM treasury_bond_holdings WHERE user_id=? AND status='active'",
                      "SELECT COALESCE(SUM(collateral),0) FROM futures_positions WHERE user_id=?",
                      "SELECT COALESCE(SUM(amount),0) FROM option_collateral_cash_lots WHERE writer_id=?",
                      "SELECT COALESCE(SUM(principal),0) FROM els_subscriptions WHERE user_id=? AND status IN ('subscribed','issued')"):
            investments += float(conn.execute(query, (account,)).fetchone()[0] or 0)
        debt = sum(cls._nwx(conn, row["outstanding_balance"], row["quote_currency"]) for row in conn.execute(
            "SELECT outstanding_balance,quote_currency FROM loans WHERE user_id=? AND status IN ('active','defaulted')", (account,)))
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
        if entity["currency"] != "NWX":
            return 0
        account = conn.execute("SELECT user_id FROM company_accounts WHERE company_code=?", (issuer,)).fetchone()
        scale = 10 ** int(entity["decimal_places"])
        books = FinancialManager.balances(conn, issuer, 2**62)
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
            "SELECT c.company_code,c.name,c.exchange_id,c.total_shares,c.share_price,"
            "COALESCE(i.balance,0) cash,COALESCE(i.locked_amount,0) locked,a.user_id account "
            "FROM company c LEFT JOIN issuer_cash_accounts i ON i.company_code=c.company_code "
            "LEFT JOIN company_accounts a ON a.company_code=c.company_code "
            "WHERE c.status='active' AND COALESCE(c.share_class,'common')='common' ORDER BY c.company_code").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            holder = item["account"]
            item["holdings"] = [dict(h) for h in conn.execute(
                "SELECT asset_code,SUM(quantity) quantity FROM user_assets WHERE user_id=? GROUP BY asset_code "
                "ORDER BY asset_code", (holder,))] if holder else []
            item["accounts"] = conn.execute("SELECT COUNT(*) FROM accounts WHERE user_id=?", (holder,)).fetchone()[0] if holder else 0
            item["loans"] = conn.execute("SELECT COALESCE(SUM(outstanding_balance),0) FROM loans WHERE user_id=? "
                                         "AND status='active'", (holder,)).fetchone()[0] if holder else 0
            controlling = conn.execute(
                "SELECT a.company_code,SUM(u.quantity) quantity FROM user_assets u JOIN company_accounts a "
                "ON a.user_id=u.user_id WHERE u.asset_code=? GROUP BY a.company_code", (item["company_code"],)).fetchall()
            item["parents"] = [{"code": r["company_code"], "percent": float(r["quantity"]) / float(item["total_shares"]) * 100}
                               for r in controlling if item["total_shares"]]
            result.append(item)
        return result
