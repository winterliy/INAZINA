import asyncio
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_inazina as fixtures
from functions.discord_views import InfoPanelView


class SearchRankingTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def listing(self, code, price, currency='NWX'):
        self.db.assets.register_asset(code, code, 'RANK', price, 'stock')
        self.db.assets.execute('UPDATE market_assets SET quote_currency=? WHERE code=?', (currency, code))

    def history(self, code, quantity, price, epoch, side='buy'):
        self.db.assets.execute('INSERT INTO market_trade_history(asset_code,side,quantity,price,recorded_epoch) VALUES (?,?,?,?,?)',
                               (code, side, quantity, price, epoch))

    def bulk(self, now):
        with self.db.atomic() as conn:
            conn.executemany("INSERT INTO market_assets(code,name,exchange_id,price,type) VALUES (?,?,'RANK',?,'stock')",
                             [(f'R{i:03}', f'종목{i}', 200+i) for i in range(103)])
            conn.executemany("INSERT INTO market_trade_history(asset_code,side,quantity,price,recorded_epoch) VALUES (?,'buy',?,?,?)",
                             [(f'R{i:03}', i+1, 200+i, now-1) for i in range(103)])
            conn.executemany('INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES (?,100,?)',
                             [(f'R{i:03}', now-2) for i in range(103)])

    def test_daily_rankings_compare_currencies_and_refresh_quotes(self):
        now = int(time.time())
        _, midnight = self.bot.market_mgr.movement_history.day_boundary(now)
        with self.db.atomic() as conn:
            conn.execute("INSERT INTO currencies(code,name,reference_rate,decimal_places) VALUES ('USD','달러','1500',2)")
            conn.execute(
                "INSERT INTO country_currencies(nationality,currency_code) VALUES ('미국','USD')"
            )
        for code, price, currency in [('A',110,'NWX'), ('B',120,'USD'), ('C',85,'NWX'), ('D',80,'NWX'), ('FLAT',100,'NWX'), ('OFF',100000,'NWX')]:
            self.listing(code, price, currency)
            self.db.assets.execute('INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES (?,100,?)', (code, midnight))
        self.db.assets.execute("UPDATE market_assets SET status='inactive' WHERE code='OFF'")
        self.db.products.register_investment('OLD','만료','stock_future',100,1000,1,'국가','RANK')
        self.db.products.execute("UPDATE investment SET created_at='1970-01-01 00:00:00' WHERE product_code='OLD'")
        self.db.products.register_investment('ELS','ELS','els',100,1000,0,'국가','RANK')
        for code, qty, price, side in [('A',3,100,'buy'), ('A',2,110,'sell'), ('B',1,120,'buy'), ('C',20,85,'buy'), ('D',10,80,'buy'), ('OFF',1000,100000,'buy'), ('OLD',1000,100000,'buy'), ('ELS',1000,100000,'buy')]:
            self.history(code, qty, price, now, side)
        self.history('A',100000,10000,midnight-1)
        self.history('A',100000,10000,now+60)
        self.db.assets.execute('INSERT INTO market_price_history(asset_code,price,recorded_epoch) VALUES (?,10000,?)', ('A', midnight-1))
        rank = lambda metric: self.bot.market_mgr.get_realtime_rankings(metric, 'citizen', now=now)
        turnover = rank('turnover')
        self.assertEqual([r['code'] for r in turnover], ['B','C','D','A'])
        self.assertEqual(turnover[0]['turnover'], 120)
        self.assertEqual(turnover[-1]['turnover'], Fraction(520,1500))
        self.assertEqual(turnover[0]['home_currency'], 'USD')
        self.assertEqual([r['code'] for r in rank('volume')], ['C','D','A','B'])
        self.assertEqual([r['code'] for r in rank('gainers')], ['B','A'])
        self.assertEqual([r['code'] for r in rank('losers')], ['D','C'])
        self.db.assets.execute("INSERT INTO market_quote_overrides(asset_code,price,updated_epoch,source) VALUES ('A',130,?,'admin')", (now,))
        self.assertEqual(rank('gainers')[0]['code'], 'A')
        self.assertEqual(rank('gainers')[0]['change_percent'], 30)

    def test_fractional_volume_is_not_truncated(self):
        now = int(time.time())
        self.listing('FRACTION', 100)
        self.history('FRACTION', 0.2, 100, now)

        rows = self.bot.market_mgr.get_realtime_rankings(
            'volume', 'citizen', now=now
        )

        self.assertEqual(rows[0]['code'], 'FRACTION')
        self.assertEqual(rows[0]['volume'], 0.2)

    def test_categories_top100_pages_and_interactive_refresh(self):
        now = int(time.time())
        self.bulk(now)
        panel = InfoPanelView(self.bot, 'citizen')
        self.assertEqual([key for key, _ in panel.CATEGORIES[:5]], ['main', *panel.RANKINGS])
        self.assertLessEqual(len(panel.category_select.options), 25)
        self.assertLessEqual(len(panel.build_embed()), 6000)
        panel.category = 'rank_volume'
        first = panel.build_embed()
        self.assertEqual(len(first.fields), 10)
        self.assertIn('R102', first.fields[0].value)
        self.assertIn('1–10위', first.footer.text)
        self.assertTrue(panel.previous_button.disabled)
        self.assertFalse(panel.next_button.disabled)
        panel.page = 999
        last = panel.build_embed()
        self.assertEqual(panel.page, 9)
        self.assertIn('91–100위', last.footer.text)
        self.assertIn('R003', last.fields[-1].value)
        self.assertTrue(panel.next_button.disabled)
        self.assertLessEqual(len(last), 6000)
        interaction = SimpleNamespace(user=SimpleNamespace(id='citizen'),
            response=SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock())

        async def run():
            panel.category_select._values = ['rank_volume']
            await panel.select_category(interaction)
            self.assertEqual(panel.page, 0)
            await panel.next_button.callback(interaction)
            self.assertIn('11–20위', interaction.edit_original_response.call_args.kwargs['embed'].footer.text)
            await panel.previous_button.callback(interaction)
            self.history('R000',100000,200,int(time.time()))
            await panel.refresh_button.callback(interaction)
            self.assertIn('R000', interaction.edit_original_response.call_args.kwargs['embed'].fields[0].value)
            outsider = SimpleNamespace(user=SimpleNamespace(id='other'), response=SimpleNamespace(send_message=AsyncMock()))
            self.assertFalse(await panel.interaction_check(outsider))

        asyncio.run(run())
        panel.category = 'rank_losers'
        empty = panel.build_embed()
        self.assertIn('해당하는 종목이 없습니다', empty.description)
        self.assertTrue(panel.previous_button.disabled and panel.next_button.disabled)

    def test_ranking_reader_uses_snapshot_without_per_symbol_queries(self):
        now = int(time.time())
        self.bulk(now)
        entered, release = threading.Event(), threading.Event()
        queries = []
        original = self.db.readonly

        @contextmanager
        def tracked():
            with original() as conn:
                conn.set_trace_callback(queries.append)
                yield conn

        def writer():
            with self.db.atomic() as conn:
                conn.execute("UPDATE market_assets SET price=10000 WHERE code='R000'")
                entered.set()
                self.assertTrue(release.wait(5))

        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(writer)
            self.assertTrue(entered.wait(2))
            try:
                with patch.object(self.db, 'readonly', side_effect=tracked):
                    rows = pool.submit(self.bot.market_mgr.get_realtime_rankings, 'gainers', 'citizen', 100, now).result(timeout=2)
                self.assertEqual(len(rows), 100)
                self.assertNotIn('R000', [r['code'] for r in rows])
                self.assertLess(len(queries), 40)
            finally:
                release.set()
            future.result(timeout=2)
        self.assertEqual(self.bot.market_mgr.get_realtime_rankings('gainers','citizen',now=now)[0]['code'], 'R000')

    def test_asset_ranking_is_top20_with_five_people_per_page(self):
        for index in range(23):
            self.db.users.create_user(f'rank-{index:02}', f'시민{index:02}', '미국', 0)
        panel = InfoPanelView(self.bot, 'citizen')
        panel.category = panel.ASSET_RANKING

        def assets(user_id):
            score = int(str(user_id).split('-')[-1]) if str(user_id).startswith('rank-') else 0
            return {'grand_total': score * 100, 'loan_total': score, 'net_worth': score * 99}

        with patch.object(self.bot.user_mgr, 'get_total_assets', side_effect=assets):
            first = panel.build_embed()
            self.assertEqual(len(first.fields), 5)
            self.assertIn('시민22', first.fields[0].name)
            self.assertIn('1–5위', first.footer.text)
            panel.page = 99
            last = panel.build_embed()
            self.assertEqual(panel.page, 3)
            self.assertEqual(len(last.fields), 5)
            self.assertIn('16–20위', last.footer.text)
