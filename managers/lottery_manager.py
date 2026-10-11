"""Weekly game lottery: frozen rules, funded prizes, durable draws and payouts."""
import hashlib
import json
import secrets
import time
from datetime import datetime,timedelta,timezone
from database.user_db import TREASURY_USER_ID
from functions.tax_calc import calculate_income_tax
from managers.user_manager import UserManager

KST = timezone(timedelta(hours=9))
MAX_MONEY = 2**53-1
DEFAULTS = {'ticket_price':1000,'max_tickets':100,'prize_percent':70,'treasury_percent':20,
            'close_weekday':5,'close_hour':20,'close_minute':0,'draw_delay_seconds':300}
WEIGHTS = (50,10,15,15,10)


class LotteryManager:
    def __init__(self,db):
        self.db = db

    @staticmethod
    def numbers(text):
        if not text or str(text).strip() in {'자동','auto'}:
            return None
        try:
            values = [int(n) for n in str(text).replace(',',' ').split()]
        except ValueError:
            raise ValueError('1~45의 서로 다른 번호 6개를 공백 또는 쉼표로 입력해주세요.') from None
        if len(values)!=6 or len(set(values))!=6 or any(not 1<=n<=45 for n in values):
            raise ValueError('1~45의 서로 다른 번호 6개를 입력해주세요.')
        return sorted(values)

    @staticmethod
    def draw_numbers(secret,entropy):
        """Publicly reproducible SHA-256 stream with unbiased rejection sampling."""
        seed = bytes.fromhex(secret)+bytes.fromhex(entropy)
        pool,out,counter = list(range(1,46)),[],0
        while len(out)<7:
            digest = hashlib.sha256(seed+counter.to_bytes(8,'big')).digest()
            counter += 1
            value = int.from_bytes(digest,'big')
            limit = 2**256-(2**256%len(pool))
            if value>=limit:
                continue
            out.append(pool.pop(value%len(pool)))
        return sorted(out[:6]),out[6]

    @staticmethod
    def rank(numbers,winning,bonus):
        matched = len(set(numbers)&set(winning))
        return 1 if matched==6 else 2 if matched==5 and bonus in numbers else 3 if matched==5 else 4 if matched==4 else 5 if matched==3 else 0

    def _rules(self,conn):
        row = conn.execute("SELECT value FROM settings WHERE key='lottery_config'").fetchone()
        rules = {**DEFAULTS,**(json.loads(row[0]) if row else {})}
        self.validate(rules)
        return rules

    @staticmethod
    def validate(rules):
        if set(rules)-set(DEFAULTS):
            raise ValueError('지원하지 않는 복권 설정입니다.')
        limits = {'ticket_price':(1,1000000),'max_tickets':(1,1000),'prize_percent':(0,100),
                  'treasury_percent':(0,100),'close_weekday':(0,6),'close_hour':(0,23),
                  'close_minute':(0,59),'draw_delay_seconds':(60,86400)}
        for key,(lo,hi) in limits.items():
            value = rules[key]
            if isinstance(value,bool) or not isinstance(value,int) or not lo<=value<=hi:
                raise ValueError(f'{key}는 {lo}~{hi}의 정수여야 합니다.')
        if rules['prize_percent']+rules['treasury_percent']>100:
            raise ValueError('상금·국고 배분의 합은 100% 이하여야 합니다.')

    def configure(self,actor,updates):
        if not isinstance(updates,dict):
            raise ValueError('설정은 JSON 객체로 입력해주세요.')
        with self.db.atomic() as conn:
            admin = conn.execute('SELECT role FROM users WHERE user_id=?',(str(actor),)).fetchone()
            if not admin or str(admin['role']).strip().lower()!='admin':
                raise ValueError('role=admin 관리자만 설정할 수 있습니다.')
            rules = {**self._rules(conn),**updates}
            self.validate(rules)
            conn.execute("INSERT INTO settings(key,value) VALUES ('lottery_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(rules),))
        return '✅ 다음에 개설되는 회차부터 복권 설정을 적용합니다. 판매 중인 회차는 기존 규칙을 유지합니다.'

    def _ensure(self,conn,now):
        latest = conn.execute('SELECT * FROM lottery_rounds ORDER BY round_id DESC LIMIT 1').fetchone()
        if latest and latest['status']!='settled':
            return latest
        rules = self._rules(conn)
        current = datetime.fromtimestamp(now,KST)
        close = current.replace(hour=rules['close_hour'],minute=rules['close_minute'],second=0,microsecond=0)
        close += timedelta(days=(rules['close_weekday']-close.weekday())%7)
        if close.timestamp()<=now:
            close += timedelta(days=7)
        secret = secrets.token_hex(32)
        commitment = hashlib.sha256(bytes.fromhex(secret)).hexdigest()
        pools = json.loads(latest['pools_json']) if latest else [0]*5
        created = conn.execute('INSERT INTO lottery_rounds(close_epoch,draw_epoch,rules_json,secret,commitment,pools_json) VALUES (?,?,?,?,?,?)',
            (int(close.timestamp()),int(close.timestamp())+rules['draw_delay_seconds'],json.dumps(rules),secret,commitment,json.dumps(pools))).lastrowid
        return conn.execute('SELECT * FROM lottery_rounds WHERE round_id=?',(created,)).fetchone()

    @staticmethod
    def _public(row):
        result = dict(row)
        override = result.pop('admin_numbers_json', None)
        result['admin_override'] = bool(override) if result['status'] in {'drawn','settled'} else False
        result['rules'] = json.loads(result.pop('rules_json'))
        result['pools'] = json.loads(result.pop('pools_json'))
        if result['status'] not in {'drawn','settled'}:
            result.pop('secret',None)
            result.pop('entropy',None)
        result['winning'] = json.loads(result.pop('winning_json') or '[]')
        result['result'] = json.loads(result.pop('result_json') or '{}')
        return result

    def current(self,now=None):
        now = time.time() if now is None else now
        with self.db.atomic() as conn:
            return self._public(self._ensure(conn,now))

    def purchase(self,user_id,quantity,numbers,request_id,now=None,expected_round=None):
        now = int(time.time() if now is None else now)
        if isinstance(quantity,bool) or not isinstance(quantity,int) or not 1<=quantity<=1000:
            raise ValueError('구매 수량은 1~1000장의 정수여야 합니다.')
        chosen = self.numbers(numbers)
        request_id = str(request_id)
        if not request_id or len(request_id)>100:
            raise ValueError('구매 요청 ID가 올바르지 않습니다.')
        with self.db.atomic() as conn:
            user = conn.execute('SELECT status,cash FROM users WHERE user_id=?',(str(user_id),)).fetchone()
            if not user or (user['status']!='active' and str(user_id)!=TREASURY_USER_ID):
                raise ValueError('등록된 활성 사용자만 구매할 수 있습니다.')
            round = self._ensure(conn,now)
            if expected_round is not None and round['round_id']!=expected_round:
                raise ValueError('회차가 변경되었습니다. /복권구매를 다시 실행해주세요.')
            if round['status']!='selling' or now>=round['close_epoch']:
                raise ValueError('판매가 마감되었습니다. 추첨 후 다음 회차를 구매해주세요.')
            rules = json.loads(round['rules_json'])
            count = conn.execute('SELECT COUNT(*) FROM lottery_tickets WHERE user_id=? AND round_id=?',(str(user_id),round['round_id'])).fetchone()[0]
            if count+quantity>rules['max_tickets']:
                raise ValueError(f"회차별 구매 한도는 {rules['max_tickets']}장입니다.")
            price = rules['ticket_price']
            total = price*quantity
            if round['sales_total']+total>MAX_MONEY:
                raise ValueError('회차별 판매 한도를 초과했습니다.')
            if not UserManager._change_cash(conn, user_id, -total):
                raise ValueError('복권을 구매할 지갑 잔액이 부족합니다.')
            prize = price*rules['prize_percent']//100
            treasury = price*rules['treasury_percent']//100
            reserve = price-prize-treasury
            changes = [prize*w//100 for w in WEIGHTS]
            changes[0] += prize-sum(changes)
            pools = json.loads(round['pools_json'])
            pools = [a+b*quantity for a,b in zip(pools,changes)]
            treasury_amount = treasury * quantity
            if not UserManager._change_cash(
                conn, TREASURY_USER_ID, treasury_amount, maximum=MAX_MONEY
            ):
                raise ValueError('국고 잔액 한도를 초과했습니다.')
            rng = secrets.SystemRandom()
            tickets = []
            for _ in range(quantity):
                selected = chosen or sorted(rng.sample(range(1,46),6))
                ticket = conn.execute('INSERT INTO lottery_tickets(round_id,user_id,numbers_json,price,request_id,created_epoch) VALUES (?,?,?,?,?,?)',
                    (round['round_id'],str(user_id),json.dumps(selected),price,request_id,now)).lastrowid
                tickets.append({'ticket_id':ticket,'numbers':selected})
            conn.execute('UPDATE lottery_rounds SET sales_total=sales_total+?,pools_json=?,reserve_total=reserve_total+?,treasury_total=treasury_total+? WHERE round_id=?',
                (total,json.dumps(pools),reserve*quantity,treasury*quantity,round['round_id']))
            return {'round_id':round['round_id'],'price':price,'tickets':tickets}

    def process_due(self,now=None):
        now = int(time.time() if now is None else now)
        # A selling round spends almost all of its lifetime before the close.
        # Keep those frequent worker polls read-only instead of reserving the
        # database writer every five seconds.
        with self.db.readonly() as conn:
            pending = conn.execute(
                "SELECT status,close_epoch,draw_epoch FROM lottery_rounds "
                "ORDER BY round_id DESC LIMIT 1"
            ).fetchone()
        if pending and (
            (pending['status'] == 'selling' and now < int(pending['close_epoch']))
            or (pending['status'] == 'closed' and now < int(pending['draw_epoch']))
        ):
            return None
        with self.db.atomic() as conn:
            round = self._ensure(conn,now)
            if now>=round['close_epoch'] and round['status']=='selling':
                conn.execute("UPDATE lottery_rounds SET status='closed' WHERE round_id=?",(round['round_id'],))
            if now>=round['draw_epoch'] and round['status'] in {'selling','closed'}:
                entropy = secrets.token_hex(32)
                winning,bonus = self.draw_numbers(round['secret'],entropy)
                if round['admin_numbers_json']:
                    override = json.loads(round['admin_numbers_json'])
                    winning,bonus = override['winning'],override['bonus']
                conn.execute("UPDATE lottery_rounds SET status='drawn',entropy=?,winning_json=?,bonus=? WHERE round_id=?",(entropy,json.dumps(winning),bonus,round['round_id']))
        with self.db.atomic() as conn:
            round = conn.execute('SELECT * FROM lottery_rounds ORDER BY round_id DESC LIMIT 1').fetchone()
            if round['status']!='drawn':
                return None
            winning = json.loads(round['winning_json'])
            pools = json.loads(round['pools_json'])
            tickets = conn.execute('SELECT * FROM lottery_tickets WHERE round_id=?',(round['round_id'],)).fetchall()
            groups = [[] for _ in range(5)]
            for ticket in tickets:
                rank = self.rank(json.loads(ticket['numbers_json']),winning,round['bonus'])
                conn.execute('UPDATE lottery_tickets SET rank=? WHERE ticket_id=?',(rank,ticket['ticket_id']))
                if rank:
                    groups[rank-1].append(ticket)
            rate = conn.execute("SELECT value FROM settings WHERE key='tax_percent'").fetchone()[0]
            result = {}
            for rank,winners in enumerate(groups,1):
                gross = pools[rank-1]//len(winners) if winners else 0
                paid = 0
                for ticket in winners:
                    tax = calculate_income_tax(gross,float(rate)/100)
                    net = gross-tax
                    if not UserManager._change_cash(
                        conn, ticket['user_id'], net, maximum=MAX_MONEY
                    ):
                        raise ValueError('당첨자의 계정 또는 잔액 한도를 확인해주세요. 추첨 결과를 보존하고 지급을 보류합니다.')
                    if not UserManager._change_cash(
                        conn, TREASURY_USER_ID, tax, maximum=MAX_MONEY
                    ):
                        raise ValueError('국고 잔액 한도를 초과하여 지급을 보류합니다.')
                    paid += gross
                pools[rank-1] -= paid
                result[str(rank)] = {'winners':len(winners),'gross_per_ticket':gross,'paid':paid,'carry':pools[rank-1]}
            conn.execute("UPDATE lottery_rounds SET status='settled',pools_json=?,result_json=?,settled_epoch=? WHERE round_id=?",(json.dumps(pools),json.dumps(result),now,round['round_id']))
            settled = round['round_id']
            self._ensure(conn,now)
            return settled

    def tickets(self,user_id,round_id=None):
        return [dict(r) for r in self.db.lottery.execute('SELECT t.*,NULL AS gross,NULL AS tax,NULL AS net FROM lottery_tickets t WHERE t.user_id=? AND (? IS NULL OR t.round_id=?) ORDER BY t.ticket_id DESC',(str(user_id),round_id,round_id))]

    def result(self,round_id=None):
        row = self.db.lottery.execute_one('SELECT * FROM lottery_rounds WHERE round_id=?',(round_id,)) if round_id is not None else self.db.lottery.execute_one("SELECT * FROM lottery_rounds WHERE status='settled' ORDER BY round_id DESC LIMIT 1")
        return self._public(row) if row else None
