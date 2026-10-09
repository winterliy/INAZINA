"""Nationality-relative cash wallets. NWX cash keeps its legacy storage unchanged."""
from fractions import Fraction
from contextlib import nullcontext
from decimal import Decimal
from database.user_db import TREASURY_USER_ID

SCALE = 1_000_000
MAX_UNITS = 2**63 - 1

class CurrencyManager:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def home(conn, user_id):
        user = conn.execute('SELECT nationality FROM users WHERE user_id=?', (str(user_id),)).fetchone()
        if not user:
            raise ValueError('등록되지 않은 사용자입니다.')
        row = conn.execute("SELECT n.currency_code FROM country_currencies n JOIN currencies c ON c.code=n.currency_code WHERE nationality=? AND c.status='active'", (user['nationality'],)).fetchone()
        if row:
            return row[0]
        # Existing citizens with no country mapping retain the legacy default.
        return conn.execute("SELECT value FROM settings WHERE key='default_currency'").fetchone()[0]

    @staticmethod
    def rate_fraction(conn, code):
        row = conn.execute("SELECT * FROM currencies WHERE code=? AND status='active'", (str(code).upper(),)).fetchone()
        if not row:
            raise ValueError(f'활성 통화를 확인해주세요: {code}')
        if row['reference_rate'] is not None:
            value = Fraction(str(row['reference_rate']))
        else:
            asset = conn.execute("SELECT price FROM market_assets WHERE code=? AND status='active'", (row['asset_code'],)).fetchone()
            if not asset:
                raise ValueError(f'{code} 환율을 조회할 수 없습니다.')
            value = Fraction(str(asset[0]))
        value *= Fraction(str(row['rate_multiplier']))
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
        if code=='NWX' and places!=0:raise ValueError('기존 NWX 현금은 정수 단위로 설정해주세요.')
        return places

    @classmethod
    def validate_units(cls,conn,units,code):
        places=cls.precision(conn,code)
        if units % (10**(6-places)):
            raise ValueError(f'{code} 금액은 소수점 {places}자리까지 입력해주세요.')

    @classmethod
    def exchange_amount(cls,conn,units,source,target,fee_nwx=0):
        value=units*cls.quote(conn,source,target)-fee_nwx*SCALE*cls.quote(conn,'NWX',target)
        output=value.numerator//value.denominator
        quantum=10**(6-cls.precision(conn,target))
        output=output//quantum*quantum
        return output

    @classmethod
    def fee(cls,conn,units,source,percent):
        value=units*cls.quote(conn,source,'NWX')*Fraction(str(percent))/(SCALE*100)
        return -(-value.numerator//value.denominator)

    @classmethod
    def convert(cls, conn, units, source, target):
        output=cls.exchange_amount(conn,units,source,target)
        if target == 'NWX':  # Legacy cash has whole-unit precision.
            output = output // SCALE * SCALE
        limit=(2**53-1)*SCALE if target=='NWX' else MAX_UNITS
        if not 0 <= output <= limit:
            raise ValueError('환산 금액이 저장 한도를 초과했습니다.')
        return output

    @staticmethod
    def balance(conn, user_id, currency):
        if currency == 'NWX':
            row = conn.execute('SELECT cash FROM users WHERE user_id=?', (str(user_id),)).fetchone()
            return int(row[0]) * SCALE if row else 0
        row = conn.execute('SELECT balance_units FROM currency_wallets WHERE user_id=? AND currency_code=?', (str(user_id), currency)).fetchone()
        return int(row[0]) if row else 0

    @classmethod
    def change(cls, conn, user_id, currency, delta, allow_deficit=False):
        user_id = str(user_id)
        if not conn.execute("SELECT 1 FROM users WHERE user_id=? AND (status='active' OR user_id=?)", (user_id,TREASURY_USER_ID)).fetchone():
            raise ValueError('등록된 활성 사용자만 이용할 수 있습니다.')
        cls.rate(conn, currency)
        value = cls.balance(conn, user_id, currency) + int(delta)
        limit=(2**53-1)*SCALE if currency=='NWX' else MAX_UNITS
        if abs(value) > limit or (value < 0 and not (allow_deficit and user_id == TREASURY_USER_ID)):
            raise ValueError(f'{currency} 잔액이 부족하거나 저장 한도를 초과했습니다.')
        if currency == 'NWX':
            if delta % SCALE:
                raise ValueError('NWX 현금은 정수 금액으로 입력해주세요.')
            conn.execute('UPDATE users SET cash=cash+? WHERE user_id=?', (delta//SCALE,user_id))
        else:
            conn.execute('INSERT INTO currency_wallets(user_id,currency_code,balance_units) VALUES (?,?,?) ON CONFLICT(user_id,currency_code) DO UPDATE SET balance_units=excluded.balance_units', (user_id,currency,value))
        return value

    def wallets(self, user_id, *, conn=None, snapshot=None):
        with (self.db.readonly() if conn is None else nullcontext(conn)) as conn:
            snapshot = snapshot or CurrencyReadSnapshot(conn)
            home = self.home(conn,user_id)
            rows = [{'currency_code':'NWX','balance_units':self.balance(conn,user_id,'NWX')}]
            rows += [dict(r) for r in conn.execute('SELECT currency_code,balance_units FROM currency_wallets WHERE user_id=? ORDER BY currency_code',(str(user_id),))]
            if not any(r['currency_code']==home for r in rows):
                rows.append({'currency_code':home,'balance_units':0})
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
        source=conn.execute("SELECT value FROM settings WHERE key='default_currency'").fetchone()[0]
        home=mapping[0]
        grant=cls.convert(conn,1_000_000*SCALE,source,home)
        conn.execute('INSERT INTO users(user_id,nickname,nationality,cash,role,registration_guild_id) VALUES (?,?,?,0,?,?)',(str(user_id),str(nickname),nationality,role,str(guild_id) if guild_id is not None else None))
        cls.change(conn,user_id,home,grant)
        return True

    def register_user(self,user_id,nickname,guild_id,role='user'):
        with self.db.atomic() as conn:
            return self.register(conn,user_id,nickname,guild_id,role)


class CurrencyReadSnapshot:
    """Request-local display rates. Never reuse this snapshot for settlements."""

    def __init__(self, conn):
        self.rows = {r['code']: dict(r) for r in conn.execute(
            "SELECT c.*, a.price AS asset_price, "
            "(SELECT GROUP_CONCAT(n.nationality, ', ') FROM country_currencies n "
            "WHERE n.currency_code=c.code ORDER BY n.nationality) AS countries "
            "FROM currencies c "
            "LEFT JOIN market_assets a ON a.code=c.asset_code AND a.status='active' "
            "WHERE c.status='active' ORDER BY c.code")}
        self._rates = {}

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
            if raw is None:
                raise ValueError(f'{code} 환율을 조회할 수 없습니다.')
            value = Fraction(str(raw)) * Fraction(str(row['rate_multiplier']))
            if value <= 0:
                raise ValueError(f'{code} 환율 설정이 올바르지 않습니다.')
            self._rates[code] = value
        return self._rates[code]

    def rate_nwx(self, code):
        # Keep the same Decimal rounding order as FXManager._rate.
        source, target = self.rate_fraction(code), self.rate_fraction('NWX')
        value = ((Decimal(source.numerator) / Decimal(source.denominator)) /
                 (Decimal(target.numerator) / Decimal(target.denominator)))
        return int(value) if value == value.to_integral_value() else value

    def precision(self, code):
        places = int(self.row(code)['decimal_places'])
        if not 0 <= places <= 6:
            raise ValueError('통화 소수 자릿수는 0~6으로 설정해주세요.')
        if code == 'NWX' and places != 0:
            raise ValueError('기존 NWX 현금은 정수 단위로 설정해주세요.')
        return places

    def convert(self, units, source, target):
        value = units * self.rate_fraction(source) / self.rate_fraction(target)
        quantum = 10 ** (6 - self.precision(target))
        output = (value.numerator // value.denominator) // quantum * quantum
        limit = (2**53 - 1) * SCALE if target == 'NWX' else MAX_UNITS
        if not 0 <= output <= limit:
            raise ValueError('환산 금액이 저장 한도를 초과했습니다.')
        return output
