"""Precision-preserving native amounts and SQLite/JSON scalar boundaries."""
from decimal import Decimal, DefaultContext, getcontext, InvalidOperation
from fractions import Fraction
from .amount_limits import DECIMAL_PRECISION, JS_SAFE_INTEGER

DefaultContext.prec = DECIMAL_PRECISION
getcontext().prec = DECIMAL_PRECISION


def decimal_value(value):
    if isinstance(value, bool):
        raise ValueError('금액에 참/거짓을 사용할 수 없습니다.')
    if isinstance(value, Fraction):
        return Decimal(value.numerator) / Decimal(value.denominator)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("금액을 확인해주세요.") from exc


class ExactDecimal(Decimal):
    """Decimal arithmetic that accepts legacy float operands via their decimal text."""
    def __new__(cls, value='0'):
        return super().__new__(cls, decimal_value(value))

    def _binary(self, other, operation, reverse=False):
        try:
            rhs = decimal_value(other)
        except (ValueError, TypeError):
            return NotImplemented
        lhs = Decimal(self)
        return ExactDecimal(operation(rhs, lhs) if reverse else operation(lhs, rhs))

    def __add__(self, other): return self._binary(other, Decimal.__add__)
    def __radd__(self, other): return self._binary(other, Decimal.__add__, True)
    def __sub__(self, other): return self._binary(other, Decimal.__sub__)
    def __rsub__(self, other): return self._binary(other, Decimal.__sub__, True)
    def __mul__(self, other): return self._binary(other, Decimal.__mul__)
    def __rmul__(self, other): return self._binary(other, Decimal.__mul__, True)
    def __truediv__(self, other): return self._binary(other, Decimal.__truediv__)
    def __rtruediv__(self, other): return self._binary(other, Decimal.__truediv__, True)
    def __floordiv__(self, other): return self._binary(other, Decimal.__floordiv__)
    def __rfloordiv__(self, other): return self._binary(other, Decimal.__floordiv__, True)
    def __mod__(self, other): return self._binary(other, Decimal.__mod__)
    def __rmod__(self, other): return self._binary(other, Decimal.__mod__, True)
    def __neg__(self): return ExactDecimal(Decimal.__neg__(self))
    def __abs__(self): return ExactDecimal(Decimal.__abs__(self))


def native_value(value):
    value = decimal_value(value)
    if not value.is_finite():
        raise ValueError('금액은 유한한 숫자여야 합니다.')
    return int(value) if value == value.to_integral_value() and abs(value) <= JS_SAFE_INTEGER else ExactDecimal(value)


def decimal_text(value):
    value = decimal_value(value)
    if not value.is_finite():
        raise ValueError('금액은 유한한 숫자여야 합니다.')
    if value == value.to_integral_value():
        return str(int(value))
    return format(value, 'f').rstrip('0').rstrip('.')


def json_amount(value):
    value = decimal_value(value)
    if value == value.to_integral_value() and abs(value) <= JS_SAFE_INTEGER:
        return int(value)
    return decimal_text(value)


# Plain sqlite clients used by maintenance/legacy paths also see this exact subclass.
# Never silently coerce an unsafe amount through a binary float.
def _legacy_sqlite_decimal(value):
    if abs(value)>JS_SAFE_INTEGER:
        raise ValueError('큰 금액은 정밀 금액 DB 연결을 사용해주세요.')
    return int(value) if value==value.to_integral_value() else float(value)

import sqlite3 as _sqlite3
_sqlite3.register_adapter(Decimal,_legacy_sqlite_decimal)
_sqlite3.register_adapter(ExactDecimal,_legacy_sqlite_decimal)
