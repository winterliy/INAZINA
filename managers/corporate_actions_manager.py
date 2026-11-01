"""Atomic dividends, equity issuance and value-preserving product combinations."""
from functions.exact_money import ExactDecimal
from functions.amount_limits import MAX_NATIVE
from functions.money import base_currency_code, currency_precision, round_money, to_minor_units, from_minor_units
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
from managers.rate_manager import RateManager


class CorporateActionsManager:
    MAX_MONEY = MAX_NATIVE
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
                'dividend_per_share':cls._integer(value, '주당 배당금', maximum=MAX_NATIVE)}

    @staticmethod
    def _dividend_configured(listing):
        amount = listing['dividend_percent'] if listing['dividend_mode'] == 'percent' else listing['dividend_per_share']
        return listing['dividend_cycle_seconds'] > 0 and amount > 0

    @staticmethod
    def _dividend_description(listing):
        if listing['dividend_mode'] == 'percent':
            return f'가격 기준 {listing["dividend_percent"]:g}%'
        return f'주당 {listing["dividend_per_share"]:,} {listing.get("quote_currency") or "—"}'

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
             json.dumps(payload,ensure_ascii=False,sort_keys=True,default=str),int(now)),
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
        underlying_currency = conn.execute(
            "SELECT COALESCE((SELECT quote_currency FROM company WHERE company_code=?),"
            "(SELECT quote_currency FROM market_assets WHERE code=?),base_currency_code())",
            (str(code), str(code)),
        ).fetchone()[0]
        rows = conn.execute('SELECT user_id,SUM(quantity) quantity FROM ('
            'SELECT user_id,quantity FROM user_assets WHERE asset_code=? UNION ALL '
            'SELECT writer_id user_id,quantity FROM option_collateral_lots WHERE underlying_code=?'
            ') GROUP BY user_id ORDER BY user_id', (code, code)).fetchall()
        dr_rows = conn.execute(
            "SELECT h.user_id,d.quote_currency payout_currency,"
            "SUM(h.quantity*d.underlying_per_receipt) quantity "
            "FROM depositary_receipts d JOIN user_assets h ON h.asset_code=d.dr_code "
            "WHERE d.underlying_code=? AND d.program_status<>'terminated' "
            "GROUP BY h.user_id,d.quote_currency ORDER BY h.user_id,d.quote_currency",
            (str(code),),
        ).fetchall()
        combined = {(str(row['user_id']), str(underlying_currency)):
                    float(row['quantity'] or 0) for row in rows}
        for row in dr_rows:
            user = str(row['user_id'])
            key = (user, str(row['payout_currency'] or underlying_currency))
            combined[key] = combined.get(key, 0.0) + float(row['quantity'] or 0)
        # Treasuries and a company's own (treasury) shares receive no dividend.
        from managers.company_account_manager import CompanyAccounts
        return [(user, currency, quantity)
                for (user, currency), quantity in sorted(combined.items()) if quantity > 0
                and not CurrencyManager.is_treasury(conn, user)
                and not CompanyAccounts.own_shares(conn, user, code)]

    @staticmethod
    def _currency_account(conn, user, currency, now):
        from managers.deposit_accounts import account
        row = account(conn, user, currency)
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
            if currency != base_currency_code(conn):
                raise ValueError('발행법인 현금 배당은 기준 통화 상품에만 사용할 수 있습니다.')
            row = conn.execute('SELECT balance FROM issuer_cash_accounts WHERE company_code=?', (issuer,)).fetchone()
            return int(row['balance']) if row else 0
        # The listing country's treasury funds treasury dividends and can always pay.
        return self.MAX_MONEY

    def _debit_dividend(self, conn, issuer, funding, currency, gross, counterpart="retained"):
        from database.fund_accounting import change_cash
        fund = conn.execute('SELECT owner_id FROM fund_accounts WHERE product_code=?', (issuer,)).fetchone()
        if fund:
            if currency != base_currency_code(conn):
                raise ValueError('펀드 분배금은 기준 통화만 지원합니다.')
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

    def _credit_dividend(self, conn, user, code, kind, amount, currency, now,
                         operation_key=None, dr_code=None):
        from database.fund_accounting import account
        from managers.currency_manager import CurrencyManager
        amount_decimal = Decimal(str(amount))
        amount_units = int((amount_decimal*SCALE).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        fund = account(conn, user)
        if fund:
            from managers.fund_dividend_receiver import FundDividendReceiver
            receipt = FundDividendReceiver(conn).receive(
                user, amount_decimal, currency_code=currency, reference=code,
                recorded_epoch=int(now),
            )
            if amount_units > 0 and not receipt.get('entry_id'):
                raise RuntimeError('펀드 배당 현금 원장 입금이 없습니다.')
            return 0, receipt['net_amount_units'], {
                'issuer_nationality': '낙원', 'underlying_nationality': '낙원',
                'recipient_nationality': '낙원', 'recipient_residency': '낙원',
                'product_type': 'fund', 'contract_epoch': int(now), 'rule_id': None,
                'treaty_code': None, 'tax_rate_percent': '0', 'dr_code': None,
                'remittance': None,
                'recipient_fund_entry_id': receipt['entry_id'],
            }
        rate = CurrencyExchangeManager._rate(conn, currency)
        dr_context = self._dr_tax_context(conn, user, code, currency, kind, now,
                                          operation_key, dr_code) if dr_code else None
        tax_row = conn.execute(
            "SELECT nationality FROM users WHERE user_id=?", (str(user),)
        ).fetchone()
        jurisdiction = (dr_context['underlying_nationality'] if dr_context
                        else str(tax_row['nationality'] if tax_row and tax_row['nationality'] else '낙원'))
        if dr_context:
            tax_rate = Decimal(dr_context['tax_rate_percent'])
        else:
            configured = conn.execute(
                "SELECT COALESCE((SELECT tax_percent FROM country_tax_rules "
                "WHERE nationality=? AND income_source=?),c.tax_percent) rate "
                "FROM country_currencies c WHERE c.nationality=?",
                (jurisdiction, f'{kind}_dividend', jurisdiction),
            ).fetchone()
            fallback = conn.execute("SELECT value FROM settings WHERE key='tax_percent'").fetchone()
            try:
                tax_rate = Decimal(str(configured['rate'] if configured and configured['rate'] is not None
                                       else fallback['value'] if fallback else '3'))
            except Exception:
                tax_rate = Decimal('3')
            if not tax_rate.is_finite() or tax_rate < 0 or tax_rate > 100:
                tax_rate = Decimal('3')
        from functions.money import currency_precision
        places = currency_precision(conn, currency)
        tax_major = (amount_decimal * tax_rate / Decimal(100)).quantize(
            Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        tax = ExactDecimal(tax_major) if places else int(tax_major)
        tax_units = int((tax_major * SCALE).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        net_units = max(0, amount_units-tax_units)
        remittance = None
        remittance_wallet_watermark = None
        if tax_units:
            try:
                treasury = CurrencyManager.treasury_id(conn, jurisdiction)
            except ValueError:
                # Older/small worlds may not yet have a country treasury configured;
                # preserve their legacy federation treasury until one is provisioned.
                from database.user_db import TREASURY_USER_ID
                treasury = TREASURY_USER_ID
            settlement_currency = (currency if CurrencyManager._treasury_target(conn, treasury, currency)
                                   else CurrencyManager.home(conn, treasury))
            remittance_wallet_watermark = int(conn.execute(
                "SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger"
            ).fetchone()[0])
            settlement_units = CurrencyManager.treasury_receive(conn, treasury, currency, tax_units)
            remittance = {"treasury_id": treasury, "jurisdiction": jurisdiction,
                          "source_currency": currency, "source_units": tax_units,
                          "settlement_currency": settlement_currency,
                          "settlement_units": settlement_units}
            tax_transaction = conn.execute('INSERT INTO tax_transactions(user_id,source,gross_amount,tax_amount,net_amount,currency_code) VALUES (?,?,?,?,?,?)',
                         (str(user), f'{"dr_cross_border_" if dr_context else ""}{kind}_dividend:{code}',
                          amount, tax, ExactDecimal(net_units)/SCALE, currency))
            from managers.accounting_journal import AccountingJournal
            AccountingJournal.link_tax_transaction_wallet_entries(
                conn, tax_transaction.lastrowid, remittance_wallet_watermark, treasury
            )
        recipient_wallet_watermark = None
        if CurrencyManager.home(conn,user) == currency:
            net_units = max(0, amount_units-tax_units)
            recipient_wallet_watermark = int(conn.execute(
                "SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger"
            ).fetchone()[0])
            changed = conn.execute('UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?',
                                   (net_units//SCALE, user, self.MAX_MONEY-net_units//SCALE))
        else:
            CurrencyExchangeManager._settle(conn, user, now)
            account = self._currency_account(conn, user, currency, now)
            recipient_wallet_watermark = int(conn.execute(
                "SELECT COALESCE(MAX(entry_id),0) FROM customer_wallet_ledger"
            ).fetchone()[0])
            from managers.deposit_accounts import PRINCIPAL_DELTA
            changed = conn.execute(f"UPDATE accounts SET balance_units=balance_units+?,{PRINCIPAL_DELTA} WHERE account_number=? AND account_type='saving' AND balance_units<=?",
                                   (net_units, net_units, account, MAX_UNITS-net_units))
        if changed.rowcount != 1:
            raise ValueError('배당 수령자의 계정 또는 잔액 한도를 확인해주세요.')
        from functions.money import to_minor_units
        expected_recipient_minor = to_minor_units(
            Decimal(net_units) / Decimal(SCALE), places,
        )
        recipient_wallet_entry = None
        if expected_recipient_minor > 0:
            recipient_wallet_entry = conn.execute(
                "SELECT entry_id FROM customer_wallet_ledger WHERE entry_id>? AND user_id=? "
                "AND currency_code=? AND decimal_places=? AND delta_minor_units=? "
                "ORDER BY entry_id LIMIT 1",
                (recipient_wallet_watermark, str(user), str(currency).upper(), places,
                 expected_recipient_minor),
            ).fetchone()
            if not recipient_wallet_entry:
                raise RuntimeError('배당 고객 지갑 입금 원장 항목이 없습니다.')
        from managers.company_account_manager import CompanyAccounts
        if CompanyAccounts.is_company(conn, user):
            # A company's dividend income is 영업외수익 in its statements.
            listing = conn.execute('SELECT COALESCE(parent_company_code,company_code) FROM company WHERE company_code=?', (code,)).fetchone()
            CompanyAccounts.log_income(conn, user, 'dividend', amount_decimal*rate,
                                       tax_major*rate, counterparty=listing[0] if listing else code)
        if dr_context:
            dr_context['remittance'] = remittance
            dr_context['tax_rate_percent'] = str(tax_rate)
            dr_context['recipient_wallet_entry_id'] = (
                int(recipient_wallet_entry['entry_id']) if recipient_wallet_entry else None
            )
        else:
            dr_context = {"issuer_nationality": jurisdiction,
                          "underlying_nationality": jurisdiction,
                          "recipient_nationality": str(tax_row['nationality'] if tax_row else '낙원'),
                          "recipient_residency": str(tax_row['nationality'] if tax_row else '낙원'),
                          "product_type": "ordinary_equity", "contract_epoch": int(now),
                          "rule_id": None, "treaty_code": None,
                          "tax_rate_percent": str(tax_rate), "dr_code": None,
                          "remittance": remittance,
                          "recipient_wallet_entry_id": (
                              int(recipient_wallet_entry['entry_id'])
                              if recipient_wallet_entry else None
                          )}
        return tax, net_units, dr_context

    @staticmethod
    def _dr_tax_context(conn, user, underlying_code, currency, kind, contract_epoch,
                        operation_key=None, dr_code=None):
        from managers.dr_dividend_tax_manager import DrDividendTaxManager
        row = conn.execute(
            "SELECT d.dr_code,d.quote_currency,COALESCE(de.nationality,'낙원') issuer_nationality,"
            "COALESCE(ue.nationality,'낙원') underlying_nationality,u.nationality recipient_nationality,"
            "COALESCE(NULLIF(h.tax_residency,''),NULLIF(u.tax_residency,''),u.nationality,'낙원') recipient_residency "
            "FROM depositary_receipts d JOIN shareholder_registers sr ON sr.asset_code=d.underlying_code "
            "JOIN shareholder_register_dr_entitlements h ON h.register_id=sr.register_id AND h.user_id=? "
            "AND h.payout_currency=d.quote_currency "
            "JOIN market_assets a ON a.code=d.dr_code "
            "LEFT JOIN market_exchanges de ON de.exchange_id=a.exchange_id "
            "LEFT JOIN company c ON c.company_code=d.underlying_code "
            "LEFT JOIN market_exchanges ue ON ue.exchange_id=c.exchange_id "
            "JOIN users u ON u.user_id=h.user_id WHERE d.underlying_code=? AND d.quote_currency=? "
            "AND d.program_status<>'terminated' AND (? IS NULL OR sr.operation_key=?) "
            "AND (? IS NULL OR d.dr_code=?) "
            "ORDER BY d.dr_code LIMIT 1",
            (str(user), str(underlying_code), str(currency), operation_key, operation_key,
             dr_code, dr_code),
        ).fetchone()
        if not row:
            return None
        issuer = str(row['issuer_nationality'] or '낙원')
        underlying = str(row['underlying_nationality'] or '낙원')
        recipient = str(row['recipient_nationality'] or '낙원')
        residency = str(row['recipient_residency'] or recipient)
        product = 'depositary_receipt'
        record = conn.execute("SELECT record_epoch FROM shareholder_registers WHERE asset_code=? AND status IN ('closed','paid') AND (? IS NULL OR operation_key=?) ORDER BY register_id DESC LIMIT 1", (str(underlying_code), operation_key, operation_key)).fetchone()
        effective_epoch = int(record['record_epoch']) if record else int(contract_epoch)
        rule = DrDividendTaxManager.applicable_rule(
            conn, issuer=issuer, underlying=underlying, recipient=recipient,
            residency=residency, product=product, contract_epoch=effective_epoch,
        )
        if rule:
            rate = Decimal('0') if rule['exempt'] else Decimal(str(rule['tax_rate_percent']))
            return {'issuer_nationality': issuer, 'underlying_nationality': underlying,
                    'recipient_nationality': recipient, 'recipient_residency': residency,
                    'product_type': product, 'contract_epoch': effective_epoch,
                    'rule_id': int(rule['rule_id']), 'treaty_code': rule['treaty_code'],
                    'tax_rate_percent': str(rate), 'dr_code': str(row['dr_code'])}
        tax_row = conn.execute("SELECT COALESCE((SELECT tax_percent FROM country_tax_rules WHERE nationality=? AND income_source=?),c.tax_percent) rate FROM users u LEFT JOIN country_currencies c ON c.nationality=u.nationality WHERE u.user_id=?", (recipient, f'{kind}_dividend', str(user))).fetchone()
        fallback = conn.execute("SELECT value FROM settings WHERE key='tax_percent'").fetchone()
        try:
            fallback_rate = Decimal(str(tax_row['rate'] if tax_row and tax_row['rate'] is not None else fallback['value'] if fallback else '3'))
        except Exception:
            fallback_rate = Decimal('3')
        if not fallback_rate.is_finite() or fallback_rate < 0 or fallback_rate > 100:
            fallback_rate = Decimal('3')
        return {'issuer_nationality': issuer, 'underlying_nationality': underlying,
                'recipient_nationality': recipient, 'recipient_residency': residency,
                'product_type': product, 'contract_epoch': effective_epoch,
                'rule_id': None, 'treaty_code': None,
                'tax_rate_percent': str(fallback_rate), 'dr_code': str(row['dr_code'])}

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
            tax, _, _ = self._credit_dividend(conn, claim['user_id'], claim['asset_code'], 'arrears', amount, claim['quote_currency'], now)
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
        holders = self._holders(conn, listing['code'])
        source_currency = str(listing.get('quote_currency') or base_currency_code(conn))
        conn.executemany(
            'INSERT INTO shareholder_register_entries(register_id,user_id,quantity,tax_residency) '
            "VALUES (?,?,?,COALESCE((SELECT COALESCE(NULLIF(tax_residency,''),nationality) "
            "FROM users WHERE user_id=?),'낙원'))",
            [(register, user, quantity, user) for user, currency, quantity in holders
             if currency == source_currency and quantity > 0])
        conn.executemany(
            'INSERT INTO shareholder_register_dr_entries(register_id,user_id,payout_currency,quantity) '
            'VALUES (?,?,?,?)',
            [(register, user, currency, quantity) for user, currency, quantity in holders
             if currency != source_currency and quantity > 0])
        # Preserve program identity even when a DR pays in the underlying's
        # currency; the aggregate shareholder register alone cannot identify tax source.
        conn.execute(
            "INSERT OR IGNORE INTO shareholder_register_dr_entitlements "
            "(register_id,user_id,dr_code,payout_currency,quantity,tax_residency) "
            "SELECT ?,h.user_id,d.dr_code,d.quote_currency,SUM(h.quantity*d.underlying_per_receipt), "
            "COALESCE(NULLIF(u.tax_residency,''),u.nationality,'낙원') "
            "FROM depositary_receipts d JOIN user_assets h ON h.asset_code=d.dr_code "
            "LEFT JOIN users u ON u.user_id=h.user_id "
            "WHERE d.underlying_code=? AND d.program_status<>'terminated' "
            "GROUP BY h.user_id,d.dr_code,d.quote_currency",
            (register, str(listing['code'])),
        )
        return register

    @staticmethod
    def _register_holders(conn, key):
        row = conn.execute('SELECT register_id,asset_code FROM shareholder_registers WHERE operation_key=?', (str(key),)).fetchone()
        if not row:
            return None
        source_currency = conn.execute(
            "SELECT COALESCE((SELECT quote_currency FROM company WHERE company_code=?),"
            "(SELECT quote_currency FROM market_assets WHERE code=?),base_currency_code())",
            (str(row['asset_code']), str(row['asset_code'])),
        ).fetchone()[0]
        holders = [(str(entry['user_id']), str(source_currency), float(entry['quantity'])) for entry in conn.execute(
            'SELECT user_id,quantity FROM shareholder_register_entries WHERE register_id=? ORDER BY user_id',
            (row['register_id'],))]
        holders.extend((str(entry['user_id']), str(entry['payout_currency']), float(entry['quantity']))
                       for entry in conn.execute(
            'SELECT user_id,payout_currency,quantity FROM shareholder_register_dr_entries '
            'WHERE register_id=? ORDER BY user_id,payout_currency', (row['register_id'],)))
        return holders

    @staticmethod
    def _register_components(conn, key, user, payout_currency, total_quantity, source_currency):
        register = conn.execute(
            "SELECT register_id FROM shareholder_registers WHERE operation_key=?", (str(key),)
        ).fetchone()
        if not register:
            return [(None, Decimal(str(total_quantity)))]
        dr_rows = conn.execute(
            "SELECT dr_code,quantity FROM shareholder_register_dr_entitlements "
            "WHERE register_id=? AND user_id=? AND payout_currency=? ORDER BY dr_code",
            (register['register_id'], str(user), str(payout_currency)),
        ).fetchall()
        components = [(str(row['dr_code']), Decimal(str(row['quantity']))) for row in dr_rows
                      if Decimal(str(row['quantity'])) > 0]
        dr_quantity = sum((quantity for _, quantity in components), Decimal(0))
        if payout_currency == source_currency:
            direct = conn.execute(
                "SELECT quantity FROM shareholder_register_entries WHERE register_id=? AND user_id=?",
                (register['register_id'], str(user)),
            ).fetchone()
            direct_quantity = max(Decimal(0), Decimal(str(direct['quantity'] if direct else 0))-dr_quantity)
            if direct_quantity > 0:
                components.insert(0, (None, direct_quantity))
        if not components:
            return [(None, Decimal(str(total_quantity)))]
        return components

    @staticmethod
    def _allocate_component_amount(conn, amount, currency, components):
        from functions.money import currency_precision
        places = currency_precision(conn, currency)
        scale = 10 ** places
        total_minor = int((Decimal(str(amount))*scale).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        weight_total = sum((weight for _, weight in components), Decimal(0))
        if total_minor <= 0 or weight_total <= 0:
            return [0 for _ in components]
        exact = [Decimal(total_minor)*weight/weight_total for _, weight in components]
        units = [int(value.to_integral_value(rounding='ROUND_FLOOR')) for value in exact]
        residual = total_minor-sum(units)
        order = sorted(range(len(exact)), key=lambda index: (-(exact[index]-units[index]),
                                                             str(components[index][0] or 'stock')))
        for index in order[:residual]:
            units[index] += 1
        return [ExactDecimal(value)/scale if places else value for value in units]

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
        payments = [(user,quantity,int((Decimal(str(quantity))*unit_amount).quantize(Decimal('1'),rounding=ROUND_HALF_UP)), payout_currency)
                    for user, payout_currency, quantity in holders]
        owed = sum(amount for _,_,amount,_ in payments)
        currency = listing.get('quote_currency') or base_currency_code(conn)
        rate = CurrencyExchangeManager._rate(conn, currency)
        if owed > self.MAX_MONEY or owed*rate > self.MAX_MONEY or owed*SCALE > MAX_UNITS:
            raise ValueError('총 배당금의 금액 또는 외환 평가액이 한도를 초과했습니다.')
        issuer, funding = self._issuer_code(listing), self._funding(listing)
        cumulative = listing['table']=='company' and listing['share_class']=='preferred' and bool(listing['cumulative_dividend'])
        gross = min(owed,self._source_balance(conn,issuer,funding,currency)) if cumulative else owed
        if gross < owed and not cumulative:
            raise ValueError('배당 재원이 부족합니다.')
        amounts = self._allocate(gross,[amount for _,_,amount,_ in payments]) if owed and gross < owed else [amount for _,_,amount,_ in payments]
        cursor = conn.execute('''INSERT INTO dividend_distributions
            (operation_key,dividend_kind,asset_code,due_epoch,per_share,dividend_mode,
             dividend_percent,reference_price,gross_amount,entitlement_amount,deferred_amount,
             quote_currency,funding_source,issuer_code,created_epoch)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (str(key),kind,listing['code'],int(due),int(per_share),mode,percent,reference_price,
             gross,owed,owed-gross,currency,funding,issuer,int(now)))
        distribution = cursor.lastrowid
        self._debit_dividend(conn,issuer,funding,currency,gross)
        from managers.bank_journal import BankJournal
        for (user,quantity,entitlement,payout_currency),source_amount in zip(payments,amounts):
            amount = source_amount
            if payout_currency != currency and source_amount:
                source_units = int(Decimal(source_amount)*SCALE)
                target_units = CurrencyManager.convert(conn, source_units, currency, payout_currency)
                fx_treasury = CurrencyManager.exchange_treasury(conn, listing['code'])
                CurrencyManager.treasury_receive(conn, fx_treasury, currency, source_units)
                CurrencyManager.treasury_pay(conn, fx_treasury, payout_currency, target_units)
                amount = target_units/SCALE
                conn.execute(
                    'INSERT INTO currency_transactions(user_id,source_currency,target_currency,'
                    'source_units,target_units,fee_nwx,recorded_epoch) VALUES (?,?,?,?,?,?,?)',
                    (user, currency, payout_currency, source_units, target_units, 0, now),
                )
            components = self._register_components(
                conn, key, user, payout_currency, quantity, currency
            )
            component_amounts = self._allocate_component_amount(
                conn, amount, payout_currency, components
            )
            tax_total = 0
            for (dr_code, component_quantity), component_amount in zip(components, component_amounts):
                if component_amount <= 0:
                    continue
                allocation_key = f"dr:{dr_code}" if dr_code else "stock"
                tax,net_units,tax_context = self._credit_dividend(
                    conn,user,listing['code'],kind,component_amount,payout_currency,now,key,dr_code
                )
                tax_total += tax
                payment_cursor = conn.execute('''INSERT INTO dividend_payments
                    (distribution_id,user_id,quantity,gross_amount,tax_amount,net_amount_units,
                     quote_currency,created_epoch,allocation_key) VALUES (?,?,?,?,?,?,?,?,?)''',
                    (distribution,user,float(component_quantity),component_amount,tax,net_units,
                     payout_currency,int(now),allocation_key))
                payment_id = int(payment_cursor.lastrowid)
                if net_units > 0:
                    wallet_entry_id = tax_context.get('recipient_wallet_entry_id')
                    fund_entry_id = tax_context.get('recipient_fund_entry_id')
                    if bool(wallet_entry_id) == bool(fund_entry_id):
                        raise RuntimeError('배당 지급을 고객 또는 펀드 현금 원장에 하나만 연결해야 합니다.')
                    if wallet_entry_id:
                        conn.execute(
                            "INSERT INTO dividend_payment_wallet_links(payment_id,wallet_entry_id) "
                            "VALUES (?,?)", (payment_id, int(wallet_entry_id)),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO dividend_payment_fund_links(payment_id,fund_entry_id) "
                            "VALUES (?,?)", (payment_id, int(fund_entry_id)),
                        )
                journal_issuer = f"{'DR_' if dr_code else ''}DIVIDEND:{tax_context['underlying_nationality']}"
                journal_ref = f"dr-dividend:{distribution}:{user}:{allocation_key}:{payout_currency}"
                net_amount = Decimal(net_units)/Decimal(SCALE)
                if not BankJournal.post(
                    conn, journal_issuer, "dr_dividend_payment" if dr_code else "dividend_payment",
                    journal_ref, payout_currency,
                    [("dividend_expense", "debit", component_amount),
                     ("customer_deposits_payable", "credit", net_amount),
                     ("tax_payable", "credit", tax)],
                    description=f"{listing['code']} dividend {allocation_key}", now=now,
                ):
                    raise ValueError("배당 지급 분개 참조키가 이미 사용되었습니다.")
                remittance = tax_context.get('remittance')
                if remittance:
                    if not BankJournal.post_tax_remittance(
                        conn, journal_issuer, f"{journal_ref}:tax", remittance['source_currency'],
                        Decimal(remittance['source_units'])/Decimal(SCALE),
                        remittance['settlement_currency'],
                        Decimal(remittance['settlement_units'])/Decimal(SCALE),
                        description=f"{listing['code']} dividend withholding remittance", now=now,
                    ):
                        raise ValueError("배당 원천징수 국고 납부 분개 참조키가 이미 사용되었습니다.")
                dr_tax = tax_context if dr_code else None
                if dr_tax:
                    from functions.money import currency_precision, to_minor_units
                    payout_places = currency_precision(conn, payout_currency)
                    gross_minor = to_minor_units(component_amount, payout_places)
                    tax_minor = to_minor_units(tax, payout_places)
                    net_minor = to_minor_units(
                        Decimal(net_units) / Decimal(SCALE), payout_places,
                    )
                    if gross_minor != tax_minor + net_minor:
                        raise ValueError("DR 배당 원천징수 최소단위 금액이 일치하지 않습니다.")
                    if dr_tax.get('remittance'):
                        remittance_treasury = dr_tax['remittance']['treasury_id']
                    else:
                        try:
                            remittance_treasury = CurrencyManager.treasury_id(conn, dr_tax['underlying_nationality'])
                        except ValueError:
                            from database.user_db import TREASURY_USER_ID
                            remittance_treasury = TREASURY_USER_ID
                    conn.execute('''INSERT INTO dr_dividend_withholding_events
                        (payment_id,distribution_id,rule_id,dr_code,issuer_nationality,underlying_nationality,
                         recipient_nationality,recipient_residency,product_type,contract_epoch,treaty_code,
                         tax_rate_percent,gross_amount,tax_amount,net_amount_units,currency_code,
                         remittance_treasury_id,decimal_places,gross_minor_units,
                         tax_minor_units,net_minor_units,recorded_epoch)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (payment_id,distribution,dr_tax['rule_id'],dr_code,dr_tax['issuer_nationality'],
                         dr_tax['underlying_nationality'],dr_tax['recipient_nationality'],
                         dr_tax['recipient_residency'],dr_tax['product_type'],dr_tax['contract_epoch'],
                         dr_tax['treaty_code'],dr_tax['tax_rate_percent'],component_amount,tax,
                         net_units,payout_currency,remittance_treasury,payout_places,gross_minor,
                         tax_minor,net_minor,int(now)))
            if cumulative and entitlement > source_amount:
                conn.execute('INSERT INTO preferred_dividend_claims(issuer_code,asset_code,distribution_id,user_id,quantity,gross_amount,quote_currency,funding_source,due_epoch,created_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (issuer,listing['code'],distribution,user,quantity,entitlement-source_amount,currency,funding,due,now))
            conn.execute('INSERT INTO product_settlements(user_id,product_code,kind,quantity,gross_amount,tax_amount,recorded_epoch) VALUES (?,?,?,?,?,?,?)',
                         (user,listing['code'],f'{kind}_dividend:{payout_currency}',quantity,amount,tax_total,now))
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
            amount = self._integer(per_share, '주당 특별배당금', 1, MAX_NATIVE)
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
                              f'<t:{pay_epoch}:T>에 주당 {amount:,} {listing.get("quote_currency") or base_currency_code(conn)}를 장부대로 지급합니다.')
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
            return True, f'✅ `{code}` 특별배당 {"지급 내역을 확인했습니다" if duplicate else "지급을 완료했습니다"}. 주당 {amount:,} · 총 {gross:,} {listing.get("quote_currency") or base_currency_code(conn)}'
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
        now = int(RateManager._validated_epoch(
            now, "invalid_corporate_action_processing_epoch"
        ))
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
        """Move spendable 기준 통화 between the treasury and a legal issuer's cash ledger."""
        try:
            value = self._integer(value,'법인 현금', maximum=MAX_NATIVE)
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
                # Issuer cash (기준 통화) moves to/from the listing country's treasury.
                treasury = CurrencyManager.exchange_treasury(conn, issuer)
                if delta > 0:
                    CurrencyManager.treasury_pay(conn, treasury, base_currency_code(conn), delta*SCALE)
                elif delta < 0:
                    CurrencyManager.treasury_receive(conn, treasury, base_currency_code(conn), -delta*SCALE)
                conn.execute('UPDATE issuer_cash_accounts SET balance=? WHERE company_code=?',(new,issuer))
                Finance.event(conn,issuer,'국고와 법인 자본 이체',
                              [('cash',delta),('capital_surplus',-delta)],
                              'financing',now,currency=base_currency_code(conn))
                conn.execute('INSERT INTO issuer_cash_transfers(company_code,actor_id,old_balance,new_balance,delta,recorded_epoch) VALUES (?,?,?,?,?,?)',
                             (issuer,str(actor),old,new,delta,now))
                self._audit(conn,actor,'issuer.cash',issuer,{'operation':operation,'value':value,'old_balance':old,'new_balance':new},now)
            return True,f'✅ `{issuer}` 법인 사용가능 현금 {old:,} → {new:,} 기준 통화. 국고와 {abs(delta):,} 기준 통화 이체'
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
            now = int(RateManager._validated_epoch(
                now, "invalid_corporate_action_liquidation_epoch"
            ))
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
                if any((row['quote_currency'] or base_currency_code(conn))!=base_currency_code(conn) for row in shares):
                    raise ValueError('법인 현금 청산은 기준 통화 주식에만 사용할 수 있습니다.')
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
                if any((bond['quote_currency'] or base_currency_code(conn))!=base_currency_code(conn) for bond in bonds):
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
                    if claim['quote_currency']!=base_currency_code(conn):
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
                        # The listing country's treasury keeps the residual (in 기준 통화 or exchanged).
                        CurrencyManager.treasury_receive(conn, item['user'], base_currency_code(conn), net * SCALE)
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
            return True,f'✅ `{issuer}` 법인과 연결 우선주를 청산했습니다. 현금 {initial:,} 기준 통화 · 회사채 → 미지급 배당 → 우선주 한도 → 보통주 순서로 지급'
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
            CurrencyManager.treasury_pay(conn, CurrencyManager.exchange_treasury(conn, issuer), base_currency_code(conn), proceeds*SCALE)
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
            if conn.execute("SELECT 1 FROM loan_collateral_assets WHERE asset_code=? AND status='pledged' LIMIT 1", (code,)).fetchone():
                raise ValueError(f'`{code}`의 대출 담보 주식을 먼저 해제해주세요.')
            if conn.execute("SELECT 1 FROM dr_custody_reservations WHERE reserved_asset_code=? AND released_epoch IS NULL LIMIT 1", (code,)).fetchone():
                raise ValueError(f'`{code}`의 예탁·상환 예약을 먼저 정리해주세요.')
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
                if not 0 < supply <= (2**53 - 1) - quantity:
                    raise ValueError('기존 발행량 또는 신규 발행량 한도를 확인해주세요.')
                if mode == '무상':
                    self._unencumbered(conn, [code])
                elif conn.execute("SELECT 1 FROM corporate_equity_offerings e JOIN market_orders o USING(order_id) WHERE e.asset_code=? AND o.status IN ('open','partial') LIMIT 1", (code,)).fetchone():
                    raise ValueError('진행 중인 유상증자 신주 판매를 먼저 완료하거나 취소해주세요.')
                price = int(listing['share_price'])
                currency = listing.get('quote_currency') or base_currency_code(conn)
                if mode == '유상':
                    from managers.market_session_manager import MarketSessionManager
                    error = MarketSessionManager(self.db).trading_error(conn=conn, now=now, asset_code=code)
                    if error:
                        raise ValueError(error)
                    issue = self._integer(price if issue_price is None else issue_price, '유상 발행가', 1, MAX_NATIVE)
                    if quantity * issue > self.MAX_MONEY:
                        raise ValueError('유상증자 모집 금액이 한도를 초과합니다.')
                    # The listing country's treasury holds and sells the new shares.
                    seller = CurrencyManager.exchange_treasury(conn, code)
                    from managers.asset_cost_manager import AssetCostManager
                    AssetCostManager.upsert(conn, seller, code, None, quantity, issue)
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
                    from managers.asset_cost_manager import AssetCostManager
                    from functions.money import currency_precision
                    for lot, new in zip(lots, allocations):
                        if new:
                            old_quantity = Fraction(str(lot['quantity']))
                            new_quantity = old_quantity + Fraction(str(new))
                            old_quantity_decimal = Decimal(old_quantity.numerator) / Decimal(old_quantity.denominator)
                            new_quantity_decimal = Decimal(new_quantity.numerator) / Decimal(new_quantity.denominator)
                            cost = Decimal(int(lot['average_cost_minor_units'] or 0)) / (10 ** currency_precision(conn, currency))
                            AssetCostManager.reprice(
                                conn, lot['holding_id'], code, float(new_quantity),
                                cost * old_quantity_decimal / new_quantity_decimal, preserve_total=True,
                            )
                    if allocations[-1]:
                        AssetCostManager.upsert(
                            conn, CurrencyManager.exchange_treasury(conn, code),
                            code, None, allocations[-1], 0,
                        )
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

    def retire_share_class(self, actor, code):
        """Retire a preferred/custom class only after all holders and encumbrances clear."""
        code = str(code)
        now = int(time.time())
        try:
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                listing = self._listing(conn, code)
                if listing['table'] != 'company' or listing['share_class'] != 'preferred':
                    raise ValueError('보통주는 이 기능으로 폐지할 수 없습니다. 별도 기업 청산 절차를 사용해주세요.')
                if listing['status'] != 'active':
                    raise ValueError('이미 거래가 중지된 주식 종류입니다.')
                if conn.execute("SELECT 1 FROM depositary_receipts WHERE underlying_code=? AND program_status<>'terminated' LIMIT 1", (code,)).fetchone():
                    raise ValueError('미종료 DR 프로그램을 먼저 상환하고 종료해주세요.')
                from managers.company_account_manager import CompanyAccounts
                holdings = conn.execute('SELECT holding_id,user_id FROM user_assets WHERE asset_code=?', (code,)).fetchall()
                external_holding = next((row for row in holdings
                    if not CurrencyManager.is_treasury(conn, row['user_id'])
                    and not CompanyAccounts.own_shares(conn, row['user_id'], code)), None)
                if external_holding:
                    raise ValueError('보유자가 남아 있어 주식 종류를 폐지할 수 없습니다. 먼저 보유 수량을 정산해주세요.')
                self._unencumbered(conn, [code], allow_orders=False)
                if conn.execute("SELECT 1 FROM market_orders WHERE asset_code=? AND status='scheduled' LIMIT 1", (code,)).fetchone():
                    raise ValueError('예약 주문을 먼저 취소해주세요.')
                if conn.execute("SELECT 1 FROM shareholder_registers WHERE asset_code=? AND status='closed' LIMIT 1", (code,)).fetchone():
                    raise ValueError('지급 대기 중인 주주명부가 있어 배당 지급을 먼저 완료해주세요.')
                if conn.execute("SELECT 1 FROM preferred_dividend_claims WHERE asset_code=? AND status IN ('unpaid','partial') LIMIT 1", (code,)).fetchone():
                    raise ValueError('미지급 누적 배당이 있어 청구를 먼저 정산해주세요.')
                if conn.execute('SELECT 1 FROM dividend_distributions WHERE asset_code=? AND deferred_amount>0 LIMIT 1', (code,)).fetchone():
                    raise ValueError('이연 배당금이 남아 있어 먼저 정산해주세요.')
                if conn.execute('SELECT 1 FROM fund_components WHERE asset_code=? LIMIT 1', (code,)).fetchone():
                    raise ValueError('펀드 구성 종목에서 제거한 뒤 폐지해주세요.')
                if conn.execute('SELECT 1 FROM market_exchange_index_components WHERE company_code=? LIMIT 1', (code,)).fetchone():
                    raise ValueError('거래소 지수 구성 종목에서 제거한 뒤 폐지해주세요.')
                for row in holdings:
                    if CurrencyManager.is_treasury(conn, row['user_id']) or CompanyAccounts.own_shares(conn, row['user_id'], code):
                        conn.execute('DELETE FROM user_assets WHERE holding_id=?', (row['holding_id'],))
                conn.execute(
                    "UPDATE company SET status='inactive',total_shares=0,listed_shares=0,"
                    "max_shareholder_id=NULL,dividend_next_epoch=NULL WHERE company_code=?", (code,)
                )
                self._reset_market_state(conn, code)
                self._record(conn, 'retire-class:' + uuid.uuid4().hex, 'share_class_retirement',
                             code, listing.get('parent_company_code'),
                             {'class_name': listing.get('name'), 'cancelled_shares': int(listing.get('total_shares') or 0)}, now)
            return True, f'✅ `{code}` 주식 종류를 폐지했습니다. 보유·주문·계약이 없어 기존 보통주에는 영향이 없습니다.'
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

    def adjust_face_value(self, actor, action, code, ratio, *, mode=None, resolution_id=None, old_shares=None, new_shares=None):
        """Apply an exact stock split or consolidation to every unit-based state."""
        try:
            action = str(action).strip()
            if mode == 'ratio_split':
                from fractions import Fraction
                old_units = self._integer(old_shares, '기존 주식 수', 1, maximum=2**53 - 1)
                new_units = self._integer(new_shares, '변경 주식 수', 1, maximum=2**53 - 1)
                conversion = Fraction(new_units, old_units)
                if conversion == 1:
                    raise ValueError('기존 주식과 변경 주식의 비율은 달라야 합니다.')
                action, ratio = ('분할' if conversion > 1 else '병합'), 2
            if action not in {'분할', '병합'} or isinstance(ratio, bool) or str(int(ratio)) != str(ratio).strip() or int(ratio) < 2:
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
                from managers.share_restructuring_manager import ShareRestructuringManager
                mode = ('stock_split' if action == '분할' else 'reverse_split') if mode is None else mode
                if mode != 'ratio_split' and (action == '병합') != (mode == 'reverse_split'):
                    raise ValueError('집행 방식과 분할·병합 방향이 다릅니다.')
                resolution = ShareRestructuringManager.prepare(conn, listing, mode, ratio, resolution_id)
                self._unencumbered(conn, [code], allow_orders=False)
                if conn.execute("SELECT 1 FROM market_orders WHERE asset_code=? AND status='scheduled' LIMIT 1", (code,)).fetchone():
                    raise ValueError('예약 주문을 먼저 취소해주세요.')
                places = currency_precision(conn, listing.get('quote_currency') or base_currency_code(conn))
                price, shares = round_money(listing['share_price'], places), int(listing['total_shares'])
                price_minor = to_minor_units(price, places)
                from fractions import Fraction
                from managers.asset_cost_manager import AssetCostManager
                conversion = conversion if mode == 'ratio_split' else (Fraction(ratio) if action == '분할' else Fraction(1, ratio))

                def converted_quantity(quantity, *, whole=True):
                    result = Fraction(str(quantity)) * conversion
                    if whole and result.denominator != 1:
                        raise ValueError('변경 비율로 발행·상장·보유 수량이 정수 주식으로 나누어떨어져야 합니다. 단주는 버리지 않습니다.')
                    if result > 2**53 - 1:
                        raise ValueError('변경 후 주식 수가 현재 수량 한도를 초과합니다.')
                    return result.numerator if result.denominator == 1 else Decimal(str(quantity)) * Decimal(conversion.numerator) / Decimal(conversion.denominator)

                new_shares = converted_quantity(shares)
                listed = listing.get('listed_shares')
                new_listed = None if listed is None else converted_quantity(listed)
                new_price_minor = price_minor * conversion.denominator // conversion.numerator
                if new_price_minor < 1:
                    raise ValueError('분할 후 주가가 통화 최소단위 미만입니다. 더 작은 분할 비율을 입력해주세요.')
                new_price = round_money(from_minor_units(new_price_minor, places), places)
                if new_price > MAX_NATIVE:
                    raise ValueError('변경 후 주가가 현재 금액 한도를 초과합니다.')
                factor = Decimal(conversion.denominator) / Decimal(conversion.numerator)
                for lot in conn.execute('SELECT holding_id,quantity,average_cost_minor_units FROM user_assets WHERE asset_code=?', (code,)):
                    quantity = converted_quantity(lot['quantity'], whole=mode == 'ratio_split' or action == '병합')
                    unit_cost = Decimal(int(lot['average_cost_minor_units'] or 0)) / (10 ** places)
                    AssetCostManager.reprice(conn, lot['holding_id'], code, quantity,
                                             unit_cost * factor, preserve_total=True)
                dividend = round_money(Decimal(str(listing.get('dividend_per_share') or 0)) * factor, places)
                liquidation = round_money(Decimal(str(listing.get('liquidation_value_per_share') or 0)) * factor, places)
                conn.execute('UPDATE company SET share_price=?,total_shares=?,listed_shares=?,dividend_per_share=?,liquidation_value_per_share=? WHERE company_code=?',
                             (new_price, new_shares, new_listed, dividend, liquidation, code))
                conn.execute('UPDATE fund_components SET base_price=base_price*? WHERE asset_code=?', (float(factor), code))
                self._reset_market_state(conn, code, new_price, new_shares)
                # Split-adjust earlier history so charts and 24h change stay continuous.
                conn.execute('UPDATE market_price_history SET price=ROUND(price*?,?) WHERE asset_code=? AND recorded_epoch<?',
                             (float(factor), places, code, now))
                conn.execute('INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES (?,?,?) ON CONFLICT(asset_code,recorded_epoch) DO UPDATE SET price=excluded.price', (code, new_price, now))
                UserManager._refresh_max_shareholder(conn, code)
                record_id = self._record(conn, f'face:{uuid.uuid4().hex}', 'face_value_adjustment', code, None,
                             {'action': action, 'mode': mode, 'ratio': f'{conversion.denominator}:{conversion.numerator}' if mode == 'ratio_split' else ratio, 'old_shares_per_ratio': conversion.denominator, 'new_shares_per_ratio': conversion.numerator, 'resolution_id': resolution_id,
                              'old_price': price, 'new_price': new_price, 'old_shares': shares, 'new_shares': new_shares}, now)
                ShareRestructuringManager.finish(conn, actor, listing, mode, ratio, resolution, record_id, now)
            label = {'par_split': '정식 액면분할', 'stock_split': '주식분할', 'reverse_split': '액면병합', 'ratio_split': '일반비율분할'}[mode]
            direction = f'{conversion.denominator:,}주 → {conversion.numerator:,}주'
            return True, f'✅ `{code}` {label} 완료 · {direction} · 가격 {price:,} → {new_price:,} · 발행주식수 {shares:,} → {new_shares:,} · 자본금 유지'
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f'❌ {exc}'

    def _adjust_share_ratio(self, actor, code, old_shares, new_shares):
        """Convert an explicit old:new ratio without discarding shares or cost basis."""
        from functions.money import currency_precision, to_minor_units, from_minor_units
        from managers.asset_cost_manager import AssetCostManager
        try:
            old = self._integer(old_shares, '기존 주식 수', 1)
            new = self._integer(new_shares, '변경 주식 수', 1)
            conversion = Fraction(new, old)
            if conversion == 1:
                raise ValueError('기존 주식과 변경 주식의 비율은 달라야 합니다.')
            now = int(time.time())
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                listing = self._listing(conn, code)
                if listing['table'] != 'company' or listing['status'] != 'active':
                    raise ValueError('활성 주식 종목만 주식 수를 조정할 수 있습니다.')
                if listing.get('par_value_minor_units') is not None:
                    raise ValueError('액면가 등록 법인은 정식 액면분할 승인을 사용해주세요.')
                if conn.execute("SELECT 1 FROM depositary_receipts WHERE underlying_code=? AND program_status<>'terminated' LIMIT 1", (code,)).fetchone():
                    raise ValueError('기초주식의 미종료 DR 프로그램을 먼저 상환·종료해주세요.')
                self._unencumbered(conn, [code], allow_orders=False)
                if conn.execute("SELECT 1 FROM market_orders WHERE asset_code=? AND status='scheduled' LIMIT 1", (code,)).fetchone():
                    raise ValueError('예약 주문을 먼저 취소해주세요.')

                def quantity(value):
                    result = Fraction(str(value)) * conversion
                    if result.denominator != 1:
                        raise ValueError('변경 후 발행·상장·보유 수량은 정수 주식이어야 합니다. 단주는 버리지 않습니다.')
                    if result > 2**53 - 1:
                        raise ValueError('변경 후 주식 수가 수량 한도를 초과합니다.')
                    return result.numerator

                shares = quantity(listing['total_shares'])
                listed = None if listing.get('listed_shares') is None else quantity(listing['listed_shares'])
                places = currency_precision(conn, listing.get('quote_currency') or base_currency_code(conn))
                price_minor = to_minor_units(listing['share_price'], places) * conversion.denominator // conversion.numerator
                if price_minor < 1:
                    raise ValueError('변경 후 주가가 통화 최소단위 미만입니다.')
                price = from_minor_units(price_minor, places)
                if price > self.MAX_MONEY:
                    raise ValueError('변경 후 주가가 금액 한도를 초과합니다.')
                factor = Decimal(conversion.denominator) / Decimal(conversion.numerator)
                exact_basis = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_cost_basis'").fetchone())
                for lot in conn.execute('SELECT holding_id,quantity,average_cost_minor_units FROM user_assets WHERE asset_code=?', (code,)):
                    count = quantity(lot['quantity'])
                    if count == 0:
                        continue
                    cost = from_minor_units(int(lot['average_cost_minor_units'] or 0), places)
                    if exact_basis:
                        AssetCostManager.reprice(conn, lot['holding_id'], code, count, cost * factor, preserve_total=True)
                    else:
                        unit_basis = Fraction(str(lot['quantity'])) * int(lot['average_cost_minor_units'] or 0) / count
                        if unit_basis.denominator != 1:
                            raise ValueError('총 취득원가를 정확히 보존할 수 없습니다. 정밀 원가 장부를 먼저 적용해주세요.')
                        conn.execute('UPDATE user_assets SET quantity=?,average_cost_minor_units=? WHERE holding_id=?',
                                     (count, unit_basis.numerator, lot['holding_id']))
                dividend = from_minor_units(to_minor_units(Decimal(str(listing.get('dividend_per_share') or 0)) * factor, places), places)
                liquidation = from_minor_units(to_minor_units(Decimal(str(listing.get('liquidation_value_per_share') or 0)) * factor, places), places)
                conn.execute('UPDATE company SET share_price=?,total_shares=?,listed_shares=?,dividend_per_share=?,liquidation_value_per_share=? WHERE company_code=?',
                             (ExactDecimal(price), shares, listed, ExactDecimal(dividend), ExactDecimal(liquidation), code))
                conn.execute('UPDATE fund_components SET base_price=base_price*? WHERE asset_code=?', (ExactDecimal(factor), code))
                self._reset_market_state(conn, code, ExactDecimal(price), shares)
                conn.execute('UPDATE market_price_history SET price=ROUND(price*?,?) WHERE asset_code=? AND recorded_epoch<?', (ExactDecimal(factor), places, code, now))
                UserManager._refresh_max_shareholder(conn, code)
                self._record(conn, f'face:{uuid.uuid4().hex}', 'face_value_adjustment', code, None,
                             {'mode': 'ratio_split', 'old_ratio': conversion.denominator, 'new_ratio': conversion.numerator,
                              'old_shares': listing['total_shares'], 'new_shares': shares, 'old_price': listing['share_price'], 'new_price': ExactDecimal(price)}, now)
            return True, f'✅ `{code}` 일반비율분할 완료 · {old}주 → {new}주 · 발행주식 {shares:,}주 · 자본금 유지'
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
                from managers.asset_cost_manager import AssetCostManager
                for asset_code in (surviving_code, absorbed_code):
                    AssetCostManager.require_unit_cost_support(conn, asset_code=asset_code)
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
                currency = a.get('quote_currency') or base_currency_code(conn)
                if currency != (b.get('quote_currency') or base_currency_code(conn)):
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
                if new_supply > 2**53 - 1 or cash > self.MAX_MONEY:
                    raise ValueError('통합 후 발행량 또는 단주 정산금이 한도를 초과합니다.')
                # The listing country's treasury pays odd-lot cash in the instrument currency.
                CurrencyManager.treasury_pay(conn, CurrencyManager.exchange_treasury(conn, surviving_code), currency, cash * SCALE)
                for user, user_lots, allocation, residual in conversions:
                    for lot, units in zip(user_lots, allocation):
                        if units:
                            from functions.money import currency_precision, from_minor_units
                            from managers.asset_cost_manager import AssetCostManager
                            currency = AssetCostManager.currency(conn, surviving_code)
                            places = currency_precision(conn, currency)
                            old_cost = from_minor_units(lot['average_cost_minor_units'] or 0, places)
                            ratio_decimal = Decimal(ratio.numerator) / Decimal(ratio.denominator)
                            cost = max(Decimal(0), old_cost / ratio_decimal)
                            AssetCostManager.upsert(
                                conn, user, surviving_code, lot['account_number'], units, cost
                            )
                    if residual and not CurrencyManager.is_treasury(conn, user):
                        if currency == CurrencyManager.home(conn, user):
                            CurrencyManager.change(conn, user, currency, residual * SCALE)
                        else:
                            CurrencyExchangeManager._settle(conn, user, now)
                            account = self._currency_account(conn, user, currency, now)
                            from managers.deposit_accounts import PRINCIPAL_DELTA
                            changed = conn.execute(f"UPDATE accounts SET balance_units=balance_units+?,{PRINCIPAL_DELTA} WHERE account_number=? AND account_type='saving' AND balance_units<=?", (residual * SCALE, residual * SCALE, account, MAX_UNITS - residual * SCALE))
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
    def _split_ratio(text):
        """Share of the parent that moves to the new company, e.g. '30' or '30%' -> 3/10."""
        try:
            percent = Decimal(str(text).strip().removesuffix('%'))
            if not percent.is_finite() or not 0 < percent < 100 or percent != percent.quantize(Decimal('0.01')):
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise ValueError('분할비율은 0 초과 100 미만의 %로 입력해주세요 (소수점 둘째 자리까지). 예: 30%') from None
        return Fraction(percent) / 100

    # Positions a company account may hold that a split cannot divide; settle them first.
    SPLIT_BLOCKERS = (
        ('market_orders', "user_id=? AND status IN ('scheduled','open','partial')", '미체결 주문'),
        ('futures_positions', 'user_id=?', '선물 포지션'),
        ('option_collateral_lots', 'writer_id=?', '옵션 담보 주식'),
        ('option_collateral_cash_lots', 'writer_id=?', '옵션 담보 현금'),
        ('els_subscriptions', "user_id=? AND status IN ('subscribed','issued')", 'ELS 청약'),
        ('treasury_bond_holdings', "user_id=? AND status='active'", '국채 보유'),
        ('loan_collateral_assets', 'user_id=?', '대출 담보로 잡힌 자산'),
    )

    @staticmethod
    def _part(value, ratio, places=0):
        """The split ratio's share of an amount, rounded to the currency's minor unit."""
        scale = 10 ** int(places)
        return ExactDecimal(round(Fraction(str(value or 0)) * scale * ratio)) / scale if places else int(round(Fraction(str(value or 0)) * ratio))

    def _split_account(self, conn, source, target, ratio, now):
        """Move the split ratio of a company account's securities, deposits and loans to ``target``."""
        from managers.interest_manager import InterestManager
        from managers.bank_manager import BankManager
        for table, where, label in self.SPLIT_BLOCKERS:
            if self._has_table(conn, table) and conn.execute(f'SELECT 1 FROM {table} WHERE {where} LIMIT 1', (source,)).fetchone():
                raise ValueError(f'분할 회사 계정의 {label}을(를) 먼저 정리해주세요.')
        moved = {'securities': 0, 'deposits': 0, 'loans': 0}
        # Securities, own shares included: each lot keeps its cost basis.
        for lot in conn.execute('SELECT * FROM user_assets WHERE user_id=? ORDER BY holding_id', (source,)).fetchall():
            quantity = Fraction(str(lot['quantity']))
            part = quantity * ratio
            part = Fraction(round(part)) if quantity.denominator == 1 else Fraction(round(part * 1000), 1000)
            if not part:
                continue
            from managers.asset_cost_manager import AssetCostManager
            moved_basis = AssetCostManager.sale_cost_minor(conn, lot['holding_id'], Decimal(part.numerator) / Decimal(part.denominator))
            left = quantity - part
            if left:
                conn.execute('UPDATE user_assets SET quantity=? WHERE holding_id=?',
                             (int(left) if left.denominator == 1 else float(left), lot['holding_id']))
            else:
                conn.execute('DELETE FROM user_assets WHERE holding_id=?', (lot['holding_id'],))
            from functions.money import currency_precision, from_minor_units
            from managers.asset_cost_manager import AssetCostManager
            currency = AssetCostManager.currency(conn, lot['asset_code'])
            cost = from_minor_units(
                lot['average_cost_minor_units'] or 0, currency_precision(conn, currency)
            )
            AssetCostManager.upsert(
                conn, target, lot['asset_code'], lot['account_number'],
                int(part) if part.denominator == 1 else float(part), cost, incoming_total_minor=moved_basis,
            )
            moved['securities'] += 1
        # Deposits: pay the interest earned so far, then open a sibling account on the same terms.
        quantum = {0: 1_000_000, 1: 100_000}
        for row in conn.execute('SELECT * FROM accounts WHERE user_id=? ORDER BY account_number', (source,)).fetchall():
            if (row['resolution_status'] or 'active') != 'active' or (self._has_table(conn, 'deposit_insurance_claims') and conn.execute(
                    'SELECT 1 FROM deposit_insurance_claims WHERE account_number=? LIMIT 1', (row['account_number'],)).fetchone()):
                raise ValueError(f'정리 중인 예금 계좌 {row["account_number"]}가 있어 분할할 수 없습니다.')
            if conn.execute('SELECT 1 FROM saving WHERE product_code=?', (row['product_code'],)).fetchone():
                InterestManager._settle_saving_account(conn, row, now)
            account = dict(conn.execute('SELECT * FROM accounts WHERE account_number=?', (row['account_number'],)).fetchone())
            step = quantum.get(int(account['balance_decimal_places'] or 0), 10_000)
            units = int(account['balance_units'] or 0) * ratio // step * step
            principal = self._part(account['principal'], ratio)
            if units <= 0 and not principal:
                continue
            sibling = dict(account, account_number=BankManager._generate_account_number(conn), user_id=target,
                           balance_units=int(units), balance=ExactDecimal(units) / SCALE, principal=principal, interest_remainder='0')
            for derived in ('balance_minor_units', 'created_at'):
                sibling.pop(derived, None)
            columns = ','.join(sibling)
            conn.execute(f'INSERT INTO accounts({columns}) VALUES ({",".join("?" for _ in sibling)})', tuple(sibling.values()))
            conn.execute('UPDATE accounts SET balance_units=balance_units-?,principal=principal-? WHERE account_number=?',
                         (int(units), principal, account['account_number']))
            moved['deposits'] += 1
        # Loans in good standing: every amount and every installment is divided by the ratio.
        loan_money = ('principal', 'outstanding_balance', 'origination_fee', 'mandatory_cost_fee',
                      'provisioned_loss', 'ecl_allowance', 'prepayment_fees_paid')
        schedule_money = ('principal_due', 'interest_due', 'fee_due', 'amount_paid', 'principal_paid', 'interest_paid', 'fee_paid')
        for loan in conn.execute("SELECT * FROM loans WHERE user_id=? AND status NOT IN ('closed','repaid','paid')", (source,)).fetchall():
            loan = dict(loan)
            linked = [table for table in ('loan_credit_support', 'loan_future_commitments', 'loan_chargeoff_events')
                      if self._has_table(conn, table) and conn.execute(f'SELECT 1 FROM {table} WHERE loan_id=? LIMIT 1', (loan['loan_id'],)).fetchone()]
            if (loan['status'] != 'active' or loan['collection_status'] != 'current' or loan['overdue_since_epoch']
                    or int(loan['collateral_amount'] or 0) or ExactDecimal(loan['charged_off_balance'] or 0) or linked):
                raise ValueError(f'대출 #{loan["loan_id"]}이 연체·담보·상각·보증 상태라 분할할 수 없습니다. 먼저 정리해주세요.')
            places = int((conn.execute('SELECT decimal_places FROM currencies WHERE code=?', (loan['quote_currency'] or base_currency_code(conn),)).fetchone() or [0])[0])
            parts = {column: self._part(loan[column], ratio, places) for column in loan_money if column in loan}
            sibling = {k: v for k, v in loan.items() if k != 'loan_id' and not k.endswith(('_minor_units', '_subminor_units'))}
            sibling.update(parts, user_id=target, interest_remainder=0, overdue_interest_remainder=0)
            new_loan = conn.execute(f'INSERT INTO loans({",".join(sibling)}) VALUES ({",".join("?" for _ in sibling)})',
                                    tuple(sibling.values())).lastrowid
            conn.execute(f'UPDATE loans SET {",".join(f"{c}={c}-?" for c in parts)} WHERE loan_id=?', (*parts.values(), loan['loan_id']))
            for item in conn.execute('SELECT * FROM loan_payment_schedule WHERE loan_id=? ORDER BY installment_number', (loan['loan_id'],)).fetchall():
                item = dict(item)
                share = {column: self._part(item[column], ratio, places) for column in schedule_money}
                copy = {k: v for k, v in item.items() if k != 'schedule_id' and not k.endswith('_minor_units')}
                copy.update(share, loan_id=new_loan)
                conn.execute(f'INSERT INTO loan_payment_schedule({",".join(copy)}) VALUES ({",".join("?" for _ in copy)})', tuple(copy.values()))
                conn.execute(f'UPDATE loan_payment_schedule SET {",".join(f"{c}={c}-?" for c in share)} WHERE schedule_id=?',
                             (*share.values(), item['schedule_id']))
            moved['loans'] += 1
        return moved

    def split(self, actor, kind, code, new_code, new_name, ratio_text, new_shares=None):
        """인적분할 (spin-off) or 물적분할 (split-off) of a listed common-stock company.

        The split ratio of everything the parent owns and owes moves to a new listed company:
        issuer cash and capital, and its company account's securities, deposits and loans.
        인적: the parent's shareholders receive the new shares pro rata and the parent's price
        drops by the same share. 물적: the parent's company account owns every new share at the
        book value it handed over, so the parent's price is unchanged.
        """
        try:
            kind = str(kind).strip().removesuffix('분할')
            if kind not in {'인적', '물적'}:
                raise ValueError('분할 유형은 인적 또는 물적입니다.')
            code, new_code, new_name = str(code).strip(), str(new_code).strip(), str(new_name).strip()
            if not new_code or len(new_code) > 80 or any(ch.isspace() for ch in new_code):
                raise ValueError('신설회사 id는 공백 없는 1~80자로 입력해주세요.')
            if not new_name or len(new_name) > 80:
                raise ValueError('신설회사 이름은 1~80자로 입력해주세요.')
            ratio = self._split_ratio(ratio_text)
            now = int(time.time())
            from managers.company_account_manager import CompanyAccounts
            with DBManager.admin_scope(actor), self.db.atomic() as conn:
                self._require(conn, actor)
                listing = self._listing(conn, code)
                if listing['table'] != 'company' or listing['status'] != 'active' or listing['share_class'] != 'common':
                    raise ValueError('분할은 거래 중인 보통주 법인에만 사용할 수 있습니다.')
                if DBManager.code_locations(conn, new_code):
                    raise ValueError(f'`{new_code}`는 이미 사용 중인 코드입니다.')
                if conn.execute("SELECT 1 FROM depositary_receipts WHERE underlying_code=? "
                                "AND program_status<>'terminated' LIMIT 1", (code,)).fetchone():
                    raise ValueError('기초주식에 미종료 DR 프로그램이 있습니다. 먼저 DR을 상환하고 프로그램을 종료해주세요.')
                from managers.asset_cost_manager import AssetCostManager
                AssetCostManager.require_unit_cost_support(conn, asset_code=code)
                self._unencumbered(conn, [code])
                price, supply = int(listing['share_price']), int(listing['total_shares'] or 0)
                shares = supply if new_shares is None else self._integer(new_shares, '신설회사 발행주식수', 1)
                if min(price, supply) <= 0:
                    raise ValueError('가격과 발행주식수가 양수인 법인만 분할할 수 있습니다.')
                currency = listing.get('quote_currency') or base_currency_code(conn)
                new_price = max(1, round(Fraction(price * supply) * ratio / shares))
                capital = int(listing['capital'] or 0)
                moved_capital = round(capital * ratio)
                Finance.ensure(conn, code, now)
                entity = Finance.entity(conn, code)
                places = int(entity['decimal_places'])
                cash = conn.execute('SELECT balance,locked_amount FROM issuer_cash_accounts WHERE company_code=?', (code,)).fetchone()
                if cash and int(cash['locked_amount']):
                    raise ValueError('법인의 잠금 예치금을 먼저 정산해주세요.')
                moved_cash = round(int(cash['balance']) * ratio) if cash and int(cash['balance']) > 0 else 0
                parent_account = CompanyAccounts.account_id(conn, code) if kind == '물적' else \
                    (conn.execute('SELECT user_id FROM company_accounts WHERE company_code=?', (code,)).fetchone() or [None])[0]
                # The statements follow the live accounts (기준 통화 books); other currencies move cash only.
                live_books = entity['currency'] == base_currency_code(conn)
                if parent_account and live_books:
                    CompanyAccounts.reconcile(conn, code)
                before = CompanyAccounts.positions(conn, parent_account, code) if parent_account else None
                if parent_account and not live_books and any(before.values()):
                    raise ValueError('기준 통화가 아닌 통화로 장부를 쓰는 법인은 법인 계정의 증권·예금·대출을 먼저 정리해야 분할할 수 있습니다.')

                # The new company keeps the parent's market and simulation settings, no dividend schedule.
                conn.execute(
                    'INSERT INTO company(company_code,name,industry,capital,total_shares,share_price,max_shareholder_id,'
                    'exchange_id,status,is_fixed,market_condition,market_volatility,quote_currency,liquidity,'
                    'trend_sensitivity,sector_sensitivity,jump_intensity,mean_reversion,Fractal) '
                    "VALUES (?,?,?,0,?,?,?,?,'active',?,?,?,?,?,?,?,?,?,?)",
                    (new_code, new_name, listing['industry'], shares, new_price, TREASURY_USER_ID,
                     listing['exchange_id'], listing['is_fixed'], listing['market_condition'], listing['market_volatility'],
                     currency, listing['liquidity'], listing['trend_sensitivity'], listing['sector_sensitivity'],
                     listing['jump_intensity'], listing['mean_reversion'], listing['Fractal']))
                new_account = CompanyAccounts.account_id(conn, new_code)  # opens the new company's empty ledger too
                conn.execute('UPDATE company SET capital=? WHERE company_code=?', (moved_capital, new_code))
                if moved_cash:
                    conn.execute('UPDATE issuer_cash_accounts SET balance=balance-? WHERE company_code=?', (moved_cash, code))
                    conn.execute('UPDATE issuer_cash_accounts SET balance=balance+? WHERE company_code=?', (moved_cash, new_code))
                moved = self._split_account(conn, parent_account, new_account, ratio, now) if parent_account \
                    else {'securities': 0, 'deposits': 0, 'loans': 0}
                after = CompanyAccounts.positions(conn, parent_account, code) if parent_account else None
                # Book value handed over, in 기준 통화: cash plus assets less debts that left the parent.
                net_moved = moved_cash + ((before['investments'] - after['investments']) + (before['treasury'] - after['treasury'])
                                          - (before['debt'] - after['debt']) if parent_account else 0)

                cash_units = Finance.converted(conn, moved_cash, base_currency_code(conn), entity['currency'], places)
                capital_units = Finance.converted(conn, moved_capital, currency, entity['currency'], places)
                label = f'{kind}분할'
                allocations = []
                if kind == '인적':
                    conn.execute('UPDATE company SET capital=? WHERE company_code=?', (capital - moved_capital, code))
                    lots = conn.execute('SELECT * FROM user_assets WHERE asset_code=? ORDER BY holding_id', (code,)).fetchall()
                    owned = sum(Fraction(str(lot['quantity'])) for lot in lots)
                    if owned > supply:
                        raise ValueError('보유 수량이 총 발행량을 초과합니다.')
                    scale = 1000 if listing.get('Fractal') else 1
                    weights = [lot['quantity'] for lot in lots] + [supply - owned]
                    allocated = [Fraction(value, scale) for value in self._allocate(shares * scale, weights)]
                    for lot, units in zip(lots, allocated):
                        quantity = Fraction(str(lot['quantity']))
                        from functions.money import currency_precision, from_minor_units
                        from managers.asset_cost_manager import AssetCostManager
                        old_currency = AssetCostManager.currency(conn, code)
                        old_places = currency_precision(conn, old_currency)
                        cost = from_minor_units(lot['average_cost_minor_units'] or 0, old_places)
                        parent_cost = cost * (
                            Decimal(1) - Decimal(ratio.numerator) / Decimal(ratio.denominator)
                        )
                        AssetCostManager.reprice(
                            conn, lot['holding_id'], code, quantity, parent_cost
                        )
                        if units:
                            new_cost = cost * Decimal(ratio.numerator) / Decimal(ratio.denominator)
                            new_cost = new_cost * Decimal(quantity.numerator) / Decimal(quantity.denominator)
                            new_cost /= Decimal(units.numerator) / Decimal(units.denominator)
                            AssetCostManager.upsert(
                                conn, lot['user_id'], new_code, lot['account_number'],
                                float(units) if scale > 1 else int(units), new_cost,
                            )
                        allocations.append({'user_id': lot['user_id'], 'account_number': lot['account_number'],
                                            'parent_quantity': float(quantity), 'new_shares': float(units)})
                    if allocated[-1]:
                        # Unissued parent shares sit with the listing treasury, as in a bonus issue.
                        AssetCostManager.upsert(
                            conn, CurrencyManager.exchange_treasury(conn, code), new_code,
                            None, float(allocated[-1]) if scale > 1 else int(allocated[-1]), 0,
                        )
                    parent_price = max(1, round(price * (1 - ratio)))
                    conn.execute('UPDATE company SET share_price=? WHERE company_code=?', (parent_price, code))
                    conn.execute('UPDATE fund_components SET base_price=base_price*?/ ? WHERE asset_code=?', (parent_price, price, code))
                    self._reset_market_state(conn, code, parent_price, supply)
                    conn.execute('INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES (?,?,?) '
                                 'ON CONFLICT(asset_code,recorded_epoch) DO UPDATE SET price=excluded.price', (code, parent_price, now))
                    owner_text = f'주주 {len({lot["user_id"] for lot in lots}):,}명에게 비례 배정'
                else:
                    # The parent holds every new share at the book value it handed over.
                    from managers.asset_cost_manager import AssetCostManager
                    parent_cost = max(0, round(Fraction(str(net_moved)) / shares)) if live_books else new_price
                    AssetCostManager.upsert(
                        conn, parent_account, new_code, None, shares, parent_cost,
                    )
                    parent_price = price
                    owner_text = f'`{code}` 법인이 100% 보유 (자회사)'

                # Statements: books move to the live accounts; what left the parent is equity
                # (인적) or the subsidiary holding (물적); the new company's net assets are its equity.
                if live_books:
                    CompanyAccounts.reconcile(conn, code, residual='adjustment')
                    CompanyAccounts.reconcile(conn, new_code, residual='capital_surplus')
                    if kind == '인적' and capital_units:
                        Finance.post(conn, code, f'split:capital:{new_code}:{now}', f'{label} · 자본금 이전',
                                     [('common_capital', capital_units), ('adjustment', -capital_units)], 'financing', now)
                    if capital_units:
                        Finance.post(conn, new_code, f'split:capital:{code}:{now}', f'{label} 신설 · 자본금 설정',
                                     [('capital_surplus', capital_units), ('common_capital', -capital_units)], 'financing', now)
                else:
                    Finance.post(conn, new_code, f'split:in:{code}:{now}', f'{label} 신설 · `{code}`에서 이전',
                                 [('cash', cash_units), ('common_capital', -capital_units),
                                  ('capital_surplus', capital_units - cash_units)], 'financing', now)
                    Finance.post(conn, code, f'split:out:{new_code}:{now}', f'{label} · `{new_code}` 신설',
                                 [('cash', -cash_units)] + ([('common_capital', capital_units), ('adjustment', cash_units - capital_units)]
                                                            if kind == '인적' else [('investments', cash_units)]),
                                 'financing' if kind == '인적' else 'investing', now)
                conn.execute('INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES (?,?,?) '
                             'ON CONFLICT(asset_code,recorded_epoch) DO UPDATE SET price=excluded.price', (new_code, new_price, now))
                Finance.record_shares(conn, new_code, now)
                UserManager._refresh_max_shareholder(conn, new_code)
                UserManager._refresh_max_shareholder(conn, code)
                payload = {'kind': kind, 'parent': code, 'new': new_code, 'name': new_name, 'ratio': str(ratio),
                           'new_shares': shares, 'new_price': new_price, 'old_price': price, 'parent_price': parent_price,
                           'moved_cash': moved_cash, 'moved_capital': moved_capital, 'moved_book_value_nwx': round(net_moved),
                           'moved_positions': moved, 'currency': currency, 'allocations': allocations}
                self._record(conn, f'split:{uuid.uuid4().hex}', 'spin_off' if kind == '인적' else 'split_off', code, new_code, payload, now)
                self._audit(conn, actor, 'split', code, payload, now)
            percent = f'{float(ratio * 100):g}%'
            positions = ' · '.join(f'{label} {moved[key]:,}건' for key, label in
                                   (('securities', '보유 증권'), ('deposits', '예금'), ('loans', '대출')) if moved[key])
            return True, (f'✅ `{code}` {kind}분할 완료 · 신설 `{new_code}` {new_name} ({percent}) · '
                          f'{shares:,}주 × {new_price:,} {currency} · {owner_text} · '
                          f'`{code}` 가격 {price:,} → {parent_price:,} · 이전 현금 {moved_cash:,} 기준 통화 · 자본금 {moved_capital:,}'
                          + (f' · 함께 이전: {positions}' if positions else ''))
        except (ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
            return False, f'❌ {exc}'

    @staticmethod
    def _merge_components(conn, a, b, price_a, price_b):
        for row in conn.execute('SELECT * FROM fund_components WHERE asset_code=?', (b,)).fetchall():
            existing = conn.execute('SELECT * FROM fund_components WHERE product_code=? AND asset_code=?', (row['product_code'], a)).fetchone()
            converted_base = ExactDecimal(row['base_price']) * price_a / price_b
            if existing:
                weight = float(existing['weight']) + float(row['weight'])
                # Harmonic basis keeps both pre-merger index contributions equal.
                basis = weight / (float(existing['weight']) / ExactDecimal(existing['base_price']) + float(row['weight']) / converted_base)
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
                if conn.execute('SELECT 1 FROM accounts WHERE product_code=? LIMIT 1', (b,)).fetchone():
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
