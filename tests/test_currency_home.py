import asyncio
import unittest
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
import test_inazina as fixtures
from database.user_db import TREASURY_USER_ID
from database.currency_db import CurrencyDB
from managers.currency_manager import CurrencyManager, SCALE
from managers.admin_commands_manager import AdminCommandsManager
from functions.currency_display import native_summary
from functions.discord_views import InfoPanelView
import discord

class CurrencyHomeTests(unittest.TestCase):
    setUp=fixtures.InazinaTests.setUp
    tearDown=fixtures.InazinaTests.tearDown

    def configure(self):
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate) VALUES ('AAA','A국 화폐','1')")
        cmd=AdminCommandsManager(self.bot)
        cmd.execute('admin','서버설치','123 A국 AAA')
        cmd.execute('admin','서버설치','낙원 NWX',guild_id=456)
        cmd.execute('admin','환율수정','NWX AAA 3')
        self.db.currency.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','AAA')")
        self.db.currency.execute("INSERT OR IGNORE INTO currency_liquidity_state(currency_code) SELECT code FROM currencies")
        self.db.currency.execute('UPDATE currency_liquidity_state SET issuance_elasticity=0,circulation_elasticity=0')
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
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_030_000)
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),1_030_000*SCALE)
        self.assertEqual(self.db.users.get_treasury_balance(),9_980_000)
        rows=self.db.currency.execute('SELECT * FROM basic_income_payments')
        self.assertEqual(len(rows),2)
        self.assertEqual({r['source_currency'] for r in rows},{'NWX'})
        self.assertEqual(len({r['batch_id'] for r in rows}),1)
        cmd.execute('admin','환율수정','NWX AAA 4')
        self.assertEqual(self.db.currency.execute_one("SELECT target_units FROM basic_income_payments WHERE user_id='citizen'")[0],30_000*SCALE)

    def test_missing_quote_rolls_back_all_recipients_and_budget(self):
        self.configure()
        self.db.currency.execute("INSERT INTO currencies(code,name) VALUES ('BAD','환율 없음')")
        self.db.currency.execute("UPDATE country_currencies SET currency_code='BAD' WHERE nationality='미국'")
        before=self.db.users.get_treasury_balance()
        ok,msg=self.bot.admin_mgr.distribute_basic_income(10000,currency_code='NWX')
        self.assertFalse(ok,msg)
        self.assertEqual(self.db.users.get_treasury_balance(),before)
        self.assertEqual(self.db.users.get_user('admin')['cash'],1_000_000)
        self.assertEqual(self.db.currency.execute_one('SELECT COUNT(*) FROM basic_income_payments')[0],0)

    def test_default_income_and_admin_balance_use_actors_home_currency(self):
        cmd=self.configure()
        self.db.users.execute("UPDATE users SET nationality='A국' WHERE user_id='admin'")
        self.assertIn('AAA',cmd.execute('admin','기본소득','10000'))
        cmd.execute('admin','잔고수정','citizen modify 5.5')
        cmd.execute('admin','잔고수정','citizen add 2')
        cmd.execute('admin','잔고수정','citizen del 1')
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),6_500_000)
        self.assertEqual(self.db.users.get_user('citizen')['cash'],6.5)

    def test_wallet_and_search_classify_nwx_as_foreign_and_value_in_home(self):
        cmd=self.configure();cmd.execute('admin','기본소득','10000 NWX')
        info=self.bot.user_mgr.get_total_assets('citizen',settle=False)
        self.assertEqual(info['national_currency'],'AAA')
        self.assertEqual(info['home_cash_units'],1_030_000*SCALE)
        self.assertEqual(info['foreign_cash_units'],0)
        self.assertFalse(any(r['currency_code']=='NWX' for r in info['wallets']))
        embed=discord.Embed();embed.add_field(name='💵 현금',value='```0 NWX```')
        native_summary(embed,info)
        self.assertEqual(embed.fields[0].value,'```1,030,000.000000 AAA```')
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
        self.assertIn('300 AAA → 100 NWX',msg)
        self.assertEqual(self.db.users.get_user('admin')['cash'],1_010_100)
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind) VALUES ('NWX_SAVE','NWX 예금',0,'NWX','currency_saving')")
        number = self.bot.currency_mgr.open_account('citizen','NWX_SAVE')
        self.bot.currency_mgr.exchange('citizen','AAA','NWX','300')
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_029_400)
        self.bot.currency_mgr.transfer('citizen','admin','10',number)
        self.assertEqual(self.db.accounts.get_account(number)['balance_units'],90*SCALE)
        with self.db.atomic() as conn:
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),1_029_400*SCALE)

    def test_migration_is_idempotent_and_all_product_denominations_present(self):
        self.configure();before=self.db.users.get_user('citizen')['cash']
        fx=CurrencyDB();fx.close()
        self.assertEqual(self.db.users.get_user('citizen')['cash'],before)
        for table in ('company','investment','market_assets','saving','liability','treasury_bond_products','accounts','loans'):
            self.assertIn('quote_currency',{r['name'] for r in self.db.currency.execute(f'PRAGMA table_info({table})')})

    def test_dynamic_currency_quote_keeps_event_price_movement(self):
        cmd=self.configure()
        self.db.assets.register_asset('CUR_T','시세 외환','FX',100,'forex')
        self.db.currency.execute("INSERT INTO currencies(code,name,asset_code) VALUES ('TST','시세 화폐','CUR_T')")
        cmd.execute('admin','환율수정','NWX TST 3')
        with self.db.atomic() as conn:
            self.assertAlmostEqual(float(CurrencyManager.rate(conn,'NWX')/CurrencyManager.rate(conn,'TST')),3)
        self.db.assets.update_price('CUR_T',200)
        with self.db.atomic() as conn:
            self.assertAlmostEqual(float(CurrencyManager.rate(conn,'NWX')/CurrencyManager.rate(conn,'TST')),1.5)

    def test_currency_precision_controls_settlement_input_and_relative_text(self):
        cmd=self.configure()
        self.db.currency.execute("UPDATE currencies SET decimal_places=2 WHERE code='AAA'")
        self.db.currency.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('KRW','원','1/1000',0)")
        from functions.currency_display import MoneyDisplay
        display=MoneyDisplay(self.db,'citizen')
        self.assertEqual(display.relative(10),'30.00 AAA')
        self.assertEqual(display.text('현금 10 NWX · 외화 1.2 AAA'),'현금 30.00 AAA · 외화 1.20 AAA')
        cmd.execute('admin','잔고수정','citizen modify 10.55')
        with self.assertRaisesRegex(ValueError,'2자리'):
            cmd.execute('admin','잔고수정','citizen modify 10.555')
        self.db.system.set_setting('currency_exchange_fee_percent','0')
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind) VALUES ('KRW_SAVE','원 예금',0,'KRW','currency_saving')")
        self.bot.currency_mgr.open_account('citizen','KRW_SAVE')
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
            self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),1_029_500*SCALE)
            self.assertEqual(CurrencyManager.balance(conn,TREASURY_USER_ID,'AAA'),500*SCALE)
        self.assertEqual(self.db.users.get_user('citizen')['cash'],1_029_500)
        ok,msg=self.bot.order_book.place_limit_order('citizen','AAA_ST','buy',5,50)
        self.assertTrue(ok,msg)
        order=self.db.assets.execute_one("SELECT order_id FROM market_orders WHERE user_id='citizen' AND status='open'")[0]
        ok,msg=self.bot.order_book.cancel_order('citizen',order)
        self.assertTrue(ok,msg)
        self.assertIn('AAA',msg)
        with self.db.atomic() as conn:self.assertEqual(CurrencyManager.balance(conn,'citizen','AAA'),1_029_500*SCALE)

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
        # Contract details retain their actual denomination; summaries convert explicitly.
        self.assertEqual(shown.description,'현금 100 NWX')
        from functions.currency_display import relative_embed
        class Page:
            money_display=display
            @relative_embed
            def build_embed(self):return discord.Embed(description='평가액 100 NWX')
        self.assertEqual(Page().build_embed().description,'평가액 300.000000 AAA')

    def test_native_currency_deposit_is_a_home_account_not_foreign_account(self):
        self.configure()
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind) VALUES ('A_SAVE','A국 예금',0,'AAA','currency_saving')")
        with self.assertRaisesRegex(ValueError,'국적 통화'):
            self.bot.currency_mgr.open_account('citizen','A_SAVE')
        self.db.products.register_saving('HOME_SAVE','국적 통화 예금',0,0,0,86400,0,'Bank')
        self.db.products.execute("UPDATE saving SET quote_currency='AAA' WHERE product_code='HOME_SAVE'")
        self.assertTrue(self.bot.bank_mgr.open_account('citizen','HOME_SAVE','pass123',100)[0])
        info=self.bot.user_mgr.get_total_assets('citizen',settle=False)
        self.assertEqual(info['home_account_total_home'],100)
        self.assertEqual(info['foreign_account_total_home'],0)
        embed=discord.Embed();embed.add_field(name='🏦 통장 잔액',value='```0 NWX```')
        native_summary(embed,info)
        self.assertEqual(embed.fields[0].value,'```100.000000 AAA```')

    def test_product_edit_tells_savings_and_currency_accounts_apart(self):
        cmd=self.configure()
        cmd.execute('admin','상품생성','SV saving "새 적금" 3 1000 30 86400 1 Inazina')
        self.assertIn('✅',cmd.execute('admin','상품수정','SV saving "새 적금" 4 1000 30 86400 1 Inazina'))
        self.assertEqual(self.db.products.execute_one("SELECT interest_rate FROM saving WHERE product_code='SV'")[0],4)
        self.db.products.execute("INSERT INTO saving(product_code,name,interest_rate,quote_currency,product_kind) VALUES ('FX_SV','A 외환통장',1,'AAA','currency_saving')")
        with self.db.atomic() as conn:
            self.assertEqual(cmd.locate(conn,'SV')[0],'saving')
            self.assertEqual(cmd.locate(conn,'FX_SV')[0],'currencyproduct')

if __name__=='__main__':unittest.main()
