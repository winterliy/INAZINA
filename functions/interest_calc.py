import math

YEAR_SECONDS = 365 * 24 * 60 * 60


def year_fraction(elapsed_seconds, convention="ACT/365"):
    """Convert elapsed seconds to a contractual year fraction."""
    seconds = max(0.0, float(elapsed_seconds or 0))
    convention = str(convention or "ACT/365").upper()
    if convention in {"ACT/360", "30/360"}:
        return seconds / (360 * 24 * 60 * 60)
    return seconds / YEAR_SECONDS


def calculate_simple_interest(principal, rate, elapsed_seconds, convention="ACT/365"):
    """연이율(%) 기준 단리. elapsed_seconds는 실제 초 단위 경과 시간입니다."""
    principal = float(principal)
    rate = float(rate)
    elapsed_seconds = float(elapsed_seconds)
    if principal <= 0 or elapsed_seconds <= 0:
        return 0.0
    return principal * (rate / 100.0) * year_fraction(elapsed_seconds, convention)


def calculate_compound_interest(principal, rate, periods, cycle_seconds):
    """정해진 초 단위 주기를 periods회 적용한 복리 이자(세전)를 반환합니다."""
    principal = float(principal)
    rate = float(rate)
    periods = int(periods)
    cycle_seconds = float(cycle_seconds)
    if principal <= 0 or periods <= 0 or cycle_seconds <= 0:
        return 0.0
    periodic_rate = (rate / 100.0) * (cycle_seconds / YEAR_SECONDS)
    if periodic_rate <= -1:
        return -principal
    exponent = periods * math.log1p(periodic_rate)
    if exponent > 700:
        raise OverflowError("복리 계산 결과가 너무 큽니다.")
    return principal * math.expm1(exponent)
