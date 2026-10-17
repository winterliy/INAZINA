import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.rate_manager import RateManager


class InstitutionMaturityGapTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,decimal_places) VALUES ('USD','US Dollar',2)"
        )
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency) "
            "VALUES ('S30','30 day deposit',1,0,30,86400,0,'Bank','NWX')"
        )
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
            "interest_cycle,min_credit_score,issuer,quote_currency) "
            "VALUES ('L90','90 day loan',5,10000,90,86400,0,'Bank','USD')"
        )
        self.now = 1_800_000_000
        self.db.accounts.execute(
            "INSERT INTO accounts(account_number,product_code,balance,principal,account_type,"
            "quote_currency,balance_units,created_at,resolution_status) "
            "VALUES ('A1','S30',500,500,'saving','NWX',0,datetime(?,'unixepoch'),'active')",
            (self.now,)
        )
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,"
            "created_at,maturity_epoch,quote_currency) "
            "VALUES ('u1','L90',1000,900,'active',datetime(?,'unixepoch'),?,'USD')",
            (self.now, self.now + 90 * 86400)
        )

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def test_contractual_maturity_gaps_remain_separated_by_currency(self):
        with self.db.readonly() as conn:
            report = RateManager.institution_maturity_gap(conn, "Bank", now=self.now)
        self.assertEqual({row["currency"] for row in report["ladders"]}, {"NWX", "USD"})
        by_currency = {row["currency"]: row["buckets"] for row in report["ladders"]}
        self.assertEqual(by_currency["NWX"][2]["outflows"], 500)
        self.assertEqual(by_currency["USD"][3]["inflows"], 900)
        self.assertEqual(by_currency["USD"][3]["cumulative_gap"], 900)
        self.assertIn("no behavioral runoff", report["method"])

    def test_empty_or_invalid_request_is_safe(self):
        with self.db.readonly() as conn:
            self.assertEqual(RateManager.institution_maturity_gap(conn, "Missing", now=self.now)["ladders"], [])
            with self.assertRaises(ValueError):
                RateManager.institution_maturity_gap(conn, "Bank", now=float("nan"))


if __name__ == "__main__":
    unittest.main()
