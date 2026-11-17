"""M7: mixed and stepped contract rates (teaser, fixed-then-floating, per-reset and lifetime limits)."""
import unittest
from decimal import Decimal

from functions.rate_schedule import RateSchedule

D = Decimal


class RateScheduleTests(unittest.TestCase):
    def test_teaser_then_fixed_step_up(self):
        schedule = RateSchedule.parse([
            {"from_day": 0, "kind": "fixed", "rate": "2.0"},
            {"from_day": 90, "kind": "fixed", "rate": "4.5"},
        ])
        self.assertEqual(schedule.rate_at(0), D("2.0"))
        self.assertEqual(schedule.rate_at(89), D("2.0"))
        self.assertEqual(schedule.rate_at(90), D("4.5"))
        self.assertEqual(schedule.rate_at(3650), D("4.5"))

    def test_fixed_then_floating_uses_the_reference_plus_spread(self):
        schedule = RateSchedule.parse([
            {"from_day": 0, "kind": "fixed", "rate": "3.0"},
            {"from_day": 365, "kind": "floating", "spread": "1.25"},
        ])
        self.assertEqual(schedule.rate_at(364, reference_rate="9"), D("3.0"))
        self.assertEqual(schedule.rate_at(365, reference_rate="2.5"), D("3.75"))
        with self.assertRaisesRegex(ValueError, "지표금리"):
            schedule.rate_at(365)

    def test_lifetime_floor_and_cap_bound_every_segment(self):
        schedule = RateSchedule.parse(
            [{"from_day": 0, "kind": "fixed", "rate": "1.0"},
             {"from_day": 30, "kind": "floating", "spread": "1.0"}],
            floor="2.0", cap="6.0")
        self.assertEqual(schedule.rate_at(0), D("2.0"))
        self.assertEqual(schedule.rate_at(30, reference_rate="9.0"), D("6.0"))
        self.assertEqual(schedule.rate_at(30, reference_rate="-3.0"), D("2.0"))

    def test_per_reset_limit_caps_how_far_a_reset_may_move_from_the_previous_rate(self):
        schedule = RateSchedule.parse(
            [{"from_day": 0, "kind": "fixed", "rate": "3.0"},
             {"from_day": 365, "kind": "floating", "spread": "1.0"}],
            max_change_per_reset="1.0")
        self.assertEqual(schedule.reset(D("3.0"), 365, reference_rate="6.0"), D("4.0"))
        self.assertEqual(schedule.reset(D("4.0"), 730, reference_rate="1.0"), D("3.0"))
        self.assertEqual(schedule.reset(D("3.0"), 730, reference_rate="2.5"), D("3.5"))
        # Inside a fixed segment a reset keeps the fixed rate regardless of the previous one.
        self.assertEqual(schedule.reset(D("9.0"), 10, reference_rate="6.0"), D("3.0"))

    def test_rounding_step_is_applied_half_up_before_the_limits(self):
        schedule = RateSchedule.parse([{"from_day": 0, "kind": "floating", "spread": "0.37"}],
                                      rounding_step="0.125", cap="5.0")
        self.assertEqual(schedule.rate_at(0, reference_rate="2.0"), D("2.375"))
        self.assertEqual(schedule.rate_at(0, reference_rate="2.07"), D("2.5"))
        self.assertEqual(schedule.rate_at(0, reference_rate="9"), D("5.0"))

    def test_serialisation_round_trips(self):
        schedule = RateSchedule.parse(
            [{"from_day": 0, "kind": "fixed", "rate": "2"}, {"from_day": 60, "kind": "floating", "spread": "1.5"}],
            floor="0", cap="12", max_change_per_reset="2", rounding_step="0.01")
        again = RateSchedule.from_json(schedule.to_json())
        self.assertEqual(again.to_json(), schedule.to_json())
        self.assertEqual(again.rate_at(60, reference_rate="3"), D("4.5"))
        self.assertEqual(again.segment_boundaries(), [0, 60])

    def test_invalid_schedules_are_rejected(self):
        bad = [
            ([], {}),                                                           # empty
            ([{"from_day": 5, "kind": "fixed", "rate": "2"}], {}),              # must start at day 0
            ([{"from_day": 0, "kind": "fixed", "rate": "2"},
              {"from_day": 0, "kind": "fixed", "rate": "3"}], {}),              # not strictly increasing
            ([{"from_day": 0, "kind": "weird", "rate": "2"}], {}),              # unknown kind
            ([{"from_day": 0, "kind": "fixed"}], {}),                           # fixed needs a rate
            ([{"from_day": 0, "kind": "floating"}], {}),                        # floating needs a spread
            ([{"from_day": 0, "kind": "fixed", "rate": "nan"}], {}),            # not finite
            ([{"from_day": 0, "kind": "fixed", "rate": "2"}], {"floor": "5", "cap": "4"}),
            ([{"from_day": 0, "kind": "fixed", "rate": "2"}], {"max_change_per_reset": "-1"}),
            ([{"from_day": 0, "kind": "fixed", "rate": "2"}], {"rounding_step": "0"}),
        ]
        for segments, options in bad:
            with self.subTest(segments=segments, options=options), self.assertRaises(ValueError):
                RateSchedule.parse(segments, **options)
        with self.assertRaises(ValueError):
            RateSchedule.parse([{"from_day": 0, "kind": "fixed", "rate": "2"}]).rate_at(-1)


if __name__ == "__main__":
    unittest.main()
