"""Monetary bounds widen independently from share and contract quantities."""
from decimal import Decimal
import pytest
from functions.amount_limits import MAX_NATIVE
from managers.order_book_manager import OrderBookManager
from managers.option_manager import OptionManager
from managers.futures_manager import FuturesManager
from managers.corporate_actions_manager import CorporateActionsManager
from managers.auto_target_engine import AutoTargetConfig, sample_target, cycle_bounds


def test_trading_money_limits_are_wide_but_quantities_stay_safe():
    assert OrderBookManager.MAX_LIMIT_PRICE == MAX_NATIVE
    assert OptionManager.MAX_PRICE == OptionManager.MAX_MONEY == MAX_NATIVE
    assert CorporateActionsManager.MAX_MONEY == MAX_NATIVE
    assert OrderBookManager.MAX_QUANTITY == OptionManager.MAX_CONTRACTS == 2**53 - 1


def test_large_dividend_parser_preserves_single_unit():
    assert CorporateActionsManager.dividend_terms(str(10**18 + 1))['dividend_per_share'] == 10**18 + 1


def test_futures_margin_accepts_wide_bound_and_rejects_overflow():
    assert FuturesManager._round_margin({'contract_size': 1}, 1, MAX_NATIVE, 1) == MAX_NATIVE
    with pytest.raises(ValueError):
        FuturesManager._round_margin({'contract_size': 1}, 1, MAX_NATIVE + 1, 1)


def test_large_automatic_bound_keeps_minor_quote():
    price = Decimal('1000000000000000000.000001')
    config = AutoTargetConfig(range_percent=0, band_percent=0)
    cycle = sample_target(price, 100, config)
    assert cycle_bounds(cycle, config) == (price, price)


import unittest
import test_inazina as fixtures
from managers.asset_cost_manager import AssetCostManager


class WideSplitBasisTests(unittest.TestCase):
    setUp = fixtures.InazinaTests.setUp
    tearDown = fixtures.InazinaTests.tearDown

    def test_regular_split_retains_large_total_basis_and_fractional_lot(self):
        self.db.companies.create_company('WIDE', '정밀분할', '게임', 100000, 100, 10**18 + 1, 'admin', 'INZ')
        self.db.assets.add_user_asset('citizen', 'WIDE', 2.5, unit_cost=10**18 + 1)
        with self.db.atomic() as conn:
            lot = conn.execute("SELECT holding_id FROM user_assets WHERE asset_code='WIDE'").fetchone()[0]
            before = AssetCostManager.total_cost_minor(conn, lot)
        ok, message = CorporateActionsManager(self.bot).adjust_face_value('admin', '분할', 'WIDE', 10)
        self.assertTrue(ok, message)
        with self.db.atomic() as conn:
            self.assertEqual(conn.execute('SELECT quantity FROM user_assets WHERE holding_id=?', (lot,)).fetchone()[0], 25)
            self.assertEqual(AssetCostManager.total_cost_minor(conn, lot), before)
