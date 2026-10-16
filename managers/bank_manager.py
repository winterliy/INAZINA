import hashlib
import hmac
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from database import DatabaseManager
from database.user_db import TREASURY_USER_ID, ACTIVE_OR_TREASURY
from functions.money import (round_money, round_currency, parse_currency_money,
                             currency_precision)
from functions.interest_calc import year_fraction
from managers.rate_manager import RateManager


class BankManager:
    """예금 계좌 및 대출 업무를 담당합니다."""

    PASSWORD_ITERATIONS = 240_000

    @staticmethod
    def _loan_cashflows(principal, annual_rate, maturity_days, frequency_days,
                        amortization_type, day_count_convention="ACT/365",
                        start_epoch=None, due_epochs=None, decimal_places=0):
        """Build contractual installment cash flows for APR and servicing."""
        principal = float(principal)
        days = max(1, int(maturity_days or 1))
        frequency = max(1, int(frequency_days or days))
        mode = str(amortization_type or "legacy")
        if mode == "legacy":
            return []
        start = float(time.time() if start_epoch is None else start_epoch)
        if due_epochs is None:
            due_days = list(range(frequency, days, frequency)) + [days]
            due_times = [start+day*86400 for day in due_days]
        else:
            due_times = sorted(float(value) for value in due_epochs if float(value) > start)
            due_days = [(value-start)/86400 for value in due_times]
        if not due_times:
            return []
        count = len(due_days)
        rate = float(annual_rate or 0) / 100.0
        fractions = []
        previous = start
        for due in due_times:
            fractions.append(year_fraction(
                max(0.0, due-previous), day_count_convention, previous
            ))
            previous = due
        rows = []
        if mode == "bullet":
            interest = round_money(principal*rate*sum(fractions), decimal_places)
            return [(due_days[-1], principal, max(0, interest))]
        if mode == "equal_principal":
            remaining = principal
            allocated = 0
            for index, (due_day, fraction) in enumerate(zip(due_days, fractions), 1):
                principal_due = (principal-allocated if index == count
                                 else round_money(principal/count, decimal_places))
                principal_due = max(0, min(remaining, principal_due))
                interest = round_money(remaining*rate*fraction, decimal_places)
                rows.append((due_day, principal_due, max(0, interest)))
                remaining -= principal_due
                allocated += principal_due
            return rows
        # Annuity: discount each potentially irregular period separately so
        # holidays and the selected day-count convention affect the payment.
        discount = 1.0
        discounts = []
        period_rates = []
        for fraction in fractions:
            periodic_rate = max(-0.999999, rate*fraction)
            period_rates.append(periodic_rate)
            discount /= 1.0+periodic_rate
            discounts.append(discount)
        payment = principal/max(1e-12, sum(discounts))
        remaining = principal
        allocated = 0
        for index, (due_day, periodic_rate) in enumerate(
            zip(due_days, period_rates), 1
        ):
            interest = max(0, round_money(remaining*periodic_rate, decimal_places))
            principal_due = (principal-allocated if index == count else
                             max(0, round_money(payment-interest, decimal_places)))
            principal_due = min(remaining, principal_due)
            rows.append((due_day, principal_due, interest))
            remaining -= principal_due
            allocated += principal_due
        return rows

    @classmethod
    def _recast_payment_schedule(cls, conn, loan, annual_rate, as_of,
                                 outstanding_balance=None):
        """Recalculate only unpaid future installments after a floating reset."""
        mode = str(loan["amortization_type"] or "legacy")
        if mode == "legacy":
            return 0
        rows = conn.execute(
            "SELECT * FROM accounts.loan_payment_schedule WHERE loan_id=? "
            "AND due_epoch>? AND status NOT IN ('paid','waived') "
            "ORDER BY installment_number",
            (loan["loan_id"], float(as_of)),
        ).fetchall()
        if not rows:
            return 0
        raw_balance = (loan["outstanding_balance"] if outstanding_balance is None
                       else outstanding_balance)
        places = currency_precision(conn, loan["quote_currency"])
        balance = max(0, round_money(raw_balance or 0, places))
        if balance <= 0:
            return 0
        due_epochs = [float(row["due_epoch"]) for row in rows]
        cashflows = cls._loan_cashflows(
            balance, annual_rate,
            max(1, math.ceil((due_epochs[-1]-float(as_of))/86400)),
            int(loan["payment_frequency_days"] or 30), mode,
            loan["day_count_convention"], float(as_of), due_epochs, places,
        )
        for row, (_days, principal_due, interest_due) in zip(rows, cashflows):
            already_paid = max(0, float(row["amount_paid"] or 0))
            conn.execute(
                "UPDATE accounts.loan_payment_schedule SET principal_due=?,interest_due=?,"
                "rate_applied=?,day_count_convention=?,recast_epoch=? WHERE schedule_id=?",
                (principal_due+already_paid, interest_due, float(annual_rate),
                 loan["day_count_convention"], float(as_of), row["schedule_id"]),
            )
        return len(cashflows)

    def __init__(self, db: DatabaseManager):
        self.db = db

    def _settle_user_interest(self, user_id, now=None):
        from managers.interest_manager import InterestManager

        return InterestManager(self.db).process_user_interests(str(user_id), now=now)

    @classmethod
    def _hash_password(cls, password):
        # Hash the submitted string verbatim: do not trim or normalize spaces.
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, cls.PASSWORD_ITERATIONS
        )
        return f"pbkdf2_sha256${cls.PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"

    @classmethod
    def _verify_password(cls, password, stored_password):
        if not isinstance(password, str) or not isinstance(stored_password, str):
            return False
        if stored_password.startswith("pbkdf2_sha256$"):
            try:
                _scheme, iterations, salt_hex, digest_hex = stored_password.split("$", 3)
                iterations = int(iterations)
                if iterations <= 0:
                    return False
                salt = bytes.fromhex(salt_hex)
                expected = bytes.fromhex(digest_hex)
                actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
                return hmac.compare_digest(actual, expected)
            except (TypeError, ValueError, OverflowError):
                return False
        # AccountDB migrates legacy plaintext values on startup; keep safe compatibility
        # for a row written by an older running process.
        return hmac.compare_digest(password, stored_password)

    def open_account(self, user_id, product_code, password, deposit_amount):
        if isinstance(deposit_amount, bool):
            return False, "❌ 초기 예치금을 확인해주세요."
        if not isinstance(password, str) or not 4 <= len(password) <= 16:
            return False, "❌ 비밀번호는 4~16자로 입력해주세요."
        # Foreign-currency savings open through the same entry point and rules.
        kind = self.db.products.execute_one(
            "SELECT product_kind,name FROM saving WHERE product_code=?", (product_code,))
        if kind and kind["product_kind"] == "currency_saving":
            from managers.currency_exchange_manager import CurrencyExchangeManager
            try:
                number = CurrencyExchangeManager(self.db).open_account(user_id, product_code, password, deposit_amount)
            except ValueError as exc:
                return False, f"❌ {exc}"
            return True, f"✅ {kind['name']} 외환통장을 개설했습니다. 계좌번호: `{number}`"
        try:
            with self.db.atomic(("users", "accounts", "product", "assets", "company")) as conn:
                product = conn.execute(
                    "SELECT * FROM product.saving WHERE product_code = ?", (product_code,)
                ).fetchone()
                if not product:
                    return False, "❌ 존재하지 않는 저축 상품입니다."
                locations = DatabaseManager.code_locations(conn, product_code)
                if len(locations) != 1 or locations[0] != "저축 상품":
                    return False, f"❌ 상품 코드 `{product_code}`가 다른 상품·자산 코드와 중복 등록되어 있습니다. SQL Browser에서 정리해주세요."
                if product["status"] != "active":
                    return False, "❌ 현재 가입할 수 없는 상품입니다."
                institution = conn.execute(
                    "SELECT status FROM financial_institutions WHERE issuer=?",
                    (product["issuer"],),
                ).fetchone()
                if institution and institution["status"] == "failed":
                    return False, "❌ 발행 금융기관이 정리 절차 중이라 신규 예금을 받을 수 없습니다."
                if product['product_kind'] == 'currency_saving':
                    return False, "❌ 외환통장은 외환통장 개설 기능을 이용해주세요."
                from managers.currency_manager import CurrencyManager
                home = CurrencyManager.home(conn,user_id)
                if product['quote_currency'] != home:
                    return False, f"❌ 이 예금은 {product['quote_currency']} 상품입니다. 국적 통화 {home} 예금만 일반 계좌로 개설할 수 있습니다."
                deposit_amount = parse_currency_money(
                    conn, deposit_amount, product['quote_currency'], minimum=0
                )
                if conn.execute(
                    "SELECT 1 FROM accounts.accounts WHERE user_id = ? AND product_code = ?",
                    (str(user_id), product_code),
                ).fetchone():
                    return False, f"❌ 이미 {product['name']} 상품에 가입한 계좌가 있습니다."
                try:
                    minimum_value = float(product["deposit_amount"] or 0)
                except (TypeError, ValueError, OverflowError):
                    return False, "❌ 상품의 최소 예치금 설정이 올바르지 않습니다."
                if not math.isfinite(minimum_value) or minimum_value < 0:
                    return False, "❌ 상품의 최소 예치금 설정이 올바르지 않습니다."
                minimum = round_currency(conn, minimum_value, home)
                if deposit_amount < minimum:
                    return False, f"❌ 최소 예치금은 {minimum:,} {home}입니다."
                cursor = conn.execute(
                    "UPDATE users.users SET cash = cash - ? "
                    f"WHERE user_id = ? AND {ACTIVE_OR_TREASURY} AND cash >= ?",
                    (deposit_amount, str(user_id), deposit_amount),
                )
                if cursor.rowcount != 1:
                    return False, f"❌ 현금 잔액이 부족합니다. (필요 예치금: {deposit_amount:,} {home})"
                account_number = self._generate_account_number(conn)
                opened_epoch = time.time()
                contract_spread = (RateManager.product_spread(conn, product, 'deposit')
                                   if product['rate_type'] == 'floating'
                                   else float(product['base_rate_spread']))
                try:
                    effective_rate = RateManager.effective_annual_rate(
                        float(product['interest_rate']), float(product['interest_cycle'] or 31536000)
                    )
                except (ValueError, OverflowError):
                    effective_rate = None
                conn.execute(
                    "INSERT INTO accounts.accounts "
                    "(account_number,user_id,product_code,balance,password,principal,last_payment_epoch,"
                    "quote_currency,contract_rate,rate_type,rate_nationality,base_rate_spread,rate_floor,"
                    "rate_cap,repricing_cycle_seconds,next_repricing_epoch,benchmark_key,"
                    "benchmark_tenor_days,repricing_lag_seconds,contract_effective_rate,"
                    "early_withdrawal_rate,early_withdrawal_fee_percent,deposit_insured) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (account_number, str(user_id), product_code, deposit_amount,
                     self._hash_password(password), deposit_amount, opened_epoch, home,
                     float(product['interest_rate']), product['rate_type'], product['rate_nationality'],
                     contract_spread,
                     (-100.0 if product['negative_rate_allowed'] else float(product['rate_floor'])),
                     product['rate_cap'],
                     int(product['repricing_cycle_seconds']),
                     opened_epoch+int(product['repricing_cycle_seconds'])
                     if product['rate_type']=='floating' else None,
                     product['benchmark_key'], int(product['benchmark_tenor_days'] or product['maturity_days'] or 0),
                     int(product['repricing_lag_seconds'] or 0), effective_rate,
                     float(product['early_withdrawal_rate'] or 0),
                     float(product['early_withdrawal_fee_percent'] or 0),
                     int(product['deposit_insured'] or 0)),
                )
                RateManager.refresh_all_market_pressure(conn, now=opened_epoch)
            return True, (
                f"✅ {product['name']} 계좌가 개설되었습니다.\n"
                f"계좌번호: `{account_number}` · 예치금: {deposit_amount:,} {home}"
            )
        except Exception as exc:
            return False, f"❌ 계좌 개설 처리 중 오류가 발생했습니다: {exc}"

    def close_account(self, user_id, account_number, destination_account=None, password=None):
        """계좌 잔액과 해당 계좌에 귀속된 투자자산을 본인에게 이관합니다."""
        try:
            self._settle_user_interest(user_id)
            with self.db.atomic(("users", "accounts", "assets")) as conn:
                source = conn.execute(
                    "SELECT account_number,user_id,balance,balance_units,password,principal,created_at,"
                    "early_withdrawal_rate,early_withdrawal_fee_percent,product_code,account_type "
                    "FROM accounts.accounts "
                    "WHERE account_number = ? AND user_id = ? AND account_type IN ('saving','currency_saving')",
                    (str(account_number), str(user_id)),
                ).fetchone()
                if not source:
                    return False, "❌ 해당 계좌를 찾을 수 없거나 권한이 없습니다."
                # System-opened foreign-currency accounts (e.g. the treasury's) have no password.
                if source["password"] is not None and not self._verify_password(password, source["password"]):
                    return False, "❌ 계좌 비밀번호가 올바르지 않습니다. 계좌 해지를 취소했습니다."
                reserved_order = conn.execute(
                    "SELECT order_id FROM assets.market_orders WHERE user_id = ? "
                    "AND funding_account = ? AND (side = 'buy' OR order_kind = 'future') "
                    "AND status IN ('open', 'partial') AND reserved_cash > 0 LIMIT 1",
                    (str(user_id), str(account_number)),
                ).fetchone()
                if reserved_order:
                    return False, (
                        f"❌ 이 계좌에 매수 주문 #{reserved_order['order_id']}의 예치금이 잠겨 있습니다. "
                        "먼저 주문을 취소하거나 체결을 기다려주세요."
                    )
                if destination_account:
                    destination = conn.execute(
                        "SELECT account_number FROM accounts.accounts "
                        "WHERE account_number = ? AND user_id = ? AND account_number <> ? "
                        "AND quote_currency=(SELECT quote_currency FROM accounts.accounts "
                        "WHERE account_number=?)",
                        (str(destination_account), str(user_id), str(account_number),
                         str(account_number)),
                    ).fetchone()
                    if not destination:
                        return False, "❌ 이관 대상은 본인 명의의 다른 계좌여야 합니다."
                    destination_number = destination["account_number"]
                else:
                    destination = conn.execute(
                        "SELECT account_number FROM accounts.accounts WHERE user_id = ? "
                        "AND account_number <> ? AND account_type = 'saving' "
                        "AND quote_currency=(SELECT quote_currency FROM accounts.accounts "
                        "WHERE account_number=?) "
                        "ORDER BY balance DESC LIMIT 1",
                        (str(user_id), str(account_number), str(account_number)),
                    ).fetchone()
                    destination_number = destination["account_number"] if destination else None

                source_currency = conn.execute(
                    "SELECT quote_currency FROM accounts.accounts WHERE account_number=?",
                    (str(account_number),),
                ).fetchone()[0]
                from managers.currency_manager import CurrencyManager as _Currency, SCALE as _SCALE
                balance = round_currency(conn, (source["balance_units"] or 0) / _SCALE, source_currency)
                early_penalty = 0
                product = conn.execute(
                    "SELECT maturity_days,day_count_convention FROM product.saving "
                    "WHERE product_code=?", (source["product_code"],)
                ).fetchone()
                if product and int(product["maturity_days"] or 0) > 0:
                    opened = datetime.strptime(
                        source["created_at"], "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc).timestamp()
                    now = time.time()
                    if now < opened+int(product["maturity_days"])*86400:
                        principal = max(0, float(source["principal"] or 0))
                        earned = max(0, balance-principal)
                        allowed = max(0, round_currency(conn,
                            principal*float(source["early_withdrawal_rate"] or 0)/100
                            * year_fraction(now-opened, product["day_count_convention"], opened),
                            source_currency
                        ))
                        fee = round_currency(
                            conn, principal*float(source["early_withdrawal_fee_percent"] or 0)/100,
                            source_currency
                        )
                        early_penalty = min(balance, max(0, earned-allowed)+fee)
                        balance -= early_penalty
                        if early_penalty:
                            from managers.currency_manager import CurrencyManager, SCALE
                            CurrencyManager.treasury_receive(
                                conn, CurrencyManager.user_treasury_id(conn, user_id),
                                source_currency, int(round(early_penalty*SCALE)))
                if destination_number:
                    conn.execute(
                        "UPDATE accounts.accounts SET balance_units = balance_units + CAST(ROUND(? * 1000000) AS INTEGER), "
                        "principal = COALESCE(principal, 0) + ?, "
                        "last_payment_epoch = ?, last_payment = CURRENT_TIMESTAMP "
                        "WHERE account_number = ?",
                        (balance, balance, time.time(), destination_number),
                    )
                elif source_currency == _Currency.home(conn, user_id):
                    conn.execute("UPDATE users.users SET cash = cash + ? WHERE user_id = ?", (balance, str(user_id)))
                else:
                    # A foreign-currency balance returns to home cash at the current rate.
                    _Currency.change(conn, user_id, _Currency.home(conn, user_id),
                                     _Currency.convert(conn, int(round(balance * _SCALE)), source_currency,
                                                       _Currency.home(conn, user_id)))

                positions = conn.execute(
                    "SELECT asset_code, quantity, average_cost FROM assets.user_assets "
                    "WHERE user_id = ? AND account_number = ?",
                    (str(user_id), str(account_number)),
                ).fetchall()
                for position in positions:
                    if destination_number:
                        conn.execute(
                            """INSERT INTO assets.user_assets
                               (user_id, asset_code, account_number, quantity, average_cost)
                               VALUES (?, ?, ?, ?, ?)
                               ON CONFLICT(user_id, asset_code, IFNULL(account_number, '')) DO UPDATE SET
                                   average_cost = ((quantity * average_cost) +
                                       (excluded.quantity * excluded.average_cost)) /
                                       (quantity + excluded.quantity),
                                   quantity = quantity + excluded.quantity""",
                            (str(user_id), position["asset_code"], destination_number,
                             position["quantity"], position["average_cost"]),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO assets.user_assets
                               (user_id, asset_code, account_number, quantity, average_cost)
                               VALUES (?, ?, NULL, ?, ?)
                               ON CONFLICT(user_id, asset_code, IFNULL(account_number, '')) DO UPDATE SET
                                   average_cost = ((quantity * average_cost) +
                                       (excluded.quantity * excluded.average_cost)) /
                                       (quantity + excluded.quantity),
                                   quantity = quantity + excluded.quantity""",
                            (str(user_id), position["asset_code"], position["quantity"], position["average_cost"]),
                        )
                conn.execute(
                    "DELETE FROM assets.user_assets WHERE user_id = ? AND account_number = ?",
                    (str(user_id), str(account_number)),
                )
                conn.execute("DELETE FROM accounts.accounts WHERE account_number = ?", (str(account_number),))
                RateManager.refresh_all_market_pressure(conn)
            target_text = f"계좌 `{destination_number}`" if destination_number else "현금 지갑"
            penalty_text = (
                f" 중도해지 조정 {early_penalty:,} {source_currency}를 차감했습니다."
                if early_penalty else ""
            )
            return True, (
                f"✅ 계좌를 해지했습니다. 잔액과 보유 자산을 {target_text}(으)로 이관했습니다."
                f"{penalty_text}"
            )
        except Exception as exc:
            return False, f"❌ 계좌 해지 처리 중 오류가 발생했습니다: {exc}"

    @staticmethod
    def _asset_collateral_plan(conn, user_id, asset_code, quantity, currency_code):
        code = str(asset_code or "").strip().upper()
        if not code and not quantity:
            return [], 0, None
        if not code or isinstance(quantity, bool):
            raise ValueError("자산 담보는 자산 코드와 수량을 함께 입력해주세요.")
        quantity = float(quantity)
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("자산 담보 수량은 0보다 커야 합니다.")
        asset = conn.execute(
            "SELECT code,name,price,status,quote_currency FROM assets.market_assets WHERE code=? "
            "UNION ALL SELECT company_code,name,share_price,status,quote_currency "
            "FROM company.company WHERE company_code=? "
            "UNION ALL SELECT product_code,name,unit_price,status,quote_currency "
            "FROM product.investment WHERE product_code=? LIMIT 1",
            (code, code, code),
        ).fetchone()
        if not asset or asset["status"] != "active" or float(asset["price"] or 0) <= 0:
            raise ValueError("담보로 설정할 수 있는 활성 자산 코드를 확인해주세요.")
        if str(asset["quote_currency"] or "NWX") != str(currency_code):
            raise ValueError("대출과 같은 결제통화의 자산만 담보로 사용할 수 있습니다.")
        lots = conn.execute(
            "SELECT holding_id,account_number,quantity,average_cost FROM assets.user_assets "
            "WHERE user_id=? AND asset_code=? ORDER BY holding_id",
            (str(user_id), code),
        ).fetchall()
        available = sum(float(row["quantity"] or 0) for row in lots)
        if available+1e-9 < quantity:
            raise ValueError(f"담보 자산 `{code}` 보유 수량이 부족합니다.")
        plan, left = [], quantity
        for row in lots:
            take = min(left, float(row["quantity"] or 0))
            if take > 0:
                plan.append({
                    "holding_id": int(row["holding_id"]),
                    "source_account": row["account_number"],
                    "quantity": take,
                    "old_quantity": float(row["quantity"]),
                    "average_cost": float(row["average_cost"] or 0),
                })
                left -= take
            if left <= 1e-9:
                break
        return plan, round_currency(
            conn, float(asset["price"])*quantity, currency_code
        ), asset

    @staticmethod
    def _pledge_asset_collateral(conn, loan_id, user_id, asset, plan, haircut, now):
        if not plan:
            return
        price = float(asset["price"])
        for lot in plan:
            if math.isclose(lot["quantity"], lot["old_quantity"], abs_tol=1e-9):
                changed = conn.execute(
                    "DELETE FROM assets.user_assets WHERE holding_id=? AND quantity=?",
                    (lot["holding_id"], lot["old_quantity"]),
                )
            else:
                changed = conn.execute(
                    "UPDATE assets.user_assets SET quantity=quantity-? "
                    "WHERE holding_id=? AND quantity=?",
                    (lot["quantity"], lot["holding_id"], lot["old_quantity"]),
                )
            if changed.rowcount != 1:
                raise RuntimeError("담보 자산 보유량이 변경되어 대출을 취소했습니다.")
            currency = conn.execute(
                "SELECT quote_currency FROM accounts.loans WHERE loan_id=?", (loan_id,)
            ).fetchone()[0]
            value = round_currency(
                conn, price*lot["quantity"]*(1-float(haircut)), currency
            )
            conn.execute(
                "INSERT INTO accounts.loan_collateral_assets "
                "(loan_id,user_id,asset_code,source_account,quantity,average_cost,"
                "pledged_epoch,haircut,last_price,last_value) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (loan_id, str(user_id), asset["code"], lot["source_account"],
                 lot["quantity"], lot["average_cost"], now, float(haircut), price, value),
            )

    @staticmethod
    def _underwriting_metrics(conn, user_id, amount, product, annual_rate, now,
                              annual_income):
        """Return income affordability and repayment-history measures at origination."""
        income = max(0.0, float(annual_income or 0))
        maturity_days = max(1, int(product["maturity_days"] or 365))
        maturity_years = maturity_days / 365.0
        proposed_principal_service = min(
            float(amount), float(amount) / max(1.0, maturity_years)
        )
        proposed_interest = float(amount) * max(0.0, float(annual_rate or 0)) / 100.0
        proposed_service = proposed_principal_service + proposed_interest

        existing_balance = 0
        existing_interest = 0.0
        existing_service = 0.0
        missed = defaults = 0
        loans = conn.execute(
            "SELECT loan_id,outstanding_balance,contract_rate,missed_payment_count,status "
            "FROM accounts.loans WHERE user_id=? AND status IN ('active','defaulted')",
            (str(user_id),),
        ).fetchall()
        horizon = float(now) + 365 * 86400
        for loan in loans:
            balance = max(0.0, float(loan["outstanding_balance"] or 0))
            existing_balance += balance
            existing_interest += balance * max(0.0, float(loan["contract_rate"] or 0)) / 100.0
            scheduled = conn.execute(
                "SELECT COALESCE(SUM(MAX(0,principal_due+interest_due+fee_due-amount_paid)),0) "
                "FROM accounts.loan_payment_schedule WHERE loan_id=? AND due_epoch<=? "
                "AND status NOT IN ('paid','waived')",
                (loan["loan_id"], horizon),
            ).fetchone()[0]
            existing_service += float(scheduled or 0)
            if not scheduled and balance:
                existing_service += min(balance, balance + existing_interest)
            missed += max(0, int(loan["missed_payment_count"] or 0))
            defaults += int(loan["status"] == "defaulted")

        if income > 0:
            # DTI includes the proposed loan's annual debt service and interest
            # on other debt; DSR includes annual principal and interest for all.
            dti = 100.0 * (proposed_service + existing_interest) / income
            dsr = 100.0 * (proposed_service + existing_service) / income
        else:
            dti = dsr = None
        history_index = min(1.0, (missed + defaults * 5) / 10.0)
        return {
            "income": income,
            "dti": dti,
            "dsr": dsr,
            "missed": missed,
            "defaults": defaults,
            "history_index": history_index,
            "existing_balance": existing_balance,
        }

    def apply_loan(self, user_id, product_code, amount, collateral_amount=0,
                   collateral_asset_code=None, collateral_asset_quantity=0):
        if isinstance(amount, bool) or isinstance(collateral_amount, bool):
            return False, "❌ 대출·담보 금액을 확인해주세요."
        try:
            self._settle_user_interest(user_id)
            with self.db.atomic(("users", "accounts", "product", "assets", "company")) as conn:
                product = conn.execute(
                    "SELECT * FROM product.liability WHERE product_code = ?", (product_code,)
                ).fetchone()
                if not product:
                    return False, "❌ 존재하지 않는 대출 상품입니다."
                locations = DatabaseManager.code_locations(conn, product_code)
                if len(locations) != 1 or locations[0] != "대출 상품":
                    return False, f"❌ 상품 코드 `{product_code}`가 다른 상품·자산 코드와 중복 등록되어 있습니다. SQL Browser에서 정리해주세요."
                if product["status"] != "active":
                    return False, "❌ 현재 대출이 불가능한 상품입니다."
                amount = parse_currency_money(
                    conn, amount, product['quote_currency'], minimum=0
                )
                collateral_amount = parse_currency_money(
                    conn, collateral_amount, product['quote_currency'], minimum=0
                )
                if amount <= 0:
                    return False, "❌ 대출 금액은 0보다 커야 합니다."
                capacity = RateManager.institution_capacity(
                    conn, product["issuer"], amount, product['quote_currency']
                )
                if not capacity["allowed"]:
                    if capacity["status"] != "active":
                        return False, (
                            "❌ 발행 금융기관이 자본규제 또는 정리 절차로 신규 대출을 "
                            "취급할 수 없습니다."
                        )
                    return False, (
                        "❌ 발행 금융기관의 지급준비·자기자본 한도를 초과합니다. "
                        f"현재 추가 대출 가능액: {math.floor(capacity['headroom']):,} "
                        f"{product['quote_currency']}"
                    )
                from managers.currency_manager import CurrencyManager
                home = CurrencyManager.home(conn,user_id)
                if product['quote_currency'] != home:
                    return False, f"❌ 이 대출은 {product['quote_currency']} 상품입니다. 국적 통화 {home} 대출만 이용할 수 있습니다."
                if conn.execute(
                    "SELECT 1 FROM accounts.loans WHERE user_id = ? AND product_code = ?",
                    (str(user_id), product_code),
                ).fetchone():
                    return False, f"❌ 이미 {product['name']} 상품을 이용한 기록이 있습니다."
                user = conn.execute(
                    f"SELECT credit_score,nationality,annual_income,income_stability "
                    f"FROM users.users WHERE user_id = ? AND "
                    f"{ACTIVE_OR_TREASURY}",
                    (str(user_id),),
                ).fetchone()
                if not user:
                    return False, "❌ 등록된 사용자만 대출을 신청할 수 있습니다."
                try:
                    limit = float(product["loan_limit"] or 0)
                    minimum_score = int(product["min_credit_score"] or 0)
                except (TypeError, ValueError):
                    return False, "❌ 대출 상품의 한도 또는 신용 기준이 잘못 설정되었습니다."
                if not math.isfinite(limit) or limit < 0:
                    return False, "❌ 대출 상품의 한도 또는 신용 기준이 잘못 설정되었습니다."
                if int(user["credit_score"] or 0) < minimum_score:
                    return False, f"❌ 신용 점수가 부족합니다. (요구 점수: {minimum_score}점)"
                limit = round_currency(conn, limit, home)
                if limit > 0 and amount > limit:
                    return False, f"❌ 대출 한도를 초과했습니다. (한도: {limit:,} {home})"
                opened_epoch = time.time()
                required_collateral = round_currency(
                    conn, amount*float(product['collateral_required_ratio'] or 0), home
                )
                asset_plan, asset_market_value, asset = self._asset_collateral_plan(
                    conn, user_id, collateral_asset_code, collateral_asset_quantity, home
                )
                haircut = float(product['collateral_haircut'] or 0)
                asset_collateral_value = round_currency(
                    conn, asset_market_value*(1-haircut), home
                )
                total_collateral_value = collateral_amount+asset_collateral_value
                if total_collateral_value < required_collateral:
                    return False, (
                        f"❌ 이 상품은 헤어컷 후 담보 {required_collateral:,} {home} 이상이 필요합니다."
                    )
                credit_score = max(0, min(1000, int(user['credit_score'] or 0)))
                proposed_country = product['rate_nationality'] or user['nationality']
                contract_country = (proposed_country if proposed_country and conn.execute(
                    "SELECT 1 FROM country_currencies WHERE nationality=?", (proposed_country,)
                ).fetchone() else None)
                base_credit_adjustment = (
                    float(product['credit_spread_max'] or 0)*(1000-credit_score)/1000
                )
                effective_income = round_currency(conn,
                    max(0.0, float(user['annual_income'] or 0))
                    * max(0.0, min(1.0, float(user['income_stability'] or 0))), home
                )
                underwriting = self._underwriting_metrics(
                    conn, user_id, amount, product,
                    float(product['interest_rate'] or 0)+base_credit_adjustment,
                    opened_epoch, effective_income,
                )
                max_dti = float(product['max_dti_percent'] or 0)
                max_dsr = float(product['max_dsr_percent'] or 0)
                if (max_dti > 0 or max_dsr > 0) and effective_income <= 0:
                    return False, "❌ 소득기반 심사가 필요한 상품입니다. 연소득 정보를 등록해주세요."
                if max_dti > 0 and underwriting['dti'] > max_dti+1e-9:
                    return False, (
                        f"❌ DTI {underwriting['dti']:.1f}%가 상품 한도 "
                        f"{max_dti:g}%를 초과합니다."
                    )
                if max_dsr > 0 and underwriting['dsr'] > max_dsr+1e-9:
                    return False, (
                        f"❌ DSR {underwriting['dsr']:.1f}%가 상품 한도 "
                        f"{max_dsr:g}%를 초과합니다."
                    )
                affordability_utilization = max(
                    (underwriting['dti'] or 0) / (max_dti or 100),
                    (underwriting['dsr'] or 0) / (max_dsr or 100),
                )
                history_adjustment = (
                    float(product['borrower_history_spread_max'] or 0)
                    * underwriting['history_index']
                )
                affordability_adjustment = (
                    float(product['affordability_spread_max'] or 0)
                    * min(1.0, max(0.0, affordability_utilization))
                )
                credit_adjustment = (
                    base_credit_adjustment+history_adjustment+affordability_adjustment
                )
                borrower_risk_score = max(0.0, min(100.0,
                    credit_score/10.0
                    - underwriting['history_index']*25.0
                    - min(1.0, affordability_utilization)*20.0
                ))
                collateral_coverage = min(1.0, total_collateral_value/amount)
                collateral_discount = float(product['collateral_discount_max'] or 0)*collateral_coverage
                if product['rate_type'] == 'floating':
                    contract_spread = (
                        RateManager.product_spread(conn, product, 'loan')
                        + credit_adjustment-collateral_discount
                    )
                    contract_rate = RateManager.floating_rate(
                        conn, contract_country, contract_spread,
                        product['rate_floor'], product['rate_cap'],
                        benchmark_key=product['benchmark_key'],
                        tenor_days=(product['benchmark_tenor_days'] or product['maturity_days']),
                        apply_legal_max=True,
                    )
                else:
                    contract_spread = float(product['base_rate_spread'] or 0)
                    contract_rate = max(0, float(product['interest_rate'])
                                        + credit_adjustment-collateral_discount)
                    legal = (RateManager.from_connection(
                        conn, 'legal_max_rate', contract_country, require_country=True
                    ) if contract_country else RateManager.legal_max_for_currency(conn, home)[0])
                    contract_rate = min(contract_rate, legal)
                fee = round_currency(
                    conn, amount*float(product['origination_fee_percent'] or 0)/100, home
                )
                mandatory_fee = round_currency(
                    conn, amount*float(product['mandatory_cost_percent'] or 0)/100, home
                )
                total_finance_fee = fee+mandatory_fee
                net_payout = max(0, amount-total_finance_fee)
                effective = RateManager.effective_annual_rate(
                    contract_rate, float(product['interest_cycle'] or 31536000)
                )
                maturity_days = int(product['maturity_days'] or 365)
                frequency_days = int(product['payment_frequency_days'] or 30)
                due_days = ([] if product['amortization_type'] == 'legacy' else
                            list(range(frequency_days, maturity_days, frequency_days))
                            + [maturity_days])
                due_epochs = [
                    RateManager.adjust_business_day(
                        conn, opened_epoch+days*86400,
                        contract_country or "낙원", product["business_day_convention"],
                    )
                    for days in due_days
                ]
                schedule = self._loan_cashflows(
                    amount, contract_rate, maturity_days, frequency_days,
                    product['amortization_type'], product['day_count_convention'],
                    opened_epoch, due_epochs, currency_precision(conn, home),
                )
                apr = RateManager.annual_percentage_rate(
                    contract_rate, total_finance_fee, net_payout, maturity_days,
                    cashflows=[(day, principal_due+interest_due)
                               for day, principal_due, interest_due in schedule] or None,
                    principal=amount,
                )
                legal_cost_cap = (RateManager.from_connection(
                    conn, 'legal_max_rate', contract_country, require_country=True
                ) if contract_country else RateManager.legal_max_for_currency(conn, home)[0])
                legal_cost_rate = (contract_rate + (total_finance_fee/max(
                                   10**(-currency_precision(conn, home)), net_payout))
                                   * 365/max(1, int(product['maturity_days'] or 365))*100)
                if legal_cost_rate > legal_cost_cap+1e-9:
                    raise ValueError(
                        f"법정 산입비용 연율 {legal_cost_rate:.3f}%가 {contract_country or home} "
                        f"법정최고이자 {legal_cost_cap:g}%를 초과합니다."
                    )
                if collateral_amount:
                    funds = UserFunds.choose(conn, str(user_id), home)
                    if not funds or funds[0] < collateral_amount or not UserFunds.debit(
                        conn, str(user_id), funds[1], collateral_amount
                    ):
                        return False, "❌ 담보로 설정할 잔액이 부족합니다."
                inserted = conn.execute(
                    "INSERT INTO accounts.loans "
                    "(user_id, product_code, principal, outstanding_balance, last_payment_epoch, "
                    "last_repayment_epoch, maturity_epoch, contract_rate,rate_type,rate_nationality,"
                    "base_rate_spread,rate_floor,rate_cap,repricing_cycle_seconds,next_repricing_epoch,"
                    "benchmark_key,benchmark_tenor_days,repricing_lag_seconds,contract_effective_rate,"
                    "contract_apr,origination_fee,mandatory_cost_fee,collateral_amount,day_count_convention,"
                    "amortization_type,payment_frequency_days,collateral_asset_value,current_ltv,"
                    "underwriting_dti,underwriting_dsr,borrower_risk_score,underwriting_income,"
                    "quote_currency,prepayment_fee_percent,prepayment_fee_decline_days) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(user_id), product_code, amount, amount, opened_epoch, opened_epoch,
                     opened_epoch+int(product['maturity_days'] or 0)*86400
                     if int(product['maturity_days'] or 0)>0 else None,
                     contract_rate, product['rate_type'], contract_country,
                     contract_spread, float(product['rate_floor']), product['rate_cap'],
                     int(product['repricing_cycle_seconds']),
                     opened_epoch+int(product['repricing_cycle_seconds'])
                     if product['rate_type']=='floating' else None,
                     product['benchmark_key'], int(product['benchmark_tenor_days'] or product['maturity_days'] or 0),
                     int(product['repricing_lag_seconds'] or 0), effective, apr, fee,
                     mandatory_fee, collateral_amount,
                     product['day_count_convention'], product['amortization_type'],
                     int(product['payment_frequency_days'] or 30), asset_collateral_value,
                     (amount/total_collateral_value if total_collateral_value else None),
                     underwriting['dti'], underwriting['dsr'], borrower_risk_score,
                     effective_income, home,
                     float(product['prepayment_fee_percent'] or 0),
                     int(product['prepayment_fee_decline_days'] or 0)),
                )
                loan_id = int(inserted.lastrowid)
                self._pledge_asset_collateral(
                    conn, loan_id, user_id, asset, asset_plan, haircut, opened_epoch
                )
                if schedule:
                    scheduled_rows = []
                    for index, (due_epoch, schedule_row) in enumerate(
                        zip(due_epochs, schedule), 1
                    ):
                        _days, principal_due, interest_due = schedule_row
                        scheduled_rows.append(
                            (loan_id, index, due_epoch, principal_due, interest_due,
                             contract_rate, product['day_count_convention'], opened_epoch)
                        )
                    conn.executemany(
                        "INSERT INTO accounts.loan_payment_schedule "
                        "(loan_id,installment_number,due_epoch,principal_due,interest_due,"
                        "rate_applied,day_count_convention,recast_epoch) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        scheduled_rows,
                    )
                payout = conn.execute(
                    "SELECT a.account_number FROM accounts.accounts a "
                    "JOIN product.saving s ON s.product_code = a.product_code "
                    "WHERE a.user_id = ? AND s.issuer IS ? AND a.account_type = 'saving' "
                    "AND a.quote_currency=? "
                    "ORDER BY a.created_at LIMIT 1",
                    (str(user_id), product["issuer"],home),
                ).fetchone()
                if payout:
                    payout_number = payout["account_number"]
                    conn.execute(
                        "UPDATE accounts.accounts SET balance_units = balance_units + CAST(ROUND(? * 1000000) AS INTEGER), "
                        "principal = COALESCE(principal, 0) + ?, last_payment_epoch = ?, "
                        "last_payment = CURRENT_TIMESTAMP "
                        "WHERE account_number = ? AND user_id = ?",
                        (net_payout, net_payout, time.time(), payout_number, str(user_id)),
                    )
                else:
                    payout_number = self._generate_account_number(conn)
                    conn.execute(
                        "INSERT INTO accounts.accounts "
                        "(account_number, user_id, product_code, balance, password, principal, "
                        "last_payment_epoch,account_type,quote_currency) "
                        "VALUES (?, ?, ?, ?, NULL, 0, unixepoch('now'), 'loan_payout',?)",
                        (payout_number, str(user_id), product_code, net_payout,home),
                    )
                if total_finance_fee:
                    RateManager.record_institution_result(
                        conn, product["issuer"], total_finance_fee, "origination_fee",
                        f"loan:{loan_id}", now=opened_epoch, currency_code=home,
                    )
                RateManager.refresh_all_market_pressure(conn, now=opened_epoch)
            return True, (
                f"✅ 대출이 승인되었습니다. 실수령액 {net_payout:,} {home}를 입금했습니다.\n"
                f"명목금리 연 {contract_rate:g}% · 실효금리 연 {effective:.3f}% · APR {apr:.3f}%"
                + (f" · 수수료 {total_finance_fee:,}" if total_finance_fee else "")
                + (f" · DTI {underwriting['dti']:.1f}% · DSR {underwriting['dsr']:.1f}%"
                   if underwriting['dti'] is not None else "")
                + (f" · 현금담보 {collateral_amount:,}" if collateral_amount else "")
                + (f" · 자산담보 {asset['code']} {float(collateral_asset_quantity):g}개"
                   if asset else "")
                + f"\n입금 계좌번호: `{payout_number}`"
            )
        except Exception as exc:
            return False, f"❌ 대출 처리 중 오류가 발생했습니다: {exc}"

    def extend_loan(self, user_id, loan_id, now=None):
        """Extend an owned loan once per maturity window, subject to product credit rules."""
        now = time.time() if now is None else float(now)
        if not math.isfinite(now):
            return False, '❌ 처리 시각이 올바르지 않습니다.'
        try:
            identity = str(loan_id).strip()
            self._settle_user_interest(user_id, now=now)
            with self.db.atomic() as conn:
                user = conn.execute("SELECT credit_score FROM users WHERE user_id=? AND "
                    f"{ACTIVE_OR_TREASURY}", (str(user_id),)).fetchone()
                if not user:
                    return False, '❌ 등록된 사용자만 대출을 연장할 수 있습니다.'
                # Product IDs are accepted as well as the individual loan number;
                # each user has at most one contract per product.
                rows = conn.execute("SELECT l.*,p.extension_days,p.extension_min_credit_score,p.status product_status "
                    "FROM loans l JOIN liability p ON p.product_code=l.product_code "
                    "WHERE l.user_id=? AND l.status='active' AND "
                    "(CAST(l.loan_id AS TEXT)=? OR l.product_code=?)", (str(user_id),identity,identity)).fetchall()
                if len(rows)!=1:
                    return False, '❌ 본인의 활성 대출 번호 또는 대출 상품 id를 입력해주세요.'
                loan = rows[0]
                days, score = int(loan['extension_days']), int(loan['extension_min_credit_score'])
                if loan['product_status']!='active' or not 1 <= days <= 3650:
                    return False, '❌ 이 상품은 대출연장이 허용되지 않습니다. extension_days를 확인해주세요.'
                if not 0 <= score <= 1000:
                    return False, '❌ 대출연장 신용도 설정을 확인해주세요.'
                if int(user['credit_score'] or 0)<score:
                    return False, f'❌ 대출연장에 신용점수 {score}점 이상이 필요합니다.'
                before = loan['maturity_epoch']
                if before is None:
                    return False, '❌ 만기가 없는 대출은 연장할 수 없습니다.'
                # A second click cannot keep extending indefinitely: it becomes
                # available again only when the previous maturity has arrived.
                eligible=loan['extension_eligible_epoch']
                if eligible is not None and now < float(eligible):
                    return False, '❌ 이미 연장한 대출입니다. 이전 만기 이후 다시 신청해주세요.'
                from managers.interest_manager import InterestManager
                product = conn.execute('SELECT * FROM liability WHERE product_code=?', (loan['product_code'],)).fetchone()
                InterestManager.settle_loan_rate_boundary(conn, loan, product, now)
                extension = float(product['extension_interest_rate'] or 0)
                applied = extension if extension > 0 else float(loan['contract_rate'] if loan['contract_rate'] is not None else product['interest_rate'])
                RateManager.require_legal_rate(conn, applied, nationality=loan['rate_nationality'], currency_code=loan['quote_currency'])
                conn.execute('UPDATE loans SET contract_rate=? WHERE loan_id=?', (applied, loan['loan_id']))
                after = max(float(before), now)+days*86400
                conn.execute("UPDATE loans SET maturity_epoch=?,extension_count=extension_count+1,"
                    "last_extended_epoch=?,extension_eligible_epoch=?,collection_status='current',"
                    "overdue_since_epoch=NULL,overdue_last_epoch=NULL,overdue_interest_remainder=0 "
                    "WHERE loan_id=?",
                    (after,now,max(float(before),now+days*86400),loan['loan_id']))
            return True, f"✅ 대출 #{loan['loan_id']} 만기를 {days}일 연장했습니다.\n변경 만기: <t:{int(after)}:F> · 적용 연이율: {applied:g}%"
        except (TypeError, ValueError, OverflowError) as exc:
            return False, f'❌ 대출연장 설정을 확인해주세요: {exc}'

    def repay_loan(self, user_id, product_code, amount, loan_id=None):
        if isinstance(amount, bool):
            return False, "❌ 상환액을 확인해주세요."
        try:
            self._settle_user_interest(user_id)
            with self.db.atomic(("users", "accounts")) as conn:
                if loan_id is None:
                    loan = conn.execute(
                        "SELECT l.loan_id,l.outstanding_balance,l.status,l.provisioned_loss,l.quote_currency,"
                        "l.created_at,l.maturity_epoch,l.prepayment_fee_percent,"
                        "l.prepayment_fee_decline_days,"
                        "p.issuer FROM accounts.loans l JOIN product.liability p "
                        "ON p.product_code=l.product_code WHERE l.user_id=? "
                        "AND l.product_code=? AND l.status IN ('active','defaulted')",
                        (str(user_id), product_code),
                    ).fetchone()
                else:
                    loan = conn.execute(
                        "SELECT l.loan_id,l.outstanding_balance,l.status,l.provisioned_loss,l.quote_currency,"
                        "l.created_at,l.maturity_epoch,l.prepayment_fee_percent,"
                        "l.prepayment_fee_decline_days,"
                        "p.issuer FROM accounts.loans l JOIN product.liability p "
                        "ON p.product_code=l.product_code WHERE l.loan_id=? AND l.user_id=? "
                        "AND l.product_code=? AND l.status IN ('active','defaulted')",
                        (int(loan_id), str(user_id), product_code),
                    ).fetchone()
                if not loan:
                    return False, "❌ 상환할 대출을 찾을 수 없습니다."
                amount = parse_currency_money(
                    conn, amount, loan["quote_currency"], minimum=0
                )
                if amount <= 0:
                    return False, "❌ 상환액은 0보다 커야 합니다."
                outstanding = round_currency(
                    conn, loan["outstanding_balance"] or 0, loan["quote_currency"]
                )
                payment = min(amount, outstanding)
                now = time.time()
                prepayment_fee = 0
                if (loan["status"] == "active"
                        and loan["maturity_epoch"] is not None
                        and now < float(loan["maturity_epoch"])
                        and float(loan["prepayment_fee_percent"] or 0) > 0):
                    opened = datetime.strptime(
                        loan["created_at"], "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc).timestamp()
                    decline_days = int(loan["prepayment_fee_decline_days"] or 0)
                    factor = (1.0 if decline_days <= 0 else
                              max(0.0, 1.0-(now-opened)/(decline_days*86400)))
                    prepayment_fee = round_currency(
                        conn,
                        payment*float(loan["prepayment_fee_percent"])*factor/100,
                        loan["quote_currency"],
                    )
                total_debit = payment+prepayment_fee
                funds = UserFunds.choose(conn, str(user_id), loan["quote_currency"])
                if not funds or funds[0] < total_debit:
                    return False, "❌ 상환할 잔액이 부족합니다."
                if not UserFunds.debit(conn, str(user_id), funds[1], total_debit):
                    return False, "❌ 잔액이 부족합니다."
                remaining = round_currency(
                    conn, max(0, outstanding-payment), loan["quote_currency"]
                )
                released = 0
                if remaining <= 0:
                    released = self._release_collateral(conn, loan["loan_id"], str(user_id))
                conn.execute(
                    "UPDATE accounts.loans SET outstanding_balance = ?, "
                    "status = CASE WHEN ? <= 0 THEN 'paid' ELSE status END, "
                    "collection_status=CASE WHEN ? <= 0 THEN 'paid' ELSE collection_status END,"
                    "provisioned_loss=MAX(0,provisioned_loss-?),"
                    "prepayment_fees_paid=prepayment_fees_paid+? "
                    "WHERE loan_id = ?",
                    (remaining, remaining, remaining,
                     min(payment, float(loan["provisioned_loss"] or 0)),
                     prepayment_fee, loan["loan_id"]),
                )
                if prepayment_fee:
                    RateManager.record_institution_result(
                        conn, loan["issuer"], prepayment_fee, "prepayment_fee",
                        f"loan:{loan['loan_id']}:prepayment:{time.time_ns()}",
                        now=now, currency_code=loan["quote_currency"],
                    )
                recovery = min(payment, float(loan["provisioned_loss"] or 0))
                if recovery:
                    RateManager.record_institution_result(
                        conn, loan["issuer"], recovery, "default_recovery",
                        f"loan:{loan['loan_id']}:payment:{time.time_ns()}",
                        currency_code=loan["quote_currency"],
                    )
                self._allocate_schedule_payment(
                    conn, loan["loan_id"], payment, now
                )
                RateManager.refresh_all_market_pressure(conn)
            currency = loan["quote_currency"]
            suffix = f" · 담보 {released:,} {currency} 반환" if released else ""
            fee_text = (
                f" · 중도상환수수료 {prepayment_fee:,} {currency}"
                if prepayment_fee else ""
            )
            return True, (
                f"✅ {payment:,} {currency}를 상환했습니다. "
                f"남은 원리금: {remaining:,} {currency}{fee_text}{suffix}"
            )
        except Exception as exc:
            return False, f"❌ 대출 상환 처리 중 오류가 발생했습니다: {exc}"

    def process_scheduled_loan_payments(self, now=None):
        """상품 설정 주기마다 가장 잔액이 큰 자금원에서 정기 상환합니다."""
        now = time.time() if now is None else float(now)
        paid_count = missed_count = 0
        notifications = []
        max_catch_up = 100
        try:
            due_users = set()
            for row in self.db.accounts.execute(
                "SELECT DISTINCT l.user_id FROM loans l "
                "JOIN loan_payment_schedule s ON s.loan_id=l.loan_id "
                "WHERE l.status='active' AND s.due_epoch<=? "
                "AND s.status NOT IN ('paid','waived')", (now,)
            ):
                due_users.add(str(row["user_id"]))
            for row in self.db.accounts.execute(
                "SELECT user_id, product_code, last_repayment_epoch FROM loans WHERE status = 'active'"
            ):
                product = self.db.products.get_liability(row["product_code"])
                if not product:
                    continue
                try:
                    cycle = float(product["repayment_cycle_seconds"] or 0)
                    last = float(row["last_repayment_epoch"] or now)
                except (TypeError, ValueError, OverflowError):
                    continue
                if cycle > 0 and now - last >= cycle:
                    due_users.add(str(row["user_id"]))
            for due_user_id in due_users:
                self._settle_user_interest(due_user_id, now=now)

            with self.db.atomic(("users", "accounts", "product")) as conn:
                loans = conn.execute(
                    "SELECT l.loan_id, l.user_id, l.product_code, l.outstanding_balance, "
                    "l.last_repayment_epoch, p.name AS product_name, "
                    "p.repayment_cycle_seconds, p.repayment_amount, l.amortization_type,"
                    "l.quote_currency "
                    "FROM accounts.loans l JOIN product.liability p "
                    "ON p.product_code = l.product_code WHERE l.status = 'active'"
                ).fetchall()
                for loan in loans:
                    if loan["amortization_type"] != "legacy":
                        paid, missed, amount_paid, balance = self._process_due_schedule(
                            conn, loan, now
                        )
                        paid_count += paid
                        missed_count += missed
                        if paid or missed:
                            notifications.append({
                                "user_id": str(loan["user_id"]),
                                "product_name": str(loan["product_name"] or loan["product_code"]),
                                "paid_count": paid,
                                "paid_amount": amount_paid,
                                "missed_count": missed,
                                "remaining": balance,
                            })
                        continue
                    try:
                        cycle = int(loan["repayment_cycle_seconds"] or 0)
                        installment = round_currency(
                            conn, loan["repayment_amount"] or 0, loan["quote_currency"]
                        )
                        balance = round_currency(
                            conn, loan["outstanding_balance"] or 0, loan["quote_currency"]
                        )
                        last = float(loan["last_repayment_epoch"] or now)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if cycle <= 0 or installment <= 0:
                        continue
                    if balance <= 0 or not math.isfinite(last):
                        if balance <= 0:
                            self._release_collateral(
                                conn, loan["loan_id"], str(loan["user_id"])
                            )
                            conn.execute(
                                "UPDATE accounts.loans SET outstanding_balance = 0, status = 'paid',"
                                "collection_status='paid' "
                                "WHERE loan_id = ?",
                                (loan["loan_id"],),
                            )
                        continue

                    due_periods = int((now - last) // cycle)
                    if due_periods <= 0:
                        continue

                    periods_to_process = min(due_periods, max_catch_up)
                    processed = 0
                    loan_paid_count = loan_missed_count = 0
                    loan_paid_amount = 0
                    for _ in range(periods_to_process):
                        if balance <= 0:
                            conn.execute(
                                "UPDATE accounts.loans SET outstanding_balance = 0, status = 'paid' "
                                "WHERE loan_id = ?",
                                (loan["loan_id"],),
                            )
                            break

                        payment = min(installment, balance)
                        funds = UserFunds.choose(
                            conn, str(loan["user_id"]), loan["quote_currency"]
                        )
                        if funds and funds[0] >= payment and UserFunds.debit(
                            conn, str(loan["user_id"]), funds[1], payment
                        ):
                            balance = max(0, balance - payment)
                            paid_count += 1
                            loan_paid_count += 1
                            loan_paid_amount += payment
                            conn.execute(
                                "UPDATE accounts.loans SET outstanding_balance = ?, "
                                "status = CASE WHEN ? <= 0 THEN 'paid' ELSE 'active' END "
                                "WHERE loan_id = ?",
                                (balance, balance, loan["loan_id"]),
                            )
                            if balance <= 0:
                                self._release_collateral(
                                    conn, loan["loan_id"], str(loan["user_id"])
                                )
                        else:
                            missed_count += 1
                            loan_missed_count += 1
                            conn.execute(
                                "UPDATE users.users SET credit_score = "
                                "MAX(0, COALESCE(credit_score, 0) - 1) WHERE user_id = ?",
                                (str(loan["user_id"]),),
                            )
                        processed += 1
                        if balance <= 0:
                            break

                    if processed:
                        conn.execute(
                            "UPDATE accounts.loans SET last_repayment_epoch = ? "
                            "WHERE loan_id = ? AND status = 'active'",
                            (last + processed * cycle, loan["loan_id"]),
                        )
                    if loan_paid_count or loan_missed_count:
                        notifications.append({
                            "user_id": str(loan["user_id"]),
                            "product_name": str(loan["product_name"] or loan["product_code"]),
                            "paid_count": loan_paid_count,
                            "paid_amount": round_currency(
                                conn, loan_paid_amount, loan["quote_currency"]
                            ),
                            "missed_count": loan_missed_count,
                            "remaining": balance,
                        })
                RateManager.refresh_all_market_pressure(conn, now=now)
            return paid_count, missed_count, notifications
        except Exception:
            raise

    @staticmethod
    def _allocate_schedule_payment(conn, loan_id, amount, paid_epoch):
        remaining = max(0.0, float(amount or 0))
        if not remaining:
            return
        rows = conn.execute(
            "SELECT schedule_id,principal_due+interest_due+fee_due AS due,amount_paid "
            "FROM accounts.loan_payment_schedule WHERE loan_id=? "
            "AND status NOT IN ('paid','waived') ORDER BY installment_number",
            (loan_id,),
        ).fetchall()
        for row in rows:
            unpaid = max(0.0, float(row["due"] or 0)-float(row["amount_paid"] or 0))
            applied = min(remaining, unpaid)
            if not applied:
                continue
            new_paid = float(row["amount_paid"] or 0)+applied
            status = "paid" if new_paid >= float(row["due"] or 0)-1e-9 else "partial"
            conn.execute(
                "UPDATE accounts.loan_payment_schedule SET amount_paid=?,status=?,"
                "paid_epoch=CASE WHEN ?='paid' THEN ? ELSE paid_epoch END WHERE schedule_id=?",
                (new_paid, status, status, paid_epoch, row["schedule_id"]),
            )
            remaining -= applied
            if remaining <= 0:
                break

    @classmethod
    def _process_due_schedule(cls, conn, loan, now):
        rows = conn.execute(
            "SELECT * FROM accounts.loan_payment_schedule WHERE loan_id=? "
            "AND due_epoch<=? AND status NOT IN ('paid','waived') "
            "ORDER BY installment_number",
            (loan["loan_id"], now),
        ).fetchall()
        balance = round_currency(
            conn, loan["outstanding_balance"] or 0, loan["quote_currency"]
        )
        paid_count = missed_count = paid_amount = 0
        for row in rows:
            contractual = (float(row["principal_due"] or 0)
                           + float(row["interest_due"] or 0)
                           + float(row["fee_due"] or 0))
            due = min(balance, max(0, contractual-float(row["amount_paid"] or 0)))
            if due <= 0:
                conn.execute(
                    "UPDATE accounts.loan_payment_schedule SET status='paid',paid_epoch=? "
                    "WHERE schedule_id=?", (now, row["schedule_id"]),
                )
                continue
            funds = UserFunds.choose(
                conn, str(loan["user_id"]), loan["quote_currency"]
            )
            if funds and funds[0] >= due and UserFunds.debit(
                conn, str(loan["user_id"]), funds[1], due
            ):
                balance -= due
                paid_count += 1
                paid_amount += due
                conn.execute(
                    "UPDATE accounts.loan_payment_schedule SET amount_paid=amount_paid+?,"
                    "status='paid',paid_epoch=? WHERE schedule_id=?",
                    (due, now, row["schedule_id"]),
                )
            else:
                newly_missed = row["status"] != "missed"
                missed_count += int(newly_missed)
                conn.execute(
                    "UPDATE accounts.loan_payment_schedule SET status='missed' "
                    "WHERE schedule_id=?", (row["schedule_id"],),
                )
                if newly_missed:
                    conn.execute(
                        "UPDATE users.users SET credit_score=MAX(0,credit_score-1) "
                        "WHERE user_id=?", (str(loan["user_id"]),),
                    )
        status = "paid" if balance <= 0 else "active"
        conn.execute(
            "UPDATE accounts.loans SET outstanding_balance=?,status=?,"
            "collection_status=CASE WHEN ?='paid' THEN 'paid' ELSE collection_status END,"
            "missed_payment_count=missed_payment_count+? WHERE loan_id=?",
            (max(0, balance), status, status, missed_count, loan["loan_id"]),
        )
        if balance <= 0:
            cls._release_collateral(conn, loan["loan_id"], str(loan["user_id"]))
        return paid_count, missed_count, paid_amount, max(0, balance)

    @staticmethod
    def _generate_account_number(conn):
        for _ in range(100):
            number = "".join(str(random.randint(0, 9)) for _ in range(10))
            exists = conn.execute(
                "SELECT 1 FROM accounts.accounts WHERE account_number = ?", (number,)
            ).fetchone()
            if not exists and not DatabaseManager.code_locations(conn, number):
                return number
        raise RuntimeError("사용 가능한 계좌번호를 생성할 수 없습니다.")

    @staticmethod
    def _release_collateral(conn, loan_id, user_id):
        row = conn.execute(
            "SELECT collateral_amount,collateral_released,collateral_seized FROM accounts.loans "
            "WHERE loan_id=?", (loan_id,)
        ).fetchone()
        if not row:
            return 0
        currency = conn.execute(
            "SELECT quote_currency FROM accounts.loans WHERE loan_id=?", (loan_id,)
        ).fetchone()[0]
        remaining = max(0.0, float(row['collateral_amount'] or 0)
                        - float(row['collateral_released'] or 0)
                        - float(row['collateral_seized'] or 0))
        if remaining:
            UserFunds.credit(conn, user_id, remaining, currency=currency)
            conn.execute(
                "UPDATE accounts.loans SET collateral_released=collateral_released+? WHERE loan_id=?",
                (remaining, loan_id),
            )
        asset_rows = conn.execute(
            "SELECT * FROM accounts.loan_collateral_assets "
            "WHERE loan_id=? AND status='pledged' ORDER BY collateral_id",
            (loan_id,),
        ).fetchall()
        for asset in asset_rows:
            existing = conn.execute(
                "SELECT holding_id,quantity,average_cost FROM assets.user_assets "
                "WHERE user_id=? AND asset_code=? AND account_number IS ?",
                (str(user_id), asset["asset_code"], asset["source_account"]),
            ).fetchone()
            if existing:
                old_quantity = float(existing["quantity"])
                added = float(asset["quantity"])
                average = round_currency(conn,
                    (old_quantity*float(existing["average_cost"] or 0)
                     + added*float(asset["average_cost"] or 0))/(old_quantity+added),
                    currency
                )
                conn.execute(
                    "UPDATE assets.user_assets SET quantity=quantity+?,average_cost=? "
                    "WHERE holding_id=?",
                    (added, average, existing["holding_id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO assets.user_assets "
                    "(user_id,asset_code,account_number,quantity,average_cost) "
                    "VALUES (?,?,?,?,?)",
                    (str(user_id), asset["asset_code"], asset["source_account"],
                     asset["quantity"], asset["average_cost"]),
                )
            conn.execute(
                "UPDATE accounts.loan_collateral_assets SET status='released' "
                "WHERE collateral_id=?", (asset["collateral_id"],),
            )
        return remaining

    @staticmethod
    def resolve_failed_institution(conn, issuer, *, now=None):
        """Freeze a failed issuer and pay insured deposits once per account."""
        issuer = str(issuer or "").strip()
        when = float(time.time() if now is None else now)
        institution = conn.execute(
            "SELECT f.status,f.deposit_insurance_limit,"
            "COALESCE(c.currency_code,'NWX') base_currency "
            "FROM financial_institutions f LEFT JOIN country_currencies c "
            "ON c.nationality=f.nationality WHERE f.issuer=?", (issuer,),
        ).fetchone()
        if not institution or institution["status"] != "failed":
            return {"accounts": 0, "covered": 0, "uninsured": 0}
        accounts = conn.execute(
            "SELECT a.account_number,a.user_id,a.balance,a.deposit_insured,a.quote_currency "
            "FROM accounts.accounts a JOIN product.saving p "
            "ON p.product_code=a.product_code WHERE p.issuer=? "
            "AND a.account_type='saving' "
            "AND a.resolution_status='active' ORDER BY a.user_id,a.account_number",
            (issuer,),
        ).fetchall()
        limit = max(0.0, float(institution["deposit_insurance_limit"] or 0))
        from managers.currency_manager import CurrencyManager, SCALE
        base_currency = str(institution["base_currency"] or "NWX")
        used_by_user = {}
        covered_total = uninsured_total = 0
        for account in accounts:
            balance = max(0.0, float(account["balance"] or 0))
            user_id = str(account["user_id"])
            used_base = used_by_user.get(user_id, 0.0)
            remaining_base = max(0.0, limit-used_base)
            if int(account["deposit_insured"] or 0) and remaining_base > 0:
                if account["quote_currency"] == base_currency:
                    available_cover = remaining_base
                else:
                    available_cover = CurrencyManager.convert(
                        conn, int(round(remaining_base*SCALE)), base_currency,
                        account["quote_currency"]
                    )/SCALE
                covered = min(balance, available_cover)
            else:
                covered = 0
            uninsured = balance-covered
            if account["quote_currency"] == base_currency:
                covered_base = covered
                uninsured_base = uninsured
            else:
                covered_base = CurrencyManager.convert(
                    conn, int(round(covered*SCALE)), account["quote_currency"],
                    base_currency
                )/SCALE if covered else 0
                uninsured_base = CurrencyManager.convert(
                    conn, int(round(uninsured*SCALE)), account["quote_currency"],
                    base_currency
                )/SCALE if uninsured else 0
            used_by_user[user_id] = used_base+covered_base
            if covered:
                paid = conn.execute(
                    "UPDATE users.users SET cash=cash+? WHERE user_id=?",
                    (covered, user_id),
                )
                if paid.rowcount != 1:
                    raise RuntimeError("예금보험금을 받을 사용자를 찾을 수 없습니다.")
                CurrencyManager.treasury_pay(
                    conn, CurrencyManager.user_treasury_id(conn, user_id),
                    account["quote_currency"] or "NWX", int(round(covered*SCALE)))
            conn.execute(
                "UPDATE accounts.accounts SET balance_units=0,principal=0,"
                "resolution_status='resolved',insurance_paid=?,uninsured_claim=? "
                "WHERE account_number=?",
                (covered, uninsured, account["account_number"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO accounts.deposit_insurance_claims "
                "(issuer,user_id,account_number,covered_amount,uninsured_amount,resolved_epoch) "
                "VALUES (?,?,?,?,?,?)",
                (issuer, user_id, account["account_number"], covered, uninsured, when),
            )
            covered_total += covered_base
            uninsured_total += uninsured_base
        conn.execute("UPDATE product.saving SET status='inactive' WHERE issuer=?", (issuer,))
        conn.execute("UPDATE product.liability SET status='inactive' WHERE issuer=?", (issuer,))
        return {
            "accounts": len(accounts),
            "covered": covered_total,
            "uninsured": uninsured_total,
        }

    @staticmethod
    def resolve_all_failed_institutions(conn, *, now=None):
        return {
            row["issuer"]: BankManager.resolve_failed_institution(
                conn, row["issuer"], now=now
            )
            for row in conn.execute(
                "SELECT issuer FROM financial_institutions WHERE status='failed'"
            )
        }

    @staticmethod
    def _liquidate_collateral_lot(conn, loan_id, row, mark_price, product, now,
                                  currency):
        """Sell whole units into resting bids, then use a depth-adjusted backstop."""
        total_quantity = float(row["quantity"] or 0)
        remaining = total_quantity
        gross = 0
        book_quantity = 0.0
        last_execution = None
        # Resting buyers already deposited limit_price * remaining_quantity.
        # Consuming their bids therefore creates an actual cash-backed sale.
        bids = conn.execute(
            "SELECT * FROM assets.market_orders WHERE order_kind='asset' "
            "AND asset_code=? AND side='buy' AND status IN ('open','partial') "
            "AND user_id<>? "
            "ORDER BY limit_price DESC,created_epoch,order_id",
            (row["asset_code"], str(row["user_id"])),
        ).fetchall()
        for bid in bids:
            affordable = math.floor(
                float(bid["reserved_cash"] or 0)
                / max(1e-12, float(bid["limit_price"]))
            )
            fill = min(int(math.floor(remaining+1e-9)),
                       int(bid["remaining_quantity"] or 0), affordable)
            if fill <= 0:
                continue
            price = max(10**(-currency_precision(conn, currency)),
                        float(bid["limit_price"]))
            conn.execute(
                "INSERT INTO assets.user_assets "
                "(user_id,asset_code,account_number,quantity,average_cost) VALUES (?,?,?,?,?) "
                "ON CONFLICT(user_id,asset_code,IFNULL(account_number,'')) DO UPDATE SET "
                "average_cost=((quantity*average_cost)+(excluded.quantity*excluded.average_cost))"
                "/(quantity+excluded.quantity),quantity=quantity+excluded.quantity",
                (str(bid["user_id"]), row["asset_code"], bid["funding_account"], fill, price),
            )
            new_remaining = int(bid["remaining_quantity"])-fill
            conn.execute(
                "UPDATE assets.market_orders SET remaining_quantity=?,"
                "filled_quantity=filled_quantity+?,reserved_cash=MAX(0,reserved_cash-?),"
                "status=?,updated_epoch=? WHERE order_id=?",
                (new_remaining, fill, round_currency(conn, price*fill, currency),
                 "filled" if new_remaining == 0 else "partial", int(now), bid["order_id"]),
            )
            gross += round_currency(conn, price*fill, currency)
            remaining -= fill
            book_quantity += fill
            last_execution = price
            if remaining <= 1e-9:
                break

        if last_execution is not None:
            from managers.order_book_manager import OrderBookManager
            OrderBookManager._apply_execution_price(
                conn, row["asset_code"], last_execution, int(now)
            )

        backstop_quantity = max(0.0, remaining)
        if backstop_quantity > 1e-9:
            depth = conn.execute(
                "SELECT liquidity,total_supply AS supply FROM assets.market_assets WHERE code=? "
                "UNION ALL SELECT liquidity,total_shares FROM company.company WHERE company_code=? "
                "UNION ALL SELECT liquidity,total_supply FROM product.investment WHERE product_code=? "
                "LIMIT 1",
                (row["asset_code"], row["asset_code"], row["asset_code"]),
            ).fetchone()
            liquidity = max(.01, float(depth["liquidity"] or .2)) if depth else .2
            supply = max(1.0, float(depth["supply"] or 1000)) if depth else 1000.0
            market_impact = min(.50, .25*backstop_quantity/max(1.0, liquidity*supply))
            backstop_price = max(0.0, float(mark_price) * (
                1.0-float(row["haircut"] or 0)-market_impact
            ))
            backstop_cost = round_currency(conn, backstop_price*backstop_quantity, currency)
            gross += backstop_cost
            # The lending bank's head office buys the unsold rest and pays for it (deficits allowed).
            from managers.currency_manager import CurrencyManager, SCALE
            bank = CurrencyManager.bank_account_id(conn, product["issuer"])
            bank_home = CurrencyManager.home(conn, bank)
            cost_units = int(round(backstop_cost*SCALE))
            if currency != bank_home:
                cost_units = CurrencyManager.convert(conn, cost_units, currency, bank_home)
            if cost_units:
                CurrencyManager.change(conn, bank, bank_home, -cost_units, allow_deficit=True)
            existing = conn.execute(
                "SELECT holding_id,quantity,average_cost FROM assets.user_assets "
                "WHERE user_id=? AND asset_code=? AND account_number IS NULL",
                (bank, row["asset_code"]),
            ).fetchone()
            if existing:
                old_quantity = float(existing["quantity"])
                average = round_currency(conn,
                    (old_quantity*float(existing["average_cost"] or 0)
                     + backstop_quantity*backstop_price)/(old_quantity+backstop_quantity),
                    currency
                )
                conn.execute(
                    "UPDATE assets.user_assets SET quantity=quantity+?,average_cost=? "
                    "WHERE holding_id=?",
                    (backstop_quantity, average, existing["holding_id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO assets.user_assets "
                    "(user_id,asset_code,account_number,quantity,average_cost) "
                    "VALUES (?,?,NULL,?,?)",
                    (bank, row["asset_code"], backstop_quantity,
                     round_currency(conn, backstop_price, currency)),
                )

        liquidation_cost = round_currency(
            conn, gross*float(product["liquidation_cost_percent"] or 0)/100,
            currency
        )
        net = max(0, gross-liquidation_cost)
        weighted_price = gross/total_quantity if total_quantity > 0 else 0.0
        conn.execute(
            "UPDATE accounts.loan_collateral_assets SET status='liquidated',"
            "liquidation_price=?,liquidation_cost=?,liquidation_proceeds=? "
            "WHERE collateral_id=?",
            (round_currency(conn, weighted_price, currency), liquidation_cost, net,
             row["collateral_id"]),
        )
        conn.execute(
            "INSERT OR IGNORE INTO accounts.collateral_liquidation_events "
            "(loan_id,collateral_id,asset_code,quantity,mark_price,execution_price,"
            "gross_proceeds,liquidation_cost,net_proceeds,book_filled_quantity,"
            "backstop_quantity,liquidated_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (loan_id, row["collateral_id"], row["asset_code"], total_quantity,
             mark_price, weighted_price, gross, liquidation_cost, net,
             book_quantity, backstop_quantity, float(now)),
        )
        return net

    @staticmethod
    def revalue_asset_collateral(conn, loan_id, outstanding_balance, product,
                                 *, liquidate=True, force=False):
        """Mark pledged assets to market and optionally liquidate on an LTV breach."""
        rows = conn.execute(
            "SELECT * FROM accounts.loan_collateral_assets "
            "WHERE loan_id=? AND status='pledged' ORDER BY collateral_id",
            (loan_id,),
        ).fetchall()
        if not rows:
            return float(outstanding_balance), None, 0
        currency = conn.execute(
            "SELECT quote_currency FROM accounts.loans WHERE loan_id=?", (loan_id,)
        ).fetchone()[0]
        total = 0
        marked = []
        for row in rows:
            quote = conn.execute(
                "SELECT price FROM assets.market_assets WHERE code=? "
                "UNION ALL SELECT share_price FROM company.company WHERE company_code=? "
                "UNION ALL SELECT unit_price FROM product.investment WHERE product_code=? LIMIT 1",
                (row["asset_code"], row["asset_code"], row["asset_code"]),
            ).fetchone()
            price = max(10**(-currency_precision(conn, currency)),
                        float(quote["price"] if quote else row["last_price"]))
            value = max(0, round_currency(conn,
                price*float(row["quantity"])*(1-float(row["haircut"])), currency
            ))
            total += value
            marked.append((row, price, value))
            conn.execute(
                "UPDATE accounts.loan_collateral_assets SET last_price=?,last_value=? "
                "WHERE collateral_id=?", (price, value, row["collateral_id"]),
            )
        ltv = float(outstanding_balance)/total if total > 0 else float("inf")
        margin_ltv = float(product["margin_call_ltv"] or .85)
        liquidation_ltv = max(margin_ltv, float(product["liquidation_ltv"] or 1))
        conn.execute(
            "UPDATE accounts.loans SET collateral_asset_value=?,current_ltv=?,"
            "collection_status=CASE WHEN collection_status IN ('current','margin_call') "
            "THEN ? ELSE collection_status END WHERE loan_id=?",
            (total, ltv if math.isfinite(ltv) else None,
             "margin_call" if ltv > margin_ltv else "current", loan_id),
        )
        if not liquidate or (not force and ltv <= liquidation_ltv):
            return float(outstanding_balance), ltv, 0
        proceeds = 0
        now = time.time()
        for row, price, value in marked:
            proceeds += BankManager._liquidate_collateral_lot(
                conn, loan_id, row, price, product, now, currency
            )
        new_balance = max(0, round_currency(
            conn, float(outstanding_balance)-proceeds, currency
        ))
        conn.execute(
            "UPDATE accounts.loans SET outstanding_balance=?,"
            "collateral_liquidated_value=collateral_liquidated_value+?,"
            "collection_status=CASE WHEN ?>0 THEN 'accelerated' ELSE 'paid' END,"
            "status=CASE WHEN ?>0 THEN status ELSE 'paid' END WHERE loan_id=?",
            (new_balance, proceeds, new_balance, new_balance, loan_id),
        )
        return new_balance, ltv, proceeds


class UserFunds:
    """대출 상환에서만 재사용하는 가장 잔액이 큰 자금원 헬퍼."""

    @staticmethod
    def choose(conn, user_id, currency="NWX"):
        from managers.currency_manager import CurrencyManager
        user = conn.execute("SELECT cash FROM users.users WHERE user_id = ?", (user_id,)).fetchone()
        if not user:
            return None
        sources = []
        if CurrencyManager.home(conn, user_id) == currency:
            sources.append((float(user["cash"] or 0), None))
        sources.extend(
            (float(row["balance"] or 0), row["account_number"])
            for row in conn.execute(
                "SELECT account_number,balance FROM accounts.accounts WHERE user_id=? "
                "AND quote_currency=? AND account_type IN ('saving','loan_payout')",
                (user_id, currency),
            ).fetchall()
        )
        if not sources:
            return None
        balance, account_number = max(sources, key=lambda source: source[0])
        return balance, account_number

    @staticmethod
    def debit(conn, user_id, account_number, amount):
        if account_number is None:
            cursor = conn.execute(
                "UPDATE users.users SET cash = cash - ? WHERE user_id = ? AND cash >= ?",
                (amount, user_id, amount),
            )
        else:
            now = time.time()
            cursor = conn.execute(
                "UPDATE accounts.accounts SET balance_units = balance_units - CAST(ROUND(? * 1000000) AS INTEGER), "
                "principal = MAX(0, COALESCE(principal, 0) - ?), last_payment_epoch = ?, "
                "last_payment = CURRENT_TIMESTAMP "
                "WHERE account_number = ? AND user_id = ? AND balance >= ?",
                (amount, amount, now, account_number, user_id, amount),
            )
        return cursor.rowcount == 1

    @staticmethod
    def credit(conn, user_id, amount, account_number=None, currency="NWX"):
        if amount <= 0:
            return True
        if account_number is None:
            from managers.currency_manager import CurrencyManager
            if CurrencyManager.home(conn, user_id) != currency:
                raise ValueError(f"{currency} 입금 계좌가 필요합니다.")
            cursor = conn.execute(
                "UPDATE users.users SET cash=cash+? WHERE user_id=?", (amount, user_id)
            )
        else:
            cursor = conn.execute(
                "UPDATE accounts.accounts SET balance_units = balance_units + CAST(ROUND(? * 1000000) AS INTEGER),principal=principal+? "
                "WHERE account_number=? AND user_id=?",
                (amount, amount, account_number, user_id),
            )
        return cursor.rowcount == 1
