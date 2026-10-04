from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def calculate_income_tax(amount, tax_rate=0.03):
    """세율(소수 비율, 3%는 0.03)을 사용해 세액을 계산합니다."""
    try:
        amount = Decimal(str(amount))
        tax_rate = Decimal(str(tax_rate))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("세금 계산 값은 유한한 숫자여야 합니다.")
    if not amount.is_finite() or not tax_rate.is_finite():
        raise ValueError("세금 계산 값은 유한한 숫자여야 합니다.")
    if amount <= 0 or tax_rate < 0 or tax_rate > 1:
        return 0
    return int((amount * tax_rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
