"""Period-end revaluation of the open FX position, with an automatic reversing entry in the next period."""
import unittest
from fractions import Fraction

from managers.accounting_journal import AccountingJournal
from managers.currency_manager import CurrencyManager
from managers.fx_revaluation import FxRevaluation, next_period_start, period_key
from functions.money import base_currency_code
from tests import test_market_features as fixtures

DAY = 86400
T0 = 1_800_000_000


class FxRevaluationTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def open_position(self, units=1000):
        """A system FX position: 10.00 USD received into FX clearing and not yet paid out."""
        self.db.currency.execute("INSERT OR IGNORE INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','Dollar','1400',2)")
        with self.db.atomic() as conn:
            AccountingJournal.register_mappings(conn, "test_fx", [("fx_conversion_clearing", "fx_conversion_clearing"),
                                                                  ("customer_cash_payable", "customer_cash_payable")])
            AccountingJournal.post(conn, ledger_scope="test_fx", entity_type="t", entity_id="1", source_key="open",
                                   currency_code="USD", decimal_places=2, recorded_epoch=T0,
                                   lines=(("customer_cash_payable", "debit", units), ("fx_conversion_clearing", "credit", units)))

    def set_rate(self, value):
        self.db.currency.execute("UPDATE currencies SET reference_rate=? WHERE code='USD'", (str(value),))

    def quote(self):
        with self.db.readonly() as conn:
            return CurrencyManager.quote(conn, "USD", base_currency_code(conn))

    def run_at(self, now):
        with self.db.atomic() as conn:
            return FxRevaluation.run(conn, "admin", now)

    def lines(self):
        with self.db.readonly() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT e.recorded_epoch,l.account_code,l.debit_units,l.credit_units FROM accounting_lines l JOIN accounting_entries e "
                "ON e.entry_id=l.entry_id WHERE e.ledger_scope='fx_revaluation' ORDER BY e.entry_id,l.line_id")]

    def test_first_run_is_a_baseline_then_a_rate_change_is_booked_and_reversed_next_period(self):
        self.open_position()
        first = {c: k for c, k, _ in self.run_at(T0)}
        self.assertEqual(first, {"USD": "baseline"})
        self.assertEqual(self.lines(), [])
        q1 = self.quote()
        self.set_rate(1500)
        q2 = self.quote()
        self.assertGreater(q2, q1)
        expected = round(Fraction(10) * (q2 - q1))
        self.assertGreater(expected, 0)
        second = self.run_at(T0 + DAY)
        self.assertEqual(second, [("USD", "revalue", expected)])
        self.assertEqual(self.lines(), [(T0 + DAY, "fx_position_revaluation", expected, 0), (T0 + DAY, "fx_revaluation_gain_loss", 0, expected)])
        self.assertEqual(self.run_at(T0 + DAY + 100), [])                                            # same period: nothing again
        self.run_at(T0 + 2 * DAY)                                                                   # next period: the reversal is booked
        lines = self.lines()
        reversal_epoch = next_period_start(period_key(T0 + DAY))
        self.assertIn((reversal_epoch, "fx_position_revaluation", 0, expected), lines)
        self.assertIn((reversal_epoch, "fx_revaluation_gain_loss", expected, 0), lines)
        with self.db.atomic() as conn:
            self.assertEqual(FxRevaluation.reverse_due(conn, "admin", T0 + 3 * DAY), [])             # nothing left to undo

    def test_a_loss_is_booked_the_other_way_and_a_closed_position_books_nothing(self):
        self.open_position()
        self.run_at(T0)
        self.set_rate(1300)
        result = self.run_at(T0 + DAY)
        self.assertLess(result[0][2], 0)
        loss = -result[0][2]
        self.assertEqual(self.lines(), [(T0 + DAY, "fx_position_revaluation", 0, loss), (T0 + DAY, "fx_revaluation_gain_loss", loss, 0)])
        with self.db.atomic() as conn:                                                              # the position is paid out
            AccountingJournal.post(conn, ledger_scope="test_fx", entity_type="t", entity_id="2", source_key="close", currency_code="USD",
                                   decimal_places=2, recorded_epoch=T0 + DAY,
                                   lines=(("fx_conversion_clearing", "debit", 1000), ("customer_cash_payable", "credit", 1000)))
        self.set_rate(1100)
        self.assertEqual(self.run_at(T0 + 2 * DAY), [("USD", "revalue", 0)])

    def test_preview_matches_the_run_and_entries_balance_in_the_base_currency(self):
        self.open_position()
        self.run_at(T0)
        self.set_rate(1450)
        with self.db.readonly() as conn:
            preview = next(row for row in FxRevaluation.preview(conn) if row["currency"] == "USD")
        self.assertEqual(self.run_at(T0 + DAY), [("USD", "revalue", preview["pnl"])])
        with self.db.readonly() as conn:
            debit, credit = conn.execute("SELECT SUM(l.debit_units),SUM(l.credit_units) FROM accounting_lines l JOIN accounting_entries e "
                                         "ON e.entry_id=l.entry_id WHERE e.ledger_scope='fx_revaluation'").fetchone()
            self.assertEqual(debit, credit)
        with self.assertRaises(Exception):
            with self.db.atomic() as conn:
                conn.execute("DELETE FROM fx_revaluation_runs")
