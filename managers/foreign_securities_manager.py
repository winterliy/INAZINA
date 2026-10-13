"""Foreign-currency stock/spot orders using the common price-time order book."""
import time
from database.user_db import TREASURY_USER_ID
from managers.currency_exchange_manager import CurrencyExchangeManager,SCALE,MAX_UNITS
from managers.currency_manager import CurrencyManager
from managers.market_session_manager import MarketSessionManager


class ForeignSecuritiesManager:
    def __init__(self,book):
        self.book,self.db,self.users = book,book.db,book.user_manager

    @staticmethod
    def credit_units(conn,user,currency,units,account=None):
        if CurrencyManager.home(conn,user) == currency:
            if units % SCALE:
                raise ValueError(f'{currency} 국적 통화 현금은 정수 단위로만 입금할 수 있습니다.')
            changed = conn.execute("UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?",
                (units//SCALE,str(user),(2**53-1)-units//SCALE))
            if changed.rowcount!=1:
                raise ValueError(f'{currency} 현금 잔액 한도를 초과했습니다.')
            return
        if account:
            changed = conn.execute("UPDATE accounts SET balance_units=balance_units+? WHERE account_number=? AND user_id=? AND quote_currency=? AND account_type='currency_saving' AND balance_units<=?",
                (units,account,str(user),currency,MAX_UNITS-units))
        else:
            changed = conn.execute("UPDATE accounts SET balance_units=balance_units+? WHERE user_id=? AND quote_currency=? AND account_type='currency_saving' AND balance_units<=?",
                (units,str(user),currency,MAX_UNITS-units))
        if changed.rowcount!=1:
            raise ValueError(f'{currency} 외환통장이 없거나 잔액 한도를 초과했습니다.')

    def order(self,user_id,code,side,quantity,limit_price):
        if side not in {'buy','sell'} or any(isinstance(v,bool) or not isinstance(v,int) or not 1<=v<=self.book.MAX_LIMIT_PRICE for v in (quantity,limit_price)):
            return False,'❌ 주문 방향·수량·지정가를 확인해주세요.'
        now = int(time.time())
        try:
            with self.db.atomic() as conn:
                exchange_id = MarketSessionManager.exchange_for_asset(conn, code)
                state = MarketSessionManager.process_conn(conn, now, exchange_id) if exchange_id else {'status': 'open'}
                deferred = state['status'] != 'open'
                asset,source = self.users._asset_row(conn,code)
                currency = asset['quote_currency']
                if source not in {'company','market_assets','investment'} or asset['type'] not in {'stock','주식','commodity_spot'}:
                    raise ValueError('외화 결제는 현재 주식·현물 원자재에 지원합니다.')
                if asset['status']!='active' or asset['price']<=0:
                    raise ValueError('현재 거래할 수 없는 종목입니다.')
                CurrencyExchangeManager._user(conn,user_id)
                CurrencyExchangeManager._rate(conn,currency)
                CurrencyExchangeManager(self.db)._settle(conn,user_id,now)
                home = CurrencyManager.home(conn,user_id)
                account = None if currency == home else conn.execute("SELECT * FROM accounts WHERE user_id=? AND quote_currency=? AND account_type='currency_saving'",(user_id,currency)).fetchone()
                if currency != home and not account:
                    raise ValueError(f'/계좌개설로 {currency} 외환통장을 먼저 개설해주세요.')
                account_number = account['account_number'] if account else None
                owned = conn.execute('SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=? AND asset_code=?',(user_id,code)).fetchone()[0]
                reserved = conn.execute("SELECT COALESCE(SUM(remaining_quantity),0) FROM market_orders WHERE user_id=? AND asset_code=? AND side=? AND status IN ('scheduled','open','partial')",(user_id,code,side)).fetchone()[0]
                supply = asset.get('supply')
                if side=='buy' and supply is not None and owned+reserved+quantity>int(supply):
                    raise ValueError('종목 발행 수량 한도를 초과했습니다.')
                cash = limit_price*quantity if side=='buy' else 0
                if cash*SCALE>MAX_UNITS:
                    raise ValueError('주문 예치금 한도를 초과했습니다.')
                if side=='buy':
                    if currency == home:
                        changed=conn.execute("UPDATE users SET cash=cash-? WHERE user_id=? AND cash>=?",(cash,str(user_id),cash))
                    else:
                        changed=conn.execute("UPDATE accounts SET balance_units=balance_units-? WHERE account_number=? AND account_type='currency_saving' AND balance_units>=?",(cash*SCALE,account_number,cash*SCALE))
                    if changed.rowcount!=1:
                        raise ValueError(f'예치할 {currency} 잔액이 부족합니다. 필요액 {cash:,} {currency}')
                elif owned-reserved<quantity:
                    raise ValueError(f'매도 가능 수량이 부족합니다. 담보 주식은 매도할 수 없습니다. 가능 {max(0,owned-reserved):,}개')
                order_id=conn.execute("INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,remaining_quantity,reserved_cash,funding_account,order_kind,status,created_epoch,updated_epoch,quote_currency,activation_epoch,scheduled_reason) VALUES (?,?,?,?,?,?,?,?,'asset',?,?,?,?,?,?)",(user_id,code,side,limit_price,quantity,quantity,cash,account_number,'scheduled' if deferred else 'open',now,now,currency,state.get('resume_epoch') if deferred else now,state.get('reason') if deferred else None)).lastrowid
                filled = 0 if deferred else self.match(conn,order_id,now)
                if deferred:
                    locked = f'예치금 {cash:,} {currency}' if side=='buy' else '매도 수량'
                    return True,f'✅ 예약 주문 #{order_id} 접수 · {quantity:,}개 × {limit_price:,} {currency}\n`{exchange_id}` 거래소가 {state.get("reason") or "정지"} 상태라 개장·재개 시 호가장에 등록됩니다. {locked}은 잠금 상태입니다.'
                return True,f'✅ 주문 #{order_id} · {quantity:,}개 × {limit_price:,} {currency} · 체결 {filled:,}개 · /내주문에서 확인'
        except (ValueError,RuntimeError) as exc:
            return False,f'❌ {exc}'

    @staticmethod
    def add_position(conn,user,code,account,quantity,price):
        conn.execute("INSERT INTO user_assets(user_id,asset_code,account_number,quantity,average_cost) VALUES (?,?,?,?,?) ON CONFLICT(user_id,asset_code,IFNULL(account_number,'')) DO UPDATE SET average_cost=(quantity*average_cost+excluded.quantity*excluded.average_cost)/(quantity+excluded.quantity),quantity=quantity+excluded.quantity",(user,code,account,quantity,price))

    def match(self,conn,order_id,now):
        incoming = conn.execute('SELECT asset_code FROM market_orders WHERE order_id=?',(order_id,)).fetchone()
        if MarketSessionManager(self.db).trading_error(conn=conn, now=now, asset_code=incoming['asset_code'] if incoming else None):
            return 0
        filled=0
        while True:
            incoming=conn.execute('SELECT * FROM market_orders WHERE order_id=?',(order_id,)).fetchone()
            if incoming['remaining_quantity']<=0:
                break
            side=incoming['side']
            comparator,sort = ('<=','ASC') if side=='buy' else ('>=','DESC')
            maker=conn.execute(f"SELECT * FROM market_orders WHERE asset_code=? AND order_kind='asset' AND side=? AND status IN ('open','partial') AND user_id<>? AND limit_price {comparator} ? ORDER BY limit_price {sort},created_epoch,order_id LIMIT 1",(incoming['asset_code'],'sell' if side=='buy' else 'buy',incoming['user_id'],incoming['limit_price'])).fetchone()
            asset,source=self.users._asset_row(conn,incoming['asset_code'])
            currency=asset['quote_currency']
            price=int(asset['price'])
            owned=conn.execute('SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE asset_code=?',(asset['code'],)).fetchone()[0]
            collateral=conn.execute('SELECT COALESCE(SUM(quantity),0) FROM option_collateral_lots WHERE underlying_code=?',(asset['code'],)).fetchone()[0]
            remaining=max(0,int(asset['supply'] or 0)-int(owned)-int(collateral)) if asset.get('supply') is not None else 0
            primary=side=='buy' and remaining>0 and price<=incoming['limit_price'] and (not maker or price<int(maker['limit_price']))
            if not primary and not maker:
                break
            quantity=min(incoming['remaining_quantity'],remaining if primary else maker['remaining_quantity'])
            price=price if primary else int(maker['limit_price'])
            buy=incoming if side=='buy' else maker
            sell=None if primary else maker if side=='buy' else incoming
            improvement=(buy['limit_price']-price)*quantity
            if improvement:
                self.credit_units(conn,buy['user_id'],currency,improvement*SCALE)
            self.add_position(conn,buy['user_id'],asset['code'],buy['funding_account'],quantity,price)
            if primary:
                self.credit_units(conn,TREASURY_USER_ID,currency,quantity*price*SCALE)
            else:
                positions=conn.execute('SELECT * FROM user_assets WHERE user_id=? AND asset_code=? ORDER BY holding_id',(sell['user_id'],asset['code'])).fetchall()
                left,gain = quantity,0
                for position in positions:
                    take=min(left,position['quantity'])
                    if not take:
                        continue
                    gain += max(0,price-int(round(position['average_cost'])))*take
                    if take==position['quantity']:
                        conn.execute('DELETE FROM user_assets WHERE holding_id=?',(position['holding_id'],))
                    else:
                        conn.execute('UPDATE user_assets SET quantity=quantity-? WHERE holding_id=?',(take,position['holding_id']))
                    left -= take
                    if not left:
                        break
                if left:
                    raise ValueError('매도 예약 수량과 보유 수량이 달라 체결을 취소했습니다.')
                rate=CurrencyExchangeManager._rate(conn,currency)
                tax=self.users._tax(conn,sell['user_id'],'foreign_security_sale_gain',gain*rate)
                tax_units=(tax*SCALE+rate-1)//rate
                self.credit_units(conn,sell['user_id'],currency,quantity*price*SCALE-tax_units)
                if source == 'company':
                    from managers.corporate_actions_manager import CorporateActionsManager
                    CorporateActionsManager.record_issue_fill(conn, sell['order_id'], quantity, price)
                self.book._reduce_order(conn,sell,quantity,now)
            self.book._reduce_order(conn,buy,quantity,now)
            self.book._apply_execution_price(conn,asset['code'],price,now)
            if source=='company':
                self.users._refresh_max_shareholder(conn,asset['code'])
            filled += quantity
        return filled
