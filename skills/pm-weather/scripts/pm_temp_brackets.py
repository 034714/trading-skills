#!/usr/bin/env python3
"""Temperature bracket audit: what is already impossible, what is still open.

The ONLY hard inference is monotonic: once today's observed maximum M exceeds a
bracket's upper bound, that bracket cannot win; a minimum works the other way.
Nothing here claims a bracket is final. The day is not over until it is over.

Settlement station, precision and revision rules must come from the contract text;
pass them in explicitly with --station and --precision.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from pm_common import (FetchError, GAMMA, as_float, depth_usd, event_month_day,
                       events_by_tag, http_json, local_today, parse_bracket, tz, vwap, yes_book)

# Polymarket resolves most city markets on the airport METAR; Hong Kong is the
# exception (HKO daily extract). Verify per market before trusting this table.
CITIES = {
    "tokyo": ("RJTT", "Asia/Tokyo"), "seoul": ("RKSI", "Asia/Seoul"),
    "busan": ("RKPK", "Asia/Seoul"), "taipei": ("RCTP", "Asia/Taipei"),
    "shanghai": ("ZSPD", "Asia/Shanghai"), "beijing": ("ZBAA", "Asia/Shanghai"),
    "shenzhen": ("ZGSZ", "Asia/Shanghai"), "guangzhou": ("ZGGG", "Asia/Shanghai"),
    "singapore": ("WSSS", "Asia/Singapore"), "hong kong": ("HKO", "Asia/Hong_Kong"),
    "istanbul": ("LTFM", "Europe/Istanbul"), "london": ("EGLC", "Europe/London"),
    "paris": ("LFPG", "Europe/Paris"), "munich": ("EDDM", "Europe/Berlin"),
    "milan": ("LIMC", "Europe/Rome"), "helsinki": ("EFHK", "Europe/Helsinki"),
    "warsaw": ("EPWA", "Europe/Warsaw"), "ankara": ("LTAC", "Europe/Istanbul"),
    "tel aviv": ("LLBG", "Asia/Jerusalem"), "nyc": ("KLGA", "America/New_York"),
    "los angeles": ("KLAX", "America/Los_Angeles"), "chicago": ("KORD", "America/Chicago"),
    "miami": ("KMIA", "America/New_York"), "dallas": ("KDFW", "America/Chicago"),
    "houston": ("KIAH", "America/Chicago"), "atlanta": ("KATL", "America/New_York"),
    "san francisco": ("KSFO", "America/Los_Angeles"), "boston": ("KBOS", "America/New_York"),
    "denver": ("KDEN", "America/Denver"), "austin": ("KAUS", "America/Chicago"),
    "philadelphia": ("KPHL", "America/New_York"), "phoenix": ("KPHX", "America/Phoenix"),
    "seattle": ("KSEA", "America/Los_Angeles"),
}


def observations(icao: str, offset_name: str):
    """Today's observations at the station, sorted oldest first, in Celsius."""
    zone = tz(offset_name)
    today = local_today(offset_name).date()
    if icao == "HKO":
        data = http_json("https://data.weather.gov.hk/weatherAPI/opendata/weather.php"
                         "?dataType=rhrread&lang=en")
        for item in data.get("temperature", {}).get("data", []):
            if item.get("place") == "Hong Kong Observatory":
                value = float(item["value"])
                # The public feed is integer-only: it cannot separate 30 from 31.
                return [(today, value)], True
        return None, False
    obs = http_json(f"https://aviationweather.gov/api/data/metar?ids={icao}&hours=30&format=json")
    rows = []
    for ob in obs or []:
        stamp, temp = ob.get("obsTime"), ob.get("temp")
        if stamp is None or temp is None:
            continue
        local = datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone(zone)
        if local.date() == today:
            rows.append((local, float(temp)))
    rows.sort()
    return rows, False


def classify(bounds, extreme: float, is_high: bool, integer_only: bool):
    low, high, unit = bounds
    if integer_only:
        return "UNKNOWN_PRECISION"
    if is_high:
        if high is not None and extreme >= high:
            return "IMPOSSIBLE_ABOVE"
        if low is not None and extreme >= low:
            return "STILL_POSSIBLE"
        return "STILL_POSSIBLE"
    if low is not None and extreme < low:
        return "IMPOSSIBLE_BELOW"
    return "STILL_POSSIBLE"


def main() -> int:
    ap = argparse.ArgumentParser(description="Monotonic bracket audit for today's temperature markets.")
    ap.add_argument("--tag", default="weather")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--date", default="", help="Local date YYYY-MM-DD; default is today.")
    ap.add_argument("--spend", type=float, default=20.0, help="USD size for the VWAP check.")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cache: dict[str, tuple] = {}
    rows = []
    try:
        events = events_by_tag(args.tag, args.limit)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for event in events:
        title = event.get("title") or ""
        low_title = title.lower()
        city = next((name for name in CITIES if name in low_title), None)
        if not city:
            continue
        icao, zone_name = CITIES[city]
        want = event_month_day(title)
        today = local_today(zone_name)
        target = (int(args.date[5:7]), int(args.date[8:10])) if args.date else (today.month, today.day)
        if want != target:
            continue  # a market for another date cannot be judged with today's data
        if icao not in cache:
            try:
                cache[icao] = observations(icao, zone_name)
            except FetchError as exc:
                print(f"# {city}: {exc}", file=sys.stderr)
                cache[icao] = (None, False)
        obs, integer_only = cache[icao]
        if not obs:
            continue
        is_high = "highest" in low_title
        temps = [t for _, t in obs]
        extreme = max(temps) if is_high else min(temps)
        extreme_at = max(obs, key=lambda r: r[1])[0] if is_high else min(obs, key=lambda r: r[1])[0]

        for market in event.get("markets") or []:
            if market.get("closed") or not market.get("active", True):
                continue
            bounds = parse_bracket(market.get("groupItemTitle") or "")
            if not bounds:
                continue
            low, high, unit = bounds
            note = classify(bounds, extreme, is_high, integer_only)
            tokens = [str(t) for t in (json.loads(market.get("clobTokenIds") or "[]"))]
            row = {
                "city": city, "station": icao, "date": str(target), "kind": "high" if is_high else "low",
                "bracket": market.get("groupItemTitle"), "observed_extreme_c": extreme,
                "extreme_at_local": extreme_at.strftime("%H:%M"),
                "state": note, "unit": unit,
                "fee_rate": as_float((market.get("feeSchedule") or {}).get("rate")),
                "volume24h": as_float(market.get("volume24hr")) or 0.0,
                "yes_book_checked": False,
            }
            if not integer_only and note.startswith("IMPOSSIBLE") and len(tokens) == 2:
                try:
                    bids, asks = yes_book(tokens[0])
                    no_asks = sorted(((1 - p, s) for p, s in bids), reverse=True)
                    price, size = vwap(no_asks, args.spend)
                    row.update(no_best_ask=no_asks[0][0] if no_asks else None,
                               no_vwap=price, no_depth=depth_usd(no_asks), yes_book_checked=True)
                except FetchError as exc:
                    row["book_error"] = str(exc)
            rows.append(row)

    impossible = [r for r in rows if str(r["state"]).startswith("IMPOSSIBLE")]
    open_rows = [r for r in rows if r["state"] == "STILL_POSSIBLE"]
    impossible.sort(key=lambda r: (r.get("no_vwap") or 1.0))

    if args.json:
        print(json.dumps({"impossible": impossible, "still_possible": open_rows}, ensure_ascii=False, indent=2))
        return 0

    print(f"TEMP BRACKET AUDIT  {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  (monotonic exclusion only)")
    print(f"brackets={len(rows)}  impossible={len(impossible)}  still_possible={len(open_rows)}  spend=${args.spend:.0f}")
    print("\n## 已被观测值排除的档（买它的 NO 才有意义；价格来自真实 NO 簿）")
    if not impossible:
        print("   无。要么极值还没走到，要么这些档的 NO 已经贵到 0.99 以上。")
    else:
        print(f"{'city':<13} {'k':<4} {'bracket':<16} {'obs':>6} {'state':<16} {'NO ask':>7} {'VWAP':>6} {'depth$':>7}")
        for r in impossible[: args.top]:
            ask = f"{r['no_best_ask']:.3f}" if r.get("no_best_ask") is not None else "-"
            vw = f"{r['no_vwap']:.3f}" if r.get("no_vwap") is not None else "-"
            print(f"{r['city']:<13} {r['kind']:<4} {str(r['bracket']):<16} {r['observed_extreme_c']:>6.0f} "
                  f"{r['state']:<16} {ask:>7} {vw:>6} {r.get('no_depth') or 0:>7.0f}")

    print("\n## 仍在范围内的档（不能判死，需要预报或等收盘）")
    for r in open_rows[:8]:
        print(f"   {r['city']:<13} {r['kind']:<4} {str(r['bracket']):<16} obs={r['observed_extreme_c']:.0f} "
              f"at {r['extreme_at_local']} local")

    print("\n提醒：这只是「哪些档已经不可能」，不是「哪个档会赢」。当天结束前最高温仍可能上升，"
          "\n站点/精度/修订规则必须按合约原文核对；香港公开接口只给整数，无法区分相邻档。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
