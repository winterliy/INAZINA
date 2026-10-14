"""Atomic currency exchange. Foreign amounts are integer millionths of a unit."""
import time
import uuid
from decimal import Decimal, InvalidOperation
from database.user_db import TREASURY_USER_ID
from managers.currency_manager import CurrencyManager

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

class CurrencyExchangeManager:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _rate(conn, code):
        value = CurrencyManager.rate(conn, code) / CurrencyManager.rate(conn, 'NWX')
        return int(value) if value == value.to_integral_value() else value

    @staticmethod
    def _user(conn, user_id):
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (str(user_id),)).fetchone()
        if not user or (user['status'] != 'active' and str(user_id) != TREASURY_USER_ID):
            raise ValueError("등록된 활성 사용자만 이용할 수 있습니다.")
        return user

    def national_currency(self, user_id):
        with self.db.readonly() as conn:
            user = self._user(conn, user_id)
            row = conn.execute("SELECT currency_code FROM country_currencies n JOIN currencies c ON c.code=n.currency_code WHERE nationality=? AND c.status='active'", (user['nationality'],)).fetchone()
            return user['nationality'], row['currency_code'] if row else 'NWX'

    def rates(self):
        with self.db.readonly() as conn:
            return [{**dict(r), 'rate': self._rate(conn, r['code'])}
                    for r in conn.execute("SELECT * FROM currencies WHERE status='active' ORDER BY code")]

    def open_account(self, user_id, product_code):
        with self.db.atomic() as conn:
            self._user(conn, user_id)
            product = conn.execute("SELECT * FROM saving WHERE product_code=? AND product_kind='currency_saving' AND status='active'", (product_code,)).fetchone()
            if not product:
                raise ValueError("외환통장 상품 코드를 확인해주세요.")
            institution = conn.execute(
                "SELECT status FROM financial_institutions WHERE issuer=?",
                (product['issuer'],),
            ).fetchone()
            if institution and institution['status'] == 'failed':
                raise ValueError("발행 금융기관이 정리 절차 중이라 외환통장을 개설할 수 없습니다.")
            self._rate(conn, product['quote_currency'])
            home = CurrencyManager.home(conn, user_id)
            if product['quote_currency'] == home:
                raise ValueError(f'{home}는 국적 통화이므로 현금 잔액에서 사용합니다.')
            if conn.execute("SELECT 1 FROM accounts WHERE user_id=? AND quote_currency=? AND account_type='currency_saving'", (str(user_id), product['quote_currency'])).fetchone():
                raise ValueError("같은 통화의 외환통장이 이미 있습니다.")
            number = 'FXA' + uuid.uuid4().hex[:16].upper()
            opened_epoch = time.time()
            from managers.rate_manager import RateManager
            contract_spread = (RateManager.product_spread(conn, product, 'deposit')
                               if product['rate_type'] == 'floating'
                               else float(product['base_rate_spread']))
            conn.execute("INSERT INTO accounts(account_number,user_id,product_code,balance,password,"
                         "last_payment_epoch,principal,account_type,quote_currency,contract_rate,rate_type,"
                         "rate_nationality,base_rate_spread,rate_floor,rate_cap,repricing_cycle_seconds,"
                         "next_repricing_epoch,benchmark_key,benchmark_tenor_days,repricing_lag_seconds,"
                         "contract_effective_rate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (number, str(user_id), product_code, 0, None, opened_epoch, 0,
                          'currency_saving', product['quote_currency'], float(product['interest_rate']),
                          product['rate_type'], product['rate_nationality'],
                          contract_spread, float(product['rate_floor']),
                          product['rate_cap'], int(product['repricing_cycle_seconds']),
                          opened_epoch+int(product['repricing_cycle_seconds'])
                          if product['rate_type']=='floating' else None,
                          product['benchmark_key'],
                          int(product['benchmark_tenor_days'] or product['maturity_days'] or 0),
                          int(product['repricing_lag_seconds'] or 0),
                          self._effective_rate(product)))
            return number

    @staticmethod
    def _effective_rate(product):
        from managers.rate_manager import RateManager
        try:
            return RateManager.effective_annual_rate(
                product['interest_rate'], product['interest_cycle'] or 31536000
            )
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def _settle(conn, user_id, now):
        rows = conn.execute("SELECT a.*,COALESCE(a.contract_rate,p.interest_rate) AS annual_rate "
                            "FROM accounts a JOIN saving p USING(product_code) "
                            "LEFT JOIN financial_institutions f ON f.issuer=p.issuer "
                            "WHERE a.user_id=? AND a.account_type='currency_saving' "
                            "AND a.resolution_status='active' AND COALESCE(f.status,'active')<>'failed'",
                            (str(user_id),)).fetchall()
        for row in rows:
            from managers.rate_manager import RateManager
            cursor = float(row['last_payment_epoch'])
            rate = float(row['annual_rate'])
            next_reset = row['next_repricing_epoch']
            cycle = int(row['repricing_cycle_seconds'] or 2592000)
            exact = Decimal(row['interest_remainder'])
            while cursor < now:
                segment_end = now
                if row['rate_type']=='floating' and next_reset is not None:
                    segment_end = min(segment_end, float(next_reset))
                elapsed = max(0, segment_end-cursor)
                exact += (Decimal(row['balance_units']) * Decimal(str(rate)) / 100
                          * Decimal(str(elapsed)) / Decimal(365*86400))
                cursor = segment_end
                if (row['rate_type']=='floating' and next_reset is not None
                        and cursor >= float(next_reset)):
                    rate = RateManager.floating_rate(
                        conn, row['rate_nationality'], row['base_rate_spread'],
                        row['rate_floor'], row['rate_cap'],
                        epoch=max(0, float(next_reset)-float(row['repricing_lag_seconds'] or 0)),
                        benchmark_key=row['benchmark_key'],
                        tenor_days=row['benchmark_tenor_days'], apply_legal_max=False,
                    )
                    next_reset = float(next_reset)+cycle
            credit = int(exact)
            if credit < -int(row['balance_units']):
                credit, exact = -int(row['balance_units']), Decimal(0)
            if row['balance_units'] + credit > MAX_UNITS:
                raise ValueError("외환 잔액 한도를 초과합니다.")
            conn.execute("UPDATE accounts SET balance_units=balance_units+?,interest_remainder=?,"
                         "last_payment_epoch=?,contract_rate=?,next_repricing_epoch=? "
                         "WHERE account_number=? AND account_type='currency_saving'",
                         (credit, str(exact-credit), max(now,row['last_payment_epoch']), rate,
                          next_reset, row['account_number']))

    def exchange(self, user_id, source, target, amount):
        source, target = str(source).strip().upper(), str(target).strip().upper()
        units = amount_units(amount)
        if source == target:
            raise ValueError("서로 다른 통화를 선택해주세요.")
        now = time.time()
        with self.db.atomic() as conn:
            from managers.market_session_manager import MarketSessionManager
            halt = MarketSessionManager(self.db).trading_error(conn=conn, now=now, exchange_id='CURRENCY')
            if halt:
                raise ValueError(halt)
            self._user(conn, user_id)
            home = CurrencyManager.home(conn, user_id)
            if source == home and units % SCALE:
                raise ValueError(f"{home} 국적 통화 현금은 정수 {home} 금액만 사용할 수 있습니다.")
            CurrencyManager.validate_units(conn, units, source)
            self._settle(conn, user_id, now)
            fee_rate = Decimal(str(conn.execute("SELECT value FROM settings WHERE key='currency_exchange_fee_percent'").fetchone()[0]))
            if not fee_rate.is_finite() or not 0 <= fee_rate <= 100:
                raise ValueError("환전 수수료 설정이 올바르지 않습니다.")
            fee = CurrencyManager.fee(conn, units, source, fee_rate)
            output = CurrencyManager.exchange_amount(conn, units, source, target, fee)
            if target == home:
                output = output // SCALE * SCALE
            if output <= 0 or output > MAX_UNITS:
                raise ValueError("환전 결과가 최소 단위 미만이거나 한도를 초과합니다.")
            CurrencyManager.change(conn, user_id, source, -units)
            CurrencyManager.change(conn, user_id, target, output)
            treasury = conn.execute("UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?", (fee,TREASURY_USER_ID,MAX_UNITS-fee))
            if treasury.rowcount != 1:
                raise ValueError("국고 잔액 한도를 초과했습니다.")
            return f"✅ {display_units(units)} {source} → {display_units(output)} {target} · 수수료 {fee:,} NWX"

    def accounts(self, user_id):
        with self.db.atomic() as conn:
            self._user(conn, user_id)
            self._settle(conn, user_id, time.time())
            home = CurrencyManager.home(conn, user_id)
            return [{**dict(r), 'value_nwx': int(Decimal(r['balance_units']) * self._rate(conn,r['currency_code']) / SCALE) if r['balance_units'] else 0}
                    for r in conn.execute("SELECT a.*,a.quote_currency AS currency_code,p.name AS product_name,COALESCE(a.contract_rate,p.interest_rate) AS annual_rate FROM accounts a "
                                          "LEFT JOIN saving p ON p.product_code=a.product_code "
                                          "WHERE a.user_id=? AND a.account_type='currency_saving' AND a.quote_currency<>? "
                                          "ORDER BY a.quote_currency", (str(user_id),home))]

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
                    return ('users','cash','user_id',owner,owner,CurrencyManager.home(conn,owner),SCALE)
                currency_account = conn.execute("SELECT *,quote_currency AS currency_code FROM accounts WHERE account_number=? AND account_type='currency_saving'",(number,)).fetchone()
                if currency_account:
                    if owner is not None and str(currency_account['user_id'])!=owner:
                        raise ValueError('출금 계좌는 본인 명의여야 합니다.')
                    self._user(conn,currency_account['user_id'])
                    return ('accounts','balance_units','account_number',number,str(currency_account['user_id']),currency_account['currency_code'],1)
                bank = conn.execute("SELECT * FROM accounts WHERE account_number=? AND account_type='saving'",(number,)).fetchone()
                if bank:
                    if owner is not None and str(bank['user_id'])!=owner:
                        raise ValueError('출금 계좌는 본인 명의여야 합니다.')
                    self._user(conn,bank['user_id'])
                    return ('accounts','balance','account_number',number,str(bank['user_id']),CurrencyManager.home(conn,bank['user_id']),SCALE)
                if owner is None:
                    self._user(conn,number)
                    return ('users','cash','user_id',number,number,CurrencyManager.home(conn,number),SCALE)
                raise ValueError('출금 계좌를 찾을 수 없습니다.')
            src,dst = endpoint(source_account,sender),endpoint(recipient)
            if src[:4]==dst[:4]:
                raise ValueError('같은 계좌로 송금할 수 없습니다.')
            if src[6]==SCALE and units%SCALE:
                raise ValueError(f'{src[5]} 국적 통화는 정수 {src[5]} 금액만 사용할 수 있습니다.')
            CurrencyManager.validate_units(conn,units,src[5])
            self._settle(conn,sender,now)
            if dst[4]!=sender:
                self._settle(conn,dst[4],now)
            fee = 0
            if src[5]!=dst[5]:
                from managers.market_session_manager import MarketSessionManager
                halt = MarketSessionManager(self.db).trading_error(conn=conn, now=now, exchange_id='CURRENCY')
                if halt:
                    raise ValueError(halt)
                setting = conn.execute("SELECT value FROM settings WHERE key='currency_exchange_fee_percent'").fetchone()
                rate = Decimal(str(setting[0]))
                if not rate.is_finite() or not 0<=rate<=100:
                    raise ValueError('환전 수수료 설정이 올바르지 않습니다.')
                fee = CurrencyManager.fee(conn,units,src[5],rate)
                output = CurrencyManager.exchange_amount(conn,units,src[5],dst[5],fee)
            else:
                output = units
            output = output//dst[6]*dst[6]
            if not 1<=output<=MAX_UNITS:
                raise ValueError('환전 결과가 최소 단위 미만이거나 한도를 초과합니다.')
            debit,credit = units//src[6],output//dst[6]
            debited_sources = []
            if src[1] in {'cash','balance'}:
                preferred = None if src[0]=='users' else src[3]
                candidates = [(None,int(conn.execute(
                    "SELECT cash FROM users WHERE user_id=?",(sender,)
                ).fetchone()[0] or 0))]
                candidates += [(str(row['account_number']),int(row['balance'] or 0)) for row in conn.execute(
                    "SELECT account_number,balance FROM accounts WHERE user_id=? "
                    "AND account_type IN ('saving','loan_payout')",(sender,)).fetchall()]
                if dst[4]==sender:
                    destination_source = None if dst[0]=='users' else dst[3]
                    candidates = [item for item in candidates if item[0]!=destination_source]
                candidates.sort(key=lambda item:(item[0]!=preferred,item[0] is None,-item[1]))
                if sum(max(0,balance) for _,balance in candidates) < debit:
                    debited = False
                else:
                    remaining = debit
                    for account_number,balance in candidates:
                        take = min(max(0,balance),remaining)
                        if take and not UserManager._debit_source(conn,sender,account_number,take):
                            raise RuntimeError('출금 잔액이 변경되어 송금을 취소했습니다.')
                        if take:
                            debited_sources.append(account_number or '지갑')
                            remaining -= take
                        if not remaining:
                            break
                    debited = remaining == 0
            else:
                debited = conn.execute(f'UPDATE {src[0]} SET {src[1]}={src[1]}-? WHERE {src[2]}=? AND {src[1]}>=?',(debit,src[3],debit)).rowcount==1
                if debited:
                    debited_sources.append(src[3])
            if not debited:
                raise ValueError('출금 잔액이 부족합니다.')
            if dst[0]=='accounts' and dst[1]=='balance':
                changed = conn.execute('UPDATE accounts SET balance=balance+?,principal=COALESCE(principal,0)+?,last_payment_epoch=?,last_payment=CURRENT_TIMESTAMP WHERE account_number=? AND balance<=?',(credit,credit,now,dst[3],MAX_UNITS-credit))
            else:
                changed = conn.execute(f'UPDATE {dst[0]} SET {dst[1]}={dst[1]}+? WHERE {dst[2]}=? AND {dst[1]}<=?',(credit,dst[3],MAX_UNITS-credit))
            if changed.rowcount!=1:
                raise ValueError('입금 계좌 잔액 한도를 초과했습니다.')
            changed = conn.execute('UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?',(fee,TREASURY_USER_ID,MAX_UNITS-fee))
            if changed.rowcount!=1:
                raise ValueError('국고 잔액 한도를 초과했습니다.')
            destination_label = '국고' if dst[4] == TREASURY_USER_ID else recipient
            sources_label = ', '.join(debited_sources)
            return (f'✅ 송금 {display_units(units)} {src[5]} → '
                    f'{display_units(output)} {dst[5]} · 받는 곳 {destination_label} · '
                    f'출금 {sources_label} · 환전 수수료 {fee:,} NWX')
