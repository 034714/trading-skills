"""Permanent regressions for wrapper retry ownership and brief/review safety."""
import json
from pathlib import Path
import sys
import urllib.error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pm_weather as w


@pytest.mark.parametrize("status,expected", [(406, 1), (429, 3), (503, 3)])
@pytest.mark.parametrize("wrapper", ["book", "text"])
def test_wrapper_bounded_retry_no_final_sleep(monkeypatch, status, expected, wrapper):
    calls, sleeps = [], []
    monkeypatch.setattr(w.time, "sleep", sleeps.append)
    monkeypatch.setattr(w, "_cache_fresh", lambda *a: False)
    monkeypatch.setattr(w, "_PREWARM_DEADLINE", None)
    def fail_json(*a, **k):
        calls.append(1)
        raise w.FetchError("HTTP failure", status=status, retryable=status != 406)
    def fail_text(*a, **k):
        calls.append(1)
        raise urllib.error.HTTPError("https://example.com/?secret=yes", status,
                                     "secret", {}, None)
    monkeypatch.setattr(w, "http_json", fail_json)
    monkeypatch.setattr(w.urllib.request, "urlopen", fail_text)
    with pytest.raises(w.FetchError) as caught:
        w.book("token") if wrapper == "book" else w.cached_text("key", "https://example.com")
    assert caught.value.status == status
    assert len(calls) == expected and len(sleeps) == expected - 1


def test_book_identity_mismatch_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(w, "http_json", lambda *a, **k: calls.append(1) or {"asset_id": "wrong"})
    with pytest.raises(w.FetchError, match="asset_id mismatch") as caught:
        w.book("right")
    assert not caught.value.retryable and len(calls) == 1


def test_no_tick_inference_or_partial_taker_promotion():
    row = {"p_calibrated": .8, "yes_bid": .49, "yes_ask": .51,
           "yes_complete": False}
    m = w.side_metrics(row)["yes"]
    assert m["maker"] is None
    assert w.recommend_side({"sides": {"yes": m}}) is None
    row["tick_size"] = .01
    assert w.side_metrics(row)["yes"]["maker"] == .5
    row["yes_bid"] = .495  # off legal grid: never mechanically undercut
    assert w.side_metrics(row)["yes"]["maker"] is None


def test_negative_edge_never_recommended():
    assert w.recommend_side({"sides": {"yes": {"complete": True, "taker": .8, "edge": -.1}}}) is None


def test_brief_obeys_edge_mode_stress_and_model_wording():
    row = {"city": "北京", "kind": "high", "label": "16°C", "event_vol": 10,
           "p_calibrated": .1, "p_stress": .2,
           "no_ask": .5, "no_vwap": .5, "no_complete": True}
    row["sides"] = w.side_metrics(row)
    def render(**kwargs):
        opts = dict(spend=20, min_edge=.04, mode="both", top=5, max_spread=2, brief=True)
        opts.update(kwargs)
        return w.fmt_table([row], **opts)
    assert "买 NO" in render() and "模型情景 90%" in render()
    assert "买 NO" not in render(mode="yes")
    assert "买 NO" not in render(min_edge=.5)
    row["p_stress"] = .9
    assert "买 NO" not in render()


def test_memory_zero_ttl_does_not_touch_disk(monkeypatch, tmp_path):
    monkeypatch.setenv("PM_WEATHER_CACHE_TTL_HOURS", "0")
    monkeypatch.setattr(w, "CACHE_MODE", "memory")
    def forbidden(*a):
        raise AssertionError("memory cache must not prune disk")
    monkeypatch.setattr(w, "prune_cache", forbidden)
    w._write_cache(tmp_path / "unused", "text")
    assert not list(tmp_path.iterdir())


def test_log_pick_is_explicit_only(monkeypatch, tmp_path):
    path = tmp_path / "data" / "picks.jsonl"
    monkeypatch.setattr(w, "PICKS", path)
    monkeypatch.setattr(w.sys, "argv", ["pm_weather", "--help"])
    with pytest.raises(SystemExit) as caught:
        w.main()
    assert caught.value.code == 0 and not path.exists()
    assert w.log_pick("id=x,side=YES,price=.5,label=16°C,code=ZBAA,kind=high,date=2026-01-01") == 0
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
