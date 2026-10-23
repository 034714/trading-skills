#!/usr/bin/env python3
"""Read-only closed-candle context, conditional linear sizing and option research."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.request
from datetime import datetime, timezone

BINANCE = "https://api.binance.com/api/v3"
DERIBIT = "https://www.deribit.com/api/v2"


def get(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=35) as response:
        data = json.loads(response.read().decode("utf-8"))
    if isinstance(data, dict) and (data.get("error") or data.get("code", 0) < 0):
        raise ValueError("Upstream API returned an error")
    return data


def positive(value, name):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def klines(symbol, interval, limit=300, now_ms=None):
    data = get(f"{BINANCE}/klines?symbol={symbol}&interval={interval}&limit={limit}")
    if not isinstance(data, list):
        raise ValueError("Invalid candle response")
    now_ms = now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000
    rows = []
    for k in data:
        if int(k[6]) >= now_ms:
            continue
        row = {"open_time": int(k[0]), "close_time": int(k[6]),
               "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
               "close": float(k[4]), "volume": float(k[5])}
        for field in ("open", "high", "low", "close"):
            positive(row[field], field)
        if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]):
            raise ValueError("Invalid candle OHLC")
        if rows and row["open_time"] <= rows[-1]["open_time"]:
            raise ValueError("Candles are not strictly ordered")
        rows.append(row)
    return rows


def ema(values, period):
    out = values[0]
    for value in values[1:]:
        out += (value - out) * 2 / (period + 1)
    return out


def rsi(closes, period=14):
    diffs = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(d, 0) for d in diffs]
    losses = [max(-d, 0) for d in diffs]
    if len(diffs) < period:
        raise ValueError("Insufficient RSI history")
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    for g, loss in zip(gains[period:], losses[period:]):
        ag, al = (ag * (period - 1) + g) / period, (al * (period - 1) + loss) / period
    return 50.0 if ag == al == 0 else 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(rows, period=14):
    trs = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"]))
           for a, b in zip(rows, rows[1:])]
    if len(trs) < period:
        raise ValueError("Insufficient ATR history")
    return sum(trs[-period:]) / period


def realized_vol(closes, window=30):
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:])][-window:]
    if len(returns) < window:
        raise ValueError("Insufficient RV history")
    mean = sum(returns) / window
    return math.sqrt(sum((r - mean) ** 2 for r in returns) / (window - 1) * 365)


def pivots(rows, left=2, right=2):
    highs, lows = [], []
    for i in range(left, len(rows) - right):
        window = rows[i-left:i+right+1]
        if rows[i]["high"] == max(r["high"] for r in window):
            highs.append((rows[i]["open_time"], rows[i]["high"]))
        if rows[i]["low"] == min(r["low"] for r in window):
            lows.append((rows[i]["open_time"], rows[i]["low"]))
    return highs, lows


def expiry_days(expiry, now=None):
    match = re.fullmatch(r"(\d{1,2})([A-Z]{3})(\d{2})", expiry)
    months = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
    if not match or match[2] not in months:
        raise ValueError("Invalid expiry tag")
    dt = datetime(2000 + int(match[3]), months[match[2]], int(match[1]), 8, tzinfo=timezone.utc)
    return (dt - (now or datetime.now(timezone.utc))).total_seconds() / 86400


def option_chain(currency):
    data = get(f"{DERIBIT}/public/get_book_summary_by_currency?currency={currency}&kind=option")
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        raise ValueError("Invalid option chain response")
    out = []
    for item in data["result"]:
        name = item.get("instrument_name", "")
        match = re.fullmatch(r"([A-Z]+)-(\d{1,2}[A-Z]{3}\d{2})-(\d+(?:\.\d+)?)-([CP])", name)
        if not match or match[1] != currency:
            continue
        row = {"name": name, "expiry": match[2], "strike": positive(float(match[3]), "strike"),
               "kind": match[4], "quote_currency": currency, "specification_verified": False}
        for source, target in (("mark_iv", "iv_pct"), ("bid_price", "bid"), ("ask_price", "ask"),
                               ("mid_price", "mid_valuation_only"), ("open_interest", "oi")):
            value = item.get(source)
            row[target] = None if value is None else float(value)
            if row[target] is not None and (not math.isfinite(row[target]) or row[target] < 0):
                raise ValueError("Invalid option quote")
        out.append(row)
    return out


def select_expiries(chain, min_days, max_days, explicit="", now=None):
    expiries = sorted({c["expiry"] for c in chain}, key=lambda e: expiry_days(e, now))
    usable = [e for e in expiries if min_days <= expiry_days(e, now) <= max_days]
    if explicit:
        if explicit not in usable:
            raise ValueError("Explicit expiry unavailable or outside requested tenor range; no substitution")
        return [explicit]
    return usable[:3]


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    ap = Parser(description=__doc__)
    ap.add_argument("--symbol", choices=["BTC", "ETH"], default="BTC")
    ap.add_argument("--bias", choices=["long", "short", "neutral"])
    ap.add_argument("--auto", action="store_true", help="Construct conditional EMA-state scenarios, not predictions")
    for name in ("entry", "stop", "equity", "risk-pct", "max-leverage"):
        ap.add_argument("--" + name, type=float)
    ap.add_argument("--expiry-hint", default="")
    ap.add_argument("--min-days", type=float, default=3)
    ap.add_argument("--max-days", type=float, default=45)
    ap.add_argument("--json", action="store_true")
    return ap


def validate(args):
    for name in ("entry", "stop", "equity", "risk_pct", "max_leverage", "min_days", "max_days"):
        value = getattr(args, name)
        if value is not None:
            positive(value, name)
    if args.min_days > args.max_days:
        raise ValueError("min-days must not exceed max-days")
    if args.risk_pct is not None and args.risk_pct > 100:
        raise ValueError("risk-pct must not exceed 100")
    if args.auto and any(v is not None for v in (args.bias, args.entry, args.stop)):
        raise ValueError("--auto is context-only and cannot be combined with bias/levels")
    if args.bias in ("long", "short"):
        if args.entry is None or args.stop is None:
            raise ValueError("Directional scenarios require entry and stop")
        if (args.bias == "long" and args.stop >= args.entry) or (args.bias == "short" and args.stop <= args.entry):
            raise ValueError("Stop must be below long entry or above short entry")
    elif args.entry is not None or args.stop is not None:
        raise ValueError("Entry/stop require long or short bias; neutral has no directional position")
    if args.expiry_hint:
        expiry_days(args.expiry_hint)


def conditional_card(args):
    card = {"status": "research_only_not_an_order", "bias": args.bias or "unspecified",
            "entry": args.entry, "stop_trigger": args.stop, "units": None,
            "execution_gate": "Verify live price, trigger, time validity, event risk, venue specifications and costs",
            "stop_warning": "Stop triggers and close requests do not guarantee execution or a loss cap"}
    if args.bias not in ("long", "short"):
        card["reason"] = "No directional signal or executable option strategy"
        return card
    distance = abs(args.entry - args.stop)
    sign = 1 if args.bias == "long" else -1
    card["gross_R_targets"] = {}
    for k in (1, 2, 3):
        target = args.entry + sign * distance * k
        card["gross_R_targets"][str(k)] = target if target > 0 else None
    card["units_per_100_usd_risk_example"] = 100 / distance
    card["quantity_formula"] = "user_risk_budget / abs(entry - stop); before fees and slippage"
    if args.equity is None or args.risk_pct is None:
        card["sizing_status"] = "missing_user_equity_or_risk_budget"
        return card
    budget = args.equity * args.risk_pct / 100
    units = budget / distance
    leverage = units * args.entry / args.equity
    card.update(risk_budget=budget, gross_units_before_costs=units,
                notional=units * args.entry, notional_equity_ratio=leverage,
                sizing_status="illustrative_linear_only_fees_slippage_funding_excluded")
    if args.max_leverage is not None and leverage > args.max_leverage:
        card["sizing_status"] = "exceeds_user_configured_cap_no_executable_size"
    return card


def build_report(args):
    now = datetime.now(timezone.utc)
    pair = args.symbol + "USDT"
    daily = klines(pair, "1d", now_ms=now.timestamp() * 1000)
    four_h = klines(pair, "4h", 200, now_ms=now.timestamp() * 1000)
    if len(daily) < 200 or len(four_h) < 5:
        raise ValueError("Insufficient completed candle history (200 daily / 5 four-hour required)")
    for rows, interval in ((daily, 86400000), (four_h, 14400000)):
        if now.timestamp() * 1000 - rows[-1]["close_time"] > interval * 2:
            raise ValueError("Stale candle history")
        if any(b["open_time"] - a["open_time"] != interval for a, b in zip(rows, rows[1:])):
            raise ValueError("Candle history contains gaps")
    closes = [r["close"] for r in daily]
    last = closes[-1]
    e20, e50, e200 = (ema(closes, p) for p in (20, 50, 200))
    highs, lows = pivots(four_h)
    hi, lo = max(r["high"] for r in daily[-90:]), min(r["low"] for r in daily[-90:])
    auto_scenarios = []
    if args.auto:
        import copy
        biases = ["long"] if last > e50 > e200 else ["short"] if last < e50 < e200 else ["long", "short"]
        for bias in biases:
            scenario = copy.copy(args)
            scenario.bias = bias
            scenario.entry = last
            width = 1.3 * atr(daily)
            structural = lows[-1][1] if bias == "long" and lows else highs[-1][1] if bias == "short" and highs else last
            scenario.stop = min(structural, last-width) if bias == "long" else max(structural, last+width)
            if scenario.stop <= 0 or scenario.stop == scenario.entry:
                continue
            card = conditional_card(scenario)
            card["trigger"] = "Confirm live price and completed-bar reclaim of entry" if bias == "long" else "Confirm live price and completed-bar rejection of entry"
            card["validity"] = "Reassess at next completed 4H candle; cancel if stop invalidated before entry"
            card["basis"] = "Daily EMA state scenario, not a prediction; entry is last completed daily close, not live spot"
            auto_scenarios.append(card)
    warnings = ["EMA arrangement is descriptive, not a directional signal", 
                "No 1H, SMC detection, news search, funding or OI trend analysis implemented",
                "30-day historical RV versus forward IV is not mispricing evidence; tenor, risk premium, fees and spread matter"]
    report = {"conditional_order_card": auto_scenarios[0] if auto_scenarios else conditional_card(args),
              "alternative_conditional_cards": auto_scenarios[1:], "symbol": args.symbol,
              "fetched_at_utc": now.isoformat(), "last_daily_close": last,
              "last_daily_close_time_ms": daily[-1]["close_time"],
              "last_4h_close_time_ms": four_h[-1]["close_time"],
              "ema20": e20, "ema50": e50, "ema200": e200,
              "ema_arrangement": "above" if last > e50 > e200 else "below" if last < e50 < e200 else "mixed",
              "rsi14": rsi(closes), "atr14_simple_mean": atr(daily), "realized_vol_30d_annualized": realized_vol(closes),
              "range90": {"high": hi, "low": lo},
              "range_retracements_not_validated_swing": {str(r): hi - (hi-lo)*r for r in (.382, .5, .618, .786)},
              "last_confirmed_4h_pivot_high": highs[-1][1] if highs else None,
              "last_confirmed_4h_pivot_low": lows[-1][1] if lows else None,
              "option_candidates": [], "warnings": warnings}
    try:
        chain = option_chain(args.symbol)
        expiries = select_expiries(chain, args.min_days, args.max_days, args.expiry_hint, now)
        for expiry in expiries:
            for kind in ("C", "P"):
                legs = [c for c in chain if c["expiry"] == expiry and c["kind"] == kind]
                if legs:
                    candidate = dict(min(legs, key=lambda c: abs(c["strike"] - last)))
                    candidate["days"] = expiry_days(expiry, now)
                    candidate["purpose"] = "research_candidate_only_not_a_trade_leg"
                    report["option_candidates"].append(candidate)
        report["options_status"] = "research_only" if expiries else "no_expiry_in_range"
    except Exception as exc:
        if args.expiry_hint:
            raise ValueError(f"Cannot satisfy explicit expiry: {exc}") from exc
        report["options_status"] = "unavailable"
        warnings.append(f"Options unavailable: {exc}")
    warnings.append("Inverse BTC/ETH option settlement, multiplier, margin and currency risk not verified; no USD maximum-loss or executable premium estimate")
    return report


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    json_mode = "--json" in argv
    try:
        args = parser().parse_args(argv)
        validate(args)
        report = build_report(args)
        # Serialization also rejects non-finite arithmetic results before any output.
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if json_mode:
            print(encoded)
        else:
            print("条件挂单卡（研究草案，默认只读，不自动下单）")
            print(json.dumps(report["conditional_order_card"], ensure_ascii=False, indent=2, allow_nan=False))
            print("简短理由：已完成日线均线仅描述状态；未完成事件与入场确认，不是即时信号。")
            print(encoded)
        return 0
    except Exception as exc:
        error = {"error": str(exc), "status": "no_plan"}
        print(json.dumps(error, ensure_ascii=False, allow_nan=False) if json_mode else f"error: {exc}",
              file=sys.stdout if json_mode else sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
