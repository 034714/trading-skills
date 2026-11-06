#!/usr/bin/env python3
"""Net edge, break-even and unit-risk sizing for a binary contract.

Two things this deliberately refuses to do: invent a fee, and pretend a stop or a
limit guarantees a fill. Costs you do not supply stay unknown.
"""
from __future__ import annotations

import argparse
import json


def main() -> int:
    ap = argparse.ArgumentParser(description="Net edge and unit-risk sizing. Research arithmetic, not advice.")
    ap.add_argument("--p", type=float, required=True, help="Your probability for the side you buy (0-1).")
    ap.add_argument("--price", type=float, required=True, help="Executable ask for that side (0-1).")
    ap.add_argument("--fee-rate", type=float, default=None,
                    help="Platform taker rate. Omit if unknown - the result then stays un-costed.")
    ap.add_argument("--equity", type=float, default=None, help="Account size, if you want a position size.")
    ap.add_argument("--risk-pct", type=float, default=None, help="Percent of equity you accept losing.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not (0.0 <= args.p <= 1.0):
        ap.error("--p must be within 0-1")
    if not (0.0 < args.price < 1.0):
        ap.error("--price must be within 0-1")
    if args.equity is not None and (args.risk_pct is None or args.risk_pct <= 0):
        ap.error("--equity needs a positive --risk-pct")

    fee = None if args.fee_rate is None else args.fee_rate * args.price * (1 - args.price)
    net_cost = args.price + fee if fee is not None else None
    out = {
        "p_side": args.p, "ask": args.price, "fee_rate": args.fee_rate,
        "fee_per_share": fee, "net_cost": net_cost,
        "gross_return_if_right": (1 - args.price) / args.price,
        "fee_included": fee is not None,
    }
    if net_cost is not None:
        out["net_edge"] = args.p - net_cost
        out["relative_ev"] = (args.p - net_cost) / net_cost
        out["breakeven_probability"] = net_cost
        out["losses_to_erase_one_win"] = net_cost / max(args.p - net_cost, 1e-9) if args.p > net_cost else None
        if args.equity is not None:
            budget = args.equity * args.risk_pct / 100
            out["risk_budget"] = budget
            out["shares_if_resolved_binary"] = budget / net_cost
            out["note"] = ("assumes the full net cost is lost on a miss and no stop is used; "
                           "fees beyond the taker rate, slippage and settlement risk are excluded")
    else:
        out["warning"] = "fee unknown: no net edge computed, do not treat gross return as edge"

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
