import asyncio
import threading
import sqlite3
import unittest
import time
from contextlib import contextmanager
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

    def test_bulk_listings_preserve_quotes_expiry_collisions_and_bounded_queries(self):
        market = self.bot.market_mgr
        with self.db.atomic() as conn:
            conn.executemany("INSERT INTO market_assets(code,name,exchange_id,price,type) VALUES (?,?,'PERF',100,'stock')",
                             [(f'B{i}', f'주식{i}') for i in range(40)])
        self.db.products.register_investment('OLD','만료','stock_future',100,1000,1,'국가','PERF')
        self.db.products.execute("UPDATE investment SET created_at='1970-01-01 00:00:00' WHERE product_code='OLD'")
        ok, message = self.bot.option_mgr.create_and_write('citizen','B0','put',100,30,1,10,60)
        self.assertTrue(ok, message)
        option = self.db.assets.execute_one('SELECT contract_code FROM option_contracts')[0]
        self.db.assets.execute('INSERT INTO market_quote_overrides(asset_code,price,updated_epoch) VALUES (?,?,?)',(option,17,int(time.time())))
        expected = sorted([market.get_asset_info(code) for code in [*(f'B{i}' for i in range(40)),'OLD',option]],
                          key=lambda item: (item['name'].casefold(),item['code']))
        queries = []
        readonly = self.db.readonly
        @contextmanager
        def traced():
            with readonly() as conn:
                conn.set_trace_callback(queries.append)
                yield conn
        with patch.object(self.db,'readonly',traced):
            self.assertEqual(market.get_market_assets(),expected)
        self.assertLessEqual(len(queries),10)
        self.assertNotIn('OLD',{row['code'] for row in market.get_market_assets(active_only=True)})
        self.db.assets.update_price('B1',120)
        self.assertEqual(next(row['price'] for row in market.get_market_assets() if row['code']=='B1'),120)
        self.db.products.register_saving('B1','중복',0,0,0,86400,0,'은행')
        with self.assertRaisesRegex(ValueError,'중복'):
            market.get_market_assets()

    def test_currency_snapshot_matches_exact_rounding_and_refreshes_each_request(self):
        from managers.currency_manager import CurrencyManager, CurrencyReadSnapshot
        from managers.currency_exchange_manager import CurrencyExchangeManager
        self.db.assets.register_asset('FX_T','환율','FX',137,'forex')
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places,rate_multiplier) VALUES ('AAA','A','0.333333333333',2,'1.17')")
            conn.execute("INSERT INTO currencies(code,name,asset_code,decimal_places) VALUES ('BBB','B','FX_T',6)")
        with self.db.readonly() as conn:
            queries = []
            conn.set_trace_callback(queries.append)
            snapshot = CurrencyReadSnapshot(conn)
            self.assertEqual(len(queries),1)
            for code in ('NWX','AAA','BBB'):
                self.assertEqual(snapshot.rate_nwx(code),CurrencyExchangeManager._rate(conn,code))
                for target in ('NWX','AAA','BBB'):
                    for units in (0,1,999999,123456789):
                        self.assertEqual(snapshot.convert(units,code,target),CurrencyManager.convert(conn,units,code,target))
            before = len(queries)
            snapshot.rate_nwx('BBB'); snapshot.convert(1000000,'AAA','BBB')
            self.assertEqual(len(queries),before)
        self.db.assets.update_price('FX_T',139)
        self.assertEqual(next(row['rate'] for row in self.bot.currency_mgr.rates() if row['code']=='BBB'),139)
        self.assertEqual(snapshot.rate_nwx('BBB'),137)

    def test_sql_candles_keep_ohlc_volume_indicators_and_partial_window_summary(self):
        from functions.technical_indicators import build_candles
        market = self.bot.market_mgr
        now = int(time.time())
        market._prepare_movement_history_read()
        start = now // 600 * 600
        points = [(start+i,100+(i*37)%101) for i in range(601)]
        volumes = [(start+4,1.5),(start+37,2),(start+500,5)]
        visible = start+37
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO market_assets(code,name,exchange_id,price,type) VALUES ('CH','차트','CH',150,'stock')")
            conn.executemany("INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES ('CH',?,?)",
                             [(p,t) for t,p in points])
            conn.executemany("INSERT INTO market_exchange_index_history(exchange_id,index_value,recorded_epoch) VALUES ('CH',?,?)",
                             [(p/7,t) for t,p in points])
        for index in (False,True):
            rows = market.get_chart_candles('CH',start,visible,60,index=index)
            normalize = (lambda p: round(p,2)) if index else int
            raw = [(t,normalize(p/7 if index else p)) for t,p in points]
            candles = [{key: row[key] if key=='time' else normalize(row[key])
                        for key in ('time','open','high','low','close')} for row in rows]
            expected = build_candles(raw,[],60)
            self.assertEqual(candles,[{k:v for k,v in c.items() if k!='volume'} for c in expected])
            self.assertEqual(sum(row['visible_count'] for row in rows),564)
            first = next(row for row in rows if row['visible_epoch'] is not None)
            self.assertEqual((first['visible_epoch'],normalize(first['visible_open'])),raw[37])
            args=dict(period_seconds=600,end_epoch=start+600,volumes=volumes,candle_seconds=60,
                      upper_indicators=['sma','vwap'],lower_indicators=['volume','rsi'])
            self.assertEqual(render_price_chart(raw,**args),render_price_chart(raw,price_candles=candles,**args))
        self.assertEqual(market.get_chart_candles('MISSING',start,visible,60),[])
        async def check_view():
            from functions.discord_views import PriceMovementView
            view = PriceMovementView(self.bot,'citizen','CH')
            view.period_seconds = 60
            for end in (start+607,start+720):
                with patch('functions.discord_views.time.time',return_value=end), \
                     patch.object(market,'_prepare_movement_history_read'), \
                     patch('functions.discord_views.render_price_chart',return_value=b'png') as draw:
                    embed, _ = view.render()
                expected = build_candles(points+[(end,150)],[],60)
                self.assertEqual(draw.call_args.kwargs['price_candles'],
                                 [{k:v for k,v in c.items() if k!='volume'} for c in expected])
                self.assertIn(f"기록 {sum(t >= end-600 for t,p in points):,}개",embed.description)
        asyncio.run(check_view())

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
            args['price_candles']=[dict(time=100,open=100,high=110,low=100,close=110)]
            render_price_chart(**args)
            args['price_candles'][0]['high']=120;render_price_chart(**args)
            self.assertEqual(draw.call_count,7)
        _cached_price_chart.cache_clear()
