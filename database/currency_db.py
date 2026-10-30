from functions.money import base_currency_code
from functions.currency_config import load_initial_currency_config
import re
import time
import uuid

from .db_manager import DBManager

SCALE = 1_000_000

class CurrencyDB(DBManager):
    def __init__(self):
        super().__init__("forex")
        initial_config = load_initial_currency_config()
        initial_base = initial_config["baseCurrency"]
        initial_base_code = str(initial_base["code"]).strip().upper()
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
        self.execute("INSERT OR IGNORE INTO currencies(code,name,decimal_places) VALUES (?,?,0)",
                     (initial_base_code, str(initial_base["name"]).strip()))
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
        self.execute("INSERT OR IGNORE INTO country_currencies(nationality,currency_code) VALUES (?,?)",
                     (str(initial_base["nationality"]).strip(), initial_base_code))
        self.execute("""CREATE TABLE IF NOT EXISTS currency_system_settings (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
            base_currency_code TEXT NOT NULL REFERENCES currencies(code),
            updated_epoch INTEGER NOT NULL DEFAULT 0
        )""")
        self.execute("""INSERT OR IGNORE INTO currency_system_settings(singleton_id,base_currency_code)
            SELECT 1,currency_code FROM country_currencies WHERE nationality=?""",
                     (str(initial_base["nationality"]).strip(),))
        self.execute("""CREATE TRIGGER IF NOT EXISTS currency_system_settings_base_precision
            BEFORE UPDATE OF base_currency_code ON currency_system_settings
            WHEN COALESCE((SELECT decimal_places FROM currencies
                           WHERE code=NEW.base_currency_code),-1)<>0
            BEGIN SELECT RAISE(ABORT,'기본 통화는 현재 정수 잔액 경로를 위해 0자리 통화여야 합니다.'); END""")
        self.execute("UPDATE currencies SET decimal_places=0 WHERE code=base_currency_code()")
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
            amount_minor_units INTEGER NOT NULL DEFAULT 0 CHECK(amount_minor_units >= 0),
            amount_decimal_places INTEGER NOT NULL DEFAULT 0 CHECK(amount_decimal_places BETWEEN 0 AND 6),
            status TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open','reviewing','closed')),
            outcome_code TEXT,
            opened_epoch REAL NOT NULL,
            reviewed_by TEXT,
            reviewed_epoch REAL,
            UNIQUE(user_id,rule_code,reference_key)
        )""")
        aml_columns = {row[1] for row in self.execute("PRAGMA table_info(aml_alerts)")}
        for column, declaration in (
            ("amount_minor_units", "INTEGER NOT NULL DEFAULT 0 CHECK(amount_minor_units >= 0)"),
            ("amount_decimal_places", "INTEGER NOT NULL DEFAULT 0 CHECK(amount_decimal_places BETWEEN 0 AND 6)"),
        ):
            if column not in aml_columns:
                self.execute(f"ALTER TABLE aml_alerts ADD COLUMN {column} {declaration}")
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
            cash_minor_units INTEGER,
            cash_decimal_places INTEGER,
            cash_currency_code TEXT,
            alerted_epoch REAL NOT NULL
        )""")
        deficit_columns = {row[1] for row in self.execute("PRAGMA table_info(bank_deficit_alerts)")}
        for column, declaration in (
            ("cash_minor_units", "INTEGER"),
            ("cash_decimal_places", "INTEGER"),
            ("cash_currency_code", "TEXT"),
        ):
            if column not in deficit_columns:
                self.execute(f"ALTER TABLE bank_deficit_alerts ADD COLUMN {column} {declaration}")
        self._ensure_bank_deficit_alert_units()
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
                self.execute(f"ALTER TABLE {table} ADD COLUMN quote_currency TEXT")
        self._init_legal_rate_caps()
        self._init_floating_rate_pricing()
        self._seed_liquidity_states()
        self._init_currency_precision_triggers()
        self._remove_legacy_currency_defaults()

    def base_currency_code(self):
        from functions.money import base_currency_code
        return base_currency_code(self.conn)

    def _remove_legacy_currency_defaults(self, targets=None):
        """Rebuild legacy tables whose currency columns defaulted to a fixed code.

        SQLite cannot alter a column default in place. Recreating only affected
        tables preserves their rows, indexes, triggers, keys, and AUTOINCREMENT
        sequence while allowing the post-insert base-currency triggers to apply.
        """
        targets = targets or {
            "accounts": {"quote_currency"}, "loans": {"quote_currency"},
            "guarantee_claims": {"quote_currency"}, "market_assets": {"quote_currency"},
            "company": {"quote_currency"}, "investment": {"quote_currency"},
            "saving": {"quote_currency"}, "liability": {"quote_currency"},
            "market_orders": {"quote_currency"}, "tax_transactions": {"currency_code"},
            "financial_institution_ledger": {"currency_code", "base_currency_code"},
        }
        self.execute("PRAGMA foreign_keys=OFF")
        try:
            for table, currency_columns in targets.items():
                self._recover_currency_rebuild(table)
                row = self.execute_one(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                )
                if not row or not row[0]:
                    continue
                ddl = str(row[0])
                columns = {item[1] for item in self.execute(f'PRAGMA table_info("{table}")')}
                replacements = {}
                for column in currency_columns & columns:
                    pattern = re.compile(
                        rf'(?i)(["`\[]?{re.escape(column)}["`\]]?\s+)TEXT\s+NOT\s+NULL\s+DEFAULT\s+[\'\"]NWX[\'\"]'
                    )
                    ddl, count = pattern.subn(r'\1TEXT', ddl)
                    replacements[column] = count
                if not any(replacements.values()):
                    continue

                indexes_triggers = self.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name=? "
                    "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name",
                    (table,),
                )
                sequence = None
                if self.execute_one("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"):
                    seq_row = self.execute_one("SELECT seq FROM sqlite_sequence WHERE name=?", (table,))
                    sequence = int(seq_row[0]) if seq_row else None
                temp = f"__currency_migrate_{table}"
                escaped = table.replace('"', '""')
                ddl = re.sub(
                    rf'(?i)^(CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+)["`\[]?{re.escape(table)}["`\]]?',
                    rf'\1"{temp}"', ddl.strip(), count=1,
                )
                names = ",".join(f'"{name.replace(chr(34), chr(34)*2)}"' for name in columns)
                with self.lock:
                    # Other tables' triggers name this table, and it is briefly gone between
                    # DROP and RENAME; legacy_alter_table stops the rename re-checking them.
                    # One transaction per table: a failure leaves the original untouched.
                    self.conn.execute("PRAGMA legacy_alter_table=ON")
                    try:
                        with self.conn:
                            self.conn.execute("BEGIN")
                            self.conn.execute(ddl)
                            self.conn.execute(f'INSERT INTO "{temp}" ({names}) SELECT {names} FROM "{escaped}"')
                            self.conn.execute(f'DROP TABLE "{escaped}"')
                            self.conn.execute(f'ALTER TABLE "{temp}" RENAME TO "{escaped}"')
                            if sequence is not None:
                                self.conn.execute("DELETE FROM sqlite_sequence WHERE name IN (?,?)", (table, temp))
                                self.conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (table, sequence))
                            for (statement,) in indexes_triggers:
                                self.conn.execute(statement)
                    finally:
                        self.conn.execute("PRAGMA legacy_alter_table=OFF")
        finally:
            self.execute("PRAGMA foreign_keys=ON")

    def _recover_currency_rebuild(self, table):
        """Settle a copy left by an interrupted rebuild of ``table``.

        Rebuilds before the transaction above ran step by step, so a failed
        rename could leave the rows only in the copy, or the original dropped
        and later recreated empty by its own table set-up.
        """
        temp = f"__currency_migrate_{table}"
        if not self.execute_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (temp,)):
            return
        escaped = table.replace('"', '""')
        original = self.execute_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
        copied = self.execute_one(f'SELECT COUNT(*) FROM "{temp}"')[0]
        restore = not original or (copied and not self.execute_one(f'SELECT COUNT(*) FROM "{escaped}"')[0])
        indexes_triggers = self.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name",
            (table,),
        ) if restore and original else []
        with self.lock:
            self.conn.execute("PRAGMA legacy_alter_table=ON")
            try:
                with self.conn:
                    self.conn.execute("BEGIN")
                    if restore:
                        # The copy holds the rows; the original is gone or an empty re-creation.
                        if original:
                            self.conn.execute(f'DROP TABLE "{escaped}"')
                        self.conn.execute(f'ALTER TABLE "{temp}" RENAME TO "{escaped}"')
                        for (statement,) in indexes_triggers:
                            self.conn.execute(statement)
                    else:
                        # The original still holds the rows; the copy is a stale partial one.
                        self.conn.execute(f'DROP TABLE "{temp}"')
            finally:
                self.conn.execute("PRAGMA legacy_alter_table=OFF")

    def _init_currency_precision_triggers(self):
        """Replace legacy whole-NWX rounding with denomination-aware rounding."""
        loan_columns = {row[1] for row in self.execute("PRAGMA table_info(loans)")}
        for legacy, minor in (("principal", "principal_minor_units"),
                              ("outstanding_balance", "outstanding_balance_minor_units"),
                              ("ecl_allowance", "ecl_allowance_minor_units"),
                              ("charged_off_balance", "charged_off_balance_minor_units")):
            if minor not in loan_columns:
                self.execute(f"ALTER TABLE loans ADD COLUMN {minor} INTEGER NOT NULL DEFAULT 0")
        # Remaining loan money accounts also need canonical denomination units;
        # snapshots alone cannot serve as the live accounting value.
        loan_money_columns = {row[1] for row in self.execute("PRAGMA table_info(loans)")}
        loan_money_fields = (
            ("origination_fee", "origination_fee_minor_units"),
            ("mandatory_cost_fee", "mandatory_cost_fee_minor_units"),
            ("collateral_amount", "collateral_amount_minor_units"),
            ("collateral_released", "collateral_released_minor_units"),
            ("collateral_seized", "collateral_seized_minor_units"),
            ("collateral_asset_value", "collateral_asset_value_minor_units"),
            ("collateral_liquidated_value", "collateral_liquidated_value_minor_units"),
            ("provisioned_loss", "provisioned_loss_minor_units"),
            ("underwriting_income", "underwriting_income_minor_units"),
            ("prepayment_fees_paid", "prepayment_fees_paid_minor_units"),
        )
        for _legacy, minor in loan_money_fields:
            if minor not in loan_money_columns:
                self.execute(f"ALTER TABLE loans ADD COLUMN {minor} INTEGER NOT NULL DEFAULT 0")
        ecl_columns = {row[1] for row in self.execute(
            "PRAGMA table_info(loan_ecl_assessments)"
        )}
        for column in ("ead_minor_units", "expected_loss_minor_units"):
            if column not in ecl_columns:
                self.execute(f"ALTER TABLE loan_ecl_assessments ADD COLUMN {column} INTEGER")
        collateral_columns = {row[1] for row in self.execute(
            "PRAGMA table_info(loan_collateral_assets)"
        )}
        collateral_amount_fields = (
            ("loan_collateral_assets", collateral_columns,
             ("average_cost", "last_price", "last_value", "liquidation_price",
              "liquidation_cost", "liquidation_proceeds")),
            ("collateral_liquidation_events",
             {row[1] for row in self.execute("PRAGMA table_info(collateral_liquidation_events)")},
             ("mark_price", "execution_price", "gross_proceeds", "liquidation_cost", "net_proceeds")),
        )
        for table, columns, fields in collateral_amount_fields:
            for field in fields:
                minor = f"{field}_minor_units"
                if minor not in columns:
                    self.execute(
                        f"ALTER TABLE {table} ADD COLUMN {minor} INTEGER NOT NULL DEFAULT 0"
                    )
        from functions.money import to_minor_units
        for event in self.execute(
            "SELECT e.event_id,e.mark_price,e.execution_price,e.mark_price_minor_units,"
            "e.execution_price_minor_units,l.quote_currency "
            "FROM collateral_liquidation_events e JOIN loans l USING(loan_id)"
        ):
            places = currency_precision(self.conn, event["quote_currency"] or base_currency_code(self.conn))
            updates = []
            if not int(event["mark_price_minor_units"] or 0) and float(event["mark_price"] or 0):
                updates.append(("mark_price_minor_units", to_minor_units(event["mark_price"], places)))
            if not int(event["execution_price_minor_units"] or 0) and float(event["execution_price"] or 0):
                updates.append(("execution_price_minor_units", to_minor_units(event["execution_price"], places)))
            if updates:
                assignments = ",".join(f"{field}=?" for field, _ in updates)
                self.execute(
                    f"UPDATE collateral_liquidation_events SET {assignments} WHERE event_id=?",
                    (*(value for _, value in updates), event["event_id"]),
                )
        liquidation_scale = (
            "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code="
            "(SELECT quote_currency FROM loans WHERE loan_id=OLD.loan_id)),0) "
            "WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
        )
        liquidation_places = (
            "COALESCE((SELECT decimal_places FROM currencies WHERE code="
            "(SELECT quote_currency FROM loans WHERE loan_id=OLD.loan_id)),0)"
        )
        def normalized_old(field):
            value = f"COALESCE(OLD.{field},0)"
            return (f"ROUND(({value})+CASE WHEN ({value})>=0 THEN 1e-12 ELSE -1e-12 END,"
                    f"{liquidation_places})")
        self.execute("DROP TRIGGER IF EXISTS trg_collateral_liquidation_events_immutable_update")
        self.execute(f"""CREATE TRIGGER trg_collateral_liquidation_events_immutable_update
            BEFORE UPDATE ON collateral_liquidation_events
            WHEN OLD.loan_id IS NOT NEW.loan_id OR OLD.collateral_id IS NOT NEW.collateral_id
              OR OLD.asset_code IS NOT NEW.asset_code OR OLD.quantity IS NOT NEW.quantity
              OR (NEW.mark_price IS NOT {normalized_old('mark_price')}
                  AND NEW.mark_price IS NOT OLD.mark_price)
              OR (NEW.execution_price IS NOT {normalized_old('execution_price')}
                  AND NEW.execution_price IS NOT OLD.execution_price)
              OR (NEW.mark_price_minor_units IS NOT CAST(ROUND(
                COALESCE(OLD.mark_price,0)*({liquidation_scale})) AS INTEGER)
                AND NEW.mark_price_minor_units IS NOT OLD.mark_price_minor_units)
              OR (NEW.execution_price_minor_units IS NOT CAST(ROUND(
                COALESCE(OLD.execution_price,0)*({liquidation_scale})) AS INTEGER)
                AND NEW.execution_price_minor_units IS NOT OLD.execution_price_minor_units)
              OR (NEW.gross_proceeds IS NOT {normalized_old('gross_proceeds')}
                  AND NEW.gross_proceeds IS NOT OLD.gross_proceeds)
              OR (NEW.liquidation_cost IS NOT {normalized_old('liquidation_cost')}
                  AND NEW.liquidation_cost IS NOT OLD.liquidation_cost)
              OR (NEW.net_proceeds IS NOT {normalized_old('net_proceeds')}
                  AND NEW.net_proceeds IS NOT OLD.net_proceeds)
              OR (NEW.gross_proceeds_minor_units IS NOT CAST(ROUND(
                COALESCE(OLD.gross_proceeds,0)*({liquidation_scale})) AS INTEGER)
                AND NEW.gross_proceeds_minor_units IS NOT OLD.gross_proceeds_minor_units)
              OR (NEW.liquidation_cost_minor_units IS NOT CAST(ROUND(
                COALESCE(OLD.liquidation_cost,0)*({liquidation_scale})) AS INTEGER)
                AND NEW.liquidation_cost_minor_units IS NOT OLD.liquidation_cost_minor_units)
              OR (NEW.net_proceeds_minor_units IS NOT CAST(ROUND(
                COALESCE(OLD.net_proceeds,0)*({liquidation_scale})) AS INTEGER)
                AND NEW.net_proceeds_minor_units IS NOT OLD.net_proceeds_minor_units)
              OR OLD.book_filled_quantity IS NOT NEW.book_filled_quantity
              OR OLD.backstop_quantity IS NOT NEW.backstop_quantity
              OR OLD.liquidated_epoch IS NOT NEW.liquidated_epoch
            BEGIN SELECT RAISE(ABORT,'collateral liquidation events are append-only'); END""")
        self.execute("DROP TRIGGER IF EXISTS trg_collateral_liquidation_events_immutable_delete")
        self.execute("""CREATE TRIGGER trg_collateral_liquidation_events_immutable_delete
            BEFORE DELETE ON collateral_liquidation_events
            BEGIN SELECT RAISE(ABORT,'collateral liquidation events are append-only'); END""")
        new_liquidation_scale = (
            "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code="
            "(SELECT quote_currency FROM loans WHERE loan_id=NEW.loan_id)),0) "
            "WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
        )
        self.execute("DROP TRIGGER IF EXISTS trg_collateral_liquidation_price_minor_insert")
        self.execute(f"""CREATE TRIGGER trg_collateral_liquidation_price_minor_insert
            AFTER INSERT ON collateral_liquidation_events BEGIN
              UPDATE collateral_liquidation_events SET
                mark_price_minor_units=CAST(ROUND(COALESCE(NEW.mark_price,0)*({new_liquidation_scale})) AS INTEGER),
                execution_price_minor_units=CAST(ROUND(COALESCE(NEW.execution_price,0)*({new_liquidation_scale})) AS INTEGER)
              WHERE event_id=NEW.event_id;
            END""")
        from functions.money import currency_precision, to_minor_units
        for loan in self.execute(
                "SELECT loan_id,quote_currency,principal,principal_minor_units,"
                "outstanding_balance,outstanding_balance_minor_units,ecl_allowance,"
                "ecl_allowance_minor_units,charged_off_balance,"
                "charged_off_balance_minor_units FROM loans"):
            places = currency_precision(self.conn, loan["quote_currency"] or base_currency_code(self.conn))
            for legacy, minor in (("principal", "principal_minor_units"),
                                  ("outstanding_balance", "outstanding_balance_minor_units"),
                                  ("ecl_allowance", "ecl_allowance_minor_units"),
                                  ("charged_off_balance", "charged_off_balance_minor_units")):
                if int(loan[minor] or 0) == 0 and float(loan[legacy] or 0) != 0:
                    self.execute(
                        f"UPDATE loans SET {minor}=? WHERE loan_id=?",
                        (to_minor_units(loan[legacy], places), loan["loan_id"]),
                    )
        loan_scale = "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0) " \
                     "WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
        for legacy, minor in (("principal", "principal_minor_units"),
                              ("outstanding_balance", "outstanding_balance_minor_units"),
                              ("ecl_allowance", "ecl_allowance_minor_units"),
                              ("charged_off_balance", "charged_off_balance_minor_units")):
            self.execute(f"DROP TRIGGER IF EXISTS trg_loan_{minor}_from_legacy")
            self.execute(f"DROP TRIGGER IF EXISTS trg_loan_{minor}_from_legacy_update")
            self.execute(f"DROP TRIGGER IF EXISTS trg_loan_{minor}_to_legacy")
            self.execute(f"""CREATE TRIGGER trg_loan_{minor}_from_legacy
                AFTER INSERT ON loans BEGIN
                    UPDATE loans SET {minor}=CAST(ROUND(COALESCE(NEW.{legacy},0)*({loan_scale})) AS INTEGER)
                    WHERE loan_id=NEW.loan_id;
                END""")
            self.execute(f"""CREATE TRIGGER trg_loan_{minor}_from_legacy_update
                AFTER UPDATE OF {legacy},quote_currency ON loans BEGIN
                    UPDATE loans SET {minor}=CAST(ROUND(COALESCE(NEW.{legacy},0)*({loan_scale})) AS INTEGER)
                    WHERE loan_id=NEW.loan_id;
                END""")
            self.execute(f"""CREATE TRIGGER trg_loan_{minor}_to_legacy
                AFTER UPDATE OF {minor} ON loans BEGIN
                    UPDATE loans SET {legacy}=CAST(NEW.{minor} AS REAL)/({loan_scale})
                    WHERE loan_id=NEW.loan_id;
                END""")
        for legacy, minor in loan_money_fields:
            for suffix in ("from_legacy", "from_legacy_update", "to_legacy"):
                self.execute(f"DROP TRIGGER IF EXISTS trg_loan_{minor}_{suffix}")
            for loan in self.execute(
                f"SELECT loan_id,quote_currency,{legacy},{minor} FROM loans"
            ):
                places = currency_precision(
                    self.conn, loan["quote_currency"] or base_currency_code(self.conn)
                )
                if int(loan[minor] or 0) == 0 and float(loan[legacy] or 0) != 0:
                    self.execute(f"UPDATE loans SET {minor}=? WHERE loan_id=?",
                                 (to_minor_units(loan[legacy], places), loan["loan_id"]))
            self.execute(f"""CREATE TRIGGER trg_loan_{minor}_from_legacy
                AFTER INSERT ON loans BEGIN
                  UPDATE loans SET {minor}=CAST(ROUND(COALESCE(NEW.{legacy},0)*({loan_scale})) AS INTEGER)
                  WHERE loan_id=NEW.loan_id;
                END""")
            self.execute(f"""CREATE TRIGGER trg_loan_{minor}_from_legacy_update
                AFTER UPDATE OF {legacy},quote_currency ON loans BEGIN
                  UPDATE loans SET {minor}=CAST(ROUND(COALESCE(NEW.{legacy},0)*({loan_scale})) AS INTEGER)
                  WHERE loan_id=NEW.loan_id;
                END""")
            self.execute(f"""CREATE TRIGGER trg_loan_{minor}_to_legacy
                AFTER UPDATE OF {minor} ON loans BEGIN
                  UPDATE loans SET {legacy}=CAST(NEW.{minor} AS REAL)/({loan_scale})
                  WHERE loan_id=NEW.loan_id;
                END""")
        remainder_fields = (
            ("interest_remainder", "interest_remainder_subminor_units"),
            ("overdue_interest_remainder", "overdue_interest_remainder_subminor_units"),
        )
        for loan in self.execute(
                "SELECT loan_id,quote_currency,interest_remainder,"
                "interest_remainder_subminor_units,overdue_interest_remainder,"
                "overdue_interest_remainder_subminor_units FROM loans"):
            places = currency_precision(self.conn, loan["quote_currency"] or base_currency_code(self.conn))
            for legacy, subminor in remainder_fields:
                if int(loan[subminor] or 0) == 0 and float(loan[legacy] or 0) != 0:
                    self.execute(
                        f"UPDATE loans SET {subminor}=CAST(ROUND(?*?) AS INTEGER) "
                        "WHERE loan_id=?",
                        (float(loan[legacy]), (10 ** places)*1_000_000, loan["loan_id"]),
                    )
        remainder_scale = "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0) " \
                          "WHEN 0 THEN 1000000 WHEN 1 THEN 10000000 ELSE 100000000 END"
        for legacy, subminor in remainder_fields:
            stem = f"trg_loan_{subminor}"
            for suffix in ("from_legacy", "from_legacy_update", "to_legacy"):
                self.execute(f"DROP TRIGGER IF EXISTS {stem}_{suffix}")
            self.execute(f"""CREATE TRIGGER {stem}_from_legacy AFTER INSERT ON loans
                BEGIN UPDATE loans SET {subminor}=
                  CAST(ROUND(COALESCE(NEW.{legacy},0)*({remainder_scale})) AS INTEGER)
                  WHERE loan_id=NEW.loan_id; END""")
            self.execute(f"""CREATE TRIGGER {stem}_from_legacy_update
                AFTER UPDATE OF {legacy},quote_currency ON loans
                BEGIN UPDATE loans SET {subminor}=
                  CAST(ROUND(COALESCE(NEW.{legacy},0)*({remainder_scale})) AS INTEGER)
                  WHERE loan_id=NEW.loan_id; END""")
            self.execute(f"""CREATE TRIGGER {stem}_to_legacy AFTER UPDATE OF {subminor} ON loans
                BEGIN UPDATE loans SET {legacy}=CAST(NEW.{subminor} AS REAL)/({remainder_scale})
                  WHERE loan_id=NEW.loan_id; END""")
        for table, key in (("loan_collateral_assets", "collateral_id"),
                           ("collateral_liquidation_events", "event_id")):
            fields = next(item[2] for item in collateral_amount_fields if item[0] == table)
            for field in fields:
                minor = f"{field}_minor_units"
                rows = self.execute(
                    f"SELECT x.{key},x.loan_id,x.{field},x.{minor},l.quote_currency "
                    f"FROM {table} x JOIN loans l USING(loan_id)"
                )
                for amount in rows:
                    places = currency_precision(self.conn, amount["quote_currency"] or base_currency_code(self.conn))
                    if int(amount[minor] or 0) == 0 and amount[field]:
                        self.execute(f"UPDATE {table} SET {minor}=? WHERE {key}=?",
                                     (to_minor_units(amount[field], places), amount[key]))
                if table == "collateral_liquidation_events":
                    # Event snapshots are immutable. Writers provide integer amounts
                    # in the INSERT itself; never use an AFTER trigger to rewrite them.
                    continue
                scale = (
                    "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code="
                    "(SELECT quote_currency FROM loans WHERE loan_id=NEW.loan_id)),0) "
                    "WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
                )
                stem = f"trg_{table}_{field}"
                self.execute(f"DROP TRIGGER IF EXISTS {stem}_from_legacy")
                self.execute(f"DROP TRIGGER IF EXISTS {stem}_from_legacy_update")
                self.execute(f"DROP TRIGGER IF EXISTS {stem}_to_legacy")
                self.execute(f"""CREATE TRIGGER {stem}_from_legacy AFTER INSERT ON {table}
                    BEGIN UPDATE {table} SET {minor}=
                      CAST(ROUND(COALESCE(NEW.{field},0)*({scale})) AS INTEGER)
                      WHERE {key}=NEW.{key}; END""")
                self.execute(f"""CREATE TRIGGER {stem}_from_legacy_update
                    AFTER UPDATE OF {field},loan_id ON {table}
                    BEGIN UPDATE {table} SET {minor}=
                      CAST(ROUND(COALESCE(NEW.{field},0)*({scale})) AS INTEGER)
                      WHERE {key}=NEW.{key}; END""")
                self.execute(f"""CREATE TRIGGER {stem}_to_legacy
                    AFTER UPDATE OF {minor} ON {table}
                    BEGIN UPDATE {table} SET {field}=
                      CAST(NEW.{minor} AS REAL)/({scale})
                      WHERE {key}=NEW.{key}; END""")
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
                       "mandatory_cost_fee", "collateral_amount", "collateral_released", "collateral_seized",
                       "collateral_asset_value", "collateral_liquidated_value",
                       "liquidation_proceeds", "provisioned_loss", "underwriting_income",
                       "ecl_allowance", "charged_off_balance",
                       "prepayment_fees_paid"),
             "loan_id", "NEW.quote_currency"),
            ("market_orders", ("limit_price", "reserved_cash"), "order_id",
             "COALESCE((SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM company WHERE company_code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code LIMIT 1),"
             "NEW.quote_currency)"),
            ("financial_institution_ledger", ("amount",), "entry_id", "NEW.currency_code"),
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
             ("principal_due", "interest_due", "fee_due", "amount_paid", "principal_paid", "interest_paid", "fee_paid"), "schedule_id",
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
             ("mark_price", "execution_price", "gross_proceeds", "liquidation_cost", "net_proceeds"),
             "event_id", "SELECT quote_currency FROM loans WHERE loan_id=NEW.loan_id"),
            ("treasury_bond_holdings", ("principal", "face_value", "acquisition_cost"),
             "holding_id", "SELECT quote_currency FROM treasury_bond_products "
             "WHERE product_code=NEW.product_code"),
            ("market_trade_history", ("price",), "trade_id",
             "SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM company WHERE company_code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code LIMIT 1"),
            ("market_last_trade", ("price",), "asset_code",
             "SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM company WHERE company_code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code LIMIT 1"),
            ("fund_accounts", ("cash", "initial_nav"), "product_code",
             "SELECT quote_currency FROM investment WHERE product_code=NEW.product_code"),
            ("fund_cash_ledger", ("amount", "balance", "amount_minor_units", "balance_minor_units"), "entry_id",
             "NEW.currency_code"),
            ("user_assets", ("average_cost",), "holding_id",
             "SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM company WHERE company_code=NEW.asset_code "
             "UNION ALL SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code LIMIT 1"),
            ("depositary_receipt_movements", ("underlying_average_cost",), "movement_id",
             "SELECT quote_currency FROM company WHERE company_code=(SELECT underlying_code "
             "FROM depositary_receipts WHERE dr_code=NEW.dr_code)"),
            ("depositary_receipt_movements", ("holder_underlying_average_cost",), "movement_id",
             "NEW.holder_underlying_currency"),
            ("depositary_receipt_movements", ("custody_underlying_average_cost",), "movement_id",
             "NEW.custody_underlying_currency"),
            ("dr_deviation_samples", ("dr_price", "reference_value"), "sample_id",
             "SELECT quote_currency FROM depositary_receipts WHERE dr_code=NEW.dr_code"),
            ("dr_deviation_alert_events", ("dr_price", "reference_value"), "event_id",
             "SELECT quote_currency FROM depositary_receipts WHERE dr_code=NEW.dr_code"),
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
            ("aml_alerts", ("amount",), "alert_id", "NEW.currency_code"),
        )
        for table, columns, key, currency in derived:
            existing = {row[1] for row in self.execute(f"PRAGMA table_info({table})")}
            available = tuple(column for column in columns if column in existing)
            if available:
                self.enforce_currency_money(table, available, key, currency)
        holding_columns = {
            row[1] for row in self.execute("PRAGMA table_info(user_assets)")
        }
        if "average_cost_minor_units" not in holding_columns:
            self.execute(
                "ALTER TABLE user_assets ADD COLUMN average_cost_minor_units "
                "INTEGER NOT NULL DEFAULT 0"
            )
        holding_currency = (
            "COALESCE((SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code "
            "UNION ALL SELECT quote_currency FROM company WHERE company_code=NEW.asset_code "
            "UNION ALL SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code "
            "LIMIT 1),base_currency_code())"
        )
        holding_scale = (
            "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code="
            f"({holding_currency})),0) WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
        )
        from functions.money import to_minor_units
        for holding in self.execute(
            "SELECT holding_id,asset_code,average_cost,average_cost_minor_units FROM user_assets"
        ):
            currency_row = self.execute(
                "SELECT COALESCE((SELECT quote_currency FROM market_assets WHERE code=? "
                "UNION ALL SELECT quote_currency FROM company WHERE company_code=? "
                "UNION ALL SELECT quote_currency FROM investment WHERE product_code=? LIMIT 1),"
                "base_currency_code())",
                (holding["asset_code"], holding["asset_code"], holding["asset_code"]),
            ).fetchone()
            currency = currency_row[0] if currency_row else base_currency_code(self.conn)
            places = currency_precision(self.conn, currency)
            expected = to_minor_units(holding["average_cost"] or 0, places)
            if int(holding["average_cost_minor_units"] or 0) != expected:
                self.execute(
                    "UPDATE user_assets SET average_cost_minor_units=? WHERE holding_id=?",
                    (expected, holding["holding_id"]),
                )
        self.execute("DROP TRIGGER IF EXISTS trg_user_assets_average_cost_minor_from_legacy")
        self.execute("DROP TRIGGER IF EXISTS trg_user_assets_average_cost_minor_from_legacy_update")
        self.execute("DROP TRIGGER IF EXISTS trg_user_assets_average_cost_minor_to_legacy")
        self.execute(f"""CREATE TRIGGER trg_user_assets_average_cost_minor_from_legacy
            AFTER INSERT ON user_assets
            WHEN NEW.average_cost_minor_units<>CAST(ROUND(
              COALESCE(NEW.average_cost,0)*({holding_scale})) AS INTEGER)
            BEGIN UPDATE user_assets SET average_cost_minor_units=CAST(ROUND(
              COALESCE(NEW.average_cost,0)*({holding_scale})) AS INTEGER)
              WHERE holding_id=NEW.holding_id; END""")
        self.execute(f"""CREATE TRIGGER trg_user_assets_average_cost_minor_from_legacy_update
            AFTER UPDATE OF average_cost,asset_code ON user_assets
            WHEN NEW.average_cost_minor_units<>CAST(ROUND(
              COALESCE(NEW.average_cost,0)*({holding_scale})) AS INTEGER)
            BEGIN UPDATE user_assets SET average_cost_minor_units=CAST(ROUND(
              COALESCE(NEW.average_cost,0)*({holding_scale})) AS INTEGER)
              WHERE holding_id=NEW.holding_id; END""")
        self.execute(f"""CREATE TRIGGER trg_user_assets_average_cost_minor_to_legacy
            AFTER UPDATE OF average_cost_minor_units ON user_assets
            WHEN NEW.average_cost IS NOT CAST(NEW.average_cost_minor_units AS REAL)/({holding_scale})
            BEGIN UPDATE user_assets SET average_cost=CAST(
              NEW.average_cost_minor_units AS REAL)/({holding_scale})
              WHERE holding_id=NEW.holding_id; END""")
        schedule_amounts = (
            ("principal_due", "principal_due_minor_units"),
            ("interest_due", "interest_due_minor_units"),
            ("fee_due", "fee_due_minor_units"),
            ("amount_paid", "amount_paid_minor_units"),
            ("principal_paid", "principal_paid_minor_units"),
            ("interest_paid", "interest_paid_minor_units"),
            ("fee_paid", "fee_paid_minor_units"),
        )
        from functions.money import currency_precision, to_minor_units
        for schedule_row in self.execute(
                "SELECT s.*,l.quote_currency FROM loan_payment_schedule s "
                "JOIN loans l USING(loan_id)"):
            places = currency_precision(self.conn, schedule_row["quote_currency"] or base_currency_code(self.conn))
            missing = [(minor, to_minor_units(schedule_row[legacy] or 0, places))
                       for legacy, minor in schedule_amounts
                       if not int(schedule_row[minor] or 0)
                       and float(schedule_row[legacy] or 0)]
            if not missing:
                continue
            assignments = ",".join(f"{minor}=?" for minor, _ in missing)
            self.execute(
                f"UPDATE loan_payment_schedule SET {assignments} WHERE schedule_id=?",
                (*(value for _, value in missing), schedule_row["schedule_id"]),
            )
        schedule_scale = "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code=" \
                         "(SELECT quote_currency FROM loans WHERE loan_id=NEW.loan_id)),0) " \
                         "WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
        minor_assignments = ",".join(
            f"{minor}=CAST(ROUND(COALESCE(NEW.{legacy},0)*({schedule_scale})) AS INTEGER)"
            for legacy, minor in schedule_amounts
        )
        self.execute("DROP TRIGGER IF EXISTS trg_loan_schedule_minor_insert")
        self.execute("DROP TRIGGER IF EXISTS trg_loan_schedule_minor_update")
        self.execute(f"""CREATE TRIGGER trg_loan_schedule_minor_insert
            AFTER INSERT ON loan_payment_schedule BEGIN
                UPDATE loan_payment_schedule SET {minor_assignments}
                WHERE schedule_id=NEW.schedule_id;
            END""")
        self.execute(f"""CREATE TRIGGER trg_loan_schedule_minor_update
            AFTER UPDATE OF {','.join(legacy for legacy, _ in schedule_amounts)}
            ON loan_payment_schedule BEGIN
                UPDATE loan_payment_schedule SET {minor_assignments}
                WHERE schedule_id=NEW.schedule_id;
            END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_insurance_payout_precision_insert
            BEFORE INSERT ON deposit_insurance_claims
            WHEN NEW.payout_amount IS NOT NULL AND NEW.payout_currency IS NOT NULL
              AND NEW.payout_amount <> ROUND(
                  NEW.payout_amount+CASE WHEN NEW.payout_amount>=0 THEN 1e-12 ELSE -1e-12 END,
                  COALESCE((SELECT decimal_places FROM currencies
                            WHERE code=NEW.payout_currency),0))
            BEGIN SELECT RAISE(ABORT,'insurance payout amount exceeds currency precision'); END""")
        # Upgrade historical payout mirrors before enforcing the append-only
        # contract.  The INTEGER amount is canonical; REAL remains a read/API
        # compatibility mirror only.
        from functions.money import to_minor_units
        payout_backfill = self.execute(
            "SELECT claim_id,payout_currency,payout_amount,payout_amount_minor_units "
            "FROM deposit_insurance_claims WHERE payout_currency IS NOT NULL "
            "AND payout_amount IS NOT NULL AND payout_amount_minor_units IS NULL"
        )
        if payout_backfill:
            self.execute("DROP TRIGGER IF EXISTS trg_deposit_insurance_claims_immutable_update")
            try:
                for claim in payout_backfill:
                    try:
                        places = currency_precision(self.conn, claim["payout_currency"])
                        minor = to_minor_units(claim["payout_amount"], places)
                    except ValueError:
                        # Preserve an unverifiable legacy record; reconciler reports it.
                        continue
                    self.execute(
                        "UPDATE deposit_insurance_claims SET payout_amount_minor_units=?,"
                        "payout_decimal_places=? WHERE claim_id=?",
                        (minor, places, claim["claim_id"]),
                    )
            finally:
                self.execute("""CREATE TRIGGER IF NOT EXISTS trg_deposit_insurance_claims_immutable_update
                    BEFORE UPDATE ON deposit_insurance_claims
                    BEGIN SELECT RAISE(ABORT,'deposit insurance claims are append-only'); END""")
        self.execute("DROP TRIGGER IF EXISTS trg_insurance_payout_minor_insert")
        self.execute("""CREATE TRIGGER trg_insurance_payout_minor_insert
            BEFORE INSERT ON deposit_insurance_claims
            WHEN (NEW.payout_amount IS NULL AND (NEW.payout_amount_minor_units IS NOT NULL OR NEW.payout_decimal_places IS NOT NULL))
              OR (NEW.payout_amount IS NOT NULL AND NEW.payout_currency IS NOT NULL
                  AND EXISTS (SELECT 1 FROM currencies WHERE code=NEW.payout_currency) AND (
                    NEW.payout_amount_minor_units IS NULL OR NEW.payout_decimal_places IS NULL
                    OR NEW.payout_decimal_places <> COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.payout_currency),-1)
                    OR NEW.payout_amount_minor_units <> CAST(ROUND(NEW.payout_amount * CASE NEW.payout_decimal_places WHEN 0 THEN 1 WHEN 1 THEN 10 WHEN 2 THEN 100 END) AS INTEGER)
                  ))
            BEGIN SELECT RAISE(ABORT,'insurance payout minor units are missing or inconsistent'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_insurance_payout_pair_insert
            BEFORE INSERT ON deposit_insurance_claims
            WHEN (NEW.payout_currency IS NULL) <> (NEW.payout_amount IS NULL)
              OR (NEW.payout_currency IS NOT NULL AND NOT EXISTS (
                  SELECT 1 FROM currencies WHERE code=NEW.payout_currency))
            BEGIN SELECT RAISE(ABORT,'insurance payout currency and amount must be paired and use a known currency'); END""")
        self._ensure_account_units()
        self._ensure_account_reference_price_units()
        self._ensure_aml_amount_units()
        self._ensure_dr_deviation_price_units()
        self._ensure_dr_movement_cost_units()
        self._ensure_cash_minor()
        self._ensure_money_snaps()
        self._ensure_market_asset_price_minor_units()
        self._ensure_market_trade_price_units()
        self._ensure_market_order_price_units()
        self._ensure_market_order_reservation_units()
        self._ensure_treasury_bond_minor_units()
        self._ensure_tax_transaction_minor_units()
        snapshot_direct = (*direct, ("accounts", ("reference_price",),
                                     "account_number", "NEW.quote_currency"))
        self._ensure_minor_amount_snapshots(snapshot_direct, derived)
        self._ensure_customer_wallet_journal()
        self._ensure_fund_cash_common_journal()
        with self.lock, self.conn:
            self._ensure_tax_transaction_wallet_links()

    def _ensure_fund_cash_common_journal(self):
        """Mirror fund cash postings in their native currency minor units."""
        self.execute("""INSERT OR IGNORE INTO accounting_account_mappings
            (ledger_scope,local_code,common_code) VALUES
            ('fund','fund_cash','fund_cash'),
            ('fund','fund_cash_clearing','fund_cash_clearing')""")
        self.execute("DROP TRIGGER IF EXISTS trg_fund_cash_common_post")
        self.execute("""INSERT OR IGNORE INTO accounting_entries
            (ledger_scope,entity_type,entity_id,source_key,currency_code,decimal_places,
             recorded_epoch,description)
            SELECT 'fund','fund_cash',product_code,'fund-cash:'||entry_id,
                currency_code,decimal_places,recorded_epoch,event
            FROM fund_cash_ledger ORDER BY entry_id""")
        self.execute("""INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
            SELECT e.entry_id,'fund_cash',
                CASE WHEN f.amount_minor_units>0 THEN f.amount_minor_units ELSE 0 END,
                CASE WHEN f.amount_minor_units<0 THEN -f.amount_minor_units ELSE 0 END
            FROM fund_cash_ledger f JOIN accounting_entries e
              ON e.ledger_scope='fund' AND e.entity_type='fund_cash'
             AND e.entity_id=f.product_code AND e.source_key='fund-cash:'||f.entry_id
            WHERE NOT EXISTS (SELECT 1 FROM accounting_lines l
                WHERE l.entry_id=e.entry_id AND l.account_code='fund_cash')""")
        self.execute("""INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
            SELECT e.entry_id,'fund_cash_clearing',
                CASE WHEN f.amount_minor_units<0 THEN -f.amount_minor_units ELSE 0 END,
                CASE WHEN f.amount_minor_units>0 THEN f.amount_minor_units ELSE 0 END
            FROM fund_cash_ledger f JOIN accounting_entries e
              ON e.ledger_scope='fund' AND e.entity_type='fund_cash'
             AND e.entity_id=f.product_code AND e.source_key='fund-cash:'||f.entry_id
            WHERE NOT EXISTS (SELECT 1 FROM accounting_lines l
                WHERE l.entry_id=e.entry_id AND l.account_code='fund_cash_clearing')""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_fund_cash_common_post
            AFTER INSERT ON fund_cash_ledger BEGIN
            INSERT INTO accounting_entries(ledger_scope,entity_type,entity_id,source_key,
                currency_code,decimal_places,recorded_epoch,description)
            VALUES ('fund','fund_cash',NEW.product_code,'fund-cash:'||NEW.entry_id,
                NEW.currency_code,NEW.decimal_places,NEW.recorded_epoch,NEW.event);
            INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
            SELECT entry_id,'fund_cash',CASE WHEN NEW.amount_minor_units>0 THEN NEW.amount_minor_units ELSE 0 END,
                CASE WHEN NEW.amount_minor_units<0 THEN -NEW.amount_minor_units ELSE 0 END FROM accounting_entries
            WHERE ledger_scope='fund' AND entity_type='fund_cash'
              AND entity_id=NEW.product_code AND source_key='fund-cash:'||NEW.entry_id;
            INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
            SELECT entry_id,'fund_cash_clearing',CASE WHEN NEW.amount_minor_units<0 THEN -NEW.amount_minor_units ELSE 0 END,
                CASE WHEN NEW.amount_minor_units>0 THEN NEW.amount_minor_units ELSE 0 END FROM accounting_entries
            WHERE ledger_scope='fund' AND entity_type='fund_cash'
              AND entity_id=NEW.product_code AND source_key='fund-cash:'||NEW.entry_id;
            END""")

    def _ensure_tax_transaction_wallet_links(self):
        """Create immutable tax-to-treasury wallet links and freeze legacy rows."""
        self.execute("""CREATE TABLE IF NOT EXISTS tax_transaction_wallet_links (
            transaction_id INTEGER NOT NULL REFERENCES tax_transactions(transaction_id),
            wallet_entry_id INTEGER NOT NULL REFERENCES customer_wallet_ledger(entry_id),
            treasury_user_id TEXT NOT NULL,
            PRIMARY KEY(transaction_id,wallet_entry_id))""")
        self.execute("""CREATE TABLE IF NOT EXISTS tax_transaction_wallet_baselines (
            transaction_id INTEGER PRIMARY KEY REFERENCES tax_transactions(transaction_id),
            reason TEXT NOT NULL DEFAULT 'pre-link historical tax row')""")
        self.execute("""CREATE TABLE IF NOT EXISTS tax_transaction_wallet_baseline_state (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
            through_transaction_id INTEGER NOT NULL CHECK(through_transaction_id>=0))""")
        self.execute("""CREATE UNIQUE INDEX IF NOT EXISTS
            idx_tax_transaction_wallet_link_entry_unique
            ON tax_transaction_wallet_links(wallet_entry_id)""")
        self.execute("""INSERT OR IGNORE INTO tax_transaction_wallet_baseline_state
            (singleton_id,through_transaction_id)
            SELECT 1,COALESCE(MAX(transaction_id),0) FROM tax_transactions""")
        self.execute("""INSERT OR IGNORE INTO tax_transaction_wallet_baselines
            (transaction_id,reason)
            SELECT t.transaction_id,'pre-link historical tax row'
            FROM tax_transactions t CROSS JOIN tax_transaction_wallet_baseline_state b
            WHERE t.transaction_id<=b.through_transaction_id
              AND COALESCE(t.tax_amount_minor_units,0)>0""")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS
                trg_tax_transaction_wallet_links_{operation.lower()}_immutable
                BEFORE {operation} ON tax_transaction_wallet_links
                BEGIN SELECT RAISE(ABORT,'tax wallet links are append-only'); END""")
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS
                trg_tax_transaction_wallet_baselines_{operation.lower()}_immutable
                BEFORE {operation} ON tax_transaction_wallet_baselines
                BEGIN SELECT RAISE(ABORT,'tax wallet baselines are append-only'); END""")
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS
                trg_tax_transaction_wallet_baseline_state_{operation.lower()}_immutable
                BEFORE {operation} ON tax_transaction_wallet_baseline_state
                BEGIN SELECT RAISE(ABORT,'tax wallet baseline watermark is immutable'); END""")

    def _ensure_market_asset_price_minor_units(self):
        """Make listed instrument quotes available as canonical currency units."""
        from functions.money import currency_precision, to_minor_units

        specs = (
            ("market_assets", "code", "code", "price", "price_minor_units",
             "NEW.quote_currency", "price,quote_currency"),
            ("company", "company_code", "company_code", "share_price", "share_price_minor_units",
             "NEW.quote_currency", "share_price,quote_currency"),
            ("investment", "product_code", "product_code", "unit_price", "unit_price_minor_units",
             "NEW.quote_currency", "unit_price,quote_currency"),
            ("market_price_history", "history_id", "asset_code", "price", "price_minor_units",
             "COALESCE((SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code),"
             "(SELECT quote_currency FROM company WHERE company_code=NEW.asset_code),"
             "(SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code),"
             "base_currency_code())", "price,asset_code"),
        )
        for table, key, asset_code, amount_column, minor_column, currency_sql, update_columns in specs:
            columns = {row[1] for row in self.execute(f'PRAGMA table_info("{table}")')}
            if amount_column not in columns:
                continue
            if minor_column not in columns:
                self.execute(f'ALTER TABLE "{table}" ADD COLUMN "{minor_column}" INTEGER NOT NULL DEFAULT 0')
            rows = self.execute(f'SELECT "{key}","{asset_code}","{amount_column}","{minor_column}" FROM "{table}"')
            for row in rows:
                if int(row[minor_column] or 0) == 0 and float(row[amount_column] or 0) != 0:
                    currency = self.execute_one(
                        "SELECT COALESCE((SELECT quote_currency FROM market_assets WHERE code=?),"
                        "(SELECT quote_currency FROM company WHERE company_code=?),"
                        "(SELECT quote_currency FROM investment WHERE product_code=?),base_currency_code())",
                        (row[asset_code], row[asset_code], row[asset_code]),
                    )[0]
                    places = currency_precision(self.conn, currency)
                    self.execute(f'UPDATE "{table}" SET "{minor_column}"=? WHERE "{key}"=?',
                                 (to_minor_units(row[amount_column], places), row[key]))
            scale = ("CASE COALESCE((SELECT decimal_places FROM currencies WHERE code="
                     f"({currency_sql})),0) WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END")
            stem = f"trg_{table}_{amount_column}_minor_authority"
            amount_new = f"COALESCE(NEW.{amount_column},0)"
            minor_new = f"NEW.{minor_column}"
            for suffix in ("legacy_insert", "legacy_update", "minor_to_legacy",
                           "minor_guard_insert", f"minor_guard_update_of_{minor_column}"):
                self.execute(f"DROP TRIGGER IF EXISTS {stem}_{suffix}")
            self.execute(f"""CREATE TRIGGER {stem}_legacy_insert AFTER INSERT ON "{table}"
                BEGIN UPDATE "{table}" SET "{minor_column}"=
                  CAST(ROUND({amount_new}*({scale})) AS INTEGER)
                  WHERE "{key}"=NEW."{key}"; END""")
            self.execute(f"""CREATE TRIGGER {stem}_legacy_update AFTER UPDATE OF {update_columns} ON "{table}"
                WHEN {minor_new}<>CAST(ROUND({amount_new}*({scale})) AS INTEGER)
                BEGIN UPDATE "{table}" SET "{minor_column}"=
                  CAST(ROUND({amount_new}*({scale})) AS INTEGER)
                  WHERE "{key}"=NEW."{key}"; END""")
            self.execute(f"""CREATE TRIGGER {stem}_minor_to_legacy AFTER UPDATE OF "{minor_column}" ON "{table}"
                WHEN NEW."{amount_column}"<>CAST({minor_new} AS REAL)/({scale})
                BEGIN UPDATE "{table}" SET "{amount_column}"=CAST({minor_new} AS REAL)/({scale})
                  WHERE "{key}"=NEW."{key}"; END""")
            for operation, suffix in (("INSERT", "insert"),
                                      (f"UPDATE OF {minor_column}", f"update_of_{minor_column}")):
                self.execute(f"""CREATE TRIGGER {stem}_minor_guard_{suffix}
                    BEFORE {operation} ON "{table}"
                    WHEN typeof(NEW."{minor_column}")<>'integer'
                    BEGIN SELECT RAISE(ABORT,'instrument price minor units must be an integer'); END""")

    def _ensure_tax_transaction_minor_units(self):
        """Persist tax gross/tax/net in currency minor units and enforce balance."""
        from functions.money import currency_precision, to_minor_units

        table = "tax_transactions"
        columns = {row[1] for row in self.execute(f'PRAGMA table_info("{table}")')}
        if not columns:
            return
        fields = ("gross_amount", "tax_amount", "net_amount")
        newly_added = []
        for field in fields:
            minor = f"{field}_minor_units"
            if minor not in columns:
                self.execute(f'ALTER TABLE "{table}" ADD COLUMN "{minor}" INTEGER NOT NULL DEFAULT 0')
                newly_added.append((field, minor))
        if newly_added:
            for row in self.execute(
                "SELECT transaction_id,currency_code,gross_amount,tax_amount,net_amount "
                "FROM tax_transactions"
            ):
                places = currency_precision(self.conn, row["currency_code"] or base_currency_code(self.conn))
                values = [to_minor_units(row[field], places) for field in fields]
                self.execute("""UPDATE tax_transactions SET gross_amount_minor_units=?,
                    tax_amount_minor_units=?,net_amount_minor_units=? WHERE transaction_id=?""",
                    (*values, row["transaction_id"]))
        self.execute("DROP TRIGGER IF EXISTS trg_tax_transactions_minor_from_legacy_insert")
        self.execute("DROP TRIGGER IF EXISTS trg_tax_transactions_minor_from_legacy_update")
        self.execute("DROP TRIGGER IF EXISTS trg_tax_transactions_legacy_from_minor")
        self.execute("DROP TRIGGER IF EXISTS trg_tax_transactions_minor_units_guard_insert")
        self.execute("DROP TRIGGER IF EXISTS trg_tax_transactions_minor_units_guard_update")
        scale = ("CASE COALESCE((SELECT decimal_places FROM currencies "
                 "WHERE code=NEW.currency_code),0) WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END")
        units = [f"{field}_minor_units" for field in fields]
        rounded = [f"CAST(ROUND(COALESCE(NEW.{field},0)*({scale})) AS INTEGER)" for field in fields]
        self.execute(f"""CREATE TRIGGER trg_tax_transactions_minor_from_legacy_insert
            AFTER INSERT ON tax_transactions BEGIN UPDATE tax_transactions SET
            gross_amount_minor_units={rounded[0]},tax_amount_minor_units={rounded[1]},
            net_amount_minor_units={rounded[2]} WHERE transaction_id=NEW.transaction_id; END""")
        self.execute(f"""CREATE TRIGGER trg_tax_transactions_minor_from_legacy_update
            AFTER UPDATE OF gross_amount,tax_amount,net_amount,currency_code ON tax_transactions
            WHEN NEW.gross_amount_minor_units<>{rounded[0]}
              OR NEW.tax_amount_minor_units<>{rounded[1]}
              OR NEW.net_amount_minor_units<>{rounded[2]}
            BEGIN UPDATE tax_transactions SET gross_amount_minor_units={rounded[0]},
              tax_amount_minor_units={rounded[1]},net_amount_minor_units={rounded[2]}
              WHERE transaction_id=NEW.transaction_id; END""")
        self.execute(f"""CREATE TRIGGER trg_tax_transactions_legacy_from_minor
            AFTER UPDATE OF gross_amount_minor_units,tax_amount_minor_units,net_amount_minor_units
            ON tax_transactions WHEN NEW.gross_amount<>CAST(NEW.gross_amount_minor_units AS REAL)/({scale})
              OR NEW.tax_amount<>CAST(NEW.tax_amount_minor_units AS REAL)/({scale})
              OR NEW.net_amount<>CAST(NEW.net_amount_minor_units AS REAL)/({scale})
            BEGIN UPDATE tax_transactions SET gross_amount=CAST(NEW.gross_amount_minor_units AS REAL)/({scale}),
              tax_amount=CAST(NEW.tax_amount_minor_units AS REAL)/({scale}),
              net_amount=CAST(NEW.net_amount_minor_units AS REAL)/({scale})
              WHERE transaction_id=NEW.transaction_id; END""")
        for operation in ("INSERT", "UPDATE"):
            ref = ("NEW" if operation == "INSERT" else "NEW")
            self.execute(f"""CREATE TRIGGER trg_tax_transactions_minor_units_guard_{operation.lower()}
                BEFORE {operation} ON tax_transactions
                WHEN typeof({ref}.gross_amount_minor_units)<>'integer'
                  OR typeof({ref}.tax_amount_minor_units)<>'integer'
                  OR typeof({ref}.net_amount_minor_units)<>'integer'
                  OR CAST(ROUND(COALESCE({ref}.gross_amount,0)*({scale})) AS INTEGER)<>
                     CAST(ROUND(COALESCE({ref}.tax_amount,0)*({scale})) AS INTEGER)+
                     CAST(ROUND(COALESCE({ref}.net_amount,0)*({scale})) AS INTEGER)
                BEGIN SELECT RAISE(ABORT,'tax transaction currency minor units are invalid or unbalanced'); END""")

    def _ensure_market_trade_price_units(self):
        """Keep execution prices in quote-currency minor units as the source of truth."""
        from functions.money import currency_precision, to_minor_units

        quote_sql = (
            "COALESCE((SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code),"
            "(SELECT quote_currency FROM company WHERE company_code=NEW.asset_code),"
            "(SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code),"
            "(SELECT quote_currency FROM treasury_bond_products WHERE product_code=NEW.asset_code),"
            "(SELECT base.quote_currency FROM option_contracts o JOIN market_assets base "
            "ON base.code=o.underlying_code WHERE o.contract_code=NEW.asset_code),"
            "(SELECT base_currency_code FROM currency_system_settings WHERE singleton_id=1))"
        )
        for table, key in (("market_trade_history", "trade_id"),
                           ("market_last_trade", "asset_code")):
            columns = {row[1] for row in self.execute(f"PRAGMA table_info({table})")}
            if "price_minor_units" not in columns:
                self.execute(
                    f"ALTER TABLE {table} ADD COLUMN price_minor_units INTEGER NOT NULL DEFAULT 0"
                )
            for row in self.execute(
                    f"SELECT {key},asset_code,price,price_minor_units FROM {table}"):
                if int(row["price_minor_units"] or 0) == 0 and float(row["price"] or 0) != 0:
                    currency = self.execute_one(
                        "SELECT " + quote_sql.replace("NEW.asset_code", "asset_code")
                        + " AS code FROM (SELECT ? AS asset_code)",
                        (row["asset_code"],),
                    )["code"]
                    places = currency_precision(self.conn, currency or base_currency_code(self.conn))
                    self.execute(
                        f"UPDATE {table} SET price_minor_units=? WHERE {key}=?",
                        (to_minor_units(row["price"], places), row[key]),
                    )
            scale = (
                "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code="
                f"({quote_sql})),0) WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
            )
            stem = f"trg_{table}_price_minor_units"
            for suffix in ("from_legacy", "from_legacy_update", "to_legacy"):
                self.execute(f"DROP TRIGGER IF EXISTS {stem}_{suffix}")
            self.execute(f"""CREATE TRIGGER {stem}_from_legacy AFTER INSERT ON {table}
                BEGIN UPDATE {table} SET price_minor_units=
                  CAST(ROUND(COALESCE(NEW.price,0)*({scale})) AS INTEGER)
                  WHERE {key}=NEW.{key}; END""")
            self.execute(f"""CREATE TRIGGER {stem}_from_legacy_update
                AFTER UPDATE OF price,asset_code ON {table}
                BEGIN UPDATE {table} SET price_minor_units=
                  CAST(ROUND(COALESCE(NEW.price,0)*({scale})) AS INTEGER)
                  WHERE {key}=NEW.{key}; END""")
            self.execute(f"""CREATE TRIGGER {stem}_to_legacy AFTER UPDATE OF price_minor_units ON {table}
                BEGIN UPDATE {table} SET price=CAST(NEW.price_minor_units AS REAL)/({scale})
                  WHERE {key}=NEW.{key}; END""")

    def _ensure_market_order_price_units(self):
        """Store every order limit price in its quote-currency quantum."""
        from functions.money import currency_precision, to_minor_units
        columns = {row[1] for row in self.execute("PRAGMA table_info(market_orders)")}
        if not columns:
            return
        if "limit_price_minor_units" not in columns:
            self.execute("ALTER TABLE market_orders ADD COLUMN limit_price_minor_units INTEGER NOT NULL DEFAULT 0")
        if "limit_price_decimal_places" not in columns:
            self.execute("ALTER TABLE market_orders ADD COLUMN limit_price_decimal_places INTEGER")
        for row in self.execute("SELECT order_id,quote_currency,limit_price,limit_price_minor_units,"
                                "limit_price_decimal_places FROM market_orders"):
            places = currency_precision(self.conn, row["quote_currency"] or base_currency_code(self.conn))
            units = to_minor_units(row["limit_price"], places)
            if row["limit_price_minor_units"] != units or row["limit_price_decimal_places"] != places:
                self.execute("UPDATE market_orders SET limit_price_minor_units=?,"
                             "limit_price_decimal_places=? WHERE order_id=?",
                             (units, places, row["order_id"]))
        scale = ("CASE COALESCE((SELECT decimal_places FROM currencies "
                 "WHERE code=NEW.quote_currency),0) " + " ".join(
                    f"WHEN {i} THEN {10**i}" for i in range(0, 7)) + " ELSE 100 END")
        for suffix in ("legacy_insert", "legacy_update", "minor"):
            self.execute(f"DROP TRIGGER IF EXISTS trg_market_order_limit_price_{suffix}")
        self.execute(f"""CREATE TRIGGER trg_market_order_limit_price_legacy_insert
            AFTER INSERT ON market_orders BEGIN
              UPDATE market_orders SET limit_price_minor_units=CAST(ROUND(NEW.limit_price*({scale})) AS INTEGER),
                limit_price_decimal_places=COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0)
              WHERE order_id=NEW.order_id; END""")
        self.execute(f"""CREATE TRIGGER trg_market_order_limit_price_legacy_update
            AFTER UPDATE OF limit_price,quote_currency ON market_orders
            WHEN NEW.limit_price IS NOT OLD.limit_price OR NEW.quote_currency IS NOT OLD.quote_currency BEGIN
              UPDATE market_orders SET limit_price_minor_units=CAST(ROUND(NEW.limit_price*({scale})) AS INTEGER),
                limit_price_decimal_places=COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0)
              WHERE order_id=NEW.order_id; END""")
        self.execute(f"""CREATE TRIGGER trg_market_order_limit_price_minor
            AFTER UPDATE OF limit_price_minor_units,limit_price_decimal_places ON market_orders
            WHEN NEW.limit_price_minor_units IS NOT OLD.limit_price_minor_units
              OR NEW.limit_price_decimal_places IS NOT OLD.limit_price_decimal_places BEGIN
              UPDATE market_orders SET limit_price=CAST(NEW.limit_price_minor_units AS REAL)/
                (CASE COALESCE(NEW.limit_price_decimal_places,0) WHEN 0 THEN 1 WHEN 1 THEN 10
                 WHEN 2 THEN 100 WHEN 3 THEN 1000 WHEN 4 THEN 10000 WHEN 5 THEN 100000
                 ELSE 1000000 END) WHERE order_id=NEW.order_id; END""")

    def _ensure_market_order_reservation_units(self):
        """Use quote-currency minor units as the authority for order cash locks."""
        from functions.money import currency_precision, to_minor_units

        columns = {row[1] for row in self.execute("PRAGMA table_info(market_orders)")}
        if "reserved_cash_minor_units" not in columns:
            self.execute("ALTER TABLE market_orders ADD COLUMN reserved_cash_minor_units INTEGER NOT NULL DEFAULT 0")
        for row in self.execute("SELECT order_id,quote_currency,reserved_cash,reserved_cash_minor_units FROM market_orders"):
            if int(row["reserved_cash_minor_units"] or 0) == 0 and float(row["reserved_cash"] or 0) != 0:
                places = currency_precision(self.conn, row["quote_currency"] or base_currency_code(self.conn))
                self.execute("UPDATE market_orders SET reserved_cash_minor_units=? WHERE order_id=?",
                             (to_minor_units(row["reserved_cash"], places), row["order_id"]))
        scale = ("CASE COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0) "
                 "WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END")
        for suffix in ("legacy", "legacy_update", "minor"):
            self.execute(f"DROP TRIGGER IF EXISTS trg_market_order_reserved_{suffix}")
        self.execute(f"""CREATE TRIGGER trg_market_order_reserved_legacy AFTER INSERT ON market_orders
            BEGIN UPDATE market_orders SET reserved_cash_minor_units=CAST(ROUND(COALESCE(NEW.reserved_cash,0)*({scale})) AS INTEGER)
            WHERE order_id=NEW.order_id; END""")
        self.execute(f"""CREATE TRIGGER trg_market_order_reserved_legacy_update
            AFTER UPDATE OF reserved_cash,quote_currency ON market_orders
            BEGIN UPDATE market_orders SET reserved_cash_minor_units=CAST(ROUND(COALESCE(NEW.reserved_cash,0)*({scale})) AS INTEGER)
            WHERE order_id=NEW.order_id; END""")
        self.execute(f"""CREATE TRIGGER trg_market_order_reserved_minor AFTER UPDATE OF reserved_cash_minor_units ON market_orders
            BEGIN UPDATE market_orders SET reserved_cash=CAST(NEW.reserved_cash_minor_units AS REAL)/({scale})
            WHERE order_id=NEW.order_id; END""")

    def _ensure_customer_wallet_journal(self):
        """Capture every customer cash/account balance delta in one native journal.

        Triggers run in the same SQLite transaction as the balance update, so
        direct SQL call sites cannot bypass the wallet subledger or its mapped
        double-entry record.
        """
        self.execute("""CREATE TABLE IF NOT EXISTS accounting_account_mappings (
            ledger_scope TEXT NOT NULL, local_code TEXT NOT NULL, common_code TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            PRIMARY KEY(ledger_scope,local_code))""")
        self.execute("""CREATE TABLE IF NOT EXISTS accounting_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT, ledger_scope TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, source_key TEXT NOT NULL,
            currency_code TEXT NOT NULL, decimal_places INTEGER NOT NULL CHECK(decimal_places BETWEEN 0 AND 6),
            recorded_epoch INTEGER NOT NULL, description TEXT NOT NULL DEFAULT '',
            UNIQUE(ledger_scope,entity_type,entity_id,source_key))""")
        self.execute("""CREATE TABLE IF NOT EXISTS accounting_lines (
            line_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL REFERENCES accounting_entries(entry_id),
            account_code TEXT NOT NULL, debit_units INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(debit_units)='integer' AND debit_units>=0),
            credit_units INTEGER NOT NULL DEFAULT 0
                CHECK(typeof(credit_units)='integer' AND credit_units>=0),
            CHECK((debit_units=0)<>(credit_units=0)))""")
        # Older execution events predate common market settlement postings. Keep
        # them as an explicit non-reconstructed baseline rather than inventing
        # tax/net settlement values during migration.
        self.execute("""INSERT OR IGNORE INTO market_execution_journal_baselines
            (execution_key,captured_epoch)
            SELECT e.execution_key,unixepoch() FROM market_execution_events e
            WHERE NOT EXISTS (SELECT 1 FROM accounting_entries a
              WHERE a.ledger_scope='market' AND a.entity_id=e.asset_code
                AND a.source_key=e.execution_key)""")
        self.execute("""CREATE TABLE IF NOT EXISTS customer_wallet_ledger (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL UNIQUE, user_id TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK(source_type IN ('cash','account')),
            source_account TEXT NOT NULL, currency_code TEXT NOT NULL,
            decimal_places INTEGER NOT NULL CHECK(decimal_places BETWEEN 0 AND 6),
            delta_minor_units INTEGER NOT NULL CHECK(typeof(delta_minor_units)='integer' AND delta_minor_units<>0),
            balance_minor_units INTEGER NOT NULL CHECK(typeof(balance_minor_units)='integer'),
            recorded_epoch INTEGER NOT NULL)""")
        for local in ("customer_funds_payable", "wallet_clearing", "opening_balance_equity"):
            self.execute("INSERT OR IGNORE INTO accounting_account_mappings(ledger_scope,local_code,common_code) VALUES ('customer',?,?)", (local,local))
        # Historical state is one opening transaction per wallet, guarded by a
        # deterministic source key so startup retries are harmless.
        cash_rows = self.execute("""SELECT u.user_id,u.cash_minor,
            COALESCE(n.currency_code,base_currency_code()) currency_code,COALESCE(c.decimal_places,0) decimal_places
            FROM users u LEFT JOIN country_currencies n ON n.nationality=u.nationality
            LEFT JOIN currencies c ON c.code=n.currency_code WHERE u.cash_minor<>0""")
        for row in cash_rows:
            source = f"opening:cash:{row['user_id']}"
            self.execute("""INSERT OR IGNORE INTO customer_wallet_ledger
                (source_key,user_id,source_type,source_account,currency_code,decimal_places,
                 delta_minor_units,balance_minor_units,recorded_epoch)
                VALUES (?,?, 'cash', ?,?,?,?,?,unixepoch())""",
                (source,str(row['user_id']),str(row['user_id']),str(row['currency_code']),
                 int(row['decimal_places']),int(row['cash_minor']),int(row['cash_minor'])))
        account_rows = self.execute("""SELECT account_number,user_id,quote_currency,
            balance_minor_units,balance_decimal_places FROM accounts
            WHERE balance_minor_units<>0 AND user_id IS NOT NULL""")
        for row in account_rows:
            source = f"opening:account:{row['account_number']}"
            self.execute("""INSERT OR IGNORE INTO customer_wallet_ledger
                (source_key,user_id,source_type,source_account,currency_code,decimal_places,
                 delta_minor_units,balance_minor_units,recorded_epoch)
                VALUES (?,?, 'account', ?,?,?,?,?,unixepoch())""",
                (source,str(row['user_id']),str(row['account_number']),str(row['quote_currency']),
                 int(row['balance_decimal_places']),int(row['balance_minor_units']),int(row['balance_minor_units'])))
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_customer_wallet_common_post
            AFTER INSERT ON customer_wallet_ledger BEGIN
            INSERT INTO accounting_entries(ledger_scope,entity_type,entity_id,source_key,
                currency_code,decimal_places,recorded_epoch,description)
            VALUES ('customer','wallet_holder',NEW.user_id,'wallet:'||NEW.entry_id,
                NEW.currency_code,NEW.decimal_places,NEW.recorded_epoch,
                NEW.source_type||' balance movement');
            INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
            SELECT entry_id,'customer_funds_payable',
                CASE WHEN NEW.delta_minor_units<0 THEN -NEW.delta_minor_units ELSE 0 END,
                CASE WHEN NEW.delta_minor_units>0 THEN NEW.delta_minor_units ELSE 0 END
            FROM accounting_entries WHERE ledger_scope='customer' AND entity_type='wallet_holder'
                AND entity_id=NEW.user_id AND source_key='wallet:'||NEW.entry_id;
            INSERT INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
            SELECT entry_id,'wallet_clearing',
                CASE WHEN NEW.delta_minor_units>0 THEN NEW.delta_minor_units ELSE 0 END,
                CASE WHEN NEW.delta_minor_units<0 THEN -NEW.delta_minor_units ELSE 0 END
            FROM accounting_entries WHERE ledger_scope='customer' AND entity_type='wallet_holder'
                AND entity_id=NEW.user_id AND source_key='wallet:'||NEW.entry_id;
            END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_customer_cash_ledger_insert AFTER INSERT ON users
            WHEN NEW.cash_minor<>0 BEGIN
            INSERT OR IGNORE INTO customer_wallet_ledger(source_key,user_id,source_type,source_account,
                currency_code,decimal_places,delta_minor_units,balance_minor_units,recorded_epoch)
            SELECT 'insert:cash:'||NEW.user_id,NEW.user_id,'cash',NEW.user_id,
                COALESCE(n.currency_code,base_currency_code()),COALESCE(c.decimal_places,0),NEW.cash_minor,NEW.cash_minor,unixepoch()
            FROM (SELECT 1) LEFT JOIN country_currencies n ON n.nationality=NEW.nationality
            LEFT JOIN currencies c ON c.code=n.currency_code; END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_customer_cash_ledger_update AFTER UPDATE OF cash_minor ON users
            WHEN NEW.cash_minor<>OLD.cash_minor BEGIN
            INSERT INTO customer_wallet_ledger(source_key,user_id,source_type,source_account,
                currency_code,decimal_places,delta_minor_units,balance_minor_units,recorded_epoch)
            SELECT 'change:cash:'||NEW.user_id||':'||unixepoch()||':'||lower(hex(randomblob(8))),
                NEW.user_id,'cash',NEW.user_id,COALESCE(n.currency_code,base_currency_code()),COALESCE(c.decimal_places,0),
                NEW.cash_minor-OLD.cash_minor,NEW.cash_minor,unixepoch()
            FROM (SELECT 1) LEFT JOIN country_currencies n ON n.nationality=NEW.nationality
            LEFT JOIN currencies c ON c.code=n.currency_code; END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_customer_account_ledger_insert AFTER INSERT ON accounts
            WHEN NEW.balance_minor_units<>0 AND NEW.user_id IS NOT NULL BEGIN
            INSERT OR IGNORE INTO customer_wallet_ledger(source_key,user_id,source_type,source_account,
                currency_code,decimal_places,delta_minor_units,balance_minor_units,recorded_epoch)
            VALUES ('insert:account:'||NEW.account_number,NEW.user_id,'account',NEW.account_number,
                NEW.quote_currency,NEW.balance_decimal_places,NEW.balance_minor_units,NEW.balance_minor_units,unixepoch()); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_customer_account_ledger_update AFTER UPDATE OF balance_minor_units ON accounts
            WHEN NEW.balance_minor_units<>OLD.balance_minor_units AND NEW.user_id IS NOT NULL BEGIN
            INSERT INTO customer_wallet_ledger(source_key,user_id,source_type,source_account,
                currency_code,decimal_places,delta_minor_units,balance_minor_units,recorded_epoch)
            VALUES ('change:account:'||NEW.account_number||':'||unixepoch()||':'||lower(hex(randomblob(8))),
                NEW.user_id,'account',NEW.account_number,NEW.quote_currency,NEW.balance_decimal_places,
                NEW.balance_minor_units-OLD.balance_minor_units,NEW.balance_minor_units,unixepoch()); END""")
        # Opening rows are seeded before the posting trigger is installed on a
        # first boot. Backfill their balanced common entries idempotently.
        self.execute("""INSERT OR IGNORE INTO accounting_entries
            (ledger_scope,entity_type,entity_id,source_key,currency_code,decimal_places,recorded_epoch,description)
            SELECT 'customer','wallet_holder',user_id,'wallet:'||entry_id,currency_code,decimal_places,
                recorded_epoch,source_type||' balance movement'
            FROM customer_wallet_ledger""")
        self.execute("""INSERT OR IGNORE INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
            SELECT e.entry_id,'customer_funds_payable',
                CASE WHEN w.delta_minor_units<0 THEN -w.delta_minor_units ELSE 0 END,
                CASE WHEN w.delta_minor_units>0 THEN w.delta_minor_units ELSE 0 END
            FROM customer_wallet_ledger w JOIN accounting_entries e
              ON e.ledger_scope='customer' AND e.entity_type='wallet_holder'
             AND e.entity_id=w.user_id AND e.source_key='wallet:'||w.entry_id""")
        self.execute("""INSERT OR IGNORE INTO accounting_lines(entry_id,account_code,debit_units,credit_units)
            SELECT e.entry_id,'wallet_clearing',
                CASE WHEN w.delta_minor_units>0 THEN w.delta_minor_units ELSE 0 END,
                CASE WHEN w.delta_minor_units<0 THEN -w.delta_minor_units ELSE 0 END
            FROM customer_wallet_ledger w JOIN accounting_entries e
              ON e.ledger_scope='customer' AND e.entity_type='wallet_holder'
             AND e.entity_id=w.user_id AND e.source_key='wallet:'||w.entry_id""")
        # Install the immutable Treasury-bond cash subledger during writable
        # startup. Keep the database package independent from manager modules;
        # read-only reports only query this table during reconciliation.
        self.execute("""CREATE TABLE IF NOT EXISTS treasury_bond_cash_events (
            event_key TEXT PRIMARY KEY,
            event_type TEXT NOT NULL CHECK(event_type IN ('coupon','redemption')),
            holding_id INTEGER NOT NULL, product_code TEXT NOT NULL, user_id TEXT NOT NULL,
            currency_code TEXT NOT NULL, decimal_places INTEGER NOT NULL
                CHECK(decimal_places BETWEEN 0 AND 6),
            principal_minor_units INTEGER NOT NULL CHECK(typeof(principal_minor_units)='integer' AND principal_minor_units>=0),
            interest_minor_units INTEGER NOT NULL CHECK(typeof(interest_minor_units)='integer' AND interest_minor_units>=0),
            tax_minor_units INTEGER NOT NULL CHECK(typeof(tax_minor_units)='integer' AND tax_minor_units>=0),
            net_minor_units INTEGER NOT NULL CHECK(typeof(net_minor_units)='integer' AND net_minor_units>=0),
            recorded_epoch INTEGER NOT NULL)""")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_treasury_bond_cash_events_{operation.lower()}_immutable
                BEFORE {operation} ON treasury_bond_cash_events
                BEGIN SELECT RAISE(ABORT,'treasury bond cash events are append-only'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS treasury_bond_cash_link_baseline_state (
            singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
            through_rowid INTEGER NOT NULL CHECK(through_rowid>=0))""")
        self.execute("""INSERT OR IGNORE INTO treasury_bond_cash_link_baseline_state
            (singleton_id,through_rowid)
            SELECT 1,COALESCE(MAX(rowid),0) FROM treasury_bond_cash_events""")
        self.execute("""CREATE TABLE IF NOT EXISTS treasury_bond_cash_link_baselines (
            event_key TEXT PRIMARY KEY REFERENCES treasury_bond_cash_events(event_key),
            reason TEXT NOT NULL DEFAULT 'pre-link historical bond cash event')""")
        self.execute("""INSERT OR IGNORE INTO treasury_bond_cash_link_baselines(event_key)
            SELECT event_key FROM treasury_bond_cash_events
            WHERE rowid<=(SELECT through_rowid FROM treasury_bond_cash_link_baseline_state
                         WHERE singleton_id=1)""")
        self.execute("""CREATE TABLE IF NOT EXISTS treasury_bond_cash_wallet_links (
            event_key TEXT PRIMARY KEY REFERENCES treasury_bond_cash_events(event_key),
            wallet_entry_id INTEGER NOT NULL UNIQUE REFERENCES customer_wallet_ledger(entry_id))""")
        self.execute("""CREATE TABLE IF NOT EXISTS treasury_bond_cash_fund_links (
            event_key TEXT PRIMARY KEY REFERENCES treasury_bond_cash_events(event_key),
            fund_entry_id INTEGER NOT NULL UNIQUE REFERENCES fund_cash_ledger(entry_id))""")
        for table, message in (
            ("treasury_bond_cash_link_baselines", "treasury bond cash baselines are append-only"),
            ("treasury_bond_cash_wallet_links", "treasury bond wallet links are append-only"),
            ("treasury_bond_cash_fund_links", "treasury bond fund links are append-only"),
            ("treasury_bond_cash_link_baseline_state", "treasury bond baseline watermark is immutable"),
        ):
            for operation in ("UPDATE", "DELETE"):
                self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_{operation.lower()}
                    BEFORE {operation} ON {table}
                    BEGIN SELECT RAISE(ABORT,'{message}'); END""")

    def _ensure_treasury_bond_minor_units(self):
        """Make bond offer and lot cash amounts authoritative in quote-currency units."""
        from functions.money import currency_precision, to_minor_units

        fields = {
            "treasury_bond_products": ("product_code", "quote_currency", (
                "face_value", "market_price", "total_supply", "sold_amount")),
            "treasury_bond_holdings": ("holding_id", "product_code", (
                "principal", "face_value", "acquisition_cost")),
        }
        for table, (key, currency_field, amounts) in fields.items():
            columns = {row[1] for row in self.execute(f'PRAGMA table_info("{table}")')}
            if not columns or any(amount not in columns for amount in amounts):
                continue
            for amount in amounts:
                minor = f"{amount}_minor_units"
                if minor not in columns:
                    self.execute(
                        f'ALTER TABLE "{table}" ADD COLUMN "{minor}" INTEGER NOT NULL DEFAULT 0'
                    )
            if table == "treasury_bond_products":
                scale_expr = (
                    "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code="
                    "NEW.quote_currency),0) WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
                )
                face_minor = f"CAST(ROUND(NEW.face_value*({scale_expr})) AS INTEGER)"
                supply_minor = f"CAST(ROUND(NEW.total_supply*({scale_expr})) AS INTEGER)"
                sold_minor = f"CAST(ROUND(NEW.sold_amount*({scale_expr})) AS INTEGER)"
                self.execute("DROP TRIGGER IF EXISTS validate_treasury_bond_insert")
                self.execute("DROP TRIGGER IF EXISTS validate_treasury_bond_update")
                self.execute(f"""CREATE TRIGGER validate_treasury_bond_insert
                    BEFORE INSERT ON treasury_bond_products
                    WHEN {face_minor}<=0 OR {supply_minor}<=0
                      OR {supply_minor}%{face_minor}<>0 OR {sold_minor}<0
                      OR {sold_minor}>{supply_minor} OR {sold_minor}%{face_minor}<>0
                    BEGIN SELECT RAISE(ABORT,'국채 발행한도와 판매액은 양의 액면가 배수여야 합니다.'); END""")
                self.execute(f"""CREATE TRIGGER validate_treasury_bond_update
                    BEFORE UPDATE OF face_value,total_supply,sold_amount,quote_currency
                    ON treasury_bond_products
                    WHEN {face_minor}<=0 OR {supply_minor}<=0
                      OR {supply_minor}%{face_minor}<>0 OR {sold_minor}<0
                      OR {sold_minor}>{supply_minor} OR {sold_minor}%{face_minor}<>0
                    BEGIN SELECT RAISE(ABORT,'국채 발행한도와 판매액은 양의 액면가 배수여야 합니다.'); END""")
                rows = self.execute(
                    "SELECT product_code,quote_currency,face_value,market_price,total_supply,"
                    "sold_amount,face_value_minor_units,market_price_minor_units,"
                    "total_supply_minor_units,sold_amount_minor_units FROM treasury_bond_products"
                )
                for row in rows:
                    places = currency_precision(self.conn, row["quote_currency"] or base_currency_code(self.conn))
                    for amount in amounts:
                        minor = f"{amount}_minor_units"
                        if not int(row[minor] or 0) and float(row[amount] or 0):
                            self.execute(
                                f"UPDATE treasury_bond_products SET {minor}=? WHERE product_code=?",
                                (to_minor_units(row[amount], places), row["product_code"]),
                            )
                currency_sql = "NEW.quote_currency"
            else:
                rows = self.execute(
                    "SELECT h.holding_id,h.product_code,p.quote_currency,h.principal,h.face_value,"
                    "h.acquisition_cost,h.principal_minor_units,h.face_value_minor_units,"
                    "h.acquisition_cost_minor_units FROM treasury_bond_holdings h "
                    "JOIN treasury_bond_products p USING(product_code)"
                )
                for row in rows:
                    places = currency_precision(self.conn, row["quote_currency"] or base_currency_code(self.conn))
                    for amount in amounts:
                        minor = f"{amount}_minor_units"
                        if not int(row[minor] or 0) and float(row[amount] or 0):
                            self.execute(
                                f"UPDATE treasury_bond_holdings SET {minor}=? WHERE holding_id=?",
                                (to_minor_units(row[amount], places), row["holding_id"]),
                            )
                currency_sql = "(SELECT quote_currency FROM treasury_bond_products " \
                               "WHERE product_code=NEW.product_code)"
            scale = (
                "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code="
                f"({currency_sql})),0) WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
            )
            for amount in amounts:
                minor = f"{amount}_minor_units"
                stem = f"trg_{table}_{amount}_minor_units"
                for suffix in ("from_legacy", "from_legacy_update", "to_legacy"):
                    self.execute(f"DROP TRIGGER IF EXISTS {stem}_{suffix}")
                self.execute(f"""CREATE TRIGGER {stem}_from_legacy AFTER INSERT ON {table}
                    BEGIN UPDATE {table} SET {minor}=
                      CAST(ROUND(COALESCE(NEW.{amount},0)*({scale})) AS INTEGER)
                      WHERE {key}=NEW.{key}; END""")
                self.execute(f"""CREATE TRIGGER {stem}_from_legacy_update
                    AFTER UPDATE OF {amount},{currency_field if table == 'treasury_bond_products' else 'product_code'} ON {table}
                    BEGIN UPDATE {table} SET {minor}=
                      CAST(ROUND(COALESCE(NEW.{amount},0)*({scale})) AS INTEGER)
                      WHERE {key}=NEW.{key}; END""")
                self.execute(f"""CREATE TRIGGER {stem}_to_legacy AFTER UPDATE OF {minor} ON {table}
                    BEGIN UPDATE {table} SET {amount}=CAST(NEW.{minor} AS REAL)/({scale})
                      WHERE {key}=NEW.{key}; END""")

    def _ensure_minor_amount_snapshots(self, direct, derived):
        """Maintain a currency-minor-unit lineage row for every configured amount.

        Operational schemas remain compatible while migration proceeds. This
        register is the auditable integer representation used by reconciliation
        and provides a complete inventory of the explicitly classified money
        columns; rates, quantities and timestamps are intentionally excluded.
        """
        self.execute("""CREATE TABLE IF NOT EXISTS financial_amount_snapshots (
            source_table TEXT NOT NULL, source_key_field TEXT NOT NULL,
            source_key TEXT NOT NULL, amount_field TEXT NOT NULL,
            currency_code TEXT NOT NULL, decimal_places INTEGER NOT NULL
                CHECK(decimal_places BETWEEN 0 AND 6), amount_minor_units INTEGER NOT NULL
                CHECK(typeof(amount_minor_units)='integer'),
            PRIMARY KEY(source_table,source_key,amount_field))""")
        self.execute("""CREATE TABLE IF NOT EXISTS financial_amount_field_registry (
            source_table TEXT NOT NULL, source_key_field TEXT NOT NULL,
            amount_field TEXT NOT NULL, currency_expression TEXT NOT NULL,
            PRIMARY KEY(source_table,source_key_field,amount_field))""")
        snapshot_columns = {row[1] for row in self.execute(
            "PRAGMA table_info(financial_amount_snapshots)")}
        if "source_key_field" not in snapshot_columns:
            self.execute("ALTER TABLE financial_amount_snapshots ADD COLUMN source_key_field TEXT NOT NULL DEFAULT ''")
        specifications = [(table, key, currency, set(columns))
                          for table, columns, key, currency in (*direct, *derived)]
        rowid_tables = {str(row[1]) for row in self.execute("PRAGMA table_list")
                        if len(row) > 4 and int(row[4]) == 0}
        from functions.money import to_minor_units
        for spec_index, (table, key, currency_expr, columns) in enumerate(specifications):
            available = {row[1] for row in self.execute(f'PRAGMA table_info("{table}")')}
            if key == "rowid" and table in rowid_tables:
                available.add("rowid")
            key_col = key if key in available else None
            if not key_col:
                continue
            amount_cols = sorted(columns & available)
            if not amount_cols:
                continue
            for amount_col in amount_cols:
                self.execute("""INSERT INTO financial_amount_field_registry
                    (source_table,source_key_field,amount_field,currency_expression)
                    VALUES (?,?,?,?) ON CONFLICT(source_table,source_key_field,amount_field)
                    DO UPDATE SET currency_expression=excluded.currency_expression""",
                    (table, key_col, amount_col, currency_expr))
            rows = self.execute(f'SELECT "{key_col}",{",".join(chr(34)+c+chr(34) for c in amount_cols)} FROM "{table}"')
            for row in rows:
                # Existing-row currency lookup uses the row context; handle the
                # direct and derived forms without interpolating data values.
                if currency_expr == "NEW.quote_currency":
                    currency_value = self.execute_one(
                        f'SELECT COALESCE(quote_currency,base_currency_code()) FROM "{table}" WHERE "{key_col}"=?',
                        (row[0],),
                    )[0]
                else:
                    sql_expr = currency_expr.replace("NEW.", f'"{table}".')
                    currency_value = self.execute_one(
                        f'SELECT COALESCE(({sql_expr}),base_currency_code()) FROM "{table}" WHERE "{key_col}"=?',
                        (row[0],),
                    )[0]
                precision = self.execute_one(
                    "SELECT decimal_places FROM currencies WHERE code=?", (str(currency_value or base_currency_code()).upper(),)
                )
                currency_value = str(currency_value or base_currency_code()).upper()
                places = int(precision[0]) if precision else 0
                for index, amount_col in enumerate(amount_cols, start=1):
                    amount = row[index]
                    if amount is None:
                        continue
                    units = to_minor_units(amount, places)
                    self.execute("""INSERT INTO financial_amount_snapshots
                        (source_table,source_key_field,source_key,amount_field,currency_code,decimal_places,amount_minor_units)
                        VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_table,source_key,amount_field)
                        DO UPDATE SET currency_code=excluded.currency_code,
                        decimal_places=excluded.decimal_places,amount_minor_units=excluded.amount_minor_units,
                        source_key_field=excluded.source_key_field""",
                        (table, key_col, str(row[0]), amount_col, currency_value, places, units))
            for amount_col in amount_cols:
                code_expr = currency_expr.replace("NEW.", "NEW.")
                places_expr = f"COALESCE((SELECT decimal_places FROM currencies WHERE code=({code_expr})),0)"
                factor_expr = f"CASE {places_expr} WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
                units_expr = f"CAST(ROUND(NEW.\"{amount_col}\"*({factor_expr})) AS INTEGER)"
                for timing, events in (("INSERT", "AFTER INSERT"),
                                       ("UPDATE", f'AFTER UPDATE OF "{amount_col}"')):
                    name = f"trg_amount_snapshot_{table}_{amount_col}_{timing.lower()}"
                    self.execute(f'DROP TRIGGER IF EXISTS "{name}"')
                    self.execute(f"""CREATE TRIGGER "{name}" {events} ON "{table}"
                        WHEN NEW."{amount_col}" IS NOT NULL
                        BEGIN INSERT INTO financial_amount_snapshots
                        (source_table,source_key_field,source_key,amount_field,currency_code,decimal_places,amount_minor_units)
                        VALUES ('{table}','{key_col}',CAST(NEW."{key_col}" AS TEXT),'{amount_col}',
                            COALESCE(({code_expr}),base_currency_code()),{places_expr},{units_expr})
                        ON CONFLICT(source_table,source_key,amount_field) DO UPDATE SET
                            currency_code=excluded.currency_code,decimal_places=excluded.decimal_places,
                            amount_minor_units=excluded.amount_minor_units,
                            source_key_field=excluded.source_key_field; END""")
            if "quote_currency" in available:
                name = f"trg_amount_snapshot_{table}_currency_update_{spec_index}"
                updates = []
                for amount_col in amount_cols:
                    places_expr = "COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0)"
                    factor_expr = f"CASE {places_expr} WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
                    updates.append(f"""INSERT INTO financial_amount_snapshots
                        (source_table,source_key_field,source_key,amount_field,currency_code,decimal_places,amount_minor_units)
                        SELECT '{table}','{key_col}',CAST(NEW."{key_col}" AS TEXT),'{amount_col}',NEW.quote_currency,
                            {places_expr},CAST(ROUND(NEW."{amount_col}"*({factor_expr})) AS INTEGER)
                        WHERE NEW."{amount_col}" IS NOT NULL
                        ON CONFLICT(source_table,source_key,amount_field) DO UPDATE SET
                            currency_code=excluded.currency_code,decimal_places=excluded.decimal_places,
                            amount_minor_units=excluded.amount_minor_units,
                            source_key_field=excluded.source_key_field;""")
                self.execute(f'DROP TRIGGER IF EXISTS "{name}"')
                self.execute(f"CREATE TRIGGER \"{name}\" AFTER UPDATE OF quote_currency ON \"{table}\" "
                             "BEGIN "+" ".join(updates)+" END")

    def register_late_amount_fields(self):
        """Register amount fields whose tables are initialized after CurrencyDB."""
        # Resolve missing currencies from the configured base currency at row
        # insertion time. SQLite does not allow a user-defined function in a
        # DEFAULT expression, so new schemas keep these columns nullable.
        for table, column in (
            ("accounts", "quote_currency"), ("market_assets", "quote_currency"),
            ("company", "quote_currency"), ("investment", "quote_currency"),
            ("saving", "quote_currency"), ("liability", "quote_currency"),
            ("loans", "quote_currency"), ("tax_transactions", "currency_code"),
            ("market_orders", "quote_currency"),
            ("financial_institution_ledger", "currency_code"),
            ("financial_institution_ledger", "base_currency_code"),
        ):
            columns = {row[1] for row in self.execute(f'PRAGMA table_info("{table}")')}
            if column not in columns:
                continue
            self.execute(f'UPDATE "{table}" SET "{column}"=base_currency_code() '
                         f'WHERE "{column}" IS NULL OR "{column}"=\'\'')
            trigger = f"trg_currency_default_{table}_{column}"
            self.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
            self.execute(f'''CREATE TRIGGER "{trigger}" AFTER INSERT ON "{table}"
                WHEN NEW."{column}" IS NULL OR NEW."{column}"=''
                BEGIN UPDATE "{table}" SET "{column}"=base_currency_code()
                WHERE rowid=NEW.rowid; END''')
        asset_currency = (
            "COALESCE((SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code),"
            "(SELECT quote_currency FROM company WHERE company_code=NEW.asset_code),"
            "(SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code),"
            "(SELECT quote_currency FROM treasury_bond_products WHERE product_code=NEW.asset_code),base_currency_code())"
        )
        underlying_currency = (
            "COALESCE((SELECT quote_currency FROM market_assets WHERE code=NEW.underlying_code),"
            "(SELECT quote_currency FROM company WHERE company_code=NEW.underlying_code),"
            "(SELECT quote_currency FROM investment WHERE product_code=NEW.underlying_code),"
            "(SELECT quote_currency FROM treasury_bond_products WHERE product_code=NEW.underlying_code),base_currency_code())"
        )
        corporate_currency = (
            "COALESCE((SELECT quote_currency FROM company WHERE company_code=NEW.company_code),base_currency_code())"
        )
        specifications = (
            ("lottery_tickets", ("price",), "ticket_id", "'NWX'"),
            ("lottery_purchases", ("price",), "request_id", "'NWX'"),
            ("lottery_payouts", ("gross", "tax", "net"), "ticket_id", "'NWX'"),
            ("lottery_pool_ledger", ("amount",), "entry_id", "'NWX'"),
            ("lottery_rounds", ("reserve_total",), "round_id", "'NWX'"),
            ("issuer_cash_accounts", ("balance", "locked_amount"), "company_code", corporate_currency),
            ("issuer_cash_transfers", ("old_balance", "new_balance", "delta"), "transfer_id",
             "SELECT quote_currency FROM company WHERE company_code=NEW.company_code"),
            ("dividend_distributions", ("reference_price", "gross_amount", "entitlement_amount",
                                         "deferred_amount"), "distribution_id", "NEW.quote_currency"),
            ("dividend_payments", ("gross_amount", "tax_amount"), "payment_id", "NEW.quote_currency"),
            ("preferred_dividend_claims", ("gross_amount", "paid_amount"), "claim_id", "NEW.quote_currency"),
            ("corporate_liquidation_payments", ("claim_amount", "paid_amount", "tax_amount"),
             "payment_id", asset_currency),
            ("dr_dividend_withholding_events", ("gross_amount", "tax_amount"), "event_id",
             "NEW.currency_code"),
            ("company_income_log", ("tax_nwx",), "entry_id", "base_currency_code()"),
            ("els_products", ("face_value", "principal_amount", "coupon_amount", "locked_amount"),
             "product_code", "SELECT quote_currency FROM investment WHERE product_code=NEW.product_code"),
            ("els_subscriptions", ("principal", "coupon_reserved"), "subscription_id",
             "SELECT quote_currency FROM investment WHERE product_code=NEW.product_code"),
            ("els_settlements", ("principal", "gross_amount", "tax_amount", "net_amount"),
             "settlement_id", "SELECT quote_currency FROM investment WHERE product_code=NEW.product_code"),
            ("hybrid_bond_events", ("cash",), "event_id",
             "SELECT quote_currency FROM investment WHERE product_code=NEW.product_code"),
            ("product_settlements", ("gross_amount", "tax_amount"), "settlement_id",
             "COALESCE((SELECT quote_currency FROM market_assets WHERE code=NEW.product_code),"
             "(SELECT quote_currency FROM company WHERE company_code=NEW.product_code),"
             "(SELECT quote_currency FROM investment WHERE product_code=NEW.product_code),base_currency_code())"),
            ("corporate_equity_offerings", ("issue_price",), "offering_id", asset_currency),
            ("market_quote_overrides", ("price",), "asset_code",
             "COALESCE((SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code),"
             "(SELECT quote_currency FROM company WHERE company_code=NEW.asset_code),"
             "(SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code),base_currency_code())"),
            ("market_future_expiry_prices", ("price",), "product_code",
             "COALESCE((SELECT quote_currency FROM market_assets WHERE code=NEW.underlying_code),"
             "(SELECT quote_currency FROM company WHERE company_code=NEW.underlying_code),"
             "(SELECT quote_currency FROM investment WHERE product_code=NEW.underlying_code),base_currency_code())"),
            ("market_auto_targets", ("anchor_price", "target_price"), "asset_code", asset_currency),
            ("market_auto_target_history", ("anchor_price", "target_price"), "history_id", asset_currency),
            ("market_simulation_state", ("latent_price", "last_display_price"),
             "asset_code", asset_currency),
            ("market_event_targets", ("start_price", "target_price", "last_price"),
             "rowid", asset_currency),
            ("els_underlyings", ("initial_price", "last_price"), "rowid",
             "COALESCE((SELECT quote_currency FROM market_assets WHERE code=NEW.underlying_code),"
             "(SELECT quote_currency FROM company WHERE company_code=NEW.underlying_code),"
             "(SELECT quote_currency FROM investment WHERE product_code=NEW.underlying_code),base_currency_code())"),
            ("market_exchange_index_components", ("previous_price",), "rowid",
             "SELECT quote_currency FROM company WHERE company_code=NEW.company_code"),
            ("fund_components", ("base_price",), "rowid",
             "COALESCE((SELECT quote_currency FROM company WHERE company_code=NEW.asset_code),"
             "(SELECT quote_currency FROM investment WHERE product_code=NEW.asset_code),"
             "(SELECT quote_currency FROM treasury_bond_products WHERE product_code=NEW.asset_code),"
             "(SELECT quote_currency FROM market_assets WHERE code=NEW.asset_code),base_currency_code())"),
            ("option_contracts", ("strike_price",), "contract_id", underlying_currency),
            ("option_positions", ("average_premium",), "rowid",
             "SELECT COALESCE((SELECT quote_currency FROM market_assets WHERE code=o.underlying_code),"
             "(SELECT quote_currency FROM company WHERE company_code=o.underlying_code),"
             "(SELECT quote_currency FROM investment WHERE product_code=o.underlying_code),base_currency_code()) "
             "FROM option_contracts o WHERE o.contract_id=NEW.contract_id"),
            ("option_collateral_lots", ("average_cost",), "collateral_id", underlying_currency),
            ("option_collateral_cash_lots", ("amount",), "collateral_id",
             "COALESCE((SELECT quote_currency FROM accounts WHERE account_number=NEW.account_number),"
             "(SELECT COALESCE((SELECT quote_currency FROM market_assets WHERE code=o.underlying_code),"
             "(SELECT quote_currency FROM company WHERE company_code=o.underlying_code),"
             "(SELECT quote_currency FROM investment WHERE product_code=o.underlying_code),base_currency_code()) "
             "FROM option_contracts o WHERE o.contract_id=NEW.contract_id),base_currency_code())"),
            ("investment", ("nav_anchor", "conversion_price", "warrant_exercise_price"),
             "product_code", "NEW.quote_currency"),
            ("financial_institution_ledger", ("amount", "base_amount"), "entry_id", "NEW.currency_code"),
            ("bank_deficit_alerts", ("cash",), "issuer", "NEW.cash_currency_code"),
            ("aml_monitoring_rules", ("rolling_24h_amount_limit",), "nationality", "NEW.currency_code"),
            ("deposit_insurance_claims", ("payout_amount",), "claim_id", "NEW.payout_currency"),
            ("futures_positions", ("settlement_price", "collateral", "net_pnl"), "rowid",
             "SELECT quote_currency FROM investment WHERE product_code=NEW.product_code"),
            ("dr_custody_instructions", ("fee_price",), "reference_key",
             "COALESCE(NEW.fee_currency,(SELECT quote_currency FROM depositary_receipts "
             "WHERE dr_code=NEW.dr_code),base_currency_code())"),
            ("leveraged_etf_state", ("base_nav",), "product_code",
             "SELECT quote_currency FROM investment WHERE product_code=NEW.product_code"),
            ("company", ("cumulative_dividend",), "company_code", "NEW.quote_currency"),
            ("currency_transactions", ("fee_nwx",), "transaction_id", "base_currency_code()"),
            ("currency_transfer_transactions", ("fee_nwx",), "transaction_id", "base_currency_code()"),
        )
        with self.lock, self.conn:
            self._ensure_minor_amount_snapshots((), specifications)
            self.execute("""CREATE TABLE IF NOT EXISTS financial_amount_field_exemptions (
                source_table TEXT NOT NULL, amount_field TEXT NOT NULL,
                reason TEXT NOT NULL, PRIMARY KEY(source_table,amount_field))""")
            exemptions = (
                ("saving", "collateral_discount_max", "담보 할인 비율"),
                ("liability", "collateral_discount_max", "담보 할인 비율"),
                ("market_exchanges", "index_value", "통화 금액이 아닌 지수 포인트"),
                ("market_exchange_indices", "index_value", "통화 금액이 아닌 지수 포인트"),
                ("market_exchange_index_history", "index_value", "통화 금액이 아닌 지수 포인트"),
                ("leveraged_etf_state", "base_index_value", "통화 금액이 아닌 기초 지수 포인트"),
                ("users", "income_stability", "소득 안정성 점수"),
                ("credit_score_policy", "value", "신용점수 정책값"),
                ("financial_institutions", "operating_cost_spread", "금액이 아닌 비용 가산율"),
                ("financial_lines", "amount", "이미 회사 원장 통화 최소단위로 저장된 서명 분개액"),
                ("users", "cash_minor", "이미 고객 통화 최소단위로 저장된 권위 잔액"),
                ("accounts", "balance_units", "통화 최소단위에 정렬된 레거시 micro-unit 잔액; balance_minor_units가 권위값"),
                ("market_orders", "reserved_cash_units", "레거시 micro-unit 미러; reserved_cash_minor_units가 권위값"),
                ("market_orders", "executed_value_units", "부분체결 반올림 잔여를 보존하는 micro-unit 중간 누계"),
                ("dividend_payments", "net_amount_units", "레거시 micro-unit 미러; 지급 정산은 고객/펀드 원장 최소단위로 대사"),
                ("dr_dividend_withholding_events", "net_amount_units", "레거시 micro-unit 미러; net_minor_units가 권위값"),
                ("financial_bond_basis", "discount_units", "회사채 발행차금 통화 최소단위 잔액"),
            )
            for exemption in exemptions:
                self.execute("""INSERT INTO financial_amount_field_exemptions
                    (source_table,amount_field,reason) VALUES (?,?,?)
                    ON CONFLICT(source_table,amount_field) DO UPDATE SET reason=excluded.reason""",
                    exemption)
            self.execute("DELETE FROM financial_amount_field_exemptions "
                         "WHERE source_table='market_events' AND amount_field='target_value'")

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
        for table, column in (("market_orders", "reserved_cash"),
                              ("loans", "outstanding_balance"),
                              ("loans", "charged_off_balance")):
            if not self.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)):
                continue
            places = "(SELECT COALESCE(MAX(decimal_places),0) FROM currencies WHERE code=COALESCE(NEW.quote_currency,base_currency_code()))"
            key = "rowid"
            for event, name in ((f"AFTER UPDATE OF {column} ON {table}", f"trg_{table}_{column}_snap"),
                                (f"AFTER INSERT ON {table}", f"trg_{table}_{column}_snap_insert")):
                self.execute(f"DROP TRIGGER IF EXISTS {name}")
                self.execute(f"""CREATE TRIGGER {name} {event}
                    WHEN NEW.{column} IS NOT NULL AND NEW.{column} <> ROUND(NEW.{column}, {places})
                    BEGIN UPDATE {table} SET {column}=ROUND(NEW.{column}, {places}) WHERE {key}=NEW.{key}; END""")

    def _ensure_account_reference_price_units(self):
        columns = {row[1] for row in self.execute("PRAGMA table_info(accounts)")}
        if "reference_price_minor_units" not in columns:
            self.execute("ALTER TABLE accounts ADD COLUMN reference_price_minor_units INTEGER")
        from functions.money import currency_precision, to_minor_units
        for row in self.execute(
            "SELECT account_number,quote_currency,reference_price "
            "FROM accounts WHERE reference_price IS NOT NULL "
            "AND reference_price_minor_units IS NULL"
        ):
            places = currency_precision(self.conn, row["quote_currency"] or base_currency_code(self.conn))
            self.execute(
                "UPDATE accounts SET reference_price_minor_units=? WHERE account_number=?",
                (to_minor_units(row["reference_price"], places), row["account_number"]),
            )
        scale = (
            "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0) "
            "WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
        )
        self.execute("DROP TRIGGER IF EXISTS trg_account_reference_price_from_legacy")
        self.execute("DROP TRIGGER IF EXISTS trg_account_reference_price_legacy_update")
        self.execute("DROP TRIGGER IF EXISTS trg_account_reference_price_minor_mirror")
        self.execute(f"""CREATE TRIGGER trg_account_reference_price_from_legacy
            AFTER INSERT ON accounts WHEN NEW.reference_price IS NOT NULL BEGIN
              UPDATE accounts SET reference_price_minor_units=
                CAST(ROUND(NEW.reference_price*({scale})) AS INTEGER)
              WHERE account_number=NEW.account_number;
            END""")
        self.execute(f"""CREATE TRIGGER trg_account_reference_price_legacy_update
            AFTER UPDATE OF reference_price,quote_currency ON accounts
            WHEN NEW.reference_price IS NOT NULL BEGIN
              UPDATE accounts SET reference_price_minor_units=
                CAST(ROUND(NEW.reference_price*({scale})) AS INTEGER)
              WHERE account_number=NEW.account_number;
            END""")
        mirror_scale = (
            "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0) "
            "WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
        )
        self.execute(f"""CREATE TRIGGER trg_account_reference_price_minor_mirror
            AFTER UPDATE OF reference_price_minor_units ON accounts
            WHEN NEW.reference_price_minor_units IS NOT OLD.reference_price_minor_units
              AND NEW.reference_price_minor_units IS NOT NULL BEGIN
              UPDATE accounts SET reference_price=NEW.reference_price_minor_units*1.0/({mirror_scale})
              WHERE account_number=NEW.account_number;
            END""")

    def _ensure_aml_amount_units(self):
        from functions.money import currency_precision, to_minor_units
        for row in self.execute(
            "SELECT alert_id,currency_code,amount,amount_minor_units "
            "FROM aml_alerts WHERE amount_minor_units=0 AND amount<>0"
        ):
            places = currency_precision(self.conn, row["currency_code"])
            self.execute(
                "UPDATE aml_alerts SET amount_minor_units=?,amount_decimal_places=? "
                "WHERE alert_id=?",
                (to_minor_units(row["amount"], places), places, row["alert_id"]),
            )
        for trigger in (
            "trg_aml_amount_minor_insert", "trg_aml_amount_minor_update",
            "trg_aml_amount_minor_mirror",
        ):
            self.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        scale = (
            "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.currency_code),0) "
            "WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
        )
        self.execute(f"""CREATE TRIGGER trg_aml_amount_minor_insert
            AFTER INSERT ON aml_alerts BEGIN
              UPDATE aml_alerts SET amount_decimal_places=COALESCE((SELECT decimal_places
                FROM currencies WHERE code=NEW.currency_code),0),
                amount_minor_units=CAST(ROUND(NEW.amount*({scale})) AS INTEGER)
              WHERE alert_id=NEW.alert_id;
            END""")
        self.execute(f"""CREATE TRIGGER trg_aml_amount_minor_update
            AFTER UPDATE OF amount,currency_code ON aml_alerts
            WHEN NEW.amount IS NOT OLD.amount OR NEW.currency_code IS NOT OLD.currency_code BEGIN
              UPDATE aml_alerts SET amount_decimal_places=COALESCE((SELECT decimal_places
                FROM currencies WHERE code=NEW.currency_code),0),
                amount_minor_units=CAST(ROUND(NEW.amount*({scale})) AS INTEGER)
              WHERE alert_id=NEW.alert_id;
            END""")
        mirror_scale = (
            "CASE COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.currency_code),0) "
            "WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END"
        )
        self.execute(f"""CREATE TRIGGER trg_aml_amount_minor_mirror
            AFTER UPDATE OF amount_minor_units ON aml_alerts
            WHEN NEW.amount_minor_units IS NOT OLD.amount_minor_units BEGIN
              UPDATE aml_alerts SET amount=NEW.amount_minor_units*1.0/({mirror_scale})
              WHERE alert_id=NEW.alert_id;
            END""")

    def _ensure_dr_deviation_price_units(self):
        from functions.money import currency_precision, to_minor_units
        sample_rows = self.execute(
            "SELECT s.sample_id,s.dr_price,s.reference_value,s.dr_price_minor_units,"
            "s.reference_value_minor_units,d.quote_currency FROM dr_deviation_samples s "
            "JOIN depositary_receipts d ON d.dr_code=s.dr_code"
        )
        for row in sample_rows:
            places = currency_precision(self.conn, row["quote_currency"] or base_currency_code(self.conn))
            updates = []
            if row["dr_price"] is not None and row["dr_price_minor_units"] is None:
                updates.append(("dr_price_minor_units", to_minor_units(row["dr_price"], places)))
            if row["reference_value"] is not None and row["reference_value_minor_units"] is None:
                updates.append(("reference_value_minor_units", to_minor_units(row["reference_value"], places)))
            if updates:
                assignments = ",".join(f"{field}=?" for field, _ in updates)
                self.execute(f"UPDATE dr_deviation_samples SET {assignments} WHERE sample_id=?",
                             (*(value for _, value in updates), row["sample_id"]))
        event_rows = self.execute(
            "SELECT e.event_id,e.dr_price,e.reference_value,e.dr_price_minor_units,"
            "e.reference_value_minor_units,d.quote_currency FROM dr_deviation_alert_events e "
            "JOIN depositary_receipts d ON d.dr_code=e.dr_code"
        )
        if event_rows:
            self.execute("DROP TRIGGER IF EXISTS trg_dr_deviation_alert_immutable_update")
            try:
                for row in event_rows:
                    places = currency_precision(self.conn, row["quote_currency"] or base_currency_code(self.conn))
                    updates = []
                    if row["dr_price"] is not None and row["dr_price_minor_units"] is None:
                        updates.append(("dr_price_minor_units", to_minor_units(row["dr_price"], places)))
                    if row["reference_value"] is not None and row["reference_value_minor_units"] is None:
                        updates.append(("reference_value_minor_units", to_minor_units(row["reference_value"], places)))
                    if updates:
                        assignments = ",".join(f"{field}=?" for field, _ in updates)
                        self.execute(f"UPDATE dr_deviation_alert_events SET {assignments} WHERE event_id=?",
                                     (*(value for _, value in updates), row["event_id"]))
            finally:
                self.execute("""CREATE TRIGGER IF NOT EXISTS trg_dr_deviation_alert_immutable_update
                    BEFORE UPDATE ON dr_deviation_alert_events
                    BEGIN SELECT RAISE(ABORT,'DR deviation alert events are append-only'); END""")

    def _ensure_dr_movement_cost_units(self):
        """Migrate the three denomination-specific DR custody cost snapshots."""
        from functions.money import currency_precision, to_minor_units
        rows = self.execute(
            "SELECT m.*,d.underlying_code,c.quote_currency AS underlying_currency "
            "FROM depositary_receipt_movements m JOIN depositary_receipts d USING(dr_code) "
            "LEFT JOIN company c ON c.company_code=d.underlying_code"
        )
        if rows:
            self.execute("DROP TRIGGER IF EXISTS trg_dr_movements_immutable_update")
            try:
                for row in rows:
                    currencies = {
                        "underlying_average_cost": str(row["underlying_average_cost_currency"]
                            or row["underlying_currency"] or base_currency_code(self.conn)).upper(),
                        "holder_underlying_average_cost": str(row["holder_underlying_currency"]
                            or row["underlying_currency"] or base_currency_code(self.conn)).upper(),
                        "custody_underlying_average_cost": str(row["custody_underlying_currency"]
                            or row["underlying_currency"] or base_currency_code(self.conn)).upper(),
                    }
                    updates = []
                    for field, currency in currencies.items():
                        places = currency_precision(self.conn, currency)
                        amount = row[field]
                        if amount is None:
                            continue
                        minor_field = f"{field}_minor_units"
                        places_field = f"{field}_decimal_places"
                        if row[minor_field] is None or row[places_field] is None:
                            updates.extend(((minor_field, to_minor_units(amount, places)),
                                            (places_field, places)))
                        if field == "underlying_average_cost" and row["underlying_average_cost_currency"] is None:
                            updates.append(("underlying_average_cost_currency", currency))
                        if field == "holder_underlying_average_cost" and row["holder_underlying_currency"] is None:
                            updates.append(("holder_underlying_currency", currency))
                        if field == "custody_underlying_average_cost" and row["custody_underlying_currency"] is None:
                            updates.append(("custody_underlying_currency", currency))
                    if updates:
                        assignments = ",".join(f'"{field}"=?' for field, _ in updates)
                        self.execute(f"UPDATE depositary_receipt_movements SET {assignments} WHERE movement_id=?",
                                     (*(value for _, value in updates), row["movement_id"]))
            finally:
                self.execute("""CREATE TRIGGER IF NOT EXISTS trg_dr_movements_immutable_update
                    BEFORE UPDATE ON depositary_receipt_movements
                    BEGIN SELECT RAISE(ABORT,'DR custody movements are append-only'); END""")

        fields = (
            ("underlying_average_cost", "underlying_average_cost_minor_units",
             "underlying_average_cost_decimal_places", "underlying_average_cost_currency",
             "(SELECT quote_currency FROM company WHERE company_code=(SELECT underlying_code "
             "FROM depositary_receipts WHERE dr_code=NEW.dr_code))"),
            ("holder_underlying_average_cost", "holder_underlying_average_cost_minor_units",
             "holder_underlying_average_cost_decimal_places", "holder_underlying_currency",
             "NEW.holder_underlying_currency"),
            ("custody_underlying_average_cost", "custody_underlying_average_cost_minor_units",
             "custody_underlying_average_cost_decimal_places", "custody_underlying_currency",
             "NEW.custody_underlying_currency"),
        )
        checks = []
        for amount, minor, places, currency_col, currency_expr in fields:
            currency = f"COALESCE({currency_expr},base_currency_code())"
            scale = "CASE COALESCE(NEW.\"%s\",0) " % places + " ".join(
                f"WHEN {i} THEN {10**i}" for i in range(0, 7)) + " END"
            checks.append(
                f"NEW.\"{minor}\" IS NULL OR NEW.\"{places}\" IS NULL "
                f"OR NEW.\"{currency_col}\" IS NOT {currency} "
                f"OR NEW.\"{places}\"<>COALESCE((SELECT decimal_places FROM currencies "
                f"WHERE code={currency}),-1) OR NEW.\"{minor}\"<>CAST(ROUND(NEW.\"{amount}\"*({scale})) AS INTEGER)"
            )
        self.execute("DROP TRIGGER IF EXISTS trg_dr_movements_cost_units_insert")
        self.execute("""CREATE TRIGGER trg_dr_movements_cost_units_insert
            BEFORE INSERT ON depositary_receipt_movements WHEN """ + " OR ".join(checks) + "\n"
            "BEGIN SELECT RAISE(ABORT,'DR movement cost minor units are missing or inconsistent'); END")

    def _ensure_bank_deficit_alert_units(self):
        """Pin a deficit warning to the bank account's home-currency quantum."""
        from functions.money import currency_precision, to_minor_units
        rows = self.execute(
            "SELECT a.issuer,a.cash,a.cash_minor_units,a.cash_currency_code,"
            "u.nationality,c.currency_code FROM bank_deficit_alerts a "
            "JOIN bank_accounts b ON b.issuer=a.issuer JOIN users u ON u.user_id=b.user_id "
            "LEFT JOIN country_currencies c ON c.nationality=u.nationality"
        )
        for row in rows:
            currency = str(row["cash_currency_code"] or row["currency_code"] or base_currency_code(self.conn)).upper()
            places = currency_precision(self.conn, currency)
            if row["cash_minor_units"] is None or row["cash_currency_code"] is None:
                self.execute(
                    "UPDATE bank_deficit_alerts SET cash_minor_units=?,cash_decimal_places=?,"
                    "cash_currency_code=? WHERE issuer=?",
                    (to_minor_units(row["cash"], places), places, currency, row["issuer"]),
                )
        factor = "CASE COALESCE(NEW.cash_decimal_places,0) WHEN 0 THEN 1 " \
                 "WHEN 1 THEN 10 ELSE 100 END"
        currency_sql = "COALESCE((SELECT currency_code FROM country_currencies WHERE nationality=" \
                       "(SELECT nationality FROM users WHERE user_id=" \
                       "(SELECT user_id FROM bank_accounts WHERE issuer=NEW.issuer))), base_currency_code())"
        self.execute("DROP TRIGGER IF EXISTS trg_bank_deficit_alert_units_insert")
        self.execute(f"""CREATE TRIGGER trg_bank_deficit_alert_units_insert
            AFTER INSERT ON bank_deficit_alerts WHEN NEW.cash_minor_units IS NULL
            BEGIN UPDATE bank_deficit_alerts SET cash_currency_code={currency_sql},
                cash_decimal_places=COALESCE((SELECT decimal_places FROM currencies
                    WHERE code={currency_sql}),0),
                cash_minor_units=CAST(ROUND(NEW.cash*(CASE COALESCE((SELECT decimal_places
                    FROM currencies WHERE code={currency_sql}),0) WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END)) AS INTEGER)
                WHERE issuer=NEW.issuer; END""")
        self.execute("DROP TRIGGER IF EXISTS trg_bank_deficit_alert_units_update")
        self.execute(f"""CREATE TRIGGER trg_bank_deficit_alert_units_update
            AFTER UPDATE OF cash ON bank_deficit_alerts WHEN NEW.cash IS NOT OLD.cash
            BEGIN UPDATE bank_deficit_alerts SET cash_currency_code={currency_sql},
                cash_decimal_places=COALESCE((SELECT decimal_places FROM currencies
                    WHERE code={currency_sql}),0),
                cash_minor_units=CAST(ROUND(NEW.cash*(CASE COALESCE((SELECT decimal_places
                    FROM currencies WHERE code={currency_sql}),0) WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END)) AS INTEGER)
                WHERE issuer=NEW.issuer; END""")
        self.execute("DROP TRIGGER IF EXISTS trg_bank_deficit_alert_units_mirror")
        self.execute("""CREATE TRIGGER trg_bank_deficit_alert_units_mirror
            AFTER UPDATE OF cash_minor_units ON bank_deficit_alerts
            WHEN NEW.cash_minor_units IS NOT OLD.cash_minor_units
            BEGIN UPDATE bank_deficit_alerts SET cash=NEW.cash_minor_units*1.0/
                (CASE COALESCE(NEW.cash_decimal_places,0) WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END)
                WHERE issuer=NEW.issuer; END""")

    def _ensure_account_units(self):
        """Keep legacy micro-unit balances and product-minor-unit mirrors aligned.

        Savings used to keep their amount in balance while foreign-currency
        accounts used balance_units. Existing paths still use 1/1,000,000 units;
        balance_minor_units is the canonical currency-precision accounting mirror.
        The legacy balance column remains a compatibility view for readers.
        """
        columns = {row[1] for row in self.execute("PRAGMA table_info(accounts)")}
        if "balance_units" not in columns:
            return
        migrated = self.execute_one("SELECT 1 FROM settings WHERE key='account_units_v1'") \
            if self.execute_one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'") else True
        for name in ("trg_accounts_units_mirror", "trg_accounts_units_insert", "trg_accounts_balance_guard",
                     "trg_accounts_minor_units_update", "trg_accounts_minor_units_insert",
                     "trg_accounts_minor_units_currency"):
            self.execute(f"DROP TRIGGER IF EXISTS {name}")
        if not migrated:
            self.execute("UPDATE accounts SET balance_units=CAST(ROUND(COALESCE(balance,0)*1000000) AS INTEGER) "
                         "WHERE account_type<>'currency_saving'")
            self.execute("INSERT INTO settings(key,value) VALUES ('account_units_v1', ?)", (str(int(time.time())),))
        # Historical account credits could retain fractional micro-units of a
        # cent after old interest paths. Round once during migration; ongoing
        # balance writes are rejected unless aligned to the account currency.
        legacy_places = "COALESCE((SELECT decimal_places FROM currencies WHERE code=accounts.quote_currency),0)"
        legacy_factor = f"CASE {legacy_places} WHEN 0 THEN 1000000 WHEN 1 THEN 100000 ELSE 10000 END"
        self.execute(f"UPDATE accounts SET balance_units=CAST(ROUND(balance_units*1.0/({legacy_factor}))*({legacy_factor}) AS INTEGER) "
                     f"WHERE balance_units%({legacy_factor})<>0")
        places_from_currency = "COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0)"
        factor_from_currency = f"CASE {places_from_currency} WHEN 0 THEN 1000000 WHEN 1 THEN 100000 ELSE 10000 END"
        self.execute("DROP TRIGGER IF EXISTS trg_accounts_currency_quantum_insert")
        self.execute("DROP TRIGGER IF EXISTS trg_accounts_currency_quantum_update")
        self.execute("DROP TRIGGER IF EXISTS trg_accounts_currency_quantum_change")
        self.execute(f"""CREATE TRIGGER trg_accounts_currency_quantum_insert BEFORE INSERT ON accounts
            WHEN NEW.balance_units%({factor_from_currency})<>0
            BEGIN SELECT RAISE(ABORT,'Account balance must align to currency minor units'); END""")
        self.execute(f"""CREATE TRIGGER trg_accounts_currency_quantum_update BEFORE UPDATE OF balance_units ON accounts
            WHEN NEW.balance_units%({factor_from_currency})<>0
            BEGIN SELECT RAISE(ABORT,'Account balance must align to currency minor units'); END""")
        self.execute(f"""CREATE TRIGGER trg_accounts_currency_quantum_change BEFORE UPDATE OF quote_currency ON accounts
            WHEN NEW.balance_units%({factor_from_currency})<>0
            BEGIN SELECT RAISE(ABORT,'Account balance must align to destination currency minor units'); END""")
        self.execute("""CREATE TRIGGER trg_accounts_units_mirror AFTER UPDATE OF balance_units ON accounts
            WHEN NEW.balance_units IS NOT OLD.balance_units
            BEGIN UPDATE accounts SET balance=NEW.balance_units/1000000.0 WHERE account_number=NEW.account_number; END""")
        # Inserts that still give only balance (older code paths) are converted once.
        insert_places = "COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0)"
        self.execute(f"""CREATE TRIGGER trg_accounts_units_insert AFTER INSERT ON accounts
            BEGIN UPDATE accounts SET balance_units=CASE WHEN NEW.balance_units=0 AND COALESCE(NEW.balance,0)<>0
                     THEN CAST(ROUND(ROUND(NEW.balance,{insert_places})*1000000) AS INTEGER)
                     ELSE NEW.balance_units END
                 WHERE account_number=NEW.account_number; END""")
        # Writing balance without balance_units would desynchronise the two; reject it.
        self.execute("""CREATE TRIGGER trg_accounts_balance_guard BEFORE UPDATE OF balance ON accounts
            WHEN NEW.balance_units IS OLD.balance_units
             AND ABS(COALESCE(NEW.balance,0) - NEW.balance_units/1000000.0) > 0.500001
            BEGIN SELECT RAISE(ABORT, 'Account balances are stored in balance_units; update balance_units instead'); END""")
        self.execute("UPDATE accounts SET balance=balance_units/1000000.0 "
                     "WHERE ABS(COALESCE(balance,0) - balance_units/1000000.0) > 0.0000001")
        account_columns = {row[1] for row in self.execute("PRAGMA table_info(accounts)")}
        if {"balance_minor_units", "balance_decimal_places"}.issubset(account_columns):
            # Keep a canonical whole-minor-unit accounting mirror beside the
            # legacy micro-unit operational field. Precision is snapshotted on
            # account creation / currency change, not rewritten retroactively.
            self.execute("""UPDATE accounts SET balance_decimal_places=COALESCE(
                (SELECT decimal_places FROM currencies WHERE code=accounts.quote_currency),0)""")
            scale = "CASE {p} WHEN 0 THEN 1 WHEN 1 THEN 10 WHEN 2 THEN 100 WHEN 3 THEN 1000 WHEN 4 THEN 10000 WHEN 5 THEN 100000 ELSE 1000000 END"
            insert_places = "COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0)"
            insert_scale = scale.format(p=insert_places)
            self.execute(f"""UPDATE accounts SET balance_minor_units=CAST(ROUND(
                balance_units / (1000000.0 / ({scale.format(p='balance_decimal_places')}))) AS INTEGER)""")
            self.execute("""CREATE TRIGGER trg_accounts_minor_units_update
                AFTER UPDATE OF balance_units ON accounts
                WHEN NEW.balance_units IS NOT OLD.balance_units
                BEGIN UPDATE accounts SET balance_minor_units=CAST(ROUND(
                    NEW.balance_units / (1000000.0 / (CASE NEW.balance_decimal_places
                    WHEN 0 THEN 1 WHEN 1 THEN 10 WHEN 2 THEN 100 WHEN 3 THEN 1000
                    WHEN 4 THEN 10000 WHEN 5 THEN 100000 ELSE 1000000 END))) AS INTEGER)
                WHERE account_number=NEW.account_number; END""")
            self.execute(f"""CREATE TRIGGER trg_accounts_minor_units_insert AFTER INSERT ON accounts
                BEGIN UPDATE accounts SET
                    balance_decimal_places={insert_places},
                    balance_minor_units=CAST(ROUND(NEW.balance_units / (1000000.0 / ({insert_scale}))) AS INTEGER)
                WHERE account_number=NEW.account_number; END""")
            self.execute("""CREATE TRIGGER trg_accounts_minor_units_currency
                AFTER UPDATE OF quote_currency ON accounts
                WHEN NEW.quote_currency IS NOT OLD.quote_currency
                BEGIN UPDATE accounts SET
                    balance_decimal_places=COALESCE((SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0),
                    balance_minor_units=CAST(ROUND(NEW.balance_units / (1000000.0 / (CASE COALESCE(
                        (SELECT decimal_places FROM currencies WHERE code=NEW.quote_currency),0)
                        WHEN 0 THEN 1 WHEN 1 THEN 10 WHEN 2 THEN 100 WHEN 3 THEN 1000
                        WHEN 4 THEN 10000 WHEN 5 THEN 100000 ELSE 1000000 END))) AS INTEGER)
                WHERE account_number=NEW.account_number; END""")

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
            "equity_capital_minor_units": "INTEGER",
            "equity_capital_decimal_places": "INTEGER",
            "equity_capital_currency_code": "TEXT",
            "retained_earnings": "REAL NOT NULL DEFAULT 0",
            "retained_earnings_minor_units": "INTEGER",
            "retained_earnings_decimal_places": "INTEGER",
            "retained_earnings_currency_code": "TEXT",
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
            "institution_id": "TEXT",
        }.items():
            if column not in institution_columns:
                self.execute(
                    f"ALTER TABLE financial_institutions ADD COLUMN {column} {declaration}"
                )
        # Every bank has a stable unique ID, chosen when it is created or filled at random (BNK + six digits).
        # The default issuer, Inazina, is the bank INAZINA so commands and forms can name it by ID.
        import random
        from functions.banks import DEFAULT_BANK
        default_issuer, default_id = DEFAULT_BANK
        if not self.execute("SELECT 1 FROM financial_institutions WHERE institution_id=?", (default_id,)):
            self.execute("UPDATE financial_institutions SET institution_id=? WHERE issuer=? AND institution_id IS NULL",
                         (default_id, default_issuer))
        taken = {row[0] for row in self.execute(
            "SELECT institution_id FROM financial_institutions WHERE institution_id IS NOT NULL")}
        for (issuer,) in self.execute(
                "SELECT issuer FROM financial_institutions WHERE institution_id IS NULL ORDER BY rowid"):
            bank_id = next(candidate for candidate in iter(lambda: f"BNK{random.randrange(10**6):06d}", None)
                           if candidate not in taken)
            taken.add(bank_id)
            self.execute("UPDATE financial_institutions SET institution_id=? WHERE issuer=?", (bank_id, issuer))
        self.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_financial_institutions_id "
                     "ON financial_institutions(institution_id)")
        # Banks that appear through a product's issuer get a random ID the same way.
        self.execute("DROP TRIGGER IF EXISTS trg_financial_institution_id")
        self.execute("""CREATE TRIGGER trg_financial_institution_id
            AFTER INSERT ON financial_institutions WHEN NEW.institution_id IS NULL BEGIN
                UPDATE financial_institutions SET institution_id=CASE
                    WHEN NEW.issuer='Inazina' AND NOT EXISTS(SELECT 1 FROM financial_institutions WHERE institution_id='INAZINA')
                    THEN 'INAZINA' ELSE printf('BNK%06d', abs(random()) % 1000000) END
                WHERE issuer=NEW.issuer;
            END""")
        from functions.money import currency_precision, to_minor_units
        for row in self.execute(
            "SELECT issuer,nationality,equity_capital FROM financial_institutions "
            "WHERE equity_capital_minor_units IS NULL OR "
            "equity_capital_currency_code IS NULL OR equity_capital_decimal_places IS NULL"
        ):
            denomination = self.execute_one(
                "SELECT currency_code FROM country_currencies WHERE nationality=?",
                (row["nationality"],),
            )
            currency = str(denomination["currency_code"] if denomination else base_currency_code(self.conn)).upper()
            places = currency_precision(self.conn, currency)
            self.execute(
                "UPDATE financial_institutions SET equity_capital_minor_units=?,"
                "equity_capital_decimal_places=?,equity_capital_currency_code=? WHERE issuer=?",
                (to_minor_units(row["equity_capital"], places), places, currency,
                 row["issuer"]),
            )
        for row in self.execute(
            "SELECT issuer,nationality,retained_earnings FROM financial_institutions "
            "WHERE retained_earnings_minor_units IS NULL OR "
            "retained_earnings_currency_code IS NULL OR retained_earnings_decimal_places IS NULL"
        ):
            denomination = self.execute_one(
                "SELECT currency_code FROM country_currencies WHERE nationality=?",
                (row["nationality"],),
            )
            currency = str(denomination["currency_code"] if denomination else base_currency_code(self.conn)).upper()
            places = currency_precision(self.conn, currency)
            self.execute(
                "UPDATE financial_institutions SET retained_earnings_minor_units=?,"
                "retained_earnings_decimal_places=?,retained_earnings_currency_code=? "
                "WHERE issuer=?",
                (to_minor_units(row["retained_earnings"], places), places, currency,
                 row["issuer"]),
            )
        self.execute("DROP TRIGGER IF EXISTS trg_institution_retained_units_insert")
        self.execute("DROP TRIGGER IF EXISTS trg_institution_retained_units_legacy_update")
        self.execute("DROP TRIGGER IF EXISTS trg_institution_retained_units_mirror")
        self.execute("""CREATE TRIGGER trg_institution_retained_units_insert
            AFTER INSERT ON financial_institutions
            WHEN NEW.retained_earnings_minor_units IS NULL BEGIN
              UPDATE financial_institutions SET
                retained_earnings_currency_code=COALESCE((SELECT currency_code
                  FROM country_currencies WHERE nationality=NEW.nationality),base_currency_code()),
                retained_earnings_decimal_places=COALESCE((SELECT decimal_places FROM currencies
                  WHERE code=COALESCE((SELECT currency_code FROM country_currencies
                    WHERE nationality=NEW.nationality),base_currency_code())),0),
                retained_earnings_minor_units=CAST(ROUND(COALESCE(NEW.retained_earnings,0)*
                  CASE COALESCE((SELECT decimal_places FROM currencies WHERE code=COALESCE(
                    (SELECT currency_code FROM country_currencies WHERE nationality=NEW.nationality),
                    base_currency_code())),0) WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END) AS INTEGER)
              WHERE issuer=NEW.issuer;
            END""")
        self.execute("""CREATE TRIGGER trg_institution_retained_units_legacy_update
            AFTER UPDATE OF retained_earnings ON financial_institutions
            WHEN NEW.retained_earnings IS NOT OLD.retained_earnings BEGIN
              UPDATE financial_institutions SET retained_earnings_minor_units=CAST(ROUND(
                COALESCE(NEW.retained_earnings,0)*CASE NEW.retained_earnings_decimal_places
                  WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END) AS INTEGER)
              WHERE issuer=NEW.issuer;
            END""")
        self.execute("""CREATE TRIGGER trg_institution_retained_units_mirror
            AFTER UPDATE OF retained_earnings_minor_units ON financial_institutions
            WHEN NEW.retained_earnings_minor_units IS NOT OLD.retained_earnings_minor_units
            BEGIN UPDATE financial_institutions SET retained_earnings=
              NEW.retained_earnings_minor_units*1.0/CASE NEW.retained_earnings_decimal_places
                WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END WHERE issuer=NEW.issuer; END""")
        self.execute("DROP TRIGGER IF EXISTS trg_institution_equity_units_insert")
        self.execute("DROP TRIGGER IF EXISTS trg_institution_equity_units_legacy_update")
        self.execute("DROP TRIGGER IF EXISTS trg_institution_equity_units_mirror")
        self.execute("""CREATE TRIGGER trg_institution_equity_units_insert
            AFTER INSERT ON financial_institutions
            WHEN NEW.equity_capital_minor_units IS NULL BEGIN
              UPDATE financial_institutions SET
                equity_capital_currency_code=COALESCE((SELECT currency_code
                  FROM country_currencies WHERE nationality=NEW.nationality),base_currency_code()),
                equity_capital_decimal_places=COALESCE((SELECT decimal_places FROM currencies
                  WHERE code=COALESCE((SELECT currency_code FROM country_currencies
                    WHERE nationality=NEW.nationality),base_currency_code())),0),
                equity_capital_minor_units=CAST(ROUND(COALESCE(NEW.equity_capital,0)*
                  CASE COALESCE((SELECT decimal_places FROM currencies WHERE code=COALESCE(
                    (SELECT currency_code FROM country_currencies WHERE nationality=NEW.nationality),
                    base_currency_code())),0) WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END) AS INTEGER)
              WHERE issuer=NEW.issuer;
            END""")
        self.execute("""CREATE TRIGGER trg_institution_equity_units_legacy_update
            AFTER UPDATE OF equity_capital ON financial_institutions
            WHEN NEW.equity_capital IS NOT OLD.equity_capital BEGIN
              UPDATE financial_institutions SET equity_capital_minor_units=CAST(ROUND(
                COALESCE(NEW.equity_capital,0)*CASE NEW.equity_capital_decimal_places
                  WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END) AS INTEGER)
              WHERE issuer=NEW.issuer;
            END""")
        self.execute("""CREATE TRIGGER trg_institution_equity_units_mirror
            AFTER UPDATE OF equity_capital_minor_units ON financial_institutions
            WHEN NEW.equity_capital_minor_units IS NOT OLD.equity_capital_minor_units
              AND NEW.equity_capital_minor_units IS NOT NULL
            BEGIN UPDATE financial_institutions SET equity_capital=
              NEW.equity_capital_minor_units*1.0/CASE NEW.equity_capital_decimal_places
                WHEN 0 THEN 1 WHEN 1 THEN 10 ELSE 100 END WHERE issuer=NEW.issuer; END""")
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
            ("currency_code", "TEXT"),
            ("base_currency_code", "TEXT"),
            ("base_amount", "REAL NOT NULL DEFAULT 0"),
            ("amount_minor_units", "INTEGER"),
            ("base_amount_minor_units", "INTEGER"),
            ("amount_decimal_places", "INTEGER"),
            ("base_decimal_places", "INTEGER"),
        ):
            if column not in ledger_columns:
                self.execute(
                    f"ALTER TABLE financial_institution_ledger ADD COLUMN {column} {declaration}"
                )
        # Losslessly backfill the append-only compatibility ledger once. Keep
        # REAL columns for old readers, but make integer minor units canonical.
        from functions.money import currency_precision, to_minor_units
        legacy_rows = self.execute(
            "SELECT entry_id,amount,currency_code,base_amount,base_currency_code "
            "FROM financial_institution_ledger WHERE amount_minor_units IS NULL "
            "OR base_amount_minor_units IS NULL"
        )
        for row in legacy_rows:
            currency = str(row["currency_code"] or base_currency_code(self.conn)).upper()
            base_currency = str(row["base_currency_code"] or base_currency_code(self.conn)).upper()
            amount_places = currency_precision(self.conn, currency)
            base_places = currency_precision(self.conn, base_currency)
            self.execute(
                "UPDATE financial_institution_ledger SET amount_minor_units=?,"
                "base_amount_minor_units=?,amount_decimal_places=?,base_decimal_places=? "
                "WHERE entry_id=?",
                (to_minor_units(row["amount"], amount_places),
                 to_minor_units(row["base_amount"], base_places), amount_places,
                 base_places, row["entry_id"]),
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
        journal_entry_columns = {
            row[1] for row in self.execute("PRAGMA table_info(bank_journal_entries)")
        }
        if "decimal_places" not in journal_entry_columns:
            self.execute("ALTER TABLE bank_journal_entries ADD COLUMN decimal_places INTEGER")
        from functions.money import currency_precision
        for entry in self.execute(
            "SELECT entry_id,currency_code FROM bank_journal_entries WHERE decimal_places IS NULL"
        ):
            self.execute(
                "UPDATE bank_journal_entries SET decimal_places=? WHERE entry_id=?",
                (currency_precision(self.conn, entry["currency_code"]), entry["entry_id"]),
            )
        self.execute("""CREATE TABLE IF NOT EXISTS bank_journal_lines (
            line_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL REFERENCES bank_journal_entries(entry_id),
            account_code TEXT NOT NULL,
            debit_units INTEGER NOT NULL DEFAULT 0 CHECK(debit_units >= 0),
            credit_units INTEGER NOT NULL DEFAULT 0 CHECK(credit_units >= 0),
            CHECK((debit_units > 0 AND credit_units = 0) OR
                  (credit_units > 0 AND debit_units = 0))
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS bank_journal_accounts (
            account_code TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN
                ('asset','contra_asset','liability','equity','revenue','expense','clearing')),
            normal_side TEXT NOT NULL CHECK(normal_side IN ('debit','credit')),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
        )""")
        bank_accounts = (
            ('settlement_asset','결제자산','asset','debit'),
            ('collateral_recovery_asset','담보회수자산','asset','debit'),
            ('loans_receivable','대출채권','asset','debit'),
            ('insurance_fund_receivable','예금보험기금미수금','asset','debit'),
            ('ecl_allowance','기대신용손실충당금','contra_asset','credit'),
            ('default_loss_allowance','부도손실충당금','contra_asset','credit'),
            ('customer_deposits_payable','고객예금부채','liability','credit'),
            ('customer_funds_payable','고객자금지급채무','liability','credit'),
            ('tax_payable','원천세납부채무','liability','credit'),
            ('treasury_payable','국고지급채무','liability','credit'),
            ('deposit_insurance_payable','예금보험금지급채무','liability','credit'),
            ('uninsured_deposit_claim','미보호예금청구채무','liability','credit'),
            ('fx_conversion_clearing','외환전환청산','clearing','debit'),
            ('interest_income','이자수익','revenue','credit'),
            ('fee_income','수수료수익','revenue','credit'),
            ('custodian_fee_income','수탁수수료수익','revenue','credit'),
            ('credit_loss_recovery_income','신용손실회수수익','revenue','credit'),
            ('impairment_reversal','손상환입수익','revenue','credit'),
            ('interest_expense','이자비용','expense','debit'),
            ('impairment_expense','손상차손','expense','debit'),
            ('credit_loss_expense','신용손실비용','expense','debit'),
            ('dividend_expense','배당비용','expense','debit'),
            ('ima_payout_expense','IMA지급비용','expense','debit'),
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO bank_journal_accounts "
            "(account_code,display_name,category,normal_side) VALUES (?,?,?,?)",
            bank_accounts,
        )
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
                WHERE n.currency_code<>base_currency_code() AND u.cash<>0""").fetchall()
            for row in legacy_cash:
                foreign_account(row['user_id'], base_currency_code(self.conn), int(row['cash']) * SCALE)
                self.conn.execute('UPDATE users SET cash=0 WHERE user_id=?', (row['user_id'],))

            rows = self.conn.execute(
                "SELECT w.user_id,w.currency_code,w.balance_units,"
                "COALESCE(n.currency_code,base_currency_code()) home_currency "
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
