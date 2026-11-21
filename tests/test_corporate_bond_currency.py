"""Corporate bond coupons and redemptions are paid in the bond's currency, exchanged for other holders."""
import time
import unittest

from tests import test_market_features as fixtures


class CorporateBondCurrencyTests(unittest.TestCase):
    setUp = fixtures.MarketFeatureTests.setUp
    tearDown = fixtures.MarketFeatureTests.tearDown
    issue = fixtures.MarketFeatureTests.issue

    def _foreign_holder(self, code, quantity=10):
        self.db.currency.execute(
            "INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('SCD','남방','1/120',2)")
        self.db.currency.execute(
            "INSERT INTO country_currencies(nationality,currency_code,tax_percent) VALUES ('United Southerns','SCD',10)")
        self.db.users.create_user('scd', '남방인', 'United Southerns', 0)
        self.db.assets.add_user_asset('scd', code, quantity, unit_cost=100)

    def _cash_minor(self, user):
        return int(self.db.users.execute_one("SELECT cash_minor FROM users WHERE user_id=?", (user,))[0])

    def _fx(self):
        return [dict(row) for row in self.db.currency.execute(
            "SELECT * FROM cash_fx_settlements ORDER BY kind,reference")]

    def test_coupon_to_a_foreign_home_holder_is_exchanged_and_recorded(self):
        self.issue("CB-FX", "corporate_bond", rate=36.5)
        self._foreign_holder("CB-FX")
        self.assertTrue(self.book.place_limit_order("alice", "CB-FX", "buy", 10, 100)[0])
        now = int(time.time())
        with self.db.atomic() as conn:
            conn.execute("UPDATE investment SET coupon_frequency_days=5,last_coupon_epoch=?,"
                         "next_coupon_epoch=? WHERE product_code='CB-FX'", (now - 5 * 86400, now))
        alice_before = self.db.users.get_user("alice")["cash"]
        paid = {row["user_id"]: row for row in self.products.pay_corporate_bond_coupons(now=now)}
        self.assertEqual(set(paid), {"alice", "scd"})
        coupon, tax = int(paid["scd"]["coupon"]), int(paid["scd"]["tax"])
        self.assertGreater(coupon, 0)
        # The base-currency holder is paid the same number as before.
        self.assertEqual(self.db.users.get_user("alice")["cash"] - alice_before,
                         int(paid["alice"]["coupon"]) - int(paid["alice"]["tax"]))
        fx = self._fx()
        self.assertEqual(len(fx), 1, fx)
        row = fx[0]
        self.assertEqual((row["kind"], row["user_id"], row["source_currency"], row["source_minor_units"],
                          row["currency"], row["decimal_places"]),
                         ("corporate_bond_coupon", "scd", "NWX", coupon - tax, "SCD", 2))
        received = self._cash_minor("scd")
        self.assertEqual(received, row["minor_units"])
        self.assertEqual(received, (coupon - tax) * int(row["rate_numerator"]) * 100 // int(row["rate_denominator"]))
        self.assertNotEqual(received, (coupon - tax) * 100)
        tax_row = self.db.tax.execute_one(
            "SELECT currency_code,tax_amount_minor_units FROM tax_transactions WHERE user_id='scd'")
        self.assertEqual(tuple(tax_row), ("NWX", tax))
        # A second run pays and records nothing more.
        self.assertEqual(self.products.pay_corporate_bond_coupons(now=now), [])
        self.assertEqual(len(self._fx()), 1)

    def test_redemption_to_a_foreign_home_holder_is_exchanged_and_recorded(self):
        self.issue("CB-FXR", "corporate_bond", rate=36.5, days=10)
        self._foreign_holder("CB-FXR", quantity=3)
        with self.db.atomic() as conn:
            conn.execute("UPDATE investment SET maturity_epoch=? WHERE product_code='CB-FXR'",
                         (int(time.time()) - 1,))
        notices = self.products.redeem_corporate_bonds()
        mine = [n for n in notices if n["user_id"] == "scd"]
        self.assertEqual(len(mine), 1, notices)
        fx = [row for row in self._fx() if row["kind"] == "corporate_bond_redemption"]
        self.assertEqual(len(fx), 1, self._fx())
        self.assertEqual(fx[0]["source_minor_units"], int(mine[0]["net"]))
        self.assertEqual(self._cash_minor("scd"), fx[0]["minor_units"])
        self.assertNotEqual(self._cash_minor("scd"), int(mine[0]["net"]) * 100)


if __name__ == "__main__":
    unittest.main()

    def test_a_bond_held_across_a_nationality_change_is_paid_in_the_new_home_currency_and_recorded(self):
        """Corporate bonds are not liquidated by a nationality change; what they pay afterwards is exchanged and recorded."""
        from types import SimpleNamespace
        from managers.nationality_manager import NationalityManager
        self.issue("CB-NAT", "corporate_bond", rate=36.5)
        self._foreign_holder("CB-NAT", quantity=1)            # also creates SCD and its nationality
        self.assertTrue(self.book.place_limit_order("alice", "CB-NAT", "buy", 10, 100)[0])
        audit = SimpleNamespace(_audit=lambda *args, **kwargs: None)
        bot = SimpleNamespace(db=self.db, user_mgr=self.users, order_book=self.book, bank_mgr=None, admin_control=audit)
        with self.db.atomic() as conn:
            conn.execute("INSERT OR IGNORE INTO country_currencies(nationality,currency_code) VALUES ('United Southerns','SCD')")
            from managers.currency_manager import CurrencyManager
            CurrencyManager.treasury_id(conn, 'United Southerns')
        NationalityManager(bot).change("admin", "alice", "United Southerns")
        kept = self.db.assets.execute_one("SELECT SUM(quantity) FROM user_assets WHERE user_id='alice' AND asset_code='CB-NAT'")[0]
        self.assertEqual(int(kept), 10)
        now = int(time.time())
        with self.db.atomic() as conn:
            conn.execute("UPDATE investment SET coupon_frequency_days=5,last_coupon_epoch=?,"
                         "next_coupon_epoch=? WHERE product_code='CB-NAT'", (now - 5 * 86400, now))
        paid = {row["user_id"]: row for row in self.products.pay_corporate_bond_coupons(now=now)}
        coupon, tax = int(paid["alice"]["coupon"]), int(paid["alice"]["tax"])
        rows = [row for row in self._fx() if row["user_id"] == "alice"]
        self.assertEqual([(r["kind"], r["source_currency"], r["source_minor_units"], r["currency"]) for r in rows],
                         [("corporate_bond_coupon", "NWX", coupon - tax, "SCD")])
