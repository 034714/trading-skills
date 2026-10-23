#!/usr/bin/env python3
"""Shared helpers for the pm-edge scripts. Read-only, standard library only."""
from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0 Safari/537.36"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"), start=1)}


class FetchError(RuntimeError):
    pass


def http_json(url: str, timeout: float = 35.0, attempts: int = 3):
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503) and attempt + 1 < attempts:
                last = FetchError(f"HTTP {exc.code} for {url}")
                time.sleep(1.5 * (attempt + 1))
                continue
            raise FetchError(f"HTTP {exc.code} for {url}") from exc
        except (urllib.error.URLError, OSError) as exc:
            last = FetchError(f"network error for {url}: {exc}")
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError(f"invalid JSON from {url}: {exc}") from exc
    raise last or FetchError(f"failed to fetch {url}")


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
        except json.JSONDecodeError:
            return []
        return out if isinstance(out, list) else []
    return []


def tz(name: str) -> ZoneInfo:
    """Local timezone with an explicit failure instead of a silent UTC fallback."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise FetchError(
            f"timezone data unavailable for {name!r}; install tzdata, refusing to assume UTC"
        ) from exc


def local_today(tz_name: str):
    return datetime.now(timezone.utc).astimezone(tz(tz_name))


def event_month_day(title: str):
    """(month, day) parsed from a title like '... on October 23?', else None."""
    m = re.search(r"(january|february|march|april|may|june|july|august|september|"
                  r"october|november|december)\s+(\d{1,2})", (title or "").lower())
    if not m:
        return None
    return (MONTHS[m.group(1)], int(m.group(2)))


def parse_bracket(label: str):
    """Return (low, high, unit). None bounds mean open-ended. unit is 'C' or 'F'."""
    text = (label or "").strip().replace("°", "")
    if not text:
        return None
    unit = "F" if re.search(r"\d\s*F\b", text) else "C"
    if "or below" in text:
        num = re.search(r"(-?\d+)", text)
        return (None, float(num.group(1)) + 1, unit) if num else None
    if "or higher" in text or "or above" in text:
        num = re.search(r"(-?\d+)", text)
        return (float(num.group(1)), None, unit) if num else None
    pair = re.match(r"^(-?\d+)\s*-\s*(-?\d+)\s*([CF])?$", text)
    if pair:
        return (float(pair.group(1)), float(pair.group(2)) + 1.0, pair.group(3) or "F")
    single = re.match(r"^(-?\d+)\s*([CF])?$", text)
    if single:
        return (float(single.group(1)), float(single.group(1)) + 1.0, single.group(2) or "C")
    return None


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def to_celsius(value: float, unit: str) -> float:
    return (value - 32.0) * 5.0 / 9.0 if unit == "F" else value


def withdraw_fee(price: float, fee_rate):
    """Fee in probability units. Unknown fee stays None - never default to zero."""
    if fee_rate is None:
        return None
    return fee_rate * price * (1.0 - price)


def events_by_tag(tag: str, limit: int = 40):
    url = (f"{GAMMA}/events?active=true&closed=false&limit={limit}"
           f"&order=volume24hr&ascending=false&tag_slug={urllib.parse.quote(tag)}")
    data = http_json(url)
    return data if isinstance(data, list) else []


def yes_book(token_id: str):
    """Real order book. Never substitute midpoints or last prices for executable quotes."""
    data = http_json(f"{CLOB}/book?token_id={urllib.parse.quote(token_id)}")
    bids = sorted(((float(b["price"]), float(b["size"])) for b in data.get("bids") or []), reverse=True)
    asks = sorted(((float(a["price"]), float(a["size"])) for a in data.get("asks") or []))
    return bids, asks


def vwap(levels, spend_usd: float):
    """Average fill price for spending spend_usd into an ask ladder."""
    cost = got = 0.0
    for price, size in levels:
        take = min(size, (spend_usd - cost) / price) if price > 0 else 0.0
        if take <= 0:
            break
        cost += take * price
        got += take
        if cost >= spend_usd - 1e-9:
            break
    return (cost / got, got) if got > 0 else (None, 0.0)


def depth_usd(levels, band: float = 0.02):
    """USD available within `band` of the best level."""
    if not levels:
        return 0.0
    best = levels[0][0]
    return sum(p * s for p, s in levels if abs(p - best) <= band)
