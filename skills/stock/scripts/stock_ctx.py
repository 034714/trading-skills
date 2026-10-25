#!/usr/bin/env python3
"""Read-only stock-perp research on Gate (USDT-settled stock/index/AI-company perpetuals),
dual-layer data: Gate execution snapshot + Yahoo underlying (US primary listings only for indicators)."""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import datetime, timedelta, timezone

GATE = "https://api.gateio.ws/api/v4"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"
ALLOWED_HOSTS = {"api.gateio.ws", "query1.finance.yahoo.com"}
# Yahoo underlying map: USD identity for US names; verified non-USD primaries are context-only.
YAHOO_MAP = {
    "AAPL": "AAPL", "TSLA": "TSLA", "NVDA": "NVDA", "MSFT": "MSFT", "META": "META",
    "AMZN": "AMZN", "GOOGL": "GOOGL", "NFLX": "NFLX", "MSTR": "MSTR", "COIN": "COIN",
    "HOOD": "HOOD", "PLTR": "PLTR", "AMD": "AMD", "MU": "MU", "TSM": "TSM",
    "AVGO": "AVGO", "WMT": "WMT", "MCD": "MCD", "LLY": "LLY", "TMUS": "TMUS",
    "QQQ": "QQQ", "SPY": "SPY", "SOXX": "SOXX", "SQQQ": "SQQQ", "TQQQX": "TQQQ",
    "MINIMAX": "0100.HK", "SMIC": "0981.HK", "SKHYNIX": "000660.KS",
}
SHARED_WARNINGS = [
    "Gate股票永续为24/7单一场所定价：美股闭市与周末没有底层撮合锚定，指数价与标记价可因情绪大幅偏离，开盘跳空可使止损失效",
    "仅在Gate定价的标的（长鑫/宇树/智谱/OpenAI等）无外部行情锚定且历史短，指标可靠性弱；合约乘数、结算与退市规则未核验",
    "财报与分红日历未自动获取，回复前必须人工核查标的财报日；财报跳空可击穿任何止损",
    "EMA/RSI/ATR/摆点/资金费率/OI/多空账户数均为描述性状态，不是信号或胜率证据",
    "止损触发与平仓不保证成交；股票永续资金费率按合约funding_interval（常见1小时）结算，持仓成本须另算",
    "不覆盖Gate美股16×5实盘通道与任何券商实盘；Yahoo底层数据未经核验，美股假期未内置（以交易所公告为准）",
]


def get(url):
    # https + exact-host allowlist; literal-IP hosts must be public (DNS names are constrained by the allowlist)
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"rejected URL: {url}")
    try:
        addr = ipaddress.ip_address(p.hostname)
        if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local:
            raise ValueError(f"rejected address: {p.hostname}")
    except ValueError:
        pass
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=35) as response:
        data = json.loads(response.read().decode("utf-8"))
    if isinstance(data, dict) and "label" in data and "message" in data:
        raise ValueError(f"Gate API error: {data.get('label')}")
    return data


def positive(value, name):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def sig(value, digits=6):
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError("non-finite value")
    if value == 0:
        return 0.0
    exponent = math.floor(math.log10(abs(value)))
    return round(value, digits - 1 - exponent)


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_symbols(raw, field, limit):
    names, seen = [], set()
    for token in str(raw).split(","):
        token = token.strip().upper()
        if not token:
            continue
        if not re.fullmatch(r"[A-Z0-9]{2,14}", token):
            raise ValueError(f"Invalid symbol in {field}: {token}")
        if token not in seen:
            seen.add(token)
            names.append(token)
    if not names:
        raise ValueError(f"{field} is empty")
    if len(names) > limit:
        raise ValueError(f"{field} supports at most {limit} symbols")
    return names


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
    return math.sqrt(sum((r - mean) ** 2 for r in returns) / (window - 1) * 252)


def pivots(rows, left=2, right=2):
    highs, lows = [], []
    for i in range(left, len(rows) - right):
        window = rows[i - left:i + right + 1]
        if rows[i]["high"] == max(r["high"] for r in window):
            highs.append((rows[i]["open_time"], rows[i]["high"]))
        if rows[i]["low"] == min(r["low"] for r in window):
            lows.append((rows[i]["open_time"], rows[i]["low"]))
    return highs, lows


def ladder_levels(bias, entry, stop, tranches, atr_value):
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


def ny_offset_minutes(dt):
    """USD-ward approximation: EDT(-240) from 2nd Sunday of March to 1st Sunday of November, else EST(-300)."""
    def nth_sunday(month, n):
        d = datetime(dt.year, month, 1, tzinfo=timezone.utc)
        count = 0
        while True:
            if d.weekday() == 6:
                count += 1
                if count == n:
                    return d
            d += timedelta(days=1)
    return -240 if nth_sunday(3, 2) <= dt < nth_sunday(11, 1) else -300


def ny_local(dt):
    return dt + timedelta(minutes=ny_offset_minutes(dt))


def ny_session(dt):
    local = ny_local(dt)
    hm = local.hour * 60 + local.minute
    if local.weekday() >= 5:
        return "weekend_closed"
    if hm < 4 * 60:
        return "closed"
    if hm < 9 * 60 + 30:
        return "pre_market"
    if hm < 16 * 60:
        return "regular"
    if hm < 20 * 60:
        return "after_hours"
    return "closed"


_CONTRACT_CACHE = None


def fetch_contracts():
    global _CONTRACT_CACHE
    if _CONTRACT_CACHE is None:
        data = get(f"{GATE}/futures/usdt/contracts")
        if not isinstance(data, list) or not data:
            raise ValueError("Invalid contracts response")
        _CONTRACT_CACHE = {c["name"]: c for c in data if isinstance(c, dict) and c.get("name")}
    return _CONTRACT_CACHE


def resolve_contract(symbol):
    name = symbol + "_USDT"
    contracts = fetch_contracts()
    if name not in contracts:
        raise ValueError(f"Gate无此合约 {name}：用 --list 查可交易的股票合约")
    c = contracts[name]
    if c.get("contract_type") != "stocks":
        raise ValueError(f"{name} 不是股票类合约（contract_type={c.get('contract_type')}）；加密标的请用 trade 技能")
    return c


def gate_klines(contract, interval, limit, now):
    data = get(f"{GATE}/futures/usdt/candlesticks?contract={contract}&interval={interval}&limit={limit}")
    if not isinstance(data, list):
        raise ValueError("Invalid candle response")
    step = 86400 if interval == "1d" else 14400 if interval == "4h" else 3600
    now_s = now.timestamp()
    rows = []
    for k in data:
        t = int(k["t"])
        if t + step > now_s:
            continue
        row = {"open_time": t * 1000, "close_time": (t + step) * 1000 - 1,
               "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]),
               "close": float(k["c"]), "volume": float(k.get("v") or 0)}
        for field in ("open", "high", "low", "close"):
            positive(row[field], field)
        if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]):
            raise ValueError("Invalid candle OHLC")
        if rows and row["open_time"] <= rows[-1]["open_time"]:
            raise ValueError("Candles are not strictly ordered")
        rows.append(row)
    return rows


def yahoo_chart(symbol, interval, rng):
    data = get(f"{YAHOO}/{symbol}?interval={interval}&range={rng}&includePrePost=false")
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError("Invalid Yahoo chart response")
    r = result[0]
    ts = r.get("timestamp") or []
    quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    rows = []
    for i, t in enumerate(ts):
        values = {}
        for field in ("open", "high", "low", "close"):
            series = quote.get(field) or []
            values[field] = series[i] if i < len(series) else None
        if any(v is None for v in values.values()):
            continue
        row = {"open_time": int(t) * 1000, "close_time": (int(t) + 3600) * 1000 - 1,
               "open": float(values["open"]), "high": float(values["high"]),
               "low": float(values["low"]), "close": float(values["close"]),
               "volume": float((quote.get("volume") or [0] * len(ts))[i] or 0)}
        for field in ("open", "high", "low", "close"):
            positive(row[field], field)
        if rows and row["open_time"] <= rows[-1]["open_time"]:
            raise ValueError("Candles are not strictly ordered")
        rows.append(row)
    return rows, r.get("meta") or {}


def completed_yahoo_daily(rows, now):
    """Drop the final daily bar while the US regular session that produces it is still running."""
    if not rows:
        return rows
    today = ny_local(now).date()
    last_day = ny_local(datetime.fromtimestamp(rows[-1]["open_time"] / 1000, tz=timezone.utc)).date()
    if last_day == today and ny_session(now) in ("pre_market", "regular"):
        return rows[:-1]
    return rows


def completed_yahoo_1h(rows, now):
    if rows and rows[-1]["close_time"] > now.timestamp() * 1000:
        return rows[:-1]
    return rows


def build_indicators(rows, source, since_ms=None):
    closes = [r["close"] for r in rows]
    last = closes[-1]
    out = {"source": source, "bars": len(rows), "last_close": sig(last)}
    if len(rows) < 200:
        since = datetime.fromtimestamp((since_ms or rows[0]["open_time"]) / 1000, tz=timezone.utc).date()
        out["history_note"] = f"仅{len(rows)}根日线（自{since}），EMA200不可用，指标可靠性弱"
    if len(closes) >= 200:
        out["ema200"] = sig(ema(closes, 200))
    if len(closes) >= 50:
        out["ema50"] = sig(ema(closes, 50))
    if len(closes) >= 20:
        out["ema20"] = sig(ema(closes, 20))
    out["rsi14"] = round(rsi(closes), 1)
    out["atr14_simple_mean"] = sig(atr(rows))
    out["atr_pct_of_price"] = round(atr(rows) / last * 100, 2)
    if len(closes) >= 31:
        out["realized_vol_30d_annualized"] = round(realized_vol(closes), 3)
    window = rows[-90:]
    out["range_available"] = {"high": sig(max(r["high"] for r in window)),
                              "low": sig(min(r["low"] for r in window)), "bars": len(window)}
    return out


def build_gate_snapshot(c):
    mark, index = _num(c.get("mark_price")), _num(c.get("index_price"))
    snap = {"contract": c["name"], "mark_price": mark, "index_price": index,
            "last_price": _num(c.get("last_price")),
            "funding": {"rate": _num(c.get("funding_rate")),
                        "indicative": _num(c.get("funding_rate_indicative")),
                        "interval_s": int(c.get("funding_interval") or 0)},
            "leverage": {"min": _num(c.get("leverage_min")), "max": _num(c.get("leverage_max"))},
            "fees": {"maker": _num(c.get("maker_fee_rate")), "taker": _num(c.get("taker_fee_rate"))},
            "quanto_multiplier": _num(c.get("quanto_multiplier")),
            "order_size_min": c.get("order_size_min"),
            "position_size_contracts": _num(c.get("position_size")),
            "long_accounts": c.get("long_users"), "short_accounts": c.get("short_users"),
            "status": c.get("status"), "in_delisting": bool(c.get("in_delisting")),
            "circuit_breaker_enabled": bool(c.get("enable_circuit_breaker")),
            "listed_since": datetime.fromtimestamp(int(c.get("create_time") or 0), tz=timezone.utc).date().isoformat()}
    if mark and index:
        snap["mark_vs_index_pct"] = round((mark - index) / index * 100, 2)
    pos = snap["position_size_contracts"]
    mult = snap["quanto_multiplier"] or 1
    if pos and mark:
        snap["open_interest_notional_usdt_approx"] = sig(pos * mult * mark)
    return snap


def build_symbol_report_full(args, symbol, now):
    contract = resolve_contract(symbol)
    name = contract["name"]
    snap = build_gate_snapshot(contract)
    warnings = []
    if contract.get("status") != "trading" or contract.get("in_delisting"):
        warnings.append(f"合约状态异常: status={contract.get('status')} in_delisting={snap['in_delisting']}")
    if not snap["circuit_breaker_enabled"]:
        warnings.append("该合约未启用熔断（enable_circuit_breaker=false）")

    yahoo_symbol = YAHOO_MAP.get(symbol)
    yahoo_block, yahoo_daily = None, None
    if yahoo_symbol:
        usd = "." not in yahoo_symbol
        try:
            daily_raw, meta = yahoo_chart(yahoo_symbol, "1d", "2y" if usd else "1y")
            daily = completed_yahoo_daily(daily_raw, now)
            if len(daily) < 15:
                raise ValueError("Insufficient Yahoo daily history")
            yahoo_block = {"symbol": yahoo_symbol, "currency": meta.get("currency"),
                           "last": _num(meta.get("regularMarketPrice")), "bars": len(daily),
                           "role": "primary_usd_underlying" if usd else "context_only_non_usd_underlying"}
            yahoo_daily = daily
        except Exception as exc:
            yahoo_block = {"symbol": yahoo_symbol, "error": f"unavailable: {type(exc).__name__}"}
            warnings.append("Yahoo底层不可用，指标退回Gate永续短历史")
    else:
        warnings.append("无已验证的交易所底层：单一场所定价，无外部锚定")

    ind_rows = yahoo_daily if yahoo_block and yahoo_block.get("role") == "primary_usd_underlying" else None
    ind_source = "yahoo_primary_usd" if ind_rows else "gate_24h_perp"
    ind_since_ms = None
    if ind_rows is None:
        ind_rows = gate_klines(name, "1d", 400, now)
        if len(ind_rows) < 15:
            raise ValueError("Insufficient daily history (both layers)")
        ind_since_ms = int(contract.get("create_time") or 0) * 1000 or None
    indicators = build_indicators(ind_rows, ind_source, ind_since_ms)
    if len(ind_rows) < 200:
        warnings.append("历史不足200根日线：长周期均线缺失，指标可靠性弱")

    four_h = gate_klines(name, "4h", 300, now)
    highs, lows = pivots(four_h)
    last_close = indicators["last_close"]

    mtf = {}
    one_h, mtf_source = None, None
    if yahoo_symbol and yahoo_block and yahoo_block.get("role") == "primary_usd_underlying":
        try:
            one_h_raw, _ = yahoo_chart(yahoo_symbol, "1h", "60d")
            one_h = completed_yahoo_1h(one_h_raw, now)
            mtf_source = "yahoo_regular_hours"
        except Exception:
            one_h = None
    if not one_h or len(one_h) < 50:
        one_h = gate_klines(name, "1h", 300, now)
        mtf_source = "gate_24h"
    if len(one_h) >= 50:
        closes_1h = [r["close"] for r in one_h]
        e20, e50 = ema(closes_1h, 20), ema(closes_1h, 50)
        state = ("above" if closes_1h[-1] > e20 > e50 else "below" if closes_1h[-1] < e20 < e50 else "mixed")
        mtf["1h"] = {"source": mtf_source, "close": sig(closes_1h[-1]), "rsi14": round(rsi(closes_1h), 1),
                     "ema20": sig(e20), "ema50": sig(e50), "state": state}
    else:
        mtf["1h"] = {"note": "unavailable_or_insufficient"}
    nearest = None
    for price, kind in [(p[1], "high") for p in highs] + [(p[1], "low") for p in lows]:
        if nearest is None or abs(price - last_close) < abs(nearest[0] - last_close):
            nearest = (price, kind)
    if nearest is not None:
        mtf["nearest_4h_pivot"] = {"kind": nearest[1], "price": sig(nearest[0]),
                                   "dist_pct": round(abs(nearest[0] - last_close) / last_close * 100, 2)}
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
    mtf["usage"] = "日内背景描述非信号；1H来源见source（yahoo=仅正股时段，gate=24/7永续）"

    premium = {"mark_vs_index_pct": snap.get("mark_vs_index_pct")}
    ylast = (yahoo_block or {}).get("last")
    if yahoo_block and yahoo_block.get("role") == "primary_usd_underlying" and ylast and snap.get("mark_price"):
        premium["gate_vs_underlying_pct"] = round((snap["mark_price"] - ylast) / ylast * 100, 2)
        if abs(premium["gate_vs_underlying_pct"]) > 5:
            warnings.append("Gate标记价与底层现价偏差超5%，数据或锚定异常，先核查再使用")

    atr_value = indicators["atr14_simple_mean"]
    cards = []
    if args.auto:
        e50v, e200v, e20v = indicators.get("ema50"), indicators.get("ema200"), indicators.get("ema20")
        if e200v is not None and e50v is not None:
            biases = ["long"] if last_close > e50v > e200v else ["short"] if last_close < e50v < e200v else ["long", "short"]
        elif e50v is not None and e20v is not None:
            biases = ["long"] if last_close > e20v > e50v else ["short"] if last_close < e20v < e50v else ["long", "short"]
        else:
            biases = ["long", "short"]
        for bias in biases:
            scenario = copy(args)
            scenario.bias = bias
            scenario.entry = last_close
            width = 1.3 * atr_value
            structural = lows[-1][1] if bias == "long" and lows else highs[-1][1] if bias == "short" and highs else last_close
            scenario.stop = min(structural, last_close - width) if bias == "long" else max(structural, last_close + width)
            if scenario.stop <= 0 or scenario.stop == scenario.entry:
                continue
            card = conditional_card(scenario, atr_value)
            card["basis"] = f"{ind_source}指标状态情景，非预测；入场为最新完成K线收盘，非实时"
            card["validity"] = "下一根4H收盘重审；财报日与时段跳空未核查前不升级为可执行"
            cards.append(card)

    return {"symbol": symbol, "gate_contract": name, "fetched_at_utc": now.isoformat(),
            "us_equity_session": ny_session(now),
            "gate": snap, "underlying": yahoo_block,
            "indicators": indicators,
            "pivots_4h_gate": {"last_confirmed_high": sig(highs[-1][1]) if highs else None,
                               "last_confirmed_low": sig(lows[-1][1]) if lows else None},
            "multi_timeframe": mtf, "premium": premium,
            "warnings": warnings,
            "conditional_order_card": cards[0] if cards else conditional_card(args, atr_value),
            "alternative_conditional_cards": cards[1:]}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parser():
    ap = Parser(description=__doc__)
    ap.add_argument("--symbols", default="", help="Comma list, required (no default), max 4, e.g. CXMT,UNITREE,ZHIPU")
    ap.add_argument("--list", nargs="?", const="", default=None, metavar="FILTER",
                    help="List Gate stock contracts (contract_type=stocks); optional substring filter, bare --list lists all")
    ap.add_argument("--tranches", type=int, default=3, help="Equal-weight entry ladder size, 1-5, default 3")
    ap.add_argument("--bias", choices=["long", "short", "neutral"])
    ap.add_argument("--auto", action="store_true", help="Conditional EMA-state scenarios from indicator layer")
    for name in ("entry", "stop", "equity", "risk-pct", "max-leverage"):
        ap.add_argument("--" + name, type=float)
    ap.add_argument("--json", action="store_true")
    return ap


def validate(args):
    if args.list is not None:
        args.symbol_list = []
        return
    if not args.symbols:
        raise ValueError("stock 无默认标的：--symbols 必填（最多4个）；不知道合约名就先 --list 查可交易的股票合约")
    for name in ("entry", "stop", "equity", "risk_pct", "max_leverage"):
        value = getattr(args, name)
        if value is not None:
            positive(value, name)
    if args.risk_pct is not None and args.risk_pct > 100:
        raise ValueError("risk-pct must not exceed 100")
    if not 1 <= args.tranches <= 5:
        raise ValueError("tranches must be between 1 and 5")
    args.symbol_list = parse_symbols(args.symbols, "--symbols", 4)
    if args.auto and any(v is not None for v in (args.bias, args.entry, args.stop)):
        raise ValueError("--auto is context-only and cannot be combined with bias/levels")
    if args.bias in ("long", "short"):
        if args.entry is None or args.stop is None:
            raise ValueError("Directional scenarios require entry and stop")
        if (args.bias == "long" and args.stop >= args.entry) or (args.bias == "short" and args.stop <= args.entry):
            raise ValueError("Stop must be below long entry or above short entry")
    elif args.entry is not None or args.stop is not None:
        raise ValueError("Entry/stop require long or short bias; neutral has no directional position")


def conditional_card(args, atr_value=None):
    card = {"status": "research_only_not_an_order", "bias": args.bias or "unspecified",
            "entry": sig(args.entry), "stop_trigger": sig(args.stop), "units": None,
            "execution_gate": "Verify live price, trigger, validity, session, funding cost, venue specs",
            "stop_warning": "Stop triggers do not guarantee execution; earnings and session gaps can exceed any cap"}
    if args.bias not in ("long", "short"):
        card["reason"] = "No directional signal; waiting is a valid answer"
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
    card["quantity_formula"] = "user_risk_budget / abs(avg_entry - stop); contracts x quanto_multiplier; before fees/slippage/funding"
    if len(levels) > 1:
        card["tranche_ladder"] = {
            "levels": [sig(v) for v in levels], "equal_weight": True, "avg_entry": sig(avg_entry),
            "note": "分批均摊成本，各批共用同一失效价；总风险预算覆盖全部批次"}
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


def build_list(filter_str):
    contracts = fetch_contracts()
    tickers = {}
    try:
        for t in get(f"{GATE}/futures/usdt/tickers"):
            if isinstance(t, dict) and t.get("contract"):
                tickers[t["contract"]] = t
    except Exception:
        pass
    rows = []
    for name, c in sorted(contracts.items()):
        if c.get("contract_type") != "stocks":
            continue
        if filter_str and filter_str.upper() not in name:
            continue
        t = tickers.get(name, {})
        rows.append({"contract": name, "mark": _num(c.get("mark_price")),
                     "vol24h_quote": _num(t.get("volume_24h_quote")),
                     "lev_max": _num(c.get("leverage_max")),
                     "yahoo_underlying": YAHOO_MAP.get(name.removesuffix("_USDT"))})
    return {"filter": filter_str, "count": len(rows), "rows": rows[:120],
            "note": "contract_type=stocks 的 Gate USDT 永续；yahoo_underlying 为空表示无已验证的交易所底层"}


def build_report(args):
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=min(4, len(args.symbol_list))) as pool:
        reports = list(pool.map(lambda sym: _safe_symbol_report(args, sym, now), args.symbol_list))
    failures = [r for r in reports if "error" in r]
    if len(failures) == len(reports):
        raise ValueError(f"All symbols failed: {failures[0].get('error')}")
    envelope = {"schema": "stock_ctx_v1", "generated_at_utc": now.isoformat(),
                "symbols": reports, "shared_warnings": list(SHARED_WARNINGS)}
    if failures:
        envelope["partial_failures"] = [f["symbol"] for f in failures]
    return envelope


def _safe_symbol_report(args, symbol, now):
    try:
        return build_symbol_report_full(args, symbol, now)
    except Exception as exc:
        return {"symbol": symbol, "error": str(exc)}


def _ms_to_str(ms):
    if not ms:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M") + "Z"


def format_symbol_human(report):
    lines = [f"== {report['symbol']}（{report['gate_contract']}）@ {report['fetched_at_utc'][:19]}Z =="]
    g = report["gate"]
    fund = g.get("funding", {})
    rate = fund.get("rate")
    interval_h = (fund.get("interval_s") or 0) / 3600
    fund_text = f"{rate * 100:.4f}%/{interval_h:.0f}h" if rate is not None and interval_h else "n/a"
    oi = g.get("open_interest_notional_usdt_approx")
    oi_text = f"${sig(oi / 1e6)}M" if oi else "n/a"
    la, sa = g.get("long_accounts"), g.get("short_accounts")
    ls_text = f"{la}/{sa}（{round(la / sa, 2)}）" if la and sa else "n/a"
    lev = (g.get("leverage") or {}).get("max")
    prem = report.get("premium", {})
    lines.append(f"Gate标记 {g['mark_price']} | 指数锚 {g['index_price']} | 溢价 {prem.get('mark_vs_index_pct', 'n/a')}%"
                 f" | 资金费 {fund_text} | 杠杆上限 {lev}x")
    lines.append(f"OI名义≈{oi_text} | 多空账户 {ls_text} | 上市 {g['listed_since']}"
                 f" | 熔断 {'开' if g['circuit_breaker_enabled'] else '关'}")
    u = report.get("underlying")
    if u is None:
        lines.append("底层：无已验证交易所底层（单一场所定价）")
    elif "error" in u:
        lines.append(f"底层：{u['symbol']} {u['error']}")
    else:
        base = (f"底层：{u['symbol']} 现价 {u['last']} {u.get('currency')}")
        if u.get("role") == "context_only_non_usd_underlying":
            base += f"（非美元计价，仅背景，指标用Gate）"
        gp = prem.get("gate_vs_underlying_pct")
        if gp is not None:
            base += f" | Gate vs 底层 {gp:+.2f}%"
        lines.append(base)
    lines.append(f"美股时段：{report['us_equity_session']}")
    ind = report["indicators"]
    parts = [f"来源 {ind['source']}", f"收盘 {ind['last_close']}（{ind['bars']}根）"]
    for k in ("ema20", "ema50", "ema200"):
        if k in ind:
            parts.append(f"{k.upper()} {ind[k]}")
    parts.append(f"RSI14 {ind['rsi14']}")
    parts.append(f"ATR {ind['atr14_simple_mean']}（{ind['atr_pct_of_price']}%）")
    if "realized_vol_30d_annualized" in ind:
        parts.append(f"RV30 {ind['realized_vol_30d_annualized'] * 100:.1f}%")
    lines.append(" | ".join(parts))
    if ind.get("history_note"):
        lines.append(f"历史：{ind['history_note']}")
    pv = report["pivots_4h_gate"]
    lines.append(f"4H摆点(Gate 24/7) 高 {pv['last_confirmed_high']} / 低 {pv['last_confirmed_low']}")
    m = report["multi_timeframe"]
    h1 = m.get("1h", {})
    h1_text = (f"1H[{h1.get('source')}] RSI {h1['rsi14']} EMA20/50 {h1['ema20']}/{h1['ema50']}（{h1['state']}）"
               if "rsi14" in h1 else "1H n/a")
    pivot = m.get("nearest_4h_pivot")
    pivot_text = (f"距最近4H{'高' if pivot['kind'] == 'high' else '低'}点 {pivot['price']}（{pivot['dist_pct']}%）"
                  if pivot else "无最近摆点")
    flags_text = "、".join(m.get("intraday_flags", [])) or "-"
    lines.append(f"多周期 {h1_text} | {pivot_text} | 日内标记: {flags_text}")
    for w in report.get("warnings", []):
        lines.append(f"⚠ {w}")
    cards = [report["conditional_order_card"]] + report.get("alternative_conditional_cards", [])
    for card in cards:
        lines.append(f"-- 卡（{card['bias']}，研究草案非订单）--")
        if card["bias"] == "unspecified":
            lines.append("未给方向假设：等待用户方向/入场/失效价，或用 --auto 生成条件情景")
            continue
        lines.append(f"entry {card.get('entry')} | stop {card.get('stop_trigger')}"
                     f" | R1/R2/R3 {card.get('gross_R_targets', {}).get('1')}/{card.get('gross_R_targets', {}).get('2')}"
                     f"/{card.get('gross_R_targets', {}).get('3')}"
                     f" | 每$100风险 {card.get('units_per_100_usd_risk_example')} 张")
        ladder = card.get("tranche_ladder")
        if ladder:
            lines.append(f"分批 {ladder['levels']}（等权，均价 {ladder['avg_entry']}，共用失效价）")
        if card.get("sizing_status"):
            lines.append(f"sizing {card['sizing_status']}")
    return lines


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    json_mode = "--json" in argv
    try:
        args = parser().parse_args(argv)
        validate(args)
        if args.list is not None:
            listing = build_list(args.list)
            if json_mode:
                print(json.dumps(listing, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
            else:
                print(f"Gate 股票合约（{listing['count']} 个，标记价/24h成交/杠杆上限/已验证底层）")
                for row in listing["rows"]:
                    vol = row["vol24h_quote"]
                    vol_text = f"{sig(vol / 1e6)}M" if vol and vol >= 1e6 else (f"{sig(vol / 1e3)}k" if vol else "n/a")
                    print(f"{row['contract']} | mark {row['mark']} | 24h ${vol_text} | lev {row['lev_max']}x | yahoo {row['yahoo_underlying'] or '-'}")
            return 0
        report = build_report(args)
        if json_mode:
            print(json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        else:
            print("条件挂单卡（股票研究草案，默认只读，不自动下单）")
            for symbol_report in report["symbols"]:
                if "error" in symbol_report:
                    print(f"== {symbol_report['symbol']} == 数据失败: {symbol_report['error']}")
                    continue
                for line in format_symbol_human(symbol_report):
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
