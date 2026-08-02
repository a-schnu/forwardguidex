"""Catalysts -> raw_triggers: executive orders + SEC 8-K material events.

Two free, no-key public sources (both verified live 2026-08-02):

* Federal Register API (executive orders). A descriptive User-Agent is polite but
  not required.
* SEC EDGAR (``company_tickers.json`` + ``data.sec.gov`` submissions) for 8-K
  filings. SEC REQUIRES a descriptive User-Agent that includes a contact address
  and limits clients to ~10 requests/second, so requests are spaced with a small
  ``time.sleep`` between companies.

Everything is resilient: any network/parse failure warns and returns what it has
rather than aborting the daily pipeline, and only ``https://`` URLs are kept.

raw_triggers schema: kind, ticker, title, date, url, topic, source, ingested_at
                     (keys: kind, url).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

from ..config import load_universe, ticker_dimension
from ..db import upsert

_UA = {"User-Agent": "ForwardGuidex/0.2 (+https://forwardguidex.com)"}
# SEC requires a descriptive UA that includes a contact address.
_SEC_UA = {"User-Agent": "ForwardGuidex/0.2 antonino.mustazza@accenture.com"}

FED_REGISTER_URL = "https://www.federalregister.gov/api/v1/documents.json"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"


def _parse_date(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _us_constituents(universe: dict) -> list[dict]:
    """Sector constituents (role ``name``) with a plain US ticker.

    Foreign listings carry an exchange suffix (``.MI``/``.DE``/``.L``/``.BR`` …)
    and are skipped — they do not file 8-Ks on EDGAR under that symbol.
    """
    out: list[dict] = []
    for d in ticker_dimension(universe):
        if d.get("role") != "name":
            continue
        if "." in d["ticker"]:
            continue
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Executive orders (Federal Register)
# --------------------------------------------------------------------------- #
def ingest_executive_orders(con, n: int = 20) -> int:
    """Fetch the ``n`` most recent executive orders -> raw_triggers."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    params = {
        "conditions[presidential_document_type][]": "executive_order",
        "per_page": n,
        "order": "newest",
        "fields[]": ["document_number", "title", "publication_date",
                     "signing_date", "html_url"],
    }
    try:
        r = requests.get(FED_REGISTER_URL, params=params, headers=_UA, timeout=60)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as exc:  # noqa: BLE001
        print(f"[triggers] federal register failed: {exc}")
        return 0

    rows: list[dict] = []
    for doc in results:
        url = doc.get("html_url")
        if not url or not str(url).startswith("https://"):
            continue
        rows.append({
            "kind": "executive_order",
            "ticker": None,
            "title": doc.get("title"),
            "date": doc.get("signing_date") or doc.get("publication_date"),
            "url": url,
            "topic": None,
            "source": "federal_register",
            "ingested_at": now,
        })
    if not rows:
        return 0
    df = pd.DataFrame(rows).drop_duplicates(subset=["kind", "url"], keep="last")
    return upsert(con, "raw_triggers", df, keys=["kind", "url"])


# --------------------------------------------------------------------------- #
# SEC 8-K material events (EDGAR)
# --------------------------------------------------------------------------- #
def _ticker_cik_map() -> dict[str, str]:
    """Map ``TICKER -> zero-padded 10-digit CIK`` from SEC ``company_tickers``."""
    r = requests.get(SEC_TICKERS_URL, headers=_SEC_UA, timeout=60)
    r.raise_for_status()
    data = r.json()
    out: dict[str, str] = {}
    for rec in data.values():
        t = str(rec.get("ticker", "")).upper()
        cik = rec.get("cik_str")
        if t and cik is not None:
            out[t] = f"{int(cik):010d}"
    return out


def ingest_sec_8k(con, days: int = 10, per_company: int = 3) -> int:
    """Fetch recent 8-K filings for US constituents -> raw_triggers."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    universe = load_universe()
    try:
        cik_map = _ticker_cik_map()
    except Exception as exc:  # noqa: BLE001
        print(f"[triggers] SEC ticker map failed: {exc}")
        return 0

    cutoff = now.date() - timedelta(days=days)
    rows: list[dict] = []
    for c in _us_constituents(universe):
        t = c["ticker"].upper()
        cik10 = cik_map.get(t)
        if not cik10:
            continue
        try:
            r = requests.get(SEC_SUBMISSIONS_URL.format(cik10=cik10),
                             headers=_SEC_UA, timeout=60)
            r.raise_for_status()
            recent = r.json().get("filings", {}).get("recent", {})
            forms = recent.get("form", []) or []
            fdates = recent.get("filingDate", []) or []
            accns = recent.get("accessionNumber", []) or []
            primdocs = recent.get("primaryDocument", []) or []
            items = recent.get("items", []) or []

            taken = 0
            for i in range(len(forms)):
                if taken >= per_company:
                    break
                if forms[i] != "8-K":
                    continue
                fdate = fdates[i] if i < len(fdates) else None
                d = _parse_date(fdate)
                if d is None or d < cutoff:
                    continue
                acc_nodash = str(accns[i] if i < len(accns) else "").replace("-", "")
                primary = primdocs[i] if i < len(primdocs) else ""
                if not acc_nodash or not primary:
                    continue
                url = (f"https://www.sec.gov/Archives/edgar/data/"
                       f"{int(cik10)}/{acc_nodash}/{primary}")
                topic = items[i] if i < len(items) and items[i] else None
                rows.append({
                    "kind": "sec_8k",
                    "ticker": t,
                    "title": f"{t} — 8-K",
                    "date": fdate,
                    "url": url,
                    "topic": topic or None,
                    "source": "sec_edgar",
                    "ingested_at": now,
                })
                taken += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[triggers] SEC 8-K {t} failed: {exc}")
        time.sleep(0.12)  # SEC fair-access: ~10 req/s

    if not rows:
        return 0
    df = pd.DataFrame(rows).drop_duplicates(subset=["kind", "url"], keep="last")
    return upsert(con, "raw_triggers", df, keys=["kind", "url"])


def ingest_triggers(con) -> int:
    """Ingest both trigger sources; return the total row count."""
    total = 0
    total += ingest_executive_orders(con)
    total += ingest_sec_8k(con)
    return total
