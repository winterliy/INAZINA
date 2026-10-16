"""Validated inline administrator commands; treasury trades retain their own forms."""
import json
import shlex
import time
import secrets
from decimal import Decimal, ROUND_HALF_UP
from database.db_manager import DBManager
from database.user_db import TREASURY_USER_ID
from managers.admin_control_manager import ENTITIES, KINDS
from managers.currency_exchange_manager import SCALE, MAX_UNITS, amount_units, display_units
from managers.rate_manager import RateManager
from functions.money import parse_currency_money

COMMANDS = {'상품수정','상품가격수정','상품생성','상품삭제','기본소득','잔고수정','등록','도박확률','도박리턴값','서버설치','소수점거래관리','상품이율수정',
            '시장분위기','상품분위기','복권당첨번호보기','복권당첨번호변경','세율','환율수정',
            '국가생성','국가삭제',
            '복권가격설정','금리수정','시장금리수정','법정최고이자수정','자본변경','증자','정기배당수정','정기배당활성','특별배당실시','합병','액면분할병합',
            '휴장','개장','장일시정지','장재개','서킷브레이커','사이드카','법인잔고수정','법인청산','ELS취소'}
TYPES = {'company':'stock','주식':'stock','자산':'asset','ETF':'etf','선물':'future','주식선물':'future',
         '회사채':'corporatebond','일반펀드':'fund','펀드':'fund','인덱스펀드':'indexfund',
         '현물원자재':'commodity','원자재':'commodity','국채':'bond','저축':'saving',
         '대출':'loan','옵션':'option','통화':'currency','외환통장':'currencyproduct','ELS':'els',
         'preferred':'stock','preferredstock':'stock','우선주':'stock',
         **{k:k for k in ENTITIES}, **{v:k for k,v in KINDS.items()}}
USAGE = {
 '소수점거래관리':'[id] [0:on/1:off]',
 '상품이율수정':'[대출상품id] [원이율%] [연장이율% · 0은 원이율 적용]',
 '상품수정':'[id] [상품종류] [유형별 순서형 인자 ...] [추가 JSON(선택)]',
 '상품가격수정':'[종목id] [add/del/modify] [금액]',
 '상품생성':'[id] [상품종류] [유형별 순서형 인자 ...] [추가 JSON(선택)]',
 '상품삭제':'[id]', '기본소득':'[금액]', '서버설치':'[서버ID] [국적] [통화]', '잔고수정':'[user_id/계좌번호] [add/del/modify] [금액]',
 '등록':'[user_id]', '도박확률':'[0~100]', '도박리턴값':'[리턴 퍼센트]',
 '시장분위기':'[bull/flat/bear]', '상품분위기':'[종목id] [bull/flat/bear]',
 '복권당첨번호보기':'', '복권당첨번호변경':'[서로 다른 1~45 번호 6개] [보너스 번호(선택)]',
 '세율':'[국적] [0~100]', '환율수정':'[외화A] [외화B] [1 A당 B 비율]',
 '국가생성':'[국가명] [통화코드] [기준금리%(선택)] [법정최고이자%(선택)] [세율%(선택)]',
 '국가삭제':'[국가명]',
 '복권가격설정':'[가격]', '금리수정':'[국적] [add/del/modify] [수치]',
 '시장금리수정':'[국적] [funding/bank_bond/treasury] [만기일수] [금리%]',
 '법정최고이자수정':'[국적] [add/del/modify] [수치]',
 '자본변경':'[증자/감자] [유상/무상] [종목id] [수량] [유상 가격(선택)]',
 '증자':'[유상/무상] [종목id] [수량] [유상 가격(선택)]',
 '정기배당수정':'[id] [주기: 초 또는 1일/1주/1달] [주당 금액 또는 비율%]',
 '정기배당활성':'[id] [on/off 또는 1/0]',
 '특별배당실시':'[id] [주당 배당금]', '합병':'[존속 A id] [통합 B id] [A:B 교환비율(선택)]',
 '액면분할병합':'[분할/병합] [종목id] [비율]',
 '휴장':'[거래소ID(선택)]', '개장':'[거래소ID(선택)]', '장일시정지':'[거래소ID(선택)]', '장재개':'[거래소ID(선택)]',
 '서킷브레이커':'[거래소ID(선택)] [1/2/3단계]', '사이드카':'[거래소ID(선택)]'}
USAGE.update({'법인잔고수정':'[보통주 법인id] [add/del/modify] [금액]',
              '법인청산':'[보통주 법인id]', 'ELS취소':'[ELS id]'})

# Core positional arguments vary by product type; extra JSON is always optional.
CREATE_ARGS = {
 'stock':('name','industry','capital','total_shares','share_price','exchange_id'),
 'asset':('name','type','price','total_supply','exchange_id'),
 'saving':('name','interest_rate','deposit_amount','maturity_days','interest_cycle','compound_type','issuer'),
 'loan':('name','interest_rate','loan_limit','maturity_days','interest_cycle','min_credit_score','issuer'),
 'etf':('name','unit_price','total_supply','underlying_exchange_id','leverage_multiplier','rebalance_cycle_seconds','exchange_id'),
 'future':('name','unit_price','total_supply','underlying_code','maturity_days','contract_size','initial_margin','maintenance_margin','exchange_id'),
 'corporatebond':('name','unit_price','total_supply','issuer_code','face_value','annual_rate','maturity_days','exchange_id'),
 'fund':('name','unit_price','total_supply','components','exchange_id'),
 'indexfund':('name','unit_price','total_supply','underlying_exchange_id','exchange_id'),
 'commodity':('name','unit_price','total_supply','exchange_id'),
 'bond':('name','market_price','total_supply','face_value','annual_rate','maturity_days','exchange_id'),
 'option':('underlying_code','option_type','strike_price','maturity_days','quantity','limit_price','volatility_percent'),
 'currency':('name','asset_code','nationality'),
 'currencyproduct':('name','currency_code','annual_rate'),
 'preferred':('name','parent_company_code','capital','total_shares','share_price','exchange_id'),
 'els':('name','face_value','total_supply','underlyings','annual_coupon','maturity_days',
        'observation_days','redemption_levels','maturity_level','knock_in_level','subscription_days','exchange_id'),
}
OPTIONAL_ARGS = {kind:('dividend_cycle_seconds','dividend_per_share')
                 for kind in ('stock','preferred','asset','etf','fund','indexfund','commodity')}
for _kind in tuple(OPTIONAL_ARGS):
    OPTIONAL_ARGS[_kind] += ('Fractal',)
OPTIONAL_ARGS['loan']=('extension_min_credit_score','extension_days','extension_interest_rate')
# Foreign-currency accounts accept the same optional saving settings after name/currency/rate.
OPTIONAL_ARGS['currencyproduct']=('deposit_amount','maturity_days','interest_cycle','compound_type','issuer')
for _kind,_fields in OPTIONAL_ARGS.items():
    CREATE_ARGS[_kind]+= _fields
TEXT_ARGS = {'name','industry','exchange_id','underlying_exchange_id','underlying_code','issuer_code',
             'issuer','type','option_type','asset_code','nationality','currency_code','parent_company_code',
             'fund_category'}


def product_profile(kind):
    return 'preferred' if kind.lower() in {'preferred','preferredstock','우선주'} else TYPES.get(kind,TYPES.get(kind.lower()))


def duration_seconds(value):
    import re
    match=re.fullmatch(r'(\d+)\s*(초|분|시간|일|주|달|s|m|h|d|w)?',str(value).strip())
    if not match:raise ValueError('배당 주기는 초 또는 1일/1주/1달처럼 입력해주세요. 0은 정기배당 중지입니다.')
    result=int(match[1])*{None:1,'초':1,'s':1,'분':60,'m':60,'시간':3600,'h':3600,
                         '일':86400,'d':86400,'주':604800,'w':604800,'달':2592000}[match[2]]
    if result>10*365*86400:raise ValueError('배당 주기는 10년 이하여야 합니다.')
    return result


def creation_usage(kind,command='상품생성'):
    category=TYPES.get(kind,TYPES.get(kind.lower()))
    if category is None:raise ValueError('지원하지 않는 상품종류입니다.')
    profile=product_profile(kind)
    return f'${command} [id] {kind} '+' '.join(f'[{"주당금액/비율%" if field == "dividend_per_share" else field}{"(선택)" if field in OPTIONAL_ARGS.get(profile,()) else ""}]' for field in CREATE_ARGS[profile])+' [추가 JSON(선택)]'


def positional_tokens(tail):
    # Preserve unquoted trailing JSON, including its internal strings and spaces.
    quote=None
    escaped=False
    for index,char in enumerate(tail):
        if escaped:
            escaped=False
            continue
        if char=='\\' and quote!="'":
            escaped=True
            continue
        if quote:
            if char==quote:quote=None
        elif char in {'"',"'"}:
            quote=char
        elif char=='{' and (index==0 or tail[index-1].isspace()):
            extra=json.loads(tail[index:])
            if not isinstance(extra,dict):raise ValueError('추가 JSON은 객체여야 합니다.')
            return shlex.split(tail[:index]),extra
    tokens=shlex.split(tail)
    extra={}
    if tokens and tokens[-1].startswith('{'):
        extra=json.loads(tokens.pop())
        if not isinstance(extra,dict):raise ValueError('추가 JSON은 객체여야 합니다.')
    return tokens,extra


class AdminCommandsManager:
    def __init__(self,bot):
        self.bot,self.db,self.control = bot,bot.db,bot.admin_control

    @staticmethod
    def adjust(current,operation,value):
        if operation not in {'add','del','modify'}:
            raise ValueError('작업은 add(더하기), del(빼기), modify(지정값)입니다.')
        return current+value if operation=='add' else current-value if operation=='del' else value

    def locate(self,conn,code):
        matches=[]
        for category,(table,key,price,supply) in ENTITIES.items():
            if category in KINDS:
                continue
            row=conn.execute(f'SELECT * FROM "{table}" WHERE "{key}"=?',(code,)).fetchone()
            # Savings and foreign-currency accounts share the saving table.
            if row and table=='saving' and (row['product_kind']=='currency_saving')!=(category=='currencyproduct'):
                row=None
            if row:
                matches.append((category,dict(row)))
        row=conn.execute('SELECT * FROM investment WHERE product_code=?',(code,)).fetchone()
        if row:
            category=next((k for k,v in KINDS.items() if v==row['product_type']),None)
            if not category:
                raise ValueError('이 투자상품 유형은 통합 상품 관리에서 지원하지 않습니다.')
            matches.append((category,dict(row)))
        if len(matches)!=1:
            raise ValueError('상품 코드를 찾을 수 없거나 중복되었습니다.')
        return matches[0]

    def country_create(self, actor, args):
        nationality, currency = str(args[0]).strip(), str(args[1]).strip().upper()
        if not nationality or len(nationality) > 80:
            raise ValueError('국가명은 1~80자로 입력해주세요.')
        raw = list(args[2:])
        base_rate = self.control._number(raw[0], -100, 100) if raw else 3
        legal_max_rate = self.control._number(raw[1], 0, 100) if len(raw) > 1 else 20
        tax_percent = self.control._number(raw[2], 0, 100) if len(raw) > 2 else 3
        with self.db.atomic() as conn:
            self.control.require(actor, conn)
            if not conn.execute("SELECT 1 FROM currencies WHERE code=? AND status='active'", (currency,)).fetchone():
                raise ValueError('활성 통화 코드를 입력해주세요. 통화는 먼저 $상품생성으로 만들어야 합니다.')
            try:
                conn.execute("INSERT INTO country_currencies(nationality,currency_code,base_rate,legal_max_rate,tax_percent) "
                             "VALUES (?,?,?,?,?)", (nationality, currency, base_rate, legal_max_rate, tax_percent))
            except Exception as exc:
                if 'UNIQUE' in str(exc).upper():
                    raise ValueError('같은 이름의 국가가 이미 있습니다.') from None
                raise
            from managers.currency_manager import CurrencyManager
            CurrencyManager.treasury_id(conn, nationality)
            self.control._audit(conn, actor, 'country.create', {
                'nationality': nationality, 'currency_code': currency,
                'base_rate': base_rate, 'legal_max_rate': legal_max_rate, 'tax_percent': tax_percent,
            })
        return f'✅ 연방국가 {nationality} 생성 · 기본화폐 {currency} · 기준금리 {base_rate:g}% · 법정최고이자 {legal_max_rate:g}% · 세율 {tax_percent:g}%'

    def country_delete(self, actor, nationality):
        nationality = str(nationality).strip()
        if nationality == '낙원':
            raise ValueError('기본 연방국가 낙원은 삭제할 수 없습니다.')
        with self.db.atomic() as conn:
            self.control.require(actor, conn)
            if not conn.execute('SELECT 1 FROM country_currencies WHERE nationality=?', (nationality,)).fetchone():
                raise ValueError('등록된 국가를 확인해주세요.')
            servers = conn.execute('SELECT COUNT(*) FROM guild_countries WHERE nationality=?', (nationality,)).fetchone()[0]
            from database.user_db import citizen_filter
            citizens = conn.execute(f"SELECT COUNT(*) FROM users WHERE nationality=? AND {citizen_filter()}",
                                    (nationality,)).fetchone()[0]
            exchanges = conn.execute('SELECT COUNT(*) FROM market_exchanges WHERE nationality=?', (nationality,)).fetchone()[0]
            products = conn.execute(
                "SELECT (SELECT COUNT(*) FROM saving WHERE rate_nationality=?)+"
                "(SELECT COUNT(*) FROM liability WHERE rate_nationality=?)",
                (nationality, nationality),
            ).fetchone()[0]
            if servers or citizens or exchanges or products:
                raise ValueError(
                    f'서버 {servers}개·시민 {citizens}명·거래소 {exchanges}개·금리상품 '
                    f'{products}개가 연결되어 삭제할 수 없습니다.'
                )
            conn.execute('DELETE FROM financial_institutions WHERE nationality=?', (nationality,))
            conn.execute('DELETE FROM market_rate_curve_history WHERE nationality=?', (nationality,))
            conn.execute('DELETE FROM market_rate_curve WHERE nationality=?', (nationality,))
            conn.execute('DELETE FROM country_rate_history WHERE nationality=?', (nationality,))
            treasury = conn.execute('SELECT user_id FROM country_treasuries WHERE nationality=?', (nationality,)).fetchone()
            conn.execute('DELETE FROM country_treasuries WHERE nationality=?', (nationality,))
            if treasury:
                conn.execute('DELETE FROM users WHERE user_id=?', (treasury['user_id'],))
            # The country's HTS login account trades with the treasury and holds nothing itself.
            conn.execute("DELETE FROM users WHERE user_id=? AND nationality=? AND COALESCE(role,'user')='nation'",
                         (nationality, nationality))
            conn.execute('DELETE FROM country_currencies WHERE nationality=?', (nationality,))
            self.control._audit(conn, actor, 'country.delete', {'nationality': nationality})
        return f'✅ 연방국가 {nationality}을(를) 삭제했습니다. 통화 자체는 유지됩니다.'

    def execute(self,actor,command,raw,guild_id=None):
        with DBManager.admin_scope(actor):
            self.control.require(actor)
            if command=='상품생성':
                return self.create(actor,raw)
            if command=='상품수정':
                return self.create(actor,raw,operation='edit')
            args=shlex.split(raw)
            if command=='서버설치' and len(args)==2 and guild_id is not None:
                args.insert(0,str(guild_id))
            count={'상품가격수정':3,'상품삭제':1,'기본소득':1,'잔고수정':3,'등록':1,
                   '도박확률':1,'도박리턴값':1,'시장분위기':1,'상품분위기':2,
                   '복권당첨번호보기':0,'환율수정':3,'복권가격설정':1,'서버설치':3,
                   '국가삭제':1,
                   '금리수정':3,'시장금리수정':4,'법정최고이자수정':3,'정기배당수정':3,'정기배당활성':2,'특별배당실시':2,'합병':3,'액면분할병합':3,
                   '휴장':1,'개장':1,'장일시정지':1,'장재개':1,'서킷브레이커':2,'사이드카':1}
            count.update({'법인잔고수정':3,'법인청산':1,'ELS취소':1,'소수점거래관리':2,'상품이율수정':3})
            flexible = ((command=='기본소득' and len(args) in {1,2})
                        or (command=='자본변경' and len(args) in {4,5})
                        or (command=='증자' and len(args) in {3,4})
                        or (command=='국가생성' and len(args) in {2,3,4,5})
                        or (command=='세율' and len(args) in {1,2})
                        or (command=='합병' and len(args) in {2,3})
                        or (command in {'휴장','개장','장일시정지','장재개','사이드카'} and len(args) in {0,1})
                        or (command=='서킷브레이커' and len(args) in {1,2}))
            if not flexible and command not in {'복권당첨번호변경'} and len(args)!=count.get(command,-1):
                raise ValueError(f'사용법: ${command} {USAGE.get(command,"")}')
            if command=='소수점거래관리':
                if args[1] not in {'0','1'}:
                    raise ValueError('0은 켜기, 1은 끄기입니다.')
                with self.db.atomic() as conn:
                    category, row = self.locate(conn,args[0])
                    if category not in {'stock','asset','etf','fund','indexfund','commodity'}:
                        raise ValueError('주식·우선주·ETF·펀드·현물 원자재만 소수점 거래를 지원합니다.')
                    return self.control.entity(actor,category,'edit',args[0],extra={'Fractal':1-int(args[1])},conn=conn)
            if command=='상품이율수정':
                base = self.control._number(args[1],0,100)
                extension = self.control._number(args[2],0,100)
                from managers.interest_manager import InterestManager
                with self.db.atomic() as conn:
                    InterestManager(self.db).settle_product_rate_boundary(conn,args[0],time.time())
                    self.control.entity(actor,'loan','edit',args[0],extra={'interest_rate':base,'extension_interest_rate':extension},conn=conn)
                    conn.execute("UPDATE loans SET contract_rate=CASE WHEN extension_count>0 AND ?>0 THEN ? ELSE ? END WHERE product_code=? AND status='active' AND rate_type='fixed'", (extension,extension,base,args[0]))
                return f'✅ {args[0]} 원이율 연 {base:g}% · 연장이율 {extension:g}%로 수정했습니다.'
            if command in {'휴장','개장','장일시정지','장재개','서킷브레이커','사이드카'}:
                from managers.market_session_manager import MarketSessionManager
                actions={'휴장':'close','개장':'open','장일시정지':'pause','장재개':'resume','서킷브레이커':'circuit','사이드카':'sidecar'}
                exchange_id=(args[0] if args else 'ALL') if command!='서킷브레이커' else (args[0] if len(args)==2 else 'ALL')
                stage=int(args[-1].removesuffix('단계')) if command=='서킷브레이커' else None
                result=MarketSessionManager(self.db).control(actor,actions[command],stage=stage,exchange_id=exchange_id)
                return self.result_text(result)
            if command=='ELS취소':
                from managers.els_manager import ELSManager
                return self.result_text(ELSManager(self.db,self.bot.user_mgr).cancel(actor,args[0]))
            if command in {'자본변경','증자','정기배당수정','정기배당활성','특별배당실시','합병','액면분할병합','법인잔고수정','법인청산'}:
                from managers.corporate_actions_manager import CorporateActionsManager
                manager=CorporateActionsManager(self.bot)
                if command=='자본변경':result=manager.change_capital(actor,args[0],args[1],args[2],self.control._number(args[3],1,integer=True),self.control._number(args[4],1,integer=True) if len(args)==5 else None)
                elif command=='증자':result=manager.change_capital(actor,'증자',args[0],args[1],self.control._number(args[2],1,integer=True),self.control._number(args[3],1,integer=True) if len(args)==4 else None)
                elif command=='정기배당수정':result=manager.configure_dividend(actor,args[0],duration_seconds(args[1]),args[2])
                elif command=='정기배당활성':
                    active={'on':1,'off':0,'1':1,'0':0}.get(args[1].lower())
                    if active is None:raise ValueError('정기배당 활성값은 on/off 또는 1/0입니다.')
                    result=manager.set_dividend_active(actor,args[0],active)
                elif command=='특별배당실시':result=manager.special_dividend(actor,args[0],self.control._number(args[1],1,integer=True))
                elif command=='액면분할병합':result=manager.adjust_face_value(actor,args[0],args[1],self.control._number(args[2],2,integer=True))
                elif command=='법인잔고수정':result=manager.fund_issuer(actor,args[0],args[1],self.control._number(args[2],0,integer=True))
                elif command=='법인청산':result=manager.liquidate(actor,args[0])
                else:result=manager.merge(actor,args[0],args[1],args[2] if len(args)==3 else '1')
                return self.result_text(result)
            if command=='기본소득':
                result=self.bot.admin_mgr.distribute_basic_income(
                    args[0],actor=actor,currency_code=args[1] if len(args)==2 else None
                )
                return result[1] if isinstance(result,tuple) else result
            if command=='등록':
                return self.control.setup(actor,args[0],args[0],'낙원')
            if command=='서버설치':
                from managers.currency_manager import CurrencyManager
                if not args[0].isdecimal():
                    raise ValueError('Discord 서버 ID를 입력해주세요.')
                with self.db.atomic() as conn:
                    code=args[2].upper(); CurrencyManager.rate(conn,code)
                    existing=conn.execute('SELECT currency_code FROM country_currencies WHERE nationality=?',(args[1],)).fetchone()
                    if existing and existing[0]!=code:
                        raise ValueError('이미 다른 화폐에 연결된 국적입니다. DB에서 기존 연결을 먼저 확인해주세요.')
                    conn.execute('INSERT OR IGNORE INTO country_currencies(nationality,currency_code) VALUES (?,?)',(args[1],code))
                    conn.execute('INSERT INTO guild_countries(guild_id,nationality) VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET nationality=excluded.nationality',(args[0],args[1]))
                return f'✅ 등록 서버 {args[0]} → {args[1]} · 기본화폐 {code} (신규 등록부터 적용)'
            if command=='국가생성':
                return self.country_create(actor,args)
            if command=='국가삭제':
                return self.country_delete(actor,args[0])
            if command=='시장금리수정':
                result = self.bot.rate_mgr.set_market_rate(
                    args[0], args[1], args[2], args[3], actor
                )
                if not result[0]:
                    raise ValueError(result[1])
                return result[1]
            if command in {'도박확률','도박리턴값','시장분위기'}:
                key={'도박확률':'gamble_chance','도박리턴값':'gamble_return','시장분위기':'market_condition'}[command]
                value=args[0]
                if command=='시장분위기' and value not in {'bull','flat','bear'}:
                    raise ValueError('bull/flat/bear 중 하나를 입력해주세요.')
                return self.control.settings(actor,{key:value})
            if command=='세율':
                if len(args)==1:
                    value=self.control._number(args[0],0,100)
                    self.db.system.set_setting('tax_percent',value)
                    return f'✅ 전체 기본 세율을 {value:g}%로 설정했습니다.'
                nationality,value=args
                value=self.control._number(value,0,100)
                with self.db.atomic() as conn:
                    self.control.require(actor,conn)
                    if conn.execute('UPDATE country_currencies SET tax_percent=? WHERE nationality=?',(value,nationality)).rowcount != 1:
                        raise ValueError('등록된 국가를 확인해주세요.')
                    self.control._audit(conn,actor,'세율',{'nationality':nationality,'tax_percent':value})
                return f'✅ {nationality} 세율을 {value:g}%로 설정했습니다.'
            if command=='복권가격설정':
                return self.bot.lottery_mgr.configure(actor,{'ticket_price':self.control._number(args[0],1,1000000,integer=True)})
            if command=='잔고수정':
                return self.balance(actor,*args)
            if command.startswith('복권당첨번호'):
                return self.lottery(actor,command,args)
            with self.db.atomic() as conn:
                self.control.require(actor,conn)
                if command in {'금리수정','법정최고이자수정'}:
                    if command == '금리수정':
                        key='base_rate'
                        nationality, operation, raw_value = args
                        row=conn.execute('SELECT base_rate FROM country_currencies WHERE nationality=?',(nationality,)).fetchone()
                        if not row:
                            raise ValueError('등록된 국가를 확인해주세요.')
                        current=float(row['base_rate'])
                        setter = lambda value: self.bot.rate_mgr.set_country_rate(key,nationality,value,actor,conn=conn)
                    else:
                        key='legal_max_rate'
                        nationality, operation, raw_value = args
                        row=conn.execute('SELECT legal_max_rate FROM country_currencies WHERE nationality=?',(nationality,)).fetchone()
                        if not row:
                            raise ValueError('통화와 연결된 국적을 확인해주세요.')
                        current=float(row['legal_max_rate'])
                        setter = lambda value: self.bot.rate_mgr.set_country_rate(
                            key,nationality,value,actor,conn=conn
                        )
                    minimum = -100 if key == 'base_rate' else 0
                    value=self.adjust(current,operation,self.control._number(raw_value,minimum,100))
                    self.control._number(value,minimum,100)
                    result=setter(value)
                    if not result[0]:
                        raise ValueError(result[1])
                    payload={'operation':operation,'before':current,'after':value}
                    if command in {'금리수정','법정최고이자수정'}:
                        payload['nationality']=nationality
                    self.control._audit(conn,actor,command,payload)
                    return result[1]
                if command=='환율수정':
                    return self.currency(conn,actor,*args)
                category,row=self.locate(conn,args[0])
                if command=='상품삭제':
                    return self.control.entity(actor,category,'delete',args[0],conn=conn)
                if command=='상품분위기':
                    if args[1] not in {'bull','flat','bear'}:
                        raise ValueError('bull/flat/bear 중 하나를 입력해주세요.')
                    if 'market_condition' not in row:
                        raise ValueError('이 상품에는 개별 시장 분위기 설정이 없습니다.')
                    return self.control.entity(actor,category,'edit',args[0],extra={'market_condition':args[1]},conn=conn)
                price_key=ENTITIES[category][2]
                if category=='option':
                    underlying,_=self.bot.user_mgr._asset_row(conn,row['underlying_code'])
                    current=self.bot.option_mgr.theoretical_price(underlying['price'],row['strike_price'],max(0,row['expiration_epoch']-time.time())/(365*86400),row['implied_volatility'],row['option_type'],RateManager.for_exchange(conn,'base_rate',underlying.get('exchange_id'))/100)
                    override=conn.execute('SELECT price FROM market_quote_overrides WHERE asset_code=?',(args[0],)).fetchone()
                    if override:current=override[0]
                elif price_key:
                    current=row[price_key]
                else:
                    raise ValueError('가격이 없는 상품입니다. 상품 이율은 $상품수정으로 변경해주세요.')
                currency_code=str(row.get('quote_currency') or 'NWX')
                entered=parse_currency_money(conn,args[2],currency_code,minimum=0)
                value=self.adjust(float(current),args[1],entered)
                value=parse_currency_money(conn,value,currency_code,minimum=0)
                if value <= 0:
                    raise ValueError('가격은 0보다 커야 합니다.')
                if category in {'option','bond'}:
                    conn.execute("INSERT INTO market_quote_overrides VALUES (?, ?, ?, 'admin') ON CONFLICT(asset_code) DO UPDATE SET price=excluded.price,updated_epoch=excluded.updated_epoch,source=excluded.source",(args[0],value,int(time.time())))
                if category!='option':
                    self.control.entity(actor,category,'edit',args[0],price=str(value),conn=conn)
                if category not in {'option','bond'}:
                    conn.execute('DELETE FROM market_quote_overrides WHERE asset_code=?',(args[0],))
                conn.execute('DELETE FROM market_simulation_state WHERE asset_code=?',(args[0],))
                from managers.market_manager import MarketManager
                MarketManager(self.db).clear_runtime_targets(args[0],conn=conn)
                self.control._audit(conn,actor,command,{'code':args[0],'operation':args[1],'before':current,'after':value})
                return f'✅ {args[0]} 가격 {current:,} → {value:,} {row.get("quote_currency","NWX")}'

    @staticmethod
    def result_text(result):
        if isinstance(result,tuple) and len(result)==2 and isinstance(result[0],bool):
            if not result[0]:raise ValueError(str(result[1]).removeprefix('❌').strip())
            return result[1]
        return str(result)

    def create(self,actor,raw,operation='create'):
        parts=raw.split(maxsplit=2)
        if len(parts)<2:
            raise ValueError('$상품생성 '+USAGE['상품생성']+' · 상품종류: company, preferred, saving, loan, asset, etf, future, corporatebond, fund, indexfund, commodity, bond, option, currency, currencyproduct, els')
        code,kind=parts[:2]
        category=TYPES.get(kind,TYPES.get(kind.lower()))
        profile=product_profile(kind)
        if category is None:
            raise ValueError('상품종류: company, preferred, saving, loan, asset, etf, future, corporatebond, fund, indexfund, commodity, bond, option, currency, currencyproduct, els')
        if len(parts)!=3:raise ValueError('사용법: '+creation_usage(kind,'상품수정' if operation=='edit' else '상품생성'))
        tail=parts[2]
        # Existing structured invocations remain compatible; the documented interface is positional.
        if tail.lstrip().startswith('{'):
            fields=json.loads(tail)
        else:
            tokens,extra=positional_tokens(tail)
            if tokens and all('=' in token for token in tokens):
                fields={}
                for item in tokens:
                    key,_,value=item.partition('=')
                    if key in fields:raise ValueError('중복 설정 필드입니다.')
                    try:fields[key]=json.loads(value)
                    except json.JSONDecodeError:fields[key]=value
            else:
                order=CREATE_ARGS[profile]
                optional=OPTIONAL_ARGS.get(profile,())
                if len(tokens) not in range(len(order)-len(optional), len(order)+1):
                    raise ValueError('사용법: '+creation_usage(kind,'상품수정' if operation=='edit' else '상품생성'))
                fields={}
                if 'Fractal' in optional and len(tokens)==len(order)-len(optional)+1:
                    fields['Fractal']=int(tokens.pop())
                for key,value in zip(order,tokens):
                    if key=='components':
                        fields[key]={}
                        for component in value.split(','):
                            asset,separator,weight=component.rpartition(':')
                            if not separator or not asset or asset in fields[key]:
                                raise ValueError('components는 종목코드:비중,종목코드:비중 형식입니다.')
                            fields[key][asset]=float(weight)
                    elif key=='dividend_cycle_seconds':
                        fields[key]=duration_seconds(value)
                    elif key=='dividend_per_share':
                        from managers.corporate_actions_manager import CorporateActionsManager
                        fields.update(CorporateActionsManager.dividend_terms(value))
                    elif profile=='els' and key in {'underlyings','observation_days','redemption_levels'}:
                        fields[key]=value
                    elif key in TEXT_ARGS:
                        fields[key]=value
                    else:
                        try:fields[key]=json.loads(value)
                        except json.JSONDecodeError:fields[key]=value
            if set(fields)&set(extra):raise ValueError('추가 JSON에서 이미 입력한 인자를 중복 지정할 수 없습니다.')
            fields.update(extra)
        if not isinstance(fields,dict):raise ValueError('상품 설정은 JSON 객체여야 합니다.')
        if category=='els':
            if operation!='create':raise ValueError('ELS 발행조건은 변경할 수 없습니다. 모집 중 취소 후 새 상품을 생성해주세요.')
            from managers.els_manager import ELSManager
            return ELSManager(self.db,self.bot.user_mgr).create(actor,code,fields)
        if profile=='preferred':
            if fields.get('share_class','preferred')!='preferred':raise ValueError('우선주는 share_class=preferred여야 합니다.')
            fields['share_class']='preferred'
        if operation=='edit':
            with self.db.atomic() as conn:
                found,row=self.locate(conn,code)
                if found!=category or (category=='stock' and (row.get('share_class')=='preferred')!=(profile=='preferred')):
                    raise ValueError('상품 코드와 상품종류가 일치하지 않습니다.')
                # Unchanged contract conditions may be repeated in a full positional
                # form. Actual changes continue to be validated by entity().
                fields={key:value for key,value in fields.items() if key not in row or row[key]!=value}
                if category=='option' and fields:
                    raise ValueError('발행 옵션 계약조건은 변경할 수 없습니다. 가격은 $상품가격수정으로 수정해주세요.')
        price_key,supply_key=ENTITIES[category][2:]
        if price_key and price_key!='price' and price_key in fields:
            if 'price' in fields:raise ValueError('가격을 중복 지정할 수 없습니다.')
            fields['price']=fields.pop(price_key)
        if supply_key and supply_key!='supply' and supply_key in fields:
            if 'supply' in fields:raise ValueError('발행량을 중복 지정할 수 없습니다.')
            fields['supply']=fields.pop(supply_key)
        name=fields.pop('name','')
        price=fields.pop('price','')
        supply=fields.pop('supply','')
        if category=='option' and operation=='create':
            if name or price or supply:
                raise ValueError('옵션은 underlying_code, option_type, strike_price, maturity_days, quantity, limit_price, volatility_percent를 입력해주세요.')
            # Contracts keep the user's requested code rather than an opaque generated code.
            error=self.bot.user_mgr._settle_user_interest(TREASURY_USER_ID)
            if error:raise ValueError(error)
            with self.db.atomic() as conn:
                self.control.require(actor,conn)
                if not code or len(code)>80 or self.db.code_locations(conn,code):
                    raise ValueError('새로운 1~80자 상품 코드를 입력해주세요.')
                success,message=self.bot.option_mgr.create_and_write(TREASURY_USER_ID,**fields,conn=conn)
                if not success:raise ValueError(message)
                row=conn.execute("SELECT contract_id,contract_code FROM option_contracts WHERE writer_id=? ORDER BY contract_id DESC LIMIT 1",(TREASURY_USER_ID,)).fetchone()
                conn.execute('UPDATE option_contracts SET contract_code=? WHERE contract_id=?',(code,row['contract_id']))
                conn.execute('UPDATE market_orders SET asset_code=? WHERE asset_code=?',(code,row['contract_code']))
                self.control._audit(conn,actor,'option.create',{'code':code,**fields})
                return f'✅ 옵션 {code} 생성 완료. '+message.replace(row['contract_code'],code)
        return self.control.entity(actor,category,operation,code,str(name),str(price),str(supply),fields)

    def balance(self,actor,identity,operation,raw_amount):
        from managers.currency_manager import CurrencyManager
        row=self.db.accounts.execute_one('SELECT user_id FROM accounts WHERE account_number=?',(identity,))
        if row:
            from managers.interest_manager import InterestManager
            InterestManager(self.db).process_user_interests(row[0])
        with self.db.atomic() as conn:
            self.control.require(actor,conn)
            currency_account=conn.execute("SELECT *,quote_currency AS currency_code FROM accounts WHERE account_number=? AND account_type='currency_saving'",(identity,)).fetchone()
            bank=conn.execute("SELECT * FROM accounts WHERE account_number=? AND account_type<>'currency_saving'",(identity,)).fetchone()
            user=conn.execute('SELECT * FROM users WHERE user_id=?',(identity,)).fetchone()
            if sum(bool(x) for x in (currency_account,bank,user))!=1:
                raise ValueError('사용자 ID 또는 계좌번호를 확인해주세요.')
            if currency_account:
                if currency_account['currency_code'] == CurrencyManager.home(conn,currency_account['user_id']):
                    raise ValueError('국적 통화는 사용자 cash 잔고를 수정해주세요.')
                self.bot.currency_mgr._settle(conn,currency_account['user_id'],time.time())
                current=conn.execute("SELECT balance_units FROM accounts WHERE account_number=? AND account_type='currency_saving'",(identity,)).fetchone()[0]
                amount=Decimal(raw_amount)
                units=amount*SCALE
                if not amount.is_finite() or amount<0 or units!=units.to_integral_value():
                    raise ValueError('외화 금액을 확인해주세요.')
                CurrencyManager.validate_units(
                    conn, int(units), currency_account['currency_code']
                )
                value=self.adjust(current,operation,int(units))
                if not 0<=value<=MAX_UNITS:raise ValueError('외화 잔고는 0 이상이며 저장 한도 이하여야 합니다.')
                conn.execute("UPDATE accounts SET balance_units=? WHERE account_number=? AND account_type='currency_saving'",(value,identity))
                unit=currency_account['currency_code']; before=display_units(current); after=display_units(value)
            else:
                table,key='accounts','account_number' if bank else 'user_id'
                if not bank:table='users'
                currency = (str(bank['quote_currency']) if bank
                            else CurrencyManager.home(conn, identity))
                current=float((bank or user)['balance' if bank else 'cash'] or 0)
                entered=parse_currency_money(conn,raw_amount,currency,minimum=0)
                value=self.adjust(current,operation,entered)
                value=parse_currency_money(conn,value,currency,minimum=0)
                if bank:
                    conn.execute('UPDATE accounts SET balance_units=CAST(ROUND(? * 1000000) AS INTEGER),principal=?,last_payment_epoch=?,last_payment=CURRENT_TIMESTAMP WHERE account_number=?',(value,value,time.time(),identity))
                else:conn.execute('UPDATE users SET cash=? WHERE user_id=?',(value,identity))
                unit=currency
                before=current;after=value
            self.control._audit(conn,actor,'balance.adjust',{'target':identity,'operation':operation,'before':before,'after':after,'currency':unit})
            return f'✅ {identity} 잔고 {before} → {after} {unit}'

    def currency(self,conn,actor,a,b,raw_ratio):
        a,b=a.upper(),b.upper()
        if a==b:raise ValueError('서로 다른 통화를 입력해주세요.')
        from fractions import Fraction
        ratio=Decimal(raw_ratio)
        if not ratio.is_finite() or ratio<=0:raise ValueError('비율은 0보다 큰 유한한 값이어야 합니다.')
        # Keep the inverse exact: 1 NWX = 3 AAA must mean 1 AAA = 1/3 NWX, not 0.333…
        exact=Fraction(ratio)
        if a == 'NWX':
            a,b,exact = b,a,1/exact
            ratio=Decimal(exact.numerator)/Decimal(exact.denominator)
        ar=self.bot.currency_mgr._rate(conn,a);br=self.bot.currency_mgr._rate(conn,b)
        row=conn.execute("SELECT * FROM currencies WHERE code=? AND status='active'",(a,)).fetchone()
        if row['reference_rate'] is not None or br*ratio < 1:
            from managers.currency_manager import CurrencyManager
            current = CurrencyManager.rate_fraction(conn,a)
            target_rate = CurrencyManager.rate_fraction(conn,b)*exact
            multiplier = Fraction(str(row['rate_multiplier']))*target_rate/current
            conn.execute('UPDATE currencies SET rate_multiplier=? WHERE code=?',(str(multiplier),a))
            self.control._audit(conn,actor,'currency.cross_rate',{'a':a,'b':b,'ratio':str(ratio)})
            return f'✅ 1 {a} = {ratio:.12g} {b}'
        target=int((br*ratio).to_integral_value(rounding=ROUND_HALF_UP))
        self.control._number(target,1,integer=True)
        conn.execute('UPDATE market_assets SET price=? WHERE code=?',(target,row['asset_code']))
        conn.execute('DELETE FROM market_simulation_state WHERE asset_code=?',(row['asset_code'],))
        from managers.market_manager import MarketManager
        MarketManager(self.db).clear_runtime_targets(row['asset_code'],conn=conn)
        self.control._audit(conn,actor,'currency.cross_rate',{'a':a,'b':b,'ratio':str(ratio),'before':str(ar),'after':target})
        return f'✅ 1 {a} = {Decimal(target)/br:.12g} {b} · {a} 시세 {target:,} NWX (정수 호가 반올림)'

    def lottery(self,actor,command,args):
        with self.db.atomic() as conn:
            self.control.require(actor,conn)
            round=self.bot.lottery_mgr._ensure(conn,int(time.time()))
            override=json.loads(round['admin_numbers_json'] or 'null')
            if command=='복권당첨번호보기':
                if override:return f'🎟️ {round["round_id"]}회 관리자 지정 번호: {override["winning"]} · 보너스 {override["bonus"]}'
                if round['winning_json']:return f'🎟️ {round["round_id"]}회 당첨번호: {round["winning_json"]} · 보너스 {round["bonus"]}'
                latest=conn.execute("SELECT round_id,winning_json,bonus FROM lottery_rounds WHERE winning_json IS NOT NULL ORDER BY round_id DESC LIMIT 1").fetchone()
                return '현재 회차 자동 추첨 번호는 추첨 시 결정됩니다.'+(f' 직전 {latest[0]}회: {latest[1]} · 보너스 {latest[2]}' if latest else '')
            values=[int(x) for x in ' '.join(args).replace(',',' ').split()]
            if len(values) not in {6,7} or len(set(values))!=len(values) or any(not 1<=x<=45 for x in values):
                raise ValueError('서로 다른 1~45 번호 6개와 선택 보너스 번호 1개를 입력해주세요.')
            if round['status'] not in {'selling','closed'} or time.time()>=round['draw_epoch']:
                raise ValueError('추첨된 회차는 변경할 수 없습니다.')
            bonus=values[6] if len(values)==7 else secrets.choice([n for n in range(1,46) if n not in values])
            override={'winning':sorted(values[:6]),'bonus':bonus}
            conn.execute('UPDATE lottery_rounds SET admin_numbers_json=? WHERE round_id=?',(json.dumps(override),round['round_id']))
            self.control._audit(conn,actor,'lottery.numbers.override',{'round_id':round['round_id'],**override})
            return f'✅ {round["round_id"]}회 번호 {override["winning"]} · 보너스 {bonus} 지정. 추첨 후 결과에 관리자 지정 표시됩니다.'
