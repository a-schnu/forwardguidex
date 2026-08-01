"""News & geopolitics via the free GDELT DOC 2.0 API -> raw_news."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import requests

from ..config import load_universe
from ..db import upsert

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def _fetch(query: str, maxrecords: int = 50, timespan: str = "1d") -> list[dict]:
    params = {
        "query": query, "mode": "ArtList", "format": "json",
        "maxrecords": maxrecords, "timespan": timespan, "sort": "DateDesc",
    }
    r = requests.get(GDELT_URL, params=params, timeout=60,
                     headers={"User-Agent": "ForwardGuidex/0.1"})
    r.raise_for_status()
    try:
        return r.json().get("articles", [])
    except ValueError:
        return []


def ingest_news(con) -> int:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows: list[dict] = []
    for item in load_universe().get("gdelt_queries", []):
        key, query = item["key"], item["query"]
        try:
            articles = _fetch(query)
        except Exception as exc:  # noqa: BLE001
            print(f"[news] {key} failed: {exc}")
            continue
        for a in articles:
            rows.append({
                "topic": key,
                "url": a.get("url"),
                "title": a.get("title"),
                "domain": a.get("domain"),
                "seendate": a.get("seendate"),
                "sourcecountry": a.get("sourcecountry"),
                "language": a.get("language"),
                "ingested_at": now,
            })
    if not rows:
        return 0
    df = (pd.DataFrame(rows)
          .dropna(subset=["url"])
          .drop_duplicates(subset=["topic", "url"]))
    return upsert(con, "raw_news", df, keys=["topic", "url"])
