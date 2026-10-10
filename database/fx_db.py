from .db_manager import DBManager

class ForeignExchangeDB(DBManager):
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
        self._seed_liquidity_states()

    def _seed_liquidity_states(self):
        """Take one neutral supply snapshot for currencies without a baseline."""
        from database.user_db import TREASURY_USER_ID

        for row in self.execute("SELECT code FROM currencies"):
            code = str(row[0])
            if code == "NWX":
                total = int(self.execute("SELECT COALESCE(SUM(cash),0) FROM users")[0][0]) * 1_000_000
                total += int(self.execute("SELECT COALESCE(SUM(balance),0) FROM accounts")[0][0]) * 1_000_000
                treasury = int(self.execute("SELECT COALESCE(cash,0) FROM users WHERE user_id=?", (TREASURY_USER_ID,))[0][0]) * 1_000_000
                treasury += int(self.execute("SELECT COALESCE(SUM(balance),0) FROM accounts WHERE user_id=?", (TREASURY_USER_ID,))[0][0]) * 1_000_000
            else:
                total = int(self.execute("SELECT COALESCE(SUM(balance_units),0) FROM currency_wallets WHERE currency_code=?", (code,))[0][0])
                total += int(self.execute("SELECT COALESCE(SUM(balance_units),0) FROM fx_accounts WHERE currency_code=?", (code,))[0][0])
                treasury = int(self.execute("SELECT COALESCE(SUM(balance_units),0) FROM currency_wallets WHERE currency_code=? AND user_id=?", (code, TREASURY_USER_ID))[0][0])
                treasury += int(self.execute("SELECT COALESCE(SUM(balance_units),0) FROM fx_accounts WHERE currency_code=? AND user_id=?", (code, TREASURY_USER_ID))[0][0])
            ratio = max(0, total - treasury) / total if total > 0 else 0
            self.execute(
                "INSERT OR IGNORE INTO currency_liquidity_state "
                "(currency_code,baseline_supply_units,baseline_circulation_ratio) VALUES (?,?,?)",
                (code, total, ratio),
            )
