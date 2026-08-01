"""Build the published EOD snapshot (schema_version 1) + manifest.

Flow (matches the release contract):
    build_snapshot(con)            -> payload dict (no meta.content_hash yet)
    compute_content_hash(payload)  -> canonical hash of payload, inserted back
    finalize(payload)              -> exact final bytes + artifact_sha256
    write_bundle(payload, out_dir) -> snapshot.<artifact_sha256>.json + latest.json

Two hashes:
* content_hash   = SHA-256 of the canonical payload (sort_keys, compact, UTF-8,
                   allow_nan=False) with meta.content_hash removed. Prefixed
                   "sha256:". Semantic identity.
* artifact_sha256 = SHA-256 of the EXACT serialized final file bytes (bare hex).
                   Used in the filename and verified byte-for-byte by the browser.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

from .. import config
from ..transform import marts
from . import calendar as fcal
from . import rights

SCHEMA_VERSION = 1
CADENCE = "EOD"
DELIVERY = "STATIC"

_SOURCE_LABELS = [("yfinance", "yfinance"), ("us_treasury", "UST"),
                  ("ny_fed", "NYFed"), ("gdelt", "GDELT")]


# --------------------------------------------------------------------------- #
# JSON safety
# --------------------------------------------------------------------------- #
def _num(v):
    """Coerce to a JSON-safe float/int or None (drops NaN/Inf)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 6)


def _str(v, default=None):
    """JSON-safe string or default (guards pandas NaN, which is a truthy float)."""
    if v is None:
        return default
    if isinstance(v, float) and math.isnan(v):
        return default
    s = str(v)
    return s if s else default


def _iso(ts) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    try:
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)  # market dates are UTC EOD
        return ts.isoformat()
    except Exception:  # noqa: BLE001
        return str(ts)


def _now_iso(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Warehouse -> payload
# --------------------------------------------------------------------------- #
def _price_item(row, *, source="yfinance", currency="USD", with_sector=False) -> dict:
    r1 = _num(row.get("ret_1d"))
    item = {
        "ticker": _str(row.get("ticker")),
        "name": _str(row.get("name"), default=_str(row.get("ticker"))),
        "last": _num(row.get("last_close")),
        "currency": currency,
        "ret_1d": r1,
        "ret_5d": _num(row.get("ret_5d")),
        "as_of": _iso(row.get("last_date")),
        "source": source,
        "quality": "OK" if r1 is not None else "PARTIAL",
    }
    if with_sector:
        item["sector"] = _str(row.get("sector_label"))
    return item


def _universe_rows(con):
    return con.execute(
        """
        SELECT g.ticker, d.name, d.role, d.sector_key, d.sector_label,
               g.last_close, g.ret_1d, g.ret_5d, g.last_date
        FROM gold_latest g LEFT JOIN dim_ticker d USING (ticker)
        """
    ).df()


def build_snapshot(con, *, market_state: str = "PRE_OPEN",
                   now: datetime | None = None, quality: str = "OK") -> dict:
    now = now or datetime.now(timezone.utc)
    uni = _universe_rows(con)
    rows = [r._asdict() if hasattr(r, "_asdict") else dict(r) for _, r in uni.iterrows()]

    indices, futures, equities = [], [], []
    by_sector: dict[str, dict] = {}
    for r in rows:
        role = r.get("role")
        if role == "index":
            indices.append(_price_item(r))
        elif role == "future":
            futures.append(_price_item(r))
        elif role in ("etf", "name"):
            equities.append(r)
            key = r.get("sector_key")
            if key:
                sec = by_sector.setdefault(key, {"key": key, "label": _str(r.get("sector_label")),
                                                 "etfs": [], "constituents": []})
                item = _price_item(r)
                if role == "etf":
                    item["role"] = "etf"
                    sec["etfs"].append(item)
                else:
                    item["role"] = "constituent"
                    sec["constituents"].append(item)

    sectors = []
    for key in sorted(by_sector):
        sec = by_sector[key]
        members = sec["etfs"] + sec["constituents"]
        r1 = [m["ret_1d"] for m in members if m["ret_1d"] is not None]
        r5 = [m["ret_5d"] for m in members if m["ret_5d"] is not None]
        sec["avg_ret_1d"] = _num(sum(r1) / len(r1)) if r1 else None
        sec["avg_ret_5d"] = _num(sum(r5) / len(r5)) if r5 else None
        sectors.append(sec)

    # movers over the equities (names + etfs)
    scored = [_price_item(r, with_sector=True) for r in equities if r.get("ret_1d") is not None]
    scored.sort(key=lambda x: x["ret_1d"], reverse=True)
    movers = {"gainers": scored[:6], "losers": list(reversed(scored[-6:])) if scored else []}

    # rates
    rates_df = marts.rates(con)
    rate_items = []
    for r in rates_df.itertuples():
        val = _num(r.value)
        rate_items.append({
            "series_id": _str(r.series_id),
            "name": _str(r.name),
            "value": val,
            "chg": _num(getattr(r, "chg", None)),
            "as_of": _iso(getattr(r, "last_date", None)),
            "source": _str(getattr(r, "source", None)),
            "quality": "OK" if val is not None else "PARTIAL",
        })

    # headlines
    # Keep only https headlines (the dashboard renders only https links, and the
    # validator rejects http URLs) — fetch extra, filter, cap at 12.
    news_df = marts.news(con, limit=40)
    headlines = []
    for h in news_df.itertuples():
        url = _str(h.url)
        if not url or not url.startswith("https://"):
            continue
        headlines.append({
            "topic": _str(h.topic), "title": _str(h.title), "domain": _str(h.domain),
            "url": url, "seendate": _str(h.seendate),
        })
        if len(headlines) >= 12:
            break

    # brief
    brief = {"markdown": "", "created_at": None}
    try:
        row = con.execute(
            "SELECT content, created_at FROM brief_history ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if row:
            brief = {"markdown": row[0], "created_at": _iso(row[1])}
    except Exception:  # noqa: BLE001
        pass

    payload = {
        "indices": indices,
        "futures": futures,
        "sectors": sectors,
        "rates": rate_items,
        "movers": movers,
        "headlines": headlines,
        "brief": brief,
    }
    payload["meta"] = _build_meta(payload, market_state=market_state, now=now,
                                  quality=quality, is_demo=False)
    return payload


def _present_source_keys(payload: dict) -> set[str]:
    return rights.sources_in_snapshot(payload)


def _source_string(keys: set[str]) -> str:
    return "+".join(label for k, label in _SOURCE_LABELS if k in keys)


def _all_as_of(payload: dict) -> list[str]:
    out: list[str] = []
    for s in ("indices", "futures"):
        out += [i["as_of"] for i in payload.get(s, []) if i.get("as_of")]
    for sec in payload.get("sectors", []):
        out += [i["as_of"] for i in sec.get("etfs", []) + sec.get("constituents", []) if i.get("as_of")]
    out += [r["as_of"] for r in payload.get("rates", []) if r.get("as_of")]
    return out


def _market_as_of(payload: dict) -> list[str]:
    out: list[str] = []
    for s in ("indices", "futures"):
        out += [i["as_of"] for i in payload.get(s, []) if i.get("as_of")]
    for sec in payload.get("sectors", []):
        out += [i["as_of"] for i in sec.get("etfs", []) + sec.get("constituents", []) if i.get("as_of")]
    return out


def _build_meta(payload: dict, *, market_state: str, now: datetime,
                quality: str, is_demo: bool) -> dict:
    keys = _present_source_keys(payload)
    freshness = fcal.assess_snapshot(payload, now=now)
    market_dt = sorted(d for d in (fcal.parse_dt(s) for s in _market_as_of(payload)) if d)
    every_dt = sorted(d for d in (fcal.parse_dt(s) for s in _all_as_of(payload)) if d)
    now_dt = fcal.parse_dt(_now_iso(now))
    data_dt = (market_dt[-1] if market_dt else (every_dt[-1] if every_dt else now_dt))
    oldest_dt = (every_dt[0] if every_dt else data_dt)
    data_as_of = data_dt.isoformat()
    oldest = oldest_dt.isoformat()
    q = quality if freshness.overall == "FRESH" else "DEGRADED"
    meta = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now_iso(now),
        "data_as_of": data_as_of,
        "oldest_required_source_as_of": oldest,
        "source_received_at": _now_iso(now),
        "freshness": freshness.overall,
        "freshness_checked_at": freshness.checked_at,
        "freshness_rule_version": freshness.rule_version,
        "cadence": CADENCE,
        "delivery": DELIVERY,
        "quality": q,
        "is_demo": is_demo,
        "content_hash": "",  # filled by compute_content_hash
        "source": _source_string(keys),
        "market_state_at_generation": market_state,
        "attribution": rights.attribution_block(keys),
    }
    return meta


# --------------------------------------------------------------------------- #
# Hashing + serialization
# --------------------------------------------------------------------------- #
def _canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def compute_content_hash(payload: dict) -> str:
    """SHA-256 of the canonical payload with meta.content_hash excluded."""
    meta = payload.get("meta", {})
    saved = meta.get("content_hash")
    meta.pop("content_hash", None)
    try:
        digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    finally:
        meta["content_hash"] = saved if saved is not None else ""
    return f"sha256:{digest}"


def finalize(payload: dict) -> tuple[dict, bytes, str, str]:
    """Insert content_hash, serialize final bytes, return artifact_sha256.

    Returns (payload, final_bytes, artifact_sha256_hex, content_hash).
    """
    content_hash = compute_content_hash(payload)
    payload["meta"]["content_hash"] = content_hash
    final_bytes = _canonical_bytes(payload)
    artifact = hashlib.sha256(final_bytes).hexdigest()
    return payload, final_bytes, artifact, content_hash


def build_manifest(payload: dict, artifact: str, content_hash: str, *,
                   demo: bool = False) -> dict:
    snap_name = "snapshot.demo.json" if demo else f"snapshot.{artifact}.json"
    return {
        "snapshot": snap_name,
        "artifact_sha256": artifact,
        "content_hash": content_hash,
        "generated_at": payload["meta"]["generated_at"],
        "schema_version": payload["meta"]["schema_version"],
    }


def write_bundle(payload: dict, out_dir, *, demo: bool = False) -> dict:
    """Serialize + write snapshot + manifest to out_dir. Returns the manifest.

    Prod (demo=False) asserts meta.is_demo is False; demo asserts it is True.
    """
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    is_demo = bool(payload.get("meta", {}).get("is_demo"))
    if demo and not is_demo:
        raise ValueError("write_bundle(demo=True) requires meta.is_demo == true")
    if not demo and is_demo:
        raise ValueError("prod bundle refuses a demo payload (meta.is_demo == true)")

    payload, final_bytes, artifact, content_hash = finalize(payload)
    manifest = build_manifest(payload, artifact, content_hash, demo=demo)

    snap_path = out / manifest["snapshot"]
    manifest_path = out / ("latest.demo.json" if demo else "latest.json")
    snap_path.write_bytes(final_bytes)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


# --------------------------------------------------------------------------- #
# Demo payload (frontend local dev; is_demo:true, never deployed to prod)
# --------------------------------------------------------------------------- #
def demo_snapshot(now: datetime | None = None) -> dict:
    now = now or datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
    eq, ust, nf, seen = "2026-07-31T20:00:00+00:00", "2026-07-31", "2026-07-30", "20260731T120000Z"

    def eqi(t, n, last, r1, r5, sector=None):
        d = {"ticker": t, "name": n, "last": last, "currency": "USD",
             "ret_1d": r1, "ret_5d": r5, "as_of": eq, "source": "yfinance", "quality": "OK"}
        if sector:
            d["sector"] = sector
        return d

    sectors_def = [
        ("oil_gas", "Oil & Gas", 1.8, [("XLE", "Energy SPDR", 92.4, 1.8, 3.1, "etf"),
            ("XOM", "Exxon", 118.2, 2.1, 3.4, "constituent"), ("CVX", "Chevron", 162.5, 1.6, 2.2, "constituent")]),
        ("defense", "Defense & Aerospace", 2.4, [("ITA", "Aerospace/Def", 148.0, 2.4, 4.0, "etf"),
            ("LMT", "Lockheed", 512.3, 1.9, 2.7, "constituent"), ("RTX", "RTX", 121.4, 2.8, 3.9, "constituent")]),
        ("staples", "Consumer Staples", -0.3, [("XLP", "Staples SPDR", 79.1, -0.3, 0.4, "etf"),
            ("PG", "P&G", 168.9, -0.1, 0.2, "constituent"), ("KO", "Coca-Cola", 63.7, -0.4, -0.1, "constituent")]),
        ("software", "Tech Software", 0.9, [("IGV", "Software ETF", 92.0, 0.9, 2.3, "etf"),
            ("MSFT", "Microsoft", 452.6, 0.7, 1.8, "constituent"), ("CRM", "Salesforce", 268.1, 1.4, 2.9, "constituent")]),
        ("semis", "Tech Hardware / Semis", -1.1, [("SMH", "Semis ETF", 245.7, -1.1, -2.4, "etf"),
            ("NVDA", "Nvidia", 168.3, -1.8, -3.2, "constituent"), ("AMD", "AMD", 172.9, -0.9, -1.7, "constituent")]),
        ("industrials", "Infrastructure & Industrials", 0.5, [("XLI", "Industrials SPDR", 138.2, 0.5, 1.2, "etf"),
            ("CAT", "Caterpillar", 402.5, 0.9, 1.9, "constituent"), ("DE", "Deere", 421.0, 0.4, 0.8, "constituent")]),
    ]
    sectors = []
    all_eq = []
    for key, label, avg, members in sectors_def:
        etfs, cons = [], []
        for t, n, last, r1, r5, role in members:
            it = eqi(t, n, last, r1, r5)
            it["role"] = role
            (etfs if role == "etf" else cons).append(it)
            all_eq.append(eqi(t, n, last, r1, r5, sector=label))
        r5s = [m["ret_5d"] for m in etfs + cons]
        sectors.append({"key": key, "label": label, "avg_ret_1d": avg,
                        "avg_ret_5d": round(sum(r5s) / len(r5s), 2), "etfs": etfs, "constituents": cons})

    all_eq.sort(key=lambda x: x["ret_1d"], reverse=True)
    payload = {
        "indices": [eqi("^GSPC", "S&P 500", 5620.4, 0.6, 1.4), eqi("^NDX", "Nasdaq 100", 20450.1, -0.2, 0.9),
                    eqi("^DJI", "Dow Jones", 41230.7, 0.4, 1.1), eqi("^RUT", "Russell 2000", 2280.5, 1.2, 2.0)],
        "futures": [eqi("CL=F", "WTI Crude", 78.4, 1.9, 3.0), eqi("GC=F", "Gold", 2412.6, 0.3, 0.7),
                    eqi("ES=F", "S&P e-mini", 5628.0, 0.5, 1.3)],
        "sectors": sectors,
        "rates": [
            {"series_id": "UST2Y", "name": "US 2Y Treasury Par Yield", "value": 4.28, "chg": 0.05,
             "as_of": ust, "source": "UST", "quality": "OK"},
            {"series_id": "UST10Y", "name": "US 10Y Treasury Par Yield", "value": 4.75, "chg": 0.07,
             "as_of": ust, "source": "UST", "quality": "OK"},
            {"series_id": "UST30Y", "name": "US 30Y Treasury Par Yield", "value": 5.27, "chg": 0.06,
             "as_of": ust, "source": "UST", "quality": "OK"},
            {"series_id": "EFFR", "name": "Effective Federal Funds Rate", "value": 3.63, "chg": 0.0,
             "as_of": nf, "source": "NYFED", "quality": "OK"},
            {"series_id": "SOFR", "name": "Secured Overnight Financing Rate", "value": 3.65, "chg": 0.01,
             "as_of": nf, "source": "NYFED", "quality": "OK"},
        ],
        "movers": {"gainers": all_eq[:6], "losers": list(reversed(all_eq[-6:]))},
        "headlines": [
            {"topic": "oil", "title": "OPEC+ holds output steady ahead of demand review", "domain": "reuters.com",
             "url": "https://www.reuters.com/markets/commodities/", "seendate": seen},
            {"topic": "defense", "title": "NATO members lift defense spending targets", "domain": "ft.com",
             "url": "https://www.ft.com/world", "seendate": seen},
            {"topic": "semis", "title": "Chip demand cools as AI capex guidance trimmed", "domain": "bloomberg.com",
             "url": "https://www.bloomberg.com/technology", "seendate": seen},
            {"topic": "fed", "title": "Fed officials signal patience on rate cuts", "domain": "wsj.com",
             "url": "https://www.wsj.com/economy", "seendate": seen},
        ],
        "brief": {"markdown": (
            "## TL;DR\n"
            "- **Energy +1.8%** on Middle-East supply risk; **Defense +2.4%**.\n"
            "- **Semis -1.1%** as AI capex guidance is trimmed.\n"
            "- Risk-off tone: 10Y at **4.75%** (+0.07).\n\n"
            "## Cross-asset read\n"
            "Crude firm, gold steady, small-caps outperform. Rates drift higher.\n\n"
            "## What to watch today\n"
            "**Short-term trade triggers:** XLE and ITA momentum; SMH near support.\n\n"
            "**Long-term investment signals:** staples and industrials stable; trend intact.\n"
        ), "created_at": eq},
    }
    payload["meta"] = _build_meta(payload, market_state="PRE_OPEN", now=now,
                                  quality="OK", is_demo=True)
    return payload
