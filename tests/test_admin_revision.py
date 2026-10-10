import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import test_inazina as fixtures
from managers.admin_commands_manager import AdminCommandsManager,COMMANDS
from managers.lottery_manager import LotteryManager
from functions.admin_prefix import AdminPrefixRegistry
from functions.slash_commands import SlashCommandRegistry
from database.user_db import TREASURY_USER_ID

class AdminRevisionTests(unittest.TestCase):
    setUp=fixtures.InazinaTests.setUp
    tearDown=fixtures.InazinaTests.tearDown

    def run_command(self,command,args=''):
        self.bot.lottery_mgr=LotteryManager(self.db)
        return AdminCommandsManager(self.bot).execute('admin',command,args)

    def test_create_modify_delete_and_rollback(self):
        self.run_command('상품생성','NEW stock name="새 주식" price=1000 supply=1000 exchange_id=TEST industry=게임 capital=1000000')
        self.run_command('상품가격수정','NEW add 200')
        self.assertEqual(self.db.companies.get_company('NEW')['share_price'],1200)
        self.run_command('상품가격수정','NEW del 100')
        self.run_command('상품가격수정','NEW modify 900')
        with self.assertRaises(ValueError):self.run_command('상품가격수정','NEW del 1000')
        self.assertEqual(self.db.companies.get_company('NEW')['share_price'],900)
        self.run_command('상품삭제','NEW')
        self.assertEqual(self.db.companies.get_company('NEW')['status'],'inactive')
        with self.assertRaises(ValueError):self.run_command('상품생성','INVALID stock name=테스트 price=-1 supply=100')
        self.assertIsNone(self.db.companies.get_company('INVALID'))

    def test_typed_creation_uses_json_and_protects_existing_claims(self):
        self.run_command('상품생성','SV saving '+json.dumps({'name':'새 적금','interest_rate':4,'deposit_amount':100,'maturity_days':30,'interest_cycle':86400,'compound_type':1,'issuer':'국가'}))
        self.assertEqual(float(self.db.products.get_saving('SV')['interest_rate']),4)
        with self.assertRaises(ValueError):self.run_command('상품가격수정','SV modify 10')
        self.run_command('상품생성','ST stock name=기초 price=1000 supply=10000 exchange_id=TEST capital=10000000')
        self.db.assets.add_user_asset(TREASURY_USER_ID,'ST',100)
        self.run_command('상품생성','OP option underlying_code=ST option_type=call strike_price=1100 maturity_days=30 quantity=1 limit_price=100 volatility_percent=50')
        self.assertIsNotNone(self.db.assets.execute_one("SELECT * FROM option_contracts WHERE contract_code='OP'"))
        self.assertEqual(self.db.assets.execute_one("SELECT asset_code FROM market_orders WHERE order_kind='option'")[0],'OP')
        self.run_command('상품가격수정','OP modify 150')
        self.assertEqual(self.db.assets.execute_one("SELECT price FROM market_quote_overrides WHERE asset_code='OP'")[0],150)
        with self.assertRaises(ValueError):self.run_command('상품삭제','ST')

    def test_balances_wallet_bank_fx_and_nonadmin(self):
        self.run_command('잔고수정','citizen add 50')
        self.run_command('잔고수정','citizen del 20')
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1000030)
        self.run_command('잔고수정','citizen modify 100')
        with self.assertRaises(ValueError):self.run_command('잔고수정','citizen del 101')
        self.assertEqual(self.db.users.get_user('citizen')['cash'],100)
        self.db.products.register_saving('SV','저축',0,1,30,86400,1,'국가')
        self.bot.bank_mgr.open_account('citizen','SV','1234',10)
        number=self.db.accounts.execute_one("SELECT account_number FROM accounts WHERE user_id='citizen'")[0]
        self.run_command('잔고수정',number+' modify 20')
        row=self.db.accounts.execute_one('SELECT balance,principal FROM accounts WHERE account_number=?',(number,))
        self.assertEqual(tuple(row),(20,20))
        fixtures.InazinaTests.seed(self)
        fx=self.bot.fx_mgr.open_account('citizen','INZFX001')
        self.run_command('잔고수정',fx+' modify 1.25')
        self.run_command('잔고수정',fx+' add 0.5')
        self.assertEqual(self.db.fx.execute_one('SELECT balance_units FROM fx_accounts WHERE account_number=?',(fx,))[0],1750000)
        with self.assertRaises(ValueError):AdminCommandsManager(self.bot).execute('citizen','세율','5')

    def test_rates_settings_and_cross_currency(self):
        self.run_command('금리수정','add 1')
        self.run_command('금리수정','del 0.5')
        self.assertEqual(self.bot.rate_mgr.get_rate('base_rate'),3.5)
        self.run_command('법정이자수정','modify 6')
        self.assertEqual(self.bot.rate_mgr.get_rate('statutory_rate'),6)
        self.assertEqual(self.db.system.get_setting('base_rate'),'3.5')
        self.run_command('도박확률','25');self.run_command('도박리턴값','200');self.run_command('세율','5')
        self.assertEqual(self.db.system.get_setting('gamble_chance'),'25')
        self.assertEqual(self.db.pot.get_return_percent(),200)
        self.assertEqual(self.db.system.get_setting('gamble_return'),'200')
        self.assertEqual(self.db.system.get_setting('tax_percent'),'5')
        self.run_command('시장분위기','bull')
        self.assertEqual(self.db.system.get_setting('market_condition'),'bull')
        fixtures.InazinaTests.seed(self)
        self.run_command('상품분위기','FX_USD bear')
        self.assertEqual(self.db.assets.get_asset_info('FX_USD')['market_condition'],'bear')
        self.db.assets.update_price('FX_EUR',2000)
        self.run_command('환율수정','USD EUR 0.9')
        self.assertEqual(self.db.assets.get_asset_info('FX_USD')['price'],1800)
        self.assertEqual(self.db.assets.get_asset_info('FX_EUR')['price'],2000)
        with self.assertRaises(ValueError):self.run_command('환율수정','USD EUR nan')

    def test_lottery_manual_numbers_private_until_draw_and_paid_once(self):
        self.bot.lottery_mgr=LotteryManager(self.db)
        now=int(time.time())
        current=self.bot.lottery_mgr.current(now)
        self.run_command('복권당첨번호변경','1 2 3 4 5 6 7')
        public=self.bot.lottery_mgr.current(now)
        self.assertEqual(public['winning'],[])
        self.assertNotIn('admin_numbers_json',public)
        self.assertFalse(public['admin_override'])
        self.assertIn('보너스 7',self.run_command('복권당첨번호보기'))
        self.bot.lottery_mgr.purchase('citizen',1,'1 2 3 4 5 6','manual-test',now)
        self.bot.lottery_mgr.process_due(current['draw_epoch'])
        round=self.db.lottery.execute_one('SELECT * FROM lottery_rounds WHERE round_id=?',(current['round_id'],))
        self.assertEqual(json.loads(round['winning_json']),[1,2,3,4,5,6])
        self.assertEqual(round['bonus'],7)
        self.assertEqual(self.db.lottery.execute_one('SELECT COUNT(*) FROM lottery_payouts')[0],1)
        self.bot.lottery_mgr.process_due(current['draw_epoch'])
        self.assertEqual(self.db.lottery.execute_one('SELECT COUNT(*) FROM lottery_payouts')[0],1)
        from functions.lottery_views import result_embed
        self.assertIn('관리자',result_embed(self.bot.lottery_mgr._public(round)).footer.text)
        with self.assertRaises(ValueError):self.run_command('복권당첨번호변경','1 1 2 3 4 5')
        self.run_command('복권가격설정','2000')
        self.assertEqual(json.loads(self.db.system.get_setting('lottery_config'))['ticket_price'],2000)

    def test_prefix_authorization_argument_dispatch_old_commands_and_help(self):
        async def run():
            registry=AdminPrefixRegistry(self.bot)
            def message(content,identity='admin'):
                session=SimpleNamespace(edit=AsyncMock())
                return SimpleNamespace(author=SimpleNamespace(id=identity,bot=False,send=AsyncMock(return_value=session)),content=content,delete=AsyncMock(),channel=SimpleNamespace(send=AsyncMock()),guild=SimpleNamespace(id=1),session=session)
            ordinary=message('$잔고수정 citizen modify 1','citizen')
            await registry.handle(ordinary)
            ordinary.author.send.assert_not_awaited();ordinary.delete.assert_not_awaited()
            command=message('$잔고수정 citizen modify 500')
            await registry.handle(command)
            self.assertEqual(self.db.users.get_user('citizen')['cash'],500)
            command.author.send.assert_awaited_once()
            command.session.edit.assert_awaited_once()
            command.delete.assert_awaited_once();command.channel.send.assert_not_awaited()
            for old in ('stock','asset','settings','setup','register','currency','catalog','volatility'):
                command=message('$'+old)
                await registry.handle(command)
                self.assertIn('알 수 없는',command.author.send.call_args.kwargs['embed'].description)
                command.delete.assert_awaited_once()
            command=message('$도움말')
            await registry.handle(command)
            edited=command.session.edit.call_args.kwargs
            self.assertNotIn('$상품생성',edited['embed'].description or '')
            view=edited['view']
            self.assertIn('메인',view.build_embed().title)
            view.page=1
            self.assertTrue(any('$상품생성' in field.value for field in view.build_embed().fields))
            view.page=len(view.PAGES)-1
            self.assertTrue(any('$매수' in field.value for field in view.build_embed().fields))
        asyncio.run(run())

    def test_wallet_categories_preserve_obligation_and_page_all_holdings(self):
        from functions.wallet_views import WalletCategoryView
        import discord
        async def run():
            summary=discord.Embed(description='국적: 미국 · 기본 통화: USD')
            sections={'stocks':[f'주식 {i}' for i in range(23)],'written':['작성 계약 1']}
            view=WalletCategoryView('citizen',summary,sections,1234)
            main=view.build_embed()
            self.assertIn('메인',main.title)
            self.assertTrue(any(f.name=='📄 옵션 작성 의무 평가' and '−1,234' in f.value for f in main.fields))
            view.category='stocks'
            seen=[]
            for page in range(3):
                view.page=page
                embed=view.build_embed()
                seen.extend(embed.description.splitlines())
                self.assertLessEqual(len(embed),6000)
            self.assertEqual(seen,sections['stocks'])
            view.category='written'
            self.assertFalse(view.build_embed().fields)
            self.assertIn('만기까지 의무',view.build_embed().description)
            outsider=SimpleNamespace(user=SimpleNamespace(id='other'),response=SimpleNamespace(send_message=AsyncMock()))
            self.assertFalse(await view.interaction_check(outsider))
        asyncio.run(run())

    def test_positional_creation_varies_by_type_and_json_is_optional(self):
        self.run_command('상품생성','NEW company "새 회사" 게임 1000000 10000 100 TEST')
        row=self.db.companies.get_company('NEW')
        self.assertEqual((row['name'],row['industry'],row['capital'],row['total_shares'],row['share_price']),('새 회사','게임',1000000,10000,100))
        self.run_command('상품생성','SV saving "새 적금" 4.5 1000 30 86400 1 Inazina')
        row=self.db.products.get_saving('SV')
        self.assertEqual((row['name'],float(row['interest_rate']),row['deposit_amount']),('새 적금',4.5,1000))
        self.run_command('상품생성','NEW2 company "고정 주식" 게임 1000000 10000 100 TEST {"is_fixed": 1, "market_volatility": 0.4}')
        self.run_command('상품생성',"NEW3 company 고정주식 게임 1000000 10000 100 TEST '{\"is_fixed\":1}'")
        self.assertEqual(self.db.companies.get_company('NEW3')['is_fixed'],1)
        row=self.db.companies.get_company('NEW2')
        self.assertEqual((row['is_fixed'],row['market_volatility']),(1,0.4))
        with self.assertRaises(ValueError):self.run_command('상품생성','BAD saving 이름 게임 1000000 10000 100 TEST')
        self.assertIsNone(self.db.products.get_saving('BAD'))
        with self.assertRaises(ValueError):self.run_command('상품생성','BAD2 saving 이름 3 1000 30 86400 1 Inazina {"interest_rate": 5}')
        self.assertIsNone(self.db.products.get_saving('BAD2'))

    def test_positional_foreign_fund_option_and_creation_usage(self):
        fixtures.InazinaTests.seed(self)
        self.run_command('상품생성',"FXACC fxproduct 달러통장 USD 1")
        self.assertEqual(self.db.fx.execute_one("SELECT currency_code FROM fx_products WHERE product_code='FXACC'")[0],'USD')
        self.run_command('상품생성','FUND fund 혼합펀드 1000 10000 INZST001:0.5,INZST002:0.5 INZ')
        self.assertEqual(self.db.products.execute_one("SELECT COUNT(*) FROM fund_components WHERE product_code='FUND'")[0],2)
        self.db.assets.add_user_asset(TREASURY_USER_ID,'INZST001',1)
        self.run_command('상품생성','CALL option INZST001 call 1200 30 1 100 50')
        self.assertIsNotNone(self.db.assets.execute_one("SELECT * FROM option_contracts WHERE contract_code='CALL'"))
        with self.assertRaisesRegex(ValueError,'interest_rate'):
            self.run_command('상품생성','MISSING saving')
        from functions.admin_embeds import admin_help_embed
        from functions.admin_prefix import NATIONAL_COMMANDS
        embed=admin_help_embed(NATIONAL_COMMANDS)
        self.assertLessEqual(len(embed),6000)
        self.assertLessEqual(len(embed.fields),25)
        self.assertTrue(all(len(f.value)<=1024 for f in embed.fields))

    def test_help_category_navigation_and_revoked_role(self):
        from functions.admin_help_view import AdminHelpView
        from functions.admin_prefix import NATIONAL_COMMANDS
        async def run():
            view=AdminHelpView(self.bot,'admin',NATIONAL_COMMANDS)
            for index,_ in enumerate(view.PAGES):
                view.page=index
                embed=view.build_embed()
                self.assertLessEqual(len(embed),6000)
                self.assertTrue(all(len(field.value)<=1024 for field in embed.fields))
                self.assertEqual(sum(option.default for option in view.category_select.options),1)
            view.category_select._values=['citizens']
            owner=SimpleNamespace(user=SimpleNamespace(id='admin'),response=SimpleNamespace(edit_message=AsyncMock(),send_message=AsyncMock()))
            await view.select_category(owner)
            self.assertEqual(view.page,2)
            self.assertIn('add / del / modify',str(view.build_embed().to_dict()))
            self.assertTrue(await view.interaction_check(owner))
            self.db.users.execute("UPDATE users SET role='user' WHERE user_id='admin'")
            self.assertFalse(await view.interaction_check(owner))
        asyncio.run(run())
