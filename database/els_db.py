"""Persistent terms, price observations and fully funded game ELS contracts."""

from .db_manager import DBManager


class ELSDB(DBManager):
    def __init__(self):
        super().__init__("els")
        self._init_db()

    def _init_db(self):
        self.execute("""CREATE TABLE IF NOT EXISTS els_products (
            product_code TEXT PRIMARY KEY, issuer_code TEXT NOT NULL,
            face_value INTEGER NOT NULL CHECK(face_value > 0),
            annual_coupon REAL NOT NULL CHECK(annual_coupon >= 0 AND annual_coupon <= 100),
            maturity_days INTEGER NOT NULL CHECK(maturity_days > 0),
            maturity_level REAL NOT NULL CHECK(maturity_level > 0 AND maturity_level <= 1),
            knock_in_level REAL NOT NULL CHECK(knock_in_level > 0 AND knock_in_level <= 1),
            subscription_end_epoch INTEGER NOT NULL, created_epoch INTEGER NOT NULL,
            issue_epoch INTEGER, maturity_epoch INTEGER,
            state TEXT NOT NULL DEFAULT 'subscription' CHECK(state IN
                ('subscription','active','observation_pending','redemption_pending','redeemed','cancelled')),
            knocked_in INTEGER NOT NULL DEFAULT 0 CHECK(knocked_in IN (0,1)),
            final_ratio REAL, final_sequence INTEGER, settlement_kind TEXT,
            settled_epoch INTEGER,
            principal_amount INTEGER NOT NULL DEFAULT 0 CHECK(principal_amount >= 0),
            coupon_amount INTEGER NOT NULL DEFAULT 0 CHECK(coupon_amount >= 0),
            locked_amount INTEGER NOT NULL DEFAULT 0 CHECK(locked_amount >= 0))""")
        self.execute("""CREATE TABLE IF NOT EXISTS els_underlyings (
            product_code TEXT NOT NULL, underlying_code TEXT NOT NULL,
            underlying_kind TEXT NOT NULL CHECK(underlying_kind IN ('stock','index')),
            initial_price REAL CHECK(initial_price > 0), last_price REAL CHECK(last_price > 0),
            last_epoch INTEGER, min_ratio REAL,
            knocked_in INTEGER NOT NULL DEFAULT 0 CHECK(knocked_in IN (0,1)),
            knock_in_epoch INTEGER, PRIMARY KEY(product_code, underlying_code))""")
        self.execute("""CREATE TABLE IF NOT EXISTS els_observations (
            product_code TEXT NOT NULL, sequence INTEGER NOT NULL,
            observation_day INTEGER NOT NULL CHECK(observation_day > 0),
            redemption_level REAL NOT NULL CHECK(redemption_level > 0 AND redemption_level <= 1),
            is_maturity INTEGER NOT NULL DEFAULT 0 CHECK(is_maturity IN (0,1)),
            scheduled_epoch INTEGER, observed_epoch INTEGER,
            prices_json TEXT, assessment_ratio REAL,
            outcome TEXT NOT NULL DEFAULT 'pending'
                CHECK(outcome IN ('pending','skipped','continue','redeem','loss')),
            PRIMARY KEY(product_code, sequence))""")
        self.execute("""CREATE TABLE IF NOT EXISTS els_subscriptions (
            subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_code TEXT NOT NULL, user_id TEXT NOT NULL, account_number TEXT,
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            principal INTEGER NOT NULL CHECK(principal > 0),
            coupon_reserved INTEGER NOT NULL CHECK(coupon_reserved >= 0),
            created_epoch INTEGER NOT NULL, confirmed_epoch INTEGER,
            status TEXT NOT NULL DEFAULT 'subscribed'
                CHECK(status IN ('subscribed','issued','settled','cancelled')))""")
        self._migrate_legacy_products()
        self.execute("CREATE INDEX IF NOT EXISTS idx_els_products_state ON els_products(state, subscription_end_epoch)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_els_subscriptions_user ON els_subscriptions(user_id, product_code, status)")
        self.execute("CREATE INDEX IF NOT EXISTS idx_els_underlying_code ON els_underlyings(underlying_code, product_code)")
        # A browser edit must not silently change the contract already paid for.
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_els_products_immutable
            BEFORE UPDATE OF issuer_code, face_value, annual_coupon, maturity_days,
                maturity_level, knock_in_level, subscription_end_epoch ON els_products
            WHEN EXISTS(SELECT 1 FROM els_subscriptions s WHERE s.product_code=OLD.product_code)
            BEGIN SELECT RAISE(ABORT, '청약 이후 ELS 발행조건을 변경할 수 없습니다.'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_els_observation_terms_immutable
            BEFORE UPDATE OF observation_day, redemption_level, is_maturity ON els_observations
            WHEN EXISTS(SELECT 1 FROM els_subscriptions s WHERE s.product_code=OLD.product_code)
            BEGIN SELECT RAISE(ABORT, '청약 이후 ELS 평가조건을 변경할 수 없습니다.'); END""")
        for operation in ("INSERT", "DELETE"):
            prefix = "NEW" if operation == "INSERT" else "OLD"
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_els_underlying_{operation.lower()}_immutable
                BEFORE {operation} ON els_underlyings
                WHEN EXISTS(SELECT 1 FROM els_subscriptions s WHERE s.product_code={prefix}.product_code)
                BEGIN SELECT RAISE(ABORT, '청약 이후 ELS 기초자산을 변경할 수 없습니다.'); END""")
            self.execute(f"""CREATE TRIGGER IF NOT EXISTS trg_els_observation_{operation.lower()}_immutable
                BEFORE {operation} ON els_observations
                WHEN EXISTS(SELECT 1 FROM els_subscriptions s WHERE s.product_code={prefix}.product_code)
                BEGIN SELECT RAISE(ABORT, '청약 이후 ELS 평가회차를 변경할 수 없습니다.'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_els_underlying_terms_immutable
            BEFORE UPDATE OF underlying_code, underlying_kind, product_code ON els_underlyings
            WHEN EXISTS(SELECT 1 FROM els_subscriptions s WHERE s.product_code=OLD.product_code)
            BEGIN SELECT RAISE(ABORT, '청약 이후 ELS 기초자산을 변경할 수 없습니다.'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_els_initial_price_immutable
            BEFORE UPDATE OF initial_price ON els_underlyings
            WHEN OLD.initial_price IS NOT NULL AND NEW.initial_price IS NOT OLD.initial_price
            BEGIN SELECT RAISE(ABORT, '발행 이후 ELS 최초기준가를 변경할 수 없습니다.'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_els_investment_terms_immutable
            BEFORE UPDATE OF face_value,annual_rate,maturity_days,total_supply,issuer_code,quote_currency ON investment
            WHEN OLD.product_type='els' AND EXISTS(SELECT 1 FROM els_subscriptions s WHERE s.product_code=OLD.product_code)
                AND (NEW.face_value IS NOT OLD.face_value OR NEW.annual_rate IS NOT OLD.annual_rate
                    OR NEW.maturity_days IS NOT OLD.maturity_days OR NEW.total_supply IS NOT OLD.total_supply
                    OR NEW.issuer_code IS NOT OLD.issuer_code OR NEW.quote_currency IS NOT OLD.quote_currency)
            BEGIN SELECT RAISE(ABORT, '청약 이후 ELS 발행조건을 변경할 수 없습니다.'); END""")
        self.execute("""CREATE TRIGGER IF NOT EXISTS trg_els_listing_immutable
            BEFORE UPDATE OF product_type,unit_price,is_fixed,product_code ON investment
            WHEN OLD.product_type='els' AND EXISTS(SELECT 1 FROM els_subscriptions s WHERE s.product_code=OLD.product_code)
                AND (NEW.product_type IS NOT OLD.product_type OR NEW.unit_price IS NOT OLD.unit_price
                    OR NEW.is_fixed IS NOT OLD.is_fixed OR NEW.product_code IS NOT OLD.product_code)
            BEGIN SELECT RAISE(ABORT, '청약 이후 ELS 코드·유형·액면가격을 변경할 수 없습니다.'); END""")

    def _migrate_legacy_products(self):
        """Merge the former one-to-one terms and reserve rows without data loss."""
        terms = self.execute_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='els_terms'"
        )
        reserves = self.execute_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='els_reserves'"
        )
        if not terms and not reserves:
            return
        if not terms:
            raise RuntimeError('els_reserves를 연결할 els_terms가 없습니다.')
        if reserves:
            mismatch = self.execute_one("""SELECT 1 FROM els_reserves r
                LEFT JOIN els_terms t USING(product_code)
                WHERE t.product_code IS NULL OR t.issuer_code<>r.issuer_code LIMIT 1""")
            if mismatch:
                raise RuntimeError('ELS 조건과 준비금의 상품 코드 또는 발행자가 일치하지 않습니다.')
        with self.lock, self.conn:
            reserve_join = ("LEFT JOIN els_reserves r USING(product_code)" if reserves else
                            "LEFT JOIN (SELECT NULL product_code,NULL principal_amount,"
                            "NULL coupon_amount,NULL locked_amount) r USING(product_code)")
            self.conn.execute(f"""INSERT OR REPLACE INTO els_products(
                product_code,issuer_code,face_value,annual_coupon,maturity_days,maturity_level,
                knock_in_level,subscription_end_epoch,created_epoch,issue_epoch,maturity_epoch,
                state,knocked_in,final_ratio,final_sequence,settlement_kind,settled_epoch,
                principal_amount,coupon_amount,locked_amount)
                SELECT t.product_code,t.issuer_code,t.face_value,t.annual_coupon,t.maturity_days,
                    t.maturity_level,t.knock_in_level,t.subscription_end_epoch,t.created_epoch,
                    t.issue_epoch,t.maturity_epoch,t.state,t.knocked_in,t.final_ratio,
                    t.final_sequence,t.settlement_kind,t.settled_epoch,
                    COALESCE(r.principal_amount,0),COALESCE(r.coupon_amount,0),
                    COALESCE(r.locked_amount,0)
                FROM els_terms t {reserve_join}""")
            self.conn.execute("DROP TABLE IF EXISTS els_reserves")
            self.conn.execute("DROP TABLE els_terms")
