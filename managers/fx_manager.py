"""Atomic currency exchange. Foreign amounts are integer millionths of a unit."""
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING
from database.user_db import TREASURY_USER_ID

SCALE = 1_000_000
MAX_UNITS = 2**63 - 1

def amount_units(value):
    try:
        amount = Decimal(str(value))
        units = amount * SCALE
        if not amount.is_finite() or units != units.to_integral_value() or not 1 <= units <= MAX_UNITS:
            raise ValueError()
        return int(units)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("금액은 소수점 6자리 이내의 양수로 입력해주세요.") from None

def display_units(units):
    return f"{Decimal(units)/SCALE:,.6f}".rstrip('0').rstrip('.')

class FXManager:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _rate(conn, code):
        row = conn.execute("SELECT * FROM currencies WHERE code=? AND status='active'", (code,)).fetchone()
        if not row:
            raise ValueError(f"거래 가능한 통화가 아닙니다: {code}")
        if code == 'NWX':
            return 1
        asset = conn.execute("SELECT price FROM market_assets WHERE code=? AND status='active'", (row['asset_code'],)).fetchone()
        if not asset or asset['price'] <= 0:
            raise ValueError("환율을 조회할 수 없습니다.")
        return int(asset['price'])

    @staticmethod
    def _user(conn, user_id):
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),)).fetchone()
        if not user or (user['status'] != 'active' and str(user_id) != TREASURY_USER_ID):
            raise ValueError("등록된 활성 사용자만 이용할 수 있습니다.")
        return user

    def national_currency(self, user_id):
        with self.db.atomic() as conn:
            user = self._user(conn, user_id)
            row = conn.execute("SELECT currency_code FROM country_currencies n JOIN currencies c ON c.code=n.currency_code WHERE nationality=? AND c.status='active'", (user['nationality'],)).fetchone()
            return user['nationality'], row['currency_code'] if row else 'NWX'

    def rates(self):
        with self.db.atomic() as conn:
            return [{**dict(r), 'rate': self._rate(conn, r['code'])}
                    for r in conn.execute("SELECT * FROM currencies WHERE status='active' ORDER BY code")]

    def open_account(self, user_id, product_code):
        with self.db.atomic() as conn:
            self._user(conn, user_id)
            product = conn.execute("SELECT * FROM fx_products WHERE product_code=? AND status='active'", (product_code,)).fetchone()
            if not product:
                raise ValueError("외환통장 상품 코드를 확인해주세요.")
            self._rate(conn, product['currency_code'])
            if conn.execute("SELECT 1 FROM fx_accounts WHERE user_id=? AND currency_code=?", (str(user_id), product['currency_code'])).fetchone():
                raise ValueError("같은 통화의 외환통장이 이미 있습니다.")
            number = 'FXA' + uuid.uuid4().hex[:16].upper()
            conn.execute("INSERT INTO fx_accounts(account_number,user_id,product_code,currency_code,last_interest_epoch) "
                         "VALUES (?,?,?,?,?)", (number, str(user_id), product_code, product['currency_code'], time.time()))
            return number

    @staticmethod
    def _settle(conn, user_id, now):
        rows = conn.execute("SELECT a.*,p.annual_rate FROM fx_accounts a JOIN fx_products p USING(product_code) WHERE user_id=?", (str(user_id),)).fetchall()
        for row in rows:
            elapsed = max(0, now - row['last_interest_epoch'])
            exact = (Decimal(row['balance_units']) * Decimal(str(row['annual_rate'])) / 100
                     * Decimal(str(elapsed)) / Decimal(365*86400) + Decimal(row['interest_remainder']))
            credit = int(exact.to_integral_value(rounding=ROUND_FLOOR))
            if row['balance_units'] + credit > MAX_UNITS:
                raise ValueError("외환 잔액 한도를 초과합니다.")
            conn.execute("UPDATE fx_accounts SET balance_units=balance_units+?,interest_remainder=?,last_interest_epoch=? "
                         "WHERE account_number=?", (credit, str(exact-credit), max(now,row['last_interest_epoch']), row['account_number']))

    def exchange(self, user_id, source, target, amount):
        source, target = str(source).strip().upper(), str(target).strip().upper()
        units = amount_units(amount)
        if source == target:
            raise ValueError("서로 다른 통화를 선택해주세요.")
        if source == 'NWX' and units % SCALE:
            raise ValueError("NWX는 정수 금액만 사용할 수 있습니다.")
        now = time.time()
        with self.db.atomic() as conn:
            from managers.market_session_manager import MarketSessionManager
            halt = MarketSessionManager(self.db).trading_error(conn=conn, now=now)
            if halt:
                raise ValueError(halt)
            self._user(conn, user_id)
            self._settle(conn, user_id, now)
            sr, tr = self._rate(conn, source), self._rate(conn, target)
            fee_rate = Decimal(str(conn.execute("SELECT value FROM settings WHERE key='fx_fee_percent'").fetchone()[0]))
            if not fee_rate.is_finite() or not 0 <= fee_rate <= 100:
                raise ValueError("환전 수수료 설정이 올바르지 않습니다.")
            value = Decimal(units) * sr / SCALE
            fee = int((value * fee_rate / 100).to_integral_value(rounding=ROUND_CEILING))
            output = int(((value-fee) / tr * SCALE).to_integral_value(rounding=ROUND_FLOOR))
            if target == 'NWX':
                output = output // SCALE * SCALE
            if output <= 0 or output > MAX_UNITS:
                raise ValueError("환전 결과가 최소 단위 미만이거나 한도를 초과합니다.")
            if source == 'NWX':
                changed = conn.execute("UPDATE users SET cash=cash-? WHERE user_id=? AND cash>=?", (units//SCALE,str(user_id),units//SCALE))
            else:
                changed = conn.execute("UPDATE fx_accounts SET balance_units=balance_units-? WHERE user_id=? AND currency_code=? AND balance_units>=?", (units,str(user_id),source,units))
            if changed.rowcount != 1:
                raise ValueError("잔액이 부족하거나 출금 외환통장이 없습니다.")
            if target == 'NWX':
                changed = conn.execute("UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?", (output//SCALE,str(user_id),MAX_UNITS-output//SCALE))
            else:
                changed = conn.execute("UPDATE fx_accounts SET balance_units=balance_units+? WHERE user_id=? AND currency_code=? AND balance_units<=?", (output,str(user_id),target,MAX_UNITS-output))
            if changed.rowcount != 1:
                raise ValueError("입금 외환통장을 먼저 개설하거나 잔액 한도를 확인해주세요.")
            treasury = conn.execute("UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?", (fee,TREASURY_USER_ID,MAX_UNITS-fee))
            if treasury.rowcount != 1:
                raise ValueError("국고 잔액 한도를 초과했습니다.")
            return f"✅ {display_units(units)} {source} → {display_units(output)} {target} · 수수료 {fee:,} NWX"

    def accounts(self, user_id):
        with self.db.atomic() as conn:
            self._user(conn, user_id)
            self._settle(conn, user_id, time.time())
            return [{**dict(r), 'value_nwx': int(Decimal(r['balance_units']) * self._rate(conn,r['currency_code']) / SCALE) if r['balance_units'] else 0}
                    for r in conn.execute("SELECT a.*,p.name AS product_name,p.annual_rate FROM fx_accounts a "
                                          "LEFT JOIN fx_products p ON p.product_code=a.product_code "
                                          "WHERE a.user_id=? ORDER BY a.currency_code", (str(user_id),))]

    def transfer(self,user_id,recipient,amount,source_account=None):
        """Account-addressed transfer; a currency change quotes and settles atomically."""
        sender = str(user_id)
        recipient = str(recipient).strip()
        if recipient.casefold() in {'국고','treasury',TREASURY_USER_ID.casefold()}:
            recipient = TREASURY_USER_ID
        source_account = str(source_account or '__wallet__').strip()
        units = amount_units(amount)
        from managers.user_manager import UserManager
        interest_error = UserManager(self.db)._settle_user_interest(sender)
        if interest_error:
            raise ValueError(interest_error)
        destination = self.db.accounts.execute_one('SELECT user_id FROM accounts WHERE account_number=?',(recipient,))
        if destination and str(destination['user_id'])!=sender:
            interest_error = UserManager(self.db)._settle_user_interest(destination['user_id'])
            if interest_error:
                raise ValueError(interest_error)
        now = time.time()
        with self.db.atomic() as conn:
            self._user(conn,sender)
            def endpoint(number,owner=None):
                if number=='__wallet__' and owner is not None:
                    return ('users','cash','user_id',owner,owner,'NWX',SCALE)
                fx = conn.execute('SELECT * FROM fx_accounts WHERE account_number=?',(number,)).fetchone()
                if fx:
                    if owner is not None and str(fx['user_id'])!=owner:
                        raise ValueError('출금 계좌는 본인 명의여야 합니다.')
                    self._user(conn,fx['user_id'])
                    return ('fx_accounts','balance_units','account_number',number,str(fx['user_id']),fx['currency_code'],1)
                bank = conn.execute("SELECT * FROM accounts WHERE account_number=? AND account_type='saving'",(number,)).fetchone()
                if bank:
                    if owner is not None and str(bank['user_id'])!=owner:
                        raise ValueError('출금 계좌는 본인 명의여야 합니다.')
                    self._user(conn,bank['user_id'])
                    return ('accounts','balance','account_number',number,str(bank['user_id']),'NWX',SCALE)
                if owner is None:
                    self._user(conn,number)
                    return ('users','cash','user_id',number,number,'NWX',SCALE)
                raise ValueError('출금 계좌를 찾을 수 없습니다.')
            src,dst = endpoint(source_account,sender),endpoint(recipient)
            if src[:4]==dst[:4]:
                raise ValueError('같은 계좌로 송금할 수 없습니다.')
            if src[5]=='NWX' and units%SCALE:
                raise ValueError('NWX는 정수 금액만 사용할 수 있습니다.')
            self._settle(conn,sender,now)
            if dst[4]!=sender:
                self._settle(conn,dst[4],now)
            sr,tr = self._rate(conn,src[5]),self._rate(conn,dst[5])
            fee = 0
            if src[5]!=dst[5]:
                from managers.market_session_manager import MarketSessionManager
                halt = MarketSessionManager(self.db).trading_error(conn=conn, now=now)
                if halt:
                    raise ValueError(halt)
                setting = conn.execute("SELECT value FROM settings WHERE key='fx_fee_percent'").fetchone()
                rate = Decimal(str(setting[0]))
                if not rate.is_finite() or not 0<=rate<=100:
                    raise ValueError('환전 수수료 설정이 올바르지 않습니다.')
                value = Decimal(units)*sr/SCALE
                fee = int((value*rate/100).to_integral_value(rounding=ROUND_CEILING))
                output = int(((value-fee)/tr*SCALE).to_integral_value(rounding=ROUND_FLOOR))
            else:
                output = units
            output = output//dst[6]*dst[6]
            if not 1<=output<=MAX_UNITS:
                raise ValueError('환전 결과가 최소 단위 미만이거나 한도를 초과합니다.')
            debit,credit = units//src[6],output//dst[6]
            if src[0]=='accounts':
                debited = UserManager._debit_source(conn,sender,src[3],debit)
            else:
                debited = conn.execute(f'UPDATE {src[0]} SET {src[1]}={src[1]}-? WHERE {src[2]}=? AND {src[1]}>=?',(debit,src[3],debit)).rowcount==1
            if not debited:
                raise ValueError('출금 잔액이 부족합니다.')
            if dst[0]=='accounts':
                changed = conn.execute('UPDATE accounts SET balance=balance+?,principal=COALESCE(principal,0)+?,last_payment_epoch=?,last_payment=CURRENT_TIMESTAMP WHERE account_number=? AND balance<=?',(credit,credit,now,dst[3],MAX_UNITS-credit))
            else:
                changed = conn.execute(f'UPDATE {dst[0]} SET {dst[1]}={dst[1]}+? WHERE {dst[2]}=? AND {dst[1]}<=?',(credit,dst[3],MAX_UNITS-credit))
            if changed.rowcount!=1:
                raise ValueError('입금 계좌 잔액 한도를 초과했습니다.')
            changed = conn.execute('UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?',(fee,TREASURY_USER_ID,MAX_UNITS-fee))
            if changed.rowcount!=1:
                raise ValueError('국고 잔액 한도를 초과했습니다.')
            return f'✅ 송금 {display_units(units)} {src[5]} → {display_units(output)} {dst[5]} · 환전 수수료 {fee:,} NWX'
