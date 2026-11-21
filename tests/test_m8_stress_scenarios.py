"""M8 composite stress: after each shock the books still balance (journals, every integrated ledger reconciles, nothing unclassified).
Each scenario also asserts that the shock really changed something, so a scenario cannot pass by doing nothing."""
import tempfile
import time
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.accounting_journal import AccountingJournal
from managers.bank_manager import BankManager
from managers.currency_exchange_manager import CurrencyExchangeManager
from managers.rate_manager import RateManager

DAY = 86400


class StressScenarioTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.bank = BankManager(self.db)
        self.rates = RateManager(self.db)
        self.db.users.create_user("admin", "Admin", "Nac", 5_000_000, role="admin")
        self.db.products.register_saving("SAV", "Saving", 2, 0, 0, 3600, 0, "Bank")
        self.db.products.register_liability("LN", "Loan", 5, 1_000_000, 90, 3600, 0, "Bank")
        self.people = []
        for index in range(6):
            name = f"u{index}"
            self.db.users.create_user(name, name, "Nac", 200_000)
            self.people.append(name)
        self.assertEqual(self.reconcile()["findings"], [])

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def reconcile(self):
        with self.db.readonly() as conn:
            return AccountingJournal.reconcile_all_financial_ledgers(conn)

    def assert_books_hold(self):
        report = self.reconcile()
        self.assertEqual(report["findings"], [], report["findings"][:3])
        self.assertTrue(report["close_ready"])
        self.assertEqual(report["status"], "matched")
        from managers.bank_journal import BankJournal
        with self.db.readonly() as conn:
            self.assertEqual(BankJournal.audit_unbalanced(conn), [])

    def open_deposits(self, amount=50_000):
        for name in self.people:
            ok, message = self.bank.open_account(name, "SAV", "pass1234", amount)
            self.assertTrue(ok, message)

    def loans(self, amount=100_000):
        for name in self.people[:4]:
            ok, message = self.bank.apply_loan(name, "LN", amount)
            self.assertTrue(ok, message)
        ids = [row[0] for row in self.db.accounts.execute("SELECT loan_id FROM loans")]
        self.assertEqual(len(ids), 4)
        return ids

    def test_1_rate_spike(self):
        self.loans()
        self.open_deposits()
        before = self.rates.get_rate("base_rate")
        ok, message = self.rates.set_rate("base_rate", 25, "admin")
        self.assertTrue(ok, message)
        self.assertNotEqual(self.rates.get_rate("base_rate"), before)
        for name in self.people:
            self.bank._settle_user_interest(name, now=time.time() + 30 * DAY)
        self.assert_books_hold()

    def test_2_deposit_run(self):
        self.open_deposits()
        numbers = [row[0] for row in self.db.accounts.execute("SELECT account_number FROM accounts WHERE user_id LIKE 'u%'")]
        self.assertEqual(len(numbers), 6)
        closed = 0
        for name in self.people:
            number = self.db.accounts.execute_one("SELECT account_number FROM accounts WHERE user_id=?", (name,))[0]
            ok, _ = self.bank.close_account(name, number, password="pass1234")
            closed += bool(ok)
        self.assertGreaterEqual(closed, 4)
        self.assert_books_hold()

    def test_3_correlated_defaults(self):
        ids = self.loans()
        from managers.credit_risk_manager import CreditRiskManager
        self.db.accounts.execute("UPDATE loans SET status='defaulted',collection_status='defaulted' WHERE loan_id IN (%s)" % ",".join(str(i) for i in ids[:3]))
        written = 0
        for number, loan_id in enumerate(ids[:3]):
            with self.db.atomic() as conn:
                result = CreditRiskManager.charge_off_loan(conn, loan_id=loan_id, event_key=f"stress-{number}", actor_id="admin", now=time.time())
            written += not result.get("duplicate")
        self.assertEqual(written, 3)
        self.assert_books_hold()

    def test_4_collateral_decline(self):
        self.db.companies.create_company("CO", "Company", "Industry", 100000, 1000, 100, "admin", "EX")
        for name in self.people[:3]:
            self.db.assets.add_user_asset(name, "CO", 50, unit_cost=100)
            ok, message = self.bank.apply_loan(name, "LN", 100_000, 0, "CO", 50)
            self.assertTrue(ok, message)
        self.assert_books_hold()
        self.db.companies.update_share_price("CO", 40)             # 100 -> 40: the pledged shares lose 60% of their value
        self.assert_books_hold()
        self.assertEqual(float(self.db.companies.execute_one("SELECT share_price FROM company WHERE company_code='CO'")[0]), 40)

    def test_5_fx_rate_jump(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','달러','1500',2),('EUR','유로','1600',2)")
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind,status) VALUES ('FX_EUR','EUR',0,'EUR','saving','active')")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','USD')")
        self.db.users.create_user("us", "us", "미국", 100_000)
        exchange = CurrencyExchangeManager(self.db)
        exchange.open_account("us", "FX_EUR")
        self.db.system.set_setting("currency_exchange_fee_percent", "0")
        base = exchange.national_currency("us")[1]
        traded = 0
        for rate in ("1600", "2400", "900", "1700"):
            self.db.currency.execute("UPDATE currencies SET reference_rate=? WHERE code='EUR'", (rate,))
            traded += "EUR" in str(exchange.exchange("us", base, "EUR", "1"))
        self.assertGreaterEqual(traded, 2)
        # Holding EUR after the jumps is an open FX position: the close gate must refuse until the period is revalued.
        self.assertIn("missing_run", [f["status"] for f in self.reconcile()["findings"]])
        from managers.fx_revaluation import FxRevaluation
        with self.db.atomic() as conn:
            FxRevaluation.run(conn, "admin", time.time())
        self.assert_books_hold()

    def test_6_settlement_failures(self):
        self.open_deposits(10_000)
        failed = 0
        for index in range(30):
            sender = self.people[index % 6]
            ok, _ = self.bank_transfer(sender, "nobody" if index % 2 else self.people[(index + 1) % 6], 5_000_000 if index % 3 else 100)
            failed += not ok
        self.assertGreater(failed, 10)
        self.assert_books_hold()

    def bank_transfer(self, sender, recipient, amount):
        from managers.user_manager import UserManager
        return UserManager(self.db).transfer(sender, recipient, amount)
