import fcntl
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

from .user_db import UserDB
from .asset_db import AssetDB
from .product_db import ProductDB
from .company_db import CompanyDB
from .account_db import AccountDB
from .pot_db import PotDB
from .tax_db import TaxDB
from .system_db import SystemDB
from .db_manager import DBManager
from .currency_db import CurrencyDB
from .lottery_db import LotteryDB
from .corporate_db import CorporateDB
from .els_db import ELSDB
from .financial_db import FinancialDB
from .credit_db import CreditDB
from .organization_db import OrganizationDB


_schema_lock = threading.RLock()


@contextmanager
def _schema_initialization_lock():
    root = Path(DBManager.database_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with _schema_lock, (root / ".inazina-schema-init.lock").open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class DatabaseManager:
    """모든 데이터베이스 모듈을 통합 관리하는 클래스"""
    atomic = staticmethod(DBManager.atomic)
    readonly = staticmethod(DBManager.readonly)
    code_locations = staticmethod(DBManager.code_locations)

    def __init__(self):
        from . import pg_backend
        if pg_backend.enabled():
            self._attach_postgres()
            return
        from . import startup_state
        with _schema_initialization_lock():
            with closing(sqlite3.connect(Path(DBManager.database_dir).resolve() / "inazina.db")) as probe:
                last_pre_hash = startup_state.recorded(probe)
            self.users = UserDB()
            self.assets = AssetDB()
            self.products = ProductDB()
            self.companies = CompanyDB()
            self.accounts = AccountDB()
            self.pot = PotDB()
            self.tax = TaxDB()
            self.system = SystemDB()
            with self.users.lock:
                pre_hash = startup_state.schema_hash(self.users.conn)
            CurrencyDB.skip_precision_migration = pre_hash == last_pre_hash
            try:
                self.currency = CurrencyDB()
            finally:
                CurrencyDB.skip_precision_migration = False
            self.lottery = LotteryDB()
            self.corporate = CorporateDB()
            self.els = ELSDB()
            self.financial = FinancialDB()
            self.credit = CreditDB()
            self.organizations = OrganizationDB()
            from .account_kind_rules import install as install_account_kind_rules
            install_account_kind_rules(self.accounts)
            self.companies.backfill_nationality_from_exchange()
            self.currency.register_late_amount_fields()
            from .nation_accounts import merge_legacy_nation_users
            merge_legacy_nation_users(self)
            from .numeric_guards import install as install_numeric_guards
            install_numeric_guards(self.users)
            # Refresh planner statistics opportunistically after schema/index
            # initialization. SQLite decides internally when analysis is needed.
            self.users.execute("PRAGMA optimize")
            with self.users.lock, self.users.conn:
                startup_state.record(self.users.conn, pre_hash)
        for database in self._databases():
            database.pooled_reads = True

    def base_currency_code(self):
        return self.currency.base_currency_code()

    @staticmethod
    def _sync_postgres_schema():
        """Add tables/columns/indexes/triggers the code defines and the live PostgreSQL lacks (see database/pg_schema_sync.py).
        Off under pytest (tests build their own schema) and with INAZINA_PG_SCHEMA_SYNC=0; a failure is reported, not fatal."""
        import os
        import sys
        if os.environ.get("INAZINA_PG_SCHEMA_SYNC") == "0" or ("pytest" in sys.modules and os.environ.get("INAZINA_PG_SCHEMA_SYNC") != "1"):
            return
        try:
            from . import pg_schema_sync
            summary = pg_schema_sync.ensure_current()
        except Exception as exc:
            print(f"[PostgreSQL schema sync] {type(exc).__name__}: {exc}", flush=True)
            return
        if summary and any(summary.values()):
            print("[PostgreSQL schema sync] " + ", ".join(f"{kind} {len(items)}" for kind, items in summary.items() if items), flush=True)
            for error in summary["errors"][:10]:
                print(f"[PostgreSQL schema sync] could not apply {error}", flush=True)

    def _attach_postgres(self):
        """Connect every database module without running the SQLite schema setup:
        PostgreSQL gets its schema from database/postgres/*.sql."""
        self._sync_postgres_schema()
        for attribute, cls, name in (
                ("users", UserDB, "users"), ("assets", AssetDB, "assets"), ("products", ProductDB, "product"),
                ("companies", CompanyDB, "company"), ("accounts", AccountDB, "accounts"), ("pot", PotDB, "pot"),
                ("tax", TaxDB, "tax"), ("system", SystemDB, "system"), ("currency", CurrencyDB, "forex"),
                ("lottery", LotteryDB, "lottery"), ("corporate", CorporateDB, "corporate"),
                ("els", ELSDB, "els"), ("financial", FinancialDB, "financial"), ("credit", CreditDB, "credit"),
                ("organizations", OrganizationDB, "organizations")):
            database = cls.__new__(cls)
            DBManager.__init__(database, name)
            database.pooled_reads = True
            setattr(self, attribute, database)
        # The policy data upgrade is backend-independent; the SQLite schema
        # initializer normally performs it, but PostgreSQL skips that setup.
        self.credit.migrate_policy_defaults()

    def _databases(self):
        return (self.users, self.assets, self.products, self.companies,
                self.accounts, self.pot, self.tax, self.system, self.currency, self.lottery,
                self.corporate, self.els, self.financial, self.credit, self.organizations)

    def close(self):
        for database in self._databases():
            database.close()
