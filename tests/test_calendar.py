"""Per-asset-class freshness + calendar edge cases."""
from datetime import datetime, timezone

from forwardguidex.serve import calendar as cal

NOW_SAT = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)      # Sat; latest session Fri 07-31
NOW_MON = datetime(2026, 8, 3, 11, 30, tzinfo=timezone.utc)    # Mon pre-open


def _snap(eq="2026-07-31T20:00:00+00:00", ust="2026-07-31", nyfed="2026-07-30",
          news="20260731T120000Z"):
    return {
        "indices": [{"as_of": eq}],
        "sectors": [{"etfs": [{"as_of": eq}], "constituents": [{"as_of": eq}]}],
        "futures": [{"as_of": eq}],
        "rates": [{"series_id": "UST10Y", "source": "UST", "as_of": ust},
                  {"series_id": "EFFR", "source": "NYFED", "as_of": nyfed}],
        "headlines": [{"seendate": news}],
    }


def test_parse_dt_forms():
    assert cal.parse_dt("20260731T120000Z").tzinfo is not None
    assert cal.parse_dt("2026-07-31").tzinfo is not None
    assert cal.parse_dt("2026-07-31T20:00:00+00:00").hour == 20
    assert cal.parse_dt(None) is None
    assert cal.parse_dt("not-a-date") is None


def test_fresh_snapshot():
    r = cal.assess_snapshot(_snap(), NOW_SAT)
    assert r.overall == "FRESH"
    assert {c.asset_class for c in r.classes} == {"equities", "futures", "ust", "nyfed", "news"}


def test_stale_equities():
    r = cal.assess_snapshot(_snap(eq="2026-07-01T20:00:00+00:00"), NOW_SAT)
    assert r.overall == "STALE"
    assert any(c.asset_class == "equities" and c.status == "STALE" for c in r.classes)


def test_stale_news_wallclock():
    r = cal.assess_snapshot(_snap(news="20260728T120000Z"), NOW_SAT)  # >48h old
    assert any(c.asset_class == "news" and c.status == "STALE" for c in r.classes)


def test_monday_preopen_friday_close_is_fresh():
    # weekend/pre-open edge: Friday's close must be fresh Monday morning
    r = cal.assess_snapshot(
        _snap(eq="2026-07-31T20:00:00+00:00", ust="2026-07-31", nyfed="2026-07-30",
              news="20260803T060000Z"), NOW_MON)
    assert r.overall == "FRESH"


def test_missing_class():
    r = cal.assess_class("equities", [], NOW_SAT)
    assert r.status == "MISSING"


def test_completed_sessions_skip_weekend():
    days = cal.completed_sessions("XNYS", NOW_SAT, 3)
    # Sat 08-01: most recent completed sessions are Fri/Thu/Wed, never Sat/Sun
    assert days[0].isoformat() == "2026-07-31"
    assert all(d.weekday() < 5 for d in days)
