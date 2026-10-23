"""Offline regression tests for pm-edge. No network access required."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from pm_common import (event_month_day, parse_bracket, to_celsius, tz, withdraw_fee)
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


if __name__ == "__main__":
    unittest.main()
