import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web_api import server

import test_inazina as fixtures
from managers.corporate_actions_manager import CorporateActionsManager
from managers.share_restructuring_manager import ShareRestructuringManager


class ShareRestructuringTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def prepare(self):
        self.db.companies.create_company('PAR', '액면회사', '게임', 100000, 100, 1000000, 'admin', 'INZ')
        self.manager = ShareRestructuringManager(self.bot)
        self.actions = CorporateActionsManager(self.bot)

    def record(self, decision='approved'):
        return self.manager.record('admin', 'PAR', 10, decision, '주총 2026-01 의사록', '정관 변경 2026-01', '1000')

    def execute(self, resolution):
        return self.actions.adjust_face_value('admin', '분할', 'PAR', 10, mode='par_split', resolution_id=resolution)

    def test_resolution_does_not_execute_until_administrator_requests_it(self):
        self.prepare()
        resolution = self.record()
        self.assertEqual(self.db.companies.get_company('PAR')['total_shares'], 100)
        self.assertIsNone(self.db.companies.get_company('PAR')['par_value_minor_units'])
        ok, message = self.execute(resolution)
        self.assertTrue(ok, message)
        row = self.db.companies.get_company('PAR')
        self.assertEqual((row['total_shares'], row['capital'], int(row['par_value_minor_units'])), (1000, 100000, 100))
        self.assertTrue(self.manager.history('PAR')[0]['executed'])
        ok, message = self.execute(resolution)
        self.assertFalse(ok)
        self.assertEqual(self.db.companies.get_company('PAR')['total_shares'], 1000)

    def test_rejected_resolution_and_missing_resolution_cannot_execute(self):
        self.prepare()
        resolution = self.record('rejected')
        for value in (resolution, None):
            ok, message = self.execute(value)
            self.assertFalse(ok, message)
        self.assertEqual(self.db.companies.get_company('PAR')['total_shares'], 100)

    def test_changed_capital_invalidates_resolution(self):
        self.prepare()
        resolution = self.record()
        self.db.companies.execute("UPDATE company SET capital=200000 WHERE company_code='PAR'")
        self.assertFalse(self.execute(resolution)[0])
        self.assertFalse(self.manager.history('PAR')[0]['executed'])

    def test_record_requires_admin_documents_and_matching_par_capital(self):
        self.prepare()
        with self.assertRaises(ValueError):
            self.manager.record('citizen', 'PAR', 10, 'approved', '의사록', '정관', '1000')
        for meeting, articles, par in (('', '정관', '1000'), ('의사록', '', '1000'), ('의사록', '정관', '999')):
            with self.assertRaises(ValueError):
                self.manager.record('admin', 'PAR', 10, 'approved', meeting, articles, par)

    def test_execution_failure_does_not_consume_resolution(self):
        self.prepare()
        resolution = self.record()
        self.db.assets.add_user_asset('citizen', 'PAR', 1, unit_cost=1000)
        ok, message = self.bot.order_book.place_limit_order('citizen', 'PAR', 'sell', 1, 1000000)
        self.assertTrue(ok, message)
        self.assertFalse(self.execute(resolution)[0])
        self.assertFalse(self.manager.history('PAR')[0]['executed'])
        self.assertIsNone(self.db.companies.get_company('PAR')['par_value_minor_units'])

    def test_http_actions_record_then_execute_without_automatic_dispatch(self):
        self.prepare()
        bot_domain = SimpleNamespace(bot=self.bot)
        with patch.object(server, 'domain', return_value=bot_domain):
            message = server.admin_company_share_action('admin', 'PAR', {
                'action': 'record_resolution', 'ratio': '10', 'decision': 'approved',
                'meetingReference': '주총 의사록', 'articlesReference': '정관 변경', 'parValue': '1000'})
            self.assertIn('주식 수는 변경되지', message)
            rows = server.admin_company_share_history('PAR')['resolutions']
            self.assertFalse(rows[0]['executed'])
            message = server.admin_company_share_action('admin', 'PAR', {
                'action': 'execute', 'mode': 'par_split', 'ratio': '10', 'resolutionId': rows[0]['resolution_id']})
            self.assertTrue(message.startswith('✅'), message)
            with self.assertRaises(ValueError):
                server.admin_company_share_action('admin', 'PAR', {'action': 'execute'})

    def test_stock_split_requires_formal_mode_after_par_value_is_registered(self):
        self.prepare()
        resolution = self.record()
        self.assertTrue(self.execute(resolution)[0])
        self.assertFalse(self.actions.adjust_face_value('admin', '분할', 'PAR', 2)[0])
        ok, message = self.actions.adjust_face_value('admin', '병합', 'PAR', 10)
        self.assertTrue(ok, message)
        row = self.db.companies.get_company('PAR')
        self.assertEqual((row['total_shares'], int(row['par_value_minor_units'])), (100, 1000))

    def test_general_ratio_split_preserves_exact_basis_and_capital(self):
        from managers.asset_cost_manager import AssetCostManager
        self.prepare()
        self.db.assets.add_user_asset('citizen', 'PAR', 4, unit_cost=7)
        with self.db.atomic() as conn:
            lot = conn.execute("SELECT holding_id FROM user_assets WHERE asset_code='PAR'").fetchone()[0]
            before = AssetCostManager.total_cost_minor(conn, lot)
        ok, message = self.actions.adjust_face_value('admin', '분할', 'PAR', 2,
            mode='ratio_split', old_shares='2', new_shares='3')
        self.assertTrue(ok, message)
        row = self.db.companies.get_company('PAR')
        self.assertEqual((row['total_shares'], row['capital']), (150, 100000))
        with self.db.atomic() as conn:
            self.assertEqual(conn.execute('SELECT quantity FROM user_assets WHERE holding_id=?', (lot,)).fetchone()[0], 6)
            self.assertEqual(AssetCostManager.total_cost_minor(conn, lot), before)
        ok, message = self.actions.adjust_face_value('admin', '병합', 'PAR', 2,
            mode='ratio_split', old_shares='3', new_shares='2')
        self.assertTrue(ok, message)
        self.assertEqual(self.db.companies.get_company('PAR')['total_shares'], 100)
        with self.db.atomic() as conn:
            self.assertEqual(AssetCostManager.total_cost_minor(conn, lot), before)

    def test_general_n_to_one_and_no_fractional_holdings(self):
        self.prepare()
        self.db.assets.add_user_asset('citizen', 'PAR', 3, unit_cost=7)
        ok, message = self.actions.adjust_face_value('admin', '병합', 'PAR', 2,
            mode='ratio_split', old_shares=2, new_shares=1)
        self.assertFalse(ok, message)
        self.assertEqual(self.db.companies.get_company('PAR')['total_shares'], 100)
        self.assertEqual(self.db.assets.execute_one("SELECT quantity FROM user_assets WHERE asset_code='PAR'")[0], 3)
        self.db.assets.execute("UPDATE user_assets SET quantity=4 WHERE asset_code='PAR'")
        ok, message = self.actions.adjust_face_value('admin', '병합', 'PAR', 2,
            mode='ratio_split', old_shares=2, new_shares=1)
        self.assertTrue(ok, message)
        self.assertEqual(self.db.companies.get_company('PAR')['total_shares'], 50)

    def test_general_ratio_validates_issued_listed_and_admin(self):
        self.prepare()
        for actor, old, new in [('citizen', 1, 2), ('admin', 3, 2), ('admin', 0, 2), ('admin', 1, 1), ('admin', '1.5', 2)]:
            ok, message = self.actions.adjust_face_value(actor, '분할', 'PAR', 2,
                mode='ratio_split', old_shares=old, new_shares=new)
            self.assertFalse(ok, (actor, old, new, message))
        self.db.companies.execute("UPDATE company SET listed_shares=99 WHERE company_code='PAR'")
        ok, message = self.actions.adjust_face_value('admin', '분할', 'PAR', 2,
            mode='ratio_split', old_shares=2, new_shares=3)
        self.assertFalse(ok, message)
        self.assertEqual(self.db.companies.get_company('PAR')['total_shares'], 100)

    def test_general_ratio_cannot_bypass_registered_par_approval(self):
        self.prepare()
        self.assertTrue(self.execute(self.record())[0])
        ok, message = self.actions.adjust_face_value('admin', '분할', 'PAR', 2,
            mode='ratio_split', old_shares=1, new_shares=2)
        self.assertFalse(ok, message)
        self.assertEqual(self.db.companies.get_company('PAR')['total_shares'], 1000)
