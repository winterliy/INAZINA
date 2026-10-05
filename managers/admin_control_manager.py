"""Validated administrator forms; no arbitrary SQL execution surface."""
import math
import time
from contextlib import nullcontext
from database.user_db import TREASURY_USER_ID
from managers.stochastic_price_engine import GarchConfig
from managers.auto_target_engine import AutoTargetConfig
from database.db_manager import DBManager

ENTITIES = {
    'stock': ('company','company_code','share_price','total_shares'),
    'asset': ('market_assets','code','price','total_supply'),
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
    'fxproduct': ('fx_products','product_code',None,None),
}
KINDS = {'etf':'etf','future':'stock_future','corporatebond':'corporate_bond',
         'fund':'mutual_fund','indexfund':'index_fund','commodity':'commodity_spot','els':'els'}
EXAMPLES = {
    'stock': {'industry':'소프트웨어','exchange_id':'INZ','capital':1000000},
    'asset': {'exchange_id':'INZ','type':'stock'},
    'etf': {'exchange_id':'INZ','underlying_exchange_id':'INZ','leverage_multiplier':1},
    'future': {'exchange_id':'FUT','underlying_code':'INZST001','maturity_days':90,'contract_size':1},
    'corporatebond': {'exchange_id':'BOND','issuer_code':'INZST001','face_value':1000,'annual_rate':4,'maturity_days':90},
    'fund': {'exchange_id':'INZ','components':{'INZST001':.5,'INZST002':.5}},
    'indexfund': {'exchange_id':'INZ','underlying_exchange_id':'INZ'},
    'commodity': {'exchange_id':'COM'},
    'bond': {'annual_rate':3,'maturity_days':90,'face_value':1000,'exchange_id':'BOND'},
    'saving': {'interest_rate':3,'deposit_amount':1000,'maturity_days':30,'interest_cycle':86400,'compound_type':1,'issuer':'Inazina'},
    'loan': {'interest_rate':5,'loan_limit':1000000,'maturity_days':30,'interest_cycle':86400,'min_credit_score':500,'issuer':'Inazina','repayment_cycle_seconds':0,'repayment_amount':0},
    'option': {'underlying_code':'INZST001','option_type':'call','strike_price':1120,'maturity_days':30,'quantity':1,'limit_price':100,'volatility_percent':60},
    'currency': {'asset_code':'FX_NEW','nationality':'새 국가'},
    'fxproduct': {'currency_code':'USD','annual_rate':1},
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
        if category not in ENTITIES or operation not in {'create','edit','delete','restore'}:
            raise ValueError("지원하는 설정을 선택해주세요.")
        code = str(code).strip()
        if category=='currency':
            code=code.upper()
        if (not code and not (category=='option' and operation=='create')) or len(code)>80:
            raise ValueError("코드는 1~80자로 입력해주세요.")
        extra = dict(extra or {})
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
            result = self.bot.option_mgr.create_and_write(TREASURY_USER_ID,**extra)
            if not result[0]:
                raise ValueError(result[1])
            return result[1]
        table,key,price_key,supply_key = ENTITIES[category]
        with (self.db.atomic() if conn is None else nullcontext(conn)) as conn:
            self.require(actor,conn)
            existing = conn.execute(f'SELECT * FROM "{table}" WHERE "{key}"=?',(code,)).fetchone()
            if category in KINDS and existing and existing['product_type']!=KINDS[category]:
                raise ValueError("이 카테고리의 상품 코드가 아닙니다.")
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
                    reservations = conn.execute("SELECT 1 FROM market_orders WHERE asset_code=? AND status IN ('open','partial') LIMIT 1",(code,)).fetchone()
                    claims = conn.execute("SELECT 1 FROM option_contracts WHERE (contract_code=? OR underlying_code=?) AND status='active' LIMIT 1",(code,code)).fetchone()
                    if reservations or claims:
                        raise ValueError("미체결 주문·옵션 의무가 남아 있습니다. 주문 취소와 만기 정산 후 삭제해주세요.")
                    if category in {'corporatebond','future'} and (
                        conn.execute("SELECT 1 FROM user_assets WHERE asset_code=? LIMIT 1",(code,)).fetchone()
                        or conn.execute("SELECT 1 FROM futures_positions WHERE product_code=? LIMIT 1",(code,)).fetchone()):
                        raise ValueError("미상환 채권·선물 포지션을 먼저 정산해주세요.")
                    if category=='currency' and conn.execute("SELECT 1 FROM fx_accounts WHERE currency_code=? AND balance_units>0",(code,)).fetchone():
                        raise ValueError("보유 잔액이 있는 통화는 삭제할 수 없습니다.")
                    currency = code if category=='currency' else (conn.execute('SELECT code FROM currencies WHERE asset_code=?',(code,)).fetchone() or [None])[0] if category=='asset' else None
                    if currency and currency!='NWX' and any(conn.execute(f'SELECT 1 FROM {table} WHERE quote_currency=? LIMIT 1',(currency,)).fetchone() for table in ('company','investment','market_assets')):
                        raise ValueError('이 통화로 결제하는 종목이 있어 통화·환율을 삭제할 수 없습니다.')
                    if category=='asset' and conn.execute("SELECT 1 FROM currencies c JOIN fx_accounts a ON a.currency_code=c.code WHERE c.asset_code=? AND a.balance_units>0",(code,)).fetchone():
                        raise ValueError("외환통장 잔액이 있는 환율 종목은 삭제할 수 없습니다.")
                    if category=='currency' and code=='NWX':
                        raise ValueError("기준 통화 NWX는 삭제할 수 없습니다.")
                # Preserve historical claims/holdings and payout definitions.
                conn.execute(f'UPDATE "{table}" SET status=? WHERE "{key}"=?',('inactive' if operation=='delete' else 'active',code))
                return '✅ 거래 중지 처리했습니다.' if operation=='delete' else '✅ 거래를 재개했습니다.'
            if operation=='create' and (existing or (category!='currency' and self.db.code_locations(conn,code))):
                raise ValueError("이미 사용 중인 코드입니다.")
            if operation=='edit' and not existing:
                raise ValueError("수정할 코드를 찾을 수 없습니다.")
            columns = {r['name'] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            protected = {key,'created_at','created_epoch','sold_amount','writer_id','contract_id','issued_contracts','status','product_type'}
            virtual = {'nationality'} if category=='currency' else {'components'} if category=='fund' else set()
            if set(extra)-columns-virtual:
                raise ValueError(f"지원하지 않는 필드: {', '.join(sorted(set(extra)-columns-virtual))}")
            if set(extra)&protected:
                raise ValueError("계약 식별자·발행 의무·판매액·상태는 일반 수정 대상이 아닙니다.")
            if category=='option' and operation=='edit':
                if set(extra)-{'implied_volatility'} or price or supply:
                    raise ValueError("옵션 계약 조건은 고정됩니다. 내재변동성 수정 또는 $사건으로 프리미엄을 조정해주세요.")
            values = {k:v for k,v in extra.items() if k in columns}
            if category=='currency':
                if operation=='create' and 'decimal_places' not in values:
                    values['decimal_places']=0 if code in {'NWX','KRW','JPY'} else 2 if code in {'USD','EUR','GBP','CHF','CNY','HKD','AUD','CAD'} else 6
                if 'decimal_places' in values:
                    values['decimal_places']=self._number(values['decimal_places'],0,6,integer=True)
                    if code=='NWX' and values['decimal_places']!=0:raise ValueError('NWX는 정수 단위입니다.')
                from fractions import Fraction
                for field in ('reference_rate','rate_multiplier'):
                    if field in values and values[field] is not None:
                        number=Fraction(str(values[field]))
                        if number<=0:raise ValueError('통화 환산 기준은 양수로 입력해주세요.')
                        values[field]=str(number)
            if 'currency_code' in values:
                values['currency_code']=str(values['currency_code']).strip().upper()
            if 'quote_currency' in values:
                values['quote_currency']=str(values['quote_currency']).strip().upper()
                if category not in {'stock','asset','commodity'} and values['quote_currency']!='NWX':
                    raise ValueError('외화 결제 종목은 주식·현물 원자재만 지원합니다.')
                if not conn.execute("SELECT 1 FROM currencies WHERE code=? AND status='active'",(values['quote_currency'],)).fetchone():
                    raise ValueError('활성 결제 통화를 입력해주세요.')
                if category=='asset' and values['quote_currency']!='NWX' and extra.get('type',(dict(existing) if existing else {}).get('type','stock')) not in {'stock','주식','commodity_spot'}:
                    raise ValueError('외화 결제 자산은 주식·현물 원자재만 지원합니다.')
                if existing and values['quote_currency']!=existing['quote_currency']:
                    raise ValueError('기존 종목의 결제 통화는 변경할 수 없습니다. 새 종목을 생성해주세요.')
            if name and 'name' in columns:
                values['name'] = name[:100]
            if price and price_key:
                values[price_key] = self._number(price,1,integer=True)
            if supply and supply_key:
                values[supply_key] = self._number(supply,1,integer=True)
            if operation=='create':
                values[key] = code
                values.update({k:v for k,v in EXAMPLES[category].items() if k in columns and k not in values})
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
            merged = {**(dict(existing) if existing else {}),**values}
            if 'Fractal' in values or merged.get('Fractal') == 1:
                from functions.quantities import validate_fractional_setting
                asset_type = 'stock' if category == 'stock' else KINDS.get(category, merged.get('type'))
                enabled = values.get('Fractal', merged.get('Fractal', 0))
                enabled = validate_fractional_setting(conn, table, key, code, enabled, asset_type, merged.get(supply_key))
                if 'Fractal' in values:
                    values['Fractal'] = enabled
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
                    if parent['quote_currency']!=merged.get('quote_currency','NWX'):
                        raise ValueError('우선주와 소속 법인의 결제 통화가 같아야 합니다.')
                    if operation=='create' and 'industry' not in extra:
                        values['industry']=parent['industry']
                    if operation=='create':
                        values.setdefault('dividend_priority',1)
                        values.setdefault('liquidation_value_per_share',values.get('share_price',merged.get('share_price',0)))
                if merged.get('dividend_funding','treasury') not in {'treasury','issuer'}:
                    raise ValueError('dividend_funding은 treasury(국고) 또는 issuer(법인현금)입니다.')
                if merged.get('dividend_funding')=='issuer' and merged.get('quote_currency','NWX')!='NWX':
                    raise ValueError('법인현금 재원 배당은 NWX 결제 회사만 지원합니다.')
                for field,maximum in (('dividend_priority',1000),('cumulative_dividend',1),('liquidation_value_per_share',2**53-1)):
                    if field in values:values[field]=self._number(values[field],0,maximum,integer=True)
                if share_class=='common' and (int(values.get('dividend_priority',merged.get('dividend_priority',0))) or int(values.get('cumulative_dividend',merged.get('cumulative_dividend',0)))):
                    raise ValueError('배당 우선순위·누적배당은 우선주에만 설정할 수 있습니다.')
            for field,maximum in (('dividend_cycle_seconds',10*365*86400),('dividend_per_share',2**53-1),
                                  ('dividend_is_active',1),('extension_min_credit_score',1000),('extension_days',3650)):
                if field in values:
                    values[field]=self._number(values[field],0,maximum,integer=True)
            if 'dividend_next_epoch' in extra:
                raise ValueError('다음 배당 시각은 배당 주기로 자동 관리합니다.')
            if any(field in values for field in ('dividend_cycle_seconds','dividend_per_share','dividend_is_active','dividend_mode','dividend_percent')):
                from managers.corporate_actions_manager import CorporateActionsManager
                dividend_type = 'stock' if category == 'stock' else KINDS.get(category, merged.get('type'))
                if dividend_type not in CorporateActionsManager.DIVIDEND_TYPES:
                    raise ValueError('정기배당은 주식·우선주·ETF·펀드·현물 상품만 지원합니다.')
                cycle=int(values.get('dividend_cycle_seconds',merged.get('dividend_cycle_seconds',0)))
                amount=int(values.get('dividend_per_share',merged.get('dividend_per_share',0)))
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
            for field in (price_key,supply_key):
                if field and field in values and values[field] is not None:
                    values[field]=self._number(values[field],1,integer=True)
            if existing and supply_key and supply_key in values:
                issued = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE asset_code=?",(code,)).fetchone()[0]
                issued += conn.execute("SELECT COALESCE(SUM(quantity),0) FROM option_collateral_lots WHERE underlying_code=?",(code,)).fetchone()[0]
                if values[supply_key] < issued:
                    raise ValueError("발행량을 이미 보유·담보 설정된 수량보다 작게 줄일 수 없습니다.")
            if category in {'etf','indexfund'} and merged.get('underlying_exchange_id'):
                exchange = merged['underlying_exchange_id']
                if not conn.execute("SELECT 1 FROM company WHERE exchange_id=? AND status='active' UNION SELECT 1 FROM market_assets WHERE exchange_id=? AND status='active' AND type IN ('stock','주식')",(exchange,exchange)).fetchone():
                    raise ValueError("기초 지수에 사용할 주식 거래소를 확인해주세요.")
            if 'extension_interest_rate' in values and values['extension_interest_rate'] is not None:
                values['extension_interest_rate']=self._number(values['extension_interest_rate'],0,100)
            for field in ('interest_rate','annual_rate'):
                if field in merged:
                    values[field] = self._number(merged[field],0,100)
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
            if category in {'future','corporatebond'}:
                underlying = merged.get('underlying_code') if category=='future' else merged.get('issuer_code')
                if not conn.execute("SELECT 1 FROM company WHERE company_code=? AND status='active'",(underlying,)).fetchone():
                    raise ValueError("활성 회사 주식 코드를 기초/발행 회사로 입력해주세요.")
                if conn.execute('SELECT quote_currency FROM company WHERE company_code=?',(underlying,)).fetchone()[0]!='NWX':
                    raise ValueError('현재 선물·회사채 발행은 NWX 결제 회사만 지원합니다.')
                if not 1 <= int(merged.get('maturity_days',0)) <= 3650:
                    raise ValueError("만기일수는 1~3650일입니다.")
                if operation=='create':
                    values['maturity_epoch'] = int(time.time())+int(merged['maturity_days'])*86400
                elif any(k in extra for k in ('underlying_code','issuer_code','maturity_days','face_value','contract_size','maturity_epoch')):
                    raise ValueError("발행된 파생·채권 계약 조건은 변경할 수 없습니다. 새 상품을 생성해주세요.")
            if category=='fxproduct' and not conn.execute("SELECT 1 FROM currencies WHERE code=? AND status='active'",(merged.get('currency_code'),)).fetchone():
                raise ValueError("활성 통화 코드를 입력해주세요.")
            if category=='fxproduct' and existing and 'currency_code' in extra and extra['currency_code']!=existing['currency_code']:
                raise ValueError("외환통장의 통화는 변경할 수 없습니다. 새 상품을 생성해주세요.")
            if category=='fxproduct' and existing and 'annual_rate' in extra:
                for account in conn.execute("SELECT DISTINCT user_id FROM fx_accounts WHERE product_code=?",(code,)).fetchall():
                    self.bot.fx_mgr._settle(conn,account['user_id'],time.time())
            if category=='currency':
                if merged.get('asset_code'):
                    if not conn.execute("SELECT 1 FROM market_assets WHERE code=? AND type='forex' AND status='active'",(merged['asset_code'],)).fetchone():
                        raise ValueError('활성 type=forex 시세 자산을 입력해주세요.')
                elif merged.get('reference_rate') is None:
                    raise ValueError('시세 자산 asset_code 또는 양수 reference_rate를 지정해주세요.')
            if category=='loan' and operation=='edit' and any(field in values and values[field]!=existing[field] for field in ('interest_rate','extension_interest_rate')):
                from managers.interest_manager import InterestManager
                InterestManager(self.db).settle_product_rate_boundary(conn,code,time.time())
            if operation=='create':
                fields = ','.join(f'"{k}"' for k in values)
                conn.execute(f'INSERT INTO "{table}" ({fields}) VALUES ({",".join("?" for _ in values)})',tuple(values.values()))
            elif values:
                assignments = ','.join(f'"{k}"=?' for k in values)
                conn.execute(f'UPDATE "{table}" SET {assignments} WHERE "{key}"=?',(*values.values(),code))
                if 'dividend_is_active' in values and values['dividend_is_active'] != existing['dividend_is_active']:
                    # The trigger also protects direct DB edits; restore the explicit admin schedule.
                    conn.execute(f'UPDATE "{table}" SET dividend_next_epoch=? WHERE "{key}"=?', (values['dividend_next_epoch'],code))
            if category=='stock' and values.get('dividend_funding',merged.get('dividend_funding'))=='issuer':
                owner=values.get('parent_company_code',merged.get('parent_company_code')) if share_class=='preferred' else code
                conn.execute('INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)',(owner,))
            if category=='currency' and extra.get('nationality'):
                conn.execute("INSERT INTO country_currencies VALUES (?,?) ON CONFLICT(nationality) DO UPDATE SET currency_code=excluded.currency_code",(str(extra['nationality']),code))
            if category=='fund' and (operation=='create' or 'components' in extra):
                weights = extra.get('components',EXAMPLES['fund']['components'])
                if not isinstance(weights,dict) or not weights:
                    raise ValueError("components에 종목별 비중을 입력해주세요.")
                total = sum(self._number(v,.000001,1) for v in weights.values())
                if not math.isclose(total,1,abs_tol=1e-8):
                    raise ValueError("펀드 구성 비중의 합은 1이어야 합니다.")
                conn.execute("DELETE FROM fund_components WHERE product_code=?",(code,))
                for asset,weight in weights.items():
                    underlying = conn.execute("SELECT share_price FROM company WHERE company_code=? AND status='active'",(asset,)).fetchone()
                    if not underlying:
                        raise ValueError("펀드 구성 종목을 확인해주세요.")
                    conn.execute("INSERT INTO fund_components VALUES (?,?,?,?)",(code,asset,weight,underlying[0]))
            return '✅ 설정을 저장했습니다.'

    def settings(self,actor,changes):
        with self.db.atomic(('system','users','tax','pot')) as conn:
            self.require(actor,conn)
            settings = {r['key']:r['value'] for r in conn.execute('SELECT * FROM settings')}
            allowed = {k for k in settings if k.startswith(('garch_','auto_target_'))} | {'market_volatility','market_condition','fx_fee_percent','base_rate','statutory_rate','tax_percent','gamble_chance','gamble_return'}
            if not isinstance(changes,dict) or not changes or set(changes)-allowed:
                raise ValueError("허용된 시장·금리·세율·도박 설정 키를 입력해주세요.")
            updated = {**settings,**{k:str(v) for k,v in changes.items()}}
            vol = self._number(updated.get('market_volatility',.3),0,1)
            GarchConfig.from_settings(updated,volatility=vol)
            AutoTargetConfig.from_settings(updated)
            self.bot.market_mgr._normalize_market_condition(updated.get('market_condition','normal'))
            for key,value in changes.items():
                if key in {'base_rate','statutory_rate','fx_fee_percent','tax_percent','gamble_chance'}:
                    self._number(value,0,100)
                if key=='gamble_return':
                    self._number(value,0,10000)
                if key in {'tax_percent','gamble_chance','gamble_return'}:
                    table,column = {'tax_percent':('tax_settings','rate_percent'),'gamble_chance':('probability','chance'),'gamble_return':('return','return_percent')}[key]
                    conn.execute(f'UPDATE "{table}" SET "{column}"=? WHERE id=1',(value,))
                else:
                    conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))
        return '✅ 경제 설정을 저장했습니다.'

    def user(self,actor,user_id,changes):
        with self.db.atomic(('users','system')) as conn:
            self.require(actor,conn)
            if not changes or set(changes)-{'nickname','nationality','cash','credit_score','status','role'}:
                raise ValueError("허용된 사용자 필드를 입력해주세요.")
            user = conn.execute("SELECT * FROM users WHERE user_id=?",(user_id,)).fetchone()
            if not user or user_id==TREASURY_USER_ID:
                raise ValueError("일반 사용자를 찾을 수 없습니다. 국고 자금은 $국고를 사용해주세요.")
            if 'nationality' in changes and not conn.execute("SELECT 1 FROM country_currencies WHERE nationality=?",(changes['nationality'],)).fetchone():
                raise ValueError("$currency에서 통화와 연결된 국적을 입력해주세요.")
            for key in ('cash','credit_score'):
                if key in changes:
                    changes[key] = self._number(changes[key],0,1000 if key=='credit_score' else 2**53-1,integer=True)
            if changes.get('role',user['role']) not in {'admin','user'} or changes.get('status',user['status']) not in {'active','inactive','bankrupt'}:
                raise ValueError("role/status 값을 확인해주세요.")
            if user_id==str(actor) and (changes.get('role')=='user' or changes.get('status') in {'inactive','bankrupt'}):
                raise ValueError("현재 관리자는 자신의 관리 권한을 제거할 수 없습니다.")
            assignments = ','.join(f'"{k}"=?' for k in changes)
            conn.execute(f'UPDATE users SET {assignments} WHERE user_id=?',(*changes.values(),user_id))
        return '✅ 사용자 설정을 저장했습니다.'

    def setup(self,actor,user_id,nickname,nationality):
        with self.db.atomic() as conn:
            self.require(actor,conn)
            if not str(user_id).isdecimal():
                raise ValueError('Discord 사용자 ID를 입력해주세요.')
            conn.execute("INSERT INTO users(user_id,nickname,nationality,cash,role) VALUES (?,?,?,1000000,'user') "
                         "ON CONFLICT(user_id) DO UPDATE SET nickname=excluded.nickname",
                         (str(user_id),nickname or str(user_id),nationality or '낙원'))
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
        return f'✅ 서버 시민 {count}명을 등록하거나 닉네임을 갱신했습니다.'
