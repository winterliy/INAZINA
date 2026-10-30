from functions.money import base_currency_code
import hashlib
import hmac
import json
import math
import os
import random
import time
from decimal import Decimal
from datetime import datetime, timezone
from database import DatabaseManager
from database.user_db import TREASURY_USER_ID, ACTIVE_OR_TREASURY, IS_TREASURY
from functions.money import (round_money, round_currency, parse_currency_money,
                             currency_precision, to_minor_units)
from functions.interest_calc import year_fraction, year_fraction_decimal
from managers.bank_journal import BankJournal
from managers.credit_score_manager import CreditScoreManager
from managers.rate_manager import RateManager


class BankManager:
    """예금 계좌 및 대출 업무를 담당합니다."""

    PASSWORD_ITERATIONS = 240_000
    MAX_SUPPORTED_EPOCH = BankJournal.MAX_SUPPORTED_EPOCH
    OVERDUE_SWEEP_INTERVAL_SECONDS = 600

    @staticmethod
    def _loan_cashflows(principal, annual_rate, maturity_days, frequency_days,
                        amortization_type, day_count_convention="ACT/365",
                        start_epoch=None, due_epochs=None, decimal_places=0):
        """Build contractual installment cash flows for APR and servicing."""
        from functions.money import from_minor_units, to_minor_units

        principal_units = max(0, to_minor_units(principal, decimal_places))
        principal = from_minor_units(principal_units, decimal_places)

        def amount_value(units):
            exact = from_minor_units(units, decimal_places)
            return int(exact) if int(decimal_places) == 0 else exact

        def amount_units(value):
            return max(0, to_minor_units(value, decimal_places))

        days = max(1, int(maturity_days or 1))
        frequency = max(1, int(frequency_days or days))
        mode = str(amortization_type or "legacy")
        if mode == "legacy":
            return []
        start = RateManager._validated_epoch(
            start_epoch, "invalid_loan_cashflow_start_epoch"
        )
        if due_epochs is None:
            due_days = list(range(frequency, days, frequency)) + [days]
            due_times = [start+day*86400 for day in due_days]
        else:
            due_times = sorted(
                value for value in (
                    RateManager._validated_epoch(item, "invalid_loan_cashflow_due_epoch")
                    for item in due_epochs
                ) if value > start
            )
            due_days = [(value-start)/86400 for value in due_times]
        if not due_times:
            return []
        count = len(due_days)
        rate = Decimal(str(annual_rate or 0)) / Decimal(100)
        if not rate.is_finite():
            raise ValueError("invalid_loan_cashflow_rate")
        fractions = []
        previous = start
        for due in due_times:
            fractions.append(year_fraction_decimal(
                max(0.0, due-previous), day_count_convention, previous
            ))
            previous = due
        rows = []
        if mode == "bullet":
            interest_units = amount_units(principal*rate*sum(fractions, Decimal(0)))
            return [(due_days[-1], amount_value(principal_units),
                     amount_value(interest_units))]
        if mode == "equal_principal":
            remaining_units = principal_units
            allocated_units = 0
            for index, (due_day, fraction) in enumerate(zip(due_days, fractions), 1):
                principal_due_units = (principal_units-allocated_units if index == count
                                        else principal_units//count)
                principal_due_units = max(0, min(remaining_units, principal_due_units))
                interest_units = amount_units(
                    from_minor_units(remaining_units, decimal_places)*rate*fraction
                )
                rows.append((due_day, amount_value(principal_due_units),
                             amount_value(interest_units)))
                remaining_units -= principal_due_units
                allocated_units += principal_due_units
            return rows
        # Annuity: discount each potentially irregular period separately so
        # holidays and the selected day-count convention affect the payment.
        discount = Decimal(1)
        discounts = []
        period_rates = []
        for fraction in fractions:
            periodic_rate = max(Decimal("-0.999999"), rate*fraction)
            period_rates.append(periodic_rate)
            discount /= Decimal(1)+periodic_rate
            discounts.append(discount)
        payment = principal/max(Decimal("1e-12"), sum(discounts, Decimal(0)))
        remaining_units = principal_units
        allocated_units = 0
        for index, (due_day, periodic_rate) in enumerate(
            zip(due_days, period_rates), 1
        ):
            remaining = from_minor_units(remaining_units, decimal_places)
            interest_units = amount_units(remaining*periodic_rate)
            payment_units = amount_units(payment)
            principal_due_units = (principal_units-allocated_units if index == count else
                                   max(0, payment_units-interest_units))
            principal_due_units = min(remaining_units, principal_due_units)
            rows.append((due_day, amount_value(principal_due_units),
                         amount_value(interest_units)))
            remaining_units -= principal_due_units
            allocated_units += principal_due_units
        return rows

    @classmethod
    def _recast_payment_schedule(cls, conn, loan, annual_rate, as_of,
                                 outstanding_balance=None):
        """Recalculate only unpaid future installments after a floating reset."""
        from functions.money import from_minor_units, to_minor_units

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
            principal_paid_units = (int(row["principal_paid_minor_units"] or 0)
                                    if "principal_paid_minor_units" in row.keys()
                                    else to_minor_units(row["principal_paid"] or 0, places))
            interest_paid_units = (int(row["interest_paid_minor_units"] or 0)
                                   if "interest_paid_minor_units" in row.keys()
                                   else to_minor_units(row["interest_paid"] or 0, places))
            principal_due_units = (to_minor_units(principal_due, places)
                                   + principal_paid_units)
            interest_due_units = (to_minor_units(interest_due, places)
                                  + interest_paid_units)
            principal_due_amount = float(from_minor_units(principal_due_units, places))
            interest_due_amount = float(from_minor_units(interest_due_units, places))
            conn.execute(
                "UPDATE accounts.loan_payment_schedule SET principal_due=?,interest_due=?,"
                "rate_applied=?,day_count_convention=?,recast_epoch=? WHERE schedule_id=?",
                (principal_due_amount, interest_due_amount, float(annual_rate),
                 loan["day_count_convention"], float(as_of), row["schedule_id"]),
            )
        return len(cashflows)

    def __init__(self, db: DatabaseManager):
        self.db = db

    @staticmethod
    def _release_ecl_allowance(conn, loan_id, issuer, currency, *, now=None, reason="settled"):
        """Reverse a loan's remaining allowance when its exposure is fully closed."""
        from functions.money import (currency_precision, from_minor_units,
                                     round_currency, to_minor_units)

        row = conn.execute(
            "SELECT ecl_allowance,ecl_allowance_minor_units FROM accounts.loans WHERE loan_id=?",
            (int(loan_id),),
        ).fetchone()
        if not row:
            return 0
        places = currency_precision(conn, currency)
        allowance_units = (max(0, int(row["ecl_allowance_minor_units"] or 0))
                           if "ecl_allowance_minor_units" in row.keys()
                           else max(0, to_minor_units(row["ecl_allowance"] or 0, places)))
        booked_units = conn.execute(
            "SELECT COALESCE(SUM(l.credit_units-l.debit_units),0) "
            "FROM accounts.bank_journal_entries e "
            "JOIN accounts.bank_journal_lines l USING(entry_id) "
            "WHERE e.issuer=? AND e.currency_code=? AND e.status='posted' "
            "AND l.account_code='ecl_allowance'",
            (str(issuer), str(currency).upper()),
        ).fetchone()[0]
        allowance_units = min(allowance_units, max(0, int(booked_units or 0)))
        allowance = from_minor_units(allowance_units, places)
        if allowance_units <= 0:
            conn.execute(
                "UPDATE accounts.loans SET ecl_allowance_minor_units=0 WHERE loan_id=?",
                (int(loan_id),),
            )
            return 0
        reference = f"loan:{int(loan_id)}:ecl-release:{reason}"
        RateManager.record_institution_result(
            conn, issuer, allowance, "expected_credit_loss_release", reference,
            now=now, currency_code=currency,
        )
        from managers.bank_journal import BankJournal
        BankJournal.post(
            conn, issuer, "expected_credit_loss_release", reference, currency,
            [("ecl_allowance", "debit", allowance),
             ("impairment_reversal", "credit", allowance)],
            description=f"Release ECL allowance for settled loan {loan_id}",
            now=now,
        )
        conn.execute(
            "UPDATE accounts.loans SET ecl_allowance_minor_units=0 WHERE loan_id=?",
            (int(loan_id),),
        )
        return allowance

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
        from managers.compliance_manager import ComplianceManager
        try:
            ComplianceManager.check_financial_eligibility(
                self.db, user_id, "deposit_open"
            )
        except ValueError as exc:
            return False, f"❌ {exc}"
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
                ComplianceManager.require_financial_eligibility(conn, user_id)
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
                    f"WHERE user_id = ? AND {ACTIVE_OR_TREASURY} AND (cash >= ? OR {IS_TREASURY})",
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
                if deposit_amount > 0:
                    from managers.bank_journal import BankJournal
                    BankJournal.post(
                        conn, product["issuer"], "deposit_open",
                        f"account:{account_number}", home,
                        [("settlement_asset", "debit", deposit_amount),
                         ("customer_deposits_payable", "credit", deposit_amount)],
                        description=f"Deposit account {account_number} opened",
                        now=opened_epoch,
                    )
                if product['product_kind'] == 'wrap':
                    # The named manager trades this account; fees accrue from opening.
                    conn.execute("UPDATE accounts.accounts SET managed_by=?,fee_paid_epoch=? WHERE account_number=?",
                                 (product['manager_id'], opened_epoch, account_number))
                elif product['product_kind'] == 'ima':
                    from managers.investment_manager import InvestmentManager
                    quote = InvestmentManager.component_quote(conn, product['reference_code'])
                    if not quote or float(quote['price'] or 0) <= 0:
                        raise ValueError('IMA 연동 종목의 가격을 확인할 수 없습니다.')
                    conn.execute("UPDATE accounts.accounts SET reference_price=? WHERE account_number=?",
                                 (float(quote['price']), account_number))
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
                    "early_withdrawal_rate,early_withdrawal_fee_percent,product_code,account_type,"
                    "reference_price,reference_price_minor_units "
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
                wrap_sell = conn.execute(
                    "SELECT order_id FROM assets.market_orders WHERE funding_account = ? AND side = 'sell' "
                    "AND status IN ('scheduled','open','partial') LIMIT 1", (str(account_number),),
                ).fetchone()
                if wrap_sell:
                    return False, f"❌ 이 계좌 보유자산의 매도 주문 #{wrap_sell['order_id']}이 남아 있습니다. 먼저 취소해주세요."
                if destination_account:
                    destination = conn.execute(
                        "SELECT a.account_number,s.issuer FROM accounts.accounts a "
                        "LEFT JOIN product.saving s ON s.product_code=a.product_code "
                        "WHERE a.account_number = ? AND a.user_id = ? AND a.account_number <> ? "
                        "AND a.quote_currency=(SELECT quote_currency FROM accounts.accounts "
                        "WHERE account_number=?)",
                        (str(destination_account), str(user_id), str(account_number),
                         str(account_number)),
                    ).fetchone()
                    if not destination:
                        return False, "❌ 이관 대상은 본인 명의의 다른 계좌여야 합니다."
                    destination_number = destination["account_number"]
                    destination_issuer = destination["issuer"]
                else:
                    destination = conn.execute(
                        "SELECT a.account_number,s.issuer FROM accounts.accounts a "
                        "JOIN product.saving s ON s.product_code=a.product_code "
                        "WHERE a.user_id = ? AND a.account_number <> ? AND a.account_type = 'saving' "
                        "AND a.quote_currency=(SELECT quote_currency FROM accounts.accounts "
                        "WHERE account_number=?) ORDER BY a.balance DESC LIMIT 1",
                        (str(user_id), str(account_number), str(account_number)),
                    ).fetchone()
                    destination_number = destination["account_number"] if destination else None
                    destination_issuer = destination["issuer"] if destination else None

                source_currency = conn.execute(
                    "SELECT quote_currency FROM accounts.accounts WHERE account_number=?",
                    (str(account_number),),
                ).fetchone()[0]
                from managers.currency_manager import CurrencyManager as _Currency, SCALE as _SCALE
                balance = round_currency(conn, (source["balance_units"] or 0) / _SCALE, source_currency)
                account_liability = balance
                early_penalty = 0
                product = conn.execute(
                    "SELECT maturity_days,day_count_convention,product_kind,issuer FROM product.saving "
                    "WHERE product_code=?", (source["product_code"],)
                ).fetchone()
                matured = True
                if product and int(product["maturity_days"] or 0) > 0:
                    opened_at = datetime.strptime(
                        source["created_at"], "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc).timestamp()
                    matured = time.time() >= opened_at + int(product["maturity_days"]) * 86400
                ima_top_up = 0
                if product and product["product_kind"] == "ima" and matured:
                    # Principal protected plus the reference gain share; the bank pays the excess.
                    from managers.capital_markets_manager import CapitalMarketsManager
                    balance, ima_top_up = CapitalMarketsManager.ima_settlement(conn, source, balance)
                    if ima_top_up:
                        from managers.currency_manager import CurrencyManager as _CM
                        bank = _CM.bank_account_id(conn, product["issuer"])
                        bank_home = _CM.home(conn, bank)
                        cost = int(round(ima_top_up * _SCALE))
                        if source_currency != bank_home:
                            cost = _CM.convert(conn, cost, source_currency, bank_home)
                        _CM.change(conn, bank, bank_home, -cost, allow_deficit=True)
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
                if product and (account_liability or ima_top_up or early_penalty):
                    from managers.bank_journal import BankJournal
                    closing_lines = [
                        ("customer_deposits_payable", "debit", account_liability),
                        ("ima_payout_expense", "debit", ima_top_up),
                        ("customer_deposits_payable" if destination_number
                         and destination_issuer == product["issuer"]
                         else "settlement_asset", "credit", balance),
                        ("treasury_payable", "credit", early_penalty),
                    ]
                    BankJournal.post(
                        conn, product["issuer"], "deposit_close",
                        f"account:{account_number}", source_currency, closing_lines,
                        description=f"Deposit account {account_number} closed",
                        now=time.time(),
                    )
                    if (destination_number and destination_issuer
                            and destination_issuer != product["issuer"] and balance > 0):
                        # A transfer to another institution closes one liability
                        # and opens a new one; keep both banks' books in sync.
                        BankJournal.post(
                            conn, destination_issuer, "deposit_transfer_in",
                            f"account:{destination_number}:from:{account_number}",
                            source_currency,
                            [("settlement_asset", "debit", balance),
                             ("customer_deposits_payable", "credit", balance)],
                            description=(f"Deposit transfer from {account_number} "
                                         f"to {destination_number}"),
                            now=time.time(),
                        )
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
                    "SELECT asset_code, quantity, average_cost,average_cost_minor_units FROM assets.user_assets "
                    "WHERE user_id = ? AND account_number = ?",
                    (str(user_id), str(account_number)),
                ).fetchall()
                for position in positions:
                    from functions.money import currency_precision, from_minor_units
                    from managers.asset_cost_manager import AssetCostManager
                    currency = AssetCostManager.currency(conn, position["asset_code"])
                    cost = from_minor_units(
                        position["average_cost_minor_units"] or 0,
                        currency_precision(conn, currency),
                    )
                    AssetCostManager.upsert(
                        conn, user_id, position["asset_code"], destination_number,
                        position["quantity"], cost, schema="assets",
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
            ) + (f" IMA 성과 분배 {ima_top_up:,} {source_currency}를 더했습니다." if ima_top_up else "")
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
        if str(asset["quote_currency"] or base_currency_code(conn)) != str(currency_code):
            raise ValueError("대출과 같은 결제통화의 자산만 담보로 사용할 수 있습니다.")
        lots = conn.execute(
            "SELECT holding_id,account_number,quantity,average_cost,"
            "average_cost_minor_units FROM assets.user_assets "
            "WHERE user_id=? AND asset_code=? ORDER BY holding_id",
            (str(user_id), code),
        ).fetchall()
        available = sum(float(row["quantity"] or 0) for row in lots)
        custody_reserved = conn.execute(
            "SELECT COALESCE(SUM(r.reserved_quantity),0) FROM assets.dr_custody_reservations r "
            "JOIN assets.dr_custody_instructions i USING(reference_key) "
            "WHERE r.user_id=? AND r.reserved_asset_code=? AND r.released_epoch IS NULL "
            "AND i.status IN ('requested','verified')", (str(user_id), code),
        ).fetchone()[0]
        available -= float(custody_reserved or 0)
        if available+1e-9 < quantity:
            raise ValueError(f"담보 자산 `{code}` 보유 수량이 부족합니다.")
        plan, left = [], quantity
        from functions.money import currency_precision, from_minor_units
        places = currency_precision(conn, currency_code)
        for row in lots:
            take = min(left, float(row["quantity"] or 0))
            if take > 0:
                plan.append({
                    "holding_id": int(row["holding_id"]),
                    "source_account": row["account_number"],
                    "quantity": take,
                    "old_quantity": float(row["quantity"]),
                    "average_cost": float(from_minor_units(
                        row["average_cost_minor_units"] or 0, places
                    )),
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
                              annual_income, stress_rate_shock_bps=200,
                              stress_fx_shock_percent=10):
        """Return income affordability and repayment-history measures at origination."""
        from managers.currency_manager import CurrencyManager, SCALE
        home_currency = CurrencyManager.home(conn, user_id)

        def to_home(value, currency):
            currency = str(currency or home_currency)
            native = round_currency(
                conn, max(0.0, float(value or 0)), currency
            )
            if str(currency or home_currency) == home_currency or native == 0:
                return native
            return CurrencyManager.convert(
                conn, int(round(native*SCALE)), currency, home_currency
            )/SCALE

        income = max(0.0, float(annual_income or 0))
        product_currency = str(product["quote_currency"] or home_currency)
        maturity_days = max(1, int(product["maturity_days"] or 365))
        maturity_years = maturity_days / 365.0
        amortization_type = str(product["amortization_type"] or "legacy")
        frequency_days = max(1, int(product["payment_frequency_days"] or maturity_days))
        proposed_schedule = None
        if amortization_type == "legacy":
            proposed_principal_service = min(
                float(amount), float(amount) / max(1.0, maturity_years)
            )
            proposed_interest = (
                float(amount) * max(0.0, float(annual_rate or 0)) / 100.0
            )
            proposed_native_service = round_currency(
                conn, proposed_principal_service+proposed_interest, product_currency
            )
        else:
            proposed_schedule = BankManager._loan_cashflows(
                amount, annual_rate, maturity_days, frequency_days,
                amortization_type,
                product["day_count_convention"] or "ACT/365",
                float(now), decimal_places=currency_precision(conn, product_currency),
            )
            proposed_native_service = round_currency(
                conn,
                sum(principal_due+interest_due for due_day, principal_due, interest_due
                    in proposed_schedule if due_day <= 365),
                product_currency,
            )
        proposed_service = to_home(proposed_native_service, product_currency)
        proposed_stress_delta = 0.0
        stress_applicable = False
        if product["rate_type"] == "floating":
            cycle = max(1, int(product["repricing_cycle_seconds"] or 31536000))
            if cycle <= 365*86400:
                stress_applicable = True
                current = float(annual_rate or 0)
                upper = (float(product["rate_cap"])
                         if product["rate_cap"] is not None else float("inf"))
                shock_percent = max(0.0, float(stress_rate_shock_bps))/100.0
                stressed = min(upper, max(float(product["rate_floor"] or 0), current+shock_percent))
                proposed_stress_delta = max(0.0, stressed-current)

        native_totals = {}
        missed = defaults = 0
        loans = conn.execute(
            "SELECT loan_id,outstanding_balance,quote_currency,contract_rate,rate_type,"
            "next_repricing_epoch,rate_floor,rate_cap,missed_payment_count,status "
            "FROM accounts.loans WHERE user_id=? AND status IN ('active','defaulted')",
            (str(user_id),),
        ).fetchall()
        horizon = float(now) + 365 * 86400
        for loan in loans:
            currency = str(loan["quote_currency"] or home_currency)
            balance = round_currency(
                conn, max(0.0, float(loan["outstanding_balance"] or 0)), currency
            )
            current_interest = round_currency(
                conn, balance*max(0.0, float(loan["contract_rate"] or 0))/100.0,
                currency,
            )
            totals = native_totals.setdefault(
                currency, {"balance": 0.0, "interest": 0.0,
                           "service": 0.0, "stress_interest": 0.0}
            )
            totals["balance"] += balance
            totals["interest"] += current_interest
            if (loan["rate_type"] == "floating" and loan["next_repricing_epoch"] is not None
                    and float(loan["next_repricing_epoch"]) <= horizon):
                stress_applicable = True
                current = float(loan["contract_rate"] or 0)
                upper = float(loan["rate_cap"]) if loan["rate_cap"] is not None else float("inf")
                shock_percent = max(0.0, float(stress_rate_shock_bps))/100.0
                stressed = min(upper, max(float(loan["rate_floor"] or 0), current+shock_percent))
                delta = max(0.0, stressed-current)
                totals["stress_interest"] += round_currency(
                    conn, balance*delta/100.0, currency
                )
            scheduled = conn.execute(
                "SELECT COALESCE(SUM(MAX(0,principal_due+interest_due+fee_due-amount_paid)),0) "
                "FROM accounts.loan_payment_schedule WHERE loan_id=? AND due_epoch<=? "
                "AND status NOT IN ('paid','waived')",
                (loan["loan_id"], horizon),
            ).fetchone()[0]
            if scheduled:
                service = round_currency(conn, scheduled, currency)
            elif balance:
                service = min(balance, balance+current_interest)
            else:
                service = 0.0
            totals["service"] += round_currency(conn, service, currency)
            missed += max(0, int(loan["missed_payment_count"] or 0))
            defaults += int(loan["status"] == "defaulted")

        existing_balance = sum(
            to_home(values["balance"], currency)
            for currency, values in native_totals.items()
        )
        existing_interest = sum(
            to_home(values["interest"], currency)
            for currency, values in native_totals.items()
        )
        existing_service = sum(
            to_home(values["service"], currency)
            for currency, values in native_totals.items()
        )
        existing_stress_interest = sum(
            to_home(values["stress_interest"], currency)
            for currency, values in native_totals.items()
        )
        fx_stress_applicable = any(
            currency != home_currency and values["service"] > 0
            for currency, values in native_totals.items()
        )
        existing_fx_stress_service = sum(
            to_home(
                round_currency(
                    conn, (values["service"]+values["stress_interest"])
                    *max(0.0, float(stress_fx_shock_percent))/100,
                    currency,
                ), currency,
            )
            for currency, values in native_totals.items()
            if currency != home_currency
        )
        if proposed_schedule is None:
            proposed_stress_native = amount*proposed_stress_delta/100.0
        else:
            stressed_schedule = BankManager._loan_cashflows(
                amount, float(annual_rate or 0)+proposed_stress_delta,
                maturity_days, frequency_days, amortization_type,
                product["day_count_convention"] or "ACT/365",
                float(now), decimal_places=currency_precision(conn, product_currency),
            )
            stressed_service = sum(
                principal_due+interest_due
                for due_day, principal_due, interest_due in stressed_schedule
                if due_day <= 365
            )
            base_service = sum(
                principal_due+interest_due
                for due_day, principal_due, interest_due in proposed_schedule
                if due_day <= 365
            )
            proposed_stress_native = max(0.0, stressed_service-base_service)
        proposed_stress_interest = to_home(
            round_currency(conn, proposed_stress_native, product_currency),
            product_currency,
        )
        proposed_fx_stress_service = (
            round_currency(
                conn,
                (proposed_service+proposed_stress_interest)
                *max(0.0, float(stress_fx_shock_percent))/100.0,
                home_currency,
            )
            if product_currency != home_currency else 0.0
        )

        if income > 0:
            # DTI includes the proposed loan's annual debt service and interest
            # on other debt; DSR includes annual principal and interest for all.
            dti = 100.0 * (proposed_service + existing_interest) / income
            dsr = 100.0 * (proposed_service + existing_service) / income
            stress_dsr = (
                100.0*(proposed_service+proposed_stress_interest
                       +proposed_fx_stress_service
                       +existing_service+existing_stress_interest
                       +existing_fx_stress_service)/income
                if stress_applicable or fx_stress_applicable
                or product_currency != home_currency else None
            )
        else:
            dti = dsr = stress_dsr = None
        history_index = min(1.0, (missed + defaults * 5) / 10.0)
        return {
            "income": income,
            "dti": dti,
            "dsr": dsr,
            "stress_dsr": stress_dsr,
            "fx_stress_shock_percent": float(stress_fx_shock_percent),
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
            from managers.compliance_manager import ComplianceManager
            ComplianceManager.check_financial_eligibility(
                self.db, user_id, "loan_apply"
            )
            self._settle_user_interest(user_id)
            with self.db.atomic(("users", "accounts", "product", "assets", "company")) as conn:
                ComplianceManager.require_financial_eligibility(conn, user_id)
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
                from functions.money import from_minor_units
                places = currency_precision(conn, product["quote_currency"])
                amount_units = to_minor_units(amount, places)
                collateral_units = to_minor_units(collateral_amount, places)
                amount_decimal = from_minor_units(amount_units, places)
                collateral_decimal = from_minor_units(collateral_units, places)
                amount = float(amount_decimal)
                collateral_amount = float(collateral_decimal)
                from managers.currency_manager import CurrencyManager
                home = CurrencyManager.home(conn,user_id)
                if product['quote_currency'] != home:
                    return False, f"❌ 이 대출은 {product['quote_currency']} 상품입니다. 국적 통화 {home} 대출만 이용할 수 있습니다."
                if conn.execute(
                    "SELECT 1 FROM accounts.loans WHERE user_id = ? AND product_code = ?",
                    (str(user_id), product_code),
                ).fetchone():
                    return False, f"❌ 이미 {product['name']} 상품을 이용한 기록이 있습니다."
                CreditScoreManager.refresh(conn, user_id, trigger="loan_application")
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
                cash_covered = min(amount, collateral_amount)
                rwa_asset_covered = min(
                    max(0.0, amount-cash_covered), max(0.0, asset_collateral_value)*0.80
                )
                unsecured = max(0.0, amount-cash_covered-rwa_asset_covered)
                high_risk = (
                    credit_score < 600
                    or (asset_collateral_value > 0
                        and amount/asset_collateral_value >= 1.0)
                )
                unsecured_weight = 1.0 if high_risk else 0.75
                proposed_rwa = rwa_asset_covered*0.35+unsecured*unsecured_weight
                proposed_risk_weight = proposed_rwa/amount
                capacity = RateManager.institution_capacity(
                    conn, product["issuer"], amount, product['quote_currency'],
                    proposed_risk_weight=proposed_risk_weight,
                )
                if not capacity["allowed"]:
                    if capacity["status"] != "active":
                        return False, (
                            "❌ 발행 금융기관이 자본규제 또는 정리 절차로 신규 대출을 "
                            "취급할 수 없습니다."
                        )
                    return False, (
                        "❌ 발행 금융기관의 지급준비·자기자본·레버리지 한도를 초과합니다. "
                        f"현재 추가 대출 가능액: {math.floor(capacity['headroom']):,} "
                        f"{product['quote_currency']}"
                    )
                proposed_country = product['rate_nationality'] or user['nationality']
                contract_country = (proposed_country if proposed_country and conn.execute(
                    "SELECT 1 FROM country_currencies WHERE nationality=?", (proposed_country,)
                ).fetchone() else None)
                stress_country = user["nationality"]
                if not stress_country or not conn.execute(
                    "SELECT 1 FROM country_currencies WHERE nationality=?",
                    (stress_country,),
                ).fetchone():
                    stress_country = contract_country
                stress_policy = (conn.execute(
                    "SELECT credit_stress_shock_bps,fx_stress_shock_percent "
                    "FROM country_macro_indicators "
                    "WHERE nationality=?", (stress_country,),
                ).fetchone() if stress_country else None)
                stress_shock_bps = max(0, min(1000, int(
                    stress_policy["credit_stress_shock_bps"] if stress_policy else 200
                )))
                fx_stress_shock_percent = max(0.0, min(100.0, float(
                    stress_policy["fx_stress_shock_percent"] if stress_policy else 10
                )))
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
                    opened_epoch, effective_income, stress_shock_bps,
                    fx_stress_shock_percent,
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
                if (max_dsr > 0 and underwriting['stress_dsr'] is not None
                        and underwriting['stress_dsr'] > max_dsr+1e-9):
                    return False, (
                        f"❌ +{stress_shock_bps}bp 금리상승 스트레스 DSR {underwriting['stress_dsr']:.1f}%가 "
                        f"상품 한도 {max_dsr:g}%를 초과합니다."
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
                # Recheck affordability against the signed price after bank and
                # borrower spreads have been applied to the initial scorecard.
                underwriting = self._underwriting_metrics(
                    conn, user_id, amount, product, contract_rate,
                    opened_epoch, effective_income, stress_shock_bps,
                    fx_stress_shock_percent,
                )
                if max_dti > 0 and underwriting['dti'] > max_dti+1e-9:
                    return False, (
                        f"❌ 계약금리 반영 후 DTI {underwriting['dti']:.1f}%가 상품 한도 "
                        f"{max_dti:g}%를 초과합니다."
                    )
                if max_dsr > 0 and underwriting['dsr'] > max_dsr+1e-9:
                    return False, (
                        f"❌ 계약금리 반영 후 DSR {underwriting['dsr']:.1f}%가 상품 한도 "
                        f"{max_dsr:g}%를 초과합니다."
                    )
                if (max_dsr > 0 and underwriting['stress_dsr'] is not None
                        and underwriting['stress_dsr'] > max_dsr+1e-9):
                    return False, (
                        f"❌ 계약금리 반영 후 +{stress_shock_bps}bp 스트레스 DSR "
                        f"{underwriting['stress_dsr']:.1f}%가 상품 한도 "
                        f"{max_dsr:g}%를 초과합니다."
                    )
                affordability_utilization = max(
                    (underwriting['dti'] or 0)/(max_dti or 100),
                    (underwriting['dsr'] or 0)/(max_dsr or 100),
                )
                borrower_risk_score = max(0.0, min(100.0,
                    credit_score/10.0-underwriting['history_index']*25.0
                    - min(1.0, affordability_utilization)*20.0
                ))
                fee_units = to_minor_units(
                    amount_decimal*Decimal(str(product['origination_fee_percent'] or 0))
                    / Decimal(100), places
                )
                mandatory_fee_units = to_minor_units(
                    amount_decimal*Decimal(str(product['mandatory_cost_percent'] or 0))
                    / Decimal(100), places
                )
                total_fee_units = fee_units+mandatory_fee_units
                fee_decimal = from_minor_units(fee_units, places)
                mandatory_fee_decimal = from_minor_units(mandatory_fee_units, places)
                total_fee_decimal = from_minor_units(total_fee_units, places)
                net_payout_decimal = from_minor_units(
                    max(0, amount_units-total_fee_units), places
                )
                fee = float(fee_decimal)
                mandatory_fee = float(mandatory_fee_decimal)
                total_finance_fee = float(total_fee_decimal)
                net_payout = float(net_payout_decimal)
                effective = RateManager.effective_annual_rate(
                    contract_rate, float(product['interest_cycle'] or 31536000)
                )
                from managers.credit_risk_manager import CreditRiskManager
                origination_pd_12m = CreditRiskManager.origination_pd_12m(
                    conn, user['nationality'], credit_score, underwriting['dti'],
                    underwriting['dsr'],
                    (amount/total_collateral_value if total_collateral_value else 0.0),
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
                    "underwriting_dti,underwriting_dsr,underwriting_stress_dsr,underwriting_stress_shock_bps,borrower_risk_score,underwriting_income,"
                    "origination_pd_12m,quote_currency,prepayment_fee_percent,prepayment_fee_decline_days) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                     underwriting['dti'], underwriting['dsr'], underwriting['stress_dsr'], stress_shock_bps, borrower_risk_score,
                     effective_income, origination_pd_12m, home,
                     float(product['prepayment_fee_percent'] or 0),
                     int(product['prepayment_fee_decline_days'] or 0)),
                )
                loan_id = int(inserted.lastrowid)
                conn.execute(
                    "UPDATE accounts.loans SET underwriting_fx_stress_shock_percent=? "
                    "WHERE loan_id=?", (fx_stress_shock_percent, loan_id),
                )
                from managers.bank_journal import BankJournal
                BankJournal.post(
                    conn, product["issuer"], "loan_origination", f"loan:{loan_id}", home,
                    [("loans_receivable", "debit", amount_decimal),
                     ("customer_funds_payable", "credit", net_payout_decimal),
                     ("fee_income", "credit", total_fee_decimal)],
                    description=f"Loan {loan_id} originated", now=opened_epoch,
                )
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
                            (loan_id, index, due_epoch, float(principal_due), float(interest_due),
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
                CreditScoreManager.refresh(conn, user_id, now=opened_epoch, trigger="loan_opened")
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
        try:
            now = time.time() if now is None else now
            if isinstance(now, bool):
                return False, '❌ 처리 시각이 올바르지 않습니다.'
            now = float(now)
        except (TypeError, ValueError, OverflowError):
            return False, '❌ 처리 시각이 올바르지 않습니다.'
        if not math.isfinite(now) or not 0 <= now <= self.MAX_SUPPORTED_EPOCH:
            return False, '❌ 처리 시각이 올바르지 않습니다.'
        try:
            identity = str(loan_id).strip()
            self._settle_user_interest(user_id, now=now)
            with self.db.atomic() as conn:
                CreditScoreManager.refresh(conn, user_id, now, trigger="loan_extension")
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
        from functions.money import currency_precision, from_minor_units, to_minor_units

        if isinstance(amount, bool):
            return False, "❌ 상환액을 확인해주세요."
        try:
            self._settle_user_interest(user_id)
            with self.db.atomic(("users", "accounts")) as conn:
                if loan_id is None:
                    loan = conn.execute(
                        "SELECT l.loan_id,l.outstanding_balance,l.outstanding_balance_minor_units,"
                        "l.status,l.provisioned_loss,l.provisioned_loss_minor_units,l.quote_currency,"
                        "l.created_at,l.maturity_epoch,l.prepayment_fee_percent,"
                        "l.prepayment_fee_decline_days,"
                        "p.issuer FROM accounts.loans l JOIN product.liability p "
                        "ON p.product_code=l.product_code WHERE l.user_id=? "
                        "AND l.product_code=? AND l.status IN ('active','defaulted')",
                        (str(user_id), product_code),
                    ).fetchone()
                else:
                    loan = conn.execute(
                        "SELECT l.loan_id,l.outstanding_balance,l.outstanding_balance_minor_units,"
                        "l.status,l.provisioned_loss,l.provisioned_loss_minor_units,l.quote_currency,"
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
                currency = str(loan["quote_currency"] or base_currency_code(conn))
                places = currency_precision(conn, currency)

                def amount_value(value):
                    exact = from_minor_units(value, places)
                    return int(exact) if places == 0 else float(exact)

                outstanding_units = (max(0, int(loan["outstanding_balance_minor_units"] or 0))
                                     if "outstanding_balance_minor_units" in loan.keys()
                                     else max(0, to_minor_units(
                                         loan["outstanding_balance"] or 0, places
                                     )))
                payment_units = min(max(0, to_minor_units(amount, places)), outstanding_units)
                payment = amount_value(payment_units)
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
                fee_units = max(0, to_minor_units(prepayment_fee, places))
                prepayment_fee = amount_value(fee_units)
                total_debit_units = payment_units+fee_units
                total_debit = amount_value(total_debit_units)
                funds = UserFunds.choose(conn, str(user_id), loan["quote_currency"])
                if not funds or funds[0] < total_debit:
                    return False, "❌ 상환할 잔액이 부족합니다."
                if not UserFunds.debit(conn, str(user_id), funds[1], total_debit):
                    return False, "❌ 잔액이 부족합니다."
                remaining_units = max(0, outstanding_units-payment_units)
                remaining = amount_value(remaining_units)
                recovery_units = min(
                    payment_units,
                    max(0, int(loan["provisioned_loss_minor_units"] or 0))
                    if "provisioned_loss_minor_units" in loan.keys()
                    else max(0, to_minor_units(loan["provisioned_loss"] or 0, places)),
                )
                recovery = amount_value(recovery_units)
                released = 0
                if remaining_units <= 0:
                    released = self._release_collateral(conn, loan["loan_id"], str(user_id))
                conn.execute(
                    "UPDATE accounts.loans SET outstanding_balance = ?, "
                    "status = CASE WHEN ? <= 0 THEN 'paid' ELSE status END, "
                    "collection_status=CASE WHEN ? <= 0 THEN 'paid' ELSE collection_status END,"
                    "provisioned_loss_minor_units=MAX(0,provisioned_loss_minor_units-?),"
                    "prepayment_fees_paid=prepayment_fees_paid+? "
                    "WHERE loan_id = ?",
                    (remaining, remaining_units, remaining_units,
                     recovery_units, prepayment_fee, loan["loan_id"]),
                )
                if prepayment_fee:
                    repayment_ref = f"loan:{loan['loan_id']}:payment:{time.time_ns()}"
                    RateManager.record_institution_result(
                        conn, loan["issuer"], prepayment_fee, "prepayment_fee",
                        repayment_ref,
                        now=now, currency_code=loan["quote_currency"],
                    )
                else:
                    repayment_ref = f"loan:{loan['loan_id']}:payment:{time.time_ns()}"
                from managers.bank_journal import BankJournal
                BankJournal.post(
                    conn, loan["issuer"], "loan_repayment",
                    repayment_ref,
                    loan["quote_currency"],
                    [("settlement_asset", "debit", total_debit),
                     ("default_loss_allowance", "debit", recovery),
                     ("loans_receivable", "credit", payment),
                     ("credit_loss_recovery_income", "credit", recovery),
                     ("fee_income", "credit", prepayment_fee)],
                    description=f"Loan {loan['loan_id']} repayment", now=now,
                )
                if remaining_units <= 0:
                    self._release_ecl_allowance(
                        conn, loan["loan_id"], loan["issuer"], loan["quote_currency"],
                        now=now, reason=f"repay:{time.time_ns()}",
                    )
                if recovery_units:
                    RateManager.record_institution_result(
                        conn, loan["issuer"], recovery, "default_recovery",
                        repayment_ref,
                        currency_code=loan["quote_currency"],
                    )
                self._allocate_schedule_payment(
                    conn, loan["loan_id"], payment, now
                )
                if remaining_units <= 0:
                    CreditScoreManager.record(
                        conn, user_id, "loan_closed", f"loan:{loan['loan_id']}",
                        now=now, loan_id=loan["loan_id"],
                    )
                else:
                    CreditScoreManager.refresh(conn, user_id, now, trigger="repayment")
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
        paid_count = missed_count = 0
        notifications = []
        try:
            now = time.time() if now is None else now
            if isinstance(now, bool):
                return paid_count, missed_count, notifications
            now = float(now)
        except (TypeError, ValueError, OverflowError):
            return paid_count, missed_count, notifications
        if not math.isfinite(now) or not 0 <= now <= self.MAX_SUPPORTED_EPOCH:
            return paid_count, missed_count, notifications
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
                    "l.outstanding_balance_minor_units, "
                    "l.last_repayment_epoch, p.name AS product_name, p.issuer, "
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
                        from functions.money import currency_precision, from_minor_units, to_minor_units

                        cycle = int(loan["repayment_cycle_seconds"] or 0)
                        currency = str(loan["quote_currency"] or base_currency_code(conn))
                        places = currency_precision(conn, currency)
                        installment_units = max(0, to_minor_units(
                            loan["repayment_amount"] or 0, places
                        ))
                        balance_units = (max(0, int(loan["outstanding_balance_minor_units"] or 0))
                                         if "outstanding_balance_minor_units" in loan.keys()
                                         else max(0, to_minor_units(
                                             loan["outstanding_balance"] or 0, places
                                         )))
                        last = float(loan["last_repayment_epoch"] or now)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    def amount_value(value):
                        exact = from_minor_units(value, places)
                        return int(exact) if places == 0 else float(exact)

                    if cycle <= 0 or installment_units <= 0:
                        continue
                    if balance_units <= 0 or not math.isfinite(last):
                        if balance_units <= 0:
                            self._release_collateral(
                                conn, loan["loan_id"], str(loan["user_id"])
                            )
                            conn.execute(
                                "UPDATE accounts.loans SET outstanding_balance = 0, status = 'paid',"
                                "collection_status='paid' "
                                "WHERE loan_id = ?",
                                (loan["loan_id"],),
                            )
                            self._release_ecl_allowance(
                                conn, loan["loan_id"], loan["issuer"], loan["quote_currency"],
                                now=now, reason=f"scheduled-zero:{int(now)}",
                            )
                            CreditScoreManager.record(
                                conn, loan["user_id"], "loan_closed", f"loan:{loan['loan_id']}",
                                now=now, loan_id=loan["loan_id"],
                            )
                        continue

                    due_periods = int((now - last) // cycle)
                    if due_periods <= 0:
                        continue

                    periods_to_process = min(due_periods, max_catch_up)
                    processed = 0
                    loan_paid_count = loan_missed_count = 0
                    loan_paid_amount_units = 0
                    for _ in range(periods_to_process):
                        if balance_units <= 0:
                            conn.execute(
                                "UPDATE accounts.loans SET outstanding_balance = 0, status = 'paid' "
                                "WHERE loan_id = ?",
                                (loan["loan_id"],),
                            )
                            break

                        payment_units = min(installment_units, balance_units)
                        payment = amount_value(payment_units)
                        funds = UserFunds.choose(
                            conn, str(loan["user_id"]), loan["quote_currency"]
                        )
                        if funds and funds[0] >= payment and UserFunds.debit(
                            conn, str(loan["user_id"]), funds[1], payment
                        ):
                            balance_units = max(0, balance_units-payment_units)
                            paid_count += 1
                            loan_paid_count += 1
                            loan_paid_amount_units += payment_units
                            due_epoch = last+(processed+1)*cycle
                            CreditScoreManager.record(
                                conn, loan["user_id"], "payment_on_time",
                                f"loan:{loan['loan_id']}:legacy:{int(due_epoch)}",
                                occurred_epoch=due_epoch, now=now,
                                loan_id=loan["loan_id"], refresh=False,
                            )
                            from managers.bank_journal import BankJournal
                            BankJournal.post(
                                conn, loan["issuer"], "loan_repayment",
                                f"loan:{loan['loan_id']}:scheduled:{int(last+(processed+1)*cycle)}",
                                loan["quote_currency"],
                                [("settlement_asset", "debit", payment),
                                 ("loans_receivable", "credit", payment)],
                                description=f"Scheduled repayment for loan {loan['loan_id']}",
                                now=last+(processed+1)*cycle,
                            )
                            conn.execute(
                                "UPDATE accounts.loans SET outstanding_balance = ?, "
                                "status = CASE WHEN ? <= 0 THEN 'paid' ELSE 'active' END "
                                "WHERE loan_id = ?",
                                (amount_value(balance_units), balance_units, loan["loan_id"]),
                            )
                            if balance_units <= 0:
                                self._release_collateral(
                                    conn, loan["loan_id"], str(loan["user_id"])
                                )
                                self._release_ecl_allowance(
                                    conn, loan["loan_id"], loan["issuer"], loan["quote_currency"],
                                    now=last+(processed+1)*cycle,
                                    reason=f"scheduled:{int(last+(processed+1)*cycle)}",
                                )
                        else:
                            missed_count += 1
                            loan_missed_count += 1
                            due_epoch = last+(processed+1)*cycle
                            CreditScoreManager.record(
                                conn, loan["user_id"], "payment_missed",
                                f"loan:{loan['loan_id']}:legacy:{int(due_epoch)}",
                                occurred_epoch=due_epoch, now=now,
                                loan_id=loan["loan_id"], refresh=False,
                            )
                        processed += 1
                        if balance_units <= 0:
                            break

                    if processed:
                        conn.execute(
                            "UPDATE accounts.loans SET last_repayment_epoch = ? "
                            "WHERE loan_id = ? AND status = 'active'",
                            (last + processed * cycle, loan["loan_id"]),
                        )
                    if balance_units <= 0:
                        CreditScoreManager.record(
                            conn, loan["user_id"], "loan_closed", f"loan:{loan['loan_id']}",
                            now=now, loan_id=loan["loan_id"], refresh=False,
                        )
                    if loan_paid_count or loan_missed_count or balance_units <= 0:
                        CreditScoreManager.refresh(
                            conn, loan["user_id"], now, trigger="scheduled_payment"
                        )
                    if loan_paid_count or loan_missed_count:
                        notifications.append({
                            "user_id": str(loan["user_id"]),
                            "product_name": str(loan["product_name"] or loan["product_code"]),
                            "paid_count": loan_paid_count,
                            "paid_amount": amount_value(loan_paid_amount_units),
                            "missed_count": loan_missed_count,
                            "remaining": amount_value(balance_units),
                        })
                RateManager.refresh_all_market_pressure(conn, now=now)
            return paid_count, missed_count, notifications
        except Exception:
            raise

    def refresh_credit_scores(self, now=None):
        """Settle loans past maturity, then re-score users with stale scores."""
        now = time.time() if now is None else float(now)
        last = getattr(self, "_last_overdue_sweep", None)
        if last is None or now-last >= self.OVERDUE_SWEEP_INTERVAL_SECONDS:
            self._last_overdue_sweep = now
            for row in self.db.accounts.execute(
                "SELECT DISTINCT user_id FROM loans WHERE status='active' "
                "AND outstanding_balance>0 AND maturity_epoch IS NOT NULL AND maturity_epoch<?",
                (now,),
            ):
                self._settle_user_interest(str(row["user_id"]), now=now)
        return CreditScoreManager(self.db).refresh_due(now)

    @staticmethod
    def _allocate_schedule_payment(conn, loan_id, amount, paid_epoch):
        from functions.money import currency_precision, from_minor_units, to_minor_units

        loan = conn.execute("SELECT quote_currency FROM accounts.loans WHERE loan_id=?",
                            (loan_id,)).fetchone()
        if not loan:
            return
        currency = str(loan["quote_currency"] or base_currency_code(conn))
        places = currency_precision(conn, currency)

        def units(row, field):
            minor_field = f"{field}_minor_units"
            if minor_field in row.keys():
                return max(0, int(row[minor_field] or 0))
            return max(0, to_minor_units(row[field] or 0, places))

        def amount_value(value):
            exact = from_minor_units(value, places)
            return int(exact) if places == 0 else float(exact)

        remaining = max(0, to_minor_units(amount or 0, places))
        if not remaining:
            return
        rows = conn.execute(
            "SELECT * "
            "FROM accounts.loan_payment_schedule WHERE loan_id=? "
            "AND status NOT IN ('paid','waived') ORDER BY installment_number",
            (loan_id,),
        ).fetchall()
        for row in rows:
            unpaid_fee = max(0, units(row, "fee_due")-units(row, "fee_paid"))
            unpaid_interest = max(0, units(row, "interest_due")-units(row, "interest_paid"))
            unpaid_principal = max(0, units(row, "principal_due")-units(row, "principal_paid"))
            due = units(row, "principal_due")+units(row, "interest_due")+units(row, "fee_due")
            unpaid = max(0, due-units(row, "amount_paid"))
            applied = min(remaining, unpaid)
            if not applied:
                continue
            fee_applied = min(applied, unpaid_fee)
            interest_applied = min(applied-fee_applied, unpaid_interest)
            principal_applied = min(applied-fee_applied-interest_applied, unpaid_principal)
            new_paid = units(row, "amount_paid")+applied
            status = "paid" if new_paid >= due else "partial"
            conn.execute(
                "UPDATE accounts.loan_payment_schedule SET amount_paid=?,fee_paid=fee_paid+?,"
                "interest_paid=interest_paid+?,principal_paid=principal_paid+?,status=?,"
                "paid_epoch=CASE WHEN ?='paid' THEN ? ELSE paid_epoch END WHERE schedule_id=?",
                (amount_value(new_paid), amount_value(fee_applied),
                 amount_value(interest_applied), amount_value(principal_applied),
                 status, status, paid_epoch, row["schedule_id"]),
            )
            # Bureau-style reporting: a missed installment paid late is a cure;
            # one settled by its due date is an on-time payment.
            if status == "paid" and (row["status"] == "missed"
                                     or paid_epoch <= float(row["due_epoch"])):
                owner = conn.execute(
                    "SELECT user_id FROM accounts.loans WHERE loan_id=?", (loan_id,)
                ).fetchone()
                if owner:
                    cured = row["status"] == "missed"
                    CreditScoreManager.record(
                        conn, owner[0], "payment_cured" if cured else "payment_on_time",
                        f"schedule:{row['schedule_id']}", occurred_epoch=paid_epoch,
                        now=paid_epoch, loan_id=loan_id, refresh=False,
                    )
            remaining -= applied
            if remaining <= 0:
                break

    @staticmethod
    def _apply_schedule_installment(conn, schedule_id, amount, paid_epoch):
        from functions.money import currency_precision, from_minor_units, to_minor_units

        row = conn.execute(
            "SELECT s.*,l.quote_currency FROM accounts.loan_payment_schedule s "
            "JOIN accounts.loans l ON l.loan_id=s.loan_id WHERE s.schedule_id=?",
            (schedule_id,),
        ).fetchone()
        if not row:
            return 0.0
        places = currency_precision(conn, str(row["quote_currency"] or base_currency_code(conn)))

        def units(row, field):
            minor_field = f"{field}_minor_units"
            if minor_field in row.keys():
                return max(0, int(row[minor_field] or 0))
            return max(0, to_minor_units(row[field] or 0, places))

        def amount_value(value):
            exact = from_minor_units(value, places)
            return int(exact) if places == 0 else float(exact)

        fee_due = max(0, units(row, "fee_due")-units(row, "fee_paid"))
        interest_due = max(0, units(row, "interest_due")-units(row, "interest_paid"))
        principal_due = max(0, units(row, "principal_due")-units(row, "principal_paid"))
        applied = min(max(0, to_minor_units(amount or 0, places)),
                      fee_due+interest_due+principal_due)
        fee_applied = min(applied, fee_due)
        interest_applied = min(applied-fee_applied, interest_due)
        principal_applied = min(applied-fee_applied-interest_applied, principal_due)
        new_paid = units(row, "amount_paid")+applied
        total_due = units(row, "fee_due")+units(row, "interest_due")+units(row, "principal_due")
        status = "paid" if new_paid >= total_due else "partial"
        conn.execute(
            "UPDATE accounts.loan_payment_schedule SET amount_paid=?,fee_paid=fee_paid+?,"
            "interest_paid=interest_paid+?,principal_paid=principal_paid+?,status=?,"
            "paid_epoch=CASE WHEN ?='paid' THEN ? ELSE paid_epoch END WHERE schedule_id=?",
            (amount_value(new_paid), amount_value(fee_applied),
             amount_value(interest_applied), amount_value(principal_applied),
             status, status, paid_epoch, schedule_id),
        )
        return amount_value(applied)

    @classmethod
    def _process_due_schedule(cls, conn, loan, now):
        from functions.money import currency_precision, from_minor_units, to_minor_units

        rows = conn.execute(
            "SELECT * FROM accounts.loan_payment_schedule WHERE loan_id=? "
            "AND due_epoch<=? AND status NOT IN ('paid','waived') "
            "ORDER BY installment_number",
            (loan["loan_id"], now),
        ).fetchall()
        currency = str(loan["quote_currency"] or base_currency_code(conn))
        places = currency_precision(conn, currency)

        def units(row, field):
            minor_field = f"{field}_minor_units"
            if minor_field in row.keys():
                return max(0, int(row[minor_field] or 0))
            return max(0, to_minor_units(row[field] or 0, places))

        def amount_value(value):
            exact = from_minor_units(value, places)
            return int(exact) if places == 0 else float(exact)

        balance = (max(0, int(loan["outstanding_balance_minor_units"] or 0))
                   if "outstanding_balance_minor_units" in loan.keys()
                   else max(0, to_minor_units(loan["outstanding_balance"] or 0, places)))
        paid_count = missed_count = paid_amount_units = 0
        for row in rows:
            contractual = sum(units(row, field) for field in
                              ("principal_due", "interest_due", "fee_due"))
            due = min(balance, max(0, contractual-units(row, "amount_paid")))
            if due <= 0:
                conn.execute(
                    "UPDATE accounts.loan_payment_schedule SET status='paid',paid_epoch=? "
                    "WHERE schedule_id=?", (now, row["schedule_id"]),
                )
                continue
            due_amount = amount_value(due)
            funds = UserFunds.choose(
                conn, str(loan["user_id"]), currency
            )
            if funds and funds[0] >= due_amount and UserFunds.debit(
                conn, str(loan["user_id"]), funds[1], due_amount
            ):
                balance -= due
                paid_count += 1
                paid_amount_units += due
                cured = row["status"] == "missed"
                CreditScoreManager.record(
                    conn, loan["user_id"], "payment_cured" if cured else "payment_on_time",
                    f"schedule:{row['schedule_id']}",
                    occurred_epoch=now if cured else float(row["due_epoch"]), now=now,
                    loan_id=loan["loan_id"], refresh=False,
                )
                from managers.bank_journal import BankJournal
                BankJournal.post(
                    conn, loan["issuer"], "loan_repayment",
                    f"loan:{loan['loan_id']}:schedule:{row['schedule_id']}",
                    currency,
                    [("settlement_asset", "debit", due_amount),
                     ("loans_receivable", "credit", due_amount)],
                    description=f"Scheduled repayment for loan {loan['loan_id']}",
                    now=now,
                )
                cls._apply_schedule_installment(conn, row["schedule_id"], due_amount, now)
            else:
                newly_missed = row["status"] != "missed"
                missed_count += int(newly_missed)
                conn.execute(
                    "UPDATE accounts.loan_payment_schedule SET status='missed' "
                    "WHERE schedule_id=?", (row["schedule_id"],),
                )
                if newly_missed:
                    CreditScoreManager.record(
                        conn, loan["user_id"], "payment_missed",
                        f"schedule:{row['schedule_id']}",
                        occurred_epoch=float(row["due_epoch"]), now=now,
                        loan_id=loan["loan_id"], refresh=False,
                    )
        status = "paid" if balance <= 0 else "active"
        conn.execute(
            "UPDATE accounts.loans SET outstanding_balance=?,status=?,"
            "collection_status=CASE WHEN ?='paid' THEN 'paid' ELSE collection_status END,"
            "missed_payment_count=missed_payment_count+? WHERE loan_id=?",
            (amount_value(max(0, balance)), status, status, missed_count, loan["loan_id"]),
        )
        if balance <= 0:
            cls._release_collateral(conn, loan["loan_id"], str(loan["user_id"]))
            BankManager._release_ecl_allowance(
                conn, loan["loan_id"], loan["issuer"], loan["quote_currency"],
                now=now, reason=f"schedule:{int(now)}",
            )
            CreditScoreManager.record(
                conn, loan["user_id"], "loan_closed", f"loan:{loan['loan_id']}",
                now=now, loan_id=loan["loan_id"], refresh=False,
            )
        if rows:
            CreditScoreManager.refresh(conn, loan["user_id"], now, trigger="scheduled_payment")
        return paid_count, missed_count, amount_value(paid_amount_units), amount_value(max(0, balance))

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
        from functions.money import currency_precision, from_minor_units, to_minor_units

        row = conn.execute(
            "SELECT collateral_amount,collateral_amount_minor_units,"
            "collateral_released,collateral_released_minor_units,"
            "collateral_seized,collateral_seized_minor_units,quote_currency "
            "FROM accounts.loans "
            "WHERE loan_id=?", (loan_id,)
        ).fetchone()
        if not row:
            return 0
        currency = str(row["quote_currency"] or base_currency_code(conn)).upper()
        places = currency_precision(conn, currency)
        def units(legacy, canonical):
            value = row[canonical]
            return (int(value or 0) if value is not None
                    else to_minor_units(row[legacy] or 0, places))
        remaining_units = max(
            0, units("collateral_amount", "collateral_amount_minor_units")
            - units("collateral_released", "collateral_released_minor_units")
            - units("collateral_seized", "collateral_seized_minor_units")
        )
        if remaining_units:
            remaining = from_minor_units(remaining_units, places)
            from managers.currency_manager import CurrencyManager
            account_number = None
            if CurrencyManager.home(conn, user_id) != currency:
                source = UserFunds.choose(conn, user_id, currency)
                account_number = source[1] if source else None
            UserFunds.credit(conn, user_id, float(remaining), account_number,
                             currency=currency)
            conn.execute(
                "UPDATE accounts.loans SET collateral_released_minor_units=? WHERE loan_id=?",
                (units("collateral_released", "collateral_released_minor_units")
                 + remaining_units, loan_id),
            )
        asset_rows = conn.execute(
            "SELECT * FROM accounts.loan_collateral_assets "
            "WHERE loan_id=? AND status='pledged' ORDER BY collateral_id",
            (loan_id,),
        ).fetchall()
        for asset in asset_rows:
            from functions.money import currency_precision, from_minor_units
            from managers.asset_cost_manager import AssetCostManager
            cost = from_minor_units(
                asset["average_cost_minor_units"] or 0,
                currency_precision(conn, currency),
            )
            AssetCostManager.upsert(
                conn, user_id, asset["asset_code"], asset["source_account"],
                asset["quantity"], cost, schema="assets",
            )
            conn.execute(
                "UPDATE accounts.loan_collateral_assets SET status='released' "
                "WHERE collateral_id=?", (asset["collateral_id"],),
            )
        return float(from_minor_units(remaining_units, places))

    @staticmethod
    def resolve_failed_institution(conn, issuer, *, now=None):
        """Freeze a failed issuer and pay insured deposits once per account."""
        issuer = str(issuer or "").strip()
        raw_when = time.time() if now is None else now
        if isinstance(raw_when, bool):
            raise ValueError("bank resolution requires a valid UNIX epoch")
        try:
            when = float(raw_when)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("bank resolution requires a valid UNIX epoch") from exc
        if not math.isfinite(when) or not 0 <= when <= BankManager.MAX_SUPPORTED_EPOCH:
            raise ValueError("bank resolution requires a valid UNIX epoch")
        institution = conn.execute(
            "SELECT f.status,f.deposit_insurance_limit,"
            "COALESCE(c.currency_code,base_currency_code()) base_currency "
            "FROM financial_institutions f LEFT JOIN country_currencies c "
            "ON c.nationality=f.nationality WHERE f.issuer=?", (issuer,),
        ).fetchone()
        if not institution or institution["status"] != "failed":
            return {"accounts": 0, "covered": 0, "uninsured": 0}
        accounts = conn.execute(
            "SELECT a.account_number,a.user_id,a.balance,a.deposit_insured,a.quote_currency,"
            "a.account_type "
            "FROM accounts.accounts a JOIN product.saving p "
            "ON p.product_code=a.product_code WHERE p.issuer=? "
            "AND a.account_type IN ('saving','currency_saving') "
            "AND a.resolution_status='active' ORDER BY a.user_id,a.account_number",
            (issuer,),
        ).fetchall()
        limit = max(0.0, float(institution["deposit_insurance_limit"] or 0))
        from managers.currency_manager import CurrencyManager, SCALE
        base_currency = str(institution["base_currency"] or base_currency_code(conn))
        insured_totals_base = {}
        for account in accounts:
            if not int(account["deposit_insured"] or 0):
                continue
            currency = str(account["quote_currency"] or base_currency_code(conn))
            balance = max(0.0, float(account["balance"] or 0))
            if currency != base_currency and balance:
                balance = CurrencyManager.convert(
                    conn, int(round(balance*SCALE)), currency, base_currency
                )/SCALE
            user_id = str(account["user_id"])
            insured_totals_base[user_id] = insured_totals_base.get(user_id, 0.0)+balance
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
                insured_total = insured_totals_base.get(user_id, 0.0)
                share = min(1.0, limit/insured_total) if insured_total > 0 else 0.0
                pro_rata_cover = round_currency(
                    conn, balance*share, account["quote_currency"] or base_currency_code(conn)
                )
                covered = min(balance, available_cover, pro_rata_cover)
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
            user_currency = CurrencyManager.home(conn, user_id)
            account_currency = str(account["quote_currency"] or base_currency_code(conn)).upper()
            covered_units = int(round(covered*SCALE))
            payout_currency = payout_amount = payout_amount_minor_units = payout_decimal_places = None
            if covered:
                payout_currency = user_currency
                payout_units = (covered_units if account_currency == user_currency else
                                CurrencyManager.convert(
                                    conn, covered_units, account_currency, user_currency
                                ))
                payout_amount = round_currency(
                    conn, payout_units/SCALE, payout_currency
                )
                from functions.money import currency_precision, to_minor_units
                payout_decimal_places = currency_precision(conn, payout_currency)
                payout_amount_minor_units = to_minor_units(
                    payout_amount, payout_decimal_places
                )
                CurrencyManager.change(conn, user_id, user_currency, payout_units)
                CurrencyManager.treasury_pay(
                    conn, CurrencyManager.user_treasury_id(conn, user_id),
                    user_currency, payout_units,
                )
                if account_currency != user_currency:
                    conn.execute(
                        "INSERT INTO accounts.currency_transactions "
                        "(user_id,source_currency,target_currency,source_units,target_units,fee_nwx,recorded_epoch) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (user_id, account_currency, user_currency, covered_units,
                         payout_units, 0, when),
                    )
            if balance > 0:
                from managers.bank_journal import BankJournal
                BankJournal.post(
                    conn, issuer, "deposit_insurance_resolution",
                    f"deposit-insurance:{account['account_number']}",
                    account["quote_currency"] or base_currency_code(conn),
                    [("customer_deposits_payable", "debit", balance),
                     ("deposit_insurance_payable", "credit", covered),
                     ("uninsured_deposit_claim", "credit", uninsured)],
                    description=f"Failed-institution claim transfer for {account['account_number']}",
                    now=when,
                )
            conn.execute(
                "UPDATE accounts.accounts SET balance_units=0,principal=0,"
                "resolution_status='resolved',insurance_paid=?,uninsured_claim=? "
                "WHERE account_number=?",
                (covered, uninsured, account["account_number"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO accounts.deposit_insurance_claims "
                "(issuer,user_id,account_number,covered_amount,uninsured_amount,"
                "payout_currency,payout_amount,payout_amount_minor_units,"
                "payout_decimal_places,resolved_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (issuer, user_id, account["account_number"], covered, uninsured,
                 payout_currency, payout_amount, payout_amount_minor_units,
                 payout_decimal_places, when),
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
        from functions.money import from_minor_units, to_minor_units

        places = currency_precision(conn, currency)
        total_quantity = float(row["quantity"] or 0)
        remaining = total_quantity
        gross_units = 0
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
            price_units = int(bid["limit_price_minor_units"] or 0)
            reserved_units = int(bid["reserved_cash_minor_units"] or 0)
            if price_units <= 0:
                price_units = to_minor_units(bid["limit_price"] or 0, places)
            if reserved_units <= 0 and float(bid["reserved_cash"] or 0) > 0:
                reserved_units = to_minor_units(bid["reserved_cash"], places)
            affordable = reserved_units // max(1, price_units)
            fill = min(int(math.floor(remaining+1e-9)),
                       int(bid["remaining_quantity"] or 0), affordable)
            if fill <= 0:
                continue
            price_units = max(1, price_units)
            price = float(from_minor_units(price_units, places))
            from managers.asset_cost_manager import AssetCostManager
            AssetCostManager.upsert(
                conn, bid["user_id"], row["asset_code"], bid["funding_account"],
                fill, price, schema="assets",
            )
            new_remaining = int(bid["remaining_quantity"])-fill
            conn.execute(
                "UPDATE assets.market_orders SET remaining_quantity=?,"
                "filled_quantity=filled_quantity+?,reserved_cash_minor_units=MAX(0,reserved_cash_minor_units-?),"
                "status=?,updated_epoch=? WHERE order_id=?",
                (new_remaining, fill, price_units*fill,
                 "filled" if new_remaining == 0 else "partial", int(now), bid["order_id"]),
            )
            gross_units += price_units*fill
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
            backstop_price_units = max(0, to_minor_units(
                Decimal(str(mark_price)) * (Decimal(1)-Decimal(str(row["haircut"] or 0))
                    - Decimal(str(market_impact))), places
            ))
            backstop_price = from_minor_units(backstop_price_units, places)
            backstop_cost_units = max(0, to_minor_units(
                backstop_price*Decimal(str(backstop_quantity)), places
            ))
            backstop_cost = from_minor_units(backstop_cost_units, places)
            gross_units += backstop_cost_units
            # The lending bank's head office buys the unsold rest and pays for it (deficits allowed).
            from managers.currency_manager import CurrencyManager, SCALE
            bank = CurrencyManager.bank_account_id(conn, product["issuer"])
            bank_home = CurrencyManager.home(conn, bank)
            cost_units = int(backstop_cost*Decimal(SCALE))
            if currency != bank_home:
                cost_units = CurrencyManager.convert(conn, cost_units, currency, bank_home)
            if cost_units:
                CurrencyManager.change(conn, bank, bank_home, -cost_units, allow_deficit=True)
            existing = conn.execute(
                "SELECT holding_id,quantity,average_cost,average_cost_minor_units FROM assets.user_assets "
                "WHERE user_id=? AND asset_code=? AND account_number IS NULL",
                (bank, row["asset_code"]),
            ).fetchone()
            from managers.asset_cost_manager import AssetCostManager
            AssetCostManager.upsert(
                conn, bank, row["asset_code"], None, backstop_quantity,
                backstop_price, schema="assets",
            )

        liquidation_cost_units = max(0, to_minor_units(
            from_minor_units(gross_units, places)
            *Decimal(str(product["liquidation_cost_percent"] or 0))/Decimal(100), places
        ))
        net_units = max(0, gross_units-liquidation_cost_units)
        gross = from_minor_units(gross_units, places)
        liquidation_cost = from_minor_units(liquidation_cost_units, places)
        net = from_minor_units(net_units, places)
        weighted_price = (from_minor_units(to_minor_units(
            gross/Decimal(str(total_quantity)), places), places)
            if total_quantity > 0 else Decimal(0))
        conn.execute(
            "UPDATE accounts.loan_collateral_assets SET status='liquidated',"
            "liquidation_price=?,liquidation_cost=?,liquidation_proceeds=? "
            "WHERE collateral_id=?",
            (float(weighted_price), float(liquidation_cost), float(net),
             row["collateral_id"]),
        )
        conn.execute(
            "INSERT OR IGNORE INTO accounts.collateral_liquidation_events "
            "(loan_id,collateral_id,asset_code,quantity,mark_price,execution_price,"
            "gross_proceeds,liquidation_cost,net_proceeds,book_filled_quantity,"
            "backstop_quantity,liquidated_epoch,mark_price_minor_units,"
            "execution_price_minor_units,gross_proceeds_minor_units,"
            "liquidation_cost_minor_units,net_proceeds_minor_units) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (loan_id, row["collateral_id"], row["asset_code"], total_quantity,
             mark_price, float(weighted_price), float(gross), float(liquidation_cost), float(net),
             book_quantity, backstop_quantity, float(now),
             to_minor_units(mark_price, places),
             to_minor_units(weighted_price, places),
             gross_units, liquidation_cost_units, net_units),
        )
        return float(net)

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
        from functions.money import from_minor_units, to_minor_units
        places = currency_precision(conn, currency)
        total_minor_units = 0
        marked = []
        for row in rows:
            quote = conn.execute(
                "SELECT price FROM assets.market_assets WHERE code=? "
                "UNION ALL SELECT share_price FROM company.company WHERE company_code=? "
                "UNION ALL SELECT unit_price FROM product.investment WHERE product_code=? LIMIT 1",
                (row["asset_code"], row["asset_code"], row["asset_code"]),
            ).fetchone()
            raw_price = quote["price"] if quote else row["last_price"]
            price_minor_units = max(1, to_minor_units(raw_price or 0, places))
            price_decimal = from_minor_units(price_minor_units, places)
            value_minor_units = max(0, to_minor_units(
                price_decimal*Decimal(str(row["quantity"]))
                *(Decimal(1)-Decimal(str(row["haircut"] or 0))), places,
            ))
            value = from_minor_units(value_minor_units, places)
            total_minor_units += value_minor_units
            price = float(price_decimal)
            marked.append((row, price, value))
            conn.execute(
                "UPDATE accounts.loan_collateral_assets SET last_price=?,last_value=? "
                "WHERE collateral_id=?", (price, float(value), row["collateral_id"]),
            )
        total = float(from_minor_units(total_minor_units, places))
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
        if new_balance <= 0:
            BankManager._release_ecl_allowance(
                conn, loan_id, product["issuer"], currency,
                now=now, reason=f"collateral:{int(now)}",
            )
        return new_balance, ltv, proceeds


class UserFunds:
    """대출 상환에서만 재사용하는 가장 잔액이 큰 자금원 헬퍼."""

    @staticmethod
    def choose(conn, user_id, currency=None):
        from managers.currency_manager import CurrencyManager
        currency = str(currency or base_currency_code(conn)).upper()
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
                f"UPDATE users.users SET cash = cash - ? WHERE user_id = ? AND (cash >= ? OR {IS_TREASURY})",
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
    def credit(conn, user_id, amount, account_number=None, currency=None):
        currency = str(currency or base_currency_code(conn)).upper()
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
