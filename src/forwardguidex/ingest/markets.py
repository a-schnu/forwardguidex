"""Market data via yfinance -> raw_prices."""
from __future__ import annotations

import pandas as pd
import yfinance as yf

from ..config import all_price_tickers, get_settings, load_universe
from ..db import upsert

_FIELDS = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}


def _download(tickers: list[str], period: str) -> pd.DataFrame:
    raw = yf.download(
        tickers, period=period, interval="1d", auto_adjust=True,
        progress=False, group_by="ticker", threads=True,
    )
    frames: list[pd.DataFrame] = []
    if isinstance(raw.columns, pd.MultiIndex):
        available = set(raw.columns.get_level_values(0))
        for t in tickers:
            if t not in available:
                continue
            sub = raw[t].reset_index()
            sub["ticker"] = t
            frames.append(sub)
    else:  # single ticker -> flat columns
        sub = raw.reset_index()
        sub["ticker"] = tickers[0]
        frames.append(sub)
    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True).rename(columns={"Date": "date", **_FIELDS})
    keep = ["date", "ticker", *_FIELDS.values()]
    df = df[[c for c in keep if c in df.columns]].dropna(subset=["close"])
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None)
    return df


def ingest_markets(con) -> int:
    tickers = all_price_tickers(load_universe())
    df = _download(tickers, get_settings().price_period)
    return upsert(con, "raw_prices", df, keys=["ticker", "date"])
