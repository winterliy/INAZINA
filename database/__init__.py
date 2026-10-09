from .user_db import UserDB
from .asset_db import AssetDB
from .product_db import ProductDB
from .company_db import CompanyDB
from .account_db import AccountDB
from .pot_db import PotDB
from .tax_db import TaxDB
from .system_db import SystemDB
from .db_manager import DBManager
from .fx_db import ForeignExchangeDB
from .lottery_db import LotteryDB
from .corporate_db import CorporateDB
from .els_db import ELSDB
from .financial_db import FinancialDB

class DatabaseManager:
    """모든 데이터베이스 모듈을 통합 관리하는 클래스"""
    atomic = staticmethod(DBManager.atomic)
    readonly = staticmethod(DBManager.readonly)
    code_locations = staticmethod(DBManager.code_locations)

    def __init__(self):
        self.users = UserDB()
        self.assets = AssetDB()
        self.products = ProductDB()
        self.companies = CompanyDB()
        self.accounts = AccountDB()
        self.pot = PotDB()
        self.tax = TaxDB()
        self.system = SystemDB()
        self.fx = ForeignExchangeDB()
        self.lottery = LotteryDB()
        self.corporate = CorporateDB()
        self.els = ELSDB()
        self.financial = FinancialDB()

    def close(self):
        for database in (self.users, self.assets, self.products, self.companies,
                         self.accounts, self.pot, self.tax, self.system, self.fx,self.lottery,self.corporate,self.els,self.financial):
            database.close()
