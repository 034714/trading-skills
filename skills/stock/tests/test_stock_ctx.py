import contextlib
import importlib.util
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("stock_ctx", Path(__file__).resolve().parents[1] / "scripts" / "stock_ctx.py")
ctx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ctx)


def gate_contract(name, ctype="stocks", mark="63.86", index="63.80", pos="14000", create=1776000000):
    return {"name": name, "contract_type": ctype, "mark_price": mark, "index_price": index,
            "last_price": mark, "funding_rate": "0", "funding_rate_indicative": "0",
            "funding_interval": 3600, "leverage_min": "1", "leverage_max": "50",
            "maker_fee_rate": "-0.0001", "taker_fee_rate": "0.00075",
            "quanto_multiplier": "1", "order_size_min": 1, "position_size": pos,
            "long_users": 411, "short_users": 263, "status": "trading",
            "in_delisting": False, "enable_circuit_breaker": False, "create_time": create}


def gate_candles(interval, count, close=100.0, unclosed=True):
    step = 86400 if interval == "1d" else 14400 if interval == "4h" else 3600
    now_s = int(time.time())
    end = now_s // step * step
    rows = []
    for i in range(count):
        t = end - step * (count - i)
        rows.append({"t": t, "o": close, "h": close + 1, "l": close - 1, "c": close, "v": 100.0})
    if unclosed:
        rows.append({"t": end, "o": close, "h": close + 1, "l": close - 1, "c": close, "v": 5.0})
    return rows


def yahoo_payload(symbol, interval, count, close=200.0, last=None, currency="USD"):
    step = 86400 if interval == "1d" else 3600
    now_s = int(time.time())
    end = now_s // step * step - step
    ts, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    for i in range(count):
        t = end - step * (count - 1 - i)
        ts.append(t)
        for arr in (opens, highs, lows, closes):
            arr.append(close)
        highs[-1] = close + 1
        lows[-1] = close - 1
        volumes.append(1000.0)
    return {"chart": {"result": [{"timestamp": ts,
                                  "indicators": {"quote": [{"open": opens, "high": highs, "low": lows,
                                                            "close": closes, "volume": volumes}]},
                                  "meta": {"currency": currency, "regularMarketPrice": last if last is not None else close}}],
                      "error": None}}


class RegressionTests(unittest.TestCase):
    def setUp(self):
        ctx._CONTRACT_CACHE = None
        self.contracts = [gate_contract("ZHIPU_USDT"),
                          gate_contract("AAPL_USDT", mark="196.0", index="195.9", pos="90000"),
                          gate_contract("MINIMAX_USDT", mark="23.8", index="23.7", pos="40000"),
                          gate_contract("BTC_USDT", ctype="")]

    def invoke(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ctx.main(argv)
        return code, out.getvalue(), err.getvalue()

    def fake_get(self, url):
        query = dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&")) if "?" in url else {}
        if "/futures/usdt/contracts" in url:
            return self.contracts
        if "/futures/usdt/tickers" in url:
            return [{"contract": c["name"], "volume_24h_quote": "1000000"} for c in self.contracts]
        if "candlesticks" in url:
            interval = query.get("interval", "1d")
            count = 120 if interval != "1d" else getattr(self, "gate_daily_count", 60)
            return gate_candles(interval, count)
        if "finance/chart" in url:
            symbol = url.split("/chart/")[1].split("?")[0]
            interval = query.get("interval", "1d")
            count = 260 if interval == "1d" else 120
            last = getattr(self, "yahoo_last", None)
            return yahoo_payload(symbol, interval, count, last=last)
        raise AssertionError("unexpected url " + url)

    def run_report(self, argv):
        with patch.object(ctx, "get", side_effect=self.fake_get):
            return self.invoke(argv)

    def test_no_symbols_requires_list_hint(self):
        code, out, err = self.invoke(["--json"])
        self.assertEqual(code, 2)
        self.assertIn("--list", json.loads(out)["error"])

    def test_invalid_parameters_before_network(self):
        cases = [["--symbols", "ZHIPU,UNITREE,ZHIPU2,CXMT,X"], ["--symbols", "ZHIPU-X"],
                 ["--symbols", "ZHIPU", "--tranches", "0"], ["--symbols", "ZHIPU", "--bias", "long"],
                 ["--symbols", "ZHIPU", "--bias", "short", "--entry", "100", "--stop", "90"],
                 ["--symbols", "ZHIPU", "--entry", "100"], ["--symbols", "ZHIPU", "--auto", "--bias", "long"]]
        with patch.object(ctx, "get", side_effect=AssertionError("network must not run")):
            for flags in cases:
                with self.subTest(flags=flags):
                    code, out, err = self.invoke(flags + ["--json"])
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(out)["status"], "no_plan")

    def test_unknown_and_non_stock_contracts(self):
        code, out, _ = self.run_report(["--symbols", "NOPE", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("--list", json.loads(out)["error"])
        code, out, _ = self.run_report(["--symbols", "BTC", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("trade", json.loads(out)["error"])

    def test_gate_klines_dict_parsing_drops_unclosed(self):
        now = datetime.now(timezone.utc)
        data = gate_candles("1d", 5)
        with patch.object(ctx, "get", return_value=data):
            rows = ctx.gate_klines("ZHIPU_USDT", "1d", 10, now)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[-1]["close_time"] % 86400000, 86399999 // 1000 * 1000 + 999)

    def test_yahoo_parsing_skips_none_closes(self):
        payload = yahoo_payload("AAPL", "1d", 5)
        payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][2] = None
        with patch.object(ctx, "get", return_value=payload):
            rows, meta = ctx.yahoo_chart("AAPL", "1d", "5d")
        self.assertEqual(len(rows), 4)
        self.assertEqual(meta["currency"], "USD")

    def test_ny_offset_and_session(self):
        summer = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)   # 11:00 NY Monday
        winter = datetime(2026, 1, 12, 15, 0, tzinfo=timezone.utc)   # 10:00 NY Monday
        self.assertEqual(ctx.ny_offset_minutes(summer), -240)
        self.assertEqual(ctx.ny_offset_minutes(winter), -300)
        self.assertEqual(ctx.ny_session(summer), "regular")
        self.assertEqual(ctx.ny_session(winter), "regular")
        saturday = datetime(2026, 7, 11, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(ctx.ny_session(saturday), "weekend_closed")
        early = datetime(2026, 7, 13, 7, 0, tzinfo=timezone.utc)     # 03:00 NY
        self.assertEqual(ctx.ny_session(early), "closed")
        pre = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)      # 08:00 NY
        self.assertEqual(ctx.ny_session(pre), "pre_market")
        after = datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)    # 18:00 NY
        self.assertEqual(ctx.ny_session(after), "after_hours")

    def test_completed_yahoo_daily_drops_in_progress(self):
        now = datetime(2026, 7, 13, 19, 0, tzinfo=timezone.utc)      # 15:00 NY, regular session
        rows = [{"open_time": int(datetime(2026, 7, 10, 13, 30, tzinfo=timezone.utc).timestamp()) * 1000,
                 "close_time": 0, "open": 1, "high": 2, "low": 0.5, "close": 1, "volume": 1},
                {"open_time": int(datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc).timestamp()) * 1000,
                 "close_time": 0, "open": 1, "high": 2, "low": 0.5, "close": 1, "volume": 1}]
        self.assertEqual(len(ctx.completed_yahoo_daily(rows, now)), 1)
        closed_now = datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)  # after hours, bar complete
        self.assertEqual(len(ctx.completed_yahoo_daily(rows, closed_now)), 2)

    def test_usd_symbol_uses_yahoo_indicators(self):
        code, out, _ = self.run_report(["--symbols", "AAPL", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        sym = report["symbols"][0]
        self.assertEqual(sym["indicators"]["source"], "yahoo_primary_usd")
        self.assertIn("ema200", sym["indicators"])
        self.assertEqual(sym["underlying"]["role"], "primary_usd_underlying")
        self.assertIn("gate_vs_underlying_pct", sym["premium"])
        self.assertEqual(sym["multi_timeframe"]["1h"]["source"], "yahoo_regular_hours")
        self.assertNotIn("\n", out.strip())

    def test_gate_only_symbol_flags_single_venue(self):
        code, out, _ = self.run_report(["--symbols", "ZHIPU", "--json"])
        self.assertEqual(code, 0)
        sym = json.loads(out)["symbols"][0]
        self.assertEqual(sym["indicators"]["source"], "gate_24h_perp")
        self.assertNotIn("ema200", sym["indicators"])
        self.assertIn("history_note", sym["indicators"])
        joined = "".join(sym["warnings"])
        self.assertIn("单一场所", joined)
        self.assertIn("熔断", joined)
        self.assertEqual(sym["multi_timeframe"]["1h"]["source"], "gate_24h")

    def test_non_usd_underlying_context_only(self):
        code, out, _ = self.run_report(["--symbols", "MINIMAX", "--json"])
        self.assertEqual(code, 0)
        sym = json.loads(out)["symbols"][0]
        self.assertEqual(sym["underlying"]["role"], "context_only_non_usd_underlying")
        self.assertEqual(sym["indicators"]["source"], "gate_24h_perp")
        self.assertNotIn("gate_vs_underlying_pct", sym["premium"])

    def test_underlying_mismatch_warning(self):
        self.yahoo_last = 150.0  # vs gate mark 196 -> >5% deviation
        code, out, _ = self.run_report(["--symbols", "AAPL", "--json"])
        sym = json.loads(out)["symbols"][0]
        self.assertTrue(any("偏差超5%" in w for w in sym["warnings"]))

    def test_auto_cards_and_partial_failure(self):
        self.gate_daily_count = 60
        code, out, _ = self.run_report(["--symbols", "ZHIPU,NOPE", "--json", "--auto"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["schema"], "stock_ctx_v1")
        self.assertEqual(report["partial_failures"], ["NOPE"])
        card = report["symbols"][0]["conditional_order_card"]
        self.assertIn(card["bias"], ("long", "short"))
        self.assertGreater(card["entry"], 0)
        self.assertIn("gross_R_targets", card)

    def test_card_math_ladder_reuse(self):
        args = ctx.parser().parse_args(["--symbols", "ZHIPU", "--bias", "long",
                                        "--entry", "100", "--stop", "90"])
        ctx.validate(args)
        card = ctx.conditional_card(args, atr_value=4)
        self.assertEqual(card["tranche_ladder"]["levels"], [100.0, 98.0, 96.0])
        self.assertEqual(card["gross_R_targets"]["1"], 106.0)

    def test_list_mode_filters_stocks(self):
        with patch.object(ctx, "get", side_effect=self.fake_get):
            listing = ctx.build_list("AAPL")
        self.assertEqual([r["contract"] for r in listing["rows"]], ["AAPL_USDT"])
        self.assertEqual(listing["rows"][0]["yahoo_underlying"], "AAPL")
        code, out, _ = self.run_report(["--list", "ZHIPU"])
        self.assertEqual(code, 0)
        self.assertIn("ZHIPU_USDT", out)
        code, out, err = self.run_report(["--list", "--json"])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(json.loads(out)["count"] >= 3, True)

    def test_human_output_format(self):
        self.gate_daily_count = 60
        code, out, err = self.run_report(["--symbols", "ZHIPU"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("条件挂单卡"))
        self.assertIn("溢价", out)
        self.assertIn("多周期", out)
        self.assertIn("警告", out)

    def test_sizing_requires_budget(self):
        args = ctx.parser().parse_args(["--symbols", "ZHIPU", "--bias", "long",
                                        "--entry", "100", "--stop", "90"])
        ctx.validate(args)
        card = ctx.conditional_card(args)
        self.assertEqual(card["sizing_status"], "missing_user_equity_or_risk_budget")
        args.equity, args.risk_pct = 1000.0, 5.0
        card = ctx.conditional_card(args)
        self.assertEqual(card["gross_units_before_costs"], 5)
        self.assertIn("illustrative", card["sizing_status"])


class EnhancementTests(unittest.TestCase):
    setUp = RegressionTests.setUp
    invoke = RegressionTests.invoke
    fake_get = RegressionTests.fake_get
    run_report = RegressionTests.run_report
    # Realistic prompts are offline rule/script evaluations, NOT fresh-session model evals.
    def test_prompt_discovery_not_old_holdings(self):
        """提示：别只看我以前的持仓，主动找几个股票机会，成交活跃点。"""
        self.contracts = [gate_contract(f"S{i:04}_USDT") for i in range(433)]
        code, out, _ = self.run_report(["--list", "--json"])
        listing = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(listing["count"], 433)
        self.assertEqual(len(listing["rows"]), 433)
        self.assertEqual(listing["rows"][-1]["contract"], "S0432_USDT")
        with patch.object(ctx, "get", side_effect=self.fake_get):
            listing = ctx.build_list("", sort="volume", top=4, min_volume=1)
        self.assertEqual(listing["returned_count"], 4)
        self.assertEqual(listing["count"], 433)

    def test_prompt_news_earnings_not_price_proxy(self):
        """提示：苹果财报和最近局势查一下，别只给我指标。"""
        code, out, _ = self.run_report(["--symbols", "AAPL", "--json"])
        sym = json.loads(out)["symbols"][0]
        self.assertEqual(code, 0)
        self.assertEqual(sym["event_research"]["earnings"], "not_checked")
        text = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
        for rule in ("发布时间", "查询时间", "403", "事实 → 解读 → 条件情景", "查不到不造数"):
            self.assertIn(rule, text)

    def test_prompt_target_horizon_and_invalidation(self):
        """提示：NVDA想拿两三天，目标价、触发和失效怎么定？"""
        self.contracts.append(gate_contract("NVDA_USDT"))
        code, out, _ = self.run_report(["--symbols", "NVDA", "--auto", "--json"])
        sym = json.loads(out)["symbols"][0]
        card = sym["conditional_order_card"]
        self.assertEqual(code, 0)
        for field in ("structural_targets", "gross_R_targets", "holding_period", "entry_trigger", "valid_until_utc"):
            self.assertIn(field, card)
        self.assertIn("unconfirmed", card["entry_trigger"])
        self.assertEqual(card["status"], "research_only_not_an_order")
        self.assertIn("4h", sym["multi_timeframe"])
        self.assertEqual(set(sym["data_quality"]), {"1d", "4h", "1h"})

    def test_list_sort_missing_volume_and_trade_status(self):
        self.contracts[0]["status"] = "suspended"
        ticks = [{"contract": "AAPL_USDT", "volume_24h_quote": "90"}]
        with patch.object(ctx, "fetch_contracts", return_value={c["name"]: c for c in self.contracts}), patch.object(ctx, "get", return_value=ticks):
            rows = ctx.build_list("", sort="volume")["rows"]
            self.assertEqual(rows[0]["contract"], "AAPL_USDT")
            self.assertNotIn("ZHIPU_USDT", [r["contract"] for r in rows])
            self.assertIsNone(rows[-1]["vol24h_quote"])
            self.assertEqual(len(ctx.build_list("", min_volume=80)["rows"]), 1)
            self.assertEqual(len(ctx.build_list("", underlying_only=True)["rows"]), 1)

    def test_list_ticker_failure_not_silent(self):
        with patch.object(ctx, "fetch_contracts", return_value={c["name"]: c for c in self.contracts}), patch.object(ctx, "get", side_effect=RuntimeError()):
            listing = ctx.build_list("")
        self.assertTrue(listing["warnings"])
        self.assertTrue(all(r["vol24h_quote"] is None for r in listing["rows"]))

    def test_list_invalid_options_before_network(self):
        with patch.object(ctx, "get", side_effect=AssertionError("no network")):
            for flags in (["--list", "--top", "0"], ["--list", "--min-volume", "nan"], ["--symbols", "AAPL", "--sort", "volume"]):
                self.assertEqual(self.invoke(flags + ["--json"])[0], 2)

    def test_yahoo_daily_and_short_final_hour_end(self):
        start = int(datetime(2026, 7, 13, 19, 30, tzinfo=timezone.utc).timestamp())
        end = int(datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(ctx.yahoo_bar_end(start, "1h", "AAPL"), end)
        self.assertEqual(ctx.yahoo_bar_end(start - 21600, "1d", "AAPL"), end)
        row = {"open_time": start * 1000, "close_time": end * 1000 - 1}
        self.assertEqual(len(ctx.completed_yahoo_1h([row], datetime.fromtimestamp(end, timezone.utc))), 1)
        self.assertEqual(len(ctx.completed_yahoo_1h([row], datetime.fromtimestamp(end - 1, timezone.utc))), 0)

    def test_dst_actual_transition_hour(self):
        self.assertEqual(ctx.ny_offset_minutes(datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc)), -300)
        self.assertEqual(ctx.ny_offset_minutes(datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)), -240)
        self.assertEqual(ctx.ny_offset_minutes(datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc)), -240)
        self.assertEqual(ctx.ny_offset_minutes(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)), -300)

    def test_stale_gate_and_weekend_yahoo_clock(self):
        now = datetime(2026, 7, 11, 15, tzinfo=timezone.utc)
        end = int(datetime(2026, 7, 10, 20, tzinfo=timezone.utc).timestamp())
        rows = [{"open_time": (end - 3600) * 1000, "close_time": end * 1000 - 1}]
        self.assertFalse(ctx.bar_quality(rows, "yahoo_regular_hours", 3600, now)["stale"])
        self.assertTrue(ctx.bar_quality(rows, "gate_24h", 3600, now)["stale"])
        self.assertEqual(ctx.bar_quality([], "gate_24h", 3600, now)["status"], "unavailable")
        monday = datetime(2026, 7, 13, 22, tzinfo=timezone.utc)
        q = ctx.bar_quality(rows, "yahoo_regular_hours", 3600, monday)
        self.assertTrue(q["stale"])
        self.assertFalse(q["calendar_verified"])

    def test_missing_multiplier_does_not_invent_oi(self):
        contract = gate_contract("AAPL_USDT")
        contract.pop("quanto_multiplier")
        self.assertNotIn("open_interest_notional_usdt_approx", ctx.build_gate_snapshot(contract))

    def test_structural_target_evidence_and_expiry(self):
        self.contracts[0]["mark_price"] = "90"
        code, out, _ = self.run_report(["--symbols", "ZHIPU", "--bias", "long", "--entry", "90", "--stop", "80", "--json"])
        sym = json.loads(out)["symbols"][0]
        card = sym["conditional_order_card"]
        self.assertEqual(code, 0)
        self.assertTrue(card["structural_targets"])
        for target in card["structural_targets"]:
            self.assertGreater(target["price"], 90)
            self.assertIn("pivot_open_time_ms", target)
            self.assertEqual(target["confirmation"], "left2_right2_completed_bars")
        now = datetime.fromisoformat(sym["fetched_at_utc"]).timestamp()
        expiry = datetime.fromisoformat(card["valid_until_utc"]).timestamp()
        self.assertGreater(expiry, now)
        self.assertLessEqual(expiry - now, 14400)
        self.assertEqual(expiry % 14400, 0)

    def test_funding_interval_and_units_label(self):
        self.contracts[0]["funding_interval"] = 1800
        code, out, _ = self.run_report(["--symbols", "ZHIPU", "--bias", "long", "--entry", "100", "--stop", "90"])
        self.assertEqual(code, 0)
        self.assertIn("/0.5h", out)
        self.assertIn("非合约张数", out)


if __name__ == "__main__":
    unittest.main()
