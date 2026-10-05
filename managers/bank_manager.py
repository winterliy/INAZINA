import hashlib
import hmac
import math
import os
import random
import time
from database import DatabaseManager


class BankManager:
    """예금 계좌 및 대출 업무를 담당합니다."""

    PASSWORD_ITERATIONS = 240_000

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
                    "SELECT product_code, name, deposit_amount, status FROM product.saving "
                    "WHERE product_code = ?", (product_code,)
                ).fetchone()
                if not product:
                    return False, "❌ 존재하지 않는 저축 상품입니다."
                locations = DatabaseManager.code_locations(conn, product_code)
                if len(locations) != 1 or locations[0] != "저축 상품":
                    return False, f"❌ 상품 코드 `{product_code}`가 다른 상품·자산 코드와 중복 등록되어 있습니다. SQL Browser에서 정리해주세요."
                if product["status"] != "active":
                    return False, "❌ 현재 가입할 수 없는 상품입니다."
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
                    return False, f"❌ 최소 예치금은 {minimum:,} NWX입니다."
                cursor = conn.execute(
                    "UPDATE users.users SET cash = cash - ? "
                    "WHERE user_id = ? AND (status = 'active' OR user_id='__treasury__') AND cash >= ?",
                    (deposit_amount, str(user_id), deposit_amount),
                )
                if cursor.rowcount != 1:
                    return False, f"❌ 현금 잔액이 부족합니다. (필요 예치금: {deposit_amount:,} NWX)"
                account_number = self._generate_account_number(conn)
                conn.execute(
                    "INSERT INTO accounts.accounts "
                    "(account_number, user_id, product_code, balance, password, principal, last_payment_epoch) "
                    "VALUES (?, ?, ?, ?, ?, ?, unixepoch('now'))",
                    (account_number, str(user_id), product_code, deposit_amount,
                     self._hash_password(password), deposit_amount),
                )
            return True, (
                f"✅ {product['name']} 계좌가 개설되었습니다.\n"
                f"계좌번호: `{account_number}` · 예치금: {deposit_amount:,} NWX"
            )
        except Exception as exc:
            return False, f"❌ 계좌 개설 처리 중 오류가 발생했습니다: {exc}"

    def close_account(self, user_id, account_number, destination_account=None, password=None):
        """계좌 잔액과 해당 계좌에 귀속된 투자자산을 본인에게 이관합니다."""
        try:
            self._settle_user_interest(user_id)
            with self.db.atomic(("users", "accounts", "assets")) as conn:
                source = conn.execute(
                    "SELECT account_number, user_id, balance, password FROM accounts.accounts "
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
            target_text = f"계좌 `{destination_number}`" if destination_number else "현금 지갑"
            return True, f"✅ 계좌를 해지했습니다. 잔액과 보유 자산을 {target_text}(으)로 이관했습니다."
        except Exception as exc:
            return False, f"❌ 계좌 해지 처리 중 오류가 발생했습니다: {exc}"

    def apply_loan(self, user_id, product_code, amount):
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            return False, "❌ 대출 금액은 0보다 큰 정수로 입력해주세요."
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
                if conn.execute(
                    "SELECT 1 FROM accounts.loans WHERE user_id = ? AND product_code = ?",
                    (str(user_id), product_code),
                ).fetchone():
                    return False, f"❌ 이미 {product['name']} 상품을 이용한 기록이 있습니다."
                user = conn.execute(
                    "SELECT credit_score FROM users.users WHERE user_id = ? AND (status = 'active' OR user_id='__treasury__')",
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
                    return False, f"❌ 대출 한도를 초과했습니다. (한도: {limit:,} NWX)"
                conn.execute(
                    "INSERT INTO accounts.loans "
                    "(user_id, product_code, principal, outstanding_balance, last_payment_epoch, "
                    "last_repayment_epoch, maturity_epoch) VALUES (?, ?, ?, ?, unixepoch('now'), unixepoch('now'), ?)",
                    (str(user_id), product_code, amount, amount,
                     time.time()+int(product['maturity_days'] or 0)*86400 if int(product['maturity_days'] or 0)>0 else None),
                )
                payout = conn.execute(
                    "SELECT a.account_number FROM accounts.accounts a "
                    "JOIN product.saving s ON s.product_code = a.product_code "
                    "WHERE a.user_id = ? AND s.issuer IS ? AND a.account_type = 'saving' "
                    "ORDER BY a.created_at LIMIT 1",
                    (str(user_id), product["issuer"]),
                ).fetchone()
                if payout:
                    payout_number = payout["account_number"]
                    conn.execute(
                        "UPDATE accounts.accounts SET balance = balance + ?, "
                        "principal = COALESCE(principal, 0) + ?, last_payment_epoch = ?, "
                        "last_payment = CURRENT_TIMESTAMP "
                        "WHERE account_number = ? AND user_id = ?",
                        (amount, amount, time.time(), payout_number, str(user_id)),
                    )
                else:
                    payout_number = self._generate_account_number(conn)
                    conn.execute(
                        "INSERT INTO accounts.accounts "
                        "(account_number, user_id, product_code, balance, password, principal, "
                        "last_payment_epoch, account_type) "
                        "VALUES (?, ?, ?, ?, NULL, 0, unixepoch('now'), 'loan_payout')",
                        (payout_number, str(user_id), product_code, amount),
                    )
            return True, (
                f"✅ 대출이 승인되었습니다. {amount:,} NWX를 발급 은행 계좌에 입금했습니다.\n"
                f"입금 계좌번호: `{payout_number}`"
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
                    "(status='active' OR user_id='__treasury__')", (str(user_id),)).fetchone()
                if not user:
                    return False, '❌ 등록된 사용자만 대출을 연장할 수 있습니다.'
                # Product IDs are accepted as well as the individual loan number;
                # each user has at most one contract per product.
                rows = conn.execute("SELECT l.*,p.extension_days,p.extension_min_credit_score,p.interest_rate,p.extension_interest_rate,p.status product_status "
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
                product=conn.execute('SELECT * FROM liability WHERE product_code=?',(loan['product_code'],)).fetchone()
                InterestManager.settle_loan_rate_boundary(conn,loan,product,now)
                extension=float(product['extension_interest_rate'] or 0)
                applied=extension if extension>0 else float(product['interest_rate'])
                if not math.isfinite(applied) or not 0<=applied<=100:raise ValueError('연장 이자율은 0~100%로 설정해주세요.')
                after = max(float(before), now)+days*86400
                conn.execute("UPDATE loans SET maturity_epoch=?,extension_count=extension_count+1,"
                    "last_extended_epoch=?,extension_eligible_epoch=? WHERE loan_id=?",
                    (after,now,max(float(before),now+days*86400),loan['loan_id']))
            return True, f"✅ 대출 #{loan['loan_id']} 만기를 {days}일 연장했습니다.\n변경 만기: <t:{int(after)}:F> · 적용 연이율: {applied:g}%"
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
                        "SELECT loan_id, outstanding_balance FROM accounts.loans "
                        "WHERE user_id = ? AND product_code = ? AND status = 'active'",
                        (str(user_id), product_code),
                    ).fetchone()
                else:
                    loan = conn.execute(
                        "SELECT loan_id, outstanding_balance FROM accounts.loans "
                        "WHERE loan_id = ? AND user_id = ? AND product_code = ? AND status = 'active'",
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
                conn.execute(
                    "UPDATE accounts.loans SET outstanding_balance = ?, "
                    "status = CASE WHEN ? <= 0 THEN 'paid' ELSE 'active' END WHERE loan_id = ?",
                    (remaining, remaining, loan["loan_id"]),
                )
            return True, f"✅ {payment:,} NWX를 상환했습니다. 남은 원리금: {remaining:,} NWX"
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
                self._settle_user_interest(due_user_id)

            with self.db.atomic(("users", "accounts", "product")) as conn:
                loans = conn.execute(
                    "SELECT l.loan_id, l.user_id, l.product_code, l.outstanding_balance, "
                    "l.last_repayment_epoch, p.name AS product_name, "
                    "p.repayment_cycle_seconds, p.repayment_amount "
                    "FROM accounts.loans l JOIN product.liability p "
                    "ON p.product_code = l.product_code WHERE l.status = 'active'"
                ).fetchall()
                for loan in loans:
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
                            conn.execute(
                                "UPDATE accounts.loans SET outstanding_balance = 0, status = 'paid' "
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
            return paid_count, missed_count, notifications
        except Exception:
            raise

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
