import tempfile
import time
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.bank_manager import BankManager
from managers.interest_manager import InterestManager
from managers.investment_manager import InvestmentManager
from managers.rate_manager import RateManager


class RealisticRateSystemTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.users.create_user("admin", "Admin", "낙원", 1_000_000, role="admin")
        self.db.users.create_user("borrower", "Borrower", "낙원", 1_000_000)
        self.rates = RateManager(self.db)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def add_loan(self, *, code="REAL", maturity=30, collateral_ratio=.1):
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
            "interest_cycle,min_credit_score,issuer,quote_currency,rate_type,rate_nationality,"
            "benchmark_key,benchmark_tenor_days,base_rate_spread,credit_spread_max,"
            "collateral_discount_max,collateral_required_ratio,origination_fee_percent,"
            "delinquency_spread,grace_period_days,acceleration_after_days,default_after_days) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, code, 5, 1_000_000, maturity, 86400, 0, "Bank", "NWX", "floating",
             "낙원", "bank_bond", 365, 1, 5, 2, collateral_ratio, 1, 3, 0, 1, 2),
        )
        with self.db.atomic() as conn:
            self.rates.reprice_floating_products(conn, "낙원")

    def test_curve_interpolation_and_policy_pass_through(self):
        with self.db.readonly() as conn:
            self.assertAlmostEqual(
                RateManager.market_rate(conn, "낙원", "funding", 60), 3.275
            )
        self.assertTrue(self.rates.set_country_rate(
            "base_rate", "낙원", 5, "admin"
        )[0])
        with self.db.readonly() as conn:
            # 30-day point passes 85%, 90-day point 80%; 60 days interpolates.
            self.assertAlmostEqual(
                RateManager.market_rate(conn, "낙원", "funding", 60), 4.925
            )

    def test_market_rate_edit_reprices_linked_product(self):
        self.add_loan()
        result = self.rates.set_market_rate(
            "낙원", "bank_bond", 365, 6, "admin"
        )
        self.assertTrue(result[0], result[1])
        self.assertAlmostEqual(
            self.db.products.get_liability("REAL")["interest_rate"], 8.7
        )

    def test_negative_policy_rate_is_supported(self):
        result = self.rates.set_country_rate("base_rate", "낙원", -0.75, "admin")
        self.assertTrue(result[0], result[1])
        self.assertEqual(self.rates.get_rate("base_rate", "낙원"), -0.75)

    def test_negative_deposit_rate_debits_without_negative_tax(self):
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency) VALUES "
            "('NEG','Negative',-10,1,365,31536000,0,'Bank','NWX')"
        )
        self.assertTrue(BankManager(self.db).open_account(
            "borrower", "NEG", "1234", 10_000
        )[0])
        row = self.db.accounts.execute_one(
            "SELECT account_number,last_payment_epoch FROM accounts WHERE user_id='borrower'"
        )
        start = 1_000.0
        self.db.accounts.execute(
            "UPDATE accounts SET created_at=datetime(?,'unixepoch'),last_payment_epoch=? "
            "WHERE account_number=?", (start, start, row["account_number"])
        )
        InterestManager(self.db).process_user_interests(
            "borrower", now=start+31536000
        )
        self.assertEqual(self.db.accounts.get_account(row["account_number"])["balance"], 9_000)

    def test_contract_rate_uses_credit_collateral_fee_and_apr(self):
        self.add_loan()
        result = BankManager(self.db).apply_loan(
            "borrower", "REAL", 100_000, collateral_amount=10_000
        )
        self.assertTrue(result[0], result[1])
        row = self.db.accounts.execute_one(
            "SELECT * FROM loans WHERE user_id='borrower'"
        )
        product = self.db.products.get_liability("REAL")
        self.assertGreater(row["contract_rate"], product["interest_rate"])
        self.assertEqual(row["origination_fee"], 1_000)
        self.assertEqual(row["collateral_amount"], 10_000)
        self.assertGreater(row["contract_apr"], row["contract_effective_rate"])

    def test_overdue_accelerates_defaults_and_seizes_collateral(self):
        self.add_loan(maturity=1)
        self.assertTrue(BankManager(self.db).apply_loan(
            "borrower", "REAL", 100_000, collateral_amount=10_000
        )[0])
        loan = self.db.accounts.execute_one(
            "SELECT maturity_epoch FROM loans WHERE user_id='borrower'"
        )
        InterestManager(self.db).process_user_interests(
            "borrower", now=float(loan["maturity_epoch"])+3*86400
        )
        row = self.db.accounts.execute_one(
            "SELECT * FROM loans WHERE user_id='borrower'"
        )
        self.assertEqual((row["status"], row["collection_status"]),
                         ("defaulted", "defaulted"))
        self.assertEqual(row["collateral_seized"], 10_000)
        self.assertIsNotNone(row["accelerated_epoch"])
        self.assertIsNotNone(row["defaulted_epoch"])

    def test_nominal_effective_and_apr_are_distinct(self):
        effective = RateManager.effective_annual_rate(12, 30*86400)
        apr = RateManager.annual_percentage_rate(effective, 10_000, 990_000, 365)
        self.assertGreater(effective, 12)
        self.assertGreater(apr, effective)

    def test_corporate_bond_uses_tenor_curve_and_credit_liquidity_spreads(self):
        now = int(time.time())
        self.db.system.execute(
            "INSERT OR REPLACE INTO market_exchanges(exchange_id,status,updated_epoch,nationality) "
            "VALUES ('CB','open',0,'낙원')"
        )
        self.db.products.execute(
            "INSERT INTO investment(product_code,name,product_type,total_supply,unit_price,"
            "maturity_days,manager,exchange_id,maturity_epoch,face_value,annual_rate,"
            "credit_spread,liquidity_spread) VALUES "
            "('CB1','Bond','corporate_bond',100,1000,365,'Bank','CB',?,1000,5,2,1)",
            (now+365*86400,),
        )
        with self.db.readonly() as conn:
            product = conn.execute(
                "SELECT * FROM investment WHERE product_code='CB1'"
            ).fetchone()
            risky = InvestmentManager.linked_price(conn, product, 0, now)
        self.db.products.execute(
            "UPDATE investment SET credit_spread=0,liquidity_spread=0 WHERE product_code='CB1'"
        )
        with self.db.readonly() as conn:
            product = conn.execute(
                "SELECT * FROM investment WHERE product_code='CB1'"
            ).fetchone()
            safer = InvestmentManager.linked_price(conn, product, 0, now)
        self.assertLess(risky, safer)


if __name__ == "__main__":
    unittest.main()
