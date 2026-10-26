#!/usr/bin/env python3
"""Read-only market discovery for Polymarket tags/keywords and Kalshi series.

This is a discovery tool: it ranks and describes mechanics (quotes, spread,
book liquidity, equal-share YES+NO pair cost). It does not estimate outcome
probability and it is not a buy list. A price is not evidence of edge.

Optional keyword filtering is deliberately simple substring matching over the
event title and question, so it works for tweet-count, awards, box office,
sports and politics alike rather than only weather brackets.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

from pm_common import (FetchError, GAMMA, KALSHI, analyze_books, as_float,
                       http_json, markdown_cell, parse_list, yes_book)


def polymarket_events(tags: list[str], limit: int):
    rows, seen = [], set()
    for tag in tags or [""]:
        url = (f"{GAMMA}/events?active=true&closed=false&limit={limit}"
               f"&order=volume24hr&ascending=false")
        if tag:
            url += f"&tag_slug={urllib.parse.quote(tag)}"
        for event in http_json(url):
            for market in event.get("markets") or []:
                key = str(market.get("conditionId") or market.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append((event, market))
    return rows


def kalshi_markets(series: list[str], limit: int):
    rows = []
    for ticker in series:
        url = (f"{KALSHI}/markets?status=open&limit={limit}"
               f"&series_ticker={urllib.parse.quote(ticker)}&mve_filter=exclude")
        data = http_json(url)
        for market in data.get("markets") or []:
            rows.append((None, market))
    return rows


def normalize(event, market, platform: str):
    if platform == "polymarket":
        prices = [as_float(p) for p in parse_list(market.get("outcomePrices"))]
        if len(prices) != 2 or any(p is None for p in prices):
            return None
        bid, ask = as_float(market.get("bestBid")), as_float(market.get("bestAsk"))
        tokens = parse_list(market.get("clobTokenIds"))
        fee_schedule = market.get("feeSchedule") or {}
        if not market.get("feesEnabled"):
            fee_rate, fee_known = 0.0, True          # explicitly no fee on this market
        else:
            fee_rate = as_float(fee_schedule.get("rate"))
            fee_known = fee_rate is not None          # enabled but rate missing = unknown
        return {
            "platform": "polymarket",
            "question": market.get("question") or "",
            "label": market.get("groupItemTitle") or "",
            "event": (event or {}).get("title") or "",
            "yes_bid": bid, "yes_ask": ask,
            "no_bid": None, "no_ask": None,
            "spread": (ask - bid) if (ask is not None and bid is not None) else None,
            "volume": as_float(market.get("volume24hr")) or 0.0,
            "liquidity": as_float(market.get("liquidity")) or 0.0,
            "fee_rate": fee_rate,
            "fee_known": fee_known,
            "yes_token": tokens[0] if len(tokens) > 0 else None,
            "no_token": tokens[1] if len(tokens) > 1 else None,
            "neg_risk": bool(market.get("negRisk")),
        }

    def dollars(prefix):
        value = as_float(market.get(f"{prefix}_dollars"))
        if value is not None:
            return value
        cents = as_float(market.get(prefix))
        return cents / 100.0 if cents is not None else None

    # Kalshi returns YES bids and NO bids only. Buying YES is offered at
    # 1 - best NO bid; buying NO at 1 - best YES bid (documented on the API page).
    yes_bid, no_bid = dollars("yes_bid"), dollars("no_bid")
    yes_ask = 1.0 - no_bid if no_bid is not None else None
    no_ask = 1.0 - yes_bid if yes_bid is not None else None
    return {
        "platform": "kalshi",
        "question": market.get("title") or "",
        "label": market.get("yes_sub_title") or "",
        "event": market.get("event_ticker") or "",
        "yes_bid": yes_bid, "yes_ask": yes_ask,
        "no_bid": no_bid, "no_ask": no_ask,
        "spread": (yes_ask - yes_bid) if (yes_ask is not None and yes_bid is not None) else None,
        "volume": (as_float(market.get("volume_24h_fp")) or 0.0) * (yes_ask or 0.0),
        "liquidity": (as_float(market.get("open_interest_fp")) or 0.0) * (yes_ask or 0.0),
        "fee_rate": None, "fee_known": False,
        "yes_token": None, "no_token": None,
        "neg_risk": False,
        "quote_note": "Kalshi 卖价由对侧买盘反推；数量阶梯需再次核对",
    }


def matches(row, words: list[str]) -> bool:
    if not words:
        return True
    blob = f"{row['event']} {row['question']} {row['label']}".lower()
    return any(w in blob for w in words)


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover prediction markets. Research input, not advice.")
    ap.add_argument("--platform", choices=["polymarket", "kalshi"], default="polymarket")
    ap.add_argument("--tag", default="", help="Polymarket tag slugs, comma separated (empty = top by volume).")
    ap.add_argument("--series", default="", help="Kalshi series tickers, comma separated.")
    ap.add_argument("--query", default="",
                    help="关键词过滤（标题/问题/档位，逗号分隔，任一命中）。适合推文、奖项、票房等。")
    ap.add_argument("--limit", type=int, default=40, help="Events (or markets per series) to fetch.")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--min-volume", type=float, default=1000.0)
    ap.add_argument("--max-spread", type=float, default=0.06)
    ap.add_argument("--books", type=int, default=0, help="读取前 N 行两侧真实订单簿")
    ap.add_argument("--shares", type=float, default=20.0,
                    help="每侧计划买入份数（配对分析用同一数量）")
    ap.add_argument("--fee-per-share", type=float, default=None,
                    help="已知每份费用；不提供则只给毛价差观察")
    ap.add_argument("--discount-ticks", type=int, default=0,
                    help="等待限价：低于本侧 ask 多少个 tick 的被动买入情景")
    ap.add_argument("--tick-size", type=float, default=None,
                    help="价格 tick；与 --discount-ticks 同时给才计算等待限价")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        if args.platform == "polymarket":
            tags = [t.strip() for t in args.tag.split(",") if t.strip()]
            raw = polymarket_events(tags, args.limit)
        else:
            if not args.series:
                ap.error("--series is required for kalshi")
            raw = kalshi_markets([s.strip() for s in args.series.split(",") if s.strip()],
                                 args.limit)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    words = [w.strip().lower() for w in args.query.split(",") if w.strip()]
    rows = [r for event, market in raw if (r := normalize(event, market, args.platform))]
    rows = [r for r in rows if matches(r, words)]
    passed = []
    for row in rows:
        if row["volume"] < args.min_volume:
            continue
        if row["spread"] is None or row["spread"] > args.max_spread:
            continue
        if row["yes_ask"] is None or not (0.01 <= row["yes_ask"] <= 0.99):
            continue
        passed.append(row)
    passed.sort(key=lambda r: -r["volume"])
    shown = passed[:args.top]

    for row in shown[:args.books]:
        if not row.get("yes_token"):
            continue
        try:
            yb, ya = yes_book(row["yes_token"])
            nb, na = yes_book(row["no_token"]) if row.get("no_token") else ([], [])
            info = analyze_books(yb, ya, nb, na, args.shares,
                                 fee_per_share=args.fee_per_share,
                                 discount_ticks=args.discount_ticks,
                                 tick_size=args.tick_size)
        except (FetchError, ValueError) as exc:
            row["book_error"] = f"{type(exc).__name__}: {exc}"
            continue
        row.update(info)

    if args.json:
        print(json.dumps(shown, ensure_ascii=False, indent=2, default=str))
        return 0

    now_utc = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    label = args.tag or args.series or "-"
    print(f"SCAN {args.platform}  tags/series={label}  keywords={args.query or '-'}  "
          f"candidates={len(passed)}  {now_utc:%Y-%m-%d %H:%M} UTC")
    print(f"{'#':>3} {'event | bracket':<46} {'YES bid/ask':>13} {'spread':>7} "
          f"{'vol':>8} {'费用':>10} {'买盘NO占比':>9} {'卖盘NO占比':>9}")
    for i, row in enumerate(shown, 1):
        fee = (f"{row['fee_rate'] * 100:.1f}%" if row.get("fee_rate") is not None
               else ("0%?" if row["fee_known"] else "未知"))
        bid = f"{row['yes_bid']:.3f}" if row.get("yes_bid") is not None else "-"
        ask = f"{row['yes_ask']:.3f}" if row.get("yes_ask") is not None else "-"
        name = row["event"] or row["question"]
        if row["label"]:
            name = f"{name} | {row['label']}"
        bid_share = row.get("bid_no_share")
        ask_share = row.get("ask_no_share")
        print(f"{i:>3} {markdown_cell(name)[:46]:<46} {bid + '/' + ask:>13} "
              f"{row['spread'] * 100:>6.1f}c {row['volume']:>8.0f} {fee:>10} "
              f"{'-' if bid_share is None else f'{bid_share:.0%}':>9} "
              f"{'-' if ask_share is None else f'{ask_share:.0%}':>9}")

    print("\n读法：买盘/卖盘 NO 占比 = 最优价 2¢ 内美元挂单占比，是流动性描述，"
          "\n不是人数、信心或概率；单次快照也不证明动量。价格不是证据，"
          "\n先审结算规则与日期，再写独立概率；费用未知只算毛价差。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
