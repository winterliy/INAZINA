"""Atomic dividends, equity issuance and value-preserving product combinations."""
import json
import time
import uuid
from collections import defaultdict
from fractions import Fraction

from database.db_manager import DBManager
from database.user_db import TREASURY_USER_ID
from managers.fx_manager import FXManager, SCALE, MAX_UNITS
from managers.user_manager import UserManager


class CorporateActionsManager:
    MAX_MONEY = 2**53 - 1
    DIVIDEND_TYPES = {'stock', '주식', 'etf', 'mutual_fund', 'index_fund', 'commodity_spot'}

    def __init__(self, bot):
        self.bot, self.db = bot, bot.db

    @staticmethod
    def _integer(value, label, minimum=0, maximum=2**53 - 1):
        try:
            number = int(value)
            if isinstance(value, bool) or str(number) != str(value).strip() or not minimum <= number <= maximum:
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f'{label}은 {minimum:,}~{maximum:,} 정수로 입력해주세요.') from None
        return number

    @staticmethod
    def _require(conn, actor):
        row = conn.execute('SELECT role FROM users WHERE user_id=?', (str(actor),)).fetchone()
        if not row or str(row['role']).strip().lower() != 'admin':
            raise ValueError('DB role=admin인 관리자만 사용할 수 있습니다.')

    @staticmethod
    def _audit(conn, actor, kind, code, payload, now):
        conn.execute('INSERT INTO admin_audit(actor_id,action,payload,recorded_epoch) VALUES (?,?,?,?)',
                     (str(actor), kind, json.dumps({'code': code, **payload}, ensure_ascii=False), now))

    @staticmethod
    def _record(conn, key, kind, source, target, payload, now):
        conn.execute('INSERT INTO corporate_actions(operation_key,kind,source_code,target_code,payload,recorded_epoch) VALUES (?,?,?,?,?,?)',
                     (key, kind, source, target, json.dumps(payload, ensure_ascii=False), now))

    @staticmethod
    def _listing(conn, code):
        matches = []
        for table, key, price, supply in (
            ('company', 'company_code', 'share_price', 'total_shares'),
            ('investment', 'product_code', 'unit_price', 'total_supply'),
            ('market_assets', 'code', 'price', 'total_supply'),
        ):
            row = conn.execute(f'SELECT * FROM {table} WHERE {key}=?', (str(code),)).fetchone()
            if row:
                data = dict(row)
                data.update(table=table, key=key, price_key=price, supply_key=supply, code=str(code),
                            type='stock' if table == 'company' else row['product_type'] if table == 'investment' else row['type'])
                matches.append(data)
        if len(matches) != 1:
            raise ValueError('주식 또는 투자상품 코드를 확인해주세요. 중복된 코드는 먼저 정리해주세요.')
        return matches[0]

    @classmethod
    def _dividend_listing(cls, conn, code):
        listing = cls._listing(conn, code)
        if listing['type'] not in cls.DIVIDEND_TYPES or listing['status'] != 'active':
            raise ValueError('거래 중인 주식·우선주·ETF·펀드·현물 상품에 배당을 설정할 수 있습니다.')
        return listing

    @staticmethod
    def _holders(conn, code):
        # A covered call transfers collateral custody, not the writer's dividend right.
        rows = conn.execute('SELECT user_id,SUM(quantity) quantity FROM ('
            'SELECT user_id,quantity FROM user_assets WHERE asset_code=? UNION ALL '
            'SELECT writer_id user_id,quantity FROM option_collateral_lots WHERE underlying_code=?'
            ') GROUP BY user_id ORDER BY user_id', (code, code)).fetchall()
        return [(str(row['user_id']), int(row['quantity'])) for row in rows if row['user_id'] != TREASURY_USER_ID]

    @staticmethod
    def _fx_account(conn, user, currency, now):
        row = conn.execute('SELECT * FROM fx_accounts WHERE user_id=? AND currency_code=?', (user, currency)).fetchone()
        if row:
            return row['account_number']
        product = conn.execute("SELECT product_code FROM fx_products WHERE currency_code=? AND status='active' ORDER BY product_code LIMIT 1", (currency,)).fetchone()
        if not product:
            raise ValueError(f'{currency} 배당을 수령할 외환통장 상품이 없습니다.')
        account = 'FXA' + uuid.uuid4().hex[:16].upper()
        conn.execute('INSERT INTO fx_accounts(account_number,user_id,product_code,currency_code,last_interest_epoch) VALUES (?,?,?,?,?)',
                     (account, user, product['product_code'], currency, now))
        return account

    def _distribute(self, conn, listing, per_share, kind, due, key, now):
        old = conn.execute('SELECT * FROM dividend_distributions WHERE operation_key=?', (key,)).fetchone()
        if old:
            return int(old['gross_amount']), True
        holders = self._holders(conn, listing['code'])
        gross = sum(quantity * per_share for _, quantity in holders)
        if gross > self.MAX_MONEY:
            raise ValueError('총 배당금이 금액 한도를 초과했습니다.')
        currency = listing.get('quote_currency') or 'NWX'
        rate = FXManager._rate(conn, currency)
        if gross * rate > self.MAX_MONEY or gross * SCALE > MAX_UNITS:
            raise ValueError('배당금의 외환 평가액 또는 지급액이 한도를 초과했습니다.')
        if currency == 'NWX':
            changed = conn.execute('UPDATE users SET cash=cash-? WHERE user_id=? AND cash>=?', (gross, TREASURY_USER_ID, gross))
        else:
            changed = conn.execute('UPDATE fx_accounts SET balance_units=balance_units-? WHERE user_id=? AND currency_code=? AND balance_units>=?',
                                   (gross * SCALE, TREASURY_USER_ID, currency, gross * SCALE))
        if changed.rowcount != 1:
            raise ValueError(f'국고의 {currency} 배당 재원이 부족합니다. 필요액 {gross:,} {currency}')
        distribution = conn.execute('INSERT INTO dividend_distributions(operation_key,asset_code,kind,due_epoch,per_share,quote_currency,gross_amount,recorded_epoch) VALUES (?,?,?,?,?,?,?,?)',
                                    (key, listing['code'], kind, due, per_share, currency, gross, now)).lastrowid
        for user, quantity in holders:
            amount = quantity * per_share
            tax = UserManager._tax(conn, user, f'{kind}_dividend:{listing["code"]}', amount * rate)
            tax_units = tax * SCALE if currency == 'NWX' else (tax * SCALE + rate - 1) // rate
            net_units = max(0, amount * SCALE - tax_units)
            if currency == 'NWX':
                changed = conn.execute('UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?',
                                       (net_units // SCALE, user, self.MAX_MONEY - net_units // SCALE))
            else:
                FXManager._settle(conn, user, now)
                account = self._fx_account(conn, user, currency, now)
                changed = conn.execute('UPDATE fx_accounts SET balance_units=balance_units+? WHERE account_number=? AND balance_units<=?',
                                       (net_units, account, MAX_UNITS - net_units))
            if changed.rowcount != 1:
                raise ValueError('배당 수령자의 계정 또는 잔액 한도를 확인해주세요.')
            conn.execute('INSERT INTO dividend_payments(distribution_id,user_id,quantity,gross_amount,tax_nwx,net_units,quote_currency) VALUES (?,?,?,?,?,?,?)',
                         (distribution, user, quantity, amount, tax, net_units, currency))
            conn.execute('INSERT INTO product_settlements(user_id,product_code,kind,quantity,gross_amount,tax_amount,recorded_epoch) VALUES (?,?,?,?,?,?,?)',
                         (user, listing['code'], f'{kind}_dividend:{currency}', quantity, amount, tax, now))
        self._record(conn, key, f'{kind}_dividend', listing['code'], None,
                     {'distribution_id': distribution, 'per_share': per_share, 'gross_amount': gross, 'currency': currency}, now)
        return gross, False

    def configure_dividend(self, actor, code, cycle_seconds, per_share):
        try:
            cycle = self._integer(cycle_seconds, '배당주기(초)', 0, 3650 * 86400)
            amount = self._integer(per_share, '주당 배당금')
            now = int(time.time())
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                listing = self._dividend_listing(conn, code)
                next_epoch = now + cycle if cycle and amount else None
                conn.execute(f'UPDATE {listing["table"]} SET dividend_cycle_seconds=?,dividend_per_share=?,dividend_next_epoch=? WHERE {listing["key"]}=?',
                             (cycle, amount, next_epoch, str(code)))
                self._audit(conn, actor, 'dividend.configure', str(code), {'cycle_seconds': cycle, 'per_share': amount, 'next_epoch': next_epoch}, now)
            return True, f'✅ `{code}` 정기배당을 설정했습니다. 주기 {cycle:,}초 · 주당 {amount:,} {listing.get("quote_currency") or "NWX"}' + (' · 자동배당 중지' if not next_epoch else '')
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'

    def special_dividend(self, actor, code, per_share, operation_key=None):
        try:
            amount = self._integer(per_share, '주당 특별배당금', 1)
            now, key = int(time.time()), operation_key or 'special:' + uuid.uuid4().hex
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                listing = self._dividend_listing(conn, code)
                gross, duplicate = self._distribute(conn, listing, amount, 'special', now, key, now)
                if not duplicate:
                    self._audit(conn, actor, 'dividend.special', str(code), {'per_share': amount, 'gross_amount': gross, 'operation_key': key}, now)
            return True, f'✅ `{code}` 특별배당 {"지급 내역을 확인했습니다" if duplicate else "지급을 완료했습니다"}. 주당 {amount:,} · 총 {gross:,} {listing.get("quote_currency") or "NWX"}'
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'

    def process_due(self, now=None):
        """Each persisted due time pays once; unfunded cycles stay due for retry."""
        now = int(time.time() if now is None else now)
        results = []
        listings = []
        for database, table, key in ((self.db.companies, 'company', 'company_code'), (self.db.products, 'investment', 'product_code'), (self.db.assets, 'market_assets', 'code')):
            listings.extend(row[key] for row in database.execute(f"SELECT {key} FROM {table} WHERE status='active' AND dividend_cycle_seconds>0 AND dividend_per_share>0"))
        for code in listings:
            for _ in range(20):
                try:
                    with self.db.atomic() as conn:
                        listing = self._dividend_listing(conn, code)
                        cycle, per_share = int(listing['dividend_cycle_seconds']), int(listing['dividend_per_share'])
                        due = listing['dividend_next_epoch']
                        if due is None:
                            conn.execute(f'UPDATE {listing["table"]} SET dividend_next_epoch=? WHERE {listing["key"]}=?', (now + cycle, code))
                            break
                        if due > now:
                            break
                        key = f'regular:{code}:{due}'
                        gross, duplicate = self._distribute(conn, listing, per_share, 'regular', due, key, now)
                        conn.execute(f'UPDATE {listing["table"]} SET dividend_next_epoch=? WHERE {listing["key"]}=?', (due + cycle, code))
                    if not duplicate:
                        results.append((code, True, gross))
                except (ValueError, OverflowError) as exc:
                    results.append((code, False, str(exc)))
                    break
        return results

    @staticmethod
    def record_issue_fill(conn, order_id, quantity, price):
        """Called within an ordinary exchange fill; no nested transaction."""
        offering = conn.execute('SELECT * FROM corporate_equity_offerings WHERE order_id=?', (order_id,)).fetchone()
        if not offering:
            return
        if quantity <= 0 or quantity + int(offering['sold_quantity']) > int(offering['quantity']):
            raise ValueError('신주 판매 기록과 체결 수량이 일치하지 않습니다.')
        changed = conn.execute('UPDATE company SET capital=COALESCE(capital,0)+? WHERE company_code=? AND COALESCE(capital,0)<=?',
                               (quantity * price, offering['asset_code'], CorporateActionsManager.MAX_MONEY - quantity * price))
        if changed.rowcount != 1:
            raise ValueError('신주 발행 법인의 자본금 한도를 초과합니다.')
        conn.execute('UPDATE corporate_equity_offerings SET sold_quantity=sold_quantity+? WHERE offering_id=?', (quantity, offering['offering_id']))

    @staticmethod
    def _unencumbered(conn, codes, allow_orders=False):
        for code in codes:
            if not allow_orders and conn.execute("SELECT 1 FROM market_orders WHERE asset_code=? AND status IN ('open','partial') LIMIT 1", (code,)).fetchone():
                raise ValueError(f'`{code}`의 미체결 주문을 먼저 취소해주세요.')
            if conn.execute("SELECT 1 FROM option_contracts WHERE (underlying_code=? OR contract_code=?) AND status='active' LIMIT 1", (code, code)).fetchone():
                raise ValueError(f'`{code}`의 활성 옵션 계약을 먼저 정산해주세요.')
            if conn.execute("SELECT 1 FROM investment WHERE status='active' AND product_type IN ('stock_future','corporate_bond') AND (underlying_code=? OR issuer_code=?) LIMIT 1", (code, code)).fetchone():
                raise ValueError(f'`{code}`에 연결된 선물·회사채 계약을 먼저 정산해주세요.')
            if conn.execute('SELECT 1 FROM futures_positions WHERE product_code=? LIMIT 1', (code,)).fetchone():
                raise ValueError('선물 포지션을 먼저 정산해주세요.')
            if conn.execute('SELECT 1 FROM corporate_equity_offerings e JOIN market_orders o USING(order_id) WHERE e.asset_code=? AND o.status IN (\'open\',\'partial\') LIMIT 1', (code,)).fetchone():
                raise ValueError('진행 중인 유상증자 신주 판매를 먼저 완료하거나 취소해주세요.')

    @staticmethod
    def _reset_market_state(conn, code, price=None, shares=None):
        for table in ('market_simulation_state', 'market_auto_targets', 'market_quote_overrides'):
            # Earlier installations use market_auto_target_state rather than a targets table.
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                conn.execute(f'DELETE FROM {table} WHERE asset_code=?', (code,))
        for table in ('market_auto_target_state',):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                conn.execute(f'DELETE FROM {table} WHERE asset_code=?', (code,))
        conn.execute("UPDATE market_event_targets SET state='superseded',completed_epoch=? WHERE asset_code=? AND state='active'", (int(time.time()), code))
        conn.execute('DELETE FROM market_last_trade WHERE asset_code=?', (code,))
        if price is not None and shares is not None:
            conn.execute('UPDATE market_exchange_index_components SET previous_price=?,total_shares=? WHERE company_code=?', (price, shares, code))

    @staticmethod
    def _allocate(total, weights):
        denominator = sum(weights)
        if denominator <= 0:
            raise ValueError('배정 기준 수량이 없습니다.')
        allocated = [total * weight // denominator for weight in weights]
        left = total - sum(allocated)
        order = sorted(range(len(weights)), key=lambda index: (-(total * weights[index] % denominator), index))
        for index in order[:left]:
            allocated[index] += 1
        return allocated

    def increase_capital(self, actor, mode, code, quantity, issue_price=None):
        try:
            if mode not in {'유상', '무상'}:
                raise ValueError('증자 유형은 유상 또는 무상입니다.')
            quantity = self._integer(quantity, '신주 수량', 1)
            now = int(time.time())
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                listing = self._listing(conn, code)
                if listing['table'] != 'company' or listing['status'] != 'active':
                    raise ValueError('증자는 거래 중인 회사 주식·우선주에 사용할 수 있습니다.')
                supply = int(listing['total_shares'] or 0)
                if not 0 < supply <= self.MAX_MONEY - quantity:
                    raise ValueError('기존 발행량 또는 신규 발행량 한도를 확인해주세요.')
                if mode == '무상':
                    self._unencumbered(conn, [code])
                elif conn.execute("SELECT 1 FROM corporate_equity_offerings e JOIN market_orders o USING(order_id) WHERE e.asset_code=? AND o.status IN ('open','partial') LIMIT 1", (code,)).fetchone():
                    raise ValueError('진행 중인 유상증자 신주 판매를 먼저 완료하거나 취소해주세요.')
                price = int(listing['share_price'])
                currency = listing.get('quote_currency') or 'NWX'
                if mode == '유상':
                    from managers.market_session_manager import MarketSessionManager
                    error = MarketSessionManager(self.db).trading_error(conn=conn, now=now)
                    if error:
                        raise ValueError(error)
                    issue = self._integer(price if issue_price is None else issue_price, '유상 발행가', 1)
                    if quantity * issue > self.MAX_MONEY:
                        raise ValueError('유상증자 모집 금액이 한도를 초과합니다.')
                    if currency != 'NWX':
                        self._fx_account(conn, TREASURY_USER_ID, currency, now)
                    conn.execute("INSERT INTO user_assets(user_id,asset_code,quantity,average_cost) VALUES (?,?,?,?) ON CONFLICT(user_id,asset_code,IFNULL(account_number,'')) DO UPDATE SET average_cost=(quantity*average_cost+excluded.quantity*excluded.average_cost)/(quantity+excluded.quantity),quantity=quantity+excluded.quantity",
                                 (TREASURY_USER_ID, code, quantity, issue))
                    order = conn.execute("INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,remaining_quantity,order_kind,status,created_epoch,updated_epoch,quote_currency) VALUES (?,?,'sell',?,?,?,'asset','open',?,?,?)",
                                         (TREASURY_USER_ID, code, issue, quantity, quantity, now, now, currency)).lastrowid
                    conn.execute('INSERT INTO corporate_equity_offerings(asset_code,order_id,quantity,issue_price,recorded_epoch) VALUES (?,?,?,?,?)', (code, order, quantity, issue, now))
                    conn.execute('UPDATE company SET total_shares=total_shares+? WHERE company_code=?', (quantity, code))
                    if hasattr(self.bot, 'order_book'):
                        if currency == 'NWX':
                            self.bot.order_book._match_incoming_order(conn, order, now)
                        else:
                            from managers.foreign_securities_manager import ForeignSecuritiesManager
                            ForeignSecuritiesManager(self.bot.order_book).match(conn, order, now)
                    self._record(conn, 'paid:' + uuid.uuid4().hex, 'paid_capital_increase', code, None, {'quantity': quantity, 'issue_price': issue, 'order_id': order}, now)
                    message = f'✅ `{code}` 유상증자 신주 {quantity:,}주를 {issue:,} {currency}에 판매합니다. 국고 매도 주문 #{order} · 자본금은 체결 금액만큼 증가'
                else:
                    if issue_price is not None:
                        raise ValueError('무상증자에는 발행가를 입력하지 않습니다.')
                    lots = conn.execute('SELECT * FROM user_assets WHERE asset_code=? ORDER BY holding_id', (code,)).fetchall()
                    owned = sum(int(lot['quantity']) for lot in lots)
                    if owned > supply:
                        raise ValueError('보유 수량이 총 발행량을 초과합니다.')
                    weights = [int(lot['quantity']) for lot in lots] + [supply - owned]
                    allocations = self._allocate(quantity, weights)
                    for lot, new in zip(lots, allocations):
                        if new:
                            conn.execute('UPDATE user_assets SET quantity=quantity+?,average_cost=? WHERE holding_id=?',
                                         (new, round(int(lot['quantity']) * int(lot['average_cost']) / (int(lot['quantity']) + new)), lot['holding_id']))
                    if allocations[-1]:
                        conn.execute("INSERT INTO user_assets(user_id,asset_code,quantity,average_cost) VALUES (?,?,?,0) ON CONFLICT(user_id,asset_code,IFNULL(account_number,'')) DO UPDATE SET average_cost=quantity*average_cost/(quantity+excluded.quantity),quantity=quantity+excluded.quantity",
                                     (TREASURY_USER_ID, code, allocations[-1]))
                    new_price = max(1, round(Fraction(price * supply, supply + quantity)))
                    conn.execute('UPDATE company SET total_shares=total_shares+?,share_price=? WHERE company_code=?', (quantity, new_price, code))
                    conn.execute('UPDATE fund_components SET base_price=base_price*?/ ? WHERE asset_code=?', (new_price, price, code))
                    self._reset_market_state(conn, code, new_price, supply + quantity)
                    conn.execute('INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES (?,?,?) ON CONFLICT(asset_code,recorded_epoch) DO UPDATE SET price=excluded.price', (code, new_price, now))
                    self._record(conn, 'bonus:' + uuid.uuid4().hex, 'bonus_capital_increase', code, None,
                                 {'quantity': quantity, 'old_supply': supply, 'old_price': price, 'new_price': new_price,
                                  'allocations': [{'user_id': lot['user_id'], 'account_number': lot['account_number'], 'old_quantity': int(lot['quantity']), 'new_shares': new, 'old_average_cost': int(lot['average_cost'])} for lot, new in zip(lots, allocations)],
                                  'unissued_bonus_to_treasury': allocations[-1]}, now)
                    message = f'✅ `{code}` 무상증자 {quantity:,}주를 비례 배정했습니다. 발행량 {supply:,} → {supply + quantity:,} · 기준가 {price:,} → {new_price:,} {currency}'
                UserManager._refresh_max_shareholder(conn, code)
                self._audit(conn, actor, 'capital_increase', code, {'mode': mode, 'quantity': quantity, 'issue_price': issue_price}, now)
            return True, message
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'

    def merge(self, actor, surviving_code, absorbed_code):
        """Exchange equal-value units, retaining B's history and deactivating B."""
        try:
            if surviving_code == absorbed_code:
                raise ValueError('서로 다른 두 코드를 입력해주세요.')
            now = int(time.time())
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                try:
                    a, b = self._listing(conn, surviving_code), self._listing(conn, absorbed_code)
                except ValueError:
                    return self._merge_nonmarket(conn, actor, surviving_code, absorbed_code, now)
                if a['table'] != b['table'] or a['type'] != b['type'] or a['status'] != 'active' or b['status'] != 'active':
                    raise ValueError('거래 중인 같은 종류의 상품끼리 통합할 수 있습니다.')
                if a['type'] == 'forex' or conn.execute('SELECT 1 FROM currencies WHERE asset_code IN (?,?) LIMIT 1', (surviving_code, absorbed_code)).fetchone():
                    raise ValueError('환율 종목은 통합할 수 없습니다. 환율수정·환전으로 통화를 관리해주세요.')
                if a['table'] == 'company' and a['share_class'] != b['share_class']:
                    raise ValueError('보통주와 우선주는 같은 종류끼리 합병해주세요.')
                if a['table'] == 'company' and a['share_class'] == 'preferred' and a['parent_company_code'] != b['parent_company_code']:
                    raise ValueError('같은 발행 법인의 우선주끼리 합병해주세요.')
                currency = a.get('quote_currency') or 'NWX'
                if currency != (b.get('quote_currency') or 'NWX'):
                    raise ValueError('결제 통화가 같은 상품끼리 통합할 수 있습니다.')
                self._unencumbered(conn, [surviving_code, absorbed_code])
                if a['type'] in {'stock_future', 'corporate_bond'} and conn.execute('SELECT 1 FROM user_assets WHERE asset_code IN (?,?) LIMIT 1', (surviving_code, absorbed_code)).fetchone():
                    raise ValueError('채권·선물의 미상환 보유분을 먼저 정산해주세요.')
                price_a, price_b = int(a[a['price_key']]), int(b[b['price_key']])
                supply_a, supply_b = int(a[a['supply_key']] or 0), int(b[b['supply_key']] or 0)
                if min(price_a, price_b, supply_a, supply_b) <= 0:
                    raise ValueError('가격과 발행량이 양수인 상품만 통합할 수 있습니다.')
                lots = conn.execute('SELECT * FROM user_assets WHERE asset_code=? ORDER BY holding_id', (absorbed_code,)).fetchall()
                groups = defaultdict(list)
                for lot in lots:
                    groups[str(lot['user_id'])].append(lot)
                conversions, total_new, cash = [], 0, 0
                for user, user_lots in groups.items():
                    quantity = sum(int(lot['quantity']) for lot in user_lots)
                    units, residual = divmod(quantity * price_b, price_a)
                    allocation = self._allocate(units, [int(lot['quantity']) for lot in user_lots]) if units else [0] * len(user_lots)
                    cash += residual if user != TREASURY_USER_ID else 0
                    total_new += units
                    conversions.append((user, user_lots, allocation, residual))
                unissued = max(0, supply_b - sum(int(lot['quantity']) for lot in lots))
                new_supply = supply_a + total_new + unissued * price_b // price_a
                if new_supply > self.MAX_MONEY or cash > self.MAX_MONEY:
                    raise ValueError('통합 후 발행량 또는 단주 정산금이 한도를 초과합니다.')
                if currency == 'NWX':
                    changed = conn.execute('UPDATE users SET cash=cash-? WHERE user_id=? AND cash>=?', (cash, TREASURY_USER_ID, cash))
                else:
                    changed = conn.execute('UPDATE fx_accounts SET balance_units=balance_units-? WHERE user_id=? AND currency_code=? AND balance_units>=?', (cash * SCALE, TREASURY_USER_ID, currency, cash * SCALE))
                if changed.rowcount != 1:
                    raise ValueError('단주 차액을 지급할 국고 잔액이 부족합니다.')
                for user, user_lots, allocation, residual in conversions:
                    for lot, units in zip(user_lots, allocation):
                        if units:
                            cost = round(Fraction(int(lot['average_cost']) * price_a, price_b))
                            conn.execute("INSERT INTO user_assets(user_id,asset_code,account_number,quantity,average_cost) VALUES (?,?,?,?,?) ON CONFLICT(user_id,asset_code,IFNULL(account_number,'')) DO UPDATE SET average_cost=(quantity*average_cost+excluded.quantity*excluded.average_cost)/(quantity+excluded.quantity),quantity=quantity+excluded.quantity",
                                         (user, surviving_code, lot['account_number'], units, cost))
                    if residual and user != TREASURY_USER_ID:
                        if currency == 'NWX':
                            changed = conn.execute('UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?', (residual, user, self.MAX_MONEY - residual))
                        else:
                            FXManager._settle(conn, user, now)
                            account = self._fx_account(conn, user, currency, now)
                            changed = conn.execute('UPDATE fx_accounts SET balance_units=balance_units+? WHERE account_number=? AND balance_units<=?', (residual * SCALE, account, MAX_UNITS - residual * SCALE))
                        if changed.rowcount != 1:
                            raise ValueError('단주 정산 수령자의 잔액 한도를 확인해주세요.')
                        conn.execute('INSERT INTO product_settlements(user_id,product_code,kind,quantity,gross_amount,recorded_epoch) VALUES (?,?,?,?,?,?)', (user, absorbed_code, f'merger_cash:{currency}', sum(int(lot['quantity']) for lot in user_lots), residual, now))
                conn.execute('DELETE FROM user_assets WHERE asset_code=?', (absorbed_code,))
                if a['table'] == 'company':
                    capital = int(a['capital'] or 0) + int(b['capital'] or 0)
                    if capital > self.MAX_MONEY:
                        raise ValueError('통합 법인의 자본금 한도를 초과합니다.')
                    conn.execute('UPDATE company SET total_shares=?,capital=? WHERE company_code=?', (new_supply, capital, surviving_code))
                    conn.execute('UPDATE company SET parent_company_code=? WHERE parent_company_code=?', (surviving_code, absorbed_code))
                    UserManager._refresh_max_shareholder(conn, surviving_code)
                else:
                    conn.execute(f'UPDATE {a["table"]} SET {a["supply_key"]}=? WHERE {a["key"]}=?', (new_supply, surviving_code))
                conn.execute(f"UPDATE {b['table']} SET status='inactive',dividend_next_epoch=NULL WHERE {b['key']}=?", (absorbed_code,))
                self._merge_components(conn, surviving_code, absorbed_code, price_a, price_b)
                self._reset_market_state(conn, surviving_code, price_a, new_supply)
                self._reset_market_state(conn, absorbed_code)
                conn.execute('DELETE FROM market_exchange_index_components WHERE company_code=?', (absorbed_code,))
                payload = {'survivor': surviving_code, 'absorbed': absorbed_code, 'exchange_ratio': str(Fraction(price_b, price_a)), 'cash_settled': cash, 'new_supply': new_supply, 'currency': currency,
                           'positions': [{'user_id': user, 'cash_residual': residual, 'lots': [{'account_number': lot['account_number'], 'source_quantity': int(lot['quantity']), 'target_quantity': units, 'source_average_cost': int(lot['average_cost'])} for lot, units in zip(user_lots, allocation)]} for user, user_lots, allocation, residual in conversions]}
                self._record(conn, 'merger:' + uuid.uuid4().hex, 'merger', absorbed_code, surviving_code, payload, now)
                self._audit(conn, actor, 'merger', surviving_code, payload, now)
            return True, f'✅ `{absorbed_code}`를 `{surviving_code}`에 통합했습니다. 교환비율 {price_b:,}/{price_a:,} · 단주 정산 {cash:,} {currency} · 존속 `{surviving_code}`'
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'

    @staticmethod
    def _merge_components(conn, a, b, price_a, price_b):
        for row in conn.execute('SELECT * FROM fund_components WHERE asset_code=?', (b,)).fetchall():
            existing = conn.execute('SELECT * FROM fund_components WHERE product_code=? AND asset_code=?', (row['product_code'], a)).fetchone()
            converted_base = float(row['base_price']) * price_a / price_b
            if existing:
                weight = float(existing['weight']) + float(row['weight'])
                # Harmonic basis keeps both pre-merger index contributions equal.
                basis = weight / (float(existing['weight']) / float(existing['base_price']) + float(row['weight']) / converted_base)
                conn.execute('UPDATE fund_components SET weight=?,base_price=? WHERE product_code=? AND asset_code=?', (weight, basis, row['product_code'], a))
                conn.execute('DELETE FROM fund_components WHERE product_code=? AND asset_code=?', (row['product_code'], b))
            else:
                conn.execute('UPDATE fund_components SET asset_code=?,base_price=? WHERE product_code=? AND asset_code=?', (a, converted_base, row['product_code'], b))

    def _merge_nonmarket(self, conn, actor, a, b, now):
        """Savings/debt offers may merge once all original contracts are closed."""
        for table in ('saving', 'liability', 'treasury_bond_products', 'fx_products'):
            first = conn.execute(f'SELECT * FROM {table} WHERE product_code=?', (a,)).fetchone()
            second = conn.execute(f'SELECT * FROM {table} WHERE product_code=?', (b,)).fetchone()
            if not first or not second:
                continue
            if first['status'] != 'active' or second['status'] != 'active':
                raise ValueError('활성 상품 두 개를 입력해주세요.')
            self._unencumbered(conn, [a, b])
            if table == 'saving' and conn.execute('SELECT 1 FROM accounts WHERE product_code=? LIMIT 1', (b,)).fetchone():
                raise ValueError('흡수할 저축상품의 계좌를 먼저 해지해주세요. 가입 계약은 자동 변경하지 않습니다.')
            if table == 'liability' and conn.execute("SELECT 1 FROM loans WHERE product_code=? AND status IN ('active','overdue') LIMIT 1", (b,)).fetchone():
                raise ValueError('흡수할 대출상품의 채무를 먼저 상환해주세요.')
            if table == 'treasury_bond_products':
                if conn.execute("SELECT 1 FROM treasury_bond_holdings WHERE product_code=? AND status='active' LIMIT 1", (b,)).fetchone():
                    raise ValueError('흡수할 국채의 보유 계약을 먼저 정산해주세요.')
                if int(first['face_value']) != int(second['face_value']):
                    raise ValueError('국채 액면가가 다릅니다.')
                extra = int(second['total_supply']) - int(second['sold_amount'])
                total = int(first['total_supply']) + extra
                if total > self.MAX_MONEY:
                    raise ValueError('통합 국채 발행 한도를 초과합니다.')
                conn.execute('UPDATE treasury_bond_products SET total_supply=? WHERE product_code=?', (total, a))
            if table == 'fx_products':
                if first['currency_code'] != second['currency_code']:
                    raise ValueError('같은 통화의 외환통장 상품끼리 통합할 수 있습니다.')
                for user in conn.execute('SELECT user_id FROM fx_accounts WHERE product_code=?', (b,)).fetchall():
                    FXManager._settle(conn, user['user_id'], now)
                conn.execute('UPDATE fx_accounts SET product_code=? WHERE product_code=?', (a, b))
            conn.execute(f"UPDATE {table} SET status='inactive' WHERE product_code=?", (b,))
            payload = {'survivor': a, 'absorbed': b, 'table': table}
            self._record(conn, 'merger:' + uuid.uuid4().hex, 'product_merger', b, a, payload, now)
            self._audit(conn, actor, 'merger', a, payload, now)
            return True, f'✅ `{b}` 상품을 `{a}` 상품에 통합했습니다. 존속 `{a}`'
        raise ValueError('같은 종류의 회사·투자·금융 상품 코드를 입력해주세요.')
