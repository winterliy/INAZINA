"""Administrator trading warnings: designation, expiry, order notices and listings."""
import time
import unittest

import test_inazina as fixtures
from managers.admin_commands_manager import AdminCommandsManager
from managers.trading_warning_manager import TradingWarnings


class TradingWarningTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def prepare(self):
        self.db.users.execute("UPDATE users SET nationality='낙원' WHERE user_id='citizen'")
        self.db.assets.register_asset('WARN', '경고주', 'EX', 100, 'stock')
        self.warnings = TradingWarnings(self.db)

    def test_command_designates_clears_and_validates(self):
        self.prepare()
        message = AdminCommandsManager(self.bot).execute('admin', '거래경고', 'WARN 경고 "급등 주의" 3')
        self.assertIn('투자경고', message)
        row = self.warnings.list()[0]
        self.assertEqual((row['asset_code'], row['level'], row['reason']), ('WARN', 'warning', '급등 주의'))
        self.assertAlmostEqual(row['expires_epoch'] - row['created_epoch'], 3 * 86400, delta=1)
        self.assertIn('해제', AdminCommandsManager(self.bot).execute('admin', '거래경고', 'WARN 해제'))
        self.assertEqual(self.warnings.list(), [])
        with self.assertRaisesRegex(ValueError, '존재하지 않는'):
            self.warnings.set('admin', 'NOPE', '주의')
        with self.assertRaisesRegex(ValueError, '단계'):
            self.warnings.set('admin', 'WARN', '최고')
        with self.assertRaises(ValueError):
            self.warnings.set('citizen', 'WARN', '주의')

    def test_orders_show_the_warning_and_expired_warnings_disappear(self):
        self.prepare()
        self.warnings.set('admin', 'WARN', '위험', '관리종목')
        ok, message = self.bot.order_book.place_limit_order('citizen', 'WARN', 'buy', 1, 100)
        self.assertTrue(ok, message)
        self.assertIn('투자위험', message)
        self.assertIn('관리종목', message)
        self.db.system.execute("UPDATE trading_warnings SET expires_epoch=?", (time.time() - 1,))
        ok, message = self.bot.order_book.place_limit_order('citizen', 'WARN', 'buy', 1, 100)
        self.assertNotIn('투자위험', message)

    def test_web_instruments_carry_the_warning(self):
        self.prepare()
        self.warnings.set('admin', 'WARN', '주의')
        from functions.discord_views import InfoPanelView
        InfoPanelView._bot_db, InfoPanelView._warnings_at = self.db, 0.0
        self.assertIn('투자주의', InfoPanelView._dividend_note({'code': 'WARN'}))
        self.assertEqual(InfoPanelView._dividend_note({'code': 'OTHER'}), '')


if __name__ == "__main__":
    unittest.main()
