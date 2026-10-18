from .db_manager import DBManager
from .fractional import initialize_fractional


class AssetDB(DBManager):
    def __init__(self):
        super().__init__("assets")
        self._init_db()
        initialize_fractional(self, 'market_assets', 'type')

    def _init_db(self):
        self.execute("""CREATE TABLE IF NOT EXISTS product_settlements (
            settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, product_code TEXT NOT NULL,
            kind TEXT NOT NULL, quantity INTEGER NOT NULL,
            gross_amount INTEGER NOT NULL, tax_amount INTEGER NOT NULL DEFAULT 0,
            recorded_epoch INTEGER NOT NULL
        )""")
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
                total_supply INTEGER DEFAULT NULL,
                settlement_delay_seconds INTEGER NOT NULL DEFAULT 0
                    CHECK(settlement_delay_seconds BETWEEN 0 AND 2592000),
                issue_fee_bps INTEGER NOT NULL DEFAULT 0 CHECK(issue_fee_bps BETWEEN 0 AND 10000),
                redeem_fee_bps INTEGER NOT NULL DEFAULT 0 CHECK(redeem_fee_bps BETWEEN 0 AND 10000)
            )"""
        )
        market_asset_columns = {row[1] for row in self.execute("PRAGMA table_info(market_assets)")}
        for column, definition in {
            "settlement_delay_seconds": "INTEGER NOT NULL DEFAULT 0 CHECK(settlement_delay_seconds BETWEEN 0 AND 2592000)",
            "issue_fee_bps": "INTEGER NOT NULL DEFAULT 0 CHECK(issue_fee_bps BETWEEN 0 AND 10000)",
            "redeem_fee_bps": "INTEGER NOT NULL DEFAULT 0 CHECK(redeem_fee_bps BETWEEN 0 AND 10000)",
        }.items():
            if column not in market_asset_columns:
                self.execute(f"ALTER TABLE market_assets ADD COLUMN {column} {definition}")
        self.execute("""CREATE TABLE IF NOT EXISTS depositary_receipts (
            dr_code TEXT PRIMARY KEY,
            underlying_code TEXT NOT NULL,
            receipt_type TEXT NOT NULL DEFAULT 'DR',
            depositary_bank TEXT,
            custodian TEXT,
            home_exchange_id TEXT,
            listing_exchange_id TEXT NOT NULL,
            quote_currency TEXT NOT NULL DEFAULT 'NWX',
            underlying_per_receipt REAL NOT NULL CHECK(underlying_per_receipt > 0),
            receipt_supply INTEGER NOT NULL CHECK(receipt_supply > 0),
            deposited_underlying_quantity REAL NOT NULL CHECK(deposited_underlying_quantity >= 0),
            program_status TEXT NOT NULL DEFAULT 'active'
                CHECK(program_status IN ('active','suspended','terminated'))
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_depositary_receipts_underlying "
                     "ON depositary_receipts(underlying_code,program_status)")
        self.execute("""CREATE TABLE IF NOT EXISTS depositary_receipt_movements (
            movement_id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_key TEXT NOT NULL UNIQUE,
            dr_code TEXT NOT NULL REFERENCES depositary_receipts(dr_code),
            user_id TEXT NOT NULL,
            movement_type TEXT NOT NULL CHECK(movement_type IN ('issue','redeem')),
            dr_quantity INTEGER NOT NULL CHECK(dr_quantity > 0),
            underlying_quantity INTEGER NOT NULL CHECK(underlying_quantity > 0),
            underlying_average_cost REAL NOT NULL DEFAULT 0 CHECK(underlying_average_cost >= 0),
            recorded_epoch REAL NOT NULL
        )""")
        movement_columns = {
            row[1] for row in self.execute("PRAGMA table_info(depositary_receipt_movements)")
        }
        for column, declaration in {
            "holder_underlying_average_cost": "REAL CHECK(holder_underlying_average_cost >= 0)",
            "holder_underlying_currency": "TEXT",
            "custody_underlying_average_cost": "REAL CHECK(custody_underlying_average_cost >= 0)",
            "custody_underlying_currency": "TEXT",
        }.items():
            if column not in movement_columns:
                self.execute(
                    f"ALTER TABLE depositary_receipt_movements ADD COLUMN {column} {declaration}"
                )
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_movements_program "
                     "ON depositary_receipt_movements(dr_code,recorded_epoch)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_movements_user_recent "
                     "ON depositary_receipt_movements(user_id,recorded_epoch DESC,movement_id DESC)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_movements_recent "
                     "ON depositary_receipt_movements(recorded_epoch DESC,movement_id DESC)")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_custody_fee_ledger (
            fee_id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_key TEXT NOT NULL UNIQUE REFERENCES depositary_receipt_movements(reference_key),
            dr_code TEXT NOT NULL REFERENCES depositary_receipts(dr_code),
            user_id TEXT NOT NULL,
            movement_type TEXT NOT NULL CHECK(movement_type IN ('issue','redeem')),
            payer_account_number TEXT NOT NULL,
            fee_currency TEXT NOT NULL,
            fee_minor_units INTEGER NOT NULL CHECK(fee_minor_units > 0),
            fee_bps INTEGER NOT NULL CHECK(fee_bps BETWEEN 1 AND 10000),
            notional_minor_units INTEGER NOT NULL CHECK(notional_minor_units >= 0),
            revenue_account TEXT NOT NULL DEFAULT 'custodian_fee_income',
            recorded_epoch REAL NOT NULL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_custody_fee_recent "
                     "ON dr_custody_fee_ledger(recorded_epoch DESC,fee_id DESC)")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_dr_custody_fee_immutable_{operation.lower()}
                BEFORE {operation} ON dr_custody_fee_ledger
                BEGIN SELECT RAISE(ABORT,'DR custody fee ledger is append-only'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_custody_process_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_key TEXT NOT NULL,
            event_sequence INTEGER NOT NULL CHECK(event_sequence BETWEEN 1 AND 3),
            event_type TEXT NOT NULL CHECK(event_type IN
                ('instruction_received','custody_verified','settled')),
            dr_code TEXT NOT NULL REFERENCES depositary_receipts(dr_code),
            user_id TEXT NOT NULL,
            movement_type TEXT NOT NULL CHECK(movement_type IN ('issue','redeem')),
            dr_quantity INTEGER NOT NULL CHECK(dr_quantity > 0),
            underlying_quantity INTEGER NOT NULL CHECK(underlying_quantity > 0),
            recorded_epoch REAL NOT NULL,
            UNIQUE(reference_key,event_sequence)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_custody_process_recent "
                     "ON dr_custody_process_events(recorded_epoch DESC,event_id DESC)")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_dr_custody_events_immutable_{operation.lower()}
                BEFORE {operation} ON dr_custody_process_events
                BEGIN SELECT RAISE(ABORT,'DR custody process events are append-only'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_custody_instructions (
            reference_key TEXT PRIMARY KEY,
            dr_code TEXT NOT NULL REFERENCES depositary_receipts(dr_code),
            user_id TEXT NOT NULL,
            movement_type TEXT NOT NULL CHECK(movement_type IN ('issue','redeem')),
            dr_quantity INTEGER NOT NULL CHECK(dr_quantity > 0),
            underlying_quantity INTEGER NOT NULL CHECK(underlying_quantity > 0),
            status TEXT NOT NULL CHECK(status IN ('requested','verified','settled','rejected','cancelled')),
            requested_epoch REAL NOT NULL,
            updated_epoch REAL NOT NULL,
            settled_epoch REAL,
            fee_bps INTEGER NOT NULL DEFAULT 0 CHECK(fee_bps BETWEEN 0 AND 10000),
            fee_currency TEXT,
            fee_price INTEGER
        )""")
        custody_instruction_columns = {
            row[1] for row in self.execute("PRAGMA table_info(dr_custody_instructions)")
        }
        for column, declaration in {
            "fee_bps": "INTEGER NOT NULL DEFAULT 0 CHECK(fee_bps BETWEEN 0 AND 10000)",
            "fee_currency": "TEXT",
            "fee_price": "INTEGER",
        }.items():
            if column not in custody_instruction_columns:
                self.execute(f"ALTER TABLE dr_custody_instructions ADD COLUMN {column} {declaration}")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_custody_instruction_status "
                     "ON dr_custody_instructions(status,requested_epoch)")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_custody_instruction_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_key TEXT NOT NULL REFERENCES dr_custody_instructions(reference_key),
            event_sequence INTEGER NOT NULL CHECK(event_sequence BETWEEN 1 AND 5),
            event_type TEXT NOT NULL CHECK(event_type IN
                ('requested','verified','settled','rejected','cancelled')),
            recorded_epoch REAL NOT NULL,
            detail_code TEXT,
            UNIQUE(reference_key,event_sequence)
        )""")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_custody_reservations (
            reference_key TEXT PRIMARY KEY REFERENCES dr_custody_instructions(reference_key),
            dr_code TEXT NOT NULL REFERENCES depositary_receipts(dr_code),
            user_id TEXT NOT NULL,
            movement_type TEXT NOT NULL CHECK(movement_type IN ('issue','redeem')),
            reserved_asset_code TEXT NOT NULL,
            reserved_quantity INTEGER NOT NULL CHECK(reserved_quantity > 0),
            created_epoch REAL NOT NULL,
            settle_after_epoch REAL NOT NULL,
            released_epoch REAL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_custody_reservations_asset "
                     "ON dr_custody_reservations(reserved_asset_code,user_id)")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_custody_settlement_attempts (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference_key TEXT NOT NULL REFERENCES dr_custody_instructions(reference_key),
            attempted_epoch REAL NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('settled','failed')),
            detail TEXT
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_custody_attempts_recent "
                     "ON dr_custody_settlement_attempts(reference_key,attempted_epoch DESC)")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_dr_custody_attempts_immutable_{operation.lower()}
                BEFORE {operation} ON dr_custody_settlement_attempts
                BEGIN SELECT RAISE(ABORT,'DR custody settlement attempts are append-only'); END""")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_dr_custody_instruction_events_immutable_{operation.lower()}
                BEFORE {operation} ON dr_custody_instruction_events
                BEGIN SELECT RAISE(ABORT,'DR custody instruction events are append-only'); END""")
        self.execute("""INSERT OR IGNORE INTO dr_custody_instructions
            (reference_key,dr_code,user_id,movement_type,dr_quantity,underlying_quantity,
             status,requested_epoch,updated_epoch,settled_epoch)
            SELECT reference_key,MIN(dr_code),MIN(user_id),MIN(movement_type),
                   MAX(dr_quantity),MAX(underlying_quantity),
                   CASE MAX(event_sequence) WHEN 1 THEN 'requested'
                        WHEN 2 THEN 'verified' ELSE 'settled' END,
                   MIN(recorded_epoch),MAX(recorded_epoch),
                   CASE WHEN MAX(event_sequence)>=3 THEN MAX(recorded_epoch) END
            FROM dr_custody_process_events GROUP BY reference_key""")
        self.execute("""INSERT OR IGNORE INTO dr_custody_instruction_events
            (reference_key,event_sequence,event_type,recorded_epoch)
            SELECT reference_key,event_sequence,
                   CASE event_type WHEN 'instruction_received' THEN 'requested'
                        WHEN 'custody_verified' THEN 'verified' ELSE 'settled' END,
                   recorded_epoch
            FROM dr_custody_process_events ORDER BY reference_key,event_sequence""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_dr_custody_instruction_event_sequence
            BEFORE INSERT ON dr_custody_instruction_events
            WHEN NOT EXISTS (
                SELECT 1 FROM dr_custody_instructions i
                WHERE i.reference_key=NEW.reference_key AND i.status=NEW.event_type
            ) OR NEW.event_sequence<>(
                SELECT COALESCE(MAX(e.event_sequence),0)+1
                FROM dr_custody_instruction_events e WHERE e.reference_key=NEW.reference_key
            )
            BEGIN SELECT RAISE(ABORT,'invalid DR custody instruction event sequence'); END""")
        self.execute("DROP TRIGGER IF EXISTS trg_dr_custody_instruction_transition")
        self.execute("""CREATE TRIGGER trg_dr_custody_instruction_transition
            BEFORE UPDATE ON dr_custody_instructions
            WHEN NOT (
                (OLD.status='requested' AND NEW.status IN ('verified','rejected','cancelled')) OR
                (OLD.status='verified' AND NEW.status IN ('settled','rejected','cancelled'))
            ) OR NEW.reference_key<>OLD.reference_key OR NEW.dr_code<>OLD.dr_code OR
               NEW.user_id<>OLD.user_id OR NEW.movement_type<>OLD.movement_type OR
               NEW.dr_quantity<>OLD.dr_quantity OR NEW.underlying_quantity<>OLD.underlying_quantity OR
               NEW.requested_epoch<>OLD.requested_epoch OR NEW.updated_epoch<OLD.updated_epoch OR
               NEW.fee_bps<>OLD.fee_bps OR NEW.fee_currency IS NOT OLD.fee_currency OR
               NEW.fee_price IS NOT OLD.fee_price OR
               (NEW.status='settled' AND NEW.settled_epoch IS NULL) OR
               (NEW.status<>'settled' AND NEW.settled_epoch IS NOT NULL)
            BEGIN SELECT RAISE(ABORT,'invalid DR custody instruction transition'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_dr_custody_instructions_no_delete
            BEFORE DELETE ON dr_custody_instructions
            BEGIN SELECT RAISE(ABORT,'DR custody instructions are retained for audit'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_voting_delegations (
            delegation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dr_code TEXT NOT NULL REFERENCES depositary_receipts(dr_code),
            owner_user_id TEXT NOT NULL,
            proxy_user_id TEXT NOT NULL,
            dr_quantity INTEGER NOT NULL CHECK(dr_quantity > 0),
            status TEXT NOT NULL CHECK(status IN ('active','revoked')),
            created_epoch REAL NOT NULL,
            revoked_epoch REAL,
            CHECK(owner_user_id <> proxy_user_id)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_voting_delegations_active "
                     "ON dr_voting_delegations(dr_code,owner_user_id,status)")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_voting_delegation_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            delegation_id INTEGER NOT NULL REFERENCES dr_voting_delegations(delegation_id),
            event_type TEXT NOT NULL CHECK(event_type IN ('delegated','revoked')),
            actor_user_id TEXT NOT NULL,
            recorded_epoch REAL NOT NULL
        )""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_dr_voting_delegations_no_delete
            BEFORE DELETE ON dr_voting_delegations
            BEGIN SELECT RAISE(ABORT,'DR voting delegations are retained for audit'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_dr_voting_delegations_revoke_only
            BEFORE UPDATE ON dr_voting_delegations
            WHEN NOT (
                OLD.status='active' AND NEW.status='revoked' AND OLD.revoked_epoch IS NULL
                AND NEW.revoked_epoch IS NOT NULL
                AND OLD.delegation_id=NEW.delegation_id AND OLD.dr_code=NEW.dr_code
                AND OLD.owner_user_id=NEW.owner_user_id AND OLD.proxy_user_id=NEW.proxy_user_id
                AND OLD.dr_quantity=NEW.dr_quantity AND OLD.created_epoch=NEW.created_epoch
                AND EXISTS (SELECT 1 FROM dr_voting_delegation_events e
                            WHERE e.delegation_id=OLD.delegation_id
                            AND e.event_type='revoked' AND e.actor_user_id=OLD.owner_user_id
                            AND e.recorded_epoch=NEW.revoked_epoch)
            )
            BEGIN SELECT RAISE(ABORT,'DR voting delegation changes require an audit event'); END""")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_dr_voting_delegation_events_immutable_{operation.lower()}
                BEFORE {operation} ON dr_voting_delegation_events
                BEGIN SELECT RAISE(ABORT,'DR voting delegation events are append-only'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_voting_meetings (
            meeting_id INTEGER PRIMARY KEY AUTOINCREMENT,
            underlying_code TEXT NOT NULL,
            title TEXT NOT NULL,
            record_epoch REAL NOT NULL,
            created_epoch REAL NOT NULL,
            voting_open_epoch REAL,
            voting_close_epoch REAL,
            quorum_percent REAL NOT NULL DEFAULT 0 CHECK(quorum_percent BETWEEN 0 AND 100),
            status TEXT NOT NULL DEFAULT 'recorded' CHECK(status='recorded')
        )""")
        meeting_columns = {row[1] for row in self.execute("PRAGMA table_info(dr_voting_meetings)")}
        for column, declaration in {
            "voting_open_epoch": "REAL",
            "voting_close_epoch": "REAL",
            "quorum_percent": "REAL NOT NULL DEFAULT 0 CHECK(quorum_percent BETWEEN 0 AND 100)",
        }.items():
            if column not in meeting_columns:
                self.execute(f"ALTER TABLE dr_voting_meetings ADD COLUMN {column} {declaration}")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_dr_voting_meeting_schedule_once
            BEFORE UPDATE OF voting_open_epoch,voting_close_epoch,quorum_percent ON dr_voting_meetings
            WHEN OLD.voting_open_epoch IS NOT NULL OR NEW.voting_open_epoch IS NULL
              OR NEW.voting_close_epoch IS NULL
            BEGIN SELECT RAISE(ABORT,'DR voting schedule is immutable once configured'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_dr_voting_meetings_no_delete
            BEFORE DELETE ON dr_voting_meetings
            BEGIN SELECT RAISE(ABORT,'DR voting meetings are retained for audit'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_voting_entitlement_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL REFERENCES dr_voting_meetings(meeting_id),
            holder_user_id TEXT NOT NULL,
            voter_user_id TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK(source_type IN ('underlying','dr')),
            source_code TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            recorded_epoch REAL NOT NULL,
            UNIQUE(meeting_id,holder_user_id,voter_user_id,source_type,source_code)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_voting_snapshot_voter "
                     "ON dr_voting_entitlement_snapshots(meeting_id,voter_user_id)")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_dr_voting_snapshots_immutable_{operation.lower()}
                BEFORE {operation} ON dr_voting_entitlement_snapshots
                BEGIN SELECT RAISE(ABORT,'DR voting entitlement snapshots are immutable'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_voting_agenda_items (
            agenda_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL REFERENCES dr_voting_meetings(meeting_id),
            title TEXT NOT NULL,
            choices_json TEXT NOT NULL,
            created_epoch REAL NOT NULL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_voting_agenda_meeting "
                     "ON dr_voting_agenda_items(meeting_id,agenda_item_id)")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_dr_voting_agenda_immutable_{operation.lower()}
                BEFORE {operation} ON dr_voting_agenda_items
                BEGIN SELECT RAISE(ABORT,'DR voting agenda items are immutable'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_voting_ballots (
            ballot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL REFERENCES dr_voting_meetings(meeting_id),
            agenda_item_id INTEGER NOT NULL REFERENCES dr_voting_agenda_items(agenda_item_id),
            voter_user_id TEXT NOT NULL,
            choice TEXT NOT NULL,
            voting_power INTEGER NOT NULL CHECK(voting_power > 0),
            cast_epoch REAL NOT NULL,
            UNIQUE(agenda_item_id,voter_user_id)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_voting_ballots_item "
                     "ON dr_voting_ballots(agenda_item_id,choice)")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_dr_voting_ballots_immutable_{operation.lower()}
                BEFORE {operation} ON dr_voting_ballots
                BEGIN SELECT RAISE(ABORT,'DR voting ballots are immutable'); END""")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_deviation_samples (
            sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dr_code TEXT NOT NULL REFERENCES depositary_receipts(dr_code),
            sample_bucket INTEGER NOT NULL,
            sampled_epoch REAL NOT NULL,
            dr_price REAL,
            reference_value REAL,
            deviation_percent REAL,
            state TEXT NOT NULL CHECK(state IN ('normal','premium','discount','unavailable')),
            UNIQUE(dr_code,sample_bucket)
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_deviation_samples_recent "
                     "ON dr_deviation_samples(dr_code,sampled_epoch DESC)")
        self.execute("""CREATE TABLE IF NOT EXISTS dr_deviation_alert_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dr_code TEXT NOT NULL REFERENCES depositary_receipts(dr_code),
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL CHECK(to_state IN ('premium','discount','normal','unavailable')),
            dr_price REAL,
            reference_value REAL,
            deviation_percent REAL,
            recorded_epoch REAL NOT NULL
        )""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_dr_deviation_alerts_recent "
                     "ON dr_deviation_alert_events(recorded_epoch DESC,event_id DESC)")
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_dr_deviation_alert_immutable_{operation.lower()}
                BEFORE {operation} ON dr_deviation_alert_events
                BEGIN SELECT RAISE(ABORT,'DR deviation alert events are append-only'); END""")
        self.enforce_integer_money(
            "depositary_receipt_movements", ("underlying_average_cost",), "movement_id"
        )
        for operation in ("UPDATE", "DELETE"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_dr_movements_immutable_{operation.lower()}
                BEFORE {operation} ON depositary_receipt_movements
                BEGIN SELECT RAISE(ABORT,'DR custody movements are append-only'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_dr_requires_program_insert
            BEFORE INSERT ON market_assets WHEN NEW.type='depositary_receipt'
            AND NOT EXISTS (SELECT 1 FROM depositary_receipts WHERE dr_code=NEW.code)
            BEGIN SELECT RAISE(ABORT,'DR asset requires a depositary receipt program'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_dr_requires_program_update
            BEFORE UPDATE OF type ON market_assets WHEN NEW.type='depositary_receipt'
            AND NOT EXISTS (SELECT 1 FROM depositary_receipts WHERE dr_code=NEW.code)
            BEGIN SELECT RAISE(ABORT,'DR asset requires a depositary receipt program'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_dr_program_no_delete
            BEFORE DELETE ON depositary_receipts
            BEGIN SELECT RAISE(ABORT,'DR programs are retained; suspend instead'); END""")
        for field in ("underlying_code", "receipt_type", "depositary_bank", "custodian",
                      "home_exchange_id", "listing_exchange_id", "quote_currency",
                      "underlying_per_receipt", "receipt_supply", "deposited_underlying_quantity"):
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_dr_program_immutable_{field}
                BEFORE UPDATE OF {field} ON depositary_receipts
                BEGIN SELECT RAISE(ABORT,'DR issuance terms are immutable'); END""")
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
                quantity REAL NOT NULL CHECK(quantity > 0),
                price REAL NOT NULL CHECK(price > 0),
                recorded_epoch INTEGER NOT NULL
            )"""
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_trade_history_code_time "
            "ON market_trade_history(asset_code, recorded_epoch)"
        )
        # Keep the latest execution quote independently from disposable chart
        # history.  This is also updated for direct SQL imports.
        self.execute(
            """CREATE TABLE IF NOT EXISTS market_last_trade (
                asset_code TEXT PRIMARY KEY,
                price REAL NOT NULL CHECK(price > 0),
                recorded_epoch INTEGER NOT NULL,
                trade_id INTEGER NOT NULL
            )"""
        )
        self.execute(
            """INSERT INTO market_last_trade(asset_code,price,recorded_epoch,trade_id)
               SELECT h.asset_code,h.price,h.recorded_epoch,h.trade_id
               FROM market_trade_history h
               WHERE NOT EXISTS (
                   SELECT 1 FROM market_trade_history newer
                   WHERE newer.asset_code=h.asset_code AND (
                       newer.recorded_epoch>h.recorded_epoch OR
                       (newer.recorded_epoch=h.recorded_epoch AND newer.trade_id>h.trade_id)))
               ON CONFLICT(asset_code) DO UPDATE SET
                   price=excluded.price, recorded_epoch=excluded.recorded_epoch,
                   trade_id=excluded.trade_id
               WHERE excluded.recorded_epoch>market_last_trade.recorded_epoch OR
                     (excluded.recorded_epoch=market_last_trade.recorded_epoch AND
                      excluded.trade_id>=market_last_trade.trade_id)"""
        )
        for operation, suffix in (("INSERT", "insert"), ("UPDATE OF price", "update")):
            self.execute(
                f"""CREATE TRIGGER IF NOT EXISTS trg_market_last_trade_{suffix}
                    AFTER {operation} ON market_trade_history BEGIN
                    INSERT INTO market_last_trade(asset_code,price,recorded_epoch,trade_id)
                    VALUES (NEW.asset_code,NEW.price,NEW.recorded_epoch,NEW.trade_id)
                    ON CONFLICT(asset_code) DO UPDATE SET
                        price=excluded.price, recorded_epoch=excluded.recorded_epoch,
                        trade_id=excluded.trade_id
                    WHERE excluded.recorded_epoch>market_last_trade.recorded_epoch OR
                          (excluded.recorded_epoch=market_last_trade.recorded_epoch AND
                           excluded.trade_id>=market_last_trade.trade_id);
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
        # Exchange-wide state (index and trading session) is owned by
        # SystemDB's market_exchanges table.  Constituents remain here because
        # an exchange has many component stocks.
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
        if "activation_epoch" not in order_columns:
            self.execute("ALTER TABLE market_orders ADD COLUMN activation_epoch INTEGER")
        if "scheduled_reason" not in order_columns:
            self.execute("ALTER TABLE market_orders ADD COLUMN scheduled_reason TEXT")
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
            "CREATE INDEX IF NOT EXISTS idx_market_orders_user_updated "
            "ON market_orders(user_id, updated_epoch DESC)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_orders_instrument "
            "ON market_orders(order_kind, asset_code, side, status, limit_price, created_epoch, order_id)"
        )
        self.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_orders_scheduled "
            "ON market_orders(status, created_epoch, order_id)"
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
        places = "COALESCE((SELECT c.decimal_places FROM currencies c WHERE c.code=COALESCE("
        places += "(SELECT quote_currency FROM market_assets WHERE code=h.asset_code),"
        places += "(SELECT quote_currency FROM company WHERE company_code=h.asset_code),"
        places += "(SELECT quote_currency FROM investment WHERE product_code=h.asset_code),"
        places += "(SELECT quote_currency FROM treasury_bond_products WHERE product_code=h.asset_code),"
        places += "'NWX')),0)"
        return self.execute(
            "SELECT h.recorded_epoch,h.side,h.quantity,"
            f"CAST(h.price_minor_units AS REAL)/CASE ({places}) "
            "WHEN 0 THEN 1 WHEN 1 THEN 10 WHEN 2 THEN 100 ELSE 1 END price,"
            "h.price_minor_units FROM market_trade_history h "
            "WHERE h.asset_code = ? AND h.recorded_epoch >= ? ORDER BY h.recorded_epoch,h.trade_id",
            (str(code), int(since_epoch)),
        )

    def get_last_trade(self, code):
        from functions.money import currency_precision, from_minor_units

        row = self.execute_one(
            "SELECT t.price_minor_units,t.recorded_epoch,t.trade_id,"
            "COALESCE((SELECT quote_currency FROM market_assets WHERE code=t.asset_code),"
            "(SELECT quote_currency FROM company WHERE company_code=t.asset_code),"
            "(SELECT quote_currency FROM investment WHERE product_code=t.asset_code),"
            "(SELECT quote_currency FROM treasury_bond_products WHERE product_code=t.asset_code),"
            "'NWX') currency_code "
            "FROM market_last_trade t WHERE t.asset_code=?", (str(code),),
        )
        if not row:
            return None
        places = currency_precision(self.conn, row["currency_code"])
        return {"price": float(from_minor_units(row["price_minor_units"], places)),
                "price_minor_units": int(row["price_minor_units"]),
                "recorded_epoch": row["recorded_epoch"], "trade_id": row["trade_id"]}

    def update_price(self, code, new_price):
        self.execute("UPDATE market_assets SET price = ? WHERE code = ?", (new_price, code))

    def update_prices_bulk(self, updates):
        if not updates:
            return
        with self.lock:
            with self.conn:
                self.conn.executemany("UPDATE market_assets SET price = ? WHERE code = ?", updates)
