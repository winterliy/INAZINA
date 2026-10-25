"""Validated administrator forms; no arbitrary SQL execution surface."""
from functions.money import base_currency_code
import json
import math
import time
from contextlib import nullcontext
from database.user_db import TREASURY_USER_ID
from managers.stochastic_price_engine import GarchConfig
from managers.auto_target_engine import AutoTargetConfig
from managers.credit_score_manager import CreditScoreManager
from managers.rate_manager import RateManager
from database.db_manager import DBManager
from functions.money import parse_currency_money

ENTITIES = {
    'stock': ('company','company_code','share_price','total_shares'),
    'asset': ('market_assets','code','price','total_supply'),
    'dr': ('market_assets','code','price','total_supply'),
    'etf': ('investment','product_code','unit_price','total_supply'),
    'future': ('investment','product_code','unit_price','total_supply'),
    'corporatebond': ('investment','product_code','unit_price','total_supply'),
    'fund': ('investment','product_code','unit_price','total_supply'),
    'indexfund': ('investment','product_code','unit_price','total_supply'),
    'commodity': ('investment','product_code','unit_price','total_supply'),
    'els': ('investment','product_code','unit_price','total_supply'),
    'bond': ('treasury_bond_products','product_code','market_price','total_supply'),
    'saving': ('saving','product_code',None,None),
    'loan': ('liability','product_code',None,None),
    'option': ('option_contracts','contract_code',None,None),
    'currency': ('currencies','code',None,None),
    'currencyproduct': ('saving','product_code',None,None),
    'etn': ('investment','product_code','unit_price','total_supply'),
    'convertiblebond': ('investment','product_code','unit_price','total_supply'),
    'warrantbond': ('investment','product_code','unit_price','total_supply'),
    'abs': ('investment','product_code','unit_price','total_supply'),
    'mbs': ('investment','product_code','unit_price','total_supply'),
    'abcp': ('investment','product_code','unit_price','total_supply'),
    'cma': ('saving','product_code',None,None),
    'ima': ('saving','product_code',None,None),
    'wrap': ('saving','product_code',None,None),
}
KINDS = {'etf':'etf','future':'stock_future','corporatebond':'corporate_bond',
         'fund':'mutual_fund','indexfund':'index_fund','commodity':'commodity_spot','els':'els',
         'etn':'etn','convertiblebond':'corporate_bond','warrantbond':'corporate_bond',
         'abs':'corporate_bond','mbs':'corporate_bond','abcp':'corporate_bond'}
# Corporate-bond variants share the corporate_bond engine (coupons, redemption, pricing).
BOND_FEATURES = {'corporatebond':'plain','convertiblebond':'convertible','warrantbond':'warrant',
                 'abs':'abs','mbs':'mbs','abcp':'abcp'}
# Savings-table products told apart by product_kind.
SAVING_KINDS = {'saving':'saving','currencyproduct':'currency_saving','cma':'cma','ima':'ima','wrap':'wrap'}
DEPOSIT_CATEGORIES = set(SAVING_KINDS)
EXAMPLES = {
    'stock': {'industry':'소프트웨어','exchange_id':'INZ','capital':1000000},
    'asset': {'exchange_id':'INZ','type':'stock'},
    'dr': {'exchange_id':'LSE','type':'depositary_receipt',
           'underlying_per_receipt':1,'receipt_type':'DR','depositary_bank':'Inazina Depositary',
           'custodian':'Inazina Custody','home_exchange_id':'INZ',
           'settlement_delay_seconds':0,'issue_fee_bps':0,'redeem_fee_bps':0},
    'etf': {'exchange_id':'INZ','underlying_exchange_id':'INZ','leverage_multiplier':1},
    'future': {'exchange_id':'FUT','underlying_code':'INZST001','maturity_days':90,'contract_size':1},
    'corporatebond': {'exchange_id':'BOND','issuer_code':'INZST001','face_value':1000,'annual_rate':4,'maturity_days':90},
    'fund': {'exchange_id':'INZ','components':{'INZST001':.5,'INZST002':.5}},
    'indexfund': {'exchange_id':'INZ','underlying_exchange_id':'INZ','fund_category':'equity'},
    'commodity': {'exchange_id':'COM'},
    'bond': {'annual_rate':3,'maturity_days':90,'face_value':1000,'exchange_id':'BOND'},
    'saving': {'interest_rate':3,'deposit_amount':1000,'maturity_days':30,'interest_cycle':86400,'compound_type':1,'issuer':'Inazina','rate_type':'fixed'},
    'loan': {'interest_rate':5,'loan_limit':1000000,'maturity_days':30,'interest_cycle':86400,'min_credit_score':500,'issuer':'Inazina','repayment_cycle_seconds':0,'repayment_amount':0,'rate_type':'fixed'},
    'option': {'underlying_code':'INZST001','option_type':'call','strike_price':1120,'maturity_days':30,'quantity':1,'limit_price':100,'volatility_percent':60},
    'currency': {'asset_code':'CUR_NEW','nationality':'새 국가'},
    'currencyproduct': {'interest_rate':1,'deposit_amount':0,'maturity_days':0,'interest_cycle':86400,'compound_type':0,'issuer':'Inazina','product_kind':'currency_saving','rate_type':'fixed'},
    'etn': {'exchange_id':'INZ','issuer_code':'INZST001','underlying_exchange_id':'INZ','leverage_multiplier':1},
    'convertiblebond': {'exchange_id':'BOND','issuer_code':'INZST001','face_value':1000,'annual_rate':1,'maturity_days':365},
    'warrantbond': {'exchange_id':'BOND','issuer_code':'INZST001','face_value':1000,'annual_rate':1.5,'maturity_days':365},
    'abs': {'exchange_id':'BOND','issuer_code':'INZST001','face_value':1000,'annual_rate':3,'maturity_days':365,'collateral_note':'매출채권 풀'},
    'mbs': {'exchange_id':'BOND','issuer_code':'INZST001','face_value':1000,'annual_rate':3,'maturity_days':1095,'collateral_note':'주택담보대출 풀'},
    'abcp': {'exchange_id':'BOND','issuer_code':'INZST001','face_value':1000,'annual_rate':3.5,'maturity_days':90,'collateral_note':'단기 매출채권'},
    'cma': {'interest_rate':2,'deposit_amount':0,'maturity_days':0,'interest_cycle':86400,'compound_type':1,'issuer':'Inazina','rate_type':'fixed'},
    'ima': {'interest_rate':0,'deposit_amount':10000,'maturity_days':365,'interest_cycle':86400,'compound_type':0,'issuer':'Inazina','rate_type':'fixed','reference_code':'INZST001','participation_percent':70},
    'wrap': {'interest_rate':0,'deposit_amount':10000,'maturity_days':0,'interest_cycle':86400,'compound_type':0,'issuer':'Inazina','rate_type':'fixed','management_fee_percent':1},
    'els': {'face_value':1000,'annual_coupon':12,'maturity_days':180,'underlyings':['INZST001'],
            'observation_days':[30,60,90],'redemption_levels':[90,85,80],
            'maturity_level':75,'knock_in_level':60,'subscription_days':1,'exchange_id':'INZ'},
}

class AdminControlManager:
    def __init__(self,bot):
        self.bot,self.db = bot,bot.db

    def require(self, actor_id, conn=None):
        user = (conn.execute("SELECT role FROM users WHERE user_id=?",(str(actor_id),)).fetchone()
                if conn else self.db.users.get_user(str(actor_id)))
        if not user or str(user['role']).strip().lower() != 'admin':
            raise ValueError("DB role=admin인 관리자만 사용할 수 있습니다.")

    @staticmethod
    def _audit(conn,actor,action,payload):
        conn.execute(
            "INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES (?,?,?,?)",
            (str(actor), action, json.dumps(payload, ensure_ascii=False, default=str), time.time()),
        )

    @staticmethod
    def _number(value, low=0, high=2**53-1, integer=False):
        if isinstance(value,bool):
            raise ValueError("숫자 입력값을 확인해주세요.")
        number = float(value)
        if not math.isfinite(number) or not low <= number <= high or (integer and number != int(number)):
            raise ValueError(f"숫자는 {low}~{high} {'정수' if integer else '유한한 값'}여야 합니다.")
        return int(number) if integer else number

    def entity(self,actor,category,operation,code,name='',price='',supply='',extra=None,conn=None):
        with DBManager.admin_scope(actor):
            return self._entity(actor,category,operation,code,name,price,supply,extra,conn)

    def _entity(self,actor,category,operation,code,name='',price='',supply='',extra=None,conn=None):
        self.require(actor)
        if category not in ENTITIES or operation not in {'create','edit','delete','restore','terminate'}:
            raise ValueError("지원하는 설정을 선택해주세요.")
        code = str(code).strip()
        if category=='currency':
            code=code.upper()
        if (not code and not (category=='option' and operation=='create')) or len(code)>80:
            raise ValueError("코드는 1~80자로 입력해주세요.")
        extra = dict(extra or {})
        if category == 'currencyproduct':
            # Keep the administrator command's older field names at the boundary.
            if 'currency_code' in extra:
                extra['quote_currency'] = extra.pop('currency_code')
            if 'annual_rate' in extra:
                extra['interest_rate'] = extra.pop('annual_rate')
        if category=='els':
            from managers.els_manager import ELSManager
            manager=ELSManager(self.db,self.bot.user_mgr)
            if operation=='create':
                if name:extra['name']=name
                if price:extra['face_value']=price
                if supply:extra['total_supply']=supply
                return manager.create(actor,code,extra,conn=conn)
            if operation=='delete':
                result=manager.cancel(actor,code,conn=conn)
                if not result[0]:raise ValueError(result[1])
                return result[1]
            raise ValueError('ELS 발행조건은 변경할 수 없습니다. 모집 중 취소 후 새 상품을 생성해주세요.')
        if category=='option' and operation=='create':
            keys = {'underlying_code','option_type','strike_price','maturity_days','quantity','limit_price','volatility_percent'}
            if set(extra)-keys:
                raise ValueError("옵션 작성 필드를 확인해주세요.")
            from managers.currency_manager import CurrencyManager
            with self.db.atomic() as conn:
                # The treasury of the country listing the underlying writes the contract.
                writer = CurrencyManager.exchange_treasury(conn, str(extra.get('underlying_code','')))
            result = self.bot.option_mgr.create_and_write(writer,**extra)
            if not result[0]:
                raise ValueError(result[1])
            with self.db.atomic() as conn:
                self._audit(conn,actor,'option.create',extra)
            return result[1]
        table,key,price_key,supply_key = ENTITIES[category]
        with (self.db.atomic() if conn is None else nullcontext(conn)) as conn:
            self.require(actor,conn)
            existing = conn.execute(f'SELECT * FROM "{table}" WHERE "{key}"=?', (code,)).fetchone()
            if category in DEPOSIT_CATEGORIES and existing and (existing['product_kind'] or 'saving') != SAVING_KINDS[category]:
                existing = None if operation == 'create' else existing
                if existing:
                    raise ValueError("이 카테고리의 상품 코드가 아닙니다.")
            if category in KINDS and existing and existing['product_type']!=KINDS[category]:
                raise ValueError("이 카테고리의 상품 코드가 아닙니다.")
            if category in BOND_FEATURES and existing and (existing['bond_feature'] or 'plain') != BOND_FEATURES[category]:
                raise ValueError("이 카테고리의 상품 코드가 아닙니다.")
            if operation == 'terminate':
                if category != 'dr' or not existing:
                    raise ValueError('주식예탁증서(DR) 프로그램만 종료할 수 있습니다.')
                program = conn.execute(
                    "SELECT program_status FROM depositary_receipts WHERE dr_code=?", (code,),
                ).fetchone()
                if not program or program['program_status'] == 'terminated':
                    raise ValueError('종료 가능한 DR 프로그램을 찾을 수 없습니다.')
                outstanding = conn.execute(
                    "SELECT "
                    "(SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE asset_code=?) + "
                    "(SELECT COALESCE(SUM(quantity),0) FROM option_collateral_lots WHERE underlying_code=?) + "
                    "(SELECT COALESCE(SUM(quantity),0) FROM accounts.loan_collateral_assets "
                    " WHERE asset_code=? AND status='pledged') + "
                    "(SELECT COALESCE(SUM(remaining_quantity),0) FROM market_orders "
                    " WHERE asset_code=? AND status IN ('scheduled','open','partial')) + "
                    "(SELECT COALESCE(SUM(CASE WHEN movement_type='issue' THEN underlying_quantity "
                    "ELSE -underlying_quantity END),0) FROM depositary_receipt_movements WHERE dr_code=?)",
                    (code, code, code, code, code),
                ).fetchone()[0]
                if float(outstanding or 0) > 0:
                    raise ValueError('보유·담보·주문 또는 시뮬레이터 보관 원주가 남아 있어 DR 프로그램을 종료할 수 없습니다.')
                conn.execute("UPDATE depositary_receipts SET program_status='terminated' WHERE dr_code=?", (code,))
                conn.execute("UPDATE market_assets SET status='inactive' WHERE code=?", (code,))
                self._audit(conn, actor, 'dr.terminate', {'code': code})
                return '✅ DR 프로그램을 종료했습니다. 이 동작은 복구할 수 없습니다.'
            if operation in {'delete','restore'}:
                if not existing:
                    raise ValueError("종목 또는 상품을 찾을 수 없습니다.")
                if operation=='delete':
                    from managers.els_manager import ELSManager
                    ELSManager.ensure_unreferenced(conn,code)
                    if category=='stock':
                        issuer=existing['parent_company_code'] or code
                        unpaid=conn.execute("SELECT 1 FROM preferred_dividend_claims WHERE status IN ('unpaid','partial') AND (issuer_code=? OR asset_code=?) LIMIT 1",(code,code)).fetchone()
                        cash=conn.execute('SELECT 1 FROM issuer_cash_accounts WHERE company_code=? AND (balance>0 OR locked_amount>0)',(code,)).fetchone()
                        linked=conn.execute("SELECT 1 FROM company WHERE parent_company_code=? AND status='active' LIMIT 1",(code,)).fetchone()
                        preference=existing['share_class']=='preferred' and existing['liquidation_value_per_share']>0 and conn.execute('SELECT 1 FROM user_assets WHERE asset_code=? AND quantity>0 LIMIT 1',(code,)).fetchone()
                        if unpaid or cash or linked or preference:
                            raise ValueError(f'법인 현금·배당채권·우선주 권리가 남아 있습니다. $법인청산 {issuer}로 정산해주세요.')
                        if conn.execute("SELECT 1 FROM depositary_receipts WHERE underlying_code=? AND program_status<>'terminated' LIMIT 1",(code,)).fetchone():
                            raise ValueError('DR 프로그램이 연결된 기초 원주는 삭제할 수 없습니다. DR을 종료한 뒤 다시 시도해주세요.')
                    reservations = conn.execute("SELECT 1 FROM market_orders WHERE asset_code=? AND status IN ('open','partial') LIMIT 1",(code,)).fetchone()
                    claims = conn.execute("SELECT 1 FROM option_contracts WHERE (contract_code=? OR underlying_code=?) AND status='active' LIMIT 1",(code,code)).fetchone()
                    if reservations or claims:
                        raise ValueError("미체결 주문·옵션 의무가 남아 있습니다. 주문 취소와 만기 정산 후 삭제해주세요.")
                    if (category == 'future' or category in BOND_FEATURES or category == 'etn') and (
                        conn.execute("SELECT 1 FROM user_assets WHERE asset_code=? LIMIT 1",(code,)).fetchone()
                        or conn.execute("SELECT 1 FROM futures_positions WHERE product_code=? LIMIT 1",(code,)).fetchone()):
                        raise ValueError("미상환 채권·선물 포지션을 먼저 정산해주세요.")
                    if category=='currency' and conn.execute("SELECT 1 FROM accounts WHERE account_type='currency_saving' AND quote_currency=? AND balance_units>0",(code,)).fetchone():
                        raise ValueError("보유 잔액이 있는 통화는 삭제할 수 없습니다.")
                    currency = code if category=='currency' else (conn.execute('SELECT code FROM currencies WHERE asset_code=?',(code,)).fetchone() or [None])[0] if category=='asset' else None
                    if currency and currency!=base_currency_code(conn) and any(conn.execute(f'SELECT 1 FROM {table} WHERE quote_currency=? LIMIT 1',(currency,)).fetchone() for table in ('company','investment','market_assets')):
                        raise ValueError('이 통화로 결제하는 종목이 있어 통화·환율을 삭제할 수 없습니다.')
                    if category=='asset' and conn.execute("SELECT 1 FROM currencies c JOIN accounts a ON a.quote_currency=c.code WHERE c.asset_code=? AND a.account_type='currency_saving' AND a.balance_units>0",(code,)).fetchone():
                        raise ValueError("외환통장 잔액이 있는 환율 종목은 삭제할 수 없습니다.")
                    if category=='currency' and code==base_currency_code(conn):
                        raise ValueError("기준 통화 기준 통화는 삭제할 수 없습니다.")
                # Preserve historical claims/holdings and payout definitions.
                conn.execute(f'UPDATE "{table}" SET status=? WHERE "{key}"=?',('inactive' if operation=='delete' else 'active',code))
                if category == 'dr':
                    program = conn.execute(
                        "SELECT program_status FROM depositary_receipts WHERE dr_code=?", (code,),
                    ).fetchone()
                    if operation == 'restore' and program and program['program_status'] == 'terminated':
                        raise ValueError('종료된 DR 프로그램은 다시 활성화할 수 없습니다.')
                    conn.execute("UPDATE depositary_receipts SET program_status=? WHERE dr_code=?",
                                 ('suspended' if operation=='delete' else 'active', code))
                self._audit(conn,actor,f'{category}.{operation}',{'code':code})
                return '✅ 거래 중지 처리했습니다.' if operation=='delete' else '✅ 거래를 재개했습니다.'
            if operation=='create' and (existing or (category!='currency' and self.db.code_locations(conn,code))):
                raise ValueError("이미 사용 중인 코드입니다.")
            if operation=='edit' and not existing:
                raise ValueError("수정할 코드를 찾을 수 없습니다.")
            columns = {r['name'] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            protected = {key,'created_at','created_epoch','sold_amount','writer_id','contract_id','issued_contracts','status','product_type'}
            virtual = ({'nationality'} if category=='currency' else
                       {'components'} if category=='fund' else
                       {'underlying_code','underlying_per_receipt','receipt_type',
                        'depositary_bank','custodian','home_exchange_id'} if category=='dr' else set())
            if set(extra)-columns-virtual:
                raise ValueError(f"지원하지 않는 필드: {', '.join(sorted(set(extra)-columns-virtual))}")
            if set(extra)&protected:
                raise ValueError("계약 식별자·발행 의무·판매액·상태는 일반 수정 대상이 아닙니다.")
            if category=='option' and operation=='edit':
                if set(extra)-{'implied_volatility'} or price or supply:
                    raise ValueError("옵션 계약 조건은 고정됩니다. 내재변동성 수정 또는 $사건으로 프리미엄을 조정해주세요.")
            values = {k:v for k,v in extra.items() if k in columns}
            if 'settlement_delay_seconds' in values:
                if category != 'dr':
                    raise ValueError('내부 결제 지연 설정은 DR 상품에만 적용할 수 있습니다.')
                values['settlement_delay_seconds'] = self._number(
                    values['settlement_delay_seconds'], 0, 2_592_000, integer=True,
                )
            for fee_field in ('issue_fee_bps', 'redeem_fee_bps'):
                if fee_field in values:
                    if category != 'dr':
                        raise ValueError('DR 전환 수수료 설정은 DR 상품에만 적용할 수 있습니다.')
                    values[fee_field] = self._number(values[fee_field], 0, 10_000, integer=True)
            if 'currency_code' in values:
                values['currency_code']=str(values['currency_code']).strip().upper()
            if 'quote_currency' in values:
                values['quote_currency']=str(values['quote_currency']).strip().upper()
                if category not in {
                    'stock','asset','dr','commodity','loan', *DEPOSIT_CATEGORIES
                } and values['quote_currency']!=base_currency_code(conn):
                    raise ValueError('이 상품 유형은 외화 결제를 지원하지 않습니다.')
                if not conn.execute("SELECT 1 FROM currencies WHERE code=? AND status='active'",(values['quote_currency'],)).fetchone():
                    raise ValueError('활성 결제 통화를 입력해주세요.')
                if category in {'asset','dr'} and values['quote_currency']!=base_currency_code(conn) and extra.get('type',(dict(existing) if existing else {}).get('type','stock')) not in {'stock','주식','commodity_spot','depositary_receipt'}:
                    raise ValueError('외화 결제 자산은 주식·현물 원자재만 지원합니다.')
                if existing and values['quote_currency']!=existing['quote_currency']:
                    raise ValueError('기존 종목의 결제 통화는 변경할 수 없습니다. 새 종목을 생성해주세요.')
            if name and 'name' in columns:
                values['name'] = name[:100]
            if price and price_key:
                price_currency = str(
                    values.get('quote_currency')
                    or (existing['quote_currency'] if existing and 'quote_currency' in existing.keys() else base_currency_code(conn))
                ).upper()
                values[price_key] = parse_currency_money(
                    conn, price, price_currency, minimum=0
                )
                if values[price_key] <= 0:
                    raise ValueError("양의 가격을 입력해주세요.")
            if supply and supply_key:
                values[supply_key] = self._number(supply,1,integer=True)
            if values.get('issuer') not in (None, '') and 'issuer' in columns:
                # A bank is chosen by its ID; the row keeps the issuer name it links by.
                from functions.banks import bank_issuer
                values['issuer'] = bank_issuer(conn, values['issuer'])
            if operation=='create':
                values[key] = code
                values.update({k:v for k,v in EXAMPLES[category].items() if k in columns and k not in values})
                if category == 'currencyproduct':
                    values.setdefault('quote_currency', base_currency_code(conn))
                if category == 'dr':
                    values['type'] = 'depositary_receipt'
                if category in DEPOSIT_CATEGORIES:
                    values['product_kind'] = SAVING_KINDS[category]
                if category in BOND_FEATURES:
                    values['bond_feature'] = BOND_FEATURES[category]
                if 'name' in columns and not values.get('name'):
                    raise ValueError("상품 이름을 입력해주세요.")
                if price_key and not values.get(price_key):
                    raise ValueError("양의 가격을 입력해주세요.")
                if supply_key and not values.get(supply_key):
                    raise ValueError("발행량을 입력해주세요.")
                if category in KINDS:
                    values['product_type'] = KINDS[category]
                    values['manager'] = 'Inazina 국고'
                    values['nav_anchor'] = values['unit_price']
                if category == 'indexfund':
                    from managers.investment_manager import InvestmentManager
                    fund_category = InvestmentManager.normalize_fund_category(
                        values.get('fund_category', 'equity')
                    )
                    if fund_category != 'equity':
                        raise ValueError('현재 인덱스 펀드는 주식 거래소 지수만 추종하므로 주식형만 지원합니다.')
                    values['fund_category'] = 'equity'
            merged = {**(dict(existing) if existing else {}),**values}
            if 'Fractal' in values or merged.get('Fractal') == 1:
                from functions.quantities import validate_fractional_setting
                asset_type = 'stock' if category == 'stock' else KINDS.get(category, merged.get('type'))
                enabled = validate_fractional_setting(conn, table, key, code,
                    values.get('Fractal', merged.get('Fractal', 0)), asset_type, merged.get(supply_key))
                if 'Fractal' in values:
                    values['Fractal'] = enabled
            if 'extension_interest_rate' in values and values['extension_interest_rate'] is not None:
                values['extension_interest_rate'] = self._number(values['extension_interest_rate'], 0, 100)
            money_fields = {
                'stock': {'capital','share_price','liquidation_value_per_share','dividend_per_share'},
                'asset': {'price','dividend_per_share'},
                'saving': {'deposit_amount'},
                'currencyproduct': {'deposit_amount'},
                'cma': {'deposit_amount'}, 'ima': {'deposit_amount'}, 'wrap': {'deposit_amount'},
                'etn': {'unit_price','dividend_per_share'},
                **{bond: {'unit_price','face_value','conversion_price','warrant_exercise_price'}
                   for bond in BOND_FEATURES if bond != 'corporatebond'},
                'loan': {'loan_limit','repayment_amount'},
                'etf': {'unit_price','face_value','dividend_per_share'},
                'future': {'unit_price','face_value','dividend_per_share'},
                'corporatebond': {'unit_price','face_value','dividend_per_share'},
                'fund': {'unit_price','face_value','dividend_per_share'},
                'indexfund': {'unit_price','face_value','dividend_per_share'},
                'commodity': {'unit_price','face_value','dividend_per_share'},
                'bond': {'market_price','face_value'},
            }.get(category, set())
            denomination = str(merged.get('quote_currency') or base_currency_code(conn)).upper()
            for field in money_fields & values.keys():
                values[field] = parse_currency_money(
                    conn, values[field], denomination, minimum=0
                )
            merged = {**merged, **values}
            if category in DEPOSIT_CATEGORIES or category == 'loan':
                rate_type = str(merged.get('rate_type') or 'fixed').strip().lower()
                if rate_type not in {'fixed','floating'}:
                    raise ValueError("rate_type은 fixed 또는 floating이어야 합니다.")
                values['rate_type'] = rate_type
                if rate_type == 'floating':
                    if operation == 'create' and 'benchmark_key' not in extra:
                        values['benchmark_key'] = 'bank_bond' if category == 'loan' else 'funding'
                    benchmark = str(values.get(
                        'benchmark_key', merged.get('benchmark_key', 'policy')
                    )).strip().lower()
                    if benchmark not in {'policy','funding','bank_bond','treasury'}:
                        raise ValueError('benchmark_key는 policy/funding/bank_bond/treasury 중 하나여야 합니다.')
                    tenor = self._number(
                        values.get('benchmark_tenor_days', merged.get('benchmark_tenor_days', 0)),
                        0, 36500, integer=True,
                    )
                    if benchmark != 'policy' and tenor == 0:
                        tenor = max(1, int(merged.get('maturity_days') or 30))
                    values.update(benchmark_key=benchmark, benchmark_tenor_days=tenor)
                    currency = str(merged.get('quote_currency') or base_currency_code(conn)).upper()
                    nationality = str(merged.get('rate_nationality') or '').strip()
                    if not nationality:
                        countries = conn.execute(
                            "SELECT nationality FROM country_currencies WHERE currency_code=? "
                            "ORDER BY nationality", (currency,)
                        ).fetchall()
                        if len(countries) != 1:
                            raise ValueError(
                                "변동금리 상품은 rate_nationality로 기준금리 적용 국가를 지정해주세요."
                            )
                        nationality = countries[0]['nationality']
                        values['rate_nationality'] = nationality
                    spread = self._number(merged.get('base_rate_spread', 0), -100, 100)
                    floor = self._number(merged.get('rate_floor', 0), 0, 100)
                    negative_allowed = bool(self._number(
                        merged.get('negative_rate_allowed', 1 if category != 'loan' else 0),
                        0, 1, integer=True,
                    ))
                    cap_raw = merged.get('rate_cap')
                    cap = None if cap_raw in (None, '') else self._number(cap_raw, 0, 100)
                    cycle = self._number(
                        merged.get('repricing_cycle_seconds', 2592000), 1, 10*365*86400,
                        integer=True,
                    )
                    interest_cycle = self._number(merged.get('interest_cycle'), .001, 365*86400)
                    if cycle < interest_cycle or not math.isclose(
                        cycle / interest_cycle, round(cycle / interest_cycle), abs_tol=1e-9
                    ):
                        raise ValueError("재산정주기는 이자주기의 정수배여야 합니다.")
                    if floor > (100 if cap is None else cap):
                        raise ValueError("상품 금리 하한은 상품 상한보다 클 수 없습니다.")
                    values.update({
                        'base_rate_spread': spread,
                        'rate_floor': floor,
                        'rate_cap': cap,
                        'repricing_cycle_seconds': cycle,
                        'negative_rate_allowed': int(negative_allowed),
                    })
                    # A supplied advertised rate is translated into a spread unless
                    # the caller explicitly supplied the spread itself.
                    if 'interest_rate' in extra and 'base_rate_spread' not in extra:
                        base = RateManager.market_rate(
                            conn, nationality, benchmark, tenor or merged.get('maturity_days', 0)
                        )
                        provisional = {**merged, **values, 'base_rate_spread': 0}
                        other_spread = RateManager.product_spread(
                            conn, provisional, 'loan' if category == 'loan' else 'deposit'
                        )
                        spread = self._number(
                            float(extra['interest_rate']) - base - other_spread, -100, 100
                        )
                        values['base_rate_spread'] = spread
                    priced = {**merged, **values}
                    values['interest_rate'] = RateManager.floating_rate(
                        conn, nationality,
                        RateManager.product_spread(
                            conn, priced, 'loan' if category == 'loan' else 'deposit'
                        ), (None if category != 'loan' and negative_allowed else floor),
                        cap, benchmark_key=benchmark,
                        tenor_days=tenor or merged.get('maturity_days', 0),
                        apply_legal_max=category == 'loan',
                    )
                elif merged.get('rate_nationality'):
                    values['rate_nationality'] = str(merged['rate_nationality']).strip()
                merged = {**merged, **values}
                day_count = str(merged.get('day_count_convention') or 'ACT/365').upper()
                if day_count not in {'ACT/365', 'ACT/360', '30/360'}:
                    raise ValueError('day_count_convention은 ACT/365, ACT/360, 30/360 중 하나여야 합니다.')
                values['day_count_convention'] = day_count
                business_day = str(
                    merged.get('business_day_convention') or 'following'
                ).lower()
                if business_day not in {'none','following','modified_following','preceding'}:
                    raise ValueError(
                        'business_day_convention은 none/following/modified_following/preceding 중 하나여야 합니다.'
                    )
                values['business_day_convention'] = business_day
                if category != 'loan':
                    values['early_withdrawal_rate'] = self._number(
                        merged.get('early_withdrawal_rate', 0), -100, 100
                    )
                    values['early_withdrawal_fee_percent'] = self._number(
                        merged.get('early_withdrawal_fee_percent', 0), 0, 100
                    )
                    values['deposit_insured'] = self._number(
                        merged.get('deposit_insured', 1), 0, 1, integer=True
                    )
                if category == 'loan':
                    amortization = str(merged.get('amortization_type') or 'legacy').lower()
                    if amortization not in {'legacy', 'bullet', 'equal_principal', 'annuity'}:
                        raise ValueError(
                            'amortization_type은 legacy/bullet/equal_principal/annuity 중 하나여야 합니다.'
                        )
                    values['amortization_type'] = amortization
                    values['payment_frequency_days'] = self._number(
                        merged.get('payment_frequency_days', 30), 1, 3650, integer=True
                    )
                    haircut = self._number(merged.get('collateral_haircut', .2), 0, 1)
                    margin_ltv = self._number(merged.get('margin_call_ltv', .85), .01, 100)
                    liquidation_ltv = self._number(
                        merged.get('liquidation_ltv', 1), margin_ltv, 100
                    )
                    values.update(
                        collateral_haircut=haircut,
                        margin_call_ltv=margin_ltv,
                        liquidation_ltv=liquidation_ltv,
                    )
                    for field in (
                        'max_dti_percent', 'max_dsr_percent',
                        'borrower_history_spread_max', 'affordability_spread_max',
                        'mandatory_cost_percent', 'liquidation_cost_percent',
                        'prepayment_fee_percent',
                    ):
                        if field in merged:
                            values[field] = self._number(merged[field], 0, 100)
                    values['prepayment_fee_decline_days'] = self._number(
                        merged.get('prepayment_fee_decline_days', 0), 0, 36500,
                        integer=True,
                    )
            if category=='stock':
                if existing and values.get('exchange_id',existing['exchange_id'])!=existing['exchange_id']:
                    from managers.els_manager import ELSManager
                    if ELSManager.index_removal_blocked(conn,[code]):
                        raise ValueError('미상환 ELS의 기초지수를 구성하는 마지막 주식은 거래소를 변경할 수 없습니다.')
                share_class=merged.get('share_class','common')
                if share_class not in {'common','preferred'}:
                    raise ValueError('share_class는 common 또는 preferred입니다.')
                if existing and share_class!=existing['share_class']:
                    raise ValueError('발행된 주식의 보통주·우선주 구분은 변경할 수 없습니다.')
                if share_class=='preferred':
                    parent=conn.execute("SELECT * FROM company WHERE company_code=? AND status='active' AND share_class='common'",
                        (merged.get('parent_company_code'),)).fetchone()
                    if not parent or parent['company_code']==code:
                        raise ValueError('우선주의 parent_company_code에 활성 보통주 법인 id를 입력해주세요.')
                    if existing and merged['parent_company_code']!=existing['parent_company_code']:
                        raise ValueError('우선주의 소속 법인은 $합병으로 변경해주세요.')
                    if parent['quote_currency']!=merged.get('quote_currency',base_currency_code(conn)):
                        raise ValueError('우선주와 소속 법인의 결제 통화가 같아야 합니다.')
                    if operation=='create' and 'industry' not in extra:
                        values['industry']=parent['industry']
                    if operation=='create':
                        values.setdefault('dividend_priority',1)
                        values.setdefault('liquidation_value_per_share',values.get('share_price',merged.get('share_price',0)))
                if merged.get('dividend_funding','treasury') not in {'treasury','issuer'}:
                    raise ValueError('dividend_funding은 treasury(국고) 또는 issuer(법인현금)입니다.')
                if merged.get('dividend_funding')=='issuer' and merged.get('quote_currency',base_currency_code(conn))!=base_currency_code(conn):
                    raise ValueError('법인현금 재원 배당은 기준 통화 결제 회사만 지원합니다.')
                for field,maximum in (('dividend_priority',1000),('cumulative_dividend',1)):
                    if field in values:values[field]=self._number(values[field],0,maximum,integer=True)
                if 'liquidation_value_per_share' in values:
                    values['liquidation_value_per_share'] = parse_currency_money(
                        conn, values['liquidation_value_per_share'], denomination,
                        minimum=0,
                    )
                if share_class=='common' and (int(values.get('dividend_priority',merged.get('dividend_priority',0))) or int(values.get('cumulative_dividend',merged.get('cumulative_dividend',0)))):
                    raise ValueError('배당 우선순위·누적배당은 우선주에만 설정할 수 있습니다.')
            for field,maximum in (('dividend_cycle_seconds',10*365*86400),
                                  ('dividend_is_active',1),('extension_min_credit_score',1000),('extension_days',3650)):
                if field in values:
                    values[field]=self._number(values[field],0,maximum,integer=True)
            if 'dividend_per_share' in values:
                values['dividend_per_share'] = parse_currency_money(
                    conn, values['dividend_per_share'], denomination, minimum=0
                )
            if 'dividend_next_epoch' in extra:
                raise ValueError('다음 배당 시각은 배당 주기로 자동 관리합니다.')
            if any(field in values for field in ('dividend_cycle_seconds','dividend_per_share','dividend_is_active','dividend_mode','dividend_percent')):
                from managers.corporate_actions_manager import CorporateActionsManager
                dividend_type = 'stock' if category == 'stock' else KINDS.get(category, merged.get('type'))
                if dividend_type not in CorporateActionsManager.DIVIDEND_TYPES:
                    raise ValueError('정기배당은 주식·우선주·ETF·펀드·현물 상품만 지원합니다.')
                cycle=int(values.get('dividend_cycle_seconds',merged.get('dividend_cycle_seconds',0)))
                amount=float(values.get('dividend_per_share',merged.get('dividend_per_share',0)))
                mode=values.get('dividend_mode')
                if mode is None:
                    mode='percent' if 'dividend_percent' in values else 'fixed' if 'dividend_per_share' in values else merged.get('dividend_mode','fixed')
                if mode not in {'fixed','percent'}:
                    raise ValueError('dividend_mode는 fixed(고정금액) 또는 percent(가격비율)입니다.')
                percent=self._number(values.get('dividend_percent',merged.get('dividend_percent',0)),0,100)
                if (mode=='fixed' and 'dividend_percent' in values and percent>0) or (mode=='percent' and 'dividend_per_share' in values and amount>0):
                    raise ValueError('주당 고정금액과 비율배당 중 한 방식만 설정해주세요.')
                values.update(dividend_mode=mode,dividend_per_share=amount if mode=='fixed' else 0,
                              dividend_percent=percent if mode=='percent' else 0)
                active=int(values.get('dividend_is_active',merged.get('dividend_is_active',1)))
                dividend_value=amount if mode=='fixed' else percent
                values['dividend_next_epoch']=int(time.time())+cycle if active and cycle>0 and dividend_value>0 else None
            if price_key and price_key in values and values[price_key] is not None:
                values[price_key] = parse_currency_money(
                    conn, values[price_key], denomination, minimum=0
                )
                if values[price_key] <= 0:
                    raise ValueError('양의 가격을 입력해주세요.')
            if supply_key and supply_key in values and values[supply_key] is not None:
                values[supply_key]=self._number(values[supply_key],1,integer=True)
            if existing and supply_key and supply_key in values:
                issued = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE asset_code=?",(code,)).fetchone()[0]
                issued += conn.execute("SELECT COALESCE(SUM(quantity),0) FROM option_collateral_lots WHERE underlying_code=?",(code,)).fetchone()[0]
                issued += conn.execute("SELECT COALESCE(SUM(quantity),0) FROM accounts.loan_collateral_assets WHERE asset_code=? AND status='pledged'",(code,)).fetchone()[0]
                if category == 'stock':
                    issued += conn.execute(
                        "SELECT COALESCE(SUM(CASE WHEN m.movement_type='issue' THEN m.underlying_quantity "
                        "ELSE -m.underlying_quantity END),0) FROM depositary_receipt_movements m "
                        "JOIN depositary_receipts d ON d.dr_code=m.dr_code "
                        "WHERE d.underlying_code=?", (code,),
                    ).fetchone()[0]
                    dr_capacity = conn.execute(
                        "SELECT COALESCE(SUM(deposited_underlying_quantity),0) "
                        "FROM depositary_receipts WHERE underlying_code=? "
                        "AND program_status<>'terminated'", (code,),
                    ).fetchone()[0]
                    issued = max(float(issued), float(dr_capacity or 0))
                if values[supply_key] < issued:
                    raise ValueError("발행량을 이미 보유·담보·DR 예탁 설정된 수량보다 작게 줄일 수 없습니다.")
            if category in {'etf','indexfund','etn'} and merged.get('underlying_exchange_id'):
                exchange = merged['underlying_exchange_id']
                if not conn.execute("SELECT 1 FROM company WHERE exchange_id=? AND status='active' UNION SELECT 1 FROM market_assets WHERE exchange_id=? AND status='active' AND type IN ('stock','주식')",(exchange,exchange)).fetchone():
                    raise ValueError("기초 지수에 사용할 주식 거래소를 확인해주세요.")
            for field in ('interest_rate','annual_rate'):
                if field in merged:
                    minimum = -100 if field == 'interest_rate' and category in DEPOSIT_CATEGORIES else 0
                    values[field] = self._number(merged[field],minimum,100)
            if category == 'loan':
                RateManager.require_legal_rate(
                    conn, values.get('interest_rate', merged.get('interest_rate', 0)),
                    nationality=merged.get('rate_nationality'),
                    currency_code=values.get('quote_currency', merged.get('quote_currency', base_currency_code(conn))),
                )
            for field in ('face_value','loan_limit','capital','contract_size','deposit_amount','maturity_days','repayment_cycle_seconds','repayment_amount'):
                if field in merged and merged[field] is not None:
                    values[field] = self._number(merged[field],0,integer=True)
            for field in ('market_volatility','implied_volatility','initial_margin','maintenance_margin',
                          'liquidity','trend_sensitivity','sector_sensitivity','jump_intensity','mean_reversion'):
                if field in values and values[field] is not None:
                    high = 5 if field == 'implied_volatility' else 10 if field in {
                        'liquidity','trend_sensitivity','sector_sensitivity','jump_intensity','mean_reversion'
                    } else 1
                    values[field] = self._number(values[field],0,high)
            if 'interest_cycle' in merged:
                values['interest_cycle'] = self._number(merged['interest_cycle'],.001,365*86400)
            if 'is_fixed' in values:
                values['is_fixed'] = self._number(values['is_fixed'],0,1,integer=True)
            if 'compound_type' in merged:
                values['compound_type']=self._number(merged['compound_type'],0,1,integer=True)
            if 'min_credit_score' in merged:
                values['min_credit_score']=self._number(merged['min_credit_score'],0,1000,integer=True)
            if 'initial_margin' in merged and not 0 < float(merged['maintenance_margin']) <= float(merged['initial_margin']) <= 1:
                raise ValueError('유지증거금은 초기증거금 이하이며 모두 0보다 커야 합니다.')
            if 'leverage_multiplier' in merged:
                values['leverage_multiplier'] = self._number(merged['leverage_multiplier'],-10,10)
                if not values['leverage_multiplier']:
                    raise ValueError("레버리지 배율 0은 사용할 수 없습니다.")
            if 'rebalance_cycle_seconds' in merged:
                values['rebalance_cycle_seconds'] = self._number(merged['rebalance_cycle_seconds'],1,365*86400,integer=True)
            if 'market_condition' in values and values['market_condition']:
                self.bot.market_mgr._normalize_market_condition(values['market_condition'])
            self._validate_variant(conn, category, merged, existing, extra)
            if category == 'future' or category in BOND_FEATURES:
                underlying = merged.get('underlying_code') if category=='future' else merged.get('issuer_code')
                if not conn.execute("SELECT 1 FROM company WHERE company_code=? AND status='active'",(underlying,)).fetchone():
                    raise ValueError("활성 회사 주식 코드를 기초/발행 회사로 입력해주세요.")
                if conn.execute('SELECT quote_currency FROM company WHERE company_code=?',(underlying,)).fetchone()[0]!=base_currency_code(conn):
                    raise ValueError('현재 선물·회사채 발행은 기준 통화 결제 회사만 지원합니다.')
                if not 1 <= int(merged.get('maturity_days',0)) <= 3650:
                    raise ValueError("만기일수는 1~3650일입니다.")
                if operation=='create':
                    values['maturity_epoch'] = int(time.time())+int(merged['maturity_days'])*86400
                elif any(k in extra for k in ('underlying_code','issuer_code','maturity_days','face_value','contract_size','maturity_epoch',
                                              'conversion_price','warrant_exercise_price')):
                    raise ValueError("발행된 파생·채권 계약 조건은 변경할 수 없습니다. 새 상품을 생성해주세요.")
            if category=='currencyproduct' and not conn.execute("SELECT 1 FROM currencies WHERE code=? AND status='active'",(merged.get('quote_currency'),)).fetchone():
                raise ValueError("활성 통화 코드를 입력해주세요.")
            if category=='currencyproduct' and existing and 'quote_currency' in extra and extra['quote_currency']!=existing['quote_currency']:
                raise ValueError("외환통장의 통화는 변경할 수 없습니다. 새 상품을 생성해주세요.")
            if category=='currencyproduct' and existing and 'interest_rate' in extra:
                for account in conn.execute("SELECT DISTINCT user_id FROM accounts WHERE account_type='currency_saving' AND product_code=?",(code,)).fetchall():
                    self.bot.currency_mgr._settle(conn,account['user_id'],time.time())
            if category=='currency':
                if code==base_currency_code(conn) and any(k!='name' for k in values):
                    raise ValueError("기준 통화 기준 통화의 환율은 고정됩니다.")
                if code!=base_currency_code(conn) and not conn.execute("SELECT 1 FROM market_assets WHERE code=? AND type='forex' AND status='active'",(merged.get('asset_code'),)).fetchone():
                    raise ValueError("먼저 $상품생성에서 type=forex 자산을 생성해주세요.")
            if category == 'dr':
                if operation != 'create':
                    immutable = {'underlying_code','underlying_per_receipt','receipt_type',
                                 'depositary_bank','custodian','home_exchange_id','quote_currency',
                                 'exchange_id','type'}
                    if immutable & set(extra) or supply:
                        raise ValueError('DR 프로그램의 기초·예탁 비율·발행량 조건은 고정됩니다.')
                    if merged.get('type') != 'depositary_receipt':
                        raise ValueError('DR 자산 유형은 변경할 수 없습니다.')
                else:
                    underlying = str(extra.get('underlying_code') or '').strip()
                    if not underlying:
                        raise ValueError('활성 원주 underlying_code가 필요합니다.')
                    original = conn.execute(
                        "SELECT company_code,exchange_id,total_shares,status FROM company "
                        "WHERE company_code=? AND status='active' AND share_class='common'",
                        (underlying,),
                    ).fetchone()
                    if not original:
                        raise ValueError('underlying_code는 활성 보통주 회사 코드여야 합니다.')
                    ratio = self._number(extra.get('underlying_per_receipt'), 0.000001, 1_000_000)
                    dr_supply = int(values.get('total_supply') or supply or 0)
                    deposited = ratio * dr_supply
                    already_deposited = conn.execute(
                        "SELECT COALESCE(SUM(deposited_underlying_quantity),0) "
                        "FROM depositary_receipts WHERE underlying_code=? AND program_status<>'terminated'",
                        (underlying,),
                    ).fetchone()[0]
                    if (dr_supply <= 0 or already_deposited + deposited
                            > int(original['total_shares'] or 0)):
                        raise ValueError('활성 DR 프로그램의 최대 예탁 가능량 합계가 원주 발행량을 초과합니다.')
                    listing_exchange = str(values.get('exchange_id') or '').strip()
                    currency = str(values.get('quote_currency') or base_currency_code(conn)).upper()
                    if not listing_exchange:
                        raise ValueError('DR 상장 거래소 exchange_id가 필요합니다.')
                    if not conn.execute("SELECT 1 FROM currencies WHERE code=? AND status='active'", (currency,)).fetchone():
                        raise ValueError('활성 DR 결제 통화를 입력해주세요.')
                    conn.execute(
                        "INSERT INTO depositary_receipts(dr_code,underlying_code,receipt_type,depositary_bank,"
                        "custodian,home_exchange_id,listing_exchange_id,quote_currency,underlying_per_receipt,"
                        "receipt_supply,deposited_underlying_quantity) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (code, underlying, str(extra.get('receipt_type') or 'DR')[:20],
                         str(extra.get('depositary_bank') or '')[:100] or None,
                         str(extra.get('custodian') or '')[:100] or None,
                         str(extra.get('home_exchange_id') or original['exchange_id'] or '')[:40] or None,
                         listing_exchange, currency, ratio, dr_supply, deposited),
                    )
            if operation=='create':
                fields = ','.join(f'"{k}"' for k in values)
                conn.execute(f'INSERT INTO "{table}" ({fields}) VALUES ({",".join("?" for _ in values)})',tuple(values.values()))
            elif values:
                assignments = ','.join(f'"{k}"=?' for k in values)
                conn.execute(f'UPDATE "{table}" SET {assignments} WHERE "{key}"=?',(*values.values(),code))
                if 'dividend_is_active' in values and values['dividend_is_active'] != existing['dividend_is_active']:
                    # The trigger also protects direct DB edits; restore the explicit admin schedule.
                    conn.execute(f'UPDATE "{table}" SET dividend_next_epoch=? WHERE "{key}"=?', (values['dividend_next_epoch'],code))
            if category=='stock' and operation=='create' and share_class=='common':
                # Open the capital account with the listing, so the company shows on every
                # account screen from the start instead of after its first trade.
                from managers.company_account_manager import CompanyAccounts
                CompanyAccounts.account_id(conn, code)
            if category=='stock' and values.get('dividend_funding',merged.get('dividend_funding'))=='issuer':
                owner=values.get('parent_company_code',merged.get('parent_company_code')) if share_class=='preferred' else code
                conn.execute('INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)',(owner,))
            if category=='currency' and extra.get('nationality'):
                conn.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES (?,?) "
                             "ON CONFLICT(nationality) DO UPDATE SET currency_code=excluded.currency_code",(str(extra['nationality']),code))
            if category=='indexfund' and 'fund_category' in extra:
                from managers.investment_manager import InvestmentManager
                if InvestmentManager.normalize_fund_category(extra['fund_category']) != 'equity':
                    raise ValueError('현재 인덱스 펀드는 주식 거래소 지수만 추종하므로 주식형만 지원합니다.')
            if category=='fund' and (operation=='create' or 'components' in extra or 'fund_category' in extra):
                from managers.investment_manager import InvestmentManager
                if 'components' in extra or operation == 'create':
                    weights = extra.get('components',EXAMPLES['fund']['components'])
                else:
                    weights = {
                        row['asset_code']: row['weight'] for row in conn.execute(
                            "SELECT asset_code,weight FROM fund_components WHERE product_code=?", (code,)
                        ).fetchall()
                    }
                requested = extra.get('fund_category')
                if requested is None and existing:
                    requested = existing['fund_category']
                inferred, components = InvestmentManager.validate_fund_components(
                    conn, weights, requested, fund_code=code
                )
                if operation != 'create' and 'components' in extra:
                    # Re-anchor at the current NAV so replacing the reference basket
                    # cannot reset a live fund to its original issue price.
                    conn.execute("UPDATE investment SET nav_anchor=unit_price WHERE product_code=?", (code,))
                conn.execute("UPDATE investment SET fund_category=? WHERE product_code=?", (inferred, code))
                if operation=='create' or 'components' in extra:
                    conn.execute("DELETE FROM fund_components WHERE product_code=?",(code,))
                    conn.executemany(
                        "INSERT INTO fund_components "
                        "(product_code,asset_code,weight,base_price,asset_source,asset_class) "
                        "VALUES (?,?,?,?,?,?)",
                        [(code, asset, weight, price, source, asset_class)
                         for asset, weight, price, source, asset_class in components],
                    )
            if category in {'fund', 'indexfund'} and operation == 'create':
                from managers.fund_manager import FundManager
                FundManager.create_empty(conn, code)
            self._audit(conn,actor,f'{category}.{operation}',{'code':code,**values})
            return '✅ 설정을 저장했습니다.'

    @staticmethod
    def _validate_variant(conn, category, merged, existing, extra):
        """Terms specific to ETN, hybrid/asset-backed bonds and CMA/IMA/wrap accounts."""
        if category == 'etn':
            issuer = conn.execute("SELECT share_class FROM company WHERE company_code=? AND status='active'",
                                  (merged.get('issuer_code'),)).fetchone()
            if not issuer or issuer['share_class'] == 'preferred':
                raise ValueError('ETN의 issuer_code에는 발행 증권사(활성 보통주 법인) 코드를 입력해주세요.')
            if not merged.get('underlying_exchange_id'):
                raise ValueError('ETN은 추종할 거래소 지수(underlying_exchange_id)가 필요합니다.')
            if existing and 'issuer_code' in extra and extra['issuer_code'] != existing['issuer_code']:
                raise ValueError('ETN 발행사는 변경할 수 없습니다.')
        if category == 'convertiblebond' and int(merged.get('conversion_price') or 0) <= 0:
            raise ValueError('전환사채는 전환가격(conversion_price)이 필요합니다.')
        if category == 'warrantbond' and int(merged.get('warrant_exercise_price') or 0) <= 0:
            raise ValueError('신주인수권부사채는 행사가격(warrant_exercise_price)이 필요합니다.')
        if category == 'abcp' and int(merged.get('maturity_days') or 0) > 365:
            raise ValueError('ABCP(자산유동화기업어음)는 만기 365일 이하 단기물입니다.')
        if category in {'abs', 'mbs', 'abcp'} and not str(merged.get('collateral_note') or '').strip():
            raise ValueError('자산유동화증권은 기초자산 풀 설명(collateral_note)이 필요합니다.')
        if category == 'ima':
            from managers.investment_manager import InvestmentManager
            if not InvestmentManager.component_quote(conn, merged.get('reference_code') or ''):
                raise ValueError('IMA는 성과를 연동할 활성 종목·펀드(reference_code)가 필요합니다.')
        if category == 'wrap':
            manager = merged.get('manager_id')
            if not manager or not conn.execute("SELECT 1 FROM users WHERE user_id=?", (str(manager),)).fetchone():
                raise ValueError('랩어카운트는 운용 담당자(manager_id) 사용자 ID가 필요합니다.')

    def settings(self,actor,changes):
        with self.db.atomic(('system','users','pot')) as conn:
            self.require(actor,conn)
            settings = {r['key']:r['value'] for r in conn.execute('SELECT * FROM settings')}
            allowed = {k for k in settings if k.startswith(('garch_','auto_target_'))} | {'market_volatility','market_condition','currency_exchange_fee_percent','base_rate','tax_percent','gamble_chance','gamble_return'}
            if not isinstance(changes,dict) or not changes or set(changes)-allowed:
                raise ValueError("허용된 시장·금리·세율·도박 설정 키를 입력해주세요.")
            updated = {**settings,**{k:str(v) for k,v in changes.items()}}
            vol = self._number(updated.get('market_volatility',.3),0,1)
            GarchConfig.from_settings(updated,volatility=vol)
            AutoTargetConfig.from_settings(updated)
            self.bot.market_mgr._normalize_market_condition(updated.get('market_condition','normal'))
            for key,value in changes.items():
                if key == 'base_rate':
                    self._number(value,-100,100)
                if key in {'currency_exchange_fee_percent','tax_percent','gamble_chance'}:
                    self._number(value,0,100)
                if key=='gamble_return':
                    self._number(value,0,10000)
                conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))
            self._audit(conn,actor,'settings',changes)
        return '✅ 경제 설정을 저장했습니다.'

    def user(self,actor,user_id,changes):
        changes=dict(changes or {})
        moved=None
        if 'nationality' in changes:
            with self.db.readonly() as conn:
                self.require(actor,conn)
                current=conn.execute("SELECT nationality FROM users WHERE user_id=?",(user_id,)).fetchone()
            if not current or user_id==TREASURY_USER_ID:
                raise ValueError("일반 사용자를 찾을 수 없습니다. 국고 자금은 $국고를 사용해주세요.")
            nationality=changes.pop('nationality')
            if nationality!=current['nationality']:
                # Liquidates everything into the new home currency (managers/nationality_manager.py).
                from managers.nationality_manager import NationalityManager
                moved=NationalityManager(self.bot).change(actor,user_id,nationality)
            if not changes:
                return moved or '✅ 사용자 설정을 저장했습니다.'
        result=self._update_user(actor,user_id,changes)
        return f"{moved}\n{result}" if moved else result

    def _update_user(self,actor,user_id,changes):
        with self.db.atomic(('users','system')) as conn:
            self.require(actor,conn)
            if not changes or set(changes)-{'nickname','nationality','cash','credit_score','credit_score_reason','annual_income','income_stability','status','role'}:
                raise ValueError("허용된 사용자 필드를 입력해주세요.")
            user = conn.execute("SELECT * FROM users WHERE user_id=?",(user_id,)).fetchone()
            if not user or user_id==TREASURY_USER_ID:
                raise ValueError("일반 사용자를 찾을 수 없습니다. 국고 자금은 $국고를 사용해주세요.")
            if 'nationality' in changes and not conn.execute("SELECT 1 FROM country_currencies WHERE nationality=?",(changes['nationality'],)).fetchone():
                raise ValueError("$currency에서 통화와 연결된 국적을 입력해주세요.")
            if 'nationality' in changes and changes['nationality'] != user['nationality']:
                old = conn.execute('SELECT currency_code FROM country_currencies WHERE nationality=?',(user['nationality'],)).fetchone()
                new = conn.execute('SELECT currency_code FROM country_currencies WHERE nationality=?',(changes['nationality'],)).fetchone()
                if old and new and old[0] != new[0]:
                    if float(user['cash'] or 0):
                        raise ValueError('국적 통화를 변경하려면 기존 cash 잔고를 먼저 0으로 정산해주세요.')
                    account = conn.execute("SELECT balance_units FROM accounts WHERE user_id=? AND quote_currency=? AND account_type='currency_saving'",(user_id,new[0])).fetchone()
                    if account and int(account[0] or 0):
                        raise ValueError('새 국적 통화의 외환통장 잔액을 먼저 0으로 정산해주세요.')
            credit_reason = changes.pop('credit_score_reason', None)
            credit_target = None
            if 'credit_score' in changes:
                # Corrections are credit events: the score keeps evolving from
                # the corrected value instead of being frozen by an overwrite.
                credit_target = self._number(
                    changes.pop('credit_score'), 0, 1000, integer=True
                )
            target_nationality = changes.get('nationality', user['nationality'])
            currency_row = conn.execute(
                "SELECT currency_code FROM country_currencies WHERE nationality=?",
                (target_nationality,),
            ).fetchone()
            currency = str(currency_row[0]) if currency_row else base_currency_code(conn)
            for key in ('cash','annual_income'):
                if key in changes:
                    changes[key] = parse_currency_money(
                        conn, changes[key], currency, minimum=0
                    )
            if 'income_stability' in changes:
                changes['income_stability'] = self._number(
                    changes['income_stability'], 0, 1
                )
            # Company accounts keep their 'company' role; everyone else picks a settable one.
            role = changes.get('role', user['role'])
            if user['role'] == 'company' and role != 'company':
                raise ValueError("법인 계정의 권한은 바꿀 수 없습니다.")
            if (user['role'] != 'company' and role not in {'admin','user','nation'}) \
                    or changes.get('status',user['status']) not in {'active','inactive','bankrupt'}:
                raise ValueError("role/status 값을 확인해주세요.")
            if user_id==str(actor) and (changes.get('role')=='user' or changes.get('status') in {'inactive','bankrupt'}):
                raise ValueError("현재 관리자는 자신의 관리 권한을 제거할 수 없습니다.")
            if changes:
                assignments = ','.join(f'"{k}"=?' for k in changes)
                conn.execute(f'UPDATE users SET {assignments} WHERE user_id=?',(*changes.values(),user_id))
            if credit_target is not None:
                CreditScoreManager.set_score(conn,user_id,credit_target,actor,credit_reason)
                changes['credit_score'] = credit_target
                changes['credit_score_reason'] = credit_reason or '관리자 정정'
            elif 'annual_income' in changes or 'income_stability' in changes:
                CreditScoreManager.refresh(conn,user_id,trigger='income_update')
            self._audit(conn,actor,'user',{'user_id':user_id,**changes})
        return '✅ 사용자 설정을 저장했습니다.'

    def setup(self,actor,user_id,nickname,nationality):
        with self.db.atomic() as conn:
            self.require(actor,conn)
            if not str(user_id).isdecimal():
                raise ValueError('Discord 사용자 ID를 입력해주세요.')
            conn.execute("INSERT INTO users(user_id,nickname,nationality,cash,role) VALUES (?,?,?,1000000,'user') "
                         "ON CONFLICT(user_id) DO UPDATE SET nickname=excluded.nickname",
                         (str(user_id),nickname or str(user_id),nationality or '낙원'))
            self._audit(conn,actor,'setup',{'user_id':str(user_id),'nationality':nationality})
        return '✅ 사용자를 등록하거나 닉네임을 갱신했습니다.'

    def register(self,actor,members):
        with self.db.atomic() as conn:
            self.require(actor,conn)
            count=0
            for member in members:
                if member.bot:
                    continue
                conn.execute("INSERT INTO users(user_id,nickname,nationality,cash,role) VALUES (?,?,'낙원',1000000,'user') "
                             "ON CONFLICT(user_id) DO UPDATE SET nickname=excluded.nickname",(str(member.id),str(member.display_name)))
                count+=1
            self._audit(conn,actor,'register',{'members':count})
        return f'✅ 서버 시민 {count}명을 등록하거나 닉네임을 갱신했습니다.'
