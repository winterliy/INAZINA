"""Integer-only currency helpers for the NWX economy."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


_WHOLE = Decimal("1")


def round_money(value):
    """Round a calculated currency value to the nearest NWX (half up)."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("금액은 유한한 숫자여야 합니다.") from None
    if not amount.is_finite():
        raise ValueError("금액은 유한한 숫자여야 합니다.")
    return int(amount.quantize(_WHOLE, rounding=ROUND_HALF_UP))


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
