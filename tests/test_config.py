"""Smoke tests that need no network or API keys."""
from forwardguidex.config import (
    all_price_tickers,
    load_sources,
    load_universe,
    normalize_entry,
    ticker_dimension,
)


def test_universe_loads():
    u = load_universe()
    assert "sectors" in u
    assert "treasury_maturities" in u and "nyfed_rates" in u
    assert "etfs" in u and "cb_policy_rates" in u
    assert "fred_series" not in u  # FRED removed for compliance


def test_treasury_and_nyfed_series():
    u = load_universe()
    assert {m["series_id"] for m in u["treasury_maturities"]} >= {"UST2Y", "UST10Y", "UST30Y"}
    nf = {r["series_id"] for r in u["nyfed_rates"]}
    assert nf == {"EFFR", "SOFR"}


def test_cb_policy_rates_config():
    u = load_universe()
    cb = {r["series_id"]: r["area"] for r in u["cb_policy_rates"]}
    # Fed (US via BIS WS_CBPOL) added for the central-bank decisions view.
    assert cb == {"USFED": "US", "ECBDFR": "XM", "BOEBR": "GB",
                  "BOJPR": "JP", "PBOCLPR1Y": "CN"}
    # every bank carries a short display label used by the decisions section
    banks = {r["series_id"]: r["bank"] for r in u["cb_policy_rates"]}
    assert banks == {"USFED": "Fed", "ECBDFR": "BCE", "BOEBR": "BoE",
                     "BOJPR": "BoJ", "PBOCLPR1Y": "PBoC"}


def test_crypto_in_universe_and_dimension():
    u = load_universe()
    assert "crypto" in u
    assert {c["ticker"] for c in u["crypto"]} == {
        "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD"}
    dim = {r["ticker"]: r for r in ticker_dimension()}
    assert dim["BTC-USD"]["role"] == "crypto" and dim["BTC-USD"]["ccy"] == "USD"
    assert "BTC-USD" in all_price_tickers()


def test_new_futures_and_etfs_in_universe():
    t = set(all_price_tickers())
    assert {"ZN=F", "ZB=F", "PA=F", "RB=F", "6B=F"} <= t
    assert {"SPY", "QQQ", "VTI", "IWM", "GLD", "SLV", "TLT", "HYG", "LQD", "AGG", "EEM", "EFA"} <= t
    dim = {r["ticker"]: r for r in ticker_dimension()}
    assert dim["ZN=F"]["role"] == "future" and dim["SPY"]["role"] == "fund"


def test_semis_apple_first_in_universe():
    u = load_universe()
    names = [normalize_entry(x)["ticker"] for x in u["sectors"]["semis"]["names"]]
    assert names[0] == "AAPL"
    assert set(names) == {"AAPL", "NVDA", "AMD", "TSM", "AVGO", "ASML", "MU", "IFX.DE", "DELL"}


def test_fx_eur_role_and_included():
    u = load_universe()
    assert "fx_eur" in u and "EURUSD=X" in u["fx_eur"]
    dim = {r["ticker"]: r for r in ticker_dimension()}
    assert dim["EURJPY=X"]["role"] == "fx"  # conversion source, never rendered
    assert "EURUSD=X" in all_price_tickers()


def test_sector_etfs_carry_names_and_ccy():
    """Sector `etfs:` accept the {ticker,name,ccy} form (European UCITS)."""
    dim = {r["ticker"]: r for r in ticker_dimension()}
    assert dim["EXH1.DE"]["role"] == "etf" and dim["EXH1.DE"]["ccy"] == "EUR"
    assert dim["EXH1.DE"]["name"] == "iShares STOXX Europe 600 Oil & Gas"
    assert dim["DFEN.DE"]["ccy"] == "EUR" and dim["NATO.L"]["ccy"] == "USD"
    # existing US sector ETFs now carry real names too
    assert dim["XLE"]["name"] == "Energy Select Sector SPDR"


def test_tickers_deduped():
    tickers = all_price_tickers()
    assert len(tickers) == len(set(tickers))
    assert "NVDA" in tickers and "^GSPC" in tickers
    # extended universe + standalone ETFs are pulled too
    assert "^N225" in tickers and "ILF" in tickers and "GLEN.L" in tickers


def test_normalize_entry_bare_and_mapping():
    # bare string -> USD default, no name
    assert normalize_entry("NVDA") == {"ticker": "NVDA", "name": None, "ccy": "USD"}
    # mapping -> honoured, ccy defaults to USD when absent
    assert normalize_entry({"ticker": "AAPL", "name": "Apple"}) == {
        "ticker": "AAPL", "name": "Apple", "ccy": "USD"}
    assert normalize_entry({"ticker": "IFX.DE", "name": "Infineon", "ccy": "EUR"})["ccy"] == "EUR"


def test_dimension_covers_sectors():
    dim = ticker_dimension()
    sectors = {r["sector_label"] for r in dim if r["sector_label"]}
    assert "Oil & Gas" in sectors and "Defense & Aerospace" in sectors
    # new sectors present with real labels
    assert {"Internet & Platforms", "Banks & Financials", "Healthcare & Pharma",
            "Materials & Mining", "Utilities & Power"} <= sectors


def test_dimension_threads_name_and_ccy():
    dim = {r["ticker"]: r for r in ticker_dimension()}
    # foreign constituent carries its real name + local currency
    assert dim["IFX.DE"]["name"] == "Infineon" and dim["IFX.DE"]["ccy"] == "EUR"
    assert dim["GLEN.L"]["ccy"] == "GBP"
    # US index defaults to USD; European index is EUR
    assert dim["^GSPC"]["ccy"] == "USD"
    assert dim["^GDAXI"]["ccy"] == "EUR" and dim["^GDAXI"]["role"] == "index"


def test_dimension_has_standalone_funds():
    dim = ticker_dimension()
    funds = {r["ticker"]: r for r in dim if r["role"] == "fund"}
    assert "ILF" in funds and "XDEW.DE" in funds
    assert funds["XDEW.DE"]["ccy"] == "EUR"
    assert funds["ILF"]["sector_key"] is None  # standalone, not a sector member


def test_sources_policy_shape():
    s = load_sources()
    assert s["deployment_mode"] in (
        "LOCAL_DEMO", "PRIVATE_PERSONAL", "PUBLIC_NONCOMMERCIAL", "PUBLIC_COMMERCIAL")
    assert set(s["sources"]) == {"us_treasury", "ny_fed", "yfinance", "gdelt", "bis",
                                 "federal_register", "sec_edgar"}
    for spec in s["sources"].values():
        assert spec["approval_status"] == "approved"
        assert spec.get("evidence_reference")
        assert spec.get("allowed_modes") and spec.get("review_expires_at")
