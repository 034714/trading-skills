import contextlib
import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("crypto_ctx", Path(__file__).resolve().parents[1] / "scripts" / "crypto_ctx.py")
ctx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ctx)


class RegressionTests(unittest.TestCase):
    def args(self, *args):
        return ctx.parser().parse_args(args)

    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ctx.main(argv)
        return code, out.getvalue(), err.getvalue()

    def candles(self, interval, count):
        step = 86400000 if interval == "1d" else 14400000
        end = int(datetime.now(timezone.utc).timestamp() * 1000) // step * step - 1
        return [{"open_time": end - step * (count-i) + 1,
                 "close_time": end - step * (count-i-1),
                 "open": 100., "high": 101., "low": 99., "close": 100., "volume": 1.}
                for i in range(count)]

    def history(self, pair, interval, limit=300, **kwargs):
        return self.candles(interval, 240 if interval == "1d" else 100)

    def test_unclosed_candle_excluded(self):
        raw = [[0, "10", "11", "9", "10", "1", 99], [100, "10", "999", "9", "999", "1", 199]]
        with patch.object(ctx, "get", return_value=raw):
            self.assertEqual([r["close"] for r in ctx.klines("BTCUSDT", "1d", now_ms=199)], [10])

    def test_flat_rsi_and_vol(self):
        self.assertEqual(ctx.rsi([100] * 40), 50)
        self.assertEqual(ctx.realized_vol([100] * 40), 0)

    def test_unknown_budget_no_size(self):
        card = ctx.conditional_card(self.args("--bias", "long", "--entry", "100", "--stop", "90"))
        self.assertIsNone(card["units"])
        self.assertNotIn("gross_units_before_costs", card)

    def test_linear_sizing_and_configured_cap(self):
        args = self.args("--bias", "long", "--entry", "100", "--stop", "99", "--equity", "100", "--risk-pct", "5")
        self.assertEqual(ctx.conditional_card(args)["gross_units_before_costs"], 5)
        self.assertNotIn("exceeds", ctx.conditional_card(args)["sizing_status"])
        args.max_leverage = 2
        self.assertIn("exceeds", ctx.conditional_card(args)["sizing_status"])

    def test_neutral_and_auto_no_directional_position(self):
        for flags in (("--bias", "neutral"), ("--auto",)):
            args = self.args(*flags)
            ctx.validate(args)
            self.assertIsNone(ctx.conditional_card(args)["units"])
            self.assertNotIn("gross_R_targets", ctx.conditional_card(args))

    def test_invalid_parameters_before_network(self):
        cases = [["--entry", "nan"], ["--equity", "0"], ["--risk-pct", "101"],
                 ["--stop", "inf"], ["--min-days", "20", "--max-days", "10"],
                 ["--bias", "long"], ["--bias", "short", "--entry", "100", "--stop", "90"],
                 ["--bias", "neutral", "--entry", "0"], ["--auto", "--bias", "long"],
                 ["--expiry-hint", "invalid"], ["--unknown"], ["--equity", "bad"]]
        with patch.object(ctx, "get", side_effect=AssertionError("network must not run")):
            for flags in cases:
                with self.subTest(flags=flags):
                    code, out, err = self.invoke(flags + ["--json"])
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(out)["status"], "no_plan")
                    self.assertEqual(err, "")

    def test_expiry_tenor_and_no_oi_override(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        chain = [{"expiry": "10JAN30", "oi": 1}, {"expiry": "10JAN31", "oi": 1e9}]
        self.assertEqual(ctx.select_expiries(chain, 3, 45, now=now), ["10JAN30"])
        self.assertEqual(ctx.select_expiries(chain, 20, 45, now=now), [])
        for explicit in ("10JAN31", "15JAN30"):
            with self.assertRaises(ValueError):
                ctx.select_expiries(chain, 3, 45, explicit, now)

    def test_expired_and_empty_chains(self):
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.assertLess(ctx.expiry_days("01JAN29", now), 0)
        self.assertEqual(ctx.select_expiries([], 3, 45, now=now), [])

    def test_quotes_missing_not_free(self):
        with patch.object(ctx, "get", return_value={"result": [{"instrument_name": "BTC-10JAN30-100-C"}]}):
            row = ctx.option_chain("BTC")[0]
        self.assertIsNone(row["ask"])
        self.assertIsNone(row["mid_valuation_only"])
        self.assertFalse(row["specification_verified"])

    def test_json_includes_card_without_auto_noise(self):
        with patch.object(ctx, "klines", side_effect=self.history), patch.object(ctx, "option_chain", return_value=[]):
            for flags in (["--auto"], ["--bias", "neutral"], ["--bias", "long", "--entry", "100", "--stop", "90"]):
                code, out, err = self.invoke(flags + ["--json"])
                self.assertEqual(code, 0)
                report = json.loads(out)
                self.assertIn("conditional_order_card", report)
                self.assertEqual(report["option_candidates"], [])
                if flags == ["--auto"]:
                    card = report["conditional_order_card"]
                    self.assertEqual(card["entry"], 100)
                    self.assertAlmostEqual(card["stop_trigger"], 97.4)
                    self.assertEqual(card["bias"], "long")
                    self.assertIn("3", card["gross_R_targets"])
                    self.assertGreater(card["units_per_100_usd_risk_example"], 0)
                self.assertEqual(err, "")

    def test_text_starts_with_card(self):
        with patch.object(ctx, "klines", side_effect=self.history), patch.object(ctx, "option_chain", return_value=[]):
            code, out, err = self.invoke([])
            self.assertEqual(code, 0)
            self.assertTrue(out.startswith("条件挂单卡"))

    def test_option_failure_and_explicit_expiry_failure(self):
        with patch.object(ctx, "klines", side_effect=self.history), patch.object(ctx, "option_chain", side_effect=ValueError("offline")):
            code, out, _ = self.invoke(["--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["options_status"], "unavailable")
            code, out, _ = self.invoke(["--json", "--expiry-hint", "10JAN30"])
            self.assertEqual(code, 2)
            self.assertIn("explicit expiry", json.loads(out)["error"])

    def test_network_and_insufficient_history_errors(self):
        for side_effect, return_value in ((OSError("offline"), None), (None, [])):
            with patch.object(ctx, "klines", side_effect=side_effect, return_value=return_value):
                code, out, _ = self.invoke(["--json"])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)["status"], "no_plan")

    def test_stale_and_gap_errors(self):
        for mutation in ("stale", "gap"):
            rows = self.history("BTCUSDT", "1d")
            if mutation == "stale":
                for row in rows:
                    row["open_time"] -= 864000000
                    row["close_time"] -= 864000000
            else:
                rows[20]["open_time"] += 1
            with patch.object(ctx, "klines", return_value=rows):
                code, out, _ = self.invoke(["--json"])
                self.assertEqual(code, 2)

    def test_negative_target_not_emitted(self):
        card = ctx.conditional_card(self.args("--bias", "short", "--entry", "100", "--stop", "200"))
        self.assertTrue(all(v is None for v in card["gross_R_targets"].values()))


if __name__ == "__main__":
    unittest.main()
