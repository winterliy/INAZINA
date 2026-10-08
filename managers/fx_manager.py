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

def display_units(units,decimal_places=None):
    if decimal_places is not None:
        return f"{Decimal(units)/SCALE:,.{int(decimal_places)}f}"
    return f"{Decimal(units)/SCALE:,.6f}".rstrip('0').rstrip('.')

class FXManager:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _rate(conn, code):
        from managers.currency_manager import CurrencyManager
        value=CurrencyManager.rate(conn,code)/CurrencyManager.rate(conn,'NWX')
        return int(value) if value==value.to_integral_value() else value

    @staticmethod
    def _user(conn, user_id):
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),)).fetchone()
        if not user or (user['status'] != 'active' and str(user_id) != TREASURY_USER_ID):
            raise ValueError("등록된 활성 사용자만 이용할 수 있습니다.")
        return user

    def national_currency(self, user_id):
        with self.db.readonly() as conn:
            user = self._user(conn, user_id)
            from managers.currency_manager import CurrencyManager
            return user['nationality'], CurrencyManager.home(conn,user_id)

    def rates(self):
        from managers.currency_manager import CurrencyReadSnapshot
        with self.db.readonly() as conn:
            snapshot = CurrencyReadSnapshot(conn)
            return [{**{k: v for k, v in row.items() if k != 'asset_price'},
                     'rate': snapshot.rate_nwx(code)} for code, row in snapshot.rows.items()]

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
        from managers.currency_manager import CurrencyManager
        source,target=str(source).strip().upper(),str(target).strip().upper()
        units=amount_units(amount)
        if source==target:raise ValueError('서로 다른 통화를 선택해주세요.')
        if source=='NWX' and units%SCALE:raise ValueError('NWX 현금은 정수 금액만 사용할 수 있습니다.')
        now=time.time()
        with self.db.atomic() as conn:
            from managers.market_session_manager import MarketSessionManager
            halt=MarketSessionManager(self.db).trading_error(conn=conn,now=now)
            if halt:raise ValueError(halt)
            self._user(conn,user_id);self._settle(conn,user_id,now)
            CurrencyManager.validate_units(conn,units,source)
            sr,tr=self._rate(conn,source),self._rate(conn,target)
            setting=conn.execute("SELECT value FROM settings WHERE key='fx_fee_percent'").fetchone()
            rate=Decimal(str(setting[0]))
            if not rate.is_finite() or not 0<=rate<=100:raise ValueError('환전 수수료 설정이 올바르지 않습니다.')
            fee=CurrencyManager.fee(conn,units,source,rate)
            output=CurrencyManager.exchange_amount(conn,units,source,target,fee)
            if target=='NWX':output=output//SCALE*SCALE
            if not 1<=output<=MAX_UNITS:raise ValueError('환전 결과가 최소 단위 미만이거나 한도를 초과합니다.')
            account=conn.execute('SELECT * FROM fx_accounts WHERE user_id=? AND currency_code=?',(str(user_id),source)).fetchone()
            if CurrencyManager.balance(conn,user_id,source)>=units:
                CurrencyManager.change(conn,user_id,source,-units)
            elif account and account['balance_units']>=units:
                conn.execute('UPDATE fx_accounts SET balance_units=balance_units-? WHERE account_number=?',(units,account['account_number']))
            else:
                CurrencyManager.change(conn,user_id,source,-units)
            account=conn.execute('SELECT * FROM fx_accounts WHERE user_id=? AND currency_code=?',(str(user_id),target)).fetchone()
            if account:
                if account['balance_units']+output>MAX_UNITS:raise ValueError('입금 잔액 한도를 초과했습니다.')
                conn.execute('UPDATE fx_accounts SET balance_units=balance_units+? WHERE account_number=?',(output,account['account_number']))
            else:
                CurrencyManager.change(conn,user_id,target,output)
            CurrencyManager.change(conn,TREASURY_USER_ID,'NWX',fee*SCALE)
            conn.execute('INSERT INTO fx_transactions(user_id,source_currency,target_currency,source_units,target_units,source_rate,target_rate,fee_nwx,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?)',(str(user_id),source,target,units,output,str(CurrencyManager.quote(conn,source,'NWX')),str(CurrencyManager.quote(conn,target,'NWX')),fee,now))
            return f'✅ {display_units(units,CurrencyManager.precision(conn,source))} {source} → {display_units(output,CurrencyManager.precision(conn,target))} {target} · 수수료 {fee:,} NWX'

    def accounts(self, user_id):
        with self.db.atomic() as conn:
            self._user(conn, user_id)
            self._settle(conn, user_id, time.time())
            return [{**dict(r), 'value_nwx': int(Decimal(r['balance_units']) * self._rate(conn,r['currency_code']) / SCALE) if r['balance_units'] else 0}
                    for r in conn.execute("SELECT a.*,p.name AS product_name,p.annual_rate,c.decimal_places FROM fx_accounts a JOIN currencies c ON c.code=a.currency_code "
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
            from managers.currency_manager import CurrencyManager
            def endpoint(number,owner=None):
                if owner is not None and (number=='__wallet__' or number.startswith('__wallet__:')):
                    currency=number.split(':',1)[1].upper() if ':' in number else CurrencyManager.home(conn,owner)
                    CurrencyManager.rate(conn,currency)
                    return ('wallet','balance_units','user_id',owner,owner,currency,1)
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
                    return ('wallet','balance_units','user_id',number,number,CurrencyManager.home(conn,number),1)
                raise ValueError('출금 계좌를 찾을 수 없습니다.')
            src,dst = endpoint(source_account,sender),endpoint(recipient)
            if src[:4]==dst[:4]:
                raise ValueError('같은 계좌로 송금할 수 없습니다.')
            if src[5]=='NWX' and units%SCALE:
                raise ValueError('NWX는 정수 금액만 사용할 수 있습니다.')
            self._settle(conn,sender,now)
            if dst[4]!=sender:
                self._settle(conn,dst[4],now)
            CurrencyManager.validate_units(conn,units,src[5])
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
                fee=CurrencyManager.fee(conn,units,src[5],rate)
                output=CurrencyManager.exchange_amount(conn,units,src[5],dst[5],fee)
            else:
                output = units
            output = output//dst[6]*dst[6]
            if dst[5]=='NWX':output=output//SCALE*SCALE
            if not 1<=output<=MAX_UNITS:
                raise ValueError('환전 결과가 최소 단위 미만이거나 한도를 초과합니다.')
            debit,credit = units//src[6],output//dst[6]
            if src[0]=='wallet':
                CurrencyManager.change(conn,sender,src[5],-units)
                debited=True
            elif src[0]=='accounts':
                debited = UserManager._debit_source(conn,sender,src[3],debit)
            else:
                debited = conn.execute(f'UPDATE {src[0]} SET {src[1]}={src[1]}-? WHERE {src[2]}=? AND {src[1]}>=?',(debit,src[3],debit)).rowcount==1
            if not debited:
                raise ValueError('출금 잔액이 부족합니다.')
            if dst[0]=='wallet':
                CurrencyManager.change(conn,dst[4],dst[5],output)
                changed=None
            elif dst[0]=='accounts':
                changed = conn.execute('UPDATE accounts SET balance=balance+?,principal=COALESCE(principal,0)+?,last_payment_epoch=?,last_payment=CURRENT_TIMESTAMP WHERE account_number=? AND balance<=?',(credit,credit,now,dst[3],MAX_UNITS-credit))
            else:
                changed = conn.execute(f'UPDATE {dst[0]} SET {dst[1]}={dst[1]}+? WHERE {dst[2]}=? AND {dst[1]}<=?',(credit,dst[3],MAX_UNITS-credit))
            if changed is not None and changed.rowcount!=1:
                raise ValueError('입금 계좌 잔액 한도를 초과했습니다.')
            changed = conn.execute('UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?',(fee,TREASURY_USER_ID,MAX_UNITS-fee))
            if changed.rowcount!=1:
                raise ValueError('국고 잔액 한도를 초과했습니다.')
            conn.execute('INSERT INTO fx_transfer_transactions(sender_id,recipient_id,source_account,target_account,source_currency,target_currency,source_units,target_units,source_rate,target_rate,fee_nwx,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (sender,dst[4],None if source_account=='__wallet__' else source_account,None if dst[0]=='wallet' else dst[3],src[5],dst[5],units,output,str(CurrencyManager.quote(conn,src[5],'NWX')),str(CurrencyManager.quote(conn,dst[5],'NWX')),fee,now))
            return f'✅ 송금 {display_units(units,CurrencyManager.precision(conn,src[5]))} {src[5]} → {display_units(output,CurrencyManager.precision(conn,dst[5]))} {dst[5]} · 환전 수수료 {fee:,} NWX'
