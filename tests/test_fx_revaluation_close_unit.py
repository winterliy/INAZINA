import sqlite3
from fractions import Fraction
from unittest.mock import patch

import pytest

from managers.fx_revaluation import FxRevaluation, next_period_start, period_key

NOW = 1_800_000_000


def connection():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE fx_revaluation_runs (run_id INTEGER, kind TEXT, period_key TEXT, currency_code TEXT,
            decimal_places INTEGER, position_minor_units INTEGER, rate_text TEXT, pnl_base_minor_units INTEGER,
            recorded_epoch INTEGER, reverses_run_id INTEGER, previous_rate_text TEXT);
        CREATE TABLE fx_revaluation_corrections (correction_id INTEGER PRIMARY KEY, run_id INTEGER, revision INTEGER,
            decimal_places INTEGER, position_minor_units INTEGER, rate_text TEXT, pnl_base_minor_units INTEGER,
            delta_base_minor_units INTEGER, actor TEXT, recorded_epoch INTEGER);
        CREATE TABLE accounting_entries (entry_id INTEGER, ledger_scope TEXT, entity_type TEXT, entity_id TEXT,
            source_key TEXT, currency_code TEXT, decimal_places INTEGER, recorded_epoch INTEGER);
        CREATE TABLE accounting_lines (entry_id INTEGER, account_code TEXT, debit_units INTEGER, credit_units INTEGER);
    ''')
    return db


def check(db, position=1000, rate=Fraction(2)):
    with patch.object(FxRevaluation, 'positions', return_value={'USD': (2, position)}), \
         patch('managers.fx_position.base_currency_code', return_value='NWX'), \
         patch('managers.fx_position.currency_precision', return_value=0), \
         patch('managers.currency_manager.CurrencyManager.quote', return_value=rate):
        return FxRevaluation.close_check(db, NOW)


def test_close_requires_current_position_rate_and_matching_posting():
    db = connection()
    assert check(db)['status'] == 'mismatch'
    db.execute('INSERT INTO fx_revaluation_runs VALUES (1,?,?,?,?,?,?,?,?,NULL,?)',
               ('revalue', period_key(NOW), 'USD', 2, 1000, '2', 7, NOW, '13/10'))
    assert check(db)['status'] == 'mismatch'  # missing journal
    db.execute("INSERT INTO accounting_entries VALUES (1,'fx_revaluation','revaluation','1','run:1','NWX',0,?)", (NOW,))
    db.executemany('INSERT INTO accounting_lines VALUES (?,?,?,?)', [
        (1, 'fx_position_revaluation', 7, 0), (1, 'fx_revaluation_gain_loss', 0, 7)])
    assert check(db)['status'] == 'matched'
    assert check(db, position=1001)['status'] == 'mismatch'
    assert check(db, rate=Fraction(3))['status'] == 'mismatch'
    db.execute("UPDATE fx_revaluation_runs SET pnl_base_minor_units=8 WHERE run_id=1")
    db.execute("UPDATE accounting_lines SET debit_units=8 WHERE account_code='fx_position_revaluation'")
    db.execute("UPDATE accounting_lines SET credit_units=8 WHERE account_code='fx_revaluation_gain_loss'")
    assert any(item['status'] == 'calculation_mismatch' for item in check(db)['findings'])
    db.execute("UPDATE fx_revaluation_runs SET pnl_base_minor_units=7 WHERE run_id=1")
    db.execute("UPDATE accounting_lines SET debit_units=7 WHERE account_code='fx_position_revaluation'")
    db.execute("UPDATE accounting_lines SET credit_units=8 WHERE account_code='fx_revaluation_gain_loss'")
    assert any(item['status'] == 'posting_mismatch' for item in check(db)['findings'])
    db.close()


def test_baseline_and_zero_position_do_not_require_a_posting():
    db = connection()
    assert check(db, position=0)['status'] == 'matched'
    db.execute('INSERT INTO fx_revaluation_runs VALUES (1,?,?,?,?,?,?,?,?,NULL,NULL)',
               ('baseline', period_key(NOW), 'USD', 2, 1000, '2', 0, NOW))
    assert check(db)['status'] == 'matched'
    db.close()


def test_zero_position_run_cannot_hide_spurious_pnl():
    db = connection()
    db.execute('INSERT INTO fx_revaluation_runs VALUES (1,?,?,?,?,?,?,?,?,NULL,?)',
               ('revalue', period_key(NOW), 'USD', 2, 0, '2', 7, NOW, '1'))
    db.execute("INSERT INTO accounting_entries VALUES (1,'fx_revaluation','revaluation','1','run:1','NWX',0,?)", (NOW,))
    db.executemany('INSERT INTO accounting_lines VALUES (?,?,?,?)', [
        (1, 'fx_position_revaluation', 7, 0), (1, 'fx_revaluation_gain_loss', 0, 7)])
    assert any(item['status'] == 'calculation_mismatch' for item in check(db, position=0)['findings'])
    db.close()


def test_missing_prior_period_reversal_blocks_close_even_without_position():
    db = connection()
    db.execute('INSERT INTO fx_revaluation_runs VALUES (1,?,?,?,?,?,?,?,?,NULL,NULL)',
               ('revalue', '2000-01-01', 'USD', 2, 1000, '2', 7, NOW - 86400))
    result = check(db, position=0)
    assert any(item['status'] == 'missing_reversal' for item in result['findings'])
    epoch = next_period_start('2000-01-01')
    db.execute('INSERT INTO fx_revaluation_runs VALUES (2,?,?,?,?,?,?,?,?,?,NULL)',
               ('reverse', '2000-01-01', 'USD', 2, 1000, '2', -7, epoch, 1))
    db.execute("INSERT INTO accounting_entries VALUES (2,'fx_revaluation','revaluation','2','run:2','NWX',0,?)", (epoch,))
    db.executemany('INSERT INTO accounting_lines VALUES (?,?,?,?)', [
        (2, 'fx_position_revaluation', 0, 7), (2, 'fx_revaluation_gain_loss', 7, 0)])
    assert check(db, position=0)['status'] == 'matched'
    db.execute("UPDATE accounting_lines SET debit_units=8 WHERE entry_id=2 AND account_code='fx_revaluation_gain_loss'")
    assert any(item['status'] == 'reversal_mismatch' for item in check(db, position=0)['findings'])
    db.close()


def test_inactive_foreign_position_is_not_silently_omitted():
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE currencies (code TEXT, decimal_places INTEGER, status TEXT);
        CREATE TABLE accounting_entries (entry_id INTEGER, currency_code TEXT);
        CREATE TABLE accounting_lines (entry_id INTEGER, account_code TEXT, debit_units INTEGER, credit_units INTEGER);
        INSERT INTO currencies VALUES ('NWX', 0, 'active'), ('USD', 2, 'inactive');
        INSERT INTO accounting_entries VALUES (1, 'USD');
        INSERT INTO accounting_lines VALUES (1, 'fx_conversion_clearing', 0, 100);
    ''')
    with patch('managers.fx_position.base_currency_code', return_value='NWX'):
        with pytest.raises(ValueError, match='USD'):
            FxRevaluation.positions(db)
    db.close()
