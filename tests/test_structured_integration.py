"""Administrative creation reaches trading, price ticks and wallet valuation."""
import unittest
from unittest.mock import patch

import test_inazina as fixtures
from managers.admin_commands_manager import AdminCommandsManager
from managers.corporate_actions_manager import CorporateActionsManager
from managers.els_manager import ELSManager, DAY


class StructuredIntegrationTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def command(self, name, args=''):
        return AdminCommandsManager(self.bot).execute('admin', name, args)

    def test_preferred_primary_proceeds_claim_wallet_and_deletion_protection(self):
        self.command('상품생성', 'CO company 회사 게임 10000 1000 1000 EX {"dividend_funding":"issuer"}')
        self.command('상품생성', 'PR preferred 우선주 CO 0 100 1000 EX 1분 10 {"dividend_funding":"issuer","cumulative_dividend":1}')
        success, message = self.bot.order_book.place_limit_order('citizen', 'PR', 'buy', 2, 1000)
        self.assertTrue(success, message)
        self.assertEqual(self.db.corporate.execute_one("SELECT balance FROM issuer_cash_accounts WHERE company_code='CO'")[0], 2000)
        self.command('법인잔고수정', 'CO modify 5')
        self.db.companies.execute("UPDATE company SET dividend_next_epoch=100 WHERE company_code='PR'")
        CorporateActionsManager(self.bot).process_due(100)
        wallet = self.bot.user_mgr.get_total_assets('citizen')
        self.assertEqual(wallet['preferred_claim_total'], 15)
        self.assertEqual(wallet['preferred_dividend_claims'][0]['asset_code'], 'PR')
        self.assertEqual(wallet['grand_total'], wallet['cash'] + wallet['stock_total'] + 15)
        for code in ('CO', 'PR'):
            with self.assertRaisesRegex(ValueError, '법인청산'):
                self.command('상품삭제', code)
        self.assertEqual(self.db.companies.get_company('PR')['status'], 'active')
        self.command('상품생성', 'PR2 preferred 우선주2 CO 0 100 900 EX {"liquidation_value_per_share":1000,"dividend_funding":"issuer","cumulative_dividend":1}')
        with self.assertRaisesRegex(ValueError, '가격이 같은'):
            self.command('합병', 'PR PR2')
        self.assertEqual(self.db.companies.get_company('PR2')['status'], 'active')

    def test_els_command_subscription_wallet_and_halted_market_observation(self):
        self.command('상품생성', 'CO company 기초회사 게임 10000 1000 100 EX')
        self.db.companies.execute("UPDATE company SET is_fixed=1 WHERE company_code='CO'")
        self.bot.market_mgr.update_prices(now=999)
        with patch('managers.els_manager.time.time', return_value=1000):
            self.command('상품생성', 'E1 els 연계상품 10000 20 CO,__exchange_index__:EX 12 180 30,60,90 90,85,80 75 60 1 EX')
        with patch('managers.els_manager.time.time', return_value=1001):
            success, message = self.bot.order_book.place_limit_order('citizen', 'E1', 'buy', 2, 10000)
        self.assertTrue(success, message)
        wallet = self.bot.user_mgr.get_total_assets('citizen')
        self.assertEqual((wallet['stock_total'], wallet['els_total']), (0, 20000))
        self.assertEqual(wallet['grand_total'], 1000000)
        els = ELSManager(self.db, self.bot.user_mgr)
        self.assertFalse(self.bot.order_book.place_limit_order('citizen', 'E1', 'sell', 1, 10000)[0])
        with patch('managers.market_session_manager.time.time', return_value=1002):
            self.command('휴장')
        self.bot.market_mgr.update_prices(now=1000+DAY)
        self.assertIsNone(els.get_details('E1')['issue_epoch'])
        with patch('managers.els_manager.time.time', return_value=1003):
            self.assertFalse(self.bot.order_book.place_limit_order('citizen', 'E1', 'buy', 1, 10000)[0])
        with patch('managers.market_session_manager.time.time', return_value=1000+DAY+1):
            self.command('개장')
        self.bot.market_mgr.update_prices(now=1000+DAY+1)
        details = els.get_details('E1')
        self.assertEqual(details['state'], 'active')
        self.assertEqual([item['initial_price'] for item in details['underlyings']], [100, 1000])
        self.bot.market_mgr.update_prices(now=1000+DAY+1+30*DAY)
        self.assertEqual(els.get_details('E1')['state'], 'redemption_pending')
        settlement = els.process_due()
        self.assertEqual(len(settlement), 1)
        self.assertEqual(settlement[0]['reason'], 'els_early')
        wallet = self.bot.user_mgr.get_total_assets('citizen')
        self.assertEqual((wallet['els_total'], wallet['els_positions']), (0, []))
        self.assertEqual(self.db.products.get_investment('E1')['unit_price'], 10000)
        self.assertIsNone(self.db.system.execute_one("SELECT 1 FROM market_simulation_state WHERE asset_code='E1'"))

    def test_referenced_index_survives_company_deletion_liquidation_and_merge(self):
        self.command('상품생성', 'CO company 회사 게임 10000 1000 100 EX')
        self.command('상품생성', 'PR preferred 우선주 CO 0 100 100 EX')
        self.command('상품생성', 'OTHER company 타회사 게임 10000 1000 100 OTHEREX')
        self.bot.market_mgr.update_prices(now=999,volatility=0)
        self.command('상품생성', 'E1 els 지수ELS 1000 20 __exchange_index__:EX 12 180 30 90 75 60 1 EX')
        with self.assertRaisesRegex(ValueError, '기초.*지수'):
            self.command('법인청산', 'CO')
        self.command('상품삭제', 'PR')
        with self.assertRaisesRegex(ValueError, '기초.*지수'):
            self.command('상품삭제', 'CO')
        with self.assertRaisesRegex(ValueError, '기초.*지수'):
            self.command('합병', 'OTHER CO')
        with self.assertRaisesRegex(ValueError, '기초.*지수'):
            self.command('상품수정', 'CO company 회사 게임 10000 1000 100 OTHEREX')
        self.assertEqual(self.db.companies.get_company('CO')['status'], 'active')
        self.command('ELS취소', 'E1')
        self.command('상품삭제', 'CO')
        self.assertEqual(self.db.companies.get_company('CO')['status'], 'inactive')
