"""Upcoming earnings dates via yfinance -> raw_earnings.

yfinance's ``Ticker.get_earnings_dates`` returns a DataFrame indexed by the
earnings Timestamp with an "EPS Estimate" column (the label varies by version,
so it is matched defensively). We keep only sector CONSTITUENTS (role ``name``)
and bound the table to a small forward/backward window so it never grows without
limit. yfinance is flaky — every ticker is fetched inside its own try/except so
one failure can never abort the loop.

raw_earnings schema: ticker, name, earnings_date, eps_estimate, ingested_at
                     (keys: ticker, earnings_date).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from ..config import load_universe, ticker_dimension
from ..db import upsert

# Bound the persisted table: keep the next ~120 days and drop anything older
# than ~3 days so stale rows are pruned on each run.
_MAX_AHEAD_DAYS = 120
_MAX_BEHIND_DAYS = 3
_LIMIT = 8  # earnings rows requested per ticker


def _constituents(universe: dict) -> list[dict]:
    """Sector constituents (role ``name``) — the tickers we track earnings for."""
    return [d for d in ticker_dimension(universe) if d.get("role") == "name"]


def _find_eps_col(columns) -> object | None:
    """Locate the EPS-estimate column across yfinance label variants."""
    for c in columns:
        norm = str(c).lower().replace(" ", "").replace("_", "")
        if norm == "epsestimate":
            return c
    for c in columns:
        norm = str(c).lower()
        if "eps" in norm and "estimate" in norm:
            return c
    return None


def _num_or_none(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _to_date(v) -> date | None:
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    dm = getattr(v, "date", None)
    if callable(dm):
        return dm()
    if isinstance(v, date):
        return v
    return None


def ingest_earnings(con) -> int:
    """Fetch upcoming earnings dates for every constituent -> raw_earnings."""
    universe = load_universe()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    lo = now.date() - timedelta(days=_MAX_BEHIND_DAYS)
    hi = now.date() + timedelta(days=_MAX_AHEAD_DAYS)

    rows: list[dict] = []
    for c in _constituents(universe):
        t = c["ticker"]
        try:
            df = yf.Ticker(t).get_earnings_dates(limit=_LIMIT)
        except Exception as exc:  # noqa: BLE001
            print(f"[earnings] {t} failed: {exc}")
            continue
        if df is None or len(df) == 0:
            continue
        eps_col = _find_eps_col(df.columns)
        for idx, row in df.iterrows():
            edate = _to_date(idx)
            if edate is None or edate < lo or edate > hi:
                continue
            eps = _num_or_none(row[eps_col]) if eps_col is not None else None
            rows.append({
                "ticker": t,
                "name": c.get("name"),
                "earnings_date": edate,
                "eps_estimate": eps,
                "ingested_at": now,
            })
    if not rows:
        print("[earnings] no earnings dates fetched")
        return 0
    out = (pd.DataFrame(rows)
           .drop_duplicates(subset=["ticker", "earnings_date"], keep="last"))
    return upsert(con, "raw_earnings", out, keys=["ticker", "earnings_date"])
