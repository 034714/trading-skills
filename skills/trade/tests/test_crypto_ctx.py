import contextlib
import importlib.util
import io
import json
from datetime import datetime, timezone
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

    def candles(self, interval, count, closes=None):
        step = 86400000 if interval == "1d" else 14400000
        end = int(datetime.now(timezone.utc).timestamp() * 1000) // step * step - 1
        rows = []
        for i in range(count):
            close = closes[i] if closes else 100.0
            prev = closes[i - 1] if closes and i > 0 else 100.0
            high = max(prev, close) + 1.0
            low = min(prev, close) - 1.0
            rows.append({"open_time": end - step * (count - i) + 1,
                         "close_time": end - step * (count - i - 1),
                         "open": prev, "high": high, "low": low, "close": close, "volume": 1.0})
        return rows

    def history(self, pair, interval, limit=300, **kwargs):
        return self.candles(interval, 240 if interval == "1d" else 100)

    def test_unclosed_candle_excluded(self):
        raw = [[0, "10", "11", "9", "10", "1", 99], [100, "10", "999", "9", "999", "1", 199]]
        with patch.object(ctx, "get", return_value=raw):
            self.assertEqual([r["close"] for r in ctx.klines("BTCUSDT", "1d", now_ms=199)], [10])

    def test_flat_rsi_and_vol(self):
        self.assertEqual(ctx.rsi([100] * 40), 50)
        self.assertEqual(ctx.realized_vol([100] * 40), 0)

    def test_sig_rounding(self):
        self.assertEqual(ctx.sig(81189.432123456), 81189.4)
        self.assertEqual(ctx.sig(0.021750123, 4), 0.02175)
        self.assertEqual(ctx.sig(0), 0.0)
        self.assertIsNone(ctx.sig(None))

    def test_symbol_parsing_and_limits(self):
        args = self.args("--symbols", "btc, eth,BTC")
        ctx.validate(args)
        self.assertEqual(args.symbol_list, ["BTC", "ETH"])
        args = self.args("--symbol", "sol")
        ctx.validate(args)
        self.assertEqual(args.symbol_list, ["SOL"])
        with self.assertRaises(ValueError):
            ctx.validate(self.args("--symbols", "BTC,ETH,SOL,BNB,XRP"))
        with self.assertRaises(ValueError):
            ctx.validate(self.args("--symbol", "BTC", "--symbols", "SOL"))

    def test_ladder_levels(self):
        self.assertEqual(ctx.ladder_levels("long", 100, 90, 3, 4), [100, 98, 96])
        self.assertEqual(ctx.ladder_levels("short", 100, 110, 3, 4), [100, 102, 104])
        self.assertEqual(ctx.ladder_levels("long", 100, 85, 3, 40), [100])
        self.assertEqual(ctx.ladder_levels("long", 100, 90, 1, 4), [100])

    def test_card_ladder_math(self):
        args = self.args("--bias", "long", "--entry", "100", "--stop", "90")
        card = ctx.conditional_card(args, atr_value=4)
        self.assertEqual(card["tranche_ladder"]["levels"], [100.0, 98.0, 96.0])
        self.assertEqual(card["tranche_ladder"]["avg_entry"], 98.0)
        self.assertEqual(card["gross_R_targets"]["1"], 106.0)
        self.assertEqual(card["units_per_100_usd_risk_example"], 12.5)
        card = ctx.conditional_card(self.args("--bias", "long", "--entry", "100", "--stop", "90",
                                              "--tranches", "1"), atr_value=4)
        self.assertNotIn("tranche_ladder", card)
        self.assertEqual(card["gross_R_targets"]["1"], 110.0)

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
                 ["--expiry-hint", "invalid"], ["--unknown"], ["--equity", "bad"],
                 ["--tranches", "0"], ["--tranches", "6"], ["--symbols", ""],
                 ["--symbols", "BT-C"], ["--scan-list", "X"]]
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

    def test_json_envelope_two_symbols_and_auto_card(self):
        with patch.object(ctx, "klines", side_effect=self.history), \
             patch.object(ctx, "option_chain", return_value=[]), \
             patch.object(ctx, "fetch_live", return_value={}):
            for flags in (["--auto"], ["--bias", "neutral"], ["--bias", "long", "--entry", "100", "--stop", "90"]):
                code, out, err = self.invoke(flags + ["--json"])
                self.assertEqual(code, 0)
                report = json.loads(out)
                self.assertEqual(report["schema"], "trade_ctx_v2")
                self.assertEqual([s["symbol"] for s in report["symbols"]], ["BTC", "ETH"])
                self.assertNotIn("\n", out.strip())
                for sym in report["symbols"]:
                    card = sym["conditional_order_card"]
                    self.assertIn("conditional_order_card", sym)
                    self.assertEqual(sym["option_candidates"], [])
                    self.assertIn("live_snapshot", sym)
                    self.assertIn("options_fit_note", sym)
                if flags == ["--auto"]:
                    card = report["symbols"][0]["conditional_order_card"]
                    self.assertEqual(card["entry"], 100)
                    self.assertAlmostEqual(card["stop_trigger"], 97.4)
                    self.assertEqual(card["bias"], "long")
                    self.assertIn("3", card["gross_R_targets"])
                    self.assertGreater(card["units_per_100_usd_risk_example"], 0)
                self.assertEqual(err, "")

    def test_text_starts_with_card(self):
        with patch.object(ctx, "klines", side_effect=self.history), \
             patch.object(ctx, "option_chain", return_value=[]), \
             patch.object(ctx, "fetch_live", return_value={}):
            code, out, err = self.invoke([])
            self.assertEqual(code, 0)
            self.assertTrue(out.startswith("条件挂单卡"))

    def test_live_snapshot_degrades_gracefully(self):
        with patch.object(ctx, "get", side_effect=OSError("offline")):
            live = ctx.fetch_live("BTCUSDT")
        self.assertEqual(set(live.get("unavailable", [])),
                         {"spot", "ticker_24h", "premium", "open_interest", "oi_hist", "lsr"})
        self.assertNotIn("spot", live)

    def test_scan_rows_descriptive(self):
        rising = self.candles("1d", 240, closes=[100.0 + i for i in range(240)])
        with patch.object(ctx, "klines", return_value=rising), \
             patch.object(ctx, "get", return_value={"lastFundingRate": "0.001"}):
            row = ctx.fetch_scan_row("SOL")
        self.assertEqual(row["symbol"], "SOL")
        for flag in ("RSI>=70", "贴近90日高点", "均线多头排列", "正资金费率偏高"):
            self.assertIn(flag, row["flags"])
        self.assertEqual(row["funding_8h"], 0.001)
        self.assertEqual(row["ema_stack"], "bull")

    def test_scan_row_marks_insufficient_history(self):
        with patch.object(ctx, "klines", return_value=self.candles("1d", 100)), \
             patch.object(ctx, "get", side_effect=OSError("offline")):
            row = ctx.fetch_scan_row("DOGE")
        self.assertIn("note", row)

    def test_partial_symbol_failure_still_outputs(self):
        def klines_fail_eth(pair, interval, limit=300, **kwargs):
            if pair == "ETHUSDT":
                raise OSError("eth down")
            return self.history(pair, interval, limit)

        with patch.object(ctx, "klines", side_effect=klines_fail_eth), \
             patch.object(ctx, "option_chain", return_value=[]), \
             patch.object(ctx, "fetch_live", return_value={}):
            code, out, _ = self.invoke(["--json"])
            self.assertEqual(code, 0)
            report = json.loads(out)
            self.assertEqual(report["partial_failures"], ["ETH"])
            self.assertEqual(report["symbols"][1]["symbol"], "ETH")
            self.assertIn("error", report["symbols"][1])

    def test_option_failure_and_explicit_expiry_failure(self):
        with patch.object(ctx, "klines", side_effect=self.history), \
             patch.object(ctx, "option_chain", side_effect=ValueError("offline")), \
             patch.object(ctx, "fetch_live", return_value={}):
            code, out, _ = self.invoke(["--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["symbols"][0]["options_status"], "unavailable")
            code, out, _ = self.invoke(["--json", "--expiry-hint", "10JAN30"])
            self.assertEqual(code, 2)
            self.assertIn("explicit expiry", json.loads(out)["error"])

    def test_network_and_insufficient_history_errors(self):
        for side_effect, return_value in ((OSError("offline"), None), (None, [])):
            with patch.object(ctx, "klines", side_effect=side_effect, return_value=return_value), \
                 patch.object(ctx, "fetch_live", return_value={}):
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
            with patch.object(ctx, "klines", return_value=rows), \
                 patch.object(ctx, "fetch_live", return_value={}):
                code, out, _ = self.invoke(["--json"])
                self.assertEqual(code, 2)

    def test_negative_target_not_emitted(self):
        card = ctx.conditional_card(self.args("--bias", "short", "--entry", "100", "--stop", "200"))
        self.assertTrue(all(v is None for v in card["gross_R_targets"].values()))

    def test_multi_timeframe_block(self):
        with patch.object(ctx, "klines", side_effect=self.history), \
             patch.object(ctx, "option_chain", return_value=[]), \
             patch.object(ctx, "fetch_live", return_value={}):
            code, out, _ = self.invoke(["--json"])
            self.assertEqual(code, 0)
            mtf = json.loads(out)["symbols"][0]["multi_timeframe"]
            self.assertEqual(mtf["1h"]["rsi14"], 50.0)
            self.assertEqual(mtf["1h"]["state"], "mixed")
            self.assertIn("rsi14", mtf["4h"])
            self.assertIn("nearest_4h_pivot", mtf)
            self.assertIn("kind", mtf["nearest_4h_pivot"])
            self.assertIsInstance(mtf["intraday_flags"], list)

    def test_multi_timeframe_1h_failure_tolerated(self):
        def klines_no_1h(pair, interval, limit=300, **kwargs):
            if interval == "1h":
                raise OSError("1h down")
            return self.history(pair, interval, limit)

        with patch.object(ctx, "klines", side_effect=klines_no_1h), \
             patch.object(ctx, "option_chain", return_value=[]), \
             patch.object(ctx, "fetch_live", return_value={}):
            code, out, _ = self.invoke(["--json"])
            self.assertEqual(code, 0)
            mtf = json.loads(out)["symbols"][0]["multi_timeframe"]
            self.assertEqual(mtf["1h"].get("note"), "unavailable_or_insufficient")
            self.assertIn("rsi14", mtf["4h"])
            self.assertNotIn("1H超买", "".join(mtf["intraday_flags"]))

    def test_multi_timeframe_intraday_flags(self):
        rising_1d = self.candles("1d", 240, closes=[100.0 + i for i in range(240)])
        rising_1h = self.candles("1h", 300, closes=[400.0 + i for i in range(300)])
        seq = {"1d": rising_1d, "4h": self.candles("4h", 100, closes=[380.0 + i for i in range(100)]),
               "1h": rising_1h}
        with patch.object(ctx, "klines", side_effect=lambda p, i, limit=300, **kw: seq[i]), \
             patch.object(ctx, "option_chain", return_value=[]), \
             patch.object(ctx, "fetch_live", return_value={}):
            code, out, _ = self.invoke(["--json"])
            self.assertEqual(code, 0)
            mtf = json.loads(out)["symbols"][0]["multi_timeframe"]
            self.assertGreaterEqual(mtf["1h"]["rsi14"], 70)
            self.assertTrue(any("1H超买" in f for f in mtf["intraday_flags"]))

    def test_human_output_contains_multi_timeframe_line(self):
        with patch.object(ctx, "klines", side_effect=self.history), \
             patch.object(ctx, "option_chain", return_value=[]), \
             patch.object(ctx, "fetch_live", return_value={}):
            code, out, _ = self.invoke([])
            self.assertEqual(code, 0)
            self.assertIn("多周期", out)
            self.assertIn("日内标记", out)


if __name__ == "__main__":
    unittest.main()
