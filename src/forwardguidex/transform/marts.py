"""Build gold marts (returns, sector rollups, latest rates) and read helpers."""
from __future__ import annotations

import pandas as pd

from ..db import build_dim_ticker, table_exists


def build_marts(con) -> None:
    build_dim_ticker(con)
    if not table_exists(con, "raw_prices"):
        raise RuntimeError("raw_prices missing - run `fwdx ingest markets` first")

    con.execute(
        """
        CREATE OR REPLACE TABLE gold_latest AS
        WITH ranked AS (
            SELECT ticker, date, close,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn,
                   LAG(close, 1) OVER (PARTITION BY ticker ORDER BY date) AS prev_close,
                   LAG(close, 5) OVER (PARTITION BY ticker ORDER BY date) AS close_5d
            FROM raw_prices
        )
        SELECT ticker,
               date AS last_date,
               close AS last_close,
               prev_close,
               CASE WHEN prev_close > 0 THEN ROUND((close / prev_close - 1) * 100, 2) END AS ret_1d,
               CASE WHEN close_5d  > 0 THEN ROUND((close / close_5d  - 1) * 100, 2) END AS ret_5d
        FROM ranked
        WHERE rn = 1
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE gold_sector AS
        SELECT d.sector_label,
               COUNT(*) AS n,
               ROUND(AVG(g.ret_1d), 2) AS avg_ret_1d,
               ROUND(AVG(g.ret_5d), 2) AS avg_ret_5d
        FROM gold_latest g
        JOIN dim_ticker d USING (ticker)
        WHERE d.sector_key IS NOT NULL
        GROUP BY d.sector_label
        ORDER BY avg_ret_1d DESC
        """
    )
    if table_exists(con, "raw_macro"):
        con.execute(
            """
            CREATE OR REPLACE TABLE gold_rates AS
            WITH r AS (
                SELECT series_id, name, source, date, value,
                       ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY date DESC) AS rn,
                       LAG(value, 1) OVER (PARTITION BY series_id ORDER BY date) AS prev
                FROM raw_macro
            )
            SELECT series_id, name, source, date AS last_date, value,
                   ROUND(value - prev, 3) AS chg
            FROM r WHERE rn = 1
            """
        )


def latest(con) -> pd.DataFrame:
    if not table_exists(con, "gold_latest"):
        return pd.DataFrame()
    return con.execute(
        """
        SELECT g.ticker, d.name, d.role, d.sector_label,
               g.last_close, g.ret_1d, g.ret_5d, g.last_date
        FROM gold_latest g LEFT JOIN dim_ticker d USING (ticker)
        ORDER BY g.ret_1d DESC NULLS LAST
        """
    ).df()


def sectors(con) -> pd.DataFrame:
    if not table_exists(con, "gold_sector"):
        return pd.DataFrame()
    return con.execute("SELECT * FROM gold_sector").df()


def rates(con) -> pd.DataFrame:
    if not table_exists(con, "gold_rates"):
        return pd.DataFrame()
    return con.execute("SELECT * FROM gold_rates ORDER BY series_id").df()


def news(con, limit: int = 20) -> pd.DataFrame:
    if not table_exists(con, "raw_news"):
        return pd.DataFrame()
    return con.execute(
        "SELECT topic, title, domain, url, seendate FROM raw_news "
        "ORDER BY seendate DESC LIMIT ?",
        [limit],
    ).df()
