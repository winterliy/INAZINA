"""M7 regression baseline: contracts signed without a rate schedule settle to these exact minor-unit values.
Any change to the settlement path (e.g. the optional rate-schedule feature) must leave every number here untouched."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from database import DatabaseManager
from database.db_manager import DBManager
from managers.bank_manager import BankManager
from managers.interest_manager import InterestManager

T0 = 1_800_000_000
DAY = 86400


class ContractBaselineTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.users.create_user("admin", "Admin", "낙원", 1_000_000, role="admin")
        self.db.users.create_user("user", "User", "낙원", 5_000_000)
        self.db.currency.execute("UPDATE country_currencies SET legal_max_rate=100 WHERE nationality='낙원'")

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def product(self, table, code, floating, **extra):
        if table == "saving":
            self.db.products.execute(
                "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,interest_cycle,compound_type,issuer,quote_currency,"
                "rate_type,rate_nationality,base_rate_spread,repricing_cycle_seconds) VALUES (?,?,5,1,365,86400,1,'Bank','NWX',?,'낙원',2,86400)",
                (code, code, "floating" if floating else "fixed"))
        else:
            self.db.products.execute(
                "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer,quote_currency,"
                "rate_type,rate_nationality,base_rate_spread,repricing_cycle_seconds) VALUES (?,?,6,1000000,365,86400,0,'Bank','NWX',?,'낙원',2,86400)",
                (code, code, "floating" if floating else "fixed"))

    def test_settled_balances_are_unchanged(self):
        bank = BankManager(self.db)
        self.product("liability", "LFIX", False)
        self.product("liability", "LFLT", True)
        self.product("liability", "LANN", False)
        self.db.products.execute("UPDATE liability SET amortization_type='annuity',payment_frequency_days=30 WHERE product_code='LANN'")
        self.product("saving", "SFIX", False)
        self.product("saving", "SFLT", True)
        with mock.patch("time.time", return_value=float(T0)):      # contracts are signed at a fixed instant, not today's date
            for code in ("LFIX", "LFLT", "LANN"):
                self.assertTrue(bank.apply_loan("user", code, 100_000)[0], code)
            for code in ("SFIX", "SFLT"):
                self.assertTrue(bank.open_account("user", code, "1234", 100_000)[0], code)
        self.db.accounts.execute("UPDATE loans SET created_at=datetime(?,'unixepoch'),last_payment_epoch=?,maturity_epoch=?,next_repricing_epoch=?",
                                 (T0, T0, T0 + 365 * DAY, T0 + 2 * DAY))
        self.db.accounts.execute("UPDATE accounts SET created_at=datetime(?,'unixepoch'),last_payment_epoch=?,next_repricing_epoch=CASE WHEN rate_type='floating' THEN ? END",
                                 (T0, T0, T0 + 2 * DAY))
        InterestManager(self.db).process_user_interests(now=T0 + 30 * DAY, user_id="user")
        loans = {r["product_code"]: tuple(r) for r in self.db.accounts.execute(
            "SELECT product_code,outstanding_balance_minor_units,contract_rate,interest_remainder_subminor_units,last_payment_epoch FROM loans ORDER BY product_code")}
        accounts = {r["product_code"]: tuple(r) for r in self.db.accounts.execute(
            "SELECT product_code,balance,contract_rate FROM accounts ORDER BY product_code")}
        schedule = [tuple(r) for r in self.db.accounts.execute(
            "SELECT installment_number,principal_due,interest_due,fee_due FROM loan_payment_schedule WHERE loan_id=(SELECT loan_id FROM loans WHERE product_code='LANN') ORDER BY installment_number")]
        self.assertEqual(loans, {"LANN": ("LANN", 100494, 6.0, 0, 1802592000.0), "LFIX": ("LFIX", 100494, 6.0, 0, 1802592000.0),
                                 "LFLT": ("LFLT", 100541, 6.7, 0, 1802592000.0)})
        self.assertEqual(accounts, {"SFIX": ("SFIX", 100400, 5.0), "SFLT": ("SFLT", 100418, 5.0)})
        self.assertEqual((schedule[:3], len(schedule)), ([(1, 7449, 510, 0), (2, 7518, 441, 0), (3, 7540, 419, 0)], 13))
