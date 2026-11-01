"""Permanent offline regression coverage for descriptive calibration evidence."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SPEC = importlib.util.spec_from_file_location(
    "pm_calibration", Path(__file__).parents[1] / "scripts" / "pm_calibration.py")
cal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cal)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), -0.1, 1.1, True, "0.5", None])
def test_invalid_probability(value):
    assert not cal.valid_probability(value)


@pytest.mark.parametrize("value", [0, 0.5, 1])
def test_valid_probability(value):
    assert cal.valid_probability(value)


def test_paired_sample_and_reliability(capsys):
    cal.score([{"p": 0, "price": None, "outcome": "yes"},
               {"p": 0.8, "price": 0.5, "outcome": "yes"},
               {"p": float("nan"), "price": 0.5, "outcome": "no"},
               {"p": 1, "price": 2, "outcome": "yes"},
               {"p": 0.5, "price": 0.5, "outcome": "void"}])
    text = capsys.readouterr().out
    assert "valid_p=3  excluded_p=1" in text
    assert "paired_n=1" in text
    assert "paired Brier(yours)=0.0400  Brier(market price)=0.2500" in text
    assert "skill=+84.0%" in text
    assert "[0.9,1.0] n=1 mean_p=1.0000 observed_yes=1.0000" in text
    assert "[0.1,0.2) n=0" in text
    assert "missing_or_invalid_temporal_evidence_n=3" in text
    assert "不存在 30 条即足够" in text
    assert "本工具不提供正式验证" in text


def test_zero_market_and_empty(capsys):
    cal.score([{"p": 0.8, "price": 1, "outcome": "yes"}])
    assert "skill=undefined" in capsys.readouterr().out
    cal.score([])
    assert "paired_n=0" in capsys.readouterr().out


def evidence():
    return dict(available_cutoff_at="2026-01-01T00:00:00Z",
                predicted_at="2026-01-01T01:00:00Z",
                logged_at="2026-01-01T01:00:01Z",
                resolved_at="2026-01-02T00:00:00Z")


def test_temporal_order():
    assert cal.temporal_evidence(evidence())
    assert not cal.temporal_evidence({})
    for field in evidence():
        row = evidence()
        row[field] = None
        assert not cal.temporal_evidence(row)
    row = evidence()
    row["available_cutoff_at"] = "2026-01-02T00:00:00Z"
    assert not cal.temporal_evidence(row)
    row = evidence()
    row["resolved_at"] = row["logged_at"]
    assert not cal.temporal_evidence(row)
    row = evidence()
    row["predicted_at"] = "2026-01-01T01:00:00"
    assert not cal.temporal_evidence(row)


def cli(monkeypatch, path, *args):
    monkeypatch.setattr(cal, "_safe_ledger", lambda value: value)
    monkeypatch.setattr(sys, "argv", ["pm_calibration", "--ledger", str(path), *args])
    return cal.main()


@pytest.mark.parametrize("option", ["--p", "--price"])
@pytest.mark.parametrize("value", ["nan", "inf", "1.1", "-0.1"])
def test_cli_rejects_invalid(monkeypatch, tmp_path, option, value):
    args = ["log", "--question", "test", "--p", "0.5", option, value]
    with pytest.raises(SystemExit) as exc:
        cli(monkeypatch, tmp_path / "ledger.jsonl", *args)
    assert exc.value.code == 2
    assert not (tmp_path / "ledger.jsonl").exists()


def test_cli_compatible_log_resolve_score(monkeypatch, tmp_path, capsys):
    path = tmp_path / "ledger.jsonl"
    assert cli(monkeypatch, path, "log", "--question", "test", "--p", "0.7") == 0
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["predicted_at"] == row["logged_at"]
    assert row["available_cutoff_at"] is None
    assert cli(monkeypatch, path, "resolve", "--id", row["id"], "--outcome", "yes") == 0
    assert cli(monkeypatch, path, "score") == 0
    assert "missing_or_invalid_temporal_evidence_n=1" in capsys.readouterr().out


def test_cli_records_explicit_evidence(monkeypatch, tmp_path):
    path = tmp_path / "ledger.jsonl"
    assert cli(monkeypatch, path, "log", "--question", "test", "--p", "0",
               "--price", "1", "--predicted-at", "2026-01-01T01:00:00+01:00",
               "--available-cutoff-at", "2025-12-31T23:00:00Z") == 0
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["predicted_at"] == "2026-01-01T00:00:00+00:00"
    assert row["available_cutoff_at"] == "2025-12-31T23:00:00+00:00"


@pytest.mark.parametrize("args", [
    ["--predicted-at", "2026-01-01T00:00:00"],
    ["--predicted-at", "2999-01-01T00:00:00Z"],
    ["--predicted-at", "2026-01-01T00:00:00Z", "--available-cutoff-at", "2026-01-02T00:00:00Z"],
])
def test_cli_rejects_temporal_inconsistency(monkeypatch, tmp_path, args):
    path = tmp_path / "ledger.jsonl"
    with pytest.raises(SystemExit):
        cli(monkeypatch, path, "log", "--question", "test", "--p", "0.5", *args)
    assert not path.exists()
