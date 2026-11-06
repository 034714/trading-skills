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
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

from pm_common import (FetchError, KALSHI, as_float, events_by_tag, full_fill,
                       http_json, markdown_cell, parse_list, yes_book)

PM_TAGS = "sports,economics,politics,finance"
KA_SERIES = "KXNBAGAME,KXNFLGAME,KXNHLGAME,KXCPI,KXPAYROLLS,KXFED,KXBTCD"
WORKERS = 10
SHARES = 20
NO_BOOKS = 4


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


def pm_label(event_title, question, outcomes):
    """Disambiguate multi-outcome events: show which side YES actually is."""
    outcomes = outcomes or []
    yes_name = outcomes[0] if outcomes else None
    title = event_title or question or ""
    if yes_name and str(yes_name).lower() not in ("yes", "no", "true", "false"):
        return f"{title}｜YES={yes_name}"
    if question and question != title:
        return f"{title}｜{question}"
    return title


def _safe(fn, label):
    """Return (value, error), preserving empty successful responses."""
    try:
        return fn(), None
    except FetchError as exc:
        return None, f"{label}: {exc}"


# ---------------------------------------------------------------- polymarket

def pm_fee(market):
    """(fee_rate, status). Unknown never becomes zero."""
    if not market.get("feesEnabled"):
        return 0.0, "无费"
    rate = as_float((market.get("feeSchedule") or {}).get("rate"))
    if rate is None:
        return None, "未知"
    return rate, f"{rate:.0%}taker"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class SourceErrors(list):
    """Track successful discovery independently of surviving candidates."""
    def __init__(self):
        super().__init__()
        self.discovery_ok = 0


class RoundBooks:
    """Full-token single-flight cache, owned by one scan; never reads/writes disk."""
    def __init__(self):
        self._lock = Lock()
        self._books = {}

    def get(self, token):
        with self._lock:
            owner = token not in self._books
            future = self._books.setdefault(token, Future())
        if owner:
            try:
                bids, asks = yes_book(token)
                future.set_result((bids, asks, utc_now()))
            except Exception as exc:
                future.set_exception(exc)
        return future.result()


def _pm_book(market, side, cache):
    tokens = parse_list(market.get("clobTokenIds"))
    return cache.get(tokens[side]) if len(tokens) > side else ([], [], None)


def matches_query(title, question, query):
    return not query or query.lower() in ((title or "") + (question or "")).lower()


def _empty_fill():
    return {"complete": False, "shares": 0.0, "vwap": None, "cost": 0.0}


def polymarket_rows(tags, per_tag, keep, max_spread, tail, query=""):
    events = []
    errors = SourceErrors()
    cache = RoundBooks()
    def fetch_events(tag):
        batch = events_by_tag(tag, per_tag)
        sampled_at = utc_now()
        return [dict(ev, _sampled_at=sampled_at) for ev in batch]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fetched = list(pool.map(
            lambda t: _safe(lambda: fetch_events(t), f"tag {t}"), tags))
    for batch, error in fetched:
        if error:
            errors.append(error)
        else:
            errors.discovery_ok += 1
        events.extend(batch or [])
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
            label = pm_label(ev.get("title"), mk.get("question"), parse_list(mk.get("outcomes")))
            if not matches_query(label[:70], (mk.get("question") or "")[:90], query):
                continue
            markets.append((ev, mk, as_float(mk.get("volume24hr")) or 0.0, bid, ask))
    markets.sort(key=lambda m: -m[2])
    markets = markets[:keep]
    with ThreadPoolExecutor(max_workers=4) as pool:
        books = list(pool.map(lambda m: _safe(
            lambda: _pm_book(m[1], 0, cache), f"YES book {m[1].get('id')}"), markets))
    out = []
    for (ev, mk, vol, gbid, gask), (book, error) in zip(markets, books):
        if error:
            errors.append(error)
        yb, ya, sampled_at = book or ([], [], None)
        ask = ya[0][0] if ya else gask
        bid = (yb[0][0] if yb else gbid)
        if ask is None:
            continue
        rate, fee_txt = pm_fee(mk)
        label = pm_label(ev.get("title"), mk.get("question"), parse_list(mk.get("outcomes")))
        out.append({
            "platform": "PM", "title": label[:70],
            "yes_book_sampled_at": sampled_at, "no_book_sampled_at": None,
            "quote_sampled_at": ev.get("_sampled_at"),
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
            lambda r: _safe(lambda: _pm_book(
                next(m[1] for m in markets if m[1].get("id") == r["id"]), 1, cache),
                f"NO book {r['id']}"),
            top))
    for r, (book, error) in zip(top, no_books):
        if error:
            errors.append(error)
        nb, na, sampled_at = book or ([], [], None)
        r["no_book_sampled_at"] = sampled_at
        if na:
            r["no_ask"], r["no_bid"] = na[0][0], nb[0][0] if nb else None
            r["no_fill"] = full_fill(na, shares=SHARES)
    return out, errors


# ---------------------------------------------------------------- kalshi

def kalshi_book(ticker, shares, *, strict=False):
    """Kalshi /orderbook returns BIDS per side; YES asks come from inverting NO bids."""
    try:
        data = http_json(f"{KALSHI}/markets/{urllib.parse.quote(ticker)}/orderbook?depth=20")
    except FetchError:
        if strict:
            raise
        return None
    fp = data.get("orderbook_fp") or {}
    no_bids = sorted(((float(p), float(sz)) for p, sz in fp.get("no_dollars") or []),
                     reverse=True)
    yes_asks = [(round(1.0 - p, 4), sz) for p, sz in no_bids if p > 0]
    fill = full_fill(sorted(yes_asks), shares=shares) if yes_asks else None
    if fill is not None:
        fill["sampled_at"] = utc_now()
    return fill


def _ka_markets(s: str, per_series: int):
    mk_url = (f"{KALSHI}/markets?status=open&limit={per_series}"
              f"&mve_filter=exclude&series_ticker={urllib.parse.quote(s)}")
    def fetch():
        markets = http_json(mk_url).get("markets") or []
        sampled_at = utc_now()
        return [dict(m, _sampled_at=sampled_at) for m in markets]
    return _safe(fetch, f"series {s}")


def _ka_titles(s: str):
    ev_url = (f"{KALSHI}/events?limit=40&status=open&series_ticker={urllib.parse.quote(s)}"
              f"&with_nested_markets=false")
    events, error = _safe(lambda: http_json(ev_url).get("events") or [], f"events {s}")
    return {e.get("event_ticker"): e.get("title") for e in (events or [])
            if e.get("event_ticker")}, error


def kalshi_rows(series, per_series, max_spread, tail, books=12, query=""):
    out = []
    errors = SourceErrors()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        batches = list(pool.map(lambda s: _ka_markets(s, per_series), series))
    # Event titles are a nice-to-have; 2 workers avoids Kalshi TLS handshake limits.
    with ThreadPoolExecutor(max_workers=2) as pool:
        title_maps = list(pool.map(lambda s: _ka_titles(s), series))
    titles = {}
    for s, (mapping, error) in zip(series, title_maps):
        titles[s] = mapping
        if error:
            errors.append(error)
    for s, (batch, error) in zip(series, batches):
        if error:
            errors.append(error)
        else:
            errors.discovery_ok += 1
        ev_titles = titles.get(s) or {}
        for mk in batch or []:
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
            if not matches_query(label[:70], (mk.get("title") or "")[:90], query):
                continue
            out.append({
                "quote_sampled_at": mk.get("_sampled_at"),
                "yes_book_sampled_at": None,
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
        fills = list(pool.map(lambda r: _safe(
            lambda: kalshi_book(r["id"], SHARES, strict=True),
            f"orderbook {r['id']}"), short[:books]))
    for r, (fill, error) in zip(short[:books], fills):
        if error:
            errors.append(error)
        if fill:
            r["fill"] = fill
            r["yes_book_sampled_at"] = fill["sampled_at"]
    return out, errors


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
        "| 平台 | 对象·周期(收盘) | 两侧 ask/bid | 20份 Y/N | 费用 | 24h量 | 状态 | 采样UTC(Y/N/列表) |",
        "|---|---|---|---|---|---|---|---|",
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
            "/".join(str(r.get(key) or "-") for key in
                     ("yes_book_sampled_at", "no_book_sampled_at", "quote_sampled_at")),
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
    ap.add_argument("--json-limit", type=int, default=None, help="optional JSON row limit (0 allowed)")
    ap.add_argument("--compact-json", action="store_true", help="JSON without rules/question and whitespace")
    args = ap.parse_args()
    if args.json_limit is not None and args.json_limit < 0:
        ap.error("--json-limit must be nonnegative")
    SHARES, NO_BOOKS = args.shares, args.no_books

    t0 = time.time()
    rows, errors = [], []
    discovery_ok = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = []
        if args.platform in ("both", "polymarket"):
            futures.append(("Polymarket", pool.submit(
                polymarket_rows, csv(args.tag), args.per_tag, args.keep,
                args.max_spread, args.tail, query=args.query)))
        if args.platform in ("both", "kalshi"):
            futures.append(("Kalshi", pool.submit(
                kalshi_rows, csv(args.series), args.per_series,
                args.max_spread, args.tail, query=args.query)))
        for name, future in futures:
            try:
                result, source_errors = future.result()
                rows.extend(result)
                discovery_ok += source_errors.discovery_ok
                errors.extend(f"{name}: {err}" for err in source_errors)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
    if args.query:
        q = args.query.lower()
        rows = [r for r in rows if q in (r["title"] + r["question"]).lower()]
    closed = [r for r in rows if horizon(r["close"]) == "已收盘"]
    rows = [r for r in rows if horizon(r["close"]) != "已收盘"]
    elapsed = time.time() - t0
    completed_at = utc_now()
    if errors:
        print(f"数据源错误（{len(errors)}个）：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        if not discovery_ok:
            print("错误：所有发现请求均失败，无法生成报告", file=sys.stderr)
            return 1
        print("部分数据可用；报告覆盖不完整。", file=sys.stderr)

    if args.json:
        for r in rows:
            r["horizon"] = horizon(r["close"])
            r["verdict"] = verdict(r, args.max_spread)
            r["close_utc"] = when(r["close"])
        total_rows = len(rows)
        if args.json_limit is not None:
            rows = rows[:args.json_limit]
        if args.compact_json:
            rows = [{k: v for k, v in r.items() if k not in ("rules", "question")} for r in rows]
        print(json.dumps({"elapsed_s": round(elapsed, 1), "rows": rows,
                          "completed_at": completed_at, "errors": errors,
                          "total_rows": total_rows, "returned_rows": len(rows)},
                         ensure_ascii=False, default=str,
                         separators=(",", ":") if args.compact_json else None))
        return 0
    print(f"# pm-liquid 扫描 {len(rows)} 个候选（{elapsed:.0f}s，{int(SHARES)}份执行检查，"
          f"价差≤{args.max_spread * 100:.0f}c，价格带{'含长尾' if args.tail else '10-90c'}"
          f"{'，剔除已收盘 ' + str(len(closed)) + ' 个' if closed else ''}）")
    print(f"扫描完成 UTC {completed_at}；数据源错误 {len(errors)} 个。")
    print(render(rows, args.max_spread, args.top, not args.no_x))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
