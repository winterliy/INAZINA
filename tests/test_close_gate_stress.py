"""A mixed run of business operations must leave the books closable: every ledger reconciles and nothing is unclassified."""
import random
import unittest

from managers.accounting_journal import AccountingJournal
from tests import test_market_features as fixtures


class CloseGateStressTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown

    def reconcile(self):
        with self.db.readonly() as conn:
            return AccountingJournal.reconcile_all_financial_ledgers(conn)

    def test_transfers_and_trades_in_random_order_keep_every_ledger_matched(self):
        rng = random.Random(20261119)
        people = ["alice", "bob"]
        for index in range(6):
            name = f"user{index}"
            self.db.users.create_user(name, name.title(), "Nac", 50_000)
            people.append(name)
        from managers.company_account_manager import CompanyAccounts
        with self.db.atomic() as conn:                      # the issuer's cash lives in a company account, as for every listed company
            CompanyAccounts.account_id(conn, "CO")
        for name in people:
            self.db.assets.add_user_asset(name, "CO", 20, unit_cost=100)
        self.assertEqual(self.reconcile()["findings"], [])
        executed = {"transfer": 0, "trade": 0}
        for step in range(120):
            kind = rng.choice(("transfer", "trade", "trade"))
            a, b = rng.sample(people, 2)
            if kind == "transfer":
                ok, _ = self.users.transfer(a, b, rng.randint(1, 500))
                executed["transfer"] += bool(ok)
            else:
                price = rng.randint(90, 99)       # below the primary issue price, so fills are between citizens
                quantity = rng.randint(1, 3)
                sold = self.book.place_limit_order(a, "CO", "sell", quantity, price)
                bought = self.book.place_limit_order(b, "CO", "buy", quantity, price)
                executed["trade"] += bool(sold[0] and bought[0])
        self.assertGreater(executed["transfer"], 20)
        self.assertGreater(executed["trade"], 20)
        report = self.reconcile()
        self.assertEqual(report["findings"], [], report["findings"][:3])
        self.assertTrue(report["close_ready"])
        self.assertEqual(report["status"], "matched")
