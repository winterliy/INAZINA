"""Idempotent game listings, each backed by its actual product/contract schema."""
import time
from database.user_db import TREASURY_USER_ID

SECTORS = ('반도체','바이오','에너지','자동차','항공','물류','금융','소프트웨어','통신','식품')
BRANDS = ('오로라','루미나','아스트라','베리타','노바')
COMMODITIES = ('금','은','백금','팔라듐','구리','알루미늄','니켈','아연','납','주석',
               '철광석','코발트','리튬','망간','몰리브덴','텅스텐','크롬','티타늄','흑연','바나듐',
               '원유','천연가스','석탄','난방유','휘발유','에탄올','프로판','부탄','우라늄','전력',
               '밀','옥수수','쌀','대두','귀리','보리','설탕','커피','코코아','면화',
               '고무','목재','팜유','올리브유','오렌지','감자','소고기','돼지고기','우유','양모')

class CatalogManager:
    VERSION = 'inazina_catalog_v1'
    CATALOG_SIZE = 25
    FOREIGN_PER_CURRENCY = 5
    def __init__(self, db):
        self.db = db

    def seed_preferred(self):
        """Add one voting-free, monthly-dividend preferred listing per base company."""
        now, version = int(time.time()), 'inazina_preferred_catalog_v1'
        with self.db.atomic() as conn:
            if conn.execute('SELECT 1 FROM settings WHERE key=?', (version,)).fetchone():
                return False
            parents = []
            for index in range(1, self.CATALOG_SIZE + 1):
                code, parent_code = f'INZPR{index:03d}', f'INZST{index:03d}'
                if self.db.code_locations(conn, code):
                    raise ValueError(f'우선주 기본 종목 코드 충돌: {code}')
                parent = conn.execute("SELECT * FROM company WHERE company_code=? AND share_class='common' AND status='active'", (parent_code,)).fetchone()
                if not parent:
                    raise ValueError(f'우선주 발행 전에 활성 보통주가 필요합니다: {parent_code}')
                parents.append((code, parent))
            for code, parent in parents:
                price = max(1, round(int(parent['share_price']) * .9))
                conn.execute('INSERT INTO company(company_code,name,industry,capital,total_shares,share_price,max_shareholder_id,exchange_id,quote_currency,share_class,parent_company_code,dividend_cycle_seconds,dividend_per_share,dividend_next_epoch) VALUES (?,?,?,?,?,?,?,?,?,\'preferred\',?,?,1,?)',
                             (code, parent['name'] + ' 우선주', parent['industry'], 0, 100000, price,
                              TREASURY_USER_ID, parent['exchange_id'], parent['quote_currency'], parent['company_code'], 30 * 86400, now + 30 * 86400))
            conn.execute('INSERT INTO settings(key,value) VALUES (?,\'1\')', (version,))
            return True

    def seed_foreign(self):
        import uuid
        currencies=(('USD','달러',100),('EUR','유로',90),('JPY','엔',15000),('GBP','파운드',80),('KRW','원',150000))
        with self.db.atomic() as conn:
            if conn.execute("SELECT 1 FROM settings WHERE key='inazina_foreign_catalog_v1'").fetchone():
                return False
            for currency,_,_ in currencies:
                if not conn.execute("SELECT 1 FROM currencies WHERE code=? AND status='active'",(currency,)).fetchone():
                    raise ValueError('기본 외환 상품을 먼저 등록해주세요.')
                product=conn.execute("SELECT product_code FROM saving WHERE quote_currency=? AND product_kind='currency_saving' AND status='active' ORDER BY product_code LIMIT 1",(currency,)).fetchone()
                if not product:
                    raise ValueError('외화 발행 대금을 받을 외환통장 상품이 필요합니다.')
                if not conn.execute("SELECT 1 FROM accounts WHERE user_id=? AND quote_currency=? AND account_type='currency_saving'",(TREASURY_USER_ID,currency)).fetchone():
                    conn.execute("INSERT INTO accounts(account_number,user_id,product_code,balance,password,last_payment_epoch,principal,account_type,quote_currency) VALUES (?,?,?,?,?,?,?,?,?)",('FXA'+uuid.uuid4().hex[:20].upper(),TREASURY_USER_ID,product[0],0,None,time.time(),0,'currency_saving',currency))
                for i in range(1, self.FOREIGN_PER_CURRENCY + 1):
                    stock,spot=f'INZ{currency}ST{i:03d}',f'INZ{currency}CM{i:03d}'
                    for code in (stock,spot):
                        if self.db.code_locations(conn,code):
                            raise ValueError(f'외화 기본 종목 코드 충돌: {code}')
            for currency,label,base in currencies:
                for i in range(1, self.FOREIGN_PER_CURRENCY + 1):
                    price=base+base*i//10
                    conn.execute('INSERT INTO company(company_code,name,industry,capital,total_shares,share_price,max_shareholder_id,exchange_id,quote_currency) VALUES (?,?,?,?,?,?,?,?,?)',
                        (f'INZ{currency}ST{i:03d}',f'{label} 결제 {SECTORS[i-1]} 주식',SECTORS[i-1],price*1000000,1000000,price,TREASURY_USER_ID,f'INZ_{currency}',currency))
                    conn.execute("INSERT INTO investment(product_code,name,product_type,total_supply,unit_price,maturity_days,manager,exchange_id,quote_currency) VALUES (?,?,'commodity_spot',1000000,?,0,'Inazina',?,?)",
                        (f'INZ{currency}CM{i:03d}',f'{label} 결제 {COMMODITIES[i-1]} 현물',price,f'INZ_{currency}',currency))
            conn.execute("INSERT INTO settings(key,value) VALUES ('inazina_foreign_catalog_v1','1')")
            return True

    def seed(self):
        now = int(time.time())
        with self.db.atomic(('users','assets','company','product','system')) as conn:
            if conn.execute("SELECT 1 FROM settings WHERE key=?", (self.VERSION,)).fetchone():
                return False
            # Never overwrite an operator-created listing with the same code.
            codes = [f'INZ{k}{i:03d}' for k in ('ST','ETF','CB','FU','MF','IF','CM','TB','OP','SV','LN','CU') for i in range(1, self.CATALOG_SIZE + 1)]
            for code in codes:
                if self.db.code_locations(conn, code):
                    raise ValueError(f"기본 상품 코드가 기존 상품과 겹칩니다: {code}")
            for i in range(1, self.CATALOG_SIZE + 1):
                sector, brand = SECTORS[(i-1)//5], BRANDS[(i-1)%5]
                stock, price = f'INZST{i:03d}', 1000+i*120
                conn.execute("INSERT INTO company(company_code,name,industry,capital,total_shares,share_price,max_shareholder_id,exchange_id) VALUES (?,?,?,?,?,?,?,?)",
                             (stock, f'{brand} {sector}', sector,price*1_000_000,1_000_000,price,TREASURY_USER_ID,'INZ'))
                for suffix, kind, name in (('ETF','etf',f'{sector} {brand} ETF'),('CB','corporate_bond',f'{brand} {sector} 회사채'),
                        ('FU','stock_future',f'{brand} {sector} 선물'),('MF','mutual_fund',f'{brand} {sector} 액티브 펀드'),
                        ('IF','index_fund',f'{brand} {sector} 인덱스 펀드'),('CM','commodity_spot',f'{COMMODITIES[i-1]} 현물')):
                    code = f'INZ{suffix}{i:03d}'
                    unit = price if kind == 'stock_future' else 1000 if kind=='corporate_bond' else 10000 if kind in {'mutual_fund','index_fund','etf'} else 500+i*200
                    days = 365 if kind in {'stock_future','corporate_bond'} else 0
                    underlying = stock if kind=='stock_future' else None
                    issuer = stock if kind=='corporate_bond' else None
                    index = 'INZ' if kind in {'index_fund','etf'} else None
                    rate = 3.5 + (i%10)*.25 if kind=='corporate_bond' else 0
                    conn.execute("""INSERT INTO investment(product_code,name,product_type,total_supply,unit_price,maturity_days,manager,exchange_id,
                        underlying_code,issuer_code,maturity_epoch,face_value,annual_rate,nav_anchor,underlying_exchange_id,fund_category)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (code,name,kind,1_000_000,unit,days,'Inazina 국고',
                        {'stock_future':'FUT','corporate_bond':'BOND','commodity_spot':'COM'}.get(kind,'INZ'),
                        underlying,issuer,now+days*86400 if days else None,1000 if kind=='corporate_bond' else 0,rate,unit,index,
                        'equity' if kind in {'mutual_fund','index_fund'} else None))
                    if kind=='etf':
                        leverage=(-3,-2,-1,.5,1,1.5,2,3)[(i-1)%8]
                        cycle=(60,300,900,3600,86400)[(i-1)//10]
                        conn.execute("UPDATE investment SET name=?,leverage_multiplier=?,rebalance_cycle_seconds=? WHERE product_code=?",
                                     (f'Inazina 지수 ETF {i:02d} · {leverage:+g}배 · {cycle}초',leverage,cycle,code))
                    if kind == 'mutual_fund':
                        for offset in range(3):
                            j = (i-1+offset) % self.CATALOG_SIZE + 1
                            conn.execute(
                                "INSERT INTO fund_components "
                                "(product_code,asset_code,weight,base_price,asset_source,asset_class) "
                                "VALUES (?,?,?,?,'company','equity')",
                                (code,f'INZST{j:03d}',1/3,1000+j*120),
                            )
                    if kind in {'mutual_fund', 'index_fund'}:
                        from managers.fund_manager import FundManager
                        FundManager.create_empty(conn, code)
                conn.execute("INSERT INTO treasury_bond_products(product_code,name,annual_rate,maturity_days,face_value,exchange_id,market_price,total_supply) VALUES (?,?,?,?,?,?,?,?)",
                             (f'INZTB{i:03d}',f'Inazina 국채 {30+i*7}일 {i:02d}호',3+(i%10)*.1,30+i*7,1000,'BOND',1000,1_000_000_000))
                # Covered calls own 100 of the one million issued shares. No
                # uncollateralized promises or fabricated option holdings.
                strike = max(1,round(price*(.8+(i%5)*.1)))
                option_code = f'INZOP{i:03d}'
                cursor = conn.execute("INSERT INTO option_contracts(contract_code,writer_id,underlying_code,option_type,strike_price,expiration_epoch,implied_volatility,issued_contracts,created_epoch) VALUES (?,?,?,'call',?,?,?,?,?)",
                                      (option_code,TREASURY_USER_ID,stock,strike,now+(30+i)*86400,.6,100,now))
                cid = cursor.lastrowid
                conn.execute("INSERT INTO option_collateral_lots(contract_id,writer_id,underlying_code,account_number,quantity,average_cost) VALUES (?,?,?,NULL,100,?)", (cid,TREASURY_USER_ID,stock,price))
                conn.execute("INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,remaining_quantity,reserved_cash,funding_account,order_kind,option_order_type,status,created_epoch,updated_epoch) VALUES (?,?,'sell',?,100,100,0,NULL,'option','write','open',?,?)",
                             (TREASURY_USER_ID,option_code,max(1,price//10),now,now))
                conn.execute("INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,interest_cycle,compound_type,issuer) VALUES (?,?,?,?,?,?,?,?)",
                             (f'INZSV{i:03d}',f'{brand} {sector} 저축 {i:02d}호',2+(i%10)*.2,1000,30+i*7,86400,i%2,'Inazina 은행'))
                conn.execute("INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer) VALUES (?,?,?,?,?,?,?,?)",
                             (f'INZLN{i:03d}',f'{brand} {sector} 대출 {i:02d}호',5+(i%10)*.3,1_000_000,30+i*7,86400,500+i,'Inazina 은행'))
            initial = [('USD','달러','미국',1500),('KRW','원','대한민국',1),('EUR','유로','유럽연합',1650),
                       ('JPY','엔','일본',10),('GBP','파운드','영국',1900)]
            initial += [(f'IZC{i:02d}',f'이자니아 {i:02d} 통화',f'이자니아 연방 {i:02d}',100+i*25) for i in range(1,21)]
            for i,(currency,name,country,price) in enumerate(initial,1):
                code = f'CUR_{currency}'
                if conn.execute("SELECT 1 FROM currencies WHERE code=?",(currency,)).fetchone():
                    raise ValueError(f"기본 통화 코드 중복: {currency}")
                if self.db.code_locations(conn,code):
                    raise ValueError(f"외환 종목 코드 중복: {code}")
                conn.execute("INSERT INTO market_assets(code,name,exchange_id,price,type,market_volatility) VALUES (?,?, 'CURRENCY',?,'forex',.05)", (code,f'{name}/{self.db.base_currency_code()}',price))
                conn.execute("INSERT INTO currencies(code,name,asset_code) VALUES (?,?,?)", (currency,name,code))
                conn.execute("INSERT OR IGNORE INTO country_currencies(nationality,currency_code) VALUES (?,?)", (country,currency))
                conn.execute("INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,interest_cycle,compound_type,issuer,status,quote_currency,product_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f'INZCU{i:03d}',f'{name} 외환통장',1+(i%5)*.25,0,0,0,1,'currency','active',currency,'currency_saving'))
            conn.execute("INSERT INTO settings(key,value) VALUES (?, '1')", (self.VERSION,))
            return True
