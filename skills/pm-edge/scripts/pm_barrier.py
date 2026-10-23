#!/usr/bin/env python3
"""Touch / terminal probability for a crypto price market, from implied vol.

Output is a risk-neutral model approximation. It is not a calibrated real-world
probability, and it cannot see how the settlement source samples prices (a 1-minute
candle high/low is not a continuous path). Use it to sanity-check a price, not to
declare edge.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone

from pm_common import norm_cdf


def touch_prob(spot: float, barrier: float, sigma: float, years: float) -> float:
    """P(max S >= barrier) for driftless GBM; barrier above spot."""
    if years <= 0 or sigma <= 0:
        return 1.0 if spot >= barrier else 0.0
    level = math.log(barrier / spot)
    if level <= 0:
        return 1.0
    nu = -0.5 * sigma * sigma
    srt = sigma * math.sqrt(years)
    return max(0.0, min(1.0, norm_cdf((nu * years - level) / srt)
                        + math.exp(2 * nu * level / (sigma * sigma))
                        * norm_cdf((-nu * years - level) / srt)))


def terminal_prob(spot: float, barrier: float, sigma: float, years: float) -> float:
    """P(S_T >= barrier)."""
    if years <= 0 or sigma <= 0:
        return 1.0 if spot >= barrier else 0.0
    nu = -0.5 * sigma * sigma
    return norm_cdf((math.log(spot / barrier) + nu * years) / (sigma * math.sqrt(years)))


def main() -> int:
    ap = argparse.ArgumentParser(description="Barrier probability from implied vol (model approximation).")
    ap.add_argument("--spot", type=float, required=True)
    ap.add_argument("--barrier", type=float, required=True)
    ap.add_argument("--deadline", required=True, help="ISO timestamp with timezone, e.g. 2026-11-01T03:59:00+00:00")
    ap.add_argument("--iv", type=float, required=True, help="Implied vol as a decimal (0.40 = 40%).")
    ap.add_argument("--mode", choices=["touch", "terminal"], default="touch")
    ap.add_argument("--ask", type=float, default=None, help="Market ask for the side you are pricing.")
    ap.add_argument("--fee-rate", type=float, default=None, help="Omit if unknown; result stays un-costed.")
    ap.add_argument("--side", choices=["yes", "no"], default="yes")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    deadline = datetime.fromisoformat(args.deadline)
    if deadline.tzinfo is None:
        ap.error("--deadline must carry a timezone offset")
    years = (deadline - datetime.now(timezone.utc)).total_seconds() / (365 * 86400)
    if years <= 0:
        ap.error("deadline is in the past")

    p = touch_prob(args.spot, args.barrier, args.iv, years) if args.mode == "touch" \
        else terminal_prob(args.spot, args.barrier, args.iv, years)
    out = {
        "mode": args.mode, "spot": args.spot, "barrier": args.barrier,
        "move_pct": (args.barrier / args.spot - 1) * 100, "days": years * 365,
        "iv": args.iv, "p_yes_model": p, "p_no_model": 1 - p,
        "label": "risk-neutral model approximation, not a calibrated probability",
    }
    if args.mode == "touch":
        out["caveat"] = ("continuous-path assumption; discrete 1-minute sampling and gap risk "
                         "change the real trigger probability")
    if args.ask is not None:
        price = args.ask if args.side == "yes" else 1 - args.ask
        fee = None if args.fee_rate is None else args.fee_rate * price * (1 - price)
        out.update(side=args.side, executable_price=price, fee_per_share=fee)
        if fee is not None:
            out["net_cost"] = price + fee
            out["model_edge"] = (p if args.side == "yes" else 1 - p) - (price + fee)
        else:
            out["warning"] = "fee unknown: no net edge computed"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
