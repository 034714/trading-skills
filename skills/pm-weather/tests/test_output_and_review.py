"""Offline tests for the recommendation table, market ladder evidence and pick log."""
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pm_weather


class MakerRequiresExplicitTick(unittest.TestCase):
    """Quote decimals must never fabricate a legal tick / executable maker price."""

    def test_unknown_tick_has_no_maker(self):
        r = {"p_calibrated": 0.5, "yes_ask": 0.512, "yes_bid": 0.491,
             "yes_vwap": 0.512, "yes_complete": True}
        self.assertIsNone(pm_weather.side_metrics(r)["yes"]["maker"])

    def test_explicit_aligned_tick_yields_passive_maker(self):
        r = {"p_calibrated": 0.5, "tick_size": 0.01, "yes_ask": 0.51, "yes_bid": 0.49,
             "yes_vwap": 0.51, "yes_complete": True}
        self.assertAlmostEqual(pm_weather.side_metrics(r)["yes"]["maker"], 0.50)

    def test_off_grid_quotes_do_not_produce_maker(self):
        r = {"p_calibrated": 0.5, "tick_size": 0.01, "yes_ask": 0.515, "yes_bid": 0.495,
             "yes_vwap": 0.515, "yes_complete": True}
        self.assertIsNone(pm_weather.side_metrics(r)["yes"]["maker"])


class MarketLadderStats(unittest.TestCase):
    def rows(self):
        return [
            {"event_id": "e1", "kind": "high", "code": "ZBAA", "label": "14°C",
             "unit": "C", "bid": 0.05, "ask": 0.07, "centre": 12.7},
            {"event_id": "e1", "kind": "high", "code": "ZBAA", "label": "15°C",
             "unit": "C", "bid": 0.32, "ask": 0.34, "centre": 12.7},
            {"event_id": "e1", "kind": "high", "code": "ZBAA", "label": "16°C",
             "unit": "C", "bid": 0.48, "ask": 0.52, "centre": 12.7},
        ]

    def test_centre_top_and_gap(self):
        rows = self.rows()
        pm_weather.market_ladder_stats(rows)
        # mids: 14->0.06, 15->0.33, 16->0.50; centre = 15.49
        self.assertAlmostEqual(rows[0]["market_centre"], 15.49, places=2)
        self.assertEqual(rows[0]["market_top_label"], "16°C")
        self.assertAlmostEqual(rows[0]["market_top_p"], 0.5 / 0.89, places=3)
        self.assertAlmostEqual(rows[0]["market_model_gap"], 15.49 - 12.7, places=2)
        self.assertTrue(rows[0]["market_model_gap_flag"])
        self.assertEqual(rows[0]["market_coverage"], 3)

    def test_missing_quotes_shrink_coverage(self):
        rows = self.rows()
        rows[1]["bid"] = rows[1]["ask"] = None
        pm_weather.market_ladder_stats(rows)
        self.assertEqual(rows[0]["market_coverage"], 2)

    def test_fahrenheit_threshold_is_wider(self):
        rows = [
            {"event_id": "e2", "kind": "high", "code": "KLGA", "label": "60-61°F",
             "unit": "F", "bid": 0.4, "ask": 0.6, "centre": 58.0},
            {"event_id": "e2", "kind": "high", "code": "KLGA", "label": "59-60°F",
             "unit": "F", "bid": 0.3, "ask": 0.5, "centre": 58.0},
        ]
        pm_weather.market_ladder_stats(rows)
        # centre about 60.0F, gap about 2.0F < 2.7F threshold
        self.assertFalse(rows[0]["market_model_gap_flag"])


class SideMetrics(unittest.TestCase):
    def test_taker_maker_profit(self):
        r = {"tick_size": 0.01, "p_calibrated": 0.30,
             "yes_ask": 0.10, "yes_bid": 0.08, "yes_vwap": 0.10, "yes_complete": True,
             "no_ask": 0.92, "no_bid": 0.90, "no_vwap": 0.92, "no_complete": True,
             "yes_depth": 100.0, "no_depth": 50.0}
        m = pm_weather.side_metrics(r)
        self.assertAlmostEqual(m["yes"]["taker"], 0.10)
        self.assertAlmostEqual(m["yes"]["maker"], 0.09)
        self.assertAlmostEqual(m["yes"]["profit"], 0.90)
        self.assertAlmostEqual(m["yes"]["profit_pct"], 9.0)
        self.assertAlmostEqual(m["yes"]["depth_shares"], 1000.0)
        self.assertAlmostEqual(m["no"]["win"], 0.70)
        self.assertAlmostEqual(m["no"]["edge"], -0.22)

    def test_one_tick_spread_joins_queue(self):
        r = {"tick_size": 0.01, "p_calibrated": 0.5, "yes_ask": 0.50, "yes_bid": 0.49,
             "yes_vwap": 0.50, "yes_complete": True}
        m = pm_weather.side_metrics(r)
        self.assertEqual(m["yes"]["maker"], 0.49)

    def test_recommend_side_picks_best_complete(self):
        r = {"sides": {"yes": {"complete": True, "edge": 0.1, "taker": 0.4},
                       "no": {"complete": True, "edge": 0.2, "taker": 0.5}}}
        side, m = pm_weather.recommend_side(r)
        self.assertEqual(side, "no")
        self.assertEqual(m["edge"], 0.2)


class GradePick(unittest.TestCase):
    def test_metar_round_half_up(self):
        rec = {"code": "ZBAA", "label": "16°C", "side": "NO"}
        self.assertFalse(pm_weather.grade_pick(rec, 16.0))
        self.assertFalse(pm_weather.grade_pick(rec, 16.4))
        self.assertTrue(pm_weather.grade_pick(rec, 15.4))
        self.assertTrue(pm_weather.grade_pick(rec, 16.5))   # rounds to 17
        self.assertTrue(pm_weather.grade_pick({**rec, "side": "YES"}, 16.4))

    def test_hko_floor_rule(self):
        rec = {"code": "HKO", "label": "22°C", "side": "NO"}
        self.assertFalse(pm_weather.grade_pick(rec, 22.9))
        self.assertTrue(pm_weather.grade_pick(rec, 23.0))
        self.assertTrue(pm_weather.grade_pick(rec, 21.9))


class PickLog(unittest.TestCase):
    def test_parse_ok(self):
        rec = pm_weather.parse_pick_arg(
            "id=123,side=no,price=0.51,label=16°C,code=ZBAA,kind=high,date=2026-11-10")
        self.assertEqual(rec["side"], "NO")
        self.assertEqual(rec["code"], "ZBAA")
        self.assertEqual(rec["date"], "2026-11-10")
        self.assertEqual(rec["unit"], "C")

    def test_parse_missing_field(self):
        with self.assertRaises(ValueError):
            pm_weather.parse_pick_arg("id=123,side=NO,price=0.51")

    def test_parse_bad_price(self):
        with self.assertRaises(ValueError):
            pm_weather.parse_pick_arg(
                "id=1,side=NO,price=1.2,label=16°C,code=ZBAA,kind=high,date=2026-11-10")


class RapidTable(unittest.TestCase):
    def row(self):
        return {
            "city": "北京", "kind": "high", "label": "16°C", "unit": "C",
            "centre": 12.7, "market_centre": 15.7, "market_model_gap": 3.0,
            "market_model_gap_flag": True, "spread": 2.6,
            "market_top_label": "16°C", "market_top_p": 0.56,
            "event_vol": 7000.0, "vol": 500.0,
            "yes_ask": 0.54, "yes_bid": 0.49, "yes_vwap": 0.54, "yes_complete": True,
            "no_ask": 0.51, "no_bid": 0.46, "no_vwap": 0.51, "no_complete": True,
            "yes_depth": 100.0, "no_depth": 200.0,
            "p_calibrated": 0.10, "p_stress": 0.2,
        }

    def test_headers_and_row(self):
        r = self.row()
        r["sides"] = pm_weather.side_metrics(r)
        text = pm_weather.fmt_table([r], 20, 0.04, "both", 5, 9.0,
                                    target=date(2026, 11, 10), brief=True)
        for header in ("吃价 / 挂价", "盈利空间", "天气预报模型", "成交量 / 深度"):
            self.assertIn(header, text)
        self.assertIn("买 NO", text)
        self.assertIn("+$0.49", text)
        self.assertIn("差 +3.0⚠", text)
        self.assertIn("众数 16°C 56%", text)

    def test_empty_message(self):
        text = pm_weather.fmt_table([], 20, 0.04, "both", 5, 9.0,
                                    target=None, brief=True)
        self.assertIn("没有同时满足", text)

    def test_min_win_filters_rapid_table(self):
        r = self.row()
        r["sides"] = pm_weather.side_metrics(r)
        text = pm_weather.fmt_table([r], 20, 0.04, "both", 5, 9.0,
                                    target=None, brief=True, min_win=0.95)
        self.assertIn("没有同时满足", text)

    def test_min_event_vol_filters_rapid_table(self):
        r = self.row()
        r["sides"] = pm_weather.side_metrics(r)
        text = pm_weather.fmt_table([r], 20, 0.04, "both", 5, 9.0,
                                    target=None, brief=True, min_event_vol=10000)
        self.assertIn("没有同时满足", text)


class AttachOrdering(unittest.TestCase):
    def test_completion_flags_reach_side_metrics(self):
        row = {"yes_token": "y", "no_token": "n", "city": "X", "kind": "high",
               "label": "17°C", "p_calibrated": 0.04}
        info = {"yes_fill": {"complete": True, "vwap": 0.10, "partial_vwap": 0.10},
                "no_fill": {"complete": True, "vwap": 0.90, "partial_vwap": 0.90},
                "yes_vwap": 0.10, "no_vwap": 0.90,
                "yes_bid": 0.09, "yes_ask": 0.11,
                "no_bid": 0.89, "no_ask": 0.91,
                "yes_depth": 10.0, "no_depth": 10.0}
        old_book, old_analyze = pm_weather.book, pm_weather.analyze_books
        pm_weather.book = lambda token, attempts=3: ([], [])
        pm_weather.analyze_books = lambda *args, **kwargs: info
        try:
            pm_weather.attach_books([row], 20, workers=1)
        finally:
            pm_weather.book, pm_weather.analyze_books = old_book, old_analyze
        self.assertTrue(row["sides"]["yes"]["complete"])
        self.assertTrue(row["sides"]["no"]["complete"])
        self.assertAlmostEqual(row["sides"]["no"]["edge"], 0.96 - 0.90)


class ReviewFlow(unittest.TestCase):
    def test_review_counts_only_logged_recommendations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "picks.jsonl"
            records = [
                {"id": "1", "side": "NO", "price": 0.51, "label": "16°C",
                 "code": "ZBAA", "kind": "high", "date": "2026-11-08", "unit": "C"},
                {"id": "2", "side": "YES", "price": 0.60, "label": "10°C",
                 "code": "ZBAA", "kind": "high", "date": "2026-11-08", "unit": "C"},
            ]
            path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            old_picks, old_value = pm_weather.PICKS, pm_weather.settled_value
            pm_weather.PICKS = path
            pm_weather.settled_value = lambda code, kind, target: 10.0
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = pm_weather.review_picks()
                text = buf.getvalue()
                self.assertEqual(code, 2)
                self.assertIn("2 单待核验", text)
                self.assertNotIn("赢 2 单", text)
                self.assertNotIn("+0.890", text)
            finally:
                pm_weather.PICKS, pm_weather.settled_value = old_picks, old_value


if __name__ == "__main__":
    unittest.main()