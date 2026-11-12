from functions.exact_money import ExactDecimal, native_value
from functions.amount_limits import MAX_NATIVE
from functions.money import base_currency_code
import random
import math
import time
from decimal import Decimal
from datetime import datetime, timezone
from database import DatabaseManager
from database.user_db import ACTIVE_OR_TREASURY, IS_TREASURY
from functions.tax_calc import calculate_income_tax
from functions.money import parse_money, round_money
from managers.treasury_bond_manager import TreasuryBondManager


class UserManager:
    """사용자의 매매·송금·도박·자산 조회를 담당합니다."""

    def __init__(self, db: DatabaseManager):
        self.db = db
        self.bond_manager = TreasuryBondManager(db)

    def _settle_user_interest(self, user_id):
        """Settle only when this user actually has an interest-bearing product."""
        user_id = str(user_id)
        has_savings = self.db.accounts.execute_one(
            "SELECT 1 FROM accounts WHERE user_id = ? AND account_type = 'saving' LIMIT 1",
            (user_id,),
        )
        has_loans = self.db.accounts.execute_one(
            "SELECT 1 FROM loans WHERE user_id = ? AND status = 'active' LIMIT 1",
            (user_id,),
        )
        if not has_savings and not has_loans:
            return None
        try:
            from managers.interest_manager import InterestManager

            InterestManager(self.db).process_user_interests(user_id)
            return None
        except Exception as exc:
            return f"❌ 이자 정산을 완료하지 못해 거래를 진행하지 않았습니다: {exc}"

    @staticmethod
    def _asset_row(conn, code):
        code_locations = DatabaseManager.code_locations(conn, code)
        if len(code_locations) > 1:
            raise ValueError(f"코드 `{code}`가 중복 등록되어 있습니다: {', '.join(code_locations)}. SQL Browser에서 중복을 정리해주세요.")
        matches = []
        row = conn.execute(
            "SELECT code, name, exchange_id, price, type, status, is_fixed, total_supply AS supply "
            ", quote_currency, Fractal FROM assets.market_assets WHERE code = ?", (code,)
        ).fetchone()
        if row:
            matches.append((dict(row), "market_assets"))
        row = conn.execute(
            "SELECT company_code AS code, name, exchange_id, share_price AS price, 'stock' AS type, "
            # A partial listing caps what the market can ever hold at listed_shares.
            "CASE WHEN status='active' AND trading_status='active' AND exchange_id IS NOT NULL THEN 'active' ELSE 'inactive' END AS status, "
            "is_fixed, COALESCE(listed_shares, total_shares) AS supply, quote_currency, Fractal "
            "FROM company.company WHERE company_code = ?", (code,)
        ).fetchone()
        if row:
            matches.append((dict(row), "company"))
        row = conn.execute(
            "SELECT product_code AS code, name, exchange_id, unit_price AS price, product_type AS type, "
            "status, is_fixed, total_supply AS supply, maturity_days, created_at, quote_currency, Fractal "
            "FROM product.investment WHERE product_code = ?", (code,)
        ).fetchone()
        if row:
            matches.append((dict(row), "investment"))
        row = conn.execute(
            "SELECT product_code AS code, name, exchange_id, market_price AS price, "
            "'bond' AS type, status, 1 AS is_fixed, NULL AS supply, face_value, "
            "annual_rate, maturity_days, total_supply, sold_amount, quote_currency,"
            "face_value_minor_units,total_supply_minor_units,sold_amount_minor_units "
            "FROM product.treasury_bond_products WHERE product_code = ?",
            (code,),
        ).fetchone()
        if row:
            matches.append((dict(row), "treasury_bond"))
        if len(matches) > 1:
            raise ValueError(f"자산 코드 `{code}`가 여러 테이블에 중복 등록되어 있습니다.")
        if matches:
            asset = matches[0][0]
            from functions.money import currency_precision
            currency = str(asset.get("quote_currency") or base_currency_code(conn))
            places = currency_precision(conn, currency)
            price = ExactDecimal(asset["price"] or 0)
            minimum = 1 / (10 ** places)
            asset["price"] = max(minimum, round_money(price, places)) if price > 0 else 0
        if matches and matches[0][1] == "investment":
            asset = matches[0][0]
            try:
                days = float(asset["maturity_days"] or 0)
                created = datetime.strptime(str(asset["created_at"]), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                ).timestamp()
                if asset['type']!='els' and asset["status"] == "active" and days > 0 and datetime.now(timezone.utc).timestamp() >= created + days * 86400:
                    asset["status"] = "matured"
            except (TypeError, ValueError, OverflowError):
                pass
        return matches[0] if matches else (None, None)

    @staticmethod
    def _choose_funds(conn, user_id, exclude_account=None, exclude_wallet=False):
        from database.fund_accounting import account
        fund = account(conn, user_id)
        if fund:
            return None if exclude_wallet else (fund['cash'], None)
        user = conn.execute(
            f"SELECT cash FROM users.users WHERE user_id = ? AND {ACTIVE_OR_TREASURY}", (user_id,)
        ).fetchone()
        if not user:
            return None
        if not exclude_wallet and conn.execute(f"SELECT 1 FROM users.users WHERE user_id=? AND {IS_TREASURY}", (str(user_id),)).fetchone():
            return (MAX_NATIVE, None)  # a treasury can always pay from its wallet
        from managers.currency_manager import CurrencyManager
        home = CurrencyManager.home(conn, user_id)
        sources = [] if exclude_wallet else [(int(round(user["cash"] or 0)), None)]
        sources.extend(
            (int(round(row["balance"] or 0)), row["account_number"])
            for row in conn.execute(
                "SELECT account_number, balance FROM accounts.accounts WHERE user_id = ? "
                "AND (? IS NULL OR account_number <> ?) AND managed_by IS NULL "
                "AND COALESCE(quote_currency, ?) = ?",
                (user_id, exclude_account, exclude_account, home, home),
            ).fetchall()
        )
        return max(sources, key=lambda source: source[0]) if sources else None

    @staticmethod
    def _change_cash(conn, user_id, amount, *, maximum=None):
        """Apply a cash delta, optionally enforcing an upper balance limit."""
        from database.fund_accounting import account, change_cash
        fund = account(conn, user_id)
        if fund:
            if fund['cash'] + amount < 0 or (maximum is not None and fund['cash'] + amount > maximum):
                return False
            change_cash(conn, user_id, amount)
            return True
        amount = int(amount)
        if amount < 0:
            cursor = conn.execute(
                f"UPDATE users.users SET cash = cash + ? WHERE user_id = ? AND (cash >= ? OR {IS_TREASURY})",
                (amount, str(user_id), -amount),
            )
        elif maximum is None:
            cursor = conn.execute(
                "UPDATE users.users SET cash = cash + ? WHERE user_id = ?",
                (amount, str(user_id)),
            )
        else:
            cursor = conn.execute(
                "UPDATE users.users SET cash = cash + ? WHERE user_id = ? AND cash <= ?",
                (amount, str(user_id), int(maximum) - amount),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _debit_source(conn, user_id, account_number, amount, currency=None):
        from database.fund_accounting import account, change_cash
        fund = account(conn, user_id)
        if fund:
            if account_number is not None or amount < 0 or fund['cash'] < amount:
                return False
            change_cash(conn, user_id, -amount, 'order_reserve')
            return True
        from functions.money import currency_precision, to_minor_units
        from managers.currency_manager import CurrencyManager, SCALE
        if account_number is None:
            currency = currency or CurrencyManager.home(conn, user_id)
            minor = to_minor_units(amount, currency_precision(conn, currency))
            cursor = conn.execute(
                "UPDATE users.users SET cash_minor = cash_minor - ? "
                f"WHERE user_id = ? AND {ACTIVE_OR_TREASURY} AND (cash_minor >= ? OR {IS_TREASURY})",
                (minor, user_id, minor),
            )
        else:
            row = conn.execute("SELECT quote_currency,account_type FROM accounts.accounts "
                               "WHERE account_number=? AND user_id=?", (account_number,user_id)).fetchone()
            currency = currency or (row['quote_currency'] if row else None) or CurrencyManager.home(conn,user_id)
            places = currency_precision(conn, currency)
            minor = to_minor_units(amount, places)
            quantum = SCALE // (10 ** places)
            now = time.time()
            stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            cursor = conn.execute(
                "UPDATE accounts.accounts SET balance_minor_units=balance_minor_units-?, "
                "balance_units=balance_units-?, principal = MAX(0, COALESCE(principal, 0) - ?), "
                "last_payment_epoch = ?, last_payment = ? "
                "WHERE account_number = ? AND user_id = ? AND balance_minor_units >= ?",
                (minor, minor*quantum, ExactDecimal(amount), now, stamp, account_number, user_id, minor),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _credit_source(conn, user_id, account_number, amount, *, event='settlement', reference=None,
                       currency=None):
        if amount <= 0:
            return
        from database.fund_accounting import account, change_cash
        if account(conn, user_id):
            change_cash(conn, user_id, amount, event, reference)
            return
        from functions.money import currency_precision, to_minor_units
        from managers.currency_manager import CurrencyManager, SCALE
        if account_number is None:
            currency = currency or CurrencyManager.home(conn, user_id)
            minor = to_minor_units(amount, currency_precision(conn, currency))
            conn.execute("UPDATE users.users SET cash_minor = cash_minor + ? WHERE user_id = ?", (minor, user_id))
        else:
            row = conn.execute("SELECT quote_currency FROM accounts.accounts "
                               "WHERE account_number=? AND user_id=?", (account_number,user_id)).fetchone()
            currency = currency or (row['quote_currency'] if row else None) or CurrencyManager.home(conn,user_id)
            places = currency_precision(conn, currency)
            minor = to_minor_units(amount, places)
            quantum = SCALE // (10 ** places)
            now = time.time()
            stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE accounts.accounts SET balance_minor_units=balance_minor_units+?, "
                "balance_units=balance_units+?, "
                "principal = COALESCE(principal, 0) + ?, last_payment_epoch = ?, "
                "last_payment = ? WHERE account_number = ? AND user_id = ?",
                (minor, minor*quantum, ExactDecimal(amount), now, stamp, account_number, user_id),
            )

    @staticmethod
    def _tax(conn, user_id, source, gross_amount, currency=None):
        """양수 소득에 시민 국적의 세율을 적용하고 내역을 기록합니다.

        ``currency``: the income's currency when it is not the citizen's own."""
        from database.fund_accounting import account
        if account(conn, user_id):
            return 0  # Fund income stays in NAV; investor gains are taxed on redemption.
        income_currency_was_supplied = currency is not None
        tax_currency = str(currency or base_currency_code(conn)).upper()
        from functions.money import currency_precision, round_currency
        places = currency_precision(conn, tax_currency)
        gross = max(0, round_currency(conn, gross_amount, tax_currency))
        if gross == 0:
            return 0
        source_class = str(source or '').split(':', 1)[0].strip().lower()
        row = conn.execute("SELECT COALESCE((SELECT r.tax_percent FROM country_tax_rules r "
                           "WHERE r.nationality=u.nationality AND r.income_source=?),c.tax_percent) tax_percent "
                           "FROM users u "
                           "LEFT JOIN country_currencies c ON c.nationality=u.nationality "
                           "WHERE u.user_id=?", (source_class,str(user_id))).fetchone()
        fallback = conn.execute("SELECT value FROM settings WHERE key='tax_percent'").fetchone()
        try:
            rate = ExactDecimal(row["tax_percent"] if row and row["tax_percent"] is not None
                         else fallback["value"] if fallback else 3.0)
        except (TypeError, ValueError):
            rate = 3.0
        if not 0 <= rate <= 100:
            rate = 3.0
        tax_amount = calculate_income_tax(gross, rate / 100.0, places)
        tax_amount = round_currency(conn, tax_amount, tax_currency)
        net_income = max(0, round_currency(conn, gross - tax_amount, tax_currency))
        treasury_id = None
        wallet_watermark = None
        if tax_amount > 0:
            from managers.currency_manager import CurrencyManager
            treasury_id = CurrencyManager.user_treasury_id(conn, user_id)
            wallet_watermark = int(conn.execute(
                "SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger"
            ).fetchone()[0])
            if income_currency_was_supplied and tax_currency != CurrencyManager.home(conn, user_id):
                from managers.currency_manager import SCALE
                CurrencyManager.treasury_receive(
                    conn, treasury_id, tax_currency,
                    int(Decimal(str(tax_amount))*SCALE),
                )
            else:
                treasury = conn.execute(
                    "UPDATE users.users SET cash = cash + ? WHERE user_id = ?",
                    (tax_amount, treasury_id),
                )
                if treasury.rowcount != 1:
                    raise RuntimeError("국고 계정을 찾을 수 없어 세금 처리를 취소했습니다.")
        inserted = conn.execute(
            "INSERT INTO tax_transactions(user_id,source,gross_amount,tax_amount,net_amount,currency_code) "
            "VALUES (?,?,?,?,?,?)",
            (str(user_id), str(source), gross, tax_amount, net_income, tax_currency),
        )
        if tax_amount > 0:
            from managers.accounting_journal import AccountingJournal
            AccountingJournal.link_tax_transaction_wallet_entries(
                conn, inserted.lastrowid, wallet_watermark, treasury_id
            )
        return tax_amount

    @staticmethod
    def _refresh_max_shareholder(conn, asset_code):
        asset_code = str(asset_code)
        dr_program = conn.execute(
            "SELECT underlying_code FROM assets.depositary_receipts WHERE dr_code=?",
            (asset_code,),
        ).fetchone()
        if dr_program:
            asset_code = str(dr_program["underlying_code"])
        conn.execute(
            """UPDATE company.company SET max_shareholder_id = (
                   SELECT user_id FROM (
                       SELECT user_id, quantity FROM assets.user_assets WHERE asset_code = ?
                       UNION ALL
                       SELECT writer_id AS user_id, quantity
                       FROM assets.option_collateral_lots WHERE underlying_code = ?
                       UNION ALL
                       SELECT h.user_id,d.underlying_per_receipt*h.quantity AS quantity
                       FROM assets.user_assets h JOIN assets.depositary_receipts d
                         ON d.dr_code=h.asset_code WHERE d.underlying_code=?
                   )
                   -- Treasury stock (the company's own shares) carries no vote.
                   WHERE user_id <> 'company:' || ?
                   GROUP BY user_id
                   ORDER BY SUM(quantity) DESC, user_id ASC
                   LIMIT 1
               ) WHERE company_code = ?""",
            (asset_code, asset_code, asset_code, asset_code, asset_code),
        )

    def buy_asset(self, user_id, asset_code, quantity):
        from managers.fund_manager import FundManager
        if FundManager(self.db).is_backed(asset_code):
            return FundManager(self.db).trade_units(user_id, asset_code, quantity)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return False, "❌ 매수 수량은 0보다 큰 정수로 입력해주세요."
        product=self.db.products.get_investment(asset_code)
        if product and product['product_type']=='els':
            from managers.els_manager import ELSManager
            return ELSManager(self.db,self).subscribe(user_id,asset_code,quantity)
        interest_error = self._settle_user_interest(user_id)
        if interest_error:
            return False, interest_error
        try:
            with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
                from managers.market_session_manager import MarketSessionManager
                error = MarketSessionManager(self.db).trading_error(conn=conn, asset_code=asset_code)
                if error:
                    return False, error
                asset, source = self._asset_row(conn, asset_code)
                if not asset:
                    return False, "❌ 존재하지 않는 자산 코드입니다."
                from managers.currency_manager import CurrencyManager, SCALE
                currency = asset.get("quote_currency") or base_currency_code(conn)
                if currency != CurrencyManager.home(conn, user_id):
                    return False, "❌ 자국 통화가 아닌 종목은 해당 외환통장을 개설하고 거래소 지정가 매수·매도로 거래해주세요."
                if asset["type"] == "stock_future":
                    return False, "❌ 주식 선물은 거래소의 지정가 매수·매도로 거래해주세요."
                if asset["status"] != "active":
                    return False, "❌ 현재 거래가 중지된 자산입니다."
                from functions.money import currency_precision, from_minor_units, to_minor_units
                places = currency_precision(conn, currency)
                price_units = to_minor_units(asset["price"] or 0, places)
                if price_units <= 0:
                    return False, "❌ 자산 가격이 올바르게 등록되지 않았습니다."
                price = from_minor_units(price_units, places)
                supply = asset["supply"]
                if supply is not None:
                    if int(supply) < 0:
                        return False, "❌ 자산의 발행 수량이 올바르게 설정되지 않았습니다."
                    issued = conn.execute(
                        "SELECT (SELECT COALESCE(SUM(quantity), 0) FROM assets.user_assets "
                        "WHERE asset_code = ?) + (SELECT COALESCE(SUM(quantity), 0) "
                        "FROM assets.option_collateral_lots WHERE underlying_code = ?) AS quantity",
                        (asset_code, asset_code),
                    ).fetchone()["quantity"]
                    if int(issued) + quantity > int(supply):
                        return False, f"❌ 매수 가능 수량을 초과했습니다. (잔여 {max(0, int(supply)-int(issued)):,}주)"
                total_cost_units = price_units * quantity
                total_cost = from_minor_units(total_cost_units, places)
                funds = self._choose_funds(conn, user_id)
                if funds is None:
                    return False, "❌ 등록된 사용자만 거래할 수 있습니다."
                balance, account_number = funds
                if balance < ExactDecimal(total_cost) or not self._debit_source(
                    conn, user_id, account_number, ExactDecimal(total_cost), currency=currency
                ):
                    return False, f"❌ 잔액이 부족합니다. (필요 금액: {total_cost:,} {currency})"
                from managers.asset_cost_manager import AssetCostManager
                AssetCostManager.upsert(
                    conn, user_id, asset_code, account_number, quantity,
                    from_minor_units(price_units, places), schema="assets",
                )
                conn.execute(
                    "INSERT INTO assets.market_trade_history "
                    "(asset_code, side, quantity, price, recorded_epoch) "
                    "VALUES (?, 'buy', ?, ?, ?)",
                    (asset_code, quantity, ExactDecimal(price), int(time.time())),
                )
                if asset["type"] in {"mutual_fund", "index_fund", "corporate_bond"}:
                    product = conn.execute("SELECT issuer_code FROM product.investment WHERE product_code = ?", (asset_code,)).fetchone()
                    if asset["type"] == "corporate_bond":
                        conn.execute("UPDATE company.company SET capital = capital + ? WHERE company_code = ?", (ExactDecimal(total_cost), product["issuer_code"]))
                    else:
                        # The listing country's treasury is the counterparty.
                        CurrencyManager.treasury_receive(
                        conn, CurrencyManager.exchange_treasury(conn, asset_code), currency,
                        total_cost_units * SCALE // (10 ** places))
                if source == "company":
                    self._refresh_max_shareholder(conn, asset_code)
                unit = "주" if str(asset["type"]).lower() in {"stock", "주식"} else "좌" if asset["type"] in {"mutual_fund", "index_fund"} else "개"
            return True, f"✅ {asset['name']} {quantity:,}{unit}를 {total_cost:,} {currency}에 매수했습니다."
        except ValueError as exc:
            return False, f"❌ {exc}"
        except Exception as exc:
            return False, f"❌ 매수 처리 중 오류가 발생했습니다: {exc}"

    def sell_asset(self, user_id, asset_code, quantity):
        from managers.fund_manager import FundManager
        if FundManager(self.db).is_backed(asset_code):
            return FundManager(self.db).trade_units(user_id, asset_code, quantity, redeem=True)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return False, "❌ 매도 수량은 0보다 큰 정수로 입력해주세요."
        product=self.db.products.get_investment(asset_code)
        if product and product['product_type']=='els':
            return False,'❌ ELS는 중도 매도·환매가 불가능하며 계약 조건에 따라 자동 상환됩니다.'
        interest_error = self._settle_user_interest(user_id)
        if interest_error:
            return False, interest_error
        try:
            with self.db.atomic(("users", "accounts", "assets", "product", "company", "tax")) as conn:
                from managers.market_session_manager import MarketSessionManager
                error = MarketSessionManager(self.db).trading_error(conn=conn, asset_code=asset_code)
                if error:
                    return False, error
                asset, source = self._asset_row(conn, asset_code)
                if not asset:
                    return False, "❌ 존재하지 않는 자산 코드입니다."
                from managers.currency_manager import CurrencyManager, SCALE
                currency = asset.get("quote_currency") or base_currency_code(conn)
                if currency != CurrencyManager.home(conn, user_id):
                    return False, "❌ 자국 통화가 아닌 종목은 거래소 지정가 매수·매도로 거래해주세요."
                if asset["status"] not in ("active", "matured"):
                    return False, "❌ 현재 거래가 중지된 자산입니다."
                if asset["type"] in {"stock_future", "corporate_bond"}:
                    return False, "❌ 선물·회사채는 거래소 지정가 매수·매도로 거래해주세요."
                from functions.money import currency_precision, from_minor_units, to_minor_units
                places = currency_precision(conn, currency)
                price_minor = to_minor_units(asset["price"] or 0, places)
                price = from_minor_units(price_minor, places)
                positions = conn.execute(
                    "SELECT holding_id, account_number, quantity, average_cost,"
                    "average_cost_minor_units "
                    "FROM assets.user_assets WHERE user_id = ? AND asset_code = ? "
                    "ORDER BY holding_id", (user_id, asset_code)
                ).fetchall()
                reserved = conn.execute(
                    "SELECT COALESCE(SUM(remaining_quantity), 0) AS quantity "
                    "FROM assets.market_orders WHERE user_id = ? AND asset_code = ? "
                    "AND side = 'sell' AND status IN ('open', 'partial')",
                    (str(user_id), asset_code),
                ).fetchone()
                delegated = 0
                if str(asset.get("type") or "") == "depositary_receipt":
                    delegated = int(conn.execute(
                        "SELECT COALESCE(SUM(dr_quantity),0) FROM assets.dr_voting_delegations "
                        "WHERE owner_user_id=? AND dr_code=? AND status='active'",
                        (str(user_id), str(asset_code)),
                    ).fetchone()[0] or 0)
                custody_reserved = int(conn.execute(
                    "SELECT COALESCE(SUM(r.reserved_quantity),0) FROM assets.dr_custody_reservations r "
                    "JOIN assets.dr_custody_instructions i USING(reference_key) "
                    "WHERE r.user_id=? AND r.reserved_asset_code=? AND r.released_epoch IS NULL "
                    "AND i.status IN ('requested','verified')", (str(user_id), str(asset_code)),
                ).fetchone()[0] or 0)
                available = (
                    sum(int(row["quantity"]) for row in positions)
                    - int(reserved["quantity"] or 0)
                    - delegated
                    - custody_reserved
                )
                if available < quantity:
                    return False, f"❌ 보유 수량이 부족합니다. (보유 {available:,}개)"

                left = quantity
                sold = []
                taxable_gain = 0
                for row in positions:
                    take = min(left, int(row["quantity"]))
                    if take <= 0:
                        continue
                    from managers.asset_cost_manager import AssetCostManager
                    basis_minor = AssetCostManager.sale_cost_minor(conn, row['holding_id'], take)
                    taxable_gain += max(0, price_minor * take - basis_minor)
                    sold.append((row["holding_id"], row["account_number"], take, int(row["quantity"])))
                    left -= take
                    if left == 0:
                        break

                gross_proceeds_minor = price_minor * quantity
                gross_proceeds = from_minor_units(gross_proceeds_minor, places)
                if asset["type"] in {"mutual_fund", "index_fund"}:
                    CurrencyManager.treasury_pay(
                        conn, CurrencyManager.exchange_treasury(conn, asset_code), currency,
                        gross_proceeds_minor * SCALE // (10 ** places))
                total_tax = self._tax(
                    conn, user_id, "stock_sale_gain",
                    from_minor_units(AssetCostManager.rounded_minor(taxable_gain), places), currency=currency,
                )
                total_tax_units = to_minor_units(total_tax, places)
                tax_left_units = total_tax_units
                qty_left = quantity
                for holding_id, account_number, take, old_quantity in sold:
                    share_tax_units = (tax_left_units if take == qty_left else
                                       total_tax_units * take // quantity)
                    net_proceeds_units = max(0, price_minor*take-share_tax_units)
                    if take == old_quantity:
                        cursor = conn.execute(
                            "DELETE FROM assets.user_assets WHERE holding_id = ? AND quantity = ?",
                            (holding_id, old_quantity),
                        )
                    else:
                        cursor = conn.execute(
                            "UPDATE assets.user_assets SET quantity = quantity - ? "
                            "WHERE holding_id = ? AND quantity > ?",
                            (take, holding_id, take),
                        )
                    if cursor.rowcount != 1:
                        raise RuntimeError("보유 수량이 변경되어 매도를 취소했습니다.")
                    self._credit_source(
                        conn, user_id, account_number,
                        ExactDecimal(from_minor_units(net_proceeds_units, places)), currency=currency,
                    )
                    tax_left_units -= share_tax_units
                    qty_left -= take
                if source == "company":
                    self._refresh_max_shareholder(conn, asset_code)
                conn.execute(
                    "INSERT INTO assets.market_trade_history "
                    "(asset_code, side, quantity, price, recorded_epoch) "
                    "VALUES (?, 'sell', ?, ?, ?)",
                    (asset_code, quantity, ExactDecimal(price), int(time.time())),
                )
            net_proceeds = from_minor_units(
                max(0, gross_proceeds_minor-total_tax_units), places
            )
            unit = "주" if str(asset["type"]).lower() in {"stock", "주식"} else "좌" if asset["type"] in {"mutual_fund", "index_fund"} else "개"
            return True, (
                f"✅ {asset['name']} {quantity:,}{unit}를 {gross_proceeds:,} {currency}에 매도했습니다.\n"
                f"실현이익 과세: {total_tax:,} {currency} · 계좌 입금: {net_proceeds:,} {currency}"
            )
        except ValueError as exc:
            return False, f"❌ {exc}"
        except Exception as exc:
            return False, f"❌ 매도 처리 중 오류가 발생했습니다: {exc}"

    def transfer(self, from_user_id, recipient, amount, source_account=None):
        # Cash is denominated by nationality, so even wallet-to-wallet transfers
        # may require conversion.  Keep every endpoint on the currency-aware path.
        try:
            from managers.currency_exchange_manager import CurrencyExchangeManager
            return True,CurrencyExchangeManager(self.db).transfer(
                from_user_id,recipient,amount,source_account
            )
        except ValueError as exc:
            return False,f'❌ {exc}'

    def gamble(self, user_id, amount, source=None):
        try:
            amount = parse_money(amount, minimum=1)
        except ValueError as exc:
            return False, f"❌ 도박 금액은 정수 기준 통화로 입력해주세요. ({exc})"
        interest_error = self._settle_user_interest(user_id)
        if interest_error:
            return False, interest_error
        try:
            with self.db.atomic(("users", "accounts", "pot", "tax")) as conn:
                from managers.game_funding import game_funds
                funds = game_funds(conn, str(user_id), source)
                if not funds:
                    return False, "❌ 등록된 사용자만 이용할 수 있습니다."
                balance, account_number = funds
                if balance < amount:
                    return False, "❌ 잔액이 부족합니다."
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = 'gamble_chance'"
                ).fetchone()
                try:
                    chance = ExactDecimal(row["value"] if row else 50)
                except (TypeError, ValueError):
                    chance = 50.0
                if not 0 <= chance <= 100:
                    chance = 50.0
                return_row = conn.execute(
                    "SELECT value FROM settings WHERE key = 'gamble_return'"
                ).fetchone()
                try:
                    return_percent = ExactDecimal(return_row["value"] if return_row else 100)
                except (TypeError, ValueError):
                    return_percent = 100.0
                if not math.isfinite(return_percent) or return_percent < 0:
                    return_percent = 100.0
                payout_before_tax = amount * return_percent / 100.0
                if not math.isfinite(payout_before_tax):
                    return False, "❌ 설정된 당첨 지급률로 상금을 계산할 수 없습니다."
                payout_before_tax = round_money(payout_before_tax)
                won = random.random() * 100 < chance
                if won:
                    tax = self._tax(conn, str(user_id), "gambling_winnings", payout_before_tax)
                    self._credit_source(conn, str(user_id), account_number, payout_before_tax - tax)
                    from managers.currency_manager import CurrencyManager
                    treasury = conn.execute(
                        "UPDATE users.users SET cash = cash - ? WHERE user_id = ?",
                        (payout_before_tax, CurrencyManager.user_treasury_id(conn, str(user_id))),
                    )
                    if treasury.rowcount != 1:
                        raise RuntimeError("국고 계정을 찾을 수 없어 당첨금 지급을 취소했습니다.")
                else:
                    if not self._debit_source(conn, str(user_id), account_number, amount):
                        return False, "❌ 잔액이 부족합니다."
                    from managers.currency_manager import CurrencyManager
                    treasury = conn.execute(
                        "UPDATE users.users SET cash = cash + ? WHERE user_id = ?",
                        (amount, CurrencyManager.user_treasury_id(conn, str(user_id))),
                    )
                    if treasury.rowcount != 1:
                        raise RuntimeError("국고 계정을 찾을 수 없어 도박 처리를 취소했습니다.")
                    tax = 0
            if won:
                net_payout = max(0, payout_before_tax - tax)
                return True, (
                    f"🎉 당첨! 지급률 {return_percent:,.2f}% · "
                    f"상금 {payout_before_tax:,} 기준 통화 · 세금 {tax:,} 기준 통화 · "
                    f"실수령 {net_payout:,} 기준 통화 (원금 별도, 국고 지급)"
                )
            return False, f"💀 꽝! {amount:,} 기준 통화를 잃어 국고에 입금했습니다."
        except Exception as exc:
            return False, f"❌ 도박 처리 중 오류가 발생했습니다: {exc}"

    def get_total_assets(self, user_id, settle=True):
        """Return the asset-page breakdown; optionally avoid settling balances.

        User-facing asset pages keep the default settlement behavior. Notification-
        only workflows can pass ``settle=False`` to take a read-only snapshot.
        """
        if settle:
            interest_error = self._settle_user_interest(user_id)
            if interest_error:
                raise RuntimeError(interest_error)
            self.bond_manager.redeem_matured(user_id=user_id)
            # Currency-account interest is also a mutation. Finish it before
            # opening the portfolio's consistent read snapshot.
            from managers.currency_exchange_manager import CurrencyExchangeManager
            CurrencyExchangeManager(self.db).accounts(user_id)
        with self.db.readonly() as conn:
            return self._get_total_assets_snapshot(user_id, conn)

    def _get_total_assets_snapshot(self, user_id, conn):
        """Build every portfolio component from one WAL read snapshot."""
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),)).fetchone()
        cash = native_value(user["cash"] or 0) if user else 0
        nationality = str(user['nationality'] or '미설정') if user else '미설정'
        # Load the whole tradable-code catalog once.  The previous lazy lookup
        # performed up to ten SQL statements for every distinct portfolio code.
        asset_cache = self._portfolio_asset_catalog(user_id, conn=conn)
        def wallet_asset(code):
            return asset_cache.get(str(code))
        quote_overrides = {str(row['asset_code']):int(row['price']) for row in
            conn.execute('SELECT asset_code,price FROM market_quote_overrides')}
        currency_rates = {base_currency_code(conn):1}
        for row in conn.execute("SELECT c.code,a.price FROM currencies c LEFT JOIN market_assets a "
                                      "ON a.code=c.asset_code WHERE c.status='active'"):
            if row['code']==base_currency_code(conn):
                continue
            if row['price'] is not None and ExactDecimal(row['price'])>0:
                currency_rates[str(row['code'])]=int(round(ExactDecimal(row['price'])))
        currency_row = conn.execute("SELECT n.currency_code FROM country_currencies n "
            "JOIN currencies c ON c.code=n.currency_code WHERE n.nationality=? AND c.status='active'",(nationality,))
        currency_row = currency_row.fetchone()
        national_currency = currency_row['currency_code'] if currency_row else base_currency_code(conn)
        from managers.currency_manager import CurrencyManager, CurrencyReadSnapshot, SCALE
        currency_snapshot = CurrencyReadSnapshot(conn)
        wallets = CurrencyManager(self.db).wallets(
            user_id, conn=conn, snapshot=currency_snapshot
        ) if user else []
        kyc_row = conn.execute(
            "SELECT status,risk_tier,expires_epoch FROM kyc_profiles WHERE user_id=?",
            (str(user_id),),
        ).fetchone() if user else None
        kyc_profile = None
        if kyc_row:
            kyc_status = str(kyc_row["status"])
            expires_epoch = None
            if kyc_row["expires_epoch"] is not None:
                from functions.time_utils import validate_epoch
                try:
                    expires_epoch = validate_epoch(kyc_row["expires_epoch"])
                except ValueError:
                    # Corrupt expiry data must never be displayed as verified.
                    if kyc_status == "verified":
                        kyc_status = "expired"
                else:
                    if kyc_status == "verified" and expires_epoch <= time.time():
                        kyc_status = "expired"
            kyc_profile = {
                "status": kyc_status,
                "risk_tier": str(kyc_row["risk_tier"]),
                "expires_epoch": expires_epoch,
            }
        home_cash_units = int(ExactDecimal(cash) * SCALE)
        foreign_cash_units = sum(
            int(row['value_home_units']) for row in wallets if row['is_foreign']
        )
        home_decimal_places = currency_snapshot.precision(national_currency)
        home_rate_nwx = currency_snapshot.rate_nwx(national_currency)
        cash_value_nwx = currency_snapshot.convert(
            home_cash_units, national_currency, base_currency_code(conn)
        ) // SCALE if user else 0
        account_details = []
        total_account_balance = 0
        account_rows = conn.execute(
            "SELECT a.account_number,a.product_code,a.balance,a.account_type,a.quote_currency,"
            "a.rate_type,a.contract_rate,a.next_repricing_epoch,a.benchmark_key,"
            "a.benchmark_tenor_days,a.resolution_status,a.repricing_lag_seconds,"
            "a.insurance_paid,a.uninsured_claim,a.managed_by,"
            "COALESCE(s.name,l.name) AS product_name "
            "FROM accounts a LEFT JOIN saving s ON s.product_code=a.product_code "
            "LEFT JOIN liability l ON l.product_code=a.product_code "
            "WHERE a.user_id=? AND NOT (a.account_type='saving' AND a.quote_currency IS NOT NULL AND a.quote_currency<>?)",
            (str(user_id),national_currency),
        ).fetchall()
        account_total_home_units = 0
        account_numbers = [str(row["account_number"]) for row in account_rows]
        rate_resets_by_account = {}
        if account_numbers:
            placeholders = ",".join("?" for _ in account_numbers)
            reset_rows = conn.execute(
                "SELECT contract_id,old_rate,new_rate,scheduled_epoch,applied_epoch,"
                "benchmark_key,benchmark_tenor_days FROM ("
                "SELECT contract_id,old_rate,new_rate,scheduled_epoch,applied_epoch,"
                "benchmark_key,benchmark_tenor_days,ROW_NUMBER() OVER ("
                "PARTITION BY contract_id ORDER BY applied_epoch DESC,reset_id DESC) reset_rank "
                "FROM accounts.contract_rate_reset_history WHERE contract_type='account' "
                f"AND contract_id IN ({placeholders})) WHERE reset_rank<=3 "
                "ORDER BY contract_id,reset_rank",
                account_numbers,
            ).fetchall()
            for reset in reset_rows:
                rate_resets_by_account.setdefault(reset["contract_id"], []).append(
                    {key: reset[key] for key in (
                        "old_rate", "new_rate", "scheduled_epoch", "applied_epoch",
                        "benchmark_key", "benchmark_tenor_days",
                    )}
                )
        for account in account_rows:
            account_currency = str(account["quote_currency"] or national_currency)
            account_places = currency_snapshot.precision(account_currency)
            balance = round_money(account["balance"] or 0, account_places)
            balance_units = int(Decimal(str(balance)) * SCALE)
            converted_units = currency_snapshot.convert(
                abs(balance_units), account_currency, national_currency
            )
            account_total_home_units += -converted_units if balance_units < 0 else converted_units
            product_name = account["product_name"] or account["product_code"]
            account_details.append({
                "number": account["account_number"],
                "account_type": account["account_type"],
                "managed_by": account["managed_by"],
                "product_name": (f"대출금 지급 · {product_name}"
                                 if account["account_type"] == "loan_payout" else product_name),
                "balance": balance,
                "currency_code": account_currency,
                "decimal_places": account_places,
                "resolution_status": account["resolution_status"],
                "rate_type": account["rate_type"],
                "contract_rate": account["contract_rate"],
                "next_repricing_epoch": account["next_repricing_epoch"],
                "benchmark_key": account["benchmark_key"],
                "benchmark_tenor_days": int(account["benchmark_tenor_days"] or 0),
                "repricing_lag_seconds": int(account["repricing_lag_seconds"] or 0),
                "rate_resets": rate_resets_by_account.get(str(account["account_number"]), []),
                "insurance_paid": round_money(account["insurance_paid"] or 0, account_places),
                "uninsured_claim": round_money(account["uninsured_claim"] or 0, account_places),
            })
        total_account_balance = round_money(
            Decimal(account_total_home_units) / Decimal(SCALE), home_decimal_places
        )

        deposit_insurance_claims = []
        from functions.money import from_minor_units
        for claim in conn.execute(
            "SELECT c.claim_id,c.issuer,c.account_number,c.covered_amount,"
            "c.uninsured_amount,c.payout_currency,c.payout_amount,"
            "c.payout_amount_minor_units,c.payout_decimal_places,c.resolved_epoch,a.quote_currency "
            "FROM deposit_insurance_claims c LEFT JOIN accounts a "
            "ON a.account_number=c.account_number WHERE c.user_id=? "
            "ORDER BY c.resolved_epoch DESC,c.claim_id DESC LIMIT 100",
            (str(user_id),),
        ).fetchall():
            currency = str(claim["quote_currency"] or national_currency)
            deposit_insurance_claims.append({
                "claim_id": int(claim["claim_id"]),
                "issuer": str(claim["issuer"]),
                "currency_code": currency,
                "decimal_places": currency_snapshot.precision(currency),
                "covered_amount": ExactDecimal(claim["covered_amount"] or 0),
                "uninsured_amount": ExactDecimal(claim["uninsured_amount"] or 0),
                "payout_currency": (str(claim["payout_currency"])
                                    if claim["payout_currency"] else None),
                "payout_decimal_places": (currency_snapshot.precision(
                    str(claim["payout_currency"])
                ) if claim["payout_currency"] else None),
                "payout_amount": (ExactDecimal(from_minor_units(
                    claim["payout_amount_minor_units"], claim["payout_decimal_places"]
                )) if claim["payout_amount_minor_units"] is not None else
                    (ExactDecimal(claim["payout_amount"])
                     if claim["payout_amount"] is not None else None)),
                "resolved_epoch": float(claim["resolved_epoch"] or 0),
            })

        stock_details = []
        total_stock_value = 0
        sell_reservations = {
            str(row["asset_code"]): float(row["quantity"] or 0)
            for row in conn.execute(
                "SELECT asset_code, SUM(remaining_quantity) AS quantity "
                "FROM market_orders WHERE order_kind = 'asset' AND user_id = ? AND side = 'sell' "
                "AND status IN ('open', 'partial') GROUP BY asset_code",
                (str(user_id),),
            )
        }
        for position in conn.execute(
            "SELECT asset_code,SUM(quantity) AS quantity FROM user_assets "
            "WHERE user_id=? GROUP BY asset_code ORDER BY asset_code", (str(user_id),)
        ):
            code, quantity = position["asset_code"], float(position["quantity"])
            asset = wallet_asset(code)
            if asset and asset.get('type')=='els':
                continue
            price = int(round(asset["price"] or 0)) if asset else 0
            currency = asset.get('quote_currency',base_currency_code(conn)) if asset else base_currency_code(conn)
            rate = currency_rates.get(currency,1)
            value = price * quantity * rate
            total_stock_value += value
            stock_details.append({
                "name": asset["name"] if asset else "확인 필요",
                "code": code, "qty": quantity, "price": price, "value": value,
                "type": asset.get("type") if asset else None,
                "quote_currency":currency,
                "sell_reserved_qty": min(quantity, sell_reservations.get(str(code), 0)),
            })

        # Long option positions are marked at the latest premium trade, or at
        # their Black-Scholes reference premium before the first trade.
        from managers.option_manager import OptionManager

        option_rows = conn.execute(
            "SELECT p.contract_id, p.quantity, p.average_premium, c.contract_code, "
            "c.option_type, c.underlying_code, c.strike_price, c.expiration_epoch, "
            "c.implied_volatility FROM option_positions p JOIN option_contracts c "
            "ON c.contract_id = p.contract_id WHERE p.user_id = ? AND c.status = 'active'",
            (str(user_id),),
        )
        option_sell_reservations = {
            int(row["contract_id"]): int(row["quantity"] or 0)
            for row in conn.execute(
                "SELECT c.contract_id, SUM(o.remaining_quantity) AS quantity "
                "FROM option_contracts c JOIN market_orders o ON o.asset_code = c.contract_code "
                "WHERE o.order_kind = 'option' AND o.option_order_type = 'resale' "
                "AND o.user_id = ? AND o.side = 'sell' AND o.status IN ('open', 'partial') "
                "GROUP BY c.contract_id",
                (str(user_id),),
            )
        }
        now = int(time.time())
        for position in option_rows:
            underlying = wallet_asset(position["underlying_code"])
            from managers.rate_manager import RateManager
            base_rate = RateManager.for_exchange(
                conn, 'base_rate', underlying.get('exchange_id') if underlying else None
            ) / 100
            spot = int(underlying["price"] or 0) if underlying else 0
            years = max(0, int(position["expiration_epoch"]) - now) / (365.0 * 86400)
            theoretical = OptionManager.theoretical_price(
                spot, int(position["strike_price"]), years,
                float(position["implied_volatility"]), position["option_type"],
                base_rate,
            )
            contract_code=str(position["contract_code"])
            price = quote_overrides.get(contract_code, theoretical)
            quantity = int(position["quantity"])
            value = price * quantity
            total_stock_value += value
            stock_details.append({
                "name": (
                    f"{underlying['name'] if underlying else position['underlying_code']} "
                    f"{'콜' if position['option_type'] == 'call' else '풋'} "
                    f"{int(position['strike_price']):,}"
                ),
                "code": str(position["contract_code"]), "qty": quantity,
                "price": price, "value": value, "type": "option",
                "sell_reserved_qty": min(
                    quantity, option_sell_reservations.get(int(position["contract_id"]), 0)
                ),
            })

        collateral_cash_row = conn.execute(
            "SELECT COALESCE(SUM(l.amount), 0) AS amount FROM option_collateral_cash_lots l "
            "JOIN option_contracts c ON c.contract_id = l.contract_id "
            "WHERE l.writer_id = ? AND c.status = 'active'",
            (str(user_id),),
        ).fetchone()
        option_collateral_cash = int(collateral_cash_row["amount"] or 0)

        call_collateral = conn.execute(
            "SELECT c.contract_code, c.underlying_code, SUM(l.quantity) AS quantity "
            "FROM option_collateral_lots l JOIN option_contracts c "
            "ON c.contract_id = l.contract_id WHERE l.writer_id = ? AND c.status = 'active' "
            "GROUP BY c.contract_id",
            (str(user_id),),
        )
        for collateral in call_collateral:
            underlying = wallet_asset(collateral["underlying_code"])
            spot = int(underlying["price"] or 0) if underlying else 0
            quantity = int(collateral["quantity"] or 0)
            value = spot * quantity
            total_stock_value += value
            stock_details.append({
                "name": f"{underlying['name'] if underlying else collateral['underlying_code']} 콜 담보",
                "code": collateral['underlying_code'], "qty": quantity,
                "is_collateral":True,"collateral_contract_code":collateral['contract_code'],
                "price": spot, "value": value, "type": "stock",
                "sell_reserved_qty": 0,
            })

        option_short_liability = 0
        written_rows = conn.execute(
            "SELECT contract_code, option_type, underlying_code, strike_price, "
            "expiration_epoch, implied_volatility, issued_contracts, outstanding_contracts, "
            "COALESCE((SELECT SUM(remaining_quantity) FROM market_orders o WHERE o.order_kind='option' "
            "AND o.option_order_type='write' AND o.asset_code=option_contracts.contract_code "
            "AND o.status IN ('open','partial')),0) offered, "
            "COALESCE((SELECT SUM(quantity) FROM option_collateral_lots l "
            "WHERE l.contract_id=option_contracts.contract_id),0) collateral_quantity, "
            "COALESCE((SELECT SUM(amount) FROM option_collateral_cash_lots l "
            "WHERE l.contract_id=option_contracts.contract_id),0) collateral_cash "
            "FROM option_contracts WHERE writer_id = ? AND status = 'active' "
            "AND (outstanding_contracts > 0 OR issued_contracts > 0)",
            (str(user_id),),
        )
        written_options=[]
        for written in written_rows:
            underlying = wallet_asset(written["underlying_code"])
            from managers.rate_manager import RateManager
            base_rate = RateManager.for_exchange(
                conn, 'base_rate', underlying.get('exchange_id') if underlying else None
            ) / 100
            spot = int(underlying["price"] or 0) if underlying else 0
            years = max(0, int(written["expiration_epoch"]) - now) / (365.0 * 86400)
            premium = OptionManager.theoretical_price(
                spot, int(written["strike_price"]), years,
                float(written["implied_volatility"]), written["option_type"],
                base_rate,
            )
            contract_code=str(written['contract_code'])
            mark=quote_overrides.get(contract_code, premium)
            option_short_liability += mark * int(written["outstanding_contracts"])
            written_options.append({**dict(written),
                'name':f"{underlying['name'] if underlying else written['underlying_code']} "
                       f"{'콜' if written['option_type']=='call' else '풋'} {int(written['strike_price']):,}",
                'price':mark,'theoretical_price':premium,'position_type':'writer',
                'quote_currency':(underlying.get('quote_currency') if underlying else None) or base_currency_code(conn)})

        reserved_rows = conn.execute(
            "SELECT quote_currency,COALESCE(SUM(reserved_cash), 0) AS total FROM market_orders "
            "WHERE user_id = ? AND (side = 'buy' OR order_kind = 'future') AND status IN ('open', 'partial') GROUP BY quote_currency",
            (str(user_id),),
        )
        order_reserved_total = 0
        for reserved in reserved_rows:
            order_reserved_total += int(reserved['total'] or 0)*currency_rates.get(reserved['quote_currency'] or base_currency_code(conn),1)

        futures_collateral = 0
        futures_unrealized = 0
        futures_rows = conn.execute(
            "SELECT f.*,p.name AS product_name,p.unit_price,p.contract_size "
            "FROM futures_positions f JOIN investment p ON p.product_code=f.product_code "
            "WHERE f.user_id=?",
            (str(user_id),),
        ).fetchall()
        for position in futures_rows:
            price = int(position["unit_price"])
            quantity = int(position["quantity"])
            collateral = int(position["collateral"])
            unrealized = quantity * int(position["contract_size"]) * (price - int(position["settlement_price"]))
            equity = max(0, collateral + unrealized)
            futures_collateral += collateral
            futures_unrealized += max(-collateral, unrealized)
            total_stock_value += equity
            stock_details.append({"name": f"{position['product_name']} {'롱' if quantity > 0 else '숏'}",
                "code": position["product_code"], "qty": quantity, "price": price, "value": equity,
                "type": "stock_future", "sell_reserved_qty": 0})

        bond_details = self.bond_manager.get_user_holdings(user_id, conn=conn)
        for item in bond_details:
            # Bonds are denominated in the currency of the country that issued them.
            row = conn.execute("SELECT COALESCE(quote_currency,base_currency_code()) FROM treasury_bond_products WHERE product_code=?",
                               (item["product_code"],)).fetchone()
            item["quote_currency"] = row[0] if row else base_currency_code(conn)
        bond_face_total = sum(item["principal"] for item in bond_details)
        bond_total = sum(item["market_value"]*currency_rates.get(item["quote_currency"],1) for item in bond_details)

        loan_rows = conn.execute(
            "SELECT COALESCE(quote_currency,base_currency_code()) currency_code,"
            "SUM(outstanding_balance_minor_units) balance_minor_units "
            "FROM loans WHERE user_id=? AND status IN ('active','defaulted') "
            "GROUP BY COALESCE(quote_currency,base_currency_code())",
            (str(user_id),),
        ).fetchall()
        loan_home_units = 0
        for loan in loan_rows:
            currency = str(loan["currency_code"] or base_currency_code(conn))
            places = currency_snapshot.precision(currency)
            minor_units = max(0, int(loan["balance_minor_units"] or 0))
            currency_micro_units = minor_units * SCALE // (10 ** places)
            loan_home_units += currency_snapshot.convert(
                currency_micro_units, currency, national_currency
            )
        loan_total = native_value(Decimal(loan_home_units) / Decimal(SCALE))
        loan_total_base = currency_snapshot.convert(
            loan_home_units, national_currency, base_currency_code(conn)
        ) // SCALE
        currency_total = 0
        currency_accounts = []
        currency_rows = conn.execute("SELECT quote_currency AS currency_code,balance_units FROM accounts WHERE user_id=? AND account_type='saving' AND quote_currency<>?",(str(user_id),national_currency)).fetchall()
        if currency_rows:
            from managers.currency_exchange_manager import CurrencyExchangeManager
            currency_accounts = [{**dict(r),'value_nwx':int(r['balance_units'])*CurrencyExchangeManager._rate(conn,r['currency_code'])//1_000_000 if r['balance_units'] else 0}
                for r in conn.execute("SELECT a.*,a.quote_currency AS currency_code,p.name AS product_name,COALESCE(a.contract_rate,p.interest_rate) AS annual_rate FROM accounts a "
                    "LEFT JOIN saving p ON p.product_code=a.product_code WHERE a.user_id=? AND a.account_type='saving' AND a.quote_currency<>? ORDER BY a.quote_currency",(str(user_id),national_currency))]
            currency_total = sum(r['value_nwx'] for r in currency_accounts)
        foreign_account_total_home = sum(
            currency_snapshot.convert(int(r['balance_units']),r['currency_code'],national_currency)
            for r in currency_accounts
        ) // SCALE
        home_account_total_nwx = currency_snapshot.convert(
            abs(account_total_home_units), national_currency, base_currency_code(conn)
        ) // SCALE
        if account_total_home_units < 0:
            home_account_total_nwx = -home_account_total_nwx
        els_positions=[]
        if getattr(self.db,'els',None) is not None:
            from managers.els_manager import ELSManager
            els_positions=ELSManager(self.db,self).get_user_positions(user_id, conn=conn)
        els_total=sum(int(item.get('principal',0))*currency_rates.get(item.get('quote_currency') or base_currency_code(conn),1) for item in els_positions)
        preferred_claims=[]
        if getattr(self.db,'corporate',None) is not None:
            preferred_claims=[{**dict(row),'remaining_amount':int(row['gross_amount'])-int(row['paid_amount'])}
                for row in conn.execute("SELECT * FROM preferred_dividend_claims WHERE user_id=? AND status IN ('unpaid','partial') ORDER BY due_epoch,claim_id",(str(user_id),))]
        preferred_claim_total=sum(int(row['remaining_amount'])*currency_rates.get(row['quote_currency'],1) for row in preferred_claims)
        grand_total = (
            cash_value_nwx + home_account_total_nwx + total_stock_value + order_reserved_total
            + bond_total + option_collateral_cash - option_short_liability + currency_total+els_total+preferred_claim_total
        )

        return {
            "nationality":nationality,
            "national_currency":national_currency,
            "wallets":wallets,
            "kyc_profile":kyc_profile,
            "home_cash_units":home_cash_units,
            "foreign_cash_units":foreign_cash_units,
            "home_decimal_places":home_decimal_places,
            "home_rate_nwx":home_rate_nwx,
            "home_account_total_home":total_account_balance,
            "foreign_account_total_home":foreign_account_total_home,
            "currency_accounts":currency_accounts,
            "written_options":written_options,
            "currency_total":currency_total,
            "els_positions":els_positions,
            "els_total":els_total,
            "preferred_dividend_claims":preferred_claims,
            "preferred_claim_total":preferred_claim_total,
            "cash": cash,
            "accounts": account_details,
            "deposit_insurance_claims": deposit_insurance_claims,
            "account_total": total_account_balance,
            "stocks": stock_details,
            "stock_total": total_stock_value,
            "order_reserved_total": order_reserved_total,
            "bonds": bond_details,
            "bond_total": bond_total,
            "bond_face_total": bond_face_total,
            "option_collateral_cash": option_collateral_cash,
            "option_short_liability": option_short_liability,
            "futures_collateral": futures_collateral,
            "futures_unrealized": futures_unrealized,
            "grand_total": grand_total,
            "loan_total": loan_total,
            # Loans are owed in the home currency; totals are valued in 기준 통화.
            "net_worth": grand_total - loan_total_base,
        }

    def _asset_from_tables(self, code):
        matches = [
            (self.db.assets.get_asset_info(code), "market_assets"),
            (self.db.companies.get_company(code), "company"),
            (self.db.products.get_investment(code), "investment"),
        ]
        matches = [(row, source) for row, source in matches if row]
        locations = [
            (self.db.assets.execute_one("SELECT 1 FROM market_assets WHERE code=?", (code,)), "시장 자산"),
            (self.db.companies.execute_one("SELECT 1 FROM company WHERE company_code=?", (code,)), "회사 주식"),
            (self.db.products.execute_one("SELECT 1 FROM saving WHERE product_code=?", (code,)), "저축 상품"),
            (self.db.products.execute_one("SELECT 1 FROM liability WHERE product_code=?", (code,)), "대출 상품"),
            (self.db.products.execute_one("SELECT 1 FROM investment WHERE product_code=?", (code,)), "투자 상품"),
            (self.db.assets.execute_one("SELECT 1 FROM option_contracts WHERE contract_code=?", (code,)), "옵션 계약"),
            (self.db.accounts.execute_one("SELECT 1 FROM accounts WHERE account_number=?", (code,)), "계좌 번호"),
        ]
        if sum(1 for row, _label in locations if row) > 1 or len(matches) > 1:
            raise ValueError("duplicate code")
        if not matches:
            return None
        row, source = matches[0]
        if source == "company":
            return {
                "name": row["name"],
                "price": max(1, int(math.floor(ExactDecimal(row["share_price"] or 0) + 0.5)))
                if ExactDecimal(row["share_price"] or 0) > 0 else 0,
                "type": "stock",
                "quote_currency":row['quote_currency'],
            }
        if source == "investment":
            price = ExactDecimal(row["unit_price"] or 0)
            return {
                "name": row["name"],
                "price": max(1, int(math.floor(price + 0.5))) if price > 0 else 0,
                "type": row["product_type"],
                "quote_currency":row['quote_currency'],
            }
        asset_type = row["type"]
        price = ExactDecimal(row["price"] or 0)
        price = max(1, int(math.floor(price + 0.5))) if price > 0 else 0
        return {"name": row["name"], "price": price, "type": asset_type,"quote_currency":row['quote_currency']}

    def _portfolio_asset_catalog(self, user_id, *, conn=None):
        """Return this user's portfolio price sources with one read query.

        Rows from non-tradable namespaces are included only to preserve the
        existing global code-collision protection.
        """
        from contextlib import nullcontext
        with (self.db.readonly() if conn is None else nullcontext(conn)) as conn:
            rows = conn.execute(
                "WITH needed(code) AS ("
                "SELECT asset_code FROM user_assets WHERE user_id=? "
                "UNION SELECT c.underlying_code FROM option_positions p "
                "JOIN option_contracts c ON c.contract_id=p.contract_id WHERE p.user_id=? "
                "UNION SELECT c.underlying_code FROM option_collateral_lots l "
                "JOIN option_contracts c ON c.contract_id=l.contract_id WHERE l.writer_id=? "
                "UNION SELECT underlying_code FROM option_contracts WHERE writer_id=?"
                ") "
                "SELECT code,name,price,type,quote_currency,'asset' AS source FROM market_assets "
                "WHERE code IN (SELECT code FROM needed) "
                "UNION ALL SELECT company_code,name,share_price,'stock',quote_currency,'company' FROM company "
                "WHERE company_code IN (SELECT code FROM needed) "
                "UNION ALL SELECT product_code,name,unit_price,product_type,quote_currency,'investment' FROM investment "
                "WHERE product_code IN (SELECT code FROM needed) "
                "UNION ALL SELECT product_code,NULL,NULL,NULL,NULL,'saving' FROM saving "
                "WHERE product_code IN (SELECT code FROM needed) "
                "UNION ALL SELECT product_code,NULL,NULL,NULL,NULL,'liability' FROM liability "
                "WHERE product_code IN (SELECT code FROM needed) "
                "UNION ALL SELECT contract_code,NULL,NULL,NULL,NULL,'option' FROM option_contracts "
                "WHERE contract_code IN (SELECT code FROM needed) "
                "UNION ALL SELECT account_number,NULL,NULL,NULL,NULL,'account' FROM accounts "
                "WHERE account_number IN (SELECT code FROM needed)",
                (str(user_id),) * 4,
            ).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(str(row["code"]), []).append(row)
        catalog = {}
        for code, matches in grouped.items():
            if len(matches) != 1 or matches[0]["source"] not in {"asset", "company", "investment"}:
                catalog[code] = None
                continue
            row = matches[0]
            numeric_price = ExactDecimal(row["price"] or 0)
            catalog[code] = {
                "name": row["name"],
                "price": max(1, int(math.floor(numeric_price + 0.5))) if numeric_price > 0 else 0,
                "type": row["type"],
                "quote_currency": row["quote_currency"],
            }
        return catalog
