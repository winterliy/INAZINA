"""Exact proportional cost allocation for a position's rational total basis."""
from fractions import Fraction


def rounded_nonnegative(value):
    value = Fraction(value)
    if value < 0:
        raise ValueError('취득원가는 음수일 수 없습니다.')
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def remaining_cost_basis(total, old_quantity, new_quantity):
    old, new = Fraction(str(old_quantity)), Fraction(str(new_quantity))
    if old <= 0 or new < 0 or new > old:
        raise ValueError('원가 배분 수량을 확인해주세요.')
    return str(Fraction(str(total)) * new / old)


def valid_cost_basis(value):
    try:
        return int(Fraction(str(value)) >= 0)
    except (ValueError, ZeroDivisionError):
        return 0


def register(connection):
    connection.create_function('remaining_cost_basis', 3, remaining_cost_basis, deterministic=True)
    connection.create_function('valid_cost_basis', 1, valid_cost_basis, deterministic=True)
