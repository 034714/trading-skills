#!/usr/bin/env python3
"""Read-only closed-candle context for BTC/ETH (+1H/4H multi-timeframe, optional altcoin screen), conditional linear sizing and option research."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import datetime, timezone

BINANCE = "https://api.binance.com/api/v3"
FAPI = "https://fapi.binance.com/fapi/v1"
FDATA = "https://fapi.binance.com/futures/data"
DERIBIT = "https://www.deribit.com/api/v2"
DEFAULT_SYMBOLS = "BTC,ETH"
DEFAULT_SCAN_LIST = "SOL,BNB,XRP,DOGE,ADA,AVAX,LINK,LTC"
SHARED_WARNINGS = [
    "EMA排列/RSI/ATR/摆点/资金费率/OI/多空比/1H与4H多周期状态均为描述性状态，不是方向信号或胜率证据",
    "30日RV与IV比较不构成期权买卖优势；期限、风险溢价、费用、价差与事件日历未核查",
    "止损触发与平仓请求不保证成交；跳空、滑点、流动性可使亏损超过预算",
    "期权为inverse结算研究候选：乘数、保证金、深度未核验，mid不是可成交成本，不输出USD最大亏损",
    "山寨币扫描仅为筛选视图；上市规格、流动性与事件未核查，不构成交易建议",
]


def get(url):
    # scheme/host check: only public https APIs, no loopback or private addresses
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or not p.hostname or p.hostname in ("localhost", "0.0.0.0"):
        raise ValueError(f"rejected URL: {url}")
    import ipaddress
    try:
        addr = ipaddress.ip_address(p.hostname)
        if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local:
            raise ValueError(f"rejected address: {p.hostname}")
    except ValueError:
        pass
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


def sig(value, digits=6):
    """Round to significant digits for compact, readable output; computation stays full precision."""
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError("non-finite value")
    if value == 0:
        return 0.0
    exponent = math.floor(math.log10(abs(value)))
    return round(value, digits - 1 - exponent)


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


def parse_symbols(raw, field, limit):
    names, seen = [], set()
    for token in str(raw).split(","):
        token = token.strip().upper()
        if not token:
            continue
        if not re.fullmatch(r"[A-Z0-9]{2,10}", token):
            raise ValueError(f"Invalid symbol in {field}: {token}")
        if token not in seen:
            seen.add(token)
            names.append(token)
    if not names:
        raise ValueError(f"{field} is empty")
    if len(names) > limit:
        raise ValueError(f"{field} supports at most {limit} symbols")
    return names


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def fetch_live(pair):
    """Best-effort public snapshot: spot, 24h ticker, funding, OI trend, long/short ratio."""
    def ticker24():
        d = get(f"{BINANCE}/ticker/24hr?symbol={pair}")
        return {"chg_pct": _num(d.get("priceChangePercent")), "high": _num(d.get("highPrice")),
                "low": _num(d.get("lowPrice")), "quote_volume": _num(d.get("quoteVolume"))}

    jobs = {
        "spot": lambda: _num(get(f"{BINANCE}/ticker/price?symbol={pair}").get("price")),
        "ticker_24h": ticker24,
        "premium": lambda: get(f"{FAPI}/premiumIndex?symbol={pair}"),
        "open_interest": lambda: _num(get(f"{FAPI}/openInterest?symbol={pair}").get("openInterest")),
        "oi_hist": lambda: get(f"{FDATA}/openInterestHist?symbol={pair}&period=1h&limit=25"),
        "lsr": lambda: get(f"{FDATA}/globalLongShortAccountRatio?symbol={pair}&period=1h&limit=1"),
    }
    out, unavailable = {}, []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {key: pool.submit(fn) for key, fn in jobs.items()}
        for key, future in futures.items():
            try:
                out[key] = future.result()
            except Exception:
                unavailable.append(key)
    live = {}
    if out.get("spot") is not None:
        live["spot"] = out["spot"]
    if isinstance(out.get("ticker_24h"), dict):
        live["ticker_24h"] = out["ticker_24h"]
    if isinstance(out.get("premium"), dict):
        live["mark_price"] = _num(out["premium"].get("markPrice"))
        nxt = out["premium"].get("nextFundingTime")
        live["funding"] = {"rate_8h": _num(out["premium"].get("lastFundingRate")),
                           "next_funding_ms": int(nxt) if isinstance(nxt, (int, float)) and nxt > 0 else None}
    if out.get("open_interest") is not None:
        live["open_interest_base"] = out["open_interest"]
    hist = out.get("oi_hist")
    if isinstance(hist, list) and len(hist) >= 2:
        first, last = _num(hist[0].get("sumOpenInterest")), _num(hist[-1].get("sumOpenInterest"))
        if first and last:
            live["oi_change_24h_pct"] = round((last - first) / first * 100, 2)
    if isinstance(out.get("lsr"), list) and out["lsr"]:
        live["long_short_account_ratio_1h"] = _num(out["lsr"][-1].get("longShortRatio"))
    if unavailable:
        live["unavailable"] = sorted(unavailable)
    return live


def ladder_levels(bias, entry, stop, tranches, atr_value):
    """Equal-weight add-on ladder spaced by 0.5*ATR; keeps only levels that stay meaningful vs stop."""
    if tranches <= 1 or not atr_value:
        return [entry]
    distance = abs(entry - stop)
    if distance <= 0:
        return [entry]
    levels = []
    for i in range(tranches):
        level = entry - 0.5 * atr_value * i if bias == "long" else entry + 0.5 * atr_value * i
        gap = (level - stop) if bias == "long" else (stop - level)
        if gap >= 0.25 * distance:
            levels.append(level)
    return levels or [entry]


def options_fit_note(atm_iv, rv):
    if atm_iv is None or rv is None:
        return "ATM IV或RV数据缺失，无法比较"
    diff = atm_iv - rv * 100
    if diff > 5:
        return f"ATM IV较30日RV高{diff:.0f}pp：方向性买权需承担波动溢价；事件未核查，不构成买卖建议"
    if diff < -5:
        return f"30日RV较ATM IV高{-diff:.0f}pp：不能据此认定期权便宜，需先核查事件日历与期限结构"
    return f"ATM IV与30日RV接近（差{diff:+.0f}pp）：仅描述性比较，不是买卖依据"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    ap = Parser(description=__doc__)
    ap.add_argument("--symbol", default="", help="Legacy single symbol; same as --symbols X")
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS, help=f"Comma list, default {DEFAULT_SYMBOLS}")
    ap.add_argument("--scan", action="store_true", help="Append descriptive altcoin screen")
    ap.add_argument("--scan-list", default=DEFAULT_SCAN_LIST, help=f"Comma list, default {DEFAULT_SCAN_LIST}")
    ap.add_argument("--tranches", type=int, default=3, help="Equal-weight entry ladder size, 1-5, default 3")
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
    if not 1 <= args.tranches <= 5:
        raise ValueError("tranches must be between 1 and 5")
    if args.symbol and args.symbols != DEFAULT_SYMBOLS:
        raise ValueError("Use either --symbol or --symbols, not both")
    args.symbol_list = parse_symbols(args.symbol or args.symbols, "--symbols", 4)
    args.scan_symbols = parse_symbols(args.scan_list, "--scan-list", 12)
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


def conditional_card(args, atr_value=None):
    card = {"status": "research_only_not_an_order", "bias": args.bias or "unspecified",
            "entry": sig(args.entry), "stop_trigger": sig(args.stop), "units": None,
            "execution_gate": "Verify live price, trigger, time validity, event risk, venue specifications and costs",
            "stop_warning": "Stop triggers and close requests do not guarantee execution or a loss cap"}
    if args.bias not in ("long", "short"):
        card["reason"] = "No directional signal or executable option strategy"
        return card
    levels = ladder_levels(args.bias, args.entry, args.stop, getattr(args, "tranches", 1) or 1, atr_value)
    avg_entry = sum(levels) / len(levels)
    distance = abs(avg_entry - args.stop)
    sign = 1 if args.bias == "long" else -1
    card["gross_R_targets"] = {}
    for k in (1, 2, 3):
        target = avg_entry + sign * distance * k
        card["gross_R_targets"][str(k)] = sig(target) if target > 0 else None
    card["units_per_100_usd_risk_example"] = sig(100 / distance)
    card["quantity_formula"] = "user_risk_budget / abs(avg_entry - stop); before fees and slippage"
    if len(levels) > 1:
        card["tranche_ladder"] = {
            "levels": [sig(v) for v in levels], "equal_weight": True, "avg_entry": sig(avg_entry),
            "note": "分批均摊成本，各批共用同一失效价；总风险预算按全部批次计算，任何一批成交不改变总预算"}
    if args.equity is None or args.risk_pct is None:
        card["sizing_status"] = "missing_user_equity_or_risk_budget"
        return card
    budget = args.equity * args.risk_pct / 100
    units = budget / distance
    leverage = units * avg_entry / args.equity
    card.update(risk_budget=sig(budget), gross_units_before_costs=sig(units),
                notional=sig(units * avg_entry), notional_equity_ratio=sig(leverage),
                sizing_status="illustrative_linear_only_fees_slippage_funding_excluded")
    if args.max_leverage is not None and leverage > args.max_leverage:
        card["sizing_status"] = "exceeds_user_configured_cap_no_executable_size"
    return card


def fetch_scan_row(symbol):
    pair = symbol + "USDT"
    row = {"symbol": symbol}
    try:
        daily = klines(pair, "1d", 220)
        if len(daily) < 200:
            raise ValueError("Insufficient daily history for EMA200")
        closes = [r["close"] for r in daily]
        last = closes[-1]
        e20, e50, e200 = (ema(closes, p) for p in (20, 50, 200))
        hi, lo = max(r["high"] for r in daily[-90:]), min(r["low"] for r in daily[-90:])
        pos = (last - lo) / (hi - lo) * 100 if hi > lo else 50.0
        r14, a14 = rsi(closes), atr(daily)
        stack = "bull" if last > e20 > e50 > e200 else "bear" if last < e20 < e50 < e200 else "mixed"
        flags = []
        if r14 <= 30:
            flags.append("RSI<=30")
        elif r14 >= 70:
            flags.append("RSI>=70")
        if pos >= 95:
            flags.append("贴近90日高点")
        elif pos <= 5:
            flags.append("贴近90日低点")
        if stack == "bull":
            flags.append("均线多头排列")
        elif stack == "bear":
            flags.append("均线空头排列")
        try:
            funding = _num(get(f"{FAPI}/premiumIndex?symbol={pair}").get("lastFundingRate"))
            if funding is not None:
                if funding >= 0.0005:
                    flags.append("正资金费率偏高")
                elif funding <= -0.0005:
                    flags.append("负资金费率")
        except Exception:
            funding = None
        row.update(close=sig(last), chg_24h_pct=round((last - closes[-2]) / closes[-2] * 100, 1),
                   rsi14=round(r14, 1), atr_pct=round(a14 / last * 100, 2), ema_stack=stack,
                   dist_ema50_pct=round((last - e50) / e50 * 100, 1),
                   dist_ema200_pct=round((last - e200) / e200 * 100, 1),
                   pos_in_90d_range_pct=round(pos, 1), funding_8h=funding, flags=flags)
    except Exception as exc:
        row["note"] = f"数据不足或接口失败: {type(exc).__name__}"
    return row


def build_scan(symbols):
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(fetch_scan_row, symbols))
    rows.sort(key=lambda r: (len(r.get("flags", [])), abs(r.get("chg_24h_pct") or 0)), reverse=True)
    return {"watchlist": symbols, "rows": rows,
            "usage_note": "筛选视图：仅描述状态，不是信号；对入围标的先做单标的完整分析，并核查上市规格与流动性"}


def build_symbol_report(args, symbol, now):
    pair = symbol + "USDT"
    now_ms = now.timestamp() * 1000
    daily = klines(pair, "1d", now_ms=now_ms)
    four_h = klines(pair, "4h", 200, now_ms=now_ms)
    try:
        one_h = klines(pair, "1h", 300, now_ms=now_ms)
    except Exception:
        one_h = []
    if len(daily) < 200 or len(four_h) < 5:
        raise ValueError("Insufficient completed candle history (200 daily / 5 four-hour required)")
    for rows, interval in ((daily, 86400000), (four_h, 14400000)):
        if now_ms - rows[-1]["close_time"] > interval * 2:
            raise ValueError("Stale candle history")
        if any(b["open_time"] - a["open_time"] != interval for a, b in zip(rows, rows[1:])):
            raise ValueError("Candle history contains gaps")
    closes = [r["close"] for r in daily]
    last = closes[-1]
    e20, e50, e200 = (ema(closes, p) for p in (20, 50, 200))
    a14 = atr(daily)
    highs, lows = pivots(four_h)
    mtf = {}
    if len(one_h) >= 50:
        closes_1h = [r["close"] for r in one_h]
        e20_1h, e50_1h = ema(closes_1h, 20), ema(closes_1h, 50)
        state = ("above" if closes_1h[-1] > e20_1h > e50_1h
                 else "below" if closes_1h[-1] < e20_1h < e50_1h else "mixed")
        mtf["1h"] = {"close": sig(closes_1h[-1]), "rsi14": round(rsi(closes_1h), 1),
                     "ema20": sig(e20_1h), "ema50": sig(e50_1h), "state": state}
    else:
        mtf["1h"] = {"note": "unavailable_or_insufficient"}
    try:
        closes_4h = [r["close"] for r in four_h]
        mtf["4h"] = {"close": sig(closes_4h[-1]), "rsi14": round(rsi(closes_4h), 1),
                     "ema20": sig(ema(closes_4h, 20))}
    except Exception:
        mtf["4h"] = {"note": "unavailable"}
    nearest = None
    for price, kind in [(p[1], "high") for p in highs] + [(p[1], "low") for p in lows]:
        if nearest is None or abs(price - last) < abs(nearest[0] - last):
            nearest = (price, kind)
    if nearest is not None:
        mtf["nearest_4h_pivot"] = {"kind": nearest[1], "price": sig(nearest[0]),
                                   "dist_pct": round(abs(nearest[0] - last) / last * 100, 2)}
    intraday_flags = []
    r1 = mtf["1h"].get("rsi14")
    if r1 is not None:
        if r1 >= 70:
            intraday_flags.append(f"1H超买({r1})")
        elif r1 <= 30:
            intraday_flags.append(f"1H超卖({r1})")
    pivot = mtf.get("nearest_4h_pivot")
    if pivot and pivot["dist_pct"] <= 0.6:
        intraday_flags.append("贴近4H高点" if pivot["kind"] == "high" else "贴近4H低点")
    mtf["intraday_flags"] = intraday_flags
    mtf["usage"] = "日内背景描述非信号；日内条件卡有效期至下一根4H收盘"
    hi, lo = max(r["high"] for r in daily[-90:]), min(r["low"] for r in daily[-90:])
    auto_scenarios = []
    if args.auto:
        biases = ["long"] if last > e50 > e200 else ["short"] if last < e50 < e200 else ["long", "short"]
        for bias in biases:
            scenario = copy(args)
            scenario.bias = bias
            scenario.entry = last
            width = 1.3 * a14
            structural = lows[-1][1] if bias == "long" and lows else highs[-1][1] if bias == "short" and highs else last
            scenario.stop = min(structural, last - width) if bias == "long" else max(structural, last + width)
            if scenario.stop <= 0 or scenario.stop == scenario.entry:
                continue
            card = conditional_card(scenario, a14)
            card["trigger"] = "Confirm live price and completed-bar reclaim of entry" if bias == "long" else "Confirm live price and completed-bar rejection of entry"
            card["validity"] = "Reassess at next completed 4H candle; cancel if stop invalidated before entry"
            card["basis"] = "Daily EMA state scenario, not a prediction; entry is last completed daily close, not live spot"
            auto_scenarios.append(card)
    report = {"symbol": symbol, "fetched_at_utc": now.isoformat(),
              "last_daily_close": sig(last),
              "last_daily_close_time_ms": daily[-1]["close_time"],
              "last_4h_close_time_ms": four_h[-1]["close_time"],
              "ema20": sig(e20), "ema50": sig(e50), "ema200": sig(e200),
              "ema_arrangement": "above" if last > e50 > e200 else "below" if last < e50 < e200 else "mixed",
              "rsi14": round(rsi(closes), 1), "atr14_simple_mean": sig(a14),
              "atr_pct_of_price": round(a14 / last * 100, 2),
              "realized_vol_30d_annualized": round(realized_vol(closes), 3),
              "range90": {"high": sig(hi), "low": sig(lo)},
              "range_retracements_not_validated_swing": {str(r): sig(hi - (hi - lo) * r) for r in (.382, .5, .618, .786)},
              "last_confirmed_4h_pivot_high": sig(highs[-1][1]) if highs else None,
              "last_confirmed_4h_pivot_low": sig(lows[-1][1]) if lows else None,
              "multi_timeframe": mtf,
              "live_snapshot": fetch_live(pair),
              "conditional_order_card": auto_scenarios[0] if auto_scenarios else conditional_card(args, a14),
              "alternative_conditional_cards": auto_scenarios[1:],
              "option_candidates": []}
    live = report["live_snapshot"]
    if live.get("spot") and last:
        live["spot_vs_daily_close_pct"] = round((live["spot"] - last) / last * 100, 2)
    try:
        chain = option_chain(symbol)
        expiries = select_expiries(chain, args.min_days, args.max_days, args.expiry_hint, now)
        for expiry in expiries:
            for kind in ("C", "P"):
                legs = [c for c in chain if c["expiry"] == expiry and c["kind"] == kind]
                if legs:
                    candidate = dict(min(legs, key=lambda c: abs(c["strike"] - last)))
                    candidate["days"] = round(expiry_days(expiry, now), 1)
                    candidate["iv_pct"] = sig(candidate.pop("iv_pct"), 4)
                    for key in ("bid", "ask", "mid_valuation_only"):
                        candidate[key] = sig(candidate[key], 4)
                    candidate["oi"] = sig(candidate["oi"], 5)
                    candidate["purpose"] = "research_candidate_only_not_a_trade_leg"
                    report["option_candidates"].append(candidate)
        report["options_status"] = "research_only" if expiries else "no_expiry_in_range"
        calls = [c for c in report["option_candidates"] if c["kind"] == "C" and c.get("iv_pct") is not None]
        if calls:
            soonest = min(c["days"] for c in calls)
            report["atm_iv_nearest_expiry_pct"] = round(min((c for c in calls if c["days"] == soonest),
                                                     key=lambda c: abs(c["strike"] - last))["iv_pct"], 2)
    except Exception as exc:
        if args.expiry_hint:
            raise ValueError(f"Cannot satisfy explicit expiry: {exc}") from exc
        report["options_status"] = "unavailable"
    report["options_fit_note"] = options_fit_note(report.get("atm_iv_nearest_expiry_pct"),
                                                  report["realized_vol_30d_annualized"])
    return report


def build_report(args):
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=min(4, len(args.symbol_list))) as pool:
        reports = list(pool.map(lambda sym: _safe_symbol_report(args, sym, now), args.symbol_list))
    failures = [r for r in reports if "error" in r]
    if len(failures) == len(reports):
        raise ValueError(f"All symbols failed: {failures[0].get('error')}")
    envelope = {"schema": "trade_ctx_v2", "generated_at_utc": now.isoformat(),
                "symbols": reports, "shared_warnings": list(SHARED_WARNINGS)}
    if failures:
        envelope["partial_failures"] = [f["symbol"] for f in failures]
    if args.scan:
        envelope["scan"] = build_scan(args.scan_symbols)
    return envelope


def _safe_symbol_report(args, symbol, now):
    try:
        return build_symbol_report(args, symbol, now)
    except Exception as exc:
        return {"symbol": symbol, "error": str(exc)}


def _ms_to_str(ms):
    if not ms:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M") + "Z"


def format_symbol_human(report):
    lines = [f"== {report['symbol']} @ {report['fetched_at_utc'][:19]}Z =="]
    lines.append(f"日线收盘 {sig(report['last_daily_close'])}（完成于 {_ms_to_str(report['last_daily_close_time_ms'])}）"
                 f" | 4H完成于 {_ms_to_str(report['last_4h_close_time_ms'])}")
    live = report.get("live_snapshot", {})
    spot = live.get("spot")
    diff = live.get("spot_vs_daily_close_pct")
    spot_text = f"{sig(spot)}（{diff:+.2f}% vs 日线收）" if spot else "unavailable"
    lines.append(f"现价快照 {spot_text} | EMA20 {report['ema20']} | EMA50 {report['ema50']} | EMA200 {report['ema200']}"
                 f"（{report['ema_arrangement']}）")
    lines.append(f"RSI14 {report['rsi14']} | ATR14 {report['atr14_simple_mean']}（{report['atr_pct_of_price']}%）"
                 f" | RV30 {report['realized_vol_30d_annualized'] * 100:.1f}%")
    rng = report["range90"]
    retr = report["range_retracements_not_validated_swing"]
    lines.append(f"90日区间 {rng['low']}~{rng['high']} | 区间回撤参考 38.2% {retr['0.382']} / 50% {retr['0.5']}")
    lines.append(f"4H确认摆点 高 {report['last_confirmed_4h_pivot_high']} / 低 {report['last_confirmed_4h_pivot_low']}")
    m = report.get("multi_timeframe", {})
    h1, h4 = m.get("1h", {}), m.get("4h", {})
    h1_text = (f"1H RSI {h1['rsi14']} EMA20/50 {h1['ema20']}/{h1['ema50']}（{h1['state']}）"
               if "rsi14" in h1 else "1H n/a")
    h4_text = f"4H RSI {h4['rsi14']} EMA20 {h4['ema20']}" if "rsi14" in h4 else "4H n/a"
    pivot = m.get("nearest_4h_pivot")
    pivot_text = (f"距最近4H{'高' if pivot['kind'] == 'high' else '低'}点 {pivot['price']}（{pivot['dist_pct']}%）"
                  if pivot else "无最近摆点")
    flags_text = "、".join(m.get("intraday_flags", [])) or "-"
    lines.append(f"多周期 {h1_text} | {h4_text} | {pivot_text} | 日内标记: {flags_text}")
    funding = live.get("funding", {})
    rate = funding.get("rate_8h")
    oi_chg = live.get("oi_change_24h_pct")
    if live.get("open_interest_base"):
        oi_text = f"OI {sig(live.get('open_interest_base'))}"
        if oi_chg is not None:
            oi_text += f"（24h {oi_chg:+}%）"
    else:
        oi_text = "OI n/a"
    parts = [f"资金费率 {rate * 100:.4f}%/8h" if rate is not None else "资金费率 n/a",
             oi_text,
             f"多空比(账户,1h) {live.get('long_short_account_ratio_1h', 'n/a')}"]
    ticker = live.get("ticker_24h", {})
    if ticker.get("chg_pct") is not None:
        parts.append(f"24h {ticker['chg_pct']:+.1f}%")
    if live.get("unavailable"):
        parts.append("快照缺失:" + ",".join(live["unavailable"]))
    lines.append(" | ".join(parts))
    iv = report.get("atm_iv_nearest_expiry_pct")
    lines.append(f"期权 {report.get('options_status')} | 最近到期 ATM IV {iv if iv is not None else 'n/a'}% "
                 f"| {report.get('options_fit_note')}")
    cards = [report["conditional_order_card"]] + report.get("alternative_conditional_cards", [])
    for card in cards:
        lines.append(f"-- 卡（{card['bias']}，研究草案非订单）--")
        if card["bias"] == "unspecified":
            lines.append("未给方向假设：等待用户方向/入场/失效价，或用 --auto 生成条件情景")
            continue
        lines.append(f"entry {card.get('entry')} | stop {card.get('stop_trigger')}"
                     f" | R1/R2/R3 {card.get('gross_R_targets', {}).get('1')}/{card.get('gross_R_targets', {}).get('2')}"
                     f"/{card.get('gross_R_targets', {}).get('3')}"
                     f" | 每$100风险 {card.get('units_per_100_usd_risk_example')} 单位")
        ladder = card.get("tranche_ladder")
        if ladder:
            lines.append(f"分批 {ladder['levels']}（等权，均价 {ladder['avg_entry']}，共用失效价）")
        if card.get("sizing_status"):
            lines.append(f"sizing {card['sizing_status']}")
    for candidate in report.get("option_candidates", [])[:4]:
        lines.append(f"期权候选 {candidate['name']} IV {candidate['iv_pct']}% bid {candidate['bid']} ask {candidate['ask']}"
                     f" OI {candidate['oi']}（{candidate['days']}天，研究用）")
    return lines


def format_scan_human(scan):
    lines = ["== 山寨币筛选（描述性状态，非信号）=="]
    for row in scan["rows"]:
        if "note" in row:
            lines.append(f"{row['symbol']}: {row['note']}")
            continue
        flags = "、".join(row["flags"]) if row["flags"] else "-"
        funding_text = "n/a" if row.get("funding_8h") is None else f"{row['funding_8h'] * 100:.4f}%"
        lines.append(f"{row['symbol']} 收 {row['close']} | 24h {row['chg_24h_pct']:+.1f}% | RSI {row['rsi14']}"
                     f" | ATR {row['atr_pct']}% | 排列 {row['ema_stack']} | 距EMA50 {row['dist_ema50_pct']:+.1f}%"
                     f" | 区间位 {row['pos_in_90d_range_pct']}% | 费率 {funding_text} | 标记: {flags}")
    return lines


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    json_mode = "--json" in argv
    try:
        args = parser().parse_args(argv)
        validate(args)
        report = build_report(args)
        # Serialization also rejects non-finite arithmetic results before any output.
        if json_mode:
            print(json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        else:
            print("条件挂单卡（研究草案，默认只读，不自动下单）")
            for symbol_report in report["symbols"]:
                if "error" in symbol_report:
                    print(f"== {symbol_report['symbol']} == 数据失败: {symbol_report['error']}")
                    continue
                for line in format_symbol_human(symbol_report):
                    print(line)
            if "scan" in report:
                for line in format_scan_human(report["scan"]):
                    print(line)
            for warning in report["shared_warnings"]:
                print(f"警告 {warning}")
        return 0
    except Exception as exc:
        error = {"error": str(exc), "status": "no_plan"}
        print(json.dumps(error, ensure_ascii=False, separators=(",", ":"), allow_nan=False) if json_mode
              else f"error: {exc}", file=sys.stdout if json_mode else sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
