from .db_manager import DBManager


class AssetDB(DBManager):
    def __init__(self):
        super().__init__("assets")
        self._init_db()

    def _init_db(self):
        self.execute("""CREATE TABLE IF NOT EXISTS corporate_actions (
            action_id INTEGER PRIMARY KEY AUTOINCREMENT, operation_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL, source_code TEXT NOT NULL, target_code TEXT,
            payload TEXT NOT NULL, recorded_epoch INTEGER NOT NULL)""")
        self.execute("""CREATE TABLE IF NOT EXISTS dividend_distributions (
            distribution_id INTEGER PRIMARY KEY AUTOINCREMENT, operation_key TEXT NOT NULL UNIQUE,
            asset_code TEXT NOT NULL, kind TEXT NOT NULL, due_epoch INTEGER NOT NULL,
            per_share INTEGER NOT NULL CHECK(per_share >= 0), quote_currency TEXT NOT NULL,
            gross_amount INTEGER NOT NULL DEFAULT 0, recorded_epoch INTEGER NOT NULL)""")
        distribution_columns = {row[1] for row in self.execute("PRAGMA table_info(dividend_distributions)")}
        for column, declaration in {
            "dividend_mode": "TEXT NOT NULL DEFAULT 'fixed' CHECK(dividend_mode IN ('fixed','percent'))",
            "dividend_percent": "REAL NOT NULL DEFAULT 0 CHECK(dividend_percent >= 0 AND dividend_percent <= 100)",
            "reference_price": "INTEGER CHECK(reference_price > 0)",
        }.items():
            if column not in distribution_columns:
                self.execute(f"ALTER TABLE dividend_distributions ADD COLUMN {column} {declaration}")
        self.execute("""CREATE TABLE IF NOT EXISTS dividend_payments (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT, distribution_id INTEGER NOT NULL,
            user_id TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
            gross_amount INTEGER NOT NULL CHECK(gross_amount >= 0), tax_nwx INTEGER NOT NULL DEFAULT 0,
            net_units INTEGER NOT NULL CHECK(net_units >= 0), quote_currency TEXT NOT NULL,
            UNIQUE(distribution_id, user_id))""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dividend_payments_user ON dividend_payments(user_id, distribution_id)")
        self.execute("""CREATE TABLE IF NOT EXISTS corporate_equity_offerings (
            offering_id INTEGER PRIMARY KEY AUTOINCREMENT, asset_code TEXT NOT NULL,
            order_id INTEGER NOT NULL UNIQUE, quantity INTEGER NOT NULL CHECK(quantity > 0),
            sold_quantity INTEGER NOT NULL DEFAULT 0 CHECK(sold_quantity >= 0 AND sold_quantity <= quantity),
            issue_price INTEGER NOT NULL CHECK(issue_price > 0), recorded_epoch INTEGER NOT NULL)""")
        self.execute("""CREATE TABLE IF NOT EXISTS market_quote_overrides (
            asset_code TEXT PRIMARY KEY, price INTEGER NOT NULL CHECK(price>0),
            updated_epoch INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'event')""")
        self.execute("""CREATE TABLE IF NOT EXISTS futures_positions (
            user_id TEXT NOT NULL, product_code TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity != 0),
            settlement_price INTEGER NOT NULL CHECK(settlement_price > 0),
            collateral INTEGER NOT NULL CHECK(collateral >= 0),
            net_pnl INTEGER NOT NULL DEFAULT 0,
            updated_epoch INTEGER NOT NULL,
            PRIMARY KEY(user_id, product_code)
        )""")
        futures_columns = {row[1] for row in self.execute("PRAGMA table_info(futures_positions)")}
        if "net_pnl" not in futures_columns:
            self.execute("ALTER TABLE futures_positions ADD COLUMN net_pnl INTEGER NOT NULL DEFAULT 0")
        self.execute("""CREATE TABLE IF NOT EXISTS product_settlements (
            settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, product_code TEXT NOT NULL,
            kind TEXT NOT NULL, quantity INTEGER NOT NULL,
            gross_amount INTEGER NOT NULL, tax_amount INTEGER NOT NULL DEFAULT 0,
            recorded_epoch INTEGER NOT NULL
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS chart_preferences (
            user_id TEXT PRIMARY KEY,
            upper_indicators TEXT NOT NULL DEFAULT '["sma"]',
            lower_indicators TEXT NOT NULL DEFAULT '["volume"]',
            anchor_epoch INTEGER
        )""")
        self.execute(
            """CREATE TABLE IF NOT EXISTS market_assets (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                exchange_id TEXT,
                price INTEGER NOT NULL,
                type TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                is_fixed INTEGER NOT NULL DEFAULT 0,
                market_volatility REAL DEFAULT NULL,
                total_supply INTEGER DEFAULT NULL
            )"""
        )
        market_columns = {row[1] for row in self.execute("PRAGMA table_info(market_assets)")}
        if "is_fixed" not in market_columns:
            self.execute("ALTER TABLE market_assets ADD COLUMN is_fixed INTEGER NOT NULL DEFAULT 0")
        if "market_volatility" not in market_columns:
            self.execute("ALTER TABLE market_assets ADD COLUMN market_volatility REAL DEFAULT NULL")
        if "market_condition" not in market_columns:
            self.execute("ALTER TABLE market_assets ADD COLUMN market_condition TEXT DEFAULT NULL")
        if "total_supply" not in market_columns:
            self.execute("ALTER TABLE market_assets ADD COLUMN total_supply INTEGER DEFAULT NULL")
        for column, declaration in {
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
        }.items():
            if column not in market_columns:
                self.execute(f"ALTER TABLE market_assets ADD COLUMN {column} {declaration}")

        self.initialize_dividend_activation("market_assets", "code")
        # Holdings are account-scoped. A NULL account_number represents the user's wallet.
        # Migrate the old (user_id, asset_code) table without discarding existing positions.
        table_info = self.execute("PRAGMA table_info(user_assets)")
        if not table_info:
            self.execute(
                """CREATE TABLE user_assets (
                    holding_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    asset_code TEXT NOT NULL,
                    account_number TEXT,
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    average_cost INTEGER NOT NULL DEFAULT 0
                )"""
            )
        else:
            columns = {row[1] for row in table_info}
            if "account_number" not in columns or "average_cost" not in columns or "holding_id" not in columns:
                old_name = "user_assets_legacy"
                legacy_exists = self.execute_one(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (old_name,)
                )
                if not legacy_exists:
                    self.execute(f"ALTER TABLE user_assets RENAME TO {old_name}")
                self.execute(
                    """CREATE TABLE IF NOT EXISTS user_assets (
                        holding_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        asset_code TEXT NOT NULL,
                        account_number TEXT,
                        quantity INTEGER NOT NULL CHECK(quantity > 0),
                        average_cost INTEGER NOT NULL DEFAULT 0
                    )"""
                )
                legacy_columns = {row[1] for row in self.execute(f"PRAGMA table_info({old_name})")}
                if {"user_id", "asset_code", "quantity"}.issubset(legacy_columns):
                    self.execute(
                        f"""INSERT INTO user_assets (user_id, asset_code, account_number, quantity, average_cost)
                            SELECT old.user_id, old.asset_code, NULL, old.quantity,
                                   COALESCE((SELECT price FROM market_assets WHERE code = old.asset_code), 0)
                            FROM {old_name} AS old
                            WHERE old.quantity > 0 AND NOT EXISTS (
                                SELECT 1 FROM user_assets current
                                WHERE current.user_id = old.user_id
                                  AND current.asset_code = old.asset_code
                                  AND current.account_number IS NULL
                            )"""
                    )
        self.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_assets_position "
            "ON user_assets(user_id, asset_code, IFNULL(account_number, ''))"
        )
        self.execute("CREATE INDEX IF NOT EXISTS idx_user_assets_user ON user_assets(user_id)")
        self.enforce_integer_money("market_assets", ("price",), "code")
        self.enforce_integer_money("user_assets", ("average_cost",), "holding_id")
        self.execute(
            """CREATE TABLE IF NOT EXISTS market_price_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_code TEXT NOT NULL,
                price INTEGER NOT NULL,
                recorded_epoch INTEGER NOT NULL,
                UNIQUE(asset_code, recorded_epoch)
            )"""
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_price_history_code_time "
            "ON market_price_history(asset_code, recorded_epoch)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_price_history_epoch "
            "ON market_price_history(recorded_epoch)"
        )
        self.enforce_integer_money("market_price_history", ("price",), "history_id")
        self.execute(
            """CREATE TABLE IF NOT EXISTS market_trade_history (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_code TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                price INTEGER NOT NULL,
                recorded_epoch INTEGER NOT NULL
            )"""
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_trade_history_code_time "
            "ON market_trade_history(asset_code, recorded_epoch)"
        )
        self.enforce_integer_money("market_trade_history", ("price",), "trade_id")
        # A current quote is state, rather than a chart log. Keep one latest
        # fill per asset so clearing yesterday's candles does not reprice
        # options at a theoretical premium after an actual trade.
        self.execute(
            """CREATE TABLE IF NOT EXISTS market_last_trade (
                asset_code TEXT PRIMARY KEY,
                price INTEGER NOT NULL,
                recorded_epoch INTEGER NOT NULL,
                trade_id INTEGER NOT NULL
            )"""
        )
        self.enforce_integer_money("market_last_trade", ("price",), "asset_code")
        self.execute(
            """INSERT INTO market_last_trade
                (asset_code, price, recorded_epoch, trade_id)
                SELECT h.asset_code, h.price, h.recorded_epoch, h.trade_id
                FROM market_trade_history h
                WHERE NOT EXISTS (
                    SELECT 1 FROM market_trade_history newer
                    WHERE newer.asset_code = h.asset_code AND (
                        newer.recorded_epoch > h.recorded_epoch OR
                        (newer.recorded_epoch = h.recorded_epoch
                         AND newer.trade_id > h.trade_id)
                    )
                )
                ON CONFLICT(asset_code) DO UPDATE SET
                    price = excluded.price,
                    recorded_epoch = excluded.recorded_epoch,
                    trade_id = excluded.trade_id
                WHERE excluded.recorded_epoch > market_last_trade.recorded_epoch
                   OR (excluded.recorded_epoch = market_last_trade.recorded_epoch
                       AND excluded.trade_id >= market_last_trade.trade_id)"""
        )
        for operation in ("INSERT", "UPDATE OF price"):
            suffix = "insert" if operation == "INSERT" else "update"
            self.execute(
                f"""CREATE TRIGGER IF NOT EXISTS trg_market_last_trade_{suffix}
                    AFTER {operation} ON market_trade_history BEGIN
                    INSERT INTO market_last_trade
                        (asset_code, price, recorded_epoch, trade_id)
                    VALUES (NEW.asset_code, ROUND(NEW.price), NEW.recorded_epoch, NEW.trade_id)
                    ON CONFLICT(asset_code) DO UPDATE SET
                        price = excluded.price,
                        recorded_epoch = excluded.recorded_epoch,
                        trade_id = excluded.trade_id
                    WHERE excluded.recorded_epoch > market_last_trade.recorded_epoch
                       OR (excluded.recorded_epoch = market_last_trade.recorded_epoch
                           AND excluded.trade_id >= market_last_trade.trade_id);
                    END"""
            )
        self.execute(
            """CREATE TABLE IF NOT EXISTS market_future_expiry_prices (
                product_code TEXT PRIMARY KEY,
                underlying_code TEXT NOT NULL,
                maturity_epoch INTEGER NOT NULL,
                price INTEGER NOT NULL CHECK(price > 0),
                recorded_epoch INTEGER NOT NULL,
                is_final INTEGER NOT NULL DEFAULT 0 CHECK(is_final IN (0, 1))
            )"""
        )
        self.enforce_integer_money("market_future_expiry_prices", ("price",), "product_code")
        # Synthetic, market-cap-weighted index series are persisted alongside
        # market history so /movement can render them like ordinary assets.
        self.execute(
            """CREATE TABLE IF NOT EXISTS market_exchange_indices (
                exchange_id TEXT PRIMARY KEY,
                index_value REAL NOT NULL DEFAULT 1000,
                updated_epoch INTEGER NOT NULL
            )"""
        )
        self.execute(
            """CREATE TABLE IF NOT EXISTS market_exchange_index_components (
                exchange_id TEXT NOT NULL,
                company_code TEXT NOT NULL,
                previous_price REAL NOT NULL,
                total_shares INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(exchange_id, company_code)
            )"""
        )
        self.execute(
            """CREATE TABLE IF NOT EXISTS market_exchange_index_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange_id TEXT NOT NULL,
                index_value REAL NOT NULL,
                recorded_epoch INTEGER NOT NULL,
                UNIQUE(exchange_id, recorded_epoch)
            )"""
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_exchange_index_history_time "
            "ON market_exchange_index_history(exchange_id, recorded_epoch)"
        )
        self.execute(
            """CREATE TABLE IF NOT EXISTS leveraged_etf_state (
                product_code TEXT PRIMARY KEY,
                underlying_exchange_id TEXT NOT NULL,
                leverage_multiplier REAL NOT NULL,
                rebalance_cycle_seconds INTEGER NOT NULL DEFAULT 86400,
                base_index_value REAL NOT NULL,
                base_nav INTEGER NOT NULL,
                last_rebalance_epoch INTEGER NOT NULL
            )"""
        )
        etf_state_columns = {
            row[1] for row in self.execute("PRAGMA table_info(leveraged_etf_state)")
        }
        if "rebalance_cycle_seconds" not in etf_state_columns:
            self.execute(
                "ALTER TABLE leveraged_etf_state ADD COLUMN "
                "rebalance_cycle_seconds INTEGER NOT NULL DEFAULT 86400"
            )
        self.enforce_integer_money("leveraged_etf_state", ("base_nav",), "product_code")
        self.enforce_integer_money(
            "market_exchange_index_components", ("previous_price",), "company_code"
        )
        self.execute(
            """CREATE TABLE IF NOT EXISTS market_orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                asset_code TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
                limit_price INTEGER NOT NULL CHECK(limit_price > 0),
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                remaining_quantity INTEGER NOT NULL CHECK(remaining_quantity >= 0),
                filled_quantity INTEGER NOT NULL DEFAULT 0,
                reserved_cash INTEGER NOT NULL DEFAULT 0,
                funding_account TEXT,
                order_kind TEXT NOT NULL DEFAULT 'asset',
                option_order_type TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_epoch INTEGER NOT NULL,
                updated_epoch INTEGER NOT NULL
            )"""
        )
        order_columns = {row[1] for row in self.execute("PRAGMA table_info(market_orders)")}
        if "filled_quantity" not in order_columns:
            self.execute(
                "ALTER TABLE market_orders ADD COLUMN filled_quantity INTEGER NOT NULL DEFAULT 0"
            )
        if "order_kind" not in order_columns:
            self.execute(
                "ALTER TABLE market_orders ADD COLUMN order_kind TEXT NOT NULL DEFAULT 'asset'"
            )
        if "option_order_type" not in order_columns:
            self.execute("ALTER TABLE market_orders ADD COLUMN option_order_type TEXT")
        self.enforce_integer_money("market_orders", ("reserved_cash",), "order_id")
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_orders_book "
            "ON market_orders(asset_code, side, status, limit_price, created_epoch, order_id)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_orders_user "
            "ON market_orders(user_id, status, order_id)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_orders_instrument "
            "ON market_orders(order_kind, asset_code, side, status, limit_price, created_epoch, order_id)"
        )
        self.execute(
            """CREATE TABLE IF NOT EXISTS option_contracts (
                contract_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_code TEXT UNIQUE,
                writer_id TEXT NOT NULL,
                underlying_code TEXT NOT NULL,
                option_type TEXT NOT NULL CHECK(option_type IN ('call', 'put')),
                strike_price INTEGER NOT NULL CHECK(strike_price > 0),
                expiration_epoch INTEGER NOT NULL,
                implied_volatility REAL NOT NULL,
                issued_contracts INTEGER NOT NULL CHECK(issued_contracts > 0),
                outstanding_contracts INTEGER NOT NULL DEFAULT 0,
                collateral_account TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_epoch INTEGER NOT NULL,
                settled_epoch INTEGER
            )"""
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_option_contracts_expiry "
            "ON option_contracts(status, expiration_epoch)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_option_contracts_underlying "
            "ON option_contracts(underlying_code, status, expiration_epoch)"
        )
        self.execute(
            """CREATE TABLE IF NOT EXISTS option_positions (
                contract_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                average_premium INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(contract_id, user_id)
            )"""
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_option_positions_user "
            "ON option_positions(user_id, contract_id)"
        )
        self.execute(
            """CREATE TABLE IF NOT EXISTS option_collateral_lots (
                collateral_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                writer_id TEXT NOT NULL,
                underlying_code TEXT NOT NULL,
                account_number TEXT,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                average_cost INTEGER NOT NULL DEFAULT 0
            )"""
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_option_collateral_contract "
            "ON option_collateral_lots(contract_id, collateral_id)"
        )
        self.execute(
            """CREATE TABLE IF NOT EXISTS option_collateral_cash_lots (
                collateral_id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                writer_id TEXT NOT NULL,
                account_number TEXT,
                amount INTEGER NOT NULL CHECK(amount > 0)
            )"""
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_option_cash_collateral_contract "
            "ON option_collateral_cash_lots(contract_id, collateral_id)"
        )

    def register_asset(self, code, name, ex_id, price, asset_type, total_supply=None):
        self.execute(
            "INSERT OR REPLACE INTO market_assets "
            "(code, name, exchange_id, price, type, status, is_fixed, total_supply) "
            "VALUES (?, ?, ?, ?, ?, 'active', 0, ?)",
            (code, name, ex_id, price, asset_type, total_supply),
        )

    def add_user_asset(self, user_id, code, qty, account_number=None, unit_cost=0):
        self.execute(
            """INSERT INTO user_assets (user_id, asset_code, account_number, quantity, average_cost)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, asset_code, IFNULL(account_number, '')) DO UPDATE SET
                average_cost = ((quantity * average_cost) + (excluded.quantity * excluded.average_cost))
                               / (quantity + excluded.quantity),
                quantity = quantity + excluded.quantity""",
            (user_id, code, account_number, qty, unit_cost),
        )

    def remove_user_asset(self, user_id, code, qty, account_number=None):
        self.execute(
            "UPDATE user_assets SET quantity = quantity - ? "
            "WHERE user_id = ? AND asset_code = ? AND account_number IS ? AND quantity >= ?",
            (qty, user_id, code, account_number, qty),
        )
        self.execute("DELETE FROM user_assets WHERE quantity <= 0")

    def get_user_positions(self, user_id, code=None):
        if code is None:
            return self.execute(
                "SELECT holding_id, user_id, asset_code, account_number, quantity, average_cost "
                "FROM user_assets WHERE user_id = ? ORDER BY asset_code, account_number",
                (user_id,),
            )
        return self.execute(
            "SELECT holding_id, user_id, asset_code, account_number, quantity, average_cost "
            "FROM user_assets WHERE user_id = ? AND asset_code = ? ORDER BY account_number",
            (user_id, code),
        )

    def get_user_portfolio(self, user_id):
        return self.execute(
            "SELECT asset_code, SUM(quantity) AS quantity FROM user_assets "
            "WHERE user_id = ? GROUP BY asset_code ORDER BY asset_code",
            (user_id,),
        )

    def get_asset_info(self, code):
        return self.execute_one("SELECT * FROM market_assets WHERE code = ?", (code,))

    def get_trade_history(self, code, since_epoch):
        return self.execute(
            "SELECT recorded_epoch, side, quantity, price FROM market_trade_history "
            "WHERE asset_code = ? AND recorded_epoch >= ? ORDER BY recorded_epoch, trade_id",
            (str(code), int(since_epoch)),
        )

    def get_last_trade(self, code):
        return self.execute_one(
            "SELECT price, recorded_epoch, trade_id FROM market_last_trade WHERE asset_code = ?",
            (str(code),),
        )

    def update_price(self, code, new_price):
        self.execute("UPDATE market_assets SET price = ? WHERE code = ?", (new_price, code))

    def update_prices_bulk(self, updates):
        if not updates:
            return
        with self.lock:
            with self.conn:
                self.conn.executemany("UPDATE market_assets SET price = ? WHERE code = ?", updates)
