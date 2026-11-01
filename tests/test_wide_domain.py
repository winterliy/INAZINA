"""Domain boundaries preserve native monetary precision above 100경."""
from decimal import Decimal
import sqlite3
import pytest
from functions.money import parse_currency_money, round_money, to_minor_units, from_minor_units
from managers.stochastic_price_engine import simulate_step, GarchConfig

@pytest.mark.parametrize('places', [0, 2, 6])
def test_wide_currency_boundary(places):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE currencies(code TEXT, decimal_places INTEGER)')
    conn.execute('INSERT INTO currencies VALUES (?, ?)', ('BIG', places))
    value = Decimal(10**18) + Decimal(1).scaleb(-places)
    parsed = parse_currency_money(conn, str(value), 'BIG')
    conn.close()
    assert Decimal(str(parsed)) == value
    assert Decimal(str(round_money(value, places))) == value
    assert from_minor_units(to_minor_units(parsed, places), places) == value

@pytest.mark.parametrize('value', ['1e25', 'NaN', 'Infinity', '1e999999999'])
def test_currency_boundary_rejects_excessive_amount(value):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE currencies(code TEXT, decimal_places INTEGER)')
    conn.execute("INSERT INTO currencies VALUES ('BIG', 6)")
    try:
        with pytest.raises(ValueError):
            parse_currency_money(conn, value, 'BIG')
    finally:
        conn.close()

def test_frozen_large_price_keeps_smallest_unit():
    price = Decimal('1000000000000000000.000001')
    step = simulate_step(price, None, None, GarchConfig(volatility=0), market_shock=0)
    assert Decimal(str(step.price)) == price

def test_expired_option_intrinsic_preserves_small_difference():
    from managers.option_manager import OptionManager
    assert OptionManager.theoretical_price(10**18 + 17, 10**18 + 1, 0, .2, 'call') == 16


def test_company_accounting_accepts_large_exact_minor_units():
    from managers.financial_manager import FinancialManager
    assert FinancialManager.units('1000000000000000000.01', 2) == 100000000000000000001


def test_corporate_split_allocates_large_fraction_without_float():
    from fractions import Fraction
    from managers.corporate_actions_manager import CorporateActionsManager
    result = CorporateActionsManager._part('2000000000000000000.02', Fraction(1, 2), 2)
    assert Decimal(str(result)) == Decimal('1000000000000000000.01')


def test_futures_margin_and_partial_allocation_preserve_large_units():
    from managers.futures_manager import FuturesManager
    assert FuturesManager._round_margin({'contract_size': 1}, 1, 10**18 + 1, 1) == 10**18 + 1
    assert FuturesManager._allocate_proportional_amount(2 * (10**18 + 1), 1, 2) == 10**18 + 1


def test_fx_micro_units_parse_large_fraction():
    from managers.currency_exchange_manager import amount_units
    assert amount_units('1000000000000000000.000001') == 1000000000000000000000001


def test_automatic_zero_range_target_preserves_large_fraction():
    from managers.auto_target_engine import AutoTargetConfig, sample_target, cycle_bounds
    price = Decimal('1000000000000000000.000001')
    config = AutoTargetConfig(range_percent=0, band_percent=0)
    cycle = sample_target(price, 100, config)
    assert Decimal(str(cycle.target_price)) == price
    assert cycle_bounds(cycle, config) == (price, price)


def test_lottery_monetary_price_uses_wide_limit_but_ticket_count_stays_bounded():
    from managers.lottery_manager import LotteryManager, DEFAULTS
    LotteryManager.validate({**DEFAULTS, 'ticket_price': 10**18 + 1})
    with pytest.raises(ValueError):
        LotteryManager.validate({**DEFAULTS, 'max_tickets': 10**18})


def test_flat_gap_fill_preserves_large_quote():
    import random
    from managers.price_gap_filler import bridge
    price = 10**18 + 1
    points = bridge(price, price, [(0, 5)], 0, random.Random(1))
    assert points
    assert all(value == price for _, value in points)
