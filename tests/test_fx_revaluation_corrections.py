"""A changed fixing or position is corrected without rewriting prior FX runs."""
import unittest
from unittest.mock import patch

from managers.accounting_journal import AccountingJournal
from managers.currency_manager import CurrencyManager
from managers.fx_revaluation import FxRevaluation, period_key
from tests.test_fx_revaluation import FxRevaluationTests, T0, DAY


class FxRevaluationCorrectionTests(unittest.TestCase):
    setUp = FxRevaluationTests.setUp
    tearDown = FxRevaluationTests.tearDown
    open_position = FxRevaluationTests.open_position
    set_rate = FxRevaluationTests.set_rate
    run_at = FxRevaluationTests.run_at

    def test_rate_and_position_corrections_are_idempotent_and_reverse(self):
        self.open_position()
        self.run_at(T0)
        self.set_rate(1500)
        first = self.run_at(T0 + DAY)
        self.assertEqual(first[0][1], 'revalue')
        self.set_rate(1600)
        with self.db.readonly() as conn:
            self.assertEqual(FxRevaluation.close_check(conn, T0 + DAY + 1)['status'], 'mismatch')
        changed = self.run_at(T0 + DAY + 1)
        self.assertEqual(changed[0][1], 'correction')
        self.assertEqual(self.run_at(T0 + DAY + 2), [])
        with self.db.atomic() as conn:
            AccountingJournal.post(conn, ledger_scope='test_fx', entity_type='t', entity_id='extra', source_key='extra',
                currency_code='USD', decimal_places=2, recorded_epoch=T0 + DAY + 3,
                lines=(('customer_cash_payable', 'debit', 100), ('fx_conversion_clearing', 'credit', 100)))
        self.assertEqual(self.run_at(T0 + DAY + 4)[0][1], 'correction')
        with self.db.readonly() as conn:
            self.assertEqual(FxRevaluation.close_check(conn, T0 + DAY + 4)['status'], 'matched')
            rows = conn.execute('SELECT revision,pnl_base_minor_units,delta_base_minor_units FROM fx_revaluation_corrections ORDER BY revision').fetchall()
            self.assertEqual([r[0] for r in rows], [1, 2])
            self.assertEqual(first[0][2] + sum(int(r[2]) for r in rows), int(rows[-1][1]))
        self.run_at(T0 + 2 * DAY)
        with self.db.readonly() as conn:
            self.assertEqual(FxRevaluation.close_check(conn, T0 + 2 * DAY)['status'], 'matched')
            reversals = conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope='fx_revaluation' AND source_key LIKE 'correction:%:reverse'").fetchone()[0]
        self.run_at(T0 + 2 * DAY + 1)
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope='fx_revaluation' AND source_key LIKE 'correction:%:reverse'").fetchone()[0], reversals)

    def test_correction_post_failure_rolls_back_even_if_caller_catches_it(self):
        self.open_position()
        self.run_at(T0)
        self.set_rate(1500)
        self.run_at(T0 + DAY)
        self.set_rate(1600)
        with self.db.atomic() as conn:
            with patch.object(AccountingJournal, 'post', side_effect=RuntimeError('posting failure')):
                with self.assertRaises(RuntimeError):
                    FxRevaluation.run(conn, 'admin', T0 + DAY + 1)
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM fx_revaluation_corrections').fetchone()[0], 0)
        self.assertEqual(self.run_at(T0 + DAY + 1)[0][1], 'correction')

    def test_corrections_are_listed_for_the_close_screen(self):
        self.open_position()
        self.run_at(T0)
        self.set_rate(1500)
        self.run_at(T0 + DAY)
        self.set_rate(1600)
        self.run_at(T0 + DAY + 1)
        with self.db.readonly() as conn:
            rows = FxRevaluation.corrections(conn)
        self.assertEqual([(r['currency_code'], r['revision'], r['period_key']) for r in rows], [('USD', 1, period_key(T0 + DAY))])
