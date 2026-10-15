import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import test_inazina as fixtures
from functions.discord_views import InfoPanelView, LoanProductApplyView, LoanRequestModal, PriceMovementView
from functions.indicator_chart import render_chart
from functions.lottery_views import rules_embed
from functions.slash_commands import SlashCommandRegistry
from functions.technical_indicators import KST
from functions.wallet_views import WalletCategoryView


class UserInterfaceRevisionTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_lottery_rules_hide_draw_commitment(self):
        embed = rules_embed({
            'round_id': 1,
            'close_epoch': 1,
            'draw_epoch': 2,
            'commitment': 'hidden',
            'rules': {
                'ticket_price': 1000,
                'max_tickets': 100,
                'prize_percent': 70,
                'treasury_percent': 20,
            },
        })
        text = str(embed.to_dict())
        self.assertNotIn('사전 약속', text)
        self.assertNotIn('SHA-256', text)
        self.assertNotIn('hidden', text)

    def test_wallet_main_contains_only_financial_summary(self):
        summary = discord.Embed(description='국적: 낙원')
        summary.add_field(name='💵 현금', value='```1,000 NWX```')
        view = WalletCategoryView('citizen', summary, {'stocks': ['보유 주식 비밀']}, 12)
        main = view.build_embed()
        self.assertEqual([field.name for field in main.fields], ['💵 현금', '📄 옵션 작성 의무 평가'])
        self.assertNotIn('보유 주식 비밀', str(main.to_dict()))
        self.assertTrue(view.previous.disabled and view.next.disabled)
        view.category = 'stocks'
        self.assertIn('보유 주식 비밀', view.build_embed().description)

    def test_loan_commands_require_plain_ids_and_amount_view_has_no_select(self):
        commands = []
        SlashCommandRegistry(SimpleNamespace(tree=SimpleNamespace(add_command=commands.append))).register()
        for name in ('대출신청', '대출연장'):
            command = next(command for command in commands if command.name == name)
            self.assertEqual([(p.name, p.required, p.autocomplete, p.choices) for p in command.parameters], [('id', True, False, [])])
        self.db.products.register_liability('BORROW', '대출 상품', 3, 500000, 30, 86400, 0, '국가')
        registry = SlashCommandRegistry(self.bot)
        registry._prepare = AsyncMock(return_value=('citizen', None, 'user'))
        registry._reply = AsyncMock()
        asyncio.run(registry.apply_loan(SimpleNamespace(), 'BORROW'))
        sent = registry._reply.call_args.kwargs
        self.assertIsInstance(sent['view'], LoanProductApplyView)
        self.assertFalse(any(isinstance(control, discord.ui.Select) for control in sent['view'].children))
        self.assertEqual(sent['view'].product_code, 'BORROW')
        outsider = SimpleNamespace(user=SimpleNamespace(id='other'), response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(asyncio.run(sent['view'].interaction_check(outsider)))
        self.assertEqual(len(LoanRequestModal(self.bot, 'citizen').children), 5)

    def test_loan_extension_passes_personal_loan_id_and_returns_result(self):
        registry = SlashCommandRegistry(self.bot)
        registry._prepare = AsyncMock(return_value=('citizen', None, 'user'))
        registry._reply = AsyncMock()
        with patch.object(self.bot.bank_mgr, 'extend_loan', return_value=(True, '✅ 연장됨')) as extend:
            asyncio.run(registry.extend_loan(SimpleNamespace(), ' 13 '))
        extend.assert_called_once_with('citizen', '13')
        self.assertIn('연장됨', registry._reply.call_args.kwargs['embed'].description)

    def test_buy_and_sell_route_nav_funds_without_a_limit_price(self):
        registry = SlashCommandRegistry(self.bot)
        registry._prepare = AsyncMock(return_value=('citizen', None, 'user'))
        registry._reply = AsyncMock()
        interaction = SimpleNamespace()
        with patch.object(
            self.bot.market_mgr,
            'get_asset_info',
            return_value={'type': 'index_fund'},
        ), patch(
            'functions.slash_commands.execute_shared_action',
            return_value=(True, '✅ 처리됨'),
        ) as execute:
            asyncio.run(registry.buy(interaction, 'IF', 3))
            execute.assert_called_once_with(
                self.bot, 'citizen', 'fund_subscribe', 'IF', 3
            )
            execute.reset_mock()
            asyncio.run(registry.sell(interaction, 'IF', 2))
            execute.assert_called_once_with(
                self.bot, 'citizen', 'fund_redeem', 'IF', 2
            )

    def test_search_index_and_rates_supply_graph_codes(self):
        self.db.companies.create_company('CO', '법인', '산업', 10000, 100, 100, 'citizen', 'EX')
        panel = InfoPanelView(self.bot, 'citizen')
        self.assertTrue({'indices', 'rates'}.issubset(dict(panel.CATEGORIES)))
        index_lines = '\n'.join(panel._category_lines('indices'))
        self.assertNotIn('__exchange_index__:EX', index_lines)
        self.assertNotIn('ID / 그래프 종목', index_lines)
        self.assertIn('1,000.00 pt', index_lines)
        rate_lines = '\n'.join(panel._category_lines('rates'))
        self.assertIn('낙원', rate_lines)
        self.assertIn('기준금리 연 **3%**', rate_lines)
        self.assertIn('법정최고이자 연 **20%**', rate_lines)
        with patch('functions.discord_views.render_rate_chart', return_value=b'chart'):
            view = PriceMovementView(self.bot, 'citizen', '__rate__:base_rate')
            view.period_seconds = 60
            embed, png = view.render()
        self.assertIn('1분', embed.description)
        self.assertEqual(png, b'chart')
        with patch('functions.discord_views.render_price_chart', return_value=b'chart'):
            embed, png = PriceMovementView(self.bot, 'citizen', '__exchange_index__:EX').render()
        self.assertIn('거래소 지수', embed.title)
        self.assertEqual(png, b'chart')

    def test_ichimoku_does_not_widen_time_axis_even_with_sparse_data(self):
        end = 1000000
        period = 1800
        with patch('functions.price_chart._Canvas.text') as labels:
            png = render_chart([(end - 20, 100), (end, 101)], period, upper_indicators=['ichimoku'], lower_indicators=[], end_epoch=end)
        expected = [datetime.fromtimestamp(end - period + period * i / 5, KST).strftime('%H:%M') for i in range(6)]
        actual = [call.args[2] for call in labels.call_args_list if isinstance(call.args[2], str) and ':' in call.args[2]]
        self.assertEqual(actual, expected)
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')

    def test_warmup_candle_at_left_boundary_does_not_enter_visible_price_scale(self):
        end, period = 1000020, 1800
        with patch('functions.price_chart._Canvas.text') as labels:
            render_chart([(end - period - 1, 999999), (end - 5, 100), (end, 101)], period,
                         upper_indicators=[], lower_indicators=[], end_epoch=end)
        prices = [float(call.args[2].replace(',', '')) for call in labels.call_args_list
                  if call.args[0] == 2 and call.args[2].replace(',', '').replace('.', '').isdigit()]
        self.assertTrue(prices)
        self.assertLess(max(prices), 110)

    def test_preferred_shares_and_dividend_terms_are_visible_without_voting_rights(self):
        self.db.companies.create_company('PREF', '법인우', '산업', 10000, 100, 100, 'citizen', 'EX')
        self.db.companies.execute("UPDATE company SET share_class='preferred',dividend_cycle_seconds=86400,dividend_per_share=5 WHERE company_code='PREF'")
        text = '\n'.join(InfoPanelView(self.bot, 'citizen')._category_lines('stocks'))
        self.assertIn('우선주', text)
        self.assertIn('1일마다', text)
        self.assertIn('주·좌당 5 NWX', text)
        self.db.companies.execute("UPDATE company SET dividend_is_active=0 WHERE company_code='PREF'")
        text = '\n'.join(InfoPanelView(self.bot, 'citizen')._category_lines('stocks'))
        self.assertIn('OFF · 지급 중지', text)
        self.assertIn('주·좌당 5 NWX', text)
        self.db.companies.execute("UPDATE company SET dividend_mode='percent',dividend_percent=2.5,dividend_per_share=0 WHERE company_code='PREF'")
        text = '\n'.join(InfoPanelView(self.bot, 'citizen')._category_lines('stocks'))
        self.assertIn('가격의 2.5%', text)
        self.assertIn('OFF · 지급 중지', text)
        registry = SlashCommandRegistry(self.bot)
        registry._prepare = AsyncMock(return_value=('citizen', None, 'user'))
        registry._reply = AsyncMock()
        asyncio.run(registry.shareholder_meeting(SimpleNamespace(), 'PREF', '여기'))
        self.assertIn('의결권', registry._reply.call_args.kwargs['content'])

    def test_wallet_displays_preferred_class_and_personal_loan_maturity(self):
        self.db.companies.create_company('PREF', '법인우', '산업', 10000, 100, 100, 'citizen', 'EX')
        self.db.companies.execute("UPDATE company SET share_class='preferred' WHERE company_code='PREF'")
        self.db.assets.execute("INSERT INTO user_assets(user_id,asset_code,quantity,average_cost) VALUES ('citizen','PREF',5,100)")
        self.db.products.register_liability('BORROW', '대출 상품', 3, 500000, 30, 86400, 0, '국가')
        self.db.products.execute("UPDATE liability SET extension_days=7,extension_min_credit_score=650 WHERE product_code='BORROW'")
        success, message = self.bot.bank_mgr.apply_loan('citizen', 'BORROW', 1000)
        self.assertTrue(success, message)
        loan = self.db.accounts.execute_one("SELECT loan_id,maturity_epoch FROM loans WHERE user_id='citizen'")
        registry = SlashCommandRegistry(self.bot)
        registry._prepare = AsyncMock(return_value=('citizen', self.db.users.get_user('citizen'), 'user'))
        registry._reply = AsyncMock()
        interaction = SimpleNamespace(user=SimpleNamespace(name='시민', display_name='시민'))
        asyncio.run(registry.assets(interaction))
        view = registry._reply.call_args.kwargs['view']
        self.assertNotIn('법인우', str(view.build_embed().to_dict()))
        view.category = 'stocks'
        self.assertIn('우선주', view.build_embed().description)
        view.category = 'loans'
        loan_description = view.build_embed().description
        self.assertIn(f"대출 #{loan['loan_id']}", loan_description)
        self.assertIn(f"<t:{int(loan['maturity_epoch'])}:D>", loan_description)
        self.assertIn(f"/대출연장 id:{loan['loan_id']}", loan_description)
        conditions = '\n'.join(InfoPanelView(self.bot, 'citizen')._category_lines('loan_products'))
        self.assertIn('650점 이상/7일', conditions)
