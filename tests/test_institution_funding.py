"""M6: wholesale funding ledger — bank bonds, interbank, repo and central-bank borrowing."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.bank_journal import BankJournal
from managers.funding_manager import FundingManager

DAY = 86400
NOW = 2_000_000_000


class InstitutionFundingTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','달러','1500',2)")
        self.db.currency.execute("INSERT INTO financial_institutions(issuer,nationality) VALUES ('Bank','낙원')")

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def _limit(self, conn, source, amount, currency="NWX"):
        FundingManager.set_limit(conn, "Bank", source, currency, amount, actor="admin", now=NOW)

    def _draw(self, conn, source="interbank", amount=1000, key="f1", **extra):
        terms = dict(rate_bps=365, maturity_days=10, counterparty="Other Bank", now=NOW)
        terms.update(extra)
        return FundingManager.draw(conn, "Bank", source, terms.pop("currency", "NWX"), amount,
                                   reference_key=key, **terms)

    def _balance(self, conn, account, currency="NWX"):
        return {row["account_code"]: row["balance_units"]
                for row in BankJournal.trial_balance(conn, "Bank", currency)}.get(account, 0)

    def test_draw_posts_a_balanced_liability_and_is_idempotent(self):
        with self.db.atomic() as conn:
            self._limit(conn, "interbank", 5000)
            funding_id = self._draw(conn)
            self.assertEqual(self._draw(conn), funding_id)  # same key and terms: nothing new
            self.assertEqual(self._balance(conn, "wholesale_funding_payable"), -1000)
            self.assertEqual(self._balance(conn, "settlement_asset"), 1000)
            self.assertEqual(BankJournal.audit_unbalanced(conn, "Bank"), [])
            with self.assertRaisesRegex(ValueError, "멱등성"):
                self._draw(conn, amount=2000)  # same key, different terms
            row = conn.execute("SELECT * FROM institution_funding WHERE funding_id=?", (funding_id,)).fetchone()
            self.assertEqual((row["source_type"], row["principal_minor_units"], row["status"],
                              row["maturity_epoch"]), ("interbank", 1000, "active", NOW + 10 * DAY))

    def test_limit_is_required_and_enforced_per_source_and_currency(self):
        with self.db.atomic() as conn:
            with self.assertRaisesRegex(ValueError, "한도"):
                self._draw(conn)  # no limit configured
            self._limit(conn, "interbank", 1500)
            self._draw(conn, key="a", amount=1000)
            with self.assertRaisesRegex(ValueError, "한도"):
                self._draw(conn, key="b", amount=501)
            self._draw(conn, key="c", amount=500)
            with self.assertRaisesRegex(ValueError, "한도"):
                self._draw(conn, source="bank_bond", key="d", amount=1)  # another source has its own limit

    def test_secured_sources_need_collateral_after_haircut(self):
        with self.db.atomic() as conn:
            self._limit(conn, "repo", 10_000)
            self._limit(conn, "central_bank", 10_000)
            with self.assertRaisesRegex(ValueError, "담보"):
                self._draw(conn, source="repo", key="r1", amount=1000)
            with self.assertRaisesRegex(ValueError, "담보"):
                self._draw(conn, source="central_bank", key="c1", amount=1000, collateral=1050, haircut_bps=500)
            funding_id = self._draw(conn, source="repo", key="r2", amount=1000, collateral=1053, haircut_bps=500)
            self.assertEqual(conn.execute("SELECT collateral_minor_units,haircut_bps FROM institution_funding "
                                          "WHERE funding_id=?", (funding_id,)).fetchone()[:], (1053, 500))

    def test_repayment_books_act_365_interest_once_and_hits_institution_results(self):
        with self.db.atomic() as conn:
            self._limit(conn, "interbank", 5000)
            funding_id = self._draw(conn, amount=1000, rate_bps=365)  # 3.65% a year = 0.01% a day
            repaid = FundingManager.repay(conn, funding_id, now=NOW + 10 * DAY)
            self.assertEqual((repaid["principal"], repaid["interest"]), (Decimal(1000), Decimal(1)))
            self.assertEqual(FundingManager.repay(conn, funding_id, now=NOW + 20 * DAY), repaid)  # replay
            self.assertEqual(self._balance(conn, "wholesale_funding_payable"), 0)
            self.assertEqual(self._balance(conn, "interest_expense"), 1)
            self.assertEqual(self._balance(conn, "settlement_asset"), -1)
            ledger = conn.execute("SELECT event_type,amount_minor_units FROM financial_institution_ledger "
                                  "WHERE issuer='Bank'").fetchall()
            self.assertEqual([tuple(row) for row in ledger], [("wholesale_funding_interest", -1)])
            results = BankJournal.reconcile_institution_results(conn, "Bank")
            self.assertTrue(all(row["status"] == "matched" for row in results), results)
            self.assertEqual(conn.execute("SELECT status FROM institution_funding").fetchone()[0], "repaid")

    def test_minor_unit_interest_in_a_decimal_currency(self):
        with self.db.atomic() as conn:
            self._limit(conn, "bank_bond", "1000.00", currency="USD")
            funding_id = self._draw(conn, source="bank_bond", amount="123.45", currency="USD",
                                    rate_bps=500, maturity_days=73)
            repaid = FundingManager.repay(conn, funding_id, now=NOW + 73 * DAY)
            # 123.45 * 5% * 73/365 = 1.2345 -> 1.23
            self.assertEqual(repaid["interest"], Decimal("1.23"))
            self.assertEqual(self._balance(conn, "interest_expense", "USD"), 123)

    def test_reconciliation_matches_and_detects_a_missing_or_tampered_row(self):
        with self.db.atomic() as conn:
            self._limit(conn, "interbank", 5000)
            self._draw(conn, key="a", amount=1000)
            self._draw(conn, key="b", amount=700)
            report = BankJournal.reconcile_all(conn, "Bank")
            self.assertIn("wholesale_funding", report["coverage"])
            self.assertEqual([row["status"] for row in report["checks"]["wholesale_funding"]], ["matched"])
            conn.execute("DROP TRIGGER trg_institution_funding_immutable_update")
            conn.execute("UPDATE institution_funding SET principal_minor_units=principal_minor_units+5 "
                         "WHERE reference_key='a'")
            rows = BankJournal.reconcile_all(conn, "Bank")["checks"]["wholesale_funding"]
            self.assertEqual([(row["status"], row["difference_units"]) for row in rows], [("mismatch", 5)])

    def test_profile_reports_cost_and_maturity_ladder(self):
        with self.db.atomic() as conn:
            for source in ("interbank", "bank_bond"):
                self._limit(conn, source, 100_000)
            self._draw(conn, key="short", amount=1000, rate_bps=200, maturity_days=5)
            self._draw(conn, source="bank_bond", key="long", amount=3000, rate_bps=400, maturity_days=400)
            profile = FundingManager.profile(conn, "Bank", now=NOW)
        nwx = profile["currencies"]["NWX"]
        self.assertEqual(nwx["outstanding_minor_units"], 4000)
        self.assertEqual(nwx["weighted_average_rate_bps"], Decimal(350))  # (1000*200+3000*400)/4000
        self.assertEqual({bucket["bucket"]: bucket["outstanding_minor_units"] for bucket in nwx["maturity_ladder"]},
                         {"0-7d": 1000, "8-30d": 0, "31-90d": 0, "91-365d": 0, "365d+": 3000})
        self.assertEqual({row["source_type"]: row["outstanding_minor_units"] for row in nwx["sources"]},
                         {"bank_bond": 3000, "interbank": 1000})

    def test_rows_and_events_are_append_only_except_repayment(self):
        with self.db.atomic() as conn:
            self._limit(conn, "interbank", 5000)
            self._draw(conn)
        for statement in ("DELETE FROM institution_funding", "UPDATE institution_funding SET rate_bps=1",
                          "DELETE FROM institution_funding_events",
                          "UPDATE institution_funding_events SET principal_minor_units=1"):
            with self.subTest(statement=statement), self.assertRaises(Exception):
                self.db.currency.execute(statement)


if __name__ == "__main__":
    unittest.main()
