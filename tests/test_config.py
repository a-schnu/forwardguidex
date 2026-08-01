"""Smoke tests that need no network or API keys."""
from forwardguidex.config import (
    all_price_tickers,
    load_sources,
    load_universe,
    ticker_dimension,
)


def test_universe_loads():
    u = load_universe()
    assert "sectors" in u
    assert "treasury_maturities" in u and "nyfed_rates" in u
    assert "fred_series" not in u  # FRED removed for compliance


def test_treasury_and_nyfed_series():
    u = load_universe()
    assert {m["series_id"] for m in u["treasury_maturities"]} >= {"UST2Y", "UST10Y", "UST30Y"}
    nf = {r["series_id"] for r in u["nyfed_rates"]}
    assert nf == {"EFFR", "SOFR"}


def test_tickers_deduped():
    tickers = all_price_tickers()
    assert len(tickers) == len(set(tickers))
    assert "NVDA" in tickers and "^GSPC" in tickers


def test_dimension_covers_sectors():
    dim = ticker_dimension()
    sectors = {r["sector_label"] for r in dim if r["sector_label"]}
    assert "Oil & Gas" in sectors and "Defense & Aerospace" in sectors


def test_sources_policy_shape():
    s = load_sources()
    assert s["deployment_mode"] in (
        "LOCAL_DEMO", "PRIVATE_PERSONAL", "PUBLIC_NONCOMMERCIAL", "PUBLIC_COMMERCIAL")
    assert set(s["sources"]) == {"us_treasury", "ny_fed", "yfinance", "gdelt"}
    for spec in s["sources"].values():
        assert spec["approval_status"] == "approved"
        assert spec.get("evidence_reference")
        assert spec.get("allowed_modes") and spec.get("review_expires_at")
