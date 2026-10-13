import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.bank_manager import BankManager
from managers.interest_manager import InterestManager
from managers.rate_manager import RateManager


class RateInteractionTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.users.create_user("admin", "Admin", "낙원", 1_000_000, role="admin")
        self.db.users.create_user("user", "User", "낙원", 2_000_000)
        self.rates = RateManager(self.db)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def add_saving(self, code, *, floating=False, cycle=86400):
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency,rate_type,rate_nationality,"
            "base_rate_spread,repricing_cycle_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, code, 5, 1, 365, 86400, 1, "Bank", "NWX",
             "floating" if floating else "fixed", "낙원", 2, cycle),
        )

    def add_loan(self, code):
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
            "interest_cycle,min_credit_score,issuer,quote_currency,rate_type,rate_nationality,"
            "base_rate_spread,repricing_cycle_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, code, 5, 1_000_000, 365, 86400, 0, "Bank", "NWX",
             "floating", "낙원", 2, 86400),
        )

    def test_existing_products_and_contracts_migrate_as_fixed(self):
        self.add_saving("FIXED")
        self.assertTrue(BankManager(self.db).open_account(
            "user", "FIXED", "1234", 100_000
        )[0])
        self.db.products.execute(
            "UPDATE saving SET interest_rate=8 WHERE product_code='FIXED'"
        )
        account = self.db.accounts.execute_one(
            "SELECT contract_rate,rate_type FROM accounts WHERE user_id='user'"
        )
        self.assertEqual((account["contract_rate"], account["rate_type"]), (5, "fixed"))

    def test_base_rate_reprices_product_now_and_contract_on_reset(self):
        self.add_saving("FLOAT", floating=True)
        self.assertEqual(self.db.products.get_saving("FLOAT")["interest_rate"], 5)
        self.assertTrue(BankManager(self.db).open_account(
            "user", "FLOAT", "1234", 1_000_000
        )[0])
        account = self.db.accounts.execute_one(
            "SELECT * FROM accounts WHERE user_id='user'"
        )
        start = float(account["last_payment_epoch"])
        result = self.rates.set_country_rate(
            "base_rate", "낙원", 5, "admin", now=start+43200
        )
        self.assertTrue(result[0], result[1])
        self.assertEqual(self.db.products.get_saving("FLOAT")["interest_rate"], 7)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT contract_rate FROM accounts WHERE user_id='user'"
        )[0], 5)
        InterestManager(self.db).process_user_interests(
            now=start+2*86400, user_id="user"
        )
        account = self.db.accounts.execute_one(
            "SELECT contract_rate,next_repricing_epoch,balance FROM accounts WHERE user_id='user'"
        )
        self.assertEqual(account["contract_rate"], 7)
        self.assertEqual(account["next_repricing_epoch"], start+3*86400)
        self.assertGreater(account["balance"], 1_000_000)

    def test_legal_maximum_clamps_advertised_and_contract_rates(self):
        self.add_saving("FLOAT", floating=True)
        self.assertTrue(BankManager(self.db).open_account(
            "user", "FLOAT", "1234", 100_000
        )[0])
        self.assertTrue(self.rates.set_country_rate(
            "legal_max_rate", "낙원", 4, "admin"
        )[0])
        self.assertEqual(self.db.products.get_saving("FLOAT")["interest_rate"], 4)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT contract_rate FROM accounts WHERE user_id='user'"
        )[0], 4)

    def test_floating_loan_uses_contract_reset_not_immediate_product_change(self):
        self.add_loan("LOAN")
        self.assertTrue(BankManager(self.db).apply_loan(
            "user", "LOAN", 100_000
        )[0])
        loan = self.db.accounts.execute_one(
            "SELECT * FROM loans WHERE user_id='user'"
        )
        start = float(loan["last_payment_epoch"])
        self.assertTrue(self.rates.set_country_rate(
            "base_rate", "낙원", 5, "admin", now=start+43200
        )[0])
        self.assertEqual(self.db.products.get_liability("LOAN")["interest_rate"], 7)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT contract_rate FROM loans WHERE user_id='user'"
        )[0], 5)
        InterestManager(self.db).process_user_interests(
            now=start+2*86400, user_id="user"
        )
        loan = self.db.accounts.execute_one(
            "SELECT contract_rate,outstanding_balance FROM loans WHERE user_id='user'"
        )
        self.assertEqual(loan["contract_rate"], 7)
        self.assertGreater(loan["outstanding_balance"], 100_000)

    def test_direct_policy_edit_reprices_floating_product(self):
        self.add_saving("FLOAT", floating=True)
        self.db.currency.execute(
            "UPDATE country_currencies SET base_rate=6 WHERE nationality='낙원'"
        )
        self.assertEqual(self.db.products.get_saving("FLOAT")["interest_rate"], 8)
        history = self.db.currency.execute_one(
            "SELECT annual_percent FROM country_rate_history WHERE nationality='낙원' "
            "AND rate_key='base_rate' ORDER BY effective_epoch DESC LIMIT 1"
        )
        self.assertEqual(history[0], 6)

    def test_floating_reset_must_align_with_interest_cycle(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError, "재산정주기"):
            self.add_saving("BAD", floating=True, cycle=100_000)


if __name__ == "__main__":
    unittest.main()
