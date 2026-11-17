"""Futures margin held inside the product must equal the wallet cash that went into it (M4)."""
import time
import unittest

from database import pg_backend
from managers.accounting_journal import AccountingJournal
from tests import test_market_features as fixtures


class FuturesCashLinkTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown
    issue = fixtures.MarketFeatureTests.issue
    issue_future = fixtures.MarketFeatureTests.issue_future

    def _check(self, conn=None):
        def read(connection):
            return next(item for item in AccountingJournal.reconcile_subledgers(connection)
                        if item["ledger_scope"] == "futures_cash")
        if conn is not None:
            return read(conn)
        with self.db.readonly() as connection:
            return read(connection)

    def test_inactive_until_the_first_futures_cash_movement(self):
        check = self._check()
        self.assertEqual((check["status"], check["active"]), ("matched", False))

    def test_margin_fills_marks_cancels_liquidation_and_maturity_all_reconcile(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 1, 100)[0])
        check = self._check()
        self.assertEqual((check["status"], check["active"]), ("matched", True), check)
        self.assertGreater(check["held"], 0)
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 1, 50)[0])   # rests
        self.assertTrue(self.book.place_limit_order("bob", "FUT", "sell", 1, 200)[0])   # rests
        self.assertEqual(self._check()["status"], "matched", self._check())
        self.db.companies.update_share_price("CO", 90)                                  # mark to market
        self.futures.settle_positions()
        self.assertEqual(self._check()["status"], "matched", self._check())
        self.db.companies.update_share_price("CO", 70)                                  # liquidation
        self.futures.settle_positions()
        self.assertEqual(self._check()["status"], "matched", self._check())
        now = int(time.time())
        self.db.products.execute("UPDATE investment SET maturity_epoch=? WHERE product_code='FUT'", (now - 1,))
        self.futures.settle_positions(now=now)                                          # expiry refunds
        check = self._check()
        self.assertEqual(check["status"], "matched", check)
        self.assertEqual(check["held"], 0)
        self.assertGreater(check["event_count"], 3)

    def test_added_margin_reconciles(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 1, 100)[0])
        ok, message = self.futures.add_margin("alice", "FUT", 500)
        self.assertTrue(ok, message)
        self.assertEqual(self._check()["status"], "matched", self._check())

    @unittest.skipIf(pg_backend.enabled(), "raises inside an open transaction to undo tampering")
    def test_collateral_that_no_wallet_row_explains_is_reported(self):
        self.issue_future()
        self.assertTrue(self.book.place_limit_order("alice", "FUT", "buy", 1, 100)[0])
        with self.assertRaises(_Undo), self.db.atomic() as conn:
            conn.execute("UPDATE futures_positions SET collateral=collateral+7")
            self.check = self._check(conn)
            raise _Undo()
        self.assertEqual(self.check["status"], "mismatch")
        self.assertEqual(self.check["gaps"][0]["status"], "futures_cash_mismatch")
        self.assertEqual(self.check["gaps"][0]["difference"], "7")


class _Undo(Exception):
    pass


if __name__ == "__main__":
    unittest.main()
