"""User routes expose ELS terms and fixed preferred-dividend claims correctly."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

import tests.test_inazina as fixtures
from functions.admin_help_view import AdminHelpView
from functions.discord_views import InfoPanelView, PriceMovementView
from functions.slash_commands import SlashCommandRegistry
from functions.wallet_views import WalletCategoryView, els_position_lines, preferred_claim_lines
from functions.admin_prefix import NATIONAL_COMMANDS
from managers.els_manager import ELSManager, DAY


class StructuredProductInterfaceTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def create_els(self, issue=False):
        self.db.companies.create_company('CO', '기초회사', '산업', 10000, 100, 100, 'citizen', 'EX')
        self.db.assets.execute("INSERT INTO market_exchange_indices(exchange_id,index_value,updated_epoch) VALUES ('EX',1000,1)")
        self.bot.els_mgr = ELSManager(self.db, self.bot.user_mgr)
        self.bot.els_mgr.create('admin', 'E1', dict(
            name='주가연동 ELS', face_value=1000, total_supply=100,
            underlyings='CO,__exchange_index__:EX', annual_coupon=12, maturity_days=180,
            observation_days='30,60,90', redemption_levels='90,85,80',
            maturity_level=75, knock_in_level=60, subscription_days=1, exchange_id='EX',
        ))
        if issue:
            success, message = self.bot.els_mgr.subscribe('citizen', 'E1', 3)
            self.assertTrue(success, message)
            end = self.bot.els_mgr.get_details('E1')['subscription_end_epoch']
            with self.db.atomic() as conn:
                self.bot.els_mgr.observe(conn, end + 1)
        return self.bot.els_mgr.get_details('E1', 'citizen')

    def test_graph_route_selects_underlying_and_uses_frozen_contract_barriers(self):
        self.create_els(issue=True)
        registry = SlashCommandRegistry(self.bot)
        registry._prepare = AsyncMock(return_value=('citizen', None, 'user'))
        registry._reply = AsyncMock()
        with patch('functions.discord_views.render_price_chart', return_value=b'chart') as chart:
            asyncio.run(registry.movement(SimpleNamespace(), 'E1'))
            sent = registry._reply.call_args.kwargs
            view = sent['view']
            self.assertEqual(view.els_code, 'E1')
            self.assertEqual({option.value for option in view.underlying_select.options}, {'CO', '__exchange_index__:EX'})
            levels = chart.call_args.kwargs['horizontal_levels']
            self.assertEqual([level['value'] for level in levels], [100, 90, 60])
            self.assertIn('ELS 평가가격이 아닙니다', str(sent['embed'].to_dict()))
            view.underlying_select._values = ['__exchange_index__:EX']
            view._edit_chart = AsyncMock()
            asyncio.run(view.select_underlying(SimpleNamespace()))
            view.render()
            self.assertEqual([level['value'] for level in chart.call_args.kwargs['horizontal_levels']], [1000, 900, 600])

    def test_subscription_chart_defers_barriers_and_search_exposes_contract_terms(self):
        self.create_els()
        panel = InfoPanelView(self.bot, 'citizen')
        panel.category = 'els'
        text = panel.build_embed().description
        self.assertIn('모집 중', text)
        self.assertIn('액면 1,000', text)
        self.assertIn('60%', text)
        self.assertIn('__exchange_index__:EX', text)
        with patch('functions.discord_views.render_price_chart', return_value=b'chart') as chart:
            embed, _ = PriceMovementView(self.bot, 'citizen', 'E1').render()
        self.assertEqual(chart.call_args.kwargs['horizontal_levels'], [])
        self.assertIn('모집 종료', str(embed.to_dict()))

    def test_wallet_contract_and_dividend_claim_categories_keep_main_private(self):
        details = self.create_els(issue=True)
        claims = [dict(issuer_code='CO', asset_code='PR', quantity=4, gross_amount=20,
                       paid_amount=5, due_epoch=1000000, quote_currency='NWX')]
        summary = discord.Embed(title='메인')
        summary.add_field(name='현금', value='```100 NWX```')
        sections = {'els': els_position_lines([details]), 'preferred_claims': preferred_claim_lines(claims)}
        view = WalletCategoryView('citizen', summary, sections)
        self.assertNotIn('주가연동 ELS', str(view.build_embed().to_dict()))
        view.category = 'els'
        self.assertIn('인수 원금 **3,000 NWX**', view.build_embed().description)
        self.assertIn('중도 매도', view.build_embed().description)
        view.category = 'preferred_claims'
        self.assertIn('15 NWX', view.build_embed().description)
        self.assertIn('매도해도', view.build_embed().description)
        registry = SlashCommandRegistry(self.bot)
        registry._prepare = AsyncMock(return_value=('citizen', self.db.users.get_user('citizen'), 'user'))
        registry._reply = AsyncMock()
        interaction = SimpleNamespace(user=SimpleNamespace(name='시민', display_name='시민'))
        asyncio.run(registry.assets(interaction))
        main = registry._reply.call_args.kwargs['embed']
        fields = {field.name: field.value for field in main.fields}
        self.assertIn('3,000 NWX', fields['📈 투자자산 합계'])
        self.assertIn('3,000 NWX', fields['🧾 ELS 인수 원금'])
        self.assertIn('💰 미지급 배당 명목액', fields)
        self.assertIn('💳 신용 점수', fields)
        self.assertNotIn('주가연동 ELS', str(main.to_dict()))

    def test_all_admin_help_pages_remain_within_discord_limits(self):
        view = AdminHelpView(self.bot, 'admin', NATIONAL_COMMANDS)
        for index, (key, _) in enumerate(view.PAGES):
            view.page = index
            embed = view.build_embed()
            self.assertLessEqual(len(embed), 6000, key)
            self.assertLessEqual(len(embed.description or ''), 4096, key)
            self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields), key)
        view.page = next(index for index, (key, _) in enumerate(view.PAGES) if key == 'structured')
        self.assertIn('법인잔고수정', str(view.build_embed().to_dict()))
