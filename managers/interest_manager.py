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
    def _tax_rate(conn):
        row = conn.execute("SELECT value FROM settings WHERE key='tax_percent'").fetchone()
        try:
            rate = float(row["value"] if row else 3.0)
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
                tax_percent = self._tax_rate(conn)
                accounts = conn.execute(
                    "SELECT account_number, user_id, product_code, balance, principal, "
                    "created_at, last_payment, last_payment_epoch FROM accounts.accounts "
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
                    rate = self._number(product["interest_rate"])
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
                    periods = self._due_periods(end - last, cycle)
                    if periods <= 0:
                        continue
                    paid_until = last + periods * cycle
                    compound = int(compound_setting) == 1
                    if compound:
                        gross = self._period_interest(balance, rate, cycle, periods)
                    else:
                        gross = calculate_simple_interest(principal, rate, periods * cycle)
                    if gross is None or not math.isfinite(gross) or gross <= 0:
                        self._advance_account_cursor(conn, account["account_number"], now)
                        continue
                    gross = round_money(gross)
                    if gross <= 0:
                        # Keep the interest cursor so sub-NWX interest accumulates until
                        # it rounds to a payable whole NWX; never store fractional cash.
                        continue
                    tax = calculate_income_tax(gross, tax_percent / 100.0)
                    net = max(0, gross - tax)
                    stamp = datetime.fromtimestamp(paid_until, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        "UPDATE accounts.accounts SET balance = balance + ?, last_payment = ?, "
                        "last_payment_epoch = ? WHERE account_number = ?",
                        (net, stamp, paid_until, account["account_number"]),
                    )
                    if tax > 0:
                        treasury = conn.execute(
                            "UPDATE users.users SET cash = cash + ? WHERE user_id = ?",
                            (tax, TREASURY_USER_ID),
                        )
                        if treasury.rowcount != 1:
                            raise RuntimeError("국고 계정을 찾을 수 없어 이자 지급을 취소했습니다.")
                    total_paid += net
                    account_count += 1

                loans = conn.execute(
                    "SELECT loan_id, user_id, product_code, outstanding_balance, created_at, last_payment_epoch "
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
                    rate = self._number(product["interest_rate"])
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
                    periods = self._due_periods(end - last, cycle)
                    if periods <= 0:
                        continue
                    interest = self._period_interest(balance, rate, cycle, periods)
                    if interest is None or not math.isfinite(interest) or interest <= 0:
                        conn.execute(
                            "UPDATE accounts.loans SET last_payment_epoch = ? "
                            "WHERE loan_id = ?",
                            (now, loan["loan_id"]),
                        )
                        continue
                    whole_interest = round_money(interest)
                    if whole_interest <= 0:
                        # Retain the old cursor; future periods are recomputed as one
                        # integer payable rather than silently discarding fractions.
                        continue
                    paid_until = last + periods * cycle
                    conn.execute(
                        "UPDATE accounts.loans SET outstanding_balance = outstanding_balance + ?, "
                        "last_payment_epoch = ? WHERE loan_id = ? AND status = 'active'",
                        (whole_interest, paid_until, loan["loan_id"]),
                    )
                    loan_count += 1
                # After contractual maturity, accrue game statutory simple interest.
                # Use a separate cursor so rounding and repeated reads cannot double-charge.
                overdue = conn.execute(
                    "SELECT l.loan_id, l.principal, l.outstanding_balance, l.created_at, u.nationality, "
                    "l.last_statutory_epoch, l.statutory_interest_remainder, l.maturity_epoch, p.maturity_days FROM accounts.loans l "
                    "JOIN product.liability p ON p.product_code = l.product_code "
                    "JOIN users.users u ON u.user_id = l.user_id "
                    "WHERE l.status = 'active' AND p.status = 'active' "
                    "AND (l.maturity_epoch IS NOT NULL OR p.maturity_days > 0) AND (? IS NULL OR l.user_id = ?)",
                    (user_id, user_id),
                ).fetchall()
                for loan in overdue:
                    created = self._utc_epoch(loan["created_at"])
                    if created is None:
                        continue
                    maturity = (float(loan['maturity_epoch']) if loan['maturity_epoch'] is not None
                                else created + float(loan["maturity_days"]) * 86400)
                    start = max(maturity, float(loan["last_statutory_epoch"] or maturity))
                    if now <= start:
                        continue
                    principal = min(int(loan["principal"]), int(loan["outstanding_balance"]))
                    accrued = RateManager.interest_between(
                        conn, "statutory_rate", principal, start, now,
                        nationality=loan["nationality"],
                    )
                    accrued += float(loan["statutory_interest_remainder"] or 0)
                    interest = max(0, round_money(accrued))
                    conn.execute("UPDATE accounts.loans SET outstanding_balance = outstanding_balance + ?, "
                        "last_statutory_epoch = ?, statutory_interest_remainder = ? WHERE loan_id = ? AND status = 'active'",
                        (interest, now, accrued - interest, loan["loan_id"]))
                    if interest > 0:
                        loan_count += 1
            return account_count, int(total_paid), loan_count
        except Exception:
            raise

    def process_user_interests(self, user_id, now=None):
        """Settle interest for one user's accounts and loans immediately before display/use."""
        return self.process_all_interests(now=now, user_id=user_id)
