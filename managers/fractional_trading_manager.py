"""Opt-in spot/fund trading with exact minor-unit cash and bounded decimal units."""
import time
from fractions import Fraction
from decimal import Decimal

from database.user_db import TREASURY_USER_ID
from functions.quantities import parse_quantity, quantity_number, quantity_units, fractional_enabled, QUANTITY_SCALE
from managers.currency_manager import CurrencyManager, SCALE, MAX_UNITS
from managers.market_session_manager import MarketSessionManager


class FractionalTradingManager:
    def __init__(self, book):
        self.book, self.db, self.users = book, book.db, book.user_manager

    @staticmethod
    def _quantum(conn, currency):
        return 10**(6-CurrencyManager.precision(conn, currency))

    @staticmethod
    def _ceil(value, quantum):
        return -(-value // quantum) * quantum

    @staticmethod
    def _native(units):
        return units // SCALE if units % SCALE == 0 else float(Fraction(units, SCALE))

    @staticmethod
    def _held(conn, code, user=None):
        rows = conn.execute('SELECT quantity FROM user_assets WHERE asset_code=?' +
                            (' AND user_id=?' if user is not None else ''),
                            (code, str(user)) if user is not None else (code,)).fetchall()
        return sum(quantity_units(r[0]) for r in rows)

    @staticmethod
    def _reserved(conn, code, user, side):
        return sum(quantity_units(r[0]) for r in conn.execute(
            "SELECT remaining_quantity FROM market_orders WHERE asset_code=? AND user_id=? AND side=? AND status IN ('open','partial')",
            (code, str(user), side)))

    def _debit(self, conn, user, currency, units, account):
        if units < 0 or units > MAX_UNITS or units % self._quantum(conn, currency):
            raise ValueError('결제 금액 또는 화폐 최소단위를 확인해주세요.')
        if units == 0:
            return
        if currency == 'NWX':
            if not self.users._debit_source(conn, str(user), account, units//SCALE):
                raise ValueError('결제 잔액이 부족합니다.')
        elif account and account.startswith('__wallet__:'):
            CurrencyManager.change(conn, user, currency, -units)
        else:
            changed = conn.execute("UPDATE accounts SET balance_units=balance_units-? WHERE account_number=? AND user_id=? AND quote_currency=? AND account_type='currency_saving' AND balance_units>=?",
                                   (units, account, str(user), currency, units))
            if changed.rowcount != 1:
                raise ValueError('외환통장 잔액이 부족합니다.')

    def _credit(self, conn, user, currency, units, account=None, now=None):
        if units <= 0:
            return
        if currency == 'NWX':
            self.book._credit_reserved_source(conn, str(user), account, units//SCALE, int(now or time.time()))
        else:
            from managers.foreign_securities_manager import ForeignSecuritiesManager
            ForeignSecuritiesManager.credit_units(conn, str(user), currency, units, account)

    def _funds(self, conn, user, currency, units, now):
        if currency == 'NWX':
            funds = self.users._choose_funds(conn, str(user))
            if not funds or funds[0]*SCALE < units:
                raise ValueError('매수 예치금이 부족합니다.')
            return funds[1]
        from managers.currency_exchange_manager import CurrencyExchangeManager
        CurrencyExchangeManager._user(conn, str(user))
        CurrencyExchangeManager(self.db)._settle(conn, str(user), now)
        account = conn.execute("SELECT account_number,balance_units FROM accounts WHERE user_id=? AND quote_currency=? AND account_type='currency_saving'", (str(user), currency)).fetchone()
        if account and account['balance_units'] >= units:
            return account['account_number']
        if CurrencyManager.home(conn, user) == currency:
            if CurrencyManager.balance(conn, user, currency) >= units:
                return '__wallet__:' + currency
            raise ValueError('기본화폐 잔액이 부족합니다.')
        raise ValueError(f'{currency} 외환통장을 개설하고 예치금을 확인해주세요.')

    @staticmethod
    def _add_lot(conn, user, code, account, units, price):
        qty = quantity_number(Fraction(units, QUANTITY_SCALE))
        conn.execute("""INSERT INTO user_assets(user_id,asset_code,account_number,quantity,average_cost)
            VALUES (?,?,?,?,?) ON CONFLICT(user_id,asset_code,IFNULL(account_number,'')) DO UPDATE SET
            average_cost=(quantity*average_cost+excluded.quantity*excluded.average_cost)/(quantity+excluded.quantity),
            quantity=ROUND(quantity+excluded.quantity,3)""", (str(user), code, account, qty, price))

    @staticmethod
    def _remove_lots(conn, user, code, units, price):
        left, gain, sold = units, 0, []
        for row in conn.execute('SELECT * FROM user_assets WHERE user_id=? AND asset_code=? ORDER BY holding_id', (str(user), code)).fetchall():
            held = quantity_units(row['quantity'])
            take = min(left, held)
            if take <= 0:
                continue
            gain += max(0, price-int(row['average_cost'])) * take * (SCALE//QUANTITY_SCALE)
            if take == held:
                conn.execute('DELETE FROM user_assets WHERE holding_id=?', (row['holding_id'],))
            else:
                conn.execute('UPDATE user_assets SET quantity=? WHERE holding_id=?',
                             (quantity_number(Fraction(held-take, QUANTITY_SCALE)), row['holding_id']))
            sold.append((row['account_number'], take))
            left -= take
            if not left:
                break
        if left:
            raise ValueError('예약된 매도 수량과 보유 수량이 일치하지 않습니다.')
        return sold, gain

    def _pay_seller(self, conn, user, code, currency, gross, sold, gain, now):
        quantum = self._quantum(conn, currency)
        rate = CurrencyManager.rate_fraction(conn, currency)
        taxable = min(gain, gross) * rate / SCALE
        taxable_nwx = taxable.numerator // taxable.denominator
        tax = self.users._tax(conn, str(user), 'fractional_sale_gain', taxable_nwx)
        tax_local = Fraction(tax*SCALE, 1) / rate
        tax_units = min(gross, self._ceil(-(-tax_local.numerator//tax_local.denominator), quantum))
        net_quanta = (gross-tax_units)//quantum
        total = sum(qty for _, qty in sold)
        allocations = [net_quanta*qty//total for _, qty in sold]
        remainders = sorted(range(len(sold)), key=lambda i: (-(net_quanta*sold[i][1] % total), i))
        for i in remainders[:net_quanta-sum(allocations)]:
            allocations[i] += 1
        for (account, _), amount in zip(sold, allocations):
            self._credit(conn, user, currency, amount*quantum, account, now)

    def order(self, user, code, side, quantity, price):
        user, code = str(user), str(code)
        try:
            if side not in {'buy', 'sell'} or isinstance(price, bool) or not isinstance(price, int) or not 1 <= price <= self.book.MAX_LIMIT_PRICE:
                raise ValueError('주문 방향과 양의 정수 지정가를 확인해주세요.')
            qty = parse_quantity(quantity, fractional=True)
            units = quantity_units(qty)
            error = self.users._settle_user_interest(user)
            if error:
                return False, error
            now = int(time.time())
            with self.db.atomic() as conn:
                error = MarketSessionManager(self.db).trading_error(conn=conn, now=now, asset_code=code)
                if error:
                    return False, error
                asset, _ = self.users._asset_row(conn, code)
                if not fractional_enabled(asset) or asset['status'] != 'active' or asset['price'] <= 0:
                    raise ValueError('현재 소수점 거래가 허용된 거래 중 상품이 아닙니다.')
                if asset['type'] in {'mutual_fund', 'index_fund'}:
                    raise ValueError('일반·인덱스 펀드는 /펀드가입·/펀드환매에서 거래해주세요.')
                currency = asset.get('quote_currency') or 'NWX'
                reserve, account = 0, None
                if side == 'buy':
                    supply = asset.get('supply')
                    collateral = sum(quantity_units(r[0]) for r in conn.execute(
                        'SELECT quantity FROM option_collateral_lots WHERE underlying_code=? AND writer_id=?', (code, user)))
                    if supply is not None and self._held(conn, code, user)+collateral+self._reserved(conn, code, user, side)+units > int(supply)*QUANTITY_SCALE:
                        raise ValueError('총 발행량 한도를 초과하는 주문입니다.')
                    reserve = self._ceil(price*units*(SCALE//QUANTITY_SCALE), self._quantum(conn, currency))
                    if reserve > MAX_UNITS:
                        raise ValueError('주문 예치금 한도를 초과했습니다.')
                    account = self._funds(conn, user, currency, reserve, now)
                    self._debit(conn, user, currency, reserve, account)
                elif self._held(conn, code, user)-self._reserved(conn, code, user, side) < units:
                    raise ValueError('매도 가능 수량이 부족합니다.')
                cursor = conn.execute("""INSERT INTO market_orders
                    (user_id,asset_code,side,limit_price,quantity,remaining_quantity,reserved_cash,funding_account,
                     order_kind,status,created_epoch,updated_epoch,quote_currency,fractional_order,reserved_cash_units)
                    VALUES (?,?,?,?,?,?,?,?, 'asset','open',?,?,?,1,?)""",
                    (user,code,side,price,qty,qty,self._native(reserve),account,now,now,currency,reserve))
                order_id = cursor.lastrowid
                _, filled, _ = self.match(conn, order_id, now)
                remaining = conn.execute('SELECT remaining_quantity FROM market_orders WHERE order_id=?', (order_id,)).fetchone()[0]
            return True, f'✅ 주문 #{order_id} · {qty:,} · 즉시 체결 {filled:,} · 미체결 {quantity_number(remaining):,} {currency}'
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'

    def _consume_buyer(self, conn, buyer, units, price, currency):
        quantum = self._quantum(conn, currency)
        before = int(buyer['executed_value_units'])
        after = before + price*units*(SCALE//QUANTITY_SCALE)
        cost = self._ceil(after, quantum)-self._ceil(before, quantum)
        reserved = int(buyer['reserved_cash_units']) if buyer['fractional_order'] else int(round(float(buyer['reserved_cash'])*SCALE))
        if cost > reserved:
            raise ValueError('매수 예치금이 부족해 체결을 취소했습니다.')
        reserved -= cost
        conn.execute('UPDATE market_orders SET executed_value_units=?,reserved_cash_units=?,reserved_cash=?,fractional_order=1 WHERE order_id=?',
                     (after, reserved, self._native(reserved), buyer['order_id']))
        return cost

    def _reduce(self, conn, order, units, currency, now):
        remaining = quantity_units(order['remaining_quantity'])-units
        filled = quantity_units(order['filled_quantity'])+units
        conn.execute('UPDATE market_orders SET remaining_quantity=?,filled_quantity=?,status=?,updated_epoch=? WHERE order_id=?',
            (quantity_number(Fraction(remaining,QUANTITY_SCALE)), quantity_number(Fraction(filled,QUANTITY_SCALE)),
             'filled' if remaining == 0 else 'partial', now, order['order_id']))
        if not remaining and order['side'] == 'buy':
            row = conn.execute('SELECT reserved_cash_units FROM market_orders WHERE order_id=?',(order['order_id'],)).fetchone()
            self._credit(conn, order['user_id'], currency, row[0], order['funding_account'], now)
            conn.execute('UPDATE market_orders SET reserved_cash=0,reserved_cash_units=0 WHERE order_id=?', (order['order_id'],))

    def match(self, conn, order_id, now):
        count, filled, value = 0, 0, 0
        order = conn.execute('SELECT asset_code FROM market_orders WHERE order_id=?', (order_id,)).fetchone()
        error = MarketSessionManager(self.db).trading_error(
            conn=conn, now=now, asset_code=order['asset_code'] if order else None
        )
        if error:
            return 0, 0, 0
        while True:
            incoming = conn.execute('SELECT * FROM market_orders WHERE order_id=?', (order_id,)).fetchone()
            if not incoming or quantity_units(incoming['remaining_quantity']) <= 0:
                break
            asset, source = self.users._asset_row(conn, incoming['asset_code'])
            if not fractional_enabled(asset) or asset['status'] != 'active':
                break
            currency = asset.get('quote_currency') or 'NWX'
            buy = incoming['side'] == 'buy'
            comparison, direction = ('<=','ASC') if buy else ('>=','DESC')
            maker = conn.execute("SELECT * FROM market_orders WHERE asset_code=? AND order_kind='asset' AND side=? AND user_id<>? "
                "AND status IN ('open','partial') AND remaining_quantity>0 "
                f'AND limit_price{comparison}? ORDER BY limit_price {direction},created_epoch,order_id LIMIT 1',
                (asset['code'], 'sell' if buy else 'buy',incoming['user_id'],incoming['limit_price'])).fetchone()
            if maker:
                units = min(quantity_units(incoming['remaining_quantity']), quantity_units(maker['remaining_quantity']))
                price = int(maker['limit_price'])
                buyer, seller = (incoming,maker) if buy else (maker,incoming)
            elif buy and int(incoming['limit_price']) >= asset['price'] and asset.get('supply') is not None:
                issued = self._held(conn, asset['code']) + sum(quantity_units(r[0]) for r in conn.execute('SELECT quantity FROM option_collateral_lots WHERE underlying_code=?', (asset['code'],)))
                units = min(quantity_units(incoming['remaining_quantity']), max(0,int(asset['supply'])*QUANTITY_SCALE-issued))
                if not units:
                    break
                buyer, seller, price = incoming, None, int(asset['price'])
            else:
                break
            from managers.financial_manager import FinancialManager as Finance
            if not seller and source=='company':Finance.ensure(conn,asset['code'],now)
            spent = self._consume_buyer(conn, buyer, units, price, currency)
            gross = price*units*(SCALE//QUANTITY_SCALE)
            gross = gross//self._quantum(conn,currency)*self._quantum(conn,currency)
            if seller:
                sold, gain = self._remove_lots(conn,seller['user_id'],asset['code'],units,price)
                self._pay_seller(conn,seller['user_id'],asset['code'],currency,gross,sold,gain,now)
                self._credit(conn,TREASURY_USER_ID,currency,spent-gross,now=now)
                if source == 'company':
                    from managers.corporate_actions_manager import CorporateActionsManager
                    CorporateActionsManager.record_issue_fill(conn,seller['order_id'],quantity_number(Fraction(units,QUANTITY_SCALE)),price)
                self._reduce(conn,seller,units,currency,now)
            elif source == 'company' and currency == 'NWX' and conn.execute("SELECT 1 FROM company WHERE company_code=? AND dividend_funding='issuer'", (asset['code'],)).fetchone():
                owner = conn.execute('SELECT COALESCE(parent_company_code,company_code) FROM company WHERE company_code=?',(asset['code'],)).fetchone()[0]
                conn.execute('INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)',(owner,))
                changed=conn.execute('UPDATE issuer_cash_accounts SET balance=balance+? WHERE company_code=? AND balance<=?',(spent//SCALE,owner,self.book.MAX_LIMIT_PRICE-spent//SCALE))
                if changed.rowcount != 1:
                    raise ValueError('발행 법인의 현금 한도를 초과했습니다.')
            else:
                self._credit(conn,TREASURY_USER_ID,currency,spent,now=now)
            if not seller and source=='company':
                share=conn.execute('SELECT dividend_funding FROM company WHERE company_code=?',(asset['code'],)).fetchone()
                amount=Decimal(spent)/SCALE
                Finance.event(conn,asset['code'],'소수점 발행주식 판매',[('cash' if currency=='NWX' and share['dividend_funding']=='issuer' else 'treasury_receivable',amount),('adjustment',-amount)],'financing',now,currency=currency)
            self._add_lot(conn,buyer['user_id'],asset['code'],buyer['funding_account'],units,price)
            self._reduce(conn,buyer,units,currency,now)
            qty = quantity_number(Fraction(units,QUANTITY_SCALE))
            self.book._apply_execution_price(conn,asset['code'],price,now)
            if source == 'company':
                self.users._refresh_max_shareholder(conn,asset['code'])
            count += 1
            filled += units
            value += spent
        return count, quantity_number(Fraction(filled,QUANTITY_SCALE)), self._native(value)

    def cancel(self, user, order_id):
        with self.db.atomic() as conn:
            row = conn.execute("SELECT * FROM market_orders WHERE order_id=? AND user_id=? AND fractional_order=1", (order_id,str(user))).fetchone()
            if not row or row['status'] not in {'open','partial'}:
                return False, '❌ 취소 가능한 본인의 주문이 없습니다.'
            self._credit(conn,user,row['quote_currency'],row['reserved_cash_units'],row['funding_account'])
            conn.execute("UPDATE market_orders SET status='cancelled',reserved_cash=0,reserved_cash_units=0,updated_epoch=? WHERE order_id=?", (int(time.time()),order_id))
        return True, f'✅ 주문 #{order_id}을 취소하고 남은 예치금을 반환했습니다.'

    def trade_at_nav(self, user, code, quantity, sell=False):
        try:
            qty = parse_quantity(quantity,fractional=True)
            units = quantity_units(qty)
            error = self.users._settle_user_interest(str(user))
            if error:
                return False,error
            now=int(time.time())
            with self.db.atomic() as conn:
                error=MarketSessionManager(self.db).trading_error(conn=conn,now=now)
                if error:
                    return False,error
                asset,source=self.users._asset_row(conn,code)
                if not fractional_enabled(asset) or asset['status']!='active' or asset['price']<=0:
                    raise ValueError('소수점 거래 상품의 상태를 확인해주세요.')
                currency=asset.get('quote_currency') or 'NWX'
                price=int(asset['price'])
                quantum=self._quantum(conn,currency)
                raw=price*units*(SCALE//QUANTITY_SCALE)
                if raw>MAX_UNITS:
                    raise ValueError('결제 금액 한도를 초과했습니다.')
                if sell:
                    if self._held(conn,code,user)-self._reserved(conn,code,user,'sell')<units:
                        raise ValueError('매도 가능 수량이 부족합니다.')
                    gross=raw//quantum*quantum
                    self._debit(conn,TREASURY_USER_ID,currency,gross,None if currency=='NWX' else '__wallet__:'+currency)
                    sold,gain=self._remove_lots(conn,user,code,units,price)
                    self._pay_seller(conn,user,code,currency,gross,sold,gain,now)
                else:
                    if asset.get('supply') is not None and self._held(conn,code)+units>int(asset['supply'])*QUANTITY_SCALE:
                        raise ValueError('잔여 발행 수량이 부족합니다.')
                    gross=self._ceil(raw,quantum)
                    account=self._funds(conn,user,currency,gross,now)
                    self._debit(conn,user,currency,gross,account)
                    self._credit(conn,TREASURY_USER_ID,currency,gross,now=now)
                    self._add_lot(conn,user,code,account,units,price)
                if source=='company':
                    self.users._refresh_max_shareholder(conn,code)
            return True,f'✅ {asset["name"]} {qty:,} · {"매도" if sell else "매수"} 금액 {self._native(gross):,} {currency}'
        except (ValueError,TypeError,OverflowError) as exc:
            return False,f'❌ {exc}'
