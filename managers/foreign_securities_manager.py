"""Foreign-currency stock/spot orders using the common price-time order book."""
import time
import uuid
from database.user_db import TREASURY_USER_ID, IS_TREASURY
from managers.currency_exchange_manager import CurrencyExchangeManager,SCALE,MAX_UNITS
from managers.currency_manager import CurrencyManager
from managers.market_session_manager import MarketSessionManager
from functions.money import (parse_currency_money, currency_precision,
                             to_minor_units, from_minor_units)


class ForeignSecuritiesManager:
    def __init__(self,book):
        self.book,self.db,self.users = book,book.db,book.user_manager

    @staticmethod
    def _minor_to_ledger_units(minor_units, places):
        """Convert native-currency minor units to the wallet's millionth-major scale."""
        return int(minor_units) * (SCALE // (10 ** int(places)))

    @staticmethod
    def credit_units(conn,user,currency,units,account=None):
        CurrencyManager.validate_units(conn, int(units), currency)
        if CurrencyManager.home(conn,user) == currency:
            places = currency_precision(conn, currency)
            quantum = SCALE // (10 ** places)
            if int(units) % quantum:
                raise ValueError(f'{currency} 금액이 통화 최소단위보다 작습니다.')
            minor = int(units) // quantum
            changed = conn.execute("UPDATE users SET cash_minor=cash_minor+? WHERE user_id=? AND cash_minor<=?",
                (minor,str(user),(2**53-1)-minor))
            if changed.rowcount!=1:
                raise ValueError(f'{currency} 현금 잔액 한도를 초과했습니다.')
            return
        if not account and CurrencyManager.is_treasury(conn, user) and not CurrencyManager._treasury_target(conn, user, currency):
            # A treasury without an account in this currency exchanges it into its own.
            CurrencyManager.treasury_receive(conn, user, currency, units)
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
        if (side not in {'buy','sell'} or isinstance(quantity,bool)
                or not isinstance(quantity,int) or not 1<=quantity<=self.book.MAX_QUANTITY):
            return False,'❌ 주문 방향·수량·지정가를 확인해주세요.'
        now = int(time.time())
        try:
            with self.db.atomic() as conn:
                exchange_id = MarketSessionManager.exchange_for_asset(conn, code)
                state = MarketSessionManager.process_conn(conn, now, exchange_id) if exchange_id else {'status': 'open'}
                deferred = state['status'] != 'open'
                asset,source = self.users._asset_row(conn,code)
                currency = asset['quote_currency']
                limit_price = parse_currency_money(
                    conn, limit_price, currency, minimum=0
                )
                if limit_price <= 0 or limit_price > self.book.MAX_LIMIT_PRICE:
                    raise ValueError('지정가를 확인해주세요.')
                if source not in {'company','market_assets','investment'} or asset['type'] not in {'stock','주식','commodity_spot','depositary_receipt'}:
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
                places = currency_precision(conn, currency)
                limit_minor = to_minor_units(limit_price, places)
                cash_minor = limit_minor * quantity if side == 'buy' else 0
                cash = from_minor_units(cash_minor, places)
                cash_units = self._minor_to_ledger_units(cash_minor, places)
                if cash_units>MAX_UNITS:
                    raise ValueError('주문 예치금 한도를 초과했습니다.')
                if side=='buy':
                    if currency == home:
                        changed=conn.execute(f"UPDATE users SET cash_minor=cash_minor-? WHERE user_id=? AND (cash_minor>=? OR {IS_TREASURY})",(cash_minor,str(user_id),cash_minor))
                    else:
                        changed=conn.execute("UPDATE accounts SET balance_units=balance_units-? WHERE account_number=? AND account_type='currency_saving' AND balance_units>=?",(cash_units,account_number,cash_units))
                    if changed.rowcount!=1:
                        raise ValueError(f'예치할 {currency} 잔액이 부족합니다. 필요액 {cash:,} {currency}')
                elif owned-reserved<quantity:
                    raise ValueError(f'매도 가능 수량이 부족합니다. 담보 주식은 매도할 수 없습니다. 가능 {max(0,owned-reserved):,}개')
                order_id=conn.execute("INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,remaining_quantity,reserved_cash,funding_account,order_kind,status,created_epoch,updated_epoch,quote_currency,activation_epoch,scheduled_reason) VALUES (?,?,?,?,?,?,?,?,'asset',?,?,?,?,?,?)",(user_id,code,side,limit_price,quantity,quantity,float(cash),account_number,'scheduled' if deferred else 'open',now,now,currency,state.get('resume_epoch') if deferred else now,state.get('reason') if deferred else None)).lastrowid
                filled = 0 if deferred else self.match(conn,order_id,now)
                if deferred:
                    locked = f'예치금 {cash:,} {currency}' if side=='buy' else '매도 수량'
                    return True,f'✅ 예약 주문 #{order_id} 접수 · {quantity:,}개 × {limit_price:,} {currency}\n`{exchange_id}` 거래소가 {state.get("reason") or "정지"} 상태라 개장·재개 시 호가장에 등록됩니다. {locked}은 잠금 상태입니다.'
                return True,f'✅ 주문 #{order_id} · {quantity:,}개 × {limit_price:,} {currency} · 체결 {filled:,}개 · /내주문에서 확인'
        except (ValueError,RuntimeError) as exc:
            return False,f'❌ {exc}'

    @staticmethod
    def treasury_currency_account(conn,currency,now):
        """The treasury receives primary-issue proceeds in any listed currency.

        Citizens open foreign-currency accounts themselves; the treasury is a
        system account, so its holding account is created on first use.
        """
        row=conn.execute("SELECT account_number FROM accounts WHERE user_id=? AND quote_currency=? AND account_type='currency_saving'",(TREASURY_USER_ID,currency)).fetchone()
        if row:
            return row['account_number']
        product=conn.execute("SELECT product_code FROM saving WHERE quote_currency=? AND product_kind='currency_saving' AND status='active' ORDER BY product_code LIMIT 1",(currency,)).fetchone()
        account='FXA'+uuid.uuid4().hex[:20].upper()
        conn.execute("INSERT INTO accounts(account_number,user_id,product_code,balance,password,last_payment_epoch,principal,account_type,quote_currency) VALUES (?,?,?,?,?,?,?,?,?)",
                     (account,TREASURY_USER_ID,product['product_code'] if product else None,0,None,now,0,'currency_saving',currency))
        return account

    @staticmethod
    def add_position(conn,user,code,account,quantity,price):
        from managers.asset_cost_manager import AssetCostManager
        AssetCostManager.upsert(conn, user, code, account, quantity, price)

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
            asset,source=self.users._asset_row(conn,incoming['asset_code'])
            currency=asset['quote_currency']
            places = currency_precision(conn, currency)
            incoming_limit_minor = int(incoming['limit_price_minor_units'])
            maker=conn.execute(f"SELECT * FROM market_orders WHERE asset_code=? AND order_kind='asset' AND side=? AND status IN ('open','partial') AND user_id<>? AND limit_price_minor_units {comparator} ? ORDER BY limit_price_minor_units {sort},created_epoch,order_id LIMIT 1",(incoming['asset_code'],'sell' if side=='buy' else 'buy',incoming['user_id'],incoming_limit_minor)).fetchone()
            price_minor = to_minor_units(asset['price'], places)
            price = from_minor_units(price_minor, places)
            owned=conn.execute('SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE asset_code=?',(asset['code'],)).fetchone()[0]
            collateral=conn.execute('SELECT COALESCE(SUM(quantity),0) FROM option_collateral_lots WHERE underlying_code=?',(asset['code'],)).fetchone()[0]
            remaining=max(0,int(asset['supply'] or 0)-int(owned)-int(collateral)) if asset.get('supply') is not None else 0
            primary=side=='buy' and remaining>0 and price_minor<=incoming_limit_minor and (not maker or price_minor<int(maker['limit_price_minor_units']))
            if not primary and not maker:
                break
            quantity=min(incoming['remaining_quantity'],remaining if primary else maker['remaining_quantity'])
            price_minor=price_minor if primary else int(maker['limit_price_minor_units'])
            price=from_minor_units(price_minor, places)
            buy=incoming if side=='buy' else maker
            sell=None if primary else maker if side=='buy' else incoming
            wallet_watermark = int(conn.execute(
                "SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger"
            ).fetchone()[0])
            improvement_minor=max(0, int(buy['limit_price_minor_units'])-price_minor)*quantity
            if improvement_minor:
                self.credit_units(conn,buy['user_id'],currency,
                                  self._minor_to_ledger_units(improvement_minor, places))
            self.add_position(conn,buy['user_id'],asset['code'],buy['funding_account'],quantity,float(price))
            if primary:
                # The listing country's treasury sells the primary issue; without an
                # account in this currency it exchanges the proceeds into its own.
                CurrencyManager.treasury_receive(
                    conn, CurrencyManager.exchange_treasury(conn, asset['code']), currency,
                    self._minor_to_ledger_units(price_minor*quantity, places))
                tax = 0
                proceeds = 0
            else:
                positions=conn.execute('SELECT * FROM user_assets WHERE user_id=? AND asset_code=? ORDER BY holding_id',(sell['user_id'],asset['code'])).fetchall()
                left,gain_minor = quantity,0
                for position in positions:
                    take=min(left,position['quantity'])
                    if not take:
                        continue
                    cost_minor = int(position['average_cost_minor_units'] or 0)
                    gain_minor += max(0, price_minor-cost_minor)*take
                    if take==position['quantity']:
                        conn.execute('DELETE FROM user_assets WHERE holding_id=?',(position['holding_id'],))
                    else:
                        conn.execute('UPDATE user_assets SET quantity=quantity-? WHERE holding_id=?',(take,position['holding_id']))
                    left -= take
                    if not left:
                        break
                if left:
                    raise ValueError('매도 예약 수량과 보유 수량이 달라 체결을 취소했습니다.')
                gain = from_minor_units(gain_minor, places)
                tax=self.users._tax(conn,sell['user_id'],'foreign_security_sale_gain',gain,
                                    currency=currency)
                tax_minor=to_minor_units(tax, places)
                proceeds_units = self._minor_to_ledger_units(price_minor*quantity-tax_minor, places)
                quantum = SCALE // (10 ** places)
                proceeds_units = max(0, proceeds_units//quantum*quantum)
                self.credit_units(conn,sell['user_id'],currency,proceeds_units)
                proceeds = from_minor_units(price_minor*quantity-tax_minor, places)
                if source == 'company':
                    from managers.corporate_actions_manager import CorporateActionsManager
                    CorporateActionsManager.record_issue_fill(conn, sell['order_id'], quantity, float(price))
                self.book._reduce_order(conn,sell,quantity,now)
            self.book._reduce_order(conn,buy,quantity,now)
            self.book._record_execution_event(
                conn, buy, sell, quantity, price, now,
                tax_amount=tax, seller_proceeds=proceeds,
                wallet_watermark=wallet_watermark,
            )
            self.book._apply_execution_price(conn,asset['code'],float(price),now)
            conn.execute(
                "INSERT INTO assets.market_trade_history "
                "(asset_code, side, quantity, price, recorded_epoch) "
                "VALUES (?, ?, ?, ?, ?)",
                (asset['code'], side, quantity, float(price), now),
            )
            if source=='company':
                self.users._refresh_max_shareholder(conn,asset['code'])
            filled += quantity
        return filled
