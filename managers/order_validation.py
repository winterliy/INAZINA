"""Shared validation for limit orders that use integer prices and quantities."""


def validate_limit_order(side, quantity, limit_price, *, max_quantity, max_price,
                         quantity_label, price_label):
    side = str(side).strip().casefold()
    if side not in {"buy", "sell"}:
        return None, "❌ 주문 방향은 매수 또는 매도여야 합니다."
    if (isinstance(quantity, bool) or not isinstance(quantity, int)
            or not 1 <= quantity <= max_quantity):
        return None, f"❌ {quantity_label}은 1 이상의 정수여야 합니다."
    if (isinstance(limit_price, bool) or not isinstance(limit_price, int)
            or not 1 <= limit_price <= max_price):
        return None, f"❌ {price_label}은 1 이상의 정수여야 합니다."
    return (side, quantity, limit_price), None
