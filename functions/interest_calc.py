import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functions.time_utils import validate_epoch

YEAR_SECONDS = 365 * 24 * 60 * 60


def year_fraction(elapsed_seconds, convention="ACT/365", start_epoch=None):
    """Convert elapsed seconds to a contractual year fraction."""
    if isinstance(elapsed_seconds, bool):
        raise ValueError("경과 시간은 유한한 초 단위 값이어야 합니다.")
    try:
        seconds = float(elapsed_seconds or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("경과 시간은 유한한 초 단위 값이어야 합니다.") from exc
    if not math.isfinite(seconds):
        raise ValueError("경과 시간은 유한한 초 단위 값이어야 합니다.")
    seconds = max(0.0, seconds)
    if start_epoch is not None:
        start_epoch = validate_epoch(start_epoch)
        end_epoch = validate_epoch(start_epoch + seconds)
    convention = str(convention or "ACT/365").upper()
    if convention == "30/360" and start_epoch is not None:
        start = datetime.fromtimestamp(start_epoch, timezone.utc)
        end = datetime.fromtimestamp(end_epoch, timezone.utc)
        day1 = min(start.day, 30)
        day2 = min(end.day, 30) if day1 == 30 else end.day
        return ((end.year-start.year)*360 + (end.month-start.month)*30
                + day2-day1) / 360.0
    if convention in {"ACT/360", "30/360"}:
        return seconds / (360 * 24 * 60 * 60)
    return seconds / YEAR_SECONDS


def year_fraction_decimal(elapsed_seconds, convention="ACT/365", start_epoch=None):
    """Decimal day-count fraction for money calculations.

    ACT conventions use exact decimal division.  The dated 30/360 convention
    intentionally delegates its calendar boundary to the legacy implementation
    so existing contract semantics remain unchanged.
    """
    try:
        seconds = Decimal(str(elapsed_seconds or 0))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("경과 시간은 유한한 초 단위 값이어야 합니다.") from exc
    if not seconds.is_finite():
        raise ValueError("경과 시간은 유한한 초 단위 값이어야 합니다.")
    seconds = max(Decimal(0), seconds)
    convention = str(convention or "ACT/365").upper()
    if convention == "30/360" and start_epoch is not None:
        return Decimal(str(year_fraction(float(seconds), convention, start_epoch)))
    denominator = (360 if convention in {"ACT/360", "30/360"} else 365) * 86400
    return seconds / Decimal(denominator)


def calculate_simple_interest(principal, rate, elapsed_seconds, convention="ACT/365",
                              start_epoch=None):
    """연이율(%) 기준 단리. elapsed_seconds는 실제 초 단위 경과 시간입니다."""
    principal = float(principal)
    rate = float(rate)
    elapsed_seconds = float(elapsed_seconds)
    if not all(math.isfinite(value) for value in (principal, rate, elapsed_seconds)):
        raise ValueError("원금·금리·경과 시간은 유한한 숫자여야 합니다.")
    if principal <= 0 or elapsed_seconds <= 0:
        return 0.0
    return principal * (rate / 100.0) * year_fraction(
        elapsed_seconds, convention, start_epoch
    )


def calculate_compound_interest(principal, rate, periods, cycle_seconds,
                                convention="ACT/365", start_epoch=None):
    """정해진 초 단위 주기를 periods회 적용한 복리 이자(세전)를 반환합니다."""
    principal = float(principal)
    rate = float(rate)
    periods = int(periods)
    cycle_seconds = float(cycle_seconds)
    if not all(math.isfinite(value) for value in (principal, rate, cycle_seconds)):
        raise ValueError("원금·금리·주기 시간은 유한한 숫자여야 합니다.")
    if principal <= 0 or periods <= 0 or cycle_seconds <= 0:
        return 0.0
    total_fraction = year_fraction(
        periods*cycle_seconds, convention, start_epoch
    )
    periodic_rate = (rate / 100.0) * total_fraction/periods
    if periodic_rate <= -1:
        return -principal
    exponent = periods * math.log1p(periodic_rate)
    if exponent > 700:
        raise OverflowError("복리 계산 결과가 너무 큽니다.")
    return principal * math.expm1(exponent)


def calculate_simple_interest_decimal(principal, rate, elapsed_seconds,
                                      convention="ACT/365", start_epoch=None):
    """Calculate simple interest with Decimal day-count and money arithmetic."""
    try:
        amount = Decimal(str(principal))
        annual_rate = Decimal(str(rate))
        elapsed = Decimal(str(elapsed_seconds))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("원금·금리·경과 시간은 유효한 숫자여야 합니다.") from exc
    if not all(value.is_finite() for value in (amount, annual_rate, elapsed)):
        raise ValueError("원금·금리·경과 시간은 유효한 숫자여야 합니다.")
    if amount <= 0 or elapsed <= 0:
        return Decimal(0)
    fraction = year_fraction_decimal(elapsed, convention, start_epoch)
    return amount * (annual_rate / Decimal(100)) * fraction


def calculate_compound_interest_decimal(principal, rate, periods, cycle_seconds,
                                        convention="ACT/365", start_epoch=None):
    """Calculate periodic compound interest without binary-float money math."""
    try:
        amount = Decimal(str(principal))
        annual_rate = Decimal(str(rate))
        cycle = Decimal(str(cycle_seconds))
        count = int(periods)
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("원금·금리·주기·횟수가 올바르지 않습니다.") from exc
    if not all(value.is_finite() for value in (amount, annual_rate, cycle)):
        raise ValueError("원금·금리·주기·횟수가 올바르지 않습니다.")
    if amount <= 0 or count <= 0 or cycle <= 0:
        return Decimal(0)
    total_fraction = year_fraction_decimal(count * cycle, convention, start_epoch)
    periodic_rate = (annual_rate / Decimal(100)) * total_fraction / count
    if periodic_rate <= -1:
        return -amount
    factor = Decimal(1) + periodic_rate
    return amount * (factor ** count - Decimal(1))
