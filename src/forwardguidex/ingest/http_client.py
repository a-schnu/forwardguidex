"""Shared outbound HTTP client for external data providers.

Centralizes rate-limit-aware retry policy so every provider (GDELT, Yahoo,
UST, NY Fed, BIS, Federal Register, SEC EDGAR) gets the same treatment:

* connect / read timeouts;
* bounded retries only for transient conditions (429, 408, 5xx, timeouts);
* `Retry-After` support (delta-seconds or HTTP-date), capped;
* exponential backoff with full jitter and a hard elapsed-time cap;
* stable non-secret ``User-Agent`` identifying ForwardGuidex;
* structured error classification (never raises on 429 — returns a classified
  outcome so the caller can update per-source health).

Not a general-purpose retry decorator: this module is intentionally small and
returns a ``FetchResult`` dataclass so the caller can distinguish
``rate_limited`` from ``server_error`` from ``timeout`` etc. and roll them up
into ``meta.source_health`` in the snapshot.
"""
from __future__ import annotations

import email.utils
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

USER_AGENT = "ForwardGuidex/0.1 (+https://github.com/a-schnu/forwardguidex)"

# Bounded, conservative default policy. Individual providers can override any of
# these via keyword args on ``fetch_json`` (see ``ingest/news.py`` for GDELT).
#
# Note on ``DEFAULT_CONNECT_TIMEOUT``: GDELT's TLS handshake regularly takes 5-10 s
# from GitHub Actions runners; a 5 s connect timeout was too aggressive and caused
# every query to fail before the first HTTP response byte was received. urllib3 v2
# reports that as ``Read timed out. (read timeout=<connect_timeout>)``, which was
# doubly confusing. Keep this generous.
DEFAULT_ATTEMPTS = 3
DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_CAP = 30.0
DEFAULT_MAX_ELAPSED = 180.0
DEFAULT_RETRY_AFTER_CAP = 30.0

# HTTP statuses we consider transient (worth a bounded retry).
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# --- Soft throttling (HTTP 200 + non-JSON body) ------------------------------
#
# Some providers — GDELT above all — signal throttling / transient overload with
# HTTP *200* and a plain-text or HTML body instead of a proper 429. Treating that
# as a permanent parse error (as we did until 2026-08-28) silently drops queries:
# CI run 33116396414 lost the `mercati` and `difesa` topics to
# `class=parse status=200` and still shipped the snapshot as DEGRADED.
#
# `fetch_json(retry_on_soft_throttle=True)` re-classifies such a body as
# RATE_LIMITED so the normal bounded-retry + backoff path applies, EXCEPT when the
# body matches a permanent marker (a malformed query is not going to fix itself).
_SOFT_THROTTLE_MARKERS = (
    "rate limit",
    "ratelimit",
    "too many request",
    "too many queries",
    "please try again",
    "try again later",
    "please wait",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "overloaded",
    "busy",
    "maintenance",
)

# Permanent: the request itself is wrong. Retrying only burns the budget.
_PERMANENT_BODY_MARKERS = (
    "query was too short",
    "too short",
    "no valid search term",
    "specify a search term",
    "invalid query",
    "syntax error",
    "unrecognized",
    "not a valid",
)

# Content types we accept as "the provider at least intended to send JSON".
_JSONISH_CONTENT_TYPES = ("json", "+json", "javascript")

# How much of an unparseable body we keep for diagnostics. Bodies can be whole
# HTML error pages; we only need the first line or two to tell throttle from bug.
_BODY_SNIPPET_CHARS = 180

_log = logging.getLogger("forwardguidex.http")


class ErrorClass:
    """Structured error classification. Stable string constants for logs / snapshot."""

    OK = "ok"
    RATE_LIMITED = "rate_limited"       # HTTP 429
    SERVER_ERROR = "server_error"       # HTTP 5xx / 408 / 425
    CLIENT_ERROR = "client_error"       # HTTP 4xx (non-429) — permanent config bug
    TIMEOUT = "timeout"                 # socket timeout / read timeout
    NETWORK = "network"                 # DNS / connection reset / TLS
    PARSE = "parse"                     # non-JSON body when JSON expected
    UNKNOWN = "unknown"


@dataclass
class FetchResult:
    """Outcome of a single provider fetch (never raises for classified errors).

    Callers use this to update per-source-health counters (attempts / success /
    rate_limited / server_error / etc.) so ``meta.source_health`` and the
    validator can distinguish "provider outage" from "legitimate zero rows".
    """

    ok: bool
    status: int | None = None
    error_class: str = ErrorClass.OK
    error_detail: str = ""
    attempts: int = 0
    elapsed: float = 0.0
    data: Any = None
    # Number of retries that saw HTTP 429 specifically (for source_health rollup).
    rate_limited_attempts: int = 0


@dataclass
class _RetryState:
    attempts: int = 0
    rate_limited_attempts: int = 0
    started: float = field(default_factory=time.monotonic)


def _classify_status(status: int) -> str:
    if status == 429:
        return ErrorClass.RATE_LIMITED
    if 500 <= status < 600 or status in (408, 425):
        return ErrorClass.SERVER_ERROR
    if 400 <= status < 500:
        return ErrorClass.CLIENT_ERROR
    return ErrorClass.UNKNOWN


def _parse_retry_after(value: str | None, *, cap: float, now: datetime | None = None) -> float | None:
    """Parse the ``Retry-After`` header (delta-seconds or HTTP-date), clamped to cap.

    Returns ``None`` when the header is absent/unparseable.
    """
    if not value:
        return None
    v = value.strip()
    try:
        secs = float(v)
        return max(0.0, min(secs, cap))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(v)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        secs = (dt - now).total_seconds()
        if secs <= 0:
            return 0.0
        return min(secs, cap)
    except (TypeError, ValueError):
        return None


def _body_snippet(resp: Any) -> str:
    """First ``_BODY_SNIPPET_CHARS`` of the response body, collapsed to one line.

    Defensive: a response double (or a streamed body already consumed) may not
    expose ``.text``. Never raises — diagnostics must not break ingestion.
    """
    try:
        text = getattr(resp, "text", "") or ""
    except Exception:  # noqa: BLE001 - diagnostics must never break ingestion
        return ""
    return " ".join(str(text).split())[:_BODY_SNIPPET_CHARS]


def _classify_unparseable_body(resp: Any, snippet: str) -> tuple[str, str]:
    """Decide whether a 200 with a non-JSON body is transient or permanent.

    Returns ``(error_class, reason)``. ``error_class`` is
    :attr:`ErrorClass.RATE_LIMITED` for a soft throttle (retry it) or
    :attr:`ErrorClass.PARSE` for a body we should never retry.

    Precedence, most specific first:

    1. a permanent marker in the body wins outright (malformed query);
    2. a known throttle/overload marker => soft throttle;
    3. the provider did not even *claim* JSON in ``Content-Type`` => soft
       throttle (an HTML error page in front of a JSON API is an infrastructure
       blip far more often than a contract change), retries stay bounded;
    4. otherwise the provider claimed JSON and sent garbage => real parse bug.
    """
    low = snippet.lower()
    for marker in _PERMANENT_BODY_MARKERS:
        if marker in low:
            return ErrorClass.PARSE, f"permanent body marker {marker!r}"
    for marker in _SOFT_THROTTLE_MARKERS:
        if marker in low:
            return ErrorClass.RATE_LIMITED, f"soft-throttle marker {marker!r}"

    try:
        ctype = (getattr(resp, "headers", {}) or {}).get("Content-Type", "") or ""
    except Exception:  # noqa: BLE001 - diagnostics must never break ingestion
        ctype = ""
    ctype = str(ctype).lower()
    if not any(tok in ctype for tok in _JSONISH_CONTENT_TYPES):
        return ErrorClass.RATE_LIMITED, f"non-JSON Content-Type {ctype or '<absent>'!r}"
    return ErrorClass.PARSE, "declared JSON but body is unparseable"


def _backoff_delay(attempt: int, *, base: float, cap: float) -> float:
    """Exponential backoff with *equal* jitter, bounded by ``cap``.

    ``attempt`` is 1-indexed (attempt=1 => ceiling == base).

    AWS-style *full* jitter — ``uniform(0, ceiling)`` — was the original
    implementation, and against GDELT it was actively harmful: with
    ``base=1.0`` and 3 attempts the expected waits were ~0.5 s and ~1 s, i.e.
    we re-hit a provider that needs seconds of headroom almost immediately and
    burned the whole attempt budget inside two seconds. Equal jitter
    (``ceiling/2 + uniform(0, ceiling/2)``) keeps the decorrelation that
    matters for thundering herds while guaranteeing the wait actually grows.
    """
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    half = ceiling / 2.0
    # Retry jitter, not a secret: `random` is the right tool here.
    return half + random.uniform(0.0, half)  # noqa: S311


class HttpClient:
    """Thin wrapper around ``requests.Session`` that applies the retry policy.

    One instance can (and should) be shared across a provider's ingest calls in
    a single daily run — the session's connection pool amortizes TCP/TLS setup.
    """

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        session: requests.Session | None = None,
    ):
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", user_agent)

    def close(self) -> None:
        self._session.close()

    def fetch_json(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
        max_elapsed: float = DEFAULT_MAX_ELAPSED,
        retry_after_cap: float = DEFAULT_RETRY_AFTER_CAP,
        min_spacing: float = 0.0,
        retry_on_soft_throttle: bool = False,
        _sleep=time.sleep,
        _now=None,
    ) -> FetchResult:
        """GET ``url`` and parse the response as JSON, with retry policy.

        Retries only on ``_TRANSIENT_STATUS`` and timeout/network errors. A
        non-retryable 4xx (permanent client error like ``404 quote not found``)
        returns immediately with ``ErrorClass.CLIENT_ERROR`` and is NOT retried.

        A malformed JSON body is normally NOT retried (``ErrorClass.PARSE``):
        retrying an endpoint that consistently returns HTML behind a 200 wastes
        budget. Providers that signal throttling with ``200`` + a plain-text or
        HTML body (GDELT) should pass ``retry_on_soft_throttle=True``: the body
        is then sniffed by :func:`_classify_unparseable_body` and a throttle-like
        body is re-classified as ``ErrorClass.RATE_LIMITED`` and retried under
        the same bounded policy. A body matching a permanent marker (malformed
        query) still returns ``PARSE`` immediately.

        Either way the first ``_BODY_SNIPPET_CHARS`` of an unparseable body are
        recorded in ``error_detail`` — without them ``Expecting value: line 1
        column 1`` is undiagnosable.

        ``min_spacing`` (seconds) can be passed by callers who want to serialize
        their own back-to-back requests (see GDELT concurrency guidance).
        """
        if min_spacing > 0:
            _sleep(min_spacing)

        state = _RetryState()
        last_result = FetchResult(ok=False)

        def _finalize(result: FetchResult) -> FetchResult:
            """Stamp the shared retry counters onto whatever we are returning."""
            result.attempts = state.attempts
            result.rate_limited_attempts = state.rate_limited_attempts
            result.elapsed = time.monotonic() - state.started
            return result

        def _next_delay(preferred: float | None = None) -> float | None:
            """Sleep to apply before the next attempt, or ``None`` to stop.

            Stops when the attempt budget is exhausted or when sleeping would
            overrun ``max_elapsed`` — the single place both bounds are enforced.
            """
            if state.attempts >= attempts:
                return None
            delay = preferred if preferred is not None else _backoff_delay(
                state.attempts, base=backoff_base, cap=backoff_cap,
            )
            remaining = max_elapsed - (time.monotonic() - state.started)
            if delay >= remaining:
                return None
            return delay

        while True:
            state.attempts += 1
            elapsed = time.monotonic() - state.started
            if elapsed >= max_elapsed:
                last_result.elapsed = elapsed
                last_result.attempts = state.attempts - 1
                last_result.rate_limited_attempts = state.rate_limited_attempts
                if not last_result.error_class or last_result.error_class == ErrorClass.OK:
                    last_result.error_class = ErrorClass.TIMEOUT
                    last_result.error_detail = "max elapsed budget exceeded"
                return last_result

            retry_after: float | None = None

            try:
                resp = self._session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=(connect_timeout, read_timeout),
                )
            except requests.Timeout as exc:
                last_result = FetchResult(
                    ok=False,
                    error_class=ErrorClass.TIMEOUT,
                    error_detail=str(exc)[:200],
                )
            except requests.RequestException as exc:
                last_result = FetchResult(
                    ok=False,
                    error_class=ErrorClass.NETWORK,
                    error_detail=type(exc).__name__ + ": " + str(exc)[:200],
                )
            else:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        snippet = _body_snippet(resp)
                        if retry_on_soft_throttle:
                            err_class, reason = _classify_unparseable_body(resp, snippet)
                        else:
                            err_class, reason = ErrorClass.PARSE, "soft-throttle sniffing disabled"
                        last_result = FetchResult(
                            ok=False,
                            status=200,
                            error_class=err_class,
                            error_detail=f"{str(exc)[:80]} | {reason} | body={snippet!r}"[:400],
                            data=None,
                        )
                        if err_class == ErrorClass.PARSE:
                            return _finalize(last_result)
                        # Soft throttle: counts as a rate limit for source_health
                        # and follows the ordinary bounded-retry path below.
                        state.rate_limited_attempts += 1
                        retry_after = _parse_retry_after(
                            (getattr(resp, "headers", {}) or {}).get("Retry-After"),
                            cap=retry_after_cap,
                        )
                    else:
                        return _finalize(FetchResult(
                            ok=True,
                            status=200,
                            error_class=ErrorClass.OK,
                            data=data,
                        ))
                else:
                    status = resp.status_code
                    err_class = _classify_status(status)
                    last_result = FetchResult(
                        ok=False,
                        status=status,
                        error_class=err_class,
                        error_detail=f"HTTP {status}",
                    )
                    if status == 429:
                        state.rate_limited_attempts += 1

                    # Only the statuses we actually documented as transient get
                    # a retry. This used to test `err_class == CLIENT_ERROR`,
                    # which let permanent server-side statuses (501 Not
                    # Implemented, 505 Version Not Supported) and anything
                    # classified UNKNOWN burn the whole attempt budget while
                    # `_TRANSIENT_STATUS` sat unused next to a docstring that
                    # claimed it was the contract.
                    if status not in _TRANSIENT_STATUS:
                        return _finalize(last_result)

                    # Respect Retry-After if provided (429 or 503).
                    retry_after = _parse_retry_after(
                        resp.headers.get("Retry-After"), cap=retry_after_cap
                    )

            delay = _next_delay(retry_after)
            if delay is None:
                return _finalize(last_result)
            _log.info(
                "retryable %s from %s (attempt %d/%d, sleeping %.2fs)",
                last_result.error_class, url, state.attempts, attempts, delay,
            )
            _sleep(delay)
