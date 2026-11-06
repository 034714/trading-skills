"""Permanent offline storage regressions; no shared cache or ledger writes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import date

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pm_weather_storage as store


def weather():
    spec = importlib.util.spec_from_file_location("weather_storage_tests", SCRIPTS / "pm_weather.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paths_and_overrides_are_lazy(tmp_path):
    local = tmp_path / "local"
    cache, data = store.storage_paths({"LOCALAPPDATA": str(local)}, "win32", tmp_path)
    assert cache == local / "trading-skills/pm-weather/cache"
    assert data == local / "trading-skills/pm-weather/data"
    assert not local.exists()
    cache, data = store.storage_paths({}, "linux", tmp_path)
    assert cache == tmp_path / ".cache/trading-skills/pm-weather/cache"
    assert data == tmp_path / ".local/share/trading-skills/pm-weather/data"
    cache, data = store.storage_paths({}, "darwin", tmp_path)
    assert "Library" in cache.parts and "Application Support" in data.parts
    cache, data = store.storage_paths({}, "win32", tmp_path)
    assert cache == tmp_path / "AppData/Local/trading-skills/pm-weather/cache"
    custom = {"PM_WEATHER_CACHE_DIR": str(tmp_path / "custom-cache"),
              "PM_WEATHER_DATA_DIR": str(tmp_path / "custom-data")}
    assert store.storage_paths(custom) == (tmp_path / "custom-cache", tmp_path / "custom-data")
    with pytest.raises(ValueError):
        store.storage_paths({"PM_WEATHER_CACHE_DIR": "relative"})
    with pytest.raises(ValueError):
        store.storage_paths({"PM_WEATHER_CACHE_DIR": str(tmp_path / ".mimosa/cache")})


def test_import_and_help_do_not_write(tmp_path):
    env = dict(os.environ, PM_WEATHER_CACHE_DIR=str(tmp_path / "cache"),
               PM_WEATHER_DATA_DIR=str(tmp_path / "data"), PYTHONDONTWRITEBYTECODE="1")
    code = "import sys;sys.path.insert(0,sys.argv[1]);import pm_weather"
    for command in ([sys.executable, "-I", "-B", "-c", code, str(SCRIPTS)],
                    [sys.executable, "-I", "-B", str(SCRIPTS / "pm_weather.py"), "--help"]):
        result = subprocess.run(command, env=env, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_expiry_capacity_and_protected_records(tmp_path, monkeypatch):
    cache, data = tmp_path / "cache", tmp_path / "cache/data"
    cache.mkdir()
    data.mkdir()
    ledger = data / "forecast_snapshots.jsonl"
    ledger.write_text("immutable prediction", encoding="utf-8")
    rule = data / "rules" / ("f" * 64 + ".txt")
    rule.parent.mkdir()
    rule.write_text("long rule", encoding="utf-8")
    security = cache / ".mimosa/nested"
    security.mkdir(parents=True)
    sealed = security / ("a" * 32 + ".json")
    sealed.write_text("sealed", encoding="utf-8")
    root_security = cache / ".mimosa" / ("b" * 32 + ".json")
    root_security.write_text("sealed root", encoding="utf-8")
    unknown = cache / "settle_history.jsonl"
    unknown.write_text("keep", encoding="utf-8")
    files = [cache / (c * 32 + ".json") for c in "123"]
    for i, path in enumerate(files):
        path.write_text("x" * 1024, encoding="utf-8")
        os.utime(path, (time.time() - 100 + i, time.time() - 100 + i))
    monkeypatch.setenv("PM_WEATHER_CACHE_MAX_MB", str(2048 / 1024**2))
    monkeypatch.setenv("PM_WEATHER_CACHE_TTL_HOURS", "1")
    store.prune_cache(cache, data)
    assert not files[0].exists()
    assert files[1].exists() and files[2].exists()
    monkeypatch.setenv("PM_WEATHER_CACHE_TTL_HOURS", "0")
    store.prune_cache(cache, data)
    assert not any(path.exists() for path in files)
    assert ledger.read_text() == "immutable prediction"
    assert rule.read_text() == "long rule"
    assert sealed.read_text() == "sealed" and root_security.read_text() == "sealed root"
    assert unknown.read_text() == "keep"
    # Even an explicitly supplied nested security root is untouchable.
    store.prune_cache(security, data)
    assert sealed.exists()


def test_cached_helpers_lazy_writes_and_ttl(tmp_path, monkeypatch):
    w = weather()
    monkeypatch.setattr(w, "CACHE_MODE", "disk")
    cache = tmp_path / "cache/new-namespace"
    monkeypatch.setattr(w, "CACHE", str(cache))
    monkeypatch.setattr(w, "DATA", tmp_path / "data")
    calls = []
    monkeypatch.setattr(w, "http_json", lambda url: calls.append(url) or {"ok": True})
    monkeypatch.setattr(w, "_validate_url", lambda url: url)
    assert w.cached("key", "https://example.com") == {"ok": True}
    assert w.cached("key", "https://example.com") == {"ok": True}
    assert len(calls) == 1
    monkeypatch.setenv("PM_WEATHER_CACHE_TTL_HOURS", "0")
    w.cached("key", "https://example.com")
    assert len(calls) == 2 and not Path(w._cache_path("key", ".json")).exists()
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b"weather text"
    monkeypatch.setattr(w.urllib.request, "urlopen", lambda *a, **k: Response())
    assert w.cached_text("text", "https://example.com") == "weather text"
    assert not Path(w._cache_path("text", ".txt")).exists()
    assert not (tmp_path / "data").exists()


def test_rule_dedup_ledgers_and_centre(tmp_path, monkeypatch):
    w = weather()
    monkeypatch.setattr(w, "SAVE_RECORDS", True)
    data = tmp_path / "data"
    monkeypatch.setattr(w, "DATA", data)
    monkeypatch.setattr(w, "FORECAST_HISTORY", data / "forecast_snapshots.jsonl")
    monkeypatch.setattr(w, "SETTLE_HISTORY", data / "settle_history.jsonl")
    row = dict(code="KLGA", kind="high", unit="C", centre=20, rules="long rule " * 1000)
    target = date(2026, 1, 2)
    w.snapshot_forecast(target, [row, dict(row)], "2026-01-01T00:00:00+00:00")
    snap = json.loads(w.FORECAST_HISTORY.read_text(encoding="utf-8"))
    assert "rules" not in snap["rows"][0]
    assert snap["rows"][0]["rules_sha256"] == snap["rows"][1]["rules_sha256"]
    assert len(list((data / "rules").iterdir())) == 1
    assert next((data / "rules").iterdir()).read_text(encoding="utf-8") == row["rules"]
    assert "rules" in row
    assert w._cached_centre("KLGA", "high", target, target, target, "C") == 20
    w._snap_fnd({target: (20, 10)})
    w._append_history([dict(d="2026-01-02", city="NY", code="KLGA", kind="high", unit="C")])
    assert (data / "hko_fnd_history.jsonl").exists()
    assert w.SETTLE_HISTORY.exists()


def test_bad_limits_fail_clearly(monkeypatch):
    for value in ("-1", "nan", "inf", "bad"):
        monkeypatch.setenv("PM_WEATHER_CACHE_TTL_HOURS", value)
        with pytest.raises(ValueError):
            store.ttl_hours(3)


def test_overlapping_data_override_never_evicts_records(tmp_path, monkeypatch):
    path = tmp_path / ("a" * 32 + ".json")
    path.write_text("persistent", encoding="utf-8")
    monkeypatch.setenv("PM_WEATHER_CACHE_MAX_MB", "0")
    store.prune_cache(tmp_path, tmp_path)
    assert path.read_text() == "persistent"


def test_cache_links_never_delete_their_target(tmp_path, monkeypatch):
    target = tmp_path / "outside"
    target.write_text("protected", encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    link = cache / ("b" * 32 + ".json")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation needs Windows developer mode or privilege")
    monkeypatch.setenv("PM_WEATHER_CACHE_MAX_MB", "0")
    store.prune_cache(cache, tmp_path / "data")
    assert link.is_symlink() and target.read_text() == "protected"


@pytest.mark.parametrize("mode", ["memory", "off"])
def test_default_cache_never_reads_or_writes_disk(tmp_path, monkeypatch, mode):
    w = weather()
    monkeypatch.setattr(w, "CACHE_MODE", mode)
    monkeypatch.setattr(w, "CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(w, "DATA", tmp_path / "data")
    # A fresh-looking legacy disk result must not be used in memory/off modes.
    path = Path(w._cache_path("key", ".json"))
    path.parent.mkdir()
    path.write_text('{"stale": true}', encoding="utf-8")
    calls = []
    monkeypatch.setattr(w, "_validate_url", lambda url: url)
    monkeypatch.setattr(w, "http_json", lambda url: calls.append(url) or {"live": True})
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert w.cached("key", "https://example.com") == {"live": True}
    assert w.cached("key", "https://example.com") == {"live": True}
    assert len(calls) == (1 if mode == "memory" else 2)
    w._write_cache(tmp_path / "cache/prevparse_KLGA_x_y.json", "{}")
    assert w._cache_fresh(tmp_path / "cache/prevparse_KLGA_x_y.json", 3) == (mode == "memory")
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not (tmp_path / "data").exists()


def test_default_history_entry_points_are_noops(tmp_path, monkeypatch, capsys):
    w = weather()
    assert w.SAVE_RECORDS is False
    monkeypatch.setattr(w, "DATA", tmp_path / "data")
    monkeypatch.setattr(w, "FORECAST_HISTORY", tmp_path / "data/forecast_snapshots.jsonl")
    monkeypatch.setattr(w, "SETTLE_HISTORY", tmp_path / "data/settle_history.jsonl")
    target = date(2026, 1, 2)
    assert w.snapshot_forecast(target, [{"rules": "must not persist"}]) is None
    w._snap_fnd({target: (20, 10)})
    w._append_history([dict(d="2026-01-02", code="HKO", kind="high", city="HK")])
    assert w.bias_report() == 1
    assert "默认不保存" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_cache_mode_validation(monkeypatch):
    monkeypatch.delenv("PM_WEATHER_CACHE_MODE", raising=False)
    assert store.cache_mode() == "memory"
    monkeypatch.setenv("PM_WEATHER_CACHE_MODE", "disk")
    assert store.cache_mode() == "disk"
    with pytest.raises(ValueError):
        store.cache_mode("invalid")


@pytest.mark.parametrize("mode", ["memory", "disk"])
def test_parsed_prewarm_cache_is_reused(tmp_path, monkeypatch, mode):
    w = weather()
    monkeypatch.setattr(w, "CACHE_MODE", mode)
    monkeypatch.setattr(w, "CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(w, "DATA", tmp_path / "data")
    start, end, target = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    raw = {"ecmwf_ifs025": {"max": {str(start): 20}, "min": {str(start): 10}}}
    w._write_cache(tmp_path / f"cache/prevparse_KLGA_{start}_{end}.json", json.dumps(raw))
    members = {"ecmwf_ifs025": [20, 21]}
    w._write_cache(tmp_path / f"cache/memparse_KLGA_{target}_temperature_2m_max.json", json.dumps(members))
    def no_network(*a, **k):
        pytest.fail("parsed prewarm cache missed and attempted another request")
    monkeypatch.setattr(w, "cached", no_network)
    assert w.prev_all_models("KLGA", "America/New_York", start, end)["ecmwf_ifs025"]["max"] == {start: 20}
    assert w.members_all_models("KLGA", "America/New_York", target, "temperature_2m_max") == members
