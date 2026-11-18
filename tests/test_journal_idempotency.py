"""Rollback and rerun rules every business posting relies on: replays are harmless, failures leave nothing behind."""
import unittest
from unittest import mock

from managers.accounting_journal import AccountingJournal
from managers.fx_residual import FxResiduals
from managers.fx_revaluation import FxRevaluation
from tests import test_market_features as fixtures

T0 = 1_800_000_000


class JournalIdempotencyTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def entries(self, scope):
        with self.db.readonly() as conn:
            return conn.execute("SELECT COUNT(*) FROM accounting_entries WHERE ledger_scope=?", (scope,)).fetchone()[0]

    def post(self, conn, key="k1", amount=5):
        AccountingJournal.register_mappings(conn, "idem", [("a", "a"), ("b", "b")])
        return AccountingJournal.post(conn, ledger_scope="idem", entity_type="t", entity_id="1", source_key=key,
                                      currency_code="NWX", decimal_places=0, recorded_epoch=T0,
                                      lines=(("a", "debit", amount), ("b", "credit", amount)))

    def test_replaying_the_same_posting_adds_nothing_and_a_changed_replay_is_refused(self):
        with self.db.atomic() as conn:
            self.post(conn)
            self.post(conn)
        self.assertEqual(self.entries("idem"), 1)
        with self.assertRaises(Exception):
            with self.db.atomic() as conn:
                self.post(conn, amount=6)
        self.assertEqual(self.entries("idem"), 1)

    def test_a_failure_after_posting_rolls_the_whole_transaction_back(self):
        with self.assertRaises(RuntimeError):
            with self.db.atomic() as conn:
                self.post(conn, key="k2")
                raise RuntimeError("later step failed")
        self.assertEqual(self.entries("idem"), 0)

    def test_a_treasury_transfer_that_fails_while_posting_leaves_cash_and_records_untouched(self):
        self.db.currency.execute("INSERT OR IGNORE INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','Dollar','1400',2)")
        with self.db.atomic() as conn:
            for index in range(5):
                conn.execute(
                    "INSERT INTO cash_fx_settlements(kind,reference,user_id,source_currency,source_decimal_places,source_minor_units,"
                    "currency,decimal_places,minor_units,rate_numerator,rate_denominator,recorded_epoch) "
                    "VALUES ('test',?,?,'USD',2,100,'NWX',0,1400,'14005','10',100)", (f"x{index}", "alice"))
            FxResiduals.recognize(conn, "admin", T0)
        with self.db.readonly() as conn:
            before = conn.execute("SELECT COALESCE(SUM(cash_minor),0) FROM users").fetchone()[0]
        with mock.patch.object(AccountingJournal, "post", side_effect=RuntimeError("journal down")):
            with self.assertRaises(RuntimeError):
                with self.db.atomic() as conn:
                    FxResiduals.transfer(conn, "admin", "NWX", 1, T0 + 1)
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT COALESCE(SUM(cash_minor),0) FROM users").fetchone()[0], before)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fx_residual_clearings WHERE kind='transfer'").fetchone()[0], 0)

    def test_a_revaluation_that_fails_midway_stores_no_baseline_and_a_rerun_succeeds(self):
        self.db.currency.execute("INSERT OR IGNORE INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','Dollar','1400',2)")
        with mock.patch.object(FxRevaluation, "_last_rate", side_effect=RuntimeError("rate store down")):
            with self.assertRaises(RuntimeError):
                with self.db.atomic() as conn:
                    FxRevaluation.run(conn, "admin", T0)
        with self.db.readonly() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fx_revaluation_runs").fetchone()[0], 0)
        with self.db.atomic() as conn:
            self.assertTrue(FxRevaluation.run(conn, "admin", T0))
        with self.db.atomic() as conn:
            self.assertEqual(FxRevaluation.run(conn, "admin", T0 + 10), [])              # the same period is not revalued twice
