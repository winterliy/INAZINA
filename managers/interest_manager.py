from datetime import datetime, timezone
import math
import time
from database import DatabaseManager
from database.user_db import TREASURY_USER_ID
from functions.interest_calc import calculate_compound_interest, calculate_simple_interest
from functions.tax_calc import calculate_income_tax
from functions.money import round_money
from managers.rate_manager import RateManager


class InterestManager:
    """저축 이자 원천징수 및 대출 이자 누적을 처리합니다."""

    YEAR_SECONDS = 365 * 24 * 60 * 60

    def __init__(self, db: DatabaseManager):
        self.db = db

    @staticmethod
    def _number(value, default=None):
        try:
            number = float(value)
            return number if math.isfinite(number) else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _utc_epoch(value):
        if value is None:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _period_interest(principal, annual_rate_percent, cycle_seconds, periods):
        if principal <= 0 or annual_rate_percent <= 0 or periods <= 0:
            return 0.0
        try:
            return calculate_compound_interest(principal, annual_rate_percent, periods, cycle_seconds)
        except (OverflowError, ValueError):
            return None

    @staticmethod
    def _due_periods(elapsed_seconds, cycle_seconds):
        if elapsed_seconds <= 0 or cycle_seconds <= 0:
            return 0
        # Tolerate binary floating-point edges for fractional-second cycles (e.g. 0.1s).
        return max(0, int(math.floor(elapsed_seconds / cycle_seconds + 1e-9)))

    @staticmethod
    def _tax_rate(conn, user_id=None):
        row = (conn.execute("SELECT c.tax_percent FROM users u LEFT JOIN country_currencies c "
                            "ON c.nationality=u.nationality WHERE u.user_id=?", (str(user_id),)).fetchone()
               if user_id is not None else None)
        try:
            rate = float(row["tax_percent"] if row and row["tax_percent"] is not None else 3.0)
        except (TypeError, ValueError):
            rate = 3.0
        return rate if math.isfinite(rate) and 0 <= rate <= 100 else 3.0

    @staticmethod
    def _advance_account_cursor(conn, account_number, timestamp):
        stamp = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE accounts.accounts SET last_payment = ?, last_payment_epoch = ? "
            "WHERE account_number = ?",
            (stamp, timestamp, account_number),
        )

    @classmethod
    def _rate_segments(cls, conn, contract, end, cycle, fallback_rate):
        """Split due interest cycles at scheduled floating-rate reset boundaries."""
        last = cls._number(contract["last_payment_epoch"])
        if last is None or end <= last:
            return [], cls._number(contract["contract_rate"], fallback_rate), contract["next_repricing_epoch"]
        rate = cls._number(contract["contract_rate"], fallback_rate)
        rate_type = contract["rate_type"] or "fixed"
        next_reset = cls._number(contract["next_repricing_epoch"])
        reset_cycle = int(cls._number(contract["repricing_cycle_seconds"], 2592000))
        if rate_type == "floating" and (not contract["rate_nationality"] or reset_cycle <= 0):
            raise ValueError("변동금리 계약의 국적 또는 재산정주기가 올바르지 않습니다.")
        segments = []
        while last < end:
            if rate_type == "floating" and next_reset is not None and next_reset <= last:
                rate = RateManager.floating_rate(
                    conn, contract["rate_nationality"], contract["base_rate_spread"],
                    contract["rate_floor"], contract["rate_cap"], epoch=next_reset,
                )
                next_reset += reset_cycle
                continue
            boundary = min(end, next_reset) if rate_type == "floating" and next_reset is not None else end
            periods = cls._due_periods(boundary-last, cycle)
            if periods <= 0:
                break
            paid_until = last+periods*cycle
            segments.append((periods, rate, paid_until))
            last = paid_until
            if (rate_type == "floating" and next_reset is not None
                    and last >= next_reset):
                rate = RateManager.floating_rate(
                    conn, contract["rate_nationality"], contract["base_rate_spread"],
                    contract["rate_floor"], contract["rate_cap"], epoch=next_reset,
                )
                next_reset += reset_cycle
            if paid_until < boundary:
                break
        return segments, rate, next_reset

    def process_all_interests(self, now=None, user_id=None):
        """Accrue due savings/loan interest, optionally for one user only.

        The account/loan last-payment epoch is the persisted cursor. A call compares
        it with the current time and applies every complete product interest cycle.
        It is safe to call on a user-facing balance/loan lookup; already-settled
        cycles are not paid twice.
        """
        now = time.time() if now is None else float(now)
        user_id = None if user_id is None else str(user_id)
        total_paid = 0
        account_count = 0
        loan_count = 0
        try:
            with self.db.atomic(("users", "accounts", "product", "tax", "system")) as conn:
                accounts = conn.execute(
                    "SELECT account_number, user_id, product_code, balance, principal, "
                    "created_at, last_payment, last_payment_epoch,contract_rate,rate_type,"
                    "rate_nationality,base_rate_spread,rate_floor,rate_cap,repricing_cycle_seconds,"
                    "next_repricing_epoch FROM accounts.accounts "
                    "WHERE (? IS NULL OR user_id = ?)",
                    (user_id, user_id),
                ).fetchall()
                for account in accounts:
                    product = conn.execute(
                        "SELECT interest_rate, maturity_days, interest_cycle, compound_type, status "
                        "FROM product.saving WHERE product_code = ?",
                        (account["product_code"],),
                    ).fetchone()
                    if not product or product["status"] != "active":
                        self._advance_account_cursor(conn, account["account_number"], now)
                        continue
                    rate = self._number(account["contract_rate"], self._number(product["interest_rate"]))
                    cycle = self._number(product["interest_cycle"])
                    balance = self._number(account["balance"], 0.0)
                    principal = self._number(account["principal"], balance)
                    last = self._number(account["last_payment_epoch"])
                    if last is None:
                        last = self._utc_epoch(account["last_payment"])
                    created = self._utc_epoch(account["created_at"])
                    if rate is None or cycle is None or cycle <= 0 or balance is None or last is None:
                        self._advance_account_cursor(conn, account["account_number"], now)
                        continue
                    if rate < 0:
                        self._advance_account_cursor(conn, account["account_number"], now)
                        continue
                    compound_setting = self._number(product["compound_type"], 0)
                    if compound_setting not in (0, 1):
                        self._advance_account_cursor(conn, account["account_number"], now)
                        continue
                    maturity_days = self._number(product["maturity_days"], 0)
                    end = now
                    if maturity_days and maturity_days > 0 and created is not None:
                        end = min(now, created + maturity_days * 86400)
                    if end <= last:
                        continue
                    segments, final_rate, next_reset = self._rate_segments(
                        conn, account, end, cycle, rate
                    )
                    if not segments:
                        continue
                    compound = int(compound_setting) == 1
                    credited = False
                    for periods, segment_rate, paid_until in segments:
                        gross = (self._period_interest(balance, segment_rate, cycle, periods)
                                 if compound else
                                 calculate_simple_interest(principal, segment_rate, periods*cycle))
                        if gross is None or not math.isfinite(gross):
                            self._advance_account_cursor(conn, account["account_number"], now)
                            break
                        gross = round_money(gross)
                        if gross <= 0:
                            if account["rate_type"] == "floating":
                                self._advance_account_cursor(
                                    conn, account["account_number"], paid_until
                                )
                            continue
                        tax = calculate_income_tax(
                            gross, self._tax_rate(conn, account['user_id']) / 100.0
                        )
                        net = max(0, gross-tax)
                        stamp = datetime.fromtimestamp(
                            paid_until, timezone.utc
                        ).strftime("%Y-%m-%d %H:%M:%S")
                        conn.execute(
                            "UPDATE accounts.accounts SET balance=balance+?,last_payment=?,"
                            "last_payment_epoch=? WHERE account_number=?",
                            (net, stamp, paid_until, account["account_number"]),
                        )
                        balance += net
                        if tax > 0:
                            treasury = conn.execute(
                                "UPDATE users.users SET cash=cash+? WHERE user_id=?",
                                (tax, TREASURY_USER_ID),
                            )
                            if treasury.rowcount != 1:
                                raise RuntimeError("국고 계정을 찾을 수 없어 이자 지급을 취소했습니다.")
                        total_paid += net
                        credited = True
                    if account["rate_type"] == "floating":
                        conn.execute(
                            "UPDATE accounts.accounts SET contract_rate=?,next_repricing_epoch=? "
                            "WHERE account_number=?",
                            (final_rate, next_reset, account["account_number"]),
                        )
                    account_count += int(credited)

                loans = conn.execute(
                    "SELECT loan_id, user_id, product_code, outstanding_balance, created_at,last_payment_epoch,"
                    "contract_rate,rate_type,rate_nationality,base_rate_spread,rate_floor,rate_cap,"
                    "repricing_cycle_seconds,next_repricing_epoch "
                    "FROM accounts.loans WHERE status = 'active' AND (? IS NULL OR user_id = ?)",
                    (user_id, user_id),
                ).fetchall()
                for loan in loans:
                    product = conn.execute(
                        "SELECT interest_rate, maturity_days, interest_cycle, status "
                        "FROM product.liability WHERE product_code = ?",
                        (loan["product_code"],),
                    ).fetchone()
                    if not product or product["status"] != "active":
                        conn.execute(
                            "UPDATE accounts.loans SET last_payment_epoch = ? "
                            "WHERE loan_id = ?",
                            (now, loan["loan_id"]),
                        )
                        continue
                    rate = self._number(loan["contract_rate"], self._number(product["interest_rate"]))
                    cycle = self._number(product["interest_cycle"])
                    balance = self._number(loan["outstanding_balance"], 0.0)
                    last = self._number(loan["last_payment_epoch"])
                    created = self._utc_epoch(loan["created_at"])
                    if rate is None or cycle is None or cycle <= 0 or balance is None or last is None or rate < 0:
                        conn.execute(
                            "UPDATE accounts.loans SET last_payment_epoch = ? "
                            "WHERE loan_id = ?",
                            (now, loan["loan_id"]),
                        )
                        continue
                    maturity_days = self._number(product["maturity_days"], 0)
                    end = now
                    if maturity_days and maturity_days > 0 and created is not None:
                        end = min(now, created + maturity_days * 86400)
                    if end <= last:
                        continue
                    segments, final_rate, next_reset = self._rate_segments(
                        conn, loan, end, cycle, rate
                    )
                    if not segments:
                        continue
                    charged = False
                    for periods, segment_rate, paid_until in segments:
                        interest = self._period_interest(balance, segment_rate, cycle, periods)
                        if interest is None or not math.isfinite(interest):
                            conn.execute(
                                "UPDATE accounts.loans SET last_payment_epoch=? WHERE loan_id=?",
                                (now, loan["loan_id"]),
                            )
                            break
                        whole_interest = round_money(interest)
                        if whole_interest <= 0:
                            if loan["rate_type"] == "floating":
                                conn.execute(
                                    "UPDATE accounts.loans SET last_payment_epoch=? WHERE loan_id=?",
                                    (paid_until, loan["loan_id"]),
                                )
                            continue
                        conn.execute(
                            "UPDATE accounts.loans SET outstanding_balance=outstanding_balance+?,"
                            "last_payment_epoch=? WHERE loan_id=? AND status='active'",
                            (whole_interest, paid_until, loan["loan_id"]),
                        )
                        balance += whole_interest
                        charged = True
                    if loan["rate_type"] == "floating":
                        conn.execute(
                            "UPDATE accounts.loans SET contract_rate=?,next_repricing_epoch=? "
                            "WHERE loan_id=?",
                            (final_rate, next_reset, loan["loan_id"]),
                        )
                    loan_count += int(charged)
            return account_count, int(total_paid), loan_count
        except Exception:
            raise

    def process_user_interests(self, user_id, now=None):
        """Settle interest for one user's accounts and loans immediately before display/use."""
        return self.process_all_interests(now=now, user_id=user_id)
