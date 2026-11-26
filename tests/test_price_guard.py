import math
import unittest

from managers.price_guard import PriceGuardConfig, clamp_price, floor_bias, guard_bias, next_anchor


class ConfigTests(unittest.TestCase):
    def test_defaults_and_settings(self):
        config = PriceGuardConfig.from_settings({})
        self.assertTrue(config.enabled)
        self.assertEqual((config.halflife_hours, config.band_percent), (12.0, 50.0))
        config = PriceGuardConfig.from_settings({"price_guard_enabled": "0", "price_guard_halflife_hours": "6", "price_guard_band_percent": "40", "price_guard_pull": "0.1"})
        self.assertFalse(config.enabled)
        self.assertEqual((config.halflife_hours, config.band_percent, config.pull), (6.0, 40.0, 0.1))

    def test_bad_settings_fall_back_instead_of_stopping_the_market(self):
        for bad in ({"price_guard_band_percent": "abc"}, {"price_guard_band_percent": "100"}, {"price_guard_band_percent": "0"},
                    {"price_guard_halflife_hours": "-1"}, {"price_guard_pull": "2"}, {"price_guard_enabled": "7"}):
            self.assertEqual(PriceGuardConfig.from_settings(bad), PriceGuardConfig(), bad)


class AnchorTests(unittest.TestCase):
    def test_anchor_follows_slowly_with_the_half_life(self):
        config = PriceGuardConfig(halflife_hours=1.0)
        self.assertAlmostEqual(next_anchor(100.0, 200.0, 3600, config), 150.0, places=6)   # one half-life: halfway
        self.assertAlmostEqual(next_anchor(100.0, 100.0, 60, config), 100.0)
        step = next_anchor(100.0, 50.0, 1, config)
        self.assertTrue(99.9 < step < 100.0)

    def test_a_missing_anchor_starts_at_the_price(self):
        self.assertEqual(next_anchor(None, 80.0, 1, PriceGuardConfig()), 80.0)


class BiasAndClampTests(unittest.TestCase):
    def test_no_pull_inside_the_soft_zone_and_a_pull_back_beyond_it(self):
        config = PriceGuardConfig()
        self.assertEqual(guard_bias(100.0, 100.0, config), 0.0)
        self.assertEqual(guard_bias(90.0, 100.0, config), 0.0)
        low = guard_bias(60.0, 100.0, config)
        high = guard_bias(160.0, 100.0, config)
        self.assertGreater(low, 0, "far below the anchor: pushed up")
        self.assertLess(high, 0, "far above the anchor: pushed down")
        self.assertGreater(guard_bias(40.0, 100.0, config), low, "the further away, the stronger")

    def test_disabled_guard_does_nothing(self):
        config = PriceGuardConfig(enabled=False)
        self.assertEqual(guard_bias(10.0, 100.0, config), 0.0)
        self.assertEqual(clamp_price(1.0, 100.0, config), 1.0)

    def test_clamp_keeps_the_price_inside_the_band_around_the_anchor(self):
        config = PriceGuardConfig(band_percent=50.0)
        self.assertEqual(clamp_price(30.0, 100.0, config), 50.0)       # never below half the anchor
        self.assertEqual(clamp_price(500.0, 100.0, config), 200.0)     # never above twice the anchor
        self.assertEqual(clamp_price(120.0, 100.0, config), 120.0)
        self.assertEqual(clamp_price(0.5, 1.0, config), 1.0, "the market's own price floor of 1 stays")
        self.assertTrue(math.isfinite(clamp_price(10.0, None, config)))
        self.assertEqual(clamp_price(10.0, None, config), 10.0, "no anchor yet: nothing to clamp to")

    def test_a_price_already_outside_the_band_is_not_snapped_back_only_stopped_from_going_further(self):
        config = PriceGuardConfig(band_percent=50.0)
        self.assertEqual(clamp_price(30.0, 100.0, config, prior=40.0), 40.0, "below the band: no lower than where it already was")
        self.assertEqual(clamp_price(45.0, 100.0, config, prior=40.0), 45.0, "moving back toward the band is free")
        self.assertEqual(clamp_price(500.0, 100.0, config, prior=300.0), 300.0)
        self.assertEqual(clamp_price(250.0, 100.0, config, prior=300.0), 250.0)


class FloorTests(unittest.TestCase):
    def test_floor_defaults_to_100_won_and_reads_settings(self):
        self.assertEqual(PriceGuardConfig().floor, 100.0)
        config = PriceGuardConfig.from_settings({"price_floor_won": "250", "price_floor_zone_percent": "20", "price_floor_pull": "0.1"})
        self.assertEqual((config.floor, config.floor_zone_percent, config.floor_pull), (250.0, 20.0, 0.1))
        self.assertEqual(PriceGuardConfig.from_settings({"price_floor_won": "0"}).floor, 0.0, "0 turns the floor off")
        for bad in ({"price_floor_won": "-1"}, {"price_floor_won": "abc"}, {"price_floor_zone_percent": "0"}, {"price_floor_pull": "5"}):
            self.assertEqual(PriceGuardConfig.from_settings(bad), PriceGuardConfig(), bad)

    def test_support_pushes_up_only_near_or_below_the_floor_and_harder_the_lower(self):
        config = PriceGuardConfig()
        self.assertEqual(floor_bias(150.0, config), 0.0, "well above the floor: nothing")
        self.assertEqual(floor_bias(115.0, config), 0.0, "at the edge of the support zone")
        near, at, below = floor_bias(108.0, config), floor_bias(100.0, config), floor_bias(80.0, config)
        self.assertTrue(0 < near < at < below)
        self.assertEqual(floor_bias(100.0, PriceGuardConfig(floor=0.0)), 0.0)
        self.assertEqual(floor_bias(100.0, PriceGuardConfig(enabled=False)), 0.0)


if __name__ == "__main__":
    unittest.main()
