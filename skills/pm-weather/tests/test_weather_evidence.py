"""Offline evidence and reporting regressions; never touch shared caches."""
import importlib.util
import json
import pathlib
import sys
from datetime import date, datetime, timezone

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("weather_evidence_module", SCRIPTS / "pm_weather.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


def event(code="KLGA", ident=1):
    return dict(id=ident, code=code, kind="high", unit="C", title=code, vol=1,
                markets=[dict(groupItemTitle="20°C")])


def test_local_day_boundary():
    now = datetime(2026, 1, 2, 1, tzinfo=timezone.utc)
    assert not w.local_day_finished(date(2026, 1, 1), "KLGA", now)
    assert w.local_day_finished(date(2026, 1, 1), "RJTT", now)


def test_snapshot_append_and_no_cache_reconstruction(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "SAVE_RECORDS", True)
    monkeypatch.setattr(w, "FORECAST_HISTORY", tmp_path / "predictions.jsonl")
    monkeypatch.setattr(w, "CACHE", str(tmp_path))
    day = date(2026, 1, 2)
    args = ("KLGA", "high", day, day, day, "C")
    assert w._cached_centre(*args) is None
    row = dict(code="KLGA", kind="high", unit="C", centre=20)
    first = w.snapshot_forecast(day, [row], "2026-01-01T00:00:00+00:00")
    second = w.snapshot_forecast(day, [dict(row, centre=99)], "2026-01-03T00:00:00+00:00")
    assert first != second
    assert w._cached_centre(*args) == 20
    records = [json.loads(x) for x in w.FORECAST_HISTORY.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert records[0]["model_issued_at"] is None


def test_settle_filter_failure_isolation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(w, "SAVE_RECORDS", True)
    monkeypatch.setattr(w, "discover", lambda *a, **k: [event(), event("RJTT", 2)])
    monkeypatch.setattr(w, "local_day_finished", lambda *a: True)
    calls = []
    def observed(code, *a):
        calls.append(code)
        if code == "KLGA":
            raise OSError("offline")
        return 20, 10
    monkeypatch.setattr(w, "settle_daily", observed)
    monkeypatch.setattr(w, "SETTLE_HISTORY", tmp_path / "settle.jsonl")
    monkeypatch.setattr(w, "FORECAST_HISTORY", tmp_path / "pred.jsonl")
    assert w.settle(date(2026, 1, 1)) == 2
    assert calls == ["KLGA", "RJTT"]
    rec = json.loads(w.SETTLE_HISTORY.read_text(encoding="utf-8"))
    assert rec["evidence_status"] == "pending_source_verification"
    calls.clear()
    assert w.settle(date(2026, 1, 1), "东京") == 0
    assert calls == ["RJTT"]


def test_no_direction_and_research_yes_excluded():
    row = dict(code="KLGA", kind="high", title="TEST", label="20°C", p_min=.1,
               p_max=.1, p_calibrated=.1, p_stress=.1, no_vwap=.7, no_price=.7,
               no_complete=True, yes_vwap=.01, yes_complete=True, research_only=True)
    result = w.fmt_table([row], 20, .04, "both", 8, 2, risky=True)
    yes, no = result.split("## NO")
    assert "TEST" not in yes
    assert "90%" in no and "70%" in no
    assert "30%" not in no
    assert "毛情景" in result and "未验证" in result


def test_bias_totals_separate_units(tmp_path, monkeypatch, capsys):
    history = tmp_path / "settle.jsonl"
    rows = [dict(d="2026-01-01", code="KLGA", city="NY", kind="high", unit=unit,
                 centre=20, actual=21, winner="20°C", centre_source="prediction_snapshot",
                 evidence_status="verified") for unit in ("C", "F")]
    history.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    monkeypatch.setattr(w, "SETTLE_HISTORY", history)
    assert w.bias_report() == 0
    text = capsys.readouterr().out
    assert "合计 °C" in text and "合计 °F" in text


def test_prewarm_retry_signature(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "CACHE", str(tmp_path))
    monkeypatch.setattr(w.time, "sleep", lambda *a: None)
    calls = []
    def fetch(*a, **k):
        calls.append(a[0])
        raise OSError("offline")
    monkeypatch.setattr(w, "cached", fetch)
    monkeypatch.setattr(w, "cached_text", fetch)
    monkeypatch.setattr(w, "_PREWARM_FAILED", set())
    w.prewarm(["KLGA"], date(2026, 1, 2), date(2025, 12, 1), date(2026, 1, 1))
    assert len(calls) == 6
    assert w._PREWARM_FAILED == {"KLGA"}


def mock_scan(monkeypatch, tmp_path, mode, outcome):
    """Exercise main without network access or shared cache/ledger writes."""
    monkeypatch.setattr(sys, "argv", ["pm_weather", "--date", "2026-01-02"] +
                        (["--json"] if mode == "json" else []))
    monkeypatch.setattr(w, "FORECAST_HISTORY", tmp_path / "predictions.jsonl")
    monkeypatch.setattr(w, "_PREWARM_FAILED", set())
    monkeypatch.setattr(w, "discover", lambda *a: [event()])
    def prewarm(*a, **k):
        if outcome == "prewarm_failed":
            w._PREWARM_FAILED.add("KLGA")
    monkeypatch.setattr(w, "prewarm", prewarm)
    def price(*a):
        if outcome == "event_failed":
            raise OSError("offline")
        if outcome == "empty":
            return []
        return [dict(code="KLGA", kind="high", unit="C", centre=20,
                     p_min=.8, p_max=.9, ask=.5, bid=.4)]
    monkeypatch.setattr(w, "price_event", price)
    monkeypatch.setattr(w, "attach_books", lambda *a, **k: None)
    monkeypatch.setattr(w, "fmt_table", lambda *a, **k: "offline table")


def test_main_output_modes_share_exit_status(monkeypatch, tmp_path, capsys):
    for outcome, expected in (("ok", 0), ("prewarm_failed", 2),
                              ("event_failed", 2), ("empty", 2)):
        for mode in ("json", "table"):
            mock_scan(monkeypatch, tmp_path, mode, outcome)
            assert w.main() == expected, (mode, outcome)
            assert not w.FORECAST_HISTORY.exists()
            output = capsys.readouterr().out
            if mode == "json":
                assert isinstance(json.loads(output), list)


def test_main_snapshot_uses_collection_completion_time(monkeypatch, tmp_path):
    mock_scan(monkeypatch, tmp_path, "json", "ok")
    monkeypatch.setattr(sys, "argv", sys.argv + ["--save-records"])
    class Clock(datetime):
        current = datetime(2026, 1, 2, 4, 59, tzinfo=timezone.utc)
        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)
    monkeypatch.setattr(w, "datetime", Clock)
    def finish_collection(*a, **k):
        # New York local midnight is 05:00 UTC in January.
        Clock.current = datetime(2026, 1, 2, 5, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(w, "attach_books", finish_collection)
    assert w.main() == 0
    snap = json.loads(w.FORECAST_HISTORY.read_text(encoding="utf-8"))
    assert snap["captured_at_utc"] == "2026-01-02T05:01:00+00:00"
    day = date(2026, 1, 2)
    assert w._cached_centre("KLGA", "high", day, day, day, "C") is None


def test_bias_no_verified_samples_explains_requirements(monkeypatch, tmp_path, capsys):
    history = tmp_path / "settle.jsonl"
    monkeypatch.setattr(w, "SETTLE_HISTORY", history)
    assert w.bias_report() == 1
    assert "不自动产生已验证回测证据" in capsys.readouterr().out
    history.write_text(json.dumps(dict(centre=20, evidence_status="pending_source_verification")),
                       encoding="utf-8")
    assert w.bias_report() == 0
    output = capsys.readouterr().out
    assert "当地日开始前完成采集" in output
    assert "verified" in output and "pending" in output
    assert "不会自动成为已验证回测证据" in output
    assert "当天要先扫过" not in output


def test_default_settle_does_not_save(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(w, "SAVE_RECORDS", False)
    monkeypatch.setattr(w, "discover", lambda *a, **k: [event()])
    monkeypatch.setattr(w, "local_day_finished", lambda *a: True)
    monkeypatch.setattr(w, "settle_daily", lambda *a: (20, 10))
    monkeypatch.setattr(w, "SETTLE_HISTORY", tmp_path / "data/settle.jsonl")
    monkeypatch.setattr(w, "FORECAST_HISTORY", tmp_path / "data/pred.jsonl")
    assert w.settle(date(2026, 1, 1)) == 0
    output = capsys.readouterr().out
    assert "未保存结算历史" in output and "已并入结算历史" not in output
    assert list(tmp_path.iterdir()) == []
