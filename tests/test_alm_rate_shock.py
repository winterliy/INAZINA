"""M6: delta NII / delta EVE for parallel rate shocks with deposit beta and core-deposit maturity, hand-computed."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.alm_manager import ALMManager, SHOCKS_BPS
from managers.rate_manager import RateManager

DAY = 86400
NOW = 1_800_000_000


class RateShockTests(unittest.TestCase):
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
            "VALUES ('L1','loan',6,10000,400,86400,0,'Bank','NWX')")
        for user in ("u1", "u2", "u3", "b1", "b2"):
            self.db.users.create_user(user, user, "낙원", 0)
        # non-term deposits: 1000 at 2% and 1000 at 0.4%; a 30-day fixed-term deposit of 500
        for number, user, code, balance, rate in (("A1", "u1", "S2", 1000, 2), ("A2", "u2", "S2", 1000, "0.4"), ("A3", "u3", "S1", 500, 2)):
            self.db.accounts.execute(
                "INSERT INTO accounts(account_number,user_id,product_code,balance,principal,account_type,quote_currency,balance_units,"
                "created_at,resolution_status,deposit_insured,contract_rate) VALUES (?,?,?,?,?,'saving','NWX',0,datetime(?,'unixepoch'),'active',1,?)",
                (number, user, code, balance, balance, NOW, rate))
        # floating loan 1000 at 6%, floor 5, cap 7, repricing now, outlasting the 12-month horizon
        self.db.accounts.execute(
            "INSERT INTO loans(loan_id,user_id,product_code,principal,outstanding_balance,status,collection_status,created_at,maturity_epoch,"
            "quote_currency,rate_type,contract_rate,rate_floor,rate_cap,next_repricing_epoch) "
            "VALUES (1,'b1','L1',1000,1000,'active','current',datetime(?,'unixepoch'),?,'NWX','floating',6,5,7,?)",
            (NOW, NOW + 400 * DAY, NOW))

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def shock(self):
        with self.db.readonly() as conn:
            report = ALMManager.report(conn, "Bank", now=NOW)
        return report, {row["basisPoints"]: row for row in report["rateShock"]["shocks"]}

    def test_delta_nii_combines_floor_cap_and_deposit_beta_with_the_zero_rate_floor(self):
        report, rows = self.shock()
        self.assertEqual(tuple(rows), SHOCKS_BPS)
        # loan: +100 -> 7% (+10), +200 capped at 7% (+10), -100 -> 5% (-10), -200 floored at 5% (-10)
        self.assertEqual({bp: rows[bp]["contractualNii"] for bp in rows}, {-200: -10, -100: -10, 0: 0, 100: 10, 200: 10})
        # deposits at beta 50%: the 2% deposit costs +-0.5%/1% of 1000; the 0.4% deposit cannot go below 0 (-0.4% of 1000 at most)
        self.assertEqual({bp: rows[bp]["depositBetaNii"] for bp in rows}, {-200: 14, -100: 9, 0: 0, 100: -10, 200: -20})
        self.assertEqual({bp: rows[bp]["deltaNii"] for bp in rows}, {-200: 4, -100: -1, 0: 0, 100: 0, 200: -10})
        self.assertEqual(report["rateShock"]["behavioralDeposits"], Decimal(2000))   # the term deposit is not behavioural

    def test_delta_eve_adds_the_core_deposit_value_to_the_fixed_rate_result(self):
        report, rows = self.shock()
        with self.db.readonly() as conn:
            fixed = {s["basisPoints"]: Decimal(str(s["estimatedEveChange"]))
                     for s in RateManager.institution_fixed_rate_eve_stress(conn, "Bank", now=NOW)["scenarios"][0]["shocks"]}
        core = Decimal(1200)                                     # 60% of 2000
        for bp in SHOCKS_BPS:
            s = Decimal(bp) / 10000
            self.assertEqual(rows[bp]["coreDepositEve"], core * 3 * s - core * 9 * s * s / 2)
            self.assertEqual(rows[bp]["contractualEve"], fixed.get(bp, Decimal(0)))
        self.assertEqual(rows[100]["coreDepositEve"], Decimal("35.46"))
        self.assertEqual(rows[-100]["coreDepositEve"], Decimal("-36.54"))
        self.assertEqual(rows[0]["deltaEve"], 0)
        self.assertAlmostEqual(rows[100]["deltaEvePercentOfTier1"], float(rows[100]["deltaEve"]) / 10, places=9)   # Tier 1 = 1000

    def test_assumptions_are_per_jurisdiction_and_zero_beta_means_no_deposit_cost_change(self):
        with self.db.atomic() as conn:
            ALMManager.set_parameter(conn, "낙원", "deposit_beta_bps", 0, actor="admin", now=NOW)
            ALMManager.set_parameter(conn, "낙원", "core_deposit_years", 0, actor="admin", now=NOW)
        report, rows = self.shock()
        self.assertTrue(all(rows[bp]["depositBetaNii"] == 0 for bp in rows))
        self.assertTrue(all(rows[bp]["coreDepositEve"] == 0 for bp in rows))
        self.assertEqual(report["rateShock"]["assumptions"], {"depositBetaBps": 0, "coreDepositShareBps": 6000, "coreDepositYears": 0,
                                                                     "basisSqueezeBps": 0, "prepaymentBps": 0})
        with self.db.atomic() as conn:
            ALMManager.set_parameter(conn, "낙원", "deposit_beta_bps", 10000, actor="admin", now=NOW)
        _, rows = self.shock()
        self.assertEqual(rows[100]["depositBetaNii"], Decimal(-20))      # full pass-through on 2000: 1% each

    def test_new_assumption_ranges_are_validated(self):
        with self.db.atomic() as conn:
            for key, value in (("deposit_beta_bps", 10001), ("core_deposit_share_bps", 10001), ("core_deposit_years", 31), ("core_deposit_years", -1)):
                with self.assertRaisesRegex(ValueError, "invalid_prudential_parameter_value"):
                    ALMManager.set_parameter(conn, "낙원", key, value, actor="admin", now=NOW)

    def test_a_bank_without_balances_has_zero_shocks(self):
        self.db.currency.execute(
            "INSERT INTO financial_institutions(issuer,nationality,equity_capital,retained_earnings,deposit_insurance_limit) VALUES ('Empty','낙원',1,0,0)")
        with self.db.readonly() as conn:
            shock = ALMManager.report(conn, "Empty", now=NOW)["rateShock"]
        self.assertTrue(all(r["deltaNii"] == 0 and r["deltaEve"] == 0 and r["deltaEvePercentOfTier1"] == 0 for r in shock["shocks"]))

    def test_basis_squeeze_and_prepayment_are_assumptions_that_default_to_zero(self):
        _, rows = self.shock()
        self.assertTrue(all(rows[bp]["basisNii"] == 0 and rows[bp]["prepaymentEve"] == 0 for bp in rows))      # not assumed unless set
        self.db.accounts.execute(
            "INSERT INTO loans(loan_id,user_id,product_code,principal,outstanding_balance,status,collection_status,created_at,maturity_epoch,"
            "quote_currency,rate_type,contract_rate) VALUES (2,'b2','L1',1000,1000,'active','current',datetime(?,'unixepoch'),?,'NWX','fixed',6)",
            (NOW, NOW + 730 * DAY))
        with self.db.atomic() as conn:
            ALMManager.set_parameter(conn, "낙원", "basis_squeeze_bps", 100, actor="admin", now=NOW)
            ALMManager.set_parameter(conn, "낙원", "prepayment_bps", 5000, actor="admin", now=NOW)
        report, rows = self.shock()
        # basis: 1% of the 1000 balance-years of floating loans, lost in every non-zero shock and not in the 0bp row
        self.assertEqual({bp: rows[bp]["basisNii"] for bp in rows}, {-200: -10, -100: -10, 0: 0, 100: -10, 200: -10})
        self.assertEqual(rows[100]["deltaNii"], rows[100]["contractualNii"] + rows[100]["depositBetaNii"] - 10)
        # prepayment: the fixed 1000 loan has 2 years (asset duration 2000, convexity 4000); half prepaid -> +A*p*s - 0.5*C*p*s^2
        self.assertEqual(rows[100]["prepaymentEve"], Decimal(1000 * 2) * Decimal("0.5") * Decimal("0.01") - Decimal(4000) * Decimal("0.5") * Decimal("0.5") * Decimal("0.0001"))
        self.assertEqual(rows[-100]["prepaymentEve"], Decimal("-10.1"))
        self.assertEqual(rows[0]["prepaymentEve"], 0)
        self.assertEqual(rows[100]["deltaEve"], rows[100]["contractualEve"] + rows[100]["coreDepositEve"] + rows[100]["prepaymentEve"])
        self.assertEqual((report["rateShock"]["assumptions"]["basisSqueezeBps"], report["rateShock"]["assumptions"]["prepaymentBps"]), (100, 5000))

    def test_new_assumption_ranges_are_validated_for_basis_and_prepayment(self):
        with self.db.atomic() as conn:
            for key, value in (("basis_squeeze_bps", 3001), ("prepayment_bps", 10001), ("basis_squeeze_bps", -1)):
                with self.assertRaisesRegex(ValueError, "invalid_prudential_parameter_value"):
                    ALMManager.set_parameter(conn, "낙원", key, value, actor="admin", now=NOW)

    def test_prefetched_schedules_and_cached_curve_rates_give_identical_results(self):
        from managers.rate_manager import RateManager
        self.db.accounts.execute(
            "INSERT INTO loans(loan_id,user_id,product_code,principal,outstanding_balance,status,collection_status,created_at,maturity_epoch,"
            "quote_currency,rate_type,contract_rate) VALUES (2,'b2','L1',900,900,'active','current',datetime(?,'unixepoch'),?,'NWX','fixed',6)",
            (NOW, NOW + 300 * DAY))
        for number, days, principal in ((1, 100, 300), (2, 200, 300), (3, 300, 300), (4, 400, 50)):      # the last one falls after maturity
            self.db.accounts.execute(
                "INSERT INTO loan_payment_schedule(loan_id,installment_number,due_epoch,principal_due,interest_due,fee_due,amount_paid,status) "
                "VALUES (2,?,?,?,0,0,0,'scheduled')", (number, NOW + days * DAY, principal))
        with self.db.readonly() as conn:
            plain = RateManager.institution_fixed_rate_eve_stress(conn, "Bank", now=NOW)
            fast = RateManager.institution_fixed_rate_eve_stress(conn, "Bank", now=NOW, prefetch_schedules=True)
            self.assertEqual(plain, fast)
            self.assertGreater(plain["scenarios"][0]["assetDurationAmountYears"], 0)
            rates = {}
            self.assertEqual(ALMManager._market_rate(conn, "낙원", "funding", 365, rates), RateManager.market_rate(conn, "낙원", "funding", 365))
            rates[("funding", 365)] = "remembered"
            self.assertEqual(ALMManager._market_rate(conn, "낙원", "funding", 365, rates), "remembered")      # a second ask reads nothing

    def test_the_report_stays_read_only(self):
        with self.db.readonly() as conn:
            ALMManager.report(conn, "Bank", now=NOW)


if __name__ == "__main__":
    unittest.main()
