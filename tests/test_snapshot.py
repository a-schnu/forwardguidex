"""Snapshot hashing (content_hash + artifact_sha256) + bundle write."""
import copy
import hashlib
import json

import pytest

from forwardguidex.serve import snapshot as S


def test_content_hash_deterministic_and_excludes_itself():
    p = S.demo_snapshot()
    h = S.compute_content_hash(p)
    # reordered / re-parsed copy hashes identically
    assert S.compute_content_hash(json.loads(json.dumps(p))) == h
    # meta.content_hash is excluded from the hash input
    p2 = copy.deepcopy(p)
    p2["meta"]["content_hash"] = "sha256:" + "0" * 64
    assert S.compute_content_hash(p2) == h
    assert h.startswith("sha256:") and len(h) == len("sha256:") + 64


def test_finalize_artifact_matches_bytes():
    p = S.demo_snapshot()
    payload, final_bytes, artifact, content_hash = S.finalize(p)
    assert hashlib.sha256(final_bytes).hexdigest() == artifact
    assert payload["meta"]["content_hash"] == content_hash
    # bytes contain no NaN/Infinity (allow_nan=False path)
    json.loads(final_bytes.decode("utf-8"))  # standard JSON parses


def test_write_bundle_demo_roundtrip(tmp_path):
    man = S.write_bundle(S.demo_snapshot(), tmp_path, demo=True)
    assert man["snapshot"] == "snapshot.demo.json"
    raw = (tmp_path / man["snapshot"]).read_bytes()
    # browser check: raw-bytes SHA-256 == manifest artifact
    assert hashlib.sha256(raw).hexdigest() == man["artifact_sha256"]
    obj = json.loads(raw)
    assert obj["meta"]["content_hash"] == man["content_hash"]
    assert obj["meta"]["is_demo"] is True
    assert (tmp_path / "latest.demo.json").exists()


def test_prod_bundle_refuses_demo(tmp_path):
    with pytest.raises(ValueError):
        S.write_bundle(S.demo_snapshot(), tmp_path, demo=False)


def test_demo_meta_timestamps_are_tz_aware():
    from datetime import datetime

    meta = S.demo_snapshot()["meta"]
    for k in ("generated_at", "data_as_of", "oldest_required_source_as_of",
              "source_received_at", "freshness_checked_at"):
        assert datetime.fromisoformat(meta[k]).tzinfo is not None


def test_demo_under_size_target():
    _, final_bytes, _, _ = S.finalize(S.demo_snapshot())
    assert len(final_bytes) < 500 * 1024


def test_demo_futures_and_etfs_counts():
    p = S.demo_snapshot()
    assert len(p["futures"]) == 18
    assert len(p["etfs"]) == 17
    for it in p["futures"] + p["etfs"]:
        assert it.get("spark") and "eur" in it and it["eur"] > 0
    fut = {f["ticker"] for f in p["futures"]}
    assert {"ZN=F", "ZB=F", "PA=F", "RB=F", "6B=F"} <= fut
    etf = {e["ticker"] for e in p["etfs"]}
    assert {"SPY", "QQQ", "VTI", "IWM", "GLD", "SLV", "TLT",
            "HYG", "LQD", "AGG", "EEM", "EFA"} <= etf


def test_demo_has_etfs_section():
    p = S.demo_snapshot()
    assert p["etfs"], "etfs section must be non-empty"
    tickers = {e["ticker"] for e in p["etfs"]}
    assert "ILF" in tickers and "XDEW.DE" in tickers
    assert any(e["currency"] == "EUR" for e in p["etfs"])


def test_fx_eur_map_and_conversion_helpers():
    rows = [
        {"role": "fx", "ticker": "EURUSD=X", "last_close": 1.1527},
        {"role": "fx", "ticker": "EURJPY=X", "last_close": 181.5},
        {"role": "index", "ticker": "^GSPC", "last_close": 5000.0},
    ]
    fx = S._fx_eur_map(rows)
    assert fx == {"USD": 1.1527, "JPY": 181.5}
    assert S._eur_price(5000.0, "USD", fx) == pytest.approx(5000.0 / 1.1527, rel=1e-6)
    assert S._eur_price(100.0, "EUR", fx) == 100.0        # EUR -> unchanged
    assert S._eur_price(100.0, "XYZ", fx) is None          # unknown currency -> omit
    assert S._eur_price(None, "USD", fx) is None           # no price -> omit


def test_demo_items_have_eur():
    p = S.demo_snapshot()
    for it in p["indices"] + p["futures"] + p["etfs"] + p["crypto"]:
        assert "eur" in it and it["eur"] > 0
    dax = next(i for i in p["indices"] if i["ticker"] == "^GDAXI")
    assert dax["currency"] == "EUR" and dax["eur"] == dax["last"]  # EUR item: eur == last
    spx = next(i for i in p["indices"] if i["ticker"] == "^GSPC")
    assert spx["eur"] == pytest.approx(spx["last"] / 1.1527, rel=1e-6)
    assert all("eur" in c for s in p["sectors"] for c in s["constituents"])


def test_demo_has_no_fx_role_rows():
    p = S.demo_snapshot()
    rendered = set()
    for sec in ("indices", "futures", "etfs", "crypto"):
        rendered |= {it["ticker"] for it in p[sec]}
    for s in p["sectors"]:
        rendered |= {e["ticker"] for e in s["etfs"] + s["constituents"]}
    assert not any(t.startswith("EUR") and t.endswith("=X") for t in rendered)


def test_demo_has_crypto_section():
    p = S.demo_snapshot()
    assert p["crypto"], "crypto section must be non-empty"
    tickers = {c["ticker"] for c in p["crypto"]}
    assert {"BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD"} <= tickers
    btc = next(c for c in p["crypto"] if c["ticker"] == "BTC-USD")
    assert btc["last"] > 1000 and "spark" in btc and btc["currency"] == "USD"


def test_demo_sectors_have_multiple_etfs():
    p = S.demo_snapshot()
    for s in p["sectors"]:
        assert len(s["etfs"]) >= 2, f"{s['key']} has < 2 etfs"
    all_etf = {e["ticker"] for s in p["sectors"] for e in s["etfs"]}
    assert {"EXH1.DE", "VVSM.DE", "DFEN.DE"} <= all_etf  # European UCITS present
    # every sector ETF carries a real (non-ticker) name
    assert all(e["name"] and e["name"] != e["ticker"]
               for s in p["sectors"] for e in s["etfs"])


def test_demo_indices_have_foreign_currencies():
    p = S.demo_snapshot()
    ccys = {i["currency"] for i in p["indices"]}
    assert {"EUR", "GBP", "JPY", "HKD", "CNY", "KRW"} <= ccys


def test_demo_has_bis_central_bank_rates():
    p = S.demo_snapshot()
    bis = [r for r in p["rates"] if r["source"] == "BIS"]
    assert {r["series_id"] for r in bis} >= {"ECBDFR", "BOEBR", "BOJPR", "PBOCLPR1Y"}


def test_demo_sectors_have_real_constituent_names():
    p = S.demo_snapshot()
    sec = {s["key"]: s for s in p["sectors"]}
    assert {"internet", "financials", "healthcare", "materials", "utilities"} <= set(sec)
    names = {c["name"] for c in sec["semis"]["constituents"]}
    assert "Nvidia" in names and "Infineon" in names
    # a foreign constituent keeps its local currency
    assert any(c["ticker"] == "IFX.DE" and c["currency"] == "EUR"
               for c in sec["semis"]["constituents"])


def test_demo_has_sparklines():
    p = S.demo_snapshot()
    assert all("spark" in i and len(i["spark"]) >= 10 for i in p["indices"])
    assert all("spark" in f for f in p["futures"])
    assert all("spark" in e for e in p["etfs"])


def test_demo_semis_apple_first():
    p = S.demo_snapshot()
    semis = next(s for s in p["sectors"] if s["key"] == "semis")
    assert semis["constituents"][0]["ticker"] == "AAPL"
    assert semis["constituents"][0]["name"] == "Apple"


def test_build_snapshot_orders_members_by_universe():
    """Sector member order follows the universe file, not SQL/insert order."""
    import duckdb
    import pandas as pd
    from datetime import datetime, timezone

    from forwardguidex import db as D
    from forwardguidex.transform import marts

    con = duckdb.connect(":memory:")
    D.build_dim_ticker(con)
    dates = pd.date_range("2026-07-01", periods=6, freq="D")
    # insert semis constituents in a deliberately scrambled order
    scrambled = ["MU", "AAPL", "NVDA", "TSM"]
    rows = [{"date": dt, "ticker": t, "close": 100 + i}
            for t in scrambled for i, dt in enumerate(dates)]
    D.upsert(con, "raw_prices", pd.DataFrame(rows), keys=["ticker", "date"])
    D.upsert(con, "raw_macro", pd.DataFrame([{
        "series_id": "UST10Y", "name": "US 10Y",
        "date": pd.Timestamp("2026-07-31"), "value": 4.75, "source": "UST"}]),
        keys=["series_id", "date"])
    marts.build_marts(con)
    p = S.build_snapshot(con, now=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
    semis = next(s for s in p["sectors"] if s["key"] == "semis")
    got = [c["ticker"] for c in semis["constituents"]]
    # universe order among the seeded tickers: AAPL(0), NVDA(1), TSM(3), MU(6)
    assert got == ["AAPL", "NVDA", "TSM", "MU"]


def test_demo_movers_are_constituents_only():
    p = S.demo_snapshot()
    constituents = {c["ticker"] for s in p["sectors"] for c in s["constituents"]}
    non_constituents = {e["ticker"] for s in p["sectors"] for e in s["etfs"]}
    non_constituents |= {i["ticker"] for i in p["indices"] + p["futures"]
                         + p["etfs"] + p["crypto"]}
    movers = p["movers"]["gainers"] + p["movers"]["losers"]
    assert movers, "movers must be non-empty"
    for m in movers:
        assert m["ticker"] in constituents        # only real company stocks
        assert m["ticker"] not in non_constituents  # no ETFs/funds/indices/futures/crypto
        assert m.get("sector")                     # with_sector=True
    # true extremes across all constituents
    all_r = [c["ret_1d"] for s in p["sectors"] for c in s["constituents"]]
    assert p["movers"]["gainers"][0]["ret_1d"] == max(all_r)
    assert p["movers"]["losers"][0]["ret_1d"] == min(all_r)


def test_demo_brief_is_italian_two_sections():
    md = S.demo_snapshot()["brief"]["markdown"]
    headings = [ln for ln in md.splitlines() if ln.startswith("## ")]
    assert headings == ["## Sintesi del giorno", "## Cosa tenere d'occhio"]
    assert md.startswith("**Regime:")
    assert "Breve termine (trading)" in md and "Lungo termine (investimento)" in md
    # dropped sections / no English scaffolding, and no insecure links
    assert "Cross-asset" not in md and "TL;DR" not in md and "What to watch" not in md
    assert "http://" not in md


def test_demo_attribution_includes_bis_and_verbatim_nyfed():
    meta = S.demo_snapshot()["meta"]
    attr = meta["attribution"]
    assert "bis" in attr and attr["bis"].startswith("Source: Bank for International Settlements")
    assert "[[REPLACE" not in attr["ny_fed"]
    assert attr["ny_fed"].startswith("The EFFR and SOFR data is subject to the Terms of Use")
    assert "BIS" in meta["source"]
