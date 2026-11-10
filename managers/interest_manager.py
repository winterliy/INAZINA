from functions.exact_money import ExactDecimal
from datetime import datetime, timezone
from contextlib import nullcontext
from decimal import Decimal, DecimalException
import math
import time
from database import DatabaseManager
from database.user_db import TREASURY_USER_ID
from functions.interest_calc import (
    calculate_compound_interest_decimal,
    calculate_simple_interest_decimal,
)
from functions.tax_calc import calculate_income_tax
from functions.money import (
    from_minor_units, round_money, round_currency, currency_precision,
    to_minor_units,
)
from managers.bank_journal import BankJournal
from managers.bank_manager import BankManager
from managers.credit_score_manager import CreditScoreManager
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

    @classmethod
    def _validated_processing_epoch(cls, value):
        try:
            return BankJournal.validate_epoch(value)
        except ValueError as exc:
            raise ValueError("invalid_interest_processing_epoch") from exc

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
    def _period_interest(principal, annual_rate_percent, cycle_seconds, periods,
                         convention="ACT/365", start_epoch=None):
        if principal <= 0 or periods <= 0:
            return 0.0
        try:
            return calculate_compound_interest_decimal(
                principal, annual_rate_percent, periods, cycle_seconds,
                convention, start_epoch
            )
        except (OverflowError, ValueError, DecimalException):
            return None

    @staticmethod
    def _due_periods(elapsed_seconds, cycle_seconds):
        if elapsed_seconds <= 0 or cycle_seconds <= 0:
            return 0
        # Tolerate binary floating-point edges for fractional-second cycles (e.g. 0.1s).
        return max(0, int(math.floor(elapsed_seconds / cycle_seconds + 1e-9)))

    @staticmethod
    def _tax_rate(conn, user_id=None, source='saving_interest'):
        row = (conn.execute("SELECT COALESCE((SELECT r.tax_percent FROM country_tax_rules r "
                            "WHERE r.nationality=u.nationality AND r.income_source=?),c.tax_percent) tax_percent "
                            "FROM users u LEFT JOIN country_currencies c "
                            "ON c.nationality=u.nationality WHERE u.user_id=?",
                            (str(source), str(user_id))).fetchone()
               if user_id is not None else None)
        fallback = conn.execute("SELECT value FROM settings WHERE key='tax_percent'").fetchone()
        try:
            rate = ExactDecimal(row["tax_percent"] if row and row["tax_percent"] is not None
                         else fallback["value"] if fallback else 3.0)
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
    def _rate_segments(cls, conn, contract, end, cycle, fallback_rate, *, apply_legal_max):
        """Split due interest cycles at scheduled floating-rate reset boundaries."""
        last = cls._number(contract["last_payment_epoch"])
        if last is None or end <= last:
            return ([], cls._number(contract["contract_rate"], fallback_rate),
                    contract["next_repricing_epoch"], [])
        rate = cls._number(contract["contract_rate"], fallback_rate)
        rate_type = contract["rate_type"] or "fixed"
        next_reset = cls._number(contract["next_repricing_epoch"])
        reset_cycle = int(cls._number(contract["repricing_cycle_seconds"], 2592000))
        if rate_type == "floating" and (not contract["rate_nationality"] or reset_cycle <= 0):
            raise ValueError("변동금리 계약의 국적 또는 재산정주기가 올바르지 않습니다.")
        segments = []
        resets = []

        def reset_at(scheduled_epoch, current_rate):
            observation = max(
                0, scheduled_epoch-float(contract["repricing_lag_seconds"] or 0)
            )
            new_rate = RateManager.floating_rate(
                conn, contract["rate_nationality"], contract["base_rate_spread"],
                contract["rate_floor"], contract["rate_cap"], epoch=observation,
                benchmark_key=contract["benchmark_key"],
                tenor_days=contract["benchmark_tenor_days"],
                apply_legal_max=apply_legal_max,
            )
            resets.append((current_rate, new_rate, scheduled_epoch, observation))
            return new_rate

        while last < end:
            if rate_type == "floating" and next_reset is not None and next_reset <= last:
                rate = reset_at(next_reset, rate)
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
                rate = reset_at(next_reset, rate)
                next_reset += reset_cycle
            if paid_until < boundary:
                break
        return segments, rate, next_reset, resets

    @staticmethod
    def _record_rate_resets(conn, contract_type, contract_id, contract, resets,
                            applied_epoch, installments_recast=0):
        for index, (old_rate, new_rate, scheduled, observation) in enumerate(resets):
            conn.execute(
                "INSERT OR IGNORE INTO accounts.contract_rate_reset_history "
                "(contract_type,contract_id,old_rate,new_rate,scheduled_epoch,"
                "observation_epoch,applied_epoch,benchmark_key,benchmark_tenor_days,"
                "installments_recast) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (contract_type, str(contract_id), old_rate, new_rate, scheduled,
                 observation, float(applied_epoch), contract["benchmark_key"],
                 int(contract["benchmark_tenor_days"] or 0),
                 installments_recast if index == len(resets)-1 else 0),
            )

    def settle_product_rate_boundary(self, conn, product_code, now):
        self.process_all_interests(now=now, conn=conn, product_code=product_code)
        product = conn.execute('SELECT * FROM liability WHERE product_code=?', (product_code,)).fetchone()
        for loan in conn.execute("SELECT * FROM loans WHERE product_code=? AND status='active'", (product_code,)).fetchall():
            self.settle_loan_rate_boundary(conn, loan, product, now)

    @classmethod
    def settle_loan_rate_boundary(cls, conn, loan, product, now):
        loan_id = loan["loan_id"]
        current = conn.execute(
            "SELECT * FROM loans WHERE loan_id=?", (loan_id,)
        ).fetchone()
        if not current or current["status"] != "active":
            return
        loan = current
        last = cls._number(loan['last_payment_epoch'], now)
        end = min(now, float(loan['maturity_epoch'])) if loan['maturity_epoch'] is not None else now
        elapsed = max(0, end-last)
        rate = cls._number(loan['contract_rate'], float(product['interest_rate']))
        cycle = float(product['interest_cycle'])
        from functions.money import (currency_precision, from_subminor_units,
                                     to_subminor_units)
        places = currency_precision(conn, loan["quote_currency"])
        accrued = (from_subminor_units(
            loan["interest_remainder_subminor_units"], places
        ) if "interest_remainder_subminor_units" in loan.keys()
            else Decimal(str(loan['interest_remainder'] or 0)))
        if elapsed and rate:
            principal_minor = (int(loan["outstanding_balance_minor_units"] or 0)
                               if "outstanding_balance_minor_units" in loan.keys()
                               else to_minor_units(loan['outstanding_balance'], places))
            principal = from_minor_units(principal_minor, places)
            periodic_rate = (Decimal(str(rate)) / Decimal(100)
                             * Decimal(str(cycle)) / Decimal(cls.YEAR_SECONDS))
            elapsed_periods = Decimal(str(elapsed)) / Decimal(str(cycle))
            accrued += principal * ((Decimal(1) + periodic_rate) ** elapsed_periods - 1)
        whole_minor = to_minor_units(accrued, places)
        whole = ExactDecimal(from_minor_units(whole_minor, places))
        remainder = accrued-from_minor_units(whole_minor, places)
        remainder_subunits = to_subminor_units(remainder, places)
        conn.execute('UPDATE loans SET outstanding_balance_minor_units='
                     'outstanding_balance_minor_units+?,'
                     'interest_remainder=?,interest_remainder_subminor_units=?,last_payment_epoch=? '
                     'WHERE loan_id=?',
            (whole_minor, ExactDecimal(from_subminor_units(remainder_subunits, places)), remainder_subunits,
             max(last, now), loan['loan_id']))
        cls._log_company_interest(conn, loan['user_id'], 'interest_expense', whole, 0, loan['quote_currency'], product['issuer'])
        if whole_minor > 0:
            reference = f"loan:{loan['loan_id']}:{int(last)}:{int(max(last, now))}"
            RateManager.record_institution_result(
                conn, product["issuer"], whole, "loan_interest", reference,
                now=max(last, now), currency_code=loan["quote_currency"],
            )
            from managers.bank_journal import BankJournal
            BankJournal.post(
                conn, product["issuer"], "loan_interest", reference,
                loan["quote_currency"],
                [("loans_receivable", "debit", whole),
                 ("interest_income", "credit", whole)],
                description=f"Rate-boundary loan interest for {loan['loan_id']}",
                now=max(last, now),
            )

    @staticmethod
    def _log_company_interest(conn, user_id, kind, gross, tax, currency, issuer):
        """A company's deposit/loan interest is 이자수익/이자비용 in its statements."""
        from managers.company_account_manager import CompanyAccounts
        if gross and CompanyAccounts.is_company(conn, user_id):
            CompanyAccounts.log_income(conn, user_id, kind, CompanyAccounts._nwx(conn, gross, currency),
                                       CompanyAccounts._nwx(conn, tax, currency), counterparty=issuer)

    @classmethod
    def _settle_saving_account(cls, conn, account, now):
        """Settle one account without leaving partial postings on failure."""
        conn.execute('SAVEPOINT savings_interest_settlement')
        try:
            result = cls._settle_saving_account_impl(conn, account, now)
        except Exception:
            conn.execute('ROLLBACK TO savings_interest_settlement')
            conn.execute('RELEASE savings_interest_settlement')
            raise
        conn.execute('RELEASE savings_interest_settlement')
        return result

    @classmethod
    def _settle_saving_account_impl(cls, conn, account, now):
        """Pay every complete interest cycle of one savings or foreign-currency account.

        Returns (net paid, whether anything was credited).
        """
        from managers.currency_manager import SCALE
        total = 0.0
        credited = False
        product = conn.execute(
            "SELECT interest_rate, maturity_days, interest_cycle, compound_type, status,"
            "day_count_convention,issuer "
            "FROM product.saving WHERE product_code = ?",
            (account["product_code"],),
        ).fetchone()
        if not product or product["status"] != "active":
            cls._advance_account_cursor(conn, account["account_number"], now)
            return 0.0, False
        rate = cls._number(account["contract_rate"], cls._number(product["interest_rate"]))
        cycle = cls._number(product["interest_cycle"])
        # Every account keeps its exact balance in balance_units (1/1,000,000).
        balance = ExactDecimal(account["balance_units"] or 0) / SCALE
        principal = ExactDecimal(account["principal"] if account["principal"] is not None else balance)
        last = cls._number(account["last_payment_epoch"])
        if last is None:
            last = cls._utc_epoch(account["last_payment"])
        created = cls._utc_epoch(account["created_at"])
        if rate is None or cycle is None or cycle <= 0 or balance is None or last is None:
            cls._advance_account_cursor(conn, account["account_number"], now)
            return 0.0, False
        compound_setting = cls._number(product["compound_type"], 0)
        if compound_setting not in (0, 1):
            cls._advance_account_cursor(conn, account["account_number"], now)
            return 0.0, False
        maturity_days = cls._number(product["maturity_days"], 0)
        end = now
        if maturity_days and maturity_days > 0 and created is not None:
            end = min(now, created + maturity_days * 86400)
        if end <= last:
            return 0.0, False
        segments, final_rate, next_reset, resets = cls._rate_segments(
            conn, account, end, cycle, rate, apply_legal_max=False
        )
        if not segments:
            return 0.0, False
        compound = int(compound_setting) == 1
        credited = False
        for periods, segment_rate, paid_until in segments:
            gross = (cls._period_interest(
                         balance, segment_rate, cycle, periods,
                         product["day_count_convention"],
                         paid_until-periods*cycle)
                     if compound else
                     calculate_simple_interest_decimal(
                         principal, segment_rate, periods*cycle,
                         product["day_count_convention"],
                         paid_until-periods*cycle))
            if gross is None or not math.isfinite(gross):
                cls._advance_account_cursor(conn, account["account_number"], now)
                break
            gross = round_currency(
                conn, gross, account["quote_currency"]
            )
            if gross == 0:
                cls._advance_account_cursor(
                    conn, account["account_number"], paid_until
                )
                continue
            tax = (round_currency(
                conn, gross*cls._tax_rate(conn, account['user_id'], 'saving_interest')/100.0,
                account["quote_currency"]
            ) if gross > 0 else 0)
            net = max(-ExactDecimal(balance), gross-tax)
            # A negative-rate charge cannot reduce an account below zero. Book
            # the amount actually applied to the balance, not the uncapped accrual.
            accounting_gross = gross if gross >= 0 else net
            stamp = datetime.fromtimestamp(
                paid_until, timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE accounts.accounts SET balance_units=balance_units+?,last_payment=?,"
                "last_payment_epoch=? WHERE account_number=?",
                (int(round(net*SCALE)), stamp, paid_until, account["account_number"]),
            )
            RateManager.record_institution_result(
                conn, product["issuer"], -accounting_gross, "deposit_interest",
                f"account:{account['account_number']}:{paid_until}",
                now=paid_until, currency_code=account["quote_currency"],
            )
            if accounting_gross != 0:
                from managers.bank_journal import BankJournal
                if accounting_gross > 0:
                    lines = [("interest_expense", "debit", accounting_gross),
                             ("customer_deposits_payable", "credit", net),
                             ("tax_payable", "credit", tax)]
                else:
                    charge = -accounting_gross
                    lines = [("customer_deposits_payable", "debit", charge),
                             ("interest_income", "credit", charge)]
                BankJournal.post(
                    conn, product["issuer"], "deposit_interest",
                    f"account:{account['account_number']}:{paid_until}",
                    account["quote_currency"],
                    lines,
                    description=f"Deposit interest for {account['account_number']}",
                    now=paid_until,
                )
            balance += net
            tax_wallet_watermark = None
            tax_treasury_id = None
            company_kind = "interest_income" if accounting_gross >= 0 else "interest_expense"
            cls._log_company_interest(
                conn, account['user_id'], company_kind, abs(accounting_gross), tax,
                account['quote_currency'], product['issuer'],
            )
            if tax > 0:
                from managers.currency_manager import CurrencyManager, SCALE
                tax_units = int(round(ExactDecimal(tax)*SCALE))
                treasury_id = CurrencyManager.user_treasury_id(conn, account['user_id'])
                home_currency = CurrencyManager.home(conn, account['user_id'])
                tax_fx_quote = CurrencyManager.quote(conn, account['quote_currency'], home_currency)
                treasury_units = CurrencyManager.convert(
                    conn, tax_units, account["quote_currency"], home_currency
                )
                tax_treasury_id = treasury_id
                tax_wallet_watermark = int(conn.execute(
                    "SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger"
                ).fetchone()[0])
                if treasury_units:
                    CurrencyManager.change(conn, treasury_id, home_currency, treasury_units,
                                           allow_deficit=True)
                from managers.bank_journal import BankJournal
                settlement_amount = Decimal(treasury_units) / Decimal(SCALE)
                BankJournal.post_tax_remittance(
                    conn, product["issuer"],
                    f"account:{account['account_number']}:{paid_until}:tax-remittance",
                    account["quote_currency"], tax, home_currency, settlement_amount,
                    description=f"Withholding tax remittance for {account['account_number']}",
                    now=paid_until,
                )
            tax_transaction = conn.execute(
                'INSERT INTO tax_transactions(user_id,source,gross_amount,tax_amount,net_amount,currency_code) '
                'VALUES (?,?,?,?,?,?)',
                (account['user_id'], 'saving_interest', accounting_gross, tax, net,
                 account['quote_currency']),
            )
            if tax > 0:
                from managers.accounting_journal import AccountingJournal
                if treasury_units:
                    AccountingJournal.link_tax_transaction_wallet_entries(
                        conn, tax_transaction.lastrowid, tax_wallet_watermark,
                        tax_treasury_id,
                    )
                AccountingJournal.link_tax_bank_remittance(
                    conn, tax_transaction.lastrowid, product['issuer'],
                    f"account:{account['account_number']}:{paid_until}:tax-remittance",
                    account['quote_currency'], home_currency, tax_fx_quote,
                    settlement_zero=treasury_units == 0,
                )
            total += net
            credited = True
        if account["rate_type"] == "floating":
            cls._record_rate_resets(
                conn, "account", account["account_number"], account,
                resets, end,
            )
            conn.execute(
                "UPDATE accounts.accounts SET contract_rate=?,next_repricing_epoch=?,"
                "contract_effective_rate=? "
                "WHERE account_number=?",
                (final_rate, next_reset,
                 RateManager.effective_annual_rate(final_rate, cycle),
                 account["account_number"]),
            )
        return total, credited

    def process_all_interests(self, now=None, user_id=None, *, conn=None, product_code=None):
        """Accrue interest atomically, including within a caller-owned transaction."""
        if conn is None:
            return self._process_all_interests_impl(now, user_id, product_code=product_code)
        conn.execute('SAVEPOINT all_interest_settlement')
        try:
            result = self._process_all_interests_impl(now, user_id, conn=conn, product_code=product_code)
        except Exception:
            conn.execute('ROLLBACK TO all_interest_settlement')
            conn.execute('RELEASE all_interest_settlement')
            raise
        conn.execute('RELEASE all_interest_settlement')
        return result

    def _process_all_interests_impl(self, now=None, user_id=None, *, conn=None, product_code=None):
        """Accrue due savings/loan interest, optionally for one user only.

        The account/loan last-payment epoch is the persisted cursor. A call compares
        it with the current time and applies every complete product interest cycle.
        It is safe to call on a user-facing balance/loan lookup; already-settled
        cycles are not paid twice.
        """
        now = self._validated_processing_epoch(now)
        user_id = None if user_id is None else str(user_id)
        total_paid = 0
        account_count = 0
        loan_count = 0
        try:
            with (self.db.atomic(("users", "accounts", "product", "tax", "system")) if conn is None else nullcontext(conn)) as conn:
                accounts = conn.execute(
                    "SELECT account_number, user_id, product_code, balance, principal, "
                    "created_at, last_payment, last_payment_epoch,contract_rate,rate_type,"
                    "rate_nationality,base_rate_spread,rate_floor,rate_cap,repricing_cycle_seconds,"
                    "next_repricing_epoch,benchmark_key,benchmark_tenor_days,repricing_lag_seconds,"
                    "contract_effective_rate,quote_currency,account_type,balance_units "
                    "FROM accounts.accounts "
                    "WHERE resolution_status='active' AND (? IS NULL OR user_id = ?) AND (? IS NULL OR product_code=?)",
                    (user_id, user_id, product_code, product_code),
                ).fetchall()
                for account in accounts:
                    net, credited = self._settle_saving_account(conn, account, now)
                    total_paid += net
                    account_count += int(credited)

                loans = conn.execute(
                    "SELECT loan_id,user_id,product_code,principal,outstanding_balance,"
                    "outstanding_balance_minor_units,created_at,last_payment_epoch,status,"
                    "contract_rate,rate_type,rate_nationality,base_rate_spread,rate_floor,rate_cap,"
                    "repricing_cycle_seconds,next_repricing_epoch,benchmark_key,benchmark_tenor_days,"
                    "repricing_lag_seconds,maturity_epoch,overdue_since_epoch,overdue_last_epoch,"
                    "overdue_interest_remainder,collection_status,accelerated_epoch,defaulted_epoch,"
                    "collateral_amount,collateral_released,collateral_seized,origination_fee,mandatory_cost_fee,"
                    "provisioned_loss,interest_remainder "
                    ",day_count_convention,amortization_type,payment_frequency_days,quote_currency "
                    "FROM accounts.loans WHERE status IN ('active','defaulted') "
                    "AND (? IS NULL OR user_id = ?) AND (? IS NULL OR product_code=?)",
                    (user_id, user_id, product_code, product_code),
                ).fetchall()
                for loan in loans:
                    product = conn.execute(
                        "SELECT interest_rate,maturity_days,interest_cycle,status,delinquency_spread,"
                        "grace_period_days,acceleration_after_days,default_after_days,"
                        "margin_call_ltv,liquidation_ltv,liquidation_cost_percent,issuer "
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
                    if loan["status"] == "defaulted" or loan["collection_status"] == "defaulted":
                        # Defaulted/stage-3 exposures are on nonaccrual in this
                        # simulator: no new interest is recognized or journaled.
                        # Advance the cursor so recovery cannot retro-book it.
                        conn.execute(
                            "UPDATE accounts.loans SET nonaccrual_since_epoch="
                            "COALESCE(nonaccrual_since_epoch,defaulted_epoch,?),"
                            "last_payment_epoch=?,interest_remainder=0,"
                            "overdue_interest_remainder=0 WHERE loan_id=?",
                            (now, now, loan["loan_id"]),
                        )
                        continue
                    rate = self._number(loan["contract_rate"], self._number(product["interest_rate"]))
                    cycle = self._number(product["interest_cycle"])
                    places = currency_precision(conn, loan["quote_currency"])
                    balance_minor = (int(loan["outstanding_balance_minor_units"] or 0)
                                     if "outstanding_balance_minor_units" in loan.keys()
                                     else to_minor_units(loan["outstanding_balance"] or 0, places))
                    balance = ExactDecimal(from_minor_units(balance_minor, places))
                    last = self._number(loan["last_payment_epoch"])
                    created = self._utc_epoch(loan["created_at"])
                    if rate is None or cycle is None or cycle <= 0 or balance is None or last is None or rate < 0:
                        conn.execute(
                            "UPDATE accounts.loans SET last_payment_epoch = ? "
                            "WHERE loan_id = ?",
                            (now, loan["loan_id"]),
                        )
                        continue
                    balance, _ltv, _liquidated = BankManager.revalue_asset_collateral(
                        conn, loan["loan_id"], balance, product
                    )
                    balance_minor = to_minor_units(balance, places)
                    balance = ExactDecimal(from_minor_units(balance_minor, places))
                    if balance <= 0:
                        BankManager._release_collateral(
                            conn, loan["loan_id"], str(loan["user_id"])
                        )
                        continue
                    maturity_days = self._number(product["maturity_days"], 0)
                    end = now
                    if loan['maturity_epoch'] is not None:
                        end = min(now, float(loan['maturity_epoch']))
                    elif maturity_days and maturity_days > 0 and created is not None:
                        end = min(now, created + maturity_days * 86400)
                    if end <= last:
                        self._process_overdue(conn, loan, product, now)
                        continue
                    segments, final_rate, next_reset, resets = self._rate_segments(
                        conn, loan, end, cycle, rate, apply_legal_max=True
                    )
                    if not segments:
                        self._process_overdue(conn, loan, product, now)
                        continue
                    charged = False
                    for periods, segment_rate, paid_until in segments:
                        interest = self._period_interest(
                            from_minor_units(balance_minor, places),
                            segment_rate, cycle, periods,
                            loan["day_count_convention"],
                            paid_until-periods*cycle
                        )
                        if interest is None or not math.isfinite(interest):
                            conn.execute(
                                "UPDATE accounts.loans SET last_payment_epoch=? WHERE loan_id=?",
                                (now, loan["loan_id"]),
                            )
                            break
                        interest_minor = to_minor_units(interest, places)
                        whole_interest = ExactDecimal(from_minor_units(interest_minor, places))
                        if whole_interest <= 0:
                            if loan["rate_type"] == "floating":
                                conn.execute(
                                    "UPDATE accounts.loans SET last_payment_epoch=? WHERE loan_id=?",
                                    (paid_until, loan["loan_id"]),
                                )
                            continue
                        balance_minor += interest_minor
                        balance = ExactDecimal(from_minor_units(balance_minor, places))
                        conn.execute(
                            "UPDATE accounts.loans SET outstanding_balance_minor_units=?,"
                            "last_payment_epoch=? WHERE loan_id=? AND status='active'",
                            (balance_minor, paid_until, loan["loan_id"]),
                        )
                        self._log_company_interest(conn, loan["user_id"], "interest_expense", whole_interest, 0,
                                                   loan["quote_currency"], product["issuer"])
                        RateManager.record_institution_result(
                            conn, product["issuer"], whole_interest, "loan_interest",
                            f"loan:{loan['loan_id']}:{paid_until}", now=paid_until,
                            currency_code=loan["quote_currency"],
                        )
                        from managers.bank_journal import BankJournal
                        BankJournal.post(
                            conn, product["issuer"], "loan_interest",
                            f"loan:{loan['loan_id']}:{paid_until}",
                            loan["quote_currency"],
                            [("loans_receivable", "debit", whole_interest),
                             ("interest_income", "credit", whole_interest)],
                            description=f"Loan interest for {loan['loan_id']}",
                            now=paid_until,
                        )
                        charged = True
                    if loan["rate_type"] == "floating":
                        recast_count = 0
                        if (loan["amortization_type"] != "legacy"
                                and not math.isclose(float(final_rate), float(rate),
                                                     abs_tol=1e-12)):
                            recast_count = BankManager._recast_payment_schedule(
                                conn, loan, final_rate, end,
                                outstanding_balance=balance,
                            )
                        self._record_rate_resets(
                            conn, "loan", loan["loan_id"], loan, resets, end,
                            recast_count,
                        )
                        conn.execute(
                            "UPDATE accounts.loans SET contract_rate=?,next_repricing_epoch=?,"
                            "contract_effective_rate=?,contract_apr=? "
                            "WHERE loan_id=?",
                            (final_rate, next_reset,
                             RateManager.effective_annual_rate(final_rate, cycle),
                             RateManager.annual_percentage_rate(
                                 final_rate,
                                 ExactDecimal(loan['origination_fee'] or 0)
                                 + ExactDecimal(loan['mandatory_cost_fee'] or 0),
                                 max(10**(-currency_precision(conn, loan['quote_currency'])),
                                     ExactDecimal(loan['principal'] or 0)
                                     - ExactDecimal(loan['origination_fee'] or 0)
                                     - ExactDecimal(loan['mandatory_cost_fee'] or 0)),
                                 int(product['maturity_days'] or 365),
                                 cashflows=[
                                     ((float(row['due_epoch'])-float(
                                         self._utc_epoch(loan['created_at']) or row['due_epoch']
                                      ))/86400,
                                      ExactDecimal(row['principal_due'])+ExactDecimal(row['interest_due'])+ExactDecimal(row['fee_due']))
                                     for row in conn.execute(
                                         "SELECT principal_due,interest_due,fee_due,due_epoch "
                                         "FROM accounts.loan_payment_schedule WHERE loan_id=? "
                                         "ORDER BY installment_number", (loan['loan_id'],)
                                     )
                                 ] or None,
                                 principal=ExactDecimal(loan['principal'] or 0),
                             ), loan["loan_id"]),
                        )
                    loan_count += int(charged)
                    self._process_overdue(conn, loan, product, now)
                from managers.credit_risk_manager import CreditRiskManager
                CreditRiskManager.assess_conn(
                    conn, now, user_id=user_id, product_code=product_code
                )
                resolutions = BankManager.resolve_all_failed_institutions(conn, now=now)
                if any(value["accounts"] for value in resolutions.values()):
                    RateManager.refresh_all_market_pressure(conn, now=now)
            return account_count, total_paid, loan_count
        except Exception:
            raise

    @classmethod
    def _process_overdue(cls, conn, loan, product, now):
        """Accrue capped default interest and advance collection/default states."""
        maturity = cls._number(loan["maturity_epoch"])
        if maturity is None or now <= maturity:
            return
        fresh = conn.execute(
            "SELECT outstanding_balance,outstanding_balance_minor_units,status,"
            "overdue_last_epoch,overdue_interest_remainder,"
            "overdue_interest_remainder_subminor_units,"
            "collection_status,collateral_amount,collateral_amount_minor_units,"
            "collateral_released,collateral_released_minor_units,"
            "collateral_seized,collateral_seized_minor_units,"
            "provisioned_loss,provisioned_loss_minor_units,"
            "ecl_allowance,ecl_allowance_minor_units "
            "FROM accounts.loans WHERE loan_id=?", (loan["loan_id"],)
        ).fetchone()
        if not fresh:
            return
        from functions.money import from_subminor_units, to_subminor_units
        places = currency_precision(conn, loan["quote_currency"])
        balance_minor = (int(fresh["outstanding_balance_minor_units"] or 0)
                         if "outstanding_balance_minor_units" in fresh.keys()
                         else to_minor_units(fresh["outstanding_balance"] or 0, places))
        if balance_minor <= 0:
            return
        balance = from_minor_units(balance_minor, places)
        cursor = max(maturity, cls._number(fresh["overdue_last_epoch"], maturity))
        legal = RateManager.from_connection(
            conn, "legal_max_rate", loan["rate_nationality"],
            require_country=bool(loan["rate_nationality"]),
        )
        overdue_rate = min(legal, max(0.0, float(loan["contract_rate"] or 0)
                                     + float(product["delinquency_spread"] or 0)))
        fractional = (from_subminor_units(
            fresh["overdue_interest_remainder_subminor_units"], places
        ) if "overdue_interest_remainder_subminor_units" in fresh.keys()
            else Decimal(str(fresh["overdue_interest_remainder"] or 0)))
        raw_interest = calculate_simple_interest_decimal(
            from_minor_units(balance_minor, places), overdue_rate, max(0, now-cursor),
            loan["day_count_convention"], cursor
        ) + Decimal(str(fractional))
        interest_minor = to_minor_units(raw_interest, places)
        whole_interest_decimal = from_minor_units(interest_minor, places)
        whole_interest = whole_interest_decimal
        remainder = raw_interest-whole_interest_decimal
        remainder_subunits = to_subminor_units(remainder, places)
        if whole_interest:
            balance_minor += interest_minor
            balance = from_minor_units(balance_minor, places)
            cls._log_company_interest(conn, loan["user_id"], "overdue_interest", whole_interest, 0,
                                      loan["quote_currency"], product["issuer"])
            RateManager.record_institution_result(
                conn, product["issuer"], whole_interest, "overdue_interest",
                f"loan:{loan['loan_id']}:{cursor}:{now}", now=now,
                currency_code=loan["quote_currency"],
            )
            from managers.bank_journal import BankJournal
            BankJournal.post(
                conn, product["issuer"], "overdue_interest",
                f"loan:{loan['loan_id']}:{cursor}:{now}", loan["quote_currency"],
                [("loans_receivable", "debit", whole_interest),
                 ("interest_income", "credit", whole_interest)],
                description=f"Overdue interest for loan {loan['loan_id']}", now=now,
            )
        age_days = (now-maturity)/86400
        grace = int(product["grace_period_days"] or 0)
        acceleration = int(product["acceleration_after_days"] or 0)
        default = int(product["default_after_days"] or 0)
        state = "grace" if age_days <= grace else "overdue"
        accelerated_epoch = loan["accelerated_epoch"]
        defaulted_epoch = loan["defaulted_epoch"]
        status = fresh["status"]
        if acceleration > 0 and age_days >= acceleration:
            state = "accelerated"
            if accelerated_epoch is None:
                accelerated_epoch = now
                # Report the contractual trigger date even when settlement runs late.
                CreditScoreManager.record(
                    conn, loan["user_id"], "acceleration", f"loan:{loan['loan_id']}",
                    occurred_epoch=maturity+acceleration*86400, now=now,
                    loan_id=loan["loan_id"], refresh=False,
                )
        def stored_units(legacy_field, minor_field):
            value = fresh[minor_field]
            return (int(value or 0) if value is not None else
                    to_minor_units(fresh[legacy_field] or 0, places))

        released_units = stored_units("collateral_released", "collateral_released_minor_units")
        seized_units = stored_units("collateral_seized", "collateral_seized_minor_units")
        provisioned_units = stored_units("provisioned_loss", "provisioned_loss_minor_units")
        ecl_allowance_units = stored_units("ecl_allowance", "ecl_allowance_minor_units")
        if default > 0 and age_days >= default:
            state, status = "defaulted", "defaulted"
            if defaulted_epoch is None:
                defaulted_epoch = now
                CreditScoreManager.record(
                    conn, loan["user_id"], "default", f"loan:{loan['loan_id']}",
                    occurred_epoch=maturity+default*86400, now=now,
                    loan_id=loan["loan_id"], refresh=False,
                )
                collateral_units = (int(fresh["collateral_amount_minor_units"] or 0)
                                    if fresh["collateral_amount_minor_units"] is not None
                                    else to_minor_units(fresh["collateral_amount"] or 0, places))
                available_units = max(0, collateral_units-released_units-seized_units)
                take_units = min(available_units, balance_minor)
                balance_minor -= take_units
                seized_units += take_units
                collateral_recovery_units = take_units
                surplus_units = available_units-take_units
                if surplus_units:
                    from managers.bank_manager import UserFunds
                    refund = from_minor_units(surplus_units, places)
                    from managers.currency_manager import CurrencyManager
                    account_number = None
                    if CurrencyManager.home(conn, loan["user_id"]) != loan["quote_currency"]:
                        source = UserFunds.choose(conn, loan["user_id"], loan["quote_currency"])
                        account_number = source[1] if source else None
                    UserFunds.credit(
                        conn, loan["user_id"], ExactDecimal(refund), account_number,
                        currency=loan["quote_currency"],
                    )
                    released_units += surplus_units
                if balance_minor <= 0:
                    balance_minor, status, state = 0, "paid", "paid"
                else:
                    before_asset_units = balance_minor
                    balance, _ltv, _liquidated = BankManager.revalue_asset_collateral(
                        conn, loan["loan_id"], from_minor_units(balance_minor, places),
                        product, force=True
                    )
                    balance_minor = max(0, to_minor_units(balance, places))
                    collateral_recovery_units += max(0, before_asset_units-balance_minor)
                    if balance_minor <= 0:
                        balance_minor, status, state = 0, "paid", "paid"
                loss_adjustment_units = balance_minor-provisioned_units-ecl_allowance_units
                loss_adjustment = from_minor_units(loss_adjustment_units, places)
                collateral_recovery = from_minor_units(collateral_recovery_units, places)
                provisioned = from_minor_units(provisioned_units, places)
                ecl_allowance = from_minor_units(ecl_allowance_units, places)
                if loss_adjustment_units:
                    RateManager.record_institution_result(
                        conn, product["issuer"], -loss_adjustment, "credit_loss",
                        f"loan:{loan['loan_id']}:default", now=now,
                        currency_code=loan["quote_currency"],
                    )
                from managers.bank_journal import BankJournal
                default_lines = []
                if collateral_recovery > 0:
                    default_lines.extend([
                        ("collateral_recovery_asset", "debit", collateral_recovery),
                        ("loans_receivable", "credit", collateral_recovery),
                    ])
                if ecl_allowance_units > 0:
                    default_lines.append(("ecl_allowance", "debit", ecl_allowance))
                if balance_minor > provisioned_units:
                    default_lines.append(("default_loss_allowance", "credit",
                                         from_minor_units(balance_minor-provisioned_units, places)))
                elif provisioned_units > balance_minor:
                    default_lines.append(("default_loss_allowance", "debit",
                                         from_minor_units(provisioned_units-balance_minor, places)))
                if loss_adjustment_units > 0:
                    default_lines.append(("credit_loss_expense", "debit", loss_adjustment))
                elif loss_adjustment_units < 0:
                    default_lines.append(("impairment_reversal", "credit", -loss_adjustment))
                if default_lines:
                    BankJournal.post(
                        conn, product["issuer"], "credit_loss_default",
                        f"loan:{loan['loan_id']}:default", loan["quote_currency"],
                        default_lines,
                        description=f"Default and collateral recovery for loan {loan['loan_id']}",
                        now=now,
                    )
                provisioned_units = balance_minor
                ecl_allowance_units = 0
        conn.execute(
            "UPDATE accounts.loans SET outstanding_balance_minor_units=?,"
            "overdue_since_epoch=COALESCE("
            "overdue_since_epoch,?),overdue_last_epoch=?,overdue_interest_remainder=?,"
            "overdue_interest_remainder_subminor_units=?,"
            "collection_status=?,accelerated_epoch=?,defaulted_epoch=?,status=?,"
            "collateral_released_minor_units=?,collateral_seized_minor_units=?,"
            "provisioned_loss_minor_units=?,ecl_allowance_minor_units=? "
            "WHERE loan_id=?",
            (balance_minor, maturity, now,
             ExactDecimal(from_subminor_units(remainder_subunits, places)),
             remainder_subunits, state, accelerated_epoch, defaulted_epoch,
             status, released_units, seized_units, provisioned_units,
             ecl_allowance_units, loan["loan_id"]),
        )
        if state != fresh["collection_status"] or status != fresh["status"]:
            CreditScoreManager.refresh(conn, loan["user_id"], now, trigger=f"collection_{state}")

    def process_user_interests(self, user_id, now=None):
        """Settle interest for one user's accounts and loans immediately before display/use."""
        return self.process_all_interests(now=now, user_id=user_id)
