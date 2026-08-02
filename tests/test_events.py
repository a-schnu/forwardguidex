"""Pure derivation logic for the Phase-2 event sections (in-memory DuckDB).

No network: every test seeds raw tables via ``db.upsert`` and asserts on the
plain dicts produced by ``transform.events`` — mirroring the in-memory style of
``tests/test_snapshot.py::test_build_snapshot_orders_members_by_universe``.
"""
from datetime import date, datetime, timezone

import duckdb
import pandas as pd

from forwardguidex import config
from forwardguidex import db as D
from forwardguidex.transform import events


def _mem():
    con = duckdb.connect(":memory:")
    D.build_dim_ticker(con)
    return con


def _macro(series_id, pairs, source="BIS"):
    """Rows for raw_macro from (date_str, value) pairs."""
    return pd.DataFrame([
        {"series_id": series_id, "name": series_id,
         "date": pd.Timestamp(d), "value": v, "source": source}
        for d, v in pairs
    ])


# --------------------------------------------------------------------------- #
# cb_events — cut / hike / hold derivation
# --------------------------------------------------------------------------- #
def test_cb_events_derives_cut_with_change_bp_and_as_of():
    con = _mem()
    # steps 4.00 -> 4.00 -> 3.75 -> 3.75 : last change is the 25bp cut on 06-18
    D.upsert(con, "raw_macro", _macro("USFED", [
        ("2026-03-01", 4.00), ("2026-04-01", 4.00),
        ("2026-06-18", 3.75), ("2026-07-01", 3.75),
    ]), keys=["series_id", "date"])
    specs = [{"series_id": "USFED", "name": "Fed target", "area": "US", "bank": "Fed"}]
    got = events.cb_events(con, specs)
    assert len(got) == 1
    ev = got[0]
    assert ev["bank"] == "Fed"
    assert ev["series_id"] == "USFED"
    assert ev["direction"] == "cut"
    assert ev["change_bp"] == -25
    assert isinstance(ev["change_bp"], int)
    assert ev["as_of"] == "2026-06-18"
    assert ev["rate"] == 3.75
    assert ev["source"] == "BIS"


def test_cb_events_derives_hike():
    con = _mem()
    D.upsert(con, "raw_macro", _macro("BOEBR", [
        ("2026-01-01", 3.75), ("2026-05-02", 4.00), ("2026-06-02", 4.00),
    ]), keys=["series_id", "date"])
    got = events.cb_events(con, [
        {"series_id": "BOEBR", "name": "BoE", "area": "GB", "bank": "BoE"}])
    ev = got[0]
    assert ev["direction"] == "hike"
    assert ev["change_bp"] == 25
    assert ev["as_of"] == "2026-05-02"
    assert ev["rate"] == 4.00


def test_cb_events_flat_series_is_hold():
    con = _mem()
    D.upsert(con, "raw_macro", _macro("BOJPR", [
        ("2026-01-01", 0.50), ("2026-04-01", 0.50), ("2026-07-01", 0.50),
    ]), keys=["series_id", "date"])
    ev = events.cb_events(con, [
        {"series_id": "BOJPR", "name": "BoJ", "area": "JP", "bank": "BoJ"}])[0]
    assert ev["direction"] == "hold"
    assert ev["change_bp"] == 0
    assert ev["as_of"] is None
    assert ev["rate"] == 0.50


def test_cb_events_preserves_order_and_skips_missing():
    con = _mem()
    D.upsert(con, "raw_macro", _macro("USFED", [
        ("2026-03-01", 4.00), ("2026-06-18", 3.75)]), keys=["series_id", "date"])
    D.upsert(con, "raw_macro", _macro("BOEBR", [
        ("2026-01-01", 3.75), ("2026-05-02", 4.00)]), keys=["series_id", "date"])
    specs = [
        {"series_id": "BOEBR", "name": "BoE", "area": "GB", "bank": "BoE"},
        {"series_id": "NOROWS", "name": "None", "area": "ZZ", "bank": "Ghost"},
        {"series_id": "USFED", "name": "Fed", "area": "US", "bank": "Fed"},
    ]
    banks = [e["bank"] for e in events.cb_events(con, specs)]
    assert banks == ["BoE", "Fed"]  # ghost (no rows) omitted, input order kept


def test_cb_events_missing_table_returns_empty():
    con = duckdb.connect(":memory:")  # no raw_macro at all
    assert events.cb_events(con, [
        {"series_id": "USFED", "name": "Fed", "area": "US", "bank": "Fed"}]) == []


# --------------------------------------------------------------------------- #
# upcoming_earnings — windowing / sort / cap
# --------------------------------------------------------------------------- #
_NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _seed_earnings(con, rows):
    df = pd.DataFrame([
        {"ticker": t, "name": n, "earnings_date": d,
         "eps_estimate": e, "ingested_at": pd.Timestamp("2026-08-01")}
        for t, n, d, e in rows
    ])
    D.upsert(con, "raw_earnings", df, keys=["ticker", "earnings_date"])


def test_upcoming_earnings_windows_sorts_and_enriches():
    con = _mem()
    uni = config.load_universe()
    _seed_earnings(con, [
        ("AAPL", "Apple", date(2026, 8, 5), 1.42),
        ("NVDA", "Nvidia", date(2026, 8, 5), 0.75),   # same date -> ticker tie-break
        ("AMD", "AMD", date(2026, 8, 10), 0.91),
        ("MSFT", "Microsoft", date(2026, 8, 20), 3.10),
        ("MU", "Micron", date(2026, 7, 20), 1.10),     # before window -> dropped
        ("TSM", "TSMC", date(2026, 9, 15), 1.60),      # beyond +21d -> dropped
    ])
    got = events.upcoming_earnings(con, uni, _NOW, days=21)
    assert [(g["date"], g["ticker"]) for g in got] == [
        ("2026-08-05", "AAPL"), ("2026-08-05", "NVDA"),
        ("2026-08-10", "AMD"), ("2026-08-20", "MSFT"),
    ]
    aapl = got[0]
    assert aapl["name"] == "Apple"
    assert aapl["sector"] == "Tech Hardware / Semis"  # joined from the universe
    assert aapl["eps_estimate"] == 1.42
    assert aapl["source"] == "yfinance"


def test_upcoming_earnings_respects_limit():
    con = _mem()
    uni = config.load_universe()
    _seed_earnings(con, [
        ("AAPL", "Apple", date(2026, 8, 5), 1.0),
        ("NVDA", "Nvidia", date(2026, 8, 5), 1.0),
        ("AMD", "AMD", date(2026, 8, 10), 1.0),
    ])
    got = events.upcoming_earnings(con, uni, _NOW, days=21, limit=2)
    assert [g["ticker"] for g in got] == ["AAPL", "NVDA"]


def test_upcoming_earnings_missing_table_returns_empty():
    con = duckdb.connect(":memory:")
    assert events.upcoming_earnings(con, config.load_universe(), _NOW) == []


# --------------------------------------------------------------------------- #
# recent_triggers — https filter / sort desc / cap
# --------------------------------------------------------------------------- #
def _seed_triggers(con, rows):
    df = pd.DataFrame([
        {"kind": k, "ticker": tk, "title": ti, "date": d, "url": u,
         "topic": tp, "source": s, "ingested_at": pd.Timestamp("2026-08-01")}
        for k, tk, ti, d, u, tp, s in rows
    ])
    D.upsert(con, "raw_triggers", df, keys=["kind", "url"])


def test_recent_triggers_drops_non_https_and_sorts_desc():
    con = _mem()
    _seed_triggers(con, [
        ("executive_order", None, "EO older", "2026-07-10",
         "https://www.federalregister.gov/a", None, "federal_register"),
        ("sec_8k", "AAPL", "AAPL — 8-K", "2026-07-25",
         "https://www.sec.gov/x", "2.02", "sec_edgar"),
        ("executive_order", None, "EO newest", "2026-07-23",
         "https://www.federalregister.gov/b", None, "federal_register"),
        ("sec_8k", "MU", "MU — 8-K", "2026-07-30",
         "http://insecure.example/x", None, "sec_edgar"),  # non-https -> dropped
    ])
    got = events.recent_triggers(con)
    assert [g["date"] for g in got] == ["2026-07-25", "2026-07-23", "2026-07-10"]
    assert all(g["url"].startswith("https://") for g in got)
    assert "http://insecure.example/x" not in {g["url"] for g in got}
    sec = next(g for g in got if g["kind"] == "sec_8k")
    assert sec["ticker"] == "AAPL" and sec["topic"] == "2.02"


def test_recent_triggers_respects_limit():
    con = _mem()
    _seed_triggers(con, [
        ("executive_order", None, "a", "2026-07-10", "https://x/1", None, "federal_register"),
        ("executive_order", None, "b", "2026-07-20", "https://x/2", None, "federal_register"),
        ("executive_order", None, "c", "2026-07-30", "https://x/3", None, "federal_register"),
    ])
    got = events.recent_triggers(con, limit=2)
    assert [g["date"] for g in got] == ["2026-07-30", "2026-07-20"]


def test_recent_triggers_missing_table_returns_empty():
    con = duckdb.connect(":memory:")
    assert events.recent_triggers(con) == []
