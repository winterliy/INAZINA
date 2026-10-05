import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from database.user_db import TREASURY_USER_ID
from functions.admin_prefix import ActionModal
from functions.slash_commands import SlashCommandRegistry
import test_inazina as fixtures
from managers.admin_commands_manager import AdminCommandsManager, COMMANDS
from managers.interest_manager import InterestManager


class LoanRateTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def command(self, name, args):
        return AdminCommandsManager(self.bot).execute('admin', name, args)

    def loan(self, original=10, extension=20):
        args = 'LN loan 대출 %s 1000000 2 86400 0 Inazina 0 7' % original
        if extension is not None:
            args += ' %s' % extension
        self.command('상품생성', args)
        self.assertTrue(self.bot.bank_mgr.apply_loan('citizen', 'LN', 365000)[0])
        start = time.time() - 100 * 86400
        self.db.accounts.execute("UPDATE loans SET created_at=datetime(?,'unixepoch'),last_payment_epoch=?,maturity_epoch=?,last_statutory_epoch=?", (start, start, start+2*86400, start))
        return start

    def balance(self):
        return self.db.accounts.execute_one('SELECT outstanding_balance FROM loans')[0]

    def test_creation_optional_rate_and_validated_admin_edit(self):
        self.command('상품생성', 'LN loan 대출 5 1000000 30 86400 0 Inazina')
        self.assertIsNone(self.db.products.get_liability('LN')['extension_interest_rate'])
        self.command('상품이율수정', 'LN 0 0')
        row = self.db.products.get_liability('LN')
        self.assertEqual((row['interest_rate'], row['extension_interest_rate']), (0, 0))
        self.command('상품생성', 'JS loan 대출 5 1000000 30 86400 0 Inazina {"extension_interest_rate":8}')
        self.assertEqual(self.db.products.get_liability('JS')['extension_interest_rate'], 8)
        for args in ('LN -1 8', 'LN 5 101', 'LN nan 8'):
            with self.assertRaises(ValueError):
                self.command('상품이율수정', args)
        with self.assertRaises(ValueError):
            self.command('상품생성', 'BAD loan 대출 5 1000000 30 86400 0 Inazina 0 7 -1')
        with self.assertRaises((ValueError, PermissionError)):
            AdminCommandsManager(self.bot).execute('citizen', '상품이율수정', 'LN 5 8')
        self.assertEqual(self.db.products.get_liability('LN')['interest_rate'], 0)
        self.assertIn('상품이율수정', COMMANDS)

    def test_extension_uses_new_rate_and_extended_maturity(self):
        start = self.loan()
        success, message = self.bot.bank_mgr.extend_loan('citizen', 'LN', now=start+86400)
        self.assertTrue(success, message)
        self.assertIn('20%', message)
        self.assertEqual(self.balance(), 365100)
        manager = InterestManager(self.db)
        manager.process_user_interests('citizen', now=start+2*86400)
        self.assertEqual(self.balance(), 365300)
        # Original maturity was day two; day three still has contractual interest.
        manager.process_user_interests('citizen', now=start+3*86400)
        self.assertEqual(self.balance(), 365500)
        manager.process_user_interests('citizen', now=start+3*86400)
        self.assertEqual(self.balance(), 365500)

    def test_blank_and_zero_extension_follow_original(self):
        start = self.loan(extension=None)
        manager = InterestManager(self.db)
        self.assertTrue(self.bot.bank_mgr.extend_loan('citizen', 'LN', now=start+86400)[0])
        manager.process_user_interests('citizen', now=start+2*86400)
        self.assertEqual(self.balance(), 365200)
        with patch('managers.admin_control_manager.time.time', return_value=start+2*86400):
            self.command('상품이율수정', 'LN 0 0')
        manager.process_user_interests('citizen', now=start+3*86400)
        self.assertEqual(self.balance(), 365200)
        with patch('managers.admin_control_manager.time.time', return_value=start+3*86400):
            self.command('상품이율수정', 'LN 10 0')
        manager.process_user_interests('citizen', now=start+4*86400)
        self.assertEqual(self.balance(), 365300)

    def test_zero_original_can_have_positive_extension_rate(self):
        start = self.loan(original=0, extension=20)
        self.assertTrue(self.bot.bank_mgr.extend_loan('citizen', 'LN', now=start+86400)[0])
        self.assertEqual(self.balance(), 365000)
        InterestManager(self.db).process_user_interests('citizen', now=start+2*86400)
        self.assertEqual(self.balance(), 365200)

    def test_rate_edit_settles_old_rate_then_uses_new_rate(self):
        start = self.loan()
        self.assertTrue(self.bot.bank_mgr.extend_loan('citizen', 'LN', now=start)[0])
        with patch('managers.admin_control_manager.time.time', return_value=start+86400):
            self.command('상품이율수정', 'LN 5 30')
        self.assertEqual(self.balance(), 365200)
        InterestManager(self.db).process_user_interests('citizen', now=start+2*86400)
        self.assertEqual(self.balance(), 365500)

    def test_partial_cycle_at_extension_preserves_old_interest(self):
        start = self.loan(original=10, extension=0)
        self.assertTrue(self.bot.bank_mgr.extend_loan('citizen', 'LN', now=start+43200)[0])
        self.assertEqual(self.balance(), 365050)
        # Turn off future interest at the same instant: accrued original interest stays.
        with patch('managers.admin_control_manager.time.time', return_value=start+43200):
            self.command('상품이율수정', 'LN 0 0')
        InterestManager(self.db).process_user_interests('citizen', now=start+2*86400)
        self.assertEqual(self.balance(), 365050)

    def test_public_and_national_extensions_apply_credit_days_and_rate(self):
        start = self.loan()
        now = start + 86400
        self.db.products.execute("UPDATE liability SET extension_min_credit_score=800 WHERE product_code='LN'")
        self.db.users.execute("UPDATE users SET credit_score=799 WHERE user_id='citizen'")
        registry = SlashCommandRegistry(self.bot)
        registry._prepare = AsyncMock(return_value=('citizen', None, 'user'))
        registry._reply = AsyncMock()
        with patch('managers.bank_manager.time.time', return_value=now):
            asyncio.run(registry.extend_loan(SimpleNamespace(), 'LN'))
        self.assertIn('800점', registry._reply.call_args.kwargs['embed'].description)
        self.assertEqual(self.db.accounts.execute_one("SELECT extension_count FROM loans WHERE user_id='citizen'")[0], 0)
        self.db.users.execute("UPDATE users SET credit_score=800 WHERE user_id='citizen'")
        with patch('managers.bank_manager.time.time', return_value=now):
            asyncio.run(registry.extend_loan(SimpleNamespace(), 'LN'))
        personal = self.db.accounts.execute_one("SELECT * FROM loans WHERE user_id='citizen'")
        self.assertEqual(personal['maturity_epoch'], start+9*86400)
        self.assertEqual(personal['extension_count'], 1)
        self.assertIn('20%', registry._reply.call_args.kwargs['embed'].description)
        self.assertTrue(self.bot.bank_mgr.apply_loan(TREASURY_USER_ID, 'LN', 365000)[0])
        self.db.users.execute("UPDATE users SET credit_score=800 WHERE user_id=?", (TREASURY_USER_ID,))
        self.db.accounts.execute("UPDATE loans SET created_at=datetime(?,'unixepoch'),last_payment_epoch=?,maturity_epoch=?,last_statutory_epoch=? WHERE user_id=?", (start, start, start+2*86400, start, TREASURY_USER_ID))
        async def national():
            return ActionModal(self.bot, 'admin', '대출연장').execute(['LN'])
        with patch('managers.bank_manager.time.time', return_value=now):
            result = asyncio.run(national())
        government = self.db.accounts.execute_one('SELECT * FROM loans WHERE user_id=?', (TREASURY_USER_ID,))
        self.assertIn('20%', result)
        self.assertEqual(government['maturity_epoch'], start+9*86400)
        self.assertEqual(government['extension_count'], 1)
        self.assertEqual(InterestManager.loan_rate(government, self.db.products.get_liability('LN')), 20)
