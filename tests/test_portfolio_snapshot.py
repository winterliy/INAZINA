import unittest
from contextlib import contextmanager
from unittest.mock import patch

import test_inazina as fixtures


class PortfolioSnapshotTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_read_only_portfolio_uses_one_snapshot_and_no_manager_queries(self):
        original_readonly = self.db.readonly
        snapshot_count = 0

        @contextmanager
        def counted_readonly():
            nonlocal snapshot_count
            snapshot_count += 1
            with original_readonly() as connection:
                yield connection

        managers = (
            self.db.users, self.db.assets, self.db.products, self.db.companies,
            self.db.accounts, self.db.system, self.db.currency, self.db.corporate,
            self.db.els,
        )
        patches = []
        for manager in managers:
            patches.extend((
                patch.object(manager, "execute", side_effect=AssertionError("snapshot bypass")),
                patch.object(manager, "execute_one", side_effect=AssertionError("snapshot bypass")),
            ))
        self.db.readonly = counted_readonly
        try:
            for active_patch in patches:
                active_patch.start()
            result = self.bot.user_mgr.get_total_assets("citizen", settle=False)
        finally:
            for active_patch in reversed(patches):
                active_patch.stop()
            self.db.readonly = original_readonly

        self.assertEqual(snapshot_count, 1)
        self.assertEqual(result["cash"], 1_000_000)
        self.assertEqual(result["net_worth"], result["grand_total"] - result["loan_total"])

    def test_foreign_currency_loan_is_read_from_minor_units_and_converted_for_net_worth(self):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places,status) "
            "VALUES ('USD','US Dollar','2',2,'active')"
        )
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code) VALUES ('US','USD')"
        )
        self.db.users.execute(
            "UPDATE users SET nationality='US' WHERE user_id='citizen'"
        )
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,"
            "interest_cycle,min_credit_score,issuer,quote_currency,rate_type) "
            "VALUES ('PORTFOLIO-USD','Portfolio USD',0,1000,365,86400,0,'Bank','USD','fixed')"
        )
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,"
            "created_at,quote_currency) VALUES "
            "('citizen','PORTFOLIO-USD',50.25,50.25,'active',datetime('now'),'USD')"
        )

        result = self.bot.user_mgr.get_total_assets("citizen", settle=False)

        self.assertEqual(result["loan_total"], 50.25)
        self.assertEqual(result["net_worth"], result["grand_total"] - 100)

    def test_totals_can_be_rebuilt_exactly_in_the_home_currency_without_the_whole_units_dropped_in_the_base_currency(self):
        """Cash, home-currency accounts, foreign-currency accounts and loans are each made whole NWX units (the rest is dropped). The
        portfolio also carries those NWX parts and their exact home-currency values, so a screen can swap one for the other."""
        from decimal import Decimal
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places,status) VALUES ('USD','US Dollar','0.7',2,'active')")
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('US','USD')")
        self.db.users.execute("UPDATE users SET nationality='US' WHERE user_id='citizen'")
        self.db.products.execute(
            "INSERT INTO liability(product_code,name,interest_rate,loan_limit,maturity_days,interest_cycle,min_credit_score,issuer,quote_currency,rate_type) "
            "VALUES ('PORTFOLIO-USD','Portfolio USD',0,1000,365,86400,0,'Bank','USD','fixed')")
        self.db.accounts.execute(
            "INSERT INTO loans(user_id,product_code,principal,outstanding_balance,status,created_at,quote_currency) "
            "VALUES ('citizen','PORTFOLIO-USD',100.5,100.5,'active',datetime('now'),'USD')")

        result = self.bot.user_mgr.get_total_assets("citizen", settle=False)

        rate = Decimal(str(result["home_rate_nwx"]))
        cash = Decimal(str(result["cash"]))
        self.assertEqual(Decimal(result["grand_exact_home"]), cash, "only cash: the exact home value is the cash itself")
        self.assertEqual(Decimal(result["loan_exact_home"]), Decimal("100.5"))
        self.assertEqual(result["grand_floor_nwx"], result["cash_value_nwx"] + 0 + 0)
        self.assertEqual(result["loan_floor_nwx"], 70, "100.5 USD is 70.35 NWX; the .35 is what the base-currency figure loses")
        # What a screen does: take the floored parts out of the NWX totals, convert the rest, and add the exact home values.
        grand = (Decimal(str(result["grand_total"])) - result["grand_floor_nwx"]) / rate + Decimal(result["grand_exact_home"])
        net = (Decimal(str(result["net_worth"])) - result["grand_floor_nwx"] + result["loan_floor_nwx"]) / rate + Decimal(result["grand_exact_home"]) - Decimal(result["loan_exact_home"])
        self.assertAlmostEqual(float(grand), float(cash), places=2)
        self.assertAlmostEqual(float(net), float(cash) - 100.5, places=2)


if __name__ == "__main__":
    unittest.main()
