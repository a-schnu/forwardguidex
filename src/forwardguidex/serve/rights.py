"""Source-rights policy: load config/sources.yaml, compute the (source, use)
pairs a candidate snapshot needs, and enforce them for a deployment mode.

Reused by the validator (gate), the snapshot builder (attribution block), and
telegram (NY Fed disclaimer). Uses are *distinct*: a dashboard approval does not
imply telegram/ai_input/persistence, and vice-versa.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .. import config

# Abstract uses a snapshot can perform.
PERSISTENCE = "persistence"
AI_INPUT = "ai_input"
DASHBOARD = "dashboard"
TELEGRAM = "telegram"

# Map the raw per-item `source` tokens used in the warehouse/snapshot to the
# policy keys in config/sources.yaml.
_SOURCE_KEY = {
    "yfinance": "yfinance",
    "UST": "us_treasury",
    "us_treasury": "us_treasury",
    "NYFED": "ny_fed",
    "ny_fed": "ny_fed",
    "GDELT": "gdelt",
    "gdelt": "gdelt",
}


class RightsError(Exception):
    """Raised when the source-rights gate rejects a snapshot for a mode."""


@dataclass(frozen=True)
class Violation:
    source: str
    use: str
    reason: str

    def __str__(self) -> str:
        return f"{self.source} / {self.use}: {self.reason}"


def normalize_source(token: str) -> str | None:
    return _SOURCE_KEY.get(token)


def concrete_use(abstract_use: str, mode: str) -> str:
    """Resolve an abstract use to the concrete allowed_uses token for `mode`.

    The dashboard token depends on public vs private; telegram is always the
    owner's private channel regardless of dashboard visibility.
    """
    if abstract_use == DASHBOARD:
        return "public_dashboard" if mode.startswith("PUBLIC") else "private_dashboard"
    if abstract_use == TELEGRAM:
        return "private_telegram"
    return abstract_use  # persistence, ai_input


def sources_in_snapshot(snapshot: dict) -> set[str]:
    """Set of policy source keys that actually appear in a built snapshot."""
    keys: set[str] = set()

    def add(tok):
        k = normalize_source(tok) if tok else None
        if k:
            keys.add(k)

    for section in ("indices", "futures"):
        for item in snapshot.get(section, []) or []:
            add(item.get("source"))
    for sec in snapshot.get("sectors", []) or []:
        for item in (sec.get("etfs", []) or []) + (sec.get("constituents", []) or []):
            add(item.get("source"))
    for r in snapshot.get("rates", []) or []:
        add(r.get("source"))
    if snapshot.get("headlines"):
        keys.add("gdelt")
    return keys


def required_pairs(snapshot: dict, *, persist: bool = True, ai_input: bool = True,
                   dashboard: bool = True, telegram: bool = True) -> set[tuple[str, str]]:
    """(source_key, abstract_use) pairs a candidate snapshot needs.

    Channels can be toggled: e.g. a pure export with no telegram send passes
    telegram=False so telegram rights are not required.
    """
    present = sources_in_snapshot(snapshot)
    uses: list[str] = []
    if persist:
        uses.append(PERSISTENCE)
    if ai_input:
        uses.append(AI_INPUT)
    if dashboard:
        uses.append(DASHBOARD)
    if telegram:
        uses.append(TELEGRAM)
    return {(src, use) for src in present for use in uses}


def _parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def enforce(mode: str, snapshot: dict | None = None,
            pairs: set[tuple[str, str]] | None = None,
            today: date | None = None, policy: dict | None = None) -> list[Violation]:
    """Return a list of rights violations (empty = pass) for `mode`."""
    policy = policy or config.load_sources()
    today = today or date.today()
    sources = policy.get("sources", {})

    violations: list[Violation] = []
    if mode not in config.DEPLOYMENT_MODES:
        violations.append(Violation("<policy>", "-", f"unknown deployment_mode '{mode}'"))
        return violations

    if pairs is None:
        if snapshot is None:
            raise ValueError("enforce() needs either `snapshot` or `pairs`")
        pairs = required_pairs(snapshot)

    for src, abstract in sorted(pairs):
        spec = sources.get(src)
        if spec is None:
            violations.append(Violation(src, abstract, "source not in sources.yaml"))
            continue
        if spec.get("approval_status") != "approved":
            violations.append(Violation(src, abstract,
                              f"approval_status={spec.get('approval_status')!r} (not approved)"))
        if not spec.get("evidence_reference"):
            violations.append(Violation(src, abstract, "missing evidence_reference"))
        exp = _parse_date(spec.get("review_expires_at"))
        if exp is None:
            violations.append(Violation(src, abstract, "missing/invalid review_expires_at"))
        elif exp < today:
            violations.append(Violation(src, abstract, f"review expired {exp.isoformat()}"))
        allowed_modes = spec.get("allowed_modes", [])
        if mode not in allowed_modes:
            violations.append(Violation(src, abstract, f"mode {mode} not in allowed_modes"))
        cu = concrete_use(abstract, mode)
        if cu not in (spec.get("allowed_uses", []) or []):
            violations.append(Violation(src, abstract, f"use '{cu}' not in allowed_uses"))
    return violations


def check(mode: str, snapshot: dict | None = None, **kw) -> None:
    """Raise RightsError if the snapshot violates the policy for `mode`."""
    violations = enforce(mode, snapshot=snapshot, **kw)
    if violations:
        lines = "\n".join(f"  - {v}" for v in violations)
        raise RightsError(f"source-rights gate rejected mode {mode}:\n{lines}")


# --- Attribution / disclaimer text (version-controlled; never LLM-generated) ---

def _read_notice(rel_path: str) -> str:
    p = config.ROOT / rel_path
    return p.read_text(encoding="utf-8").strip()


def attribution_block(source_keys: set[str], policy: dict | None = None) -> dict[str, str]:
    """meta.attribution mapping for the sources present in a snapshot.

    Treasury -> attribution text; NY Fed -> verbatim disclaimer text.
    """
    policy = policy or config.load_sources()
    sources = policy.get("sources", {})
    out: dict[str, str] = {}
    if "us_treasury" in source_keys:
        spec = sources.get("us_treasury", {})
        f = spec.get("attribution_file", "legal/us-treasury.txt")
        out["us_treasury"] = _read_notice(f)
    if "ny_fed" in source_keys:
        spec = sources.get("ny_fed", {})
        f = spec.get("disclaimer_file", "legal/nyfed-reference-rates.txt")
        out["ny_fed"] = _read_notice(f)
    return out


def nyfed_disclaimer(policy: dict | None = None) -> str:
    policy = policy or config.load_sources()
    spec = policy.get("sources", {}).get("ny_fed", {})
    return _read_notice(spec.get("disclaimer_file", "legal/nyfed-reference-rates.txt"))


def treasury_attribution(policy: dict | None = None) -> str:
    policy = policy or config.load_sources()
    spec = policy.get("sources", {}).get("us_treasury", {})
    return _read_notice(spec.get("attribution_file", "legal/us-treasury.txt"))
