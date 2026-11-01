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


def _safe_ledger(path: str) -> str:
    """Normalize and pin the ledger under the user's home directory."""
    resolved = os.path.realpath(os.path.expanduser(path))
    home = os.path.realpath(os.path.expanduser("~"))
    if not resolved.startswith(home + os.sep) and resolved != home:
        raise ValueError(f"ledger must stay under the home directory: {resolved}")
    return resolved


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


def valid_probability(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1)


def timestamp(value):
    """Require an explicit timezone; never guess local time for evidence."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def temporal_evidence(row):
    """Self-reported ordering only, not proof of historical availability."""
    try:
        cutoff = timestamp(row["available_cutoff_at"])
        forecast = timestamp(row["predicted_at"])
        logged = timestamp(row["logged_at"])
        resolved = timestamp(row["resolved_at"])
        return cutoff <= forecast <= logged < resolved
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def score(rows):
    resolved = [r for r in rows if r.get("outcome") in ("yes", "no")]
    valid = [r for r in resolved if valid_probability(r.get("p"))]
    paired = [r for r in valid if valid_probability(r.get("price"))]

    def brier(sample, key):
        return sum((r[key] - (r["outcome"] == "yes")) ** 2 for r in sample) / len(sample)

    print("描述性评分（非正式无泄漏验证）；Brier 越低越好。")
    print(f"resolved={len(resolved)}  valid_p={len(valid)}  excluded_p={len(resolved) - len(valid)}")
    if not valid:
        print("no valid resolved records yet; Brier and calibration need valid probabilities and outcomes")
    else:
        print(f"all_valid_n={len(valid)}  Brier(yours)={brier(valid, 'p'):.4f}")
    print(f"paired_n={len(paired)}  excluded_missing_or_invalid_price={len(valid) - len(paired)}")
    if paired:
        model, market = brier(paired, "p"), brier(paired, "price")
        print(f"paired Brier(yours)={model:.4f}  Brier(market price)={market:.4f}")
        print(f"skill={1 - model / market:+.1%}" if market > 0 else "skill=undefined (market Brier is zero)")
    evidence = sum(temporal_evidence(r) for r in valid)
    print(f"self_reported_temporal_order_n={evidence}  missing_or_invalid_temporal_evidence_n={len(valid) - evidence}")
    print("可靠性分箱：全部有效模型预测；空箱显示 n=0，末箱包含 1。")
    for index in range(10):
        bucket = [r for r in valid if min(int(r["p"] * 10), 9) == index]
        label = f"[{index / 10:.1f},{(index + 1) / 10:.1f}{']' if index == 9 else ')'}"
        if bucket:
            mean = sum(r["p"] for r in bucket) / len(bucket)
            frequency = sum(r["outcome"] == "yes" for r in bucket) / len(bucket)
            print(f"{label} n={len(bucket)} mean_p={mean:.4f} observed_yes={frequency:.4f} gap={frequency - mean:+.4f}")
        else:
            print(f"{label} n=0 mean_p=NA observed_yes=NA gap=NA")
    print("时间字段是自报证据，旧记录缺失不得视为无泄漏验证；本工具不提供正式验证。")
    print("正式验证还需可审计的时点数据、结算规则、样本外设计及独立性/选择偏差检查。")
    print("不存在 30 条即足够的阈值；分箱和技能分数无置信保证，相关样本降低有效样本量；只衡量概率，不衡量盈亏。")


def main() -> int:
    ap = argparse.ArgumentParser(description="Log and score your own probability forecasts.")
    ap.add_argument("--ledger", default=DEFAULT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    log = sub.add_parser("log")
    log.add_argument("--question", required=True)
    log.add_argument("--p", type=float, required=True, help="Your probability that YES resolves true.")
    log.add_argument("--price", type=float, default=None, help="Executable ask you recorded, if any.")
    log.add_argument("--predicted-at", default=None, help="Forecast timestamp with timezone; defaults to logging time.")
    log.add_argument("--available-cutoff-at", default=None,
                     help="Self-reported latest information availability timestamp with timezone (not a future settlement cutoff).")
    log.add_argument("--resolve-date", default="")
    log.add_argument("--thesis", default="")
    log.add_argument("--invalidation", default="")
    log.add_argument("--size", type=float, default=0.0)

    res = sub.add_parser("resolve")
    res.add_argument("--id", required=True)
    res.add_argument("--outcome", choices=["yes", "no", "void"], required=True)

    sub.add_parser("score")
    args = ap.parse_args()
    args.ledger = _safe_ledger(args.ledger)

    if args.cmd == "log":
        if not valid_probability(args.p):
            ap.error("--p must be finite and within 0-1")
        if args.price is not None and not valid_probability(args.price):
            ap.error("--price must be finite and within 0-1")
        now = datetime.now(timezone.utc)
        try:
            predicted = timestamp(args.predicted_at) if args.predicted_at else now
            cutoff = timestamp(args.available_cutoff_at) if args.available_cutoff_at else None
            if predicted > now or (cutoff is not None and cutoff > predicted):
                raise ValueError("require available-cutoff-at <= predicted-at <= logging time")
        except (ValueError, TypeError) as exc:
            ap.error(str(exc))
        record = {
            "id": now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6],
            "logged_at": now.isoformat(),
            "predicted_at": predicted.isoformat(),
            "available_cutoff_at": cutoff.isoformat() if cutoff else None,
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
        import pathlib
        pathlib.Path(args.ledger).write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8")
        print(f"resolved {args.id} -> {args.outcome}")
        return 0

    score(load(args.ledger))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
