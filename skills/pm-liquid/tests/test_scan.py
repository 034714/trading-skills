"""Offline tests for pm-liquid/scan.py pure logic. No network."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import scan  # noqa: E402


def test_csv():
    assert scan.csv("a, b ,c") == ["a", "b", "c"]
    assert scan.csv("") == []
    assert scan.csv(None) == []


def test_fnum():
    assert scan.fnum("0.6100") == 0.61
    assert scan.fnum(None) is None
    assert scan.fnum("") is None
    assert scan.fnum("0.0000") is None   # Kalshi zero means absent, not a real quote


def test_horizon():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=2)).isoformat()
    soon = (now + timedelta(hours=10)).isoformat()
    week = (now + timedelta(days=3)).isoformat()
    month = (now + timedelta(days=20)).isoformat()
    long_ = (now + timedelta(days=90)).isoformat()
    assert scan.horizon(past) == "已收盘"
    assert scan.horizon(soon) == "日内"
    assert scan.horizon(week) == "1-7天"
    assert scan.horizon(month) == "1-4周"
    assert scan.horizon(long_) == "1月+"
    assert scan.horizon("not-a-date") == "?"
    assert scan.horizon(None) == "?"


def test_band_ok_longshot_screen():
    assert scan.band_ok(0.05, tail=False) is False
    assert scan.band_ok(0.09, tail=False) is False
    assert scan.band_ok(0.10, tail=False) is True
    assert scan.band_ok(0.90, tail=False) is True
    assert scan.band_ok(0.95, tail=False) is False
    assert scan.band_ok(0.02, tail=True) is True


def test_same_event_strict():
    assert scan.same_event("Lakers vs Warriors", "Lakers vs Warriors")
    # city-name collision must not pair a mayoral market with a team market
    assert not scan.same_event("Los Angeles Mayoral Election", "Los Angeles L wins")
    assert not scan.same_event("Bitcoin price on Nov 4", "Ethereum price on Nov 4")


def _row(**kw):
    base = {"yes_ask": 0.50, "yes_bid": 0.48, "no_ask": 0.52, "no_bid": 0.50,
            "spread": 0.02, "tail": False,
            "fill": {"complete": True, "shares": 20},
            "no_fill": {"complete": True, "shares": 20}}
    base.update(kw)
    return base


def test_verdict():
    scan.SHARES = 20
    assert scan.verdict(_row(), 0.05) == "Y/N可挂"
    assert scan.verdict(_row(no_fill={"complete": False, "shares": 0}), 0.05) == "Y可挂"
    assert scan.verdict(_row(fill={"complete": False, "shares": 0}), 0.05) == "N可挂"
    row = _row(fill={"complete": False, "shares": 5},
               no_fill={"complete": False, "shares": 5})
    assert scan.verdict(row, 0.05) == "Y深度5<20+N深度5<20"
    row["spread"] = 0.09
    assert "价差" in scan.verdict(row, 0.05)
    row = _row(yes_ask=0.04, no_ask=0.96, spread=0.01)
    assert scan.verdict(row, 0.05) == "Y低价屏+N高价屏"


def test_when_utc():
    from datetime import datetime, timedelta, timezone
    iso = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    out = scan.when(iso)
    assert out.endswith("Z") and len(out) == 12
    assert scan.when("bad") == ""


def test_kalshi_book_inverts_no_bids(monkeypatch):
    """Kalshi orderbook returns per-side BIDS; YES asks = 1 - NO bid."""
    fake = {"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.7000", "100.00"]]}}
    monkeypatch.setattr(scan, "http_json", lambda url, **kw: fake)
    fill = scan.kalshi_book("KXFAKE", 20)
    assert fill["complete"] is True
    assert abs(fill["vwap"] - 0.30) < 1e-9
    assert fill["shares"] == 20


def test_kalshi_book_handles_missing(monkeypatch):
    from pm_common import FetchError

    def boom(url, **kw):
        raise FetchError("nope")

    monkeypatch.setattr(scan, "http_json", boom)
    assert scan.kalshi_book("KXFAKE", 20) is None
