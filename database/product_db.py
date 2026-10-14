from .db_manager import DBManager

class ProductDB(DBManager):
    def __init__(self):
        super().__init__("product")
        self._init_db()

    def _init_db(self):
        # 저축 상품 (Saving)
        self.execute('''
            CREATE TABLE IF NOT EXISTS saving (
                product_code TEXT PRIMARY KEY,
                name TEXT,
                interest_rate REAL,
                deposit_amount INTEGER,
                maturity_days INTEGER,
                interest_cycle REAL, -- 초 단위 주기; 1 = 1초, 0.1 = 0.1초, 3600 = 1시간
                compound_type INTEGER, -- 0: 단리, 1: 복리
                issuer TEXT,
                status TEXT DEFAULT 'active',
                quote_currency TEXT NOT NULL DEFAULT 'NWX',
                product_kind TEXT NOT NULL DEFAULT 'saving'
            )
        ''')
        saving_columns = {row[1] for row in self.execute("PRAGMA table_info(saving)")}
        if "quote_currency" not in saving_columns:
            self.execute("ALTER TABLE saving ADD COLUMN quote_currency TEXT NOT NULL DEFAULT 'NWX'")
        if "product_kind" not in saving_columns:
            self.execute("ALTER TABLE saving ADD COLUMN product_kind TEXT NOT NULL DEFAULT 'saving'")
        self._init_rate_pricing_columns("saving", saving_columns)
        # 대출 상품 (Liability)
        self.execute('''
            CREATE TABLE IF NOT EXISTS liability (
                product_code TEXT PRIMARY KEY,
                name TEXT,
                interest_rate REAL,
                loan_limit INTEGER,
                maturity_days INTEGER,
                interest_cycle REAL, -- 초 단위 주기; 소수 초 입력 가능
                min_credit_score INTEGER,
                issuer TEXT,
                status TEXT DEFAULT 'active',
                repayment_cycle_seconds INTEGER NOT NULL DEFAULT 0,
                repayment_amount INTEGER NOT NULL DEFAULT 0
            )
        ''')
        liability_columns = {row[1] for row in self.execute("PRAGMA table_info(liability)")}
        if "repayment_cycle_seconds" not in liability_columns:
            self.execute(
                "ALTER TABLE liability ADD COLUMN repayment_cycle_seconds INTEGER NOT NULL DEFAULT 0"
            )
        if "repayment_amount" not in liability_columns:
            self.execute(
                "ALTER TABLE liability ADD COLUMN repayment_amount INTEGER NOT NULL DEFAULT 0"
            )
        self._init_rate_pricing_columns("liability", liability_columns)
        for column, declaration in {
            "extension_min_credit_score": "INTEGER NOT NULL DEFAULT 700 CHECK(extension_min_credit_score BETWEEN 0 AND 1000)",
            "extension_days": "INTEGER NOT NULL DEFAULT 0 CHECK(extension_days >= 0)",
        }.items():
            if column not in liability_columns:
                self.execute(f"ALTER TABLE liability ADD COLUMN {column} {declaration}")
        # 투자 상품 (Investment)
        self.execute('''
            CREATE TABLE IF NOT EXISTS investment (
                product_code TEXT PRIMARY KEY,
                name TEXT,
                product_type TEXT,
                total_supply INTEGER,
                unit_price INTEGER,
                maturity_days INTEGER,
                manager TEXT,
                exchange_id TEXT,
                status TEXT DEFAULT 'active',
                is_fixed INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                market_condition TEXT DEFAULT NULL,
                market_volatility REAL DEFAULT NULL,
                underlying_exchange_id TEXT DEFAULT NULL,
                leverage_multiplier REAL NOT NULL DEFAULT 1,
                rebalance_cycle_seconds INTEGER NOT NULL DEFAULT 86400
            )
        ''')
        investment_columns = {row[1] for row in self.execute("PRAGMA table_info(investment)")}
        extra_columns = {
            "underlying_code": "TEXT", "issuer_code": "TEXT", "maturity_epoch": "INTEGER",
            "face_value": "INTEGER NOT NULL DEFAULT 0", "annual_rate": "REAL NOT NULL DEFAULT 0",
            "contract_size": "INTEGER NOT NULL DEFAULT 1", "initial_margin": "REAL NOT NULL DEFAULT 0.2",
            "maintenance_margin": "REAL NOT NULL DEFAULT 0.15", "nav_anchor": "REAL NOT NULL DEFAULT 0",
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
            "credit_spread": "REAL NOT NULL DEFAULT 0 CHECK(credit_spread BETWEEN 0 AND 100)",
            "liquidity_spread": "REAL NOT NULL DEFAULT 0 CHECK(liquidity_spread BETWEEN 0 AND 100)",
            "coupon_frequency_days": "INTEGER NOT NULL DEFAULT 0 CHECK(coupon_frequency_days >= 0)",
            "day_count_convention": "TEXT NOT NULL DEFAULT 'ACT/365' CHECK(day_count_convention IN ('ACT/365','ACT/360','30/360'))",
            "last_coupon_epoch": "INTEGER",
            "next_coupon_epoch": "INTEGER",
        }
        for column, declaration in extra_columns.items():
            if column not in investment_columns:
                self.execute(f"ALTER TABLE investment ADD COLUMN {column} {declaration}")
        self.initialize_dividend_activation("investment", "product_code")
        self.execute("""CREATE TABLE IF NOT EXISTS fund_components (
            product_code TEXT NOT NULL, asset_code TEXT NOT NULL,
            weight REAL NOT NULL CHECK(weight > 0), base_price REAL NOT NULL CHECK(base_price > 0),
            PRIMARY KEY(product_code, asset_code)
        )""")
        if "is_fixed" not in investment_columns:
            self.execute("ALTER TABLE investment ADD COLUMN is_fixed INTEGER NOT NULL DEFAULT 0")
        if "created_at" not in investment_columns:
            self.execute("ALTER TABLE investment ADD COLUMN created_at DATETIME")
            self.execute("UPDATE investment SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
        if "market_condition" not in investment_columns:
            self.execute("ALTER TABLE investment ADD COLUMN market_condition TEXT DEFAULT NULL")
        if "market_volatility" not in investment_columns:
            self.execute("ALTER TABLE investment ADD COLUMN market_volatility REAL DEFAULT NULL")
        if "underlying_exchange_id" not in investment_columns:
            self.execute("ALTER TABLE investment ADD COLUMN underlying_exchange_id TEXT DEFAULT NULL")
        if "leverage_multiplier" not in investment_columns:
            self.execute("ALTER TABLE investment ADD COLUMN leverage_multiplier REAL NOT NULL DEFAULT 1")
        if "rebalance_cycle_seconds" not in investment_columns:
            self.execute(
                "ALTER TABLE investment ADD COLUMN rebalance_cycle_seconds INTEGER NOT NULL DEFAULT 86400"
            )
        self.enforce_integer_money("saving", ("deposit_amount",), "product_code")
        self.enforce_integer_money(
            "liability", ("loan_limit", "repayment_amount"), "product_code"
        )
        self.enforce_integer_money("investment", ("unit_price",), "product_code")

        self.execute(
            """CREATE TABLE IF NOT EXISTS treasury_bond_products (
                product_code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                annual_rate REAL NOT NULL CHECK(annual_rate >= 0),
                maturity_days INTEGER NOT NULL CHECK(maturity_days > 0),
                face_value INTEGER NOT NULL DEFAULT 1 CHECK(face_value > 0),
                exchange_id TEXT NOT NULL DEFAULT 'BOND',
                market_price INTEGER NOT NULL DEFAULT 1 CHECK(market_price > 0),
                total_supply INTEGER NOT NULL CHECK(total_supply > 0),
                sold_amount INTEGER NOT NULL DEFAULT 0 CHECK(sold_amount >= 0),
                status TEXT NOT NULL DEFAULT 'active',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        bond_columns = {
            row[1] for row in self.execute("PRAGMA table_info(treasury_bond_products)")
        }
        for column, declaration in {
            "coupon_frequency_days": "INTEGER NOT NULL DEFAULT 0 CHECK(coupon_frequency_days >= 0)",
            "day_count_convention": "TEXT NOT NULL DEFAULT 'ACT/365' CHECK(day_count_convention IN ('ACT/365','ACT/360','30/360'))",
            "credit_spread": "REAL NOT NULL DEFAULT 0 CHECK(credit_spread BETWEEN 0 AND 100)",
            "liquidity_spread": "REAL NOT NULL DEFAULT 0 CHECK(liquidity_spread BETWEEN 0 AND 100)",
            "last_coupon_epoch": "INTEGER",
            "next_coupon_epoch": "INTEGER",
        }.items():
            if column not in bond_columns:
                self.execute(
                    f"ALTER TABLE treasury_bond_products ADD COLUMN {column} {declaration}"
                )
        if "face_value" not in bond_columns:
            # Existing offers used arbitrary whole-NWX purchase amounts; 1 NWX
            # preserves that behavior while new issues specify their denomination.
            self.execute(
                "ALTER TABLE treasury_bond_products "
                "ADD COLUMN face_value INTEGER NOT NULL DEFAULT 1 CHECK(face_value > 0)"
            )
        if "exchange_id" not in bond_columns:
            self.execute(
                "ALTER TABLE treasury_bond_products "
                "ADD COLUMN exchange_id TEXT NOT NULL DEFAULT 'BOND'"
            )
        if "market_price" not in bond_columns:
            self.execute(
                "ALTER TABLE treasury_bond_products "
                "ADD COLUMN market_price INTEGER NOT NULL DEFAULT 1 CHECK(market_price > 0)"
            )
            self.execute(
                "UPDATE treasury_bond_products SET market_price = face_value"
            )
        self.enforce_integer_money(
            "treasury_bond_products",
            ("face_value", "market_price", "total_supply", "sold_amount"),
            "product_code",
        )
        # SQL Browser is the management surface for bond issuance. Keep the
        # fixed face-value denomination and supply limits valid even for
        # direct edits that bypass TreasuryBondManager's form validation.
        self.execute(
            """CREATE TRIGGER IF NOT EXISTS validate_treasury_bond_insert
            BEFORE INSERT ON treasury_bond_products
            WHEN NEW.face_value <= 0 OR NEW.total_supply <= 0
              OR NEW.total_supply % NEW.face_value != 0
              OR NEW.sold_amount < 0 OR NEW.sold_amount > NEW.total_supply
              OR NEW.sold_amount % NEW.face_value != 0
            BEGIN
                SELECT RAISE(ABORT, '국채 발행한도와 판매액은 양의 액면가 배수여야 합니다.');
            END"""
        )
        self.execute(
            """CREATE TRIGGER IF NOT EXISTS validate_treasury_bond_update
            BEFORE UPDATE OF face_value, total_supply, sold_amount
            ON treasury_bond_products
            WHEN NEW.face_value <= 0 OR NEW.total_supply <= 0
              OR NEW.total_supply % NEW.face_value != 0
              OR NEW.sold_amount < 0 OR NEW.sold_amount > NEW.total_supply
              OR NEW.sold_amount % NEW.face_value != 0
            BEGIN
                SELECT RAISE(ABORT, '국채 발행한도와 판매액은 양의 액면가 배수여야 합니다.');
            END"""
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_treasury_bond_products_status "
            "ON treasury_bond_products(status, name)"
        )
        for table in ("liability", "investment", "treasury_bond_products"):
            columns = {row[1] for row in self.execute(f"PRAGMA table_info({table})")}
            if "quote_currency" not in columns:
                self.execute(f"ALTER TABLE {table} ADD COLUMN quote_currency TEXT NOT NULL DEFAULT 'NWX'")

    def _init_rate_pricing_columns(self, table, existing_columns):
        """Add benchmark, institution, borrower-risk, and delinquency pricing terms.

        Existing rows remain fixed-rate.  A floating product's advertised rate
        is derived from its selected benchmark plus pricing components. The
        nationality's legal maximum is applied only to lending.
        """
        definitions = {
            "rate_type": "TEXT NOT NULL DEFAULT 'fixed' CHECK(rate_type IN ('fixed','floating'))",
            "rate_nationality": "TEXT",
            "base_rate_spread": "REAL NOT NULL DEFAULT 0 CHECK(base_rate_spread BETWEEN -100 AND 100)",
            "rate_floor": "REAL NOT NULL DEFAULT 0 CHECK(rate_floor BETWEEN 0 AND 100)",
            "rate_cap": "REAL CHECK(rate_cap IS NULL OR rate_cap BETWEEN 0 AND 100)",
            "repricing_cycle_seconds": "INTEGER NOT NULL DEFAULT 2592000 CHECK(repricing_cycle_seconds > 0)",
            "benchmark_key": "TEXT NOT NULL DEFAULT 'policy' CHECK(benchmark_key IN ('policy','funding','bank_bond','treasury'))",
            "benchmark_tenor_days": "INTEGER NOT NULL DEFAULT 0 CHECK(benchmark_tenor_days >= 0)",
            "maturity_spread": "REAL NOT NULL DEFAULT 0 CHECK(maturity_spread BETWEEN -100 AND 100)",
            "promotional_adjustment": "REAL NOT NULL DEFAULT 0 CHECK(promotional_adjustment BETWEEN -100 AND 100)",
            "credit_spread_max": "REAL NOT NULL DEFAULT 0 CHECK(credit_spread_max BETWEEN 0 AND 100)",
            "collateral_discount_max": "REAL NOT NULL DEFAULT 0 CHECK(collateral_discount_max BETWEEN 0 AND 100)",
            "collateral_required_ratio": "REAL NOT NULL DEFAULT 0 CHECK(collateral_required_ratio BETWEEN 0 AND 1)",
            "origination_fee_percent": "REAL NOT NULL DEFAULT 0 CHECK(origination_fee_percent BETWEEN 0 AND 100)",
            "repricing_lag_seconds": "INTEGER NOT NULL DEFAULT 0 CHECK(repricing_lag_seconds >= 0)",
            "delinquency_spread": "REAL NOT NULL DEFAULT 3 CHECK(delinquency_spread BETWEEN 0 AND 100)",
            "grace_period_days": "INTEGER NOT NULL DEFAULT 3 CHECK(grace_period_days >= 0)",
            "acceleration_after_days": "INTEGER NOT NULL DEFAULT 30 CHECK(acceleration_after_days >= 0)",
            "default_after_days": "INTEGER NOT NULL DEFAULT 90 CHECK(default_after_days >= 0)",
            "negative_rate_allowed": (
                "INTEGER NOT NULL DEFAULT 1 CHECK(negative_rate_allowed IN (0,1))"
                if table == "saving" else
                "INTEGER NOT NULL DEFAULT 0 CHECK(negative_rate_allowed IN (0,1))"
            ),
            "day_count_convention": "TEXT NOT NULL DEFAULT 'ACT/365' CHECK(day_count_convention IN ('ACT/365','ACT/360','30/360'))",
            "amortization_type": "TEXT NOT NULL DEFAULT 'legacy' CHECK(amortization_type IN ('legacy','bullet','equal_principal','annuity'))",
            "payment_frequency_days": "INTEGER NOT NULL DEFAULT 30 CHECK(payment_frequency_days > 0)",
            "collateral_haircut": "REAL NOT NULL DEFAULT 0.20 CHECK(collateral_haircut BETWEEN 0 AND 1)",
            "margin_call_ltv": "REAL NOT NULL DEFAULT 0.85 CHECK(margin_call_ltv > 0)",
            "liquidation_ltv": "REAL NOT NULL DEFAULT 1.00 CHECK(liquidation_ltv > 0)",
            "early_withdrawal_rate": "REAL NOT NULL DEFAULT 0 CHECK(early_withdrawal_rate BETWEEN -100 AND 100)",
            "early_withdrawal_fee_percent": "REAL NOT NULL DEFAULT 0 CHECK(early_withdrawal_fee_percent BETWEEN 0 AND 100)",
            "deposit_insured": "INTEGER NOT NULL DEFAULT 1 CHECK(deposit_insured IN (0,1))",
            "business_day_convention": "TEXT NOT NULL DEFAULT 'following' CHECK(business_day_convention IN ('none','following','modified_following','preceding'))",
            # Zero keeps legacy products available until an administrator opts
            # into income-based underwriting for that product.
            "max_dti_percent": "REAL NOT NULL DEFAULT 0 CHECK(max_dti_percent BETWEEN 0 AND 100)",
            "max_dsr_percent": "REAL NOT NULL DEFAULT 0 CHECK(max_dsr_percent BETWEEN 0 AND 100)",
            "borrower_history_spread_max": "REAL NOT NULL DEFAULT 0 CHECK(borrower_history_spread_max BETWEEN 0 AND 100)",
            "affordability_spread_max": "REAL NOT NULL DEFAULT 0 CHECK(affordability_spread_max BETWEEN 0 AND 100)",
            "mandatory_cost_percent": "REAL NOT NULL DEFAULT 0 CHECK(mandatory_cost_percent BETWEEN 0 AND 100)",
            "liquidation_cost_percent": "REAL NOT NULL DEFAULT 2 CHECK(liquidation_cost_percent BETWEEN 0 AND 100)",
            "prepayment_fee_percent": "REAL NOT NULL DEFAULT 0 CHECK(prepayment_fee_percent BETWEEN 0 AND 100)",
            "prepayment_fee_decline_days": "INTEGER NOT NULL DEFAULT 0 CHECK(prepayment_fee_decline_days >= 0)",
        }
        for column, declaration in definitions.items():
            if column not in existing_columns:
                self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def register_saving(self, code, name, rate, deposit, maturity, cycle, compound, issuer):
        self.execute(
            """INSERT OR REPLACE INTO saving
               (product_code,name,interest_rate,deposit_amount,maturity_days,interest_cycle,
                compound_type,issuer,status,quote_currency,product_kind)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 'NWX', 'saving')""",
            (code, name, rate, deposit, maturity, cycle, compound, issuer),
        )

    def register_liability(
        self, code, name, rate, limit, maturity, cycle, credit, issuer,
        repayment_cycle_seconds=0, repayment_amount=0,
    ):
        self.execute(
            "INSERT OR REPLACE INTO liability "
            "(product_code, name, interest_rate, loan_limit, maturity_days, interest_cycle, "
            "min_credit_score, issuer, status, repayment_cycle_seconds, repayment_amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (code, name, rate, limit, maturity, cycle, credit, issuer,
             repayment_cycle_seconds, repayment_amount),
        )

    def register_investment(
        self, code, name, p_type, supply, price, maturity, manager, ex_id,
        underlying_exchange_id=None, leverage_multiplier=1, rebalance_cycle_seconds=86400,
    ):
        self.execute("INSERT OR REPLACE INTO investment "
                     "(product_code, name, product_type, total_supply, unit_price, maturity_days, "
                     "manager, exchange_id, status, underlying_exchange_id, leverage_multiplier, "
                     "rebalance_cycle_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                     (code, name, p_type, supply, price, maturity, manager, ex_id,
                      underlying_exchange_id, leverage_multiplier, rebalance_cycle_seconds))

    def get_saving(self, code):
        return self.execute_one("SELECT * FROM saving WHERE product_code = ?", (code,))

    def get_liability(self, code):
        return self.execute_one("SELECT * FROM liability WHERE product_code = ?", (code,))

    def get_investment(self, code):
        return self.execute_one("SELECT * FROM investment WHERE product_code = ?", (code,))

    def get_treasury_bond(self, code):
        return self.execute_one(
            "SELECT * FROM treasury_bond_products WHERE product_code = ?", (code,)
        )

    def get_active_savings(self):
        return self.execute("SELECT * FROM saving WHERE status = 'active' ORDER BY name")

    def get_active_liabilities(self):
        return self.execute("SELECT * FROM liability WHERE status = 'active' ORDER BY name")

    def get_active_investments(self):
        return self.execute("SELECT * FROM investment WHERE status = 'active' ORDER BY name")
