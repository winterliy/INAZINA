import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import discord
from database import DatabaseManager
from database.db_manager import DBManager
from database.cleanup_legacy import cleanup_legacy
from database.user_db import TREASURY_USER_ID
from functions.discord_views import FXRatesView,AssetAccountsView,PriceMovementView,InfoPanelView
from functions.lottery_views import LotteryPurchaseView,LotteryTicketsView,result_embed
from functions.slash_commands import SlashCommandRegistry
from managers.catalog_manager import CatalogManager
from managers.fx_manager import FXManager
from managers.user_manager import UserManager
from managers.market_manager import MarketManager
from managers.lottery_manager import LotteryManager


class LatestRequestsTests(unittest.TestCase):
    def setUp(self):
        self.old_root = DBManager.database_dir
        self.tmp = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tmp.name)
        self.db = DatabaseManager()
        self.db.users.create_user('alice','앨리스','미국',1000000)
        self.db.users.create_user('bob','밥','대한민국',1000000)
        self.db.users.create_user('admin','관리자','낙원',1000000,'admin')
        self.bot = SimpleNamespace(db=self.db,fx_mgr=FXManager(self.db),user_mgr=UserManager(self.db),
            market_mgr=MarketManager(self.db),lottery_mgr=LotteryManager(self.db))

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.old_root
        self.tmp.cleanup()

    def seed(self):
        CatalogManager(self.db).seed()

    def test_graph_ranges_drive_history_and_render_window(self):
        self.db.assets.register_asset('GRAPH','그래프','EX',1000,'stock')
        view = PriceMovementView(self.bot,'alice','GRAPH')
        mapping = [('1분',60),('5분',300),('10분',600),('30분',1800),('1시간',3600),('6시간',21600),('12시간',43200),('1일',86400)]
        self.assertEqual(list(view.PERIODS),mapping)
        for label,seconds in mapping:
            view.period_seconds = seconds
            with patch('functions.discord_views.render_price_chart',return_value=b'PNG') as render:
                embed,_ = view.render()
                self.assertEqual(render.call_args.args[1],seconds)
                self.assertIn(label,embed.description)

    def test_search_separates_categories_and_overview_fits_discord(self):
        self.seed()
        self.db.products.register_investment('CF','원자재 선물','commodity_futures',100,1000,0,'국가','EX')
        panel = InfoPanelView(self.bot,'alice')
        self.assertNotIn('investments',dict(panel.CATEGORIES))
        self.assertNotIn('overview',dict(panel.CATEGORIES))
        self.assertNotIn('my_accounts',dict(panel.CATEGORIES))
        self.assertEqual(panel.category,'main')
        self.assertEqual(len(panel._category_lines('commodity_futures')),1)
        embed = panel.build_embed()
        self.assertLessEqual(len(embed),6000)
        self.assertNotIn('주식 외 투자상품',str(embed.to_dict()))
        self.assertTrue(any('ETF' in field.name for field in embed.fields))
        self.assertTrue(all(field.value.endswith('개') and '`' not in field.value for field in embed.fields))

    def test_trading_codes_are_plain_required_arguments(self):
        commands = []
        SlashCommandRegistry(SimpleNamespace(tree=SimpleNamespace(add_command=commands.append))).register()
        mapping = {'매수':'자산','매도':'자산','선물증거금':'선물','펀드가입':'펀드','펀드환매':'펀드','그래프':'종목'}
        for name,argument in mapping.items():
            parameter = next(p for p in next(c for c in commands if c.name==name).parameters if p.name==argument)
            self.assertTrue(parameter.required)
            self.assertFalse(parameter.autocomplete)
        self.assertTrue({'내옵션','외환계좌','국채','국적','내외환','환율','내자산'}.isdisjoint({c.name for c in commands}))

    def test_unified_account_opening_accepts_foreign_product_code_and_standard_button(self):
        self.seed()
        registry=SlashCommandRegistry(self.bot)
        registry._prepare=AsyncMock(return_value=('alice',self.db.users.get_user('alice'),'user'))
        registry._reply=AsyncMock()
        async def run():
            await registry.open_account(SimpleNamespace(),상품='INZFX001')
            self.assertIn('외환통장 개설',registry._reply.call_args.kwargs['content'])
            self.assertEqual(self.db.fx.execute_one("SELECT COUNT(*) FROM fx_accounts WHERE user_id='alice'")[0],1)
            await registry.open_account(SimpleNamespace(),상품='INZSV001')
            from functions.discord_views import AccountProductOpenView
            view=registry._reply.call_args.kwargs['view']
            self.assertIsInstance(view,AccountProductOpenView)
            self.assertFalse(any(isinstance(item,discord.ui.Select) for item in view.children))
        asyncio.run(run())

    def test_foreign_catalog_and_primary_fill_use_native_currency_and_nwx_valuation(self):
        self.seed()
        self.assertTrue(CatalogManager(self.db).seed_foreign())
        self.assertFalse(CatalogManager(self.db).seed_foreign())
        self.assertEqual(self.db.companies.execute_one("SELECT COUNT(*) FROM company WHERE quote_currency<>'NWX'")[0],50)
        self.assertEqual(self.db.products.execute_one("SELECT COUNT(*) FROM investment WHERE quote_currency<>'NWX'")[0],50)
        from managers.order_book_manager import OrderBookManager
        book=OrderBookManager(self.db,self.bot.user_mgr)
        self.bot.fx_mgr.open_account('alice','INZFX001')
        self.bot.fx_mgr.exchange('alice','NWX','USD','400000')
        success,message=book.place_limit_order('alice','INZUSDST001','buy',2,120)
        self.assertTrue(success,message)
        self.assertEqual(self.db.assets.execute_one("SELECT quote_currency FROM market_orders WHERE user_id='alice'")[0],'USD')
        self.assertEqual(self.db.fx.execute_one("SELECT balance_units FROM fx_accounts WHERE user_id='alice' AND currency_code='USD'")[0],46400000)
        self.assertEqual(self.db.fx.execute_one("SELECT balance_units FROM fx_accounts WHERE user_id=? AND currency_code='USD'",(TREASURY_USER_ID,))[0],220000000)
        info=self.bot.user_mgr.get_total_assets('alice')
        self.assertEqual(info['stock_total'],330000)
        self.assertEqual(info['grand_total'],999600)
        self.db.assets.update_price('FX_USD',1600)
        self.assertEqual(self.bot.user_mgr.get_total_assets('alice')['stock_total'],352000)
        panel=InfoPanelView(self.bot,'alice')
        self.assertTrue(any('INZUSDST001' in line and 'USD' in line for line in panel._category_lines('stocks')))
        graph,_=PriceMovementView(self.bot,'alice','INZUSDST001').render()
        self.assertIn('USD',graph.description)

    def test_foreign_order_cancel_refunds_and_secondary_fill_applies_profit_tax(self):
        self.seed()
        CatalogManager(self.db).seed_foreign()
        from managers.order_book_manager import OrderBookManager
        book=OrderBookManager(self.db,self.bot.user_mgr)
        for user in ('alice','bob'):
            self.bot.fx_mgr.open_account(user,'INZFX001')
            self.bot.fx_mgr.exchange(user,'NWX','USD','400000')
        before=self.bot.fx_mgr.accounts('alice')[0]['balance_units']
        success,message=book.place_limit_order('alice','INZUSDST001','buy',1,90)
        self.assertTrue(success,message)
        reserved=self.db.assets.execute_one("SELECT order_id FROM market_orders WHERE user_id='alice'")[0]
        info=self.bot.user_mgr.get_total_assets('alice')
        self.assertEqual(info['order_reserved_total'],135000)
        self.assertTrue(book.cancel_order('alice',reserved)[0])
        self.assertEqual(self.bot.fx_mgr.accounts('alice')[0]['balance_units'],before)
        self.assertTrue(book.place_limit_order('alice','INZUSDST001','buy',1,110)[0])
        self.db.companies.update_share_price('INZUSDST001',200)
        self.assertTrue(book.place_limit_order('alice','INZUSDST001','sell',1,120)[0])
        self.assertTrue(book.place_limit_order('bob','INZUSDST001','buy',1,130)[0])
        self.assertIsNone(self.db.assets.execute_one("SELECT quantity FROM user_assets WHERE user_id='alice' AND asset_code='INZUSDST001'"))
        self.assertEqual(self.db.assets.execute_one("SELECT quantity FROM user_assets WHERE user_id='bob' AND asset_code='INZUSDST001'")[0],1)
        tax=self.db.tax.execute_one("SELECT * FROM tax_transactions WHERE source='foreign_security_sale_gain'")
        self.assertEqual((tax['gross_amount'],tax['tax_amount']),(15000,450))
        self.assertEqual(self.db.fx.execute_one("SELECT balance_units FROM fx_accounts WHERE user_id='alice' AND currency_code='USD'")[0],276100000)

    def test_foreign_security_missing_account_and_insufficient_currency_do_not_use_nwx_cash(self):
        self.seed()
        CatalogManager(self.db).seed_foreign()
        from managers.order_book_manager import OrderBookManager
        book=OrderBookManager(self.db,self.bot.user_mgr)
        before=self.db.users.get_user('alice')['cash']
        self.assertFalse(self.bot.user_mgr.buy_asset('alice','INZUSDST001',1)[0])
        self.assertFalse(self.bot.user_mgr.sell_asset('alice','INZUSDST001',1)[0])
        self.assertFalse(book.place_limit_order('alice','INZUSDST001','buy',1,110)[0])
        self.bot.fx_mgr.open_account('alice','INZFX001')
        self.assertFalse(book.place_limit_order('alice','INZUSDST001','buy',1,110)[0])
        self.assertEqual(self.db.users.get_user('alice')['cash'],before)
        self.assertEqual(self.db.assets.execute_one("SELECT COUNT(*) FROM market_orders WHERE asset_code='INZUSDST001'")[0],0)

    def test_written_option_liability_is_marked_at_current_premium_and_collateral_is_locked(self):
        self.seed()
        self.db.assets.execute("UPDATE option_contracts SET outstanding_contracts=1 WHERE contract_code='INZOP001'")
        self.db.assets.execute("INSERT INTO market_quote_overrides VALUES ('INZOP001',123,1,'event')")
        info=self.bot.user_mgr.get_total_assets(TREASURY_USER_ID)
        self.assertEqual(info['option_short_liability'],123)
        self.assertEqual(len(info['written_options']),50)
        collateral=next(row for row in info['stocks'] if row.get('collateral_contract_code')=='INZOP001')
        self.assertTrue(collateral['is_collateral'])
        self.assertEqual(collateral['code'],'INZST001')
        from managers.order_book_manager import OrderBookManager
        self.assertFalse(OrderBookManager(self.db,self.bot.user_mgr).place_limit_order(TREASURY_USER_ID,'INZST001','sell',1,1000)[0])

    def test_rates_page_10_each_latest_quotes_and_owner_only(self):
        self.seed()
        async def run():
            view = FXRatesView(self.bot,'alice')
            seen = []
            for page,count in enumerate((10,10,10,10,10,1)):
                view.page = page
                embed = view.build_embed()
                self.assertEqual(len(embed.fields),count)
                self.assertLessEqual(len(embed),6000)
                seen.extend(f.name.split(' · ')[0] for f in embed.fields)
            self.assertEqual(len(set(seen)),51)
            self.db.assets.update_price('FX_USD',1600)
            view.page=5
            self.assertTrue(any('1 NWX = **0.000625 USD**' in f.value for f in view.build_embed().fields if f.name.startswith('USD')))
            interaction=SimpleNamespace(user=SimpleNamespace(id='bob'),response=SimpleNamespace(send_message=AsyncMock()))
            self.assertFalse(await view.interaction_check(interaction))
        asyncio.run(run())

    def test_search_forex_inverts_rates_pages_ten_and_refreshes_quotes(self):
        self.seed()
        panel = InfoPanelView(self.bot,'alice')
        panel.category = 'forex'
        seen = []
        for page in range(5):
            panel.page = page
            embed = panel.build_embed()
            rows = [line for line in embed.description.splitlines() if line.startswith('•')]
            self.assertEqual(len(rows),10)
            self.assertTrue(all('1 NWX = ' in line for line in rows))
            seen.extend(rows)
        self.assertEqual(len(set(seen)),50)
        self.db.assets.update_price('FX_USD',1600)
        self.assertTrue(any('0.000625 USD' in line for line in panel._category_lines('forex')))
        self.db.assets.update_price('FX_USD',2000)
        self.assertIn('0.0005 USD',panel.build_embed().description)

    def test_assets_include_nationality_and_all_foreign_accounts(self):
        self.seed()
        for index in range(1,7):
            self.bot.fx_mgr.open_account('alice',f'INZFX{index:03d}')
        self.bot.fx_mgr.exchange('alice','NWX','USD','15000')
        info = self.bot.user_mgr.get_total_assets('alice')
        self.assertEqual((info['nationality'],info['national_currency']),('미국','USD'))
        self.assertEqual(len(info['fx_accounts']),6)
        self.assertEqual(info['fx_total'],14985)
        before = self.db.fx.execute_one("SELECT last_interest_epoch FROM fx_accounts WHERE user_id='alice' AND currency_code='USD'")[0]
        snapshot=self.bot.user_mgr.get_total_assets('alice',settle=False)
        self.assertEqual(len(snapshot['fx_accounts']),6)
        self.assertEqual(self.db.fx.execute_one("SELECT last_interest_epoch FROM fx_accounts WHERE user_id='alice' AND currency_code='USD'")[0],before)
        registry = SlashCommandRegistry(self.bot)
        registry._prepare=AsyncMock(return_value=('alice',self.db.users.get_user('alice'),'user'))
        registry._reply=AsyncMock()
        interaction=SimpleNamespace(user=SimpleNamespace(display_name='앨리스',name='Alice'))
        asyncio.run(registry.assets(interaction))
        sent = registry._reply.call_args.kwargs
        self.assertIn('미국',sent['embed'].description)
        self.assertIn('USD',sent['embed'].description)
        from functions.wallet_views import WalletCategoryView
        self.assertIsInstance(sent['view'],WalletCategoryView)
        seen=[]
        sent['view'].category='fx_accounts'
        seen.append(sent['view'].build_embed().description)
        for account in info['fx_accounts']:
            self.assertIn(account['account_number'],'\n'.join(seen))

    def test_transfer_wallet_foreign_wallet_and_other_user_without_conversion_fee(self):
        self.seed()
        alice=self.bot.fx_mgr.open_account('alice','INZFX001')
        bob=self.bot.fx_mgr.open_account('bob','INZFX001')
        success,message=self.bot.user_mgr.transfer('alice',alice,'15000','__wallet__')
        self.assertTrue(success,message)
        self.assertEqual(self.db.fx.execute_one('SELECT balance_units FROM fx_accounts WHERE account_number=?',(alice,))[0],9990000)
        success,message=self.bot.user_mgr.transfer('alice',bob,'1.5',alice)
        self.assertTrue(success,message)
        row=self.db.fx.execute_one('SELECT * FROM fx_transfer_transactions ORDER BY transaction_id DESC LIMIT 1')
        self.assertEqual((row['fee_nwx'],row['target_units'],row['recipient_id']),(0,1500000,'bob'))
        success,message=self.bot.user_mgr.transfer('alice','alice','1',alice)
        self.assertTrue(success,message)
        self.assertEqual(self.db.fx.execute_one('SELECT target_units FROM fx_transfer_transactions ORDER BY transaction_id DESC LIMIT 1')[0],1498000000)

    def test_foreign_transfer_insufficient_wrong_owner_and_fractional_nwx_rollback(self):
        self.seed()
        alice=self.bot.fx_mgr.open_account('alice','INZFX001')
        bob=self.bot.fx_mgr.open_account('bob','INZFX001')
        before=self.db.users.get_user('alice')['cash']
        for source,target,amount in [(bob,alice,'1'),('__wallet__',alice,'1.5'),(alice,bob,'1'),(alice,alice,'1')]:
            success,_=self.bot.user_mgr.transfer('alice',target,amount,source)
            self.assertFalse(success)
        self.assertEqual(self.db.users.get_user('alice')['cash'],before)
        self.assertEqual(self.db.fx.execute_one('SELECT COUNT(*) FROM fx_transfer_transactions')[0],0)

    def test_foreign_transfers_update_bank_principal_and_offer_account_codes(self):
        self.seed()
        from managers.bank_manager import BankManager
        success,message=BankManager(self.db).open_account('alice','INZSV001','pass1234',15000)
        self.assertTrue(success,message)
        bank=self.db.accounts.execute_one("SELECT account_number FROM accounts WHERE user_id='alice'")[0]
        fx=self.bot.fx_mgr.open_account('alice','INZFX001')
        success,message=self.bot.user_mgr.transfer('alice',fx,'15000',bank)
        self.assertTrue(success,message)
        self.assertEqual(tuple(self.db.accounts.execute_one('SELECT balance,principal FROM accounts WHERE account_number=?',(bank,))),(0,0))
        success,message=self.bot.user_mgr.transfer('alice',bank,'1',fx)
        self.assertTrue(success,message)
        self.assertEqual(tuple(self.db.accounts.execute_one('SELECT balance,principal FROM accounts WHERE account_number=?',(bank,))),(1498,1498))
        registry=SlashCommandRegistry(self.bot)
        interaction=SimpleNamespace(user=SimpleNamespace(id='alice'))
        self.assertIn(fx,[c.value for c in asyncio.run(registry.source_account_autocomplete(interaction,'USD'))])
        self.assertIn(fx,[c.value for c in asyncio.run(registry.recipient_autocomplete(interaction,fx))])

    def test_lottery_confirmation_is_required_and_draw_secret_is_private(self):
        current=self.bot.lottery_mgr.current(now=1000000)
        self.assertNotIn('secret',current)
        self.assertNotIn('entropy',current)
        registry=SlashCommandRegistry(self.bot)
        registry._prepare=AsyncMock(return_value=('alice',None,'user'))
        registry._reply=AsyncMock()
        # Use an actual current round for the UI's wall-clock close check.
        self.db.lottery.execute('DELETE FROM lottery_rounds')
        asyncio.run(registry.lottery_buy(SimpleNamespace(id=123),수량=2,번호='자동'))
        self.assertIsInstance(registry._reply.call_args.kwargs['view'],LotteryPurchaseView)
        self.assertEqual(self.db.lottery.execute_one('SELECT COUNT(*) FROM lottery_tickets')[0],0)

    def test_lottery_ui_confirmation_retry_and_private_ticket_pages(self):
        async def run():
            round=self.bot.lottery_mgr.current()
            view=LotteryPurchaseView(self.bot,'alice',round,12,'자동','confirm-request')
            interaction=SimpleNamespace(user=SimpleNamespace(id='alice'),response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),message=SimpleNamespace(edit=AsyncMock()))
            self.assertTrue(await view.interaction_check(interaction))
            for _ in range(2):
                await view.confirm.callback(interaction)
                sent=interaction.followup.send.call_args.kwargs
                self.assertTrue(sent['ephemeral'])
                sent['file'].close()
            self.assertEqual(self.db.users.get_user('alice')['cash'],988000)
            tickets=LotteryTicketsView('alice',self.bot.lottery_mgr.tickets('alice'))
            self.assertEqual(len(tickets.build_embed().fields),10)
            tickets.page=1
            self.assertEqual(len(tickets.build_embed().fields),2)
            interaction.user.id='bob'
            self.assertFalse(await tickets.interaction_check(interaction))
        asyncio.run(run())

    def test_lottery_ranks_and_no_ticket_draw_rollover(self):
        lottery=self.bot.lottery_mgr
        winning=[1,2,3,4,5,6]
        for numbers,rank in [([1,2,3,4,5,6],1),([1,2,3,4,5,7],2),([1,2,3,4,5,8],3),([1,2,3,4,8,9],4),([1,2,3,8,9,10],5),([1,2,8,9,10,11],0)]:
            self.assertEqual(lottery.rank(numbers,winning,7),rank)
        round=lottery.current(now=1000000)
        lottery.process_due(now=round['draw_epoch'])
        result=lottery.result(round['round_id'])
        self.assertEqual(result['status'],'settled')
        self.assertEqual(result['sales_total'],0)
        self.assertTrue(all(r['winners']==0 and r['paid']==0 for r in result['result'].values()))

    def test_lottery_purchase_is_atomic_idempotent_and_limited(self):
        lottery=self.bot.lottery_mgr
        current=lottery.current(now=1000000)
        result=lottery.purchase('alice',2,'1 2 3 4 5 6','purchase',now=1000001,expected_round=current['round_id'])
        self.assertEqual(self.db.users.get_user('alice')['cash'],998000)
        self.assertEqual(self.db.users.get_treasury_balance(),400)
        self.assertEqual(lottery.purchase('alice',2,'1 2 3 4 5 6','purchase',now=1000002),result)
        for quantity,numbers,request,now in [(99,'자동','limit',1000002),(1,'1 1 2 3 4 5','bad',1000002),(1,'자동','late',current['close_epoch'])]:
            with self.assertRaises(ValueError):lottery.purchase('alice',quantity,numbers,request,now=now)
        self.assertEqual(len(lottery.tickets('alice')),2)

    def test_lottery_concurrent_duplicate_purchase_charges_once(self):
        lottery=self.bot.lottery_mgr
        lottery.current(now=1000000)
        def buy(_):return lottery.purchase('alice',1,'자동','same-request',now=1000001)
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(buy,range(4)))
        self.assertTrue(all(r==results[0] for r in results))
        self.assertEqual(self.db.users.get_user('alice')['cash'],999000)
        self.assertEqual(len(lottery.tickets('alice')),1)

    def test_lottery_known_draw_pays_tax_once_and_rolls_unwon_pools(self):
        lottery=self.bot.lottery_mgr
        round=lottery.current(now=1000000)
        lottery.purchase('alice',2,'1 2 3 4 5 6','win',now=1000001)
        self.db.lottery.execute("UPDATE lottery_rounds SET status='drawn',winning_json='[1,2,3,4,5,6]',bonus=7,entropy=? WHERE round_id=?",('ab'*32,round['round_id']))
        settled=lottery.process_due(now=round['draw_epoch'])
        self.assertEqual(settled,round['round_id'])
        self.assertEqual(self.db.users.get_user('alice')['cash'],998678)
        self.assertEqual(self.db.users.get_treasury_balance(),422)
        payouts=self.db.lottery.execute('SELECT * FROM lottery_payouts')
        self.assertEqual([(p['gross'],p['tax'],p['net']) for p in payouts],[(350,11,339)]*2)
        before=self.db.users.get_user('alice')['cash']
        lottery.process_due(now=round['draw_epoch'])
        self.assertEqual(self.db.users.get_user('alice')['cash'],before)
        self.assertEqual(lottery.current(now=round['draw_epoch'])['pools'],[0,140,210,210,140])
        self.assertEqual(lottery.result(round['round_id'])['status'],'settled')

    def test_lottery_restarts_reproduce_public_draw_and_resume_failed_payout(self):
        lottery=self.bot.lottery_mgr
        round=lottery.current(now=1000000)
        entropy='ef'*32
        secret=self.db.lottery.execute_one('SELECT secret FROM lottery_rounds')[0]
        winning,bonus=lottery.draw_numbers(secret,entropy)
        self.assertEqual(len(set(winning+[bonus])),7)
        self.assertEqual(hashlib.sha256(bytes.fromhex(secret)).hexdigest(),round['commitment'])
        lottery.purchase('alice',1,' '.join(map(str,winning)),'winner',now=1000001)
        self.db.users.execute('UPDATE users SET cash=? WHERE user_id=?',(2**53-1,'alice'))
        with patch('managers.lottery_manager.secrets.token_hex',return_value=entropy):
            with self.assertRaises(ValueError):lottery.process_due(now=round['draw_epoch'])
        pending=lottery.result(round['round_id'])
        self.assertEqual(pending['winning'],winning)
        self.assertEqual(pending['status'],'drawn')
        self.assertEqual(self.db.lottery.execute_one('SELECT COUNT(*) FROM lottery_payouts')[0],0)
        self.db.users.execute('UPDATE users SET cash=0 WHERE user_id=?',('alice',))
        restarted=LotteryManager(self.db)
        restarted.process_due(now=round['draw_epoch']+1)
        result=restarted.result(round['round_id'])
        self.assertEqual(result['winning'],winning)
        self.assertEqual(result['entropy'],entropy)
        self.assertLessEqual(len(result_embed(result)),6000)

    def test_lottery_rules_freeze_and_ledger_mismatch_preserves_draw(self):
        lottery=self.bot.lottery_mgr
        round=lottery.current(now=1000000)
        with self.assertRaises(ValueError):lottery.configure('alice',{'ticket_price':2000})
        lottery.configure('admin',{'ticket_price':2000})
        self.assertEqual(lottery.current(now=1000001)['rules']['ticket_price'],1000)
        lottery.purchase('alice',1,'자동','ticket',now=1000001)
        self.db.lottery.execute("UPDATE lottery_pool_ledger SET amount=amount+1 WHERE kind='sale' AND rank=1")
        with self.assertRaises(ValueError):lottery.process_due(now=round['draw_epoch'])
        self.assertEqual(lottery.result(round['round_id'])['status'],'drawn')
        self.assertEqual(self.db.lottery.execute_one('SELECT COUNT(*) FROM lottery_payouts')[0],0)


class LegacyCleanupTests(unittest.TestCase):
    def test_cleanup_archives_only_migrated_known_databases_and_keeps_record_db(self):
        from database.unified import migrate
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with closing(sqlite3.connect(root/'users.db')) as conn:
                conn.execute('CREATE TABLE users(user_id TEXT PRIMARY KEY,cash INTEGER)')
                conn.execute("INSERT INTO users VALUES ('user',100)")
                conn.commit()
            with closing(sqlite3.connect(root/'records.db')) as conn:
                conn.execute('CREATE TABLE history(value INTEGER)')
                conn.commit()
            migrate(root)
            archive=root/'backup.zip'
            self.assertEqual(cleanup_legacy(root,archive),['users.db'])
            self.assertTrue((root/'records.db').exists())
            self.assertTrue((root/'inazina.db').exists())
            with zipfile.ZipFile(archive) as zip:
                data=zip.read('users.db')
                manifest=json.loads(zip.read('manifest.json'))
                self.assertEqual(hashlib.sha256(data).hexdigest(),manifest['users.db']['sha256'])
            self.assertEqual(cleanup_legacy(root,archive),[])

    def test_cleanup_refuses_unmigrated_db_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with closing(sqlite3.connect(root/'inazina.db')) as conn:
                conn.execute('CREATE TABLE migration_manifest(source TEXT)')
                conn.commit()
            with closing(sqlite3.connect(root/'users.db')) as conn:
                conn.execute('CREATE TABLE users(user_id TEXT)')
                conn.commit()
            with self.assertRaises(RuntimeError):cleanup_legacy(root,root/'backup.zip')
            self.assertTrue((root/'users.db').exists())
