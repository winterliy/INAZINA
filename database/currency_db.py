import time
import uuid

from .db_manager import DBManager

SCALE = 1_000_000

class CurrencyDB(DBManager):
    def __init__(self):
        super().__init__("forex")
        self.execute("""CREATE TABLE IF NOT EXISTS currencies (
            code TEXT PRIMARY KEY, name TEXT NOT NULL, asset_code TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'active')""")
        currency_columns = {row[1] for row in self.execute("PRAGMA table_info(currencies)")}
        for column, definition in (
            ('reference_rate', 'TEXT'),
            ('rate_multiplier', "TEXT NOT NULL DEFAULT '1'"),
            ('decimal_places', 'INTEGER NOT NULL DEFAULT 6 CHECK(decimal_places BETWEEN 0 AND 6)'),
        ):
            if column not in currency_columns:
                self.execute(f"ALTER TABLE currencies ADD COLUMN {column} {definition}")
        self.execute("INSERT OR IGNORE INTO currencies(code,name) VALUES ('NWX','낙원자화')")
        self.execute("UPDATE currencies SET decimal_places=0 WHERE code='NWX'")
        # Currencies have at most two decimal places (cents). The column's legacy
        # default is 6, so new rows are clamped and explicit larger values refused.
        self.execute("UPDATE currencies SET decimal_places=2 WHERE decimal_places>2")
        self.execute('''CREATE TRIGGER IF NOT EXISTS currencies_places_default
            AFTER INSERT ON currencies WHEN NEW.decimal_places>2
            BEGIN UPDATE currencies SET decimal_places=2 WHERE code=NEW.code; END''')
        self.execute('''CREATE TRIGGER IF NOT EXISTS currencies_places_limit
            BEFORE UPDATE OF decimal_places ON currencies WHEN NEW.decimal_places>2
            BEGIN SELECT RAISE(ABORT,'통화 소수 자릿수는 0~2로 설정해주세요.'); END''')
        self.execute("""CREATE TABLE IF NOT EXISTS currency_transactions (
            transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            source_currency TEXT NOT NULL, target_currency TEXT NOT NULL,
            source_units INTEGER NOT NULL, target_units INTEGER NOT NULL,
            fee_nwx INTEGER NOT NULL, recorded_epoch REAL NOT NULL)""")
        self.execute("""CREATE TABLE IF NOT EXISTS currency_transfer_transactions (
            transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL, source_account TEXT, target_account TEXT,
            source_currency TEXT NOT NULL, target_currency TEXT NOT NULL,
            source_units INTEGER NOT NULL, target_units INTEGER NOT NULL,
            fee_nwx INTEGER NOT NULL, recorded_epoch REAL NOT NULL)""")
        self.execute("""CREATE TABLE IF NOT EXISTS basic_income_payments (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL, user_id TEXT NOT NULL,
            source_currency TEXT NOT NULL, target_currency TEXT NOT NULL,
            source_units INTEGER NOT NULL, target_units INTEGER NOT NULL,
            source_rate TEXT NOT NULL, target_rate TEXT NOT NULL,
            recorded_epoch REAL NOT NULL)""")
        # Settled FX, transfer, and public-payment rows are audit records, not
        # mutable balances. Corrections must be represented by a new event.
        for table in ("currency_transactions", "currency_transfer_transactions",
                      "basic_income_payments"):
            for operation in ("UPDATE", "DELETE"):
                self.execute(f"""CREATE TRIGGER IF NOT EXISTS
                    trg_{table}_immutable_{operation.lower()}
                    BEFORE {operation} ON {table}
                    BEGIN SELECT RAISE(ABORT,'currency transaction history is append-only'); END""")
        # A currency's quoted base rate remains administrator controlled.  This
        # table stores the neutral supply point used to apply a bounded liquidity
        # adjustment at settlement/read time.  Units are millionths of a currency.
        self.execute("""CREATE TABLE IF NOT EXISTS currency_liquidity_state (
            currency_code TEXT PRIMARY KEY REFERENCES currencies(code),
            baseline_supply_units INTEGER NOT NULL DEFAULT 0 CHECK(baseline_supply_units >= 0),
            baseline_circulation_ratio REAL NOT NULL DEFAULT 0 CHECK(baseline_circulation_ratio BETWEEN 0 AND 1),
            issuance_elasticity REAL NOT NULL DEFAULT 0.50 CHECK(issuance_elasticity BETWEEN 0 AND 3),
            circulation_elasticity REAL NOT NULL DEFAULT 0.20 CHECK(circulation_elasticity BETWEEN 0 AND 3),
            min_multiplier REAL NOT NULL DEFAULT 0.25 CHECK(min_multiplier > 0),
            max_multiplier REAL NOT NULL DEFAULT 4.00 CHECK(max_multiplier >= min_multiplier),
            updated_epoch REAL NOT NULL DEFAULT 0
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS country_currencies (
            nationality TEXT PRIMARY KEY, currency_code TEXT NOT NULL REFERENCES currencies(code))""")
        country_columns = {row[1] for row in self.execute("PRAGMA table_info(country_currencies)")}
        added_base_rate = 'base_rate' not in country_columns
        added_legal_max_rate = 'legal_max_rate' not in country_columns
        if added_base_rate:
            self.execute("ALTER TABLE country_currencies ADD COLUMN base_rate REAL NOT NULL DEFAULT 0 "
                         "CHECK(base_rate BETWEEN 0 AND 100)")
        if added_legal_max_rate:
            self.execute("ALTER TABLE country_currencies ADD COLUMN legal_max_rate REAL NOT NULL DEFAULT 20 "
                         "CHECK(legal_max_rate BETWEEN 0 AND 100)")
        if 'tax_percent' not in country_columns:
            self.execute("ALTER TABLE country_currencies ADD COLUMN tax_percent REAL NOT NULL DEFAULT 3 "
                         "CHECK(tax_percent BETWEEN 0 AND 100)")
        if 'statutory_rate' in country_columns:
            self.execute("ALTER TABLE country_currencies DROP COLUMN statutory_rate")
        self._ensure_negative_policy_rates()
        self.execute("INSERT OR IGNORE INTO country_currencies(nationality,currency_code) VALUES ('낙원','NWX')")
        self.execute("""CREATE TABLE IF NOT EXISTS country_macro_indicators (
            nationality TEXT PRIMARY KEY REFERENCES country_currencies(nationality) ON DELETE CASCADE,
            inflation_percent REAL NOT NULL DEFAULT 2 CHECK(inflation_percent BETWEEN -100 AND 1000),
            real_gdp_growth_percent REAL NOT NULL DEFAULT 2 CHECK(real_gdp_growth_percent BETWEEN -100 AND 100),
            unemployment_percent REAL NOT NULL DEFAULT 5 CHECK(unemployment_percent BETWEEN 0 AND 100),
            fiscal_debt_percent REAL NOT NULL DEFAULT 60 CHECK(fiscal_debt_percent BETWEEN 0 AND 1000),
            credit_stress_shock_bps INTEGER NOT NULL DEFAULT 200 CHECK(credit_stress_shock_bps BETWEEN 0 AND 1000),
            fx_stress_shock_percent REAL NOT NULL DEFAULT 10 CHECK(fx_stress_shock_percent BETWEEN 0 AND 100),
            updated_by TEXT NOT NULL DEFAULT 'system',
            updated_epoch REAL NOT NULL DEFAULT 0
        )""")
        macro_columns = {row[1] for row in self.execute(
            "PRAGMA table_info(country_macro_indicators)"
        )}
        if "credit_stress_shock_bps" not in macro_columns:
            self.execute(
                "ALTER TABLE country_macro_indicators ADD COLUMN "
                "credit_stress_shock_bps INTEGER NOT NULL DEFAULT 200 "
                "CHECK(credit_stress_shock_bps BETWEEN 0 AND 1000)"
            )
        if "fx_stress_shock_percent" not in macro_columns:
            self.execute(
                "ALTER TABLE country_macro_indicators ADD COLUMN "
                "fx_stress_shock_percent REAL NOT NULL DEFAULT 10 "
                "CHECK(fx_stress_shock_percent BETWEEN 0 AND 100)"
            )
        self.execute(
            "INSERT OR IGNORE INTO country_macro_indicators(nationality) "
            "SELECT nationality FROM country_currencies"
        )
        self.execute("""CREATE TRIGGER IF NOT EXISTS country_macro_indicators_seed
            AFTER INSERT ON country_currencies BEGIN
                INSERT OR IGNORE INTO country_macro_indicators(nationality) VALUES (NEW.nationality);
            END""")
        self.execute('''CREATE TABLE IF NOT EXISTS country_tax_rules (
            nationality TEXT NOT NULL REFERENCES country_currencies(nationality),
            income_source TEXT NOT NULL CHECK(length(income_source) BETWEEN 1 AND 64),
            tax_percent REAL NOT NULL CHECK(tax_percent BETWEEN 0 AND 100),
            updated_by TEXT NOT NULL,
            updated_epoch REAL NOT NULL,
            PRIMARY KEY(nationality,income_source))''')
        self.execute("""CREATE TABLE IF NOT EXISTS guild_countries (
            guild_id TEXT PRIMARY KEY,
            nationality TEXT NOT NULL REFERENCES country_currencies(nationality)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_guild_countries_nationality "
                     "ON guild_countries(nationality)")
        # The legacy Nakwon treasury remains the lottery treasury.  Other
        # federation countries receive an independent nation account so taxes
        # can never be mixed merely because they share one SQLite database.
        self.execute("""CREATE TABLE IF NOT EXISTS country_treasuries (
            nationality TEXT PRIMARY KEY REFERENCES country_currencies(nationality),
            user_id TEXT NOT NULL UNIQUE REFERENCES users(user_id)
        )""")
        self.execute("INSERT OR IGNORE INTO country_treasuries(nationality,user_id) "
                     "VALUES ('낙원','20230216')")
        self.execute("""CREATE TABLE IF NOT EXISTS aml_monitoring_rules (
            nationality TEXT PRIMARY KEY REFERENCES country_currencies(nationality),
            currency_code TEXT NOT NULL REFERENCES currencies(code),
            large_transaction_limit REAL NOT NULL DEFAULT 0 CHECK(large_transaction_limit >= 0),
            rolling_24h_amount_limit REAL NOT NULL DEFAULT 0 CHECK(rolling_24h_amount_limit >= 0),
            rolling_24h_count_limit INTEGER NOT NULL DEFAULT 0 CHECK(rolling_24h_count_limit >= 0),
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            updated_epoch REAL NOT NULL DEFAULT 0
        )""")
        self.execute("""INSERT OR IGNORE INTO aml_monitoring_rules(nationality,currency_code)
            SELECT nationality,currency_code FROM country_currencies""")
        self.execute("""CREATE TABLE IF NOT EXISTS aml_alerts (
            alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            nationality TEXT NOT NULL,
            rule_code TEXT NOT NULL,
            reference_key TEXT NOT NULL,
            currency_code TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount >= 0),
            status TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open','reviewing','closed')),
            outcome_code TEXT,
            opened_epoch REAL NOT NULL,
            reviewed_by TEXT,
            reviewed_epoch REAL,
            UNIQUE(user_id,rule_code,reference_key)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_aml_alerts_opened "
                     "ON aml_alerts(status,opened_epoch)")
        self.execute("""CREATE TABLE IF NOT EXISTS aml_case_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL REFERENCES aml_alerts(alert_id),
            actor_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            prior_status TEXT,
            new_status TEXT NOT NULL,
            outcome_code TEXT,
            recorded_epoch REAL NOT NULL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_aml_case_events_alert_time "
                     "ON aml_case_events(alert_id,recorded_epoch)")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_aml_case_events_immutable_{operation.lower()}
                BEFORE {operation} ON aml_case_events
                BEGIN SELECT RAISE(ABORT,'AML case history is append-only'); END""")
        # Head-office account of each bank (financial institution) for assets it must hold.
        self.execute("""CREATE TABLE IF NOT EXISTS bank_accounts (
            issuer TEXT PRIMARY KEY,
            user_id TEXT NOT NULL UNIQUE REFERENCES users(user_id)
        )""")
        # A head office may run a deficit; administrators are warned once per episode.
        self.execute("""CREATE TABLE IF NOT EXISTS bank_deficit_alerts (
            issuer TEXT PRIMARY KEY,
            cash REAL NOT NULL,
            alerted_epoch REAL NOT NULL
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS financial_holidays (
            nationality TEXT NOT NULL REFERENCES country_currencies(nationality),
            holiday_date TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(nationality,holiday_date)
        )""")
        # Newly added countries receive an independent default, which an
        # administrator can change without affecting any other nationality.
        if added_base_rate:
            self.execute("UPDATE country_currencies SET base_rate=COALESCE("
                         "(SELECT CAST(value AS REAL) FROM settings WHERE key='base_rate'),0)")
        if added_legal_max_rate:
            self.execute("UPDATE country_currencies SET legal_max_rate=COALESCE("
                         "(SELECT CAST(value AS REAL) FROM settings WHERE key='legal_max_rate'),20)")
        self.execute("UPDATE country_currencies SET tax_percent=COALESCE(tax_percent, "
                     "(SELECT CAST(value AS REAL) FROM settings WHERE key='tax_percent'),3)")
        self._init_country_rate_history()
        self._init_market_rate_structure()
        self._init_credit_risk_scenarios()
        self._migrate_currency_wallets()
        self._normalize_home_currency_accounts()
        self.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('currency_exchange_fee_percent','0.1')")
        self._migrate_currency_identifiers()
        for table in ('company','investment','market_assets','market_orders'):
            columns = {r[1] for r in self.execute(f'PRAGMA table_info({table})')}
            if 'quote_currency' not in columns:
                self.execute(f"ALTER TABLE {table} ADD COLUMN quote_currency TEXT NOT NULL DEFAULT 'NWX'")
        self._init_legal_rate_caps()
        self._init_floating_rate_pricing()
        self._seed_liquidity_states()
        self._init_currency_precision_triggers()

    def _init_currency_precision_triggers(self):
        """Replace legacy whole-NWX rounding with denomination-aware rounding."""
        direct = (
            ("company", ("capital", "share_price", "liquidation_value_per_share",
                         "dividend_per_share"), "company_code", "NEW.quote_currency"),
            ("market_assets", ("price", "dividend_per_share"), "code", "NEW.quote_currency"),
            ("saving", ("deposit_amount",), "product_code", "NEW.quote_currency"),
            ("liability", ("loan_limit", "repayment_amount"), "product_code", "NEW.quote_currency"),
            ("investment", ("unit_price", "face_value", "dividend_per_share"),
             "product_code", "NEW.quote_currency"),
            ("treasury_bond_products", ("face_value", "market_price", "total_supply",
                                         "sold_amount"), "product_code", "NEW.quote_currency"),
            ("accounts", ("balance", "principal", "insurance_paid", "uninsured_claim"),
             "account_number", "NEW.quote_currency"),
            ("loans", ("principal", "outstanding_balance", "origination_fee",
                       "mandatory_cost_fee", "collateral_amount", "collateral_seized",
                       "collateral_asset_value", "collateral_liquidated_value",
                       "provisioned_loss", "underwriting_income",
                       "ecl_allowance",
                       "prepayment_fees_paid"),
             "loan_id", "NEW.quote_currency"),
            ("market_orders", ("limit_price", "reserved_cash"), "order_id",
             "NEW.quote_currency"),
            ("financial_institution_ledger", ("base_amount",), "entry_id",
             "NEW.base_currency_code"),
        )
        for table, columns, key, currency in direct:
            existing = {row[1] for row in self.execute(f"PRAGMA table_info({table})")}
            available = tuple(column for column in columns if column in existing)
            if available and "quote_currency" in existing:
                self.enforce_currency_money(table, available, key, currency)

        derived = (
            ("users", ("cash", "annual_income"), "user_id",
             "SELECT currency_code FROM country_currencies WHERE nationality=NEW.nationality"),
            ("loan_payment_schedule",
             ("principal_due", "interest_due", "fee_due", "amount_paid"), "schedule_id",
             "SELECT quote_currency FROM loans WHERE loan_id=NEW.loan_id"),
            ("loan_ecl_assessments", ("ead", "expected_loss"), "assessment_id",
             "SELECT quote_currency FROM loans WHERE loan_id=NEW.loan_id"),
            ("loan_collateral_assets",
             ("average_cost", "last_price", "last_value", "liquidation_price",
              "liquidation_cost", "liquidation_proceeds"), "collateral_id",
             "SELECT quote_currency FROM loans WHERE loan_id=NEW.loan_id"),
            ("deposit_insurance_claims", ("covered_amount", "uninsured_amount"), "claim_id",
             "SELECT quote_currency FROM accounts WHERE account_number=NEW.account_number"),
            ("collateral_liquidation_events",
             ("mark_price", "gross_proceeds", "liquidation_cost", "net_proceeds"),
             "event_id", "SELECT quote_currency FROM loans WHERE loan_id=NEW.loan_id"),
            ("treasury_bond_holdings", ("principal", "face_value", "acquisition_cost"),
             "holding_id", "SELECT quote_currency FROM treasury_bond_products "
             "WHERE product_code=NEW.product_code"),
            ("user_assets", ("average_cost",), "holding_id",
             "SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM company WHERE company_code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code LIMIT 1"),
            ("depositary_receipt_movements", ("underlying_average_cost",), "movement_id",
             "SELECT quote_currency FROM company WHERE company_code=(SELECT underlying_code "
             "FROM depositary_receipts WHERE dr_code=NEW.dr_code)"),
            ("market_price_history", ("price",), "history_id",
             "SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM company WHERE company_code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code LIMIT 1"),
            ("financial_institutions",
             ("equity_capital", "retained_earnings", "deposit_insurance_limit"),
             "issuer", "SELECT c.currency_code FROM country_currencies c "
             "WHERE c.nationality=NEW.nationality"),
            ("tax_transactions", ("gross_amount", "tax_amount", "net_amount"),
             "transaction_id", "NEW.currency_code"),
        )
        for table, columns, key, currency in derived:
            existing = {row[1] for row in self.execute(f"PRAGMA table_info({table})")}
            available = tuple(column for column in columns if column in existing)
            if available:
                self.enforce_currency_money(table, available, key, currency)
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_insurance_payout_precision_insert
            BEFORE INSERT ON deposit_insurance_claims
            WHEN NEW.payout_amount IS NOT NULL AND NEW.payout_currency IS NOT NULL
              AND NEW.payout_amount <> ROUND(
                  NEW.payout_amount+CASE WHEN NEW.payout_amount>=0 THEN 1e-12 ELSE -1e-12 END,
                  COALESCE((SELECT decimal_places FROM currencies
                            WHERE code=NEW.payout_currency),0))
            BEGIN SELECT RAISE(ABORT,'insurance payout amount exceeds currency precision'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_insurance_payout_pair_insert
            BEFORE INSERT ON deposit_insurance_claims
            WHEN (NEW.payout_currency IS NULL) <> (NEW.payout_amount IS NULL)
              OR (NEW.payout_currency IS NOT NULL AND NOT EXISTS (
                  SELECT 1 FROM currencies WHERE code=NEW.payout_currency))
            BEGIN SELECT RAISE(ABORT,'insurance payout currency and amount must be paired and use a known currency'); END""")
        self._ensure_account_units()
        self._ensure_cash_minor()
        self._ensure_money_snaps()

    # Minor units of a citizen's home currency: x1 for 0 decimal places, x10, x100.
    PLACES = ("(SELECT COALESCE(MAX(c.decimal_places),0) FROM country_currencies n "
              "JOIN currencies c ON c.code=n.currency_code WHERE n.nationality={who})")
    FACTOR = "(CASE {places} WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END)"

    def _ensure_cash_minor(self):
        """Wallet cash is stored exactly as an integer of the home currency's minor unit.

        ``users.cash_minor`` is authoritative; ``users.cash`` is its mirror in native
        units for existing readers. Code that still writes ``cash`` is snapped to the
        currency's minor unit (e.g. cents) at once, so float error never accumulates.
        """
        columns = {row[1] for row in self.execute("PRAGMA table_info(users)")}
        if "cash_minor" not in columns:
            self.execute("ALTER TABLE users ADD COLUMN cash_minor INTEGER NOT NULL DEFAULT 0")
        factor = self.FACTOR.format(places=self.PLACES.format(who="NEW.nationality"))
        mirror = (f"CASE WHEN NEW.cash_minor % {factor} = 0 THEN NEW.cash_minor / {factor} "
                  f"ELSE NEW.cash_minor * 1.0 / {factor} END")
        for name in ("trg_users_cash_minor_mirror", "trg_users_cash_snap", "trg_users_cash_insert"):
            self.execute(f"DROP TRIGGER IF EXISTS {name}")
        self.execute(f"""CREATE TRIGGER trg_users_cash_minor_mirror AFTER UPDATE OF cash_minor ON users
            WHEN NEW.cash_minor IS NOT OLD.cash_minor
            BEGIN UPDATE users SET cash={mirror} WHERE user_id=NEW.user_id; END""")
        self.execute(f"""CREATE TRIGGER trg_users_cash_snap AFTER UPDATE OF cash ON users
            WHEN NEW.cash IS NOT OLD.cash
             AND CAST(ROUND(COALESCE(NEW.cash,0) * {factor}) AS INTEGER) IS NOT NEW.cash_minor
            BEGIN UPDATE users SET cash_minor=CAST(ROUND(COALESCE(NEW.cash,0) * {factor}) AS INTEGER)
                  WHERE user_id=NEW.user_id; END""")
        self.execute(f"""CREATE TRIGGER trg_users_cash_insert AFTER INSERT ON users
            BEGIN UPDATE users SET cash_minor=CAST(ROUND(COALESCE(NEW.cash,0) * {factor}) AS INTEGER)
                  WHERE user_id=NEW.user_id; END""")
        # A citizen's minor unit changes with a currency's decimal places or a nationality.
        self.execute("DROP TRIGGER IF EXISTS trg_currency_places_rescale")
        self.execute("DROP TRIGGER IF EXISTS trg_users_nationality_rescale")
        self.execute(f"""CREATE TRIGGER trg_currency_places_rescale AFTER UPDATE OF decimal_places ON currencies
            BEGIN UPDATE users SET cash_minor=CAST(ROUND(COALESCE(cash,0) *
                    {self.FACTOR.format(places=self.PLACES.format(who="users.nationality"))}) AS INTEGER)
                  WHERE nationality IN (SELECT nationality FROM country_currencies WHERE currency_code=NEW.code); END""")
        self.execute(f"""CREATE TRIGGER trg_users_nationality_rescale AFTER UPDATE OF nationality ON users
            WHEN NEW.nationality IS NOT OLD.nationality
            BEGIN UPDATE users SET cash_minor=CAST(ROUND(COALESCE(NEW.cash,0) * {factor}) AS INTEGER)
                  WHERE user_id=NEW.user_id; END""")
        rescale = (f"UPDATE users SET cash_minor=CAST(ROUND(COALESCE(cash,0) * "
                   f"{self.FACTOR.format(places=self.PLACES.format(who='users.nationality'))}) AS INTEGER) "
                   "WHERE nationality=NEW.nationality;")
        for name, event in (("trg_country_currency_insert", "AFTER INSERT ON country_currencies"),
                            ("trg_country_currency_update", "AFTER UPDATE OF currency_code ON country_currencies")):
            self.execute(f"DROP TRIGGER IF EXISTS {name}")
            self.execute(f"CREATE TRIGGER {name} {event} BEGIN {rescale} END")
        self.execute(f"UPDATE users SET cash_minor=CAST(ROUND(COALESCE(cash,0) * "
                     f"{self.FACTOR.format(places=self.PLACES.format(who='users.nationality'))}) AS INTEGER)")

    def _ensure_money_snaps(self):
        """Money held in other tables is kept at its currency's minor unit (no float drift)."""
        for table, column in (("market_orders", "reserved_cash"), ("loans", "outstanding_balance")):
            if not self.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)):
                continue
            places = "(SELECT COALESCE(MAX(decimal_places),0) FROM currencies WHERE code=COALESCE(NEW.quote_currency,'NWX'))"
            key = "rowid"
            for event, name in ((f"AFTER UPDATE OF {column} ON {table}", f"trg_{table}_{column}_snap"),
                                (f"AFTER INSERT ON {table}", f"trg_{table}_{column}_snap_insert")):
                self.execute(f"DROP TRIGGER IF EXISTS {name}")
                self.execute(f"""CREATE TRIGGER {name} {event}
                    WHEN NEW.{column} IS NOT NULL AND NEW.{column} <> ROUND(NEW.{column}, {places})
                    BEGIN UPDATE {table} SET {column}=ROUND(NEW.{column}, {places}) WHERE {key}=NEW.{key}; END""")

    def _ensure_account_units(self):
        """balance_units is every account's exact balance; balance only mirrors it.

        Savings used to keep their amount in balance while foreign-currency
        accounts used balance_units. Both now store 1/1,000,000 units, and the
        legacy balance column is maintained by trigger for readers.
        """
        columns = {row[1] for row in self.execute("PRAGMA table_info(accounts)")}
        if "balance_units" not in columns:
            return
        migrated = self.execute_one("SELECT 1 FROM settings WHERE key='account_units_v1'") \
            if self.execute_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'") else True
        for name in ("trg_accounts_units_mirror", "trg_accounts_units_insert", "trg_accounts_balance_guard"):
            self.execute(f"DROP TRIGGER IF EXISTS {name}")
        if not migrated:
            self.execute("UPDATE accounts SET balance_units=CAST(ROUND(COALESCE(balance,0)*1000000) AS INTEGER) "
                         "WHERE account_type<>'currency_saving'")
            self.execute("INSERT INTO settings(key,value) VALUES ('account_units_v1', ?)", (str(int(time.time())),))
        self.execute("""CREATE TRIGGER trg_accounts_units_mirror AFTER UPDATE OF balance_units ON accounts
            WHEN NEW.balance_units IS NOT OLD.balance_units
            BEGIN UPDATE accounts SET balance=NEW.balance_units/1000000.0 WHERE account_number=NEW.account_number; END""")
        # Inserts that still give only balance (older code paths) are converted once.
        self.execute("""CREATE TRIGGER trg_accounts_units_insert AFTER INSERT ON accounts
            BEGIN UPDATE accounts SET balance_units=CASE WHEN NEW.balance_units=0 AND COALESCE(NEW.balance,0)<>0
                     THEN CAST(ROUND(NEW.balance*1000000) AS INTEGER) ELSE NEW.balance_units END
                 WHERE account_number=NEW.account_number; END""")
        # Writing balance without balance_units would desynchronise the two; reject it.
        self.execute("""CREATE TRIGGER trg_accounts_balance_guard BEFORE UPDATE OF balance ON accounts
            WHEN NEW.balance_units IS OLD.balance_units
             AND ABS(COALESCE(NEW.balance,0) - NEW.balance_units/1000000.0) > 0.500001
            BEGIN SELECT RAISE(ABORT, 'Account balances are stored in balance_units; update balance_units instead'); END""")
        self.execute("UPDATE accounts SET balance=balance_units/1000000.0 "
                     "WHERE ABS(COALESCE(balance,0) - balance_units/1000000.0) > 0.0000001")

    def _ensure_negative_policy_rates(self):
        """Rebuild the small country table once to permit negative policy rates."""
        sql = self.execute_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='country_currencies'"
        )
        definition = str(sql['sql'] if sql else '')
        if "base_rate BETWEEN -100 AND 100" in definition:
            return
        with self.lock:
            self.conn.commit()
            self.conn.execute("PRAGMA foreign_keys=OFF")
            try:
                # SQLite validates every trigger body while a referenced table is
                # rebuilt. All of these triggers are recreated by the schema
                # initializers later in this constructor.
                triggers = self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND sql LIKE '%country_currencies%'"
                ).fetchall()
                self.conn.execute("BEGIN IMMEDIATE")
                for trigger in triggers:
                    name = str(trigger[0]).replace('"', '""')
                    self.conn.execute(f'DROP TRIGGER "{name}"')
                self.conn.execute("""CREATE TABLE country_currencies_new (
                        nationality TEXT PRIMARY KEY,
                        currency_code TEXT NOT NULL REFERENCES currencies(code),
                        base_rate REAL NOT NULL DEFAULT 0 CHECK(base_rate BETWEEN -100 AND 100),
                        legal_max_rate REAL NOT NULL DEFAULT 20 CHECK(legal_max_rate BETWEEN 0 AND 100),
                        tax_percent REAL NOT NULL DEFAULT 3 CHECK(tax_percent BETWEEN 0 AND 100)
                    )""")
                self.conn.execute("""INSERT INTO country_currencies_new
                        (nationality,currency_code,base_rate,legal_max_rate,tax_percent)
                    SELECT nationality,currency_code,base_rate,legal_max_rate,tax_percent
                    FROM country_currencies""")
                self.conn.execute("DROP TABLE country_currencies")
                self.conn.execute(
                    "ALTER TABLE country_currencies_new RENAME TO country_currencies"
                )
                self.conn.commit()
            except Exception:
                if self.conn.in_transaction:
                    self.conn.rollback()
                raise
            finally:
                self.conn.execute("PRAGMA foreign_keys=ON")

    def _init_country_rate_history(self):
        """Persist policy-rate dates so delayed variable resets are reproducible."""
        existing = self.execute_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='country_rate_history'"
        )
        schema_sql = "".join(str(existing['sql'] if existing else "").lower().split())
        supports_negative_policy_rate = (
            "rate_key='base_rate'" in schema_sql
            and "annual_percentbetween-100and100" in schema_sql
        )
        if existing and not supports_negative_policy_rate:
            legacy_rows = list(self.execute(
                "SELECT nationality,rate_key,annual_percent,effective_epoch,changed_by "
                "FROM country_rate_history"
            ))
            self.execute('DROP TRIGGER IF EXISTS "trg_country_rate_history_insert"')
            self.execute('DROP TRIGGER IF EXISTS "trg_country_rate_history_update"')
            self.execute("DROP TABLE country_rate_history")
        else:
            legacy_rows = []
        self.execute("""CREATE TABLE IF NOT EXISTS country_rate_history (
            nationality TEXT NOT NULL,
            rate_key TEXT NOT NULL CHECK(rate_key IN ('base_rate','legal_max_rate')),
            annual_percent REAL NOT NULL CHECK(
                (rate_key='base_rate' AND annual_percent BETWEEN -100 AND 100)
                OR (rate_key='legal_max_rate' AND annual_percent BETWEEN 0 AND 100)),
            effective_epoch REAL NOT NULL,
            changed_by TEXT NOT NULL DEFAULT 'migration',
            PRIMARY KEY(nationality,rate_key,effective_epoch)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_country_rate_history_lookup "
                     "ON country_rate_history(nationality,rate_key,effective_epoch DESC)")
        for row in legacy_rows:
            self.execute(
                "INSERT OR IGNORE INTO country_rate_history VALUES (?,?,?,?,?)",
                tuple(row),
            )
        self.execute("""INSERT OR IGNORE INTO country_rate_history
            (nationality,rate_key,annual_percent,effective_epoch,changed_by)
            SELECT nationality,'base_rate',base_rate,0,'migration' FROM country_currencies""")
        self.execute("""INSERT OR IGNORE INTO country_rate_history
            (nationality,rate_key,annual_percent,effective_epoch,changed_by)
            SELECT nationality,'legal_max_rate',legal_max_rate,0,'migration' FROM country_currencies""")
        self.execute('DROP TRIGGER IF EXISTS "trg_country_rate_history_insert"')
        self.execute('DROP TRIGGER IF EXISTS "trg_country_rate_history_update"')
        self.execute("""CREATE TRIGGER trg_country_rate_history_insert
            AFTER INSERT ON country_currencies BEGIN
                INSERT OR REPLACE INTO country_rate_history VALUES
                    (NEW.nationality,'base_rate',NEW.base_rate,
                     MAX((julianday('now')-2440587.5)*86400.0,COALESCE((
                         SELECT MAX(effective_epoch)+0.000001 FROM country_rate_history
                         WHERE nationality=NEW.nationality AND rate_key='base_rate'),0)),'sql');
                INSERT OR REPLACE INTO country_rate_history VALUES
                    (NEW.nationality,'legal_max_rate',NEW.legal_max_rate,
                     MAX((julianday('now')-2440587.5)*86400.0,COALESCE((
                         SELECT MAX(effective_epoch)+0.000001 FROM country_rate_history
                         WHERE nationality=NEW.nationality AND rate_key='legal_max_rate'),0)),'sql');
            END""")
        self.execute("""CREATE TRIGGER trg_country_rate_history_update
            AFTER UPDATE OF base_rate,legal_max_rate ON country_currencies BEGIN
                INSERT OR REPLACE INTO country_rate_history
                    SELECT NEW.nationality,'base_rate',NEW.base_rate,
                        MAX((julianday('now')-2440587.5)*86400.0,COALESCE((
                            SELECT MAX(effective_epoch)+0.000001 FROM country_rate_history
                            WHERE nationality=NEW.nationality AND rate_key='base_rate'),0)),'sql'
                    WHERE NEW.base_rate IS NOT OLD.base_rate;
                INSERT OR REPLACE INTO country_rate_history
                    SELECT NEW.nationality,'legal_max_rate',NEW.legal_max_rate,
                        MAX((julianday('now')-2440587.5)*86400.0,COALESCE((
                            SELECT MAX(effective_epoch)+0.000001 FROM country_rate_history
                            WHERE nationality=NEW.nationality AND rate_key='legal_max_rate'),0)),'sql'
                    WHERE NEW.legal_max_rate IS NOT OLD.legal_max_rate;
            END""")
        self.execute("""CREATE TABLE IF NOT EXISTS country_rate_history_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation TEXT NOT NULL CHECK(operation IN ('insert','update','delete')),
            nationality TEXT NOT NULL,
            rate_key TEXT NOT NULL,
            before_percent REAL,
            after_percent REAL,
            before_epoch REAL,
            after_epoch REAL,
            before_actor TEXT,
            after_actor TEXT,
            recorded_epoch REAL NOT NULL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_country_rate_history_events_recent "
                     "ON country_rate_history_events(nationality,event_id DESC)")
        for operation, trigger_operation in (("INSERT", "insert"), ("UPDATE", "update"),
                                             ("DELETE", "delete")):
            before = "OLD" if operation != "INSERT" else None
            after = "NEW" if operation != "DELETE" else None
            before_nationality = f"{before}.nationality" if before else "NULL"
            after_nationality = f"{after}.nationality" if after else "NULL"
            before_rate_key = f"{before}.rate_key" if before else "NULL"
            after_rate_key = f"{after}.rate_key" if after else "NULL"
            before_values = [f"{before}.{column}" if before else "NULL" for column in
                             ("annual_percent", "effective_epoch", "changed_by")]
            after_values = [f"{after}.{column}" if after else "NULL" for column in
                            ("annual_percent", "effective_epoch", "changed_by")]
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS
                trg_country_rate_history_event_{trigger_operation}
                AFTER {operation} ON country_rate_history BEGIN
                INSERT INTO country_rate_history_events(
                    operation,nationality,rate_key,before_percent,after_percent,
                    before_epoch,after_epoch,before_actor,after_actor,recorded_epoch
                ) VALUES (
                    '{trigger_operation}',COALESCE({after_nationality},{before_nationality}),
                    COALESCE({after_rate_key},{before_rate_key}),
                    {before_values[0]},{after_values[0]},
                    {before_values[1]},{after_values[1]},
                    {before_values[2]},{after_values[2]},
                    (julianday('now')-2440587.5)*86400.0
                ); END""")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS
                trg_country_rate_history_events_immutable_{operation.lower()}
                BEFORE {operation} ON country_rate_history_events
                BEGIN SELECT RAISE(ABORT,'country rate history events are append-only'); END""")

    def _init_market_rate_structure(self):
        """Create market benchmarks, a tenor curve, and institution cost inputs."""
        self.execute("""CREATE TABLE IF NOT EXISTS market_rate_curve (
            nationality TEXT NOT NULL REFERENCES country_currencies(nationality),
            benchmark_key TEXT NOT NULL CHECK(benchmark_key IN ('funding','bank_bond','treasury')),
            tenor_days INTEGER NOT NULL CHECK(tenor_days > 0),
            annual_rate REAL NOT NULL CHECK(annual_rate BETWEEN -100 AND 100),
            pass_through REAL NOT NULL DEFAULT 0.75 CHECK(pass_through BETWEEN 0 AND 2),
            updated_epoch REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(nationality,benchmark_key,tenor_days)
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS market_rate_curve_history (
            nationality TEXT NOT NULL,
            benchmark_key TEXT NOT NULL,
            tenor_days INTEGER NOT NULL,
            annual_rate REAL NOT NULL CHECK(annual_rate BETWEEN -100 AND 100),
            effective_epoch REAL NOT NULL,
            changed_by TEXT NOT NULL DEFAULT 'system',
            PRIMARY KEY(nationality,benchmark_key,tenor_days,effective_epoch)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_market_curve_history_lookup ON "
                     "market_rate_curve_history(nationality,benchmark_key,tenor_days,effective_epoch DESC)")
        self.execute("""CREATE TABLE IF NOT EXISTS market_rate_pressure (
            nationality TEXT NOT NULL,
            benchmark_key TEXT NOT NULL CHECK(benchmark_key IN ('funding','bank_bond','treasury')),
            adjustment REAL NOT NULL DEFAULT 0 CHECK(adjustment BETWEEN -20 AND 20),
            updated_epoch REAL NOT NULL,
            PRIMARY KEY(nationality,benchmark_key)
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS market_rate_pressure_history (
            nationality TEXT NOT NULL,
            benchmark_key TEXT NOT NULL,
            adjustment REAL NOT NULL CHECK(adjustment BETWEEN -20 AND 20),
            effective_epoch REAL NOT NULL,
            PRIMARY KEY(nationality,benchmark_key,effective_epoch)
        )""")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_rate_pressure_history "
            "ON market_rate_pressure_history(nationality,benchmark_key,effective_epoch DESC)"
        )
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS
                trg_market_rate_pressure_history_immutable_{operation.lower()}
                BEFORE {operation} ON market_rate_pressure_history
                BEGIN SELECT RAISE(ABORT,'market rate pressure history is append-only'); END""")
        curve = {
            'funding': ((30, .20, .85), (90, .35, .80), (365, .55, .70)),
            'bank_bond': ((90, .45, .90), (365, .75, .85), (1095, 1.10, .75)),
            'treasury': ((30, .05, .95), (90, .15, .90), (365, .35, .85),
                         (1095, .65, .75), (3650, 1.00, .60)),
        }
        for country in self.execute("SELECT nationality,base_rate FROM country_currencies"):
            for key, points in curve.items():
                for tenor, spread, pass_through in points:
                    self.execute(
                        "INSERT OR IGNORE INTO market_rate_curve VALUES (?,?,?,?,?,0)",
                        (country['nationality'], key, tenor,
                         float(country['base_rate'])+spread, pass_through),
                    )
                self.execute(
                    "INSERT OR IGNORE INTO market_rate_pressure VALUES (?,?,0,0)",
                    (country['nationality'], key),
                )
        self.execute("""INSERT OR IGNORE INTO market_rate_curve_history
            SELECT nationality,benchmark_key,tenor_days,annual_rate,0,'migration'
            FROM market_rate_curve""")
        self.execute('DROP TRIGGER IF EXISTS "trg_market_curve_history_insert"')
        self.execute('DROP TRIGGER IF EXISTS "trg_market_curve_history_update"')
        self.execute("""CREATE TRIGGER trg_market_curve_history_insert
            AFTER INSERT ON market_rate_curve BEGIN
                INSERT OR REPLACE INTO market_rate_curve_history VALUES
                    (NEW.nationality,NEW.benchmark_key,NEW.tenor_days,NEW.annual_rate,
                     COALESCE(NULLIF(NEW.updated_epoch,0),unixepoch()),'sql');
            END""")
        self.execute("""CREATE TRIGGER trg_market_curve_history_update
            AFTER UPDATE OF annual_rate ON market_rate_curve
            WHEN NEW.annual_rate IS NOT OLD.annual_rate BEGIN
                INSERT OR REPLACE INTO market_rate_curve_history VALUES
                    (NEW.nationality,NEW.benchmark_key,NEW.tenor_days,NEW.annual_rate,
                     COALESCE(NULLIF(NEW.updated_epoch,0),unixepoch()),'sql');
            END""")
        self.execute("""CREATE TABLE IF NOT EXISTS market_rate_curve_history_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation TEXT NOT NULL CHECK(operation IN ('insert','update','delete','replace')),
            nationality TEXT NOT NULL,
            benchmark_key TEXT NOT NULL,
            tenor_days INTEGER NOT NULL,
            before_rate REAL,
            after_rate REAL,
            before_epoch REAL,
            after_epoch REAL,
            before_actor TEXT,
            after_actor TEXT,
            recorded_epoch REAL NOT NULL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_market_curve_history_events_recent "
                     "ON market_rate_curve_history_events(nationality,event_id DESC)")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_market_curve_history_event_replace
            BEFORE INSERT ON market_rate_curve_history
            WHEN EXISTS (SELECT 1 FROM market_rate_curve_history WHERE
                nationality=NEW.nationality AND benchmark_key=NEW.benchmark_key
                AND tenor_days=NEW.tenor_days AND effective_epoch=NEW.effective_epoch)
            BEGIN
                INSERT INTO market_rate_curve_history_events(
                    operation,nationality,benchmark_key,tenor_days,before_rate,after_rate,
                    before_epoch,after_epoch,before_actor,after_actor,recorded_epoch)
                SELECT 'replace',old.nationality,old.benchmark_key,old.tenor_days,
                    old.annual_rate,NEW.annual_rate,old.effective_epoch,NEW.effective_epoch,
                    old.changed_by,NEW.changed_by,(julianday('now')-2440587.5)*86400.0
                FROM market_rate_curve_history old WHERE old.nationality=NEW.nationality
                    AND old.benchmark_key=NEW.benchmark_key AND old.tenor_days=NEW.tenor_days
                    AND old.effective_epoch=NEW.effective_epoch;
            END""")
        for operation, trigger_operation in (("INSERT", "insert"), ("UPDATE", "update"),
                                             ("DELETE", "delete")):
            before = "OLD" if operation != "INSERT" else None
            after = "NEW" if operation != "DELETE" else None
            before_identity = [f"{before}.{column}" if before else "NULL" for column in
                               ("nationality", "benchmark_key", "tenor_days")]
            after_identity = [f"{after}.{column}" if after else "NULL" for column in
                              ("nationality", "benchmark_key", "tenor_days")]
            before_values = [f"{before}.{column}" if before else "NULL" for column in
                             ("annual_rate", "effective_epoch", "changed_by")]
            after_values = [f"{after}.{column}" if after else "NULL" for column in
                            ("annual_rate", "effective_epoch", "changed_by")]
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS
                trg_market_curve_history_event_{trigger_operation}
                AFTER {operation} ON market_rate_curve_history BEGIN
                INSERT INTO market_rate_curve_history_events(
                    operation,nationality,benchmark_key,tenor_days,before_rate,after_rate,
                    before_epoch,after_epoch,before_actor,after_actor,recorded_epoch
                ) VALUES (
                    '{trigger_operation}',COALESCE({after_identity[0]},{before_identity[0]}),
                    COALESCE({after_identity[1]},{before_identity[1]}),
                    COALESCE({after_identity[2]},{before_identity[2]}),
                    {before_values[0]},{after_values[0]},
                    {before_values[1]},{after_values[1]},
                    {before_values[2]},{after_values[2]},
                    (julianday('now')-2440587.5)*86400.0
                ); END""")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS
                trg_market_curve_history_events_immutable_{operation.lower()}
                BEFORE {operation} ON market_rate_curve_history_events
                BEGIN SELECT RAISE(ABORT,'market rate curve history events are append-only'); END""")
        self.execute('DROP TRIGGER IF EXISTS "trg_country_market_curve_insert"')
        self.execute('DROP TRIGGER IF EXISTS "trg_country_market_curve_policy_move"')
        self.execute("""CREATE TRIGGER trg_country_market_curve_insert
            AFTER INSERT ON country_currencies BEGIN
                INSERT OR IGNORE INTO market_rate_curve VALUES
                    (NEW.nationality,'funding',30,NEW.base_rate+0.20,0.85,unixepoch()),
                    (NEW.nationality,'funding',90,NEW.base_rate+0.35,0.80,unixepoch()),
                    (NEW.nationality,'funding',365,NEW.base_rate+0.55,0.70,unixepoch()),
                    (NEW.nationality,'bank_bond',90,NEW.base_rate+0.45,0.90,unixepoch()),
                    (NEW.nationality,'bank_bond',365,NEW.base_rate+0.75,0.85,unixepoch()),
                    (NEW.nationality,'bank_bond',1095,NEW.base_rate+1.10,0.75,unixepoch()),
                    (NEW.nationality,'treasury',30,NEW.base_rate+0.05,0.95,unixepoch()),
                    (NEW.nationality,'treasury',90,NEW.base_rate+0.15,0.90,unixepoch()),
                    (NEW.nationality,'treasury',365,NEW.base_rate+0.35,0.85,unixepoch()),
                    (NEW.nationality,'treasury',1095,NEW.base_rate+0.65,0.75,unixepoch()),
                    (NEW.nationality,'treasury',3650,NEW.base_rate+1.00,0.60,unixepoch());
                INSERT OR IGNORE INTO market_rate_pressure VALUES
                    (NEW.nationality,'funding',0,unixepoch()),
                    (NEW.nationality,'bank_bond',0,unixepoch()),
                    (NEW.nationality,'treasury',0,unixepoch());
            END""")
        self.execute("""CREATE TRIGGER trg_country_market_curve_policy_move
            AFTER UPDATE OF base_rate ON country_currencies
            WHEN NEW.base_rate IS NOT OLD.base_rate BEGIN
                UPDATE market_rate_curve
                SET annual_rate=MAX(-100,MIN(100,
                        annual_rate+(NEW.base_rate-OLD.base_rate)*pass_through)),
                    updated_epoch=unixepoch()
                WHERE nationality=NEW.nationality;
            END""")
        self.execute("""CREATE TABLE IF NOT EXISTS financial_institutions (
            issuer TEXT PRIMARY KEY,
            nationality TEXT NOT NULL REFERENCES country_currencies(nationality),
            funding_spread REAL NOT NULL DEFAULT 0 CHECK(funding_spread BETWEEN -100 AND 100),
            operating_cost_spread REAL NOT NULL DEFAULT 0.5 CHECK(operating_cost_spread BETWEEN 0 AND 100),
            liquidity_spread REAL NOT NULL DEFAULT 0.2 CHECK(liquidity_spread BETWEEN -100 AND 100),
            target_margin REAL NOT NULL DEFAULT 1 CHECK(target_margin BETWEEN -100 AND 100),
            competition_adjustment REAL NOT NULL DEFAULT 0 CHECK(competition_adjustment BETWEEN -100 AND 100)
        )""")

        institution_columns = {
            row[1] for row in self.execute("PRAGMA table_info(financial_institutions)")
        }
        for column, declaration in {
            "equity_capital": "REAL NOT NULL DEFAULT 1000000 CHECK(equity_capital > 0)",
            "retained_earnings": "REAL NOT NULL DEFAULT 0",
            "target_capital_ratio": "REAL NOT NULL DEFAULT 0.10 CHECK(target_capital_ratio BETWEEN 0 AND 1)",
            "target_loan_deposit_ratio": "REAL NOT NULL DEFAULT 0.90 CHECK(target_loan_deposit_ratio > 0)",
            "funding_pressure_sensitivity": "REAL NOT NULL DEFAULT 0.50 CHECK(funding_pressure_sensitivity BETWEEN 0 AND 100)",
            "capital_pressure_sensitivity": "REAL NOT NULL DEFAULT 1.00 CHECK(capital_pressure_sensitivity BETWEEN 0 AND 100)",
            "competition_sensitivity": "REAL NOT NULL DEFAULT 0.25 CHECK(competition_sensitivity BETWEEN 0 AND 100)",
            "reserve_ratio": "REAL NOT NULL DEFAULT 0.10 CHECK(reserve_ratio BETWEEN 0 AND 1)",
            "deposit_insurance_limit": "INTEGER NOT NULL DEFAULT 50000000 CHECK(deposit_insurance_limit >= 0)",
            "status": "TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','restricted','failed'))",
            "failed_epoch": "REAL",
            "last_capital_update_epoch": "REAL",
        }.items():
            if column not in institution_columns:
                self.execute(
                    f"ALTER TABLE financial_institutions ADD COLUMN {column} {declaration}"
                )
        self.execute("""CREATE TABLE IF NOT EXISTS financial_institution_ledger (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            issuer TEXT NOT NULL,
            event_type TEXT NOT NULL,
            amount REAL NOT NULL,
            reference_key TEXT NOT NULL,
            created_epoch REAL NOT NULL,
            UNIQUE(issuer,event_type,reference_key)
        )""")
        ledger_columns = {
            row[1] for row in self.execute("PRAGMA table_info(financial_institution_ledger)")
        }
        for column, declaration in (
            ("currency_code", "TEXT NOT NULL DEFAULT 'NWX'"),
            ("base_currency_code", "TEXT NOT NULL DEFAULT 'NWX'"),
            ("base_amount", "REAL NOT NULL DEFAULT 0"),
        ):
            if column not in ledger_columns:
                self.execute(
                    f"ALTER TABLE financial_institution_ledger ADD COLUMN {column} {declaration}"
                )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_financial_institution_ledger "
            "ON financial_institution_ledger(issuer,created_epoch,event_type)"
        )
        self.execute("""CREATE TABLE IF NOT EXISTS bank_journal_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            issuer TEXT NOT NULL,
            event_type TEXT NOT NULL,
            reference_key TEXT NOT NULL,
            currency_code TEXT NOT NULL,
            created_epoch REAL NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','posted')),
            UNIQUE(issuer,event_type,reference_key)
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS bank_journal_lines (
            line_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL REFERENCES bank_journal_entries(entry_id),
            account_code TEXT NOT NULL,
            debit_units INTEGER NOT NULL DEFAULT 0 CHECK(debit_units >= 0),
            credit_units INTEGER NOT NULL DEFAULT 0 CHECK(credit_units >= 0),
            CHECK((debit_units > 0 AND credit_units = 0) OR
                  (credit_units > 0 AND debit_units = 0))
        )""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_bank_journal_post_balanced
            BEFORE UPDATE OF status ON bank_journal_entries
            WHEN NEW.status='posted' AND OLD.status='draft'
            BEGIN
                SELECT CASE WHEN
                    (SELECT COUNT(*) FROM bank_journal_lines WHERE entry_id=NEW.entry_id)<2
                    OR (SELECT COALESCE(SUM(debit_units),0)-COALESCE(SUM(credit_units),0)
                        FROM bank_journal_lines WHERE entry_id=NEW.entry_id)<>0
                THEN RAISE(ABORT,'unbalanced bank journal entry') END;
            END""")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_bank_journal_lines_immutable_{operation.lower()}
                BEFORE {operation} ON bank_journal_lines
                WHEN (SELECT status FROM bank_journal_entries WHERE entry_id=OLD.entry_id)='posted'
                BEGIN SELECT RAISE(ABORT,'posted bank journal lines are immutable'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_bank_journal_entry_immutable
            BEFORE UPDATE ON bank_journal_entries WHEN OLD.status='posted'
            BEGIN SELECT RAISE(ABORT,'posted bank journal entries are immutable'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_bank_journal_entry_no_delete
            BEFORE DELETE ON bank_journal_entries WHEN OLD.status='posted'
            BEGIN SELECT RAISE(ABORT,'posted bank journal entries are immutable'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_bank_journal_line_no_late_insert
            BEFORE INSERT ON bank_journal_lines
            WHEN (SELECT status FROM bank_journal_entries WHERE entry_id=NEW.entry_id)='posted'
            BEGIN SELECT RAISE(ABORT,'posted bank journal lines are immutable'); END""")
        self.execute("""INSERT OR IGNORE INTO financial_institutions(issuer,nationality)
            SELECT DISTINCT p.issuer,COALESCE(p.rate_nationality,
                (SELECT MIN(c.nationality) FROM country_currencies c
                 WHERE c.currency_code=p.quote_currency),'낙원')
            FROM (SELECT issuer,rate_nationality,quote_currency FROM saving
                  UNION SELECT issuer,rate_nationality,quote_currency FROM liability) p
            WHERE p.issuer IS NOT NULL AND TRIM(p.issuer)<>''""")
        for table in ("saving", "liability"):
            for suffix, operation in (("insert", "INSERT"), ("update", "UPDATE OF issuer,rate_nationality")):
                trigger = f"trg_{table}_institution_{suffix}"
                self.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
                self.execute(f'''CREATE TRIGGER "{trigger}" AFTER {operation} ON {table}
                    WHEN NEW.issuer IS NOT NULL AND TRIM(NEW.issuer)<>'' BEGIN
                        INSERT OR IGNORE INTO financial_institutions(issuer,nationality)
                        VALUES (NEW.issuer,COALESCE(NEW.rate_nationality,
                            (SELECT MIN(c.nationality) FROM country_currencies c
                             WHERE c.currency_code=NEW.quote_currency),'낙원'));
                    END''')
        self.execute('DROP TRIGGER IF EXISTS "trg_market_curve_product_reprice"')
        self.execute("""CREATE TRIGGER trg_market_curve_product_reprice
            AFTER UPDATE OF annual_rate ON market_rate_curve
            WHEN NEW.annual_rate IS NOT OLD.annual_rate BEGIN
                UPDATE saving SET interest_rate=MIN(COALESCE(rate_cap,100),MAX(
                    CASE WHEN negative_rate_allowed=1 THEN -100 ELSE rate_floor END,
                    NEW.annual_rate+COALESCE((SELECT adjustment FROM market_rate_pressure m
                        WHERE m.nationality=NEW.nationality
                          AND m.benchmark_key=NEW.benchmark_key),0)+
                    base_rate_spread+maturity_spread+promotional_adjustment+
                    COALESCE((SELECT competition_adjustment FROM financial_institutions f
                              WHERE f.issuer=saving.issuer),0)))
                WHERE rate_type='floating' AND rate_nationality=NEW.nationality
                  AND benchmark_key=NEW.benchmark_key AND benchmark_tenor_days=NEW.tenor_days;
                UPDATE liability SET interest_rate=MIN(
                    (SELECT legal_max_rate FROM country_currencies
                     WHERE nationality=NEW.nationality),COALESCE(rate_cap,100),MAX(rate_floor,
                    NEW.annual_rate+COALESCE((SELECT adjustment FROM market_rate_pressure m
                        WHERE m.nationality=NEW.nationality
                          AND m.benchmark_key=NEW.benchmark_key),0)+
                    base_rate_spread+maturity_spread+promotional_adjustment+
                    COALESCE((SELECT funding_spread+operating_cost_spread+liquidity_spread+
                        target_margin+competition_adjustment FROM financial_institutions f
                        WHERE f.issuer=liability.issuer),1.7)))
                WHERE rate_type='floating' AND rate_nationality=NEW.nationality
                  AND benchmark_key=NEW.benchmark_key AND benchmark_tenor_days=NEW.tenor_days;
            END""")

    def _init_credit_risk_scenarios(self):
        """Seed configurable forward-looking ECL scenarios for each country."""
        self.execute("""CREATE TABLE IF NOT EXISTS credit_risk_scenarios (
            nationality TEXT NOT NULL REFERENCES country_currencies(nationality),
            scenario_key TEXT NOT NULL CHECK(scenario_key IN ('optimistic','base','downside')),
            probability REAL NOT NULL CHECK(probability BETWEEN 0 AND 1),
            pd_multiplier REAL NOT NULL CHECK(pd_multiplier BETWEEN 0 AND 20),
            lgd_multiplier REAL NOT NULL CHECK(lgd_multiplier BETWEEN 0 AND 5),
            updated_epoch REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(nationality,scenario_key)
        )""")
        defaults = (("optimistic", .20, .80, .90),
                    ("base", .60, 1.00, 1.00),
                    ("downside", .20, 2.00, 1.15))
        for row in self.execute("SELECT nationality FROM country_currencies"):
            self.conn.executemany(
                "INSERT OR IGNORE INTO credit_risk_scenarios "
                "(nationality,scenario_key,probability,pd_multiplier,lgd_multiplier) "
                "VALUES (?,?,?,?,?)",
                [(row["nationality"], key, weight, pd_mult, lgd_mult)
                 for key, weight, pd_mult, lgd_mult in defaults],
            )
        # Older versions accepted non-normalized positive weights and the ECL
        # evaluator normalized them at use time. Normalize stored probabilities
        # once per open while preserving their effective relative weights.
        for row in self.execute(
                "SELECT nationality,SUM(probability) total,COUNT(*) scenario_count "
                "FROM credit_risk_scenarios GROUP BY nationality"):
            total = float(row["total"] or 0)
            if (row["scenario_count"] == 3 and total > 0
                    and abs(total-1.0) > 1e-6):
                self.execute(
                    "UPDATE credit_risk_scenarios SET probability=probability/? "
                    "WHERE nationality=?", (total, row["nationality"]),
                )
        self.execute('DROP TRIGGER IF EXISTS trg_country_credit_scenario_seed')
        self.execute("""CREATE TRIGGER trg_country_credit_scenario_seed
            AFTER INSERT ON country_currencies BEGIN
            INSERT OR IGNORE INTO credit_risk_scenarios
            (nationality,scenario_key,probability,pd_multiplier,lgd_multiplier)
            VALUES (NEW.nationality,'optimistic',.20,.80,.90),
                   (NEW.nationality,'base',.60,1.00,1.00),
                   (NEW.nationality,'downside',.20,2.00,1.15);
            END""")

    def _init_legal_rate_caps(self):
        """Apply the legal maximum to lending, not deposits or bond coupons."""
        currency_cap = """COALESCE(
            (SELECT c.legal_max_rate FROM country_currencies c
             WHERE c.nationality=NEW.rate_nationality),
            (SELECT MIN(c.legal_max_rate) FROM country_currencies c
             WHERE c.currency_code=NEW.quote_currency),
            CAST((SELECT value FROM settings WHERE key='legal_max_rate') AS REAL), 20)"""
        # Remove triggers installed by the older broad-cap model.
        for table in ("saving", "liability", "investment", "treasury_bond_products"):
            for suffix in ("insert", "update"):
                self.execute(f'DROP TRIGGER IF EXISTS "trg_{table}_legal_max_{suffix}"')
        for table, rate_column, cap, condition in (
            ("liability", "interest_rate", currency_cap, None),
        ):
            for operation in ("INSERT", f"UPDATE OF {rate_column}, quote_currency, rate_nationality"):
                suffix = "insert" if operation == "INSERT" else "update"
                trigger = f"trg_{table}_legal_max_{suffix}"
                self.execute(f'''CREATE TRIGGER "{trigger}" BEFORE {operation} ON "{table}"
                    WHEN {condition or ''}(
                        NEW.{rate_column} IS NULL OR NEW.{rate_column} < 0
                        OR NEW.{rate_column} > {cap}
                    )
                    BEGIN
                        SELECT RAISE(ABORT, '상품 이율이 해당 국적의 법정최고이자를 초과합니다.');
                    END''')
        loan_cap = """COALESCE(
            (SELECT c.legal_max_rate FROM country_currencies c
             WHERE c.nationality=NEW.rate_nationality),
            (SELECT MIN(c.legal_max_rate) FROM liability p JOIN country_currencies c
             ON c.currency_code=p.quote_currency WHERE p.product_code=NEW.product_code),
            CAST((SELECT value FROM settings WHERE key='legal_max_rate') AS REAL),20)"""
        for suffix, operation in (("insert", "INSERT"),
                                  ("update", "UPDATE OF contract_rate,rate_nationality,product_code")):
            trigger = f"trg_loans_legal_max_{suffix}"
            self.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
            self.execute(f'''CREATE TRIGGER "{trigger}" BEFORE {operation} ON loans
                WHEN NEW.contract_rate IS NOT NULL AND
                  (NEW.contract_rate<0 OR NEW.contract_rate>{loan_cap})
                BEGIN
                    SELECT RAISE(ABORT, '대출 계약 이율이 해당 국적의 법정최고이자를 초과합니다.');
                END''')

        for name in ("trg_country_legal_max_insert", "trg_country_legal_max_update"):
            self.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        clamp = """
            UPDATE liability SET interest_rate=MIN(interest_rate,NEW.legal_max_rate)
                WHERE rate_nationality=NEW.nationality;
            UPDATE liability SET interest_rate=MIN(interest_rate,
                (SELECT MIN(c.legal_max_rate) FROM country_currencies c
                 WHERE c.currency_code=liability.quote_currency))
                WHERE rate_nationality IS NULL AND quote_currency=NEW.currency_code;
            UPDATE loans SET contract_rate=MIN(contract_rate,NEW.legal_max_rate),
                contract_effective_rate=CASE WHEN contract_rate>NEW.legal_max_rate THEN NULL
                    ELSE contract_effective_rate END,
                contract_apr=CASE WHEN contract_rate>NEW.legal_max_rate THEN NULL ELSE contract_apr END
                WHERE rate_nationality=NEW.nationality;
        """
        self.execute(f'''CREATE TRIGGER trg_country_legal_max_insert
            AFTER INSERT ON country_currencies BEGIN {clamp} END''')
        self.execute(f'''CREATE TRIGGER trg_country_legal_max_update
            AFTER UPDATE OF legal_max_rate, currency_code ON country_currencies
            BEGIN {clamp} END''')
        self.execute('DROP TRIGGER IF EXISTS "trg_settings_legal_max_clamp"')
        self.execute('''CREATE TRIGGER trg_settings_legal_max_clamp
            AFTER UPDATE OF value ON settings WHEN NEW.key='legal_max_rate'
            BEGIN
                UPDATE liability SET interest_rate=MIN(interest_rate,COALESCE(
                    (SELECT MIN(c.legal_max_rate) FROM country_currencies c
                     WHERE c.currency_code=liability.quote_currency),CAST(NEW.value AS REAL)));
                UPDATE loans SET contract_rate=MIN(contract_rate,COALESCE(
                    (SELECT c.legal_max_rate FROM country_currencies c
                     WHERE c.nationality=loans.rate_nationality),CAST(NEW.value AS REAL))),
                    contract_effective_rate=NULL,contract_apr=NULL;
            END''')

        # Bring legacy rows under the new cap once during schema initialization.
        self.execute("""UPDATE liability SET interest_rate=MIN(interest_rate,COALESCE(
            (SELECT MIN(c.legal_max_rate) FROM country_currencies c
             WHERE c.currency_code=liability.quote_currency),20))""")
        self.execute("""UPDATE loans SET contract_rate=MIN(contract_rate,COALESCE(
            (SELECT c.legal_max_rate FROM country_currencies c
             WHERE c.nationality=loans.rate_nationality),20))
            WHERE contract_rate IS NOT NULL AND rate_nationality IS NOT NULL""")

    def _init_floating_rate_pricing(self):
        """Validate floating terms and keep policy-linked legacy products live."""
        for table in ("saving", "liability"):
            self.execute(f"""UPDATE {table} SET rate_nationality=(
                SELECT MIN(c.nationality) FROM country_currencies c
                WHERE c.currency_code={table}.quote_currency)
                WHERE rate_nationality IS NULL AND 1=(
                    SELECT COUNT(*) FROM country_currencies c
                    WHERE c.currency_code={table}.quote_currency)""")
            validation_insert = f"trg_{table}_floating_terms_validate_insert"
            validation_update = f"trg_{table}_floating_terms_validate_update"
            insert_reprice = f"trg_{table}_floating_reprice_insert"
            update_reprice = f"trg_{table}_floating_reprice_update"
            for trigger in (validation_insert, validation_update, insert_reprice, update_reprice):
                self.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
            if table == "liability":
                new_costs = ("+NEW.maturity_spread+NEW.promotional_adjustment+COALESCE((SELECT "
                             "funding_spread+operating_cost_spread+liquidity_spread+target_margin+"
                             "competition_adjustment FROM financial_institutions WHERE issuer=NEW.issuer),1.7)")
                row_costs = "+maturity_spread+promotional_adjustment+1.7"
            else:
                new_costs = ("+NEW.maturity_spread+NEW.promotional_adjustment+COALESCE((SELECT "
                             "competition_adjustment FROM financial_institutions WHERE issuer=NEW.issuer),0)")
                row_costs = "+maturity_spread+promotional_adjustment"
            invalid = """NEW.rate_type='floating' AND (
                    NEW.rate_nationality IS NULL
                    OR NOT EXISTS (SELECT 1 FROM country_currencies c
                        WHERE c.nationality=NEW.rate_nationality
                          AND c.currency_code=NEW.quote_currency)
                    OR NEW.rate_floor<0 OR NEW.rate_floor>100
                    OR NEW.rate_cap IS NOT NULL AND
                       (NEW.rate_cap<NEW.rate_floor OR NEW.rate_cap>100)
                    OR NEW.benchmark_key NOT IN ('policy','funding','bank_bond','treasury')
                    OR NEW.benchmark_key<>'policy' AND NEW.benchmark_tenor_days<=0
                    OR NEW.repricing_lag_seconds<0
                    OR NEW.repricing_cycle_seconds<=0 OR NEW.interest_cycle<=0
                    OR NEW.repricing_cycle_seconds<NEW.interest_cycle
                    OR ABS(NEW.repricing_cycle_seconds/NEW.interest_cycle
                           - ROUND(NEW.repricing_cycle_seconds/NEW.interest_cycle))>0.000000001
                )"""
            self.execute(f'''CREATE TRIGGER "{validation_insert}"
                BEFORE INSERT ON "{table}" WHEN {invalid} BEGIN
                    SELECT RAISE(ABORT, '변동금리 국적·하한·상한·재산정주기 설정을 확인해주세요.');
                END''')
            self.execute(f'''CREATE TRIGGER "{validation_update}"
                BEFORE UPDATE OF rate_type,rate_nationality,base_rate_spread,rate_floor,
                    rate_cap,repricing_cycle_seconds,interest_cycle,quote_currency,benchmark_key,
                    benchmark_tenor_days,repricing_lag_seconds ON "{table}"
                WHEN {invalid} BEGIN
                    SELECT RAISE(ABORT, '변동금리 국적·하한·상한·재산정주기 설정을 확인해주세요.');
                END''')
            self.execute(f'''CREATE TRIGGER "{insert_reprice}"
                AFTER INSERT ON "{table}"
                WHEN NEW.rate_type='floating' AND NEW.benchmark_key='policy' BEGIN
                    UPDATE "{table}" SET interest_rate=MIN(
                        {"(SELECT legal_max_rate FROM country_currencies WHERE nationality=NEW.rate_nationality)," if table == "liability" else ""}
                        COALESCE(NEW.rate_cap,100),
                        MAX(NEW.rate_floor,(SELECT base_rate FROM country_currencies
                            WHERE nationality=NEW.rate_nationality)+NEW.base_rate_spread{new_costs}))
                    WHERE product_code=NEW.product_code;
                END''')
            self.execute(f'''CREATE TRIGGER "{update_reprice}"
                AFTER UPDATE OF rate_type,rate_nationality,base_rate_spread,rate_floor,
                    rate_cap,repricing_cycle_seconds,quote_currency,benchmark_key,
                    benchmark_tenor_days,maturity_spread,promotional_adjustment ON "{table}"
                WHEN NEW.rate_type='floating' AND NEW.benchmark_key='policy' BEGIN
                    UPDATE "{table}" SET interest_rate=MIN(
                        {"(SELECT legal_max_rate FROM country_currencies WHERE nationality=NEW.rate_nationality)," if table == "liability" else ""}
                        COALESCE(NEW.rate_cap,100),
                        MAX(NEW.rate_floor,(SELECT base_rate FROM country_currencies
                            WHERE nationality=NEW.rate_nationality)+NEW.base_rate_spread{new_costs}))
                    WHERE product_code=NEW.product_code;
                END''')

        # Reprice direct SQL edits to policy rates as well as command-driven edits.
        self.execute('DROP TRIGGER IF EXISTS "trg_country_floating_reprice"')
        self.execute('''CREATE TRIGGER trg_country_floating_reprice
            AFTER UPDATE OF base_rate,legal_max_rate ON country_currencies BEGIN
                UPDATE saving SET interest_rate=MIN(COALESCE(rate_cap,100),
                    MAX(CASE WHEN negative_rate_allowed=1 THEN -100 ELSE rate_floor END,
                        NEW.base_rate+base_rate_spread+maturity_spread+
                        promotional_adjustment+COALESCE((SELECT competition_adjustment
                        FROM financial_institutions WHERE issuer=saving.issuer),0)))
                    WHERE rate_type='floating' AND benchmark_key='policy'
                      AND rate_nationality=NEW.nationality;
                UPDATE liability SET interest_rate=MIN(NEW.legal_max_rate,COALESCE(rate_cap,100),
                    MAX(rate_floor,NEW.base_rate+base_rate_spread+maturity_spread+
                        promotional_adjustment+COALESCE((SELECT funding_spread+operating_cost_spread+
                        liquidity_spread+target_margin+competition_adjustment FROM financial_institutions
                        WHERE issuer=liability.issuer),1.7)))
                    WHERE rate_type='floating' AND benchmark_key='policy'
                      AND rate_nationality=NEW.nationality;
            END''')
        # Normalize rows created before this migration or edited while triggers were absent.
        for table in ("saving", "liability"):
            self.execute(f'''UPDATE {table} SET interest_rate=MIN(
                {"(SELECT legal_max_rate FROM country_currencies WHERE nationality=" + table + ".rate_nationality)," if table == "liability" else ""}COALESCE(rate_cap,100),
                MAX(rate_floor,(SELECT base_rate FROM country_currencies
                    WHERE nationality={table}.rate_nationality)+base_rate_spread{row_costs}))
                WHERE rate_type='floating' AND benchmark_key='policy'
                  AND rate_nationality IS NOT NULL''')
        # Existing contracts keep their old fixed-rate character, but retain the
        # issuing nationality so a statutory maximum can still be enforced.
        self.execute("""UPDATE accounts SET rate_nationality=(
            SELECT p.rate_nationality FROM saving p
            WHERE p.product_code=accounts.product_code)
            WHERE rate_nationality IS NULL""")
        self.execute("""UPDATE loans SET rate_nationality=(
            SELECT p.rate_nationality FROM liability p
            WHERE p.product_code=loans.product_code)
            WHERE rate_nationality IS NULL""")
        self.execute("""UPDATE loans SET contract_rate=MIN(contract_rate,
            (SELECT legal_max_rate FROM country_currencies c
             WHERE c.nationality=loans.rate_nationality))
            WHERE contract_rate IS NOT NULL AND rate_nationality IS NOT NULL""")

    def _migrate_currency_wallets(self):
        """Merge the removed wallet store into home cash or currency accounts.

        ``users.cash`` is denominated by the user's nationality.  Every other
        currency belongs to exactly one ``currency_saving`` account.
        """
        exists = self.execute_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='currency_wallets'"
        )
        if not exists:
            return
        with self.lock, self.conn:
            def foreign_account(user_id, currency_code, units):
                account = self.conn.execute(
                    "SELECT account_number,balance_units FROM accounts "
                    "WHERE user_id=? AND quote_currency=? AND account_type='currency_saving'",
                    (user_id, currency_code),
                ).fetchone()
                if account:
                    if int(account['balance_units']) + units > 2**63 - 1:
                        raise RuntimeError('외환통장 이관 잔액이 저장 한도를 초과합니다.')
                    self.conn.execute(
                        "UPDATE accounts SET balance_units=balance_units+? WHERE account_number=?",
                        (units, account['account_number']),
                    )
                    return
                product = self.conn.execute(
                    "SELECT product_code FROM saving WHERE product_kind='currency_saving' "
                    "AND quote_currency=? ORDER BY status='active' DESC,product_code LIMIT 1",
                    (currency_code,),
                ).fetchone()
                if not product:
                    product_code = f"SYSFX_{currency_code}"
                    self.conn.execute(
                        "INSERT OR IGNORE INTO saving(product_code,name,interest_rate,deposit_amount,"
                        "maturity_days,interest_cycle,compound_type,issuer,status,quote_currency,product_kind) "
                        "VALUES (?,?,0,0,0,86400,0,'system','active',?,'currency_saving')",
                        (product_code, f"{currency_code} 외환통장", currency_code),
                    )
                else:
                    product_code = product['product_code']
                self.conn.execute(
                    "INSERT INTO accounts(account_number,user_id,product_code,balance,password,"
                    "last_payment_epoch,principal,account_type,quote_currency,balance_units) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    ('FXA' + uuid.uuid4().hex[:16].upper(), user_id, product_code,
                     0, None, time.time(), 0, 'currency_saving', currency_code, units),
                )

            # In the legacy schema users.cash was always NWX, even for citizens
            # whose nationality mapped to another currency.
            legacy_cash = self.conn.execute("""SELECT u.user_id,u.cash,n.currency_code home_currency
                FROM users u JOIN country_currencies n ON n.nationality=u.nationality
                WHERE n.currency_code<>'NWX' AND u.cash<>0""").fetchall()
            for row in legacy_cash:
                foreign_account(row['user_id'], 'NWX', int(row['cash']) * SCALE)
                self.conn.execute('UPDATE users SET cash=0 WHERE user_id=?', (row['user_id'],))

            rows = self.conn.execute(
                "SELECT w.user_id,w.currency_code,w.balance_units,"
                "COALESCE(n.currency_code,(SELECT value FROM settings WHERE key='default_currency')) home_currency "
                "FROM currency_wallets w JOIN users u ON u.user_id=w.user_id "
                "LEFT JOIN country_currencies n ON n.nationality=u.nationality "
                "WHERE w.balance_units<>0 ORDER BY w.user_id,w.currency_code"
            ).fetchall()
            for row in rows:
                units = int(row['balance_units'])
                if row['currency_code'] == row['home_currency']:
                    if units % SCALE:
                        raise RuntimeError(
                            f"{row['user_id']}의 국적 통화 {row['currency_code']} 잔액에 "
                            "1 미만 단위가 있어 users.cash로 이관할 수 없습니다."
                        )
                    self.conn.execute(
                        "UPDATE users SET cash=cash+? WHERE user_id=?",
                        (units // SCALE, row['user_id']),
                    )
                    continue
                foreign_account(row['user_id'], row['currency_code'], units)
            self.conn.execute("DROP TABLE currency_wallets")

    def _normalize_home_currency_accounts(self):
        """Move obsolete home-currency FX accounts into nationality cash."""
        with self.lock, self.conn:
            rows = self.conn.execute("""SELECT a.account_number,a.user_id,a.balance_units,a.quote_currency
                FROM accounts a JOIN users u ON u.user_id=a.user_id
                JOIN country_currencies n ON n.nationality=u.nationality
                WHERE a.account_type='currency_saving' AND a.quote_currency=n.currency_code""").fetchall()
            for row in rows:
                units = int(row['balance_units'] or 0)
                if units % SCALE:
                    raise RuntimeError(
                        f"{row['user_id']}의 국적 통화 계좌 {row['account_number']}에 "
                        "1 미만 단위가 있어 users.cash로 이관할 수 없습니다."
                    )
                if units:
                    self.conn.execute('UPDATE users SET cash=cash+? WHERE user_id=?',
                                      (units//SCALE,row['user_id']))
                self.conn.execute("UPDATE accounts SET balance_units=0,account_type='home_currency_migrated' "
                                  "WHERE account_number=?",(row['account_number'],))

    def _migrate_currency_identifiers(self):
        """Move legacy currency product/exchange identifiers to the current namespace."""
        legacy_exchange = 'F' + 'X'
        legacy_asset_prefix = legacy_exchange + '_%'
        legacy_product_prefix = 'INZ' + legacy_exchange + '%'
        self.execute(
            "DELETE FROM market_exchanges WHERE exchange_id=? "
            "AND EXISTS (SELECT 1 FROM market_exchanges WHERE exchange_id='CURRENCY')",
            (legacy_exchange,),
        )
        tables = [row[0] for row in self.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        pairs = list(self.execute("SELECT code, 'CUR_' || substr(code, 4) FROM market_assets WHERE code LIKE ?", (legacy_asset_prefix,)))
        for table in tables:
            columns = {row[1] for row in self.execute(f'PRAGMA table_info("{table}")')}
            if 'exchange_id' in columns:
                self.execute(f'UPDATE "{table}" SET exchange_id=? WHERE exchange_id=?', ('CURRENCY', legacy_exchange))
            if 'asset_code' in columns:
                for old, new in pairs:
                    self.execute(f'UPDATE "{table}" SET asset_code=? WHERE asset_code=?', (new, old))
            if 'product_code' in columns:
                self.execute(f"UPDATE \"{table}\" SET product_code='INZCU' || substr(product_code, 6) WHERE product_code LIKE ?", (legacy_product_prefix,))
        for old, new in pairs:
            self.execute('UPDATE market_assets SET code=? WHERE code=?', (new, old))

    def _seed_liquidity_states(self):
        """Take one neutral supply snapshot for currencies without a baseline."""
        from database.user_db import TREASURY_USER_ID

        for row in self.execute("SELECT code FROM currencies"):
            code = str(row[0])
            home_cash = """SELECT COALESCE(SUM(u.cash),0) FROM users u
                JOIN country_currencies n ON n.nationality=u.nationality
                WHERE n.currency_code=?"""
            from decimal import Decimal
            total = int(Decimal(str(self.execute(home_cash, (code,))[0][0] or 0)) * SCALE)
            total += int(self.execute("SELECT COALESCE(SUM(balance_units),0) FROM accounts WHERE quote_currency=?", (code,))[0][0])
            treasury_rows = self.execute("""SELECT COALESCE(u.cash,0) FROM users u
                JOIN country_currencies n ON n.nationality=u.nationality
                WHERE u.user_id=? AND n.currency_code=?""", (TREASURY_USER_ID, code))
            treasury = int(Decimal(str(treasury_rows[0][0] if treasury_rows else 0)) * SCALE)
            treasury += int(self.execute("SELECT COALESCE(SUM(balance_units),0) FROM accounts WHERE quote_currency=? AND user_id=?", (code, TREASURY_USER_ID))[0][0])
            ratio = max(0, total - treasury) / total if total > 0 else 0
            self.execute(
                "INSERT OR IGNORE INTO currency_liquidity_state "
                "(currency_code,baseline_supply_units,baseline_circulation_ratio) VALUES (?,?,?)",
                (code, total, ratio),
            )
