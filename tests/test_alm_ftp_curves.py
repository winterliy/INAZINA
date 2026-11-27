"""M6: forward rates from the zero curves and the read-only FTP preview, hand-computed."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.alm_manager import ALMManager
from managers.funding_manager import FundingManager

NOW = 1_800_000_000


class FtpCurveTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO financial_institutions(issuer,nationality,equity_capital,retained_earnings,deposit_insurance_limit) "
            "VALUES ('Bank','낙원',1000,0,300)")
        for code, days in (("S2", 0), ("S1", 30)):
            self.db.products.execute(
                "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,interest_cycle,compound_type,issuer,quote_currency) "
                "VALUES (?,?,2,0,?,86400,0,'Bank','NWX')", (code, code, days))
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer,quote_currency) "
            "VALUES ('L1','loan',6,10000,730,86400,0,'Bank','NWX')")
        # funding curve: 2% up to one year, 3% from two years on; bank bond curve negative and falling
        self.db.currency.execute("DELETE FROM market_rate_curve WHERE nationality='낙원' AND benchmark_key IN ('funding','bank_bond')")
        for key, points in (("funding", ((365, 2.0), (730, 3.0))), ("bank_bond", ((365, -1.0), (730, -2.0)))):
            for tenor, rate in points:
                self.db.currency.execute("INSERT INTO market_rate_curve VALUES ('낙원',?,?,?,0.75,0)", (key, tenor, rate))

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def report(self):
        with self.db.readonly() as conn:
            return ALMManager.report(conn, "Bank", now=NOW)

    def near(self, value, expected):
        self.assertAlmostEqual(float(value), expected, places=6)

    def test_forward_rates_follow_the_zero_curve_and_negative_ones_are_flagged_not_floored(self):
        curves = {c["benchmark"]: {(f["fromDays"], f["toDays"]): f for f in c["forwards"]} for c in self.report()["forwardCurves"]["curves"]}
        funding = curves["funding"]
        self.near(funding[(180, 365)]["forwardPercent"], 2.0)       # flat part
        self.near(funding[(365, 730)]["forwardPercent"], 4.0)       # (3 x 2 - 2 x 1) / 1
        self.near(funding[(730, 1095)]["forwardPercent"], 3.0)      # flat again
        self.assertFalse(funding[(365, 730)]["negative"])
        bond = curves["bank_bond"][(365, 730)]
        self.near(bond["forwardPercent"], -3.0)                     # (-2 x 2 - (-1) x 1) / 1
        self.assertTrue(bond["negative"])

    def test_ftp_is_the_funding_curve_plus_premium_and_margins_follow_the_side(self):
        ftp = self.report()["ftp"]
        products = {p["productCode"]: p for p in ftp["products"]}
        self.near(ftp["liquidityPremiumPercent"], 0.3)
        self.assertEqual(products["L1"]["tenorDays"], 730)
        self.near(products["L1"]["ftpPercent"], 3.3)
        self.near(products["L1"]["marginPercent"], 2.7)             # loan: 6 - 3.3
        self.assertEqual((products["S1"]["tenorDays"], products["S1"]["tenorBasis"]), (30, "contract term"))
        self.near(products["S1"]["marginPercent"], 0.3)             # deposit: FTP 2.3 - rate 2
        self.assertEqual((products["S2"]["tenorDays"], products["S2"]["tenorBasis"]), (1095, "behavioural maturity assumption"))
        self.near(products["S2"]["marginPercent"], 1.3)             # deposit: FTP 3.3 - rate 2
        self.assertIs(ftp["applied"], False)
        self.assertIsNone(ftp["wholesaleWeightedAverageRatePercent"])

    def test_premium_is_a_jurisdiction_assumption_and_wholesale_average_is_shown_for_reference(self):
        with self.db.atomic() as conn:
            ALMManager.set_parameter(conn, "낙원", "ftp_liquidity_premium_bps", 100, actor="admin", now=NOW)
            FundingManager.set_limit(conn, "Bank", "interbank", "NWX", 10000, actor="admin", now=NOW)
            FundingManager.draw(conn, "Bank", "interbank", "NWX", 100, reference_key="k1", rate_bps=300, maturity_days=90, now=NOW)
            FundingManager.draw(conn, "Bank", "interbank", "NWX", 200, reference_key="k2", rate_bps=600, maturity_days=90, now=NOW)
        ftp = self.report()["ftp"]
        self.near(ftp["wholesaleWeightedAverageRatePercent"], 5.0)  # (100 x 3% + 200 x 6%) / 300
        self.near({p["productCode"]: p for p in ftp["products"]}["L1"]["marginPercent"], 2.0)    # 6 - (3 + 1)
        with self.db.atomic() as conn:
            with self.assertRaisesRegex(ValueError, "invalid_prudential_parameter_value"):
                ALMManager.set_parameter(conn, "낙원", "ftp_liquidity_premium_bps", 10001, actor="admin", now=NOW)

    def test_term_premium_defaults_to_zero_and_corrects_long_tenor_margins_when_set(self):
        products = {p["productCode"]: p for p in self.report()["ftp"]["products"]}
        for p in products.values():
            self.assertEqual((p["termPremiumPercent"], p["marginBeforeTermPercent"]), (0, p["marginPercent"]))       # default keeps today's behaviour
        with self.db.atomic() as conn:
            for key, value in (("ftp_term_premium_2y_bps", 50), ("ftp_term_premium_3y_bps", 150), ("ftp_term_premium_5y_bps", 250), ("ftp_term_premium_10y_bps", 400)):
                ALMManager.set_parameter(conn, "낙원", key, value, actor="admin", now=NOW)
        ftp = self.report()["ftp"]
        products = {p["productCode"]: p for p in ftp["products"]}
        self.near(products["L1"]["termPremiumPercent"], 0.5)                 # 730 days -> the 2y anchor
        self.near(products["L1"]["ftpPercent"], 3.8)                         # 3.0 curve + 0.3 liquidity + 0.5 term
        self.near(products["L1"]["marginPercent"], 2.2)                      # 6 - 3.8, was 2.7
        self.near(products["L1"]["marginBeforeTermPercent"], 2.7)
        self.near(products["S2"]["termPremiumPercent"], 1.5)                 # behavioural 1095 days -> the 3y anchor
        self.near(products["S2"]["marginPercent"], 2.8)                      # deposit credited 3.0 + 0.3 + 1.5 = 4.8, less 2.0 paid
        self.near(products["S1"]["termPremiumPercent"], 0)                   # 30 days: no term premium
        self.assertEqual([c["tenorDays"] for c in ftp["curve"]][-1], 3650)
        self.near(ftp["curve"][-1]["ftpPercent"] - ftp["curve"][-1]["ftpBeforeTermPercent"], 4.0)

    def test_term_premium_interpolates_linearly_and_is_flat_outside_the_anchors(self):
        param = {"ftp_term_premium_2y_bps": 50, "ftp_term_premium_3y_bps": 150, "ftp_term_premium_5y_bps": 250, "ftp_term_premium_10y_bps": 400}
        at = lambda days: ALMManager.term_premium_percent(param, days)
        self.assertEqual((at(1), at(365)), (0, 0))
        self.assertEqual(at(730), Decimal("0.5"))
        self.assertEqual(at(547), Decimal(182) / 365 * Decimal("0.5"))                     # midway between 0 and the 2y anchor
        self.assertEqual(at(1460), Decimal("1.5") + (Decimal("2.5") - Decimal("1.5")) * Decimal(365) / 730)
        self.assertEqual((at(3650), at(10000)), (Decimal(4), Decimal(4)))

    def test_term_premium_ranges_are_validated(self):
        with self.db.atomic() as conn:
            for value in (3001, -1):
                with self.assertRaisesRegex(ValueError, "invalid_prudential_parameter_value"):
                    ALMManager.set_parameter(conn, "낙원", "ftp_term_premium_5y_bps", value, actor="admin", now=NOW)

    def test_the_preview_never_changes_a_product_rate(self):
        before = [tuple(r) for r in self.db.products.execute("SELECT product_code,interest_rate FROM saving UNION SELECT product_code,interest_rate FROM liability ORDER BY 1")]
        self.report()
        after = [tuple(r) for r in self.db.products.execute("SELECT product_code,interest_rate FROM saving UNION SELECT product_code,interest_rate FROM liability ORDER BY 1")]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
