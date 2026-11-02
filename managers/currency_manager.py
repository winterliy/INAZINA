"""Nationality-relative cash and account-backed foreign currency balances."""
from functions.amount_limits import MAX_NATIVE, MAX_MINOR
from functions.money import base_currency_code
from fractions import Fraction
from contextlib import nullcontext
from decimal import Decimal
import math
import time
from database.user_db import TREASURY_USER_ID

SCALE = 1_000_000
MAX_UNITS = MAX_MINOR

class CurrencyManager:
    @staticmethod
    def base_currency(conn):
        return base_currency_code(conn)

    @staticmethod
    def set_base_currency(conn, currency_code):
        currency = str(currency_code or "").strip().upper()
        row = conn.execute("SELECT decimal_places FROM currencies WHERE code=? AND status='active'",
                           (currency,)).fetchone()
        if not row:
            raise ValueError("활성 기본 통화를 선택해주세요.")
        if int(row[0]) != 0:
            raise ValueError("지갑 기본 잔액의 소수 정밀도 이관 전에는 0자리 통화만 기본 통화로 지정할 수 있습니다.")
        conn.execute("UPDATE currency_system_settings SET base_currency_code=?,updated_epoch=? "
                     "WHERE singleton_id=1", (currency, int(time.time())))
        return currency

    @staticmethod
    def treasury_id(conn, nationality, user_id=None):
        """Return the single national account used for treasury and country work.

        This is not a separate treasury-only wallet. Its numeric id (given by
        $국가생성, else the next free 9xxxxxxx) is also the country's account and
        나루 login ID.
        """
        nationality = str(nationality or '낙원')
        row = conn.execute('SELECT user_id FROM country_treasuries WHERE nationality=?',
                           (nationality,)).fetchone()
        if row:
            return str(row['user_id'])
        country = conn.execute('SELECT 1 FROM country_currencies WHERE nationality=?',
                               (nationality,)).fetchone()
        if not country:
            raise ValueError('국고를 찾을 국가가 등록되어 있지 않습니다.')
        if user_id is None:
            user_id = 90000000
            while conn.execute('SELECT 1 FROM users WHERE user_id=?', (str(user_id),)).fetchone():
                user_id += 1
        user_id = str(user_id)
        if not user_id.isdecimal() or len(user_id) > 19:
            raise ValueError('국가 ID는 19자리 이하 숫자로 입력해주세요.')
        if conn.execute('SELECT 1 FROM users WHERE user_id=?', (user_id,)).fetchone():
            raise ValueError('이미 사용 중인 ID입니다. 다른 국가 ID를 입력해주세요.')
        conn.execute("INSERT OR IGNORE INTO users(user_id,nickname,nationality,cash,status,role) "
                     "VALUES (?,?,?,0,'active','nation')",
                     (user_id, nationality, nationality))
        conn.execute('INSERT INTO country_treasuries(nationality,user_id) VALUES (?,?)',
                     (nationality, user_id))
        return user_id

    @staticmethod
    def nationality_of(conn, key):
        """A country's name from its name or its numeric id (the treasury id)."""
        key = str(key).strip()
        row = conn.execute('SELECT nationality FROM country_treasuries WHERE user_id=?', (key,)).fetchone() \
            if key.isdecimal() else None
        return str(row['nationality']) if row else key

    @classmethod
    def user_treasury_id(cls, conn, user_id):
        row = conn.execute('SELECT nationality FROM users WHERE user_id=?',
                           (str(user_id),)).fetchone()
        nationality = row['nationality'] if row else '낙원'
        known = conn.execute('SELECT 1 FROM country_currencies WHERE nationality=?',
                             (nationality,)).fetchone()
        return cls.treasury_id(conn, nationality if known else '낙원')
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _liquidity_totals(conn, code):
        """Return issued and non-treasury circulating minor units for ``code``."""
        code = str(code).upper()
        total_native = conn.execute("""SELECT COALESCE(SUM(u.cash),0) FROM users u
            JOIN country_currencies n ON n.nationality=u.nationality
            WHERE n.currency_code=?""", (code,)).fetchone()[0]
        total = int(Decimal(str(total_native or 0))*SCALE)
        # Every account counts in its own currency with its exact balance_units,
        # whatever its owner's (possibly changed) nationality.
        total += int(conn.execute(
            "SELECT COALESCE(SUM(balance_units),0) FROM accounts WHERE quote_currency=?", (code,)
        ).fetchone()[0])
        treasury_row = conn.execute("""SELECT COALESCE(u.cash,0) FROM users u
            JOIN country_currencies n ON n.nationality=u.nationality
            WHERE u.user_id=? AND n.currency_code=?""",
            (TREASURY_USER_ID, code)).fetchone()
        treasury = int(Decimal(str(treasury_row[0] if treasury_row else 0))*SCALE)
        treasury += int(conn.execute(
            "SELECT COALESCE(SUM(balance_units),0) FROM accounts WHERE quote_currency=? AND user_id=?",
            (code, TREASURY_USER_ID)).fetchone()[0])
        return max(0, total), max(0, total - treasury)

    @classmethod
    def ensure_liquidity_state(cls, conn, code, *, refresh_empty=True):
        """Create a neutral baseline when a newly issued currency first gains supply."""
        code = str(code).upper()
        issued, circulating = cls._liquidity_totals(conn, code)
        # Treasury deficits can leave circulation above issuance; keep the ratio in range.
        ratio = min(1, max(0, circulating / issued)) if issued > 0 else 0
        conn.execute(
            'INSERT OR IGNORE INTO currency_liquidity_state '
            '(currency_code,baseline_supply_units,baseline_circulation_ratio,updated_epoch) '
            'VALUES (?,?,?,?)',
            (code, str(issued), ratio, time.time()),
        )
        if refresh_empty and issued:
            conn.execute(
                'UPDATE currency_liquidity_state SET baseline_supply_units=?, '
                'baseline_circulation_ratio=?, updated_epoch=? '
                'WHERE currency_code=? AND baseline_supply_units=0',
                (str(issued), ratio, time.time(), code),
            )

    @classmethod
    def liquidity_multiplier(cls, conn, code, *, totals=None, state=None):
        """Bounded supply/circulation adjustment around an administrator's base quote."""
        code = str(code).upper()
        if state is None:
            state = conn.execute(
                'SELECT * FROM currency_liquidity_state WHERE currency_code=?', (code,)
            ).fetchone()
        if not state:
            return 1.0
        issued, circulating = totals if totals is not None else cls._liquidity_totals(conn, code)
        baseline = int(state['baseline_supply_units'] or 0)
        baseline_ratio = float(state['baseline_circulation_ratio'] or 0)
        if issued <= 0 or baseline <= 0:
            return 1.0
        try:
            issuance = float(state['issuance_elasticity'])
            circulation = float(state['circulation_elasticity'])
            floor = float(state['min_multiplier'])
            ceiling = float(state['max_multiplier'])
            current_ratio = circulating / issued
            multiplier = (baseline / issued) ** issuance
            if baseline_ratio > 0 and current_ratio > 0:
                multiplier *= (current_ratio / baseline_ratio) ** circulation
            if not all(math.isfinite(value) for value in (multiplier, floor, ceiling)):
                return 1.0
            return max(floor, min(ceiling, multiplier))
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            return 1.0

    @staticmethod
    def home(conn, user_id):
        from database.fund_accounting import account
        if account(conn, user_id):
            return base_currency_code(conn)
        user = conn.execute('SELECT nationality FROM users WHERE user_id=?', (str(user_id),)).fetchone()
        if not user:
            raise ValueError('등록되지 않은 사용자입니다.')
        row = conn.execute("SELECT n.currency_code FROM country_currencies n JOIN currencies c ON c.code=n.currency_code WHERE nationality=? AND c.status='active'", (user['nationality'],)).fetchone()
        if row:
            return row[0]
        # Unmapped legacy citizens inherit the configured simulation base.
        return base_currency_code(conn)

    @classmethod
    def rate_fraction(cls, conn, code):
        row = conn.execute("SELECT * FROM currencies WHERE code=? AND status='active'", (str(code).upper(),)).fetchone()
        if not row:
            raise ValueError(f'활성 통화를 확인해주세요: {code}')
        if row['reference_rate'] is not None:
            value = Fraction(str(row['reference_rate']))
        else:
            asset = conn.execute("SELECT price FROM market_assets WHERE code=? AND status='active'", (row['asset_code'],)).fetchone()
            if code == base_currency_code(conn) and not asset:
                value = Fraction(1)
            elif not asset:
                raise ValueError(f'{code} 환율을 조회할 수 없습니다.')
            else:
                value = Fraction(str(asset[0]))
        value *= Fraction(str(row['rate_multiplier']))
        value *= Fraction(str(cls.liquidity_multiplier(conn, code)))
        if value <= 0:
            raise ValueError(f'{code} 환율 설정이 올바르지 않습니다.')
        return value

    @classmethod
    def rate(cls, conn, code):
        value=cls.rate_fraction(conn,code)
        return Decimal(value.numerator)/Decimal(value.denominator)

    @classmethod
    def quote(cls,conn,source,target):
        return cls.rate_fraction(conn,source)/cls.rate_fraction(conn,target)

    @staticmethod
    def precision(conn,code):
        row=conn.execute("SELECT decimal_places FROM currencies WHERE code=? AND status='active'",(code,)).fetchone()
        if not row:raise ValueError(f'활성 통화를 확인해주세요: {code}')
        places=int(row[0])
        if not 0<=places<=6:raise ValueError('통화 소수 자릿수는 0~6으로 설정해주세요.')
        if code==base_currency_code(conn) and places!=0:raise ValueError('기존 기준 통화 현금은 정수 단위로 설정해주세요.')
        return places

    @classmethod
    def validate_units(cls,conn,units,code):
        places=cls.precision(conn,code)
        if units % (10**(6-places)):
            raise ValueError(f'{code} 금액은 소수점 {places}자리까지 입력해주세요.')

    @classmethod
    def exchange_amount(cls,conn,units,source,target,fee_nwx=0):
        value=units*cls.quote(conn,source,target)-fee_nwx*SCALE*cls.quote(conn,base_currency_code(conn),target)
        output=value.numerator//value.denominator
        quantum=10**(6-cls.precision(conn,target))
        output=output//quantum*quantum
        return output

    @classmethod
    def fee(cls,conn,units,source,percent):
        value=units*cls.quote(conn,source,base_currency_code(conn))*Fraction(str(percent))/(SCALE*100)
        return -(-value.numerator//value.denominator)

    @classmethod
    def convert(cls, conn, units, source, target):
        output=cls.exchange_amount(conn,units,source,target)
        if target == base_currency_code(conn):  # Legacy cash has whole-unit precision.
            output = output // SCALE * SCALE
        limit=MAX_NATIVE*SCALE if target==base_currency_code(conn) else MAX_UNITS
        if not 0 <= output <= limit:
            raise ValueError('환산 금액이 저장 한도를 초과했습니다.')
        return output

    @staticmethod
    def balance(conn, user_id, currency):
        currency = str(currency).upper()
        if currency == CurrencyManager.home(conn, user_id):
            row = conn.execute('SELECT cash_minor FROM users WHERE user_id=?', (str(user_id),)).fetchone()
            return int(row[0] or 0) * 10 ** (6 - CurrencyManager.precision(conn, currency)) if row else 0
        from managers.deposit_accounts import account
        row = account(conn, user_id, currency)
        return int(row['balance_units']) if row else 0

    @staticmethod
    def is_treasury(conn, user_id):
        """Every country's treasury, not only 낙원's, is a treasury."""
        user_id = str(user_id)
        return user_id == TREASURY_USER_ID or conn.execute(
            "SELECT 1 FROM country_treasuries WHERE user_id=?", (user_id,)).fetchone() is not None

    @classmethod
    def exchange_treasury(cls, conn, asset_code):
        """Treasury of the country whose exchange lists ``asset_code``.

        Primary issues, fund NAV dealing, treasury bonds and other trades where
        the state is the counterparty settle with this treasury.
        """
        from managers.market_session_manager import MarketSessionManager
        exchange = MarketSessionManager.exchange_for_asset(conn, asset_code)
        row = conn.execute("SELECT nationality FROM market_exchanges WHERE exchange_id=?",
                           (exchange,)).fetchone() if exchange else None
        nationality = row["nationality"] if row and row["nationality"] else '낙원'
        known = conn.execute('SELECT 1 FROM country_currencies WHERE nationality=?', (nationality,)).fetchone()
        return cls.treasury_id(conn, nationality if known else '낙원')

    @staticmethod
    def is_bank(conn, user_id):
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='bank_accounts'").fetchone()
        return bool(exists and conn.execute("SELECT 1 FROM bank_accounts WHERE user_id=?", (str(user_id),)).fetchone())

    @staticmethod
    def bank_account_id(conn, issuer):
        """Head-office account of a bank, created on first use in its country's currency."""
        issuer = str(issuer)
        row = conn.execute("SELECT user_id FROM bank_accounts WHERE issuer=?", (issuer,)).fetchone()
        if row:
            return str(row[0])
        nationality = conn.execute("SELECT nationality FROM financial_institutions WHERE issuer=?", (issuer,)).fetchone()
        nationality = nationality[0] if nationality and nationality[0] else '낙원'
        user_id = f'bank:{issuer}'  # non-decimal namespace: never a Discord ID
        conn.execute("INSERT OR IGNORE INTO users(user_id,nickname,nationality,cash,status,role) "
                     "VALUES (?,?,?,0,'active','nation')", (user_id, f'{issuer} 본사', nationality))
        conn.execute("INSERT INTO bank_accounts(issuer,user_id) VALUES (?,?)", (issuer, user_id))
        return user_id

    @classmethod
    def majority_treasury(cls, conn, asset_codes):
        """Treasury of the country listing most of ``asset_codes`` (ties: the earliest listed)."""
        treasuries = [cls.exchange_treasury(conn, code) for code in asset_codes]
        if not treasuries:
            return cls.treasury_id(conn, '낙원')
        return max(treasuries, key=lambda t: (treasuries.count(t), -treasuries.index(t)))

    @classmethod
    def _treasury_target(cls, conn, treasury, currency):
        """Where a treasury holds ``currency``: its cash, its FX account, or (None) neither."""
        currency = str(currency).upper()
        if currency == cls.home(conn, treasury):
            return 'cash'
        from managers.deposit_accounts import account
        return 'fx' if account(conn, treasury, currency) else None

    @classmethod
    def treasury_receive(cls, conn, treasury, currency, units):
        """Credit a treasury: kept in that currency if it holds it, else exchanged to its own."""
        units = int(units)
        if not units:
            return 0
        currency = str(currency).upper()
        if cls._treasury_target(conn, treasury, currency):
            cls.change(conn, treasury, currency, units, allow_deficit=True)
            return units
        home = cls.home(conn, treasury)
        converted = cls.convert(conn, units, currency, home)
        if converted:
            cls.change(conn, treasury, home, converted, allow_deficit=True)
        return converted

    @classmethod
    def treasury_pay(cls, conn, treasury, currency, units):
        """Debit a treasury for a payment made in ``currency`` (deficits allowed)."""
        units = int(units)
        if not units:
            return 0
        currency = str(currency).upper()
        if cls._treasury_target(conn, treasury, currency):
            cls.change(conn, treasury, currency, -units, allow_deficit=True)
            return units
        home = cls.home(conn, treasury)
        exact = Fraction(units) * cls.quote(conn, currency, home)
        quantum = 10 ** (6 - cls.precision(conn, home))
        ceiling = -(-exact.numerator // exact.denominator)
        cost = -(-ceiling // quantum) * quantum
        cls.change(conn, treasury, home, -cost, allow_deficit=True)
        return cost

    @classmethod
    def credit_treasury_fee(cls, conn, user_id, fee_nwx):
        """Exchange fees (quoted in 기준 통화) go to the user's nationality treasury, in its currency."""
        if not fee_nwx:
            return None
        treasury = cls.user_treasury_id(conn, user_id)
        cls.treasury_receive(conn, treasury, base_currency_code(conn), int(fee_nwx) * SCALE)
        return treasury

    @classmethod
    def change(cls, conn, user_id, currency, delta, allow_deficit=False):
        user_id = str(user_id)
        if not (conn.execute("SELECT 1 FROM users WHERE user_id=? AND status='active'", (user_id,)).fetchone()
                or cls.is_treasury(conn, user_id)):
            raise ValueError('등록된 활성 사용자만 이용할 수 있습니다.')
        cls.rate(conn, currency)
        currency = str(currency).upper()
        home = cls.home(conn, user_id)
        cls.validate_units(conn, int(delta), currency)
        value = cls.balance(conn, user_id, currency) + int(delta)
        limit=MAX_NATIVE*SCALE if currency == home else MAX_UNITS
        if abs(value) > limit or (value < 0 and not (cls.is_treasury(conn, user_id) or (allow_deficit and cls.is_bank(conn, user_id)))):
            raise ValueError(f'{currency} 잔액이 부족하거나 저장 한도를 초과했습니다.')
        if currency == home:
            # Exact integer arithmetic on the minor unit; ``cash`` follows by trigger.
            minor = int(delta) // 10 ** (6 - cls.precision(conn, home))
            conn.execute('UPDATE users SET cash_minor=cash_minor+? WHERE user_id=?', (minor, user_id))
        else:
            from managers.deposit_accounts import PRIMARY_NUMBER, PRINCIPAL_DELTA
            changed = conn.execute(f"UPDATE accounts SET balance_units=?,{PRINCIPAL_DELTA} WHERE account_number={PRIMARY_NUMBER}",
                                   (value,int(delta),str(user_id),currency))
            if changed.rowcount != 1:
                raise ValueError(f'{currency} 외환통장을 먼저 개설해주세요.')
        # This is deliberately after the balance change: a brand new currency's
        # first issuance establishes its neutral supply rather than immediately
        # moving its own quote.
        cls.ensure_liquidity_state(conn, currency)
        return value

    def wallets(self, user_id, *, conn=None, snapshot=None):
        with (self.db.readonly() if conn is None else nullcontext(conn)) as conn:
            snapshot = snapshot or CurrencyReadSnapshot(conn)
            home = self.home(conn,user_id)
            rows = [{'currency_code':home,'balance_units':self.balance(conn,user_id,home)}]
            rows += [dict(r) for r in conn.execute(
                "SELECT quote_currency AS currency_code,balance_units FROM accounts a "
                "WHERE user_id=? AND account_type='saving' AND quote_currency<>? "
                "AND rowid=(SELECT rowid FROM accounts b WHERE b.user_id=a.user_id AND b.quote_currency=a.quote_currency "
                "AND b.account_type='saving' ORDER BY b.created_at,b.rowid LIMIT 1) "
                "ORDER BY quote_currency", (str(user_id),home))]
            for row in rows:
                row['decimal_places']=snapshot.precision(row['currency_code'])
                row['is_foreign'] = row['currency_code'] != home
                units = row['balance_units']
                row['value_home_units'] = snapshot.convert(abs(units),row['currency_code'],home) * (-1 if units < 0 else 1)
            return rows

    @classmethod
    def register(cls, conn, user_id, nickname, guild_id, role='user', nationality=None):
        existing = conn.execute('SELECT 1 FROM users WHERE user_id=?',(str(user_id),)).fetchone()
        if existing:
            conn.execute('UPDATE users SET nickname=? WHERE user_id=?',(str(nickname),str(user_id)))
            return False
        if nationality is None:
            country = conn.execute('SELECT nationality FROM guild_countries WHERE guild_id=?',(str(guild_id),)).fetchone() if guild_id is not None else None
            if not country:
                raise ValueError('등록 서버의 국적을 먼저 설정해주세요. 관리자: $서버설치 [서버ID] [국적] [통화]')
            nationality=country[0]
        mapping = conn.execute("SELECT n.currency_code FROM country_currencies n JOIN currencies c ON c.code=n.currency_code WHERE nationality=? AND c.status='active'",(nationality,)).fetchone()
        if not mapping:
            raise ValueError('등록 국가의 활성 통화를 먼저 설정해주세요.')
        source = base_currency_code(conn)
        home=mapping[0]
        grant=cls.convert(conn,1_000_000*SCALE,source,home)
        if grant <= 0:
            raise ValueError('가입 지원금이 국적 통화의 현금 최소 단위보다 작습니다.')
        conn.execute('INSERT INTO users(user_id,nickname,nationality,cash,role,registration_guild_id) VALUES (?,?,?,0,?,?)',(str(user_id),str(nickname),nationality,role,str(guild_id) if guild_id is not None else None))
        cls.change(conn,user_id,home,grant)
        return True

    def register_user(self,user_id,nickname,guild_id,role='user'):
        with self.db.atomic() as conn:
            return self.register(conn,user_id,nickname,guild_id,role)


class CurrencyReadSnapshot:
    """Request-local display rates. Never reuse this snapshot for settlements."""

    def __init__(self, conn):
        self.conn = conn
        self.rows = {r['code']: dict(r) for r in conn.execute("""
            WITH cash AS (
                SELECT n.currency_code, SUM(u.cash) AS total,
                    SUM(CASE WHEN u.user_id=? THEN u.cash ELSE 0 END) AS treasury
                FROM users u JOIN country_currencies n ON n.nationality=u.nationality
                GROUP BY n.currency_code
            ), savings AS (
                -- Accounts are counted below by their own currency; kept for the column shape.
                SELECT NULL AS currency_code, 0 AS total, 0 AS treasury WHERE ? IS NULL AND 0
            ), foreign_savings AS (
                SELECT quote_currency AS currency_code, SUM(balance_units) AS total,
                    SUM(CASE WHEN user_id=? THEN balance_units ELSE 0 END) AS treasury
                FROM accounts GROUP BY quote_currency
            )
            SELECT c.*, a.price AS asset_price, cfg.base_currency_code AS configured_base_currency,
                (SELECT GROUP_CONCAT(n.nationality, ', ') FROM country_currencies n
                 WHERE n.currency_code=c.code ORDER BY n.nationality) AS countries,
                cash.total AS cash_total, cash.treasury AS cash_treasury,
                savings.total AS savings_total, savings.treasury AS savings_treasury,
                foreign_savings.total AS foreign_total, foreign_savings.treasury AS foreign_treasury,
                s.baseline_supply_units, s.baseline_circulation_ratio,
                s.issuance_elasticity, s.circulation_elasticity, s.min_multiplier, s.max_multiplier
            FROM currencies c
            CROSS JOIN currency_system_settings cfg
            LEFT JOIN market_assets a ON a.code=c.asset_code AND a.status='active'
            LEFT JOIN cash ON cash.currency_code=c.code
            LEFT JOIN savings ON savings.currency_code=c.code
            LEFT JOIN foreign_savings ON foreign_savings.currency_code=c.code
            LEFT JOIN currency_liquidity_state s ON s.currency_code=c.code
            WHERE c.status='active' ORDER BY c.code
            """, (TREASURY_USER_ID, TREASURY_USER_ID, TREASURY_USER_ID))}
        self.base_currency = next((r['configured_base_currency'] for r in self.rows.values()), None)
        self._rates = {}
        self._liquidity_states = {}
        self._liquidity_totals = {}
        for code, row in self.rows.items():
            self._liquidity_states[code] = row if row['baseline_supply_units'] is not None else {}
            total = (int(Decimal(str(row['cash_total'] or 0))*SCALE)
                     + int(Decimal(str(row['savings_total'] or 0))*SCALE)
                     + int(row['foreign_total'] or 0))
            treasury = (int(Decimal(str(row['cash_treasury'] or 0))*SCALE)
                        + int(Decimal(str(row['savings_treasury'] or 0))*SCALE)
                        + int(row['foreign_treasury'] or 0))
            self._liquidity_totals[code] = max(0, total), max(0, total-treasury)

    def row(self, code):
        row = self.rows.get(str(code).upper())
        if row is None:
            raise ValueError(f'활성 통화를 확인해주세요: {code}')
        return row

    def rate_fraction(self, code):
        code = str(code).upper()
        if code not in self._rates:
            row = self.row(code)
            raw = row['reference_rate'] if row['reference_rate'] is not None else row['asset_price']
            if raw is None and code == self.base_currency:
                raw = 1
            elif raw is None:
                raise ValueError(f'{code} 환율을 조회할 수 없습니다.')
            value = Fraction(str(raw)) * Fraction(str(row['rate_multiplier']))
            value *= Fraction(str(CurrencyManager.liquidity_multiplier(
                self.conn, code, totals=self._liquidity_totals[code],
                state=self._liquidity_states.get(code),
            )))
            if value <= 0:
                raise ValueError(f'{code} 환율 설정이 올바르지 않습니다.')
            self._rates[code] = value
        return self._rates[code]

    def rate_nwx(self, code):
        # Preserve the legacy method name while valuing against the configured base.
        source, target = self.rate_fraction(code), self.rate_fraction(self.base_currency)
        value = ((Decimal(source.numerator) / Decimal(source.denominator)) /
                 (Decimal(target.numerator) / Decimal(target.denominator)))
        return int(value) if value == value.to_integral_value() else value

    def precision(self, code):
        places = int(self.row(code)['decimal_places'])
        if not 0 <= places <= 6:
            raise ValueError('통화 소수 자릿수는 0~6으로 설정해주세요.')
        if code == self.base_currency and places != 0:
            raise ValueError('기본 통화 현금은 정수 단위로 설정해주세요.')
        return places

    def convert(self, units, source, target):
        value = units * self.rate_fraction(source) / self.rate_fraction(target)
        quantum = 10 ** (6 - self.precision(target))
        output = (value.numerator // value.denominator) // quantum * quantum
        limit = MAX_NATIVE * SCALE if target == self.base_currency else MAX_UNITS
        if not 0 <= output <= limit:
            raise ValueError('환산 금액이 저장 한도를 초과했습니다.')
        return output
