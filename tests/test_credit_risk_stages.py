import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.bank_journal import BankJournal
from managers.credit_risk_manager import CreditRiskManager


class CreditRiskStageTests(unittest.TestCase):
    def test_performing_and_significantly_deteriorated_loans(self):
        self.assertEqual(CreditRiskManager._stage("active", "current", 0, .5, False), 1)
        self.assertEqual(CreditRiskManager._stage("active", "overdue", 0, .5, True), 2)
        self.assertEqual(CreditRiskManager._stage("active", "current", 2, .5, False), 2)
        self.assertEqual(CreditRiskManager._stage("active", "current", 0, 1.0, False), 2)

    def test_defaulted_exposure_is_stage_three(self):
        self.assertEqual(CreditRiskManager._stage("defaulted", "defaulted", 0, 0, True), 3)
        self.assertEqual(CreditRiskManager._stage("active", "defaulted", 0, 0, True), 3)

    def test_short_arrears_do_not_alone_trigger_stage_two(self):
        self.assertEqual(
            CreditRiskManager._stage("active", "overdue", 0, .5, True, overdue_days=1), 1
        )
        self.assertEqual(
            CreditRiskManager._stage("active", "overdue", 0, .5, True, overdue_days=29.9), 1
        )
        self.assertEqual(
            CreditRiskManager._stage("active", "overdue", 0, .5, True, overdue_days=30), 2
        )

    def test_double_origination_pd_is_a_significant_increase_trigger(self):
        self.assertEqual(CreditRiskManager._stage(
            "active", "current", 0, .5, False,
            origination_pd_12m=.01, current_pd_12m=.019,
        ), 1)
        self.assertEqual(CreditRiskManager._stage(
            "active", "current", 0, .5, False,
            origination_pd_12m=.01, current_pd_12m=.02,
        ), 2)


class EclScenarioConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.users.create_user("scenario-admin", "Admin", "낙원", 0, role="admin")

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def test_scenario_probabilities_must_be_a_normalized_distribution(self):
        valid = [
            {"key": "optimistic", "probability": .2, "pdMultiplier": .8, "lgdMultiplier": .9},
            {"key": "base", "probability": .6, "pdMultiplier": 1, "lgdMultiplier": 1},
            {"key": "downside", "probability": .2, "pdMultiplier": 2, "lgdMultiplier": 1.15},
        ]
        invalid = [dict(item) for item in valid]
        invalid[1]["probability"] = .5
        with self.assertRaisesRegex(ValueError, "합은 1"):
            with self.db.atomic() as conn:
                CreditRiskManager.configure_scenarios(
                    conn, "낙원", "scenario-admin", invalid
                )
        with self.db.atomic() as conn:
            result = CreditRiskManager.configure_scenarios(
                conn, "낙원", "scenario-admin", valid
            )
            weights = [row[0] for row in conn.execute(
                "SELECT probability FROM credit_risk_scenarios "
                "WHERE nationality='낙원' ORDER BY scenario_key"
            )]
        self.assertEqual(result["updated"], 3)
        self.assertAlmostEqual(sum(weights), 1.0)

    def test_legacy_scenario_weights_are_normalized_without_changing_ratios(self):
        self.db.currency.execute(
            "UPDATE credit_risk_scenarios SET probability=CASE scenario_key "
            "WHEN 'optimistic' THEN .1 WHEN 'base' THEN .5 ELSE .2 END "
            "WHERE nationality='낙원'"
        )
        self.db.close()
        self.db = DatabaseManager()
        rows = self.db.currency.execute(
            "SELECT scenario_key,probability FROM credit_risk_scenarios "
            "WHERE nationality='낙원'"
        )
        weights = {row["scenario_key"]: row["probability"] for row in rows}
        self.assertAlmostEqual(sum(weights.values()), 1.0)
        self.assertAlmostEqual(weights["optimistic"], .125)
        self.assertAlmostEqual(weights["base"], .625)
        self.assertAlmostEqual(weights["downside"], .25)


class DefaultedLoanEclTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.users.create_user("ecl-borrower", "Borrower", "낙원", 0)
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
            "interest_cycle,min_credit_score,issuer,quote_currency) "
            "VALUES ('ECL3','ECL test',5,10000,365,86400,0,'Bank','NWX')"
        )
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,"
            "created_at,collection_status,quote_currency) "
            "VALUES ('ecl-borrower','ECL3',1000,1000,'defaulted',datetime('now'),'defaulted','NWX')"
        )
        self.db.currency.execute(
            "UPDATE credit_risk_scenarios SET pd_multiplier=0 WHERE nationality='낙원'"
        )

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def test_defaulted_loan_is_assessed_and_provisioned_as_stage_three(self):
        self.db.accounts.execute(
            "UPDATE loans SET contract_effective_rate=10 WHERE product_code='ECL3'"
        )
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL3'"
        )["loan_id"]
        self.db.accounts.execute(
            "INSERT INTO loan_payment_schedule(loan_id,installment_number,due_epoch,principal_due) "
            "VALUES (?,?,?,500)", (loan_id, 1, 1_800_000_000+90*86400),
        )
        with self.db.atomic() as conn:
            count = CreditRiskManager.assess_conn(conn, 1_800_000_000)
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, 1_800_000_000), 0)
            self.assertEqual(BankJournal.audit_unbalanced(conn), [])
        self.assertEqual(count, 1)
        row = self.db.accounts.execute_one(
            "SELECT stage,pd_12m,lifetime_pd,lgd,ead,expected_loss,discount_factor,"
            "effective_interest_rate,expected_loss_time_years,macro_pd_multiplier,scenario "
            "FROM loan_ecl_assessments"
        )
        self.assertEqual(row["stage"], 3)
        self.assertEqual(row["pd_12m"], 1)
        self.assertEqual(row["lifetime_pd"], 1)
        self.assertEqual(row["ead"], 1000)
        self.assertAlmostEqual(row["discount_factor"], 1/(1.1**0.5))
        self.assertEqual(row["effective_interest_rate"], 10)
        self.assertEqual(row["expected_loss_time_years"], 0.5)
        self.assertEqual(row["macro_pd_multiplier"], 1)
        self.assertLess(row["discount_factor"], 1)
        self.assertGreater(row["discount_factor"], 0)
        self.assertLess(row["expected_loss"], row["ead"] * row["lgd"])
        self.assertIn('"discountFactor":', row["scenario"])
        self.assertGreater(row["expected_loss"], 0)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT ecl_allowance FROM loans WHERE product_code='ECL3'"
        )["ecl_allowance"], row["expected_loss"])
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT COUNT(*) count FROM bank_journal_entries WHERE event_type='expected_credit_loss'"
        )["count"], 1)

    def test_matured_pd_backtest_uses_one_snapshot_per_loan(self):
        sample_epoch = 1_700_000_000.0
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL3'"
        )["loan_id"]
        self.db.accounts.execute(
            "UPDATE loans SET defaulted_epoch=? WHERE loan_id=?",
            (sample_epoch+100*86400, loan_id),
        )
        self.db.accounts.execute(
            "INSERT INTO loan_ecl_assessments "
            "(loan_id,as_of_day,stage,pd_12m,lifetime_pd,lgd,ead,expected_loss,"
            "quote_currency,calculated_epoch) VALUES (?,?,?,?,?,?,?,?,?,?),(?,?,?,?,?,?,?,?,?,?)",
            (loan_id, "2023-11-04", 2, .9, .9, .4, 1000, 360, "NWX", sample_epoch-10*86400,
             loan_id, "2023-11-14", 2, .1, .2, .4, 1000, 80, "NWX", sample_epoch),
        )
        with self.db.readonly() as conn:
            report = CreditRiskManager.backtest_12m(
                conn, "낙원", issuer="Bank", now=sample_epoch+400*86400,
            )
        self.assertEqual(report["horizonDays"], 365)
        group = report["groups"][0]
        self.assertEqual((group["sampleCount"], group["observedDefaults"]), (1, 1))
        self.assertEqual(group["pdBand"], "5–20%")
        self.assertEqual(group["observedDefaultRate"], 1)
        self.assertAlmostEqual(group["observedRateLower95"], .2065, places=3)
        self.assertEqual(group["observedRateUpper95"], 1)
        self.assertAlmostEqual(group["brierScore"], .81)
        self.assertIn("per loan", report["method"])

    def test_zero_effective_rate_has_no_discount(self):
        self.db.accounts.execute(
            "UPDATE loans SET contract_effective_rate=0 WHERE product_code='ECL3'"
        )
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, 1_800_086_400), 1)
        row = self.db.accounts.execute_one(
            "SELECT discount_factor,ead,lgd,expected_loss FROM loan_ecl_assessments "
            "ORDER BY calculated_epoch DESC LIMIT 1"
        )
        self.assertEqual(row["discount_factor"], 1)
        self.assertGreater(row["expected_loss"], 0)

    def test_missing_contract_rates_fall_back_to_current_product_rate(self):
        self.db.accounts.execute(
            "UPDATE loans SET contract_effective_rate=NULL,contract_rate=NULL "
            "WHERE product_code='ECL3'"
        )
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, 1_800_172_800), 1)
        row = self.db.accounts.execute_one(
            "SELECT discount_factor FROM loan_ecl_assessments "
            "ORDER BY calculated_epoch DESC LIMIT 1"
        )
        self.assertGreater(row["discount_factor"], 0)
        self.assertLess(row["discount_factor"], 1)

    def test_negative_effective_rate_increases_discount_factor(self):
        self.db.accounts.execute(
            "UPDATE loans SET contract_effective_rate=-5 WHERE product_code='ECL3'"
        )
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, 1_800_259_200), 1)
        row = self.db.accounts.execute_one(
            "SELECT discount_factor FROM loan_ecl_assessments "
            "ORDER BY calculated_epoch DESC LIMIT 1"
        )
        self.assertGreater(row["discount_factor"], 1)

    def test_stage_two_uses_remaining_contract_life_for_pd_and_discounting(self):
        epoch = 1_800_000_000
        self.db.accounts.execute(
            "UPDATE loans SET status='active',collection_status='current',"
            "missed_payment_count=2,maturity_epoch=?,contract_effective_rate=8 "
            "WHERE product_code='ECL3'",
            (epoch+90*86400,),
        )
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, epoch), 1)
        row = self.db.accounts.execute_one(
            "SELECT stage,pd_12m,lifetime_pd,discount_factor,effective_interest_rate,"
            "expected_loss_time_years,macro_pd_multiplier FROM loan_ecl_assessments "
            "ORDER BY calculated_epoch DESC LIMIT 1"
        )
        years = 90/365
        self.assertEqual(row["stage"], 2)
        self.assertAlmostEqual(row["lifetime_pd"], 1-(1-row["pd_12m"])**years)
        self.assertAlmostEqual(row["discount_factor"], 1/(1.08**(years/2)))
        self.assertEqual(row["effective_interest_rate"], 8)
        self.assertAlmostEqual(row["expected_loss_time_years"], years/2)

    def test_ecl_stage_two_arrears_trigger_uses_elapsed_days(self):
        epoch = 1_800_000_000
        self.db.accounts.execute(
            "UPDATE loans SET status='active',collection_status='overdue',"
            "missed_payment_count=0,current_ltv=.5,overdue_since_epoch=?,"
            "origination_pd_12m=NULL,maturity_epoch=?,contract_effective_rate=5 "
            "WHERE product_code='ECL3'",
            (epoch-29*86400, epoch+365*86400),
        )
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, epoch), 1)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT stage FROM loan_ecl_assessments ORDER BY calculated_epoch DESC LIMIT 1"
        )["stage"], 1)

        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, epoch+86400), 1)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT stage FROM loan_ecl_assessments ORDER BY calculated_epoch DESC LIMIT 1"
        )["stage"], 2)

    def test_doubled_current_pd_triggers_stage_two_against_origination_baseline(self):
        epoch = 1_800_000_000
        baseline = CreditRiskManager.origination_pd_12m(
            self.db.currency.conn, "낙원", 700, 0, 0, .5
        )
        self.db.accounts.execute(
            "UPDATE loans SET status='active',collection_status='current',"
            "missed_payment_count=0,current_ltv=.5,overdue_since_epoch=NULL,"
            "origination_pd_12m=?,maturity_epoch=?,contract_effective_rate=5 "
            "WHERE product_code='ECL3'", (baseline, epoch+365*86400),
        )
        self.db.users.execute("UPDATE users SET credit_score=0 WHERE user_id='ecl-borrower'")
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, epoch), 1)
        row = self.db.accounts.execute_one(
            "SELECT stage,pd_12m FROM loan_ecl_assessments ORDER BY calculated_epoch DESC LIMIT 1"
        )
        self.assertEqual(row["stage"], 2)
        self.assertGreaterEqual(row["pd_12m"], baseline*2)

    def test_ead_uses_principal_amortization_due_before_expected_loss_midpoint(self):
        epoch = 1_800_000_000
        self.db.accounts.execute(
            "UPDATE loans SET status='active',collection_status='current',"
            "missed_payment_count=0,current_ltv=.5,maturity_epoch=?,"
            "contract_effective_rate=0 WHERE product_code='ECL3'",
            (epoch+365*86400,),
        )
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL3'"
        )["loan_id"]
        self.db.accounts.execute(
            "INSERT INTO loan_payment_schedule(loan_id,installment_number,due_epoch,principal_due) "
            "VALUES (?,?,?,?),(?,?,?,?),(?,?,?,?)",
            (loan_id, 1, epoch+90*86400, 300,
             loan_id, 2, epoch+180*86400, 300,
             loan_id, 3, epoch+365*86400, 400),
        )
        self.db.accounts.execute(
            "INSERT INTO loan_payment_schedule"
            "(loan_id,installment_number,due_epoch,principal_due,status) VALUES (?,?,?,?,?)",
            (loan_id, 4, epoch-30*86400, 100, "missed"),
        )
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, epoch), 1)
        row = self.db.accounts.execute_one(
            "SELECT stage,ead,scenario FROM loan_ecl_assessments "
            "ORDER BY calculated_epoch DESC LIMIT 1"
        )
        self.assertEqual(row["stage"], 1)
        self.assertEqual(row["ead"], 400)
        self.assertIn('"eadMethod":"midpoint_after_scheduled_principal"', row["scenario"])

    def test_stage_two_cure_requires_ninety_days_of_improved_indicators(self):
        epoch = 1_800_000_000
        self.db.accounts.execute(
            "UPDATE loans SET status='active',collection_status='current',"
            "missed_payment_count=2,current_ltv=.5,overdue_since_epoch=NULL,"
            "maturity_epoch=?,contract_effective_rate=0 WHERE product_code='ECL3'",
            (epoch+365*86400,),
        )
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, epoch), 1)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT stage FROM loan_ecl_assessments ORDER BY calculated_epoch DESC LIMIT 1"
        )["stage"], 2)

        self.db.accounts.execute(
            "UPDATE loans SET missed_payment_count=0 WHERE product_code='ECL3'"
        )
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, epoch+86400), 1)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT stage FROM loan_ecl_assessments ORDER BY calculated_epoch DESC LIMIT 1"
        )["stage"], 2)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT stage_cure_since_epoch FROM loans WHERE product_code='ECL3'"
        )["stage_cure_since_epoch"], epoch+86400)

        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, epoch+90*86400), 1)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT stage FROM loan_ecl_assessments ORDER BY calculated_epoch DESC LIMIT 1"
        )["stage"], 2)
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, epoch+91*86400), 1)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT stage FROM loan_ecl_assessments ORDER BY calculated_epoch DESC LIMIT 1"
        )["stage"], 1)
        self.assertIsNone(self.db.accounts.execute_one(
            "SELECT stage_cure_since_epoch FROM loans WHERE product_code='ECL3'"
        )["stage_cure_since_epoch"])

    def test_recurrent_stage_two_trigger_resets_cure_clock(self):
        epoch = 1_800_000_000
        self.db.accounts.execute(
            "UPDATE loans SET status='active',collection_status='current',"
            "missed_payment_count=2,current_ltv=.5,overdue_since_epoch=NULL,"
            "maturity_epoch=?,contract_effective_rate=0 WHERE product_code='ECL3'",
            (epoch+365*86400,),
        )
        with self.db.atomic() as conn:
            CreditRiskManager.assess_conn(conn, epoch)
        self.db.accounts.execute("UPDATE loans SET missed_payment_count=0 WHERE product_code='ECL3'")
        with self.db.atomic() as conn:
            CreditRiskManager.assess_conn(conn, epoch+86400)

        self.db.accounts.execute("UPDATE loans SET missed_payment_count=2 WHERE product_code='ECL3'")
        with self.db.atomic() as conn:
            CreditRiskManager.assess_conn(conn, epoch+10*86400)
        self.assertIsNone(self.db.accounts.execute_one(
            "SELECT stage_cure_since_epoch FROM loans WHERE product_code='ECL3'"
        )["stage_cure_since_epoch"])

        self.db.accounts.execute("UPDATE loans SET missed_payment_count=0 WHERE product_code='ECL3'")
        with self.db.atomic() as conn:
            CreditRiskManager.assess_conn(conn, epoch+11*86400)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT stage_cure_since_epoch FROM loans WHERE product_code='ECL3'"
        )["stage_cure_since_epoch"], epoch+11*86400)
        with self.db.atomic() as conn:
            CreditRiskManager.assess_conn(conn, epoch+100*86400)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT stage FROM loan_ecl_assessments ORDER BY calculated_epoch DESC LIMIT 1"
        )["stage"], 2)
        with self.db.atomic() as conn:
            CreditRiskManager.assess_conn(conn, epoch+101*86400)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT stage FROM loan_ecl_assessments ORDER BY calculated_epoch DESC LIMIT 1"
        )["stage"], 1)

    def test_macro_downturn_increases_stage_two_pd_and_is_recorded(self):
        epoch = 1_800_000_000
        self.db.currency.execute(
            "UPDATE credit_risk_scenarios SET pd_multiplier=1 WHERE nationality='낙원'"
        )
        self.db.accounts.execute(
            "UPDATE loans SET status='active',collection_status='current',missed_payment_count=2,"
            "maturity_epoch=?,contract_effective_rate=5 WHERE product_code='ECL3'",
            (epoch+365*86400,),
        )
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, epoch), 1)
        self.db.currency.execute(
            "UPDATE country_macro_indicators SET inflation_percent=10,"
            "real_gdp_growth_percent=-3,unemployment_percent=20 WHERE nationality='낙원'"
        )
        with self.db.atomic() as conn:
            self.assertEqual(CreditRiskManager.assess_conn(conn, epoch+86400), 1)
        rows = self.db.accounts.execute(
            "SELECT pd_12m,scenario,macro_pd_multiplier FROM loan_ecl_assessments ORDER BY as_of_day"
        )
        baseline_scenario = json.loads(rows[0]["scenario"])
        stress_scenario = json.loads(rows[1]["scenario"])
        self.assertGreater(rows[1]["pd_12m"], rows[0]["pd_12m"])
        self.assertEqual(rows[0]["macro_pd_multiplier"], 1)
        self.assertGreater(rows[1]["macro_pd_multiplier"], 1)
        self.assertEqual(baseline_scenario[0]["macroPdMultiplier"], 1)
        self.assertGreater(stress_scenario[0]["macroPdMultiplier"], 1)

    def test_legacy_assessment_migration_preserves_rows_and_uses_neutral_factor(self):
        self.db.accounts.execute(
            "INSERT INTO loan_ecl_assessments "
            "(loan_id,as_of_day,stage,pd_12m,lifetime_pd,lgd,ead,expected_loss,"
            "quote_currency,scenario,discount_factor,calculated_epoch) "
            "VALUES (1,'2026-01-01',3,1,1,0.5,1000,500,'NWX','[]',0.9,1)"
        )
        self.db.accounts.execute(
            "ALTER TABLE loan_ecl_assessments DROP COLUMN discount_factor"
        )
        self.db.accounts.execute(
            "ALTER TABLE loan_ecl_assessments DROP COLUMN effective_interest_rate"
        )
        self.db.accounts.execute(
            "ALTER TABLE loan_ecl_assessments DROP COLUMN expected_loss_time_years"
        )
        self.db.accounts.execute(
            "ALTER TABLE loan_ecl_assessments DROP COLUMN macro_pd_multiplier"
        )
        self.db.accounts._init_db()
        row = self.db.accounts.execute_one(
            "SELECT expected_loss,discount_factor,effective_interest_rate,"
            "expected_loss_time_years,macro_pd_multiplier FROM loan_ecl_assessments "
            "WHERE as_of_day='2026-01-01'"
        )
        self.assertEqual(row["expected_loss"], 500)
        self.assertEqual(row["discount_factor"], 1)
        self.assertEqual(row["effective_interest_rate"], 0)
        self.assertEqual(row["expected_loss_time_years"], 0)
        self.assertEqual(row["macro_pd_multiplier"], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.accounts.execute(
                "UPDATE loan_ecl_assessments SET discount_factor=0.95 "
                "WHERE as_of_day='2026-01-01'"
            )


if __name__ == "__main__":
    unittest.main()
