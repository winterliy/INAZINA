from .db_manager import DBManager
from .fractional import initialize_fractional

class CompanyDB(DBManager):
    def __init__(self):
        super().__init__("company")
        self._init_db()
        initialize_fractional(self, 'company')

    def _init_db(self):
        self.execute('''
            CREATE TABLE IF NOT EXISTS company (
                company_code TEXT PRIMARY KEY,
                name TEXT,
                industry TEXT,
                capital INTEGER,
                total_shares INTEGER,
                share_price INTEGER,
                max_shareholder_id TEXT,
                exchange_id TEXT,
                status TEXT DEFAULT 'active',
                is_fixed INTEGER NOT NULL DEFAULT 0,
                market_condition TEXT DEFAULT NULL,
                market_volatility REAL DEFAULT NULL
            )
        ''')
        columns = {row[1] for row in self.execute("PRAGMA table_info(company)")}
        if "is_fixed" not in columns:
            self.execute("ALTER TABLE company ADD COLUMN is_fixed INTEGER NOT NULL DEFAULT 0")
        if "market_condition" not in columns:
            self.execute("ALTER TABLE company ADD COLUMN market_condition TEXT DEFAULT NULL")
        if "market_volatility" not in columns:
            self.execute("ALTER TABLE company ADD COLUMN market_volatility REAL DEFAULT NULL")
        for column, declaration in {
            "par_value_minor_units": "TEXT DEFAULT NULL",
            "share_class": "TEXT NOT NULL DEFAULT 'common' CHECK(share_class IN ('common','preferred'))",
            "parent_company_code": "TEXT",
            "dividend_priority": "INTEGER NOT NULL DEFAULT 0 CHECK(dividend_priority >= 0)",
            "cumulative_dividend": "INTEGER NOT NULL DEFAULT 0 CHECK(cumulative_dividend IN (0,1))",
            "liquidation_value_per_share": "INTEGER NOT NULL DEFAULT 0 CHECK(liquidation_value_per_share >= 0)",
            "dividend_funding": "TEXT NOT NULL DEFAULT 'treasury' CHECK(dividend_funding IN ('treasury','issuer'))",
            "dividend_cycle_seconds": "INTEGER NOT NULL DEFAULT 0 CHECK(dividend_cycle_seconds >= 0)",
            "dividend_per_share": "INTEGER NOT NULL DEFAULT 0 CHECK(dividend_per_share >= 0)",
            "dividend_mode": "TEXT NOT NULL DEFAULT 'fixed' CHECK(dividend_mode IN ('fixed','percent'))",
            "dividend_percent": "REAL NOT NULL DEFAULT 0 CHECK(dividend_percent >= 0 AND dividend_percent <= 100)",
            "dividend_is_active": "INTEGER NOT NULL DEFAULT 1 CHECK(dividend_is_active IN (0,1))",
            "dividend_next_epoch": "INTEGER",
            "liquidity": "REAL DEFAULT NULL CHECK(liquidity IS NULL OR liquidity > 0)",
            "trend_sensitivity": "REAL DEFAULT NULL CHECK(trend_sensitivity IS NULL OR trend_sensitivity >= 0)",
            "sector_sensitivity": "REAL DEFAULT NULL CHECK(sector_sensitivity IS NULL OR sector_sensitivity >= 0)",
            "jump_intensity": "REAL DEFAULT NULL CHECK(jump_intensity IS NULL OR jump_intensity >= 0)",
            "mean_reversion": "REAL DEFAULT NULL CHECK(mean_reversion IS NULL OR mean_reversion >= 0)",
            # Shares offered to the market when listed (a partial listing); NULL means all of them.
            "listed_shares": "INTEGER DEFAULT NULL CHECK(listed_shares IS NULL OR listed_shares >= 0)",
            # Stable link to the legal share issuance; company_code remains the market ticker.
            "share_issuance_id": "INTEGER",
        }.items():
            if column not in columns:
                self.execute(f"ALTER TABLE company ADD COLUMN {column} {declaration}")
        if "dividend_priority" not in columns:
            self.execute("UPDATE company SET dividend_priority=1 WHERE share_class='preferred'")
        self._rename_unlisted_exchange()
        self.initialize_dividend_activation("company", "company_code")
        self.enforce_integer_money("company", ("capital", "share_price"), "company_code")

    def _rename_unlisted_exchange(self):
        """Unlisted companies moved from the UNLISTED marker to the 3DSG over-the-counter market."""
        tables = {row[0] for row in self.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("company", "market_assets", "investment", "treasury_bond_products"):
            if table in tables:
                self.execute(f"UPDATE {table} SET exchange_id='3DSG' WHERE exchange_id='UNLISTED'")

    def create_company(self, code, name, industry, capital, total_shares, share_price, max_shareholder_id, exchange_id):
        self.execute("INSERT INTO company "
                     "(company_code, name, industry, capital, total_shares, share_price, "
                     "max_shareholder_id, exchange_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')", 
                     (code, name, industry, capital, total_shares, share_price, max_shareholder_id, exchange_id))

    def get_company(self, code):
        return self.execute_one("SELECT * FROM company WHERE company_code = ?", (code,))

    def update_share_price(self, code, new_price):
        self.execute("UPDATE company SET share_price = ? WHERE company_code = ?", (new_price, code))

    def set_status(self, code, status):
        self.execute("UPDATE company SET status = ? WHERE company_code = ?", (status, code))
