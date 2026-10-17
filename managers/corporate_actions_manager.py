"""Atomic dividends, equity issuance and value-preserving product combinations."""
import json
import time
import uuid
from collections import defaultdict
from fractions import Fraction
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_CEILING

from database.db_manager import DBManager
from database.user_db import TREASURY_USER_ID
from managers.currency_exchange_manager import CurrencyExchangeManager, SCALE, MAX_UNITS
from managers.currency_manager import CurrencyManager
from managers.user_manager import UserManager
from managers.financial_manager import FinancialManager as Finance


class CorporateActionsManager:
    MAX_MONEY = 2**53 - 1
    RECORD_LEAD = 600  # the shareholder register closes 10 minutes before payment
    # DR holders receive same-currency underlying dividends through the underlying
    # shareholder register; a second independent DR schedule would double-pay.
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

    @classmethod
    def dividend_terms(cls, value):
        """A bare integer is fixed money; a % suffix selects price-based dividends."""
        text = str(value).strip()
        if text.endswith('%'):
            try:
                percent = Decimal(text[:-1])
                if not percent.is_finite() or not 0 <= percent <= 100:
                    raise ValueError()
            except (InvalidOperation, ValueError):
                raise ValueError('비율배당은 0~100%로 입력해주세요. 예: 5%') from None
            return {'dividend_mode':'percent','dividend_percent':float(percent),'dividend_per_share':0}
        return {'dividend_mode':'fixed','dividend_percent':0,
                'dividend_per_share':cls._integer(value, '주당 배당금')}

    @staticmethod
    def _dividend_configured(listing):
        amount = listing['dividend_percent'] if listing['dividend_mode'] == 'percent' else listing['dividend_per_share']
        return listing['dividend_cycle_seconds'] > 0 and amount > 0

    @staticmethod
    def _dividend_description(listing):
        if listing['dividend_mode'] == 'percent':
            return f'가격 기준 {listing["dividend_percent"]:g}%'
        return f'주당 {listing["dividend_per_share"]:,} {listing.get("quote_currency") or "NWX"}'

    @staticmethod
    def _require(conn, actor):
        row = conn.execute('SELECT role FROM users WHERE user_id=?', (str(actor),)).fetchone()
        if not row or str(row['role']).strip().lower() != 'admin':
            raise ValueError('DB role=admin인 관리자만 사용할 수 있습니다.')

    @staticmethod
    def _audit(conn, actor, kind, code, payload, now):
        return

    @staticmethod
    def _record(conn, key, kind, source, target, payload, now):
        cursor = conn.execute(
            'INSERT OR IGNORE INTO corporate_action_records '
            '(operation_key,action_kind,source_code,target_code,payload_json,created_epoch) '
            'VALUES (?,?,?,?,?,?)',
            (str(key),str(kind),str(source),None if target is None else str(target),
             json.dumps(payload,ensure_ascii=False,sort_keys=True),int(now)),
        )
        return cursor.lastrowid if cursor.rowcount else None

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
        # In-currency DR programs pass their custodied underlying entitlement
        # through to current DR holders. Cross-currency settlement needs a
        # separate depositary FX/cash ledger and is intentionally not implied.
        underlying_currency = conn.execute(
            "SELECT COALESCE((SELECT quote_currency FROM company WHERE company_code=?),"
            "(SELECT quote_currency FROM market_assets WHERE code=?),'NWX')",
            (str(code), str(code)),
        ).fetchone()[0]
        dr_rows = conn.execute(
            "SELECT h.user_id,SUM(h.quantity*d.underlying_per_receipt) quantity "
            "FROM depositary_receipts d JOIN user_assets h ON h.asset_code=d.dr_code "
            "WHERE d.underlying_code=? AND d.program_status<>'terminated' "
            "AND d.quote_currency=? GROUP BY h.user_id ORDER BY h.user_id",
            (str(code), str(underlying_currency)),
        ).fetchall()
        combined = {str(row['user_id']): float(row['quantity'] or 0) for row in rows}
        for row in dr_rows:
            user = str(row['user_id'])
            combined[user] = combined.get(user, 0.0) + float(row['quantity'] or 0)
        # Treasuries and a company's own (treasury) shares receive no dividend.
        from managers.company_account_manager import CompanyAccounts
        return [(user, quantity) for user, quantity in sorted(combined.items()) if quantity > 0
                and not CurrencyManager.is_treasury(conn, user)
                and not CompanyAccounts.own_shares(conn, user, code)]

    @staticmethod
    def _currency_account(conn, user, currency, now):
        row = conn.execute("SELECT * FROM accounts WHERE user_id=? AND quote_currency=? AND account_type='currency_saving'", (user, currency)).fetchone()
        if row:
            return row['account_number']
        raise ValueError(f'{currency} 외환통장을 먼저 개설해주세요.')

    @staticmethod
    def _issuer_code(listing):
        return str(listing.get('parent_company_code') or listing['code'])

    @classmethod
    def _funding(cls, listing):
        return listing.get('dividend_funding', 'treasury') if listing['table'] == 'company' else 'treasury'

    def _source_balance(self, conn, issuer, funding, currency):
        if funding == 'issuer':
            if currency != 'NWX':
                raise ValueError('발행법인 현금 배당은 NWX 상품에만 사용할 수 있습니다.')
            row = conn.execute('SELECT balance FROM issuer_cash_accounts WHERE company_code=?', (issuer,)).fetchone()
            return int(row['balance']) if row else 0
        # The listing country's treasury funds treasury dividends and can always pay.
        return self.MAX_MONEY

    def _debit_dividend(self, conn, issuer, funding, currency, gross, counterpart="retained"):
        from database.fund_accounting import change_cash
        fund = conn.execute('SELECT owner_id FROM fund_accounts WHERE product_code=?', (issuer,)).fetchone()
        if fund:
            if currency != 'NWX':
                raise ValueError('펀드 분배금은 NWX만 지원합니다.')
            change_cash(conn, fund['owner_id'], -gross, 'distribution', issuer)
            return
        if not gross:
            return
        tracked = bool(conn.execute('SELECT 1 FROM company WHERE company_code=?',(issuer,)).fetchone())
        if tracked:
            Finance.ensure(conn,issuer)
        if funding == 'issuer':
            changed = conn.execute('UPDATE issuer_cash_accounts SET balance=balance-? WHERE company_code=? AND balance>=?', (gross, issuer, gross))
        else:
            # A treasury can always pay, even into deficit.
            CurrencyManager.treasury_pay(conn, CurrencyManager.exchange_treasury(conn, issuer), currency, gross*SCALE)
            changed = None
        if changed is not None and changed.rowcount != 1:
            raise ValueError(f'{"법인" if funding == "issuer" else "국고"}의 {currency} 배당 재원이 부족합니다. 필요액 {gross:,} {currency}')
        if tracked:
            Finance.event(conn,issuer,'배당 지급',
                          [(counterpart,gross),('cash' if funding=='issuer' else 'capital_surplus',-gross)],
                          'financing',currency=currency)

    def _credit_dividend(self, conn, user, code, kind, amount, currency, now):
        from database.fund_accounting import account, change_cash
        if account(conn, user):
            if currency != 'NWX':
                raise ValueError('펀드 배당 수령 통화는 NWX만 지원합니다.')
            change_cash(conn, user, amount, 'dividend', code)
            return 0, amount*SCALE
        rate = CurrencyExchangeManager._rate(conn, currency)
        tax = UserManager._tax(conn, user, f'{kind}_dividend:{code}', amount*rate)
        if CurrencyManager.home(conn,user) == currency:
            tax_native = int((Decimal(tax)/Decimal(rate)).to_integral_value(rounding=ROUND_CEILING))
            net_units = max(0,amount-tax_native)*SCALE
            changed = conn.execute('UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?',
                                   (net_units//SCALE, user, self.MAX_MONEY-net_units//SCALE))
        else:
            tax_units = tax*SCALE if currency == 'NWX' else int((Decimal(tax*SCALE)/Decimal(rate)).to_integral_value(rounding=ROUND_CEILING))
            net_units = max(0, amount*SCALE-tax_units)
            CurrencyExchangeManager._settle(conn, user, now)
            account = self._currency_account(conn, user, currency, now)
            changed = conn.execute("UPDATE accounts SET balance_units=balance_units+? WHERE account_number=? AND account_type='currency_saving' AND balance_units<=?",
                                   (net_units, account, MAX_UNITS-net_units))
        if changed.rowcount != 1:
            raise ValueError('배당 수령자의 계정 또는 잔액 한도를 확인해주세요.')
        from managers.company_account_manager import CompanyAccounts
        if CompanyAccounts.is_company(conn, user):
            # A company's dividend income is 영업외수익 in its statements.
            listing = conn.execute('SELECT COALESCE(parent_company_code,company_code) FROM company WHERE company_code=?', (code,)).fetchone()
            CompanyAccounts.log_income(conn, user, 'dividend', amount*rate, tax, counterparty=listing[0] if listing else code)
        return tax, net_units

    def _settle_claims(self, conn, issuer, now):
        """Pay the original record-date owner even after the shares were traded."""
        claims = conn.execute("SELECT c.*,COALESCE(s.dividend_priority,1) priority FROM preferred_dividend_claims c LEFT JOIN company s ON s.company_code=c.asset_code WHERE c.issuer_code=? AND c.status IN ('unpaid','partial') ORDER BY priority DESC,c.due_epoch,c.claim_id", (issuer,)).fetchall()
        paid = 0
        for claim in claims:
            remaining = int(claim['gross_amount'])-int(claim['paid_amount'])
            amount = min(remaining, self._source_balance(conn, issuer, claim['funding_source'], claim['quote_currency']))
            if not amount:
                continue
            self._debit_dividend(conn, issuer, claim['funding_source'], claim['quote_currency'], amount,
                                 counterpart='dividend_payable' if claim['funding_source']=='issuer' else 'retained')
            tax, _ = self._credit_dividend(conn, claim['user_id'], claim['asset_code'], 'arrears', amount, claim['quote_currency'], now)
            total_paid = int(claim['paid_amount'])+amount
            conn.execute("UPDATE preferred_dividend_claims SET paid_amount=?,status=? WHERE claim_id=?",
                         (total_paid, 'paid' if total_paid == claim['gross_amount'] else 'partial', claim['claim_id']))
            conn.execute('INSERT INTO product_settlements(user_id,product_code,kind,quantity,gross_amount,tax_amount,recorded_epoch) VALUES (?,?,?,?,?,?,?)',
                         (claim['user_id'],claim['asset_code'],f'preferred_arrears:{claim["claim_id"]}:{claim["quote_currency"]}',claim['quantity'],amount,tax,now))
            paid += amount
        return paid

    def _priority_blocked(self, conn, listing, now):
        if listing['table'] != 'company':
            return False
        issuer = self._issuer_code(listing)
        priority = int(listing['dividend_priority'])
        if conn.execute("SELECT 1 FROM preferred_dividend_claims c JOIN company s ON s.company_code=c.asset_code WHERE c.issuer_code=? AND c.status IN ('unpaid','partial') AND s.dividend_priority>? LIMIT 1", (issuer,priority)).fetchone():
            return True
        return bool(conn.execute("SELECT 1 FROM company WHERE status='active' AND COALESCE(parent_company_code,company_code)=? AND dividend_priority>? AND dividend_is_active=1 AND dividend_cycle_seconds>0 AND dividend_next_epoch<=? AND ((dividend_mode='fixed' AND dividend_per_share>0) OR (dividend_mode='percent' AND dividend_percent>0)) LIMIT 1", (issuer,priority,now)).fetchone())

    def _close_register(self, conn, listing, kind, key, pay_epoch, now, per_share=0, actor=None):
        """Snapshot the entitled holders once; later trades do not change the register."""
        row = conn.execute('SELECT register_id FROM shareholder_registers WHERE operation_key=?', (str(key),)).fetchone()
        if row:
            return row['register_id']
        register = conn.execute(
            'INSERT INTO shareholder_registers(operation_key,asset_code,dividend_kind,record_epoch,pay_epoch,'
            'per_share,actor_id,created_epoch) VALUES (?,?,?,?,?,?,?,?)',
            (str(key), listing['code'], kind, int(now), int(pay_epoch), int(per_share), actor, int(now))).lastrowid
        conn.executemany('INSERT INTO shareholder_register_entries(register_id,user_id,quantity) VALUES (?,?,?)',
                         [(register, user, quantity) for user, quantity in self._holders(conn, listing['code']) if quantity > 0])
        return register

    @staticmethod
    def _register_holders(conn, key):
        row = conn.execute('SELECT register_id FROM shareholder_registers WHERE operation_key=?', (str(key),)).fetchone()
        if not row:
            return None
        return [(str(entry['user_id']), float(entry['quantity'])) for entry in conn.execute(
            'SELECT user_id,quantity FROM shareholder_register_entries WHERE register_id=? ORDER BY user_id',
            (row['register_id'],))]

    def _distribute(self, conn, listing, per_share, kind, due, key, now, *, percent=None, reference_price=None):
        existing = conn.execute(
            'SELECT distribution_id,gross_amount FROM dividend_distributions WHERE operation_key=?',
            (str(key),),
        ).fetchone()
        if existing:
            return int(existing['gross_amount']), True
        if self._priority_blocked(conn, listing, now):
            raise ValueError('이 법인의 우선순위가 높은 주식에 미지급 배당이 있어 배당을 보류합니다.')
        # Pay whoever the closed register lists; close it now if it never was.
        holders = self._register_holders(conn, key)
        if holders is None:
            self._close_register(conn, listing, kind, key, now, now, per_share)
            holders = self._register_holders(conn, key)
        mode = 'percent' if percent is not None else 'fixed'
        unit_amount = Decimal(reference_price)*Decimal(str(percent))/100 if percent is not None else Decimal(per_share)
        payments = [(user,quantity,int((Decimal(str(quantity))*unit_amount).quantize(Decimal('1'),rounding=ROUND_HALF_UP))) for user,quantity in holders]
        owed = sum(amount for _,_,amount in payments)
        currency = listing.get('quote_currency') or 'NWX'
        rate = CurrencyExchangeManager._rate(conn, currency)
        if owed > self.MAX_MONEY or owed*rate > self.MAX_MONEY or owed*SCALE > MAX_UNITS:
            raise ValueError('총 배당금의 금액 또는 외환 평가액이 한도를 초과했습니다.')
        issuer, funding = self._issuer_code(listing), self._funding(listing)
        cumulative = listing['table']=='company' and listing['share_class']=='preferred' and bool(listing['cumulative_dividend'])
        gross = min(owed,self._source_balance(conn,issuer,funding,currency)) if cumulative else owed
        if gross < owed and not cumulative:
            raise ValueError('배당 재원이 부족합니다.')
        amounts = self._allocate(gross,[amount for _,_,amount in payments]) if owed and gross < owed else [amount for _,_,amount in payments]
        cursor = conn.execute('''INSERT INTO dividend_distributions
            (operation_key,dividend_kind,asset_code,due_epoch,per_share,dividend_mode,
             dividend_percent,reference_price,gross_amount,entitlement_amount,deferred_amount,
             quote_currency,funding_source,issuer_code,created_epoch)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (str(key),kind,listing['code'],int(due),int(per_share),mode,percent,reference_price,
             gross,owed,owed-gross,currency,funding,issuer,int(now)))
        distribution = cursor.lastrowid
        self._debit_dividend(conn,issuer,funding,currency,gross)
        for (user,quantity,entitlement),amount in zip(payments,amounts):
            tax,net_units = self._credit_dividend(conn,user,listing['code'],kind,amount,currency,now)
            conn.execute('''INSERT INTO dividend_payments
                (distribution_id,user_id,quantity,gross_amount,tax_amount,net_amount_units,
                 quote_currency,created_epoch) VALUES (?,?,?,?,?,?,?,?)''',
                (distribution,user,quantity,amount,tax,net_units,currency,int(now)))
            if cumulative and entitlement > amount:
                conn.execute('INSERT INTO preferred_dividend_claims(issuer_code,asset_code,distribution_id,user_id,quantity,gross_amount,quote_currency,funding_source,due_epoch,created_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (issuer,listing['code'],distribution,user,quantity,entitlement-amount,currency,funding,due,now))
            conn.execute('INSERT INTO product_settlements(user_id,product_code,kind,quantity,gross_amount,tax_amount,recorded_epoch) VALUES (?,?,?,?,?,?,?)',
                         (user,listing['code'],f'{kind}_dividend:{currency}',quantity,amount,tax,now))
        if cumulative and owed>gross and funding=='issuer':
            Finance.event(conn,issuer,'우선주 미지급배당 확정',
                          [('retained',owed-gross),('dividend_payable',gross-owed)],
                          now=now,currency=currency)
        conn.execute("UPDATE shareholder_registers SET status='paid' WHERE operation_key=?", (str(key),))
        self._record(conn,key,f'{kind}_dividend',listing['code'],None,
                     {'distribution_id':distribution,'per_share':per_share,'mode':mode,'percent':percent,'reference_price':reference_price,
                      'gross_amount':gross,'entitlement':owed,'deferred_amount':owed-gross,'currency':currency,'funding':funding,'issuer':issuer},now)
        return gross, False

    def configure_dividend(self, actor, code, cycle_seconds, per_share):
        try:
            cycle = self._integer(cycle_seconds, '배당주기(초)', 0, 3650 * 86400)
            terms = self.dividend_terms(per_share)
            now = int(time.time())
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                listing = self._dividend_listing(conn, code)
                listing.update(terms,dividend_cycle_seconds=cycle)
                next_epoch = now + cycle if listing['dividend_is_active'] and self._dividend_configured(listing) else None
                conn.execute(f'UPDATE {listing["table"]} SET dividend_cycle_seconds=?,dividend_mode=?,dividend_percent=?,dividend_per_share=?,dividend_next_epoch=? WHERE {listing["key"]}=?',
                             (cycle,terms['dividend_mode'],terms['dividend_percent'],terms['dividend_per_share'],next_epoch,str(code)))
                self._audit(conn, actor, 'dividend.configure', str(code), {'cycle_seconds':cycle,**terms,'next_epoch':next_epoch}, now)
            return True, f'✅ `{code}` 정기배당을 설정했습니다. 주기 {cycle:,}초 · {self._dividend_description(listing)}' + (' · 자동배당 중지' if not next_epoch else '')
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'

    def set_dividend_active(self, actor, code, is_active):
        try:
            active = self._integer(is_active, '정기배당 활성값', 0, 1)
            now = int(time.time())
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                listing = self._dividend_listing(conn, code)
                cycle = int(listing['dividend_cycle_seconds'])
                if active and not self._dividend_configured(listing):
                    raise ValueError('먼저 $정기배당수정으로 양의 배당주기와 주당 금액 또는 비율을 설정해주세요.')
                next_epoch = listing['dividend_next_epoch']
                if active != listing['dividend_is_active'] or (active and next_epoch is None):
                    next_epoch = now + cycle if active else None
                    conn.execute(f'UPDATE {listing["table"]} SET dividend_is_active=? WHERE {listing["key"]}=?', (active,str(code)))
                    conn.execute(f'UPDATE {listing["table"]} SET dividend_next_epoch=? WHERE {listing["key"]}=?', (next_epoch,str(code)))
                self._audit(conn, actor, 'dividend.activation', str(code), {'is_active':active,'next_epoch':next_epoch}, now)
            return True, f'✅ `{code}` 정기배당 {"켜짐" if active else "꺼짐"}. 주기 {cycle:,}초 · {self._dividend_description(listing)}'
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'

    def special_dividend(self, actor, code, per_share, operation_key=None, *, delay=None):
        """Close the shareholder register now and pay ``delay`` seconds later (10 minutes).

        ``delay=0`` pays in the same transaction.
        """
        delay = self.RECORD_LEAD if delay is None else int(delay)
        try:
            amount = self._integer(per_share, '주당 특별배당금', 1)
            now, key = int(time.time()), operation_key or 'special:' + uuid.uuid4().hex
            if delay > 0:
                with DBManager.admin_scope(actor), self.db.atomic() as conn:
                    self._require(conn, actor)
                    listing = self._dividend_listing(conn, code)
                    existing = conn.execute('SELECT pay_epoch FROM shareholder_registers WHERE operation_key=?', (key,)).fetchone()
                    pay_epoch = int(existing['pay_epoch']) if existing else now + delay
                    self._close_register(conn, listing, 'special', key, pay_epoch, now, amount, actor)
                    holders = len(self._register_holders(conn, key))
                    if not existing:
                        self._audit(conn, actor, 'dividend.special.register', str(code),
                                    {'per_share': amount, 'operation_key': key, 'pay_epoch': pay_epoch, 'holders': holders}, now)
                return True, (f'✅ `{code}` 특별배당 주주명부를 마감했습니다 (주주 {holders:,}명). '
                              f'<t:{pay_epoch}:T>에 주당 {amount:,} {listing.get("quote_currency") or "NWX"}를 장부대로 지급합니다.')
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                listing = self._dividend_listing(conn, code)
                if listing['table'] == 'company':
                    issuer = self._issuer_code(listing)
                    self._settle_claims(conn, issuer, now)
                    higher = conn.execute("SELECT company_code FROM company WHERE status='active' AND COALESCE(parent_company_code,company_code)=? AND dividend_priority>? AND dividend_is_active=1 AND dividend_next_epoch<=? ORDER BY dividend_priority DESC,company_code",
                                          (issuer, listing['dividend_priority'], now)).fetchall()
                    for preferred in higher:
                        self._regular_due(conn, preferred['company_code'], now)
                gross, duplicate = self._distribute(conn, listing, amount, 'special', now, key, now)
                if not duplicate:
                    self._audit(conn, actor, 'dividend.special', str(code), {'per_share': amount, 'gross_amount': gross, 'operation_key': key}, now)
            return True, f'✅ `{code}` 특별배당 {"지급 내역을 확인했습니다" if duplicate else "지급을 완료했습니다"}. 주당 {amount:,} · 총 {gross:,} {listing.get("quote_currency") or "NWX"}'
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'

    def _regular_due(self, conn, code, now):
        results = []
        for _ in range(20):
            listing = self._dividend_listing(conn,code)
            if not listing['dividend_is_active'] or not self._dividend_configured(listing):
                break
            due,cycle = listing['dividend_next_epoch'],int(listing['dividend_cycle_seconds'])
            if due is None:
                conn.execute(f'UPDATE {listing["table"]} SET dividend_next_epoch=? WHERE {listing["key"]}=?',(now+cycle,code))
                break
            if due - self.RECORD_LEAD <= now < due:
                self._close_register(conn, listing, 'regular', f'regular:{code}:{due}', due, now)
            if due > now:
                break
            percent = listing['dividend_percent'] if listing['dividend_mode']=='percent' else None
            price = int(listing[listing['price_key']]) if percent is not None else None
            if price is not None and price <= 0:
                raise ValueError('비율배당의 기준가격은 양수여야 합니다.')
            gross,duplicate = self._distribute(conn,listing,int(listing['dividend_per_share']) if percent is None else 0,
                                              'regular',due,f'regular:{code}:{due}',now,percent=percent,reference_price=price)
            conn.execute(f'UPDATE {listing["table"]} SET dividend_next_epoch=? WHERE {listing["key"]}=?',(due+cycle,code))
            if not duplicate:
                results.append((code,True,gross))
        return results

    def process_due(self, now=None):
        """Group an issuer's dividend decisions in one transaction, in priority order."""
        now = int(time.time() if now is None else now)
        groups = defaultdict(list)
        for database,table,key in ((self.db.companies,'company','company_code'),(self.db.products,'investment','product_code'),(self.db.assets,'market_assets','code')):
            for row in database.execute(f"SELECT * FROM {table} WHERE status='active' AND dividend_is_active=1 AND dividend_cycle_seconds>0 AND ((dividend_mode='fixed' AND dividend_per_share>0) OR (dividend_mode='percent' AND dividend_percent>0))"):
                issuer = str(row['parent_company_code'] or row[key]) if table=='company' else '@'+str(row[key])
                priority = int(row['dividend_priority']) if table=='company' else 0
                groups[issuer].append((priority,str(row[key])))
        # Claims remain payable even when the current dividend switch is off.
        for row in self.db.companies.execute("SELECT DISTINCT issuer_code FROM preferred_dividend_claims WHERE status IN ('unpaid','partial')"):
            groups.setdefault(str(row['issuer_code']),[])
        results = []
        for issuer,items in groups.items():
            pending = []
            try:
                with self.db.atomic() as conn:
                    if not issuer.startswith('@'):
                        self._settle_claims(conn,issuer,now)
                    for priority,code in sorted(items,key=lambda item:(-item[0],item[1])):
                        conn.execute('SAVEPOINT dividend_item')
                        try:
                            current = self._regular_due(conn,code,now)
                            conn.execute('RELEASE SAVEPOINT dividend_item')
                            pending.extend(current)
                        except (ValueError,OverflowError) as exc:
                            conn.execute('ROLLBACK TO SAVEPOINT dividend_item')
                            conn.execute('RELEASE SAVEPOINT dividend_item')
                            pending.append((code,False,str(exc)))
                results.extend(pending)
            except (ValueError,OverflowError) as exc:
                results.extend((code,False,str(exc)) for _,code in items)
        results.extend(self._pay_due_specials(now))
        return results

    def _pay_due_specials(self, now):
        results = []
        due = self.db.corporate.execute(
            "SELECT * FROM shareholder_registers WHERE dividend_kind='special' AND status='closed' AND pay_epoch<=? "
            "ORDER BY pay_epoch,register_id", (now,))
        for register in due:
            code, key = register['asset_code'], register['operation_key']
            try:
                with self.db.atomic() as conn:
                    listing = self._dividend_listing(conn, code)
                    if listing['table'] == 'company':
                        issuer = self._issuer_code(listing)
                        self._settle_claims(conn, issuer, now)
                        for preferred in conn.execute("SELECT company_code FROM company WHERE status='active' AND COALESCE(parent_company_code,company_code)=? AND dividend_priority>? AND dividend_is_active=1 AND dividend_next_epoch<=? ORDER BY dividend_priority DESC,company_code",
                                                      (issuer, listing['dividend_priority'], now)).fetchall():
                            self._regular_due(conn, preferred['company_code'], now)
                    gross, duplicate = self._distribute(conn, listing, int(register['per_share']), 'special',
                                                        int(register['pay_epoch']), key, now)
                    if not duplicate:
                        self._audit(conn, register['actor_id'] or 'system', 'dividend.special', str(code),
                                    {'per_share': int(register['per_share']), 'gross_amount': gross, 'operation_key': key}, now)
                results.append((code, True, gross))
            except (ValueError, OverflowError) as exc:
                with self.db.atomic() as conn:
                    conn.execute("UPDATE shareholder_registers SET status='failed',note=? WHERE register_id=?",
                                 (str(exc)[:500], register['register_id']))
                results.append((code, False, str(exc)))
        return results

    def fund_issuer(self, actor, code, operation, value):
        """Move spendable NWX between the treasury and a legal issuer's cash ledger."""
        try:
            value = self._integer(value,'법인 현금')
            if operation not in {'add','del','modify'}:
                raise ValueError('연산은 add / del / modify로 입력해주세요.')
            now = int(time.time())
            with DBManager.admin_scope(actor),self.db.atomic() as conn:
                self._require(conn,actor)
                listing = self._dividend_listing(conn,code)
                if listing['table'] != 'company':
                    raise ValueError('법인 현금은 회사 또는 우선주 코드로 조정해주세요.')
                issuer = self._issuer_code(listing)
                Finance.ensure(conn,issuer,now)
                conn.execute('INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)',(issuer,))
                old = int(conn.execute('SELECT balance FROM issuer_cash_accounts WHERE company_code=?',(issuer,)).fetchone()['balance'])
                new = old+value if operation=='add' else old-value if operation=='del' else value
                if not 0 <= new <= self.MAX_MONEY:
                    raise ValueError('사용 가능한 법인 현금의 한도를 확인해주세요. 잠금 예치금은 사용할 수 없습니다.')
                delta = new-old
                # Issuer cash (NWX) moves to/from the listing country's treasury.
                treasury = CurrencyManager.exchange_treasury(conn, issuer)
                if delta > 0:
                    CurrencyManager.treasury_pay(conn, treasury, 'NWX', delta*SCALE)
                elif delta < 0:
                    CurrencyManager.treasury_receive(conn, treasury, 'NWX', -delta*SCALE)
                conn.execute('UPDATE issuer_cash_accounts SET balance=? WHERE company_code=?',(new,issuer))
                Finance.event(conn,issuer,'국고와 법인 자본 이체',
                              [('cash',delta),('capital_surplus',-delta)],
                              'financing',now,currency='NWX')
                conn.execute('INSERT INTO issuer_cash_transfers(company_code,actor_id,old_balance,new_balance,delta,recorded_epoch) VALUES (?,?,?,?,?,?)',
                             (issuer,str(actor),old,new,delta,now))
                self._audit(conn,actor,'issuer.cash',issuer,{'operation':operation,'value':value,'old_balance':old,'new_balance':new},now)
            return True,f'✅ `{issuer}` 법인 사용가능 현금 {old:,} → {new:,} NWX. 국고와 {abs(delta):,} NWX 이체'
        except (ValueError,TypeError,OverflowError) as exc:
            return False,f'❌ {exc}'

    @staticmethod
    def _has_table(conn, table):
        return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())

    @classmethod
    def _els_encumbered(cls, conn, codes, issuer=None):
        if not cls._has_table(conn,'els_products'):
            return False
        placeholders = ','.join('?' for _ in codes)
        return bool(conn.execute(f"SELECT 1 FROM els_products t LEFT JOIN els_underlyings u ON u.product_code=t.product_code WHERE t.state IN ('subscription','active','observation_pending','redemption_pending') AND (t.issuer_code IN ({placeholders}) OR u.underlying_code IN ({placeholders})) LIMIT 1",tuple(codes)+tuple(codes)).fetchone())

    def liquidate(self, actor, code, operation_key=None, now=None):
        """One atomic waterfall: debt, fixed arrears, preference capital, residual equity."""
        try:
            now = int(time.time() if now is None else now)
            key = operation_key or 'liquidation:'+uuid.uuid4().hex
            with DBManager.admin_scope(actor),self.db.atomic() as conn:
                self._require(conn,actor)
                previous = conn.execute(
                    "SELECT action_kind,source_code FROM corporate_action_records WHERE operation_key=?",
                    (key,),
                ).fetchone()
                if previous:
                    if previous['action_kind']!='liquidation' or previous['source_code']!=str(code):
                        raise ValueError('다른 기업행동에 이미 사용한 실행 식별자입니다.')
                    return True,f'✅ `{code}` 청산 지급 기록을 확인했습니다. 중복 지급하지 않습니다.'
                listing = self._dividend_listing(conn,code)
                if listing['table']!='company':
                    raise ValueError('청산할 회사 주식 또는 우선주 코드를 입력해주세요.')
                issuer = self._issuer_code(listing)
                issuer_row = conn.execute("SELECT * FROM company WHERE company_code=? AND share_class='common' AND status='active'",(issuer,)).fetchone()
                if not issuer_row:
                    raise ValueError('거래 중인 발행법인을 찾지 못했습니다.')
                shares = conn.execute("SELECT * FROM company WHERE status='active' AND (company_code=? OR parent_company_code=?) ORDER BY dividend_priority DESC,company_code",(issuer,issuer)).fetchall()
                codes = [str(row['company_code']) for row in shares]
                placeholders = ','.join('?' for _ in codes)
                if any((row['quote_currency'] or 'NWX')!='NWX' for row in shares):
                    raise ValueError('법인 현금 청산은 NWX 주식에만 사용할 수 있습니다.')
                if conn.execute(f"SELECT 1 FROM option_contracts WHERE status='active' AND underlying_code IN ({placeholders}) LIMIT 1",tuple(codes)).fetchone():
                    raise ValueError('활성 옵션 계약과 담보를 먼저 정산해주세요.')
                future_codes = [r['product_code'] for r in conn.execute(f"SELECT product_code FROM investment WHERE product_type='stock_future' AND underlying_code IN ({placeholders})",tuple(codes)).fetchall()]
                if future_codes and conn.execute(f"SELECT 1 FROM futures_positions WHERE product_code IN ({','.join('?' for _ in future_codes)}) LIMIT 1",tuple(future_codes)).fetchone():
                    raise ValueError('연결된 선물 포지션을 먼저 정산해주세요.')
                if self._els_encumbered(conn,codes,issuer):
                    raise ValueError('발행하거나 기초자산으로 연결된 ELS를 먼저 정산·취소해주세요.')
                from managers.els_manager import ELSManager
                if ELSManager.index_removal_blocked(conn,codes):
                    raise ValueError('미상환 ELS의 기초지수를 구성하는 마지막 주식은 청산할 수 없습니다.')
                Finance.ensure(conn,issuer,now)
                cash = conn.execute('SELECT * FROM issuer_cash_accounts WHERE company_code=?',(issuer,)).fetchone()
                if cash and int(cash['locked_amount']):
                    raise ValueError('법인의 잠금 예치금을 먼저 정산해주세요.')
                balance = int(cash['balance']) if cash else 0
                initial = balance
                bonds = conn.execute(f"SELECT * FROM investment WHERE product_type IN ('corporate_bond','etn') AND status IN ('active','defaulted') AND issuer_code IN ({placeholders})",tuple(codes)).fetchall()
                if any((bond['quote_currency'] or 'NWX')!='NWX' for bond in bonds):
                    raise ValueError('외화 회사채를 먼저 정산해주세요.')
                debt_codes = [str(row['product_code']) for row in bonds]
                cleanup_codes = codes+debt_codes+future_codes
                for asset in cleanup_codes:
                    orders = conn.execute("SELECT * FROM market_orders WHERE asset_code=? AND status IN ('open','partial')",(asset,)).fetchall()
                    from managers.order_book_manager import OrderBookManager
                    for order in orders:
                        if order['side']=='buy' and int(order['reserved_cash'] or 0):
                            OrderBookManager._credit_reserved_source(conn,order['user_id'],order['funding_account'],int(order['reserved_cash']),now)
                        conn.execute("UPDATE market_orders SET status='cancelled',reserved_cash=0,updated_epoch=? WHERE order_id=?",(now,order['order_id']))
                waterfall = []
                debt = []
                secured = []  # asset-backed paper is repaid from its pool before other debt
                for bond in bonds:
                    if bond['product_type'] == 'etn':
                        # ETN holders are unsecured creditors for the note's current value.
                        for holder in conn.execute('SELECT user_id,SUM(quantity) quantity FROM user_assets WHERE asset_code=? GROUP BY user_id ORDER BY user_id',(bond['product_code'],)).fetchall():
                            value = int(Decimal(str(holder['quantity']))*int(bond['unit_price']))
                            debt.append({'asset':bond['product_code'],'user':holder['user_id'],'quantity':holder['quantity'],'claim':value,'principal':value,'kind':'debt'})
                        continue
                    target = secured if (bond['bond_feature'] or 'plain') in {'abs','mbs','abcp'} else debt
                    maturity = int(bond['maturity_epoch'] or now)
                    duration = max(0,int(bond['maturity_days'] or 0))*86400
                    elapsed = max(0,min(duration,now-(maturity-duration)))
                    for holder in conn.execute('SELECT user_id,SUM(quantity) quantity FROM user_assets WHERE asset_code=? GROUP BY user_id ORDER BY user_id',(bond['product_code'],)).fetchall():
                        quantity = int(holder['quantity'])
                        principal = quantity*int(bond['face_value'])
                        coupon = int((Decimal(principal)*Decimal(str(bond['annual_rate']))/100*Decimal(elapsed)/Decimal(365*86400)).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
                        target.append({'asset':bond['product_code'],'user':holder['user_id'],'quantity':quantity,'claim':principal+coupon,'principal':principal,'kind':'debt'})
                claims = []
                for claim in conn.execute("SELECT * FROM preferred_dividend_claims WHERE issuer_code=? AND status IN ('unpaid','partial') ORDER BY claim_id",(issuer,)).fetchall():
                    if claim['quote_currency']!='NWX':
                        raise ValueError('외화 미지급 배당채권을 먼저 정산해주세요.')
                    claims.append({'asset':claim['asset_code'],'user':claim['user_id'],'quantity':int(claim['quantity']),
                                   'claim':int(claim['gross_amount'])-int(claim['paid_amount']),'kind':'arrears','claim_id':int(claim['claim_id'])})
                equity = defaultdict(list)
                common = []
                for stock in shares:
                    for holder in conn.execute('SELECT user_id,SUM(quantity) quantity FROM user_assets WHERE asset_code=? GROUP BY user_id ORDER BY user_id',(stock['company_code'],)).fetchall():
                        record = {'asset':stock['company_code'],'user':holder['user_id'],'quantity':float(holder['quantity']),'kind':'preferred' if stock['share_class']=='preferred' else 'common'}
                        if stock['share_class']=='preferred':
                            record['claim'] = int((Decimal(str(record['quantity']))*Decimal(str(stock['liquidation_value_per_share']))).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                            equity[int(stock['dividend_priority'])].append(record)
                        else:
                            record['claim'] = record['quantity']
                            common.append(record)
                tiers = [secured,debt,claims]+[equity[priority] for priority in sorted(equity,reverse=True)]
                for tier in tiers:
                    total = sum(item['claim'] for item in tier)
                    payout = min(balance,total)
                    allocations = self._allocate(payout,[item['claim'] for item in tier]) if total else [0]*len(tier)
                    for item,amount in zip(tier,allocations):
                        waterfall.append((item,amount))
                    balance -= payout
                if common:
                    allocations = self._allocate(balance,[item['quantity'] for item in common])
                    waterfall.extend(zip(common,allocations))
                    balance = 0
                # No outside shareholder leaves cash to the treasury as owner of
                # the unissued residual; preference caps never become residual claims.
                if balance:
                    waterfall.append(({'asset':issuer,'user':CurrencyManager.exchange_treasury(conn, str(code)),'quantity':0,'claim':balance,'kind':'unowned_residual'},balance))
                    balance = 0
                if initial > self.MAX_MONEY or any(item['claim']>self.MAX_MONEY for item,_ in waterfall):
                    raise ValueError('청산 채권 또는 지급액이 금액 한도를 초과했습니다.')
                for item,amount in waterfall:
                    taxable = amount if item['kind']=='arrears' else max(0,amount-item.get('principal',amount)) if item['kind']=='debt' else 0
                    tax = UserManager._tax(conn,item['user'],f'liquidation:{item["kind"]}:{issuer}',taxable)
                    net = amount-tax
                    if item['kind']=='unowned_residual':
                        # The listing country's treasury keeps the residual (in NWX or exchanged).
                        CurrencyManager.treasury_receive(conn, item['user'], 'NWX', net * SCALE)
                    elif not UserManager._change_cash(conn, item['user'], net, maximum=self.MAX_MONEY):
                        raise ValueError('청산금을 받을 사용자 또는 잔액 한도를 확인해주세요.')
                    conn.execute('INSERT INTO corporate_liquidation_payments(operation_key,issuer_code,asset_code,user_id,kind,quantity,claim_amount,paid_amount,tax_amount,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)',
                                 (key,issuer,item['asset'],item['user'],item['kind'],item['quantity'],item['claim'],amount,tax,now))
                    conn.execute('INSERT INTO product_settlements(user_id,product_code,kind,quantity,gross_amount,tax_amount,recorded_epoch) VALUES (?,?,?,?,?,?,?)',
                                 (item['user'],item['asset'],'liquidation:'+item['kind'],item['quantity'],amount,tax,now))
                    if item['kind']=='arrears':
                        conn.execute("UPDATE preferred_dividend_claims SET paid_amount=paid_amount+?,status=CASE WHEN paid_amount+?=gross_amount THEN 'paid' ELSE 'written_off' END WHERE claim_id=?",(amount,amount,item['claim_id']))
                conn.execute('INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)',(issuer,))
                conn.execute('UPDATE issuer_cash_accounts SET balance=0 WHERE company_code=?',(issuer,))
                for asset in cleanup_codes:
                    conn.execute('DELETE FROM user_assets WHERE asset_code=?',(asset,))
                    self._reset_market_state(conn,asset)
                conn.execute(f"UPDATE company SET status='inactive',max_shareholder_id=NULL,dividend_next_epoch=NULL WHERE company_code IN ({placeholders})",tuple(codes))
                conn.execute(f'DELETE FROM market_exchange_index_components WHERE company_code IN ({placeholders})',tuple(codes))
                for asset in debt_codes+future_codes:
                    conn.execute("UPDATE investment SET status='liquidated',dividend_next_epoch=NULL WHERE product_code=?",(asset,))
                payload = {'issuer':issuer,'cash_available':initial,'codes':codes,'debt_codes':debt_codes,'payouts':len(waterfall),'cash_ledger_existed':bool(cash),
                           'rule':'debt_accrued_interest,arrears,preferred_caps,common_residual; capital_not_cash'}
                Finance.liquidate(conn,issuer,now)
                self._record(conn,key,'liquidation',str(code),None,payload,now)
                self._audit(conn,actor,'liquidation',issuer,payload,now)
            return True,f'✅ `{issuer}` 법인과 연결 우선주를 청산했습니다. 현금 {initial:,} NWX · 회사채 → 미지급 배당 → 우선주 한도 → 보통주 순서로 지급'
        except (ValueError,TypeError,OverflowError) as exc:
            return False,f'❌ {exc}'

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
        company = conn.execute('SELECT dividend_funding,parent_company_code FROM company WHERE company_code=?', (offering['asset_code'],)).fetchone()
        if company and company['dividend_funding'] == 'issuer':
            issuer = company['parent_company_code'] or offering['asset_code']
            proceeds = quantity * price
            # The listing treasury received the sale; it forwards it to the issuer's cash.
            CurrencyManager.treasury_pay(conn, CurrencyManager.exchange_treasury(conn, issuer), 'NWX', proceeds*SCALE)
            conn.execute('INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)', (issuer,))
            funded = conn.execute('UPDATE issuer_cash_accounts SET balance=balance+? WHERE company_code=? AND balance<=?', (proceeds, issuer, CorporateActionsManager.MAX_MONEY-proceeds))
            if funded.rowcount != 1:
                raise ValueError('신주 발행 법인의 현금 한도를 초과합니다.')
        conn.execute('UPDATE corporate_equity_offerings SET sold_quantity=sold_quantity+? WHERE offering_id=?', (quantity, offering['offering_id']))

    @staticmethod
    def _unencumbered(conn, codes, allow_orders=False):
        if CorporateActionsManager._els_encumbered(conn, codes):
            raise ValueError('발행하거나 기초자산으로 연결된 ELS를 먼저 정산·취소해주세요.')
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

    def _reset_market_state(self, conn, code, price=None, shares=None):
        for table in ('market_simulation_state', 'market_quote_overrides'):
            # Earlier installations use market_auto_target_state rather than a targets table.
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                conn.execute(f'DELETE FROM {table} WHERE asset_code=?', (code,))
        for table in ('market_auto_target_state',):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                conn.execute(f'DELETE FROM {table} WHERE asset_code=?', (code,))
        from managers.market_manager import MarketManager
        MarketManager(self.bot.db).clear_runtime_targets(code, conn=conn)
        if price is not None and shares is not None:
            conn.execute('UPDATE market_exchange_index_components SET previous_price=?,total_shares=? WHERE company_code=?', (price, shares, code))

    @staticmethod
    def _allocate(total, weights):
        total = int(total)
        weights = [Fraction(str(weight)) for weight in weights]
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
                    error = MarketSessionManager(self.db).trading_error(conn=conn, now=now, asset_code=code)
                    if error:
                        raise ValueError(error)
                    issue = self._integer(price if issue_price is None else issue_price, '유상 발행가', 1)
                    if quantity * issue > self.MAX_MONEY:
                        raise ValueError('유상증자 모집 금액이 한도를 초과합니다.')
                    # The listing country's treasury holds and sells the new shares.
                    seller = CurrencyManager.exchange_treasury(conn, code)
                    conn.execute("INSERT INTO user_assets(user_id,asset_code,quantity,average_cost) VALUES (?,?,?,?) ON CONFLICT(user_id,asset_code,IFNULL(account_number,'')) DO UPDATE SET average_cost=(quantity*average_cost+excluded.quantity*excluded.average_cost)/(quantity+excluded.quantity),quantity=quantity+excluded.quantity",
                                 (seller, code, quantity, issue))
                    order = conn.execute("INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,remaining_quantity,order_kind,status,created_epoch,updated_epoch,quote_currency) VALUES (?,?,'sell',?,?,?,'asset','open',?,?,?)",
                                         (seller, code, issue, quantity, quantity, now, now, currency)).lastrowid
                    conn.execute('INSERT INTO corporate_equity_offerings(asset_code,order_id,quantity,issue_price,recorded_epoch) VALUES (?,?,?,?,?)', (code, order, quantity, issue, now))
                    conn.execute('UPDATE company SET total_shares=total_shares+? WHERE company_code=?', (quantity, code))
                    if hasattr(self.bot, 'order_book'):
                        if currency == CurrencyManager.home(conn, seller):
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
                    owned = sum(float(lot['quantity']) for lot in lots)
                    if owned > supply:
                        raise ValueError('보유 수량이 총 발행량을 초과합니다.')
                    weights = [float(lot['quantity']) for lot in lots] + [supply - owned]
                    scale = 1000 if listing.get('Fractal') else 1
                    allocations = [value / scale for value in self._allocate(quantity*scale, weights)]
                    for lot, new in zip(lots, allocations):
                        if new:
                            conn.execute('UPDATE user_assets SET quantity=quantity+?,average_cost=? WHERE holding_id=?',
                                         (new, round(float(lot['quantity']) * float(lot['average_cost']) / (float(lot['quantity']) + new)), lot['holding_id']))
                    if allocations[-1]:
                        conn.execute("INSERT INTO user_assets(user_id,asset_code,quantity,average_cost) VALUES (?,?,?,0) ON CONFLICT(user_id,asset_code,IFNULL(account_number,'')) DO UPDATE SET average_cost=quantity*average_cost/(quantity+excluded.quantity),quantity=quantity+excluded.quantity",
                                     (CurrencyManager.exchange_treasury(conn, code), code, allocations[-1]))
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

    def change_capital(self, actor, direction, mode, code, quantity, issue_price=None):
        """Unified capital-change entry point; reductions use a reverse split."""
        direction, mode = str(direction).strip(), str(mode).strip()
        if direction == '증자':
            return self.increase_capital(actor, mode, code, quantity, issue_price)
        if direction != '감자' or mode not in {'유상', '무상'}:
            return False, '❌ 자본변경은 증자/감자와 유상/무상으로 입력해주세요.'
        if issue_price is not None:
            return False, '❌ 감자는 현재 액면 병합 방식이며 가격 인자를 받지 않습니다.'
        return self.adjust_face_value(actor, '병합', code, quantity)

    def adjust_face_value(self, actor, action, code, ratio):
        """Apply an exact stock split or consolidation to every unit-based state."""
        try:
            action = str(action).strip()
            if action not in {'분할', '병합'} or int(ratio) < 2:
                raise ValueError('분할/병합과 2 이상의 정수 비율을 입력해주세요.')
            now, ratio = int(time.time()), int(ratio)
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                listing = self._listing(conn, code)
                if listing['table'] != 'company' or listing['status'] != 'active':
                    raise ValueError('활성 주식 종목만 액면분할·병합할 수 있습니다.')
                dr_program = conn.execute(
                    "SELECT dr_code FROM depositary_receipts WHERE underlying_code=? "
                    "AND program_status<>'terminated' LIMIT 1", (code,),
                ).fetchone()
                if dr_program:
                    raise ValueError('기초주식에 미종료 DR 프로그램이 있습니다. 먼저 DR을 상환하고 프로그램을 종료해주세요.')
                self._unencumbered(conn, [code], allow_orders=True)
                price, shares = int(listing['share_price']), int(listing['total_shares'])
                if action == '분할':
                    new_price, new_shares = max(1, price // ratio), shares * ratio
                    for lot in conn.execute('SELECT holding_id,quantity,average_cost FROM user_assets WHERE asset_code=?', (code,)):
                        conn.execute('UPDATE user_assets SET quantity=?,average_cost=? WHERE holding_id=?',
                                     (int(lot['quantity']) * ratio, max(1, round(int(lot['average_cost']) / ratio)), lot['holding_id']))
                    conn.execute("UPDATE market_orders SET quantity=quantity*?,remaining_quantity=remaining_quantity*?,filled_quantity=filled_quantity*?,limit_price=MAX(1,limit_price/?),reserved_cash=remaining_quantity*MAX(1,limit_price/?) WHERE asset_code=? AND status IN ('scheduled','open','partial')",
                                 (ratio, ratio, ratio, ratio, ratio, code))
                else:
                    if shares % ratio or any(int(row['quantity']) % ratio for row in conn.execute('SELECT quantity FROM user_assets WHERE asset_code=?', (code,))):
                        raise ValueError('병합 비율로 나누어떨어지는 발행·보유 수량만 지원합니다.')
                    new_price, new_shares = price * ratio, shares // ratio
                    for lot in conn.execute('SELECT holding_id,quantity,average_cost FROM user_assets WHERE asset_code=?', (code,)):
                        conn.execute('UPDATE user_assets SET quantity=?,average_cost=? WHERE holding_id=?',
                                     (int(lot['quantity']) // ratio, int(lot['average_cost']) * ratio, lot['holding_id']))
                    conn.execute("UPDATE market_orders SET quantity=quantity/?,remaining_quantity=remaining_quantity/?,filled_quantity=filled_quantity/?,limit_price=limit_price*?,reserved_cash=remaining_quantity*limit_price WHERE asset_code=? AND status IN ('scheduled','open','partial')",
                                 (ratio, ratio, ratio, ratio, code))
                conn.execute('UPDATE company SET share_price=?,total_shares=? WHERE company_code=?', (new_price, new_shares, code))
                conn.execute('UPDATE fund_components SET base_price=? WHERE asset_code=?', (new_price, code))
                self._reset_market_state(conn, code, new_price, new_shares)
                self._record(conn, f'face:{uuid.uuid4().hex}', 'face_value_adjustment', code, None,
                             {'action': action, 'ratio': ratio, 'old_price': price, 'new_price': new_price}, now)
            return True, f'✅ `{code}` {ratio}:1 액면{action} 완료 · 가격 {price:,} → {new_price:,} · 발행주식수 {shares:,} → {new_shares:,}'
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'

    def merge(self, actor, surviving_code, absorbed_code, ratio_text='1'):
        """Exchange equal-value units, retaining B's history and deactivating B."""
        try:
            if surviving_code == absorbed_code:
                raise ValueError('서로 다른 두 코드를 입력해주세요.')
            ratio = self._integer(ratio_text, 'A:B 교환비율', 1)
            now = int(time.time())
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                try:
                    a, b = self._listing(conn, surviving_code), self._listing(conn, absorbed_code)
                except ValueError:
                    return self._merge_nonmarket(conn, actor, surviving_code, absorbed_code, now)
                if a['table'] != b['table'] or a['type'] != b['type'] or a['status'] != 'active' or b['status'] != 'active':
                    raise ValueError('거래 중인 같은 종류의 상품끼리 통합할 수 있습니다.')
                if a['type'] == 'depositary_receipt' or b['type'] == 'depositary_receipt':
                    raise ValueError('DR 프로그램 간 합병은 상환·예탁 비율 승계 규칙이 없어 지원하지 않습니다.')
                if a['table'] == 'company' and conn.execute(
                        "SELECT 1 FROM depositary_receipts WHERE underlying_code IN (?,?) "
                        "AND program_status<>'terminated' LIMIT 1", (surviving_code, absorbed_code),
                ).fetchone():
                    raise ValueError('기초주식에 미종료 DR 프로그램이 있습니다. 먼저 DR을 상환하고 프로그램을 종료해주세요.')
                if a['type'] == 'forex' or conn.execute('SELECT 1 FROM currencies WHERE asset_code IN (?,?) LIMIT 1', (surviving_code, absorbed_code)).fetchone():
                    raise ValueError('환율 종목은 통합할 수 없습니다. 환율수정·환전으로 통화를 관리해주세요.')
                if a['table'] == 'company' and a['share_class'] != b['share_class']:
                    raise ValueError('보통주와 우선주는 같은 종류끼리 합병해주세요.')
                if a['table'] == 'company' and a['share_class'] == 'preferred' and a['parent_company_code'] != b['parent_company_code']:
                    raise ValueError('같은 발행 법인의 우선주끼리 합병해주세요.')
                if a['table'] == 'company' and a['share_class'] == 'preferred' and any(a[field] != b[field] for field in ('dividend_priority','cumulative_dividend','liquidation_value_per_share','dividend_funding')):
                    raise ValueError('배당·청산 우선 조건이 같은 우선주끼리 합병해주세요.')
                from managers.els_manager import ELSManager
                if ELSManager.index_removal_blocked(conn,[absorbed_code]):
                    raise ValueError('미상환 ELS의 기초지수를 구성하는 마지막 주식은 합병할 수 없습니다.')
                currency = a.get('quote_currency') or 'NWX'
                if currency != (b.get('quote_currency') or 'NWX'):
                    raise ValueError('결제 통화가 같은 상품끼리 통합할 수 있습니다.')
                self._unencumbered(conn, [surviving_code, absorbed_code])
                if a['table']=='company' and a['share_class']=='common':
                    Finance.ensure(conn,surviving_code,now)
                    Finance.ensure(conn,absorbed_code,now)
                if a['type'] in {'stock_future', 'corporate_bond'} and conn.execute('SELECT 1 FROM user_assets WHERE asset_code IN (?,?) LIMIT 1', (surviving_code, absorbed_code)).fetchone():
                    raise ValueError('채권·선물의 미상환 보유분을 먼저 정산해주세요.')
                price_a, price_b = int(a[a['price_key']]), int(b[b['price_key']])
                if a['table']=='company' and a['share_class']=='preferred' and a['liquidation_value_per_share']>0 and price_a!=price_b:
                    raise ValueError('청산 한도가 있는 우선주는 교환 후 권리액 보존을 위해 가격이 같은 종목끼리만 합병할 수 있습니다.')
                supply_a, supply_b = int(a[a['supply_key']] or 0), int(b[b['supply_key']] or 0)
                if min(price_a, price_b, supply_a, supply_b) <= 0:
                    raise ValueError('가격과 발행량이 양수인 상품만 통합할 수 있습니다.')
                lots = conn.execute('SELECT * FROM user_assets WHERE asset_code=? ORDER BY holding_id', (absorbed_code,)).fetchall()
                groups = defaultdict(list)
                for lot in lots:
                    groups[str(lot['user_id'])].append(lot)
                conversions, total_new, cash = [], 0, 0
                for user, user_lots in groups.items():
                    quantity = sum(Fraction(str(lot['quantity'])) for lot in user_lots)
                    entitlement = quantity * ratio
                    units = int(entitlement)
                    residual = round((entitlement-units)*price_a)
                    allocation = self._allocate(units, [lot['quantity'] for lot in user_lots]) if units else [0] * len(user_lots)
                    cash += residual if not CurrencyManager.is_treasury(conn, user) else 0
                    total_new += units
                    conversions.append((user, user_lots, allocation, residual))
                unissued = max(0, supply_b - sum(Fraction(str(lot['quantity'])) for lot in lots))
                new_supply = supply_a + total_new + int(unissued * ratio)
                if new_supply > self.MAX_MONEY or cash > self.MAX_MONEY:
                    raise ValueError('통합 후 발행량 또는 단주 정산금이 한도를 초과합니다.')
                # The listing country's treasury pays odd-lot cash in the instrument currency.
                CurrencyManager.treasury_pay(conn, CurrencyManager.exchange_treasury(conn, surviving_code), currency, cash * SCALE)
                for user, user_lots, allocation, residual in conversions:
                    for lot, units in zip(user_lots, allocation):
                        if units:
                            cost = max(1, round(Fraction(int(lot['average_cost']), ratio)))
                            conn.execute("INSERT INTO user_assets(user_id,asset_code,account_number,quantity,average_cost) VALUES (?,?,?,?,?) ON CONFLICT(user_id,asset_code,IFNULL(account_number,'')) DO UPDATE SET average_cost=(quantity*average_cost+excluded.quantity*excluded.average_cost)/(quantity+excluded.quantity),quantity=quantity+excluded.quantity",
                                         (user, surviving_code, lot['account_number'], units, cost))
                    if residual and not CurrencyManager.is_treasury(conn, user):
                        if currency == CurrencyManager.home(conn, user):
                            CurrencyManager.change(conn, user, currency, residual * SCALE)
                        else:
                            CurrencyExchangeManager._settle(conn, user, now)
                            account = self._currency_account(conn, user, currency, now)
                            changed = conn.execute("UPDATE accounts SET balance_units=balance_units+? WHERE account_number=? AND account_type='currency_saving' AND balance_units<=?", (residual * SCALE, account, MAX_UNITS - residual * SCALE))
                        if currency != CurrencyManager.home(conn, user) and changed.rowcount != 1:
                            raise ValueError('단주 정산 수령자의 잔액 한도를 확인해주세요.')
                conn.execute('DELETE FROM user_assets WHERE asset_code=?', (absorbed_code,))
                if a['table'] == 'company':
                    capital = int(a['capital'] or 0) + int(b['capital'] or 0)
                    if capital > self.MAX_MONEY:
                        raise ValueError('통합 법인의 자본금 한도를 초과합니다.')
                    conn.execute('UPDATE company SET total_shares=?,capital=? WHERE company_code=?', (new_supply, capital, surviving_code))
                    conn.execute('UPDATE company SET parent_company_code=? WHERE parent_company_code=?', (surviving_code, absorbed_code))
                    if a['share_class'] == 'common':
                        accounts = conn.execute('SELECT * FROM issuer_cash_accounts WHERE company_code IN (?,?)', (surviving_code, absorbed_code)).fetchall()
                        if any(int(account['locked_amount']) for account in accounts):
                            raise ValueError('법인의 잠금 예치금을 먼저 정산해주세요.')
                        if accounts:
                            balance = sum(int(account['balance']) for account in accounts)
                            if balance > self.MAX_MONEY:
                                raise ValueError('합병 법인의 현금 한도를 초과합니다.')
                            conn.execute('INSERT OR IGNORE INTO issuer_cash_accounts(company_code) VALUES (?)', (surviving_code,))
                            conn.execute('UPDATE issuer_cash_accounts SET balance=? WHERE company_code=?', (balance, surviving_code))
                            conn.execute('DELETE FROM issuer_cash_accounts WHERE company_code=?', (absorbed_code,))
                        conn.execute('UPDATE preferred_dividend_claims SET issuer_code=? WHERE issuer_code=?', (surviving_code, absorbed_code))
                    UserManager._refresh_max_shareholder(conn, surviving_code)
                else:
                    conn.execute(f'UPDATE {a["table"]} SET {a["supply_key"]}=? WHERE {a["key"]}=?', (new_supply, surviving_code))
                conn.execute(f"UPDATE {b['table']} SET status='inactive',dividend_next_epoch=NULL WHERE {b['key']}=?", (absorbed_code,))
                self._merge_components(conn, surviving_code, absorbed_code, price_a, price_b)
                self._reset_market_state(conn, surviving_code, price_a, new_supply)
                self._reset_market_state(conn, absorbed_code)
                conn.execute('DELETE FROM market_exchange_index_components WHERE company_code=?', (absorbed_code,))
                payload = {'survivor': surviving_code, 'absorbed': absorbed_code, 'exchange_ratio': f'{ratio}:1', 'cash_settled': cash, 'new_supply': new_supply, 'currency': currency,
                           'positions': [{'user_id': user, 'cash_residual': residual, 'lots': [{'account_number': lot['account_number'], 'source_quantity': int(lot['quantity']), 'target_quantity': units, 'source_average_cost': int(lot['average_cost'])} for lot, units in zip(user_lots, allocation)]} for user, user_lots, allocation, residual in conversions]}
                if a['table']=='company' and a['share_class']=='common':
                    Finance.merge(conn,surviving_code,absorbed_code,now)
                self._record(conn, 'merger:' + uuid.uuid4().hex, 'merger', absorbed_code, surviving_code, payload, now)
                self._audit(conn, actor, 'merger', surviving_code, payload, now)
            return True, f'✅ `{absorbed_code}`를 `{surviving_code}`에 통합했습니다. 교환비율 {ratio}:1 · 단주 정산 {cash:,} {currency} · 존속 `{surviving_code}`'
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
        for table in ('saving', 'liability', 'treasury_bond_products'):
            first = conn.execute(f'SELECT * FROM {table} WHERE product_code=?', (a,)).fetchone()
            second = conn.execute(f'SELECT * FROM {table} WHERE product_code=?', (b,)).fetchone()
            if not first or not second:
                continue
            if first['status'] != 'active' or second['status'] != 'active':
                raise ValueError('활성 상품 두 개를 입력해주세요.')
            self._unencumbered(conn, [a, b])
            if table == 'saving':
                first_is_currency = first['product_kind'] == 'currency_saving'
                second_is_currency = second['product_kind'] == 'currency_saving'
                if first_is_currency != second_is_currency:
                    continue
                if first_is_currency:
                    if first['quote_currency'] != second['quote_currency']:
                        raise ValueError('같은 통화의 외환통장 상품끼리 통합할 수 있습니다.')
                    for user in conn.execute("SELECT user_id FROM accounts WHERE account_type='currency_saving' AND product_code=?", (b,)).fetchall():
                        CurrencyExchangeManager._settle(conn, user['user_id'], now)
                    conn.execute("UPDATE accounts SET product_code=? WHERE account_type='currency_saving' AND product_code=?", (a, b))
                elif conn.execute('SELECT 1 FROM accounts WHERE product_code=? LIMIT 1', (b,)).fetchone():
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
            conn.execute(f"UPDATE {table} SET status='inactive' WHERE product_code=?", (b,))
            payload = {'survivor': a, 'absorbed': b, 'table': table}
            self._record(conn, 'merger:' + uuid.uuid4().hex, 'product_merger', b, a, payload, now)
            self._audit(conn, actor, 'merger', a, payload, now)
            return True, f'✅ `{b}` 상품을 `{a}` 상품에 통합했습니다. 존속 `{a}`'
        raise ValueError('같은 종류의 회사·투자·금융 상품 코드를 입력해주세요.')
