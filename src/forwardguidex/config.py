"""Configuration: environment settings + investment-universe loading."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
BRIEF_DIR = DATA_DIR / "briefs"
CONFIG_DIR = ROOT / "config"
LEGAL_DIR = ROOT / "legal"
UNIVERSE_PATH = CONFIG_DIR / "universe.yaml"
SOURCES_PATH = CONFIG_DIR / "sources.yaml"

# Valid source-rights deployment modes (mirrors config/sources.yaml).
DEPLOYMENT_MODES = (
    "LOCAL_DEMO",
    "PRIVATE_PERSONAL",
    "PUBLIC_NONCOMMERCIAL",
    "PUBLIC_COMMERCIAL",
)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    deployment_mode: str
    openrouter_api_key: str | None
    openrouter_model: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    price_period: str
    firestore_project: str | None
    firestore_collection: str


@lru_cache
def get_settings() -> Settings:
    db_path = Path(os.getenv("FGX_DB_PATH", str(DATA_DIR / "forwardguidex.duckdb")))
    return Settings(
        db_path=db_path,
        deployment_mode=os.getenv("FGX_DEPLOYMENT_MODE", "PRIVATE_PERSONAL"),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        price_period=os.getenv("FGX_PRICE_PERIOD", "1y"),
        firestore_project=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("FGX_FIRESTORE_PROJECT"),
        firestore_collection=os.getenv("FGX_FIRESTORE_COLLECTION", "snapshots_history"),
    )


@lru_cache
def load_universe() -> dict:
    with open(UNIVERSE_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache
def load_sources() -> dict:
    """Load the source-rights policy (config/sources.yaml)."""
    with open(SOURCES_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def normalize_entry(entry: str | dict) -> dict:
    """Normalize a universe entry to ``{ticker, name, ccy}``.

    Entries may be a bare ticker string (``"NVDA"``) or a mapping
    (``{ticker, name, ccy}``). ``ccy`` defaults to ``"USD"`` and ``name`` to
    ``None`` — currencies are controlled by our static config, never yfinance.
    """
    if isinstance(entry, str):
        return {"ticker": entry, "name": None, "ccy": "USD"}
    return {
        "ticker": entry["ticker"],
        "name": entry.get("name"),
        "ccy": entry.get("ccy", "USD"),
    }


def all_price_tickers(universe: dict | None = None) -> list[str]:
    """Flat, de-duplicated list of every ticker we pull prices for."""
    u = universe or load_universe()
    entries: list[str | dict] = []
    entries += u.get("indexes", [])
    entries += u.get("futures", [])
    entries += u.get("etfs", [])
    entries += u.get("crypto", [])
    entries += u.get("fx_eur", [])
    for sec in u.get("sectors", {}).values():
        entries += sec.get("etfs", []) + sec.get("names", [])
    seen, out = set(), []
    for e in entries:
        t = normalize_entry(e)["ticker"]
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def ticker_dimension(universe: dict | None = None) -> list[dict]:
    """Rows for the dim_ticker table: ticker -> name + ccy + role + sector."""
    u = universe or load_universe()
    rows: list[dict] = []

    def _row(entry, role, sector_key=None, sector_label=None):
        e = normalize_entry(entry)
        return {"ticker": e["ticker"], "name": e["name"], "ccy": e["ccy"],
                "role": role, "sector_key": sector_key, "sector_label": sector_label}

    for x in u.get("indexes", []):
        rows.append(_row(x, "index"))
    for x in u.get("futures", []):
        rows.append(_row(x, "future"))
    for x in u.get("etfs", []):
        rows.append(_row(x, "fund"))
    for x in u.get("crypto", []):
        rows.append(_row(x, "crypto"))
    for x in u.get("fx_eur", []):
        rows.append(_row(x, "fx"))  # role "fx": conversion source, never rendered
    for key, sec in u.get("sectors", {}).items():
        label = sec.get("label")
        for t in sec.get("etfs", []):
            rows.append(_row(t, "etf", key, label))
        for t in sec.get("names", []):
            rows.append(_row(t, "name", key, label))
    return rows
