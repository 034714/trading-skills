#!/usr/bin/env python3
"""Liquidity-first discovery for Polymarket + Kalshi event contracts.

Read-only. Ranks markets by *executable* liquidity (spread + ask-side fill at a
fixed share count) on BOTH sides, not by headline volume: trailing 24h volume is
annotated as context only because it is unreliable (Kalshi counts $1 notional per
contract and combos inflate it; ended games keep volume with an empty book).

Output is a compact table the agent can turn into a decisive recommendation.
No probability estimation happens here.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pm_common import (FetchError, KALSHI, as_float, events_by_tag, full_fill,
                       http_json, markdown_cell, parse_list, yes_book)

PM_TAGS = "sports,economics,politics,finance"
KA_SERIES = "KXNBAGAME,KXNFLGAME,KXNHLGAME,KXCPI,KXPAYROLLS,KXFED,KXBTCD"
WORKERS = 10
SHARES = 20
NO_BOOKS = 4
BOOK_TTL = 90.0
CACHE = Path(__file__).resolve().parent / ".cache"


# ---------------------------------------------------------------- helpers

def csv(value: str):
    """CLI comma lists are not JSON; pm_common.parse_list would eat them."""
    return [s.strip() for s in (value or "").split(",") if s.strip()]


def fnum(value):
    """Kalshi fp fields arrive as strings; ''/'0.0000' means absent, not zero."""
    if value in (None, ""):
        return None
    v = as_float(value)
    return v if v not in (None, 0.0) else None


def horizon(close_iso: str | None):
    if not close_iso:
        return "?"
    try:
        close = datetime.fromisoformat(str(close_iso).replace("Z", "+00:00"))
    except ValueError:
        return "?"
    days = (close - datetime.now(timezone.utc)).total_seconds() / 86400.0
    if days < 0:
        return "已收盘"
    if days <= 1:
        return "日内"
    if days <= 7:
        return "1-7天"
    if days <= 30:
        return "1-4周"
    return "1月+"


def is_closed(close_iso: str | None):
    return horizon(close_iso) == "已收盘"


def when(close_iso: str | None):
    """Compact UTC close label so a 6-hour game and a 6-day game don't look alike."""
    if not close_iso:
        return ""
    try:
        close = datetime.fromisoformat(str(close_iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return close.astimezone(timezone.utc).strftime("%m-%d %H:%MZ")


def band_ok(price, tail: bool):
    """Longshot screen: default drops <10c and >90c (documented favourite-longshot bias)."""
    if price is None or tail:
        return True
    return 0.10 <= price <= 0.90


STOP = {"will", "the", "on", "in", "of", "a", "an", "be", "win", "wins", "by",
        "for", "to", "and", "at", "is", "before", "after", "this", "that", "it"}


def title_tokens(text: str):
    words = "".join(c if c.isalnum() else " " for c in (text or "").lower()).split()
    return {w for w in words if w not in STOP and len(w) > 1}


def same_event(a: str, b: str):
    """Heuristic cross-platform matcher; pairs still need human confirmation."""
    ta, tb = title_tokens(a), title_tokens(b)
    if len(ta) < 2 or len(tb) < 2:
        return False
    shared = ta & tb
    return len(shared) >= 3 and len(shared) / len(ta | tb) >= 0.6


def _safe(fn, label):
    try:
        return fn()
    except FetchError as exc:
        print(f"warn: {label}: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------- polymarket

def pm_fee(market):
    """(fee_rate, status). Unknown never becomes zero."""
    if not market.get("feesEnabled"):
        return 0.0, "无费"
    rate = as_float((market.get("feeSchedule") or {}).get("rate"))
    if rate is None:
        return None, "未知"
    return rate, f"{rate:.0%}taker"


def _cached_book(token: str):
    """90s disk cache: CLOB rate-limits bursts, and a rescan should not refetch."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"book_{token[:20]}.json"
    try:
        if path.exists() and time.time() - path.stat().st_mtime < BOOK_TTL:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return ([tuple(x) for x in raw["bids"]], [tuple(x) for x in raw["asks"]])
    except (OSError, ValueError, KeyError):
        pass
    try:
        bids, asks = yes_book(token)
    except FetchError:
        return [], []
    try:
        path.write_text(json.dumps({"bids": bids, "asks": asks}), encoding="utf-8")
    except OSError:
        pass
    return bids, asks


def _pm_yes_book(market):
    tokens = parse_list(market.get("clobTokenIds"))
    return _cached_book(tokens[0]) if tokens else ([], [])


def _pm_no_book(market):
    tokens = parse_list(market.get("clobTokenIds"))
    return _cached_book(tokens[1]) if len(tokens) > 1 else ([], [])


def _empty_fill():
    return {"complete": False, "shares": 0.0, "vwap": None, "cost": 0.0}


def polymarket_rows(tags, per_tag, keep, max_spread, tail):
    events = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fetched = list(pool.map(
            lambda t: _safe(lambda: events_by_tag(t, per_tag), f"tag {t}"), tags))
    for batch in fetched:
        events.extend(batch)
    seen, markets = set(), []
    for ev in events:
        for mk in ev.get("markets") or []:
            key = str(mk.get("conditionId") or mk.get("id"))
            if key in seen:
                continue
            seen.add(key)
            prices = [as_float(p) for p in parse_list(mk.get("outcomePrices"))]
            bid, ask = as_float(mk.get("bestBid")), as_float(mk.get("bestAsk"))
            if len(prices) != 2 or bid is None or ask is None or ask <= bid:
                continue
            close = mk.get("endDate") or ev.get("endDate")
            if is_closed(close):
                continue
            markets.append((ev, mk, as_float(mk.get("volume24hr")) or 0.0, bid, ask))
    markets.sort(key=lambda m: -m[2])
    markets = markets[:keep]
    with ThreadPoolExecutor(max_workers=4) as pool:
        books = list(pool.map(lambda m: _pm_yes_book(m[1]), markets))
    out = []
    for (ev, mk, vol, gbid, gask), (yb, ya) in zip(markets, books):
        ask = ya[0][0] if ya else gask
        bid = (yb[0][0] if yb else gbid)
        if ask is None:
            continue
        rate, fee_txt = pm_fee(mk)
        out.append({
            "platform": "PM", "title": (ev.get("title") or "")[:70],
            "question": (mk.get("question") or "")[:90],
            "yes_bid": bid, "yes_ask": ask,
            "no_bid": None, "no_ask": None,
            "spread": (ask - bid) if bid is not None else None,
            "fill": full_fill(ya, shares=SHARES) if ya else _empty_fill(),
            "no_fill": _empty_fill(),
            "fee_rate": rate, "fee": fee_txt,
            "vol24h": vol, "liquidity": as_float(mk.get("liquidity")) or 0.0,
            "close": mk.get("endDate") or ev.get("endDate"),
            "rules": (mk.get("description") or ev.get("description") or "")[:400],
            "id": mk.get("id"), "tail": tail, "vol_unit": "$",
        })
    # NO-side books are the expensive half: fetch them only for the best few.
    top = sorted(out, key=lambda r: -r["vol24h"])[:NO_BOOKS]
    with ThreadPoolExecutor(max_workers=3) as pool:
        no_books = list(pool.map(
            lambda r: _pm_no_book(next(m[1] for m in markets if m[1].get("id") == r["id"])),
            top))
    for r, (nb, na) in zip(top, no_books):
        if na:
            r["no_ask"], r["no_bid"] = na[0][0], nb[0][0] if nb else None
            r["no_fill"] = full_fill(na, shares=SHARES)
    return out


# ---------------------------------------------------------------- kalshi

def kalshi_book(ticker, shares):
    """Kalshi /orderbook returns BIDS per side; YES asks come from inverting NO bids."""
    try:
        data = http_json(f"{KALSHI}/markets/{urllib.parse.quote(ticker)}/orderbook?depth=20")
    except FetchError:
        return None
    fp = data.get("orderbook_fp") or {}
    no_bids = sorted(((float(p), float(sz)) for p, sz in fp.get("no_dollars") or []),
                     reverse=True)
    yes_asks = [(round(1.0 - p, 4), sz) for p, sz in no_bids if p > 0]
    return full_fill(sorted(yes_asks), shares=shares) if yes_asks else None


def _ka_markets(s: str, per_series: int):
    mk_url = (f"{KALSHI}/markets?status=open&limit={per_series}"
              f"&mve_filter=exclude&series_ticker={urllib.parse.quote(s)}")
    return _safe(lambda: http_json(mk_url).get("markets") or [], f"series {s}")


def _ka_titles(s: str):
    ev_url = (f"{KALSHI}/events?limit=40&status=open&series_ticker={urllib.parse.quote(s)}"
              f"&with_nested_markets=false")
    events = _safe(lambda: http_json(ev_url).get("events") or [], f"events {s}")
    return {e.get("event_ticker"): e.get("title") for e in events if e.get("event_ticker")}


def kalshi_rows(series, per_series, max_spread, tail, books=12):
    out = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        batches = list(pool.map(lambda s: _ka_markets(s, per_series), series))
    # Event titles are a nice-to-have; 2 workers avoids Kalshi TLS handshake limits.
    with ThreadPoolExecutor(max_workers=2) as pool:
        title_maps = list(pool.map(lambda s: _ka_titles(s), series))
    titles = {s: m for s, m in zip(series, title_maps)}
    for s, batch in zip(series, batches):
        ev_titles = titles.get(s) or {}
        for mk in batch:
            yb, ya = fnum(mk.get("yes_bid_dollars")), fnum(mk.get("yes_ask_dollars"))
            if ya is None:
                nb0 = fnum(mk.get("no_bid_dollars"))
                ya = 1.0 - nb0 if nb0 else None
            if ya is None or (yb is not None and ya <= yb):
                continue
            close = mk.get("close_time") or mk.get("expected_expiration_time")
            if is_closed(close):
                continue
            nb = fnum(mk.get("no_bid_dollars"))
            na = fnum(mk.get("no_ask_dollars"))
            if na is None and yb is not None:
                na = 1.0 - yb
            depth = fnum(mk.get("yes_ask_size_fp")) or 0.0
            no_depth = fnum(mk.get("yes_bid_size_fp")) or 0.0
            ev_title = ev_titles.get(mk.get("event_ticker")) or ""
            label = f"{ev_title}｜{mk.get('title')}" if ev_title else (mk.get("title") or "")
            out.append({
                "platform": "KA", "title": label[:70],
                "question": (mk.get("title") or "")[:90],
                "yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na,
                "spread": (ya - yb) if yb is not None else None,
                "fill": {"complete": depth >= SHARES, "shares": min(depth, SHARES),
                         "vwap": ya, "cost": ya * min(depth, SHARES)},
                "no_fill": {"complete": no_depth >= SHARES,
                            "shares": min(no_depth, SHARES),
                            "vwap": na,
                            "cost": (na or 0.0) * min(no_depth, SHARES)},
                "fee_rate": None, "fee": "?",
                "vol24h": fnum(mk.get("volume_24h_fp")) or 0.0,
                "liquidity": fnum(mk.get("open_interest_fp")) or 0.0,
                "close": close,
                "rules": (mk.get("rules_primary") or "")[:400],
                "id": mk.get("ticker"), "tail": tail, "series": s, "vol_unit": "份",
            })
    short = [r for r in out if band_ok(r["yes_ask"], tail)
             and r["spread"] is not None and r["spread"] <= max_spread
             and not (r["fill"]["complete"] or r["no_fill"]["complete"])]
    short.sort(key=lambda r: -r["vol24h"])
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fills = list(pool.map(lambda r: kalshi_book(r["id"], SHARES), short[:books]))
    for r, fill in zip(short[:books], fills):
        if fill:
            r["fill"] = fill
    return out


# ---------------------------------------------------------------- verdict

def side_ok(row, side, max_spread):
    """(ok, reason) for buying one side at its real ask."""
    price = row["yes_ask"] if side == "yes" else row["no_ask"]
    fill = row["fill"] if side == "yes" else row["no_fill"]
    tag = "Y" if side == "yes" else "N"
    if price is None:
        return False, f"{tag}无报价"
    if price < 0.10:
        return False, f"{tag}低价屏"
    if price > 0.90:
        return False, f"{tag}高价屏"
    if row["spread"] is None:
        return False, f"{tag}无买盘"
    if row["spread"] > max_spread:
        return False, f"{tag}价差"
    if not fill or not fill.get("complete"):
        return False, f"{tag}深度{int((fill or {}).get('shares', 0))}<{int(SHARES)}"
    return True, "ok"


def verdict(row, max_spread):
    yok, ywhy = side_ok(row, "yes", max_spread)
    nok, nwhy = side_ok(row, "no", max_spread)
    if yok and nok:
        return "Y/N可挂"
    if yok:
        return "Y可挂"
    if nok:
        return "N可挂"
    return ywhy if ywhy == nwhy else f"{ywhy}+{nwhy}"


def _q(value, fmt=".3f"):
    return format(value, fmt) if value is not None else "-"


def render(rows, max_spread, top, xmatch):
    live = [r for r in rows if side_ok(r, "yes", max_spread)[0]
            or side_ok(r, "no", max_spread)[0]]
    watch = [r for r in rows if r not in live]
    live.sort(key=lambda r: -r["vol24h"])
    lines = [
        "| 平台 | 对象·周期(收盘) | 两侧 ask/bid | 20份 Y/N | 费用 | 24h量 | 状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in live[:top]:
        lines.append("| " + " | ".join([
            r["platform"],
            markdown_cell(r["title"][:44]) + "·" + horizon(r["close"]) + " " + when(r["close"]),
            f"Y {_q(r['yes_ask'])}/{_q(r['yes_bid'])} · N {_q(r['no_ask'])}/{_q(r['no_bid'])}",
            f"Y {_q(r['fill'].get('vwap'))} · N {_q(r['no_fill'].get('vwap'))}",
            r["fee"],
            f"{r['vol24h']:,.0f}{r['vol_unit']}",
            verdict(r, max_spread),
        ]) + " |")
    lines.append("")
    lines.append(f"可挂单 {len(live)} 个；观察区 {len(watch)} 个。"
                 "两侧是真实 ask/bid，20份列是 VWAP；24h量 PM 为美元、KA 为份（$1名义），"
                 "临开赛/刚结束都会失真，只作参考。")
    if watch:
        lines.append("观察区Top：" + "；".join(
            f"{r['platform']} {r['title'][:24]}·{horizon(r['close'])} "
            f"({verdict(r, max_spread)})" for r in
            sorted(watch, key=lambda r: -r["vol24h"])[:6]))
    if xmatch:
        pairs = []
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if a["platform"] != b["platform"] and same_event(a["title"], b["title"]):
                    pairs.append((a, b))
        if pairs:
            lines.append("")
            lines.append("跨平台同事件（需人工确认是同一合约）：")
            for a, b in pairs[:8]:
                lines.append(f"  {a['platform']} {a['title'][:38]} ask {_q(a['yes_ask'])}"
                             f" | {b['platform']} {b['title'][:38]} ask {_q(b['yes_ask'])}"
                             f" | 差 {abs((a['yes_ask'] or 0) - (b['yes_ask'] or 0)) * 100:.1f}c")
        else:
            lines.append("跨平台：本轮无标题匹配的同一事件；指定事件用 --query 同轮读两边更可靠。")
    return "\n".join(lines)


def main():
    global SHARES, NO_BOOKS
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", choices=["both", "polymarket", "kalshi"], default="both")
    ap.add_argument("--tag", default=PM_TAGS, help="Polymarket tag slugs, comma separated")
    ap.add_argument("--series", default=KA_SERIES, help="Kalshi series tickers, comma separated")
    ap.add_argument("--query", default="", help="substring filter on title/question")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--shares", type=int, default=20, help="execution check size")
    ap.add_argument("--max-spread", type=float, default=0.05)
    ap.add_argument("--keep", type=int, default=8, help="Polymarket markets to YES-book-check")
    ap.add_argument("--no-books", type=int, default=4, help="NO-side books to fetch (top by volume)")
    ap.add_argument("--per-tag", type=int, default=12)
    ap.add_argument("--per-series", type=int, default=40)
    ap.add_argument("--tail", action="store_true", help="allow <10c / >90c (high-odds mode)")
    ap.add_argument("--no-x", action="store_true", help="skip cross-platform matching")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    SHARES, NO_BOOKS = args.shares, args.no_books

    t0 = time.time()
    rows = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = []
        if args.platform in ("both", "polymarket"):
            futures.append(pool.submit(polymarket_rows, csv(args.tag), args.per_tag,
                                       args.keep, args.max_spread, args.tail))
        if args.platform in ("both", "kalshi"):
            futures.append(pool.submit(kalshi_rows, csv(args.series), args.per_series,
                                       args.max_spread, args.tail))
        for fut in futures:
            rows += fut.result()
    if args.query:
        q = args.query.lower()
        rows = [r for r in rows if q in (r["title"] + r["question"]).lower()]
    closed = [r for r in rows if horizon(r["close"]) == "已收盘"]
    rows = [r for r in rows if horizon(r["close"]) != "已收盘"]
    elapsed = time.time() - t0

    if args.json:
        for r in rows:
            r["horizon"] = horizon(r["close"])
            r["verdict"] = verdict(r, args.max_spread)
            r["close_utc"] = when(r["close"])
        print(json.dumps({"elapsed_s": round(elapsed, 1), "rows": rows},
                         ensure_ascii=False, default=str))
        return 0
    print(f"# pm-liquid 扫描 {len(rows)} 个候选（{elapsed:.0f}s，{int(SHARES)}份执行检查，"
          f"价差≤{args.max_spread * 100:.0f}c，价格带{'含长尾' if args.tail else '10-90c'}"
          f"{'，剔除已收盘 ' + str(len(closed)) + ' 个' if closed else ''}）")
    print(render(rows, args.max_spread, args.top, not args.no_x))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
