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


def test_pm_label():
    assert scan.pm_label("Zverev vs Musetti", "Zverev vs Musetti",
                         ["Zverev", "Musetti"]) == "Zverev vs Musetti｜YES=Zverev"
    assert scan.pm_label("AEK vs Real Madrid", "Will Real Madrid win?", ["Yes", "No"])         == "AEK vs Real Madrid｜Will Real Madrid win?"
    assert scan.pm_label("Aliens", "Aliens", ["Yes", "No"]) == "Aliens"


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


# Regression coverage for read-only round-scoped fetching and CLI error semantics.
def _pm_market(id, title, volume=1):
    return {"id": id, "conditionId": id, "question": title,
            "clobTokenIds": [id + "y", id + "n"], "outcomePrices": ["0.5", "0.5"],
            "outcomes": ["Yes", "No"], "bestBid": 0.48, "bestAsk": 0.5,
            "volume24hr": volume}


def test_round_books_full_keys_single_flight_and_no_disk(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone
    import builtins
    from threading import Lock
    calls, lock = [], Lock()

    def book(token):
        with lock:
            calls.append(token)
        return [(0.4, 30)], [(0.5, 30)]

    def forbidden(*args, **kwargs):
        raise AssertionError("business disk IO")

    monkeypatch.setattr(scan, "yes_book", book)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    a, b = "1" * 20 + "A", "1" * 20 + "B"
    before = datetime.now(timezone.utc)
    cache = scan.RoundBooks()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(cache.get, [a] * 12 + [b]))
    after = datetime.now(timezone.utc)
    assert calls.count(a) == calls.count(b) == 1
    assert all(before <= datetime.fromisoformat(r[2]) <= after for r in results)
    assert len({r[2] for r in results[:12]}) == 1
    scan.RoundBooks().get(a)
    assert calls.count(a) == 2


def test_round_books_failures_are_shared_not_timestamped(monkeypatch):
    import pytest
    calls = []
    def fail(token):
        calls.append(token)
        raise scan.FetchError("offline")
    monkeypatch.setattr(scan, "yes_book", fail)
    cache = scan.RoundBooks()
    for _ in range(2):
        with pytest.raises(scan.FetchError):
            cache.get("full-token")
    assert calls == ["full-token"]


def test_pm_query_before_keep_and_books(monkeypatch):
    markets = [_pm_market("other", "other", 100), _pm_market("target", "Needle", 1)]
    monkeypatch.setattr(scan, "events_by_tag", lambda *a: [{"markets": markets}])
    calls = []
    def book(token):
        calls.append(token)
        return [(0.48, 30)], [(0.5, 30)]
    monkeypatch.setattr(scan, "yes_book", book)
    rows, errors = scan.polymarket_rows(["sports"], 12, 1, .05, False, query="needle")
    assert [r["id"] for r in rows] == ["target"]
    assert calls == ["targety", "targetn"]
    assert errors.discovery_ok == 1 and not errors
    assert rows[0]["yes_book_sampled_at"] and rows[0]["no_book_sampled_at"]


def test_pm_book_errors_are_reported(monkeypatch):
    monkeypatch.setattr(scan, "events_by_tag", lambda *a: [{"markets": [_pm_market("a", "a")]}])
    def fail(token):
        raise scan.FetchError("book unavailable")
    monkeypatch.setattr(scan, "yes_book", fail)
    rows, errors = scan.polymarket_rows(["sports"], 12, 8, .05, False)
    assert errors.discovery_ok == 1 and len(errors) == 2
    assert rows[0]["yes_book_sampled_at"] is None
    assert not rows[0]["fill"]["complete"]


def test_kalshi_query_before_orderbooks_and_title_errors(monkeypatch):
    calls = []
    def fetch(url):
        calls.append(url)
        if "/events?" in url:
            raise scan.FetchError("titles offline")
        if "/orderbook?" in url:
            return {"orderbook_fp": {"no_dollars": [["0.5", "30"]]}}
        return {"markets": [dict(ticker=t, title=t, yes_bid_dollars=".48",
                                 yes_ask_dollars=".5") for t in ("other", "Needle")]}
    monkeypatch.setattr(scan, "http_json", fetch)
    rows, errors = scan.kalshi_rows(["series"], 40, .05, False, query="needle")
    assert [r["id"] for r in rows] == ["Needle"]
    assert errors.discovery_ok == 1 and len(errors) == 1
    assert sum("/orderbook?" in url for url in calls) == 1
    assert rows[0]["quote_sampled_at"] and rows[0]["yes_book_sampled_at"]


def test_safe_preserves_empty_and_errors():
    assert scan._safe(lambda: [], "empty") == ([], None)
    def fail():
        raise scan.FetchError("offline")
    assert scan._safe(fail, "source") == (None, "source: offline")


def _source_result(rows=(), errors=(), success=1):
    status = scan.SourceErrors()
    status.extend(errors)
    status.discovery_ok = success
    return list(rows), status


def test_cli_all_failed_exit_one_no_report(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["scan", "--json"])
    monkeypatch.setattr(scan, "polymarket_rows", lambda *a, **kw: _source_result(errors=["PM offline"], success=0))
    monkeypatch.setattr(scan, "kalshi_rows", lambda *a, **kw: _source_result(errors=["KA offline"], success=0))
    assert scan.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Polymarket" in captured.err and "Kalshi" in captured.err


def test_cli_partial_failure_zero_candidates_is_success(monkeypatch, capsys):
    import json
    monkeypatch.setattr(sys, "argv", ["scan", "--json", "--query", "no-match"])
    monkeypatch.setattr(scan, "polymarket_rows", lambda *a, **kw: _source_result(errors=["offline"], success=0))
    monkeypatch.setattr(scan, "kalshi_rows", lambda *a, **kw: _source_result())
    assert scan.main() == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["rows"] == [] and result["errors"]
    assert "覆盖不完整" in captured.err


def test_cli_unexpected_platform_exception_reported(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["scan", "--platform", "polymarket", "--json"])
    def fail(*a, **kw):
        raise ValueError("bad payload")
    monkeypatch.setattr(scan, "polymarket_rows", fail)
    assert scan.main() == 1
    assert "bad payload" in capsys.readouterr().err


def test_cli_json_default_limit_and_compact(monkeypatch, capsys):
    import json
    rows = [_row(id=str(i), title="market", question="question", rules="rules",
                 close=None, vol24h=1) for i in range(3)]
    monkeypatch.setattr(scan, "polymarket_rows", lambda *a, **kw: _source_result(rows))
    for extra, count, compact in [([], 3, False), (["--json-limit", "1"], 1, False),
                                   (["--json-limit", "0"], 0, False),
                                   (["--compact-json"], 3, True)]:
        monkeypatch.setattr(sys, "argv", ["scan", "--platform", "polymarket", "--json"] + extra)
        assert scan.main() == 0
        result = json.loads(capsys.readouterr().out)
        assert result["total_rows"] == 3 and result["returned_rows"] == count
        assert len(result["rows"]) == count and result["completed_at"]
        for row in result["rows"]:
            assert ("rules" in row) is not compact
            assert ("question" in row) is not compact


def test_cli_negative_json_limit_rejected(monkeypatch):
    import pytest
    monkeypatch.setattr(sys, "argv", ["scan", "--json-limit", "-1"])
    with pytest.raises(SystemExit) as error:
        scan.main()
    assert error.value.code == 2



def test_cli_read_only_table_and_two_rounds(monkeypatch, capsys):
    import builtins
    calls = []
    monkeypatch.setattr(scan, "events_by_tag", lambda *a: [{"markets": [_pm_market("a", "market")]}])
    def book(token):
        calls.append(token)
        return [(0.48, 30)], [(0.5, 30)]
    def forbidden(*args, **kwargs):
        raise AssertionError("business disk IO")
    monkeypatch.setattr(scan, "yes_book", book)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(sys, "argv", ["scan", "--platform", "polymarket", "--no-x"])
    for _ in range(2):
        assert scan.main() == 0
        captured = capsys.readouterr()
        assert "采样UTC" in captured.out and "扫描完成 UTC" in captured.out
        assert not captured.err
    assert calls == ["ay", "an", "ay", "an"]


def test_cli_successful_empty_source(monkeypatch, capsys):
    import json
    monkeypatch.setattr(scan, "events_by_tag", lambda *a: [])
    monkeypatch.setattr(sys, "argv", ["scan", "--platform", "polymarket", "--json"])
    assert scan.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["rows"] == [] and result["errors"] == []


def test_kalshi_orderbook_failure_aggregated(monkeypatch):
    def fetch(url):
        if "/events?" in url:
            return {"events": []}
        if "/orderbook?" in url:
            raise scan.FetchError("book offline")
        return {"markets": [{"ticker": "A", "title": "A", "yes_bid_dollars": ".48",
                             "yes_ask_dollars": ".5"}]}
    monkeypatch.setattr(scan, "http_json", fetch)
    rows, errors = scan.kalshi_rows(["S"], 40, .05, False)
    assert errors.discovery_ok == 1 and len(errors) == 1
    assert "orderbook A" in errors[0]
    assert rows[0]["yes_book_sampled_at"] is None
    assert not rows[0]["fill"]["complete"]
