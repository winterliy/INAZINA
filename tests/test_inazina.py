import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
import discord
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from contextlib import closing

from database import DatabaseManager
from database.db_manager import DBManager
from database.unified import migrate, sql
from database.user_db import TREASURY_USER_ID
from managers.user_manager import UserManager
from managers.market_manager import MarketManager
from managers.option_manager import OptionManager
from managers.order_book_manager import OrderBookManager
from managers.rate_manager import RateManager
from managers.investment_manager import InvestmentManager
from managers.bank_manager import BankManager
from managers.futures_manager import FuturesManager
from managers.admin_manager import AdminManager
from managers.admin_control_manager import AdminControlManager,ENTITIES,EXAMPLES
from managers.catalog_manager import CatalogManager
from managers.fx_manager import FXManager,SCALE
from functions.admin_prefix import AdminPrefixRegistry,AdminCommandView,EntityModal,ActionModal,TreasuryAssetsView,READ_ONLY
from functions.discord_views import PriceMovementView,InfoPanelView
from functions.slash_commands import SlashCommandRegistry

class InazinaTests(unittest.TestCase):
    def setUp(self):
        self.old_root=DBManager.database_dir
        self.tmp=tempfile.TemporaryDirectory()
        DBManager.database_dir=Path(self.tmp.name)
        self.db=DatabaseManager()
        self.db.users.create_user('admin','관리자','낙원',1000000,'admin')
        self.db.users.create_user('citizen','시민','미국',1000000)
        self.db.users.update_cash(TREASURY_USER_ID,10000000)
        users=UserManager(self.db)
        self.bot=SimpleNamespace(db=self.db,user_mgr=users,market_mgr=MarketManager(self.db),
            option_mgr=OptionManager(self.db,users),order_book=OrderBookManager(self.db,users),
            rate_mgr=RateManager(self.db),investment_mgr=InvestmentManager(self.db,users),
            bank_mgr=BankManager(self.db),futures_mgr=FuturesManager(self.db,users),
            admin_mgr=AdminManager(self.db),fx_mgr=FXManager(self.db),treasury_bond_mgr=users.bond_manager)
        self.bot.order_book.option_manager=self.bot.option_mgr
        self.bot.order_book.futures_manager=self.bot.futures_mgr
        self.bot.admin_control=AdminControlManager(self.bot)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir=self.old_root
        self.tmp.cleanup()

    def seed(self):
        return CatalogManager(self.db).seed()

    def test_only_one_database_and_cross_manager_atomic_rollback(self):
        paths={d.db_path for d in (self.db.users,self.db.assets,self.db.accounts,self.db.products,self.db.system,self.db.fx)}
        self.assertEqual(len(paths),1)
        self.assertEqual(list(Path(self.tmp.name).glob('*.db')),[Path(self.tmp.name)/'inazina.db'])
        with self.assertRaises(RuntimeError):
            with self.db.atomic(('users','system')) as conn:
                conn.execute("UPDATE users.users SET cash=1 WHERE user_id='citizen'")
                conn.execute("UPDATE system.settings SET value='7' WHERE key='base_rate'")
                raise RuntimeError()
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1000000)
        self.assertEqual(self.db.system.get_setting('base_rate'),'3')
        self.assertEqual(sql("SELECT 'users.users', x FROM pot.\"return\" -- assets.market_assets"),"SELECT 'users.users', x FROM \"return\" -- assets.market_assets")

    def test_catalog_has_fifty_per_category_and_is_idempotent(self):
        self.assertTrue(self.seed())
        self.assertFalse(self.seed())
        counts={r['product_type']:r['n'] for r in self.db.products.execute('SELECT product_type,COUNT(*) n FROM investment GROUP BY product_type')}
        self.assertEqual(counts,{k:50 for k in ('etf','stock_future','corporate_bond','mutual_fund','index_fund','commodity_spot')})
        for table in ('company','treasury_bond_products','saving','liability','option_contracts','fx_products'):
            self.assertEqual(self.db.system.execute_one(f'SELECT COUNT(*) FROM {table}')[0],50)
        self.assertEqual(self.db.fx.execute_one("SELECT COUNT(*) FROM currencies WHERE code<>'NWX'")[0],50)
        self.assertEqual(self.db.assets.execute_one('SELECT SUM(quantity) FROM option_collateral_lots')[0],5000)
        self.assertEqual(self.db.assets.execute_one("SELECT COUNT(*) FROM market_orders WHERE option_order_type='write'")[0],50)

    def test_seed_collision_rolls_back_every_listing(self):
        self.db.assets.register_asset('INZST005','衝突','INZ',100,'stock')
        with self.assertRaises(ValueError): self.seed()
        self.assertEqual(self.db.companies.execute_one('SELECT COUNT(*) FROM company')[0],0)
        self.assertIsNone(self.db.system.get_setting(CatalogManager.VERSION))

    def test_currency_exchange_debits_and_credits_exact_units(self):
        self.seed()
        self.bot.fx_mgr.open_account('citizen','INZFX001')
        result=self.bot.fx_mgr.exchange('citizen','NWX','USD','15000')
        self.assertIn('수수료 15',result)
        self.assertEqual(self.db.users.get_user('citizen')['cash'],985000)
        self.assertEqual(self.db.fx.execute_one("SELECT balance_units FROM fx_accounts WHERE user_id='citizen'")[0],9_990_000)
        self.assertEqual(self.db.users.get_treasury_balance(),10000015)
        self.assertEqual(self.bot.fx_mgr.national_currency('citizen'),('미국','USD'))
        self.db.assets.update_price('FX_USD',1600)
        rows=self.bot.fx_mgr.accounts('citizen')
        self.assertEqual(rows[0]['value_nwx'],15984)
        self.assertEqual(self.bot.user_mgr.get_total_assets('citizen')['fx_total'],15984)

    def test_exchange_failure_and_insufficient_funds_rollback_everything(self):
        self.seed()
        self.bot.fx_mgr.open_account('citizen','INZFX001')
        before=self.db.users.get_user('citizen')['cash']
        for source,target,amount in [('NWX','EUR','100'),('NWX','USD','2000000'),('USD','NWX','1'),('NWX','USD','nan'),('NWX','USD','1.5'),('NWX','NWX','10')]:
            with self.assertRaises(ValueError):self.bot.fx_mgr.exchange('citizen',source,target,amount)
        self.assertEqual(self.db.users.get_user('citizen')['cash'],before)
        self.assertEqual(self.db.fx.execute_one('SELECT COUNT(*) FROM fx_transactions')[0],0)

    def test_cross_currency_exchange_and_interest_fraction_carry(self):
        self.seed()
        self.bot.fx_mgr.open_account('citizen','INZFX001')
        self.bot.fx_mgr.open_account('citizen','INZFX003')
        self.bot.fx_mgr.exchange('citizen','NWX','USD','15000')
        result=self.bot.fx_mgr.exchange('citizen','USD','EUR','1')
        self.assertIn('EUR',result)
        with self.db.atomic() as conn:
            row=conn.execute("SELECT * FROM fx_accounts WHERE user_id='citizen' AND currency_code='USD'").fetchone()
            self.bot.fx_mgr._settle(conn,'citizen',row['last_interest_epoch']+365*86400)
            updated=conn.execute('SELECT * FROM fx_accounts WHERE account_number=?',(row['account_number'],)).fetchone()
            self.assertGreater(updated['balance_units'],row['balance_units'])

    def test_admin_reauthorization_crud_audit_and_settings_validation(self):
        control=self.bot.admin_control
        with self.assertRaises(ValueError): control.entity('citizen','stock','create','BAD','Bad',100,100,{})
        control.entity('admin','stock','create','NEW','새주식','100','1000',EXAMPLES['stock'])
        control.entity('admin','stock','edit','NEW',price='200',extra={'market_volatility':.5})
        self.assertEqual(self.db.companies.get_company('NEW')['share_price'],200)
        self.assertTrue(self.bot.user_mgr.buy_asset('citizen','NEW',5)[0])
        with self.assertRaises(ValueError): control.entity('admin','stock','edit','NEW',supply='1')
        control.entity('admin','stock','delete','NEW')
        self.assertEqual(self.db.companies.get_company('NEW')['status'],'inactive')
        self.assertEqual(self.db.assets.get_user_portfolio('citizen')[0]['quantity'],5)
        control.entity('admin','stock','restore','NEW')
        with self.assertRaises(ValueError): control.settings('admin',{'garch_alpha':.9,'garch_beta':.9})
        self.assertEqual(self.db.system.get_setting('garch_alpha'),'0.05')
        control.settings('admin',{'market_volatility':.4,'tax_percent':5,'base_rate':4})
        self.assertEqual(self.db.tax.get_rate_percent(),5)
        self.db.users.update_role('admin','user')
        with self.assertRaises(ValueError): control.settings('admin',{'market_volatility':.5})
        self.assertGreater(self.db.system.execute_one('SELECT COUNT(*) FROM admin_audit')[0],3)

    def test_national_trade_uses_treasury_identity(self):
        self.db.assets.register_asset('TRADE','국가주식','EX',100,'stock')
        self.db.users.execute("UPDATE users SET cash=10000 WHERE user_id=?",(TREASURY_USER_ID,))
        async def run():
            modal=ActionModal(self.bot,'admin','매수')
            return modal.execute(['TRADE','2','100'])
        self.assertIn('✅',asyncio.run(run()))
        self.assertEqual(self.db.users.get_user('admin')['cash'],1000000)
        order=self.db.assets.execute_one("SELECT * FROM market_orders WHERE user_id=?",(TREASURY_USER_ID,))
        self.assertEqual(order['quantity'],2)
        self.assertEqual(self.db.users.get_treasury_balance(),9800)
        self.assertTrue(self.bot.order_book.cancel_order(TREASURY_USER_ID,order['order_id'])[0])
        self.assertEqual(self.db.users.get_treasury_balance(),10000)

    def test_prefix_deletes_chat_and_denies_ordinary_users(self):
        channel=SimpleNamespace(send=AsyncMock())
        registry=AdminPrefixRegistry(self.bot)
        message=SimpleNamespace(author=SimpleNamespace(id='admin',bot=False,send=AsyncMock()),content='$사건',delete=AsyncMock(),channel=channel,guild=SimpleNamespace(id=1))
        asyncio.run(registry.handle(message))
        message.delete.assert_awaited_once()
        self.assertIsInstance(message.author.send.call_args_list[0].kwargs.get('view',message.author.send.return_value.edit.call_args.kwargs['view']),AdminCommandView)
        self.assertEqual(message.author.send.call_args.kwargs['delete_after'],300)
        channel.send.assert_not_awaited()
        message.author.id='citizen'
        message.delete.reset_mock()
        message.author.send.reset_mock()
        asyncio.run(registry.handle(message))
        message.delete.assert_not_awaited()
        message.author.send.assert_not_awaited()
        channel.send.assert_not_awaited()

    def test_prefix_private_response_precedes_cleanup_for_all_command_kinds(self):
        async def run():
            registry=AdminPrefixRegistry(self.bot)
            guild=SimpleNamespace(id=123)
            for content in ('$stock','$help','$사건','$매수','$not_a_command'):
                events=[]
                async def send(*args,**kwargs):
                    events.append('response')
                    if 'view' in kwargs:
                        self.assertIs(kwargs['view'].guild,guild)
                    return SimpleNamespace(edit=AsyncMock())
                async def delete():
                    events.append('delete')
                message=SimpleNamespace(author=SimpleNamespace(id='admin',bot=False,send=AsyncMock(side_effect=send)),content=content,delete=AsyncMock(side_effect=delete),channel=SimpleNamespace(send=AsyncMock()),guild=guild)
                await registry.handle(message)
                self.assertEqual(events[-1],'delete')
                self.assertTrue(all(e=='response' for e in events[:-1]))
                message.channel.send.assert_not_awaited()
        asyncio.run(run())

    def test_prefix_blocked_dm_never_publishes_controls_and_cleans_up(self):
        response=SimpleNamespace(status=403,reason='Forbidden')
        message=SimpleNamespace(author=SimpleNamespace(id='admin',bot=False,send=AsyncMock(side_effect=discord.Forbidden(response,'DM blocked'))),content='$사건',delete=AsyncMock(),channel=SimpleNamespace(send=AsyncMock()),guild=SimpleNamespace(id=1))
        asyncio.run(AdminPrefixRegistry(self.bot).handle(message))
        message.delete.assert_awaited_once()
        message.channel.send.assert_not_awaited()

    def test_prefix_cleanup_failure_disables_private_session(self):
        response=SimpleNamespace(status=403,reason='Forbidden')
        session=SimpleNamespace(edit=AsyncMock())
        message=SimpleNamespace(author=SimpleNamespace(id='admin',bot=False,send=AsyncMock(return_value=session)),content='$사건',delete=AsyncMock(side_effect=discord.Forbidden(response,'Missing permissions')),channel=SimpleNamespace(send=AsyncMock()),guild=SimpleNamespace(id=1))
        asyncio.run(AdminPrefixRegistry(self.bot).handle(message))
        self.assertEqual(session.edit.call_args.kwargs,{"view":None})
        view=session.edit.call_args_list[0].kwargs['view']
        self.assertTrue(view.is_finished())
        self.assertIn('메시지 관리',message.author.send.call_args.kwargs['embed'].description)
        message.channel.send.assert_not_awaited()

    def test_modal_fields_and_revoked_role_are_checked(self):
        async def run():
            modal=EntityModal(self.bot,'admin','stock','create')
            self.assertEqual(len(modal.children),5)
            interaction=SimpleNamespace(user=SimpleNamespace(id='admin'),response=SimpleNamespace(send_message=AsyncMock()))
            self.db.users.update_role('admin','user')
            self.assertFalse(await modal.interaction_check(interaction))
        asyncio.run(run())

    def test_option_bond_fx_and_linked_events_preserve_history_and_terms(self):
        self.seed()
        now=int(time.time())
        for code in ('INZOP001','INZTB001','INZCB001','INZFU001','INZMF001','INZIF001','INZETF001','FX_USD'):
            with self.subTest(code=code):
                asset=self.bot.market_mgr.get_asset_info(code)
                goal=int(asset['price']*1.5)
                success,message=self.bot.admin_mgr.create_market_event('admin','good',100,code,10,now=now,target_price=goal)
                self.assertTrue(success,message)
                self.bot.market_mgr.update_prices(now=now+10,volatility=0)
                self.assertEqual(self.bot.market_mgr.get_asset_info(code)['price'],goal)
                self.assertTrue(self.bot.market_mgr.get_price_history(code,now))
        self.assertEqual(self.db.products.get_treasury_bond('INZTB001')['face_value'],1000)
        self.assertEqual(self.db.assets.execute_one("SELECT strike_price FROM option_contracts WHERE contract_code='INZOP001'")[0],1008)

    def test_national_put_collateral_order_cancellation_and_bank_account(self):
        self.seed()
        balance=self.db.users.get_treasury_balance()
        success,message=self.bot.option_mgr.create_and_write(TREASURY_USER_ID,'INZST001','put',1000,30,2,10,60)
        self.assertTrue(success,message)
        self.assertEqual(self.db.users.get_treasury_balance(),balance-2000)
        order=self.db.assets.execute_one("SELECT * FROM market_orders WHERE user_id=? AND asset_code LIKE 'OPT%'",(TREASURY_USER_ID,))
        success,message=self.bot.order_book.cancel_order(TREASURY_USER_ID,order['order_id'])
        self.assertTrue(success,message)
        self.assertEqual(self.db.users.get_treasury_balance(),balance)
        success,message=self.bot.bank_mgr.open_account(TREASURY_USER_ID,'INZSV001','pass123',1000)
        self.assertTrue(success,message)
        self.assertEqual(self.db.accounts.get_user_accounts(TREASURY_USER_ID)[0]['balance'],1000)

    def test_admin_scope_blocks_queued_transaction_after_role_revocation(self):
        with DBManager.admin_scope('admin'):
            self.db.users.update_role('admin','user')
            with self.assertRaises(ValueError):
                self.bot.fx_mgr.open_account('citizen','MISSING')
        with self.db.atomic() as conn:
            self.assertEqual(conn.execute("SELECT role FROM users WHERE user_id='admin'").fetchone()[0],'user')

    def test_zero_percent_event_cancels_previous_goal_and_clears_overlay(self):
        self.db.assets.register_asset('ZERO','취소','EX',1000,'commodity_spot')
        self.bot.admin_mgr.create_market_event('admin','good',50,'ZERO',60,now=1000)
        success,message=self.bot.admin_mgr.create_market_event('admin','good',0,'ZERO',60,now=1001)
        self.assertTrue(success,message)
        self.assertEqual(self.db.system.execute_one("SELECT COUNT(*) FROM market_event_targets WHERE state='active'")[0],0)

    def test_operator_can_create_each_supported_financial_product(self):
        self.seed()
        control=self.bot.admin_control
        for category in ('stock','asset','etf','future','corporatebond','fund','indexfund','commodity','bond','saving','loan','fxproduct'):
            with self.subTest(category=category):
                code='NEW_'+category.upper()
                result=control.entity('admin',category,'create',code,'새 '+category,'1000','1000000',EXAMPLES[category])
                self.assertIn('✅',result)
        control.entity('admin','asset','create','FX_NEW','새 통화','100','1000000',{'exchange_id':'FX','type':'forex'})
        control.entity('admin','currency','create','NEW','새 통화',extra={'asset_code':'FX_NEW','nationality':'새 국가'})
        self.assertEqual(self.db.fx.execute_one("SELECT currency_code FROM country_currencies WHERE nationality='새 국가'")[0],'NEW')

    def test_national_queries_submit_through_modals_and_graph_returns_file(self):
        self.seed()
        async def run():
            for command,values in [('전체자산확인',['확인']),('검색',['확인']),('그래프',['INZOP001']),('국적확인',['확인'])]:
                modal=ActionModal(self.bot,'admin',command)
                for field,value in zip(modal.fields,values):field._value=value
                interaction=SimpleNamespace(user=SimpleNamespace(id='admin'),
                    response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()))
                await modal.on_submit(interaction)
                interaction.followup.send.assert_awaited_once()
                if command=='그래프':
                    file=interaction.followup.send.call_args.kwargs['file']
                    self.assertEqual(file.filename,'movement.png')
                    self.assertTrue(file.fp.read().startswith(b'\x89PNG'))
                    file.close()
        with patch.object(
            PriceMovementView,
            'render',
            return_value=(discord.Embed(title='그래프'), b'\x89PNG\r\n\x1a\n'),
        ):
            asyncio.run(run())

    def test_treasury_assets_button_skips_dummy_modal_and_pages_all_holdings(self):
        self.seed()
        async def run():
            view=AdminCommandView(self.bot,'admin','내지갑')
            interaction=SimpleNamespace(user=SimpleNamespace(id='admin'),
                response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock(),send_modal=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()))
            await view.children[0].callback(interaction)
            interaction.response.send_modal.assert_not_awaited()
            sent=interaction.followup.send.call_args.kwargs
            self.assertTrue(sent['ephemeral'])
            self.assertIsInstance(sent['view'],TreasuryAssetsView)
            assets_view=sent['view']
            main=assets_view.build_embed()
            self.assertNotIn('INZST001',str(main.to_dict()))
            self.assertIn('총자산',[field.name for field in main.fields])
            assets_view.category='stocks'
            stock_count=len(assets_view.sections['stocks'])
            self.assertGreater(stock_count,0)
            shown=[]
            for page in range((stock_count+9)//10):
                assets_view.page=page
                embed=assets_view.build_embed()
                self.assertLessEqual(len(embed),6000)
                self.assertLessEqual(len(embed.fields),25)
                shown.extend(field.name for field in embed.fields)
            self.assertEqual(shown,[item[0][:120] for item in assets_view.items])
            self.assertTrue(await assets_view.interaction_check(interaction))
            self.db.users.update_role('admin','user')
            self.assertFalse(await assets_view.interaction_check(interaction))
            for command in READ_ONLY:
                self.assertEqual(ActionModal(self.bot,'admin',command).fields,[])
        asyncio.run(run())

    def test_long_national_query_results_fit_discord_content_limit(self):
        self.seed()
        async def run():
            interaction=SimpleNamespace(user=SimpleNamespace(id='admin'),
                response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()))
            await ActionModal(self.bot,'admin','국채목록').on_submit(interaction)
            sent=interaction.followup.send.call_args
            self.assertLessEqual(len(sent.kwargs['embed']),6000)
            self.assertLessEqual(len(sent.kwargs['embed'].description),4096)
            file=sent.kwargs['file']
            text=file.fp.read().decode('utf-8')
            self.assertIn('INZTB001',text)
            self.assertIn('INZTB050',text)
            file.close()
        asyncio.run(run())

    def test_linked_event_replacement_uses_displayed_overlay_and_currency_prefers_nationality(self):
        self.seed()
        now=int(time.time())
        self.db.assets.execute("INSERT INTO market_quote_overrides VALUES ('INZCB001',1400,?,'event')",(now,))
        success,message=self.bot.admin_mgr.create_market_event('admin','good',50,'INZCB001',10,now=now)
        self.assertTrue(success,message)
        target=self.db.system.execute_one("SELECT * FROM market_event_targets WHERE asset_code='INZCB001'")
        self.assertEqual((target['start_price'],target['target_price']),(1400,2100))
        registry=SlashCommandRegistry(self.bot)
        choices=asyncio.run(registry.currency_autocomplete(SimpleNamespace(user=SimpleNamespace(id='citizen')),''))
        self.assertEqual([c.value for c in choices[:2]],['USD','NWX'])

class LegacyMigrationTests(unittest.TestCase):
    def test_migration_preserves_data_sequences_triggers_and_legacy_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with closing(sqlite3.connect(root/'users.db')) as conn,conn:
                conn.execute('CREATE TABLE citizen_rows(id INTEGER PRIMARY KEY AUTOINCREMENT,cash INTEGER)')
                conn.execute('INSERT INTO citizen_rows(id,cash) VALUES (20,12345)')
                conn.execute('DELETE FROM citizen_rows')
                conn.execute('INSERT INTO citizen_rows(cash) VALUES (77)')
                conn.execute('CREATE INDEX idx_citizen_cash ON citizen_rows(cash)')
                conn.execute('CREATE TRIGGER preserve_cash BEFORE UPDATE ON citizen_rows WHEN NEW.cash<0 BEGIN SELECT RAISE(ABORT,\'negative\'); END')
            with closing(sqlite3.connect(root/'assets.db')) as conn,conn:
                conn.execute('CREATE TABLE price_samples(code TEXT,price INTEGER)')
                conn.execute('INSERT INTO price_samples VALUES (?,?)',('KEEP',500))
            path=migrate(root)
            with closing(sqlite3.connect(path)) as conn,conn:
                self.assertEqual(conn.execute('SELECT id,cash FROM citizen_rows').fetchone(),(21,77))
                self.assertEqual(conn.execute('SELECT * FROM price_samples').fetchone(),('KEEP',500))
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                with self.assertRaises(sqlite3.IntegrityError): conn.execute('UPDATE citizen_rows SET cash=-1')
                conn.execute('INSERT INTO citizen_rows(cash) VALUES (123)')
                self.assertEqual(conn.execute('SELECT MAX(id) FROM citizen_rows').fetchone()[0],22)
            self.assertTrue((root/'users.db').exists())
            self.assertEqual(migrate(root),path)

    def test_collision_never_publishes_partial_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('users','assets'):
                with closing(sqlite3.connect(root/f'{name}.db')) as conn,conn:
                    conn.execute('CREATE TABLE conflicting_rows(id INTEGER)')
            with self.assertRaises(RuntimeError):migrate(root)
            self.assertFalse((root/'inazina.db').exists())

if __name__=='__main__':unittest.main()
