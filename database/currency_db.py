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
        self._migrate_currency_wallets()
        self._normalize_home_currency_accounts()
        self.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('currency_exchange_fee_percent','0.1')")
        self._migrate_currency_identifiers()
        for table in ('company','investment','market_assets','market_orders'):
            columns = {r[1] for r in self.execute(f'PRAGMA table_info({table})')}
            if 'quote_currency' not in columns:
                self.execute(f"ALTER TABLE {table} ADD COLUMN quote_currency TEXT NOT NULL DEFAULT 'NWX'")
        self._init_legal_rate_caps()
        self._seed_liquidity_states()

    def _init_legal_rate_caps(self):
        """Keep every interest-bearing product within its nationality's cap.

        Savings and loans are attached to a currency. If several nationalities
        share it, the strictest cap wins. Bonds use their exchange nationality,
        then fall back to the currency/global cap when an exchange is unmapped.
        """
        currency_cap = """COALESCE(
            (SELECT MIN(c.legal_max_rate) FROM country_currencies c
             WHERE c.currency_code=NEW.quote_currency),
            CAST((SELECT value FROM settings WHERE key='legal_max_rate') AS REAL), 20)"""
        exchange_cap = """COALESCE(
            (SELECT c.legal_max_rate FROM market_exchanges e
             JOIN country_currencies c ON c.nationality=e.nationality
             WHERE e.exchange_id=NEW.exchange_id),
            (SELECT MIN(c.legal_max_rate) FROM country_currencies c
             WHERE c.currency_code=COALESCE(NEW.quote_currency,'NWX')),
            CAST((SELECT value FROM settings WHERE key='legal_max_rate') AS REAL), 20)"""
        definitions = (
            ("saving", "interest_rate", currency_cap, None),
            ("liability", "interest_rate", currency_cap, None),
            ("investment", "annual_rate", exchange_cap,
             "NEW.product_type IN ('corporate_bond') AND "),
            ("treasury_bond_products", "annual_rate", exchange_cap, None),
        )
        for table, rate_column, cap, condition in definitions:
            for operation in ("INSERT", f"UPDATE OF {rate_column}, quote_currency, exchange_id"):
                # Not every product table has both routing columns.
                if table in {"saving", "liability"}:
                    operation = "INSERT" if operation == "INSERT" else f"UPDATE OF {rate_column}, quote_currency"
                if table == "treasury_bond_products":
                    operation = "INSERT" if operation == "INSERT" else f"UPDATE OF {rate_column}, exchange_id, quote_currency"
                suffix = "insert" if operation == "INSERT" else "update"
                trigger = f"trg_{table}_legal_max_{suffix}"
                self.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
                self.execute(f'''CREATE TRIGGER "{trigger}" BEFORE {operation} ON "{table}"
                    WHEN {condition or ''}(
                        NEW.{rate_column} IS NULL OR NEW.{rate_column} < 0
                        OR NEW.{rate_column} > {cap}
                    )
                    BEGIN
                        SELECT RAISE(ABORT, '상품 이율이 해당 국적의 법정최고이자를 초과합니다.');
                    END''')

        for name in ("trg_country_legal_max_insert", "trg_country_legal_max_update"):
            self.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        clamp = """
            UPDATE saving SET interest_rate=MIN(interest_rate,
                (SELECT MIN(c.legal_max_rate) FROM country_currencies c
                 WHERE c.currency_code=saving.quote_currency))
                WHERE quote_currency=NEW.currency_code;
            UPDATE liability SET interest_rate=MIN(interest_rate,
                (SELECT MIN(c.legal_max_rate) FROM country_currencies c
                 WHERE c.currency_code=liability.quote_currency))
                WHERE quote_currency=NEW.currency_code;
            UPDATE investment SET annual_rate=MIN(annual_rate,NEW.legal_max_rate)
                WHERE product_type='corporate_bond' AND exchange_id IN
                (SELECT exchange_id FROM market_exchanges WHERE nationality=NEW.nationality);
            UPDATE treasury_bond_products SET annual_rate=MIN(annual_rate,NEW.legal_max_rate)
                WHERE exchange_id IN
                (SELECT exchange_id FROM market_exchanges WHERE nationality=NEW.nationality);
            UPDATE treasury_bond_holdings SET annual_rate=MIN(annual_rate,NEW.legal_max_rate)
                WHERE product_code IN (SELECT product_code FROM treasury_bond_products
                    WHERE exchange_id IN (SELECT exchange_id FROM market_exchanges
                                          WHERE nationality=NEW.nationality));
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
                UPDATE saving SET interest_rate=MIN(interest_rate,COALESCE(
                    (SELECT MIN(c.legal_max_rate) FROM country_currencies c
                     WHERE c.currency_code=saving.quote_currency),CAST(NEW.value AS REAL)));
                UPDATE liability SET interest_rate=MIN(interest_rate,COALESCE(
                    (SELECT MIN(c.legal_max_rate) FROM country_currencies c
                     WHERE c.currency_code=liability.quote_currency),CAST(NEW.value AS REAL)));
                UPDATE investment SET annual_rate=MIN(annual_rate,COALESCE(
                    (SELECT c.legal_max_rate FROM market_exchanges e
                     JOIN country_currencies c ON c.nationality=e.nationality
                     WHERE e.exchange_id=investment.exchange_id),CAST(NEW.value AS REAL)))
                    WHERE product_type='corporate_bond';
                UPDATE treasury_bond_products SET annual_rate=MIN(annual_rate,COALESCE(
                    (SELECT c.legal_max_rate FROM market_exchanges e
                     JOIN country_currencies c ON c.nationality=e.nationality
                     WHERE e.exchange_id=treasury_bond_products.exchange_id),CAST(NEW.value AS REAL)));
                UPDATE treasury_bond_holdings SET annual_rate=MIN(annual_rate,COALESCE(
                    (SELECT p.annual_rate FROM treasury_bond_products p
                     WHERE p.product_code=treasury_bond_holdings.product_code),annual_rate));
            END''')

        # Bring legacy rows under the new cap once during schema initialization.
        self.execute("""UPDATE saving SET interest_rate=MIN(interest_rate,COALESCE(
            (SELECT MIN(c.legal_max_rate) FROM country_currencies c
             WHERE c.currency_code=saving.quote_currency),20))""")
        self.execute("""UPDATE liability SET interest_rate=MIN(interest_rate,COALESCE(
            (SELECT MIN(c.legal_max_rate) FROM country_currencies c
             WHERE c.currency_code=liability.quote_currency),20))""")
        self.execute("""UPDATE investment SET annual_rate=MIN(annual_rate,COALESCE(
            (SELECT c.legal_max_rate FROM market_exchanges e JOIN country_currencies c
             ON c.nationality=e.nationality WHERE e.exchange_id=investment.exchange_id),20))
            WHERE product_type='corporate_bond'""")
        self.execute("""UPDATE treasury_bond_products SET annual_rate=MIN(annual_rate,COALESCE(
            (SELECT c.legal_max_rate FROM market_exchanges e JOIN country_currencies c
             ON c.nationality=e.nationality WHERE e.exchange_id=treasury_bond_products.exchange_id),20))""")
        self.execute("""UPDATE treasury_bond_holdings SET annual_rate=MIN(annual_rate,COALESCE(
            (SELECT p.annual_rate FROM treasury_bond_products p
             WHERE p.product_code=treasury_bond_holdings.product_code),annual_rate))""")

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
