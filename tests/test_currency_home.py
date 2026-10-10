import asyncio
import unittest
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
import test_inazina as fixtures
from database.user_db import TREASURY_USER_ID
from database.currency_db import ForeignExchangeDB
from managers.currency_manager import CurrencyManager, SCALE
from managers.admin_commands_manager import AdminCommandsManager
from functions.currency_display import native_summary
from functions.discord_views import InfoPanelView
import discord

class CurrencyHomeTests(unittest.TestCase):
    setUp=fixtures.InazinaTests.setUp
    tearDown=fixtures.InazinaTests.tearDown

    def configure(self):
        self.db.fx.execute("INSERT INTO currencies(code,name,reference_rate) VALUES ('AAA','A국 화폐','1')")
        cmd=AdminCommandsManager(self.bot)
        cmd.execute('admin','서버설치','123 A국 AAA')
        cmd.execute('admin','서버설치','낙원 NWX',guild_id=456)
        cmd.execute('admin','환율수정','NWX AAA 3')
        self.db.fx.execute("INSERT INTO country_currencies VALUES ('미국','AAA')")
        return cmd

    def test_registration_uses_server_and_existing_citizens_keep_nation_and_cash(self):
        self.configure();cm=CurrencyManager(self.db)
        self.assertTrue(cm.register_user('new','신규',123))
        self.assertEqual(self.db.users.get_user('new')['nationality'],'A국')
        self.assertEqual(self.db.users.get_user('new')['registration_guild_id'],'123')
        with self.db.atomic() as conn:
            self.assertEqual(cm.balance(conn,'new','AAA'),3_000_000*SCALE)
            self.assertEqual(cm.balance(conn,'new','NWX'),0)
        self.assertFalse(cm.register_user('new','다른 이름',456))
        self.assertEqual(self.db.users.get_user('new')['nationality'],'A국')
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_000_000)
        with self.assertRaisesRegex(ValueError,'등록 서버'):cm.register_user('dm','DM',None)
        self.assertIsNone(self.db.users.get_user('dm'))

    def test_income_native_credit_treasury_debit_and_quote_snapshot(self):
        cmd=self.configure()
        message=cmd.execute('admin','기본소득','10000 NWX')
        self.assertIn('✅',message)
        self.assertEqual(self.db.users.get_user('admin')['cash'],1_010_000)
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_000_000)
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),30_000*SCALE)
        self.assertEqual(self.db.users.get_treasury_balance(),9_980_000)
        rows=self.db.fx.execute('SELECT * FROM basic_income_payments')
        self.assertEqual(len(rows),2)
        self.assertEqual({r['source_currency'] for r in rows},{'NWX'})
        self.assertEqual(len({r['batch_id'] for r in rows}),1)
        cmd.execute('admin','환율수정','NWX AAA 4')
        self.assertEqual(self.db.fx.execute_one("SELECT target_units FROM basic_income_payments WHERE user_id='citizen'")[0],30_000*SCALE)

    def test_missing_quote_rolls_back_all_recipients_and_budget(self):
        self.configure()
        self.db.fx.execute("INSERT INTO currencies(code,name) VALUES ('BAD','환율 없음')")
        self.db.fx.execute("UPDATE country_currencies SET currency_code='BAD' WHERE nationality='미국'")
        before=self.db.users.get_treasury_balance()
        ok,msg=self.bot.admin_mgr.distribute_basic_income(10000,currency_code='NWX')
        self.assertFalse(ok,msg)
        self.assertEqual(self.db.users.get_treasury_balance(),before)
        self.assertEqual(self.db.users.get_user('admin')['cash'],1_000_000)
        self.assertEqual(self.db.fx.execute_one('SELECT COUNT(*) FROM basic_income_payments')[0],0)

    def test_default_income_and_admin_balance_use_actors_home_currency(self):
        cmd=self.configure()
        self.db.users.execute("UPDATE users SET nationality='A국' WHERE user_id='admin'")
        self.assertIn('AAA',cmd.execute('admin','기본소득','10000'))
        cmd.execute('admin','잔고수정','citizen modify 5.5')
        cmd.execute('admin','잔고수정','citizen add 2')
        cmd.execute('admin','잔고수정','citizen del 1')
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),6_500_000)
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_000_000)

    def test_wallet_and_search_classify_nwx_as_foreign_and_value_in_home(self):
        cmd=self.configure();cmd.execute('admin','기본소득','10000 NWX')
        info=self.bot.user_mgr.get_total_assets('citizen',settle=False)
        self.assertEqual(info['national_currency'],'AAA')
        self.assertEqual(info['home_cash_units'],30_000*SCALE)
        self.assertEqual(info['foreign_cash_units'],3_000_000*SCALE)
        self.assertTrue(next(r for r in info['wallets'] if r['currency_code']=='NWX')['is_foreign'])
        embed=discord.Embed();embed.add_field(name='💵 현금',value='```0 NWX```')
        native_summary(embed,info)
        self.assertEqual(embed.fields[0].value,'```30,000.000000 AAA```')
        async def panel():
            view=InfoPanelView(self.bot,'citizen')
            return view._category_lines('forex')
        lines=asyncio.run(panel())
        self.assertTrue(any('1 AAA' in line and 'NWX' in line for line in lines),lines)
        self.assertFalse(any('(AAA)' in line for line in lines))

    def test_native_wallet_exchange_transfer_and_source_wallet_preserve_denominations(self):
        cmd=self.configure();cmd.execute('admin','기본소득','10000 NWX')
        self.db.system.set_setting('currency_exchange_fee_percent','0')
        msg=self.bot.currency_mgr.transfer('citizen','admin','300')
        self.assertIn('300.000000 AAA → 100 NWX',msg)
        self.assertEqual(self.db.users.get_user('admin')['cash'],1_010_100)
        self.bot.currency_mgr.exchange('citizen','AAA','NWX','300')
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_000_100)
        self.bot.currency_mgr.transfer('citizen','admin','10','__wallet__:NWX')
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_000_090)
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),29_400*SCALE)

    def test_migration_is_idempotent_and_all_product_denominations_present(self):
        self.configure();before=self.db.users.get_user('citizen')['cash']
        fx=ForeignExchangeDB();fx.close()
        self.assertEqual(self.db.users.get_user('citizen')['cash'],before)
        for table in ('company','investment','market_assets','saving','liability','treasury_bond_products','option_contracts','accounts','loans'):
            self.assertIn('quote_currency',{r['name'] for r in self.db.fx.execute(f'PRAGMA table_info({table})')})

    def test_dynamic_currency_quote_keeps_event_price_movement(self):
        cmd=self.configure()
        self.db.assets.register_asset('FX_T','시세 외환','FX',100,'forex')
        self.db.fx.execute("INSERT INTO currencies(code,name,asset_code) VALUES ('TST','시세 화폐','FX_T')")
        cmd.execute('admin','환율수정','NWX TST 3')
        with self.db.atomic() as conn:
            self.assertAlmostEqual(float(CurrencyManager.rate(conn,'NWX')/CurrencyManager.rate(conn,'TST')),3)
        self.db.assets.update_price('FX_T',200)
        with self.db.atomic() as conn:
            self.assertAlmostEqual(float(CurrencyManager.rate(conn,'NWX')/CurrencyManager.rate(conn,'TST')),1.5)

    def test_currency_precision_controls_settlement_input_and_relative_text(self):
        cmd=self.configure()
        self.db.fx.execute("UPDATE currencies SET decimal_places=2 WHERE code='AAA'")
        self.db.fx.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('KRW','원','1/1000',0)")
        from functions.currency_display import MoneyDisplay
        display=MoneyDisplay(self.db,'citizen')
        self.assertEqual(display.relative(10),'30.00 AAA')
        self.assertEqual(display.text('현금 10 NWX · 외화 1.2 AAA'),'현금 30.00 AAA · 외화 1.20 AAA')
        cmd.execute('admin','잔고수정','citizen modify 10.55')
        with self.assertRaisesRegex(ValueError,'2자리'):
            cmd.execute('admin','잔고수정','citizen modify 10.555')
        self.db.system.set_setting('currency_exchange_fee_percent','0')
        self.bot.currency_mgr.exchange('citizen','AAA','KRW','0.01')
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','KRW'),3*SCALE)
        with self.assertRaisesRegex(ValueError,'0자리'):
            self.bot.currency_mgr.exchange('citizen','KRW','AAA','0.1')

    def test_native_stock_order_and_cancel_use_home_cash_not_nwx(self):
        cmd=self.configure();cmd.execute('admin','기본소득','10000 NWX')
        self.bot.admin_control.entity('admin','stock','create','AAA_ST','A국 주식',100,1000,{'quote_currency':'AAA','exchange_id':'A'})
        ok,msg=self.bot.order_book.place_limit_order('citizen','AAA_ST','buy',5,100)
        self.assertTrue(ok,msg)
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),29_500*SCALE)
            self.assertEqual(CurrencyManager.balance(conn,TREASURY_USER_ID,'AAA'),500*SCALE)
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_000_000)
        ok,msg=self.bot.order_book.place_limit_order('citizen','AAA_ST','buy',5,50)
        self.assertTrue(ok,msg)
        order=self.db.assets.execute_one("SELECT order_id FROM market_orders WHERE user_id='citizen' AND status='open'")[0]
        ok,msg=self.bot.order_book.cancel_order('citizen',order)
        self.assertTrue(ok,msg)
        self.assertIn('AAA',msg)
        with self.db.atomic() as conn:self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),29_500*SCALE)

    def test_personal_response_and_pagination_display_relative_currency(self):
        self.configure()
        from functions.currency_display import MoneyDisplay
        from functions.slash_commands import SlashCommandRegistry
        from functions.discord_views import AllAssetsView
        from unittest.mock import AsyncMock
        display=MoneyDisplay(self.db,'citizen')
        response=SimpleNamespace(is_done=lambda:False,send_message=AsyncMock())
        interaction=SimpleNamespace(response=response,extras={'money_display':display},command=SimpleNamespace(name='대출상환'))
        embed=discord.Embed(description='현금 100 NWX')
        asyncio.run(SlashCommandRegistry._reply(interaction,embed=embed))
        shown=response.send_message.call_args.kwargs['embed']
        self.assertEqual(shown.description,'현금 300.000000 AAA')
        from functions.currency_display import relative_embed
        class Page:
            money_display=display
            @relative_embed
            def build_embed(self):return discord.Embed(description='평가액 100 NWX')
        self.assertEqual(Page().build_embed().description,'평가액 300.000000 AAA')

    def test_native_currency_deposit_is_a_home_account_not_foreign_account(self):
        self.configure()
        self.db.fx.execute("INSERT INTO currency_saving_products(product_code,name,currency_code,annual_rate) VALUES ('A_SAVE','A국 예금','AAA',0)")
        number=self.bot.currency_mgr.open_account('citizen','A_SAVE')
        self.db.fx.execute('UPDATE currency_saving_accounts SET balance_units=? WHERE account_number=?',(100*SCALE,number))
        info=self.bot.user_mgr.get_total_assets('citizen',settle=False)
        self.assertEqual(info['home_account_total_home'],100)
        self.assertEqual(info['foreign_account_total_home'],0)
        embed=discord.Embed();embed.add_field(name='🏦 통장 잔액',value='```0 NWX```')
        native_summary(embed,info)
        self.assertEqual(embed.fields[0].value,'```100.000000 AAA```')

if __name__=='__main__':unittest.main()
