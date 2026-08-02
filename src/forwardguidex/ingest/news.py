"""News & geopolitics via the free GDELT DOC 2.0 API -> raw_news.

Uses the shared :mod:`forwardguidex.ingest.http_client` for bounded, jittered
retries + `Retry-After` support. Per-query attempts / successes / rate-limited
counts are captured in a :class:`NewsCollectionReport` and persisted to the
DuckDB warehouse table ``raw_news_health`` so :mod:`forwardguidex.serve.snapshot`
can surface ``meta.source_health.gdelt`` and the validator can distinguish a
transient rate-limit burst from a legitimate zero-result day.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from ..config import load_universe
from ..db import upsert
from .http_client import ErrorClass, HttpClient

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT throttles aggressively (documented ~10 QPM soft ceiling). We serialize
# our topic queries with a short spacing to avoid self-inflicted 429 bursts.
GDELT_MIN_SPACING_SEC = 1.5
GDELT_ATTEMPTS = 4
GDELT_READ_TIMEOUT = 20.0

_log = logging.getLogger(__name__)


@dataclass
class QueryOutcome:
    key: str
    status: str          # ErrorClass value (ok / rate_limited / server_error / ...)
    http_status: int | None = None
    attempts: int = 0
    rate_limited_attempts: int = 0
    articles: int = 0
    error_detail: str = ""


@dataclass
class NewsCollectionReport:
    """Provider-level health rollup emitted alongside the rows written.

    Consumed by :func:`forwardguidex.serve.snapshot.build_snapshot` to build
    ``meta.source_health.gdelt`` (status / attempted / successful / failed /
    rate_limited / errors[]) and to gate ``quality`` in the snapshot builder
    and validator.
    """

    attempted_queries: int = 0
    successful_queries: int = 0
    failed_queries: int = 0
    rate_limited_queries: int = 0
    rows: int = 0
    last_success_at: str | None = None
    per_query: list[QueryOutcome] = field(default_factory=list)

    @property
    def status(self) -> str:
        """Roll ``per_query`` into ``OK`` / ``DEGRADED`` / ``FAILED``.

        FAILED = every attempted query failed OR zero rows produced.
        DEGRADED = at least one failure but at least one success.
        OK = every query succeeded and produced (or was allowed to produce)
        headlines.
        """
        if self.attempted_queries == 0:
            return "FAILED"
        if self.successful_queries == 0 or self.rows == 0:
            return "FAILED"
        if self.failed_queries > 0:
            return "DEGRADED"
        return "OK"

    def to_metadata(self) -> dict:
        """Bounded, snapshot-safe representation for ``meta.source_health.gdelt``."""
        errors = []
        for q in self.per_query:
            if q.status == ErrorClass.OK:
                continue
            errors.append({
                "category": q.key,
                "class": q.status,
                "status": q.http_status,
                "attempts": q.attempts,
            })
            if len(errors) >= 20:
                break
        return {
            "status": self.status,
            "attempted_queries": self.attempted_queries,
            "successful_queries": self.successful_queries,
            "failed_queries": self.failed_queries,
            "rate_limited_queries": self.rate_limited_queries,
            "rows": self.rows,
            "last_success_at": self.last_success_at,
            "errors": errors,
        }


def _fetch_query(
    client: HttpClient,
    key: str,
    query: str,
    *,
    maxrecords: int = 50,
    timespan: str = "1d",
) -> tuple[list[dict], QueryOutcome]:
    params = {
        "query": query, "mode": "ArtList", "format": "json",
        "maxrecords": maxrecords, "timespan": timespan, "sort": "DateDesc",
    }
    result = client.fetch_json(
        GDELT_URL,
        params=params,
        attempts=GDELT_ATTEMPTS,
        read_timeout=GDELT_READ_TIMEOUT,
        min_spacing=GDELT_MIN_SPACING_SEC,
    )
    outcome = QueryOutcome(
        key=key,
        status=result.error_class,
        http_status=result.status,
        attempts=result.attempts,
        rate_limited_attempts=result.rate_limited_attempts,
        error_detail=result.error_detail,
    )
    if not result.ok:
        _log.warning(
            "[news] %s failed: class=%s status=%s attempts=%d detail=%s",
            key, result.error_class, result.status, result.attempts, result.error_detail,
        )
        return [], outcome
    body = result.data if isinstance(result.data, dict) else {}
    articles = body.get("articles") or []
    outcome.articles = len(articles)
    return articles, outcome


def ingest_news(con) -> int:
    """Legacy return: number of rows inserted (for CLI print).

    Health details are persisted separately via ``raw_news_health`` (see below).
    """
    report = ingest_news_with_report(con)
    return report.rows


def ingest_news_with_report(con) -> NewsCollectionReport:
    now = datetime.now(timezone.utc)
    now_naive = now.replace(tzinfo=None)
    report = NewsCollectionReport()
    rows: list[dict] = []

    client = HttpClient()
    try:
        for item in load_universe().get("gdelt_queries", []):
            key, query = item["key"], item["query"]
            report.attempted_queries += 1
            articles, outcome = _fetch_query(client, key, query)
            report.per_query.append(outcome)

            if outcome.status == ErrorClass.OK:
                report.successful_queries += 1
                report.last_success_at = now.isoformat()
            else:
                report.failed_queries += 1
                if outcome.rate_limited_attempts > 0:
                    report.rate_limited_queries += 1

            for a in articles:
                rows.append({
                    "topic": key,
                    "url": a.get("url"),
                    "title": a.get("title"),
                    "domain": a.get("domain"),
                    "seendate": a.get("seendate"),
                    "sourcecountry": a.get("sourcecountry"),
                    "language": a.get("language"),
                    "ingested_at": now_naive,
                })
    finally:
        client.close()

    if rows:
        df = (pd.DataFrame(rows)
              .dropna(subset=["url"])
              .drop_duplicates(subset=["topic", "url"]))
        report.rows = upsert(con, "raw_news", df, keys=["topic", "url"])
    else:
        report.rows = 0

    _persist_health(con, now_naive, report)
    return report


def _persist_health(con, ingested_at: datetime, report: NewsCollectionReport) -> None:
    """Write one row per collection run into ``raw_news_health`` for auditing.

    Snapshot builder reads the latest row via :func:`latest_news_health`.
    """
    row = {
        "ingested_at": ingested_at,
        "status": report.status,
        "attempted_queries": report.attempted_queries,
        "successful_queries": report.successful_queries,
        "failed_queries": report.failed_queries,
        "rate_limited_queries": report.rate_limited_queries,
        "rows": report.rows,
        "last_success_at": report.last_success_at,
    }
    df = pd.DataFrame([row])
    upsert(con, "raw_news_health", df, keys=["ingested_at"])


def latest_news_health(con) -> dict | None:
    """Return the most recent ``raw_news_health`` row as a dict, or None.

    Consumed by :func:`forwardguidex.serve.snapshot.build_snapshot` to fill
    ``meta.source_health.gdelt``. Returns ``None`` when the table is absent
    (fresh warehouse, dev-only path) so the export stays fail-open on shape.
    """
    from ..db import table_exists

    if not table_exists(con, "raw_news_health"):
        return None
    row = con.execute(
        """
        SELECT status, attempted_queries, successful_queries, failed_queries,
               rate_limited_queries, rows, last_success_at, ingested_at
        FROM raw_news_health
        ORDER BY ingested_at DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    status, attempted, success, failed, rate_limited, rows, last_success, ingested = row
    return {
        "status": status,
        "attempted_queries": int(attempted or 0),
        "successful_queries": int(success or 0),
        "failed_queries": int(failed or 0),
        "rate_limited_queries": int(rate_limited or 0),
        "rows": int(rows or 0),
        "last_success_at": last_success.isoformat() if hasattr(last_success, "isoformat") else last_success,
        "checked_at": ingested.isoformat() if hasattr(ingested, "isoformat") else ingested,
        "errors": [],
    }
