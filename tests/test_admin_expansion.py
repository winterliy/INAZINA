import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
import test_inazina as fixtures
from managers.admin_commands_manager import AdminCommandsManager, COMMANDS
from managers.interest_manager import InterestManager
from functions.admin_prefix import NATIONAL_COMMANDS, AdminPrefixRegistry, TreasuryAssetsView
from database.user_db import TREASURY_USER_ID


class AdminExpansionTests(unittest.TestCase):
    setUp=fixtures.InazinaTests.setUp
    tearDown=fixtures.InazinaTests.tearDown

    def command(self,cmd,args=''):
        return AdminCommandsManager(self.bot).execute('admin',cmd,args)

    def test_product_full_modification_and_price_name(self):
        self.command('상품생성','NEW company 새회사 게임 1000000 10000 100 INZ 1일 5')
        self.command('상품수정','NEW company 수정회사 금융 2000000 10000 120 INZ 1주 7')
        row=self.db.companies.get_company('NEW')
        self.assertEqual((row['name'],row['industry'],row['capital'],row['share_price']),('수정회사','금융',2000000,120))
        self.assertEqual((row['dividend_cycle_seconds'],row['dividend_per_share']),(604800,7))
        self.command('상품가격수정','NEW add 10')
        self.assertEqual(self.db.companies.get_company('NEW')['share_price'],130)
        with self.assertRaises(ValueError):self.command('상품수정','NEW modify 200')
        with self.assertRaises(ValueError):self.command('상품수정','NEW saving 저축 3 1 30 86400 1 Inazina')
        with self.assertRaises(ValueError):self.command('상품수정','NEW company 회사 금융 1 10000 130 INZ -1 5')
        self.assertEqual(self.db.companies.get_company('NEW')['dividend_per_share'],7)

    def test_preferred_creation_and_loan_extension_fields(self):
        self.command('상품생성','P company 법인 게임 1000000 10000 100 INZ')
        self.command('상품생성','PR preferred 우선주 P 100000 1000 90 INZ 1일 3')
        row=self.db.companies.get_company('PR')
        self.assertEqual((row['share_class'],row['parent_company_code'],row['dividend_per_share']),('preferred','P',3))
        with self.assertRaises(ValueError):self.command('상품생성','BAD preferred 우선주 NONE 1 10 1 INZ')
        self.command('상품생성','LN loan 대출 0 10000 30 86400 500 Inazina 700 7')
        product=self.db.products.get_liability('LN')
        self.assertEqual((product['extension_min_credit_score'],product['extension_days']),(700,7))
        self.assertTrue(self.bot.bank_mgr.apply_loan('citizen','LN',1000)[0])
        loan=self.db.accounts.execute_one("SELECT * FROM loans WHERE user_id='citizen'")
        with patch.object(self.bot.bank_mgr,'_settle_user_interest'):
            result=self.bot.bank_mgr.extend_loan('citizen','LN')
            self.assertTrue(result[0],result[1])
            self.assertFalse(self.bot.bank_mgr.extend_loan('citizen',loan['loan_id'])[0])
            self.assertFalse(self.bot.bank_mgr.extend_loan('admin',loan['loan_id'])[0])
        after=self.db.accounts.execute_one('SELECT * FROM loans WHERE loan_id=?',(loan['loan_id'],))
        self.assertAlmostEqual(after['maturity_epoch']-loan['maturity_epoch'],7*86400,delta=1)
        self.assertEqual(after['extension_count'],1)

    def test_dividend_creation_and_scheduled_payment_once(self):
        from managers.corporate_actions_manager import CorporateActionsManager
        listings = (
            ('ST', 'company 회사 게임 1000000 10000 100 INZ', 'company', 'company_code'),
            ('PR', 'preferred 우선주 ST 100000 1000 90 INZ', 'company', 'company_code'),
            ('AS', 'asset 현물 commodity_spot 100 1000 COM', 'market_assets', 'code'),
            ('ET', 'etf ETF 100 1000 INZ 1 86400 INZ', 'investment', 'product_code'),
            ('FD', 'fund 펀드 100 1000 ST:1 INZ', 'investment', 'product_code'),
            ('IF', 'indexfund 인덱스펀드 100 1000 INZ INZ', 'investment', 'product_code'),
            ('CM', 'commodity 원자재 100 1000 COM', 'investment', 'product_code'),
        )
        now = 2000000000
        with patch('managers.admin_control_manager.time.time', return_value=now):
            for code, args, table, key in listings:
                with self.subTest(code=code):
                    self.command('상품생성', f'{code} {args} 1일 5')
                    row = self.db.system.execute_one(f'SELECT * FROM {table} WHERE {key}=?', (code,))
                    self.assertEqual((row['dividend_cycle_seconds'], row['dividend_per_share'], row['dividend_next_epoch']), (86400, 5, now+86400))
                    self.assertEqual(row['dividend_is_active'], 1)
                    self.db.assets.add_user_asset('citizen', code, 2)
        with self.assertRaisesRegex(ValueError, '정기배당'):
            self.command('상품생성', 'NO future 선물 100 1000 ST 30 1 0.2 0.15 FUT {"dividend_cycle_seconds":86400,"dividend_per_share":5}')
        self.assertIsNone(self.db.products.get_investment('NO'))
        self.command('세율', '10')
        manager = CorporateActionsManager(self.bot)
        self.assertEqual(manager.process_due(now=now+86399), [])
        cash = self.db.users.get_user('citizen')['cash']
        treasury = self.db.users.get_user(TREASURY_USER_ID)['cash']
        results = manager.process_due(now=now+86400)
        self.assertEqual(len(results), len(listings))
        self.assertTrue(all(success and gross==10 for _,success,gross in results), results)
        self.assertEqual(self.db.users.get_user('citizen')['cash']-cash, 9*len(listings))
        self.assertEqual(treasury-self.db.users.get_user(TREASURY_USER_ID)['cash'], 9*len(listings))
        self.assertEqual(manager.process_due(now=now+86400), [])
        self.assertEqual(self.db.assets.execute_one('SELECT COUNT(*) FROM dividend_payments')[0], len(listings))
        self.command('정기배당수정', 'ST 0 5')
        self.assertIsNone(self.db.companies.get_company('ST')['dividend_next_epoch'])

    def test_dividend_switch_preserves_terms_and_restarts_without_arrears(self):
        from managers.corporate_actions_manager import CorporateActionsManager
        manager = CorporateActionsManager(self.bot)
        start = 2000000000
        listings = (
            ('ST', 'company 회사 게임 1000000 10000 100 INZ', 'company', 'company_code'),
            ('AS', 'asset 현물 commodity_spot 100 1000 COM', 'market_assets', 'code'),
            ('CM', 'commodity 원자재 100 1000 COM', 'investment', 'product_code'),
        )
        with patch('managers.admin_control_manager.time.time', return_value=start):
            for code, args, table, key in listings:
                self.command('상품생성', f'{code} {args} 1일 5 {{"dividend_is_active":0}}')
                self.db.assets.add_user_asset('citizen', code, 2)
                row = self.db.system.execute_one(f'SELECT * FROM {table} WHERE {key}=?', (code,))
                self.assertEqual((row['dividend_is_active'],row['dividend_next_epoch']), (0,None))
        resumed = start + 10*86400
        self.assertEqual(manager.process_due(now=resumed), [])
        with self.assertRaises(ValueError):self.command('정기배당활성', 'ST maybe')
        with self.assertRaises(ValueError):AdminCommandsManager(self.bot).execute('citizen', '정기배당활성', 'ST on')
        with self.assertRaises(ValueError):self.command('상품수정', 'ST company {"dividend_is_active":2}')
        with patch('managers.corporate_actions_manager.time.time', return_value=resumed):
            self.command('정기배당수정', 'ST 1일 7')
            self.assertIsNone(self.db.companies.get_company('ST')['dividend_next_epoch'])
            self.command('특별배당실시', 'ST 3')
            self.command('정기배당활성', 'ST ON')
        due = resumed + 86400
        with patch('managers.corporate_actions_manager.time.time', return_value=resumed+10):
            self.command('정기배당활성', 'ST 1')
        self.assertEqual(self.db.companies.get_company('ST')['dividend_next_epoch'], due)
        self.assertEqual(manager.process_due(now=due-1), [])
        self.assertEqual(manager.process_due(now=due), [('ST', True, 14)])
        self.command('정기배당활성', 'ST off')
        row = self.db.companies.get_company('ST')
        self.assertEqual((row['dividend_cycle_seconds'],row['dividend_per_share'],row['dividend_is_active'],row['dividend_next_epoch']), (86400,7,0,None))
        self.assertEqual(manager.process_due(now=due+100*86400), [])
        # Direct DB Browser edits use the same switch without paying paused cycles.
        for code, args, table, key in listings:
            self.db.system.execute(f'UPDATE {table} SET dividend_next_epoch=?,dividend_is_active=1 WHERE {key}=?', (start,code))
            self.assertIsNone(self.db.system.execute_one(f'SELECT dividend_next_epoch FROM {table} WHERE {key}=?', (code,))[0])
        later = due + 100*86400
        self.assertEqual(manager.process_due(now=later), [])
        for code, args, table, key in listings:
            self.assertEqual(self.db.system.execute_one(f'SELECT dividend_next_epoch FROM {table} WHERE {key}=?', (code,))[0], later+86400)
        self.assertEqual(len(manager.process_due(now=later+86400)), 3)
        self.assertEqual(manager.process_due(now=later+86400), [])
        with patch('managers.admin_control_manager.time.time', return_value=later+86400):
            self.command('상품수정', 'CM commodity {"dividend_is_active":0}')
            self.command('상품수정', 'CM commodity {"dividend_is_active":1}')
        self.assertEqual(self.db.products.get_investment('CM')['dividend_next_epoch'], later+2*86400)

    def test_percent_dividend_uses_payment_price_and_one_mode(self):
        from managers.corporate_actions_manager import CorporateActionsManager
        manager = CorporateActionsManager(self.bot)
        start = 2000000000
        listings = (
            ('ST', 'company 회사 게임 1000000 10000 100 INZ', 'company', 'company_code', 'share_price'),
            ('AS', 'asset 현물 commodity_spot 100 1000 COM', 'market_assets', 'code', 'price'),
            ('CM', 'commodity 원자재 100 1000 COM', 'investment', 'product_code', 'unit_price'),
        )
        self.command('세율', '0')
        with patch('managers.admin_control_manager.time.time', return_value=start):
            for code, args, table, key, price in listings:
                self.command('상품생성', f'{code} {args} 1일 0.5%')
                row = self.db.system.execute_one(f'SELECT * FROM {table} WHERE {key}=?', (code,))
                self.assertEqual((row['dividend_mode'],row['dividend_percent'],row['dividend_per_share'],row['dividend_next_epoch']), ('percent',0.5,0,start+86400))
                self.db.assets.add_user_asset('citizen',code,3)
                self.db.assets.add_user_asset('admin',code,5)
                self.db.system.execute(f'UPDATE {table} SET {price}=105 WHERE {key}=?', (code,))
        citizen_cash = self.db.users.get_user('citizen')['cash']
        admin_cash = self.db.users.get_user('admin')['cash']
        treasury = self.db.users.get_user(TREASURY_USER_ID)['cash']
        self.assertEqual(manager.process_due(now=start+86399), [])
        results = manager.process_due(now=start+86400)
        self.assertEqual(len(results),3)
        self.assertTrue(all(success and gross==5 for _,success,gross in results),results)
        self.assertEqual(self.db.users.get_user('citizen')['cash']-citizen_cash,6)
        self.assertEqual(self.db.users.get_user('admin')['cash']-admin_cash,9)
        self.assertEqual(treasury-self.db.users.get_user(TREASURY_USER_ID)['cash'],15)
        for row in self.db.assets.execute('SELECT * FROM dividend_distributions'):
            self.assertEqual((row['dividend_mode'],row['reference_price'],row['dividend_percent'],row['per_share']), ('percent',105,0.5,0))
        self.assertEqual(manager.process_due(now=start+86400), [])
        for value in ('101%','-1%','NaN%','Infinity%','oops%','1.5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.command('정기배당수정',f'ST 1일 {value}')
        with self.assertRaises(ValueError):
            self.command('상품수정','ST company {"dividend_mode":"percent","dividend_percent":5,"dividend_per_share":7}')
        with patch('managers.corporate_actions_manager.time.time', return_value=start+86400):
            self.command('정기배당활성','ST off')
            self.command('정기배당수정','ST 1주 2%')
            self.assertIsNone(self.db.companies.get_company('ST')['dividend_next_epoch'])
            self.command('정기배당활성','ST on')
            self.assertEqual(self.db.companies.get_company('ST')['dividend_next_epoch'],start+8*86400)
            self.command('정기배당수정','ST 1주 7')
            row = self.db.companies.get_company('ST')
            self.assertEqual((row['dividend_mode'],row['dividend_percent'],row['dividend_per_share']),('fixed',0,7))
        self.command('상품수정','ST company {"dividend_percent":3}')
        row = self.db.companies.get_company('ST')
        self.assertEqual((row['dividend_mode'],row['dividend_percent'],row['dividend_per_share']),('percent',3,0))
        self.command('상품수정','ST company {"dividend_per_share":9}')
        row = self.db.companies.get_company('ST')
        self.assertEqual((row['dividend_mode'],row['dividend_percent'],row['dividend_per_share']),('fixed',0,9))

    def test_extension_credit_and_no_post_maturity_statutory_interest(self):
        self.command('상품생성','LN loan 대출 0 10000 1 86400 0 Inazina 800 7')
        self.assertTrue(self.bot.bank_mgr.apply_loan('citizen','LN',1000)[0])
        self.db.users.execute("UPDATE users SET credit_score=799 WHERE user_id='citizen'")
        with patch.object(self.bot.bank_mgr,'_settle_user_interest'):
            self.assertFalse(self.bot.bank_mgr.extend_loan('citizen','LN')[0])
        now=time.time()
        self.db.users.execute("UPDATE users SET credit_score=800 WHERE user_id='citizen'")
        self.db.accounts.execute("UPDATE loans SET created_at=datetime(?,'unixepoch'),maturity_epoch=?,last_payment_epoch=?",(now-2*86400,now-86400,now))
        with patch.object(self.bot.bank_mgr,'_settle_user_interest'):
            self.assertTrue(self.bot.bank_mgr.extend_loan('citizen','LN',now=now)[0])
            self.assertFalse(self.bot.bank_mgr.extend_loan('citizen','LN',now=now+1)[0])
        InterestManager(self.db).process_user_interests('citizen',now=now+86400)
        self.assertEqual(self.db.accounts.execute_one('SELECT outstanding_balance FROM loans')[0],1000)

    def test_private_wallet_main_and_removed_command(self):
        self.assertNotIn('내옵션',NATIONAL_COMMANDS)
        self.assertNotIn('내자산',NATIONAL_COMMANDS)
        self.assertIn('내지갑',NATIONAL_COMMANDS)
        info=self.bot.user_mgr.get_total_assets(TREASURY_USER_ID)
        info['accounts']=[{'product_name':'통장','number':'SECRET-NUMBER','balance':100}]
        async def run():
            view=TreasuryAssetsView(self.bot,'admin',info)
            embed=view.build_embed()
            self.assertNotIn('SECRET-NUMBER',str(embed.to_dict()))
            self.assertIn('총자산',[field.name for field in embed.fields])
            view.category='accounts'
            self.assertIn('SECRET-NUMBER',str(view.build_embed().to_dict()))
            view.stop()
            message=SimpleNamespace(content='$내옵션',author=SimpleNamespace(id='citizen',bot=False,send=AsyncMock()),delete=AsyncMock(),guild=None)
            await AdminPrefixRegistry(self.bot).handle(message)
            message.author.send.assert_not_called()
            message.delete.assert_not_called()
        asyncio.run(run())

    def test_prefix_wallet_reuses_pending_dm_instead_of_sending_twice(self):
        async def run():
            session=SimpleNamespace(edit=AsyncMock())
            author=SimpleNamespace(id='admin',bot=False,send=AsyncMock(return_value=session))
            message=SimpleNamespace(content='$내지갑',author=author,guild=None,delete=AsyncMock())
            await AdminPrefixRegistry(self.bot).handle(message)
            author.send.assert_awaited_once()
            session.edit.assert_awaited_once()
            self.assertIsInstance(session.edit.call_args.kwargs['view'],TreasuryAssetsView)
            self.assertIn('메인',session.edit.call_args.kwargs['embed'].title)
            message.delete.assert_not_awaited()
        asyncio.run(run())

    def test_halt_commands_and_corporate_registration(self):
        self.assertTrue({'증자','정기배당수정','특별배당실시','합병','휴장','개장','서킷브레이커','사이드카'} <= COMMANDS)
        self.command('휴장')
        self.assertEqual(self.db.system.execute_one('SELECT status FROM market_session')[0],'closed')
        self.command('개장')
        self.command('장일시정지')
        self.command('장재개')
        self.command('서킷브레이커','1단계')
        self.assertEqual(self.db.system.execute_one('SELECT circuit_stage FROM market_session')[0],1)
        self.command('개장')
        self.command('사이드카')
        state=self.db.system.execute_one('SELECT * FROM market_session')
        self.assertEqual(state['resume_epoch']-state['halt_started_epoch'],300)


if __name__=='__main__':unittest.main()
