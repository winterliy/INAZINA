"""Cash-backed NWX funds. Fund owners are not rows in the citizens table."""
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
        if product['quote_currency'] != 'NWX':
            raise ValueError('실보유형 펀드는 NWX 결제만 지원합니다.')
        if conn.execute('SELECT 1 FROM fund_accounts WHERE product_code=?', (code,)).fetchone():
            return
        if not funded_conversion and conn.execute('SELECT 1 FROM user_assets WHERE asset_code=? AND quantity>0', (code,)).fetchone():
            raise ValueError('기존 보유자가 있는 가격추종형 펀드는 별도의 자산 이전·대사 후 전환해야 합니다.')
        if conn.execute('SELECT 1 FROM users WHERE user_id=?', (PREFIX+code,)).fetchone():
            raise ValueError('펀드 소유자 ID가 기존 사용자와 충돌합니다.')
        conn.execute('INSERT INTO fund_accounts(product_code,owner_id,initial_nav,created_epoch) VALUES (?,?,?,?)',
                     (code, PREFIX+code, product['unit_price'], int(time.time())))

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
                    raise ValueError(f'국고에서 {funding:,} NWX를 이전해야 합니다. 현재 전환 금액을 확인하고 승인해주세요.')
                from managers.currency_manager import CurrencyManager
                # The treasury of the country listing the fund funds the conversion.
                from managers.currency_manager import SCALE
                treasury = CurrencyManager.exchange_treasury(conn, str(code))
                home = CurrencyManager.home(conn, treasury)
                if CurrencyManager.balance(conn, treasury, home) < CurrencyManager.convert(conn, funding * SCALE, 'NWX', home):
                    raise ValueError('기존 펀드의 순자산을 이전할 국고 잔액이 부족합니다.')
                CurrencyManager.treasury_pay(conn, treasury, 'NWX', funding * SCALE)
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
        ledger = conn.execute('SELECT COALESCE(SUM(amount),0) FROM fund_cash_ledger WHERE product_code=?', (fund['product_code'],)).fetchone()[0]
        if ledger != fund['cash']:
            raise ValueError('펀드 현금 원장 대사가 일치하지 않습니다.')
        from managers.investment_manager import InvestmentManager
        product = conn.execute('SELECT * FROM investment WHERE product_code=?', (fund['product_code'],)).fetchone()
        if not product or product['status'] != 'active':
            raise ValueError('거래 중인 펀드만 구성자산을 주문할 수 있습니다.')
        quote = InvestmentManager.component_quote(conn, code)
        if not quote or quote['quote_currency'] != 'NWX':
            raise ValueError('펀드는 활성 NWX 주식·채권·원자재·부동산·특별자산만 거래할 수 있습니다.')
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
        for row in conn.execute("SELECT h.product_code,SUM(h.principal*1.0/h.face_value) quantity,p.market_price price "
                                "FROM treasury_bond_holdings h JOIN treasury_bond_products p ON p.product_code=h.product_code "
                                "WHERE h.user_id=? AND h.status='active' GROUP BY h.product_code,p.market_price", (owner,)):
            market_value = Decimal(str(row['quantity'])) * Decimal(str(row['price']))
            value += market_value
            positions.append({'code': row['product_code'], 'quantity': row['quantity'], 'price': row['price'],
                              'value': float(market_value), 'assetClass': 'bond'})
        reserved = Decimal(str(conn.execute("SELECT COALESCE(SUM(reserved_cash),0) FROM market_orders "
                                "WHERE user_id=? AND status IN ('open','partial','scheduled')", (owner,)).fetchone()[0]))
        issued = sum((Decimal(str(r[0])) for r in conn.execute('SELECT quantity FROM user_assets WHERE asset_code=?', (code,))), Decimal(0))
        receivables = Decimal(str(conn.execute(
            "SELECT COALESCE(SUM(gross_amount-paid_amount),0) FROM preferred_dividend_claims "
            "WHERE user_id=? AND status IN ('unpaid','partial')", (owner,)
        ).fetchone()[0]))
        net = value + fund['cash'] + reserved + receivables
        nav = net / issued if issued else Decimal(fund['initial_nav'])
        ledger = conn.execute('SELECT COALESCE(SUM(amount),0) FROM fund_cash_ledger WHERE product_code=?', (code,)).fetchone()[0]
        return {'code': code, 'mode': 'backed', 'ownerId': owner, 'availableCash': fund['cash'],
                'reservedCash': float(reserved), 'cash': float(reserved + fund['cash']),
                'receivables': float(receivables),
                'assetValue': float(value), 'netAssets': net if exact else float(net),
                'issuedUnits': issued if exact else float(issued),
                'nav': nav if exact else float(nav), 'positions': positions, 'ledgerBalanced': ledger == fund['cash']}

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
                    raise ValueError('거래 금액은 1 NWX 이상이며 결제 한도 이하여야 합니다.')
                if redeem:
                    if fractions._held(conn, code, user) - fractions._reserved(conn, code, user, 'sell') < units:
                        raise ValueError('환매 가능한 좌수가 부족합니다.')
                    if state['availableCash'] < gross:
                        raise ValueError('펀드 현금이 부족합니다. 구성자산 매도·미체결 매수 취소 후 환매를 다시 요청해주세요. 좌수는 차감되지 않았습니다.')
                    if Decimal(str(qty)) == issued and (state['positions'] or state['reservedCash'] or state['receivables']):
                        raise ValueError('최종 좌수 환매 전에는 구성자산을 모두 매도하고 매수 주문을 취소해주세요.')
                    left, gain = Decimal(str(qty)), Decimal(0)
                    for lot in conn.execute('SELECT quantity,average_cost FROM user_assets WHERE user_id=? AND asset_code=? ORDER BY holding_id', (str(user), code)):
                        take = min(left, Decimal(str(lot['quantity'])))
                        gain += max(Decimal(0), nav-Decimal(str(lot['average_cost']))) * take
                        left -= take
                        if left <= 0:
                            break
                    change_cash(conn, state['ownerId'], -gross, 'redemption', str(user))
                    sold, _ = fractions._remove_lots(conn, user, code, units, float(nav))
                    fractions._pay_seller(conn, user, code, 'NWX', gross*SCALE, sold, int(gain*SCALE), int(time.time()))
                else:
                    if issued + Decimal(str(qty)) > product['total_supply']:
                        raise ValueError('펀드 발행 한도를 초과합니다.')
                    source = fractions._funds(conn, user, 'NWX', gross*SCALE, int(time.time()))
                    fractions._debit(conn, user, 'NWX', gross*SCALE, source)
                    change_cash(conn, state['ownerId'], gross, 'subscription', str(user))
                    fractions._add_lot(conn, user, code, source, units, gross / float(qty))
                after = self.snapshot_conn(conn, code)
                conn.execute('UPDATE investment SET unit_price=? WHERE product_code=?', (max(1, round_money(after['nav'])), code))
                conn.execute('INSERT INTO market_trade_history(asset_code,side,quantity,price,recorded_epoch) VALUES (?,?,?,?,?)',
                             (code, 'sell' if redeem else 'buy', qty, max(1, round_money(nav)), int(time.time())))
            return True, f"✅ {code} {qty:,}좌 {'환매' if redeem else '가입'} · {gross:,} NWX"
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'
