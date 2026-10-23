#!/usr/bin/env python3
"""Local calibration ledger: log your own probability, then score it later.

Writes only to your machine (default ~/.prediction-market/ledger.jsonl). It stores
no keys, no balances and nothing about order routing - just forecast vs outcome,
so you can see whether your probabilities beat the price you recorded.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import uuid
from datetime import datetime, timezone

DEFAULT = os.path.join(os.path.expanduser("~"), ".prediction-market", "ledger.jsonl")


def load(path: str):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def append(path: str, record: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Log and score your own probability forecasts.")
    ap.add_argument("--ledger", default=DEFAULT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    log = sub.add_parser("log")
    log.add_argument("--question", required=True)
    log.add_argument("--p", type=float, required=True, help="Your probability that YES resolves true.")
    log.add_argument("--price", type=float, default=None, help="Executable ask you recorded, if any.")
    log.add_argument("--resolve-date", default="")
    log.add_argument("--thesis", default="")
    log.add_argument("--invalidation", default="")
    log.add_argument("--size", type=float, default=0.0)

    res = sub.add_parser("resolve")
    res.add_argument("--id", required=True)
    res.add_argument("--outcome", choices=["yes", "no", "void"], required=True)

    sub.add_parser("score")
    args = ap.parse_args()

    if args.cmd == "log":
        if not 0 <= args.p <= 1:
            ap.error("--p must be within 0-1")
        record = {
            "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6],
            "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "question": args.question, "p": args.p, "price": args.price,
            "resolve_date": args.resolve_date, "thesis": args.thesis,
            "invalidation": args.invalidation, "size": args.size,
            "outcome": None,
        }
        append(args.ledger, record)
        print(f"logged {record['id']}  p={args.p:.3f}  price={args.price}")
        return 0

    if args.cmd == "resolve":
        rows = load(args.ledger)
        hit = next((r for r in rows if r["id"] == args.id), None)
        if not hit:
            print(f"error: no record {args.id}")
            return 2
        if hit.get("outcome"):
            print(f"error: already resolved as {hit['outcome']}")
            return 2
        hit["outcome"] = args.outcome
        hit["resolved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(args.ledger, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"resolved {args.id} -> {args.outcome}")
        return 0

    rows = [r for r in load(args.ledger) if r.get("outcome") in ("yes", "no")]
    if not rows:
        print("no resolved records yet; Brier and calibration need outcomes, not intentions")
        return 0
    brier = sum((r["p"] - (1.0 if r["outcome"] == "yes" else 0.0)) ** 2 for r in rows) / len(rows)
    market = [r for r in rows if r.get("price") is not None]
    market_brier = (sum((r["price"] - (1.0 if r["outcome"] == "yes" else 0.0)) ** 2
                        for r in market) / len(market)) if market else None
    print(f"resolved={len(rows)}  Brier(yours)={brier:.4f}")
    if market_brier is not None:
        print(f"Brier(market price)={market_brier:.4f}  skill={1 - brier / market_brier:+.1%}"
              if market_brier > 0 else "market Brier is zero")
    print("样本少于 30 条时，把差异当作噪音；这里只衡量概率，不衡量盈亏。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
