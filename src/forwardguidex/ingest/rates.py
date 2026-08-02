"""Rates & yields from primary sources -> raw_macro.

Three public, no-key sources (FRED was removed for compliance — see config/sources.yaml):

* US Treasury Daily Par Yield Curve Rates (public domain) -> UST maturities.
* NY Fed Markets API (attribution + disclaimer required) -> EFFR + SOFR.
* BIS central-bank policy rates (WS_CBPOL; attribution required) -> ECB/BoE/BoJ/PBoC.

raw_macro schema: series_id, name, date, value, source  (keys: series_id, date).
"""
from __future__ import annotations

from datetime import date
from io import StringIO

import pandas as pd
import requests

from ..config import load_universe
from ..db import upsert

_UA = {"User-Agent": "ForwardGuidex/0.2 (+https://forwardguidex.com)"}

TREASURY_CSV = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)
NYFED_URL = "https://markets.newyorkfed.org/api/rates/{group}/{rate}/last/{n}.json"
# BIS SDMX REST: policy rates (WS_CBPOL), daily. The CSV representation is
# requested via the `Accept` header (a `format=` query param 406s).
BIS_URL = "https://stats.bis.org/api/v1/data/WS_CBPOL/D.{areas}/all?lastNObservations={n}"
BIS_HEADERS = {**_UA, "Accept": "application/vnd.sdmx.data+csv"}


def _fetch_treasury_year(year: int) -> pd.DataFrame:
    """Return the Treasury daily par-yield CSV for `year` (Date + tenor columns)."""
    r = requests.get(TREASURY_CSV.format(year=year), headers=_UA, timeout=60)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    if "Date" not in df.columns:
        return pd.DataFrame()
    return df


def _treasury_rows(maturities: list[dict]) -> pd.DataFrame:
    """Long-form Treasury rows for the configured maturities, ~2y of history."""
    this_year = date.today().year
    frames: list[pd.DataFrame] = []
    for yr in (this_year - 1, this_year):
        try:
            wide = _fetch_treasury_year(yr)
        except Exception as exc:  # noqa: BLE001
            print(f"[rates] Treasury {yr} failed: {exc}")
            continue
        if wide.empty:
            continue
        wide["date"] = pd.to_datetime(wide["Date"], errors="coerce")
        for m in maturities:
            col = m["column"]
            if col not in wide.columns:
                print(f"[rates] Treasury column '{col}' missing for {yr}")
                continue
            sub = wide[["date", col]].copy()
            sub["value"] = pd.to_numeric(sub[col], errors="coerce")
            sub["series_id"] = m["series_id"]
            sub["name"] = m["name"]
            sub["source"] = "UST"
            frames.append(sub[["series_id", "name", "date", "value", "source"]])
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).dropna(subset=["date", "value"])


def _nyfed_rows(rates: list[dict], n: int = 30) -> pd.DataFrame:
    """Long-form NY Fed reference-rate rows (EFFR/SOFR) with recent history."""
    rows: list[dict] = []
    for spec in rates:
        url = NYFED_URL.format(group=spec["group"], rate=spec["rate"], n=n)
        try:
            r = requests.get(url, headers=_UA, timeout=60)
            r.raise_for_status()
            payload = r.json().get("refRates", [])
        except Exception as exc:  # noqa: BLE001
            print(f"[rates] NY Fed {spec['series_id']} failed: {exc}")
            continue
        for rec in payload:
            val = rec.get("percentRate")
            eff = rec.get("effectiveDate")
            if val is None or eff is None:
                continue
            rows.append({
                "series_id": spec["series_id"],
                "name": spec["name"],
                "date": eff,
                "value": val,
                "source": "NYFED",
            })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna(subset=["date", "value"])


def _bis_rows(cb_rates: list[dict], n: int = 10) -> pd.DataFrame:
    """Long-form BIS central-bank policy-rate rows (source = "BIS").

    One request covers every configured area (REF_AREA codes joined with `+`).
    Resilient: on any fetch/parse failure it warns and returns an empty frame so
    the daily pipeline is never blocked by BIS being unavailable.
    """
    if not cb_rates:
        return pd.DataFrame()
    by_area = {spec["area"]: spec for spec in cb_rates}
    areas = "+".join(by_area)  # e.g. XM+GB+JP+CN
    url = BIS_URL.format(areas=areas, n=n)
    try:
        r = requests.get(url, headers=BIS_HEADERS, timeout=60)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
    except Exception as exc:  # noqa: BLE001
        print(f"[rates] BIS policy rates failed: {exc}")
        return pd.DataFrame()
    needed = {"REF_AREA", "TIME_PERIOD", "OBS_VALUE"}
    if not needed <= set(df.columns):
        print(f"[rates] BIS CSV missing columns {needed - set(df.columns)}")
        return pd.DataFrame()
    rows: list[dict] = []
    for rec in df.itertuples(index=False):
        spec = by_area.get(str(getattr(rec, "REF_AREA")))
        if spec is None:
            continue
        rows.append({
            "series_id": spec["series_id"],
            "name": spec["name"],
            "date": getattr(rec, "TIME_PERIOD"),
            "value": getattr(rec, "OBS_VALUE"),
            "source": "BIS",
        })
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    return out.dropna(subset=["date", "value"])


def ingest_rates(con) -> int:
    u = load_universe()
    frames = [
        _treasury_rows(u.get("treasury_maturities", [])),
        _nyfed_rows(u.get("nyfed_rates", [])),
        _bis_rows(u.get("cb_policy_rates", [])),
    ]
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        print("[rates] no rate data fetched")
        return 0
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["date"].dt.tz_localize(None) if df["date"].dt.tz is not None else df["date"]
    df = df.drop_duplicates(subset=["series_id", "date"], keep="last")
    return upsert(con, "raw_macro", df[["series_id", "name", "date", "value", "source"]],
                  keys=["series_id", "date"])
