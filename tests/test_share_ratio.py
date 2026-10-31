import unittest
import test_inazina as fixtures
from managers.corporate_actions_manager import CorporateActionsManager


class ShareRatioTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def prepare(self, quantity=4, cost=6):
        self.db.companies.create_company('RATIO', '비율회사', '게임', 100000, 100, 1000000, 'admin', 'INZ')
        self.db.assets.add_user_asset('citizen', 'RATIO', quantity, unit_cost=cost)
        self.actions = CorporateActionsManager(self.bot)

    def change(self, old, new, actor='admin'):
        return self.actions.adjust_face_value(actor, '분할', 'RATIO', 2,
            mode='ratio_split', old_shares=old, new_shares=new)

    def test_rational_conversion_and_reverse_preserve_capital_and_basis(self):
        self.prepare()
        ok, message = self.change(2, 3)
        self.assertTrue(ok, message)
        row = self.db.companies.get_company('RATIO')
        self.assertEqual((row['total_shares'], row['capital']), (150, 100000))
        lot = self.db.assets.execute_one("SELECT quantity,average_cost_minor_units FROM user_assets WHERE asset_code='RATIO'")
        self.assertEqual(tuple(lot), (6, 4))
        self.assertTrue(self.change(3, 2)[0])
        self.assertEqual(self.db.companies.get_company('RATIO')['total_shares'], 100)

    def test_fractional_holding_is_rejected_atomically(self):
        self.prepare(quantity=3)
        self.assertFalse(self.change(2, 1)[0])
        self.assertEqual(self.db.companies.get_company('RATIO')['total_shares'], 100)
        self.assertEqual(self.db.assets.execute_one("SELECT quantity FROM user_assets WHERE asset_code='RATIO'")[0], 3)

    def test_issued_listed_admin_and_input_validation(self):
        self.prepare()
        for old, new, actor in [(3, 2, 'admin'), (0, 2, 'admin'), (1, 1, 'admin'), ('1.5', 2, 'admin'), (1, 2, 'citizen')]:
            self.assertFalse(self.change(old, new, actor)[0])
        self.db.companies.execute("UPDATE company SET listed_shares=99 WHERE company_code='RATIO'")
        self.assertFalse(self.change(2, 3)[0])

    def test_nonrepresentable_basis_without_exact_ledger_fails(self):
        self.prepare(cost=7)
        with self.db.atomic() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_cost_basis'").fetchone()
        if exists:
            self.skipTest('Exact cost ledger supports fractional unit basis')
        self.assertFalse(self.change(2, 3)[0])
        self.assertEqual(self.db.companies.get_company('RATIO')['total_shares'], 100)

    def test_open_order_blocks_adjustment(self):
        self.prepare()
        ok, message = self.bot.order_book.place_limit_order('citizen', 'RATIO', 'sell', 1, 1000000)
        self.assertTrue(ok, message)
        ok, message = self.change(1, 2)
        self.assertFalse(ok, message)
        self.assertEqual(self.db.companies.get_company('RATIO')['total_shares'], 100)
