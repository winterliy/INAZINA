"""M6: loans, deposits and funding by currency and the open foreign-currency position, hand-computed."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from database import DatabaseManager
from database.db_manager import DBManager
from managers.alm_manager import ALMManager
from managers.funding_manager import FundingManager

NOW = 1_800_000_000


class CurrencyRiskTests(unittest.TestCase):
    def setUp(self):
        self.old_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.currency.execute(
            "INSERT INTO financial_institutions(issuer,nationality,equity_capital,retained_earnings,deposit_insurance_limit) "
            "VALUES ('Bank','낙원',1000,0,300)")
        # 120 SCD = 1 NWX; 1 EUX = 1/2 NWX
        for code, rate, places in (("SCD", "1/120", 2), ("EUX", "1/2", 2)):
            self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES (?,?,?,?)", (code, code, rate, places))
        for code, currency in (("S2", "NWX"), ("SSCD", "SCD"), ("SEUX", "EUX")):
            self.db.products.execute(
                "INSERT INTO saving(product_code,name,interest_rate,deposit_amount,maturity_days,interest_cycle,compound_type,issuer,quote_currency) "
                "VALUES (?,?,1,0,0,86400,0,'Bank',?)", (code, code, currency))
        for code, currency in (("L1", "NWX"), ("LSCD", "SCD")):
            self.db.products.execute(
                "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer,quote_currency) "
                "VALUES (?,?,5,100000000,365,86400,0,'Bank',?)", (code, code, currency))
        for user in ("u1", "u2", "u3", "b1", "b2"):
            self.db.users.create_user(user, user, "낙원", 0)
        # NWX: loan 600, deposit 400 -> +200 (base, no FX risk). SCD: loan 24000, deposit 12000 -> +12000 SCD = +100 NWX (long).
        # EUX: deposit 400, no loans -> -400 EUX = -200 NWX (short).
        for number, user, code, currency, units, balance in (("A1", "u1", "S2", "NWX", 0, 400), ("A2", "u2", "SSCD", "SCD", 12_000_000_000, 0),
                                                            ("A3", "u3", "SEUX", "EUX", 400_000_000, 0)):
            kind = "saving" if currency == "NWX" else "currency_saving"
            self.db.accounts.execute(
                "INSERT INTO accounts(account_number,user_id,product_code,balance,principal,account_type,quote_currency,balance_units,"
                "created_at,resolution_status,deposit_insured,contract_rate) VALUES (?,?,?,?,?,?,?,?,datetime(?,'unixepoch'),'active',1,1)",
                (number, user, code, balance, balance, kind, currency, units, NOW))
        for loan_id, user, code, currency, amount in ((1, "b1", "L1", "NWX", 600), (2, "b2", "LSCD", "SCD", 24000)):
            self.db.accounts.execute(
                "INSERT INTO loans(loan_id,user_id,product_code,principal,outstanding_balance,status,collection_status,created_at,maturity_epoch,"
                "quote_currency) VALUES (?,?,?,?,?,'active','current',datetime(?,'unixepoch'),?,?)",
                (loan_id, user, code, amount, amount, NOW, NOW + 100 * 86400, currency))

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_dir
        self.tempdir.cleanup()

    def risk(self):
        with self.db.readonly() as conn:
            return ALMManager.report(conn, "Bank", now=NOW)["currencyRisk"]

    def test_net_position_per_currency_and_the_open_position_in_base(self):
        risk = self.risk()
        by = {c["currency"]: c for c in risk["currencies"]}
        self.assertEqual([c["currency"] for c in risk["currencies"]], ["NWX", "EUX", "SCD"])      # base first
        self.assertEqual((by["NWX"]["net"], by["NWX"]["isBase"]), (Decimal(200), True))
        self.assertEqual((by["SCD"]["netLoans"], by["SCD"]["deposits"], by["SCD"]["net"], by["SCD"]["netInBase"]),
                         (Decimal(24000), Decimal(12000), Decimal(12000), Decimal(100)))
        self.assertEqual((by["EUX"]["net"], by["EUX"]["netInBase"]), (Decimal(-400), Decimal(-200)))
        self.assertEqual((risk["longPosition"], risk["shortPosition"], risk["netOpenPosition"]), (Decimal(100), Decimal(200), Decimal(200)))
        self.assertAlmostEqual(risk["netOpenPositionPercentOfCet1"], 20.0, places=9)              # 200 / CET1 1000

    def test_shock_scenarios_follow_the_net_and_gross_positions(self):
        scenarios = self.risk()["scenarios"]
        # foreign net = +100 - 200 = -100 base; a 10% fall of the foreign currencies gains 10, a rise loses 10; gross worst case is 10% of 300
        self.assertEqual((scenarios["foreignDown"], scenarios["foreignUp"], scenarios["grossWorstCase"]), (Decimal(10), Decimal(-10), Decimal(-30)))

    def test_foreign_liability_share_and_the_assumed_shock_size(self):
        # liabilities in base: NWX 400, SCD 12000/120 = 100, EUX 400 x 1/2 = 200 -> foreign share = 300 / 700
        self.assertAlmostEqual(self.risk()["foreignLiabilityShare"], 300 * 100 / 700, places=6)
        with self.db.atomic() as conn:
            ALMManager.set_parameter(conn, "낙원", "fx_shock_bps", 2000, actor="admin", now=NOW)
            with self.assertRaisesRegex(ValueError, "invalid_prudential_parameter_value"):
                ALMManager.set_parameter(conn, "낙원", "fx_shock_bps", 10001, actor="admin", now=NOW)
        self.assertEqual(self.risk()["scenarios"]["grossWorstCase"], Decimal(-60))

    def test_wholesale_funding_in_a_foreign_currency_is_a_liability_of_that_currency(self):
        with self.db.atomic() as conn:
            FundingManager.set_limit(conn, "Bank", "interbank", "EUX", 100000, actor="admin", now=NOW)
            FundingManager.draw(conn, "Bank", "interbank", "EUX", 100, reference_key="k1", rate_bps=300, maturity_days=90, now=NOW)
        by = {c["currency"]: c for c in self.risk()["currencies"]}
        self.assertEqual((by["EUX"]["wholesale"], by["EUX"]["net"], by["EUX"]["netInBase"]), (Decimal(100), Decimal(-500), Decimal(-250)))

    def test_rate_shock_converts_negative_foreign_currency_changes(self):
        # regression: a loss in a foreign currency (negative delta NII) used to make the whole report fail in the exchange conversion
        self.db.accounts.execute(
            "UPDATE loans SET rate_type='floating',contract_rate=5,rate_floor=0,rate_cap=NULL,next_repricing_epoch=?,maturity_epoch=? WHERE loan_id=2",
            (NOW, NOW + 400 * 86400))
        with self.db.readonly() as conn:
            rows = {r["basisPoints"]: r for r in ALMManager.report(conn, "Bank", now=NOW)["rateShock"]["shocks"]}
        # 24000 SCD x 1% over a year = 240 SCD = 2 NWX each way
        self.assertEqual((rows[100]["contractualNii"], rows[-100]["contractualNii"]), (Decimal(2), Decimal(-2)))

    def test_a_bank_with_only_base_currency_business_has_no_open_position(self):
        self.db.accounts.execute("DELETE FROM loans WHERE loan_id=2")
        self.db.accounts.execute("DELETE FROM accounts WHERE account_number IN ('A2','A3')")
        risk = self.risk()
        self.assertEqual((risk["netOpenPosition"], risk["scenarios"]["grossWorstCase"], risk["foreignLiabilityShare"]), (0, 0, 0.0))


if __name__ == "__main__":
    unittest.main()
