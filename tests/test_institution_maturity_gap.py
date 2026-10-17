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
            "INSERT INTO currencies(code,name,decimal_places,reference_rate) "
            "VALUES ('USD','US Dollar',2,'1')"
        )
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code) VALUES ('USA','USD')"
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
        self.db.products.execute(
            "UPDATE saving SET rate_type='floating',rate_nationality='낙원',"
            "repricing_cycle_seconds=86400 WHERE product_code='S30'"
        )
        self.db.products.execute(
            "UPDATE liability SET rate_type='floating',rate_nationality='USA',"
            "repricing_cycle_seconds=86400 WHERE product_code='L90'"
        )
        self.db.users.create_user("u1", "U1", "낙원", 0)
        self.now = 1_800_000_000
        self.db.accounts.execute(
            "INSERT INTO accounts(account_number,user_id,product_code,balance,principal,account_type,"
            "quote_currency,balance_units,created_at,resolution_status,next_repricing_epoch,rate_type,"
            "rate_nationality,repricing_cycle_seconds) "
            "VALUES ('A1','u1','S30',500,500,'saving','NWX',0,datetime(?,'unixepoch'),'active',?,'floating','낙원',86400)",
            (self.now, self.now + 10 * 86400)
        )
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,"
            "created_at,maturity_epoch,quote_currency,rate_type,next_repricing_epoch,rate_nationality,"
            "repricing_cycle_seconds,contract_rate,rate_floor,rate_cap) "
            "VALUES ('u1','L90',1000,900,'active',datetime(?,'unixepoch'),?,'USD','floating',?,'USA',86400,5,3,5.5)",
            (self.now, self.now + 90 * 86400, self.now + 30 * 86400)
        )
        self.db.accounts.execute(
            "INSERT INTO loan_payment_schedule(loan_id,installment_number,due_epoch,"
            "principal_due,interest_due,fee_due,amount_paid,status) "
            "VALUES (1,1,?,100,10,2,12,'scheduled')", (self.now+20*86400,)
        )

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def test_contractual_maturity_gaps_remain_separated_by_currency(self):
        with self.db.readonly() as conn:
            report = RateManager.institution_maturity_gap(conn, "Bank", now=self.now)
            rate_stress = RateManager.institution_rate_stress(conn, "Bank", now=self.now)
            liquidity = RateManager.institution_liquidity_stress(conn, "Bank", now=self.now)
            fx_stress = RateManager.institution_fx_stress(conn, "Bank", now=self.now)
        self.assertEqual({row["currency"] for row in report["ladders"]}, {"NWX", "USD"})
        by_currency = {row["currency"]: row["buckets"] for row in report["ladders"]}
        self.assertEqual(by_currency["NWX"][2]["outflows"], 500)
        self.assertEqual(by_currency["USD"][3]["inflows"], 900)
        self.assertEqual(by_currency["USD"][3]["cumulative_gap"], 900)
        nwx_stress = next(row for row in report["ladders"] if row["currency"] == "NWX")["depositRunoffStress"]
        self.assertEqual([row["assumedRunoff"] for row in nwx_stress], [50, 125, 250])
        self.assertEqual([row["oneDayGap"] for row in nwx_stress], [-50, -125, -250])
        self.assertIn("behavioral calibration", report["method"])
        stress_by_currency = {row["currency"]: row for row in rate_stress["scenarios"]}
        nwx_up = next(row for row in stress_by_currency["NWX"]["shocks"] if row["basisPoints"] == 200)
        usd_up = next(row for row in stress_by_currency["USD"]["shocks"] if row["basisPoints"] == 200)
        self.assertEqual(nwx_up["liabilityInterestChange"], 1)
        self.assertEqual(usd_up["assetInterestChange"], 0.74)
        self.assertEqual(usd_up["netInterestIncomeChange"], 0.74)
        self.assertEqual(liquidity["eligibleInflows"], 50)
        self.assertEqual(liquidity["depositorConcentration"]["largestDepositorExposure"], 500)
        self.assertEqual(liquidity["depositorConcentration"]["largestDepositorSharePercent"], 100)
        self.assertEqual(liquidity["depositorConcentration"]["topFiveDepositorSharePercent"], 100)
        largest_stress = next(
            row for row in liquidity["concentrationScenarios"]
            if row["group"] == "largest_depositor"
        )
        self.assertEqual(largest_stress["depositOutflow"], 500)
        self.assertEqual(largest_stress["netOutflow"], 450)
        stress_20 = next(row for row in liquidity["scenarios"] if row["runoffPercent"] == 20)
        self.assertEqual(stress_20["depositOutflow"], 100)
        self.assertEqual(stress_20["netOutflow"], 50)
        self.assertGreater(stress_20["coveragePercent"], 100)
        self.assertIn("not LCR", liquidity["method"])
        self.assertEqual(len(fx_stress["positions"]), 1)
        usd_fx = fx_stress["positions"][0]
        self.assertEqual(usd_fx["currency"], "USD")
        self.assertEqual(usd_fx["netOpenPositionInBase"], 900)
        self.assertEqual([row["capitalImpact"] for row in usd_fx["shocks"]], [-180, -90, 90, 180])

    def test_fixed_rate_duration_gap_estimates_parallel_eve_sensitivity(self):
        self.db.accounts.execute(
            "UPDATE accounts SET rate_type='fixed' WHERE account_number='A1'"
        )
        self.db.accounts.execute(
            "UPDATE loans SET rate_type='fixed' WHERE product_code='L90'"
        )
        with self.db.readonly() as conn:
            report = RateManager.institution_fixed_rate_eve_stress(
                conn, "Bank", now=self.now
            )
        by_currency = {row["currency"]: row for row in report["scenarios"]}
        self.assertEqual(by_currency["NWX"]["fixedDeposits"], 1)
        self.assertAlmostEqual(by_currency["NWX"]["liabilityDurationAmountYears"],
                               500*30/365)
        self.assertAlmostEqual(by_currency["USD"]["assetDurationAmountYears"],
                               100*20/365+800*90/365)
        usd_up = next(row for row in by_currency["USD"]["shocks"]
                      if row["basisPoints"] == 200)
        usd_down = next(row for row in by_currency["USD"]["shocks"]
                        if row["basisPoints"] == -200)
        self.assertAlmostEqual(usd_up["estimatedEveChange"],
                               -by_currency["USD"]["netDurationGapAmountYears"]*.02,
                               places=2)
        self.assertAlmostEqual(usd_down["estimatedEveChange"],
                               by_currency["USD"]["netDurationGapAmountYears"]*.02,
                               places=2)
        self.assertIn("not a regulatory IRRBB", report["method"])

    def test_empty_or_invalid_request_is_safe(self):
        with self.db.readonly() as conn:
            self.assertEqual(RateManager.institution_maturity_gap(conn, "Missing", now=self.now)["ladders"], [])
            with self.assertRaises(ValueError):
                RateManager.institution_maturity_gap(conn, "Bank", now=float("nan"))

    def test_depositor_concentration_aggregates_multiple_accounts_by_user(self):
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency) "
            "VALUES ('S31','second deposit',1,0,30,86400,0,'Bank','NWX')"
        )
        self.db.accounts.execute(
            "INSERT INTO accounts(account_number,user_id,product_code,balance,principal,"
            "account_type,quote_currency,balance_units,resolution_status) "
            "VALUES ('A2','u1','S31',100,100,'saving','NWX',0,'active')"
        )
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,"
            "interest_cycle,compound_type,issuer,quote_currency) "
            "VALUES ('SUSD','foreign deposit',1,0,30,86400,0,'Bank','USD')"
        )
        self.db.accounts.execute(
            "INSERT INTO accounts(account_number,user_id,product_code,balance,principal,"
            "account_type,quote_currency,balance_units,resolution_status) "
            "VALUES ('A3','u1','SUSD',0,250,'currency_saving','USD',250000000,'active')"
        )
        self.db.currency.execute(
            "UPDATE financial_institutions SET deposit_insurance_limit=500 WHERE issuer='Bank'"
        )
        with self.db.readonly() as conn:
            liquidity = RateManager.institution_liquidity_stress(
                conn, "Bank", now=self.now
            )
        concentration = liquidity["depositorConcentration"]
        self.assertEqual(concentration["largestDepositorExposure"], 850)
        self.assertEqual(concentration["uninsuredDepositExposure"], 350)
        self.assertAlmostEqual(concentration["uninsuredDepositSharePercent"], 350/850*100)
        self.assertEqual(concentration["largestDepositorSharePercent"], 100)
        self.assertEqual(concentration["topFiveDepositorSharePercent"], 100)
        outflow = next(
            row for row in liquidity["concentrationScenarios"]
            if row["group"] == "top_five_depositors"
        )
        self.assertEqual(outflow["depositOutflow"], 850)


if __name__ == "__main__":
    unittest.main()
