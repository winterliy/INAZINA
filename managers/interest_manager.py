from datetime import datetime, timezone
from contextlib import nullcontext
import math
import time
from database import DatabaseManager
from database.user_db import TREASURY_USER_ID
from functions.interest_calc import calculate_compound_interest, calculate_simple_interest
from functions.tax_calc import calculate_income_tax
from functions.money import round_money, round_currency, currency_precision
from managers.bank_manager import BankManager
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
    def _period_interest(principal, annual_rate_percent, cycle_seconds, periods,
                         convention="ACT/365", start_epoch=None):
        if principal <= 0 or periods <= 0:
            return 0.0
        try:
            return calculate_compound_interest(
                principal, annual_rate_percent, periods, cycle_seconds,
                convention, start_epoch
            )
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
        last = cls._number(loan['last_payment_epoch'], now)
        end = min(now, float(loan['maturity_epoch'])) if loan['maturity_epoch'] is not None else now
        elapsed = max(0, end-last)
        rate = cls._number(loan['contract_rate'], float(product['interest_rate']))
        cycle = float(product['interest_cycle'])
        accrued = float(loan['interest_remainder'] or 0)
        if elapsed and rate:
            accrued += float(loan['outstanding_balance'])*math.expm1(elapsed/cycle*math.log1p(rate/100*cycle/cls.YEAR_SECONDS))
        whole = round_currency(conn, accrued, loan['quote_currency'])
        conn.execute('UPDATE loans SET outstanding_balance=outstanding_balance+?,interest_remainder=?,last_payment_epoch=? WHERE loan_id=?',
            (whole, accrued-whole, max(last, now), loan['loan_id']))
        cls._log_company_interest(conn, loan['user_id'], 'interest_expense', whole, 0, loan['quote_currency'], product['issuer'])

    @staticmethod
    def _log_company_interest(conn, user_id, kind, gross, tax, currency, issuer):
        """A company's deposit/loan interest is 이자수익/이자비용 in its statements."""
        from managers.company_account_manager import CompanyAccounts
        if gross and CompanyAccounts.is_company(conn, user_id):
            CompanyAccounts.log_income(conn, user_id, kind, CompanyAccounts._nwx(conn, gross, currency),
                                       CompanyAccounts._nwx(conn, tax, currency), counterparty=issuer)

    FX_INTEREST_MIGRATION = "fx_interest_unified_v1"

    @classmethod
    def migrate_currency_accounts(cls, conn, now):
        """One-time switch of foreign-currency accounts to savings interest rules.

        Interest accrued so far is settled with the old continuous method, then
        empty product settings get defaults (daily cycle, simple, no maturity).
        """
        done = conn.execute("SELECT 1 FROM settings WHERE key=?", (cls.FX_INTEREST_MIGRATION,)).fetchone()
        if done:
            return False
        from managers.currency_exchange_manager import CurrencyExchangeManager
        rows = conn.execute("SELECT a.*,COALESCE(a.contract_rate,p.interest_rate) AS annual_rate "
                            "FROM accounts a JOIN saving p USING(product_code) "
                            "LEFT JOIN financial_institutions f ON f.issuer=p.issuer "
                            "WHERE a.account_type='currency_saving' AND a.resolution_status='active' "
                            "AND COALESCE(f.status,'active')<>'failed'").fetchall()
        CurrencyExchangeManager._legacy_continuous_interest(conn, rows, now)
        conn.execute("UPDATE accounts SET interest_remainder='0' WHERE account_type='currency_saving'")
        conn.execute("UPDATE saving SET interest_cycle=86400 WHERE product_kind='currency_saving' "
                     "AND (interest_cycle IS NULL OR interest_cycle<=0)")
        conn.execute("UPDATE saving SET compound_type=0 WHERE product_kind='currency_saving' "
                     "AND (compound_type IS NULL OR compound_type NOT IN (0,1))")
        conn.execute("UPDATE saving SET maturity_days=0 WHERE product_kind='currency_saving' AND maturity_days IS NULL")
        conn.execute("INSERT INTO settings(key,value) VALUES (?,?)", (cls.FX_INTEREST_MIGRATION, str(int(now))))
        return True

    @classmethod
    def _settle_saving_account(cls, conn, account, now):
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
        fx = account["account_type"] == "currency_saving"
        balance = (account["balance_units"] or 0) / SCALE
        principal = balance if fx else cls._number(account["principal"], balance)
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
                     calculate_simple_interest(
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
                conn, gross*cls._tax_rate(conn, account['user_id'])/100.0,
                account["quote_currency"]
            ) if gross > 0 else 0)
            net = max(-float(balance), gross-tax)
            stamp = datetime.fromtimestamp(
                paid_until, timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE accounts.accounts SET balance_units=balance_units+?,last_payment=?,"
                "last_payment_epoch=? WHERE account_number=?",
                (int(round(net*SCALE)), stamp, paid_until, account["account_number"]),
            )
            RateManager.record_institution_result(
                conn, product["issuer"], -gross, "deposit_interest",
                f"account:{account['account_number']}:{paid_until}",
                now=paid_until, currency_code=account["quote_currency"],
            )
            balance += net
            conn.execute('INSERT INTO tax_transactions(user_id,source,gross_amount,tax_amount,net_amount) VALUES (?,?,?,?,?)',
                (account['user_id'], 'saving_interest', gross, tax, net))
            cls._log_company_interest(conn, account['user_id'], 'interest_income', gross, tax, account['quote_currency'], product['issuer'])
            if tax > 0:
                from managers.currency_manager import CurrencyManager, SCALE
                tax_units = int(round(float(tax)*SCALE))
                treasury_id = CurrencyManager.user_treasury_id(conn, account['user_id'])
                home_currency = CurrencyManager.home(conn, account['user_id'])
                treasury_units = CurrencyManager.convert(
                    conn, tax_units, account["quote_currency"], home_currency
                )
                treasury = conn.execute(
                    "UPDATE users.users SET cash=cash+? WHERE user_id=?",
                    (treasury_units//SCALE, treasury_id),
                )
                if treasury.rowcount != 1:
                    raise RuntimeError("국고 계정을 찾을 수 없어 이자 지급을 취소했습니다.")
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
            with (self.db.atomic(("users", "accounts", "product", "tax", "system")) if conn is None else nullcontext(conn)) as conn:
                self.migrate_currency_accounts(conn, now)
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
                    "SELECT loan_id,user_id,product_code,principal,outstanding_balance,created_at,last_payment_epoch,"
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
                    balance, _ltv, _liquidated = BankManager.revalue_asset_collateral(
                        conn, loan["loan_id"], balance, product
                    )
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
                            balance, segment_rate, cycle, periods,
                            loan["day_count_convention"],
                            paid_until-periods*cycle
                        )
                        if interest is None or not math.isfinite(interest):
                            conn.execute(
                                "UPDATE accounts.loans SET last_payment_epoch=? WHERE loan_id=?",
                                (now, loan["loan_id"]),
                            )
                            break
                        whole_interest = round_currency(
                            conn, interest, loan["quote_currency"]
                        )
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
                        self._log_company_interest(conn, loan["user_id"], "interest_expense", whole_interest, 0,
                                                   loan["quote_currency"], product["issuer"])
                        RateManager.record_institution_result(
                            conn, product["issuer"], whole_interest, "loan_interest",
                            f"loan:{loan['loan_id']}:{paid_until}", now=paid_until,
                            currency_code=loan["quote_currency"],
                        )
                        balance += whole_interest
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
                                 float(loan['origination_fee'] or 0)
                                 + float(loan['mandatory_cost_fee'] or 0),
                                 max(10**(-currency_precision(conn, loan['quote_currency'])),
                                     float(loan['principal'] or 0)
                                     - float(loan['origination_fee'] or 0)
                                     - float(loan['mandatory_cost_fee'] or 0)),
                                 int(product['maturity_days'] or 365),
                                 cashflows=[
                                     ((float(row['due_epoch'])-float(
                                         self._utc_epoch(loan['created_at']) or row['due_epoch']
                                      ))/86400,
                                      float(row['principal_due'])+float(row['interest_due'])+float(row['fee_due']))
                                     for row in conn.execute(
                                         "SELECT principal_due,interest_due,fee_due,due_epoch "
                                         "FROM accounts.loan_payment_schedule WHERE loan_id=? "
                                         "ORDER BY installment_number", (loan['loan_id'],)
                                     )
                                 ] or None,
                                 principal=float(loan['principal'] or 0),
                             ), loan["loan_id"]),
                        )
                    loan_count += int(charged)
                    self._process_overdue(conn, loan, product, now)
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
            "SELECT outstanding_balance,status,overdue_last_epoch,overdue_interest_remainder,"
            "collection_status,collateral_amount,collateral_released,collateral_seized,"
            "provisioned_loss "
            "FROM accounts.loans WHERE loan_id=?", (loan["loan_id"],)
        ).fetchone()
        if not fresh or float(fresh["outstanding_balance"] or 0) <= 0:
            return
        balance = float(fresh["outstanding_balance"])
        cursor = max(maturity, cls._number(fresh["overdue_last_epoch"], maturity))
        legal = RateManager.from_connection(
            conn, "legal_max_rate", loan["rate_nationality"],
            require_country=bool(loan["rate_nationality"]),
        )
        overdue_rate = min(legal, max(0.0, float(loan["contract_rate"] or 0)
                                     + float(product["delinquency_spread"] or 0)))
        fractional = float(fresh["overdue_interest_remainder"] or 0)
        raw_interest = calculate_simple_interest(
            balance, overdue_rate, max(0, now-cursor),
            loan["day_count_convention"], cursor
        )+fractional
        whole_interest = round_currency(
            conn, raw_interest, loan["quote_currency"]
        )
        remainder = 0
        if whole_interest:
            balance += whole_interest
            cls._log_company_interest(conn, loan["user_id"], "overdue_interest", whole_interest, 0,
                                      loan["quote_currency"], product["issuer"])
            RateManager.record_institution_result(
                conn, product["issuer"], whole_interest, "overdue_interest",
                f"loan:{loan['loan_id']}:{cursor}:{now}", now=now,
                currency_code=loan["quote_currency"],
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
                conn.execute(
                    "UPDATE users.users SET credit_score=MAX(0,credit_score-25) WHERE user_id=?",
                    (loan["user_id"],),
                )
        released = float(fresh["collateral_released"] or 0)
        seized = float(fresh["collateral_seized"] or 0)
        provisioned = float(fresh["provisioned_loss"] or 0)
        if default > 0 and age_days >= default:
            state, status = "defaulted", "defaulted"
            if defaulted_epoch is None:
                defaulted_epoch = now
                conn.execute(
                    "UPDATE users.users SET credit_score=MAX(0,credit_score-100) WHERE user_id=?",
                    (loan["user_id"],),
                )
                available = max(0.0, float(fresh["collateral_amount"] or 0)-released-seized)
                take = min(available, balance)
                balance -= take
                seized += take
                surplus = available-take
                if surplus:
                    conn.execute(
                        "UPDATE users.users SET cash=cash+? WHERE user_id=?",
                        (surplus, loan["user_id"]),
                    )
                    released += surplus
                if balance <= 0:
                    balance, status, state = 0, "paid", "paid"
                else:
                    balance, _ltv, _liquidated = BankManager.revalue_asset_collateral(
                        conn, loan["loan_id"], balance, product, force=True
                    )
                    if balance <= 0:
                        balance, status, state = 0, "paid", "paid"
                if balance > provisioned:
                    new_provision = balance-provisioned
                    RateManager.record_institution_result(
                        conn, product["issuer"], -new_provision, "credit_loss",
                        f"loan:{loan['loan_id']}:default", now=now,
                        currency_code=loan["quote_currency"],
                    )
                    provisioned += new_provision
        conn.execute(
            "UPDATE accounts.loans SET outstanding_balance=?,overdue_since_epoch=COALESCE("
            "overdue_since_epoch,?),overdue_last_epoch=?,overdue_interest_remainder=?,"
            "collection_status=?,accelerated_epoch=?,defaulted_epoch=?,status=?,"
            "collateral_released=?,collateral_seized=?,provisioned_loss=? WHERE loan_id=?",
            (balance, maturity, now, remainder, state, accelerated_epoch, defaulted_epoch,
             status, released, seized, provisioned, loan["loan_id"]),
        )

    def process_user_interests(self, user_id, now=None):
        """Settle interest for one user's accounts and loans immediately before display/use."""
        return self.process_all_interests(now=now, user_id=user_id)
