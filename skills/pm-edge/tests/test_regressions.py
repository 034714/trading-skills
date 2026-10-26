"""Offline regression tests for pm-edge. No network access required."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from pm_common import (analyze_books, event_month_day, full_fill, markdown_cell,
                       parse_bracket, passive_limit, to_celsius, tz, withdraw_fee)
from pm_temp_brackets import classify
from pm_barrier import terminal_prob, touch_prob


class BracketParsing(unittest.TestCase):
    def test_single_celsius(self):
        self.assertEqual(parse_bracket("26°C"), (26.0, 27.0, "C"))

    def test_two_degree_fahrenheit(self):
        low, high, unit = parse_bracket("62-63°F")
        self.assertEqual((low, high, unit), (62.0, 64.0, "F"))

    def test_open_ended(self):
        self.assertEqual(parse_bracket("26°C or below"), (None, 27.0, "C"))
        self.assertEqual(parse_bracket("29°C or higher"), (29.0, None, "C"))

    def test_fahrenheit_open_ended_keeps_unit(self):
        # Regression: "or below" used to be forced to Celsius, producing fake edges.
        self.assertEqual(parse_bracket("61°F or below"), (None, 62.0, "F"))

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_bracket("no numbers here"))


class MonotonicExclusion(unittest.TestCase):
    def test_high_above_bracket_is_impossible(self):
        self.assertEqual(classify((24.0, 25.0, "C"), 26.0, True, False), "IMPOSSIBLE_ABOVE")

    def test_high_equal_to_upper_bound(self):
        # Observed 27 already exceeds [26, 27).
        self.assertEqual(classify((26.0, 27.0, "C"), 27.0, True, False), "IMPOSSIBLE_ABOVE")

    def test_high_inside_bracket_is_not_final(self):
        # Holding the current extreme must NOT be reported as locked/final.
        self.assertEqual(classify((26.0, 27.0, "C"), 26.0, True, False), "STILL_POSSIBLE")

    def test_low_below_bracket_is_impossible(self):
        self.assertEqual(classify((20.0, 21.0, "C"), 18.0, False, False), "IMPOSSIBLE_BELOW")

    def test_low_inside_bracket_is_not_final(self):
        self.assertEqual(classify((20.0, 21.0, "C"), 20.0, False, False), "STILL_POSSIBLE")

    def test_integer_only_feed_is_unknown(self):
        self.assertEqual(classify((30.0, 31.0, "C"), 31.0, True, True), "UNKNOWN_PRECISION")

    def test_negative_temperatures(self):
        self.assertEqual(classify((-10.0, -9.0, "C"), -8.0, True, False), "IMPOSSIBLE_ABOVE")


class Dates(unittest.TestCase):
    def test_month_day_parsed(self):
        self.assertEqual(event_month_day("Highest temperature in Tokyo on October 23?"), (10, 23))

    def test_month_day_absent(self):
        self.assertIsNone(event_month_day("Will it rain?"))

    def test_timezone_has_dst(self):
        zone = tz("America/New_York")
        winter = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).astimezone(zone)
        summer = datetime(2026, 7, 15, 12, tzinfo=timezone.utc).astimezone(zone)
        self.assertNotEqual(winter.utcoffset(), summer.utcoffset())


class Fees(unittest.TestCase):
    def test_unknown_fee_stays_unknown(self):
        self.assertIsNone(withdraw_fee(0.5, None))

    def test_zero_fee_is_explicit(self):
        self.assertEqual(withdraw_fee(0.5, 0.0), 0.0)

    def test_fee_shape(self):
        self.assertAlmostEqual(withdraw_fee(0.5, 0.07), 0.0175)
        self.assertLess(withdraw_fee(0.95, 0.07), withdraw_fee(0.5, 0.07))


class BarrierModel(unittest.TestCase):
    def test_terminal_at_the_money_is_slightly_below_half(self):
        # Driftless GBM puts the median below the spot, so an at-the-money close is
        # a touch under 50%, not exactly 50%.
        value = terminal_prob(100.0, 100.0, 0.5, 0.01)
        self.assertLess(value, 0.5)
        self.assertAlmostEqual(value, 0.5, places=1)

    def test_terminal_monotone_in_barrier(self):
        low = terminal_prob(100.0, 110.0, 0.5, 0.05)
        high = terminal_prob(100.0, 120.0, 0.5, 0.05)
        self.assertGreater(low, high)

    def test_touch_exceeds_terminal(self):
        # Reaching a level at any time must be at least as likely as ending above it.
        self.assertGreaterEqual(touch_prob(100.0, 105.0, 0.4, 0.05),
                                terminal_prob(100.0, 105.0, 0.4, 0.05))

    def test_barrier_already_breached_is_certain(self):
        # Barrier below spot on a touch market: already touched, probability 1.
        self.assertEqual(touch_prob(100.0, 90.0, 0.4, 0.0), 1.0)


class FahrenheithConversion(unittest.TestCase):
    def test_freezing_point(self):
        self.assertAlmostEqual(to_celsius(32.0, "F"), 0.0)

    def test_celsius_passthrough(self):
        self.assertEqual(to_celsius(26.0, "C"), 26.0)


class FullFill(unittest.TestCase):
    def test_partial_liquidity_is_not_a_vwap(self):
        # Asking for 100 shares against 50 available must NOT return a "price"
        # that can be presented as a complete fill.
        out = full_fill([(0.40, 50)], shares=100)
        self.assertFalse(out["complete"])
        self.assertIsNone(out["vwap"])
        self.assertAlmostEqual(out["partial_vwap"], 0.40)
        self.assertAlmostEqual(out["shares"], 50)

    def test_walks_the_ladder_in_price_order(self):
        out = full_fill([(0.60, 10), (0.50, 90)], shares=100)
        self.assertTrue(out["complete"])
        self.assertAlmostEqual(out["cost"], 0.5 * 90 + 0.6 * 10)

    def test_budget_mode_respects_budget(self):
        out = full_fill([(0.50, 1000)], spend_usd=10)
        self.assertTrue(out["complete"])
        self.assertAlmostEqual(out["cost"], 10.0, places=6)
        self.assertAlmostEqual(out["shares"], 20.0, places=6)

    def test_invalid_inputs_rejected(self):
        for kwargs in ({}, {"shares": 0}, {"shares": float("nan")},
                       {"shares": 10, "spend_usd": 5}):
            with self.assertRaises(ValueError):
                full_fill([(0.5, 1)], **kwargs)
        with self.assertRaises(ValueError):
            full_fill([(1.5, 10)], shares=1)


class PairAnalysis(unittest.TestCase):
    def test_equal_share_both_ask_pair(self):
        out = analyze_books([], [(0.40, 100)], [], [(0.55, 100)], 100)
        self.assertTrue(out["pair"]["complete"])
        self.assertAlmostEqual(out["pair"]["gross_profit"], 5.0)
        self.assertIsNone(out["pair"]["net_profit"])       # fee unknown
        self.assertIsNone(out["yes_fee"])

    def test_one_leg_short_means_no_lock(self):
        out = analyze_books([], [(0.40, 50)], [], [(0.55, 100)], 100)
        self.assertFalse(out["pair"]["complete"])
        self.assertIsNone(out["pair"]["gross_profit"])
        self.assertIn("深度不足", out["pair"]["status"])

    def test_bids_alone_never_form_a_lock(self):
        # Buying NO at the ASK is required; a high YES bid is not a sellable pair leg.
        out = analyze_books([(0.99, 100)], [(0.60, 100)], [], [(0.60, 100)], 100)
        self.assertAlmostEqual(out["pair"]["gross_profit"], -20.0)

    def test_verified_zero_fee_is_net_zero_cost(self):
        out = analyze_books([], [(0.40, 100)], [], [(0.55, 100)], 100,
                            fee_per_share=0.0, fees_verified=True)
        self.assertAlmostEqual(out["pair"]["net_profit"], 5.0)

    def test_scenario_fee_is_labelled_not_verified(self):
        out = analyze_books([], [(0.40, 100)], [], [(0.55, 100)], 100, fee_rate=0.05)
        self.assertIsNone(out["pair"]["net_profit"])
        self.assertIsNotNone(out["pair"]["scenario_net_profit"])
        self.assertIn("未核实", out["fee_status"])

    def test_ratio_is_described_as_orders_not_probability(self):
        out = analyze_books([(0.2, 100)], [(0.4, 300)], [(0.5, 100)], [(0.6, 100)], 10)
        self.assertIn("不是人数、信心或概率", out["liquidity_basis"])
        self.assertIsNotNone(out["ask_no_share"])


class PassiveLimit(unittest.TestCase):
    def test_below_ask_by_ticks(self):
        self.assertAlmostEqual(passive_limit(0.40, 2, 0.01), 0.38)

    def test_tick_floor_applied(self):
        self.assertAlmostEqual(passive_limit(0.403, 0, 0.01) if False else
                               passive_limit(0.403, 1, 0.01), 0.39)

    def test_price_never_crosses_the_ask(self):
        self.assertIsNone(passive_limit(0.02, 5, 0.01))
        self.assertIsNone(passive_limit(0.01, 1, 0.01))

    def test_invalid_ticks(self):
        with self.assertRaises(ValueError):
            passive_limit(0.5, 0, 0.01)
        with self.assertRaises(ValueError):
            passive_limit(0.5, 1, 0)


class MarkdownSafety(unittest.TestCase):
    def test_pipes_and_newlines_cannot_break_table(self):
        self.assertEqual(markdown_cell("a|b\nc"), "a&#124;b c")


class WeatherBracketGeometry(unittest.TestCase):
    """Regression for the fabricated centre distance: a centre INSIDE the bracket
    being sold is zero distance away, not half a step."""

    def test_centre_inside_bracket_is_zero_steps(self):
        import pm_weather
        self.assertEqual(pm_weather._steps_from_centre(25.0, 26.0, 25.4, hk=True), 0.0)
        self.assertEqual(pm_weather._steps_from_centre(20.0, 20.0, 20.2, hk=False), 0.0)

    def test_outside_distance_is_integer_steps(self):
        import pm_weather
        # HK floor bracket 25 covers [25,26); centre 27.5 is 1.5 away.
        self.assertAlmostEqual(pm_weather._steps_from_centre(25.0, 26.0, 27.5, hk=True), 1.5)
        # US 70-71°F covers [69.5,71.5]; centre 74 is 2.5F past the edge = 1.25 widths.
        self.assertAlmostEqual(pm_weather._steps_from_centre(70.0, 71.0, 74.0, hk=False), 1.25)

    def test_open_ended_has_no_distance(self):
        import pm_weather
        self.assertIsNone(pm_weather._steps_from_centre(None, 26.0, 20.0, hk=False))


class BatchMapping(unittest.TestCase):
    """Regression: a batch reply zipped positionally silently attached one city's
    forecast to another. Mapping must fail closed instead."""

    def _stations(self):
        import pm_weather
        return pm_weather.STATIONS

    def test_coordinate_mismatch_rejected(self):
        import pm_weather
        sts = ["EPWA", "EGLC"]
        bad = [{"latitude": 51.5, "longitude": 0.05},     # London data labelled first
               {"latitude": 52.16, "longitude": 20.96}]
        with self.assertRaises(ValueError):
            pm_weather._mapped_batch(bad, sts, "test")

    def test_wrong_count_rejected(self):
        import pm_weather
        with self.assertRaises(ValueError):
            pm_weather._mapped_batch([{"latitude": 52.16, "longitude": 20.96}],
                                     ["EPWA", "EGLC"], "test")

    def test_single_location_dict_accepted(self):
        import pm_weather
        out = pm_weather._mapped_batch({"latitude": 52.16, "longitude": 20.96}, ["EPWA"], "t")
        self.assertEqual(out[0][0], "EPWA")

    def test_cache_key_depends_on_station_set(self):
        import pm_weather
        a = pm_weather._batch_cache_key("ens", "Asia/Tokyo", ["RJTT"], "2026-10-26")
        b = pm_weather._batch_cache_key("ens", "Asia/Tokyo", ["RKPK"], "2026-10-26")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
