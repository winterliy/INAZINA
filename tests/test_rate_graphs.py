import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from database import DatabaseManager
from database.db_manager import DBManager
from functions.discord_views import PriceMovementView
from functions.price_chart import render_rate_chart
from functions.slash_commands import SlashCommandRegistry
from managers.market_manager import MarketManager
from managers.movement_history_manager import MovementHistoryManager
from managers.rate_manager import RATE_CHART_PREFIX, RateManager


class RateGraphTests(unittest.TestCase):
    def setUp(self):
        self.previous_dir = DBManager.database_dir
        self.tempdir = tempfile.TemporaryDirectory()
        DBManager.database_dir = Path(self.tempdir.name)
        self.db = DatabaseManager()
        self.db.users.create_user("admin", "Admin", "Nac", 1000, role="admin")
        self.db.users.create_user("alice", "Alice", "Nac", 1000)
        self.rates = RateManager(self.db)
        self.market = MarketManager(self.db)
        self.bot = SimpleNamespace(db=self.db, rate_mgr=self.rates, market_mgr=self.market)

    def tearDown(self):
        self.db.close()
        DBManager.database_dir = self.previous_dir
        self.tempdir.cleanup()

    def history(self, key, rows):
        self.db.system.execute("DELETE FROM rate_history WHERE rate_key = ?", (key,))
        for epoch, rate in rows:
            self.db.system.execute(
                "INSERT INTO rate_history (rate_key, annual_percent, effective_epoch, changed_by) "
                "VALUES (?, ?, ?, 'test')", (key, rate, epoch),
            )

    def test_rate_chart_codes_and_names_resolve_without_confusing_asset_codes(self):
        for key, label in RateManager.KEYS.items():
            code = RateManager.chart_code(key)
            self.assertEqual(code, RATE_CHART_PREFIX + key)
            self.assertEqual(RateManager.rate_key_from_code(code), key)
            self.assertEqual(RateManager.rate_key_from_code(label), key)
        self.assertIsNone(RateManager.rate_key_from_code("CO"))
        self.assertIsNone(RateManager.rate_key_from_code(RATE_CHART_PREFIX + "unknown"))

    def test_rate_history_boundary_cases(self):
        cases = (
            ("prior value and future exclusion", "base_rate",
             [(0, 3), (110.25, 3.5), (130.75, 4), (200, 99)], 100, 150,
             [(100.0, 3.0), (110.25, 3.5), (130.75, 4.0), (150.0, 4.0)]),
            ("same instant uses last value", "base_rate",
             [(0, 3), (110, 3.5), (110, 4)], 100, 150,
             [(100.0, 3.0), (110.0, 4.0), (150.0, 4.0)]),
            ("window boundary is effective", "base_rate",
             [(0, 3), (100, 4)], 100, 150,
             [(100.0, 4.0), (150.0, 4.0)]),
            ("zero and hundred are retained", "statutory_rate",
             [(0, 0), (120, 100)], 100, 150,
             [(100.0, 0.0), (120.0, 100.0), (150.0, 100.0)]),
        )
        for name, key, rows, start, end, expected in cases:
            with self.subTest(name=name):
                self.history(key, rows)
                self.assertEqual(self.rates.get_rate_history(key, start, now=end), expected)

        self.db.system.set_setting("base_rate", "9")
        self.history("base_rate", [(200, 9)])
        self.assertEqual(self.rates.get_rate_history("base_rate", 100, now=150),
                         [(100.0, 0.0), (150.0, 0.0)])
        self.assertEqual(self.rates.get_rate_history("base_rate", 150, now=150),
                         [(150.0, 0.0)])
        self.assertEqual(self.rates.get_rate_history("base_rate", 100, now=200),
                         [(100.0, 0.0), (200.0, 9.0)])
        self.history("base_rate", [])
        self.assertEqual(self.rates.get_rate_history("base_rate", 100, now=150),
                         [(100.0, 9.0), (150.0, 9.0)])

    def test_chart_read_does_not_change_nonretroactive_interest_or_history(self):
        self.history("statutory_rate", [(0, 5), (1000, 10)])
        with self.db.atomic(("system",)) as conn:
            previous = RateManager.interest_between(conn, "statutory_rate", 100000, 0, 2000)
        records = [tuple(row) for row in self.db.system.execute("SELECT * FROM rate_history")]
        self.rates.get_rate_history("statutory_rate", 500, now=1500)
        with self.db.atomic(("system",)) as conn:
            after = RateManager.interest_between(conn, "statutory_rate", 100000, 0, 2000)
        self.assertEqual(previous, after)
        self.assertEqual(records, [tuple(row) for row in self.db.system.execute("SELECT * FROM rate_history")])

    def test_daily_movement_cleanup_preserves_all_rate_changes(self):
        self.history("base_rate", [(0, 3), (60, 4)])
        self.history("statutory_rate", [(0, 5), (70, 6)])
        records = [tuple(row) for row in self.db.system.execute("SELECT * FROM rate_history")]
        self.assertTrue(MovementHistoryManager(self.db).cleanup(now=86400 * 2))
        self.assertEqual(records, [tuple(row) for row in self.db.system.execute("SELECT * FROM rate_history")])

    def test_rate_view_has_time_range_and_refresh_without_market_indicators(self):
        view = PriceMovementView(self.bot, "alice", RateManager.chart_code("base_rate"))
        self.assertEqual([option.label for option in view.period_select.options],
                         [label for label, _ in view.PERIODS])
        selects = [item for item in view.children if isinstance(item, discord.ui.Select)]
        buttons = [item.label for item in view.children if isinstance(item, discord.ui.Button)]
        self.assertEqual(selects, [view.period_select])
        self.assertEqual(buttons, ["새로고침"])

    def test_rate_chart_uses_percent_and_percentage_points_for_every_period(self):
        self.history("base_rate", [(0, 0), (120, 3.5)])
        view = PriceMovementView(self.bot, "alice", "기준금리")
        with patch("functions.discord_views.time.time", return_value=150.5), \
             patch("functions.discord_views.render_rate_chart", return_value=b"chart") as render:
            for label, seconds in view.PERIODS:
                with self.subTest(period=label):
                    view.period_seconds = seconds
                    embed, png = view.render()
                    self.assertIn("기준금리", embed.title)
                    self.assertRegex(embed.description, r"3\.5(?:0+)?%")
                    self.assertIn("%p", embed.description)
                    self.assertNotIn("NWX", embed.description)
                    self.assertNotIn("거래량", embed.description)
                    self.assertIn(label, embed.description)
                    self.assertEqual(embed.image.url, "attachment://movement.png")
                    self.assertEqual(png, b"chart")
                    self.assertEqual(render.call_args.kwargs["sample_seconds"], seconds)
                    self.assertFalse(embed.fields)

    def test_zero_and_hundred_percent_flat_rates_render_png(self):
        for rate in (0, 100):
            with self.subTest(rate=rate):
                png = render_rate_chart([(100.0, rate), (160.0, rate)], 60, end_epoch=160.0)
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
                self.assertGreater(len(png), 100)

    def test_rate_chart_changes_as_horizontal_steps_without_interpolation(self):
        from functions.price_chart import _Canvas

        segments = []
        original = _Canvas.line

        def record_line(canvas, x0, y0, x1, y1, color, thickness=1):
            if thickness == 3:
                segments.append((x0, y0, x1, y1))
            return original(canvas, x0, y0, x1, y1, color, thickness)

        with patch.object(_Canvas, "line", record_line):
            render_rate_chart([(100, 3), (130, 4), (160, 4)], 60, end_epoch=160)
        self.assertTrue(all(x0 == x1 or y0 == y1 for x0, y0, x1, y1 in segments))
        self.assertTrue(any(x0 == x1 and abs(y0 - y1) > 10 for x0, y0, x1, y1 in segments))
        self.assertTrue(any(y0 == y1 and abs(x0 - x1) > 100 for x0, y0, x1, y1 in segments))

    def test_other_user_cannot_operate_rate_view(self):
        view = PriceMovementView(self.bot, "alice", "법정이자")
        response = SimpleNamespace(send_message=AsyncMock())
        allowed = asyncio.run(view.interaction_check(SimpleNamespace(user=SimpleNamespace(id="bob"), response=response)))
        self.assertFalse(allowed)
        response.send_message.assert_awaited_once()
        self.assertTrue(response.send_message.call_args.kwargs["ephemeral"])

    def rate_interaction(self):
        return SimpleNamespace(
            user=SimpleNamespace(id="alice"),
            response=SimpleNamespace(is_done=lambda: False, defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

    def check_edited_rate_chart(self, interaction):
        interaction.response.defer.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once()
        options = interaction.edit_original_response.call_args.kwargs
        self.assertIn("법정이자", options["embed"].title)
        self.assertEqual(options["attachments"][0].filename, "movement.png")
        self.assertEqual(options["attachments"][0].fp.read(8), b"\x89PNG\r\n\x1a\n")
        options["attachments"][0].close()
        return options

    def test_rate_period_callback_edits_chart_and_keeps_selected_period(self):
        view = PriceMovementView(self.bot, "alice", "법정이자")
        view.period_select._values = ["60"]
        interaction = self.rate_interaction()
        asyncio.run(view.select_period(interaction))
        options = self.check_edited_rate_chart(interaction)
        self.assertIn("1분", options["embed"].description)
        self.assertNotIn("표시 범위 5분",options['embed'].description)
        self.assertEqual(view.period_seconds, 60)
        self.assertEqual([option.value for option in view.period_select.options if option.default], ["60"])

    def test_rate_refresh_callback_reads_new_browser_rate_and_edits_attachment(self):
        view = PriceMovementView(self.bot, "alice", "법정이자")
        self.db.system.set_setting("statutory_rate", "7.25")
        interaction = self.rate_interaction()
        asyncio.run(view.refresh_button.callback(interaction))
        options = self.check_edited_rate_chart(interaction)
        self.assertRegex(options["embed"].description, r"7\.25(?:0+)?%")

    def test_direct_graph_command_accepts_rate_name_and_generates_attachment(self):
        registry = SlashCommandRegistry(self.bot)
        registry._prepare = AsyncMock(return_value=("alice", None, "user"))
        registry._reply = AsyncMock()
        asyncio.run(registry.movement(SimpleNamespace(), 종목="법정이자"))
        options = registry._reply.call_args.kwargs
        self.assertIn("법정이자", options["embed"].title)
        self.assertIsInstance(options["view"], PriceMovementView)
        self.assertEqual(options["file"].filename, "movement.png")
        self.assertEqual(options["file"].fp.read(8), b"\x89PNG\r\n\x1a\n")
        options["file"].close()

    def test_graph_command_rejects_blank_code_without_asset_picker(self):
        registry = SlashCommandRegistry(self.bot)
        registry._prepare = AsyncMock(return_value=("alice", None, "user"))
        registry._reply = AsyncMock()
        asyncio.run(registry.movement(SimpleNamespace(),종목=' '))
        self.assertNotIn('view',registry._reply.call_args.kwargs)
        self.assertIn('종목 코드',registry._reply.call_args.kwargs['content'])


if __name__ == "__main__":
    unittest.main()
