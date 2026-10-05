"""Bounded thousandth-unit quantities; money is settled separately in minor units."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

QUANTITY_SCALE = 1000
MAX_FRACTIONAL_QUANTITY = 10**12
FRACTIONAL_TYPES = frozenset({'stock', '주식', 'etf', 'mutual_fund', 'index_fund', 'commodity_spot'})


def quantity_units(value):
    amount = Decimal(value.numerator)/Decimal(value.denominator) if hasattr(value, 'numerator') else Decimal(str(value))
    if not amount.is_finite():
        raise ValueError('수량은 유한한 숫자여야 합니다.')
    return int((amount * QUANTITY_SCALE).to_integral_value(rounding=ROUND_HALF_UP))


def quantity_number(value):
    units = quantity_units(value or 0)
    return units // QUANTITY_SCALE if units % QUANTITY_SCALE == 0 else float(Decimal(units) / QUANTITY_SCALE)


def parse_quantity(value, *, fractional=False, maximum=2**53-1):
    try:
        if isinstance(value, bool):
            raise ValueError()
        amount = Decimal(str(value).strip())
        if not amount.is_finite() or amount <= 0 or amount > maximum:
            raise ValueError()
        if fractional:
            if amount > MAX_FRACTIONAL_QUANTITY or amount * QUANTITY_SCALE != (amount * QUANTITY_SCALE).to_integral_value():
                raise ValueError()
        elif amount != amount.to_integral_value():
            raise ValueError()
        return quantity_number(amount) if fractional else int(amount)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise ValueError('수량은 0보다 크고 소수점 3자리 이하로 입력해주세요.' if fractional
                         else '이 상품의 거래 수량은 1 이상의 정수로 입력해주세요.') from None


def fractional_enabled(asset):
    return bool(asset and asset.get('Fractal') == 1 and asset.get('type') in FRACTIONAL_TYPES)


def validate_fractional_setting(conn, table, key, code, enabled, asset_type, supply=None):
    if isinstance(enabled, bool) or str(enabled) not in {'0', '1'}:
        raise ValueError('Fractal은 0(불허) 또는 1(허용)입니다.')
    enabled = int(enabled)
    if enabled:
        if asset_type not in FRACTIONAL_TYPES:
            raise ValueError('소수점 거래는 주식·우선주·ETF·일반/인덱스 펀드·현물 원자재만 지원합니다.')
        if supply is not None and int(supply) > MAX_FRACTIONAL_QUANTITY:
            raise ValueError('소수점 지원 상품의 총 수량은 1조 이하로 설정해주세요.')
    else:
        for query in (
            'SELECT 1 FROM user_assets WHERE asset_code=? AND ROUND(quantity,3)!=CAST(ROUND(quantity,3) AS INTEGER) LIMIT 1',
            "SELECT 1 FROM market_orders WHERE asset_code=? AND status IN ('open','partial') AND fractional_order=1 LIMIT 1",
            'SELECT 1 FROM option_collateral_lots WHERE underlying_code=? AND ROUND(quantity,3)!=CAST(ROUND(quantity,3) AS INTEGER) LIMIT 1',
        ):
            if conn.execute(query, (code,)).fetchone():
                raise ValueError('소수점 보유분·담보 또는 소수점 거래 주문을 먼저 정리해주세요.')
    return enabled
