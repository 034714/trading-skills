#!/usr/bin/env python3
"""Read-only market scanner for Polymarket tags and Kalshi series.

Ranks candidates and shows mechanical facts. It does not estimate probabilities
and it is not a buy list: a price is not evidence of edge.
"""
from __future__ import annotations

import argparse
import json
import sys

from pm_common import (FetchError, GAMMA, KALSHI, as_float, depth_usd, http_json,
                       parse_list, vwap, yes_book)

import urllib.parse


def polymarket_events(tags: list[str], limit: int):
    rows = []
    seen = set()
    for tag in tags or [""]:
        url = (f"{GAMMA}/events?active=true&closed=false&limit={limit}"
               f"&order=volume24hr&ascending=false")
        if tag:
            url += f"&tag_slug={urllib.parse.quote(tag)}"
        for event in http_json(url) if True else []:
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
        fee_schedule = market.get("feeSchedule") or {}
        fee_rate = as_float(fee_schedule.get("rate")) if market.get("feesEnabled") else 0.0
        return {
            "platform": "polymarket",
            "question": market.get("question") or "",
            "label": market.get("groupItemTitle") or "",
            "event": (event or {}).get("title") or "",
            "yes_bid": bid, "yes_ask": ask,
            "spread": (ask - bid) if (ask is not None and bid is not None) else None,
            "volume": as_float(market.get("volume24hr")) or 0.0,
            "liquidity": as_float(market.get("liquidity")) or 0.0,
            "fee_rate": fee_rate,
            "fee_known": (not market.get("feesEnabled")) or fee_rate is not None,
            "token": (parse_list(market.get("clobTokenIds")) or [None])[0],
            "neg_risk": bool(market.get("negRisk")),
        }
    def dollars(prefix):
        value = as_float(market.get(f"{prefix}_dollars"))
        if value is not None:
            return value
        cents = as_float(market.get(prefix))
        return cents / 100.0 if cents is not None else None
    bid, ask = dollars("yes_bid"), dollars("yes_ask")
    return {
        "platform": "kalshi",
        "question": market.get("title") or "",
        "label": market.get("yes_sub_title") or "",
        "event": market.get("event_ticker") or "",
        "yes_bid": bid, "yes_ask": ask,
        "spread": (ask - bid) if (ask is not None and bid is not None) else None,
        "volume": (as_float(market.get("volume_24h_fp")) or 0.0) * (ask or 0.0),
        "liquidity": (as_float(market.get("open_interest_fp")) or 0.0) * (ask or 0.0),
        "fee_rate": 0.07, "fee_known": False,
        "token": None, "neg_risk": False,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan Polymarket tags or Kalshi series. Research input, not advice.")
    ap.add_argument("--platform", choices=["polymarket", "kalshi"], default="polymarket")
    ap.add_argument("--tag", default="weather", help="Polymarket tag slugs, comma separated.")
    ap.add_argument("--series", default="", help="Kalshi series tickers, comma separated.")
    ap.add_argument("--limit", type=int, default=40, help="Events (or markets per series) to fetch.")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--min-volume", type=float, default=1000.0)
    ap.add_argument("--max-spread", type=float, default=0.06)
    ap.add_argument("--books", type=int, default=0, help="Check the real YES/NO book for the top N rows.")
    ap.add_argument("--spend", type=float, default=20.0, help="USD size for the VWAP check.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        if args.platform == "polymarket":
            raw = polymarket_events([t.strip() for t in args.tag.split(",") if t.strip()], args.limit)
        else:
            if not args.series:
                ap.error("--series is required for kalshi")
            raw = kalshi_markets([s.strip() for s in args.series.split(",") if s.strip()], args.limit)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rows = [r for event, market in raw if (r := normalize(event, market, args.platform))]
    passed = []
    for row in rows:
        if row["volume"] < args.min_volume:
            continue
        if row["spread"] is None or row["spread"] > args.max_spread:
            continue
        if row["yes_ask"] is None or not (0.02 < row["yes_ask"] < 0.98):
            continue
        passed.append(row)
    passed.sort(key=lambda r: -r["volume"])
    shown = passed[: args.top]

    for row in shown[: args.books]:
        if not row["token"]:
            continue
        try:
            bids, asks = yes_book(row["token"])
            no_asks = sorted(((1 - p, s) for p, s in bids), reverse=True)
            row["no_ask"], row["no_vwap"] = (no_asks[0][0] if no_asks else None), vwap(no_asks, args.spend)[0]
            row["yes_vwap"] = vwap(asks, args.spend)[0]
            row["depth_2c"] = depth_usd(asks)
        except FetchError as exc:
            row["book_error"] = str(exc)

    if args.json:
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        return 0

    now_utc = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    print(f"SCAN {args.platform}  tags/series={args.tag or args.series}  candidates={len(passed)}  {now_utc:%Y-%m-%d %H:%M} UTC")
    print(f"{'#':>3} {'event | bracket':<52} {'YES bid/ask':>13} {'spread':>7} {'vol':>8} {'fee':>8}")
    for i, row in enumerate(shown, 1):
        fee = f"{row['fee_rate'] * 100:.1f}%" if row["fee_known"] else "unknown"
        bid = f"{row['yes_bid']:.3f}" if row["yes_bid"] is not None else "-"
        ask = f"{row['yes_ask']:.3f}" if row["yes_ask"] is not None else "-"
        name = row["event"] or row["question"]
        if row["label"]:
            name = f"{name} | {row['label']}"
        print(f"{i:>3} {name[:52]:<52} {bid + '/' + ask:>13} "
              f"{row['spread'] * 100:>6.1f}c {row['volume']:>8.0f} {fee:>8}")
    print("\n价格不是证据。先审结算规则、确认站点与日期，再自己写概率；"
          "\n费用未知时不要按 0 计算；NO 只能按真实 NO 订单簿成交。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
