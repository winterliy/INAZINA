from .db_manager import DBManager

class CompanyDB(DBManager):
    def __init__(self):
        super().__init__("company")
        self._init_db()

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
            "share_class": "TEXT NOT NULL DEFAULT 'common' CHECK(share_class IN ('common','preferred'))",
            "parent_company_code": "TEXT",
            "dividend_cycle_seconds": "INTEGER NOT NULL DEFAULT 0 CHECK(dividend_cycle_seconds >= 0)",
            "dividend_per_share": "INTEGER NOT NULL DEFAULT 0 CHECK(dividend_per_share >= 0)",
            "dividend_next_epoch": "INTEGER",
        }.items():
            if column not in columns:
                self.execute(f"ALTER TABLE company ADD COLUMN {column} {declaration}")
        self.enforce_integer_money("company", ("capital", "share_price"), "company_code")

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
