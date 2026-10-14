"""Currency-aware decimal helpers.

Database values are expressed in the named currency (not always NWX).  Calculated
amounts are rounded half-up to that currency's configured smallest unit.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


_WHOLE = Decimal("1")


def round_money(value, decimal_places=0):
    """Round a calculated amount half-up and return a SQLite-bindable number."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("금액은 유한한 숫자여야 합니다.") from None
    if not amount.is_finite():
        raise ValueError("금액은 유한한 숫자여야 합니다.")
    try:
        places = int(decimal_places)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("통화 소수 자릿수가 올바르지 않습니다.") from None
    if not 0 <= places <= 6:
        raise ValueError("통화 소수 자릿수는 0~6이어야 합니다.")
    rounded = amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return int(rounded) if places == 0 else float(rounded)


def currency_precision(conn, currency_code):
    row = conn.execute(
        "SELECT decimal_places FROM currencies WHERE code=?",
        (str(currency_code or "").upper(),),
    ).fetchone()
    if not row:
        raise ValueError(f"통화를 확인해주세요: {currency_code}")
    places = int(row[0])
    if not 0 <= places <= 6:
        raise ValueError("통화 소수 자릿수는 0~6이어야 합니다.")
    return places


def round_currency(conn, value, currency_code):
    return round_money(value, currency_precision(conn, currency_code))


def parse_currency_money(conn, value, currency_code, *, minimum=0):
    """Parse a native-currency amount without accepting sub-minimum-unit digits."""
    if isinstance(value, bool):
        raise ValueError(f"{currency_code} 금액을 확인해주세요.")
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{currency_code} 금액을 확인해주세요.") from None
    places = currency_precision(conn, currency_code)
    quantum = Decimal(1).scaleb(-places)
    if (not amount.is_finite() or amount < Decimal(str(minimum))
            or amount != amount.quantize(quantum)):
        raise ValueError(
            f"{currency_code} 금액은 0 이상, 소수점 {places}자리까지 입력해주세요."
        )
    return int(amount) if places == 0 else float(amount)


def parse_money(value, *, minimum=0):
    """Parse a whole-NWX user/configured amount; reject fractional currency."""
    if isinstance(value, bool):
        raise ValueError("금액은 정수 NWX로 입력해주세요.")
    try:
        amount = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("금액은 정수 NWX로 입력해주세요.") from None
    if not amount.is_finite() or amount != amount.to_integral_value():
        raise ValueError("금액은 소수점 없는 정수 NWX로 입력해주세요.")
    result = int(amount)
    if result < minimum:
        raise ValueError(f"금액은 {minimum:,} NWX 이상이어야 합니다.")
    return result
