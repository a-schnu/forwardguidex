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
from ..transform import events as fevents
from ..transform import marts
from . import calendar as fcal
from . import rights

SCHEMA_VERSION = 1
CADENCE = "EOD"
DELIVERY = "STATIC"

_SOURCE_LABELS = [("yfinance", "yfinance"), ("us_treasury", "UST"),
                  ("ny_fed", "NYFed"), ("bis", "BIS"), ("gdelt", "GDELT"),
                  ("federal_register", "FedReg"), ("sec_edgar", "EDGAR")]

# Required keys per Phase-2 event item — items missing any are dropped before the
# schema sees them (defence-in-depth: a stray NULL in a source row can never make
# the whole fail-closed export invalid).
_EVENT_REQUIRED = {
    "cb_events": ("bank", "series_id", "direction", "source"),
    "earnings": ("ticker", "date", "source"),
    "triggers": ("kind", "title", "date", "url", "source"),
}


def _clean_events(section: str, items, cap: int) -> list[dict]:
    """Keep only items with every required key non-empty; cap the count."""
    required = _EVENT_REQUIRED[section]
    out = [it for it in (items or []) if all(it.get(k) for k in required)]
    return out[:cap]


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
def _fx_eur_map(rows) -> dict[str, float]:
    """Map currency -> units of that currency per 1 EUR.

    Built from role ``"fx"`` rows whose ticker is ``EUR{CUR}=X`` (e.g. EURJPY=X
    -> {"JPY": <last_close>}). These rows are never emitted into any section.
    """
    fx: dict[str, float] = {}
    for r in rows:
        if r.get("role") != "fx":
            continue
        t = _str(r.get("ticker")) or ""
        rate = _num(r.get("last_close"))
        if rate and t.startswith("EUR") and t.endswith("=X"):
            fx[t[3:-2]] = rate
    return fx


def _eur_price(last, ccy, fx):
    """Convert `last` (quoted in `ccy`) to EUR, or None when not convertible."""
    if last is None:
        return None
    if ccy == "EUR":
        return _num(last)
    if not fx:
        return None
    rate = fx.get(ccy)
    return _num(last / rate) if rate else None


def _price_item(row, *, source="yfinance", with_sector=False, fx=None) -> dict:
    r1 = _num(row.get("ret_1d"))
    last = _num(row.get("last_close"))
    ccy = _str(row.get("ccy"), default="USD")
    item = {
        "ticker": _str(row.get("ticker")),
        "name": _str(row.get("name"), default=_str(row.get("ticker"))),
        "last": last,
        "currency": ccy,
        "ret_1d": r1,
        "ret_5d": _num(row.get("ret_5d")),
        "as_of": _iso(row.get("last_date")),
        "source": source,
        "quality": "OK" if r1 is not None else "PARTIAL",
    }
    eur = _eur_price(last, ccy, fx)
    if eur is not None:
        item["eur"] = eur
    if with_sector:
        item["sector"] = _str(row.get("sector_label"))
    return item


def _spark_map(con, n: int = 30) -> dict[str, list]:
    """ticker -> last ~n daily closes (oldest first) for sparklines.

    One grouped query over raw_prices; returns {} if the table is absent so the
    export never fails when history has not been ingested yet.
    """
    from ..db import table_exists

    if not table_exists(con, "raw_prices"):
        return {}
    df = con.execute(
        """
        WITH ranked AS (
            SELECT ticker, date, close,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM raw_prices
        )
        SELECT ticker, close FROM ranked WHERE rn <= ? ORDER BY ticker, date ASC
        """,
        [n],
    ).df()
    out: dict[str, list] = {}
    for row in df.itertuples(index=False):
        out.setdefault(str(row.ticker), []).append(_num(row.close))
    return out


def _attach_spark(items: list[dict], spark_map: dict[str, list]) -> None:
    """Attach a `spark` array to each price item that has recent history."""
    for it in items:
        s = spark_map.get(it.get("ticker"))
        if s:
            it["spark"] = s


def _universe_rows(con):
    return con.execute(
        """
        SELECT g.ticker, d.name, d.ccy, d.role, d.sector_key, d.sector_label,
               g.last_close, g.ret_1d, g.ret_5d, g.last_date
        FROM gold_latest g LEFT JOIN dim_ticker d USING (ticker)
        """
    ).df()


def _sector_order(universe: dict) -> dict[str, dict[str, int]]:
    """Per-sector ``{ticker: position}`` from the universe file (``etfs`` then
    ``names``). Makes the universe file the single source of truth for the
    display order of each sector's members (the SQL SELECT is unordered)."""
    out: dict[str, dict[str, int]] = {}
    for key, sec in (universe.get("sectors", {}) or {}).items():
        order: dict[str, int] = {}
        for entry in (sec.get("etfs", []) or []) + (sec.get("names", []) or []):
            t = config.normalize_entry(entry)["ticker"]
            order.setdefault(t, len(order))
        out[key] = order
    return out


def build_snapshot(con, *, market_state: str = "PRE_OPEN",
                   now: datetime | None = None, quality: str = "OK") -> dict:
    now = now or datetime.now(timezone.utc)
    uni = _universe_rows(con)
    rows = [r._asdict() if hasattr(r, "_asdict") else dict(r) for _, r in uni.iterrows()]
    fx = _fx_eur_map(rows)  # role "fx" rows -> currency conversion map (not rendered)

    indices, futures, etfs, crypto, equities = [], [], [], [], []
    by_sector: dict[str, dict] = {}
    for r in rows:
        role = r.get("role")
        if role == "index":
            indices.append(_price_item(r, fx=fx))
        elif role == "future":
            futures.append(_price_item(r, fx=fx))
        elif role == "fund":
            etfs.append(_price_item(r, fx=fx))
        elif role == "crypto":
            crypto.append(_price_item(r, fx=fx))
        elif role in ("etf", "name"):
            equities.append(r)
            key = r.get("sector_key")
            if key:
                sec = by_sector.setdefault(key, {"key": key, "label": _str(r.get("sector_label")),
                                                 "etfs": [], "constituents": []})
                item = _price_item(r, fx=fx)
                if role == "etf":
                    item["role"] = "etf"
                    sec["etfs"].append(item)
                else:
                    item["role"] = "constituent"
                    sec["constituents"].append(item)

    order_map = _sector_order(config.load_universe())
    sectors = []
    for key in sorted(by_sector):
        sec = by_sector[key]
        # Deterministic member order = universe order (SELECT is unordered).
        order = order_map.get(key, {})
        sec["etfs"].sort(key=lambda it: order.get(it["ticker"], 1_000_000))
        sec["constituents"].sort(key=lambda it: order.get(it["ticker"], 1_000_000))
        members = sec["etfs"] + sec["constituents"]
        r1 = [m["ret_1d"] for m in members if m["ret_1d"] is not None]
        r5 = [m["ret_5d"] for m in members if m["ret_5d"] is not None]
        sec["avg_ret_1d"] = _num(sum(r1) / len(r1)) if r1 else None
        sec["avg_ret_5d"] = _num(sum(r5) / len(r5)) if r5 else None
        sectors.append(sec)

    # Sparklines: last ~30 daily closes per ticker on each price item.
    spark_map = _spark_map(con)
    _attach_spark(indices, spark_map)
    _attach_spark(futures, spark_map)
    _attach_spark(etfs, spark_map)
    _attach_spark(crypto, spark_map)
    for sec in sectors:
        _attach_spark(sec["etfs"], spark_map)
        _attach_spark(sec["constituents"], spark_map)

    # movers = the true biggest winners/losers across all sector CONSTITUENTS
    # (role "name") only — sector/standalone ETFs, indices, futures and crypto
    # are excluded so movers reflects real company stocks.
    scored = [_price_item(r, with_sector=True, fx=fx) for r in equities
              if r.get("role") == "name" and r.get("ret_1d") is not None]
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

    # Phase-2 event sections (central-bank decisions / earnings / catalysts).
    # Each is derived defensively (missing source table -> []) and is deliberately
    # kept OUT of the freshness assessment and the meta data_as_of/oldest window:
    # events are sparse by nature (a CB meeting is ~6 weeks apart, an EO is
    # irregular) and must never flip the EOD market snapshot to STALE.
    uni_cfg = config.load_universe()
    cb_ev = _clean_events("cb_events", fevents.cb_events(con, uni_cfg.get("cb_policy_rates", [])), 12)
    earn = _clean_events("earnings", fevents.upcoming_earnings(con, uni_cfg, now), 24)
    trig = _clean_events("triggers", fevents.recent_triggers(con), 16)

    payload = {
        "indices": indices,
        "futures": futures,
        "etfs": etfs,
        "crypto": crypto,
        "sectors": sectors,
        "rates": rate_items,
        "movers": movers,
        "headlines": headlines,
        "cb_events": cb_ev,
        "earnings": earn,
        "triggers": trig,
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
    for s in ("indices", "futures", "etfs", "crypto"):
        out += [i["as_of"] for i in payload.get(s, []) if i.get("as_of")]
    for sec in payload.get("sectors", []):
        out += [i["as_of"] for i in sec.get("etfs", []) + sec.get("constituents", []) if i.get("as_of")]
    out += [r["as_of"] for r in payload.get("rates", []) if r.get("as_of")]
    return out


def _market_as_of(payload: dict) -> list[str]:
    out: list[str] = []
    for s in ("indices", "futures", "etfs", "crypto"):
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
    eq, ust, nf, bis, seen = ("2026-07-31T20:00:00+00:00", "2026-07-31",
                              "2026-07-30", "2026-07-31", "20260731T120000Z")

    def spark(end, pct_5d, n=24):
        """A gentle ~n-point series ending near `end`, drifting ~pct_5d% overall."""
        start = end / (1 + pct_5d / 100.0) if (1 + pct_5d / 100.0) else end
        out = []
        for i in range(n):
            f = i / (n - 1)
            base = start + (end - start) * f
            out.append(round(base + base * 0.004 * math.sin(i * 1.3), 4))
        return out

    # Rough EUR crosses (units of CUR per 1 EUR) for the demo's per-item EUR price.
    fx = {"USD": 1.1527, "GBP": 0.8551, "JPY": 181.5, "HKD": 9.03, "CNY": 7.76,
          "KRW": 1662.0, "CHF": 0.935}

    def eqi(t, n, last, r1, r5, sector=None, ccy="USD"):
        d = {"ticker": t, "name": n, "last": last, "currency": ccy,
             "ret_1d": r1, "ret_5d": r5, "as_of": eq, "source": "yfinance", "quality": "OK"}
        eur = last if ccy == "EUR" else (
            round(last / fx[ccy], 6) if (ccy in fx and last is not None) else None)
        if eur is not None:
            d["eur"] = eur
        if sector:
            d["sector"] = sector
        return d

    # (ticker, name, last, ret_1d, ret_5d, role, ccy) — real constituent names,
    # non-USD listings tagged with their local currency (EUR/GBP).
    sectors_def = [
        ("oil_gas", "Oil & Gas", 1.8, [
            ("XLE", "Energy Select Sector SPDR", 92.4, 1.8, 3.1, "etf", "USD"),
            ("XOP", "SPDR S&P Oil & Gas E&P", 148.7, 2.0, 3.6, "etf", "USD"),
            ("EXH1.DE", "iShares STOXX Europe 600 Oil & Gas", 42.8, 1.6, 2.9, "etf", "EUR"),
            ("XOM", "Exxon Mobil", 118.2, 2.1, 3.4, "constituent", "USD"),
            ("CVX", "Chevron", 162.5, 1.6, 2.2, "constituent", "USD"),
            ("COP", "ConocoPhillips", 108.9, 1.9, 2.8, "constituent", "USD"),
            ("EQNR", "Equinor", 27.4, 1.2, 1.9, "constituent", "USD"),
            ("SPM.MI", "Saipem", 2.31, 2.4, 4.1, "constituent", "EUR")]),
        ("defense", "Defense & Aerospace", 2.4, [
            ("ITA", "iShares U.S. Aerospace & Defense", 148.0, 2.4, 4.0, "etf", "USD"),
            ("XAR", "SPDR S&P Aerospace & Defense", 168.9, 2.6, 4.3, "etf", "USD"),
            ("DFEN.DE", "VanEck Defense", 58.4, 3.0, 5.6, "etf", "EUR"),
            ("NATO.L", "HANetf Future of Defence", 18.9, 2.7, 4.9, "etf", "USD"),
            ("LMT", "Lockheed Martin", 512.3, 1.9, 2.7, "constituent", "USD"),
            ("RTX", "RTX", 121.4, 2.8, 3.9, "constituent", "USD"),
            ("NOC", "Northrop Grumman", 498.1, 1.7, 2.5, "constituent", "USD"),
            ("LDO.MI", "Leonardo", 24.8, 3.1, 5.2, "constituent", "EUR"),
            ("RHM.DE", "Rheinmetall", 612.5, 3.4, 6.0, "constituent", "EUR")]),
        ("staples", "Consumer Staples", -0.3, [
            ("XLP", "Consumer Staples Select Sector SPDR", 79.1, -0.3, 0.4, "etf", "USD"),
            ("VDC", "Vanguard Consumer Staples", 214.6, -0.2, 0.3, "etf", "USD"),
            ("EXH9.DE", "iShares STOXX Europe 600 Food & Beverage", 132.7, -0.3, 0.2, "etf", "EUR"),
            ("XDWS.DE", "Xtrackers MSCI World Consumer Staples", 38.4, -0.2, 0.3, "etf", "EUR"),
            ("PG", "Procter & Gamble", 168.9, -0.1, 0.2, "constituent", "USD"),
            ("KO", "Coca-Cola", 63.7, -0.4, -0.1, "constituent", "USD"),
            ("PEP", "PepsiCo", 172.4, -0.5, -0.3, "constituent", "USD"),
            ("COST", "Costco", 892.7, 0.2, 0.9, "constituent", "USD"),
            ("WMT", "Walmart", 111.2, 0.3, 0.8, "constituent", "USD"),
            ("MDLZ", "Mondelez", 62.3, -0.2, 0.1, "constituent", "USD"),
            ("NESN.SW", "Nestlé", 81.0, -0.1, 0.2, "constituent", "CHF")]),
        ("software", "Tech Software", 0.9, [
            ("IGV", "iShares Expanded Tech-Software", 92.0, 0.9, 2.3, "etf", "USD"),
            ("WCLD", "WisdomTree Cloud Computing", 34.2, 1.1, 2.7, "etf", "USD"),
            ("XDWT.DE", "Xtrackers MSCI World Information Technology", 68.9, 0.8, 2.0, "etf", "EUR"),
            ("EXV3.DE", "iShares STOXX Europe 600 Technology", 112.4, 0.9, 2.2, "etf", "EUR"),
            ("MSFT", "Microsoft", 452.6, 0.7, 1.8, "constituent", "USD"),
            ("CRM", "Salesforce", 268.1, 1.4, 2.9, "constituent", "USD"),
            ("ORCL", "Oracle", 214.3, 0.8, 2.1, "constituent", "USD"),
            ("NOW", "ServiceNow", 902.4, 1.0, 2.4, "constituent", "USD"),
            ("ADBE", "Adobe", 250.4, 0.6, 1.5, "constituent", "USD"),
            ("INTU", "Intuit", 316.1, 0.9, 2.0, "constituent", "USD"),
            ("SAP.DE", "SAP", 155.9, 0.8, 1.9, "constituent", "EUR")]),
        ("semis", "Tech Hardware / Semis", -1.1, [
            ("SMH", "VanEck Semiconductor", 245.7, -1.1, -2.4, "etf", "USD"),
            ("SOXX", "iShares Semiconductor", 228.9, -1.0, -2.2, "etf", "USD"),
            ("VVSM.DE", "VanEck Semiconductor UCITS", 42.6, -1.1, -2.5, "etf", "EUR"),
            ("AAPL", "Apple", 228.5, -0.4, -0.8, "constituent", "USD"),
            ("NVDA", "Nvidia", 168.3, -1.8, -3.2, "constituent", "USD"),
            ("AMD", "AMD", 172.9, -0.9, -1.7, "constituent", "USD"),
            ("TSM", "TSMC", 188.4, -0.7, -1.4, "constituent", "USD"),
            ("ASML", "ASML", 942.1, -1.2, -2.6, "constituent", "USD"),
            ("MU", "Micron", 118.6, -1.5, -2.9, "constituent", "USD"),
            ("IFX.DE", "Infineon", 33.7, -0.8, -1.9, "constituent", "EUR")]),
        ("industrials", "Infrastructure & Industrials", 0.5, [
            ("XLI", "Industrial Select Sector SPDR", 138.2, 0.5, 1.2, "etf", "USD"),
            ("PAVE", "Global X U.S. Infrastructure Development", 41.8, 0.7, 1.5, "etf", "USD"),
            ("EXH4.DE", "iShares STOXX Europe 600 Industrial Goods & Services", 168.3, 0.6, 1.4, "etf", "EUR"),
            ("CAT", "Caterpillar", 402.5, 0.9, 1.9, "constituent", "USD"),
            ("DE", "Deere", 421.0, 0.4, 0.8, "constituent", "USD"),
            ("HON", "Honeywell", 214.7, 0.3, 0.7, "constituent", "USD"),
            ("GE", "GE Aerospace", 178.9, 0.8, 1.6, "constituent", "USD"),
            ("UNP", "Union Pacific", 292.1, 0.5, 1.1, "constituent", "USD"),
            ("ETN", "Eaton", 415.2, 0.7, 1.5, "constituent", "USD"),
            ("SIE.DE", "Siemens", 281.1, 0.6, 1.4, "constituent", "EUR"),
            ("PRY.MI", "Prysmian", 62.4, 0.6, 1.3, "constituent", "EUR")]),
        ("internet", "Internet & Platforms", 1.3, [
            ("KWEB", "KraneShares CSI China Internet", 32.1, 1.3, 2.8, "etf", "USD"),
            ("FDN", "First Trust Dow Jones Internet", 244.6, 1.0, 2.1, "etf", "USD"),
            ("KWEB.L", "KraneShares CSI China Internet UCITS", 28.7, 1.4, 2.9, "etf", "USD"),
            ("GOOGL", "Alphabet", 356.1, 1.2, 2.5, "constituent", "USD"),
            ("META", "Meta Platforms", 556.7, 1.5, 3.1, "constituent", "USD"),
            ("AMZN", "Amazon", 271.6, 0.9, 1.9, "constituent", "USD"),
            ("NFLX", "Netflix", 1210.0, 0.7, 1.6, "constituent", "USD"),
            ("TCEHY", "Tencent", 54.8, 1.6, 3.4, "constituent", "USD"),
            ("BABA", "Alibaba", 84.2, 1.1, 2.3, "constituent", "USD")]),
        ("financials", "Banks & Financials", 0.7, [
            ("XLF", "Financial Select Sector SPDR", 48.9, 0.7, 1.4, "etf", "USD"),
            ("EUFN", "iShares MSCI Europe Financials", 26.3, 0.9, 1.8, "etf", "USD"),
            ("EXX1.DE", "iShares STOXX Europe 600 Banks", 22.4, 1.1, 2.3, "etf", "EUR"),
            ("JPM", "JPMorgan Chase", 351.8, 0.8, 1.6, "constituent", "USD"),
            ("BAC", "Bank of America", 61.9, 0.9, 1.9, "constituent", "USD"),
            ("GS", "Goldman Sachs", 1018.4, 1.1, 2.2, "constituent", "USD"),
            ("MS", "Morgan Stanley", 210.4, 0.7, 1.5, "constituent", "USD"),
            ("UCG.MI", "UniCredit", 41.6, 1.2, 2.5, "constituent", "EUR"),
            ("ISP.MI", "Intesa Sanpaolo", 6.51, 1.0, 2.1, "constituent", "EUR"),
            ("HSBA.L", "HSBC", 1576.0, 0.6, 1.3, "constituent", "GBP"),
            ("DB", "Deutsche Bank", 18.7, 1.0, 2.0, "constituent", "USD")]),
        ("healthcare", "Healthcare & Pharma", -0.2, [
            ("XLV", "Health Care Select Sector SPDR", 148.3, -0.2, 0.3, "etf", "USD"),
            ("IHE", "iShares U.S. Pharmaceuticals", 62.7, -0.1, 0.4, "etf", "USD"),
            ("EXV4.DE", "iShares STOXX Europe 600 Health Care", 148.6, -0.2, 0.3, "etf", "EUR"),
            ("XDWH.DE", "Xtrackers MSCI World Health Care", 54.2, -0.1, 0.4, "etf", "EUR"),
            ("LLY", "Eli Lilly", 1148.8, 0.4, 1.2, "constituent", "USD"),
            ("UNH", "UnitedHealth", 414.4, -0.6, -1.1, "constituent", "USD"),
            ("JNJ", "Johnson & Johnson", 162.8, -0.1, 0.2, "constituent", "USD"),
            ("MRK", "Merck", 130.2, -0.2, 0.3, "constituent", "USD"),
            ("ABBV", "AbbVie", 250.9, 0.1, 0.6, "constituent", "USD"),
            ("PFE", "Pfizer", 25.0, -0.4, -0.9, "constituent", "USD"),
            ("NVO", "Novo Nordisk", 47.1, -0.8, -1.6, "constituent", "USD"),
            ("AZN", "AstraZeneca", 169.6, 0.2, 0.7, "constituent", "USD"),
            ("NOVN.SW", "Novartis", 126.6, 0.1, 0.4, "constituent", "CHF"),
            ("SNY", "Sanofi", 52.4, -0.3, 0.1, "constituent", "USD")]),
        ("materials", "Materials & Mining", 1.1, [
            ("XME", "SPDR S&P Metals & Mining", 68.4, 1.1, 2.4, "etf", "USD"),
            ("GDX", "VanEck Gold Miners", 42.9, 1.4, 3.0, "etf", "USD"),
            ("EXV6.DE", "iShares STOXX Europe 600 Basic Resources", 58.7, 1.0, 2.2, "etf", "EUR"),
            ("FCX", "Freeport-McMoRan", 62.6, 1.3, 2.7, "constituent", "USD"),
            ("NEM", "Newmont", 93.7, 1.5, 3.1, "constituent", "USD"),
            ("NUE", "Nucor", 257.3, 0.8, 1.7, "constituent", "USD"),
            ("RIO", "Rio Tinto", 96.9, 0.9, 2.0, "constituent", "USD"),
            ("GLEN.L", "Glencore", 3.98, 0.9, 2.1, "constituent", "GBP"),
            ("BHP", "BHP", 58.6, 1.0, 2.2, "constituent", "USD"),
            ("AEM", "Agnico Eagle Mines", 92.7, 1.6, 3.3, "constituent", "USD")]),
        ("utilities", "Utilities & Power", 0.4, [
            ("XLU", "Utilities Select Sector SPDR", 78.2, 0.4, 0.9, "etf", "USD"),
            ("JXI", "iShares Global Utilities", 74.1, 0.3, 0.8, "etf", "USD"),
            ("EXV5.DE", "iShares STOXX Europe 600 Utilities", 42.9, 0.4, 1.0, "etf", "EUR"),
            ("XDWU.DE", "Xtrackers MSCI World Utilities", 46.8, 0.3, 0.9, "etf", "EUR"),
            ("NEE", "NextEra Energy", 86.9, 0.6, 1.3, "constituent", "USD"),
            ("DUK", "Duke Energy", 125.4, 0.3, 0.7, "constituent", "USD"),
            ("SO", "Southern Company", 94.5, 0.4, 0.9, "constituent", "USD"),
            ("D", "Dominion Energy", 69.2, 0.5, 1.0, "constituent", "USD"),
            ("ENEL.MI", "Enel", 9.84, 0.7, 1.5, "constituent", "EUR"),
            ("IBE.MC", "Iberdrola", 20.5, 0.6, 1.2, "constituent", "EUR"),
            ("NG.L", "National Grid", 1190.0, 0.2, 0.5, "constituent", "GBP"),
            ("EOAN.DE", "E.ON", 13.6, 0.5, 1.1, "constituent", "EUR"),
            ("ELI.BR", "Elia Group", 92.4, 0.2, 0.6, "constituent", "EUR")]),
    ]
    sectors = []
    all_eq = []
    for key, label, avg, members in sectors_def:
        etfs, cons = [], []
        for t, n, last, r1, r5, role, ccy in members:
            it = eqi(t, n, last, r1, r5, ccy=ccy)
            it["role"] = role
            (etfs if role == "etf" else cons).append(it)
            # movers rank real company stocks only (constituents), never ETFs.
            if role == "constituent":
                all_eq.append(eqi(t, n, last, r1, r5, sector=label, ccy=ccy))
        r5s = [m["ret_5d"] for m in etfs + cons]
        sectors.append({"key": key, "label": label, "avg_ret_1d": avg,
                        "avg_ret_5d": round(sum(r5s) / len(r5s), 2), "etfs": etfs, "constituents": cons})

    all_eq.sort(key=lambda x: x["ret_1d"], reverse=True)
    payload = {
        "indices": [
            # US
            eqi("^GSPC", "S&P 500", 5620.4, 0.6, 1.4), eqi("^NDX", "Nasdaq 100", 20450.1, -0.2, 0.9),
            eqi("^DJI", "Dow Jones", 41230.7, 0.4, 1.1), eqi("^RUT", "Russell 2000", 2280.5, 1.2, 2.0),
            # Europe
            eqi("^STOXX", "STOXX Europe 600", 524.8, 0.5, 1.3, ccy="EUR"),
            eqi("^STOXX50E", "Euro Stoxx 50", 5012.6, 0.6, 1.5, ccy="EUR"),
            eqi("^GDAXI", "DAX", 18942.3, 0.7, 1.8, ccy="EUR"),
            eqi("^FCHI", "CAC 40", 7614.9, 0.3, 1.0, ccy="EUR"),
            eqi("^FTSE", "FTSE 100", 8284.1, 0.4, 1.1, ccy="GBP"),
            eqi("FTSEMIB.MI", "FTSE MIB", 34120.5, 0.8, 2.0, ccy="EUR"),
            # Asia
            eqi("^N225", "Nikkei 225", 39820.4, -0.5, 0.6, ccy="JPY"),
            eqi("^HSI", "Hang Seng", 17640.2, 1.1, 2.4, ccy="HKD"),
            eqi("000001.SS", "Shanghai Composite", 2984.6, 0.9, 1.7, ccy="CNY"),
            eqi("^KS11", "KOSPI", 2712.8, -0.3, 0.4, ccy="KRW")],
        "futures": [
            eqi("CL=F", "WTI Crude", 78.4, 1.9, 3.0), eqi("BZ=F", "Brent Crude", 82.1, 1.7, 2.8),
            eqi("NG=F", "Nat Gas", 2.94, -1.2, -2.6), eqi("GC=F", "Gold", 2412.6, 0.3, 0.7),
            eqi("SI=F", "Silver", 29.8, 0.6, 1.4), eqi("HG=F", "Copper", 4.32, 0.9, 2.1),
            eqi("PL=F", "Platinum", 968.4, 0.4, 1.0), eqi("ES=F", "S&P e-mini", 5628.0, 0.5, 1.3),
            eqi("NQ=F", "Nasdaq e-mini", 20475.0, -0.2, 0.8), eqi("DX=F", "US Dollar Index", 104.6, 0.1, 0.4),
            eqi("6E=F", "Euro FX", 1.084, -0.1, -0.3), eqi("ZC=F", "Corn", 398.2, -0.7, -1.5),
            eqi("ZW=F", "Wheat", 542.6, -0.4, -1.1), eqi("ZN=F", "10Y T-Note", 110.48, -0.2, -0.5),
            eqi("ZB=F", "30Y T-Bond", 118.16, -0.4, -0.9), eqi("PA=F", "Palladium", 1024.5, 0.5, 1.3),
            eqi("RB=F", "RBOB Gasoline", 2.38, 1.6, 2.7), eqi("6B=F", "British Pound", 1.278, 0.2, 0.5)],
        "etfs": [
            eqi("XDEW.DE", "Xtrackers S&P 500 Equal Weight", 112.4, 0.5, 1.2, ccy="EUR"),
            eqi("XMME.DE", "Xtrackers MSCI EM (acc)", 48.9, 0.8, 1.9, ccy="EUR"),
            eqi("SXRT.DE", "iShares Core Euro Stoxx 50", 168.7, 0.6, 1.5, ccy="EUR"),
            eqi("VWCE.DE", "Vanguard FTSE All-World (acc)", 128.3, 0.4, 1.1, ccy="EUR"),
            eqi("ILF", "iShares Latin America 40", 24.6, 1.0, 2.2),
            eqi("SPY", "SPDR S&P 500", 561.0, 0.6, 1.4), eqi("QQQ", "Invesco QQQ (Nasdaq 100)", 498.4, -0.2, 0.9),
            eqi("VTI", "Vanguard Total Stock Market", 278.9, 0.5, 1.3), eqi("IWM", "iShares Russell 2000", 226.7, 1.2, 2.0),
            eqi("GLD", "SPDR Gold Shares", 223.4, 0.3, 0.7), eqi("SLV", "iShares Silver Trust", 27.3, 0.6, 1.4),
            eqi("TLT", "iShares 20+ Year Treasury Bond", 94.2, -0.4, -1.1),
            eqi("HYG", "iShares iBoxx $ High Yield Corp Bond", 79.6, 0.1, 0.3),
            eqi("LQD", "iShares iBoxx $ Inv Grade Corp Bond", 109.4, -0.2, -0.5),
            eqi("AGG", "iShares Core U.S. Aggregate Bond", 98.7, -0.1, -0.3),
            eqi("EEM", "iShares MSCI Emerging Markets", 44.8, 0.8, 1.9),
            eqi("EFA", "iShares MSCI EAFE", 82.5, 0.5, 1.2)],
        "crypto": [
            eqi("BTC-USD", "Bitcoin", 63120.0, 1.4, 3.2), eqi("ETH-USD", "Ethereum", 1872.4, 0.9, 2.6),
            eqi("SOL-USD", "Solana", 73.2, 2.1, 4.8), eqi("XRP-USD", "XRP", 1.08, -0.6, 1.1),
            eqi("BNB-USD", "BNB", 584.3, 0.7, 1.9)],
        "sectors": sectors,
        "rates": [
            {"series_id": "UST2Y", "name": "US 2Y Treasury Par Yield", "value": 4.28, "chg": 0.05,
             "as_of": ust, "source": "UST", "quality": "OK"},
            {"series_id": "UST5Y", "name": "US 5Y Treasury Par Yield", "value": 4.51, "chg": 0.06,
             "as_of": ust, "source": "UST", "quality": "OK"},
            {"series_id": "UST10Y", "name": "US 10Y Treasury Par Yield", "value": 4.75, "chg": 0.07,
             "as_of": ust, "source": "UST", "quality": "OK"},
            {"series_id": "UST30Y", "name": "US 30Y Treasury Par Yield", "value": 5.27, "chg": 0.06,
             "as_of": ust, "source": "UST", "quality": "OK"},
            {"series_id": "EFFR", "name": "Effective Federal Funds Rate", "value": 3.63, "chg": 0.0,
             "as_of": nf, "source": "NYFED", "quality": "OK"},
            {"series_id": "SOFR", "name": "Secured Overnight Financing Rate", "value": 3.65, "chg": 0.01,
             "as_of": nf, "source": "NYFED", "quality": "OK"},
            {"series_id": "ECBDFR", "name": "ECB Policy Rate (DFR)", "value": 2.00, "chg": 0.0,
             "as_of": bis, "source": "BIS", "quality": "OK"},
            {"series_id": "BOEBR", "name": "BoE Bank Rate", "value": 3.75, "chg": -0.25,
             "as_of": bis, "source": "BIS", "quality": "OK"},
            {"series_id": "BOJPR", "name": "BoJ Policy Rate", "value": 0.50, "chg": 0.0,
             "as_of": bis, "source": "BIS", "quality": "OK"},
            {"series_id": "PBOCLPR1Y", "name": "PBoC Loan Prime Rate 1Y", "value": 3.00, "chg": 0.0,
             "as_of": bis, "source": "BIS", "quality": "OK"},
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
        "cb_events": [
            {"bank": "Fed", "series_id": "USFED", "rate": 3.625, "change_bp": -25,
             "direction": "cut", "as_of": "2026-06-18", "source": "BIS"},
            {"bank": "BCE", "series_id": "ECBDFR", "rate": 2.00, "change_bp": 0,
             "direction": "hold", "as_of": None, "source": "BIS"},
            {"bank": "BoE", "series_id": "BOEBR", "rate": 3.75, "change_bp": -25,
             "direction": "cut", "as_of": "2026-06-19", "source": "BIS"},
            {"bank": "BoJ", "series_id": "BOJPR", "rate": 0.50, "change_bp": 25,
             "direction": "hike", "as_of": "2026-01-24", "source": "BIS"},
            {"bank": "PBoC", "series_id": "PBOCLPR1Y", "rate": 3.00, "change_bp": 0,
             "direction": "hold", "as_of": None, "source": "BIS"},
        ],
        "earnings": [
            {"ticker": "XOM", "name": "Exxon Mobil", "date": "2026-08-03", "eps_estimate": 1.95,
             "sector": "Oil & Gas", "source": "yfinance"},
            {"ticker": "GOOGL", "name": "Alphabet", "date": "2026-08-04", "eps_estimate": 1.90,
             "sector": "Internet & Platforms", "source": "yfinance"},
            {"ticker": "AAPL", "name": "Apple", "date": "2026-08-05", "eps_estimate": 1.42,
             "sector": "Tech Hardware / Semis", "source": "yfinance"},
            {"ticker": "CAT", "name": "Caterpillar", "date": "2026-08-06", "eps_estimate": 4.85,
             "sector": "Infrastructure & Industrials", "source": "yfinance"},
            {"ticker": "LLY", "name": "Eli Lilly", "date": "2026-08-07", "eps_estimate": 6.10,
             "sector": "Healthcare & Pharma", "source": "yfinance"},
            {"ticker": "NEE", "name": "NextEra Energy", "date": "2026-08-10", "eps_estimate": 0.98,
             "sector": "Utilities & Power", "source": "yfinance"},
            {"ticker": "KO", "name": "Coca-Cola", "date": "2026-08-12", "eps_estimate": 0.82,
             "sector": "Consumer Staples", "source": "yfinance"},
            {"ticker": "RHM.DE", "name": "Rheinmetall", "date": "2026-08-14", "eps_estimate": 5.40,
             "sector": "Defense & Aerospace", "source": "yfinance"},
        ],
        "triggers": [
            {"kind": "sec_8k", "title": "NVDA — 8-K (risultati / guidance)", "ticker": "NVDA",
             "date": "2026-07-30",
             "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001045810&type=8-K",
             "topic": "2.02, 7.01", "source": "sec_edgar"},
            {"kind": "sec_8k", "title": "XOM — 8-K (risultati)", "ticker": "XOM",
             "date": "2026-07-29",
             "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000034088&type=8-K",
             "topic": "2.02", "source": "sec_edgar"},
            {"kind": "executive_order",
             "title": "Restoring Trust in the Smithsonian Institution", "ticker": None,
             "date": "2026-07-24",
             "url": "https://www.federalregister.gov/documents/2026/07/29/2026-15357/restoring-trust-in-the-smithsonian-institution",
             "topic": None, "source": "federal_register"},
            {"kind": "executive_order",
             "title": "Securing America's Defense Supply Chains and Ensuring Domestic Acquisition of Critical Materials",
             "ticker": None, "date": "2026-07-20",
             "url": "https://www.federalregister.gov/documents/2026/07/23/2026-15003/securing-americas-defense-supply-chains-and-ensuring-domestic-acquisition-of-critical-materials",
             "topic": None, "source": "federal_register"},
        ],
        "brief": {"markdown": (
            "**Regime: Risk-off** — rotazione verso i difensivi: difesa ed energia "
            "europee guidano, mentre i semiconduttori USA arretrano sul taglio delle "
            "stime di capex sull'AI.\n\n"
            "## Sintesi del giorno\n"
            "- **USA:** S&P 500 +0.6%, Nasdaq 100 -0.2%; small-cap Russell 2000 +1.2% in controtendenza.\n"
            "- **Europa:** DAX +0.7%, FTSE MIB +0.8%, CAC 40 +0.3% — banche e difesa in spolvero.\n"
            "- **Asia:** Hang Seng +1.1% e Shanghai +0.9% in rialzo; Nikkei -0.5%, KOSPI -0.3% deboli.\n"
            "- **Settori:** **Difesa +2.4%** ed **Energia +1.8%** i migliori; **Semiconduttori -1.1%** il peggiore.\n"
            "- **Tassi e banche centrali:** UST 10Y al **4.75%** (+0.07); la **BoE taglia 25pb al 3.75%**, "
            "BCE ferma al 2.00%, BoJ 0.50%, PBoC 3.00%.\n"
            "- **In evidenza:** Rheinmetall +3.4% traina la difesa europea; Bitcoin **$63.120** (+1.4%).\n\n"
            "## Cosa tenere d'occhio\n"
            "**Breve termine (trading)**\n"
            "- Momentum sulla difesa (DFEN, ITA) sulla scia della forza europea.\n"
            "- SMH vicino al supporto dopo il calo dei semiconduttori.\n"
            "- WTI in area $78-80: rottura da monitorare.\n\n"
            "**Lungo termine (investimento)**\n"
            "- Staples e industriali stabili, trend intatto.\n"
            "- Esposizione a difesa ed energia via UCITS europei che allarga la leadership.\n"
            "- Crypto costruttiva sopra il range precedente.\n"
        ), "created_at": eq},
    }
    # Sparklines: full history for indices/futures/etfs/crypto, plus the first
    # couple of instruments in each sector so the frontend has constituent data.
    for it in payload["indices"] + payload["futures"] + payload["etfs"] + payload["crypto"]:
        it["spark"] = spark(it["last"], it["ret_5d"])
    for sec in payload["sectors"]:
        for it in (sec["etfs"] + sec["constituents"])[:2]:
            it.setdefault("spark", spark(it["last"], it["ret_5d"]))
    payload["meta"] = _build_meta(payload, market_state="PRE_OPEN", now=now,
                                  quality="OK", is_demo=True)
    return payload
