"""Phase-2 event sections derived from the warehouse (pure reads, never raise).

Three derivations feed the snapshot's optional event sections:

* ``cb_events``        -> central-bank decisions from BIS policy-rate history.
* ``upcoming_earnings``-> yfinance earnings calendar in a forward window.
* ``recent_triggers``  -> executive orders + SEC 8-K catalysts.

Every read is guarded by ``table_exists`` so a section whose source has not been
ingested yet returns ``[]`` instead of raising — the daily export must never fail
because a Phase-2 table is absent. The lead's ``snapshot.py`` re-normalizes and
caps these lists (via its own ``_iso``/``_num``/``_str``), so returning plain,
JSON-safe dicts (ISO date strings / floats / None) here is sufficient.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd

from ..config import ticker_dimension
from ..db import table_exists


# --------------------------------------------------------------------------- #
# small JSON-safe coercions
# --------------------------------------------------------------------------- #
def _clean_str(v) -> str | None:
    """Return a non-empty string or None (guards pandas NaN / "nan" NULLs)."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v)
    return s if s and s.lower() != "nan" else None


def _num(v) -> float | None:
    """Coerce to a finite float or None."""
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
    """Coerce a Timestamp / datetime / date / ISO string / NaT to a date or None."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    # datetime / pandas.Timestamp expose a callable .date(); a plain date does not.
    dm = getattr(v, "date", None)
    if callable(dm):
        return dm()
    if isinstance(v, date):
        return v
    return None


def _date_str(v) -> str | None:
    d = _to_date(v)
    return d.isoformat() if d is not None else None


# --------------------------------------------------------------------------- #
# Section 1 — central-bank decisions
# --------------------------------------------------------------------------- #
def cb_events(con, cb_specs: list[dict]) -> list[dict]:
    """Most-recent policy decision per configured central bank (BIS history).

    For each spec ``{series_id, name, area, bank}`` read the BIS ``raw_macro``
    rows for that ``series_id`` ordered by date ascending, then find the LAST
    transition where the value differs from the previous distinct value:

    * ``as_of``      = date of the new (changed) value,
    * ``change_bp``  = ``round((new - old) * 100)`` as an int (+hike / -cut),
    * ``direction``  = ``"hike"`` if the value rose else ``"cut"``,
    * ``rate``       = the latest (max-date) value.

    With no transition in the available history the bank is reported as a hold
    (``direction="hold"``, ``change_bp=0``, ``as_of=None``, ``rate``=latest).
    A bank with no rows at all is omitted. Output order matches ``cb_specs``.
    """
    if not table_exists(con, "raw_macro"):
        return []
    out: list[dict] = []
    for spec in cb_specs:
        series_id = spec.get("series_id")
        df = con.execute(
            "SELECT date, value FROM raw_macro "
            "WHERE series_id = ? AND source = 'BIS' ORDER BY date ASC",
            [series_id],
        ).df()
        if df is None or len(df) == 0:
            continue  # no history for this bank -> omit
        values = df["value"].tolist()
        dates = df["date"].tolist()

        latest = _num(values[-1])
        last_change: tuple[float, float, object] | None = None
        prev = values[0]
        for i in range(1, len(values)):
            cur = values[i]
            if cur != prev:
                last_change = (prev, cur, dates[i])
                prev = cur

        if last_change is None:
            direction, change_bp, as_of = "hold", 0, None
        else:
            old, new, changed_on = last_change
            change_bp = round((float(new) - float(old)) * 100)
            direction = "hike" if float(new) > float(old) else "cut"
            as_of = _date_str(changed_on)

        out.append({
            "bank": _clean_str(spec.get("bank")),
            "series_id": _clean_str(series_id),
            "rate": latest,
            "change_bp": change_bp,
            "direction": direction,
            "as_of": as_of,
            "source": "BIS",
        })
    return out


# --------------------------------------------------------------------------- #
# Section 2 — upcoming earnings
# --------------------------------------------------------------------------- #
def upcoming_earnings(con, universe: dict, now: datetime,
                      days: int = 21, limit: int = 24) -> list[dict]:
    """Sector-constituent earnings dates in ``[now, now+days]``, sorted + capped.

    Rows come from ``raw_earnings``; name + sector are joined from the universe's
    constituents (role ``name``). Sorted by (date asc, ticker asc) and capped at
    ``limit``. Returns ``[]`` when the table is missing.
    """
    if not table_exists(con, "raw_earnings"):
        return []
    dim = {d["ticker"]: d for d in ticker_dimension(universe) if d.get("role") == "name"}
    lo = now.date()
    hi = lo + timedelta(days=days)
    df = con.execute(
        "SELECT ticker, name, earnings_date, eps_estimate FROM raw_earnings"
    ).df()
    out: list[dict] = []
    for rec in df.to_dict("records"):
        edate = _to_date(rec.get("earnings_date"))
        if edate is None or edate < lo or edate > hi:
            continue
        ticker = _clean_str(rec.get("ticker"))
        if ticker is None:
            continue
        info = dim.get(ticker, {})
        out.append({
            "ticker": ticker,
            "name": _clean_str(info.get("name")) or _clean_str(rec.get("name")) or ticker,
            "date": edate.isoformat(),
            "eps_estimate": _num(rec.get("eps_estimate")),
            "sector": _clean_str(info.get("sector_label")),
            "source": "yfinance",
        })
    out.sort(key=lambda x: (x["date"], x["ticker"]))
    return out[:limit]


# --------------------------------------------------------------------------- #
# Section 3 — catalysts / triggers
# --------------------------------------------------------------------------- #
def recent_triggers(con, limit: int = 16) -> list[dict]:
    """Recent catalysts (executive orders + SEC 8-K), https-only, newest first.

    Rows come from ``raw_triggers``; only ``https://`` URLs are kept (the
    frontend + validator reject anything else). Sorted by date descending and
    capped at ``limit``. Returns ``[]`` when the table is missing.
    """
    if not table_exists(con, "raw_triggers"):
        return []
    df = con.execute(
        "SELECT kind, ticker, title, date, url, topic, source FROM raw_triggers"
    ).df()
    out: list[dict] = []
    for rec in df.to_dict("records"):
        url = _clean_str(rec.get("url"))
        if not url or not url.startswith("https://"):
            continue
        out.append({
            "kind": _clean_str(rec.get("kind")),
            "title": _clean_str(rec.get("title")),
            "ticker": _clean_str(rec.get("ticker")),
            "date": _date_str(rec.get("date")),
            "url": url,
            "topic": _clean_str(rec.get("topic")),
            "source": _clean_str(rec.get("source")),
        })
    out.sort(key=lambda x: (x["date"] or ""), reverse=True)
    return out[:limit]
