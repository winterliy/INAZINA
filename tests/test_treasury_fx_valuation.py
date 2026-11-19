from fractions import Fraction
from unittest.mock import patch

from managers.accounting_journal import AccountingJournal


def test_fx_clearing_valuation_is_signed_and_currency_precise():
    positions = [
        {'currency_code': 'USD', 'decimal_places': 2, 'balance_units': -125},
        {'currency_code': 'NWX', 'decimal_places': 0, 'balance_units': 3},
    ]
    with patch('functions.money.base_currency_code', return_value='NWX'), \
         patch('functions.money.currency_precision', return_value=0), \
         patch('managers.currency_manager.CurrencyManager.quote', side_effect=lambda conn, source, target: Fraction(2) if source == 'USD' else Fraction(1)):
        report = AccountingJournal.treasury_fx_valuation(None, positions)
    assert report['base_currency'] == 'NWX'
    assert [row['base_minor_units'] for row in report['positions']] == [-2, 3]
    assert report['total_base_minor_units'] == 1
    assert report['simulation_only'] is True
