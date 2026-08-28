"""P0.1: news collection rolls up per-query outcomes into source_health.

Uses monkeypatch to swap the shared HTTP client so no real GDELT call is made.
Verifies the two must-not-regress cases from the brief:

* all queries fail with 429 -> status=FAILED, rows=0, and the snapshot
  validator refuses ``quality=OK``;
* a partial-failure day (some 429, some 200) -> status=DEGRADED with exact
  counts and the validator still accepts the snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

import pytest

from forwardguidex.ingest import http_client as httpc
from forwardguidex.ingest import news as newsmod
from forwardguidex.serve import snapshot as S
from forwardguidex.serve import validate as V

NOW = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


class _StubClient:
    def __init__(self, per_key):
        # per_key: {topic_key: FetchResult}
        self._per_key = per_key

    def fetch_json(self, url, *, params=None, **_kw):
        key = params["query"] if params else ""
        return self._per_key[key]

    def close(self):
        pass


def _make_result(*, ok, articles=0, status=200, err=httpc.ErrorClass.OK,
                 rate_limited=0):
    data = {"articles": [{"url": f"https://x/{i}"} for i in range(articles)]}
    return httpc.FetchResult(
        ok=ok,
        status=status,
        error_class=err,
        error_detail="" if ok else "HTTP " + str(status),
        attempts=1 + rate_limited,
        rate_limited_attempts=rate_limited,
        elapsed=0.01,
        data=data if ok else None,
    )


class _StubUniverse:
    QUERIES: ClassVar[list[dict]] = [
        {"key": "fed",    "query": "Q-FED"},
        {"key": "bce",    "query": "Q-BCE"},
        {"key": "petrol", "query": "Q-OIL"},
    ]

    @classmethod
    def load(cls):
        return {"gdelt_queries": cls.QUERIES}


def _install_stub(monkeypatch, per_key):
    def stub_ctor():
        stub = _StubClient(per_key)
        return stub

    monkeypatch.setattr(newsmod, "HttpClient", stub_ctor)
    monkeypatch.setattr(newsmod, "load_universe", _StubUniverse.load)
    # Skip DB writes; the report is what we care about here.
    monkeypatch.setattr(newsmod, "upsert", lambda con, table, df, keys: len(df))
    monkeypatch.setattr(newsmod, "_persist_health", lambda con, ts, r: None)


def test_all_429_report_is_failed(monkeypatch):
    per_key = {
        "Q-FED":  _make_result(ok=False, status=429, err=httpc.ErrorClass.RATE_LIMITED, rate_limited=4),
        "Q-BCE":  _make_result(ok=False, status=429, err=httpc.ErrorClass.RATE_LIMITED, rate_limited=4),
        "Q-OIL":  _make_result(ok=False, status=429, err=httpc.ErrorClass.RATE_LIMITED, rate_limited=4),
    }
    _install_stub(monkeypatch, per_key)
    r = newsmod.ingest_news_with_report(con=None)
    assert r.status == "FAILED"
    assert r.attempted_queries == 3
    assert r.successful_queries == 0
    assert r.failed_queries == 3
    assert r.rate_limited_queries == 3
    assert r.rows == 0


def test_partial_failure_is_degraded(monkeypatch):
    per_key = {
        "Q-FED":  _make_result(ok=True,  articles=5),
        "Q-BCE":  _make_result(ok=False, status=429, err=httpc.ErrorClass.RATE_LIMITED, rate_limited=4),
        "Q-OIL":  _make_result(ok=True,  articles=2),
    }
    _install_stub(monkeypatch, per_key)
    r = newsmod.ingest_news_with_report(con=None)
    assert r.status == "DEGRADED"
    assert r.successful_queries == 2
    assert r.failed_queries == 1
    assert r.rate_limited_queries == 1
    assert r.rows == 7  # 5 + 2 (stubbed upsert returns len)


def test_validator_rejects_ok_when_gdelt_failed():
    payload = S.demo_snapshot()
    payload["meta"]["source_health"] = {
        "gdelt": {
            "status": "FAILED",
            "attempted_queries": 10,
            "successful_queries": 0,
            "failed_queries": 10,
            "rate_limited_queries": 10,
            "rows": 0,
            "errors": [],
        }
    }
    payload["meta"]["quality"] = "OK"
    raw = S._canonical_bytes(payload)
    errs = V.validate_payload(payload, raw, mode="LOCAL_DEMO", now=NOW,
                              snap_path=Path("x.json"), manifest_path=None)
    assert any("source_health.gdelt" in e and "quality=OK" in e for e in errs), errs


def test_validator_accepts_degraded_with_rows():
    payload = S.demo_snapshot()
    payload["meta"]["source_health"] = {
        "gdelt": {
            "status": "DEGRADED",
            "attempted_queries": 10,
            "successful_queries": 7,
            "failed_queries": 3,
            "rate_limited_queries": 2,
            "rows": 40,
            "errors": [{"category": "fed", "class": "rate_limited", "status": 429, "attempts": 4}],
        }
    }
    payload["meta"]["quality"] = "DEGRADED"
    raw = S._canonical_bytes(payload)
    errs = V.validate_payload(payload, raw, mode="LOCAL_DEMO", now=NOW,
                              snap_path=Path("x.json"), manifest_path=None)
    assert not any("source_health" in e for e in errs), errs


def test_validator_rejects_fresh_when_stale_fallback():
    payload = S.demo_snapshot()
    payload["meta"]["source_health"] = {
        "gdelt": {"status": "STALE_FALLBACK", "rows": 5, "attempted_queries": 3,
                  "successful_queries": 0, "failed_queries": 3}
    }
    raw = S._canonical_bytes(payload)
    errs = V.validate_payload(payload, raw, mode="LOCAL_DEMO", now=NOW,
                              snap_path=Path("x.json"), manifest_path=None)
    assert any("STALE_FALLBACK" in e and "freshness=FRESH" in e for e in errs), errs


# ---------------------------------------------------------------------------
# Adaptive spacing + whole-domain budget (2026-08-28 hardening).
# ---------------------------------------------------------------------------

class _RecordingClient(_StubClient):
    """Stub client that records the ``min_spacing`` used for each query."""

    def __init__(self, per_key):
        super().__init__(per_key)
        self.spacings: list[float] = []

    def fetch_json(self, url, *, params=None, **kw):
        self.spacings.append(kw.get("min_spacing"))
        return super().fetch_json(url, params=params, **kw)


def _install_recording(monkeypatch, per_key):
    rec = _RecordingClient(per_key)
    monkeypatch.setattr(newsmod, "HttpClient", lambda: rec)
    monkeypatch.setattr(newsmod, "load_universe", _StubUniverse.load)
    monkeypatch.setattr(newsmod, "upsert", lambda con, table, df, keys: len(df))
    monkeypatch.setattr(newsmod, "_persist_health", lambda con, ts, r: None)
    return rec


def test_spacing_widens_after_throttle_and_recovers(monkeypatch):
    per_key = {
        "Q-FED": _make_result(ok=False, status=429, err=httpc.ErrorClass.RATE_LIMITED, rate_limited=3),
        "Q-BCE": _make_result(ok=True, articles=3),
        "Q-OIL": _make_result(ok=True, articles=1),
    }
    rec = _install_recording(monkeypatch, per_key)
    newsmod.ingest_news_with_report(con=None)

    base = newsmod.GDELT_MIN_SPACING_SEC
    assert rec.spacings[0] == base
    # throttled -> back off the *next* topic too
    assert rec.spacings[1] == pytest.approx(base * newsmod.GDELT_SPACING_BACKOFF)
    # clean -> narrow back, but never below the floor
    assert rec.spacings[2] == pytest.approx(
        max(base, base * newsmod.GDELT_SPACING_BACKOFF * newsmod.GDELT_SPACING_RECOVERY)
    )


def test_soft_throttle_counts_as_rate_limited_query(monkeypatch):
    """A 200 + non-JSON throttle must not be filed as a plain failure."""
    per_key = {
        "Q-FED": _make_result(ok=True, articles=2),
        "Q-BCE": httpc.FetchResult(
            ok=False, status=200, error_class=httpc.ErrorClass.RATE_LIMITED,
            error_detail="soft throttle", attempts=3, rate_limited_attempts=3,
        ),
        "Q-OIL": _make_result(ok=True, articles=1),
    }
    _install_stub(monkeypatch, per_key)
    r = newsmod.ingest_news_with_report(con=None)
    assert r.status == "DEGRADED"
    assert r.rate_limited_queries == 1
    assert r.failed_queries == 1


def test_news_budget_exhaustion_skips_remaining_topics(monkeypatch):
    """Topics we never reach are recorded, not silently dropped from the universe."""
    per_key = {
        "Q-FED": _make_result(ok=True, articles=4),
        "Q-BCE": _make_result(ok=True, articles=4),
        "Q-OIL": _make_result(ok=True, articles=4),
    }
    _install_stub(monkeypatch, per_key)

    # monotonic() calls: deadline setup, then once per topic.
    ticks = iter([0.0, 0.0, 999.0, 999.0])

    class _Clock:
        monotonic = staticmethod(lambda: next(ticks))

    monkeypatch.setattr(newsmod, "time", _Clock)
    r = newsmod.ingest_news_with_report(con=None)

    assert r.attempted_queries == 3
    assert r.successful_queries == 1
    assert r.failed_queries == 2
    assert [q.status for q in r.per_query] == ["ok", "skipped", "skipped"]
    assert r.status == "DEGRADED"
    # skipped topics surface in meta.source_health.gdelt.errors[]
    classes = {e["class"] for e in r.to_metadata()["errors"]}
    assert classes == {"skipped"}


def test_circuit_breaker_stops_asking_a_refusing_provider(monkeypatch):
    """1635 s for zero rows (2026-08-28 probe) must not be repeatable."""
    monkeypatch.setattr(newsmod, "GDELT_CONSECUTIVE_FAILURE_LIMIT", 2)
    reset = httpc.FetchResult(
        ok=False, status=None, error_class=httpc.ErrorClass.NETWORK,
        error_detail="ConnectionResetError", attempts=4,
    )
    per_key = {"Q-FED": reset, "Q-BCE": reset, "Q-OIL": reset}
    rec = _install_recording(monkeypatch, per_key)
    r = newsmod.ingest_news_with_report(con=None)

    # Third topic is never requested.
    assert len(rec.spacings) == 2
    assert [q.status for q in r.per_query] == ["network", "network", "skipped"]
    assert r.attempted_queries == 3
    assert r.failed_queries == 3
    assert r.status == "FAILED"


def test_a_success_resets_the_circuit_breaker(monkeypatch):
    monkeypatch.setattr(newsmod, "GDELT_CONSECUTIVE_FAILURE_LIMIT", 2)
    per_key = {
        "Q-FED": _make_result(ok=False, status=503, err=httpc.ErrorClass.SERVER_ERROR),
        "Q-BCE": _make_result(ok=True, articles=2),
        "Q-OIL": _make_result(ok=False, status=503, err=httpc.ErrorClass.SERVER_ERROR),
    }
    rec = _install_recording(monkeypatch, per_key)
    r = newsmod.ingest_news_with_report(con=None)
    assert len(rec.spacings) == 3          # breaker never opened
    assert r.status == "DEGRADED"
