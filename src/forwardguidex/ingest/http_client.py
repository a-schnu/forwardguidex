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


def _backoff_delay(attempt: int, *, base: float, cap: float) -> float:
    """Exponential backoff with full jitter, bounded by ``cap``.

    ``attempt`` is 1-indexed (attempt=1 => base*1..2). AWS-style full jitter.
    """
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)


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
        _sleep=time.sleep,
        _now=None,
    ) -> FetchResult:
        """GET ``url`` and parse the response as JSON, with retry policy.

        Retries only on ``_TRANSIENT_STATUS`` and timeout/network errors. A
        non-retryable 4xx (permanent client error like ``404 quote not found``)
        returns immediately with ``ErrorClass.CLIENT_ERROR`` and is NOT retried.

        A malformed JSON body is NOT retried (``ErrorClass.PARSE``): retrying an
        endpoint that consistently returns HTML behind a 200 wastes budget.

        ``min_spacing`` (seconds) can be passed by callers who want to serialize
        their own back-to-back requests (see GDELT concurrency guidance).
        """
        if min_spacing > 0:
            _sleep(min_spacing)

        state = _RetryState()
        last_result = FetchResult(ok=False)

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
                        last_result = FetchResult(
                            ok=False,
                            status=200,
                            error_class=ErrorClass.PARSE,
                            error_detail=str(exc)[:200],
                            data=None,
                        )
                        last_result.attempts = state.attempts
                        last_result.rate_limited_attempts = state.rate_limited_attempts
                        last_result.elapsed = time.monotonic() - state.started
                        return last_result
                    last_result = FetchResult(
                        ok=True,
                        status=200,
                        error_class=ErrorClass.OK,
                        data=data,
                    )
                    last_result.attempts = state.attempts
                    last_result.rate_limited_attempts = state.rate_limited_attempts
                    last_result.elapsed = time.monotonic() - state.started
                    return last_result

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

                # Non-retryable client error: return immediately.
                if err_class == ErrorClass.CLIENT_ERROR:
                    last_result.attempts = state.attempts
                    last_result.rate_limited_attempts = state.rate_limited_attempts
                    last_result.elapsed = time.monotonic() - state.started
                    return last_result

                # Respect Retry-After if provided (429 or 503).
                retry_after = _parse_retry_after(
                    resp.headers.get("Retry-After"), cap=retry_after_cap
                )
                delay = retry_after if retry_after is not None else _backoff_delay(
                    state.attempts, base=backoff_base, cap=backoff_cap,
                )

                if state.attempts >= attempts:
                    last_result.attempts = state.attempts
                    last_result.rate_limited_attempts = state.rate_limited_attempts
                    last_result.elapsed = time.monotonic() - state.started
                    return last_result

                remaining = max_elapsed - (time.monotonic() - state.started)
                if delay >= remaining:
                    last_result.attempts = state.attempts
                    last_result.rate_limited_attempts = state.rate_limited_attempts
                    last_result.elapsed = time.monotonic() - state.started
                    return last_result

                _log.info(
                    "retryable %s from %s (attempt %d/%d, sleeping %.2fs)",
                    err_class, url, state.attempts, attempts, delay,
                )
                _sleep(delay)
                continue

            # timeout / network branches share the retry decision
            if state.attempts >= attempts:
                last_result.attempts = state.attempts
                last_result.rate_limited_attempts = state.rate_limited_attempts
                last_result.elapsed = time.monotonic() - state.started
                return last_result
            delay = _backoff_delay(state.attempts, base=backoff_base, cap=backoff_cap)
            remaining = max_elapsed - (time.monotonic() - state.started)
            if delay >= remaining:
                last_result.attempts = state.attempts
                last_result.rate_limited_attempts = state.rate_limited_attempts
                last_result.elapsed = time.monotonic() - state.started
                return last_result
            _log.info(
                "retryable %s (attempt %d/%d, sleeping %.2fs)",
                last_result.error_class, state.attempts, attempts, delay,
            )
            _sleep(delay)
