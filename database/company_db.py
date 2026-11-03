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
        self.execute("""CREATE TABLE IF NOT EXISTS company_capital_interests (
            interest_id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_code TEXT NOT NULL,
            holder_id TEXT NOT NULL,
            holder_kind TEXT NOT NULL CHECK(holder_kind IN ('user','company','country')),
            contribution_amount TEXT NOT NULL DEFAULT '0',
            currency_code TEXT NOT NULL,
            economic_percent TEXT NOT NULL DEFAULT '0',
            voting_percent TEXT NOT NULL DEFAULT '0',
            created_epoch INTEGER NOT NULL DEFAULT (unixepoch()),
            updated_epoch INTEGER NOT NULL DEFAULT (unixepoch()),
            UNIQUE(organization_code,holder_kind,holder_id)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_capital_interests_holder ON company_capital_interests(holder_kind,holder_id)")
        columns = {row[1] for row in self.execute("PRAGMA table_info(company)")}
        added_trading_status = "trading_status" not in columns
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
            "dividend_funding": "TEXT NOT NULL DEFAULT 'issuer' CHECK(dividend_funding IN ('treasury','issuer'))",
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
            "legal_form": "TEXT NOT NULL DEFAULT 'stock_company' CHECK(legal_form IN ('stock_company','limited_company','limited_liability_company','general_partnership','limited_partnership'))",
            "ownership_unit": "TEXT NOT NULL DEFAULT 'shares' CHECK(ownership_unit IN ('shares','capital_interest'))",
            "trading_status": "TEXT NOT NULL DEFAULT 'active' CHECK(trading_status IN ('active','suspended'))",
            "nationality": "TEXT NOT NULL DEFAULT '낙원'",
        }.items():
            if column not in columns:
                self.execute(f"ALTER TABLE company ADD COLUMN {column} {declaration}")
        if added_trading_status:
            # Older inactive stock rows represented trade suspension, not legal dissolution.
            self.execute("UPDATE company SET trading_status='suspended',status='active' WHERE status='inactive'")
        # Company stock dividends always come from the issuing company's cash.
        self.execute("UPDATE company SET dividend_funding='issuer' WHERE dividend_funding<>'issuer'")
        self.execute("""
            CREATE TRIGGER IF NOT EXISTS company_dividend_funding_issuer_insert
            AFTER INSERT ON company WHEN NEW.dividend_funding<>'issuer' BEGIN
              UPDATE company SET dividend_funding='issuer' WHERE company_code=NEW.company_code;
            END
        """)
        self.execute("""
            CREATE TRIGGER IF NOT EXISTS company_dividend_funding_issuer_update
            AFTER UPDATE OF dividend_funding ON company WHEN NEW.dividend_funding<>'issuer' BEGIN
              UPDATE company SET dividend_funding='issuer' WHERE company_code=NEW.company_code;
            END
        """)
        if "dividend_priority" not in columns:
            self.execute("UPDATE company SET dividend_priority=1 WHERE share_class='preferred'")
        self.initialize_dividend_activation("company", "company_code")
        self.enforce_integer_money("company", ("capital", "share_price"), "company_code")

    def backfill_nationality_from_exchange(self):
        """Preserve each existing company's home country before venues become optional."""
        self.execute("UPDATE company SET nationality=COALESCE((SELECT o.country_code FROM organizations o "
                     "WHERE o.organization_code=company.company_code),nationality) WHERE EXISTS "
                     "(SELECT 1 FROM organizations o WHERE o.organization_code=company.company_code "
                     "AND o.country_code IS NOT NULL)")
        self.execute("UPDATE company SET nationality=COALESCE((SELECT nationality FROM market_exchanges e "
                     "WHERE e.exchange_id=company.exchange_id),nationality) WHERE exchange_id IS NOT NULL "
                     "AND exchange_id<>'UNLISTED' "
                     "AND EXISTS(SELECT 1 FROM market_exchanges e WHERE e.exchange_id=company.exchange_id)")
        # The organization catalog is the authoritative legal classification when it exists.
        self.execute("UPDATE company SET legal_form=(SELECT o.legal_form FROM organizations o "
                     "WHERE o.organization_code=company.company_code), ownership_unit=CASE "
                     "WHEN (SELECT o.legal_form FROM organizations o WHERE o.organization_code=company.company_code)='stock_company' "
                     "THEN 'shares' ELSE 'capital_interest' END WHERE EXISTS "
                     "(SELECT 1 FROM organizations o WHERE o.organization_code=company.company_code "
                     "AND o.legal_form IN ('stock_company','limited_company','limited_liability_company','general_partnership','limited_partnership'))")
        self._rename_unlisted_exchange()

    def _rename_unlisted_exchange(self):
        """Map the old sentinel only to Nakwon's OTC venue; other private assets get no venue."""
        tables = {row[0] for row in self.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "company" in tables:
            self.execute("UPDATE company SET exchange_id=CASE WHEN nationality='낙원' THEN '3DSG' ELSE NULL END "
                         "WHERE exchange_id='UNLISTED'")
        for table in ("market_assets", "investment", "treasury_bond_products"):
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
