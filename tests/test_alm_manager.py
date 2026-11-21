"""M6: capital, leverage, LCR and NSFR of a financial institution, with exact hand-computed figures."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.alm_manager import ALMManager, PRUDENTIAL_DEFAULTS
from managers.funding_manager import FundingManager
from managers.rate_manager import RateManager

DAY = 86400
NOW = 1_800_000_000


class ALMManagerTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO financial_institutions(issuer,nationality,equity_capital,retained_earnings,deposit_insurance_limit) "
            "VALUES ('Bank','낙원',1000,0,300)")
        self.db.products.execute(
            "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,interest_cycle,compound_type,issuer,quote_currency) "
            "VALUES ('S1','deposit',1,0,30,86400,0,'Bank','NWX')")
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer,quote_currency) "
            "VALUES ('L1','loan',5,10000,90,86400,0,'Bank','NWX')")
        for user in ("u1", "u2", "company:acme", "b1", "b2", "b3"):
            self.db.users.create_user(user, user, "낙원", 0)
        # retail u1 500 insured (limit 300 -> 300 stable, 200 less stable); retail u2 100 uninsured; company 400 insured
        for number, user, balance, insured in (("A1", "u1", 500, 1), ("A2", "u2", 100, 0), ("A3", "company:acme", 400, 1)):
            self.db.accounts.execute(
                "INSERT INTO accounts(account_number,user_id,product_code,balance,principal,account_type,quote_currency,balance_units,"
                "created_at,resolution_status,deposit_insured) VALUES (?,?,?,?,?,'saving','NWX',0,datetime(?,'unixepoch'),'active',?)",
                (number, user, "S1", balance, balance, NOW, insured))
        # loans: 600 (100 days, performing, stage 1), 200 (400 days, stage 2), 100 defaulted (stage 3)
        for loan_id, balance, days, status, collection in ((1, 600, 100, "active", "current"), (2, 200, 400, "active", "current"),
                                                           (3, 100, 50, "defaulted", "defaulted")):
            self.db.accounts.execute(
                "INSERT INTO loans(loan_id,user_id,product_code,principal,outstanding_balance,status,collection_status,created_at,"
                "maturity_epoch,quote_currency) VALUES (?,?,'L1',?,?,?,?,datetime(?,'unixepoch'),?,'NWX')",
                (loan_id, f"b{loan_id}", balance, balance, status, collection, NOW, NOW + days * DAY))
        for loan_id, stage, loss in ((1, 1, 6), (2, 2, 4), (3, 3, 50)):
            self.db.accounts.execute(
                "INSERT INTO loan_ecl_assessments(loan_id,as_of_day,stage,pd_12m,lifetime_pd,lgd,ead,expected_loss,quote_currency,calculated_epoch) "
                "VALUES (?,'2027-01-15',?,0.01,0.02,0.4,100,?,'NWX',?)", (loan_id, stage, loss, NOW))
        self.db.accounts.execute(
            "INSERT INTO loan_payment_schedule(loan_id,installment_number,due_epoch,principal_due,interest_due,fee_due,amount_paid,status) "
            "VALUES (1,1,?,100,0,0,0,'scheduled')", (NOW + 10 * DAY,))
        # wholesale: interbank 200 (20 days), repo 100 (400 days, collateral 120), central bank 50 (100 days)
        with self.db.atomic() as conn:
            for source, amount, days, extra in (("interbank", 200, 20, {}), ("repo", 100, 400, {"collateral": 120}),
                                                ("central_bank", 50, 100, {"collateral": 60})):
                FundingManager.set_limit(conn, "Bank", source, "NWX", 1000, actor="admin", now=NOW)
                FundingManager.draw(conn, "Bank", source, "NWX", amount, reference_key=f"k-{source}", rate_bps=300,
                                    maturity_days=days, now=NOW, **extra)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def report(self):
        with self.db.readonly() as conn:
            return ALMManager.report(conn, "Bank", now=NOW)

    def test_balance_sheet_is_modelled_from_the_books(self):
        sheet = self.report()["balanceSheet"]
        self.assertEqual(sheet["deposits"], {"retailStable": Decimal(300), "retailLessStable": Decimal(300),
                                             "corporate": Decimal(400), "total": Decimal(1000)})
        self.assertEqual((sheet["grossLoans"], sheet["allowance"], sheet["netLoans"]), (Decimal(900), Decimal(60), Decimal(840)))
        self.assertEqual((sheet["wholesaleFunding"], sheet["equity"]), (Decimal(350), Decimal(1000)))
        # what deposits, wholesale funding and equity fund beyond the net loans is cash and reserves
        self.assertEqual(sheet["cashAndReserves"], Decimal(1000 + 350 + 1000 - 840))
        self.assertEqual(sheet["totalAssets"], sheet["cashAndReserves"] + sheet["netLoans"])
        self.assertEqual(sheet["fundingGap"], 0)

    def test_capital_classes_buffers_and_ratios(self):
        report = self.report()
        capital = report["capital"]
        with self.db.readonly() as conn:
            rwa = Decimal(str(RateManager.institution_balance_sheet(conn, "Bank")["risk_weighted_assets"]))
        self.assertGreater(rwa, 0)
        self.assertEqual((capital["cet1"], capital["at1"], capital["tier1"]), (Decimal(1000),) * 1 + (Decimal(0), Decimal(1000)))
        # Tier 2: stage 1-2 allowances (6 + 4), never above 1.25% of the risk-weighted assets
        self.assertEqual(capital["tier2"], min(Decimal(10), rwa * Decimal("0.0125")))
        self.assertEqual(capital["total"], capital["tier1"] + capital["tier2"])
        self.assertAlmostEqual(capital["ratiosPercent"]["cet1"], float(Decimal(1000) * 100 / rwa), places=6)
        self.assertEqual(capital["requirementsPercent"], {"cet1": 7.0, "tier1": 8.5, "total": 10.5})
        self.assertEqual(capital["buffersPercent"], {"conservation": 2.5, "countercyclical": 0.0, "systemic": 0.0})
        self.assertEqual(capital["status"]["cet1"], "ok")
        self.assertAlmostEqual(capital["headroomPercent"]["cet1"], capital["ratiosPercent"]["cet1"] - 7.0, places=9)

    def test_leverage_ratio_uses_tier1_over_total_assets(self):
        leverage = self.report()["leverage"]
        self.assertEqual(leverage["exposure"], Decimal(1510 + 840))
        self.assertAlmostEqual(leverage["ratioPercent"], 1000 * 100 / 2350, places=9)
        self.assertEqual((leverage["minimumPercent"], leverage["status"]), (3.0, "ok"))

    def test_lcr_follows_the_run_off_and_inflow_rules(self):
        lcr = self.report()["lcr"]
        by_category = {row["category"]: row for row in lcr["outflows"]}
        self.assertEqual(by_category["retail_stable_deposits"]["amount"], Decimal(15))      # 300 x 5%
        self.assertEqual(by_category["retail_less_stable_deposits"]["amount"], Decimal(30))  # 300 x 10%
        self.assertEqual(by_category["corporate_deposits"]["amount"], Decimal(160))          # 400 x 40%
        self.assertEqual(by_category["funding_interbank_due_30d"]["amount"], Decimal(200))   # unsecured, due in 20 days
        self.assertNotIn("funding_repo_due_30d", by_category)                                # 400 days away
        self.assertEqual(lcr["totalOutflows"], Decimal(405))
        self.assertEqual(lcr["inflows"]["performingLoanPayments30d"], Decimal(100))
        self.assertEqual(lcr["inflows"]["countedInflows"], Decimal(50))                      # 100 x 50%, far below the 75% cap
        self.assertEqual(lcr["netOutflows"], Decimal(355))
        self.assertEqual(lcr["hqla"]["total"], Decimal(1510))
        self.assertAlmostEqual(lcr["ratioPercent"], 1510 * 100 / 355, places=9)
        self.assertEqual(lcr["status"], "ok")

    def test_inflows_are_capped_at_75_percent_of_outflows(self):
        with self.db.atomic() as conn:
            ALMManager.set_parameter(conn, "낙원", "inflow_performing_loan_bps", 100000, actor="admin", now=NOW)
        lcr = self.report()["lcr"]
        self.assertEqual(lcr["inflows"]["grossInflows"], Decimal(1000))                      # 100 x 1000%
        self.assertEqual(lcr["inflows"]["countedInflows"], Decimal("303.75"))                # 75% of 405
        self.assertEqual(lcr["netOutflows"], Decimal("101.25"))

    def test_nsfr_weights_stable_funding_and_required_funding(self):
        nsfr = self.report()["nsfr"]
        asf = {row["category"]: row["amount"] for row in nsfr["asf"]}
        self.assertEqual(asf["equity"], Decimal(1000))
        self.assertEqual(asf["retail_stable_deposits"], Decimal(285))
        self.assertEqual(asf["retail_less_stable_deposits"], Decimal(270))
        self.assertEqual(asf["corporate_deposits"], Decimal(200))
        self.assertEqual(asf["wholesale_1y_plus"], Decimal(100))        # the repo that matures in 400 days
        self.assertEqual(asf["wholesale_under_6m"], Decimal(0))         # interbank 20 days + central bank 100 days
        self.assertEqual(nsfr["availableStableFunding"], Decimal(1855))
        rsf = {row["category"]: row["amount"] for row in nsfr["rsf"]}
        self.assertEqual(rsf["cash_and_reserves"], Decimal(0))
        # loans carried net of the allowances (840 / 900), then 50% / 85% / 100%
        self.assertEqual(rsf["loans_under_1y"], Decimal(280))
        self.assertEqual(rsf["loans_1y_plus"].quantize(Decimal("0.0001")), Decimal("158.6667"))
        self.assertEqual(rsf["defaulted_loans"].quantize(Decimal("0.0001")), Decimal("93.3333"))
        self.assertEqual(nsfr["requiredStableFunding"].quantize(Decimal("0.0001")), Decimal("532.0000"))
        self.assertAlmostEqual(nsfr["ratioPercent"], 1855 * 100 / 532, places=6)

    def test_a_thin_capital_base_is_reported_as_breach(self):
        self.db.currency.execute("UPDATE financial_institutions SET equity_capital=10 WHERE issuer='Bank'")
        report = self.report()
        self.assertEqual(report["capital"]["status"]["cet1"], "breach")
        self.assertLess(report["capital"]["headroomPercent"]["cet1"], 0)

    def test_loans_larger_than_their_funding_are_a_funding_gap_not_negative_cash(self):
        self.db.currency.execute("UPDATE financial_institutions SET equity_capital=1 WHERE issuer='Bank'")
        self.db.accounts.execute("UPDATE loans SET outstanding_balance=5000 WHERE loan_id=1")
        sheet = self.report()["balanceSheet"]
        self.assertEqual(sheet["cashAndReserves"], Decimal(0))
        self.assertGreater(sheet["fundingGap"], 0)

    def test_jurisdiction_override_changes_the_requirement_and_is_listed(self):
        with self.db.atomic() as conn:
            ALMManager.set_parameter(conn, "낙원", "countercyclical_buffer_bps", 100, actor="admin", now=NOW)
        report = self.report()
        self.assertEqual(report["capital"]["buffersPercent"]["countercyclical"], 1.0)
        self.assertEqual(report["capital"]["requirementsPercent"]["cet1"], 8.0)
        self.assertEqual(report["parameters"]["overriddenForJurisdiction"], ["countercyclical_buffer_bps"])
        self.assertEqual(report["parameters"]["values"]["cet1_min_bps"], PRUDENTIAL_DEFAULTS["cet1_min_bps"])

    def test_override_input_is_validated(self):
        with self.db.atomic() as conn:
            for key, value in (("unknown_key", 100), ("cet1_min_bps", -1), ("cet1_min_bps", 1.5), ("cet1_min_bps", True), ("cet1_min_bps", 100001)):
                with self.assertRaisesRegex(ValueError, "invalid_prudential_parameter"):
                    ALMManager.set_parameter(conn, "낙원", key, value, actor="admin", now=NOW)
            with self.assertRaisesRegex(ValueError, "invalid_prudential_parameter"):
                ALMManager.set_parameter(conn, "", "cet1_min_bps", 100, actor="admin", now=NOW)

    def test_report_reads_only(self):
        with self.db.readonly() as conn:
            before = conn.execute("SELECT COUNT(*) FROM institution_funding").fetchone()[0]
            ALMManager.report(conn, "Bank", now=NOW)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM institution_funding").fetchone()[0], before)
            with self.assertRaisesRegex(ValueError, "financial_institution_not_found"):
                ALMManager.report(conn, "  ", now=NOW)


if __name__ == "__main__":
    unittest.main()
