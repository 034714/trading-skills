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


def check_url(url: str) -> str:
    """Scheme must be http(s); reject loopback, private, reserved, link-local hosts."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        raise FetchError(f"non-http(s) scheme: {p.scheme}")
    host = p.hostname or ""
    if not host or host in ("localhost", "0.0.0.0") or host.endswith(".local"):
        raise FetchError(f"rejected host: {host}")
    import ipaddress
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local:
            raise FetchError(f"rejected address: {host}")
    except ValueError:
        pass  # hostname, not an IP — fine
    return url


def http_json(url: str, timeout: float = 35.0, attempts: int = 3):
    check_url(url)
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


def full_fill(levels, *, spend_usd=None, shares=None):
    """Buy asks by budget OR share count; partial liquidity never yields a full VWAP.

    The legacy vwap() keeps its tuple contract for other scripts. Callers making
    executable-size claims must use this helper and inspect complete.
    """
    if (spend_usd is None) == (shares is None):
        raise ValueError("specify exactly one of spend_usd or shares")
    target = spend_usd if spend_usd is not None else shares
    if not isinstance(target, (int, float)) or not math.isfinite(target) or target <= 0:
        raise ValueError("fill size must be finite and positive")
    clean = []
    for price, size in levels:
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (price, size)):
            raise ValueError("invalid book level")
        if not 0 < price <= 1 or size < 0:
            raise ValueError("invalid book price or size")
        clean.append((price, size))
    cost = got = 0.0
    for price, size in sorted(clean):
        remain = (target - cost) / price if spend_usd is not None else target - got
        take = min(size, max(0.0, remain))
        cost += take * price
        got += take
        if (cost if spend_usd is not None else got) >= target - 1e-9:
            break
    complete = (cost if spend_usd is not None else got) >= target - 1e-9
    average = cost / got if got else None
    return {"complete": complete, "cost": cost, "shares": got,
            "vwap": average if complete else None, "partial_vwap": average,
            "requested_spend": spend_usd, "requested_shares": shares}


def passive_limit(best_ask, discount_ticks, tick_size, best_bid=None):
    """A passive bid scenario below the best ask; never an available ask or a fill.

    best_bid is accepted for context only: bidding slightly above it to improve
    the queue is still passive as long as the price stays below best_ask.
    """
    from decimal import Decimal, ROUND_FLOOR
    if not isinstance(discount_ticks, int) or discount_ticks < 1:
        raise ValueError("discount must be a positive number of ticks")
    if not math.isfinite(tick_size) or not 0 < tick_size < 1:
        raise ValueError("tick size must be between zero and one")
    if best_ask is None:
        return None
    ask, tick = Decimal(str(best_ask)), Decimal(str(tick_size))
    price = ((ask / tick).to_integral_value(rounding=ROUND_FLOOR) - discount_ticks) * tick
    return float(price) if 0 < price < ask else None


def analyze_books(yes_bids, yes_asks, no_bids, no_asks, shares,
                  *, fee_per_share=None, fees_verified=False, fee_rate=None,
                  discount_ticks=0, tick_size=None):
    """Descriptive liquidity analysis of TWO real token books, same share count.

    Ratios are USD liquidity within 2c of each side's best quote: they describe
    orders, not traders, conviction or outcome probability. A YES+NO pair is
    only meaningful with equal shares bought from both ASK ladders; a partially
    filled leg is not a locked pair. Fees are a user scenario unless explicitly
    verified, and even verified arithmetic still depends on execution/settlement.
    """
    for fee in (fee_per_share, fee_rate):
        if fee is not None and (not math.isfinite(fee) or fee < 0):
            raise ValueError("fees must be finite and nonnegative")
    if not isinstance(shares, (int, float)) or not math.isfinite(shares) or shares <= 0:
        raise ValueError("shares must be finite and positive")
    yb, ya = sorted(yes_bids, reverse=True), sorted(yes_asks)
    nb, na = sorted(no_bids, reverse=True), sorted(no_asks)
    yf, nf = full_fill(ya, shares=shares), full_fill(na, shares=shares)
    out = {"yes_fill": yf, "no_fill": nf,
           "yes_vwap": yf["vwap"], "no_vwap": nf["vwap"],
           "yes_bid": yb[0][0] if yb else None, "yes_ask": ya[0][0] if ya else None,
           "no_bid": nb[0][0] if nb else None, "no_ask": na[0][0] if na else None,
           "yes_depth": depth_usd(ya), "no_depth": depth_usd(na),
           "yes_bid_depth": depth_usd(yb), "no_bid_depth": depth_usd(nb),
           "liquidity_basis": "各侧最优报价2美分内美元深度；是挂单量，不是人数、信心或概率",
           "fee_status": "已核实每份费用" if fees_verified and fee_per_share is not None else
                         "用户费用情景（未核实）" if fee_per_share is not None or fee_rate is not None else "费用未知"}
    for side, fill in (("yes", yf), ("no", nf)):
        if fee_per_share is not None:
            out[f"{side}_fee"] = fee_per_share
        elif fee_rate is not None and fill["complete"]:
            # Fee formula varies per market; integrate each price level.
            ladder = ya if side == "yes" else na
            remaining, total = shares, 0.0
            for price, size in ladder:
                take = min(size, remaining)
                total += take * withdraw_fee(price, fee_rate)
                remaining -= take
                if remaining <= 1e-9:
                    break
            out[f"{side}_fee"] = total / shares
        else:
            out[f"{side}_fee"] = None
        if discount_ticks:
            out[f"{side}_limit"] = (passive_limit(out[f"{side}_ask"], discount_ticks,
                                                   tick_size, out[f"{side}_bid"])
                                    if tick_size is not None else None)
    ask_total = out["yes_depth"] + out["no_depth"]
    bid_total = out["yes_bid_depth"] + out["no_bid_depth"]
    out["ask_no_share"] = out["no_depth"] / ask_total if ask_total else None
    out["bid_no_share"] = out["no_bid_depth"] / bid_total if bid_total else None
    complete = bool(yf["complete"] and nf["complete"])
    gross = (shares - yf["cost"] - nf["cost"]) if complete else None
    net = (gross - shares * (out["yes_fee"] + out["no_fee"])
           if complete and fees_verified and out["yes_fee"] is not None
           and out["no_fee"] is not None else None)
    scenario = (gross - shares * (out["yes_fee"] + out["no_fee"])
                if complete and out["yes_fee"] is not None and out["no_fee"] is not None
                else None)
    out["pair"] = {"shares_each": shares, "complete": complete,
                   "yes_fill": yf, "no_fill": nf,
                   "gross_profit": gross, "net_profit": net,
                   "scenario_net_profit": scenario,
                   "fees_verified": fees_verified and fee_per_share is not None,
                   "status": ("深度不足，两腿不能等份成交" if not complete else
                              "已核实费用的条件净价差" if net is not None else "仅毛价差观察"),
                   "warning": "等份双侧ASK快照；非原子成交，需复核结算一致性、费用及撤单风险"}
    return out


def markdown_cell(value):
    """Escape text so an external market title cannot break a Markdown table."""
    return str(value if value is not None else "-").replace("|", "&#124;").replace("\r", " ").replace("\n", " ")


def depth_usd(levels, band: float = 0.02):
    """USD available within `band` of the best level."""
    if not levels:
        return 0.0
    best = levels[0][0]
    return sum(p * s for p, s in levels if abs(p - best) <= band)
