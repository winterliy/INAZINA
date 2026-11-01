"""A country and its treasury are one account: leftover country-named users fold into the treasury."""
import unittest
from decimal import Decimal

import test_inazina as fixtures
from database.nation_accounts import merge_legacy_nation_users
from database.user_db import TREASURY_USER_ID
from web_api.server import ensure_auth_table


class NationAccountTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def leftover(self, cash=0):
        self.db.users.execute(
            "INSERT INTO users(user_id,nickname,nationality,cash,status,role) VALUES ('낙원','낙원 국가 계정','낙원',?,'inactive','nation')",
            (cash,))
        with self.db.atomic() as conn:
            ensure_auth_table(conn)
            conn.execute("INSERT INTO web_auth_credentials(user_id,salt,password_hash,created_epoch,updated_epoch,auth_version) "
                         "VALUES ('낙원','s','h',0,0,0)")

    def cash(self, user_id):
        return Decimal(str(self.db.users.get_user(user_id)['cash']))

    def exists(self, user_id):
        return self.db.users.execute_one("SELECT 1 FROM users WHERE user_id=?", (user_id,)) is not None

    def test_empty_leftover_is_folded_into_the_treasury(self):
        self.leftover()
        before = self.cash(TREASURY_USER_ID)
        self.assertEqual(merge_legacy_nation_users(self.db), ['낙원'])
        self.assertFalse(self.exists('낙원'))
        self.assertEqual(self.cash(TREASURY_USER_ID), before)
        self.assertIsNone(self.db.users.execute_one("SELECT 1 FROM web_auth_credentials WHERE user_id='낙원'"))

    def test_leftover_with_a_balance_is_left_untouched(self):
        self.leftover(cash=500)
        self.assertEqual(merge_legacy_nation_users(self.db), [])
        self.assertTrue(self.exists('낙원'))

    def test_merge_is_idempotent(self):
        self.leftover()
        merge_legacy_nation_users(self.db)
        self.assertEqual(merge_legacy_nation_users(self.db), [])

    def test_leftover_that_owns_assets_is_left_untouched(self):
        self.leftover()
        self.db.assets.register_asset('NATA', '국가자산', 'EX', 100, 'stock')
        self.db.assets.execute("INSERT INTO user_assets(user_id,asset_code,quantity,average_cost) VALUES ('낙원','NATA',1,1)")
        self.assertEqual(merge_legacy_nation_users(self.db), [])
        self.assertTrue(self.exists('낙원'))


if __name__ == "__main__":
    unittest.main()
