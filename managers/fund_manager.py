"""Cash-backed 기준 통화 funds. Fund owners are not rows in the citizens table."""
from functions.money import base_currency_code
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from database.fund_accounting import PREFIX, MAX_MONEY, account, change_cash
from functions.admin_access import resolve_user_role
from functions.money import round_money
from functions.quantities import parse_quantity, quantity_units

FUND_TYPES = {'mutual_fund', 'index_fund'}


class FundManager:
    def __init__(self, db):
        self.db = db

    def is_backed(self, code):
        return self.db.products.execute_one(
            'SELECT 1 FROM fund_accounts WHERE product_code=?', (str(code),)
        ) is not None

    @staticmethod
    def create_empty(conn, code, *, funded_conversion=False):
        product = conn.execute('SELECT * FROM investment WHERE product_code=?', (code,)).fetchone()
        if not product or product['product_type'] not in FUND_TYPES:
            raise ValueError('펀드 코드를 확인해주세요.')
        if product['quote_currency'] != base_currency_code(conn):
            raise ValueError('실보유형 펀드는 기준 통화 결제만 지원합니다.')
        if conn.execute('SELECT 1 FROM fund_accounts WHERE product_code=?', (code,)).fetchone():
            return
        if not funded_conversion and conn.execute('SELECT 1 FROM user_assets WHERE asset_code=? AND quantity>0', (code,)).fetchone():
            raise ValueError('기존 보유자가 있는 가격추종형 펀드는 별도의 자산 이전·대사 후 전환해야 합니다.')
        if conn.execute('SELECT 1 FROM users WHERE user_id=?', (PREFIX+code,)).fetchone():
            raise ValueError('펀드 소유자 ID가 기존 사용자와 충돌합니다.')
        conn.execute('INSERT INTO fund_accounts(product_code,owner_id,initial_nav,created_epoch) VALUES (?,?,?,?)',
                     (code, PREFIX+code, product['unit_price'], int(time.time())))
        currency = str(product['quote_currency']).upper()
        places = conn.execute("SELECT decimal_places FROM currencies WHERE code=?", (currency,)).fetchone()
        conn.execute("INSERT OR IGNORE INTO fund_cash_currency_balances "
                     "(product_code,currency_code,decimal_places,balance_minor_units) "
                     "VALUES (?, ?, ?, 0)", (code, currency, int(places[0]) if places else 0))

    @staticmethod
    def _require(conn, actor, code):
        user = conn.execute('SELECT * FROM users WHERE user_id=?', (str(actor),)).fetchone()
        product = conn.execute('SELECT * FROM investment WHERE product_code=?', (str(code),)).fetchone()
        if not product or product['product_type'] not in FUND_TYPES:
            raise ValueError('펀드 코드를 확인해주세요.')
        if not user or user['status'] != 'active' or (
            resolve_user_role(str(actor), dict(user)) != 'admin' and product['manager'] != str(actor)
        ):
            raise ValueError('관리자 또는 해당 펀드 운용자만 사용할 수 있습니다.')
        return product

    def activate(self, actor, code, *, expected_funding=None):
        with self.db.atomic() as conn:
            self._require(conn, actor, code)
            if account(conn, PREFIX+str(code)):
                return True, '✅ 이미 실보유형 펀드입니다. 중복 이전하지 않았습니다.'
            state = self.snapshot_conn(conn, str(code))
            funding = state['conversionCash']
            if not 0 <= funding <= MAX_MONEY:
                raise ValueError('전환에 필요한 현금이 펀드 계정 한도를 초과합니다.')
            if funding:
                from database.user_db import TREASURY_USER_ID
                from managers.user_manager import UserManager
                user = conn.execute('SELECT * FROM users WHERE user_id=?', (str(actor),)).fetchone()
                if resolve_user_role(str(actor), dict(user)) != 'admin':
                    raise ValueError('기존 펀드의 국고 자금 이전은 관리자만 할 수 있습니다.')
                if isinstance(expected_funding, bool) or expected_funding != funding:
                    raise ValueError(f'국고에서 {funding:,} 기준 통화를 이전해야 합니다. 현재 전환 금액을 확인하고 승인해주세요.')
                from managers.currency_manager import CurrencyManager
                # The treasury of the country listing the fund funds the conversion.
                from managers.currency_manager import SCALE
                treasury = CurrencyManager.exchange_treasury(conn, str(code))
                CurrencyManager.treasury_pay(conn, treasury, base_currency_code(conn), funding * SCALE)
            self.create_empty(conn, str(code), funded_conversion=bool(funding))
            if funding:
                change_cash(conn, PREFIX+str(code), funding, 'conversion', str(actor))
        return True, '✅ 펀드 전용 계정을 개설했습니다. 시민은 생성하지 않았습니다.'

    @staticmethod
    def validate_order(conn, owner, code, side):
        fund = account(conn, owner)
        if not fund:
            if str(owner).startswith(PREFIX):
                raise ValueError('등록되지 않은 펀드 계정입니다.')
            return
        product = conn.execute('SELECT * FROM investment WHERE product_code=?', (fund['product_code'],)).fetchone()
        fund_currency = str(product['quote_currency']).upper()
        ledger = conn.execute("SELECT COALESCE(SUM(amount_minor_units),0) FROM fund_cash_ledger "
                              "WHERE product_code=? AND currency_code=?", (fund['product_code'], fund_currency)).fetchone()[0]
        if ledger != fund['cash']:
            raise ValueError('펀드 현금 원장 대사가 일치하지 않습니다.')
        from managers.investment_manager import InvestmentManager
        if not product or product['status'] != 'active':
            raise ValueError('거래 중인 펀드만 구성자산을 주문할 수 있습니다.')
        quote = InvestmentManager.component_quote(conn, code)
        if not quote or str(quote['quote_currency']).upper() != fund_currency:
            raise ValueError(f'펀드는 활성 {fund_currency} 주식·채권·원자재·부동산·특별자산만 거래할 수 있습니다.')
        if side == 'buy':
            allowed = conn.execute('SELECT 1 FROM fund_components WHERE product_code=? AND asset_code=?',
                                   (fund['product_code'], code)).fetchone()
            if product['product_type'] == 'index_fund':
                allowed = conn.execute("SELECT 1 FROM company WHERE company_code=? AND exchange_id=? AND status='active'",
                                       (code, product['underlying_exchange_id'])).fetchone()
            if not allowed:
                raise ValueError('펀드 구성 목록에 포함된 자산만 매수할 수 있습니다.')

    @staticmethod
    def snapshot_conn(conn, code, *, exact=False):
        from managers.investment_manager import InvestmentManager
        fund = conn.execute('SELECT * FROM fund_accounts WHERE product_code=?', (code,)).fetchone()
        if not fund:
            product = conn.execute('SELECT unit_price FROM investment WHERE product_code=?', (code,)).fetchone()
            issued = sum((Decimal(str(r[0])) for r in conn.execute('SELECT quantity FROM user_assets WHERE asset_code=?', (code,))), Decimal(0))
            funding = int((issued * Decimal(str(product['unit_price']))).to_integral_value(rounding=ROUND_CEILING)) if product else 0
            return {'code': code, 'mode': 'synthetic', 'issuedUnits': float(issued), 'conversionCash': funding}
        owner = fund['owner_id']
        product = conn.execute('SELECT * FROM investment WHERE product_code=?', (code,)).fetchone()
        targets = FundManager.target_weights(conn, product)
        target_map = {item['code']: item['weight'] for item in targets}
        positions = []
        value = Decimal(0)
        for row in conn.execute('SELECT asset_code,SUM(quantity) quantity FROM user_assets WHERE user_id=? GROUP BY asset_code', (owner,)):
            quote = InvestmentManager.component_quote(conn, row['asset_code'], active_only=False)
            if not quote:
                raise ValueError(f"보유자산 평가가격이 없습니다: {row['asset_code']}")
            market_value = Decimal(str(row['quantity'])) * Decimal(str(quote['price']))
            value += market_value
            positions.append({'code': row['asset_code'], 'quantity': row['quantity'],
                              'price': quote['price'], 'value': float(market_value), 'assetClass': quote['asset_class']})
        from functions.money import currency_precision, from_minor_units, to_minor_units
        bond_holdings = conn.execute("SELECT h.product_code,h.principal_minor_units,h.face_value_minor_units,"
            "p.market_price_minor_units,p.quote_currency FROM treasury_bond_holdings h "
            "JOIN treasury_bond_products p ON p.product_code=h.product_code "
            "WHERE h.user_id=? AND h.status='active' ORDER BY h.holding_id", (owner,)).fetchall()
        bond_positions = {}
        for row in bond_holdings:
            currency = str(row['quote_currency'] or base_currency_code(conn)).upper()
            places = currency_precision(conn, currency)
            face_units = int(row['face_value_minor_units'] or 0)
            if face_units <= 0:
                raise ValueError(f"국채 액면 최소단위를 확인할 수 없습니다: {row['product_code']}")
            quantity = Decimal(int(row['principal_minor_units'] or 0)) / Decimal(face_units)
            price = from_minor_units(int(row['market_price_minor_units'] or 0), places)
            market_value = from_minor_units(to_minor_units(quantity * price, places), places)
            current = bond_positions.get(row['product_code'])
            if current is None:
                bond_positions[row['product_code']] = [quantity, market_value, price, currency]
            else:
                if current[3] != currency:
                    raise ValueError(f"국채 보유 통화가 서로 다릅니다: {row['product_code']}")
                current[0] += quantity
                current[1] += market_value
        for code, (quantity, market_value, price, currency) in bond_positions.items():
            value += market_value
            positions.append({'code': code, 'quantity': float(quantity), 'price': float(price),
                              'value': float(market_value), 'assetClass': 'bond'})
        reserved = Decimal(str(conn.execute("SELECT COALESCE(SUM(reserved_cash),0) FROM market_orders "
                                "WHERE user_id=? AND status IN ('open','partial','scheduled')", (owner,)).fetchone()[0]))
        issued = sum((Decimal(str(r[0])) for r in conn.execute('SELECT quantity FROM user_assets WHERE asset_code=?', (code,))), Decimal(0))
        receivables = Decimal(str(conn.execute(
            "SELECT COALESCE(SUM(gross_amount-paid_amount),0) FROM preferred_dividend_claims "
            "WHERE user_id=? AND status IN ('unpaid','partial')", (owner,)
        ).fetchone()[0]))
        from functions.money import from_minor_units
        from managers.currency_manager import CurrencyManager
        cash_rows = conn.execute("SELECT currency_code,decimal_places,balance_minor_units "
                                 "FROM fund_cash_currency_balances WHERE product_code=? "
                                 "ORDER BY currency_code", (code,)).fetchall()
        cash_by_currency = {}
        cash_nav_value = Decimal(0)
        for cash_row in cash_rows:
            currency = str(cash_row['currency_code']).upper()
            places = int(cash_row['decimal_places'])
            minor = int(cash_row['balance_minor_units'])
            amount = from_minor_units(minor, places)
            cash_by_currency[currency] = float(amount)
            if currency == str(product['quote_currency']).upper():
                cash_nav_value += amount
            elif minor > 0:
                source_units = minor * 10 ** (6 - places)
                cash_nav_value += Decimal(CurrencyManager.convert(
                    conn, source_units, currency, str(product['quote_currency']).upper()
                )) / Decimal(1_000_000)
        net = value + cash_nav_value + reserved + receivables
        nav = net / issued if issued else Decimal(fund['initial_nav'])
        ledger_gaps = conn.execute("""SELECT b.currency_code FROM fund_cash_currency_balances b
            WHERE b.product_code=? AND b.balance_minor_units<>COALESCE((
                SELECT SUM(l.amount_minor_units) FROM fund_cash_ledger l
                WHERE l.product_code=b.product_code AND l.currency_code=b.currency_code),0)""",
            (code,)).fetchall()
        fund_currency = str(product['quote_currency']).upper()
        home_cash_ledger = conn.execute("SELECT COALESCE(SUM(amount_minor_units),0) "
            "FROM fund_cash_ledger WHERE product_code=? AND currency_code=?",
            (code, fund_currency)).fetchone()[0]
        for position in positions:
            position['targetWeight'] = target_map.get(position['code'], 0.0)
            position['actualWeight'] = float(Decimal(str(position['value'])) / net) if net else 0.0
        allocations = []
        for target in targets:
            position = next((item for item in positions if item['code'] == target['code']), None)
            allocations.append({**target, 'actualValue': position['value'] if position else 0.0,
                               'targetValue': float(net * Decimal(str(target['weight']))),
                               'actualWeight': position['actualWeight'] if position else 0.0})
        return {'code': code, 'mode': 'backed', 'ownerId': owner, 'availableCash': fund['cash'],
                'cashByCurrency': cash_by_currency,
                'reservedCash': float(reserved), 'cash': float(reserved + cash_nav_value),
                'receivables': float(receivables),
                'assetValue': float(value), 'netAssets': net if exact else float(net),
                'issuedUnits': issued if exact else float(issued),
                'nav': nav if exact else float(nav), 'positions': positions, 'allocations': allocations,
                'ledgerBalanced': not ledger_gaps and int(home_cash_ledger) == fund['cash']}

    @staticmethod
    def target_weights(conn, product):
        if not product:
            return []
        if product['product_type'] == 'mutual_fund':
            rows = conn.execute("SELECT asset_code,weight FROM fund_components WHERE product_code=? ORDER BY asset_code",
                                (product['product_code'],)).fetchall()
            return [{'code': row['asset_code'], 'weight': float(row['weight'])} for row in rows]
        if product['product_type'] == 'index_fund':
            rows = conn.execute("SELECT company_code AS code,share_price AS price,total_shares AS shares "
                                "FROM company WHERE exchange_id=? AND status='active' AND share_price>0 "
                                "ORDER BY company_code", (product['underlying_exchange_id'],)).fetchall()
            if not rows:
                return []
            caps = [Decimal(str(row['price'])) * max(0, int(row['shares'] or 0)) for row in rows]
            total = sum(caps, Decimal(0))
            if total <= 0:
                weight = Decimal(1) / Decimal(len(rows))
                return [{'code': row['code'], 'weight': float(weight)} for row in rows]
            return [{'code': row['code'], 'weight': float(cap / total)}
                    for row, cap in zip(rows, caps) if cap > 0]
        return []

    @staticmethod
    def _order_quantity(value, price, *, fractional):
        quantum = Decimal('0.001') if fractional else Decimal(1)
        count = (Decimal(str(value)) / Decimal(str(price)) / quantum).to_integral_value(rounding=ROUND_FLOOR)
        quantity = count * quantum
        if quantity <= 0:
            return None
        return f'{quantity:f}'.rstrip('0').rstrip('.') if fractional else int(quantity)

    def rebalance(self, actor, code):
        """Trade toward target weights using ordinary limit orders; never borrow or mint cash."""
        from managers.order_book_manager import OrderBookManager
        from managers.user_manager import UserManager
        from managers.market_session_manager import MarketSessionManager
        users = UserManager(self.db)
        book = OrderBookManager(self.db, users)
        owner = PREFIX + str(code)
        try:
            with self.db.atomic() as conn:
                product = self._require(conn, actor, code)
                if not account(conn, owner):
                    raise ValueError('실보유형 펀드만 리밸런싱할 수 있습니다.')
                if product['status'] != 'active':
                    raise ValueError('거래 중인 펀드만 리밸런싱할 수 있습니다.')
                pending = conn.execute("SELECT COUNT(*) FROM market_orders WHERE user_id=? "
                    "AND status IN ('open','partial','scheduled')", (owner,)).fetchone()[0]
                if pending:
                    raise ValueError('펀드의 미체결 주문을 먼저 체결시키거나 취소해주세요.')
                state = self.snapshot_conn(conn, code)
                targets = self.target_weights(conn, product)
                if not targets:
                    raise ValueError('리밸런싱할 수 있는 목표 구성자산이 없습니다.')
                fund_currency = str(product['quote_currency']).upper()
                ledger = conn.execute("SELECT COALESCE(SUM(amount_minor_units),0) FROM fund_cash_ledger "
                                      "WHERE product_code=? AND currency_code=?",
                                      (code, fund_currency)).fetchone()[0]
                if ledger != conn.execute('SELECT cash FROM fund_accounts WHERE product_code=?', (code,)).fetchone()[0]:
                    raise ValueError('현금 원장 대사 후 리밸런싱해주세요.')
                error = MarketSessionManager(self.db).trading_error(conn=conn, asset_code=code)
                if error:
                    raise ValueError(error)
            # Sell overweight/removed holdings first. Regular exchange orders
            # enforce supply, sessions, reservations, and order-price rules.
            target = {item['code']: Decimal(str(item['weight'])) for item in targets}
            orders = []
            sold_codes = []
            for position in state['positions']:
                code_value = position['code']
                price = int(round_money(position['price']))
                desired = Decimal(str(state['netAssets'])) * target.get(code_value, Decimal(0))
                excess = max(Decimal(0), Decimal(str(position['value'])) - desired)
                if excess < price:
                    continue
                asset = self.db.users.execute_one(
                    "SELECT type,Fractal FROM market_assets WHERE code=? UNION ALL "
                    "SELECT 'stock',Fractal FROM company WHERE company_code=? UNION ALL "
                    "SELECT product_type,Fractal FROM investment WHERE product_code=? UNION ALL "
                    "SELECT 'bond',0 FROM treasury_bond_products WHERE product_code=?",
                    (code_value, code_value, code_value, code_value),
                )
                if not asset:
                    continue
                quantity = self._order_quantity(min(excess, Decimal(str(position['value']))), price,
                                                fractional=bool(asset['Fractal'] and asset['type'] in {'stock','mutual_fund','index_fund','commodity_spot'}))
                if not quantity:
                    continue
                result = book.place_limit_order(owner, code_value, 'sell', quantity, price)
                orders.append({'assetCode': code_value, 'side': 'sell', 'quantity': quantity,
                               'success': bool(result[0]), 'message': result[1]})
                if result[0]:
                    sold_codes.append(code_value)
            # Spend only cash available after real fills, capped by each deficit.
            refreshed = self.snapshot(code)
            current_values = {item['code']: Decimal(str(item['value'])) for item in refreshed['positions']}
            used_codes = set(sold_codes)
            for item in sorted(targets, key=lambda row: (
                -(Decimal(str(refreshed['netAssets'])) * Decimal(str(row['weight']))
                  - current_values.get(row['code'], Decimal(0))), row['code'])):
                if item['code'] in used_codes or refreshed['availableCash'] <= 0:
                    continue
                quote = self.db.products.execute_one(
                    "SELECT share_price AS price,Fractal FROM company WHERE company_code=? "
                    "UNION ALL SELECT unit_price,Fractal FROM investment WHERE product_code=? "
                    "UNION ALL SELECT price,0 FROM market_assets WHERE code=? "
                    "UNION ALL SELECT market_price,0 FROM treasury_bond_products WHERE product_code=?",
                    (item['code'], item['code'], item['code'], item['code']),
                )
                if not quote or float(quote['price'] or 0) <= 0:
                    continue
                price = max(1, round_money(quote['price']))
                desired = Decimal(str(refreshed['netAssets'])) * Decimal(str(item['weight']))
                deficit = max(Decimal(0), desired - current_values.get(item['code'], Decimal(0)))
                amount = min(deficit, Decimal(str(refreshed['availableCash'])))
                quantity = self._order_quantity(amount, price, fractional=bool(quote['Fractal']))
                if not quantity:
                    continue
                result = book.place_limit_order(owner, item['code'], 'buy', quantity, price)
                orders.append({'assetCode': item['code'], 'side': 'buy', 'quantity': quantity,
                               'success': bool(result[0]), 'message': result[1]})
                used_codes.add(item['code'])
                refreshed = self.snapshot(code)
                current_values = {row['code']: Decimal(str(row['value'])) for row in refreshed['positions']}
            refreshed = self.snapshot(code)
            pending = bool(refreshed['orders'])
            failed = any(not row['success'] for row in orders)
            message = ('일부 주문이 거절되었습니다. 주문별 결과를 확인한 뒤 다시 실행해주세요.' if failed else
                       '리밸런싱 주문을 제출했습니다. 미체결 주문은 체결되거나 취소될 때까지 재실행할 수 없습니다.'
                       if pending else '리밸런싱을 완료했습니다. 소수 단위·발행 한도 때문에 작은 비중 차이는 남을 수 있습니다.')
            return {'success': True, 'message': message, 'orders': orders, 'fund': refreshed,
                    'pending': pending, 'partial': failed}
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(str(exc)) from exc

    def snapshot(self, code):
        with self.db.readonly() as conn:
            result = self.snapshot_conn(conn, str(code))
            result['ledger'] = [dict(r) for r in conn.execute(
                'SELECT * FROM fund_cash_ledger WHERE product_code=? ORDER BY entry_id DESC LIMIT 100', (str(code),))]
            result['orders'] = [dict(r) for r in conn.execute(
                "SELECT order_id,asset_code,side,limit_price,remaining_quantity,reserved_cash,status FROM market_orders "
                "WHERE user_id=? AND status IN ('open','partial','scheduled') ORDER BY order_id DESC", (PREFIX+str(code),))]
            return result

    def order(self, actor, code, asset_code, side, quantity, price):
        from managers.user_manager import UserManager
        from managers.order_book_manager import OrderBookManager
        with self.db.readonly() as conn:
            self._require(conn, actor, code)
            if not account(conn, PREFIX+code):
                raise ValueError('실보유형 펀드 계정을 먼저 개설해주세요.')
            self.validate_order(conn, PREFIX+code, asset_code, side)
        return OrderBookManager(self.db, UserManager(self.db)).place_limit_order(
            PREFIX+code, asset_code, side, quantity, price)

    def cancel(self, actor, code, order_id):
        from managers.user_manager import UserManager
        from managers.order_book_manager import OrderBookManager
        with self.db.readonly() as conn:
            self._require(conn, actor, code)
        return OrderBookManager(self.db, UserManager(self.db)).cancel_order(PREFIX+code, order_id)

    def trade_units(self, user, code, quantity, redeem=False):
        from managers.user_manager import UserManager
        from managers.order_book_manager import OrderBookManager
        from managers.fractional_trading_manager import FractionalTradingManager
        from managers.market_session_manager import MarketSessionManager
        from managers.currency_manager import SCALE
        users = UserManager(self.db)
        fractions = FractionalTradingManager(OrderBookManager(self.db, users))
        try:
            error = users._settle_user_interest(str(user))
            if error:
                return False, error
            with self.db.atomic() as conn:
                product = conn.execute('SELECT * FROM investment WHERE product_code=?', (code,)).fetchone()
                investor = conn.execute("SELECT 1 FROM users WHERE user_id=? AND status='active'", (str(user),)).fetchone()
                if not investor or not product or product['status'] != 'active' or str(user).startswith(PREFIX):
                    raise ValueError('가입·환매 가능한 펀드와 투자자를 확인해주세요.')
                qty = parse_quantity(quantity, fractional=bool(product['Fractal']))
                units = quantity_units(qty)
                error = MarketSessionManager(self.db).trading_error(conn=conn, asset_code=code)
                if error:
                    return False, error
                state = self.snapshot_conn(conn, code, exact=True)
                if state['mode'] != 'backed' or not state['ledgerBalanced']:
                    raise ValueError('펀드 계정과 현금 원장의 대사가 필요합니다.')
                # Compute from quantities and holdings, never the stale displayed quote.
                net = Decimal(str(state['netAssets']))
                issued = Decimal(str(state['issuedUnits']))
                nav = net / issued if issued else Decimal(str(state['nav']))
                if nav <= 0:
                    raise ValueError('펀드 순자산이 없어 가입·환매할 수 없습니다.')
                gross = int((nav * Decimal(str(qty))).to_integral_value(rounding=ROUND_FLOOR if redeem else ROUND_CEILING))
                if not 0 < gross <= MAX_MONEY:
                    raise ValueError('거래 금액은 1 기준 통화 이상이며 결제 한도 이하여야 합니다.')
                if redeem:
                    if fractions._held(conn, code, user) - fractions._reserved(conn, code, user, 'sell') < units:
                        raise ValueError('환매 가능한 좌수가 부족합니다.')
                    if state['availableCash'] < gross:
                        raise ValueError('펀드 현금이 부족합니다. 구성자산 매도·미체결 매수 취소 후 환매를 다시 요청해주세요. 좌수는 차감되지 않았습니다.')
                    if Decimal(str(qty)) == issued and (state['positions'] or state['reservedCash'] or state['receivables']):
                        raise ValueError('최종 좌수 환매 전에는 구성자산을 모두 매도하고 매수 주문을 취소해주세요.')
                    left, gain = Decimal(str(qty)), Decimal(0)
                    for lot in conn.execute('SELECT quantity,average_cost_minor_units FROM user_assets WHERE user_id=? AND asset_code=? ORDER BY holding_id', (str(user), code)):
                        take = min(left, Decimal(str(lot['quantity'])))
                        gain += max(
                            Decimal(0), nav-Decimal(int(lot['average_cost_minor_units'] or 0))
                        ) * take
                        left -= take
                        if left <= 0:
                            break
                    change_cash(conn, state['ownerId'], -gross, 'redemption', str(user))
                    sold, _ = fractions._remove_lots(conn, user, code, units, float(nav))
                    fractions._pay_seller(conn, user, code, base_currency_code(conn), gross*SCALE, sold, int(gain*SCALE), int(time.time()))
                else:
                    if issued + Decimal(str(qty)) > product['total_supply']:
                        raise ValueError('펀드 발행 한도를 초과합니다.')
                    source = fractions._funds(conn, user, base_currency_code(conn), gross*SCALE, int(time.time()))
                    fractions._debit(conn, user, base_currency_code(conn), gross*SCALE, source)
                    change_cash(conn, state['ownerId'], gross, 'subscription', str(user))
                    fractions._add_lot(conn, user, code, source, units, gross / float(qty))
                after = self.snapshot_conn(conn, code)
                conn.execute('UPDATE investment SET unit_price=? WHERE product_code=?', (max(1, round_money(after['nav'])), code))
                conn.execute('INSERT INTO market_trade_history(asset_code,side,quantity,price,recorded_epoch) VALUES (?,?,?,?,?)',
                             (code, 'sell' if redeem else 'buy', qty, max(1, round_money(nav)), int(time.time())))
            return True, f"✅ {code} {qty:,}좌 {'환매' if redeem else '가입'} · {gross:,} 기준 통화"
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'
