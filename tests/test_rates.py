"""Rates ingest parsing (Treasury CSV + NY Fed JSON) with mocked HTTP."""
import duckdb
import pytest

from forwardguidex.ingest import rates

_TREASURY_CSV = (
    "Date,1 Mo,2 Yr,5 Yr,10 Yr,30 Yr\n"
    "07/31/2026,5.10,4.28,4.45,4.75,5.27\n"
    "07/30/2026,5.09,4.23,4.40,4.68,5.21\n"
)

_NYFED = {
    "effr": {"refRates": [
        {"type": "EFFR", "effectiveDate": "2026-07-30", "percentRate": 3.63},
        {"type": "EFFR", "effectiveDate": "2026-07-29", "percentRate": 3.62},
    ]},
    "sofr": {"refRates": [
        {"type": "SOFR", "effectiveDate": "2026-07-30", "percentRate": 3.65},
        {"type": "SOFR", "effectiveDate": "2026-07-29", "percentRate": 3.64},
    ]},
}


class _Resp:
    def __init__(self, *, text="", payload=None):
        self.text = text
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_get(url, **kw):
    if "treasury" in url:
        return _Resp(text=_TREASURY_CSV)
    if "newyorkfed" in url:
        rate = "effr" if "/effr/" in url else "sofr"
        return _Resp(payload=_NYFED[rate])
    raise AssertionError(f"unexpected url {url}")


@pytest.fixture
def con():
    return duckdb.connect(":memory:")


def test_ingest_rates_sources_and_counts(con, monkeypatch):
    monkeypatch.setattr(rates.requests, "get", _fake_get)
    n = rates.ingest_rates(con)
    assert n > 0
    df = con.execute("SELECT source, COUNT(*) c FROM raw_macro GROUP BY source ORDER BY source").df()
    counts = dict(zip(df["source"], df["c"]))
    assert counts["UST"] == 8      # 4 maturities x 2 dates (deduped across years)
    assert counts["NYFED"] == 4    # EFFR + SOFR, 2 dates each


def test_treasury_values_mapped(con, monkeypatch):
    monkeypatch.setattr(rates.requests, "get", _fake_get)
    rates.ingest_rates(con)
    row = con.execute(
        "SELECT value FROM raw_macro WHERE series_id='UST10Y' ORDER BY date DESC LIMIT 1"
    ).fetchone()
    assert row[0] == pytest.approx(4.75)


def test_nyfed_effr_latest(con, monkeypatch):
    monkeypatch.setattr(rates.requests, "get", _fake_get)
    rates.ingest_rates(con)
    row = con.execute(
        "SELECT value, source FROM raw_macro WHERE series_id='EFFR' ORDER BY date DESC LIMIT 1"
    ).fetchone()
    assert row[0] == pytest.approx(3.63) and row[1] == "NYFED"
