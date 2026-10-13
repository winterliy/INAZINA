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
        self.execute("""CREATE TABLE IF NOT EXISTS guild_countries (
            guild_id TEXT PRIMARY KEY,
            nationality TEXT NOT NULL REFERENCES country_currencies(nationality)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_guild_countries_nationality "
                     "ON guild_countries(nationality)")
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
        if existing and "annual_percent BETWEEN 0 AND 100" in str(existing['sql']):
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
                    (NEW.nationality,'base_rate',NEW.base_rate,unixepoch(),'sql');
                INSERT OR REPLACE INTO country_rate_history VALUES
                    (NEW.nationality,'legal_max_rate',NEW.legal_max_rate,unixepoch(),'sql');
            END""")
        self.execute("""CREATE TRIGGER trg_country_rate_history_update
            AFTER UPDATE OF base_rate,legal_max_rate ON country_currencies BEGIN
                INSERT OR REPLACE INTO country_rate_history
                    SELECT NEW.nationality,'base_rate',NEW.base_rate,unixepoch(),'sql'
                    WHERE NEW.base_rate IS NOT OLD.base_rate;
                INSERT OR REPLACE INTO country_rate_history
                    SELECT NEW.nationality,'legal_max_rate',NEW.legal_max_rate,unixepoch(),'sql'
                    WHERE NEW.legal_max_rate IS NOT OLD.legal_max_rate;
            END""")

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
            "target_capital_ratio": "REAL NOT NULL DEFAULT 0.10 CHECK(target_capital_ratio BETWEEN 0 AND 1)",
            "target_loan_deposit_ratio": "REAL NOT NULL DEFAULT 0.90 CHECK(target_loan_deposit_ratio > 0)",
            "funding_pressure_sensitivity": "REAL NOT NULL DEFAULT 0.50 CHECK(funding_pressure_sensitivity BETWEEN 0 AND 100)",
            "capital_pressure_sensitivity": "REAL NOT NULL DEFAULT 1.00 CHECK(capital_pressure_sensitivity BETWEEN 0 AND 100)",
            "competition_sensitivity": "REAL NOT NULL DEFAULT 0.25 CHECK(competition_sensitivity BETWEEN 0 AND 100)",
            "reserve_ratio": "REAL NOT NULL DEFAULT 0.10 CHECK(reserve_ratio BETWEEN 0 AND 1)",
            "deposit_insurance_limit": "INTEGER NOT NULL DEFAULT 50000000 CHECK(deposit_insurance_limit >= 0)",
        }.items():
            if column not in institution_columns:
                self.execute(
                    f"ALTER TABLE financial_institutions ADD COLUMN {column} {declaration}"
                )
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
                UPDATE saving SET interest_rate=MIN(COALESCE(rate_cap,100),MAX(rate_floor,
                    NEW.annual_rate+base_rate_spread+maturity_spread+promotional_adjustment+
                    COALESCE((SELECT competition_adjustment FROM financial_institutions f
                              WHERE f.issuer=saving.issuer),0)))
                WHERE rate_type='floating' AND rate_nationality=NEW.nationality
                  AND benchmark_key=NEW.benchmark_key AND benchmark_tenor_days=NEW.tenor_days;
                UPDATE liability SET interest_rate=MIN(
                    (SELECT legal_max_rate FROM country_currencies
                     WHERE nationality=NEW.nationality),COALESCE(rate_cap,100),MAX(rate_floor,
                    NEW.annual_rate+base_rate_spread+maturity_spread+promotional_adjustment+
                    COALESCE((SELECT funding_spread+operating_cost_spread+liquidity_spread+
                        target_margin+competition_adjustment FROM financial_institutions f
                        WHERE f.issuer=liability.issuer),1.7)))
                WHERE rate_type='floating' AND rate_nationality=NEW.nationality
                  AND benchmark_key=NEW.benchmark_key AND benchmark_tenor_days=NEW.tenor_days;
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
                    MAX(rate_floor,NEW.base_rate+base_rate_spread+maturity_spread+
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
                        "VALUES (?,?,0,0,0,0,0,'system','active',?,'currency_saving')",
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
            total = int(self.execute(home_cash, (code,))[0][0]) * SCALE
            total += int(self.execute("""SELECT COALESCE(SUM(a.balance),0) FROM accounts a
                JOIN users u ON u.user_id=a.user_id
                JOIN country_currencies n ON n.nationality=u.nationality
                WHERE n.currency_code=? AND a.account_type<>'currency_saving'""", (code,))[0][0]) * SCALE
            total += int(self.execute("SELECT COALESCE(SUM(balance_units),0) FROM accounts WHERE account_type='currency_saving' AND quote_currency=?", (code,))[0][0])
            treasury_rows = self.execute("""SELECT COALESCE(u.cash,0) FROM users u
                JOIN country_currencies n ON n.nationality=u.nationality
                WHERE u.user_id=? AND n.currency_code=?""", (TREASURY_USER_ID, code))
            treasury = int(treasury_rows[0][0] if treasury_rows else 0) * SCALE
            treasury += int(self.execute("""SELECT COALESCE(SUM(a.balance),0) FROM accounts a
                JOIN users u ON u.user_id=a.user_id
                JOIN country_currencies n ON n.nationality=u.nationality
                WHERE a.user_id=? AND n.currency_code=? AND a.account_type<>'currency_saving'""",
                (TREASURY_USER_ID,code))[0][0]) * SCALE
            treasury += int(self.execute("SELECT COALESCE(SUM(balance_units),0) FROM accounts WHERE account_type='currency_saving' AND quote_currency=? AND user_id=?", (code, TREASURY_USER_ID))[0][0])
            ratio = max(0, total - treasury) / total if total > 0 else 0
            self.execute(
                "INSERT OR IGNORE INTO currency_liquidity_state "
                "(currency_code,baseline_supply_units,baseline_circulation_ratio) VALUES (?,?,?)",
                (code, total, ratio),
            )
