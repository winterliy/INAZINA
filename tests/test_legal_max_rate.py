import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.interest_manager import InterestManager
from managers.rate_manager import RateManager
from functions.money import to_subminor_units


class LegalMaximumRateTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.users.create_user("admin", "Admin", "낙원", 1_000_000, role="admin")
        self.rates = RateManager(self.db)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def add_country(self, nationality, currency, cap):
        self.db.currency.execute(
            "INSERT OR IGNORE INTO currencies(code,name) VALUES (?,?)",
            (currency, currency),
        )
        self.db.currency.execute(
            "INSERT INTO country_currencies"
            "(nationality,currency_code,base_rate,legal_max_rate) VALUES (?,?,0,?)",
            (nationality, currency, cap),
        )

    def test_statutory_interest_is_removed(self):
        settings = {row["key"] for row in self.db.system.execute("SELECT key FROM settings")}
        country_columns = {
            row["name"] for row in self.db.currency.execute("PRAGMA table_info(country_currencies)")
        }
        loan_columns = {
            row["name"] for row in self.db.accounts.execute("PRAGMA table_info(loans)")
        }
        self.assertNotIn("statutory_rate", settings)
        self.assertNotIn("statutory_rate", country_columns)
        self.assertNotIn("last_statutory_epoch", loan_columns)
        self.assertNotIn("statutory_interest_remainder", loan_columns)
        self.assertEqual(self.rates.get_rate("legal_max_rate", "낙원"), 20)

    def test_currency_cap_only_blocks_loans_and_clamps_existing_loans(self):
        self.add_country("미국", "USD", 12)
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency) "
            "VALUES ('USD_SAVE','USD Saving',11,1,30,86400,1,'Bank','USD')"
        )
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
            "interest_cycle,min_credit_score,issuer,quote_currency) "
            "VALUES ('USD_LOAN','USD Loan',12,1000,30,86400,0,'Bank','USD')"
        )
        self.db.products.execute(
            "UPDATE saving SET interest_rate=13 WHERE product_code='USD_SAVE'"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "법정최고이자"):
            self.db.products.execute(
                "UPDATE liability SET interest_rate=13 WHERE product_code='USD_LOAN'"
            )
        result = self.rates.set_country_rate(
            "legal_max_rate", "미국", 8, "admin"
        )
        self.assertTrue(result[0], result[1])
        self.assertEqual(self.db.products.get_saving("USD_SAVE")["interest_rate"], 13)
        self.assertEqual(self.db.products.get_liability("USD_LOAN")["interest_rate"], 8)
        self.assertEqual(self.rates.get_rate("legal_max_rate", "미국"), 8)
        self.assertEqual(self.rates.get_rate("legal_max_rate", "낙원"), 20)

    def test_shared_currency_uses_strictest_country_cap(self):
        self.add_country("A국", "SHR", 15)
        self.add_country("B국", "SHR", 7)
        with self.db.readonly() as conn:
            cap, country = RateManager.legal_max_for_currency(conn, "SHR")
        self.assertEqual((cap, country), (7, "B국"))
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency) "
            "VALUES ('SHARED','Shared',8,1,30,86400,1,'Bank','SHR')"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "법정최고이자"):
            self.db.products.execute(
                "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
                "interest_cycle,min_credit_score,issuer,quote_currency) "
                "VALUES ('SHARED_LOAN','Shared',8,1000,30,86400,0,'Bank','SHR')"
            )

    def test_exchange_country_does_not_cap_bond_coupon(self):
        self.add_country("채권국", "BND", 6)
        self.db.system.execute(
            "INSERT INTO market_exchanges(exchange_id,status,updated_epoch,nationality) "
            "VALUES ('BOND-X','open',0,'채권국')"
        )
        self.db.products.execute(
            "INSERT INTO treasury_bond_products(product_code,name,annual_rate,maturity_days,"
            "face_value,exchange_id,market_price,total_supply) "
                "VALUES ('TB','Treasury',7,30,100,'BOND-X',100,1000)"
        )
        self.assertEqual(
            self.db.products.execute_one(
                "SELECT annual_rate FROM treasury_bond_products WHERE product_code='TB'"
            )[0],
            7,
        )

    def test_matured_loan_gets_contractual_overdue_interest(self):
        self.db.users.create_user("borrower", "Borrower", "낙원", 0)
        self.db.products.register_liability("LOAN", "Loan", 12, 10_000, 1, 86400, 0, "Bank")
        now = time.time()
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,created_at,"
            "last_payment_epoch,maturity_epoch) VALUES "
            "('borrower','LOAN',10000,10000,datetime(?,'unixepoch'),?,?)",
            (now - 3 * 86400, now - 2 * 86400, now - 2 * 86400),
        )
        InterestManager(self.db).process_user_interests("borrower", now=now)
        row = self.db.accounts.execute_one(
            "SELECT outstanding_balance,collection_status,overdue_interest_remainder,"
            "overdue_interest_remainder_subminor_units FROM loans WHERE user_id='borrower'"
        )
        self.assertGreater(row["outstanding_balance"], 10000)
        self.assertIn(row["collection_status"], {"grace", "overdue"})
        self.assertNotEqual(row["overdue_interest_remainder_subminor_units"], 0)
        self.assertEqual(
            row["overdue_interest_remainder_subminor_units"],
            to_subminor_units(row["overdue_interest_remainder"], 0),
        )


if __name__ == "__main__":
    unittest.main()
