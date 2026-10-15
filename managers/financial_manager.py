"""Company journals and reports. No share-price-derived fictional operating income."""
import json
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from database.db_manager import DBManager
from database.financial_db import ACCOUNTS

KST = ZoneInfo('Asia/Seoul')
MAX_UNITS = 2**52  # Exact SQLite TOTAL validation and bounded report aggregation.


def month(epoch=None):
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, KST).strftime('%Y-%m')


def period_bounds(period):
    try:
        start = datetime.strptime(period, '%Y-%m').replace(tzinfo=KST)
        if start.strftime('%Y-%m') != period:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('기간은 YYYY-MM 형식으로 입력해주세요.') from None
    end = (start + timedelta(days=32)).replace(day=1)
    return int(start.timestamp()), int(end.timestamp())


class FinancialManager:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def resolve(conn, code):
        row = conn.execute('SELECT * FROM company WHERE company_code=?', (str(code).strip(),)).fetchone()
        if row is None:
            raise ValueError('회사 주식·우선주 product_id를 입력해주세요. 다른 상품은 재무제표 대상이 아닙니다.')
        if row['share_class'] == 'preferred':
            row = conn.execute('SELECT * FROM company WHERE company_code=?', (row['parent_company_code'],)).fetchone()
            if row is None:
                raise ValueError('우선주의 발행회사를 찾을 수 없습니다.')
        return dict(row)

    @staticmethod
    def require(conn, actor):
        user = conn.execute('SELECT role FROM users WHERE user_id=?', (str(actor),)).fetchone()
        if not user or str(user['role']).strip().lower() != 'admin':
            raise ValueError('관리자 권한이 필요합니다.')

    @staticmethod
    def entity(conn, code):
        return conn.execute('SELECT * FROM financial_entities WHERE company_code=?', (code,)).fetchone()

    @staticmethod
    def units(value, places):
        try:
            amount = Decimal(str(value)) * 10**places
            if not amount.is_finite() or amount != amount.to_integral_value() or abs(amount) > MAX_UNITS:
                raise ValueError()
            return int(amount)
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError(f'금액은 소수점 {places}자리 이내의 유한한 값으로 입력해주세요.') from None

    @classmethod
    def post(cls, conn, code, key, description, lines, flow='none', now=None, reversal=None):
        """Must run within the caller's transaction, so game cash and journal commit together."""
        now = int(time.time() if now is None else now)
        prior = conn.execute('SELECT entry_id FROM financial_entries WHERE company_code=? AND source_key=?', (code,key)).fetchone()
        if prior:
            return prior[0]
        combined = {}
        for account, amount in lines:
            if account not in ACCOUNTS or isinstance(amount,bool) or not isinstance(amount,int):
                raise ValueError('계정코드 또는 정수 최소단위 금액을 확인해주세요.')
            combined[account] = combined.get(account,0)+amount
        combined = {k:v for k,v in combined.items() if v}
        if not combined:
            return None
        if len(combined)<2 or sum(combined.values()) or sum(abs(v) for v in combined.values())>MAX_UNITS:
            raise ValueError('차변·대변 합계가 다르거나 분개 금액 한도를 초과했습니다.')
        period = month(now)
        last = conn.execute('SELECT MAX(period) FROM financial_periods WHERE company_code=?', (code,)).fetchone()[0]
        if last and period<=last:
            raise ValueError('마감한 기간에는 기록할 수 없습니다. 다음 기간에 정정해주세요.')
        cursor = conn.execute('''INSERT INTO financial_entries
            (company_code,period,recorded_epoch,source_key,description,cash_flow,reversal_of)
            VALUES (?,?,?,?,?,?,?)''', (code,period,now,key,str(description)[:500],flow,reversal))
        entry_id = cursor.lastrowid
        conn.executemany('INSERT INTO financial_lines(entry_id,account,amount) VALUES (?,?,?)',
                         [(entry_id,k,v) for k,v in combined.items()])
        conn.execute("UPDATE financial_entries SET status='posted' WHERE entry_id=?", (entry_id,))
        return entry_id

    @classmethod
    def converted(cls, conn, value, source, target, places):
        if source==target:return cls.units(value,places)
        from managers.currency_manager import CurrencyManager
        rate=CurrencyManager.quote(conn,source,target)
        amount=(Decimal(str(value))*rate.numerator/rate.denominator*(10**places)).quantize(Decimal(1),rounding=ROUND_HALF_UP)
        if abs(amount)>MAX_UNITS:raise ValueError('환산 금액이 회계 한도를 초과했습니다.')
        return int(amount)

    @classmethod
    def ensure(cls, conn, code, now=None):
        company = cls.resolve(conn,code)
        issuer = company['company_code']
        existing = cls.entity(conn,issuer)
        currency = company.get('quote_currency') or 'NWX'
        if existing:
            if existing['currency']!=currency:
                raise ValueError('회계 원장 개설 후 기준통화를 변경할 수 없습니다.')
            return issuer
        now = int(time.time() if now is None else now)
        precision = conn.execute('SELECT decimal_places FROM currencies WHERE code=?', (currency,)).fetchone()
        places = int(precision[0]) if precision else (0 if currency=='NWX' else 6)
        conn.execute('INSERT INTO financial_entities VALUES (?,?,?,?)', (issuer,currency,places,now))
        cash = conn.execute('SELECT * FROM issuer_cash_accounts WHERE company_code=?', (issuer,)).fetchone()
        lines = []
        if cash:
            lines += [(k,cls.converted(conn,cash[col],'NWX',currency,places)) for k,col in [('cash','balance'),('restricted_cash','locked_amount')]]
        shares = conn.execute('SELECT capital,share_class,quote_currency FROM company WHERE company_code=? OR parent_company_code=?', (issuer,issuer)).fetchall()
        for share in shares:
            lines.append(('preferred_capital' if share['share_class']=='preferred' else 'common_capital', -cls.converted(conn,share['capital'] or 0,share['quote_currency'] or 'NWX',currency,places)))
        debt_rows=conn.execute("""SELECT COALESCE(p.quote_currency,'NWX') currency,SUM(a.quantity*p.face_value) amount
            FROM investment p JOIN user_assets a ON a.asset_code=p.product_code
            WHERE p.product_type='corporate_bond' AND p.status IN ('active','defaulted')
            AND p.issuer_code IN (SELECT company_code FROM company WHERE company_code=? OR parent_company_code=?)
            GROUP BY COALESCE(p.quote_currency,'NWX')""",(issuer,issuer))
        for row in debt_rows:lines.append(('bonds',-cls.converted(conn,row['amount'],row['currency'],currency,places)))
        arrears=conn.execute("""SELECT quote_currency currency,SUM(gross_amount-paid_amount) amount
            FROM preferred_dividend_claims WHERE issuer_code=? AND funding_source='issuer'
            AND status IN ('unpaid','partial') GROUP BY quote_currency""",(issuer,))
        for row in arrears:lines.append(('dividend_payable',-cls.converted(conn,row['amount'],row['currency'],currency,places)))
        els=conn.execute('SELECT COALESCE(SUM(principal_amount),0) FROM els_products WHERE issuer_code=?',(issuer,)).fetchone()[0]
        lines.append(('els_liability',-cls.converted(conn,els,'NWX',currency,places)))
        lines.append(('opening_adjustment',-sum(v for _,v in lines)))
        cls.record_shares(conn,issuer,now)
        cls.post(conn,issuer,'opening','기초 잔액 이관: 미확인 과거 자산·손익은 자본조정에 표시',lines,'opening',now)
        return issuer

    @classmethod
    def event(cls, conn, code, description, lines, flow='none', now=None, key=None, currency=None):
        if sum(Decimal(str(v)) for _,v in lines)!=0:raise ValueError('거래 분개 합계가 일치하지 않습니다.')
        issuer = cls.ensure(conn,code,now)
        ent=cls.entity(conn,issuer)
        currency=currency or ent['currency']
        entries=[(k,cls.converted(conn,v,currency,ent['currency'],ent['decimal_places'])) for k,v in lines]
        rounding=sum(v for _,v in entries)
        if rounding:entries.append(('currency_rounding',-rounding))
        return cls.post(conn,issuer,key or 'event:'+uuid.uuid4().hex,description,entries,flow,now)

    @classmethod
    def cash_state(cls, conn, issuer):
        row = conn.execute('SELECT balance,locked_amount FROM issuer_cash_accounts WHERE company_code=?',(issuer,)).fetchone()
        return (int(row[0]),int(row[1])) if row else (0,0)

    def initialize(self):
        with self.db.atomic() as conn:
            for row in conn.execute("SELECT company_code FROM company WHERE share_class='common' AND status='active'").fetchall():
                self.ensure(conn,row[0])

    @staticmethod
    def balances(conn, code, before):
        values = {r['account']:int(r['amount']) for r in conn.execute('''SELECT l.account,SUM(l.amount) amount
            FROM financial_lines l JOIN financial_entries e ON e.entry_id=l.entry_id
            WHERE e.company_code=? AND e.status='posted' AND e.recorded_epoch<? GROUP BY l.account''', (code,before))}
        return {account: values.get(account, 0) for account in ACCOUNTS}

    @classmethod
    def summarize(cls, balances):
        sums = {category:sum(v for k,v in balances.items() if ACCOUNTS[k][1]==category)
                for category in {v[1] for v in ACCOUNTS.values()}}
        net = -sums['revenue']-sums['expense']
        return dict(current_assets=sums['current_asset'], noncurrent_assets=sums['noncurrent_asset'],
                    assets=sums['current_asset']+sums['noncurrent_asset'],
                    current_liabilities=-sums['current_liability'], noncurrent_liabilities=-sums['noncurrent_liability'],
                    liabilities=-sums['current_liability']-sums['noncurrent_liability'],
                    equity=-sums['equity']+net, cumulative_profit=net)

    @classmethod
    def build(cls, conn, code, period):
        start,end = period_bounds(period)
        ent = cls.entity(conn,code)
        company = cls.resolve(conn,code)
        b = cls.balances(conn,code,end)
        opening = cls.balances(conn,code,start)
        changes = {k:b.get(k,0)-opening.get(k,0) for k in ACCOUNTS}
        s, previous = cls.summarize(b), cls.summarize(opening)
        revenue = -changes['revenue']
        gross = revenue-changes['cogs']
        operating = gross-sum(changes[k] for k in ('sga','depreciation','amortization'))
        pretax = operating-sum(changes[k] for k in ('other_income','interest_income','other_expense','interest_expense','currency_rounding'))
        profit = pretax-changes['income_tax']
        flow_values = {r['cash_flow']:int(r['amount']) for r in conn.execute('''SELECT e.cash_flow,SUM(l.amount) amount
            FROM financial_entries e JOIN financial_lines l ON l.entry_id=e.entry_id
            WHERE e.company_code=? AND e.period=? AND e.status='posted' AND l.account='cash' GROUP BY e.cash_flow''', (code,period))}
        flows = {key: flow_values.get(key, 0) for key in
                 ('opening','operating','investing','financing','adjustment','none')}
        # Recognized opening entries are excluded from the period's operating result.
        average = lambda field: (s[field]+previous[field])/2 if ent['opened_epoch']<start else None
        ratio = lambda n,d,percent=True: n/d*(100 if percent else 1) if d is not None and d>0 else None
        ratios = {}
        def r(name,n,d,pct=True): ratios[name]={'value':ratio(n,d,pct),'unit':'%' if pct else '배'}
        quick = sum(b.get(k,0) for k in ('cash','short_investments','receivables','treasury_receivable'))
        r('당좌비율',quick,s['current_liabilities']); r('현금비율',b.get('cash',0)+b.get('short_investments',0),s['current_liabilities'])
        for label,value in [('총자산이익률',profit),('총자산영업이익률',operating),('총자산세전이익률',pretax),('총자본세전이익률',pretax)]:r(label,value,average('assets'))
        for label,value in [('자기자본세전이익률',pretax),('자기자본이익률',profit)]:r(label,value,average('equity'))
        for label,value in [('매출액영업이익률',operating),('매출액세전순이익률',pretax),('매출액순이익률',profit)]:r(label,value,revenue)
        for label,value in [('부채비율',s['liabilities']),('유동부채비율',s['current_liabilities']),('고정부채비율',s['noncurrent_liabilities']),('고정비율',s['noncurrent_assets'])]:r(label,value,s['equity'])
        r('자기자본비율',s['equity'],s['assets']); r('고정장기적합률',s['noncurrent_assets'],s['equity']+s['noncurrent_liabilities'])
        r('이자보상배율',operating,changes['interest_expense'],False)
        for label,field in [('총자본회전율','assets'),('자기자본회전율','equity'),('고정자산회전율','noncurrent_assets')]:r(label,revenue,average(field),False)
        for label,account,n in [('유형자산회전율','ppe',revenue),('재고자산회전율','inventory',changes['cogs']),('매출채권회전율','receivables',revenue)]:
            avg=(b.get(account,0)+opening.get(account,0))/2 if ent['opened_epoch']<start else None
            r(label,n,avg,False)
        weighted_shares,eps=cls.eps(conn,code,start,end,profit,ent['opened_epoch'])
        capex=0
        for row in conn.execute('''SELECT e.entry_id,
            SUM(CASE WHEN l.account='cash' THEN l.amount ELSE 0 END) cash,
            SUM(CASE WHEN l.account IN ('ppe','intangibles') THEN l.amount ELSE 0 END) assets
            FROM financial_entries e JOIN financial_lines l ON l.entry_id=e.entry_id
            WHERE e.company_code=? AND e.period=? AND e.status='posted' AND e.cash_flow='investing' GROUP BY e.entry_id''',(code,period)):
            capex+=min(max(0,-int(row['cash'])),max(0,int(row['assets'])))
        cash_difference = b.get('cash',0)-opening.get('cash',0)-sum(flows.values())
        actual = cls.cash_state(conn,code)
        cash_matches = None if ent['currency']!='NWX' or period!=month() else b.get('cash',0)==actual[0]*10**ent['decimal_places']
        count = conn.execute("SELECT COUNT(*) FROM financial_entries WHERE company_code=? AND period=? AND status='posted'",(code,period)).fetchone()[0]
        return dict(code=code,name=company['name'],currency=ent['currency'],places=ent['decimal_places'],period=period,
            opened_epoch=ent['opened_epoch'],balances=b,opening=opening,changes=changes,summary=s,previous=previous,
            income=dict(revenue=revenue,gross=gross,operating=operating,pretax=pretax,tax=changes['income_tax'],profit=profit,
                        ebitda=operating+changes['depreciation']+changes['amortization']),
            cash_flows=flows,capex=capex,fcf=flows.get('operating',0)-capex,eps=eps,weighted_shares=weighted_shares,ratios=ratios,entry_count=count,
            checks=dict(balance_sheet=s['assets']==s['liabilities']+s['equity'],cash_flow=cash_difference==0,
                        live_cash=cash_matches),closed=False,
            notes=['게임 회계 기준 · 월간 비율(연율화하지 않음)',
                   '도입 전 미확인 잔액은 자산이나 과거 이익으로 추정하지 않습니다.',
                   '총자본은 총자산과 동일하게 정의합니다. 평균 잔액이 없는 최초 월의 회전율·ROA·ROE는 미산출입니다.',
                   'EPS는 우선주 확정배당 차감 후 보통주 시간가중 유통주식수로 계산합니다. 국고 보유분 제외, 담보주식 포함. 완전한 기간 이력 없으면 미산출. FFO/AFFO는 부동산 모델 미지원',
                   '기타포괄손익 미지원: 총포괄손익은 당기순이익과 같습니다.',
                   '기존 NWX 법인현금은 거래 시 환율로 회사 통화에 환산합니다. 기말환산·외화 현금대사는 미지원입니다.',
                   '회사채 이자는 기존 게임의 만기 지급 시점에 인식합니다. 발행차금은 만기 일괄 상각합니다.'])

    def report(self, code, period=None):
        period = period or month()
        start,end = period_bounds(period)
        if period>month():raise ValueError('미래 기간은 조회할 수 없습니다.')
        with self.db.readonly() as conn:
            issuer=self.resolve(conn,code)['company_code']
            exists=self.entity(conn,issuer)
        if not exists:
            with self.db.atomic() as conn:self.ensure(conn,code)
        with self.db.readonly() as conn:
            ent=self.entity(conn,issuer)
            if end<=ent['opened_epoch']:raise ValueError('원장 도입 전 기간에는 재무자료가 없습니다.')
            saved=conn.execute('SELECT report_json FROM financial_periods WHERE company_code=? AND period=?',(issuer,period)).fetchone()
            return json.loads(saved[0]) if saved else self.build(conn,issuer,period)

    def periods(self, code):
        with self.db.readonly() as conn:
            issuer=self.resolve(conn,code)['company_code']
            return [r[0] for r in conn.execute('SELECT DISTINCT period FROM financial_entries WHERE company_code=? ORDER BY period DESC LIMIT 24',(issuer,))]

    def close_period(self, actor, code, period):
        start,end=period_bounds(period)
        if end>time.time():raise ValueError('아직 끝나지 않은 월은 결산할 수 없습니다.')
        with self.db.atomic() as conn:
            self.require(conn,actor)
            issuer=self.ensure(conn,code)
            if end<=self.entity(conn,issuer)['opened_epoch']:raise ValueError('도입 전 기간은 결산할 수 없습니다.')
            if conn.execute('SELECT 1 FROM financial_periods WHERE company_code=? AND period=?',(issuer,period)).fetchone():
                return '이미 결산된 기간입니다.'
            pending=conn.execute('SELECT 1 FROM financial_operating_profiles WHERE company_code=? AND enabled=1 AND start_period<=?',(issuer,period)).fetchone()
            if pending:raise ValueError('이 기간의 자동 영업 인식이 아직 끝나지 않았습니다. 영업 처리 후 결산해주세요.')
            last=conn.execute('SELECT MAX(period) FROM financial_periods WHERE company_code=?',(issuer,)).fetchone()[0]
            if last and period<last:raise ValueError('이미 마감한 월보다 이전 기간은 추가 결산할 수 없습니다.')
            report=self.build(conn,issuer,period)
            if not all(report['checks'][k] for k in ('balance_sheet','cash_flow')):raise ValueError('재무제표 검증에 실패했습니다.')
            report['closed']=True
            conn.execute('INSERT INTO financial_periods VALUES (?,?,?,?)',(issuer,period,int(time.time()),json.dumps(report,ensure_ascii=False)))
        return f'✅ {issuer} {period} 결산 완료'

    def manual(self, actor, code, description, lines, flow='none', reversal=None):
        if flow not in {'none','operating','investing','financing'}:raise ValueError('현금흐름 구분을 확인해주세요.')
        with self.db.atomic() as conn:
            self.require(conn,actor)
            issuer=self.ensure(conn,code)
            if reversal is not None:
                old=conn.execute("SELECT * FROM financial_entries WHERE entry_id=? AND company_code=? AND source_key LIKE 'manual:%'",(int(reversal),issuer)).fetchone()
                if not old:raise ValueError('이 회사의 관리자 분개만 정정할 수 있습니다. 자동 거래는 원본 기능에서 처리해주세요.')
                if conn.execute('SELECT 1 FROM financial_entries WHERE reversal_of=?',(int(reversal),)).fetchone():raise ValueError('이미 정정한 분개입니다.')
                entries=[(r['account'],-int(r['amount'])) for r in conn.execute('SELECT * FROM financial_lines WHERE entry_id=?',(int(reversal),))]
                flow=old['cash_flow']
            else:
                if not isinstance(lines,list) or not 2<=len(lines)<=30:raise ValueError('분개는 2~30개 계정 행으로 입력해주세요.')
                places=self.entity(conn,issuer)['decimal_places']
                entries=[(row['account'],self.units(row['amount'],places)) for row in lines]
            # Cash and tracked issuance accounts must remain reconciled with actual game settlement.
            protected={'restricted_cash','common_capital','preferred_capital','bonds','els_liability','dividend_payable','opening_adjustment','treasury_receivable'}
            if any(k in protected for k,_ in entries):raise ValueError('발행자본·회사채·배당채무·기초잔액·예치금은 자동 연동 계정입니다. 원본 관리 명령어를 이용해주세요.')
            cash_delta=sum(v for k,v in entries if k=='cash')
            if cash_delta:
                if self.entity(conn,issuer)['currency']!='NWX':raise ValueError('외화 법인현금 직접 입력은 아직 지원하지 않습니다. 외상거래 계정을 이용해주세요.')
                if flow not in {'operating','investing','financing'}:raise ValueError('현금 분개는 현금흐름 구분 operating/investing/financing이 필요합니다.')
                before=self.cash_state(conn,issuer)[0]
                if not 0<=before+cash_delta<=2**53-1:raise ValueError('법인 현금 잔액 또는 한도를 확인해주세요.')
                conn.execute('INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)',(issuer,))
                conn.execute('UPDATE issuer_cash_accounts SET balance=balance+? WHERE company_code=?',(cash_delta,issuer))
            ident=self.post(conn,issuer,'manual:'+uuid.uuid4().hex,description,entries,flow,reversal=reversal)
            if ident is None:raise ValueError('서로 상쇄되는 빈 분개입니다.')
        return f'✅ {issuer} 분개 #{ident} 기록 완료'

    @classmethod
    def merge(cls, conn, surviving, absorbed, now):
        b=cls.balances(conn,absorbed,now+1)
        # Prior profit transfers into retained earnings, not the survivor's current revenue.
        transferred={k:v for k,v in b.items() if ACCOUNTS[k][1] not in {'revenue','expense'}}
        transferred['retained']=transferred.get('retained',0)+sum(v for k,v in b.items() if ACCOUNTS[k][1] in {'revenue','expense'})
        cls.post(conn,surviving,'merge:'+absorbed+':'+str(now),'합병 장부 인수',list(transferred.items()),'investing',now)
        # Keep the absorbed company's historical P&L visible; close balance accounts via equity adjustment.
        terminal={k:-v for k,v in b.items() if ACCOUNTS[k][1] not in {'revenue','expense'}}
        terminal['adjustment']=terminal.get('adjustment',0)-sum(terminal.values())
        cls.post(conn,absorbed,'merged:'+str(now),'존속회사로 장부 이전',list(terminal.items()),'investing',now)

    @classmethod
    def liquidate(cls, conn, issuer, now):
        b=cls.balances(conn,issuer,now+1)
        terminal={k:-v for k,v in b.items() if ACCOUNTS[k][1] not in {'revenue','expense'}}
        terminal['adjustment']=terminal.get('adjustment',0)-sum(terminal.values())
        cls.post(conn,issuer,'liquidation:'+str(now),'법인 청산 · 잔여 장부 제거(자본조정)',list(terminal.items()),'financing',now)

    @classmethod
    def record_bond_discount(cls,conn,code,product,discount):
        issuer=cls.ensure(conn,code)
        units=cls.units(discount,cls.entity(conn,issuer)['decimal_places'])
        conn.execute('INSERT INTO financial_bond_basis VALUES (?,?,?) ON CONFLICT(product_code) DO UPDATE SET discount_units=discount_units+excluded.discount_units',(product,issuer,units))

    @classmethod
    def amortize_bond(cls,conn,code,product,now):
        row=conn.execute('SELECT * FROM financial_bond_basis WHERE product_code=?',(product,)).fetchone()
        if row and row['discount_units']:
            cls.post(conn,row['company_code'],'amortize:'+product,'회사채 발행차금 만기 상각',
                [('interest_expense',int(row['discount_units'])),('bond_discount',-int(row['discount_units']))],now=now)
            conn.execute('UPDATE financial_bond_basis SET discount_units=0 WHERE product_code=?',(product,))

    def configure(self,actor,code,config):
        if not isinstance(config,dict) or set(config)-{'enabled','revenue','cogs','sga','depreciation','tax_percent'}:
            raise ValueError('설정은 enabled, revenue, cogs, sga, depreciation, tax_percent 항목의 JSON 객체입니다.')
        if type(config.get('enabled',False)) is not bool:raise ValueError('enabled는 true/false입니다.')
        with self.db.atomic() as conn:
            self.require(conn,actor)
            issuer=self.ensure(conn,code)
            places=self.entity(conn,issuer)['decimal_places']
            normalized={}
            for key in ('revenue','cogs','sga','depreciation'):
                value=self.units(config.get(key,0),places)
                if value<0:raise ValueError('월간 영업 설정 금액은 0 이상이어야 합니다.')
                normalized[key]=value
            rate=Decimal(str(config.get('tax_percent',0)))
            if not rate.is_finite() or not 0<=rate<=100:raise ValueError('세율은 0~100입니다.')
            normalized['tax_percent']=str(rate)
            conn.execute('''INSERT INTO financial_operating_profiles VALUES (?,?,?,?) ON CONFLICT(company_code)
                DO UPDATE SET config_json=excluded.config_json,start_period=excluded.start_period,enabled=excluded.enabled''',
                (issuer,json.dumps(normalized),month(),int(config.get('enabled',False))))
        return f'✅ {issuer} 월간 영업 {"활성" if config.get("enabled") else "중지"}. 이번 달부터 적용하고 다음 달에 외상매출·비용을 자동 인식합니다. 현금 수금·지급은 회계분개에서 처리합니다.'

    def process_due(self,now=None):
        now=int(time.time() if now is None else now)
        if now<getattr(self,'_next_operating_check',0):return []
        completed=[]
        with self.db.atomic() as conn:
            profiles=conn.execute('''SELECT p.* FROM financial_operating_profiles p JOIN company c USING(company_code)
                WHERE p.enabled=1 AND c.status='active' AND p.start_period<?''',(month(now),)).fetchall()
            for profile in profiles:
                period=profile['start_period']
                config=json.loads(profile['config_json'])
                for _ in range(24):  # Catch up in bounded transactions after downtime.
                    start,end=period_bounds(period)
                    if end>now:break
                    code=profile['company_code']
                    if conn.execute('SELECT 1 FROM financial_periods WHERE company_code=? AND period>=?',(code,period)).fetchone():
                        raise ValueError(f'{code} {period} 영업 인식 전에 결산되었습니다.')
                    b=self.balances(conn,code,end)
                    depreciation=min(config['depreciation'],max(0,b.get('ppe',0)+b.get('depreciation_accumulated',0)))
                    pretax=config['revenue']-config['cogs']-config['sga']-depreciation
                    tax=int((Decimal(max(0,pretax))*Decimal(config['tax_percent'])/100).quantize(Decimal(1),rounding=ROUND_HALF_UP))
                    lines=[('receivables',config['revenue']),('revenue',-config['revenue']),
                           ('cogs',config['cogs']),('sga',config['sga']),('payables',-config['cogs']-config['sga']),
                           ('depreciation',depreciation),('depreciation_accumulated',-depreciation),('income_tax',tax),('accrued_expenses',-tax)]
                    self.post(conn,code,'operating:'+period,'관리자 설정에 따른 월간 영업(외상거래)',lines,now=end-1)
                    completed.append((code,period))
                    period=month(end)
                conn.execute('UPDATE financial_operating_profiles SET start_period=? WHERE company_code=?',(period,profile['company_code']))
        self._next_operating_check=now+60
        return completed

    @staticmethod
    def record_shares(conn,code,now):
        from database.user_db import TREASURY_USER_ID
        shares=conn.execute('''SELECT
            COALESCE((SELECT SUM(quantity) FROM user_assets WHERE asset_code=? AND user_id<>?),0)
            +COALESCE((SELECT SUM(quantity) FROM option_collateral_lots WHERE underlying_code=? AND writer_id<>?),0)''',
            (code,TREASURY_USER_ID,code,TREASURY_USER_ID)).fetchone()[0]
        conn.execute('INSERT INTO financial_share_history VALUES (?,?,?) '
                     'ON CONFLICT(company_code,recorded_epoch) DO UPDATE SET '
                     'quantity_milli=excluded.quantity_milli',(code,now,round(shares*1000)))

    @classmethod
    def eps(cls,conn,code,start,end,profit,opened):
        if opened>start:return None,None
        until=min(end,int(time.time()))
        if until<=start:return None,None
        prior=conn.execute('SELECT quantity_milli FROM financial_share_history WHERE company_code=? AND recorded_epoch<=? ORDER BY recorded_epoch DESC LIMIT 1',(code,start)).fetchone()
        if prior is None:return None,None
        quantity=int(prior[0]);weighted=0;cursor=start
        for row in conn.execute('SELECT recorded_epoch,quantity_milli FROM financial_share_history WHERE company_code=? AND recorded_epoch>? AND recorded_epoch<? ORDER BY recorded_epoch',(code,start,until)):
            weighted+=quantity*(row[0]-cursor);quantity=int(row[1]);cursor=row[0]
        weighted+=quantity*(until-cursor)
        average=weighted/(until-start)/1000
        ent=cls.entity(conn,code)
        preferred=0
        rows=conn.execute('''SELECT d.gross_amount,d.quote_currency,
            COALESCE((SELECT SUM(c.gross_amount) FROM preferred_dividend_claims c WHERE c.distribution_id=d.distribution_id),0) unpaid
            FROM dividend_distributions d JOIN company s ON s.company_code=d.asset_code
            WHERE s.share_class='preferred' AND s.parent_company_code=? AND d.created_epoch>=? AND d.created_epoch<?''',(code,start,until))
        for row in rows:
            preferred+=cls.converted(conn,row['gross_amount']+row['unpaid'],row['quote_currency'],ent['currency'],ent['decimal_places'])
        return average,(profit-preferred)/average if average>0 else None
