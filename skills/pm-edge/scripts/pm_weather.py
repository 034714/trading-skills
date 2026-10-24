#!/usr/bin/env python3
"""One-shot weather-market scanner for Polymarket daily temperature brackets.

Facts it encodes (all read out of the market rules, not assumed):
  * qualifying value = daily max/min of *routine* METAR reports in the station's
    local day; SPECI is excluded; US 5-minute ASOS interludes (MADISHF) are not
    routine METARs and are excluded because the ladder settles on the METAR feed
  * HK markets settle on the Hong Kong Observatory Daily Extract (0.1 C), so they
    get the HKO official 9-day forecast as centre plus a grid->Observatory bias
  * bracket mapping rounds the qualifying value half-up to whole degrees

Probabilities: for each of ECMWF/GFS/ICON take the target-day ensemble members,
shift by that model's trailing bias (obs - previous_day1), and read off the
fraction of members inside the bracket. The reported p range is the min and max
over 3 models x {raw, bias-shifted}, i.e. the least and most favourable reading.

Every NO row also carries a drift check: the worst P(hit) if all members move
+/-0.5/1/1.5 deg toward the bracket. A NO whose edge evaporates under a 1 deg
forecast miss is "fragile" and is filtered out by default (--risky shows it) --
adjacent-to-centre brackets are where NO tickets go to die.

`--settle YYYY-MM-DD` grades a finished date instead of scanning: actuals from
IEM/HKO, the winning bracket, and the cached model centre's miss.

Data pulls are batched per timezone group (Open-Meteo multi-location) and per
6-station chunk (IEM), which is what makes a cold scan fast.

Read-only. Prints a table; nothing is ordered.
"""
from __future__ import annotations

import argparse
import csv
import io
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from statistics import mean, median, pstdev
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_common import UA, GAMMA, CLOB, KALSHI, http_json, as_float

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(CACHE, exist_ok=True)
LOCK = threading.Lock()
# Open-Meteo allows 600 calls/min but 429s on bursts; batching cut the per-scan
# call count to ~30, so 4 in flight is comfortably inside the limit.
_OM_SLOT = threading.Semaphore(4)
# IEM's ASOS archive tolerates more parallelism and is the bulk of the download.
_IEM_SLOT = threading.Semaphore(6)

MODELS = ("ecmwf_ifs025", "gfs_seamless", "icon_seamless")
ENS_NAME = {"ecmwf_ifs025": "ecmwf_ifs025", "gfs_seamless": "gfs025", "icon_seamless": "icon_seamless"}
CITY_CN = {
    "LFPB": "巴黎", "EGLC": "伦敦", "RKPK": "釜山", "EHAM": "阿姆斯特丹", "RCSS": "台北",
    "VILK": "勒克瑙", "RJTT": "东京", "ZGGG": "广州", "RKSI": "首尔", "KLGA": "纽约",
    "RPLL": "马尼拉", "OEJN": "吉达", "ZUUU": "成都", "LTAC": "安卡拉", "KHOU": "休斯顿",
    "NZWN": "惠灵顿", "MMMX": "墨西哥城", "ZBAA": "北京", "KSEA": "西雅图", "CYYZ": "多伦多",
    "ZSPD": "上海", "WSSS": "新加坡", "LLBG": "特拉维夫", "KDAL": "达拉斯", "KMIA": "迈阿密",
    "KLAX": "洛杉矶", "EPWA": "华沙", "EDDM": "慕尼黑", "SBGR": "圣保罗", "ZUCK": "重庆",
    "KAUS": "奥斯汀", "KATL": "亚特兰大", "KDEN": "丹佛", "KORD": "芝加哥", "KSFO": "旧金山",
    "EHAM ": "阿姆斯特丹", "VHHH": "香港", "HKO": "香港", "VILK ": "勒克瑙",
    "Moscow": "莫斯科", "UUWW": "莫斯科", "UUEE": "莫斯科", "HKJK": "内罗毕",
    "SBGL": "里约", "SCEL": "圣地亚哥", "SPJC": "利马", "SKBO": "波哥大",
    "EDDF": "法兰克福", "LSZH": "苏黎世", "LOWW": "维也纳", "LEMD": "马德里",
    "LIRF": "罗马", "EBBR": "布鲁塞尔", "EKCH": "哥本哈根", "ENGM": "奥斯陆",
    "ESSA": "斯德哥尔摩", "EFHK": "赫尔辛基", "EPWA ": "华沙", "LKPR": "布拉格",
    "LHBP": "布达佩斯", "LROP": "布加勒斯特", "LTFM": "伊斯坦布尔", "LTAC ": "安卡拉",
    "OMDB": "迪拜", "OTHH": "多哈", "OERK": "利雅得", "OEJN ": "吉达", "OIII": "德黑兰",
    "OPKC": "卡拉奇", "VIDP": "德里", "VABB": "孟买", "VILK": "勒克瑙", "VCBI": "科伦坡",
    "VTBS": "曼谷", "VVTS": "胡志明市", "WIII": "雅加达", "RPLL ": "马尼拉",
    "RCTP": "台北", "RCSS ": "台北", "ZGSZ": "深圳", "ZHHH": "武汉", "ZSQD": "青岛",
    "ZUUU ": "成都", "ZUCK ": "重庆", "ZSPD ": "上海", "ZBAA ": "北京", "ZGGG ": "广州",
    "RJTT ": "东京", "RJAA": "东京", "RKSI ": "首尔", "RKPK ": "釜山",
    "CYYZ ": "多伦多", "CYVR": "温哥华", "CYMX": "蒙特利尔", "CYYC": "卡尔加里",
    "KLGA ": "纽约", "KJFK": "纽约", "KLAX ": "洛杉矶", "KSEA ": "西雅图",
    "KDAL ": "达拉斯", "KDFW": "达拉斯", "KHOU ": "休斯顿", "KIAH": "休斯顿",
    "KMIA ": "迈阿密", "KAUS ": "奥斯汀", "KATL ": "亚特兰大", "KDEN ": "丹佛",
    "KORD ": "芝加哥", "KSFO ": "旧金山", "KBOS": "波士顿", "KPHL": "费城",
    "MMMX ": "墨西哥城", "SBGR ": "圣保罗", "SAEZ": "布宜诺斯艾利斯", "SCL": "圣地亚哥",
    "NZWN ": "惠灵顿", "NZAA": "奥克兰", "YSSY": "悉尼", "YMML": "墨尔本",
    "FACT": "开普敦", "FAOR": "约翰内斯堡",
}


# ---------------------------------------------------------------- helpers

def _cache_path(key: str, ext: str) -> str:
    """Hash-based cache filename; hex-only so traversal is impossible by construction."""
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return os.path.join(CACHE, digest + ext)


def _validate_url(url: str) -> str:
    """Scheme must be http(s); reject loopback, private, reserved, link-local hosts."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"non-http(s) scheme: {p.scheme}")
    host = p.hostname or ""
    if not host or host in ("localhost", "0.0.0.0") or host.endswith(".local"):
        raise ValueError(f"rejected host: {host}")
    import ipaddress
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local:
            raise ValueError(f"rejected address: {host}")
    except ValueError:
        pass  # hostname, not an IP — fine
    return url


def cached(key: str, url: str, ttl_hours: float = 3.0):
    path = _cache_path(key, ".json")
    p = pathlib.Path(path)
    if p.exists():
        if (datetime.now().timestamp() - p.stat().st_mtime) / 3600 < ttl_hours:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    data = None
    for attempt in range(3):
        try:
            with _OM_SLOT:
                data = http_json(_validate_url(url))
            break
        except Exception as exc:
            if "429" in str(exc) and attempt < 2:
                time.sleep(2.0 * (attempt + 1))   # ensemble API bursts get throttled
                continue
            raise
    p.write_text(json.dumps(data), encoding="utf-8")
    return data


def cached_text(key: str, url: str, ttl_hours: float = 6.0, timeout: float = 120.0):
    path = _cache_path(key, ".txt")
    p = pathlib.Path(path)
    if p.exists():
        if (datetime.now().timestamp() - p.stat().st_mtime) / 3600 < ttl_hours:
            return p.read_text(encoding="utf-8")
    req = urllib.request.Request(_validate_url(url), headers={"User-Agent": UA})
    slot = _IEM_SLOT if "mesonet.agron" in url else _OM_SLOT
    text, last = None, None
    for attempt in range(3):          # proxies hiccup; a dead fetch should not kill a scan
        try:
            with slot:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    text = r.read().decode("utf-8", "replace")
            break
        except Exception as exc:
            last = exc
            time.sleep(0.8 * (attempt + 1))
    if text is None:
        raise last
    p.write_text(text, encoding="utf-8")
    return text


T_REMARK = re.compile(r"\bT([01])(\d{3})([01]\d{3})\b")
BODY_T = re.compile(r"(?<![\w/])(M?\d{2})/(M?\d{2})(?![\w/])")


def metar_temp(report: str):
    head, _, tail = report.partition(" RMK ")
    if tail:
        m = T_REMARK.search(tail)
        if m:
            v = int(m.group(2)) / 10.0
            return -v if m.group(1) == "1" else v
    m = BODY_T.search(head)
    if m:
        return float(m.group(1).replace("M", "-"))
    return None


def is_routine(report: str, us: bool) -> bool:
    tokens = report.split()
    if "SPECI" in tokens[:2]:
        return False
    if us:
        return ("AO1" in report.partition(" RMK ")[2]) or ("AO2" in report.partition(" RMK ")[2])
    return True


_OBS_MEMO: dict = {}


def observed_daily(station: str, zone: str, start: date, end: date):
    """Routine-METAR daily extremes in the station's local day (memoized; both max and min)."""
    key = (station, start, end)
    if key in _OBS_MEMO:
        return _OBS_MEMO[key]
    path = os.path.join(CACHE, f"asos_{station}_{start}_{end}.csv")
    p = pathlib.Path(path)
    if not p.exists():
        url = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
               f"station={station}&data=tmpc,metar"
               f"&year1={start.year}&month1={start.month}&day1={start.day}"
               f"&year2={end.year}&month2={end.month}&day2={end.day}"
               "&tz=UTC&format=onlycomma&latlon=no&missing=M&trace=T&direct=no")
        text = cached_text(f"asosraw_{station}_{start}_{end}", url, ttl_hours=72)
        p.write_text(text, encoding="utf-8")
    text = p.read_text(encoding="utf-8")
    tz = ZoneInfo(zone)
    us = station.startswith("K")
    hi, lo, cnt = {}, {}, {}
    seen = set()
    for row in csv.DictReader(io.StringIO(text)):
        report = (row.get("metar") or "").strip()
        stamp = (row.get("valid") or "").strip()
        if not report or not stamp or report in seen:
            continue
        seen.add(report)
        if not is_routine(report, us):
            continue
        t = metar_temp(report)
        if t is None:
            continue
        try:
            utc = datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("UTC"))
        except ValueError:
            continue
        d = utc.astimezone(tz).date()
        hi[d] = max(hi.get(d, -99.0), t)
        lo[d] = min(lo.get(d, 99.0), t)
        cnt[d] = cnt.get(d, 0) + 1
    need = 16 if us else 18
    keep = {d for d, n in cnt.items() if n >= need}
    result = ({d: v for d, v in hi.items() if d in keep},
              {d: v for d, v in lo.items() if d in keep})
    with LOCK:
        _OBS_MEMO[key] = result
    return result


_PREV_MEMO: dict = {}


def prev_daily(station: str, zone: str, model: str, start: date, end: date, is_max: bool):
    """Genuine 1-day-ahead forecast (previous_day1) aggregated to a local daily extreme.

    One HTTP call per (station, model, window); both the daily max and min are derived
    from it, because high and low markets for the same station share the same series.
    """
    key = (station, model, start, end)
    if key not in _PREV_MEMO:
        q = urllib.parse.urlencode({
            "latitude": STATIONS[station]["lat"], "longitude": STATIONS[station]["lon"],
            "hourly": "temperature_2m_previous_day1",
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "timezone": zone, "models": model,
        })
        data = cached(f"prev1_{station}_{model}_{start}_{end}",
                      f"https://previous-runs-api.open-meteo.com/v1/forecast?{q}")
        h = data.get("hourly") or {}
        hi, lo = {}, {}
        for t, v in zip(h.get("time") or [], h.get("temperature_2m_previous_day1") or []):
            if v is None:
                continue
            d = datetime.fromisoformat(t).date()
            hi[d] = max(hi.get(d, -999.0), float(v))
            lo[d] = min(lo.get(d, 999.0), float(v))
        with LOCK:
            _PREV_MEMO[key] = (hi, lo)
    return _PREV_MEMO[key][0 if is_max else 1]


def members(station: str, zone: str, model: str, target: date, variable: str):
    q = urllib.parse.urlencode({
        "latitude": STATIONS[station]["lat"], "longitude": STATIONS[station]["lon"],
        "daily": variable, "start_date": target.isoformat(), "end_date": target.isoformat(),
        "timezone": zone, "models": ENS_NAME[model],
    })
    key = f"ens_{station}_{model}_{target}_{variable}"
    data = cached(key, f"https://ensemble-api.open-meteo.com/v1/ensemble?{q}")
    daily = data.get("daily") or {}
    return [float(v[0]) for k, v in daily.items()
            if k.startswith(variable) and v and v[0] is not None]


def deterministic(station: str, zone: str, model: str, target: date, variable: str):
    q = urllib.parse.urlencode({
        "latitude": STATIONS[station]["lat"], "longitude": STATIONS[station]["lon"],
        "daily": variable, "start_date": target.isoformat(), "end_date": target.isoformat(),
        "timezone": zone, "models": model,
    })
    key = f"det_{station}_{model}_{target}_{variable}"
    data = cached(key, f"https://api.open-meteo.com/v1/forecast?{q}")
    daily = data.get("daily") or {}
    val = (daily.get(f"{variable}_{model}") or daily.get(variable) or [None])[0]
    return float(val) if val is not None else None


def bounds(label: str):
    """Bracket bounds on the integer scale the market uses."""
    t = (label or "").replace("°", "").strip()
    m = re.search(r"(-?\d+)", t)
    if not m:
        return None
    if "or below" in t:
        return (None, float(m.group(1)))
    if "or higher" in t:
        return (float(m.group(1)), None)
    pair = re.match(r"^(-?\d+)\s*-\s*(-?\d+)\s*([CF])?$", t)
    if pair:
        return (float(pair.group(1)), float(pair.group(2)))
    one = re.match(r"^(-?\d+)\s*([CF])?$", t)
    return (float(one.group(1)), float(one.group(1)))


def hk_interval(label: str):
    """HK settles on a 0.1 C reading and the bracket CONTAINS it -> floor rule.

    '31°C' covers 31.0-31.9, '26°C or below' covers <= 26.9, '36°C or higher' >= 36.0.
    """
    bd = bounds(label)
    if not bd:
        return None
    lo, hi = bd
    if lo is None:
        return (None, hi + 1.0)
    if hi is None:
        return (lo, None)
    return (lo, hi + 1.0)


def frac_in(vals, lo, hi):
    if not vals:
        return None
    hits = 0
    for v in vals:
        r = math.floor(v + 0.5)
        if (lo is None or r >= lo) and (hi is None or r <= hi):
            hits += 1
    return hits / len(vals)


def book(token: str, attempts: int = 3):
    last = None
    for i in range(attempts):
        try:
            data = http_json(f"{CLOB}/book?token_id={urllib.parse.quote(token)}")
            asks = sorted((float(a["price"]), float(a["size"])) for a in data.get("asks") or [])
            bids = sorted(((float(b["price"]), float(b["size"])) for b in data.get("bids") or []),
                          reverse=True)
            return bids, asks
        except Exception as exc:      # the CLOB rate-limits; back off and retry
            last = exc
            import time
            time.sleep(0.6 * (i + 1))
    raise last


def vwap(levels, spend):
    cost = got = 0.0
    for p, s in levels:
        if p <= 0:
            break
        take = min(s, (spend - cost) / p)
        if take <= 0:
            break
        cost += take * p
        got += take
        if cost >= spend - 1e-9:
            break
    return (cost / got, got) if got > 0 else (None, 0.0)


def depth(levels, band=0.02):
    if not levels:
        return 0.0
    best = levels[0][0]
    return sum(p * s for p, s in levels if abs(p - best) <= band)


# ---------------------------------------------------------------- discovery

STATIONS: dict[str, dict] = {}
STATION_COORDS = {
    "LFPB": (48.967, 2.428, "Europe/Paris"), "EGLC": (51.505, 0.055, "Europe/London"),
    "RKPK": (35.179, 128.938, "Asia/Seoul"), "EHAM": (52.315, 4.79, "Europe/Amsterdam"),
    "RCSS": (25.069, 121.552, "Asia/Taipei"), "VILK": (26.761, 80.889, "Asia/Kolkata"),
    "RJTT": (35.553, 139.781, "Asia/Tokyo"), "ZGGG": (23.392, 113.307, "Asia/Shanghai"),
    "RKSI": (37.469, 126.451, "Asia/Seoul"), "KLGA": (40.779, -73.880, "America/New_York"),
    "RPLL": (14.507, 121.004, "Asia/Manila"), "OEJN": (21.685, 39.166, "Asia/Riyadh"),
    "ZUUU": (30.576, 103.95, "Asia/Shanghai"), "LTAC": (40.128, 32.995, "Europe/Istanbul"),
    "KHOU": (29.646, -95.282, "America/Chicago"), "NZWN": (-41.331, 174.806, "Pacific/Auckland"),
    "MMMX": (19.436, -99.072, "America/Mexico_City"), "ZBAA": (40.082, 116.603, "Asia/Shanghai"),
    "KSEA": (47.445, -122.314, "America/Los_Angeles"), "CYYZ": (43.679, -79.629, "America/Toronto"),
    "ZSPD": (31.146, 121.8, "Asia/Shanghai"), "WSSS": (1.368, 103.982, "Asia/Singapore"),
    "LLBG": (32.011, 34.887, "Asia/Jerusalem"), "KDAL": (32.838, -96.836, "America/Chicago"),
    "KMIA": (25.788, -80.317, "America/New_York"), "KLAX": (33.938, -118.387, "America/Los_Angeles"),
    "EPWA": (52.163, 20.961, "Europe/Warsaw"), "EDDM": (48.348, 11.813, "Europe/Berlin"),
    "SBGR": (-23.432, -46.469, "America/Sao_Paulo"), "ZUCK": (29.718, 106.639, "Asia/Shanghai"),
    "KAUS": (30.183, -97.681, "America/Chicago"), "KATL": (33.640, -84.427, "America/New_York"),
    "KDEN": (39.860, -104.667, "America/Denver"), "KORD": (41.978, -87.905, "America/Chicago"),
    "KSFO": (37.619, -122.375, "America/Los_Angeles"), "VHHH": (22.309, 113.922, "Asia/Hong_Kong"),
    "KJFK": (40.639, -73.779, "America/New_York"), "KDFW": (32.897, -97.022, "America/Chicago"),
    "KIAH": (29.984, -95.341, "America/Chicago"), "KBOS": (42.363, -71.006, "America/New_York"),
    "KPHL": (39.872, -75.241, "America/New_York"), "CYVR": (49.195, -123.184, "America/Vancouver"),
    "CYYC": (51.113, -114.020, "America/Edmonton"), "SAEZ": (-34.822, -58.536, "America/Argentina/Buenos_Aires"),
    "SCEL": (-33.393, -70.786, "America/Santiago"), "SKBO": (4.701, -74.147, "America/Bogota"),
    "SPJC": (-12.022, -77.114, "America/Lima"), "FACT": (-33.971, 18.602, "Africa/Johannesburg"),
    "FAOR": (-26.139, 28.246, "Africa/Johannesburg"), "OMDB": (25.253, 55.365, "Asia/Dubai"),
    "OTHH": (25.274, 51.608, "Asia/Qatar"), "OERK": (24.958, 46.698, "Asia/Riyadh"),
    "VIDP": (28.556, 77.100, "Asia/Kolkata"), "VABB": (19.089, 72.868, "Asia/Kolkata"),
    "OPKC": (24.906, 67.161, "Asia/Karachi"), "VCBI": (7.180, 79.884, "Asia/Colombo"),
    "VTBS": (13.690, 100.750, "Asia/Bangkok"), "WIII": (-6.125, 106.656, "Asia/Jakarta"),
    "YSSY": (-33.946, 151.177, "Australia/Sydney"), "YMML": (-37.673, 144.843, "Australia/Melbourne"),
    "NZAA": (-37.008, 174.792, "Pacific/Auckland"), "LTFM": (41.262, 28.742, "Europe/Istanbul"),
    "UUEE": (55.973, 37.415, "Europe/Moscow"), "LKPR": (50.101, 14.260, "Europe/Prague"),
    "LOWW": (48.110, 16.570, "Europe/Vienna"), "LSZH": (47.458, 8.548, "Europe/Zurich"),
    "EDDF": (50.033, 8.570, "Europe/Berlin"), "LIRF": (41.804, 12.251, "Europe/Rome"),
    "LEMD": (40.472, -3.561, "Europe/Madrid"), "EBBR": (50.901, 4.484, "Europe/Brussels"),
    "EKCH": (55.618, 12.656, "Europe/Copenhagen"), "ENGM": (60.194, 11.100, "Europe/Oslo"),
    "ESSA": (59.652, 17.919, "Europe/Stockholm"), "EFHK": (60.317, 24.963, "Europe/Helsinki"),
    "LHBP": (47.437, 19.256, "Europe/Budapest"), "LROP": (44.572, 26.102, "Europe/Bucharest"),
    "EGLL": (51.478, -0.461, "Europe/London"), "EGKK": (51.148, -0.190, "Europe/London"),
}
for code, (lat, lon, tzname) in STATION_COORDS.items():
    STATIONS[code] = {"lat": lat, "lon": lon, "tz": tzname}

HK_SLUG_HINT = "hong kong"


def city_name(code: str) -> str:
    return CITY_CN.get(code) or CITY_CN.get(code + " ") or code


def discover(target: date, closed: bool = False):
    """Weather events for the target local date (open by default, closed if asked)."""
    want = (target.month, target.day)
    base = ("closed=true&active=false&order=id" if closed
            else "active=true&closed=false&order=volume24hr")

    def page(off):
        return http_json(f"{GAMMA}/events?{base}&limit=100&offset={off}"
                         "&ascending=false&tag_slug=weather")

    with ThreadPoolExecutor(max_workers=6) as pool:
        pages = [p for p in pool.map(page, range(0, 600, 100)) if p]
    events, seen = [], set()
    for batch in pages:
        for e in batch:
            title = e.get("title") or ""
            m = re.search(r"(january|february|march|april|may|june|july|august|september|october|"
                          r"november|december)\s+(\d{1,2})", title.lower())
            if not m or (list(_MONTHS).index(m.group(1)) + 1, int(m.group(2))) != want:
                continue
            if "temperature" not in title.lower():
                continue
            if e.get("id") in seen:
                continue
            seen.add(e.get("id"))
            desc = e.get("description") or ""
            sm = re.search(r"reported at [^(]+\(([A-Z]{4})\)", desc)
            hk = ("Hong Kong Observatory" in desc) or ("hong kong" in title.lower())
            code = sm.group(1) if sm else ("HKO" if hk else None)
            if not code:
                continue
            if hk:
                code = "HKO"
            if code != "HKO" and code not in STATIONS:
                continue
            unit = "F" if "degrees Fahrenheit" in desc else "C"
            kind = "low" if "lowest" in title.lower() else "high"
            events.append({"id": e.get("id"), "title": title, "code": code, "hk": hk,
                           "unit": unit, "kind": kind, "vol": float(e.get("volume24hr") or 0),
                           "markets": e.get("markets") or []})
    return events


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")


# ---------------------------------------------------------------- Hong Kong

def hko_extract_month(year: int, month: int, kind: str):
    """Observatory Daily Extract via the website's own data file (updates with a
    1-2 day lag even for the current month, unlike the CLMMAXT opendata feed
    which stays empty until the month closes). Max temp is column 2, min col 4."""
    text = cached_text(f"hkoext_{kind}_{year}_{month}",
                       f"https://www.hko.gov.hk/cis/dailyExtract/dailyExtract_{year}{month:02d}.xml",
                       ttl_hours=6)
    out = {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return out
    col = 2 if kind == "max" else 4
    for block in (data.get("stn") or {}).get("data") or []:
        for row in block.get("dayData") or []:
            try:
                d = date(year, int(block.get("month", month)), int(row[0]))
                out[d] = float(row[col])
            except (ValueError, TypeError, IndexError):
                continue
    return out


def hko_month(kind: str, year: int, month: int):
    url = ("https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
           f"?dataType={kind}&year={year}&month={month}&rformat=csv&station=HKO")
    text = cached_text(f"hko_{kind}_{year}_{month}", url, ttl_hours=48)
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 4 or row[0].strip() != str(year):
            continue
        try:
            out[date(year, int(row[1]), int(row[2]))] = float(row[3])
        except (ValueError, IndexError):
            continue
    return out


def _snap_fnd(fc: dict):
    """Archive the day-ahead official max/min per target date. The accumulated
    file eventually yields a real HKO-forecast error sigma to replace the
    grid-residual proxy (HKO scores its own forecasts full marks within 1.5 C,
    so a ~1 C official miss is ordinary and must not be treated as 5-sigma)."""
    path = pathlib.Path(CACHE, "hko_fnd_history.jsonl")
    have = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                have.add((rec["issued"], rec["target"]))
            except Exception:
                pass
    issued = datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
    fresh = [{"issued": issued, "target": d.isoformat(), "max": mx, "min": mn}
             for d, (mx, mn) in fc.items() if (issued, d.isoformat()) not in have]
    if fresh:
        with LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                for rec in fresh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def hk_model(target: date):
    """HKO official 9-day forecast as centre; grid->Observatory bias for the spread."""
    lat, lon, zone = 22.302, 114.174, "Asia/Hong_Kong"
    fnd = cached("hko_fnd", "https://data.weather.gov.hk/weatherAPI/opendata/weather.php"
                            "?dataType=fnd&lang=en", ttl_hours=1)
    fc = {}
    for d in fnd.get("weatherForecast") or []:
        try:
            fc[datetime.strptime(d["forecastDate"], "%Y%m%d").date()] = (
                float(d["forecastMaxtemp"]["value"]), float(d["forecastMintemp"]["value"]))
        except (KeyError, ValueError):
            continue
    try:
        _snap_fnd(fc)
    except Exception:
        pass

    obs_max, obs_min = {}, {}
    for back in (0, 1, 2, 3):
        y, mth = (target.year, target.month - back) if target.month - back >= 1 else (target.year - 1, 12)
        # CLMMAXT is empty for the open month; the Daily Extract file covers it
        obs_max.update({d: v for d, v in hko_month("CLMMAXT", y, mth).items()})
        obs_min.update({d: v for d, v in hko_month("CLMMINT", y, mth).items()})
    for kind_key, acc in (("max", obs_max), ("min", obs_min)):
        acc.update({d: v for d, v in hko_extract_month(target.year, target.month, kind_key).items()
                    if d < target})

    out = {}
    for key, variable, obs in (("high", "temperature_2m_max", obs_max),
                               ("low", "temperature_2m_min", obs_min)):
        q = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon, "hourly": "temperature_2m_previous_day1",
            "start_date": (target - timedelta(days=100)).isoformat(),
            "end_date": (target - timedelta(days=1)).isoformat(),
            "timezone": zone, "models": "ecmwf_ifs025",
        })
        data = cached(f"hko_prev_{key}", f"https://previous-runs-api.open-meteo.com/v1/forecast?{q}")
        h = data.get("hourly") or {}
        fcday = {}
        for t, v in zip(h.get("time") or [], h.get("temperature_2m_previous_day1") or []):
            if v is None:
                continue
            d = datetime.fromisoformat(t).date()
            fcday[d] = (max(fcday.get(d, -999), float(v)) if key == "high"
                        else min(fcday.get(d, 999), float(v)))
        paired = sorted(set(obs) & set(fcday))
        bias = mean([obs[d] - fcday[d] for d in paired]) if paired else 0.0
        sigma = max(pstdev([obs[d] - fcday[d] for d in paired]), 0.7) if len(paired) > 5 else 1.2
        off = fc.get(target, (None, None))[0 if key == "high" else 1]
        if off is None:
            continue
        out[key] = {"official": off, "bias": bias, "sigma": sigma, "n": len(paired)}
    return out


# ---------------------------------------------------------------- pricing

def prev_all_models(station: str, zone: str, start: date, end: date):
    """One request for all three models' previous_day1 series (max and min daily)."""
    key = (station, start, end)
    if key in _PREV_ALL:
        return _PREV_ALL[key]
    pp = pathlib.Path(CACHE, f"prevparse_{station}_{start}_{end}.json")
    if pp.exists() and (time.time() - pp.stat().st_mtime) < 3600 * 3:
        try:
            raw = json.loads(pp.read_text(encoding="utf-8"))
            out = {}
            for model, v in raw.items():
                out[model] = {"max": {date.fromisoformat(d): x for d, x in v["max"].items()},
                              "min": {date.fromisoformat(d): x for d, x in v["min"].items()}}
            with LOCK:
                _PREV_ALL[key] = out
            return out
        except Exception:
            pass
    q = urllib.parse.urlencode({
        "latitude": STATIONS[station]["lat"], "longitude": STATIONS[station]["lon"],
        "hourly": "temperature_2m_previous_day1",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "timezone": zone, "models": ",".join(MODELS),
    })
    data = cached(f"prevall_{station}_{start}_{end}",
                  f"https://previous-runs-api.open-meteo.com/v1/forecast?{q}")
    h = data.get("hourly") or {}
    times = h.get("time") or []
    out = {}
    for model in MODELS:
        series = (h.get(f"temperature_2m_previous_day1_{model}")
                  or h.get("temperature_2m_previous_day1") or [])
        if not series:
            continue
        hi, lo = {}, {}
        for t, v in zip(times, series):
            if v is None:
                continue
            d = datetime.fromisoformat(t).date()
            hi[d] = max(hi.get(d, -999.0), float(v))
            lo[d] = min(lo.get(d, 999.0), float(v))
        out[model] = {"max": hi, "min": lo}
    with LOCK:
        _PREV_ALL[key] = out
    return out


_PREV_ALL: dict = {}


def members_all_models(station: str, zone: str, target: date, variable: str):
    """One request for all three models' ensemble members."""
    key = (station, target, variable)
    if key in _MEM_ALL:
        return _MEM_ALL[key]
    mp = pathlib.Path(CACHE, f"memparse_{station}_{target}_{variable}.json")
    if mp.exists() and (time.time() - mp.stat().st_mtime) < 3600 * 3:
        try:
            with LOCK:
                _MEM_ALL[key] = json.loads(mp.read_text(encoding="utf-8"))
            return _MEM_ALL[key]
        except Exception:
            pass
    q = urllib.parse.urlencode({
        "latitude": STATIONS[station]["lat"], "longitude": STATIONS[station]["lon"],
        "daily": variable, "start_date": target.isoformat(), "end_date": target.isoformat(),
        "timezone": zone, "models": "ecmwf_ifs025,gfs025,icon_seamless",
    })
    data = cached(f"memall_{station}_{target}_{variable}",
                  f"https://ensemble-api.open-meteo.com/v1/ensemble?{q}")
    daily = data.get("daily") or {}
    out = {m: [] for m in MODELS}
    # key suffixes differ by provider: ecmwf_ifs025_ensemble, ncep_gefs025,
    # icon_seamless_eps. Match on a substring, not the requested model name.
    tags = (("ecmwf_ifs025", "ecmwf"), ("gfs_seamless", "gefs"),
            ("icon_seamless", "icon"))
    for k, series in daily.items():
        if not k.startswith(variable) or not series or series[0] is None:
            continue
        for model, tag in tags:
            if tag in k:
                out[model].append(float(series[0]))
                break
    with LOCK:
        _MEM_ALL[key] = out
    return out


_MEM_ALL: dict = {}

# Open-Meteo accepts comma-separated coordinates and returns one dict per
# location; the ensemble API additionally takes both daily variables at once.
ENS_MODELS = "ecmwf_ifs025,gfs025,icon_seamless"
MEMBER_TAGS = (("ecmwf_ifs025", "ecmwf"), ("gfs_seamless", "gefs"), ("icon_seamless", "icon"))


def _split_members(daily: dict, variable: str) -> dict:
    per = {m: [] for m in MODELS}
    for k, series in (daily or {}).items():
        if not k.startswith(variable) or not series or series[0] is None:
            continue
        for model, tag in MEMBER_TAGS:
            if tag in k:
                per[model].append(float(series[0]))
                break
    return per


def prewarm(codes, target: date, obs_start: date, obs_end: date, workers=4):
    """Pull everything the pricing pass needs in bulk, before the per-event pool.

    previous_day1 and the ensembles go out as one multi-location request per
    timezone group (the API takes a single timezone per call); IEM takes
    comma-joined station lists in chunks of six. Everything lands in the memo
    dicts and per-station cache files, so station_stats never touches the network
    on a warm run.
    """
    codes = [c for c in dict.fromkeys(codes) if c != "HKO" and c in STATIONS]
    groups: dict[str, list] = {}
    for c in codes:
        groups.setdefault(STATIONS[c]["tz"], []).append(c)

    def prev_one(zone, sts):
        q = urllib.parse.urlencode({
            "latitude": ",".join(str(STATIONS[c]["lat"]) for c in sts),
            "longitude": ",".join(str(STATIONS[c]["lon"]) for c in sts),
            "hourly": "temperature_2m_previous_day1",
            "start_date": obs_start.isoformat(), "end_date": obs_end.isoformat(),
            "timezone": zone, "models": ",".join(MODELS),
        })
        data = cached(f"prevbatch_{zone}_{obs_start}_{obs_end}",
                      f"https://previous-runs-api.open-meteo.com/v1/forecast?{q}")
        for st, item in zip(sts, data if isinstance(data, list) else [data]):
            h = item.get("hourly") or {}
            times = h.get("time") or []
            per = {}
            for model in MODELS:
                series = h.get(f"temperature_2m_previous_day1_{model}")
                if not series:
                    continue
                hi, lo = {}, {}
                for t, v in zip(times, series):
                    if v is None:
                        continue
                    d = datetime.fromisoformat(t).date()
                    hi[d] = max(hi.get(d, -999.0), float(v))
                    lo[d] = min(lo.get(d, 999.0), float(v))
                per[model] = {"max": hi, "min": lo}
            if not per:
                continue
            with LOCK:
                _PREV_ALL[(st, obs_start, obs_end)] = per
            pathlib.Path(CACHE, f"prevparse_{st}_{obs_start}_{obs_end}.json").write_text(
                json.dumps({m: {"max": {d.isoformat(): x for d, x in v["max"].items()},
                                "min": {d.isoformat(): x for d, x in v["min"].items()}}
                            for m, v in per.items()}), encoding="utf-8")

    def ens_one(zone, sts):
        q = urllib.parse.urlencode({
            "latitude": ",".join(str(STATIONS[c]["lat"]) for c in sts),
            "longitude": ",".join(str(STATIONS[c]["lon"]) for c in sts),
            "daily": "temperature_2m_max,temperature_2m_min",
            "start_date": target.isoformat(), "end_date": target.isoformat(),
            "timezone": zone, "models": ENS_MODELS,
        })
        data = cached(f"ensbatch_{zone}_{target}",
                      f"https://ensemble-api.open-meteo.com/v1/ensemble?{q}")
        for st, item in zip(sts, data if isinstance(data, list) else [data]):
            daily = item.get("daily") or {}
            for variable in ("temperature_2m_max", "temperature_2m_min"):
                per = _split_members(daily, variable)
                if not any(per.values()):
                    continue
                with LOCK:
                    _MEM_ALL[(st, target, variable)] = per
                pathlib.Path(CACHE, f"memparse_{st}_{target}_{variable}.json").write_text(
                    json.dumps(per), encoding="utf-8")

    def iem_one(sts):
        for i in range(0, len(sts), 6):
            chunk = sts[i:i + 6]
            url = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
                   f"station={','.join(chunk)}&data=tmpc,metar"
                   f"&year1={obs_start.year}&month1={obs_start.month}&day1={obs_start.day}"
                   f"&year2={obs_end.year}&month2={obs_end.month}&day2={obs_end.day}"
                   "&tz=UTC&format=onlycomma&latlon=no&missing=M&trace=T&direct=no")
            text = cached_text(f"iembatch_{'-'.join(chunk)}_{obs_start}_{obs_end}", url,
                               ttl_hours=72)
            rows = list(csv.reader(io.StringIO(text)))
            if not rows or "station" not in rows[0]:
                return  # unexpected shape; per-station fallback will fetch singly
            si = rows[0].index("station")
            out = {c: [",".join(rows[0])] for c in chunk}
            for r in rows[1:]:
                st = r[si].strip() if si < len(r) else ""
                if st in out:
                    out[st].append(",".join(r))
            with LOCK:
                for c, lines in out.items():
                    pathlib.Path(CACHE, f"asos_{c}_{obs_start}_{obs_end}.csv").write_text(
                        "\n".join(lines) + "\n", encoding="utf-8")

    jobs = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for zone, sts in groups.items():
            jobs.append(pool.submit(prev_one, zone, sts))
            jobs.append(pool.submit(ens_one, zone, sts))
        jobs.append(pool.submit(iem_one, codes))
        for j in jobs:
            try:
                j.result()
            except Exception as exc:
                print(f"prewarm skip: {type(exc).__name__} {exc}", file=sys.stderr)


def station_stats(code: str, kind: str, target: date, obs_start: date, obs_end: date):
    """Bias / sigma / member samples per model, memoized on disk for the trading day.

    The expensive part of a scan is re-deriving these for every bracket; they only
    depend on (station, kind, target), so one computation serves the whole ladder.
    """
    tag = hashlib.sha256(f"{code}|{kind}|{target}|{obs_start}|{obs_end}".encode()).hexdigest()[:24]
    p = pathlib.Path(CACHE, "stats_" + tag + ".json")
    if p.exists() and (datetime.now().timestamp() - p.stat().st_mtime) < 3600 * 6:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass

    zone = STATIONS[code]["tz"]
    is_max = kind == "high"
    variable = "temperature_2m_max" if is_max else "temperature_2m_min"
    hi, lo = observed_daily(code, zone, obs_start, obs_end)
    obs = hi if is_max else lo
    prevs = prev_all_models(code, zone, obs_start, obs_end)
    mems = members_all_models(code, zone, target, variable)

    per_model = {}
    for model in MODELS:
        if model not in prevs:
            continue
        fct = prevs[model]["max" if is_max else "min"]
        paired = sorted(set(obs) & set(fct))[-40:]
        mem = mems.get(model) or []
        if len(paired) < 20 or not mem:
            continue
        resid = [obs[d] - fct[d] for d in paired]
        per_model[model] = {"bias": mean(resid), "sigma": pstdev(resid),
                            "members": mem, "n": len(paired)}
    result = {"per_model": per_model}
    p.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def price_event(ev, target: date, obs_start: date, obs_end: date, spend: float):
    code = ev["code"]
    rows = []
    if code == "HKO":
        # HK settles on the Observatory Daily Extract at 0.1 C precision, and the
        # rule says the bracket CONTAINS the reading -> boundaries sit on the integer,
        # not at k+/-0.5 (this is a floor rule, unlike the METAR markets).
        est = hk_model(target)
        if ev["kind"] not in est:
            return rows
        e = est[ev["kind"]]
        centre, sigma = e["official"], e["sigma"]
        for mk in ev["markets"]:
            bd = hk_interval(mk.get("groupItemTitle"))
            if not bd:
                continue
            lo, hi = bd
            ps = _norm_probs([centre], sigma, lo, hi, half_width=0.0)
            p = ps[0]
            drift = 0.0
            for s in (0.5, 1.0, 1.5):
                drift = max(drift,
                            _norm_probs([centre - s], sigma, lo, hi, half_width=0.0)[0],
                            _norm_probs([centre + s], sigma, lo, hi, half_width=0.0)[0])
            rows.append(_row(ev, mk, p, p, p, None, None, centre=centre, sigma=sigma,
                             note=f"天文台官方预报{centre:.0f}°C，误差±{sigma:.1f}", spread=0.0,
                             p_drift=drift))
        return rows

    stats = station_stats(code, ev["kind"], target, obs_start, obs_end)
    raw = stats["per_model"]
    if len(raw) < 2:
        return rows

    factor = 9 / 5 if ev["unit"] == "F" else 1.0
    offset = 32.0 if ev["unit"] == "F" else 0.0
    per_model = {}
    for model, v in raw.items():
        mem = v["members"]
        per_model[model] = {
            "raw": [m * factor + offset for m in mem],
            "bias": [(m + v["bias"]) * factor + offset for m in mem],
            "sigma": v["sigma"] * factor,
            "centre": (median(mem) + v["bias"]) * factor + offset,
        }

    centres = [v["centre"] for v in per_model.values()]
    spread = max(centres) - min(centres)

    for mk in ev["markets"]:
        bd = bounds(mk.get("groupItemTitle"))
        if not bd:
            continue
        lo, hi = bd
        pr = {m: frac_in(v["raw"], lo, hi) for m, v in per_model.items()}
        pb = {m: frac_in(v["bias"], lo, hi) for m, v in per_model.items()}
        every = list(pr.values()) + list(pb.values())
        # Drift check: the classic NO killer is a uniform ~1 deg forecast miss that
        # lands the centre on (or next to) the bracket you sold. Worst case over
        # +/-0.5/1/1.5 deg shifts, in market units (1.5 C = 2.7 F).
        drift = 0.0
        for s in (0.5 * factor, 1.0 * factor, 1.5 * factor):
            for v in per_model.values():
                drift = max(drift,
                            frac_in([x - s for x in v["bias"]], lo, hi),
                            frac_in([x + s for x in v["bias"]], lo, hi))
        rows.append(_row(ev, mk, min(every), median(every), max(every),
                         pr, pb, centre=median(centres), sigma=median(
                             v["sigma"] for v in per_model.values()),
                         note=f"三模式中心 {min(centres):.0f}-{max(centres):.0f}"
                              f"{ev['unit']}，分歧 {spread:.1f}",
                         spread=spread, p_drift=drift))
    return rows


def _norm_probs(centres, sigma, lo, hi, half_width=0.5):
    from pm_common import norm_cdf
    out = []
    for c in centres:
        a = 0.0 if lo is None else norm_cdf((lo - half_width - c) / sigma)
        b = 1.0 if hi is None else norm_cdf((hi + half_width - c) / sigma)
        out.append(max(0.0, b - a))
    return out


def _row(ev, mk, pmin, pmean, pmax, pr, pb, centre, sigma, note="", spread=None, p_drift=0.0):
    tokens = mk.get("clobTokenIds")
    tokens = json.loads(tokens) if isinstance(tokens, str) else (tokens or [])
    return {
        "code": ev["code"], "city": city_name(ev["code"]), "kind": ev["kind"], "unit": ev["unit"],
        "title": ev["title"], "label": mk.get("groupItemTitle"),
        "p_min": pmin, "p_mean": pmean, "p_max": pmax, "p_drift": p_drift,
        "bid": as_float(mk.get("bestBid")), "ask": as_float(mk.get("bestAsk")),
        "vol": as_float(mk.get("volume24hr")) or 0.0,
        "yes_token": tokens[0] if tokens else None,
        "no_token": tokens[1] if len(tokens) > 1 else None,
        "centre": centre, "sigma": sigma, "note": note, "spread": spread,
        "event_vol": ev["vol"],
    }


def attach_books(rows, spend, workers=4):
    def one(r):
        if not r["yes_token"]:
            return
        try:
            yb, ya = book(r["yes_token"])
            nb, na = book(r["no_token"]) if r["no_token"] else ([], [])
        except Exception:
            return
        yv, _ = vwap(ya, spend)
        nv, _ = vwap(na, spend)
        r.update({"yes_vwap": yv, "no_vwap": nv,
                  "yes_depth": depth(ya), "no_depth": depth(na),
                  "yes_bid": yb[0][0] if yb else None,
                  "no_ask_realtime": na[0][0] if na else None})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, rows))


# ---------------------------------------------------------------- report

KIND_CN = {"high": "最高温", "low": "最低温"}


def fmt_table(rows, spend, min_edge, mode, top, max_spread, risky=False):
    def ok(r):
        sp = r.get("spread")
        if sp is not None and sp > max_spread:
            return False
        return True

    out = []
    if mode in ("yes", "both"):
        cand = [r for r in rows if r.get("yes_vwap") and r["p_min"] - r["yes_vwap"] >= min_edge
                and ok(r) and r.get("yes_depth", 0) >= 3 * spend]
        cand.sort(key=lambda r: -(r["p_min"] - r["yes_vwap"]))
        out.append("## 买 YES｜花小钱赌这一档会中")
        out.append("| 城市 | 市场 | 档位 | 模型给的概率 | 实际买入价 | 便宜多少 | 盘口深度 | 依据 |")
        out.append("|---|---|---|---|---|---|---|---|")
        for r in cand[:top]:
            out.append(f"| {r['city']} | {KIND_CN[r['kind']]} | {r['label']} "
                       f"| {r['p_min']:.0%}（乐观 {r['p_max']:.0%}） "
                       f"| {r['yes_vwap']:.3f} | +{(r['p_min']-r['yes_vwap'])*100:.0f}pp "
                       f"| ${r['yes_depth']:.0f} | {r['note']} |")
        out.append("")
    if mode in ("no", "both"):
        cand, fragile = [], []
        for r in rows:
            if not (r.get("no_vwap") and (1 - r["p_max"]) - r["no_vwap"] >= min_edge
                    and ok(r) and r.get("no_depth", 0) >= 3 * spend):
                continue
            # 抗漂移门槛：±1.5 度朝该档漂移后仍要剩 5pp 边际，否则这是
            # "一次普通预报误差就翻车"的相邻档 NO
            if (1 - r.get("p_drift", 0.0)) - r["no_vwap"] < 0.05:
                fragile.append(r)
            else:
                cand.append(r)
        cand.sort(key=lambda r: -((1 - r["p_max"]) - r["no_vwap"]))
        out.append("## 买 NO｜高胜率，赌这一档不会中")
        out.append("| 城市 | 市场 | 档位 | 模型给的胜率 | 买入价 | 赔率 | 便宜多少 | 抗漂移胜率 | 盘口深度 | 依据 |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")
        shown = cand[:top]
        if risky:
            shown = shown + fragile[:max(0, top - len(shown))]
        for r in shown:
            no_v = r["no_vwap"]
            mark = " ⚠️脆弱" if r in fragile else ""
            out.append(f"| {r['city']} | {KIND_CN[r['kind']]} | {r['label']}{mark} "
                       f"| {1-r['p_max']:.0%}（保守 {1-r['p_min']:.0%}） "
                       f"| {no_v:.3f} | {(1-no_v)/no_v:.2f}:1 | +{((1-r['p_max'])-no_v)*100:.0f}pp "
                       f"| {1-r.get('p_drift',0):.0%} | ${r['no_depth']:.0f} | {r['note']} |")
        if fragile:
            out.append(f"\n已过滤 {len(fragile)} 个漂移脆弱档（模型概率够，但 ±1.5°C 漂移就翻车"
                       f"——10/24 华沙 6°、亚特兰大 70-71° 都是这类）。加 --risky 查看。")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- settle

def settle_daily(station: str, zone: str, day: date):
    """Routine-METAR extremes for one local day, no completeness gate (grading only)."""
    prev_d, next_d = day - timedelta(days=1), day + timedelta(days=1)
    url = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
           f"station={station}&data=tmpc,metar"
           f"&year1={prev_d.year}&month1={prev_d.month}&day1={prev_d.day}"
           f"&year2={next_d.year}&month2={next_d.month}&day2={next_d.day}"
           "&tz=UTC&format=onlycomma&latlon=no&missing=M&trace=T&direct=no")
    text = cached_text(f"settle_{station}_{day}", url, ttl_hours=24)
    tz, us = ZoneInfo(zone), station.startswith("K")
    hi, lo, seen = -99.0, 99.0, set()
    for row in csv.DictReader(io.StringIO(text)):
        rep, stamp = (row.get("metar") or "").strip(), (row.get("valid") or "").strip()
        if not rep or not stamp or rep in seen:
            continue
        seen.add(rep)
        if not is_routine(rep, us):
            continue
        t = metar_temp(rep)
        if t is None:
            continue
        try:
            utc = datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("UTC"))
        except ValueError:
            continue
        if utc.astimezone(tz).date() != day:
            continue
        hi, lo = max(hi, t), min(lo, t)
    return (hi if hi > -50 else None, lo if lo < 50 else None)


def _winner_label(ev, v: float, hk: bool):
    """Bracket containing the settled value: floor rule for HK, round-half-up elsewhere."""
    r = math.floor(v) if hk else math.floor(v + 0.5)
    for m in ev["markets"]:
        lab = m.get("groupItemTitle") or ""
        bd = hk_interval(lab) if hk else bounds(lab)
        if not bd:
            continue
        lo, hi = bd
        if hk:
            # hk_interval already widened the upper edge: the reading is in [lo, hi)
            if (lo is None or v >= lo) and (hi is None or v < hi):
                return lab
        else:
            # round-half-up ladders: middle brackets are lo<=r<=hi, tails are
            # "X or below" (r<=hi) and "X or higher" (r>=lo)
            if (lo is None or r >= lo) and (hi is None or r <= hi):
                return lab
    return f"{v:.1f}（未匹配到档位）"


def _cached_centre(code, kind, target, obs_start, obs_end, unit):
    """Model centre from the scan-time stats cache, any age (grading only)."""
    tag = hashlib.sha256(f"{code}|{kind}|{target}|{obs_start}|{obs_end}".encode()).hexdigest()[:24]
    p = pathlib.Path(CACHE, "stats_" + tag + ".json")
    if not p.exists():
        return None
    try:
        stats = json.loads(p.read_text(encoding="utf-8"))
        cs = [sorted(v["members"])[len(v["members"]) // 2] + v["bias"]
              for v in stats["per_model"].values() if v.get("members")]
        if not cs:
            return None
        c = median(cs)
        return c * (9 / 5 if unit == "F" else 1.0) + (32.0 if unit == "F" else 0.0)
    except Exception:
        return None


def settle(target: date) -> int:
    """Grade a finished date: actuals, winning bracket, and the model centre's miss."""
    merged, ids = [], set()
    for ev in discover(target) + discover(target, closed=True):
        if ev["id"] not in ids:
            ids.add(ev["id"])
            merged.append(ev)
    events = merged
    print(f"# {target} 结算核对（{len(events)} 个事件）\n")
    print("| 城市 | 市场 | 实际 | 获胜档 | 模型中心（缓存） | 偏差 |")
    print("|---|---|---|---|---|---|")
    obs_end = target - timedelta(days=1)
    obs_start = obs_end - timedelta(days=64)
    for ev in sorted(events, key=lambda e: -e.get("vol") or 0):
        code, kind = ev["code"], ev["kind"]
        if code == "HKO":
            kind_key = "CLMMAXT" if kind == "high" else "CLMMINT"
            col = "max" if kind == "high" else "min"
            v = (hko_extract_month(target.year, target.month, col).get(target)
                 or hko_month(kind_key, target.year, target.month).get(target))
            if v is None:
                print(f"| 香港 | {KIND_CN[kind]} | 天文台摘录未发布（滞后1-2天） | - | - | - |")
                continue
            winner, unit = _winner_label(ev, v, hk=True), "°C"
        else:
            hi, lo = settle_daily(code, STATIONS[code]["tz"], target)
            v = hi if kind == "high" else lo
            if v is None:
                print(f"| {city_name(code)} | {KIND_CN[kind]} | 无观测数据 | - | - | - |")
                continue
            if ev["unit"] == "F":          # METAR obs are Celsius; the ladder is Fahrenheit
                v = v * 9 / 5 + 32
            winner, unit = _winner_label(ev, v, hk=False), ("°F" if ev["unit"] == "F" else "°C")
        centre = _cached_centre(code, kind, target, obs_start, obs_end, ev["unit"])
        miss = f"{v - centre:+.1f}" if centre is not None else "-"
        c_txt = f"{centre:.1f}" if centre is not None else "-"
        print(f"| {city_name(code)} | {KIND_CN[kind]} | {v:.1f}{unit} | {winner} | {c_txt} | {miss} |")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Weather bracket scanner (routine-METAR basis).")
    ap.add_argument("--date", default="", help="Target local date YYYY-MM-DD (default: tomorrow UTC).")
    ap.add_argument("--spend", type=float, default=20.0, help="USD per order for the VWAP check.")
    ap.add_argument("--min-edge", type=float, default=0.04)
    ap.add_argument("--mode", choices=["yes", "no", "both"], default="both")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--only", default="",
                    help="Comma-separated city codes or Chinese names to limit the scan "
                         "(e.g. KLGA,东京,香港). Cuts a cold scan from minutes to seconds.")
    ap.add_argument("--max-spread", type=float, default=1.5,
                    help="Reject brackets where the three model centres disagree by more than this (C or F).")
    ap.add_argument("--lookback", type=int, default=64, help="Days of history for bias/sigma.")
    ap.add_argument("--workers", type=int, default=4, help="Parallel stations (Open-Meteo rate limit).")
    ap.add_argument("--settle", default="",
                    help="Grade a finished date (YYYY-MM-DD) instead of scanning: actuals, "
                         "winning bracket, cached model centre.")
    ap.add_argument("--risky", action="store_true",
                    help="NO 表附带漂移脆弱档（默认过滤掉 ±1.5°C 就翻车的相邻档）。")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    target = (date.fromisoformat(args.date) if args.date
              else datetime.now(ZoneInfo("UTC")).date() + timedelta(days=1))
    if args.settle:
        return settle(date.fromisoformat(args.settle))
    obs_end = target - timedelta(days=1)
    obs_start = obs_end - timedelta(days=args.lookback)

    events = discover(target)
    if args.only:
        want = {x.strip().lower() for x in args.only.split(",") if x.strip()}
        events = [e for e in events
                  if e["code"].lower() in want or city_name(e["code"]).lower() in want
                  or any(w in e["title"].lower() for w in want)]
    if not events:
        print(f"no open temperature markets for {target}")
        return 1
    print(f"{target} | {len(events)} events", file=sys.stderr)

    t0 = time.time()
    prewarm([ev["code"] for ev in events], target, obs_start, obs_end, workers=args.workers)
    print(f"prewarm {time.time() - t0:.1f}s", file=sys.stderr)

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(price_event, ev, target, obs_start, obs_end, args.spend): ev
                   for ev in events if ev["code"] != "HKO"}
        hk = [ev for ev in events if ev["code"] == "HKO"]
        for f in futures:
            ev = futures[f]
            try:
                rows.extend(f.result())
            except Exception as exc:
                print(f"skip {ev['title']}: {type(exc).__name__} {exc}", file=sys.stderr)
        for ev in hk:
            try:
                rows.extend(price_event(ev, target, obs_start, obs_end, args.spend))
            except Exception as exc:
                print(f"skip {ev['title']}: {type(exc).__name__} {exc}", file=sys.stderr)

    # only bother reading books for plausible candidates
    shortlist = [r for r in rows
                 if (r["p_min"] - (r["ask"] or 1) > -0.02) or ((1 - r["p_max"]) - (1 - (r["bid"] or 0)) > 0.02)]
    attach_books(shortlist, args.spend)

    if args.json:
        print(json.dumps(shortlist, ensure_ascii=False, indent=1, default=str))
        return 0

    print(f"\n# {target} 天气盘扫描  (${args.spend:.0f} 单量, 边际门槛 {args.min_edge*100:.0f}pp)\n")
    table = fmt_table(shortlist, args.spend, args.min_edge, args.mode, args.top,
                      args.max_spread, risky=args.risky)
    print(table if table.strip() else "没有过门槛的档位。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
