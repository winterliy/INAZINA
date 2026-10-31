import asyncio
import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_inazina as fixtures
from database.user_db import TREASURY_USER_ID
from functions.admin_prefix import ActionModal
from functions.discord_views import MyOrdersView
from functions.quantities import parse_quantity
from functions.slash_commands import SlashCommandRegistry
from functions.technical_indicators import build_candles
from managers.admin_commands_manager import AdminCommandsManager, creation_usage
from managers.corporate_actions_manager import CorporateActionsManager
from managers.currency_manager import CurrencyManager, SCALE


class FractionalTradingTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def command(self, name, args, actor='admin'):
        return AdminCommandsManager(self.bot).execute(actor, name, args)

    def stock(self, code='ST', price=100, supply=10):
        self.command('상품생성', f'{code} company 주식 게임 100000 {supply} {price} EX 1')

    def holding(self, user, code):
        return self.db.assets.execute_one('SELECT COALESCE(SUM(quantity),0) FROM user_assets WHERE user_id=? AND asset_code=?', (user, code))[0]

    def test_eligibility_flags_creation_and_disable_guards(self):
        self.stock()
        self.command('상품생성', 'PR preferred 우선주 ST 100000 10 100 EX 1일 10 1')
        self.command('상품생성', 'ETF etf ETF 100 10 EX 1 86400 EX 1')
        self.command('상품생성', 'MF fund 펀드 100 10 ST:1 EX 1')
        self.command('상품생성', 'IF indexfund 인덱스 100 10 EX EX 1')
        self.command('상품생성', 'GOLD commodity 금 100 10 EX 1')
        for code in ('ST', 'PR', 'ETF', 'MF', 'IF', 'GOLD'):
            self.assertEqual(self.bot.market_mgr.get_asset_info(code)['Fractal'], 1)
        self.command('상품생성', 'OFF company 정수주 게임 100000 10 100 EX')
        self.assertEqual(self.db.companies.get_company('OFF')['Fractal'], 0)
        self.assertFalse(self.bot.order_book.place_limit_order('citizen', 'OFF', 'buy', '0.5', 100)[0])
        self.assertTrue(self.bot.order_book.place_limit_order('citizen', 'OFF', 'buy', '1', 100)[0])
        with self.assertRaises(ValueError):
            self.command('소수점거래관리', 'OFF 0', actor='citizen')
        self.command('소수점거래관리', 'OFF 0')
        self.assertEqual(self.db.companies.get_company('OFF')['Fractal'], 1)
        self.command('소수점거래관리', 'OFF 1')
        self.assertEqual(self.db.companies.get_company('OFF')['Fractal'], 0)
        for kind in ('stock_future', 'corporate_bond', 'els'):
            code = kind.upper()
            self.db.products.register_investment(code, code, kind, 100, 10, 0, '국가', 'EX')
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.command('소수점거래관리', f'{code} 0')
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.products.execute('UPDATE investment SET Fractal=1 WHERE product_code=?', (code,))
        self.db.assets.register_asset('FUT', '선물', 'EX', 100, 'commodity_futures')
        with self.assertRaises(ValueError):
            self.command('소수점거래관리', 'FUT 0')
        self.assertTrue(self.bot.order_book.place_limit_order('citizen', 'ST', 'buy', '.5', 100)[0])
        with self.assertRaises(ValueError):
            self.command('소수점거래관리', 'ST 1')
        for invalid in ('0', '-1', 'nan', 'inf', '.0001', '1000000000000.001'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_quantity(invalid, fractional=True)
        self.assertIn('Fractal', creation_usage('company'))

    def test_partial_orders_refund_cash_and_preserve_wallet_chart_volume(self):
        self.stock()
        self.db.assets.add_user_asset('admin', 'ST', .5, unit_cost=100)
        buy = self.bot.order_book.place_limit_order('citizen', 'ST', 'buy', '.75', 99)
        self.assertTrue(buy[0], buy[1])
        order = self.db.assets.execute_one("SELECT * FROM market_orders WHERE user_id='citizen'")
        self.assertEqual(order['reserved_cash_units'], 75*SCALE)
        self.assertTrue(self.bot.order_book.place_limit_order('admin', 'ST', 'sell', '.2', 99)[0])
        self.assertEqual(self.holding('citizen', 'ST'), .2)
        self.assertEqual(self.holding('admin', 'ST'), .3)
        self.assertFalse(self.bot.order_book.place_limit_order('admin', 'ST', 'sell', '.4', 99)[0])
        active = self.bot.order_book.get_user_orders('citizen')
        async def view():
            panel = MyOrdersView(self.bot, 'citizen', active)
            return panel.build_embed(active)
        self.assertIn('0.2', asyncio.run(view()).description)
        self.assertTrue(self.bot.order_book.cancel_order('citizen', order['order_id'])[0])
        self.assertEqual(self.db.users.get_user('citizen')['cash'], 999980)
        self.assertEqual(self.db.users.get_user('admin')['cash'], 1000019)
        self.assertEqual(self.db.users.get_treasury_balance(), 10000001)
        self.assertEqual(self.bot.user_mgr.get_total_assets('citizen')['stocks'][0]['qty'], .2)
        now = int(time.time())
        self.assertEqual(self.bot.market_mgr.get_realtime_rankings('volume', 'admin', now=now)[0]['volume'], .2)
        self.assertEqual(sum(q for _, q in self.bot.market_mgr.get_exchange_trade_history('EX', now-60)), .2)
        candles = build_candles([(now, 99)], [(now, .2)], 60)
        self.assertEqual(candles[0]['volume'], .2)
        with self.assertRaises(ValueError):
            self.command('소수점거래관리', 'ST 1')

    def test_funds_dividends_and_fractional_share_collateral(self):
        self.stock(supply=100)
        self.command('상품생성', 'MF fund 펀드 100 100 ST:1 EX 1')
        self.assertTrue(self.bot.investment_mgr.trade_fund('citizen', 'MF', '.5')[0])
        self.assertEqual(self.holding('citizen', 'MF'), .5)
        self.assertTrue(self.bot.investment_mgr.trade_fund('citizen', 'MF', '.2', redeem=True)[0])
        self.assertEqual(self.holding('citizen', 'MF'), .3)
        self.assertEqual(self.db.users.get_user('citizen')['cash'], 999970)
        self.assertTrue(self.bot.order_book.place_limit_order('citizen', 'ST', 'buy', '1.5', 100)[0])
        manager = CorporateActionsManager(self.bot)
        before = self.db.users.get_user('citizen')['cash']
        result = manager.special_dividend('admin', 'ST', 10, delay=0)
        self.assertTrue(result[0], result[1])
        self.assertEqual(self.db.users.get_user('citizen')['cash']-before, 15)
        result = self.bot.option_mgr.create_and_write('citizen', 'ST', 'call', 100, 1, 1, 10)
        self.assertTrue(result[0], result[1])
        self.assertEqual(self.holding('citizen', 'ST'), .5)
        code = self.bot.option_mgr.get_active_contracts('ST')[0]['contract_code']
        self.assertFalse(self.bot.order_book.place_limit_order('admin', code, 'buy', '.5', 10)[0])
        self.assertTrue(self.bot.order_book.place_limit_order('admin', code, 'buy', '1', 10)[0])
        self.db.companies.update_share_price('ST', 90)
        self.db.assets.execute('UPDATE option_contracts SET expiration_epoch=?', (int(time.time())-1,))
        self.bot.option_mgr.expire_matured()
        self.assertEqual(self.holding('citizen', 'ST'), 1.5)

    def test_foreign_currency_minor_units_and_national_trade(self):
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','달러','1500',2)")
            conn.execute("INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','USD')")
            conn.execute("UPDATE users SET nationality='낙원' WHERE user_id='citizen'")
            conn.execute("INSERT INTO accounts(account_number,user_id,product_code,account_type,quote_currency,balance_units) VALUES ('USD_ACCOUNT','citizen','USD_SAVE','saving','USD',0)")
            CurrencyManager.change(conn, 'citizen', 'USD', 10*SCALE)
        self.stock(price=1)
        self.command('상품생성', 'US company 미국주식 게임 100000 10 1 EX 1 {"quote_currency":"USD"}')
        self.assertTrue(self.bot.order_book.place_limit_order('citizen', 'US', 'buy', '.005', 1)[0])
        self.assertEqual(self.db.accounts.execute_one("SELECT balance_units FROM accounts WHERE account_number='USD_ACCOUNT'")[0], 9990000)
        self.assertEqual(self.holding('citizen', 'US'), .005)
        async def national():
            return ActionModal(self.bot, 'admin', '매수').execute(['ST', '.5', '1'])
        self.assertIn('✅', asyncio.run(national()))
        self.assertEqual(self.holding(TREASURY_USER_ID, 'ST'), .5)
        self.assertEqual(self.db.users.get_user('admin')['cash'], 1000000)

    def test_thousandth_fills_round_once_and_multilot_option_delivery(self):
        self.stock(price=1, supply=1)
        self.db.assets.add_user_asset('admin', 'ST', 1, unit_cost=1)
        self.assertTrue(self.bot.order_book.place_limit_order('citizen', 'ST', 'buy', '1', 1)[0])
        for qty in ('.333', '.333', '.334'):
            self.assertTrue(self.bot.order_book.place_limit_order('admin', 'ST', 'sell', qty, 1)[0])
        self.assertEqual(self.holding('citizen', 'ST'), 1)
        self.assertEqual(self.holding('admin', 'ST'), 0)
        self.assertEqual(self.db.users.get_user('citizen')['cash'], 999999)
        self.assertEqual(self.db.users.get_treasury_balance(), 10000001)
        self.stock('LOT', supply=10)
        for account in ('A', 'B'):
            self.db.accounts.create_account(account, 'citizen', 'TEST'+account, 0, '1234')
            self.db.assets.add_user_asset('citizen', 'LOT', .5, account_number=account, unit_cost=100)
        result = self.bot.option_mgr.create_and_write('citizen', 'LOT', 'call', 100, 1, 1, 10)
        self.assertTrue(result[0], result[1])
        code = self.bot.option_mgr.get_active_contracts('LOT')[0]['contract_code']
        self.assertTrue(self.bot.order_book.place_limit_order('admin', code, 'buy', 1, 10)[0])
        self.db.companies.update_share_price('LOT', 120)
        self.db.assets.execute('UPDATE option_contracts SET expiration_epoch=? WHERE contract_code=?', (int(time.time())-1, code))
        self.bot.option_mgr.expire_matured()
        self.assertEqual(self.holding('admin', 'LOT'), 1)
        self.assertEqual(self.holding('citizen', 'LOT'), 0)

    def test_fractional_foreign_order_accepts_decimal_quote_price(self):
        self.stock()
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) "
                         "VALUES ('USD','달러','1500',2)")
            conn.execute("INSERT INTO country_currencies(nationality,currency_code) "
                         "VALUES ('미국','USD')")
            conn.execute("UPDATE users SET nationality='낙원' WHERE user_id='citizen'")
            conn.execute("INSERT INTO accounts(account_number,user_id,product_code,account_type,"
                         "quote_currency,balance_units) VALUES ('USD_ACCOUNT','citizen','USD_SAVE',"
                         "'saving','USD',0)")
            CurrencyManager.change(conn, 'citizen', 'USD', 10*SCALE)
        self.command('상품생성', 'US company 미국주식 게임 100000 10 1 EX 1 {"quote_currency":"USD"}')
        self.db.companies.execute(
            "UPDATE company SET share_price=1.25 WHERE company_code='US'"
        )
        ok, message = self.bot.order_book.place_limit_order(
            'citizen', 'US', 'buy', '.008', '1.25'
        )
        self.assertTrue(ok, message)
        holding = self.db.assets.execute_one(
            "SELECT quantity,average_cost_minor_units FROM user_assets "
            "WHERE user_id='citizen' AND asset_code='US'"
        )
        self.assertEqual(tuple(holding), (.008, 125))
        self.assertEqual(self.db.accounts.execute_one(
            "SELECT balance_units FROM accounts WHERE account_number='USD_ACCOUNT'"
        )[0], 9_990_000)

    def test_fractional_preferred_claim_and_merger_cash(self):
        self.stock()
        self.command('상품생성', 'PR preferred 우선주 ST 100000 10 100 EX 1 {"dividend_funding":"issuer","liquidation_value_per_share":1}')
        self.db.assets.add_user_asset('citizen', 'PR', .5, unit_cost=100)
        CorporateActionsManager(self.bot).fund_issuer('admin', 'ST', 'add', 1)
        result = CorporateActionsManager(self.bot).liquidate('admin', 'ST', 'fractional-preference')
        self.assertTrue(result[0], result[1])
        self.assertEqual(self.db.corporate.execute_one("SELECT quantity FROM corporate_liquidation_payments WHERE asset_code='PR'")[0], .5)
        self.assertEqual(self.db.users.get_user('citizen')['cash'], 1000001)
        self.stock('A', price=100)
        self.stock('B', price=100)
        self.db.assets.add_user_asset('citizen', 'B', .5, unit_cost=100)
        before = self.db.users.get_user('citizen')['cash']
        result = CorporateActionsManager(self.bot).merge('admin', 'A', 'B')
        self.assertTrue(result[0], result[1])
        self.assertEqual(self.db.users.get_user('citizen')['cash']-before, 50)
        self.assertEqual(self.holding('citizen', 'B'), 0)

    def test_fractional_shareholder_meeting_dm_and_bonus_allocation(self):
        holder, actor = '111111111111111111', '222222222222222222'
        self.db.users.create_user(holder, '소수점 주주', '낙원', 10000)
        self.db.users.create_user(actor, '관리자', '낙원', 10000, 'admin')
        self.stock(supply=2)
        self.assertTrue(self.bot.order_book.place_limit_order(holder, 'ST', 'buy', '.5', 100)[0])
        target = SimpleNamespace(send=AsyncMock())
        self.bot.get_user = Mock(return_value=target)
        self.bot.fetch_user = AsyncMock()
        registry = SlashCommandRegistry(self.bot)
        interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))
        # A 0.5-share largest shareholder can call the meeting and receives a DM.
        with patch.object(registry, '_prepare', AsyncMock(return_value=(holder, {}, 'user'))):
            asyncio.run(registry.shareholder_meeting(interaction, 'ST', '온라인'))
        target.send.assert_awaited_once()
        self.bot.get_user.assert_called_with(int(holder))
        self.assertIn('1명 성공', interaction.followup.send.call_args.kwargs['content'])
        self.db.assets.add_user_asset(TREASURY_USER_ID, 'ST', 1, unit_cost=100)
        target.send.reset_mock()
        async def national_meeting():
            await ActionModal(self.bot, actor, '주총').national_meeting(interaction, ['ST', '온라인'])
        asyncio.run(national_meeting())
        target.send.assert_awaited_once()
        result = CorporateActionsManager(self.bot).increase_capital('admin', '무상', 'ST', 2)
        self.assertTrue(result[0], result[1])
        self.assertEqual(self.holding(holder, 'ST'), 1)
        self.assertEqual(self.holding(TREASURY_USER_ID, 'ST'), 2.5)
