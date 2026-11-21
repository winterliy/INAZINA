"""Held payouts that were not settled by their deadline, or were decided without a record, block the close."""
import unittest

from managers.accounting_journal import AccountingJournal
from managers.payout_manager import DAY, HOLD_DAYS, PayoutManager
from tests import test_market_features as fixtures


class PayoutCloseFindingTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def offer(self, reference, now):
        with self.db.atomic() as conn:
            return PayoutManager.offer(conn, "alice", "bob", "NWX", 5, "futures", reference, now=now)

    def findings(self, now):
        with self.db.readonly() as conn:
            return AccountingJournal.payout_findings(conn, now)

    def test_overdue_payout_blocks_until_it_is_returned_to_the_payer(self):
        start = 1_800_000_000
        payout = self.offer("r1", start)
        self.assertEqual(self.findings(start + 1), [])
        late = start + HOLD_DAYS * DAY + 1
        self.assertEqual(self.findings(late), [{"payout_id": payout, "status": "overdue_not_returned"}])
        with self.db.atomic() as conn:
            self.assertEqual(len(PayoutManager.expire_due(conn, late)), 1)
        self.assertEqual(self.findings(late), [])

    def test_decided_payout_without_a_decision_time_is_reported(self):
        payout = self.offer("r2", 1_800_000_000)
        with self.db.atomic() as conn:
            conn.execute("UPDATE payout_requests SET status='converted' WHERE payout_id=?", (payout,))
        self.assertEqual(self.findings(1_800_000_001), [{"payout_id": payout, "status": "decision_missing"}])

    def test_close_report_lists_the_scope(self):
        with self.db.readonly() as conn:
            report = AccountingJournal.reconcile_all_financial_ledgers(conn)
        self.assertIn("payout_requests", report["coverage"])
