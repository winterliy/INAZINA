"""Company journals and reports. No share-price-derived fictional operating income."""
from functions.exact_money import ExactDecimal, native_value
from functions.amount_limits import MAX_NATIVE, MAX_MINOR
from functions.money import base_currency_code
import json
import hashlib
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from database.db_manager import DBManager
from database.financial_db import ACCOUNTS
from functions.time_utils import validate_epoch

KST = ZoneInfo('Asia/Seoul')
MAX_UNITS = MAX_MINOR  # Exact currency smallest-unit boundary.
# Cash and tracked issuance accounts must remain reconciled with actual game settlement.
PROTECTED_ACCOUNTS = frozenset({'restricted_cash','common_capital','preferred_capital','bonds','els_liability','dividend_payable','opening_adjustment','treasury_receivable'})


def month(epoch=None):
    try:
        epoch = validate_epoch(epoch)
    except ValueError as exc:
        raise ValueError('회계 시각은 유효한 UNIX epoch이어야 합니다.') from exc
    return datetime.fromtimestamp(epoch, KST).strftime('%Y-%m')


def period_bounds(period):
    try:
        start = datetime.strptime(period, '%Y-%m').replace(tzinfo=KST)
        if start.strftime('%Y-%m') != period:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('기간은 YYYY-MM 형식으로 입력해주세요.') from None
    end = (start + timedelta(days=32)).replace(day=1)
    return int(start.timestamp()), int(end.timestamp())


def cls_money(units, places):
    return f'{ExactDecimal(units) / 10 ** places:,.{places}f}'


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
        now = int(validate_epoch(now))
        prior = conn.execute('SELECT entry_id FROM financial_entries WHERE company_code=? AND source_key=?', (code,key)).fetchone()
        if prior:
            from managers.accounting_journal import AccountingJournal
            AccountingJournal.rebuild_from_subledgers(conn)
            return prior[0]
        combined = {}
        for account, amount in lines:
            if account not in ACCOUNTS or isinstance(amount,bool) or not isinstance(amount,int):
                raise ValueError('계정코드 또는 정수 최소단위 금액을 확인해주세요.')
            combined[account] = combined.get(account,0)+amount
        combined = {k:v for k,v in combined.items() if v}
        if not combined:
            return None
        if len(combined)<2 or sum(combined.values()) or any(abs(v)>MAX_UNITS for v in combined.values()):
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
        entity = cls.entity(conn, code)
        from managers.accounting_journal import AccountingJournal
        common_lines = []
        for account, signed_units in combined.items():
            AccountingJournal.register_mapping(conn, "company", account, account)
            common_lines.append((account, "debit" if signed_units > 0 else "credit", abs(signed_units)))
        AccountingJournal.post(
            conn, ledger_scope="company", entity_type="company", entity_id=code,
            source_key=key, currency_code=entity['currency'],
            decimal_places=int(entity['decimal_places']), recorded_epoch=now,
            lines=common_lines, description=description,
        )
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
        now = int(validate_epoch(now))
        company = cls.resolve(conn,code)
        issuer = company['company_code']
        existing = cls.entity(conn,issuer)
        currency = company.get('quote_currency') or base_currency_code(conn)
        if existing:
            if existing['currency']!=currency:
                raise ValueError('회계 원장 개설 후 기준통화를 변경할 수 없습니다.')
            return issuer
        precision = conn.execute('SELECT decimal_places FROM currencies WHERE code=?', (currency,)).fetchone()
        places = int(precision[0]) if precision else (0 if currency==base_currency_code(conn) else 6)
        conn.execute('INSERT INTO financial_entities VALUES (?,?,?,?)', (issuer,currency,places,now))
        cash = conn.execute('SELECT * FROM issuer_cash_accounts WHERE company_code=?', (issuer,)).fetchone()
        lines = []
        if cash:
            lines += [(k,cls.converted(conn,cash[col],base_currency_code(conn),currency,places)) for k,col in [('cash','balance'),('restricted_cash','locked_amount')]]
        shares = conn.execute('SELECT capital,share_class,quote_currency FROM company WHERE company_code=? OR parent_company_code=?', (issuer,issuer)).fetchall()
        for share in shares:
            lines.append(('preferred_capital' if share['share_class']=='preferred' else 'common_capital', -cls.converted(conn,share['capital'] or 0,share['quote_currency'] or base_currency_code(conn),currency,places)))
        debt_rows=conn.execute("""SELECT COALESCE(p.quote_currency,base_currency_code()) currency,SUM(a.quantity*p.face_value) amount
            FROM investment p JOIN user_assets a ON a.asset_code=p.product_code
            WHERE p.product_type='corporate_bond' AND p.status IN ('active','defaulted')
            AND p.issuer_code IN (SELECT company_code FROM company WHERE company_code=? OR parent_company_code=?)
            GROUP BY COALESCE(p.quote_currency,base_currency_code())""",(issuer,issuer))
        for row in debt_rows:lines.append(('bonds',-cls.converted(conn,row['amount'],row['currency'],currency,places)))
        arrears=conn.execute("""SELECT quote_currency currency,SUM(gross_amount-paid_amount) amount
            FROM preferred_dividend_claims WHERE issuer_code=? AND funding_source='issuer'
            AND status IN ('unpaid','partial') GROUP BY quote_currency""",(issuer,))
        for row in arrears:lines.append(('dividend_payable',-cls.converted(conn,row['amount'],row['currency'],currency,places)))
        els=conn.execute('SELECT COALESCE(SUM(principal_amount),0) FROM els_products WHERE issuer_code=?',(issuer,)).fetchone()[0]
        lines.append(('els_liability',-cls.converted(conn,els,base_currency_code(conn),currency,places)))
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

    @classmethod
    def reconcile_sources(cls, conn, code, positions=None, now=None):
        """Append current source differences; unidentified changes are equity, not income."""
        now = int(validate_epoch(now))
        issuer = cls.ensure(conn, code, now)
        if conn.execute('SELECT 1 FROM financial_periods WHERE company_code=? AND period>=?',
                        (issuer, month(now))).fetchone():
            return None
        ent = cls.entity(conn, issuer)
        currency, places = ent['currency'], int(ent['decimal_places'])
        base = base_currency_code(conn)
        def source_units(value, source_currency):
            if source_currency == currency:
                value = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
            return cls.converted(conn, value, source_currency, currency, places)
        native = lambda value: source_units(value, base)
        books = cls.balances(conn, issuer, now + 1)
        cash = cls.cash_state(conn, issuer)
        targets = {'cash': native(cash[0]), 'restricted_cash': native(cash[1])}
        for account, share_class in (('common_capital', 'common'), ('preferred_capital', 'preferred')):
            targets[account] = -sum(source_units(r['capital'] or 0, r['quote_currency'] or base) for r in conn.execute(
                'SELECT capital,quote_currency FROM company WHERE (company_code=? OR parent_company_code=?) AND share_class=?',
                (issuer, issuer, share_class)))
        targets['bonds'] = -sum(source_units(Decimal(str(r['quantity'])) * Decimal(str(r['face_value'])),
            r['quote_currency'] or base) for r in conn.execute(
            "SELECT a.quantity,p.face_value,p.quote_currency FROM investment p JOIN user_assets a ON a.asset_code=p.product_code "
            "WHERE p.product_type='corporate_bond' AND p.status IN ('active','defaulted') "
            "AND p.issuer_code IN (SELECT company_code FROM company WHERE company_code=? OR parent_company_code=?)", (issuer, issuer)))
        targets['dividend_payable'] = -sum(source_units(Decimal(str(r['gross_amount'])) - Decimal(str(r['paid_amount'])),
            r['quote_currency'] or base) for r in conn.execute(
            "SELECT gross_amount,paid_amount,quote_currency FROM preferred_dividend_claims WHERE issuer_code=? "
            "AND funding_source='issuer' AND status IN ('unpaid','partial')", (issuer,)))
        targets['els_liability'] = -sum(native(r[0]) for r in conn.execute(
            'SELECT principal_amount FROM els_products WHERE issuer_code=?', (issuer,)))
        if positions is not None:
            for account, key, sign in (('short_investments', 'investments', 1), ('treasury_stock', 'treasury', 1), ('short_debt', 'debt', -1)):
                manual = sum(int(r[0]) for r in conn.execute(
                    "SELECT l.amount FROM financial_lines l JOIN financial_entries e USING(entry_id) "
                    "WHERE e.company_code=? AND e.status='posted' AND e.source_key LIKE 'manual:%' AND l.account=?", (issuer, account)))
                targets[account] = sign * native(positions[key]) + manual
        lines = [(k, v - books.get(k, 0)) for k, v in targets.items() if v != books.get(k, 0)]
        if not lines:
            return None
        difference = -sum(v for _, v in lines)
        if difference:
            lines.append(('adjustment', difference))
        cursor = conn.execute('SELECT COALESCE(MAX(entry_id),0) FROM financial_entries WHERE company_code=?', (issuer,)).fetchone()[0]
        digest = hashlib.sha256(json.dumps([cursor, sorted(targets.items())]).encode()).hexdigest()
        return cls.post(conn, issuer, 'sources:' + digest, '실제 잔액 자동 대사 · 미확인 차이는 자본조정', lines, 'adjustment', now)

    def initialize(self):
        with self.db.atomic() as conn:
            for row in conn.execute("SELECT company_code FROM company WHERE share_class='common' AND status='active'").fetchall():
                self.ensure(conn,row[0])

    @staticmethod
    def balances(conn, code, before):
        before = validate_epoch(before)
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
    def build(cls, conn, code, period, consolidate=True):
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
        pretax = operating-sum(changes[k] for k in ('other_income','interest_income','investment_gain','other_expense',
                                                    'investment_loss','interest_expense','overdue_interest','currency_rounding'))
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
        r('이자보상배율',operating,changes['interest_expense']+changes['overdue_interest'],False)
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
        cash_matches = None if ent['currency']!=base_currency_code(conn) or period!=month() else b.get('cash',0)==actual[0]*10**ent['decimal_places']
        count = conn.execute("SELECT COUNT(*) FROM financial_entries WHERE company_code=? AND period=? AND status='posted'",(code,period)).fetchone()[0]
        report = dict(code=code,name=company['name'],currency=ent['currency'],places=ent['decimal_places'],period=period,
            opened_epoch=ent['opened_epoch'],balances=b,opening=opening,changes=changes,summary=s,previous=previous,
            income=dict(revenue=revenue,gross=gross,operating=operating,pretax=pretax,tax=changes['income_tax'],profit=profit,
                        ebitda=operating+changes['depreciation']+changes['amortization']),
            cash_flows=flows,capex=capex,fcf=flows.get('operating',0)-capex,eps=eps,weighted_shares=weighted_shares,ratios=ratios,entry_count=count,
            checks=dict(balance_sheet=s['assets']==s['liabilities']+s['equity'],cash_flow=cash_difference==0,
                        live_cash=cash_matches),closed=False,
            notes=['회계 기준 · 월간 비율(연율화하지 않음)',
                   '현재 월은 실제 현금·발행채무를 자동 대사합니다. 원본으로 확인되지 않는 차이는 손익 대신 기타 자본조정에 표시합니다.',
                   '도입 전 미확인 잔액은 자산이나 과거 이익으로 추정하지 않습니다.',
                   '총자본은 총자산과 동일하게 정의합니다. 평균 잔액이 없는 최초 월의 회전율·ROA·ROE는 미산출입니다.',
                   'EPS는 우선주 확정배당 차감 후 보통주 시간가중 유통주식수로 계산합니다. 국고 보유분 제외, 담보주식 포함. 완전한 기간 이력 없으면 미산출. FFO/AFFO는 부동산 모델 미지원',
                   '기타포괄손익 미지원: 총포괄손익은 당기순이익과 같습니다.',
                   '기존 기준 통화 법인현금은 거래 시 환율로 회사 통화에 환산합니다. 기말환산·외화 현금대사는 미지원입니다.',
                   '회사채 이자는 기존 만기 지급 시점에 인식합니다. 발행차금은 만기 일괄 상각합니다.'])
        report['consolidated'] = cls.consolidate(conn, code, period, report) if consolidate else None
        return report

    @classmethod
    def subsidiaries(cls, conn, code):
        """Companies whose voting shares are more than half held by ``code``'s capital account."""
        account = conn.execute('SELECT user_id FROM company_accounts WHERE company_code=?', (code,)).fetchone() \
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='company_accounts'").fetchone() else None
        if not account:
            return []
        positions = {}
        from functions.money import currency_precision, from_minor_units, round_currency
        for row in conn.execute(
                "SELECT c.company_code,c.name,c.total_shares,c.quote_currency,"
                "u.quantity,u.average_cost_minor_units "
                "FROM user_assets u JOIN company c ON c.company_code=u.asset_code "
                "WHERE u.user_id=? AND COALESCE(c.share_class,'common')='common' "
                "AND c.company_code<>? ORDER BY c.company_code,u.holding_id",
                (account[0], code)).fetchall():
            company_code = row["company_code"]
            currency = str(row["quote_currency"] or base_currency_code(conn))
            places = currency_precision(conn, currency)
            position = positions.setdefault(company_code, {
                "code": company_code, "name": row["name"],
                "total_shares": row["total_shares"], "held": 0.0, "cost": Decimal(0),
            })
            position["held"] += float(row["quantity"] or 0)
            position["cost"] += (
                from_minor_units(row["average_cost_minor_units"] or 0, places)
                * Decimal(str(row["quantity"] or 0))
            )
        dr_positions = conn.execute(
            "SELECT d.underlying_code,c.name,c.total_shares,d.quote_currency dr_currency,"
            "c.quote_currency underlying_currency,"
            "u.quantity,d.underlying_per_receipt,u.average_cost_minor_units "
            "FROM assets.user_assets u JOIN assets.depositary_receipts d ON d.dr_code=u.asset_code "
            "JOIN company c ON c.company_code=d.underlying_code "
            "WHERE u.user_id=? AND COALESCE(c.share_class,'common')='common' "
            "AND c.company_code<>? ORDER BY d.underlying_code,d.dr_code,u.holding_id",
            (str(account[0]), str(code)),
        ).fetchall()
        from managers.currency_manager import CurrencyManager
        dr_costs = {}
        dr_cost_currencies = {}
        for row in dr_positions:
            underlying_code = row["underlying_code"]
            underlying_currency = str(row["underlying_currency"] or base_currency_code(conn))
            dr_currency = str(row["dr_currency"] or base_currency_code(conn))
            quote = CurrencyManager.quote(conn, dr_currency, underlying_currency)
            dr_places = currency_precision(conn, dr_currency)
            dr_cost = (from_minor_units(row["average_cost_minor_units"] or 0, dr_places)
                       * Decimal(str(row["quantity"] or 0))
                       * Decimal(quote.numerator)
                       / Decimal(quote.denominator))
            position = positions.setdefault(underlying_code, {
                "code": underlying_code, "name": row["name"],
                "total_shares": row["total_shares"], "held": 0.0, "cost": Decimal(0),
            })
            position["held"] += float(row["quantity"] or 0) * float(row["underlying_per_receipt"] or 0)
            dr_costs[underlying_code] = (
                dr_costs.get(underlying_code, Decimal(0))+dr_cost
            )
            dr_cost_currencies[underlying_code] = underlying_currency
        for underlying_code, dr_cost in dr_costs.items():
            currency = dr_cost_currencies[underlying_code]
            positions[underlying_code]["cost"] += Decimal(str(
                round_currency(conn, dr_cost, currency)
            ))

        result = []
        for row in positions.values():
            own = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=?",
                               ('company:' + row['code'],)).fetchone()[0]
            own_dr = conn.execute(
                "SELECT COALESCE(SUM(u.quantity*d.underlying_per_receipt),0) "
                "FROM assets.user_assets u JOIN assets.depositary_receipts d ON d.dr_code=u.asset_code "
                "WHERE u.user_id=? AND d.underlying_code=?",
                ('company:' + row['code'], row['code']),
            ).fetchone()[0]
            outstanding = (float(row['total_shares'] or 0) - float(own or 0)
                           - float(own_dr or 0))  # treasury common shares/DRs carry no vote
            if outstanding > 0 and float(row['held']) / outstanding > 0.5:
                result.append({'code': row['code'], 'name': row['name'],
                               'ownership': float(row['held']) / outstanding,
                               'cost': ExactDecimal(row['cost'] or 0)})
        return result

    @staticmethod
    def _translation(conn, source, target, source_places, target_places):
        """Factor turning ``source`` minor units into ``target`` minor units at today's rate."""
        from fractions import Fraction
        from managers.currency_manager import CurrencyManager
        rate = Fraction(1) if source == target else CurrencyManager.quote(conn, source, target)
        return rate * Fraction(10 ** target_places, 10 ** source_places)

    @classmethod
    def consolidate(cls, conn, code, period, report, _visited=None):
        """Consolidated statements of ``code`` and every company it controls, at any depth.

        - Control: more than half of the voting shares (treasury stock excluded).
        - Each subsidiary enters with its own consolidated figures (so a subsidiary's
          subsidiaries are included), translated into the parent's currency at the
          current rate (closing-rate method).
        - The parent's investment (at cost) is replaced by the subsidiary's net
          assets: goodwill is the excess of cost over the parent's share, the outside
          share is the non-controlling interest (비지배지분).
        - Intra-group dividends and transfers, and shares a subsidiary holds in a
          group member (상호주, treated as treasury stock) are eliminated.
        """
        top = _visited is None
        visited = set(_visited or ()) | {code}
        subsidiaries = [sub for sub in cls.subsidiaries(conn, code) if sub['code'] not in visited]
        if not subsidiaries:
            return None
        places, currency = report['places'], report['currency']
        balances = dict(report['balances'])
        income = dict(report['income'])
        assets, liabilities = report['summary']['assets'], report['summary']['liabilities']
        goodwill = nci = nci_profit = 0
        members, notes, group = [], [], {code}
        for sub in subsidiaries:
            entity = cls.entity(conn, sub['code'])
            if not entity:
                notes.append(f"{sub['code']}: 회계 원장이 아직 없어 연결에서 제외했습니다. /재무로 한 번 조회하면 개설됩니다.")
                continue
            sub_report = cls.build(conn, sub['code'], period, consolidate=False)
            inner = cls.consolidate(conn, sub['code'], period, sub_report, visited) or {}
            if inner.get('subsidiaries'):
                figures = dict(inner['summary'], income=inner['income'], balances=inner['balances'],
                               profit_parent=inner['profit_attributable']['parent'],
                               profit_nci=inner['profit_attributable']['noncontrolling'])
                group |= set(inner.get('group', []))
                notes += [f"{sub['code']} › {note}" for note in inner.get('member_notes', [])]
            else:
                summary = sub_report['summary']
                figures = dict(assets=summary['assets'], liabilities=summary['liabilities'], equity=summary['equity'],
                               parent_equity=summary['equity'], noncontrolling_interest=0, goodwill=0,
                               income=sub_report['income'], balances=sub_report['balances'],
                               profit_parent=sub_report['income']['profit'], profit_nci=0)
            try:
                factor = cls._translation(conn, entity['currency'], currency, int(entity['decimal_places']), places)
                cost_factor = cls._translation(conn, sub.get('currency') or entity['currency'], currency, 0, places)
            except ValueError:
                notes.append(f"{sub['code']}: {entity['currency']} 환율이 없어 연결에서 제외했습니다.")
                continue
            convert = lambda value: int(round(value * factor))
            if entity['currency'] != currency:
                notes.append(f"{sub['code']}: {entity['currency']} 재무제표를 현재 환율로 {currency} 환산했습니다 (환산차이는 자본에 포함).")
            ownership = ExactDecimal(sub['ownership'])
            cost = int((Decimal(str(sub['cost'])) * Decimal(cost_factor.numerator) / Decimal(cost_factor.denominator)).to_integral_value(rounding=ROUND_HALF_UP))
            parent_equity = convert(figures['parent_equity'])
            share = int(round(parent_equity * ownership))
            member_goodwill = max(0, cost - share)
            member_nci = convert(figures['noncontrolling_interest']) + parent_equity - share
            for account, value in figures['balances'].items():
                if account == 'goodwill' or ACCOUNTS.get(account, ('', 'equity'))[1] not in {'equity', 'revenue', 'expense'}:
                    balances[account] = balances.get(account, 0) + convert(value)
            balances['short_investments'] = balances.get('short_investments', 0) - cost
            assets += convert(figures['assets']) - cost + member_goodwill
            liabilities += convert(figures['liabilities'])
            goodwill += member_goodwill
            nci += member_nci
            nci_profit += convert(figures['profit_nci']) + int(round(convert(figures['profit_parent']) * (1 - ownership)))
            for key in ('revenue', 'gross', 'operating', 'pretax', 'tax', 'profit', 'ebitda'):
                income[key] = income.get(key, 0) + convert(figures['income'][key])
            if cost < share:
                notes.append(f"{sub['code']}: 취득원가가 지분 순자산보다 {cls_money(share - cost, places)} 작아 염가매수차익으로 자본에 반영했습니다.")
            group.add(sub['code'])
            members.append({'code': sub['code'], 'name': sub['name'], 'parent': code, 'ownership': ownership * 100,
                            'currency': entity['currency'], 'cost': cost, 'equity': parent_equity,
                            'goodwill': member_goodwill, 'nci': member_nci})
            members += [dict(member, depth=member.get('depth', 1) + 1) for member in inner.get('subsidiaries', [])]
        if not members:
            return {'subsidiaries': [], 'notes': notes}
        eliminations = cls._eliminations(conn, group, period, currency, places) if top else {}
        profit_cut = eliminations.get('dividends', 0)
        for key in ('pretax', 'profit'):
            income[key] = income.get(key, 0) - profit_cut
        cross = eliminations.get('cross_holdings', 0)
        assets -= cross
        balances['short_investments'] = balances.get('short_investments', 0) - cross
        balances['goodwill'] = balances.get('goodwill', 0) + goodwill
        goodwill_total = balances['goodwill']
        equity_total = assets - liabilities
        result = {
            'subsidiaries': members, 'balances': balances, 'income': income, 'group': sorted(group),
            'summary': {'assets': assets, 'liabilities': liabilities, 'equity': equity_total,
                        'parent_equity': equity_total - nci, 'noncontrolling_interest': nci,
                        'goodwill': goodwill_total},
            'profit_attributable': {'parent': income['profit'] - nci_profit, 'noncontrolling': nci_profit},
            'eliminations': eliminations,
            'member_notes': notes,
        }
        result['notes'] = notes + [
            '지배력: 자기주식을 뺀 의결권 과반. 자회사의 자회사까지 모두 연결합니다.',
            '모회사 투자주식은 취득원가로 제거하고 자회사 순자산으로 대체합니다. 영업권 = 취득원가 − 지분율 × 자회사 순자산. 비지배지분 = 외부 지분율 × 자회사 순자산.',
            '외화 자회사는 현재 환율로 환산합니다(현행환율법).',
            '내부거래 제거: 그룹 내 배당금수익, 그룹 내 송금(수익·비용 상계), 자회사가 보유한 그룹 법인 주식(상호주 → 자기주식).',
        ] if top else notes
        return result

    @classmethod
    def _eliminations(cls, conn, group, period, currency, places):
        """Intra-group flows of the period and cross-holdings, in the parent's minor units."""
        start, end = period_bounds(period)
        marks = ','.join('?' for _ in group)
        factor = cls._translation(conn, base_currency_code(conn), currency, 0, places)
        rows = conn.execute(
            f"SELECT kind,COALESCE(SUM(gross_nwx),0) amount FROM company_income_log WHERE company_code IN ({marks}) "
            f"AND counterparty IN ({marks}) AND recorded_epoch>=? AND recorded_epoch<? GROUP BY kind",
            (*group, *group, start, end)).fetchall() if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='company_income_log'").fetchone() else []
        flows = {row['kind']: int(round(row['amount'] * factor)) for row in rows}
        cross = 0
        from fractions import Fraction
        from managers.currency_manager import CurrencyManager
        from functions.money import currency_precision
        for row in conn.execute(
                f"SELECT a.company_code holder,u.asset_code,u.quantity,"
                f"u.average_cost_minor_units,c.quote_currency "
                f"FROM user_assets u JOIN company_accounts a ON a.user_id=u.user_id "
                f"JOIN company c ON c.company_code=u.asset_code "
                f"WHERE a.company_code IN ({marks}) AND u.asset_code IN ({marks}) "
                f"AND u.asset_code<>a.company_code", (*group, *group)).fetchall():
            # Shares of a group member that are not the parent's consolidated investment
            # (e.g. a subsidiary holding its parent) are treasury stock of the group.
            if not any(sub['code'] == row['asset_code'] for sub in cls.subsidiaries(conn, row['holder'])):
                source_currency = str(row["quote_currency"] or base_currency_code(conn))
                source_places = currency_precision(conn, source_currency)
                rate = (Fraction(1) if source_currency == currency else
                        CurrencyManager.quote(conn, source_currency, currency))
                unit_factor = rate * Fraction(10 ** places, 10 ** source_places)
                target_minor = (
                    Decimal(int(row["average_cost_minor_units"] or 0))
                    * Decimal(str(row["quantity"] or 0))
                    * Decimal(unit_factor.numerator) / Decimal(unit_factor.denominator)
                )
                cross += int(target_minor.to_integral_value(rounding=ROUND_HALF_UP))
        return {'dividends': flows.get('dividend', 0), 'transfers': flows.get('transfer_in', 0), 'cross_holdings': cross}

    def report(self, code, period=None):
        period = period or month()
        start,end = period_bounds(period)
        if period>month():raise ValueError('미래 기간은 조회할 수 없습니다.')
        with self.db.readonly() as conn:
            issuer=self.resolve(conn,code)['company_code']
            exists=self.entity(conn,issuer)
        if not exists:
            with self.db.atomic() as conn:self.ensure(conn,code)
        if period == month():
            with self.db.atomic() as conn:self.reconcile_sources(conn, issuer)
        with self.db.readonly() as conn:
            ent=self.entity(conn,issuer)
            if end<=ent['opened_epoch']:raise ValueError('원장 도입 전 기간에는 재무자료가 없습니다.')
            saved=conn.execute('SELECT report_json FROM financial_periods WHERE company_code=? AND period=?',(issuer,period)).fetchone()
            return json.loads(saved[0]) if saved else self.build(conn,issuer,period)

    def periods(self, code):
        with self.db.readonly() as conn:
            issuer=self.resolve(conn,code)['company_code']
            return [r[0] for r in conn.execute('SELECT DISTINCT period FROM financial_entries WHERE company_code=? ORDER BY period DESC LIMIT 24',(issuer,))]

    def journal(self, code, period=None):
        """Entries of one month with their lines, for the administrator ledger editor."""
        period = period or month()
        period_bounds(period)
        with self.db.readonly() as conn:
            issuer=self.resolve(conn,code)['company_code']
            exists=self.entity(conn,issuer)
        if not exists:
            with self.db.atomic() as conn:self.ensure(conn,code)
        with self.db.readonly() as conn:
            ent=self.entity(conn,issuer)
            rows=conn.execute('''SELECT e.*, (SELECT entry_id FROM financial_entries r WHERE r.reversal_of=e.entry_id) AS reversed_by
                FROM financial_entries e WHERE e.company_code=? AND e.period=? AND e.status='posted'
                ORDER BY e.entry_id DESC LIMIT 200''',(issuer,period)).fetchall()
            lines={}
            for line in conn.execute(f'''SELECT entry_id,account,amount FROM financial_lines WHERE entry_id IN ({",".join("?"*len(rows)) or "NULL"})
                ORDER BY line_id''',[r['entry_id'] for r in rows]):
                lines.setdefault(line['entry_id'],[]).append({'account':line['account'],'amount':int(line['amount'])})
            profile=conn.execute('SELECT * FROM financial_operating_profiles WHERE company_code=?',(issuer,)).fetchone()
            closed=[r[0] for r in conn.execute('SELECT period FROM financial_periods WHERE company_code=? ORDER BY period DESC LIMIT 24',(issuer,))]
        return {
            'company':issuer,'period':period,'currency':ent['currency'],'places':int(ent['decimal_places']),
            'accounts':[{'code':k,'name':v[0],'category':v[1],'protected':k in PROTECTED_ACCOUNTS} for k,v in ACCOUNTS.items()],
            'entries':[{'id':r['entry_id'],'epoch':r['recorded_epoch'],'description':r['description'],'flow':r['cash_flow'],
                        'manual':str(r['source_key']).startswith('manual:'),'reversalOf':r['reversal_of'],'reversedBy':r['reversed_by'],
                        'lines':lines.get(r['entry_id'],[])} for r in rows],
            'profile':({**json.loads(profile['config_json']),'enabled':bool(profile['enabled']),'start_period':profile['start_period']} if profile else None),
            'closed':closed,
        }

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
            conn.execute('INSERT INTO financial_periods VALUES (?,?,?,?)',(issuer,period,int(time.time()),json.dumps(report,ensure_ascii=False,default=str)))
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
            if any(k in PROTECTED_ACCOUNTS for k,_ in entries):raise ValueError('발행자본·회사채·배당채무·기초잔액·예치금은 자동 연동 계정입니다. 원본 관리 명령어를 이용해주세요.')
            cash_delta=sum(v for k,v in entries if k=='cash')
            if cash_delta:
                if self.entity(conn,issuer)['currency']!=base_currency_code(conn):raise ValueError('외화 법인현금 직접 입력은 아직 지원하지 않습니다. 외상거래 계정을 이용해주세요.')
                if flow not in {'operating','investing','financing'}:raise ValueError('현금 분개는 현금흐름 구분 operating/investing/financing이 필요합니다.')
                before=self.cash_state(conn,issuer)[0]
                if not 0<=before+cash_delta<=MAX_NATIVE:raise ValueError('법인 현금 잔액 또는 한도를 확인해주세요.')
                conn.execute('INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)',(issuer,))
                conn.execute('UPDATE issuer_cash_accounts SET balance=balance+? WHERE company_code=?',(cash_delta,issuer))
            ident=self.post(conn,issuer,'manual:'+uuid.uuid4().hex,description,entries,flow,reversal=reversal)
            if ident is None:raise ValueError('서로 상쇄되는 빈 분개입니다.')
        return f'✅ {issuer} 분개 #{ident} 기록 완료'

    @classmethod
    def merge(cls, conn, surviving, absorbed, now):
        now = int(validate_epoch(now))
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
        now = int(validate_epoch(now))
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
        now = int(validate_epoch(now))
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
        now=int(validate_epoch(now))
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
        now = int(validate_epoch(now))
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
        start = validate_epoch(start)
        end = validate_epoch(end)
        if opened is not None:
            opened = validate_epoch(opened)
        if opened is None or opened>start:return None,None
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
