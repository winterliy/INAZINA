"""Rounding dust of conversions: recognized into the 환전 손익 account, then movable to the treasury."""
import unittest

from managers.accounting_journal import AccountingJournal
from managers.fx_residual import FxResiduals
from tests import test_market_features as fixtures


class FxResidualTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def conversion(self, reference, source_minor, paid, numerator, denominator=1000):
        """A USD (2 places) -> base currency conversion snapshot paid rounded down."""
        self.db.currency.execute("INSERT OR IGNORE INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','Dollar','1400',2)")
        with self.db.atomic() as conn:
            conn.execute(
                "INSERT INTO cash_fx_settlements(kind,reference,user_id,source_currency,source_decimal_places,source_minor_units,"
                "currency,decimal_places,minor_units,rate_numerator,rate_denominator,recorded_epoch) "
                "VALUES ('test',?,?,'USD',2,?,'NWX',0,?,?,?,100)", (reference, "alice", source_minor, paid, str(numerator), str(denominator)))

    def report(self):
        with self.db.readonly() as conn:
            return {row["currency"]: row for row in FxResiduals.report(conn)}

    def test_dust_adds_up_across_conversions_and_whole_units_are_recognized_once(self):
        for reference in ("a", "b", "c"):                                                     # 1.00 USD at 1400.3 -> 1400 paid, 0.3 left
            self.conversion(reference, 100, 1400, 14003, 10)
        row = self.report()["NWX"]
        self.assertAlmostEqual(float(row["dust"]), 0.9, places=6)
        self.assertEqual((row["pending"], row["recognized"], row["balance"]), (0, 0, 0))     # still under one unit
        self.conversion("d", 100, 1400, 14003, 10)                                            # 1.2 in total
        self.assertEqual(self.report()["NWX"]["pending"], 1)
        with self.db.atomic() as conn:
            self.assertEqual(FxResiduals.recognize(conn, "admin", 100), [("NWX", 1)])
            self.assertEqual(FxResiduals.recognize(conn, "admin", 101), [])                    # nothing new: idempotent
        row = self.report()["NWX"]
        self.assertEqual((row["pending"], row["recognized"], row["balance"]), (0, 1, 1))
        with self.db.readonly() as conn:
            lines = conn.execute("SELECT l.account_code,l.debit_units,l.credit_units FROM accounting_lines l "
                                 "JOIN accounting_entries e ON e.entry_id=l.entry_id WHERE e.ledger_scope='fx_residual' ORDER BY l.line_id").fetchall()
        self.assertEqual(sorted(tuple(r) for r in lines), [("fx_gain_loss", 0, 1), ("fx_rounding_clearing", 1, 0)])

    def test_the_balance_can_be_moved_to_the_treasury_but_never_beyond_it(self):
        for index in range(5):
            self.conversion(f"d{index}", 100, 1400, 14005, 10)                                # 0.5 NWX each -> 2.5 dust
        with self.db.atomic() as conn:
            FxResiduals.recognize(conn, "admin", 100)
        self.assertEqual(self.report()["NWX"]["balance"], 2)
        with self.db.atomic() as conn:
            with self.assertRaises(ValueError):
                FxResiduals.transfer(conn, "admin", "NWX", 3, 200)
            treasury = FxResiduals.transfer(conn, "admin", "NWX", 1, 200)
        row = self.report()["NWX"]
        self.assertEqual((row["transferred"], row["balance"]), (1, 1))
        self.assertTrue(treasury)
        with self.db.readonly() as conn:
            business = AccountingJournal.reconcile_business_cash(conn)
            self.assertEqual([g for g in business["gaps"] if g.get("kind") == "fx_residual"], [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM business_cash_events WHERE kind='fx_residual'").fetchone()[0], 1)
            sums = conn.execute("SELECT SUM(l.debit_units),SUM(l.credit_units) FROM accounting_lines l JOIN accounting_entries e "
                                "ON e.entry_id=l.entry_id WHERE e.ledger_scope='fx_residual'").fetchone()
            self.assertEqual(tuple(sums), (3, 3))             # 2 recognized + 1 moved
            report = AccountingJournal.reconcile_all_financial_ledgers(conn)
            mine = [f for f in report["findings"] if "fx_residual" in str(f)]
            self.assertEqual(mine, [], mine)

    def test_rows_are_append_only(self):
        self.conversion("e", 100, 1400, 14005, 10)
        self.conversion("f", 100, 1400, 14005, 10)
        with self.db.atomic() as conn:
            FxResiduals.recognize(conn, "admin", 100)
        with self.assertRaises(Exception):
            with self.db.atomic() as conn:
                conn.execute("DELETE FROM fx_residual_clearings")
