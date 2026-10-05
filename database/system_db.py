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
            key TEXT PRIMARY KEY CHECK(key = 'market'),
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
        for key, value in (("base_rate", "3"), ("statutory_rate", "5")):
            self.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
        self.execute("""CREATE TABLE IF NOT EXISTS rate_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            rate_key TEXT NOT NULL CHECK(rate_key IN ('base_rate', 'statutory_rate')),
            annual_percent REAL NOT NULL CHECK(annual_percent >= 0 AND annual_percent <= 100),
            effective_epoch REAL NOT NULL,
            changed_by TEXT NOT NULL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_rate_history_effective "
                     "ON rate_history(rate_key, effective_epoch, history_id)")
        for key in ("base_rate", "statutory_rate"):
            self.execute("INSERT INTO rate_history (rate_key, annual_percent, effective_epoch, changed_by) "
                         "SELECT ?, CAST(value AS REAL), 0, 'initial' FROM settings WHERE key = ? "
                         "AND NOT EXISTS (SELECT 1 FROM rate_history WHERE rate_key = ?)", (key, key, key))
        self._init_rate_triggers()
        # Preserve past accrual when a rate was edited before browser triggers
        # were installed. Record the stored value from this migration onward.
        self.execute("""INSERT INTO rate_history
            (rate_key, annual_percent, effective_epoch, changed_by)
            SELECT key, CAST(value AS REAL), (julianday('now') - 2440587.5) * 86400.0, 'database'
            FROM settings WHERE key IN ('base_rate', 'statutory_rate')
            AND CAST(value AS REAL) <> (
                SELECT annual_percent FROM rate_history WHERE rate_key = settings.key
                ORDER BY history_id DESC LIMIT 1
            )""")
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
        # A persisted market halt survives process restarts. Deadlines and event
        # clocks are shifted exactly once when the market reopens.
        self.execute("""CREATE TABLE IF NOT EXISTS market_session (
            session_id INTEGER PRIMARY KEY CHECK(session_id = 1),
            status TEXT NOT NULL CHECK(status IN ('open', 'closed', 'paused')),
            reason TEXT NOT NULL DEFAULT '',
            halt_started_epoch INTEGER,
            resume_epoch INTEGER,
            circuit_stage INTEGER CHECK(circuit_stage IN (1, 2, 3)),
            updated_epoch INTEGER NOT NULL
        )""")
        self.execute("INSERT OR IGNORE INTO market_session "
                     "(session_id, status, updated_epoch) VALUES (1, 'open', 0)")
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
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_event_quote_after_trade
            AFTER INSERT ON market_trade_history BEGIN
            DELETE FROM market_quote_overrides WHERE asset_code=NEW.asset_code
              AND NOT EXISTS (SELECT 1 FROM market_event_targets
                              WHERE asset_code=NEW.asset_code AND state='active');
            END""")

    def _init_rate_triggers(self):
        """Keep browser edits validated and synchronized with dated interest."""
        for operation in ("INSERT", "UPDATE OF key, value"):
            suffix = "insert" if operation == "INSERT" else "update"
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_settings_rate_validate_{suffix}
                BEFORE {operation} ON settings
                WHEN NEW.key IN ('base_rate', 'statutory_rate')
                BEGIN
                    SELECT CASE WHEN json_valid(NEW.value) = 0
                        THEN RAISE(ABORT, 'Rate must be a number between 0 and 100') END;
                    SELECT CASE WHEN json_type(NEW.value) NOT IN ('integer', 'real')
                        OR CAST(NEW.value AS REAL) NOT BETWEEN 0 AND 100
                        THEN RAISE(ABORT, 'Rate must be a number between 0 and 100') END;
                END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_settings_rate_history_insert
            AFTER INSERT ON settings WHEN NEW.key IN ('base_rate', 'statutory_rate')
            BEGIN
                INSERT INTO rate_history (rate_key, annual_percent, effective_epoch, changed_by)
                VALUES (NEW.key, CAST(NEW.value AS REAL),
                        (julianday('now') - 2440587.5) * 86400.0, 'database');
            END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_settings_rate_history_update
            AFTER UPDATE OF key, value ON settings
            WHEN NEW.key IN ('base_rate', 'statutory_rate')
                AND (OLD.key <> NEW.key OR CAST(OLD.value AS REAL) <> CAST(NEW.value AS REAL))
            BEGIN
                INSERT INTO rate_history (rate_key, annual_percent, effective_epoch, changed_by)
                VALUES (NEW.key, CAST(NEW.value AS REAL),
                        (julianday('now') - 2440587.5) * 86400.0, 'database');
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
