import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.bank_manager import BankManager
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
        self.assertEqual(row["origination_fee"], 500)
        self.assertEqual(row["collateral_amount"], 10_000)
        self.assertGreater(row["contract_apr"], row["contract_effective_rate"])
        institution = self.db.currency.execute_one(
            "SELECT retained_earnings FROM financial_institutions WHERE issuer='Bank'"
        )
        self.assertEqual(institution["retained_earnings"], 500)

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
            "UPDATE accounts SET balance=1100,principal=1000 WHERE account_number=?",
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
            "SELECT status,amount_paid FROM loan_payment_schedule WHERE schedule_id=?",
            (schedule[0]["schedule_id"],),
        )
        self.assertEqual(first["status"], "paid")
        self.assertGreater(first["amount_paid"], 0)

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
        self.assertEqual(loan["collateral_liquidated_value"], 50_000)
        self.assertLess(loan["outstanding_balance"], 100_000)
        treasury = self.db.assets.execute_one(
            "SELECT quantity FROM user_assets WHERE user_id='20230216' AND asset_code='COLL'"
        )
        self.assertEqual(treasury["quantity"], 125)

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
