import hashlib
import hmac
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from database import DatabaseManager
from database.user_db import TREASURY_USER_ID
from functions.money import round_money
from functions.interest_calc import year_fraction
from managers.rate_manager import RateManager


class BankManager:
    """예금 계좌 및 대출 업무를 담당합니다."""

    PASSWORD_ITERATIONS = 240_000

    @staticmethod
    def _loan_cashflows(principal, annual_rate, maturity_days, frequency_days,
                        amortization_type, day_count_convention="ACT/365",
                        start_epoch=None, due_epochs=None):
        """Build contractual installment cash flows for APR and servicing."""
        principal = int(principal)
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
            interest = round_money(principal*rate*sum(fractions))
            return [(due_days[-1], principal, max(0, interest))]
        if mode == "equal_principal":
            remaining = principal
            allocated = 0
            for index, (due_day, fraction) in enumerate(zip(due_days, fractions), 1):
                principal_due = (principal-allocated if index == count
                                 else round_money(principal/count))
                principal_due = max(0, min(remaining, principal_due))
                interest = round_money(remaining*rate*fraction)
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
            interest = max(0, round_money(remaining*periodic_rate))
            principal_due = (principal-allocated if index == count else
                             max(0, round_money(payment-interest)))
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
        balance = max(0, int(round(raw_balance or 0)))
        if balance <= 0:
            return 0
        due_epochs = [float(row["due_epoch"]) for row in rows]
        cashflows = cls._loan_cashflows(
            balance, annual_rate,
            max(1, math.ceil((due_epochs[-1]-float(as_of))/86400)),
            int(loan["payment_frequency_days"] or 30), mode,
            loan["day_count_convention"], float(as_of), due_epochs,
        )
        for row, (_days, principal_due, interest_due) in zip(rows, cashflows):
            already_paid = max(0, int(row["amount_paid"] or 0))
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
        if isinstance(deposit_amount, bool) or not isinstance(deposit_amount, int) or deposit_amount < 0:
            return False, "❌ 초기 예치금은 0 이상의 정수로 입력해주세요."
        if not isinstance(password, str) or not 4 <= len(password) <= 16:
            return False, "❌ 비밀번호는 4~16자로 입력해주세요."
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
                minimum = math.ceil(minimum_value)
                if deposit_amount < minimum:
                    return False, f"❌ 최소 예치금은 {minimum:,} {home}입니다."
                cursor = conn.execute(
                    "UPDATE users.users SET cash = cash - ? "
                    f"WHERE user_id = ? AND (status = 'active' OR user_id='{TREASURY_USER_ID}') AND cash >= ?",
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
                    "SELECT account_number,user_id,balance,password,principal,created_at,"
                    "early_withdrawal_rate,early_withdrawal_fee_percent,product_code "
                    "FROM accounts.accounts "
                    "WHERE account_number = ? AND user_id = ? AND account_type = 'saving'",
                    (str(account_number), str(user_id)),
                ).fetchone()
                if not source:
                    return False, "❌ 해당 계좌를 찾을 수 없거나 권한이 없습니다."
                if not self._verify_password(password, source["password"]):
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
                        "WHERE account_number = ? AND user_id = ? AND account_number <> ?",
                        (str(destination_account), str(user_id), str(account_number)),
                    ).fetchone()
                    if not destination:
                        return False, "❌ 이관 대상은 본인 명의의 다른 계좌여야 합니다."
                    destination_number = destination["account_number"]
                else:
                    destination = conn.execute(
                        "SELECT account_number FROM accounts.accounts WHERE user_id = ? "
                        "AND account_number <> ? AND account_type = 'saving' "
                        "ORDER BY balance DESC LIMIT 1",
                        (str(user_id), str(account_number)),
                    ).fetchone()
                    destination_number = destination["account_number"] if destination else None

                balance = int(round(source["balance"] or 0))
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
                        principal = max(0, int(source["principal"] or 0))
                        earned = max(0, balance-principal)
                        allowed = max(0, round_money(
                            principal*float(source["early_withdrawal_rate"] or 0)/100
                            * year_fraction(now-opened, product["day_count_convention"], opened)
                        ))
                        fee = round_money(
                            principal*float(source["early_withdrawal_fee_percent"] or 0)/100
                        )
                        early_penalty = min(balance, max(0, earned-allowed)+fee)
                        balance -= early_penalty
                        if early_penalty:
                            conn.execute(
                                "UPDATE users.users SET cash=cash+? WHERE user_id=?",
                                (early_penalty, TREASURY_USER_ID),
                            )
                if destination_number:
                    conn.execute(
                        "UPDATE accounts.accounts SET balance = balance + ?, "
                        "principal = COALESCE(principal, 0) + ?, "
                        "last_payment_epoch = ?, last_payment = CURRENT_TIMESTAMP "
                        "WHERE account_number = ?",
                        (balance, balance, time.time(), destination_number),
                    )
                else:
                    conn.execute("UPDATE users.users SET cash = cash + ? WHERE user_id = ?", (balance, str(user_id)))

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
            penalty_text = f" 중도해지 조정 {early_penalty:,} NWX를 차감했습니다." if early_penalty else ""
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
        if not asset or asset["status"] != "active" or int(asset["price"] or 0) <= 0:
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
                    "average_cost": int(row["average_cost"] or 0),
                })
                left -= take
            if left <= 1e-9:
                break
        return plan, int(round(float(asset["price"])*quantity)), asset

    @staticmethod
    def _pledge_asset_collateral(conn, loan_id, user_id, asset, plan, haircut, now):
        if not plan:
            return
        price = int(asset["price"])
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
            value = round_money(price*lot["quantity"]*(1-float(haircut)))
            conn.execute(
                "INSERT INTO accounts.loan_collateral_assets "
                "(loan_id,user_id,asset_code,source_account,quantity,average_cost,"
                "pledged_epoch,haircut,last_price,last_value) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (loan_id, str(user_id), asset["code"], lot["source_account"],
                 lot["quantity"], lot["average_cost"], now, float(haircut), price, value),
            )

    def apply_loan(self, user_id, product_code, amount, collateral_amount=0,
                   collateral_asset_code=None, collateral_asset_quantity=0):
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            return False, "❌ 대출 금액은 0보다 큰 정수로 입력해주세요."
        if (isinstance(collateral_amount, bool) or not isinstance(collateral_amount, int)
                or collateral_amount < 0):
            return False, "❌ 담보 금액은 0 이상의 정수로 입력해주세요."
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
                capacity = RateManager.institution_capacity(
                    conn, product["issuer"], amount
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
                    f"SELECT credit_score,nationality FROM users.users WHERE user_id = ? AND (status = 'active' OR user_id='{TREASURY_USER_ID}')",
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
                limit = int(round(limit))
                if limit > 0 and amount > limit:
                    return False, f"❌ 대출 한도를 초과했습니다. (한도: {limit:,} {home})"
                opened_epoch = time.time()
                required_collateral = math.ceil(
                    amount*float(product['collateral_required_ratio'] or 0)
                )
                asset_plan, asset_market_value, asset = self._asset_collateral_plan(
                    conn, user_id, collateral_asset_code, collateral_asset_quantity, home
                )
                haircut = float(product['collateral_haircut'] or 0)
                asset_collateral_value = round_money(asset_market_value*(1-haircut))
                total_collateral_value = collateral_amount+asset_collateral_value
                if total_collateral_value < required_collateral:
                    return False, (
                        f"❌ 이 상품은 헤어컷 후 담보 {required_collateral:,} {home} 이상이 필요합니다."
                    )
                if collateral_amount:
                    funds = UserFunds.choose(conn, str(user_id))
                    if not funds or funds[0] < collateral_amount or not UserFunds.debit(
                        conn, str(user_id), funds[1], collateral_amount
                    ):
                        return False, "❌ 담보로 설정할 잔액이 부족합니다."
                credit_score = max(0, min(1000, int(user['credit_score'] or 0)))
                proposed_country = product['rate_nationality'] or user['nationality']
                contract_country = (proposed_country if proposed_country and conn.execute(
                    "SELECT 1 FROM country_currencies WHERE nationality=?", (proposed_country,)
                ).fetchone() else None)
                credit_adjustment = float(product['credit_spread_max'] or 0)*(1000-credit_score)/1000
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
                fee = round_money(amount*float(product['origination_fee_percent'] or 0)/100)
                net_payout = max(0, amount-fee)
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
                    opened_epoch, due_epochs,
                )
                apr = RateManager.annual_percentage_rate(
                    contract_rate, fee, net_payout, maturity_days,
                    cashflows=[(day, principal_due+interest_due)
                               for day, principal_due, interest_due in schedule] or None,
                    principal=amount,
                )
                legal_cost_cap = (RateManager.from_connection(
                    conn, 'legal_max_rate', contract_country, require_country=True
                ) if contract_country else RateManager.legal_max_for_currency(conn, home)[0])
                legal_cost_rate = (contract_rate + (fee/max(1, net_payout))
                                   * 365/max(1, int(product['maturity_days'] or 365))*100)
                if legal_cost_rate > legal_cost_cap+1e-9:
                    raise ValueError(
                        f"법정 산입비용 연율 {legal_cost_rate:.3f}%가 {contract_country or home} "
                        f"법정최고이자 {legal_cost_cap:g}%를 초과합니다."
                    )
                inserted = conn.execute(
                    "INSERT INTO accounts.loans "
                    "(user_id, product_code, principal, outstanding_balance, last_payment_epoch, "
                    "last_repayment_epoch, maturity_epoch, contract_rate,rate_type,rate_nationality,"
                    "base_rate_spread,rate_floor,rate_cap,repricing_cycle_seconds,next_repricing_epoch,"
                    "benchmark_key,benchmark_tenor_days,repricing_lag_seconds,contract_effective_rate,"
                    "contract_apr,origination_fee,collateral_amount,day_count_convention,"
                    "amortization_type,payment_frequency_days,collateral_asset_value,current_ltv) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(user_id), product_code, amount, amount, opened_epoch, opened_epoch,
                     opened_epoch+int(product['maturity_days'] or 0)*86400
                     if int(product['maturity_days'] or 0)>0 else None,
                     contract_rate, product['rate_type'], contract_country,
                     contract_spread, float(product['rate_floor']), product['rate_cap'],
                     int(product['repricing_cycle_seconds']),
                     opened_epoch+int(product['repricing_cycle_seconds'])
                     if product['rate_type']=='floating' else None,
                     product['benchmark_key'], int(product['benchmark_tenor_days'] or product['maturity_days'] or 0),
                     int(product['repricing_lag_seconds'] or 0), effective, apr, fee, collateral_amount,
                     product['day_count_convention'], product['amortization_type'],
                     int(product['payment_frequency_days'] or 30), asset_collateral_value,
                     (amount/total_collateral_value if total_collateral_value else None)),
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
                        "UPDATE accounts.accounts SET balance = balance + ?, "
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
                if fee:
                    RateManager.record_institution_result(
                        conn, product["issuer"], fee, "origination_fee",
                        f"loan:{loan_id}", now=opened_epoch,
                    )
                RateManager.refresh_all_market_pressure(conn, now=opened_epoch)
            return True, (
                f"✅ 대출이 승인되었습니다. 실수령액 {net_payout:,} {home}를 입금했습니다.\n"
                f"명목금리 연 {contract_rate:g}% · 실효금리 연 {effective:.3f}% · APR {apr:.3f}%"
                + (f" · 수수료 {fee:,}" if fee else "")
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
                    f"(status='active' OR user_id='{TREASURY_USER_ID}')", (str(user_id),)).fetchone()
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
                after = max(float(before), now)+days*86400
                conn.execute("UPDATE loans SET maturity_epoch=?,extension_count=extension_count+1,"
                    "last_extended_epoch=?,extension_eligible_epoch=?,collection_status='current',"
                    "overdue_since_epoch=NULL,overdue_last_epoch=NULL,overdue_interest_remainder=0 "
                    "WHERE loan_id=?",
                    (after,now,max(float(before),now+days*86400),loan['loan_id']))
            return True, f"✅ 대출 #{loan['loan_id']} 만기를 {days}일 연장했습니다.\n변경 만기: <t:{int(after)}:F>"
        except (TypeError, ValueError, OverflowError) as exc:
            return False, f'❌ 대출연장 설정을 확인해주세요: {exc}'

    def repay_loan(self, user_id, product_code, amount, loan_id=None):
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            return False, "❌ 상환액은 0보다 큰 정수로 입력해주세요."
        try:
            self._settle_user_interest(user_id)
            with self.db.atomic(("users", "accounts")) as conn:
                if loan_id is None:
                    loan = conn.execute(
                        "SELECT l.loan_id,l.outstanding_balance,l.status,l.provisioned_loss,"
                        "p.issuer FROM accounts.loans l JOIN product.liability p "
                        "ON p.product_code=l.product_code WHERE l.user_id=? "
                        "AND l.product_code=? AND l.status IN ('active','defaulted')",
                        (str(user_id), product_code),
                    ).fetchone()
                else:
                    loan = conn.execute(
                        "SELECT l.loan_id,l.outstanding_balance,l.status,l.provisioned_loss,"
                        "p.issuer FROM accounts.loans l JOIN product.liability p "
                        "ON p.product_code=l.product_code WHERE l.loan_id=? AND l.user_id=? "
                        "AND l.product_code=? AND l.status IN ('active','defaulted')",
                        (int(loan_id), str(user_id), product_code),
                    ).fetchone()
                if not loan:
                    return False, "❌ 상환할 대출을 찾을 수 없습니다."
                outstanding = int(round(loan["outstanding_balance"] or 0))
                payment = min(amount, outstanding)
                funds = UserFunds.choose(conn, str(user_id))
                if not funds or funds[0] < payment:
                    return False, "❌ 상환할 잔액이 부족합니다."
                if not UserFunds.debit(conn, str(user_id), funds[1], payment):
                    return False, "❌ 잔액이 부족합니다."
                remaining = max(0, outstanding - payment)
                released = 0
                if remaining <= 0:
                    released = self._release_collateral(conn, loan["loan_id"], str(user_id))
                conn.execute(
                    "UPDATE accounts.loans SET outstanding_balance = ?, "
                    "status = CASE WHEN ? <= 0 THEN 'paid' ELSE status END, "
                    "collection_status=CASE WHEN ? <= 0 THEN 'paid' ELSE collection_status END,"
                    "provisioned_loss=MAX(0,provisioned_loss-?) "
                    "WHERE loan_id = ?",
                    (remaining, remaining, remaining,
                     min(payment, int(loan["provisioned_loss"] or 0)), loan["loan_id"]),
                )
                recovery = min(payment, int(loan["provisioned_loss"] or 0))
                if recovery:
                    RateManager.record_institution_result(
                        conn, loan["issuer"], recovery, "default_recovery",
                        f"loan:{loan['loan_id']}:payment:{time.time_ns()}",
                    )
                self._allocate_schedule_payment(
                    conn, loan["loan_id"], payment, time.time()
                )
                RateManager.refresh_all_market_pressure(conn)
            suffix = f" · 담보 {released:,} NWX 반환" if released else ""
            return True, f"✅ {payment:,} NWX를 상환했습니다. 남은 원리금: {remaining:,} NWX{suffix}"
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
                    "p.repayment_cycle_seconds, p.repayment_amount, l.amortization_type "
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
                        installment = int(round(loan["repayment_amount"] or 0))
                        balance = int(round(loan["outstanding_balance"] or 0))
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
                        funds = UserFunds.choose(conn, str(loan["user_id"]))
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
                            "paid_amount": int(loan_paid_amount),
                            "missed_count": loan_missed_count,
                            "remaining": int(balance),
                        })
                RateManager.refresh_all_market_pressure(conn, now=now)
            return paid_count, missed_count, notifications
        except Exception:
            raise

    @staticmethod
    def _allocate_schedule_payment(conn, loan_id, amount, paid_epoch):
        remaining = max(0, int(amount or 0))
        if not remaining:
            return
        rows = conn.execute(
            "SELECT schedule_id,principal_due+interest_due+fee_due AS due,amount_paid "
            "FROM accounts.loan_payment_schedule WHERE loan_id=? "
            "AND status NOT IN ('paid','waived') ORDER BY installment_number",
            (loan_id,),
        ).fetchall()
        for row in rows:
            unpaid = max(0, int(row["due"] or 0)-int(row["amount_paid"] or 0))
            applied = min(remaining, unpaid)
            if not applied:
                continue
            new_paid = int(row["amount_paid"] or 0)+applied
            status = "paid" if new_paid >= int(row["due"] or 0) else "partial"
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
        balance = int(round(loan["outstanding_balance"] or 0))
        paid_count = missed_count = paid_amount = 0
        for row in rows:
            contractual = (int(row["principal_due"] or 0)
                           + int(row["interest_due"] or 0)
                           + int(row["fee_due"] or 0))
            due = min(balance, max(0, contractual-int(row["amount_paid"] or 0)))
            if due <= 0:
                conn.execute(
                    "UPDATE accounts.loan_payment_schedule SET status='paid',paid_epoch=? "
                    "WHERE schedule_id=?", (now, row["schedule_id"]),
                )
                continue
            funds = UserFunds.choose(conn, str(loan["user_id"]))
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
        remaining = max(0, int(row['collateral_amount'] or 0)
                        - int(row['collateral_released'] or 0)
                        - int(row['collateral_seized'] or 0))
        if remaining:
            UserFunds.credit(conn, user_id, remaining)
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
                average = round_money(
                    (old_quantity*int(existing["average_cost"] or 0)
                     + added*int(asset["average_cost"] or 0))/(old_quantity+added)
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
            "SELECT status,deposit_insurance_limit FROM financial_institutions "
            "WHERE issuer=?", (issuer,),
        ).fetchone()
        if not institution or institution["status"] != "failed":
            return {"accounts": 0, "covered": 0, "uninsured": 0}
        accounts = conn.execute(
            "SELECT a.account_number,a.user_id,a.balance,a.deposit_insured "
            "FROM accounts.accounts a JOIN product.saving p "
            "ON p.product_code=a.product_code WHERE p.issuer=? "
            "AND a.account_type='saving' "
            "AND a.resolution_status='active' ORDER BY a.user_id,a.account_number",
            (issuer,),
        ).fetchall()
        limit = max(0, int(institution["deposit_insurance_limit"] or 0))
        used_by_user = {}
        covered_total = uninsured_total = 0
        for account in accounts:
            balance = max(0, int(account["balance"] or 0))
            user_id = str(account["user_id"])
            used = used_by_user.get(user_id, 0)
            covered = (min(balance, max(0, limit-used))
                       if int(account["deposit_insured"] or 0) else 0)
            uninsured = balance-covered
            used_by_user[user_id] = used+covered
            if covered:
                paid = conn.execute(
                    "UPDATE users.users SET cash=cash+? WHERE user_id=?",
                    (covered, user_id),
                )
                if paid.rowcount != 1:
                    raise RuntimeError("예금보험금을 받을 사용자를 찾을 수 없습니다.")
                conn.execute(
                    "UPDATE users.users SET cash=cash-? WHERE user_id=?",
                    (covered, TREASURY_USER_ID),
                )
            conn.execute(
                "UPDATE accounts.accounts SET balance=0,principal=0,"
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
            covered_total += covered
            uninsured_total += uninsured
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
    def revalue_asset_collateral(conn, loan_id, outstanding_balance, product,
                                 *, liquidate=True, force=False):
        """Mark pledged assets to market and optionally liquidate on an LTV breach."""
        rows = conn.execute(
            "SELECT * FROM accounts.loan_collateral_assets "
            "WHERE loan_id=? AND status='pledged' ORDER BY collateral_id",
            (loan_id,),
        ).fetchall()
        if not rows:
            return int(outstanding_balance), None, 0
        total = 0
        marked = []
        for row in rows:
            quote = conn.execute(
                "SELECT price FROM assets.market_assets WHERE code=? "
                "UNION ALL SELECT share_price FROM company.company WHERE company_code=? "
                "UNION ALL SELECT unit_price FROM product.investment WHERE product_code=? LIMIT 1",
                (row["asset_code"], row["asset_code"], row["asset_code"]),
            ).fetchone()
            price = max(1, int(quote["price"] if quote else row["last_price"]))
            value = max(0, round_money(
                price*float(row["quantity"])*(1-float(row["haircut"]))
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
            return int(outstanding_balance), ltv, 0
        proceeds = 0
        for row, price, value in marked:
            existing = conn.execute(
                "SELECT holding_id,quantity,average_cost FROM assets.user_assets "
                "WHERE user_id=? AND asset_code=? AND account_number IS NULL",
                (TREASURY_USER_ID, row["asset_code"]),
            ).fetchone()
            if existing:
                old_quantity = float(existing["quantity"])
                quantity = float(row["quantity"])
                average = round_money(
                    (old_quantity*int(existing["average_cost"] or 0)+quantity*price)
                    /(old_quantity+quantity)
                )
                conn.execute(
                    "UPDATE assets.user_assets SET quantity=quantity+?,average_cost=? "
                    "WHERE holding_id=?", (quantity, average, existing["holding_id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO assets.user_assets "
                    "(user_id,asset_code,account_number,quantity,average_cost) "
                    "VALUES (?,?,NULL,?,?)",
                    (TREASURY_USER_ID, row["asset_code"], row["quantity"], price),
                )
            proceeds += value
            conn.execute(
                "UPDATE accounts.loan_collateral_assets SET status='liquidated' "
                "WHERE collateral_id=?", (row["collateral_id"],),
            )
        new_balance = max(0, int(outstanding_balance)-proceeds)
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
    def choose(conn, user_id):
        user = conn.execute("SELECT cash FROM users.users WHERE user_id = ?", (user_id,)).fetchone()
        if not user:
            return None
        sources = [(int(round(user["cash"] or 0)), None)]
        sources.extend(
            (int(round(row["balance"] or 0)), row["account_number"])
            for row in conn.execute(
                "SELECT account_number, balance FROM accounts.accounts WHERE user_id = ?", (user_id,)
            ).fetchall()
        )
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
                "UPDATE accounts.accounts SET balance = balance - ?, "
                "principal = MAX(0, COALESCE(principal, 0) - ?), last_payment_epoch = ?, "
                "last_payment = CURRENT_TIMESTAMP "
                "WHERE account_number = ? AND user_id = ? AND balance >= ?",
                (amount, amount, now, account_number, user_id, amount),
            )
        return cursor.rowcount == 1

    @staticmethod
    def credit(conn, user_id, amount, account_number=None):
        if amount <= 0:
            return True
        if account_number is None:
            cursor = conn.execute(
                "UPDATE users.users SET cash=cash+? WHERE user_id=?", (amount, user_id)
            )
        else:
            cursor = conn.execute(
                "UPDATE accounts.accounts SET balance=balance+?,principal=principal+? "
                "WHERE account_number=? AND user_id=?",
                (amount, amount, account_number, user_id),
            )
        return cursor.rowcount == 1
