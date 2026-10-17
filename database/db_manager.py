import sqlite3
import threading
import time
import logging
from contextlib import contextmanager
from pathlib import Path
from .unified import UnifiedConnection, migrate

class DBManager:
    """SQLite 데이터베이스 연결 및 기본 설정을 관리하는 클래스"""
    database_dir = Path(__file__).resolve().parent
    _path_locks = {}
    _path_locks_guard = threading.Lock()
    _admin_context = threading.local()
    BUSY_TIMEOUT_MS = 2_000
    BEGIN_BUSY_TIMEOUT_MS = 200
    BEGIN_RETRY_DELAYS = (0.05, 0.10, 0.20, 0.40, 0.50)

    @staticmethod
    @contextmanager
    def admin_scope(actor_id):
        previous = getattr(DBManager._admin_context,'actor',None)
        DBManager._admin_context.actor = str(actor_id)
        try:
            yield
        finally:
            DBManager._admin_context.actor = previous

    @classmethod
    def _lock_for_path(cls, path):
        """Return one process-wide lock for every connection to the same SQLite file."""
        key = str(Path(path).resolve())
        with cls._path_locks_guard:
            lock = cls._path_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                cls._path_locks[key] = lock
            return lock

    def __init__(self, db_name):
        self.db_path = str(Path(self.database_dir).resolve() / "inazina.db")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # Different managers/threads can connect to the same file. SQLite serializes
        # writers, so coordinate those connections in-process before they contend.
        self.lock = self._lock_for_path(self.db_path)
        with self.lock:
            migrate(Path(self.database_dir).resolve())
            self.conn = self._get_connection()

    def _get_connection(self):
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.BUSY_TIMEOUT_MS / 1_000,
            check_same_thread=False,
            factory=UnifiedConnection,
        )
        conn.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS};")
        # WAL 모드 활성화: 동시성 향상 및 쓰기 성능 최적화
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        if str(mode).lower() != "wal":
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.row_factory = sqlite3.Row # 결과를 딕셔너리 형태로 반환
        return conn

    def execute(self, query, params=()):
        with self.lock:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(query, params)
                return cursor.fetchall()

    def execute_one(self, query, params=()):
        with self.lock:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute(query, params)
                return cursor.fetchone()

    def commit(self):
        with self.lock:
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    def enforce_integer_money(self, table, columns, primary_key):
        """Round legacy currency rows and keep SQL/browser edits whole-NWX only."""
        columns = tuple(columns)
        if not columns:
            return
        # These tables contain an explicit or derivable denomination. CurrencyDB
        # installs their per-row precision triggers after the currency schema is
        # available. Do not destroy decimal balances earlier in startup.
        currency_aware = {
            "users", "company", "market_assets", "user_assets",
            "market_price_history", "market_orders", "saving", "liability",
            "investment", "treasury_bond_products", "accounts", "loans",
            "loan_payment_schedule", "loan_collateral_assets",
            "collateral_liquidation_events", "deposit_insurance_claims",
            "treasury_bond_holdings", "issuer_cash_accounts",
            "preferred_dividend_claims", "dividend_distributions",
            "dividend_payments", "financial_institutions",
            "financial_institution_ledger", "depositary_receipt_movements",
            "tax_transactions",
        }
        if table in currency_aware:
            safe_table = "".join(ch if ch.isalnum() else "_" for ch in table)
            for suffix in ("insert", "update"):
                self.execute(
                    f'DROP TRIGGER IF EXISTS "trg_{safe_table}_integer_money_{suffix}"'
                )
            return
        quoted_table = f'"{table}"'
        quoted_columns = [f'"{column}"' for column in columns]
        assignments = ", ".join(
            f'{column} = ROUND(COALESCE(NEW.{column}, 0))'
            for column in quoted_columns
        )
        fractional = " OR ".join(
            f'(NEW.{column} IS NULL OR NEW.{column} <> ROUND(COALESCE(NEW.{column}, 0)))'
            for column in quoted_columns
        )
        for column in quoted_columns:
            self.execute(
                f"UPDATE {quoted_table} SET {column} = ROUND(COALESCE({column}, 0)) "
                f"WHERE {column} IS NULL OR {column} <> ROUND(COALESCE({column}, 0))"
            )
        safe_table = "".join(ch if ch.isalnum() else "_" for ch in table)
        self.execute(f'DROP TRIGGER IF EXISTS "trg_{safe_table}_integer_money_insert"')
        self.execute(f'DROP TRIGGER IF EXISTS "trg_{safe_table}_integer_money_update"')
        self.execute(
            f'CREATE TRIGGER "trg_{safe_table}_integer_money_insert" '
            f'AFTER INSERT ON {quoted_table} WHEN {fractional} BEGIN '
            f'UPDATE {quoted_table} SET {assignments} WHERE "{primary_key}" = NEW."{primary_key}"; END'
        )
        self.execute(
            f'CREATE TRIGGER "trg_{safe_table}_integer_money_update" '
            f'AFTER UPDATE OF {", ".join(quoted_columns)} ON {quoted_table} '
            f'WHEN {fractional} BEGIN '
            f'UPDATE {quoted_table} SET {assignments} WHERE "{primary_key}" = NEW."{primary_key}"; END'
        )

    def enforce_currency_money(self, table, columns, primary_key, currency_sql):
        """Round native amounts to each row's product/contract currency precision.

        ``currency_sql`` is a trusted schema expression containing ``NEW`` (for
        example ``NEW.quote_currency`` or a scalar subquery).  This replaces the
        legacy whole-NWX trigger while keeping NWX at zero decimals.
        """
        columns = tuple(columns)
        if not columns:
            return
        safe_table = "".join(ch if ch.isalnum() else "_" for ch in table)
        for suffix in ("insert", "update"):
            self.execute(f'DROP TRIGGER IF EXISTS "trg_{safe_table}_integer_money_{suffix}"')
            self.execute(f'DROP TRIGGER IF EXISTS "trg_{safe_table}_currency_money_{suffix}"')
        quoted_table = f'"{table}"'
        quoted_columns = [f'"{column}"' for column in columns]
        places = (
            "COALESCE((SELECT decimal_places FROM currencies WHERE code="
            f"({currency_sql})),0)"
        )
        def rounded(column):
            # SQLite's binary ROUND(1.005, 2) can produce 1.00.  A tiny signed
            # guard digit makes exact decimal half cases agree with the public
            # Decimal/ROUND_HALF_UP API without affecting supported 0~6 places.
            value = f"COALESCE(NEW.{column},0)"
            return (
                f"ROUND(({value})+CASE WHEN ({value})>=0 THEN 1e-12 ELSE -1e-12 END,"
                f"{places})"
            )
        assignments = ", ".join(
            f'{column}={rounded(column)}'
            for column in quoted_columns
        )
        changed = " OR ".join(
            f'(NEW.{column} IS NULL OR NEW.{column}<>{rounded(column)})'
            for column in quoted_columns
        )
        self.execute(
            f'CREATE TRIGGER "trg_{safe_table}_currency_money_insert" '
            f'AFTER INSERT ON {quoted_table} WHEN {changed} BEGIN '
            f'UPDATE {quoted_table} SET {assignments} WHERE "{primary_key}"=NEW."{primary_key}"; END'
        )
        self.execute(
            f'CREATE TRIGGER "trg_{safe_table}_currency_money_update" '
            f'AFTER UPDATE OF {", ".join(quoted_columns)} ON {quoted_table} WHEN {changed} BEGIN '
            f'UPDATE {quoted_table} SET {assignments} WHERE "{primary_key}"=NEW."{primary_key}"; END'
        )

    def initialize_dividend_activation(self, table, primary_key):
        """Browser edits to the dividend switch discard the old payment schedule."""
        self.execute(
            f'CREATE TRIGGER IF NOT EXISTS "trg_{table}_dividend_activation" '
            f'AFTER UPDATE OF dividend_is_active ON "{table}" '
            f'WHEN NEW.dividend_is_active <> OLD.dividend_is_active BEGIN '
            f'UPDATE "{table}" SET dividend_next_epoch=NULL '
            f'WHERE "{primary_key}"=NEW."{primary_key}"; END'
        )

    @staticmethod
    def code_locations(conn, code):
        """Return every table using a symbol/product/account number as its code."""
        checks = (
            ("assets.market_assets", "code", "시장 자산"),
            ("company.company", "company_code", "회사 주식"),
            ("product.saving", "product_code", "저축 상품"),
            ("product.liability", "product_code", "대출 상품"),
            ("product.investment", "product_code", "투자 상품"),
            ("product.treasury_bond_products", "product_code", "국채 상품"),
            ("assets.option_contracts", "contract_code", "옵션 계약"),
            ("accounts.accounts", "account_number", "계좌 번호"),
        )
        locations = []
        for table, column, label in checks:
            if conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (code,)).fetchone():
                locations.append(label)
        return locations

    @staticmethod
    @contextmanager
    def atomic(databases=("users", "accounts", "assets", "product", "company", "pot", "tax")):
        """inazina.db의 모든 테이블을 한 연결에서 다루는 원자적 작업 컨텍스트.

        거래 중간에 프로세스가 실패하면 변경 사항을 롤백합니다. 각 연결도
        IMMEDIATE 트랜잭션으로 시작해 동시 명령이 잔액/수량을 이중 사용하지
        못하게 합니다. databases 인자는 이전 호출부와의 호환용 분류명이며
        모든 분류는 같은 파일과 잠금을 사용합니다.
        """
        root = Path(DBManager.database_dir).resolve()
        database_paths = {}
        locks = []
        acquired_locks = []
        conn = None
        try:
            for name in databases:
                if not name.replace("_", "").isalnum():
                    raise ValueError(f"Invalid database alias: {name}")
                database_paths[name] = (root / "inazina.db").resolve()

            # Acquire file locks in a stable order so overlapping transactions from
            # the interest/loan workers and slash-command threads cannot deadlock or
            # race each other through separate SQLite connections.
            locks = [
                DBManager._lock_for_path(path)
                for path in sorted(set(database_paths.values()), key=str)
            ]
            lock_started = time.perf_counter()
            for lock in locks:
                lock.acquire()
                acquired_locks.append(lock)
            lock_wait_ms = (time.perf_counter() - lock_started) * 1000
            if lock_wait_ms >= 100:
                logging.getLogger("inazina.performance").warning(
                    "sqlite_process_lock_wait %.1f ms", lock_wait_ms
                )

            # Fail well before the HTTP proxy's 10-second timeout.  A short,
            # bounded retry handles ordinary cross-process writer hand-offs
            # without leaving abandoned requests blocked for 30 seconds.
            conn = sqlite3.connect(
                root / "inazina.db",
                timeout=DBManager.BEGIN_BUSY_TIMEOUT_MS / 1_000,
                isolation_level=None,
                factory=UnifiedConnection,
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={DBManager.BEGIN_BUSY_TIMEOUT_MS}")
            begin_started = time.perf_counter()
            for attempt in range(len(DBManager.BEGIN_RETRY_DELAYS) + 1):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    locked = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
                    if not locked or attempt == len(DBManager.BEGIN_RETRY_DELAYS):
                        raise
                    time.sleep(DBManager.BEGIN_RETRY_DELAYS[attempt])
            begin_wait_ms = (time.perf_counter() - begin_started) * 1000
            if begin_wait_ms >= 100:
                logging.getLogger("inazina.performance").warning(
                    "sqlite_writer_wait %.1f ms attempts=%d", begin_wait_ms, attempt + 1
                )
            conn.execute(f"PRAGMA busy_timeout={DBManager.BUSY_TIMEOUT_MS}")
            actor = getattr(DBManager._admin_context,'actor',None)
            if actor is not None:
                role = conn.execute('SELECT role FROM users WHERE user_id=?',(actor,)).fetchone()
                if not role or str(role['role']).strip().lower()!='admin':
                    raise ValueError('관리 권한이 취소되어 작업을 중단했습니다.')
            yield conn
            conn.commit()
        except Exception:
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            try:
                if conn is not None:
                    conn.close()
            finally:
                for lock in reversed(acquired_locks):
                    lock.release()

    @staticmethod
    @contextmanager
    def readonly():
        """Open a short-lived, query-only SQLite snapshot.

        Read paths must not use :meth:`atomic`: ``BEGIN IMMEDIATE`` reserves
        SQLite's single writer and can make an otherwise harmless dashboard
        refresh block background settlements.  A separate read-only connection
        works concurrently with writers in WAL mode and rejects accidental
        mutations.
        """
        root = Path(DBManager.database_dir).resolve()
        path = (root / "inazina.db").resolve()
        conn = None
        try:
            conn = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, timeout=5,
                isolation_level=None, factory=UnifiedConnection,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            yield conn
        finally:
            if conn is not None:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                finally:
                    conn.close()
