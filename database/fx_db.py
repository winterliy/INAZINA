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
        self.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('fx_fee_percent','0.1')")
        for table in ('company','investment','market_assets','market_orders'):
            columns = {r[1] for r in self.execute(f'PRAGMA table_info({table})')}
            if 'quote_currency' not in columns:
                self.execute(f"ALTER TABLE {table} ADD COLUMN quote_currency TEXT NOT NULL DEFAULT 'NWX'")
