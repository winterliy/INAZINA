import fcntl
import threading
from contextlib import contextmanager
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
        with _schema_initialization_lock():
            self.users = UserDB()
            self.assets = AssetDB()
            self.products = ProductDB()
            self.companies = CompanyDB()
            self.accounts = AccountDB()
            self.pot = PotDB()
            self.tax = TaxDB()
            self.system = SystemDB()
            self.currency = CurrencyDB()
            self.lottery = LotteryDB()
            self.corporate = CorporateDB()
            self.els = ELSDB()

    def close(self):
        for database in (self.users, self.assets, self.products, self.companies,
                         self.accounts, self.pot, self.tax, self.system, self.currency,self.lottery,self.corporate,self.els):
            database.close()
