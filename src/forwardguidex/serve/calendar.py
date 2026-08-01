"""Per-asset-class EOD freshness via exchange calendars.

Each asset class has its own calendar + tolerated lag (in completed sessions):

* equities / sectors  -> NYSE (XNYS)
* futures             -> CME equity (CME_Equity)
* UST par yields      -> SIFMA US bond calendar (published ~15:30 ET same day)
* EFFR / SOFR         -> SIFMA US, but reference the PRIOR business day and
                         publish next morning -> tolerate an extra session lag
* news (GDELT)        -> wall-clock age, no calendar

"Fresh" = the class's most-recent `as_of` is within the last `allowed_lag + 1`
completed sessions (a session is "completed" once its market_close has passed in
UTC — this handles weekends, holidays, early closes and DST automatically).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from .rights import normalize_source

FRESHNESS_RULE_VERSION = "1.0"

# Cheap module-level cache of calendar objects (import is lazy so tests that
# never touch a session rule don't pay for it).
_CAL_CACHE: dict = {}


@dataclass(frozen=True)
class FreshnessRule:
    name: str
    calendar: str | None            # market-calendars name, or None for wall-clock
    allowed_lag: int = 1            # tolerated completed-session lag (session cadence)
    max_age_hours: float = 48.0    # wall-clock cadence only


RULES: dict[str, FreshnessRule] = {
    "equities": FreshnessRule("equities", "XNYS", allowed_lag=1),
    "futures": FreshnessRule("futures", "CME_Equity", allowed_lag=1),
    "ust": FreshnessRule("ust", "SIFMA_US", allowed_lag=1),
    "nyfed": FreshnessRule("nyfed", "SIFMA_US", allowed_lag=2),
    "news": FreshnessRule("news", None, max_age_hours=48.0),
}


@dataclass
class ClassFreshness:
    asset_class: str
    status: str                    # FRESH | STALE | MISSING
    max_as_of: datetime | None = None
    min_as_of: datetime | None = None
    oldest_acceptable: date | None = None
    n_items: int = 0
    n_stale_items: int = 0
    detail: str = ""


@dataclass
class SnapshotFreshness:
    overall: str
    checked_at: str
    rule_version: str
    classes: list[ClassFreshness] = field(default_factory=list)


def _get_cal(name: str):
    if name not in _CAL_CACHE:
        import pandas_market_calendars as mcal

        _CAL_CACHE[name] = mcal.get_calendar(name)
    return _CAL_CACHE[name]


def parse_dt(value) -> datetime | None:
    """Parse ISO-8601, GDELT compact (YYYYMMDDTHHMMSSZ), or datetime/date to
    a tz-aware UTC datetime. Naive inputs are assumed UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        s = str(value).strip()
        dt = None
        # GDELT compact form, e.g. 20260731T120000Z
        if len(s) == 16 and s[8] == "T" and s.endswith("Z") and s[:8].isdigit():
            try:
                dt = datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                dt = None
        if dt is None:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def completed_sessions(cal_name: str, now: datetime, count: int) -> list[date]:
    """Session dates whose market_close <= now, most recent first (<= count)."""
    cal = _get_cal(cal_name)
    end = now.date()
    # window wide enough to cover `count` sessions across weekends + holidays
    start = end - timedelta(days=14 + count * 4)
    sched = cal.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    if sched.empty:
        return []
    closed = sched[sched["market_close"] <= pd.Timestamp(now)]
    days = [d.date() if hasattr(d, "date") else d for d in closed.index]
    return days[::-1][:count]


def _assess_session(rule: FreshnessRule, as_of_list: list[datetime],
                    now: datetime) -> ClassFreshness:
    sessions = completed_sessions(rule.calendar, now, rule.allowed_lag + 1)
    if not sessions:
        return ClassFreshness(rule.name, "STALE", detail="no completed sessions in window")
    oldest_ok = sessions[-1]
    max_as_of = max(as_of_list)
    min_as_of = min(as_of_list)
    n_stale = sum(1 for a in as_of_list if a.date() < oldest_ok)
    status = "FRESH" if max_as_of.date() >= oldest_ok else "STALE"
    return ClassFreshness(
        asset_class=rule.name, status=status, max_as_of=max_as_of, min_as_of=min_as_of,
        oldest_acceptable=oldest_ok, n_items=len(as_of_list), n_stale_items=n_stale,
        detail=f"oldest_acceptable={oldest_ok.isoformat()}",
    )


def _assess_wallclock(rule: FreshnessRule, as_of_list: list[datetime],
                      now: datetime) -> ClassFreshness:
    max_as_of = max(as_of_list)
    age_h = (now - max_as_of).total_seconds() / 3600.0
    status = "FRESH" if age_h <= rule.max_age_hours else "STALE"
    return ClassFreshness(
        asset_class=rule.name, status=status, max_as_of=max_as_of, min_as_of=min(as_of_list),
        n_items=len(as_of_list), detail=f"age={age_h:.1f}h max={rule.max_age_hours}h",
    )


def assess_class(asset_class: str, as_of_values: list, now: datetime) -> ClassFreshness:
    rule = RULES[asset_class]
    parsed = [d for d in (parse_dt(v) for v in as_of_values) if d is not None]
    if not parsed:
        return ClassFreshness(asset_class, "MISSING", detail="no parseable as_of")
    if rule.calendar is None:
        return _assess_wallclock(rule, parsed, now)
    return _assess_session(rule, parsed, now)


def collect_as_of(snapshot: dict) -> dict[str, list]:
    """Group per-item `as_of` timestamps by asset class."""
    out: dict[str, list] = {k: [] for k in RULES}
    for item in snapshot.get("indices", []) or []:
        out["equities"].append(item.get("as_of"))
    for sec in snapshot.get("sectors", []) or []:
        for item in (sec.get("etfs", []) or []) + (sec.get("constituents", []) or []):
            out["equities"].append(item.get("as_of"))
    for item in snapshot.get("futures", []) or []:
        out["futures"].append(item.get("as_of"))
    for r in snapshot.get("rates", []) or []:
        key = normalize_source(r.get("source"))
        if key == "us_treasury":
            out["ust"].append(r.get("as_of"))
        elif key == "ny_fed":
            out["nyfed"].append(r.get("as_of"))
    for h in snapshot.get("headlines", []) or []:
        out["news"].append(h.get("seendate"))
    return {k: [v for v in vs if v is not None] for k, vs in out.items()}


def assess_snapshot(snapshot: dict, now: datetime | None = None) -> SnapshotFreshness:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    grouped = collect_as_of(snapshot)
    classes: list[ClassFreshness] = []
    for cls, values in grouped.items():
        if not values:
            continue  # class absent from this snapshot -> not evaluated
        classes.append(assess_class(cls, values, now))
    overall = "FRESH" if classes and all(c.status == "FRESH" for c in classes) else "STALE"
    return SnapshotFreshness(
        overall=overall,
        checked_at=now.isoformat(),
        rule_version=FRESHNESS_RULE_VERSION,
        classes=classes,
    )
