from .db_manager import DBManager
import hashlib
import os
import time
from datetime import datetime, timezone

class AccountDB(DBManager):
    def __init__(self):
        super().__init__("accounts")
        self._init_db()

    def _init_db(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                account_number TEXT PRIMARY KEY,
                user_id TEXT,
                product_code TEXT,
                balance INTEGER,
                password TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_payment DATETIME DEFAULT CURRENT_TIMESTAMP,
                quote_currency TEXT,
                balance_units INTEGER NOT NULL DEFAULT 0,
                balance_minor_units INTEGER NOT NULL DEFAULT 0,
                balance_decimal_places INTEGER NOT NULL DEFAULT 0 CHECK(balance_decimal_places BETWEEN 0 AND 6),
                interest_remainder TEXT NOT NULL DEFAULT '0'
            )
        ''')
        try:
            self.execute("ALTER TABLE accounts ADD COLUMN last_payment DATETIME DEFAULT CURRENT_TIMESTAMP")
        except Exception:
            pass

        columns = {row[1] for row in self.execute("PRAGMA table_info(accounts)")}
        if "last_payment_epoch" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN last_payment_epoch REAL")
        if "principal" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN principal INTEGER NOT NULL DEFAULT 0")
        if "account_type" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN account_type TEXT NOT NULL DEFAULT 'saving'")
        if "quote_currency" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN quote_currency TEXT")
        if "balance_units" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN balance_units INTEGER NOT NULL DEFAULT 0")
        if "balance_minor_units" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN balance_minor_units INTEGER NOT NULL DEFAULT 0")
        if "balance_decimal_places" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN balance_decimal_places INTEGER NOT NULL DEFAULT 0")
        if "interest_remainder" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN interest_remainder TEXT NOT NULL DEFAULT '0'")
        account_rate_columns = {
            "contract_rate": "REAL",
            "rate_type": "TEXT NOT NULL DEFAULT 'fixed' CHECK(rate_type IN ('fixed','floating'))",
            "rate_nationality": "TEXT",
            "base_rate_spread": "REAL NOT NULL DEFAULT 0",
            "rate_floor": "REAL NOT NULL DEFAULT 0",
            "rate_cap": "REAL",
            "repricing_cycle_seconds": "INTEGER NOT NULL DEFAULT 2592000",
            "next_repricing_epoch": "REAL",
            "benchmark_key": "TEXT NOT NULL DEFAULT 'policy'",
            "benchmark_tenor_days": "INTEGER NOT NULL DEFAULT 0",
            "repricing_lag_seconds": "INTEGER NOT NULL DEFAULT 0",
            "contract_effective_rate": "REAL",
            "early_withdrawal_rate": "REAL NOT NULL DEFAULT 0",
            "early_withdrawal_fee_percent": "REAL NOT NULL DEFAULT 0",
            "deposit_insured": "INTEGER NOT NULL DEFAULT 1",
            "resolution_status": "TEXT NOT NULL DEFAULT 'active'",
            "insurance_paid": "INTEGER NOT NULL DEFAULT 0",
            "uninsured_claim": "INTEGER NOT NULL DEFAULT 0",
            "reference_price": "REAL",  # IMA: reference product price at opening
            "reference_price_minor_units": "INTEGER",
            "fee_paid_epoch": "REAL",  # wrap: management fee charged up to here
            "managed_by": "TEXT",  # wrap: manager allowed to trade this account
        }
        for column, declaration in account_rate_columns.items():
            if column not in columns:
                self.execute(f"ALTER TABLE accounts ADD COLUMN {column} {declaration}")
        self.execute(
            "UPDATE accounts SET contract_rate=COALESCE(contract_rate, "
            "(SELECT interest_rate FROM saving p WHERE p.product_code=accounts.product_code), 0)"
        )
        now = time.time()
        for row in self.execute(
            "SELECT account_number, last_payment FROM accounts "
            "WHERE last_payment_epoch IS NULL AND last_payment IS NOT NULL"
        ):
            try:
                legacy_time = datetime.strptime(row["last_payment"], "%Y-%m-%d %H:%M:%S")
                epoch = legacy_time.replace(tzinfo=timezone.utc).timestamp()
                # Older versions wrote local-time strings. A timestamp far in the future
                # when read as UTC is a reliable signal; convert it as the old code did.
                if epoch > now + 300:
                    epoch = time.mktime(legacy_time.timetuple())
                self.execute(
                    "UPDATE accounts SET last_payment_epoch = ? WHERE account_number = ?",
                    (epoch, row["account_number"]),
                )
            except (TypeError, ValueError, OverflowError):
                continue
        # Repair rows migrated by a pre-fix build that interpreted a local timestamp as UTC.
        for row in self.execute(
            "SELECT account_number, last_payment, last_payment_epoch FROM accounts "
            "WHERE last_payment_epoch > ? AND last_payment IS NOT NULL",
            (now + 300,),
        ):
            try:
                legacy_time = datetime.strptime(row["last_payment"], "%Y-%m-%d %H:%M:%S")
                local_epoch = time.mktime(legacy_time.timetuple())
                if local_epoch <= now + 300:
                    self.execute(
                        "UPDATE accounts SET last_payment_epoch = ? WHERE account_number = ?",
                        (local_epoch, row["account_number"]),
                    )
            except (TypeError, ValueError, OverflowError):
                continue
        self.execute("UPDATE accounts SET last_payment_epoch = ? WHERE last_payment_epoch IS NULL", (time.time(),))
        self.execute("UPDATE accounts SET principal = balance WHERE principal = 0 AND balance > 0")
        self._migrate_plaintext_passwords()
        self.enforce_integer_money("accounts", ("balance", "principal"), "account_number")

        self.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_user_product_unique
            ON accounts (user_id, product_code)
        ''')
        self.execute('''
            CREATE INDEX IF NOT EXISTS idx_accounts_user_currency
            ON accounts (user_id, quote_currency, account_type, created_at)
        ''')

        self.execute('''
            CREATE TABLE IF NOT EXISTS loans (
                loan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                product_code TEXT NOT NULL,
                principal INTEGER NOT NULL,
                outstanding_balance INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_payment_epoch REAL,
                last_repayment_epoch REAL,
                status TEXT NOT NULL DEFAULT 'active'
            )
        ''')
        loan_columns = {row[1] for row in self.execute("PRAGMA table_info(loans)")}
        for legacy_column in ("statutory_interest_remainder", "last_statutory_epoch"):
            if legacy_column in loan_columns:
                self.execute(f"ALTER TABLE loans DROP COLUMN {legacy_column}")
        loan_columns = {row[1] for row in self.execute("PRAGMA table_info(loans)")}
        for column, declaration in (
            ("maturity_epoch", "REAL"),
            ("extension_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_extended_epoch", "REAL"),
            ("extension_eligible_epoch", "REAL"),
            ("interest_remainder", "REAL NOT NULL DEFAULT 0"),
            ("interest_remainder_subminor_units", "INTEGER NOT NULL DEFAULT 0"),
            ("contract_rate", "REAL"),
            ("rate_type", "TEXT NOT NULL DEFAULT 'fixed' CHECK(rate_type IN ('fixed','floating'))"),
            ("rate_nationality", "TEXT"),
            ("base_rate_spread", "REAL NOT NULL DEFAULT 0"),
            ("rate_floor", "REAL NOT NULL DEFAULT 0"),
            ("rate_cap", "REAL"),
            ("repricing_cycle_seconds", "INTEGER NOT NULL DEFAULT 2592000"),
            ("next_repricing_epoch", "REAL"),
            ("benchmark_key", "TEXT NOT NULL DEFAULT 'policy'"),
            ("benchmark_tenor_days", "INTEGER NOT NULL DEFAULT 0"),
            ("repricing_lag_seconds", "INTEGER NOT NULL DEFAULT 0"),
            ("contract_effective_rate", "REAL"),
            ("contract_apr", "REAL"),
            ("origination_fee", "INTEGER NOT NULL DEFAULT 0"),
            ("collateral_amount", "INTEGER NOT NULL DEFAULT 0"),
            ("collateral_released", "INTEGER NOT NULL DEFAULT 0"),
            ("overdue_since_epoch", "REAL"),
            ("overdue_last_epoch", "REAL"),
            ("overdue_interest_remainder", "REAL NOT NULL DEFAULT 0"),
            ("overdue_interest_remainder_subminor_units", "INTEGER NOT NULL DEFAULT 0"),
            ("collection_status", "TEXT NOT NULL DEFAULT 'current'"),
            ("accelerated_epoch", "REAL"),
            ("defaulted_epoch", "REAL"),
            ("nonaccrual_since_epoch", "REAL"),
            ("collateral_seized", "INTEGER NOT NULL DEFAULT 0"),
            ("day_count_convention", "TEXT NOT NULL DEFAULT 'ACT/365'"),
            ("amortization_type", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("payment_frequency_days", "INTEGER NOT NULL DEFAULT 30"),
            ("missed_payment_count", "INTEGER NOT NULL DEFAULT 0"),
            ("current_ltv", "REAL"),
            ("collateral_asset_value", "INTEGER NOT NULL DEFAULT 0"),
            ("collateral_liquidated_value", "INTEGER NOT NULL DEFAULT 0"),
            ("provisioned_loss", "INTEGER NOT NULL DEFAULT 0"),
            ("ecl_allowance", "INTEGER NOT NULL DEFAULT 0 CHECK(ecl_allowance >= 0)"),
            ("charged_off_balance", "INTEGER NOT NULL DEFAULT 0 CHECK(charged_off_balance >= 0)"),
            ("mandatory_cost_fee", "INTEGER NOT NULL DEFAULT 0"),
            ("underwriting_dti", "REAL"),
            ("underwriting_dsr", "REAL"),
            ("underwriting_stress_dsr", "REAL"),
            ("underwriting_stress_shock_bps", "INTEGER NOT NULL DEFAULT 200 CHECK(underwriting_stress_shock_bps BETWEEN 0 AND 1000)"),
            ("underwriting_fx_stress_shock_percent", "REAL NOT NULL DEFAULT 0 CHECK(underwriting_fx_stress_shock_percent BETWEEN 0 AND 100)"),
            ("borrower_risk_score", "REAL"),
            ("origination_pd_12m", "REAL CHECK(origination_pd_12m IS NULL OR (origination_pd_12m >= 0 AND origination_pd_12m <= 1))"),
            ("stage_cure_since_epoch", "REAL"),
            ("underwriting_income", "INTEGER NOT NULL DEFAULT 0"),
            ("prepayment_fee_percent", "REAL NOT NULL DEFAULT 0"),
            ("prepayment_fee_decline_days", "INTEGER NOT NULL DEFAULT 0"),
            ("prepayment_fees_paid", "INTEGER NOT NULL DEFAULT 0"),
            ("quote_currency", "TEXT"),
        ):
            if column not in loan_columns:
                self.execute(f"ALTER TABLE loans ADD COLUMN {column} {declaration}")
        self.execute(
            "UPDATE loans SET contract_rate=COALESCE(contract_rate, "
            "(SELECT interest_rate FROM liability p WHERE p.product_code=loans.product_code), 0)"
        )
        self.execute(
            "UPDATE loans SET quote_currency=COALESCE((SELECT quote_currency FROM liability p "
            "WHERE p.product_code=loans.product_code),quote_currency,base_currency_code())"
        )
        self.execute("""CREATE TABLE IF NOT EXISTS loan_chargeoff_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            issuer TEXT NOT NULL,
            event_key TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL CHECK(event_type IN ('charge_off','recovery')),
            amount_minor_units INTEGER NOT NULL CHECK(amount_minor_units > 0),
            decimal_places INTEGER NOT NULL,
            currency_code TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            created_epoch REAL NOT NULL,
            memo TEXT NOT NULL DEFAULT ''
        )""")
        # Fix each contract's maturity rather than changing existing loans when
        # an administrator edits the product's duration.
        self.execute("UPDATE loans SET maturity_epoch = CAST(strftime('%s', created_at) AS REAL) + "
            "86400 * (SELECT maturity_days FROM liability p WHERE p.product_code=loans.product_code) "
            "WHERE maturity_epoch IS NULL AND (SELECT maturity_days FROM liability p "
            "WHERE p.product_code=loans.product_code)>0")
        if "last_payment_epoch" not in loan_columns:
            self.execute("ALTER TABLE loans ADD COLUMN last_payment_epoch REAL")
        if "status" not in loan_columns:
            self.execute("ALTER TABLE loans ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "last_repayment_epoch" not in loan_columns:
            self.execute("ALTER TABLE loans ADD COLUMN last_repayment_epoch REAL")
            # Existing loans start their first scheduled installment from migration time.
            self.execute(
                "UPDATE loans SET last_repayment_epoch = unixepoch('now') "
                "WHERE last_repayment_epoch IS NULL"
            )
        self.execute(
            "UPDATE loans SET last_payment_epoch = CAST(strftime('%s', created_at) AS REAL) "
            "WHERE last_payment_epoch IS NULL"
        )
        self.execute(
            "UPDATE loans SET last_repayment_epoch = COALESCE(last_repayment_epoch, "
            "CAST(strftime('%s', created_at) AS REAL), unixepoch('now')) "
            "WHERE last_repayment_epoch IS NULL"
        )
        self.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_loans_user_product_unique
            ON loans (user_id, product_code)
        ''')
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_loans_due "
            "ON loans (status, last_repayment_epoch)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_loans_active_user "
            "ON loans(user_id) WHERE status = 'active'"
        )
        self.enforce_integer_money(
            "loans",
            ("principal", "outstanding_balance", "origination_fee", "mandatory_cost_fee", "collateral_amount", "collateral_seized", "underwriting_income"),
            "loan_id",
        )

        self.execute("""CREATE TABLE IF NOT EXISTS loan_payment_schedule (
            schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            installment_number INTEGER NOT NULL,
            due_epoch REAL NOT NULL,
            principal_due INTEGER NOT NULL DEFAULT 0 CHECK(principal_due >= 0),
            interest_due INTEGER NOT NULL DEFAULT 0 CHECK(interest_due >= 0),
            fee_due INTEGER NOT NULL DEFAULT 0 CHECK(fee_due >= 0),
            amount_paid INTEGER NOT NULL DEFAULT 0 CHECK(amount_paid >= 0),
            paid_epoch REAL,
            status TEXT NOT NULL DEFAULT 'scheduled'
                CHECK(status IN ('scheduled','partial','paid','missed','waived')),
            UNIQUE(loan_id, installment_number)
        )""")
        schedule_columns = {
            row[1] for row in self.execute("PRAGMA table_info(loan_payment_schedule)")
        }
        payment_allocation_migration = any(
            column not in schedule_columns
            for column in ("principal_paid", "interest_paid", "fee_paid")
        )
        for column, declaration in (
            ("rate_applied", "REAL"),
            ("day_count_convention", "TEXT NOT NULL DEFAULT 'ACT/365'"),
            ("recast_epoch", "REAL"),
            ("principal_paid", "INTEGER NOT NULL DEFAULT 0 CHECK(principal_paid >= 0)"),
            ("interest_paid", "INTEGER NOT NULL DEFAULT 0 CHECK(interest_paid >= 0)"),
            ("fee_paid", "INTEGER NOT NULL DEFAULT 0 CHECK(fee_paid >= 0)"),
            ("principal_due_minor_units", "INTEGER NOT NULL DEFAULT 0 CHECK(principal_due_minor_units >= 0)"),
            ("interest_due_minor_units", "INTEGER NOT NULL DEFAULT 0 CHECK(interest_due_minor_units >= 0)"),
            ("fee_due_minor_units", "INTEGER NOT NULL DEFAULT 0 CHECK(fee_due_minor_units >= 0)"),
            ("amount_paid_minor_units", "INTEGER NOT NULL DEFAULT 0 CHECK(amount_paid_minor_units >= 0)"),
            ("principal_paid_minor_units", "INTEGER NOT NULL DEFAULT 0 CHECK(principal_paid_minor_units >= 0)"),
            ("interest_paid_minor_units", "INTEGER NOT NULL DEFAULT 0 CHECK(interest_paid_minor_units >= 0)"),
            ("fee_paid_minor_units", "INTEGER NOT NULL DEFAULT 0 CHECK(fee_paid_minor_units >= 0)"),
        ):
            if column not in schedule_columns:
                self.execute(
                    f"ALTER TABLE loan_payment_schedule ADD COLUMN {column} {declaration}"
                )
        # Older releases kept only the aggregate amount_paid. Backfill it using
        # the same fee -> interest -> principal convention used for new payments.
        if payment_allocation_migration:
            self.execute("""UPDATE loan_payment_schedule SET
                fee_paid=MIN(fee_due,amount_paid),
                interest_paid=MIN(interest_due,MAX(0,amount_paid-fee_due)),
                principal_paid=MIN(principal_due,MAX(0,amount_paid-fee_due-interest_due))
                WHERE amount_paid>0 AND fee_paid=0 AND interest_paid=0 AND principal_paid=0""")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_loan_schedule_due "
            "ON loan_payment_schedule(status,due_epoch,loan_id)"
        )
        self.enforce_integer_money(
            "loan_payment_schedule",
            ("principal_due", "interest_due", "fee_due", "amount_paid",
             "principal_paid", "interest_paid", "fee_paid"),
            "schedule_id",
        )
        self.execute("""CREATE TABLE IF NOT EXISTS loan_ecl_assessments (
            assessment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            as_of_day TEXT NOT NULL,
            stage INTEGER NOT NULL CHECK(stage IN (1,2,3)),
            pd_12m REAL NOT NULL CHECK(pd_12m BETWEEN 0 AND 1),
            lifetime_pd REAL NOT NULL CHECK(lifetime_pd BETWEEN 0 AND 1),
            lgd REAL NOT NULL CHECK(lgd BETWEEN 0 AND 1),
            ead REAL NOT NULL CHECK(ead >= 0),
            expected_loss REAL NOT NULL CHECK(expected_loss >= 0),
            quote_currency TEXT NOT NULL,
            scenario TEXT NOT NULL DEFAULT 'base',
            discount_factor REAL NOT NULL DEFAULT 1 CHECK(discount_factor > 0),
            effective_interest_rate REAL NOT NULL DEFAULT 0,
            expected_loss_time_years REAL NOT NULL DEFAULT 0 CHECK(expected_loss_time_years >= 0),
            macro_pd_multiplier REAL NOT NULL DEFAULT 1 CHECK(macro_pd_multiplier BETWEEN 0.5 AND 2.5),
            calculated_epoch REAL NOT NULL,
            UNIQUE(loan_id,as_of_day)
        )""")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_loan_ecl_assessments_epoch_loan "
            "ON loan_ecl_assessments(calculated_epoch,loan_id)"
        )
        ecl_columns = {row[1] for row in self.execute("PRAGMA table_info(loan_ecl_assessments)")}
        if "discount_factor" not in ecl_columns:
            self.execute(
                "ALTER TABLE loan_ecl_assessments ADD COLUMN discount_factor "
                "REAL NOT NULL DEFAULT 1 CHECK(discount_factor > 0)"
            )
        if "effective_interest_rate" not in ecl_columns:
            self.execute(
                "ALTER TABLE loan_ecl_assessments ADD COLUMN effective_interest_rate "
                "REAL NOT NULL DEFAULT 0"
            )
        if "expected_loss_time_years" not in ecl_columns:
            self.execute(
                "ALTER TABLE loan_ecl_assessments ADD COLUMN expected_loss_time_years "
                "REAL NOT NULL DEFAULT 0 CHECK(expected_loss_time_years >= 0)"
            )
        if "macro_pd_multiplier" not in ecl_columns:
            self.execute(
                "ALTER TABLE loan_ecl_assessments ADD COLUMN macro_pd_multiplier "
                "REAL NOT NULL DEFAULT 1 CHECK(macro_pd_multiplier BETWEEN 0.5 AND 2.5)"
            )
        if "risk_grade" not in ecl_columns:
            # Preserve historical append-only rows; old grades remain NULL rather
            # than being backfilled by rewriting immutable assessment evidence.
            self.execute("ALTER TABLE loan_ecl_assessments ADD COLUMN risk_grade TEXT")
        self.execute("CREATE INDEX IF NOT EXISTS idx_loan_ecl_day "
                     "ON loan_ecl_assessments(as_of_day,stage)")
        self.execute("""CREATE TRIGGER IF NOT EXISTS loan_ecl_assessments_no_update
            BEFORE UPDATE ON loan_ecl_assessments BEGIN
            SELECT RAISE(ABORT,'ECL assessments are append-only'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS loan_ecl_assessments_no_delete
            BEFORE DELETE ON loan_ecl_assessments BEGIN
            SELECT RAISE(ABORT,'ECL assessments are append-only'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS contract_rate_reset_history (
            reset_id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_type TEXT NOT NULL CHECK(contract_type IN ('account','loan')),
            contract_id TEXT NOT NULL,
            old_rate REAL NOT NULL,
            new_rate REAL NOT NULL,
            scheduled_epoch REAL NOT NULL,
            observation_epoch REAL NOT NULL,
            applied_epoch REAL NOT NULL,
            benchmark_key TEXT NOT NULL,
            benchmark_tenor_days INTEGER NOT NULL DEFAULT 0,
            installments_recast INTEGER NOT NULL DEFAULT 0,
            UNIQUE(contract_type,contract_id,scheduled_epoch)
        )""")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_contract_rate_reset_history_contract "
            "ON contract_rate_reset_history(contract_type,contract_id,scheduled_epoch)"
        )
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_contract_rate_reset_history_immutable_{operation.lower()}
                BEFORE {operation} ON contract_rate_reset_history
                BEGIN SELECT RAISE(ABORT,'contract rate reset history is append-only'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS loan_collateral_assets (
            collateral_id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            asset_code TEXT NOT NULL,
            source_account TEXT,
            quantity REAL NOT NULL CHECK(quantity > 0),
            average_cost INTEGER NOT NULL DEFAULT 0 CHECK(average_cost >= 0),
            pledged_epoch REAL NOT NULL,
            haircut REAL NOT NULL DEFAULT 0.20 CHECK(haircut BETWEEN 0 AND 1),
            seniority_rank INTEGER NOT NULL DEFAULT 1 CHECK(seniority_rank BETWEEN 1 AND 100),
            recovery_days INTEGER NOT NULL DEFAULT 0 CHECK(recovery_days BETWEEN 0 AND 3650),
            recovery_cost_bps INTEGER NOT NULL DEFAULT 0 CHECK(recovery_cost_bps BETWEEN 0 AND 10000),
            last_price INTEGER NOT NULL CHECK(last_price > 0),
            last_value INTEGER NOT NULL CHECK(last_value >= 0),
            status TEXT NOT NULL DEFAULT 'pledged'
                CHECK(status IN ('pledged','released','liquidated'))
        )""")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_loan_collateral_assets "
            "ON loan_collateral_assets(loan_id,status)"
        )
        self.enforce_integer_money(
            "loan_collateral_assets",
            ("average_cost", "last_price", "last_value"),
            "collateral_id",
        )
        collateral_columns = {
            row[1] for row in self.execute("PRAGMA table_info(loan_collateral_assets)")
        }
        for column, declaration in (
            ("seniority_rank", "INTEGER NOT NULL DEFAULT 1 CHECK(seniority_rank BETWEEN 1 AND 100)"),
            ("recovery_days", "INTEGER NOT NULL DEFAULT 0 CHECK(recovery_days BETWEEN 0 AND 3650)"),
            ("recovery_cost_bps", "INTEGER NOT NULL DEFAULT 0 CHECK(recovery_cost_bps BETWEEN 0 AND 10000)"),
        ):
            if column not in collateral_columns:
                self.execute(f"ALTER TABLE loan_collateral_assets ADD COLUMN {column} {declaration}")
        for column, declaration in (
            ("liquidation_price", "INTEGER NOT NULL DEFAULT 0"),
            ("liquidation_cost", "INTEGER NOT NULL DEFAULT 0"),
            ("liquidation_proceeds", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in collateral_columns:
                self.execute(
                    f"ALTER TABLE loan_collateral_assets ADD COLUMN {column} {declaration}"
                )
        self.enforce_integer_money(
            "loan_collateral_assets",
            ("average_cost", "last_price", "last_value", "liquidation_price",
             "liquidation_cost", "liquidation_proceeds"),
            "collateral_id",
        )
        self.execute("""CREATE TABLE IF NOT EXISTS collateral_liquidation_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            collateral_id INTEGER NOT NULL,
            asset_code TEXT NOT NULL,
            quantity REAL NOT NULL CHECK(quantity > 0),
            mark_price INTEGER NOT NULL CHECK(mark_price > 0),
            execution_price REAL NOT NULL CHECK(execution_price >= 0),
            gross_proceeds INTEGER NOT NULL DEFAULT 0 CHECK(gross_proceeds >= 0),
            liquidation_cost INTEGER NOT NULL DEFAULT 0 CHECK(liquidation_cost >= 0),
            net_proceeds INTEGER NOT NULL DEFAULT 0 CHECK(net_proceeds >= 0),
            book_filled_quantity REAL NOT NULL DEFAULT 0,
            backstop_quantity REAL NOT NULL DEFAULT 0,
            liquidated_epoch REAL NOT NULL,
            UNIQUE(collateral_id)
        )""")
        self.enforce_integer_money(
            "collateral_liquidation_events",
            ("mark_price", "gross_proceeds", "liquidation_cost", "net_proceeds"),
            "event_id",
        )
        self.execute("DROP TRIGGER IF EXISTS trg_collateral_liquidation_events_immutable_update")
        self.execute("DROP TRIGGER IF EXISTS trg_collateral_liquidation_events_immutable_delete")
        self.execute("""CREATE TABLE IF NOT EXISTS loan_credit_support (
            support_id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER NOT NULL,
            party_user_id TEXT NOT NULL,
            support_type TEXT NOT NULL CHECK(support_type IN ('co_borrower','guarantor','connected_party')),
            exposure_share_bps INTEGER NOT NULL DEFAULT 0 CHECK(exposure_share_bps BETWEEN 0 AND 10000),
            underwriting_dti REAL CHECK(underwriting_dti IS NULL OR underwriting_dti BETWEEN 0 AND 200),
            underwriting_dsr REAL CHECK(underwriting_dsr IS NULL OR underwriting_dsr BETWEEN 0 AND 200),
            guarantee_limit_minor_units INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(guarantee_limit_minor_units)='integer' AND guarantee_limit_minor_units>=0),
            quote_currency TEXT,
            guarantee_recovery_bps INTEGER NOT NULL DEFAULT 10000 CHECK(guarantee_recovery_bps BETWEEN 0 AND 10000),
            recovery_days INTEGER NOT NULL DEFAULT 180 CHECK(recovery_days BETWEEN 0 AND 3650),
            recovery_cost_bps INTEGER NOT NULL DEFAULT 0 CHECK(recovery_cost_bps BETWEEN 0 AND 10000),
            seniority_rank INTEGER NOT NULL DEFAULT 1 CHECK(seniority_rank BETWEEN 1 AND 100),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_epoch INTEGER NOT NULL,
            updated_epoch INTEGER NOT NULL,
            UNIQUE(loan_id,party_user_id,support_type)
        )""")
        credit_support_columns = {row[1] for row in self.execute("PRAGMA table_info(loan_credit_support)")}
        for column in ("underwriting_dti", "underwriting_dsr"):
            if column not in credit_support_columns:
                self.execute(f"ALTER TABLE loan_credit_support ADD COLUMN {column} REAL")
        self.execute("""CREATE TABLE IF NOT EXISTS loan_credit_support_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            support_id INTEGER NOT NULL,
            loan_id INTEGER NOT NULL,
            party_user_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('created','updated','released')),
            snapshot TEXT NOT NULL,
            recorded_epoch INTEGER NOT NULL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_loan_credit_support_loan ON loan_credit_support(loan_id,active,support_type)")
        for table in ("loan_credit_support_events",):
            for operation in ("UPDATE", "DELETE"):
                self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_{operation.lower()}_immutable
                    BEFORE {operation} ON {table}
                    BEGIN SELECT RAISE(ABORT,'loan credit support events are append-only'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS credit_risk_policy_overrides (
            nationality TEXT NOT NULL,
            product_code TEXT NOT NULL,
            pd_multiplier REAL NOT NULL DEFAULT 1 CHECK(pd_multiplier BETWEEN 0.1 AND 10),
            grade_a_max REAL NOT NULL DEFAULT 0.005,
            grade_b_max REAL NOT NULL DEFAULT 0.01,
            grade_c_max REAL NOT NULL DEFAULT 0.025,
            grade_d_max REAL NOT NULL DEFAULT 0.05,
            grade_e_max REAL NOT NULL DEFAULT 0.10,
            grade_f_max REAL NOT NULL DEFAULT 0.20,
            updated_epoch INTEGER NOT NULL,
            updated_by TEXT NOT NULL,
            PRIMARY KEY(nationality,product_code),
            CHECK(grade_a_max>0 AND grade_a_max<grade_b_max AND grade_b_max<grade_c_max
              AND grade_c_max<grade_d_max AND grade_d_max<grade_e_max
              AND grade_e_max<grade_f_max AND grade_f_max<1)
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS credit_risk_policy_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            nationality TEXT NOT NULL, product_code TEXT NOT NULL,
            actor_id TEXT NOT NULL, snapshot TEXT NOT NULL, recorded_epoch INTEGER NOT NULL
        )""")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_credit_risk_policy_events_{operation.lower()}_immutable
                BEFORE {operation} ON credit_risk_policy_events
                BEGIN SELECT RAISE(ABORT,'credit risk policy events are append-only'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS loan_future_commitments (
            loan_id INTEGER PRIMARY KEY,
            committed_limit_minor_units INTEGER NOT NULL CHECK(typeof(committed_limit_minor_units)='integer' AND committed_limit_minor_units>=0),
            credit_conversion_factor_bps INTEGER NOT NULL CHECK(credit_conversion_factor_bps BETWEEN 0 AND 10000),
            expires_epoch INTEGER,
            active INTEGER NOT NULL CHECK(active IN (0,1)),
            updated_epoch INTEGER NOT NULL,
            updated_by TEXT NOT NULL
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS loan_future_commitment_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, loan_id INTEGER NOT NULL,
            actor_id TEXT NOT NULL, snapshot TEXT NOT NULL, recorded_epoch INTEGER NOT NULL
        )""")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_loan_future_commitment_events_{operation.lower()}_immutable
                BEFORE {operation} ON loan_future_commitment_events
                BEGIN SELECT RAISE(ABORT,'loan future commitment events are append-only'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS loan_collateral_recovery_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, collateral_id INTEGER NOT NULL,
            loan_id INTEGER NOT NULL, actor_id TEXT NOT NULL,
            prior_snapshot TEXT NOT NULL, new_snapshot TEXT NOT NULL,
            recorded_epoch INTEGER NOT NULL
        )""")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_loan_collateral_recovery_events_{operation.lower()}_immutable
                BEFORE {operation} ON loan_collateral_recovery_events
                BEGIN SELECT RAISE(ABORT,'loan collateral recovery events are append-only'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS deposit_insurance_claims (
            claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
            issuer TEXT NOT NULL,
            user_id TEXT NOT NULL,
            account_number TEXT NOT NULL,
            covered_amount INTEGER NOT NULL DEFAULT 0 CHECK(covered_amount >= 0),
            uninsured_amount INTEGER NOT NULL DEFAULT 0 CHECK(uninsured_amount >= 0),
            payout_currency TEXT,
            payout_amount REAL CHECK(payout_amount IS NULL OR payout_amount >= 0),
            payout_amount_minor_units INTEGER CHECK(payout_amount_minor_units IS NULL OR payout_amount_minor_units >= 0),
            payout_decimal_places INTEGER CHECK(payout_decimal_places IS NULL OR payout_decimal_places BETWEEN 0 AND 6),
            resolved_epoch REAL NOT NULL,
            UNIQUE(issuer,account_number)
        )""")
        claim_columns = {row[1] for row in self.execute(
            "PRAGMA table_info(deposit_insurance_claims)"
        )}
        for column, declaration in (
            ("payout_currency", "TEXT"),
            ("payout_amount", "REAL CHECK(payout_amount IS NULL OR payout_amount >= 0)"),
            ("payout_amount_minor_units", "INTEGER CHECK(payout_amount_minor_units IS NULL OR payout_amount_minor_units >= 0)"),
            ("payout_decimal_places", "INTEGER CHECK(payout_decimal_places IS NULL OR payout_decimal_places BETWEEN 0 AND 6)"),
        ):
            if column not in claim_columns:
                self.execute(
                    f"ALTER TABLE deposit_insurance_claims ADD COLUMN {column} {declaration}"
                )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_deposit_insurance_claims_user "
            "ON deposit_insurance_claims(user_id,issuer,resolved_epoch)"
        )
        self.enforce_integer_money(
            "deposit_insurance_claims",
            ("covered_amount", "uninsured_amount"),
            "claim_id",
        )
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS
                trg_deposit_insurance_claims_immutable_{operation.lower()}
                BEFORE {operation} ON deposit_insurance_claims
                BEGIN SELECT RAISE(ABORT,'deposit insurance claims are append-only'); END""")

        self.execute(
            """CREATE TABLE IF NOT EXISTS treasury_bond_holdings (
                holding_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                product_code TEXT NOT NULL,
                product_name TEXT NOT NULL,
                annual_rate REAL NOT NULL,
                maturity_days INTEGER NOT NULL,
                principal INTEGER NOT NULL CHECK(principal > 0),
                face_value INTEGER NOT NULL DEFAULT 1 CHECK(face_value > 0),
                acquisition_cost INTEGER NOT NULL DEFAULT 0 CHECK(acquisition_cost >= 0),
                purchased_epoch INTEGER NOT NULL,
                maturity_epoch INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                redeemed_epoch INTEGER
            )"""
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_bond_holdings_due "
            "ON treasury_bond_holdings(status, maturity_epoch, user_id)"
        )
        bond_columns = {
            row[1] for row in self.execute("PRAGMA table_info(treasury_bond_holdings)")
        }
        for column, declaration in (
            ("product_name", "TEXT NOT NULL DEFAULT ''"),
            ("annual_rate", "REAL NOT NULL DEFAULT 0"),
            ("maturity_days", "INTEGER NOT NULL DEFAULT 1"),
            ("face_value", "INTEGER NOT NULL DEFAULT 1 CHECK(face_value > 0)"),
            ("acquisition_cost", "INTEGER NOT NULL DEFAULT 0 CHECK(acquisition_cost >= 0)"),
            ("coupon_frequency_days", "INTEGER NOT NULL DEFAULT 0 CHECK(coupon_frequency_days >= 0)"),
            ("day_count_convention", "TEXT NOT NULL DEFAULT 'ACT/365'"),
            ("last_coupon_epoch", "INTEGER"),
        ):
            if column not in bond_columns:
                self.execute(
                    f"ALTER TABLE treasury_bond_holdings ADD COLUMN {column} {declaration}"
                )
        self.enforce_integer_money(
            "treasury_bond_holdings",
            ("principal", "face_value", "acquisition_cost"),
            "holding_id",
        )
        # Interest-rate swaps against the citizen's nationality treasury.
        self.execute("""CREATE TABLE IF NOT EXISTS swap_contracts (
            swap_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            nationality TEXT NOT NULL,
            currency TEXT NOT NULL,
            notional INTEGER NOT NULL CHECK(notional > 0),
            side TEXT NOT NULL CHECK(side IN ('pay_fixed','receive_fixed')),
            fixed_rate REAL NOT NULL,
            tenor_days INTEGER NOT NULL CHECK(tenor_days > 0),
            start_epoch REAL NOT NULL,
            maturity_epoch REAL NOT NULL,
            last_settle_epoch REAL NOT NULL,
            margin INTEGER NOT NULL DEFAULT 0 CHECK(margin >= 0),
            realized INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed','matured','defaulted')),
            closed_epoch REAL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_swaps_open ON swap_contracts(status, user_id)")

    def _migrate_plaintext_passwords(self):
        """기존 평문 비밀번호를 저장 즉시 PBKDF2-SHA256 형식으로 변환합니다."""
        rows = self.execute(
            "SELECT account_number, password FROM accounts "
            "WHERE password IS NOT NULL AND password NOT LIKE 'pbkdf2_sha256$%'"
        )
        for row in rows:
            salt = os.urandom(16)
            iterations = 240_000
            digest = hashlib.pbkdf2_hmac(
                "sha256", str(row["password"]).encode("utf-8"), salt, iterations
            )
            encoded = f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"
            self.execute(
                "UPDATE accounts SET password = ? WHERE account_number = ?",
                (encoded, row["account_number"]),
            )

    def create_account(self, account_number, user_id, product_code, balance, password):
        now = time.time()
        self.execute(
            "INSERT INTO accounts "
            "(account_number, user_id, product_code, balance, password, principal, last_payment_epoch, "
            "account_type, contract_rate, rate_type, rate_nationality, base_rate_spread, rate_floor, "
            "rate_cap, repricing_cycle_seconds, next_repricing_epoch,benchmark_key,"
            "benchmark_tenor_days,repricing_lag_seconds) "
            "SELECT ?, ?, ?, ?, ?, ?, ?, 'saving', interest_rate, rate_type, rate_nationality, "
            "base_rate_spread, rate_floor, rate_cap, repricing_cycle_seconds, "
            "CASE WHEN rate_type='floating' THEN ?+repricing_cycle_seconds END,benchmark_key,"
            "CASE WHEN benchmark_tenor_days>0 THEN benchmark_tenor_days ELSE maturity_days END,"
            "repricing_lag_seconds "
            "FROM saving WHERE product_code=?",
            (account_number, user_id, product_code, balance, password, balance, now, now, product_code),
        )

    def get_account(self, account_number):
        return self.execute_one("SELECT * FROM accounts WHERE account_number = ?", (account_number,))

    def get_user_accounts(self, user_id):
        return self.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,))

    def has_account_for_product(self, user_id, product_code):
        """사용자가 해당 저축상품에 이미 가입했는지 확인합니다."""
        return self.execute_one(
            "SELECT 1 FROM accounts WHERE user_id = ? AND product_code = ?",
            (user_id, product_code),
        ) is not None

    def has_loan_for_product(self, user_id, product_code):
        """사용자가 해당 대출상품을 이미 이용 중인지 확인합니다."""
        return self.execute_one(
            "SELECT 1 FROM loans WHERE user_id = ? AND product_code = ?",
            (user_id, product_code),
        ) is not None

    def create_loan(self, user_id, product_code, amount):
        now = time.time()
        self.execute(
            '''
            INSERT INTO loans
            (user_id, product_code, principal, outstanding_balance, last_payment_epoch, last_repayment_epoch,
             maturity_epoch, contract_rate, rate_type, rate_nationality, base_rate_spread, rate_floor,
             rate_cap, repricing_cycle_seconds, next_repricing_epoch,benchmark_key,
             benchmark_tenor_days,repricing_lag_seconds)
            SELECT ?, ?, ?, ?, ?, ?, CASE WHEN maturity_days>0 THEN ?+86400*maturity_days END,
                   interest_rate, rate_type, rate_nationality, base_rate_spread, rate_floor,
                   rate_cap, repricing_cycle_seconds,
                   CASE WHEN rate_type='floating' THEN ?+repricing_cycle_seconds END,benchmark_key,
                   CASE WHEN benchmark_tenor_days>0 THEN benchmark_tenor_days ELSE maturity_days END,
                   repricing_lag_seconds
            FROM liability WHERE product_code=?
            ''',
            (user_id, product_code, amount, amount, now, now, now, now, product_code),
        )

    def delete_account(self, account_number):
        self.execute("DELETE FROM accounts WHERE account_number = ?", (account_number,))

    def check_exists(self, account_number):
        return self.execute_one("SELECT 1 FROM accounts WHERE account_number = ?", (account_number,)) is not None

    def update_balance(self, account_number, amount):
        self.execute("UPDATE accounts SET balance_units = balance_units + CAST(ROUND(? * 1000000) AS INTEGER) WHERE account_number = ?", (amount, account_number))

    def update_last_payment(self, account_number, timestamp):
        self.execute("UPDATE accounts SET last_payment = ? WHERE account_number = ?", (timestamp, account_number))

    def apply_interest_updates(self, updates):
        """이자, UTC 지급시각, epoch를 한 트랜잭션으로 저장합니다.

        updates: (interest_amount, last_payment_timestamp, last_payment_epoch, account_number) 목록
        """
        if not updates:
            return
        with self.lock:
            with self.conn:
                self.conn.executemany(
                    "UPDATE accounts SET balance_units = balance_units + CAST(ROUND(? * 1000000) AS INTEGER), last_payment = ?, "
                    "last_payment_epoch = ? WHERE account_number = ?",
                    updates,
                )

    def apply_loan_interest_updates(self, updates):
        """updates: (interest, last_payment_epoch, loan_id) 목록"""
        if not updates:
            return
        with self.lock:
            with self.conn:
                self.conn.executemany(
                    "UPDATE loans SET outstanding_balance = outstanding_balance + ?, "
                    "last_payment_epoch = ? WHERE loan_id = ? AND status = 'active'",
                    updates,
                )
