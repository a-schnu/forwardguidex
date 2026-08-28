"""Bounded retry / rate-limit tests for the shared HTTP client.

Covers the P0.1 acceptance cases from the remediation brief:

1. 429 then 200: retries with bounded delay and succeeds.
2. 429 with Retry-After: respects the header within the configured cap.
3. all attempts return 429: outcome is rate_limited; caller can classify FAILED.
4. permanent 4xx (404): NOT retried; classified client_error.
5. provider timeout: transient; retries are bounded.
6. malformed JSON: classified parse; no unbounded retry.
"""
from __future__ import annotations

import requests

from forwardguidex.ingest.http_client import ErrorClass, HttpClient


class _FakeResp:
    def __init__(self, *, status: int, json_body=None, raise_json=False, retry_after=None,
                 text: str = "", content_type: str | None = None):
        self.status_code = status
        self._json = json_body
        self._raise_json = raise_json
        self.text = text
        self.headers = {}
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after
        if content_type is not None:
            self.headers["Content-Type"] = content_type

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._json


class _FakeSession:
    def __init__(self, responses):
        # `responses` is a list of _FakeResp instances OR exception instances
        # to raise on that call.
        self._responses = list(responses)
        self.calls = 0
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


def _client(responses):
    session = _FakeSession(responses)
    return HttpClient(session=session), session


def _no_sleep(_seconds):
    return None


def test_429_then_200_succeeds():
    client, session = _client([
        _FakeResp(status=429),
        _FakeResp(status=200, json_body={"articles": [{"url": "https://x"}]}),
    ])
    r = client.fetch_json("https://api.example/x", attempts=3, backoff_base=0.01,
                          backoff_cap=0.02, max_elapsed=5, _sleep=_no_sleep)
    assert r.ok
    assert r.status == 200
    assert r.error_class == ErrorClass.OK
    assert r.attempts == 2
    assert r.rate_limited_attempts == 1
    assert session.calls == 2


def test_429_with_retry_after_header_respected(monkeypatch):
    seen_delays = []

    def spy_sleep(secs):
        seen_delays.append(secs)

    client, _session = _client([
        _FakeResp(status=429, retry_after="1"),
        _FakeResp(status=200, json_body={}),
    ])
    r = client.fetch_json("https://api.example/x", attempts=3, backoff_base=100,
                          backoff_cap=100, retry_after_cap=5, _sleep=spy_sleep)
    assert r.ok
    # Retry-After of "1" must beat the huge backoff base (proves the header wins).
    assert seen_delays[0] == 1.0


def test_all_429_classified_rate_limited():
    client, _ = _client([_FakeResp(status=429)] * 4)
    r = client.fetch_json("https://api.example/x", attempts=4, backoff_base=0.001,
                          backoff_cap=0.002, max_elapsed=5, _sleep=_no_sleep)
    assert not r.ok
    assert r.error_class == ErrorClass.RATE_LIMITED
    assert r.status == 429
    assert r.attempts == 4
    assert r.rate_limited_attempts == 4


def test_permanent_4xx_not_retried():
    client, session = _client([_FakeResp(status=404, json_body=None)])
    r = client.fetch_json("https://api.example/missing", attempts=5, backoff_base=0.001,
                          _sleep=_no_sleep)
    assert not r.ok
    assert r.error_class == ErrorClass.CLIENT_ERROR
    assert r.status == 404
    assert session.calls == 1  # NOT retried


def test_timeout_is_retried_bounded():
    client, session = _client([
        requests.Timeout("read timeout"),
        _FakeResp(status=200, json_body={}),
    ])
    r = client.fetch_json("https://api.example/slow", attempts=3, backoff_base=0.001,
                          backoff_cap=0.002, max_elapsed=5, _sleep=_no_sleep)
    assert r.ok
    assert session.calls == 2


def test_malformed_json_classified_parse_no_retry():
    client, session = _client([_FakeResp(status=200, raise_json=True)])
    r = client.fetch_json("https://api.example/junk", attempts=5, _sleep=_no_sleep)
    assert not r.ok
    assert r.error_class == ErrorClass.PARSE
    assert session.calls == 1


def test_max_elapsed_short_circuits():
    # Retry-After header claims a delay larger than remaining budget -> no retry.
    client, session = _client([_FakeResp(status=429, retry_after="60")])
    r = client.fetch_json("https://api.example/x", attempts=5, max_elapsed=0.1,
                          retry_after_cap=60, _sleep=_no_sleep)
    assert not r.ok
    assert r.error_class == ErrorClass.RATE_LIMITED
    assert session.calls == 1


def test_network_error_retried():
    client, session = _client([
        requests.ConnectionError("dns fail"),
        _FakeResp(status=200, json_body={}),
    ])
    r = client.fetch_json("https://api.example/x", attempts=3, backoff_base=0.001,
                          backoff_cap=0.002, max_elapsed=5, _sleep=_no_sleep)
    assert r.ok
    assert session.calls == 2


# ---------------------------------------------------------------------------
# Soft throttling: HTTP 200 + non-JSON body (the way GDELT actually throttles).
#
# Regression guard for CI run 33116396414, where `mercati` and `difesa` were
# dropped as permanent `class=parse status=200` on the first attempt and the
# snapshot still shipped as DEGRADED.
# ---------------------------------------------------------------------------

_THROTTLE_BODY = "Your query was rate limited, please try again shortly."


def test_soft_throttle_200_is_retried_and_succeeds():
    client, session = _client([
        _FakeResp(status=200, raise_json=True, text=_THROTTLE_BODY,
                  content_type="text/plain; charset=utf-8"),
        _FakeResp(status=200, json_body={"articles": [1, 2]}),
    ])
    res = client.fetch_json("https://api.example/doc", retry_on_soft_throttle=True,
                            _sleep=_no_sleep)
    assert res.ok
    assert res.data == {"articles": [1, 2]}
    assert session.calls == 2
    assert res.attempts == 2
    # A soft throttle must be counted as a rate limit for source_health.
    assert res.rate_limited_attempts == 1


def test_soft_throttle_exhausts_attempts_and_reports_rate_limited():
    client, session = _client([
        _FakeResp(status=200, raise_json=True, text=_THROTTLE_BODY, content_type="text/html")
        for _ in range(3)
    ])
    res = client.fetch_json("https://api.example/doc", attempts=3,
                            retry_on_soft_throttle=True, _sleep=_no_sleep)
    assert not res.ok
    assert res.error_class == ErrorClass.RATE_LIMITED
    assert res.status == 200
    assert session.calls == 3
    assert res.rate_limited_attempts == 3


def test_soft_throttle_honours_retry_after():
    slept: list[float] = []
    client, _ = _client([
        _FakeResp(status=200, raise_json=True, text=_THROTTLE_BODY,
                  content_type="text/plain", retry_after="7"),
        _FakeResp(status=200, json_body={"ok": True}),
    ])
    res = client.fetch_json("https://api.example/doc", retry_on_soft_throttle=True,
                            _sleep=slept.append)
    assert res.ok
    assert slept == [7.0]


def test_permanent_body_marker_is_not_retried():
    """A malformed query never fixes itself — do not spend the retry budget."""
    client, session = _client([
        _FakeResp(status=200, raise_json=True,
                  text="Your query was too short. Please specify a search term.",
                  content_type="text/plain"),
    ])
    res = client.fetch_json("https://api.example/doc", retry_on_soft_throttle=True,
                            _sleep=_no_sleep)
    assert not res.ok
    assert res.error_class == ErrorClass.PARSE
    assert session.calls == 1


def test_declared_json_but_garbage_body_is_parse_not_throttle():
    """Content-Type says JSON: that is a contract bug, not an infra blip."""
    client, session = _client([
        _FakeResp(status=200, raise_json=True, text="{unclosed",
                  content_type="application/json"),
    ])
    res = client.fetch_json("https://api.example/doc", retry_on_soft_throttle=True,
                            _sleep=_no_sleep)
    assert not res.ok
    assert res.error_class == ErrorClass.PARSE
    assert session.calls == 1


def test_unparseable_body_snippet_is_captured_for_diagnosis():
    """`Expecting value: line 1 column 1` alone is undiagnosable."""
    client, _ = _client([
        _FakeResp(status=200, raise_json=True, text="<html><body>502 Bad Gateway</body></html>",
                  content_type="text/html"),
    ])
    res = client.fetch_json("https://api.example/doc", _sleep=_no_sleep)
    assert res.error_class == ErrorClass.PARSE       # sniffing off by default
    assert "502 Bad Gateway" in res.error_detail


def test_soft_throttle_sniffing_is_opt_in():
    """Providers that never soft-throttle keep the cheap fail-fast behaviour."""
    client, session = _client([
        _FakeResp(status=200, raise_json=True, text=_THROTTLE_BODY, content_type="text/plain"),
    ])
    res = client.fetch_json("https://api.example/doc", _sleep=_no_sleep)
    assert res.error_class == ErrorClass.PARSE
    assert session.calls == 1


def test_permanent_server_status_is_not_retried():
    """501/505 are permanent: `_TRANSIENT_STATUS` is the retry contract, not 5xx."""
    client, session = _client([_FakeResp(status=501)])
    res = client.fetch_json("https://api.example/x", attempts=4, _sleep=_no_sleep)
    assert not res.ok
    assert res.error_class == ErrorClass.SERVER_ERROR
    assert session.calls == 1


def test_transient_server_status_is_retried():
    client, session = _client([
        _FakeResp(status=503),
        _FakeResp(status=200, json_body={"ok": 1}),
    ])
    res = client.fetch_json("https://api.example/x", attempts=4, backoff_base=0.001,
                            backoff_cap=0.002, _sleep=_no_sleep)
    assert res.ok
    assert session.calls == 2
