import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from database import DatabaseManager
from database.db_manager import DBManager
from managers.bank_manager import BankManager
from managers.bank_journal import BankJournal
from managers.credit_risk_manager import CreditRiskManager
from managers.interest_manager import InterestManager
from managers.investment_manager import InvestmentManager
from managers.rate_manager import RateManager
from managers.treasury_bond_manager import TreasuryBondManager
from functions.interest_calc import year_fraction


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

    def add_loan(self, *, code="REAL", maturity=30, collateral_ratio=.1,
                 origination_fee=.5):
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
            "interest_cycle,min_credit_score,issuer,quote_currency,rate_type,rate_nationality,"
            "benchmark_key,benchmark_tenor_days,base_rate_spread,credit_spread_max,"
            "collateral_discount_max,collateral_required_ratio,origination_fee_percent,"
            "delinquency_spread,grace_period_days,acceleration_after_days,default_after_days) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, code, 5, 1_000_000, maturity, 86400, 0, "Bank", "NWX", "floating",
             "낙원", "bank_bond", 365, 1, 5, 2, collateral_ratio,
             origination_fee, 3, 0, 1, 2),
        )
        with self.db.atomic() as conn:
            self.rates.reprice_floating_products(conn, "낙원")

    def test_curve_interpolation_and_policy_pass_through(self):
        with self.db.readonly() as conn:
            self.assertAlmostEqual(
                RateManager.market_rate(conn, "낙원", "funding", 60), 3.3125
            )
        self.assertTrue(self.rates.set_country_rate(
            "base_rate", "낙원", 5, "admin"
        )[0])
        with self.db.readonly() as conn:
            # 30-day point passes 85%, 90-day point 80%; 60 days interpolates.
            self.assertAlmostEqual(
                RateManager.market_rate(conn, "낙원", "funding", 60), 4.9375
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

    def test_customer_deposit_journal_reconciliation_detects_balance_drift(self):
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency) VALUES "
            "('RECON','Reconciliation',0,1,365,31536000,0,'Bank','NWX')"
        )
        self.assertTrue(BankManager(self.db).open_account(
            "borrower", "RECON", "1234", 1_250
        )[0])
        with self.db.atomic() as conn:
            result = BankJournal.reconcile_customer_deposits(conn, "Bank")
        self.assertEqual(result, [{
            "issuer": "Bank", "currency_code": "NWX", "account_units": 1250,
            "journal_units": 1250, "difference_units": 0,
            "decimal_places": 0, "status": "matched",
        }])

        self.db.accounts.execute(
            "UPDATE accounts SET balance_units=balance_units+1000000 WHERE product_code='RECON'"
        )
        with self.db.atomic() as conn:
            result = BankJournal.reconcile_customer_deposits(conn, "Bank")
        self.assertEqual(result[0]["difference_units"], 1)
        self.assertEqual(result[0]["status"], "mismatch")

    def test_loan_receivable_reconciliation_uses_product_currency_minor_units(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('AAA','Test currency',2,2)"
        )
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code) VALUES ('AAA Nation','AAA')"
        )
        self.add_loan(code="FX-RECON")
        self.db.products.execute(
            "UPDATE liability SET quote_currency='AAA',rate_nationality='AAA Nation' "
            "WHERE product_code='FX-RECON'"
        )
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,quote_currency) "
            "VALUES ('borrower','FX-RECON',10.25,10.25,'active','AAA')"
        )
        with self.db.atomic() as conn:
            BankJournal.post(
                conn, "Bank", "loan_origination", "loan:fx-recon", "AAA",
                [("loans_receivable", "debit", "10.25"),
                 ("customer_funds_payable", "credit", "10.25")],
            )
            result = BankJournal.reconcile_loan_receivables(conn, "Bank")
        self.assertEqual(result, [{
            "issuer": "Bank", "currency_code": "AAA", "account_units": 1025,
            "journal_units": 1025, "difference_units": 0,
            "decimal_places": 2, "status": "matched",
        }])
        self.db.accounts.execute(
            "UPDATE loans SET outstanding_balance=outstanding_balance+0.01 "
            "WHERE product_code='FX-RECON'"
        )
        with self.db.atomic() as conn:
            result = BankJournal.reconcile_loan_receivables(conn, "Bank")
        self.assertEqual(result[0]["difference_units"], 1)
        self.assertEqual(result[0]["status"], "mismatch")

    def test_ecl_allowance_reconciliation_uses_currency_minor_units(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('AAA','Test currency',2,2)"
        )
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code) VALUES ('AAA Nation','AAA')"
        )
        self.add_loan(code="ECL-RECON")
        self.db.products.execute(
            "UPDATE liability SET quote_currency='AAA',rate_nationality='AAA Nation' "
            "WHERE product_code='ECL-RECON'"
        )
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,ecl_allowance,status,quote_currency) "
            "VALUES ('borrower','ECL-RECON',100,100,1.23,'active','AAA')"
        )
        with self.db.atomic() as conn:
            result = BankJournal.reconcile_ecl_allowances(conn, "Bank")
        self.assertEqual(result[0]["status"], "missing_journal")
        self.assertEqual(result[0]["account_units"], 123)

        with self.db.atomic() as conn:
            BankJournal.post(
                conn, "Bank", "expected_credit_loss", "loan:ecl-recon:ecl:day1", "AAA",
                [("impairment_expense", "debit", 1.23), ("ecl_allowance", "credit", 1.23)],
            )
            result = BankJournal.reconcile_ecl_allowances(conn, "Bank")
        self.assertEqual((result[0]["journal_units"], result[0]["difference_units"],
                          result[0]["status"]), (123, 0, "matched"))

        self.db.accounts.execute(
            "UPDATE loans SET ecl_allowance=1.24 WHERE product_code='ECL-RECON'"
        )
        with self.db.atomic() as conn:
            result = BankJournal.reconcile_ecl_allowances(conn, "Bank")
        self.assertEqual((result[0]["difference_units"], result[0]["status"]), (1, "mismatch"))

    def test_fully_repaid_loan_releases_its_ecl_allowance(self):
        self.add_loan(code="ECL-REPAY", maturity=365, origination_fee=0)
        success, message = BankManager(self.db).apply_loan(
            "borrower", "ECL-REPAY", 10_000, collateral_amount=1_000
        )
        self.assertTrue(success, message)
        loan = self.db.accounts.execute_one(
            "SELECT loan_id,outstanding_balance FROM loans WHERE product_code='ECL-REPAY'"
        )
        with self.db.atomic() as conn:
            conn.execute(
                "UPDATE accounts.loans SET ecl_allowance=250 WHERE loan_id=?",
                (loan["loan_id"],),
            )
            BankJournal.post(
                conn, "Bank", "expected_credit_loss", "loan:ecl-repay:assessment", "NWX",
                [("impairment_expense", "debit", 250), ("ecl_allowance", "credit", 250)],
            )

        bank = BankManager(self.db)
        success, message = bank.repay_loan(
            "borrower", "ECL-REPAY", 1_000, loan["loan_id"]
        )
        self.assertTrue(success, message)
        still_open = self.db.accounts.execute_one(
            "SELECT status,ecl_allowance,outstanding_balance FROM loans WHERE loan_id=?",
            (loan["loan_id"],),
        )
        self.assertEqual(still_open["status"], "active")
        self.assertGreater(still_open["ecl_allowance"], 0)

        success, message = bank.repay_loan(
            "borrower", "ECL-REPAY", still_open["outstanding_balance"], loan["loan_id"]
        )
        self.assertTrue(success, message)
        closed = self.db.accounts.execute_one(
            "SELECT status,ecl_allowance FROM loans WHERE loan_id=?", (loan["loan_id"],)
        )
        self.assertEqual((closed["status"], closed["ecl_allowance"]), ("paid", 0))
        with self.db.atomic() as conn:
            result = BankJournal.reconcile_ecl_allowances(conn, "Bank")
            p_and_l = BankJournal.reconcile_institution_results(conn, "Bank")
        self.assertEqual(result[0]["status"], "matched")
        self.assertEqual(result[0]["journal_units"], 0)
        release = next(row for row in p_and_l
                       if row["reference_key"].startswith(f"loan:{loan['loan_id']}:ecl-release:"))
        self.assertEqual(release["status"], "matched")

    def test_unposted_legacy_allowance_is_not_recognized_as_release_income(self):
        self.add_loan(code="ECL-LEGACY")
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,ecl_allowance,status,quote_currency) "
            "VALUES ('borrower','ECL-LEGACY',100,100,25,'active','NWX')"
        )
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL-LEGACY'"
        )[0]
        with self.db.atomic() as conn:
            released = BankManager._release_ecl_allowance(
                conn, loan_id, "Bank", "NWX", now=1, reason="legacy-close"
            )
            remaining = conn.execute(
                "SELECT ecl_allowance FROM loans WHERE loan_id=?", (loan_id,)
            ).fetchone()[0]
            journal = conn.execute(
                "SELECT COUNT(*) FROM bank_journal_entries WHERE reference_key LIKE ?",
                (f"loan:{loan_id}:ecl-release:%",),
            ).fetchone()[0]
        self.assertEqual((released, remaining, journal), (0, 0, 0))

    def test_scheduled_full_repayment_releases_ecl_allowance(self):
        self.add_loan(code="ECL-SCHEDULE", maturity=90, collateral_ratio=0,
                      origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET rate_type='fixed',interest_rate=0,"
            "amortization_type='annuity',payment_frequency_days=30 "
            "WHERE product_code='ECL-SCHEDULE'"
        )
        success, message = BankManager(self.db).apply_loan(
            "borrower", "ECL-SCHEDULE", 90_000
        )
        self.assertTrue(success, message)
        loan = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL-SCHEDULE'"
        )
        schedule = self.db.accounts.execute(
            "SELECT due_epoch FROM loan_payment_schedule WHERE loan_id=? ORDER BY installment_number",
            (loan["loan_id"],),
        )
        with self.db.atomic() as conn:
            conn.execute(
                "UPDATE accounts.loans SET ecl_allowance=25,contract_rate=0 WHERE loan_id=?",
                (loan["loan_id"],),
            )
            conn.execute(
                "UPDATE loan_payment_schedule SET principal_due=30000,interest_due=0,fee_due=0 "
                "WHERE loan_id=?",
                (loan["loan_id"],),
            )
            BankJournal.post(
                conn, "Bank", "expected_credit_loss", "loan:ecl-schedule:assessment", "NWX",
                [("impairment_expense", "debit", 25), ("ecl_allowance", "credit", 25)],
            )

        paid, missed, _ = BankManager(self.db).process_scheduled_loan_payments(
            now=float(schedule[-1]["due_epoch"])+1
        )
        self.assertEqual((paid, missed), (3, 0))
        closed = self.db.accounts.execute_one(
            "SELECT status,outstanding_balance,ecl_allowance FROM loans WHERE loan_id=?",
            (loan["loan_id"],),
        )
        self.assertEqual((closed["status"], closed["outstanding_balance"],
                          closed["ecl_allowance"]), ("paid", 0, 0))
        with self.db.atomic() as conn:
            result = BankJournal.reconcile_ecl_allowances(conn, "Bank")
        self.assertEqual(result[0]["status"], "matched")
        self.assertEqual(result[0]["journal_units"], 0)

    def test_withholding_tax_ledger_keeps_income_currency_precision(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('AAA','Test currency',2,2)"
        )
        self.db.currency.execute(
            "INSERT INTO country_tax_rules(nationality,income_source,tax_percent,updated_by,updated_epoch) "
            "VALUES ('낙원','treasury_bond_interest',10,'admin',1)"
        )
        from managers.user_manager import UserManager
        with self.db.atomic() as conn:
            tax = UserManager._tax(
                conn, "borrower", "treasury_bond_interest", "12.34", currency="AAA"
            )
        self.assertEqual(tax, 1.23)
        row = self.db.tax.execute_one(
            "SELECT gross_amount,tax_amount,net_amount,currency_code FROM tax_transactions "
            "WHERE user_id='borrower' AND source='treasury_bond_interest'"
        )
        self.assertEqual(tuple(row), (12.34, 1.23, 11.11, "AAA"))

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
        with self.db.atomic() as conn:
            deposit_reconciliation = BankJournal.reconcile_customer_deposits(conn, "Bank")
            result_reconciliation = BankJournal.reconcile_institution_results(conn, "Bank")
        self.assertEqual(deposit_reconciliation[0]["status"], "matched")
        self.assertEqual(result_reconciliation[0]["status"], "matched")
        self.assertEqual(result_reconciliation[0]["ledger_amount"], 1_000)

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
        self.assertEqual(row["origination_fee"], 500)
        self.assertEqual(row["collateral_amount"], 10_000)
        self.assertGreater(row["contract_apr"], row["contract_effective_rate"])
        institution = self.db.currency.execute_one(
            "SELECT retained_earnings FROM financial_institutions WHERE issuer='Bank'"
        )
        self.assertEqual(institution["retained_earnings"], 500)
        with self.db.atomic() as conn:
            reconciliation = BankJournal.reconcile_loan_receivables(conn, "Bank")
        self.assertEqual(reconciliation[0]["status"], "matched")
        self.assertEqual(reconciliation[0]["difference_units"], 0)
        repaid, message = BankManager(self.db).repay_loan("borrower", "REAL", 1_000)
        self.assertTrue(repaid, message)
        with self.db.atomic() as conn:
            reconciliation = BankJournal.reconcile_loan_receivables(conn, "Bank")
        self.assertEqual(reconciliation[0]["status"], "matched")
        self.db.accounts.execute(
            "UPDATE loans SET outstanding_balance=outstanding_balance+1 WHERE loan_id=?",
            (row["loan_id"],),
        )
        with self.db.atomic() as conn:
            reconciliation = BankJournal.reconcile_loan_receivables(conn, "Bank")
        self.assertEqual(reconciliation[0]["status"], "mismatch")
        self.assertEqual(reconciliation[0]["difference_units"], 1)

    def test_overdue_accelerates_defaults_and_seizes_collateral(self):
        self.add_loan(maturity=1, origination_fee=0)
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

    def test_apr_is_cashflow_irr(self):
        apr = RateManager.annual_percentage_rate(
            0, 0, 1_000, 365, cashflows=[(365, 1_100)]
        )
        self.assertAlmostEqual(apr, 10.0, places=7)

    def test_day_count_conventions_are_distinct(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        elapsed = 180*86400
        self.assertAlmostEqual(year_fraction(elapsed, "ACT/365", start), 180/365)
        self.assertAlmostEqual(year_fraction(elapsed, "ACT/360", start), .5)
        self.assertAlmostEqual(year_fraction(181*86400, "30/360", start), .5)

    def test_payment_schedule_uses_selected_day_count(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        act365 = BankManager._loan_cashflows(
            10_000, 10, 180, 180, "bullet", "ACT/365", start
        )
        act360 = BankManager._loan_cashflows(
            10_000, 10, 180, 180, "bullet", "ACT/360", start
        )
        self.assertEqual(act365[0][2], 493)
        self.assertEqual(act360[0][2], 500)

    def test_business_day_calendar_rolls_weekends_and_holidays(self):
        saturday = datetime(2026, 1, 3, 12, tzinfo=timezone.utc).timestamp()
        self.db.currency.execute(
            "INSERT INTO financial_holidays VALUES ('낙원','2026-01-05','holiday')"
        )
        with self.db.readonly() as conn:
            following = RateManager.adjust_business_day(
                conn, saturday, "낙원", "following"
            )
            preceding = RateManager.adjust_business_day(
                conn, saturday, "낙원", "preceding"
            )
        self.assertEqual(
            datetime.fromtimestamp(following, timezone.utc).date().isoformat(),
            "2026-01-06",
        )
        self.assertEqual(
            datetime.fromtimestamp(preceding, timezone.utc).date().isoformat(),
            "2026-01-02",
        )

    def test_early_withdrawal_claws_back_excess_interest(self):
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency,early_withdrawal_rate) "
            "VALUES ('EARLY','Early',10,1,365,31536000,0,'Bank','NWX',0)"
        )
        bank = BankManager(self.db)
        self.assertTrue(bank.open_account("borrower", "EARLY", "1234", 1_000)[0])
        account = self.db.accounts.execute_one(
            "SELECT account_number FROM accounts WHERE product_code='EARLY'"
        )
        self.db.accounts.execute(
            "UPDATE accounts SET balance_units=1100000000,principal=1000 WHERE account_number=?",
            (account["account_number"],),
        )
        treasury_before = self.db.users.get_treasury_balance()
        result = bank.close_account(
            "borrower", account["account_number"], password="1234"
        )
        self.assertTrue(result[0], result[1])
        self.assertIn("중도해지 조정 100", result[1])
        self.assertEqual(self.db.users.get_treasury_balance()-treasury_before, 100)

    def test_annuity_contract_creates_and_services_payment_schedule(self):
        self.add_loan(code="AMORT", maturity=90, collateral_ratio=0,
                      origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET rate_type='fixed',interest_rate=6,"
            "amortization_type='annuity',payment_frequency_days=30 "
            "WHERE product_code='AMORT'"
        )
        result = BankManager(self.db).apply_loan("borrower", "AMORT", 90_000)
        self.assertTrue(result[0], result[1])
        loan = self.db.accounts.execute_one(
            "SELECT * FROM loans WHERE product_code='AMORT'"
        )
        schedule = self.db.accounts.execute(
            "SELECT * FROM loan_payment_schedule WHERE loan_id=? ORDER BY installment_number",
            (loan["loan_id"],),
        )
        self.assertEqual(len(schedule), 3)
        self.assertEqual(sum(row["principal_due"] for row in schedule), 90_000)
        paid, missed, _ = BankManager(self.db).process_scheduled_loan_payments(
            now=float(schedule[0]["due_epoch"])+1
        )
        self.assertEqual((paid, missed), (1, 0))
        first = self.db.accounts.execute_one(
            "SELECT status,amount_paid,fee_paid,interest_paid,principal_paid "
            "FROM loan_payment_schedule WHERE schedule_id=?",
            (schedule[0]["schedule_id"],),
        )
        self.assertEqual(first["status"], "paid")
        self.assertGreater(first["amount_paid"], 0)
        self.assertEqual(
            first["amount_paid"],
            first["fee_paid"]+first["interest_paid"]+first["principal_paid"],
        )
        self.assertGreater(first["interest_paid"], 0)
        ok, message = BankManager(self.db).repay_loan(
            "borrower", "AMORT", 1_000, loan["loan_id"]
        )
        self.assertTrue(ok, message)
        second = self.db.accounts.execute_one(
            "SELECT amount_paid,fee_paid,interest_paid,principal_paid "
            "FROM loan_payment_schedule WHERE schedule_id=?",
            (schedule[1]["schedule_id"],),
        )
        self.assertEqual(
            second["amount_paid"],
            second["fee_paid"]+second["interest_paid"]+second["principal_paid"],
        )
        self.assertGreater(second["interest_paid"], 0)

    def test_floating_reset_recasts_unpaid_future_schedule(self):
        self.add_loan(code="RECAST", maturity=90, collateral_ratio=0,
                      origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET amortization_type='annuity',payment_frequency_days=30,"
            "repricing_cycle_seconds=86400,benchmark_key='bank_bond',"
            "benchmark_tenor_days=90 WHERE product_code='RECAST'"
        )
        bank = BankManager(self.db)
        result = bank.apply_loan("borrower", "RECAST", 90_000)
        self.assertTrue(result[0], result[1])
        loan = self.db.accounts.execute_one(
            "SELECT * FROM loans WHERE product_code='RECAST'"
        )
        before = self.db.accounts.execute_one(
            "SELECT rate_applied,interest_due FROM loan_payment_schedule "
            "WHERE loan_id=? ORDER BY installment_number LIMIT 1", (loan["loan_id"],)
        )
        self.assertTrue(self.rates.set_market_rate(
            "낙원", "bank_bond", 90, 12, "admin"
        )[0])
        InterestManager(self.db).process_user_interests(
            "borrower", now=float(loan["next_repricing_epoch"])+86400
        )
        after = self.db.accounts.execute_one(
            "SELECT rate_applied,interest_due,recast_epoch FROM loan_payment_schedule "
            "WHERE loan_id=? ORDER BY installment_number LIMIT 1", (loan["loan_id"],)
        )
        self.assertIsNotNone(after["recast_epoch"])
        self.assertNotEqual(after["rate_applied"], before["rate_applied"])
        self.assertGreater(after["interest_due"], before["interest_due"])
        reset = self.db.accounts.execute_one(
            "SELECT * FROM contract_rate_reset_history WHERE contract_type='loan' "
            "AND contract_id=? ORDER BY scheduled_epoch DESC LIMIT 1",
            (str(loan["loan_id"]),),
        )
        self.assertIsNotNone(reset)
        self.assertEqual(reset["benchmark_key"], "bank_bond")
        self.assertGreater(reset["installments_recast"], 0)

    def test_manual_prepayment_charges_snapshotted_declining_fee(self):
        self.add_loan(code="PREPAY", collateral_ratio=0, origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET rate_type='fixed',interest_rate=0,"
            "prepayment_fee_percent=2,prepayment_fee_decline_days=0 "
            "WHERE product_code='PREPAY'"
        )
        bank = BankManager(self.db)
        self.assertTrue(bank.apply_loan("borrower", "PREPAY", 1_000)[0])
        # Later product edits must not rewrite the signed contract.
        self.db.products.execute(
            "UPDATE liability SET prepayment_fee_percent=0 WHERE product_code='PREPAY'"
        )
        ok, message = bank.repay_loan("borrower", "PREPAY", 500)
        self.assertTrue(ok, message)
        self.assertIn("중도상환수수료 10 NWX", message)
        loan = self.db.accounts.execute_one(
            "SELECT outstanding_balance,prepayment_fees_paid FROM loans "
            "WHERE product_code='PREPAY'"
        )
        self.assertEqual((loan["outstanding_balance"], loan["prepayment_fees_paid"]),
                         (500, 10))
        ledger = self.db.currency.execute_one(
            "SELECT amount,currency_code FROM financial_institution_ledger "
            "WHERE event_type='prepayment_fee'"
        )
        self.assertEqual((ledger["amount"], ledger["currency_code"]), (10, "NWX"))

    def test_contract_amounts_follow_product_currency_precision(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('USD','Dollar','1',2)"
        )
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code) VALUES ('US','USD')"
        )
        self.db.users.create_user("usd-user", "USD user", "US", 100.125)
        self.assertEqual(self.db.users.get_user("usd-user")["cash"], 100.13)
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency) VALUES "
            "('USD-SAVE','USD saving',0,1.005,365,31536000,0,'US Bank','USD')"
        )
        self.assertEqual(
            self.db.products.get_saving("USD-SAVE")["deposit_amount"], 1.01
        )
        bank = BankManager(self.db)
        ok, message = bank.open_account("usd-user", "USD-SAVE", "1234", 10.55)
        self.assertTrue(ok, message)
        account = self.db.accounts.execute_one(
            "SELECT account_number,balance FROM accounts WHERE product_code='USD-SAVE'"
        )
        self.assertEqual(account["balance"], 10.55)
        self.assertEqual(self.db.users.get_user("usd-user")["cash"], 89.58)
        self.db.accounts.execute(
            "UPDATE accounts SET balance_units=1239000 WHERE account_number=?",
            (account["account_number"],),
        )
        self.assertEqual(
            self.db.accounts.get_account(account["account_number"])["balance"], 1.24
        )

    def test_reserve_and_capital_constraints_reject_excess_new_loan(self):
        self.add_loan(code="REGULATED", collateral_ratio=0, origination_fee=0)
        self.db.currency.execute(
            "UPDATE financial_institutions SET equity_capital=1000,retained_earnings=0,"
            "reserve_ratio=.1,target_capital_ratio=.1,status='active' WHERE issuer='Bank'"
        )
        result = BankManager(self.db).apply_loan(
            "borrower", "REGULATED", 100_000
        )
        self.assertFalse(result[0])
        self.assertIn("지급준비·자기자본", result[1])
        self.assertIsNone(self.db.accounts.execute_one(
            "SELECT 1 FROM loans WHERE product_code='REGULATED'"
        ))

    def test_simple_leverage_floor_restricts_deposit_heavy_institution(self):
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency) VALUES "
            "('LEV-SAVE','Leverage test',0,1,365,31536000,0,'Bank','NWX')"
        )
        self.assertTrue(BankManager(self.db).open_account(
            "borrower", "LEV-SAVE", "1234", 100_000
        )[0])
        self.db.currency.execute(
            "UPDATE financial_institutions SET equity_capital=1000,retained_earnings=0,"
            "target_capital_ratio=.01,status='active' WHERE issuer='Bank'"
        )
        with self.db.atomic() as conn:
            capacity = RateManager.institution_capacity(conn, "Bank", proposed_loan=1)
        self.assertAlmostEqual(capacity["leverage_ratio"], 1000/101000)
        self.assertEqual(capacity["minimum_leverage_ratio"], .03)
        self.assertEqual(capacity["leverage_headroom"], 0)
        self.assertEqual(capacity["status"], "restricted")
        self.assertFalse(capacity["allowed"])

    def test_failed_bank_pays_only_insured_limit_and_preserves_claim(self):
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency,deposit_insured) "
            "VALUES ('INSURED','Insured',0,1,365,31536000,0,'Bank','NWX',1)"
        )
        bank = BankManager(self.db)
        self.assertTrue(bank.open_account(
            "borrower", "INSURED", "1234", 1_000
        )[0])
        self.db.currency.execute(
            "UPDATE financial_institutions SET equity_capital=100,retained_earnings=0,"
            "deposit_insurance_limit=600,status='active' WHERE issuer='Bank'"
        )
        treasury_before = self.db.users.get_treasury_balance()
        with self.db.atomic(("users", "accounts", "product")) as conn:
            RateManager.record_institution_result(
                conn, "Bank", -200, "stress_loss", "test:failure", now=1000
            )
            resolved = BankManager.resolve_failed_institution(
                conn, "Bank", now=1000
            )
        self.assertEqual(resolved, {
            "accounts": 1, "covered": 600, "uninsured": 400,
        })
        account = self.db.accounts.execute_one(
            "SELECT balance,resolution_status,insurance_paid,uninsured_claim "
            "FROM accounts WHERE product_code='INSURED'"
        )
        self.assertEqual(
            (account["balance"], account["resolution_status"],
             account["insurance_paid"], account["uninsured_claim"]),
            (0, "resolved", 600, 400),
        )
        self.assertEqual(self.db.users.get_treasury_balance(), treasury_before-600)
        self.assertIsNotNone(self.db.accounts.execute_one(
            "SELECT 1 FROM deposit_insurance_claims WHERE account_number IN "
            "(SELECT account_number FROM accounts WHERE product_code='INSURED')"
        ))
        with self.db.atomic() as conn:
            reconciliation = BankJournal.reconcile_customer_deposits(conn, "Bank")
            journal = BankJournal.trial_balance(conn, "Bank", "NWX")
        self.assertEqual(reconciliation[0]["status"], "matched")
        balances = {row["account_code"]: row["balance_units"] for row in journal}
        self.assertEqual(balances.get("customer_deposits_payable", 0), 0)
        self.assertEqual(balances.get("deposit_insurance_payable", 0), -600)
        self.assertEqual(balances.get("uninsured_deposit_claim", 0), -400)
        from web_api.server import admin_bank_journal_report
        with patch("web_api.server.connect_readonly", self.db.readonly):
            report = admin_bank_journal_report("Bank")
        self.assertEqual(report["depositInsuranceClaims"][0]["covered_amount_text"], "600")
        self.assertEqual(report["depositInsuranceClaims"][0]["uninsured_amount_text"], "400")

    def test_competing_banks_and_balance_sheet_pressure_change_spread(self):
        self.add_loan(code="BANK1", collateral_ratio=0, origination_fee=0)
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
            "interest_cycle,min_credit_score,issuer,quote_currency) VALUES "
            "('BANK2','Bank 2',5,1000000,365,86400,0,'Other Bank','NWX')"
        )
        with self.db.readonly() as conn:
            before = RateManager.product_spread(
                conn, self.db.products.get_liability("BANK1"), "loan"
            )
        self.db.currency.execute(
            "UPDATE financial_institutions SET equity_capital=100000,"
            "target_loan_deposit_ratio=.1 WHERE issuer='Bank'"
        )
        self.db.currency.execute(
            "UPDATE financial_institutions SET equity_capital=1000,"
            "target_loan_deposit_ratio=.1 WHERE issuer='Other Bank'"
        )
        self.assertTrue(BankManager(self.db).apply_loan(
            "borrower", "BANK1", 100_000
        )[0])
        with self.db.readonly() as conn:
            after = RateManager.product_spread(
                conn, self.db.products.get_liability("BANK1"), "loan"
            )
        self.assertGreater(after, before)
        pressure = self.db.currency.execute_one(
            "SELECT adjustment FROM market_rate_pressure "
            "WHERE nationality='낙원' AND benchmark_key='bank_bond'"
        )
        self.assertGreater(pressure["adjustment"], 0)
        self.assertIsNotNone(self.db.currency.execute_one(
            "SELECT 1 FROM market_rate_pressure_history "
            "WHERE nationality='낙원' AND benchmark_key='bank_bond'"
        ))

    def test_macro_indicators_move_market_curves_without_changing_policy_rate(self):
        before = self.db.currency.execute_one(
            "SELECT base_rate FROM country_currencies WHERE nationality='낙원'"
        )["base_rate"]
        self.db.currency.execute(
            "UPDATE country_macro_indicators SET inflation_percent=10,"
            "real_gdp_growth_percent=-3,unemployment_percent=20,fiscal_debt_percent=160 "
            "WHERE nationality='낙원'"
        )
        with self.db.atomic() as conn:
            RateManager.refresh_market_pressure(conn, "낙원", now=1_800_000_000)
        pressures = {
            row["benchmark_key"]: row["adjustment"]
            for row in self.db.currency.execute(
                "SELECT benchmark_key,adjustment FROM market_rate_pressure "
                "WHERE nationality='낙원'"
            )
        }
        self.assertGreater(pressures["funding"], 0)
        self.assertGreater(pressures["bank_bond"], 0)
        self.assertGreater(pressures["treasury"], 0)
        self.assertEqual(self.db.currency.execute_one(
            "SELECT base_rate FROM country_currencies WHERE nationality='낙원'"
        )["base_rate"], before)

    def test_foreign_policy_rate_differential_increases_market_funding_pressure(self):
        domestic_rate = self.db.currency.execute_one(
            "SELECT base_rate FROM country_currencies WHERE nationality='낙원'"
        )["base_rate"]
        with self.db.atomic() as conn:
            RateManager.refresh_market_pressure(conn, "낙원", now=1_799_913_600)
        baseline = self.db.currency.execute_one(
            "SELECT adjustment FROM market_rate_pressure "
            "WHERE nationality='낙원' AND benchmark_key='funding'"
        )["adjustment"]
        self.db.currency.execute("INSERT INTO currencies(code,name) VALUES ('AGC','가국화')")
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code,base_rate) "
            "VALUES ('가국','AGC',10)"
        )
        with self.db.atomic() as conn:
            RateManager.refresh_market_pressure(conn, "낙원", now=1_800_000_000)
        pressure = self.db.currency.execute_one(
            "SELECT adjustment FROM market_rate_pressure "
            "WHERE nationality='낙원' AND benchmark_key='funding'"
        )["adjustment"]
        self.assertGreater(pressure, baseline)
        self.assertEqual(self.db.currency.execute_one(
            "SELECT base_rate FROM country_currencies WHERE nationality='낙원'"
        )["base_rate"], domestic_rate)

    def test_risk_weighted_assets_use_collateral_credit_state_and_default_bands(self):
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
            "interest_cycle,min_credit_score,issuer,quote_currency) "
            "VALUES ('RWA','RWA test',5,1000000,365,86400,0,'Bank','NWX')"
        )
        for user_id, score in (("rwa-good", 750), ("rwa-weak", 500), ("rwa-default", 750)):
            self.db.users.create_user(user_id, user_id, "낙원", 0)
            self.db.users.execute("UPDATE users SET credit_score=? WHERE user_id=?", (score, user_id))
        with self.db.atomic() as conn:
            conn.execute(
                "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,"
                "quote_currency,collateral_amount) VALUES "
                "('rwa-good','RWA',100000,100000,'active','NWX',0),"
                "('rwa-weak','RWA',100000,100000,'active','NWX',0),"
                "('rwa-default','RWA',100000,100000,'defaulted','NWX',20000)"
            )
            default_id = conn.execute("SELECT loan_id FROM loans WHERE user_id='rwa-default'").fetchone()[0]
            conn.execute(
                "INSERT INTO loan_collateral_assets(loan_id,user_id,asset_code,quantity,"
                "pledged_epoch,last_price,last_value,status) "
                "VALUES (?, 'rwa-default','TEST',1,1,50000,50000,'pledged')", (default_id,)
            )
            rwa = RateManager.institution_risk_weighted_assets(conn, "Bank")
            sheet = RateManager.institution_balance_sheet(conn, "Bank")
            stress = CreditRiskManager.portfolio_default_stress(conn, "Bank")
        self.assertEqual(rwa["amount"], 249_000)
        self.assertEqual(rwa["weightedComponents"]["performing_unsecured"], 75_000)
        self.assertEqual(rwa["weightedComponents"]["higher_risk_unsecured"], 100_000)
        self.assertEqual(rwa["weightedComponents"]["secured_asset"], 14_000)
        self.assertEqual(rwa["weightedComponents"]["defaulted_unsecured"], 60_000)
        self.assertEqual(sheet["risk_weighted_assets"], rwa["amount"])
        self.assertEqual(rwa["cashCollateralCovered"], 20_000)
        self.assertEqual(stress["performingEad"], 200_000)
        self.assertEqual(stress["estimatedFullDefaultLoss"], 130_000)
        self.assertEqual(stress["scenarios"][0]["defaultedEad"], 10_000)
        self.assertEqual(stress["scenarios"][0]["incrementalProvision"], 6_500)
        self.assertAlmostEqual(stress["scenarios"][0]["capitalConsumedPercent"], .65)
        self.db.currency.execute(
            "UPDATE financial_institutions SET equity_capital=20000,target_capital_ratio=.1 "
            "WHERE issuer='Bank'"
        )
        with self.db.atomic() as conn:
            capacity = RateManager.institution_capacity(conn, "Bank", proposed_loan=1)
        self.assertEqual(capacity["risk_weighted_assets"], 249_000)
        self.assertEqual(capacity["capital_headroom"], 0)
        self.assertFalse(capacity["allowed"])

    def test_asset_collateral_is_haircut_revalued_and_liquidated(self):
        self.add_loan(code="ASSETLOAN", collateral_ratio=1, origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET collateral_haircut=.2,margin_call_ltv=.85,"
            "liquidation_ltv=1 WHERE product_code='ASSETLOAN'"
        )
        self.db.assets.execute(
            "INSERT INTO market_assets(code,name,exchange_id,price,type,quote_currency) "
            "VALUES ('COLL','Collateral','EX',1000,'stock','NWX')"
        )
        self.db.assets.execute(
            "INSERT INTO user_assets(user_id,asset_code,quantity,average_cost) "
            "VALUES ('borrower','COLL',125,900)"
        )
        result = BankManager(self.db).apply_loan(
            "borrower", "ASSETLOAN", 100_000, 0, "COLL", 125
        )
        self.assertTrue(result[0], result[1])
        loan = self.db.accounts.execute_one(
            "SELECT * FROM loans WHERE product_code='ASSETLOAN'"
        )
        self.assertEqual(loan["collateral_asset_value"], 100_000)
        self.assertIsNone(self.db.assets.execute_one(
            "SELECT 1 FROM user_assets WHERE user_id='borrower' AND asset_code='COLL'"
        ))
        self.db.assets.execute("UPDATE market_assets SET price=500 WHERE code='COLL'")
        InterestManager(self.db).process_user_interests("borrower")
        loan = self.db.accounts.execute_one(
            "SELECT * FROM loans WHERE product_code='ASSETLOAN'"
        )
        # With no bid depth the resolution backstop applies the contractual
        # haircut, market-impact discount, and liquidation cost.
        self.assertGreater(loan["collateral_liquidated_value"], 0)
        self.assertLess(loan["collateral_liquidated_value"], 50_000)
        self.assertLess(loan["outstanding_balance"], 100_000)
        # The lending bank's head office takes the unsold rest and pays for it.
        bank = self.db.assets.execute_one(
            "SELECT b.user_id,u.cash FROM bank_accounts b JOIN users u ON u.user_id=b.user_id "
            "WHERE b.issuer=(SELECT issuer FROM liability WHERE product_code='ASSETLOAN')"
        )
        held = self.db.assets.execute_one(
            "SELECT quantity FROM user_assets WHERE user_id=? AND asset_code='COLL'", (bank["user_id"],)
        )
        self.assertEqual(held["quantity"], 125)
        self.assertLess(bank["cash"], 0)
        event = self.db.accounts.execute_one(
            "SELECT * FROM collateral_liquidation_events WHERE loan_id=?", (loan["loan_id"],)
        )
        self.assertEqual(event["book_filled_quantity"], 0)
        self.assertEqual(event["backstop_quantity"], 125)
        self.assertGreater(event["liquidation_cost"], 0)

    def test_income_dsr_and_repayment_history_affect_underwriting(self):
        self.db.users.execute(
            "UPDATE users SET annual_income=100000,income_stability=1 WHERE user_id='borrower'"
        )
        self.add_loan(code="DSR", collateral_ratio=0, origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET max_dsr_percent=50,amortization_type='bullet' "
            "WHERE product_code='DSR'"
        )
        denied = BankManager(self.db).apply_loan("borrower", "DSR", 60_000)
        self.assertFalse(denied[0])
        self.assertIn("DSR", denied[1])

        self.db.users.create_user("clean", "Clean", "낙원", 1_000_000)
        self.db.users.execute(
            "UPDATE users SET annual_income=1000000 WHERE user_id IN ('borrower','clean')"
        )
        self.add_loan(code="HISTORY", collateral_ratio=0, origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET borrower_history_spread_max=6 WHERE product_code='HISTORY'"
        )
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,"
            "missed_payment_count,contract_rate) VALUES ('borrower','OLD',1000,1000,'defaulted',5,5)"
        )
        self.assertTrue(BankManager(self.db).apply_loan(
            "borrower", "HISTORY", 10_000
        )[0])
        self.assertTrue(BankManager(self.db).apply_loan(
            "clean", "HISTORY", 10_000
        )[0])
        risky = self.db.accounts.execute_one(
            "SELECT contract_rate,borrower_risk_score,origination_pd_12m,underwriting_dsr,"
            "underwriting_stress_dsr FROM loans "
            "WHERE user_id='borrower' AND product_code='HISTORY'"
        )
        clean = self.db.accounts.execute_one(
            "SELECT contract_rate,borrower_risk_score,origination_pd_12m FROM loans "
            "WHERE user_id='clean' AND product_code='HISTORY'"
        )
        self.assertGreater(risky["contract_rate"], clean["contract_rate"])
        self.assertLess(risky["borrower_risk_score"], clean["borrower_risk_score"])
        self.assertGreaterEqual(risky["origination_pd_12m"], 0)
        self.assertGreaterEqual(clean["origination_pd_12m"], 0)
        self.assertLessEqual(risky["origination_pd_12m"], 1)
        self.assertIsNotNone(risky["underwriting_dsr"])
        self.assertGreater(risky["underwriting_stress_dsr"], risky["underwriting_dsr"])

    def test_underwriting_converts_existing_foreign_debt_to_home_currency(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('USD','US Dollar','2',2)"
        )
        self.add_loan(code="CROSS", collateral_ratio=0, origination_fee=0)
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,"
            "contract_rate,rate_type,quote_currency) "
            "VALUES ('borrower','OLD-USD',1000,1000,'active',5,'fixed','USD')"
        )
        product = self.db.products.get_liability("CROSS")
        with self.db.atomic() as conn:
            metrics = BankManager._underwriting_metrics(
                conn, "borrower", 1_000, product, 5, 1_800_000_000, 100_000
            )
            from managers.currency_manager import CurrencyManager, SCALE
            expected = CurrencyManager.convert(
                conn, 1_000*SCALE, "USD", "NWX"
            )/SCALE
        self.assertEqual(metrics["existing_balance"], expected)

    def test_country_stress_shock_is_applied_and_persisted_at_origination(self):
        self.add_loan(code="COUNTRY-SHOCK", maturity=365, collateral_ratio=0,
                      origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET max_dsr_percent=100 WHERE product_code='COUNTRY-SHOCK'"
        )
        self.db.users.execute(
            "UPDATE users SET annual_income=100000 WHERE user_id='borrower'"
        )
        self.db.currency.execute(
            "UPDATE country_macro_indicators SET credit_stress_shock_bps=400 "
            "WHERE nationality='낙원'"
        )
        self.assertTrue(BankManager(self.db).apply_loan(
            "borrower", "COUNTRY-SHOCK", 10_000
        )[0])
        loan = self.db.accounts.execute_one(
            "SELECT underwriting_dsr,underwriting_stress_dsr,"
            "underwriting_stress_shock_bps FROM loans WHERE product_code='COUNTRY-SHOCK'"
        )
        self.assertEqual(loan["underwriting_stress_shock_bps"], 400)
        self.assertAlmostEqual(
            loan["underwriting_stress_dsr"]-loan["underwriting_dsr"], .4, places=4
        )

    def test_fixed_rate_loan_has_no_repricing_stress_dsr(self):
        self.add_loan(code="FIXED-DSR", collateral_ratio=0, origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET rate_type='fixed' WHERE product_code='FIXED-DSR'"
        )
        product = self.db.products.get_liability("FIXED-DSR")
        with self.db.atomic() as conn:
            metrics = BankManager._underwriting_metrics(
                conn, "borrower", 10_000, product, 5, 1_800_000_000, 100_000, 400,
            )
        self.assertIsNone(metrics["stress_dsr"])

    def test_country_stress_shock_reprices_existing_variable_debt_in_dsr(self):
        now = 1_800_000_000
        self.add_loan(code="CANDIDATE", collateral_ratio=0, origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET rate_type='fixed' WHERE product_code='CANDIDATE'"
        )
        self.add_loan(code="EXISTING-FLOAT", collateral_ratio=0, origination_fee=0)
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,"
            "contract_rate,rate_type,quote_currency,next_repricing_epoch) "
            "VALUES ('borrower','EXISTING-FLOAT',10000,10000,'active',5,'floating','NWX',?)",
            (now+30*86400,),
        )
        self.db.users.execute(
            "UPDATE users SET annual_income=100000 WHERE user_id='borrower'"
        )
        product = self.db.products.get_liability("CANDIDATE")
        with self.db.atomic() as conn:
            metrics = BankManager._underwriting_metrics(
                conn, "borrower", 10_000, product, 5, now, 100_000, 400,
            )
        self.assertAlmostEqual(metrics["stress_dsr"]-metrics["dsr"], .4, places=4)

    def test_mandatory_finance_cost_is_included_in_legal_maximum(self):
        self.add_loan(code="COSTCAP", collateral_ratio=0, origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET interest_rate=5,maturity_days=365,"
            "mandatory_cost_percent=20 WHERE product_code='COSTCAP'"
        )
        result = BankManager(self.db).apply_loan("borrower", "COSTCAP", 10_000)
        self.assertFalse(result[0])
        self.assertIn("법정최고이자", result[1])

    def test_collateral_liquidation_consumes_resting_buy_bid(self):
        self.add_loan(code="BIDLOAN", collateral_ratio=1, origination_fee=0)
        self.db.products.execute(
            "UPDATE liability SET collateral_haircut=.2,margin_call_ltv=.85,"
            "liquidation_ltv=1,liquidation_cost_percent=2 WHERE product_code='BIDLOAN'"
        )
        self.db.assets.execute(
            "INSERT INTO market_assets(code,name,exchange_id,price,type,quote_currency) "
            "VALUES ('BIDCOLL','Bid Collateral','EX',1000,'stock','NWX')"
        )
        self.db.assets.execute(
            "INSERT INTO user_assets(user_id,asset_code,quantity,average_cost) "
            "VALUES ('borrower','BIDCOLL',125,900)"
        )
        self.db.users.create_user("buyer", "Buyer", "낙원", 0)
        self.db.assets.execute(
            "INSERT INTO market_orders(user_id,asset_code,side,limit_price,quantity,"
            "remaining_quantity,reserved_cash,order_kind,status,created_epoch,updated_epoch) "
            "VALUES ('buyer','BIDCOLL','buy',450,125,125,56250,'asset','open',1,1)"
        )
        self.assertTrue(BankManager(self.db).apply_loan(
            "borrower", "BIDLOAN", 100_000, 0, "BIDCOLL", 125
        )[0])
        self.db.assets.execute("UPDATE market_assets SET price=500 WHERE code='BIDCOLL'")
        InterestManager(self.db).process_user_interests("borrower")
        event = self.db.accounts.execute_one(
            "SELECT * FROM collateral_liquidation_events WHERE asset_code='BIDCOLL'"
        )
        self.assertEqual(event["book_filled_quantity"], 125)
        self.assertEqual(event["backstop_quantity"], 0)
        self.assertEqual(event["gross_proceeds"], 56_250)
        self.assertEqual(event["net_proceeds"], 55_125)
        self.assertEqual(self.db.assets.execute_one(
            "SELECT quantity FROM user_assets WHERE user_id='buyer' AND asset_code='BIDCOLL'"
        )["quantity"], 125)

    def test_periodic_bond_quote_has_clean_dirty_and_curve_mark(self):
        with self.db.readonly() as conn:
            quote = RateManager.bond_quote(
                conn, "낙원", 1_000, 6, 300,
                coupon_frequency_days=180, original_maturity_days=365,
            )
        self.assertGreater(quote["dirty_price"], quote["clean_price"])
        self.assertGreater(quote["accrued_interest"], 0)
        self.db.system.execute(
            "INSERT OR REPLACE INTO market_exchanges(exchange_id,status,updated_epoch,nationality) "
            "VALUES ('BOND','open',0,'낙원')"
        )
        self.db.products.execute(
            "INSERT INTO treasury_bond_products(product_code,name,annual_rate,maturity_days,"
            "face_value,exchange_id,market_price,total_supply,coupon_frequency_days) "
            "VALUES ('TB','Treasury',6,365,1000,'BOND',1000,100000,180)"
        )
        with self.db.readonly() as conn:
            bond = conn.execute(
                "SELECT * FROM treasury_bond_products WHERE product_code='TB'"
            ).fetchone()
            marked = TreasuryBondManager.theoretical_price(conn, bond)
        self.assertGreater(marked["dirty_price"], 0)
        self.assertNotEqual(round(marked["dirty_price"]), 1_000)

    def test_bond_quote_reports_curve_bump_duration_convexity_and_dv01(self):
        with self.db.readonly() as conn:
            quote = RateManager.bond_quote(
                conn, "낙원", 1_000, 6, 300,
                coupon_frequency_days=180, original_maturity_days=365,
            )
            bumped_up = RateManager.bond_quote(
                conn, "낙원", 1_000, 6, 300,
                coupon_frequency_days=180, original_maturity_days=365, spread=0.01,
            )
            bumped_down = RateManager.bond_quote(
                conn, "낙원", 1_000, 6, 300,
                coupon_frequency_days=180, original_maturity_days=365, spread=-0.01,
            )
        self.assertGreater(quote["macaulay_duration"], 0)
        self.assertGreater(quote["modified_duration"], 0)
        self.assertGreater(quote["convexity"], 0)
        self.assertGreater(quote["dv01"], 0)
        self.assertAlmostEqual(
            quote["modified_duration"],
            quote["dv01"]/(quote["dirty_price"]*0.0001),
        )
        self.assertAlmostEqual(quote["dv01"], (bumped_down["dirty_price"]-bumped_up["dirty_price"])/2)

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
