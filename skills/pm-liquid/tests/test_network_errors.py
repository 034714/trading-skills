"""Offline transport contract; also shipped with the independent event skill."""
import io
import json
from pathlib import Path
import ssl
import sys
import urllib.error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pm_common as c


@pytest.mark.parametrize("status,tries", [(406, 1), (429, 3), (500, 3), (503, 3), (504, 3), (599, 3)])
def test_http_status_retry_bound_and_safe_summary(monkeypatch, status, tries):
    calls, sleeps = [], []
    url = "https://user:password@example.com/private-secret?api_key=query-secret"
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise urllib.error.HTTPError(url, status, "Cookie: cookie-secret " * 1000,
                                     {"Set-Cookie": "header-secret"}, io.BytesIO(b"body-secret"))
    monkeypatch.setattr(c.urllib.request, "urlopen", fail)
    monkeypatch.setattr(c.time, "sleep", sleeps.append)
    with pytest.raises(c.FetchError) as caught:
        c.http_json(url)
    error = caught.value
    assert error.status == status
    assert error.retryable is (status == 429 or status >= 500)
    assert len(calls) == tries and len(sleeps) == tries - 1
    assert all(call["context"] is c.tls_context() for call in calls)
    assert len(str(error)) <= 160 and "example.com" in str(error)
    assert not any(text in str(error) for text in
                   ("password", "secret", "Cookie", "api_key", "?", "/private"))
    assert error.__suppress_context__


def test_legacy_fetch_error_and_retry_metadata():
    error = c.FetchError("legacy")
    assert str(error) == "legacy" and error.status is None and error.retryable
    assert not c.FetchError("stop", status=406, retryable=False).retryable


def test_network_summary_never_echoes_exception(monkeypatch):
    sleeps = []
    monkeypatch.setattr(c.time, "sleep", sleeps.append)
    def fail(*a, **k):
        raise urllib.error.URLError("Authorization: Bearer secret " * 1000)
    monkeypatch.setattr(c.urllib.request, "urlopen", fail)
    with pytest.raises(c.FetchError) as caught:
        c.http_json("https://example.com/?token=secret", attempts=2)
    assert str(caught.value) == "network/read error from example.com"
    assert sleeps == [1.5]


def test_tls_failure_never_downgrades_or_retries(monkeypatch):
    calls, sleeps = [], []
    def fail(*a, **k):
        calls.append(k)
        raise urllib.error.URLError(ssl.SSLCertVerificationError("secret"))
    monkeypatch.setattr(c.urllib.request, "urlopen", fail)
    monkeypatch.setattr(c.time, "sleep", sleeps.append)
    with pytest.raises(c.FetchError) as caught:
        c.http_json("https://example.com")
    assert not caught.value.retryable and len(calls) == 1 and not sleeps
    assert calls[0]["context"].verify_mode == ssl.CERT_REQUIRED
    assert calls[0]["context"].check_hostname


def test_invalid_json_safe_no_retry(monkeypatch):
    class Reply(io.BytesIO):
        pass
    calls = []
    monkeypatch.setattr(c.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or Reply(b"secret invalid JSON"))
    with pytest.raises(c.FetchError) as caught:
        c.http_json("https://example.com/?secret=yes")
    assert not caught.value.retryable and len(calls) == 1
    assert str(caught.value) == "invalid JSON from example.com"


def test_zero_attempts_rejected():
    with pytest.raises(ValueError, match="attempts"):
        c.http_json("https://example.com", attempts=0)
