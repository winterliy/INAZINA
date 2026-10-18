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
    def test_simulator_pd_rating_bands_are_ordered_and_boundary_stable(self):
        self.assertEqual(
            [CreditRiskManager.risk_grade(pd) for pd in (0, .005, .0051, .01, .025, .05, .1, .2, 1)],
            ["A", "A", "B", "B", "C", "D", "E", "F", "G"],
        )

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

    def test_macro_scenario_forecasts_are_explainable_and_ordered(self):
        with self.db.readonly() as conn:
            optimistic = CreditRiskManager._macro_scenario_path(
                conn, "낙원", "optimistic", 3.0,
            )
            base = CreditRiskManager._macro_scenario_path(conn, "낙원", "base", 3.0)
            downside = CreditRiskManager._macro_scenario_path(
                conn, "낙원", "downside", 3.0,
            )
        self.assertEqual([point["year"] for point in downside["path"]], [1, 2, 3])
        self.assertEqual(downside["macroFactorOrder"],
                         ["inflation", "real_gdp_growth", "unemployment"])
        matrix = downside["macroFactorCorrelation"]
        self.assertEqual(len(matrix), 3)
        self.assertTrue(all(abs(matrix[i][j]-matrix[j][i]) < 1e-12
                            for i in range(3) for j in range(3)))
        self.assertTrue(all(abs(matrix[i][i]-1.0) < 1e-12 for i in range(3)))
        self.assertEqual(downside["correlationCalibration"],
                         "illustrative_simulator_assumption_not_empirically_calibrated")
        self.assertIn("Cholesky-correlated", downside["method"])
        self.assertLess(optimistic["relativePdMultiplier"], 1)
        self.assertEqual(base["relativePdMultiplier"], 1)
        self.assertGreater(downside["relativePdMultiplier"], 1)
        self.assertGreater(downside["path"][-1]["unemploymentPercent"],
                           downside["path"][0]["unemploymentPercent"])
        with self.assertRaisesRegex(ValueError, "invalid_macro_scenario"):
            with self.db.readonly() as conn:
                CreditRiskManager._macro_scenario_path(conn, "낙원", "unknown", 1)

    def test_scenario_configuration_rejects_invalid_epoch_without_audit_or_mutation(self):
        valid = [
            {"key": "optimistic", "probability": .2, "pdMultiplier": .8, "lgdMultiplier": .9},
            {"key": "base", "probability": .6, "pdMultiplier": 1, "lgdMultiplier": 1},
            {"key": "downside", "probability": .2, "pdMultiplier": 2, "lgdMultiplier": 1.15},
        ]
        before = [tuple(row) for row in self.db.currency.execute(
            "SELECT scenario_key,probability,pd_multiplier,lgd_multiplier,updated_epoch "
            "FROM credit_risk_scenarios WHERE nationality='낙원' ORDER BY scenario_key"
        )]
        audit_before = self.db.system.execute_one(
            "SELECT COUNT(*) FROM admin_audit WHERE action='credit_risk.scenarios.configure'"
        )[0]
        for invalid_epoch in (-1, float("nan"), float("inf"), 1e300, True, "invalid"):
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, "유효한 UNIX 시각"):
                    with self.db.atomic() as conn:
                        CreditRiskManager.configure_scenarios(
                            conn, "낙원", "scenario-admin", valid, now=invalid_epoch
                        )
        after = [tuple(row) for row in self.db.currency.execute(
            "SELECT scenario_key,probability,pd_multiplier,lgd_multiplier,updated_epoch "
            "FROM credit_risk_scenarios WHERE nationality='낙원' ORDER BY scenario_key"
        )]
        audit_after = self.db.system.execute_one(
            "SELECT COUNT(*) FROM admin_audit WHERE action='credit_risk.scenarios.configure'"
        )[0]
        self.assertEqual(after, before)
        self.assertEqual(audit_after, audit_before)

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
        self.db.users.create_user("support-admin", "Admin", "낙원", 0, role="admin")
        self.db.users.create_user("ecl-borrower", "Borrower", "낙원", 0)
        self.db.users.create_user("ecl-coborrower", "Co-borrower", "낙원", 0)
        self.db.users.create_user("ecl-guarantor", "Guarantor", "낙원", 0)
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
            "SELECT stage,risk_grade,pd_12m,lifetime_pd,lgd,ead,expected_loss,"
            "ead_minor_units,expected_loss_minor_units,discount_factor,"
            "effective_interest_rate,expected_loss_time_years,macro_pd_multiplier,scenario "
            "FROM loan_ecl_assessments"
        )
        self.assertEqual(row["stage"], 3)
        self.assertEqual(row["risk_grade"], "G")
        self.assertEqual(row["pd_12m"], 1)
        self.assertEqual(row["lifetime_pd"], 1)
        self.assertEqual(row["ead"], 1000)
        self.assertEqual(row["ead_minor_units"], 1000)
        self.assertEqual(row["expected_loss_minor_units"], row["expected_loss"])
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
            "SELECT ecl_allowance_minor_units FROM loans WHERE product_code='ECL3'"
        )["ecl_allowance_minor_units"], row["expected_loss_minor_units"])
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT nonaccrual_since_epoch FROM loans WHERE product_code='ECL3'"
        )["nonaccrual_since_epoch"], 1_800_000_000)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT COUNT(*) count FROM bank_journal_entries WHERE event_type='expected_credit_loss'"
        )["count"], 1)
        with self.db.atomic() as conn:
            BankJournal.post(conn, "Bank", "loan_origination", f"loan:{loan_id}:origin",
                             "NWX", [("loans_receivable", "debit", 1000),
                                     ("settlement_asset", "credit", 1000)],
                             now=1_800_000_000)
            chargeoff = CreditRiskManager.charge_off_loan(
                conn, loan_id=loan_id, event_key="lifecycle-chargeoff",
                actor_id="support-admin", now=1_800_000_010,
            )
            recovery = CreditRiskManager.record_chargeoff_recovery(
                conn, loan_id=loan_id, amount=5, event_key="lifecycle-recovery",
                actor_id="support-admin", now=1_800_000_020,
            )
            self.assertEqual(BankJournal.audit_unbalanced(conn), [])
            self.assertEqual(BankJournal.reconcile_loan_receivables(conn, "Bank")[0]["status"], "matched")
            self.assertEqual(BankJournal.reconcile_ecl_allowances(conn, "Bank")[0]["status"], "matched")
            self.assertEqual(BankJournal.reconcile_charged_off_balances(conn, "Bank")[0]["status"], "matched")
            integrated = BankJournal.reconcile_all(conn, "Bank")
            self.assertEqual(integrated["status"], "matched", integrated["findings"])
            from managers.accounting_journal import AccountingJournal
            common_checks = {
                row["ledger_scope"]: row
                for row in AccountingJournal.reconcile_subledgers(conn)
            }
            self.assertEqual(common_checks["customer"]["status"], "matched",
                             common_checks["customer"])
            self.assertEqual(common_checks["institution"]["status"], "matched",
                             common_checks["institution"])
            memo_snapshot = conn.execute("""SELECT amount_minor_units FROM financial_amount_snapshots
                WHERE source_table='loans' AND source_key=? AND amount_field='charged_off_balance'""",
                (str(loan_id),),
            ).fetchone()
            self.assertIsNotNone(memo_snapshot)
            self.assertEqual(memo_snapshot["amount_minor_units"], recovery["remainingMinorUnits"])
            memo_units = conn.execute(
                "SELECT charged_off_balance_minor_units FROM accounts.loans WHERE loan_id=?",
                (loan_id,),
            ).fetchone()[0]
            self.assertEqual(memo_units, recovery["remainingMinorUnits"])
        self.assertGreater(chargeoff["allowanceMinorUnits"], 0)
        self.assertEqual(recovery["recoveryMinorUnits"], 5)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT status,charged_off_balance FROM loans WHERE loan_id=?", (loan_id,),
        )["status"], "charged_off")

    def test_defaulted_loan_nonaccrual_does_not_book_interest(self):
        from managers.interest_manager import InterestManager
        loan_before = self.db.accounts.execute_one(
            "SELECT loan_id,outstanding_balance FROM loans WHERE product_code='ECL3'"
        )
        InterestManager(self.db).process_all_interests(now=1_800_000_000)
        loan_after = self.db.accounts.execute_one(
            "SELECT outstanding_balance,nonaccrual_since_epoch FROM loans WHERE loan_id=?",
            (loan_before["loan_id"],),
        )
        self.assertEqual(loan_after["outstanding_balance"], loan_before["outstanding_balance"])
        self.assertIsNotNone(loan_after["nonaccrual_since_epoch"])
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT COUNT(*) count FROM bank_journal_entries "
            "WHERE event_type IN ('loan_interest','overdue_interest')"
        )["count"], 0)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT stage FROM loan_ecl_assessments WHERE loan_id=?",
            (loan_before["loan_id"],),
        )["stage"], 3)
        with self.db.readonly() as conn:
            self.assertEqual(BankJournal.audit_unbalanced(conn), [])
            from managers.accounting_journal import AccountingJournal
            common = {row["ledger_scope"]: row
                      for row in AccountingJournal.reconcile_subledgers(conn)}
            self.assertEqual(common["customer"]["status"], "matched",
                             common["customer"])
            self.assertEqual(common["institution"]["status"], "matched",
                             common["institution"])

    def test_chargeoff_uses_allowance_and_recovery_is_idempotent_and_reconciled(self):
        from managers.rate_manager import RateManager
        loan = self.db.accounts.execute_one(
            "SELECT loan_id,outstanding_balance FROM loans WHERE product_code='ECL3'"
        )
        loan_id = loan["loan_id"]
        with self.db.atomic() as conn:
            BankJournal.post(conn, "Bank", "loan_origination", f"loan:{loan_id}:origin",
                             "NWX", [("loans_receivable", "debit", 1000),
                                     ("settlement_asset", "credit", 1000)], now=1_800_000_000)
            BankJournal.post(conn, "Bank", "expected_credit_loss", f"loan:{loan_id}:seed-ecl",
                             "NWX", [("impairment_expense", "debit", 300),
                                     ("ecl_allowance", "credit", 300)], now=1_800_000_001)
            RateManager.record_institution_result(
                conn, "Bank", -300, "expected_credit_loss", f"loan:{loan_id}:seed-ecl",
                now=1_800_000_001, currency_code="NWX",
            )
            conn.execute("UPDATE accounts.loans SET ecl_allowance=300 WHERE loan_id=?", (loan_id,))
            result = CreditRiskManager.charge_off_loan(
                conn, loan_id=loan_id, event_key="chargeoff-1", actor_id="support-admin",
                now=1_800_000_002,
            )
        self.assertEqual(result["allowanceMinorUnits"], 300)
        self.assertEqual(result["lossMinorUnits"], 700)
        state = self.db.accounts.execute_one(
            "SELECT outstanding_balance,charged_off_balance,status,ecl_allowance,"
            "outstanding_balance_minor_units,charged_off_balance_minor_units "
            "FROM loans WHERE loan_id=?", (loan_id,),
        )
        self.assertEqual((state["outstanding_balance"], state["charged_off_balance"],
                          state["status"], state["ecl_allowance"]), (0, 1000, "charged_off", 0))
        self.assertEqual((state["outstanding_balance_minor_units"],
                          state["charged_off_balance_minor_units"]), (0, 1000))
        with self.db.atomic() as conn:
            duplicate = CreditRiskManager.charge_off_loan(
                conn, loan_id=loan_id, event_key="chargeoff-1", actor_id="support-admin",
                now=1_800_000_003,
            )
            recovery = CreditRiskManager.record_chargeoff_recovery(
                conn, loan_id=loan_id, amount=100, event_key="recovery-1",
                actor_id="support-admin", now=1_800_000_004,
            )
            recovery_duplicate = CreditRiskManager.record_chargeoff_recovery(
                conn, loan_id=loan_id, amount=100, event_key="recovery-1",
                actor_id="support-admin", now=1_800_000_005,
            )
            self.assertEqual(BankJournal.audit_unbalanced(conn), [])
            self.assertEqual(BankJournal.reconcile_loan_receivables(conn, "Bank")[0]["status"], "matched")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(recovery["remainingMinorUnits"], 900)
        self.assertTrue(recovery_duplicate["duplicate"])
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT charged_off_balance FROM loans WHERE loan_id=?", (loan_id,),
        )[0], 900)
        audit_actions = {row[0] for row in self.db.system.execute(
            "SELECT action FROM admin_audit WHERE action LIKE 'credit_risk.loan_chargeoff%' "
            "OR action='credit_risk.loan_charge_off'"
        )}
        self.assertIn("credit_risk.loan_charge_off", audit_actions)
        self.assertIn("credit_risk.loan_chargeoff_recovery", audit_actions)

    def test_chargeoff_recovery_cannot_exceed_memorandum_balance(self):
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL3'"
        )["loan_id"]
        with self.db.atomic() as conn:
            conn.execute("UPDATE accounts.loans SET status='charged_off',"
                         "collection_status='charged_off',charged_off_balance=25 WHERE loan_id=?",
                         (loan_id,))
            with self.assertRaisesRegex(ValueError, "미회수 상각잔액"):
                CreditRiskManager.record_chargeoff_recovery(
                    conn, loan_id=loan_id, amount=26, event_key="over-recovery",
                    actor_id="support-admin", now=1_800_000_100,
                )
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT COUNT(*) FROM loan_chargeoff_events WHERE event_key='over-recovery'"
        )[0], 0)

    def test_credit_support_changes_are_audited_and_guarantee_reduces_lgd(self):
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL3'"
        )["loan_id"]
        with self.db.atomic() as conn:
            loan = conn.execute("SELECT * FROM accounts.loans WHERE loan_id=?", (loan_id,)).fetchone()
            before = CreditRiskManager._lgd(conn, loan)
            CreditRiskManager.configure_loan_credit_support(
                conn, loan_id=loan_id, party_user_id="ecl-guarantor",
                actor_id="support-admin", support_type="guarantor",
                guarantee_limit=400, recovery_days=0,
            )
            after = CreditRiskManager._lgd(conn, loan)
            events = conn.execute(
                "SELECT COUNT(*) FROM accounts.loan_credit_support_events"
            ).fetchone()[0]
        self.assertLess(after, before)
        self.assertEqual(events, 1)
        with self.assertRaisesRegex(ValueError, "범위를 벗어났습니다"):
            with self.db.atomic() as conn:
                CreditRiskManager.configure_loan_credit_support(
                    conn, loan_id=loan_id, party_user_id="ecl-coborrower",
                    actor_id="support-admin", support_type="co_borrower",
                    exposure_share_bps=10001,
                )

    def test_coborrower_pd_is_exposure_weighted_in_new_assessment(self):
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL3'"
        )["loan_id"]
        self.db.accounts.execute(
            "UPDATE loans SET status='active',collection_status='current',"
            "defaulted_epoch=NULL,overdue_since_epoch=NULL,missed_payment_count=0,"
            "current_ltv=0,underwriting_dti=0,underwriting_dsr=0 WHERE loan_id=?",
            (loan_id,),
        )
        self.db.users.execute(
            "UPDATE users SET credit_score=1000 WHERE user_id='ecl-coborrower'"
        )
        epoch = 1_800_000_000
        with self.db.atomic() as conn:
            CreditRiskManager.assess_conn(conn, epoch)
            baseline = conn.execute(
                "SELECT pd_12m FROM accounts.loan_ecl_assessments WHERE loan_id=?",
                (loan_id,),
            ).fetchone()[0]
            CreditRiskManager.configure_loan_credit_support(
                conn, loan_id=loan_id, party_user_id="ecl-coborrower",
                actor_id="support-admin", support_type="co_borrower",
                exposure_share_bps=10000, underwriting_dti=100, underwriting_dsr=100,
            )
            CreditRiskManager.assess_conn(conn, epoch+86400)
            weighted = conn.execute(
                "SELECT pd_12m FROM accounts.loan_ecl_assessments WHERE loan_id=? "
                "ORDER BY calculated_epoch DESC LIMIT 1", (loan_id,),
            ).fetchone()[0]
        self.assertLess(weighted, baseline)
        self.assertAlmostEqual(weighted, CreditRiskManager._pd_12m(1000, 100, 100, 0, 0, False))

    def test_jurisdiction_product_policy_overrides_pd_and_grade_bands(self):
        thresholds = {"a": .001, "b": .002, "c": .003, "d": .004, "e": .005, "f": .006}
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL3'"
        )["loan_id"]
        self.db.accounts.execute(
            "UPDATE loans SET status='active',collection_status='current',"
            "defaulted_epoch=NULL,overdue_since_epoch=NULL WHERE loan_id=?", (loan_id,),
        )
        with self.db.atomic() as conn:
            configured = CreditRiskManager.configure_policy_override(
                conn, "낙원", "ECL3", "support-admin", pd_multiplier=1.5,
                grade_thresholds=thresholds,
            )
            row = conn.execute(
                "SELECT pd_multiplier,grade_f_max,updated_by FROM accounts.credit_risk_policy_overrides "
                "WHERE nationality='낙원' AND product_code='ECL3'"
            ).fetchone()
            event_count = conn.execute(
                "SELECT COUNT(*) FROM accounts.credit_risk_policy_events"
            ).fetchone()[0]
            CreditRiskManager.assess_conn(conn, 1_800_000_000)
            assessment = conn.execute(
                "SELECT pd_12m,risk_grade FROM accounts.loan_ecl_assessments WHERE loan_id=?",
                (loan_id,),
            ).fetchone()
            transitions = CreditRiskManager.grade_transition_report(conn, "낙원")["transitions"]
        self.assertEqual(configured["productCode"], "ECL3")
        self.assertEqual(tuple(row), (1.5, .006, "support-admin"))
        self.assertEqual(event_count, 1)
        self.assertEqual(assessment["risk_grade"], "G")
        self.assertGreater(assessment["pd_12m"], .006)
        self.assertEqual(transitions, [{"issuer": "Bank", "nationality": "낙원",
                                        "from_grade": "G", "to_grade": "G",
                                        "loan_count": 1}])
        with self.assertRaisesRegex(ValueError, "오름차순"):
            with self.db.atomic() as conn:
                CreditRiskManager.configure_policy_override(
                    conn, "낙원", "ECL3", "support-admin", grade_thresholds={
                        "a": .02, "b": .01, "c": .03, "d": .04, "e": .1, "f": .2,
                    },
                )

    def test_future_undrawn_commitment_contributes_ccf_weighted_ead(self):
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL3'"
        )["loan_id"]
        with self.db.atomic() as conn:
            CreditRiskManager.configure_future_commitment(
                conn, loan_id, "support-admin", 1500, 5000,
            )
            ead, method = CreditRiskManager._ead_at_expected_loss(
                conn, loan_id, 1000, 1_800_000_000, .5, 1, "NWX",
            )
            events = conn.execute(
                "SELECT COUNT(*) FROM accounts.loan_future_commitment_events"
            ).fetchone()[0]
        self.assertEqual(ead, 1250)
        self.assertIn("undrawn_commitment", method)
        self.assertEqual(events, 1)

    def test_ead_uses_minor_unit_schedule_and_reconciliation_flags_legacy_drift(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) "
            "VALUES ('USD','Dollar','1',2)"
        )
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
            "interest_cycle,min_credit_score,issuer,quote_currency) "
            "VALUES ('ECL-USD','USD ECL',5,10000,365,86400,0,'Bank','USD')"
        )
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,"
            "created_at,quote_currency) VALUES ('ecl-borrower','ECL-USD',100,100,'active',"
            "datetime('now'),'USD')"
        )
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL-USD'"
        )["loan_id"]
        epoch = 1_800_000_000
        self.db.accounts.execute(
            "INSERT INTO loan_payment_schedule(loan_id,installment_number,due_epoch,"
            "principal_due,principal_paid) VALUES (?,1,?,20,10)",
            (loan_id, epoch+100*86400),
        )
        schedule_id = self.db.accounts.execute_one(
            "SELECT schedule_id FROM loan_payment_schedule WHERE loan_id=?", (loan_id,)
        )["schedule_id"]
        self.db.accounts.execute(
            "UPDATE loan_payment_schedule SET principal_due_minor_units=3000 "
            "WHERE schedule_id=?", (schedule_id,),
        )
        with self.db.readonly() as conn:
            ead, method = CreditRiskManager._ead_at_expected_loss(
                conn, loan_id, 100, epoch, .5, 1, "USD",
            )
            findings = BankJournal.reconcile_loan_payment_schedules(conn, "Bank")
        self.assertEqual(ead, 80)
        self.assertIn("midpoint_after_scheduled_principal", method)
        finding = next(row for row in findings if row["schedule_id"] == schedule_id)
        self.assertIn("principal_due", finding["legacy_mirror_mismatch_fields"])

    def test_collateral_recovery_assumption_update_is_audited_and_versioned(self):
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL3'"
        )["loan_id"]
        self.db.accounts.execute(
            "INSERT INTO loan_collateral_assets(loan_id,user_id,asset_code,quantity,pledged_epoch,"
            "last_price,last_value,status) VALUES (?,?,?,1,1,500,500,'pledged')",
            (loan_id, "ecl-borrower", "COLLATERAL"),
        )
        collateral = self.db.accounts.execute_one(
            "SELECT last_value,last_value_minor_units FROM loan_collateral_assets "
            "WHERE loan_id=?", (loan_id,),
        )
        self.assertEqual((collateral["last_value"], collateral["last_value_minor_units"]),
                         (500, 500))
        self.db.accounts.execute(
            "UPDATE loans SET collateral_amount=0,collateral_released=0,collateral_seized=0 "
            "WHERE loan_id=?", (loan_id,),
        )
        with self.db.atomic() as conn:
            loan = conn.execute("SELECT * FROM accounts.loans WHERE loan_id=?",
                                (loan_id,)).fetchone()
            lgd_with_collateral = CreditRiskManager._lgd(conn, loan)
            conn.execute(
                "UPDATE accounts.loan_collateral_assets SET last_value_minor_units=0 "
                "WHERE loan_id=?", (loan_id,),
            )
            lgd_without_collateral = CreditRiskManager._lgd(
                conn, conn.execute("SELECT * FROM accounts.loans WHERE loan_id=?",
                                   (loan_id,)).fetchone(),
            )
            conn.execute(
                "UPDATE accounts.loan_collateral_assets SET last_value_minor_units=500 "
                "WHERE loan_id=?", (loan_id,),
            )
        self.assertLess(lgd_with_collateral, lgd_without_collateral)
        collateral_id = self.db.accounts.execute_one(
            "SELECT collateral_id FROM loan_collateral_assets WHERE loan_id=?", (loan_id,)
        )["collateral_id"]
        with self.db.atomic() as conn:
            result = CreditRiskManager.configure_collateral_recovery(
                conn, collateral_id, "support-admin", seniority_rank=3,
                recovery_days=365, recovery_cost_bps=500,
            )
            event = conn.execute(
                "SELECT prior_snapshot,new_snapshot FROM accounts.loan_collateral_recovery_events"
            ).fetchone()
        self.assertEqual(result["seniority_rank"], 3)
        self.assertEqual(json.loads(event["prior_snapshot"]), {
            "seniority_rank": 1, "recovery_days": 0, "recovery_cost_bps": 0,
        })
        self.assertEqual(json.loads(event["new_snapshot"]), {
            "seniority_rank": 3, "recovery_days": 365, "recovery_cost_bps": 500,
        })

    def test_collateral_liquidation_minor_units_reconcile_to_immutable_event(self):
        loan_id = self.db.accounts.execute_one(
            "SELECT loan_id FROM loans WHERE product_code='ECL3'"
        )["loan_id"]
        self.db.accounts.execute(
            "INSERT INTO loan_collateral_assets(loan_id,user_id,asset_code,quantity,pledged_epoch,"
            "last_price,last_value,status,liquidation_cost,liquidation_proceeds) "
            "VALUES (?,?,?,1,1,100,100,'liquidated',10,90)",
            (loan_id, "ecl-borrower", "COLLATERAL"),
        )
        collateral_id = self.db.accounts.execute_one(
            "SELECT collateral_id FROM loan_collateral_assets WHERE loan_id=?", (loan_id,)
        )["collateral_id"]
        asset = self.db.accounts.execute_one(
            "SELECT liquidation_cost_minor_units,liquidation_proceeds_minor_units "
            "FROM loan_collateral_assets WHERE collateral_id=?", (collateral_id,)
        )
        self.assertEqual((asset["liquidation_cost_minor_units"],
                          asset["liquidation_proceeds_minor_units"]), (10, 90))
        self.db.accounts.execute(
            "INSERT INTO collateral_liquidation_events(loan_id,collateral_id,asset_code,quantity,"
            "mark_price,execution_price,gross_proceeds,liquidation_cost,net_proceeds,"
            "liquidated_epoch,gross_proceeds_minor_units,liquidation_cost_minor_units,"
            "net_proceeds_minor_units) VALUES (?,?,?,1,100,100,100,10,90,1,100,10,90)",
            (loan_id, collateral_id, "COLLATERAL"),
        )
        with self.db.atomic() as conn:
            findings = BankJournal.reconcile_collateral_liquidations(conn, "Bank")
        self.assertEqual(findings[0]["status"], "matched")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.db.accounts.execute(
                "UPDATE collateral_liquidation_events SET net_proceeds=91 WHERE collateral_id=?",
                (collateral_id,),
            )
        self.db.accounts.execute(
            "UPDATE loan_collateral_assets SET liquidation_proceeds=89 WHERE collateral_id=?",
            (collateral_id,),
        )
        with self.db.atomic() as conn:
            findings = BankJournal.reconcile_collateral_liquidations(conn, "Bank")
        self.assertEqual(findings[0]["status"], "mismatch")

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

    def test_pd_backtest_rejects_invalid_epoch_ranges(self):
        with self.db.readonly() as conn:
            for invalid_epoch in (-1, float("nan"), float("inf"), 1e300, True, "invalid"):
                with self.subTest(invalid_epoch=invalid_epoch):
                    with self.assertRaisesRegex(ValueError, "invalid_ecl_backtest_epoch"):
                        CreditRiskManager.backtest_12m(
                            conn, "낙원", issuer="Bank", now=invalid_epoch
                        )

    def test_pd_backtest_reports_out_of_time_discrimination_and_cohort_stability(self):
        epoch = 1_800_000_000.0
        cohorts = ((epoch-5*365*86400, (.01, .01)),
                   (epoch-2*365*86400, (.30, .30)))
        for cohort_index, (assessed, probabilities) in enumerate(cohorts):
            for loan_index, pd in enumerate(probabilities):
                borrower_id = f"ecl-borrower-{cohort_index}-{loan_index}"
                product_code = f"ECL-VAL-{cohort_index}-{loan_index}"
                self.db.users.create_user(borrower_id, "Borrower", "낙원", 0)
                self.db.products.execute(
                    "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
                    "interest_cycle,min_credit_score,issuer,quote_currency) "
                    "VALUES (?, 'Validation',5,10000,365,86400,0,'ValidationBank','NWX')",
                    (product_code,),
                )
                is_default = cohort_index == 0 and loan_index == 0
                default_epoch = assessed+100*86400 if is_default else None
                self.db.accounts.execute(
                    "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,"
                    "created_at,collection_status,quote_currency,defaulted_epoch) "
                    "VALUES (?, ?,100,100,'active',datetime('now'),'current','NWX',?)",
                    (borrower_id,product_code,default_epoch),
                )
                loan_id = self.db.accounts.execute_one(
                    "SELECT MAX(loan_id) FROM loans WHERE user_id=? AND product_code=?",
                    (borrower_id,product_code),
                )[0]
                self.db.accounts.execute(
                    "INSERT INTO loan_ecl_assessments(loan_id,as_of_day,stage,pd_12m,lifetime_pd,"
                    "lgd,ead,expected_loss,quote_currency,calculated_epoch,risk_grade) "
                    "VALUES (?, ?,1,?, ?, .4,100,1,'NWX',?,?)",
                    (loan_id, f"{cohort_index}-{loan_index}", pd, pd, assessed,
                     CreditRiskManager.risk_grade(pd)),
                )
        with self.db.readonly() as conn:
            report = CreditRiskManager.backtest_12m(conn, "낙원", issuer="ValidationBank", now=epoch)
        self.assertAlmostEqual(report["discrimination"][0]["auroc"], 1/6)
        self.assertEqual(sum(row["borrowerCount"] for row in report["borrowerHoldout"]), 2)
        self.assertTrue(all("20% SHA-256 borrower holdout" in row["method"]
                            for row in report["borrowerHoldout"]))
        self.assertEqual(
            sum(row["borrowerCount"] for row in report["outOfTimeBorrowerHoldout"]), 2
        )
        recent_holdout = report["outOfTimeBorrowerHoldout"][0]
        self.assertIn("chronological out-of-time holdout", recent_holdout["method"])
        self.assertGreater(recent_holdout["cohortEndEpoch"], recent_holdout["cohortStartEpoch"])
        self.assertLessEqual(recent_holdout["observedRateLower95"],
                             recent_holdout["observedDefaultRate"])
        self.assertGreaterEqual(recent_holdout["observedRateUpper95"],
                                recent_holdout["observedDefaultRate"])
        for row in report["borrowerHoldout"]:
            self.assertLessEqual(row["observedRateLower95"], row["observedDefaultRate"])
            self.assertGreaterEqual(row["observedRateUpper95"], row["observedDefaultRate"])
            self.assertIn("borrower-level Wilson 95% interval", row["method"])
        stability = report["stability"][0]
        self.assertEqual((stability["olderCount"], stability["recentCount"]), (2, 2))
        self.assertGreater(stability["populationStabilityIndex"], 0)

    def test_ecl_assessment_rejects_invalid_epoch_before_recording(self):
        before = self.db.accounts.execute_one(
            "SELECT COUNT(*) FROM loan_ecl_assessments"
        )[0]
        for invalid_epoch in (-1, float("nan"), float("inf"), 1e300, True, "invalid"):
            with self.subTest(invalid_epoch=invalid_epoch):
                with self.assertRaisesRegex(ValueError, "invalid_ecl_assessment_epoch"):
                    with self.db.atomic() as conn:
                        CreditRiskManager.assess_conn(conn, invalid_epoch)
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT COUNT(*) FROM loan_ecl_assessments"
        )[0], before)

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
        baseline_path = next(item["macroForecast"] for item in baseline_scenario
                             if item["key"] == "base")
        stress_path = next(item["macroForecast"] for item in stress_scenario
                           if item["key"] == "downside")
        self.assertEqual(baseline_path["relativePdMultiplier"], 1)
        self.assertGreater(stress_path["relativePdMultiplier"], 1)
        self.assertIn("simulator assumptions, not external forecasts",
                      stress_path["method"])

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
