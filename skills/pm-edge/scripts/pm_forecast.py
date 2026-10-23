#!/usr/bin/env python3
"""Model-based probability for a future temperature bracket. NOT a win-rate estimate.

Puts ECMWF/GFS/ICON/GEM (and the NWS official gridpoint for US cities) into a
normal distribution and compares that with the market ask. The spread used as
sigma is model disagreement, which is a crude uncertainty proxy - it is not a
validated forecast-error model, and this script cannot prove edge on its own.
"""
from __future__ import annotations

import argparse
import json
import math
import urllib.parse

from pm_common import FetchError, event_month_day, events_by_tag, http_json, norm_cdf, parse_bracket

CITIES = {
    "tokyo": (35.55, 139.78, "Asia/Tokyo", "RJTT"), "seoul": (37.46, 126.44, "Asia/Seoul", "RKSI"),
    "busan": (35.18, 128.94, "Asia/Seoul", "RKPK"), "taipei": (25.08, 121.23, "Asia/Taipei", "RCTP"),
    "shanghai": (31.14, 121.81, "Asia/Shanghai", "ZSPD"), "beijing": (40.08, 116.58, "Asia/Shanghai", "ZBAA"),
    "shenzhen": (22.64, 113.81, "Asia/Shanghai", "ZGSZ"), "guangzhou": (23.39, 113.30, "Asia/Shanghai", "ZGGG"),
    "singapore": (1.35, 103.99, "Asia/Singapore", "WSSS"), "hong kong": (22.30, 114.17, "Asia/Hong_Kong", "HKO"),
    "istanbul": (41.26, 28.74, "Europe/Istanbul", "LTFM"), "london": (51.50, 0.05, "Europe/London", "EGLC"),
    "paris": (49.01, 2.55, "Europe/Paris", "LFPG"), "munich": (48.35, 11.79, "Europe/Berlin", "EDDM"),
    "milan": (45.63, 8.72, "Europe/Rome", "LIMC"), "helsinki": (60.32, 24.96, "Europe/Helsinki", "EFHK"),
    "warsaw": (52.17, 20.97, "Europe/Warsaw", "EPWA"), "ankara": (40.13, 32.99, "Europe/Istanbul", "LTAC"),
    "nyc": (40.78, -73.87, "America/New_York", "KLGA"), "los angeles": (33.94, -118.41, "America/Los_Angeles", "KLAX"),
    "chicago": (41.98, -87.90, "America/Chicago", "KORD"), "miami": (25.79, -80.29, "America/New_York", "KMIA"),
    "dallas": (32.90, -97.04, "America/Chicago", "KDFW"), "houston": (29.98, -95.36, "America/Chicago", "KIAH"),
    "atlanta": (33.64, -84.43, "America/New_York", "KATL"), "san francisco": (37.62, -122.38, "America/Los_Angeles", "KSFO"),
    "boston": (42.36, -71.01, "America/New_York", "KBOS"), "denver": (39.86, -104.67, "America/Denver", "KDEN"),
}
MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_seamless")


def model_values(lat: float, lon: float, zone: str, date_str: str, variable: str):
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "daily": variable,
        "timezone": zone, "forecast_days": 5, "models": ",".join(MODELS),
    })
    data = http_json(f"https://api.open-meteo.com/v1/forecast?{query}")
    daily = data.get("daily") or {}
    if date_str not in (daily.get("time") or []):
        return {}
    index = daily["time"].index(date_str)
    out = {}
    for model in MODELS:
        series = daily.get(f"{variable}_{model}")
        if series and index < len(series) and series[index] is not None:
            out[model] = float(series[index])
    return out


def nws_high(lat: float, lon: float, date_str: str):
    points = http_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
    url = (points.get("properties") or {}).get("forecast")
    if not url:
        return None
    periods = (http_json(url).get("properties") or {}).get("periods") or []
    for period in periods:
        if str(period.get("startTime") or "")[:10] == date_str and period.get("isDaytime"):
            value = float(period["temperature"])
            return (value - 32) * 5 / 9 if (period.get("temperatureUnit") or "F").upper() == "F" else value
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Model probability for a future temperature bracket.")
    ap.add_argument("--date", required=True, help="Local date YYYY-MM-DD of the market.")
    ap.add_argument("--tag", default="weather")
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--sigma", type=float, default=None, help="Override sigma in Celsius.")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        events = events_by_tag(args.tag, args.limit)
    except FetchError as exc:
        print(f"error: {exc}")
        return 2

    want = (int(args.date[5:7]), int(args.date[8:10]))
    rows = []
    cache: dict[str, dict] = {}
    for event in events:
        title = event.get("title") or ""
        low = title.lower()
        if event_month_day(title) != want:
            continue
        city = next((name for name in CITIES if name in low), None)
        if not city:
            continue
        lat, lon, zone, icao = CITIES[city]
        is_high = "highest" in low
        key = f"{city}:{'max' if is_high else 'min'}"
        if key not in cache:
            try:
                values = model_values(lat, lon, zone, args.date, "temperature_2m_max" if is_high else "temperature_2m_min")
            except FetchError:
                values = {}
            official = None
            if is_high and zone.startswith("America/"):
                try:
                    official = nws_high(lat, lon, args.date)
                except FetchError:
                    official = None
            cache[key] = {"values": values, "nws": official}
        entry = cache[key]
        values = entry["values"]
        if not values:
            continue
        mean = sum(values.values()) / len(values)
        spread = max(values.values()) - min(values.values())
        sigma = args.sigma or max(1.2, spread * 0.8)
        centre = entry["nws"] if entry["nws"] is not None and is_high else mean

        for market in event.get("markets") or []:
            if market.get("closed") or not market.get("active", True):
                continue
            bounds = parse_bracket(market.get("groupItemTitle") or "")
            if not bounds:
                continue
            low_b, high_b, unit = bounds
            c = centre * 9 / 5 + 32 if unit == "F" else centre
            s = sigma * 9 / 5 if unit == "F" else sigma
            p_hi = 1.0 if high_b is None else norm_cdf((high_b - c) / s)
            p_lo = 0.0 if low_b is None else norm_cdf((low_b - c) / s)
            ask = market.get("bestAsk")
            try:
                ask = float(ask)
            except (TypeError, ValueError):
                continue
            rows.append({
                "city": city, "date": args.date, "kind": "high" if is_high else "low",
                "bracket": market.get("groupItemTitle"), "centre": round(centre, 1), "unit": unit,
                "models": {k: round(v, 1) for k, v in values.items()},
                "nws_used": entry["nws"] is not None and is_high,
                "spread": round(spread, 1), "sigma": round(sigma, 2),
                "p_model": max(0.0, p_hi - p_lo), "ask": ask,
                "diff": max(0.0, p_hi - p_lo) - ask,
            })

    rows.sort(key=lambda r: -abs(r["diff"]))
    if args.json:
        print(json.dumps(rows[: args.top], ensure_ascii=False, indent=2))
        return 0

    print(f"MODEL PROBABILITY  {args.date}  brackets={len(rows)}")
    print("这列 p 是模型正态近似，不是校准胜率，也不是「真实概率」。")
    print(f"{'city':<13} {'k':<4} {'bracket':<16} {'centre':>7} {'spread':>6} {'σ':>4} {'p_model':>7} {'ask':>6} {'diff':>6}")
    for r in rows[: args.top]:
        print(f"{r['city']:<13} {r['kind']:<4} {str(r['bracket']):<16} {r['centre']:>7.1f} "
              f"{r['spread']:>6.1f} {r['sigma']:>4.1f} {r['p_model']:>7.3f} {r['ask']:>6.3f} "
              f"{r['diff'] * 100:>+5.1f}pp")
    print("\n用法：只把 diff 当作「值得打开规则读一遍」的线索。模型分歧当 σ 是粗代理，"
          "\n四个模型常常高度相关；美国城市用 NWS 官方网格点，其余为原始模式输出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
