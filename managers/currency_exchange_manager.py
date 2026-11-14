"""Atomic currency exchange. Foreign amounts are integer millionths of a unit."""
from functions.exact_money import ExactDecimal
from functions.amount_limits import MAX_NATIVE, MAX_MINOR
from functions.money import base_currency_code
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from database.user_db import TREASURY_USER_ID
from managers.currency_manager import CurrencyManager
from managers.deposit_accounts import PRINCIPAL_DELTA

SCALE = 1_000_000
MAX_UNITS = MAX_MINOR

def amount_units(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount.copy_abs() > MAX_NATIVE:
            raise ValueError()
        units = amount * SCALE
        if not amount.is_finite() or units != units.to_integral_value() or not 1 <= units <= MAX_UNITS:
            raise ValueError()
        return int(units)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("금액은 소수점 6자리 이내의 양수로 입력해주세요.") from None

def display_units(units, decimal_places=None):
    """Format stored units; with ``decimal_places`` show exactly that precision."""
    value = Decimal(units) / SCALE
    if decimal_places is None:
        return f"{value:,.6f}".rstrip('0').rstrip('.')
    places = max(0, min(6, int(decimal_places)))
    return f"{value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN):,.{places}f}"

class CurrencyExchangeManager:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _rate(conn, code):
        value = CurrencyManager.rate(conn, code) / CurrencyManager.rate(conn, base_currency_code(conn))
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
            return user['nationality'], row['currency_code'] if row else base_currency_code(conn)

    def rates(self):
        with self.db.readonly() as conn:
            return [{**dict(r), 'rate': self._rate(conn, r['code'])}
                    for r in conn.execute("SELECT * FROM currencies WHERE status='active' ORDER BY code")]

    def open_account(self, user_id, product_code, password=None, deposit_amount=0):
        """Open a foreign-currency saving account.

        Citizens open it like any saving (password, at least the product's minimum
        deposit, paid from home cash at the current rate). System callers such as
        the treasury pass no password and open it empty.
        """
        from fractions import Fraction
        from managers.bank_manager import BankManager
        from functions.money import parse_currency_money
        if password is not None and (not isinstance(password, str) or not 4 <= len(password) <= 16):
            raise ValueError("비밀번호는 4~16자로 입력해주세요.")
        from managers.compliance_manager import ComplianceManager
        ComplianceManager.check_financial_eligibility(
            self.db, user_id, "deposit_open"
        )
        with self.db.atomic() as conn:
            self._user(conn, user_id)
            ComplianceManager.require_financial_eligibility(conn, user_id)
            product = conn.execute("SELECT * FROM saving WHERE product_code=? AND product_kind='saving' AND status='active'", (product_code,)).fetchone()
            if not product:
                raise ValueError("저축 상품 코드를 확인해주세요.")
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
            deposit_units = cost_units = 0
            if password is not None:
                deposit = parse_currency_money(conn, deposit_amount or 0, product['quote_currency'], minimum=0)
                minimum = ExactDecimal(product['deposit_amount'] or 0)
                if deposit < minimum:
                    raise ValueError(f"최소 예치금은 {minimum:,g} {product['quote_currency']}입니다.")
                deposit_units = amount_units(deposit) if deposit > 0 else 0
                if deposit_units:
                    # Paid from home cash at the current rate, rounded up to the home minor unit.
                    exact = Fraction(deposit_units) * CurrencyManager.quote(conn, product['quote_currency'], home)
                    quantum = 10 ** (6 - CurrencyManager.precision(conn, home))
                    cost_units = -(-int(-(-exact.numerator // exact.denominator)) // quantum) * quantum
                    if CurrencyManager.balance(conn, user_id, home) < cost_units:
                        raise ValueError(f"현금이 부족합니다. 필요 금액 {display_units(cost_units)} {home}")
                    CurrencyManager.change(conn, user_id, home, -cost_units)
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
                         "contract_effective_rate,deposit_insured) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (number, str(user_id), product_code, 0, None, opened_epoch, 0,
                          'saving', product['quote_currency'], float(product['interest_rate']),
                          product['rate_type'], product['rate_nationality'],
                          contract_spread, float(product['rate_floor']),
                          product['rate_cap'], int(product['repricing_cycle_seconds']),
                          opened_epoch+int(product['repricing_cycle_seconds'])
                          if product['rate_type']=='floating' else None,
                          product['benchmark_key'],
                          int(product['benchmark_tenor_days'] or product['maturity_days'] or 0),
                          int(product['repricing_lag_seconds'] or 0),
                          self._effective_rate(product), int(product['deposit_insured'] or 0)))
            if password is not None:
                conn.execute("UPDATE accounts SET password=?,principal=? WHERE account_number=?",
                             (BankManager._hash_password(password), ExactDecimal(deposit_units) / SCALE, number))
            if deposit_units:
                conn.execute("UPDATE accounts SET balance_units=? WHERE account_number=?", (deposit_units, number))
                conn.execute('INSERT INTO currency_transactions(user_id,source_currency,target_currency,source_units,target_units,fee_nwx,recorded_epoch) VALUES (?,?,?,?,?,?,?)',
                             (str(user_id), home, product['quote_currency'], cost_units, deposit_units, 0, opened_epoch))
                CurrencyManager.ensure_liquidity_state(conn, product['quote_currency'])
                # The opening deposit is a foreign-currency liability funded by
                # home-currency cash. Keep both currency legs balanced separately
                # through the bank's FX conversion clearing account.
                from managers.bank_journal import BankJournal
                BankJournal.post(
                    conn, product['issuer'], "fx_account_open_funding",
                    f"account:{number}:funding:home", home,
                    [("settlement_asset", "debit", Decimal(cost_units) / SCALE),
                     ("fx_conversion_clearing", "credit", Decimal(cost_units) / SCALE)],
                    description=f"Foreign-currency account {number} funded from home cash",
                    now=opened_epoch,
                )
                BankJournal.post(
                    conn, product['issuer'], "fx_account_open_funding",
                    f"account:{number}:funding:{product['quote_currency']}",
                    product['quote_currency'],
                    [("fx_conversion_clearing", "debit", Decimal(deposit_units) / SCALE),
                     ("customer_deposits_payable", "credit", Decimal(deposit_units) / SCALE)],
                    description=f"Foreign-currency account {number} opened",
                    now=opened_epoch,
                )
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
        """Pay due interest on a user's foreign-currency accounts with savings rules."""
        from managers.interest_manager import InterestManager
        rows = conn.execute("SELECT a.* FROM accounts a "
                            "WHERE a.user_id=? AND a.account_type='saving' AND a.quote_currency<>? "
                            "AND a.resolution_status='active'", (str(user_id),CurrencyManager.home(conn,user_id))).fetchall()
        for row in rows:
            InterestManager._settle_saving_account(conn, row, now)

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
            CurrencyManager.validate_units(conn, units, source)
            self._settle(conn, user_id, now)
            fee_rate = Decimal(str(conn.execute("SELECT value FROM settings WHERE key='currency_exchange_fee_percent'").fetchone()[0]))
            if not fee_rate.is_finite() or not 0 <= fee_rate <= 100:
                raise ValueError("환전 수수료 설정이 올바르지 않습니다.")
            fee = CurrencyManager.fee(conn, units, source, fee_rate)
            output = CurrencyManager.exchange_amount(conn, units, source, target, fee)
            if output <= 0 or output > MAX_UNITS:
                raise ValueError("환전 결과가 최소 단위 미만이거나 한도를 초과합니다.")
            from managers.accounting_journal import AccountingJournal
            before = AccountingJournal.wallet_watermark(conn)
            CurrencyManager.change(conn, user_id, source, -units)
            between = AccountingJournal.wallet_watermark(conn)
            CurrencyManager.change(conn, user_id, target, output)
            after = AccountingJournal.wallet_watermark(conn)
            fee_receipt = CurrencyManager.credit_treasury_fee_receipt(conn, user_id, fee)
            cursor = conn.execute('INSERT INTO currency_transactions(user_id,source_currency,target_currency,source_units,target_units,fee_nwx,recorded_epoch) VALUES (?,?,?,?,?,?,?)',
                (str(user_id),source,target,units,output,fee,now))
            AccountingJournal.post_currency_cash(conn, "exchange", cursor.lastrowid, now, (
                ("source", str(user_id), source, -units, before, between),
                ("target", str(user_id), target, output, between, after),
            ))
            AccountingJournal.post_currency_fee(conn, "exchange", cursor.lastrowid, now,
                                               fee, fee_receipt, after)
            return f"✅ {display_units(units)} {source} → {display_units(output)} {target} · 수수료 {fee:,} 기준 통화"

    def accounts(self, user_id):
        with self.db.atomic() as conn:
            self._user(conn, user_id)
            self._settle(conn, user_id, time.time())
            home = CurrencyManager.home(conn, user_id)
            return [{**dict(r), 'value_nwx': int(Decimal(r['balance_units']) * self._rate(conn,r['currency_code']) / SCALE) if r['balance_units'] else 0}
                    for r in conn.execute("SELECT a.*,a.quote_currency AS currency_code,p.name AS product_name,COALESCE(a.contract_rate,p.interest_rate) AS annual_rate FROM accounts a "
                                          "LEFT JOIN saving p ON p.product_code=a.product_code "
                                          "WHERE a.user_id=? AND a.account_type='saving' AND a.quote_currency<>? "
                                          "ORDER BY a.quote_currency", (str(user_id),home))]

    @staticmethod
    def _source_for_target(conn, target_units, source, target, fee_rate):
        """Return the smallest source-currency amount that funds an exact target credit."""
        quantum = 10 ** (6 - CurrencyManager.precision(conn, source))
        low, high = 1, MAX_UNITS // quantum

        def quote(steps):
            source_units = steps * quantum
            fee = CurrencyManager.fee(conn, source_units, source, fee_rate)
            output = CurrencyManager.exchange_amount(
                conn, source_units, source, target, fee
            )
            return source_units, fee, output

        if quote(high)[2] < target_units:
            raise ValueError('환전 금액이 저장 한도를 초과합니다.')
        while low < high:
            middle = (low + high) // 2
            if quote(middle)[2] >= target_units:
                high = middle
            else:
                low = middle + 1
        source_units, fee, output = quote(low)
        if output < target_units:
            raise ValueError('환전 결과가 최소 단위 미만이거나 한도를 초과합니다.')
        return source_units, fee

    def transfer(self,user_id,recipient,amount,source_account=None):
        """Transfer atomically; self FX-account deposits use destination-currency amounts."""
        sender = str(user_id)
        recipient = str(recipient).strip()
        if recipient.casefold() in {'국고','treasury'}:
            with self.db.atomic() as conn:
                recipient = CurrencyManager.user_treasury_id(conn, sender)
        source_account = str(source_account or '__wallet__').strip()
        requested_units = amount_units(amount)
        from managers.user_manager import UserManager
        from managers.compliance_manager import ComplianceManager
        ComplianceManager.check_financial_eligibility(
            self.db, sender, "transfer"
        )
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
            ComplianceManager.require_financial_eligibility(conn, sender, now=now)
            def endpoint(number,owner=None):
                if number=='__wallet__' and owner is not None:
                    return ('users','cash','user_id',owner,owner,CurrencyManager.home(conn,owner),SCALE)
                currency_account = conn.execute("SELECT *,quote_currency AS currency_code FROM accounts WHERE account_number=? AND account_type='saving'",(number,)).fetchone()
                if currency_account and currency_account['currency_code']==CurrencyManager.home(conn,currency_account['user_id']):
                    currency_account = None
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
            self._settle(conn,sender,now)
            if dst[4]!=sender:
                self._settle(conn,dst[4],now)
            target_basis = (
                dst[0] == 'accounts'
                and dst[1] == 'balance_units'
                and dst[4] == sender
                and src[5] != dst[5]
            )
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
                if target_basis:
                    CurrencyManager.validate_units(conn, requested_units, dst[5])
                    units, fee = self._source_for_target(
                        conn, requested_units, src[5], dst[5], rate
                    )
                    output = requested_units
                else:
                    units = requested_units
                    CurrencyManager.validate_units(conn, units, src[5])
                    fee = CurrencyManager.fee(conn,units,src[5],rate)
                    output = CurrencyManager.exchange_amount(conn,units,src[5],dst[5],fee)
            else:
                units = requested_units
                CurrencyManager.validate_units(conn, units, src[5])
                output = units
            quantum = 10**(6-CurrencyManager.precision(conn,dst[5]))
            output = output//quantum*quantum
            if not 1<=output<=MAX_UNITS:
                raise ValueError('환전 결과가 최소 단위 미만이거나 한도를 초과합니다.')
            debit,credit = units,output

            def native_amount(value_units):
                value = Decimal(int(value_units)) / Decimal(SCALE)
                return int(value) if value == value.to_integral_value() else ExactDecimal(value)

            only_named = False
            if src[0] == 'accounts':
                # 마이너스통장: a payment from it uses its balance and then its limit,
                # never the wallet. The shortfall is lent in before the payment
                # window opens, so the wallet ledger of the payment stays exact.
                from managers.overdraft import cover
                only_named = conn.execute("SELECT account_kind FROM accounts WHERE account_number=?",
                                          (src[3],)).fetchone()[0] == 'combined'
                if only_named:
                    cover(conn, src[3], debit, now)
            from managers.accounting_journal import AccountingJournal
            before = AccountingJournal.wallet_watermark(conn)
            debited_sources = []
            if src[1] in {'cash','balance'}:
                preferred = None if src[0]=='users' else src[3]
                candidates = [(None,int(Decimal(str(conn.execute(
                    "SELECT cash FROM users WHERE user_id=?",(sender,)
                ).fetchone()[0] or 0))*SCALE))]
                candidates += [(str(row['account_number']),int(Decimal(str(row['balance'] or 0))*SCALE)) for row in conn.execute(
                    "SELECT account_number,balance FROM accounts WHERE user_id=? "
                    "AND account_type IN ('saving','loan_payout') AND quote_currency=? "
                    # Only spendable accounts top up a payment; a locked account pays only when named.
                    "AND (COALESCE(account_kind,'demand') IN ('demand','parking','cma','combined') OR account_number=?)",
                    (sender,src[5],preferred)).fetchall()]
                if dst[4]==sender:
                    destination_source = None if dst[0]=='users' else dst[3]
                    candidates = [item for item in candidates if item[0]!=destination_source]
                if only_named:
                    candidates = [item for item in candidates if item[0]==preferred]
                candidates.sort(key=lambda item:(item[0]!=preferred,item[0] is None,-item[1]))
                if sum(max(0,balance) for _,balance in candidates) < debit:
                    debited = False
                else:
                    remaining = debit
                    for account_number,balance in candidates:
                        take = min(max(0,balance),remaining)
                        if take and not UserManager._debit_source(
                            conn, sender, account_number, native_amount(take)
                        ):
                            raise RuntimeError('출금 잔액이 변경되어 송금을 취소했습니다.')
                        if take:
                            debited_sources.append(account_number or '지갑')
                            remaining -= take
                        if not remaining:
                            break
                    debited = remaining == 0
            else:
                principal = f',{PRINCIPAL_DELTA}' if src[0]=='accounts' else ''
                debited = conn.execute(f'UPDATE {src[0]} SET {src[1]}={src[1]}-?{principal} WHERE {src[2]}=? AND {src[1]}>=?',(debit,*((-debit,) if principal else ()),src[3],debit)).rowcount==1
                if debited:
                    debited_sources.append(src[3])
            if not debited:
                raise ValueError('출금 잔액이 부족합니다.')
            between = AccountingJournal.wallet_watermark(conn)
            if dst[0]=='accounts' and dst[1]=='balance':
                native_credit = native_amount(credit)
                changed = conn.execute('UPDATE accounts SET balance_units = balance_units + CAST(ROUND(? * 1000000) AS INTEGER),principal=COALESCE(principal,0)+?,last_payment_epoch=?,last_payment=CURRENT_TIMESTAMP WHERE account_number=? AND balance<=?',(native_credit,native_credit,now,dst[3],MAX_NATIVE-native_credit))
            elif dst[0]=='users' and dst[1]=='cash':
                native_credit = native_amount(credit)
                changed = conn.execute(
                    'UPDATE users SET cash=cash+? WHERE user_id=? AND cash<=?',
                    (native_credit,dst[3],MAX_NATIVE-native_credit),
                )
            else:
                principal = f',{PRINCIPAL_DELTA}' if dst[0]=='accounts' else ''
                changed = conn.execute(f'UPDATE {dst[0]} SET {dst[1]}={dst[1]}+?{principal} WHERE {dst[2]}=? AND {dst[1]}<=?',(credit,*((credit,) if principal else ()),dst[3],MAX_UNITS-credit))
            if changed.rowcount!=1:
                raise ValueError('입금 계좌 잔액 한도를 초과했습니다.')
            after = AccountingJournal.wallet_watermark(conn)
            fee_receipt = CurrencyManager.credit_treasury_fee_receipt(conn, sender, fee)
            destination_label = '국고' if CurrencyManager.is_treasury(conn, dst[4]) else recipient
            transfer_cursor = conn.execute('INSERT INTO currency_transfer_transactions(sender_id,recipient_id,source_account,target_account,source_currency,target_currency,source_units,target_units,fee_nwx,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (sender,dst[4],source_account,recipient,src[5],dst[5],units,output,fee,now))
            transfer_id = int(transfer_cursor.lastrowid)
            AccountingJournal.post_currency_cash(conn, "transfer", transfer_id, now, (
                ("source", sender, src[5], -units, before, between),
                ("target", dst[4], dst[5], output, between, after),
            ))
            AccountingJournal.post_currency_fee(conn, "transfer", transfer_id, now,
                                               fee, fee_receipt, after)
            if sender != dst[4]:
                from managers.compliance_manager import ComplianceManager
                for monitored_user, monitored_units, monitored_currency in (
                    (sender, units, src[5]), (dst[4], output, dst[5]),
                ):
                    if CurrencyManager.is_treasury(conn, monitored_user):
                        continue
                    nationality = conn.execute(
                        "SELECT nationality FROM users.users WHERE user_id=?",
                        (monitored_user,),
                    ).fetchone()
                    if nationality:
                        ComplianceManager.monitor_transfer(
                            conn, user_id=monitored_user,
                            nationality=nationality["nationality"],
                            transaction_id=transfer_id,
                            source_units=monitored_units,
                            source_currency=monitored_currency, now=now,
                        )

            if sender != dst[4]:
                # Company books classify transfers as 영업외 income/expense (and consolidation eliminates intra-group ones).
                from managers.company_account_manager import CompanyAccounts
                if CompanyAccounts.is_company(conn, sender):
                    CompanyAccounts.log_income(conn, sender, 'transfer_out', CurrencyManager.convert(conn, units, src[5], base_currency_code(conn))//SCALE,
                                               counterparty=CompanyAccounts.issuer_of(conn, dst[4]) or dst[4])
                if CompanyAccounts.is_company(conn, dst[4]):
                    CompanyAccounts.log_income(conn, dst[4], 'transfer_in', CurrencyManager.convert(conn, output, dst[5], base_currency_code(conn))//SCALE,
                                               counterparty=CompanyAccounts.issuer_of(conn, sender) or sender)
            sources_label = ', '.join(debited_sources)
            basis_label = (
                f'환전 기준 {display_units(requested_units)} {dst[5]} · '
                if target_basis else ''
            )
            return (f'✅ {basis_label}송금 {display_units(units)} {src[5]} → '
                    f'{display_units(output)} {dst[5]} · 받는 곳 {destination_label} · '
                    f'출금 {sources_label} · 환전 수수료 {fee:,} 기준 통화')
