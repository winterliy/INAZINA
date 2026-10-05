from .db_manager import DBManager

class ForeignExchangeDB(DBManager):
    def __init__(self):
        super().__init__("forex")
        self.execute("""CREATE TABLE IF NOT EXISTS currencies (
            code TEXT PRIMARY KEY, name TEXT NOT NULL, asset_code TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'active')""")
        self.execute("INSERT OR IGNORE INTO currencies(code,name) VALUES ('NWX','낙원자화')")
        self.execute("""CREATE TABLE IF NOT EXISTS country_currencies (
            nationality TEXT PRIMARY KEY, currency_code TEXT NOT NULL REFERENCES currencies(code))""")
        self.execute("INSERT OR IGNORE INTO country_currencies VALUES ('낙원','NWX')")
        self.execute("""CREATE TABLE IF NOT EXISTS fx_products (
            product_code TEXT PRIMARY KEY, name TEXT NOT NULL, currency_code TEXT NOT NULL,
            annual_rate REAL NOT NULL CHECK(annual_rate BETWEEN 0 AND 100),
            status TEXT NOT NULL DEFAULT 'active')""")
        self.execute("""CREATE TABLE IF NOT EXISTS fx_accounts (
            account_number TEXT PRIMARY KEY, user_id TEXT NOT NULL, product_code TEXT NOT NULL,
            currency_code TEXT NOT NULL, balance_units INTEGER NOT NULL DEFAULT 0 CHECK(balance_units>=0),
            interest_remainder TEXT NOT NULL DEFAULT '0', last_interest_epoch REAL NOT NULL,
            UNIQUE(user_id, currency_code))""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_fx_accounts_user ON fx_accounts(user_id)")
        self.execute("""CREATE TABLE IF NOT EXISTS fx_transactions (
            transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            source_currency TEXT NOT NULL, target_currency TEXT NOT NULL,
            source_units INTEGER NOT NULL, target_units INTEGER NOT NULL,
            source_rate INTEGER NOT NULL, target_rate INTEGER NOT NULL,
            fee_nwx INTEGER NOT NULL, recorded_epoch REAL NOT NULL)""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_fx_transactions_user_time ON fx_transactions(user_id,recorded_epoch)")
        self.execute("""CREATE TABLE IF NOT EXISTS fx_transfer_transactions (
            transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL, source_account TEXT, target_account TEXT,
            source_currency TEXT NOT NULL, target_currency TEXT NOT NULL,
            source_units INTEGER NOT NULL, target_units INTEGER NOT NULL,
            source_rate INTEGER NOT NULL, target_rate INTEGER NOT NULL,
            fee_nwx INTEGER NOT NULL, recorded_epoch REAL NOT NULL)""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_fx_transfers_sender ON fx_transfer_transactions(sender_id,recorded_epoch)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_fx_transfers_recipient ON fx_transfer_transactions(recipient_id,recorded_epoch)")
        self.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('fx_fee_percent','0.1')")
        self.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('default_currency','NWX')")
        columns = {r[1] for r in self.execute('PRAGMA table_info(currencies)')}
        if 'reference_rate' not in columns:
            self.execute('ALTER TABLE currencies ADD COLUMN reference_rate TEXT')
            self.execute("UPDATE currencies SET reference_rate='1' WHERE code='NWX'")
        if 'rate_multiplier' not in columns:
            self.execute("ALTER TABLE currencies ADD COLUMN rate_multiplier TEXT NOT NULL DEFAULT '1'")
        if 'decimal_places' not in columns:
            self.execute("ALTER TABLE currencies ADD COLUMN decimal_places INTEGER NOT NULL DEFAULT 6 CHECK(decimal_places BETWEEN 0 AND 6)")
            self.execute("UPDATE currencies SET decimal_places=0 WHERE code IN ('NWX','KRW','JPY')")
            self.execute("UPDATE currencies SET decimal_places=2 WHERE code IN ('USD','EUR','GBP','CHF','CNY','HKD','AUD','CAD')")
        self.execute("""CREATE TABLE IF NOT EXISTS guild_countries (
            guild_id TEXT PRIMARY KEY, nationality TEXT NOT NULL REFERENCES country_currencies(nationality))""")
        self.execute("""CREATE TABLE IF NOT EXISTS currency_wallets (
            user_id TEXT NOT NULL REFERENCES users(user_id), currency_code TEXT NOT NULL REFERENCES currencies(code),
            balance_units INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id,currency_code), CHECK(currency_code!='NWX'),
            CHECK(balance_units>=0 OR user_id='__treasury__'))""")
        self.execute("""CREATE TABLE IF NOT EXISTS basic_income_payments (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, user_id TEXT NOT NULL,
            source_currency TEXT NOT NULL, target_currency TEXT NOT NULL, source_units INTEGER NOT NULL,
            target_units INTEGER NOT NULL, source_rate TEXT NOT NULL, target_rate TEXT NOT NULL,
            recorded_epoch REAL NOT NULL)""")
        users_columns = {r[1] for r in self.execute('PRAGMA table_info(users)')}
        if 'registration_guild_id' not in users_columns:
            self.execute('ALTER TABLE users ADD COLUMN registration_guild_id TEXT')
        for table in ('company','investment','market_assets','market_orders','saving','liability','treasury_bond_products','option_contracts','accounts','loans'):
            columns = {r[1] for r in self.execute(f'PRAGMA table_info({table})')}
            if 'quote_currency' not in columns:
                self.execute(f"ALTER TABLE {table} ADD COLUMN quote_currency TEXT NOT NULL DEFAULT 'NWX'")
