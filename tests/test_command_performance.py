import asyncio
import threading
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import test_inazina as fixtures
from functions.currency_display import MoneyDisplay
from functions.price_chart import render_price_chart, _cached_price_chart
from functions.slash_commands import SlashCommandRegistry


class CommandPerformanceTests(unittest.TestCase):
    setUp=fixtures.InazinaTests.setUp
    tearDown=fixtures.InazinaTests.tearDown

    def test_reader_sees_committed_cash_while_writer_is_pending(self):
        entered,release=threading.Event(),threading.Event()
        old=self.db.users.get_user('citizen')['cash']
        def writer():
            with self.db.atomic() as conn:
                conn.execute("UPDATE users SET cash=cash+100 WHERE user_id='citizen'")
                entered.set()
                self.assertTrue(release.wait(5))
        with ThreadPoolExecutor(max_workers=2) as pool:
            future=pool.submit(writer)
            self.assertTrue(entered.wait(2))
            try:
                # A read must finish while the writer is still holding its lock.
                value=pool.submit(self.bot.user_mgr.get_total_assets,'citizen',False).result(timeout=2)
                self.assertEqual(value['cash'],old)
                display=pool.submit(MoneyDisplay,self.db,'citizen').result(timeout=2)
                self.assertEqual(display.relative_currency,'NWX')
            finally:release.set()
            future.result(timeout=2)
        self.assertEqual(self.db.users.get_user('citizen')['cash'],old+100)
        with self.db.readonly() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE users SET cash=0 WHERE user_id='citizen'")

    def test_wallet_valuation_keeps_duplicate_protection_and_external_edits_live(self):
        for i in range(50):
            self.db.assets.register_asset(f'PF{i}',f'상품{i}','PERF',100,'stock')
            self.db.assets.add_user_asset('citizen',f'PF{i}',2)
        info=self.bot.user_mgr.get_total_assets('citizen',settle=False)
        self.assertEqual(info['stock_total'],10000)
        self.db.assets.update_price('PF0',150)
        self.assertEqual(self.bot.user_mgr.get_total_assets('citizen',settle=False)['stock_total'],10100)
        self.db.products.register_saving('PF0','중복',0,0,0,86400,0,'은행')
        info=self.bot.user_mgr.get_total_assets('citizen',settle=False)
        self.assertEqual(info['stock_total'],9800)
        self.assertEqual(next(row['name'] for row in info['stocks'] if row['code']=='PF0'),'확인 필요')

    def test_search_does_not_settle_interest(self):
        async def run():
            self.bot.interest_mgr=SimpleNamespace(process_user_interests=AsyncMock())
            registry=SlashCommandRegistry(self.bot)
            registry._prepare=AsyncMock(return_value=('citizen',None,'user'))
            registry._reply=AsyncMock()
            await registry.search(SimpleNamespace())
            self.bot.interest_mgr.process_user_interests.assert_not_called()
            self.assertEqual(registry._reply.call_args.kwargs['embed'].title,'ℹ️ 메인')
        asyncio.run(run())

    def test_chart_cache_includes_price_window_trades_and_contract_levels(self):
        _cached_price_chart.cache_clear()
        args=dict(points=[(100,100),(160,110)],period_seconds=600,end_epoch=160,
            trades=[(150,1,105)],horizontal_levels=[{'label':'Barrier','value':90}])
        with patch('functions.indicator_chart.render_chart',return_value=b'png') as draw:
            self.assertEqual(render_price_chart(**args),b'png')
            render_price_chart(**args)
            self.assertEqual(draw.call_count,1)
            args['points'][1]=(160,111);render_price_chart(**args)
            args['trades'][0]=(150,2,105);render_price_chart(**args)
            args['horizontal_levels'][0]['value']=91;render_price_chart(**args)
            args['end_epoch']=161;render_price_chart(**args)
            self.assertEqual(draw.call_count,5)
        _cached_price_chart.cache_clear()
