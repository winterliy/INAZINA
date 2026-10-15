from .db_manager import DBManager


class SystemDB(DBManager):
    """운영자가 SQL Browser에서 관리하는 시스템 설정 저장소."""

    def __init__(self):
        super().__init__("system")
        self._init_db()

    def _init_db(self):
        self.execute(
            '''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            '''
        )
        self.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("gamble_win_rate", "0.5"),
        )
        self.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("gamble_return", "100"),
        )
        legacy_probability = self.execute_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'probability'"
        )
        if legacy_probability:
            self.execute(
                """INSERT INTO settings (key, value)
                   SELECT 'gamble_chance', CAST(chance AS TEXT) FROM probability WHERE id = 1
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value"""
            )
            self.execute("DROP TABLE probability")
        self.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("gamble_chance", "50"),
        )
        legacy_return = self.execute_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'return'"
        )
        legacy_migrated = self.execute_one(
            "SELECT 1 FROM settings WHERE key = 'migration_return_to_settings'"
        )
        if legacy_return and not legacy_migrated:
            self.execute(
                """UPDATE settings
                   SET value = (SELECT CAST(return_percent AS TEXT) FROM "return" WHERE id = 1)
                   WHERE key = 'gamble_return'
                     AND EXISTS (SELECT 1 FROM "return" WHERE id = 1)"""
            )
            self.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                ("migration_return_to_settings", "1"),
            )
        self.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("market_volatility", "0.15"),
        )
        self.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("market_condition", "flat"),
        )
        for key, value in (
            ("garch_alpha", "0.05"), ("garch_beta", "0.90"),
            ("garch_annual_volatility", "0.30"),
            ("garch_market_correlation", "0.55"),
            ("garch_step_seconds", "1"),
            ("garch_trading_minutes_per_step", "1"),
            ("garch_student_df", "6"),
        ):
            self.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        # This model state is independent of Movement's daily chart logs, so a
        # new calendar day or a process restart cannot erase volatility memory.
        self.execute("""CREATE TABLE IF NOT EXISTS market_simulation_state (
            asset_code TEXT PRIMARY KEY,
            variance REAL NOT NULL CHECK(variance >= 0 AND variance < 1.0e308),
            innovation REAL NOT NULL CHECK(ABS(innovation) < 1.0e308),
            latent_price REAL NOT NULL CHECK(latent_price >= 1 AND latent_price <= 9007199254740991),
            last_epoch INTEGER NOT NULL,
            last_display_price INTEGER NOT NULL CHECK(last_display_price > 0),
            event_id INTEGER
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS market_factor_state (
            key TEXT PRIMARY KEY,
            last_epoch INTEGER NOT NULL,
            current_factor REAL NOT NULL CHECK(ABS(current_factor) < 1.0e308)
        )""")
        for key, value in (
            ("auto_target_enabled", "1"),
            ("auto_target_range_percent", "10"),
            ("auto_target_min_seconds", "60"),
            ("auto_target_max_seconds", "180"),
            ("auto_target_pull", "0.08"),
            ("auto_target_noise_multiplier", "6"),
            ("auto_target_band_percent", "25"),
        ):
            self.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        # Soft automatic targets are model state, not disposable chart history.
        # Explicit administrator events retain priority in the price manager.
        self.execute("""CREATE TABLE IF NOT EXISTS market_auto_targets (
            asset_code TEXT PRIMARY KEY,
            anchor_price REAL NOT NULL CHECK(anchor_price >= 1 AND anchor_price <= 9007199254740991),
            target_price REAL NOT NULL CHECK(target_price >= 1 AND target_price <= 9007199254740991),
            start_epoch INTEGER NOT NULL,
            end_epoch INTEGER NOT NULL CHECK(end_epoch > start_epoch)
        )""")
        for key, value in (("base_rate", "3"), ("legal_max_rate", "20"), ("tax_percent", "3")):
            self.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        # The former statutory rate accrued extra interest after loan maturity.
        # A legal maximum is a cap instead, so do not migrate the old value.
        self.execute("DELETE FROM settings WHERE key='statutory_rate'")
        self._init_gamble_triggers()
        self._init_rate_triggers()
        self.execute("""CREATE TABLE IF NOT EXISTS market_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_code TEXT,
            event_type TEXT NOT NULL CHECK(event_type IN ('good', 'bad')),
            severity INTEGER NOT NULL CHECK(severity BETWEEN 0 AND 100),
            starts_epoch INTEGER NOT NULL,
            ends_epoch INTEGER NOT NULL CHECK(ends_epoch > starts_epoch),
            created_by TEXT NOT NULL,
            target_mode TEXT NOT NULL DEFAULT 'legacy' CHECK(target_mode IN ('legacy', 'percent', 'price')),
            target_value REAL
        )""")
        event_columns = {row[1] for row in self.execute("PRAGMA table_info(market_events)")}
        if "target_mode" not in event_columns:
            self.execute("ALTER TABLE market_events ADD COLUMN target_mode TEXT NOT NULL DEFAULT 'legacy' "
                         "CHECK(target_mode IN ('legacy', 'percent', 'price'))")
        if "target_value" not in event_columns:
            self.execute("ALTER TABLE market_events ADD COLUMN target_value REAL")
        if "volatility_percent" not in event_columns:
            self.execute("ALTER TABLE market_events ADD COLUMN volatility_percent REAL "
                         "CHECK(volatility_percent BETWEEN 0 AND 100)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_market_events_active ON market_events(ends_epoch, starts_epoch)")
        # One row is the complete current state of one exchange: its index and
        # its trading session.  Index components/history remain separate
        # one-to-many/time-series tables.
        self.execute("""CREATE TABLE IF NOT EXISTS market_exchanges (
            exchange_id TEXT PRIMARY KEY,
            index_value REAL NOT NULL DEFAULT 1000,
            index_updated_epoch INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK(status IN ('open', 'closed', 'paused')),
            reason TEXT NOT NULL DEFAULT '',
            halt_started_epoch INTEGER,
            resume_epoch INTEGER,
            circuit_stage INTEGER CHECK(circuit_stage IN (1, 2, 3)),
            updated_epoch INTEGER NOT NULL,
            trading_halted INTEGER NOT NULL DEFAULT 0 CHECK(trading_halted IN (0, 1)),
            nationality TEXT NOT NULL DEFAULT '낙원'
        )""")
        exchange_columns = {row[1] for row in self.execute("PRAGMA table_info(market_exchanges)")}
        if 'nationality' not in exchange_columns:
            self.execute("ALTER TABLE market_exchanges ADD COLUMN nationality TEXT NOT NULL DEFAULT '낙원'")
        # Migrate both former exchange tables before removing them.  This also
        # covers installations that predate per-exchange market sessions.
        legacy_tables = {str(row[0]) for row in self.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('market_exchange_indices', 'market_exchange_sessions', 'market_session')"
        )}
        if "market_exchange_indices" in legacy_tables:
            self.execute("""INSERT OR IGNORE INTO market_exchanges
                (exchange_id, index_value, index_updated_epoch)
                SELECT exchange_id, index_value, updated_epoch
                FROM market_exchange_indices""")
            self.execute("""UPDATE market_exchanges
                SET index_value=(SELECT index_value FROM market_exchange_indices old
                                 WHERE old.exchange_id=market_exchanges.exchange_id),
                    index_updated_epoch=(SELECT updated_epoch FROM market_exchange_indices old
                                         WHERE old.exchange_id=market_exchanges.exchange_id)
                WHERE EXISTS (SELECT 1 FROM market_exchange_indices old
                              WHERE old.exchange_id=market_exchanges.exchange_id)""")
        if "market_exchange_sessions" in legacy_tables:
            self.execute("""INSERT OR IGNORE INTO market_exchanges
                (exchange_id,status,reason,halt_started_epoch,resume_epoch,circuit_stage,updated_epoch,trading_halted)
                SELECT exchange_id,status,reason,halt_started_epoch,resume_epoch,circuit_stage,updated_epoch,trading_halted
                FROM market_exchange_sessions""")
            self.execute("""UPDATE market_exchanges
                SET status=(SELECT status FROM market_exchange_sessions old WHERE old.exchange_id=market_exchanges.exchange_id),
                    reason=(SELECT reason FROM market_exchange_sessions old WHERE old.exchange_id=market_exchanges.exchange_id),
                    halt_started_epoch=(SELECT halt_started_epoch FROM market_exchange_sessions old WHERE old.exchange_id=market_exchanges.exchange_id),
                    resume_epoch=(SELECT resume_epoch FROM market_exchange_sessions old WHERE old.exchange_id=market_exchanges.exchange_id),
                    circuit_stage=(SELECT circuit_stage FROM market_exchange_sessions old WHERE old.exchange_id=market_exchanges.exchange_id),
                    updated_epoch=(SELECT updated_epoch FROM market_exchange_sessions old WHERE old.exchange_id=market_exchanges.exchange_id),
                    trading_halted=(SELECT trading_halted FROM market_exchange_sessions old WHERE old.exchange_id=market_exchanges.exchange_id)
                WHERE EXISTS (SELECT 1 FROM market_exchange_sessions old WHERE old.exchange_id=market_exchanges.exchange_id)""")
        if "market_session" in legacy_tables:
            self.execute("""INSERT OR IGNORE INTO market_exchanges
                (exchange_id,status,reason,halt_started_epoch,resume_epoch,circuit_stage,updated_epoch,trading_halted)
                SELECT exchange_id, s.status, s.reason, s.halt_started_epoch,
                       s.resume_epoch, s.circuit_stage, s.updated_epoch,
                       CASE WHEN s.status='open' THEN 0 ELSE 1 END
                FROM (
                SELECT DISTINCT TRIM(exchange_id) AS exchange_id FROM market_assets
                WHERE exchange_id IS NOT NULL AND TRIM(exchange_id) <> ''
                UNION SELECT DISTINCT TRIM(exchange_id) FROM company
                WHERE exchange_id IS NOT NULL AND TRIM(exchange_id) <> ''
                UNION SELECT DISTINCT TRIM(exchange_id) FROM investment
                WHERE exchange_id IS NOT NULL AND TRIM(exchange_id) <> ''
                UNION SELECT DISTINCT TRIM(exchange_id) FROM treasury_bond_products
                WHERE exchange_id IS NOT NULL AND TRIM(exchange_id) <> ''
                UNION SELECT 'CURRENCY'
                ) exchanges CROSS JOIN market_session s WHERE s.session_id=1""")
        self.execute("""INSERT OR IGNORE INTO market_exchanges
            (exchange_id,status,updated_epoch,trading_halted)
            SELECT exchange_id, 'open', 0, 0 FROM (
                SELECT DISTINCT TRIM(exchange_id) AS exchange_id FROM market_assets WHERE exchange_id IS NOT NULL AND TRIM(exchange_id) <> ''
                UNION SELECT DISTINCT TRIM(exchange_id) FROM company WHERE exchange_id IS NOT NULL AND TRIM(exchange_id) <> ''
                UNION SELECT DISTINCT TRIM(exchange_id) FROM investment WHERE exchange_id IS NOT NULL AND TRIM(exchange_id) <> ''
                UNION SELECT DISTINCT TRIM(exchange_id) FROM treasury_bond_products WHERE exchange_id IS NOT NULL AND TRIM(exchange_id) <> ''
                UNION SELECT 'CURRENCY'
            )""")
        self.execute("DROP TABLE IF EXISTS market_exchange_indices")
        self.execute("DROP TABLE IF EXISTS market_exchange_sessions")
        self.execute("DROP TABLE IF EXISTS market_session")
        self.execute("""CREATE TABLE IF NOT EXISTS market_event_targets (
            event_id INTEGER NOT NULL,
            asset_code TEXT NOT NULL,
            start_price INTEGER NOT NULL CHECK(start_price > 0),
            target_price INTEGER NOT NULL CHECK(target_price > 0),
            start_epoch INTEGER NOT NULL,
            end_epoch INTEGER NOT NULL CHECK(end_epoch > start_epoch),
            state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active', 'completed', 'superseded')),
            completed_epoch INTEGER,
            PRIMARY KEY(event_id, asset_code)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_market_event_targets_asset "
                     "ON market_event_targets(asset_code, state, start_epoch)")
        target_columns = {row[1] for row in self.execute("PRAGMA table_info(market_event_targets)")}
        for column, definition in (("noise_fraction", "REAL NOT NULL DEFAULT 0"),
                                   ("last_tick_epoch", "INTEGER"), ("last_price", "INTEGER"),
                                   ("jump_applied_epoch", "INTEGER")):
            if column not in target_columns:
                self.execute(f"ALTER TABLE market_event_targets ADD COLUMN {column} {definition}")
        self.execute("""CREATE TABLE IF NOT EXISTS admin_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            payload TEXT NOT NULL,
            recorded_epoch REAL NOT NULL
        )""")

    def _init_rate_triggers(self):
        """Keep browser edits to policy/default rate settings valid."""
        for operation in ("INSERT", "UPDATE OF key, value"):
            suffix = "insert" if operation == "INSERT" else "update"
            self.execute(f"DROP TRIGGER IF EXISTS trg_settings_rate_validate_{suffix}")
            self.execute(f"""CREATE TRIGGER trg_settings_rate_validate_{suffix}
                BEFORE {operation} ON settings
                WHEN NEW.key IN ('base_rate', 'legal_max_rate')
                BEGIN
                    SELECT CASE WHEN json_valid(NEW.value) = 0
                        THEN RAISE(ABORT, 'Rate must be a valid number') END;
                    SELECT CASE WHEN json_type(NEW.value) NOT IN ('integer', 'real')
                        OR (NEW.key='base_rate' AND CAST(NEW.value AS REAL) NOT BETWEEN -100 AND 100)
                        OR (NEW.key='legal_max_rate' AND CAST(NEW.value AS REAL) NOT BETWEEN 0 AND 100)
                        THEN RAISE(ABORT, 'Rate is outside its permitted range') END;
                END""")

    def _init_gamble_triggers(self):
        """Keep browser edits to gambling settings within supported ranges."""
        for operation in ("INSERT", "UPDATE OF key, value"):
            suffix = "insert" if operation == "INSERT" else "update"
            self.execute(f"DROP TRIGGER IF EXISTS trg_settings_gamble_validate_{suffix}")
            self.execute(f"""CREATE TRIGGER trg_settings_gamble_validate_{suffix}
                BEFORE {operation} ON settings
                WHEN NEW.key IN ('gamble_chance', 'gamble_return')
                BEGIN
                    SELECT CASE WHEN json_valid(NEW.value) = 0
                        THEN RAISE(ABORT, 'Gamble setting must be numeric') END;
                    SELECT CASE WHEN json_type(NEW.value) NOT IN ('integer', 'real')
                        OR (NEW.key = 'gamble_chance' AND CAST(NEW.value AS REAL) NOT BETWEEN 0 AND 100)
                        OR (NEW.key = 'gamble_return' AND CAST(NEW.value AS REAL) NOT BETWEEN 0 AND 10000)
                        THEN RAISE(ABORT, 'Gamble setting is outside its supported range') END;
                END""")

    def get_setting(self, key, default=None):
        row = self.execute_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
