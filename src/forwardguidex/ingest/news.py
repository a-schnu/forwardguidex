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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from ..config import load_universe
from ..db import upsert
from .http_client import ErrorClass, HttpClient

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT throttles aggressively and is *slow*. Measured directly against
# api.gdeltproject.org on 2026-08-28, one ArtList query at a time:
#
#   query 1 -> 200 in 25.9 s
#   query 2 -> 200 in 24.4 s   (6 s spacing)
#   query 3 -> ConnectionReset after 21.0 s
#
# So a full ArtList response routinely needs ~25 s. The previous
# ``GDELT_READ_TIMEOUT = 25.0`` sat exactly on that boundary, which is why CI run
# 33116396414 lost `fed` and `bce` to `class=timeout` on all three attempts: we
# were aborting responses that were about to arrive. Give the read phase real
# headroom (the per-query ``max_elapsed`` and ``GDELT_TOTAL_BUDGET_SEC`` below are
# what actually bound the cost) and keep the connect timeout tight, since the TLS
# handshake itself is fast.
#
# Throttling shows up in three different disguises — HTTP 429, a TCP reset
# (``ErrorClass.NETWORK``), and HTTP 200 with a non-JSON body
# (``retry_on_soft_throttle``) — so all three feed the adaptive spacing below.
GDELT_MIN_SPACING_SEC = 6.0
GDELT_ATTEMPTS = 4
# 20.0 s was the single biggest cause of lost topics. urllib3 v2 keeps the
# *connect* timeout on the socket until the first response byte arrives, and
# GDELT regularly thinks for 20-30 s before answering — so a slow-but-healthy
# query surfaced as `Read timed out. (read timeout=20.0)`, i.e. the connect
# value, which is why the previous diagnosis chased TLS handshakes. Verified
# 2026-08-28: connect=20 -> 0/10 topics; connect=45 -> topics answer in ~26 s.
GDELT_CONNECT_TIMEOUT = 45.0
GDELT_READ_TIMEOUT = 60.0
# Full-jitter backoff with base 1.0 s meant retries after ~0.5 s / ~1 s — far
# too fast for a provider that wants seconds of headroom. See
# `http_client._backoff_delay` (equal jitter as of 2026-08-28).
GDELT_BACKOFF_BASE = 5.0
GDELT_MAX_ELAPSED_SEC = 150.0

# Adaptive spacing: every throttle signal (HTTP 429 *or* the 200 + non-JSON soft
# throttle GDELT actually uses) widens the gap before the next topic query, and a
# clean run narrows it back. Fixed 1.5 s spacing was not enough on CI run
# 33116396414 — once GDELT started throttling we kept hammering at the same rate
# for the remaining topics.
GDELT_MAX_SPACING_SEC = 12.0
GDELT_SPACING_BACKOFF = 2.0     # multiplier applied after a throttled query
GDELT_SPACING_RECOVERY = 0.75   # multiplier applied after a clean query

# Whole-domain wall-clock budget. Worst case without it is
# len(queries) * GDELT_MAX_ELAPSED_SEC (10 * 150 s = 25 min) — nearly the whole
# `daily.build-validate-deploy` job budget, spent on news alone. Topics not
# reached are recorded as `skipped` failures so the health rollup stays honest.
GDELT_TOTAL_BUDGET_SEC = 600.0

# Circuit breaker. When GDELT refuses a client it refuses it for a while: the
# 2026-08-28 probe burned 1635 s across all 10 topics for zero rows, every one
# failing with a reset or a 429. Once this many topics have failed back-to-back
# under pressure, stop asking and leave the remaining wall-clock to the rest of
# the pipeline — the outcome is identical and the snapshot is FAILED either way.
GDELT_CONSECUTIVE_FAILURE_LIMIT = 4

# Query window and page size.
#
# `timespan="1d"` made every failed run a permanent hole: GDELT only serves the
# requested window, so yesterday's headlines cease to exist for us the moment a
# run fails (unlike prices/rates/earnings, whose providers serve history and can
# be re-fetched). A 3-day window lets the next successful run pick up the topics
# it missed — `upsert(keys=["topic", "url"])` already deduplicates, so a wider
# window costs nothing but response size.
#
# `maxrecords` has to grow with it: GDELT sorts DateDesc and truncates, so a 3-day
# window with the old 50-record page would return the same newest 50 as a 1-day
# window and back-fill exactly nothing. 150 = 3 x the old page; the documented
# ceiling is 250.
GDELT_TIMESPAN = "3d"
GDELT_MAXRECORDS = 150

# Failure classes that mean "the provider is under pressure" and should widen
# the spacing for the *next* topic, not just retry the current one.
_PRESSURE_CLASSES = frozenset({
    ErrorClass.RATE_LIMITED,
    ErrorClass.TIMEOUT,
    ErrorClass.NETWORK,
    ErrorClass.SERVER_ERROR,
})

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
    maxrecords: int = GDELT_MAXRECORDS,
    timespan: str = GDELT_TIMESPAN,
    spacing: float = GDELT_MIN_SPACING_SEC,
    max_elapsed: float | None = None,
) -> tuple[list[dict], QueryOutcome]:
    params = {
        "query": query, "mode": "ArtList", "format": "json",
        "maxrecords": maxrecords, "timespan": timespan, "sort": "DateDesc",
    }
    result = client.fetch_json(
        GDELT_URL,
        params=params,
        attempts=GDELT_ATTEMPTS,
        connect_timeout=GDELT_CONNECT_TIMEOUT,
        read_timeout=GDELT_READ_TIMEOUT,
        backoff_base=GDELT_BACKOFF_BASE,
        max_elapsed=min(
            GDELT_MAX_ELAPSED_SEC,
            max_elapsed if max_elapsed is not None else GDELT_MAX_ELAPSED_SEC,
        ),
        min_spacing=spacing,
        # GDELT answers a throttled request with HTTP 200 and a plain-text /
        # HTML body, never a 429. Without this the query is dropped as a
        # permanent `parse` error on the first attempt.
        retry_on_soft_throttle=True,
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
    spacing = GDELT_MIN_SPACING_SEC
    consecutive_pressure_failures = 0
    deadline = time.monotonic() + GDELT_TOTAL_BUDGET_SEC
    try:
        for item in load_universe().get("gdelt_queries", []):
            key, query = item["key"], item["query"]
            report.attempted_queries += 1

            remaining = deadline - time.monotonic()
            if consecutive_pressure_failures >= GDELT_CONSECUTIVE_FAILURE_LIMIT:
                _log.warning(
                    "[news] %s skipped: provider circuit breaker open after %d consecutive failures",
                    key, consecutive_pressure_failures,
                )
                report.per_query.append(QueryOutcome(
                    key=key, status="skipped", attempts=0,
                    error_detail=(
                        f"circuit breaker open after {consecutive_pressure_failures} "
                        "consecutive provider failures"
                    ),
                ))
                report.failed_queries += 1
                continue
            if remaining <= spacing:
                # Out of wall-clock budget: record the topic as skipped rather
                # than silently shrinking the universe, and keep going so the
                # counters in `raw_news_health` still add up.
                _log.warning("[news] %s skipped: news budget exhausted", key)
                report.per_query.append(QueryOutcome(
                    key=key, status="skipped", attempts=0,
                    error_detail=f"news budget of {GDELT_TOTAL_BUDGET_SEC:.0f}s exhausted",
                ))
                report.failed_queries += 1
                continue

            articles, outcome = _fetch_query(
                client, key, query, spacing=spacing, max_elapsed=remaining,
            )
            report.per_query.append(outcome)

            if outcome.status == ErrorClass.OK:
                report.successful_queries += 1
                report.last_success_at = now.isoformat()
                spacing = max(GDELT_MIN_SPACING_SEC, spacing * GDELT_SPACING_RECOVERY)
                consecutive_pressure_failures = 0
            else:
                report.failed_queries += 1
                throttled = (
                    outcome.rate_limited_attempts > 0
                    or outcome.status == ErrorClass.RATE_LIMITED
                )
                if throttled:
                    report.rate_limited_queries += 1
                if throttled or outcome.status in _PRESSURE_CLASSES:
                    # Back off the *next* topic too: GDELT throttles per client,
                    # not per query. A TCP reset or a timeout is the same signal
                    # as a 429 — it just arrives in a different disguise.
                    spacing = min(GDELT_MAX_SPACING_SEC, spacing * GDELT_SPACING_BACKOFF)
                    consecutive_pressure_failures += 1

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
