"""Fail-closed validation of a candidate EOD snapshot.

Gates (any failure => non-zero exit / non-empty error list):
  1. NaN/Inf rejected at JSON parse (parse_constant).
  2. JSON Schema (Draft 2020-12) + FormatChecker, additionalProperties:false, bounds.
  3. https-only for headline URLs + no http:// links in the brief.
  4. RFC3339 tz-aware meta timestamps; item as_of parseable.
  5. range / anomaly checks (last>0, rate sanity, |return| sanity).
  6. coverage (indices/sectors/rates present).
  7. per-asset-class freshness via exchange calendar -> must be FRESH.
  8. source-rights policy gate for the deployment mode.
  9. is_demo guard (demo only allowed in LOCAL_DEMO mode).
 10. size < 750 KiB (target 500).
 11. manifest cross-check: raw-bytes SHA-256 == artifact_sha256; content_hash matches.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from ..config import get_settings
from . import calendar as fcal
from . import rights
from . import snapshot as snap

SCHEMA_PATH = Path(__file__).with_name("snapshot_schema.json")
MAX_BYTES = 750 * 1024
TARGET_BYTES = 500 * 1024

_META_TS = ("generated_at", "data_as_of", "oldest_required_source_as_of",
            "source_received_at", "freshness_checked_at")


class _NaNReject(ValueError):
    pass


def _reject_constant(x):
    raise _NaNReject(f"non-finite JSON constant: {x}")


_schema_cache = None


def load_schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _schema_cache


# --------------------------------------------------------------------------- #
def _schema_errors(payload: dict) -> list[str]:
    validator = Draft202012Validator(load_schema(), format_checker=FormatChecker())
    out = []
    for e in sorted(validator.iter_errors(payload), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in e.path) or "<root>"
        out.append(f"schema[{loc}]: {e.message}")
    return out


def _timestamp_errors(payload: dict) -> list[str]:
    errs = []
    meta = payload.get("meta", {})
    for k in _META_TS:
        v = meta.get(k)
        d = fcal.parse_dt(v)
        if d is None:
            errs.append(f"meta.{k}: unparseable timestamp {v!r}")
        elif d.tzinfo is None:
            errs.append(f"meta.{k}: timestamp not timezone-aware {v!r}")
    # item as_of parseability (best-effort; nulls allowed)
    def check_items(items, where):
        for i, it in enumerate(items or []):
            a = it.get("as_of")
            if a is not None and fcal.parse_dt(a) is None:
                errs.append(f"{where}[{i}].as_of: unparseable {a!r}")
    check_items(payload.get("indices"), "indices")
    check_items(payload.get("futures"), "futures")
    check_items(payload.get("etfs"), "etfs")
    check_items(payload.get("crypto"), "crypto")
    for si, sec in enumerate(payload.get("sectors", []) or []):
        check_items(sec.get("etfs"), f"sectors[{si}].etfs")
        check_items(sec.get("constituents"), f"sectors[{si}].constituents")
    check_items(payload.get("rates"), "rates")
    return errs


def _https_errors(payload: dict) -> list[str]:
    errs = []
    for i, h in enumerate(payload.get("headlines", []) or []):
        url = h.get("url") or ""
        if not url.startswith("https://"):
            errs.append(f"headlines[{i}].url: not https ({url[:40]!r})")
    for i, t in enumerate(payload.get("triggers", []) or []):
        url = t.get("url") or ""
        if not url.startswith("https://"):
            errs.append(f"triggers[{i}].url: not https ({url[:40]!r})")
    md = (payload.get("brief", {}) or {}).get("markdown", "") or ""
    for m in re.findall(r"http://[^\s)\"'>]+", md):
        errs.append(f"brief.markdown: insecure http:// link {m[:60]!r}")
    return errs


def _anomaly_errors(payload: dict) -> list[str]:
    errs = []

    def scan_price(items, where):
        for i, it in enumerate(items or []):
            last = it.get("last")
            if last is not None and last <= 0:
                errs.append(f"{where}[{i}].last: non-positive price {last}")
            for f in ("ret_1d", "ret_5d"):
                v = it.get(f)
                if v is not None and abs(v) > 80:
                    errs.append(f"{where}[{i}].{f}: implausible return {v}")

    scan_price(payload.get("indices"), "indices")
    scan_price(payload.get("futures"), "futures")
    scan_price(payload.get("etfs"), "etfs")
    scan_price(payload.get("crypto"), "crypto")
    for si, sec in enumerate(payload.get("sectors", []) or []):
        scan_price(sec.get("etfs"), f"sectors[{si}].etfs")
        scan_price(sec.get("constituents"), f"sectors[{si}].constituents")
    for i, r in enumerate(payload.get("rates", []) or []):
        v = r.get("value")
        if v is not None and not (-5 <= v <= 30):
            errs.append(f"rates[{i}].value: implausible rate {v}")
    return errs


def _coverage_errors(payload: dict) -> list[str]:
    errs = []
    if len(payload.get("indices", []) or []) < 1:
        errs.append("coverage: no indices")
    if len(payload.get("sectors", []) or []) < 1:
        errs.append("coverage: no sectors")
    if len(payload.get("rates", []) or []) < 1:
        errs.append("coverage: no rates")
    movers = payload.get("movers", {}) or {}
    if len(movers.get("gainers", []) or []) < 1 and len(movers.get("losers", []) or []) < 1:
        errs.append("coverage: no movers")
    return errs


# Required source-health domains: a total failure or fallback here cannot ship
# as `quality=OK` / `freshness=FRESH`. Extend as we instrument more providers.
_REQUIRED_HEALTH_DOMAINS = ("gdelt",)


def _source_health_errors(payload: dict) -> list[str]:
    """Reject values, not only field presence.

    Enforces the P0.1 remediation rules:
      * ``quality=OK`` forbidden when a required domain is FAILED.
      * ``freshness=FRESH`` forbidden when any domain is a STALE_FALLBACK.
      * ``news.rows=0`` forbidden when GDELT reports any failure.
      * request/success/failure counts must be internally consistent.
    """
    errs: list[str] = []
    meta = payload.get("meta", {}) or {}
    sh = meta.get("source_health") or {}
    quality = meta.get("quality")
    freshness = meta.get("freshness")

    for domain in _REQUIRED_HEALTH_DOMAINS:
        info = sh.get(domain)
        if info is None:
            continue
        status = info.get("status")
        attempted = info.get("attempted_queries") or 0
        success = info.get("successful_queries") or 0
        failed = info.get("failed_queries") or 0
        rate_limited = info.get("rate_limited_queries") or 0
        rows = info.get("rows") or 0

        if status == "FAILED" and quality == "OK":
            errs.append(f"source_health.{domain}: status=FAILED but meta.quality=OK")
        if status == "STALE_FALLBACK" and freshness == "FRESH":
            errs.append(
                f"source_health.{domain}: STALE_FALLBACK but meta.freshness=FRESH")
        if success + failed > attempted:
            errs.append(
                f"source_health.{domain}: success({success})+failed({failed}) > "
                f"attempted({attempted})")
        if rate_limited > attempted:
            errs.append(
                f"source_health.{domain}: rate_limited_queries({rate_limited}) > "
                f"attempted_queries({attempted})")
        if domain == "gdelt":
            if attempted > 0 and success == 0 and rows > 0:
                errs.append(
                    "source_health.gdelt: rows>0 but successful_queries=0 — inconsistent")
            if failed > 0 and rows == 0 and quality == "OK":
                errs.append(
                    "source_health.gdelt: every query failed AND rows=0 but meta.quality=OK")
    return errs


def _freshness_errors(payload: dict, now: datetime | None) -> list[str]:
    fr = fcal.assess_snapshot(payload, now=now)
    if fr.overall != "FRESH":
        stale = [f"{c.asset_class}={c.status}" for c in fr.classes if c.status != "FRESH"]
        return [f"freshness: overall {fr.overall} ({', '.join(stale)})"]
    return []


def _rights_errors(payload: dict, mode: str) -> list[str]:
    return [f"rights: {v}" for v in rights.enforce(mode, snapshot=payload)]


def _manifest_errors(payload: dict, raw: bytes, manifest_path: Path | None,
                     snap_path: Path) -> list[str]:
    if manifest_path is None:
        cand = snap_path.parent / "latest.json"
        manifest_path = cand if cand.exists() else None
    if manifest_path is None:
        return []  # nothing to cross-check
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"manifest: unreadable {manifest_path.name}: {e}"]
    errs = []
    art = hashlib.sha256(raw).hexdigest()
    if manifest.get("artifact_sha256") != art:
        errs.append("manifest.artifact_sha256 != sha256(snapshot bytes)")
    if manifest.get("snapshot") not in (snap_path.name, None) and \
            manifest.get("snapshot") != snap_path.name:
        errs.append(f"manifest.snapshot {manifest.get('snapshot')!r} != file {snap_path.name!r}")
    recomputed = snap.compute_content_hash(payload)
    if payload.get("meta", {}).get("content_hash") != recomputed:
        errs.append("meta.content_hash != recomputed canonical content hash")
    if manifest.get("content_hash") != recomputed:
        errs.append("manifest.content_hash != recomputed canonical content hash")
    # The manifest is a public claim ABOUT the snapshot, so every field it
    # restates must actually match the snapshot. Hashes alone were not enough:
    # a hand-built manifest once shipped `generated_at` copied from the archive
    # record's `deployed_at` — same bytes, same hashes, wrong timestamp — and
    # validated clean all the way to production. Anything `build_manifest()`
    # copies out of `meta` gets cross-checked here.
    meta = payload.get("meta", {}) or {}
    for field in ("generated_at", "schema_version"):
        if manifest.get(field) != meta.get(field):
            errs.append(
                f"manifest.{field} {manifest.get(field)!r} != "
                f"meta.{field} {meta.get(field)!r}"
            )
    return errs


# --------------------------------------------------------------------------- #
def validate_payload(payload: dict, raw: bytes, *, mode: str, now: datetime | None,
                     snap_path: Path, manifest_path: Path | None) -> list[str]:
    errors: list[str] = []
    # size
    if len(raw) >= MAX_BYTES:
        errors.append(f"size: {len(raw)} bytes >= {MAX_BYTES} (750 KiB ceiling)")
    # schema first (structural)
    schema_errs = _schema_errors(payload)
    errors += schema_errs
    # semantic checks run regardless (surface as much as possible)
    errors += _timestamp_errors(payload)
    errors += _https_errors(payload)
    errors += _anomaly_errors(payload)
    errors += _coverage_errors(payload)
    errors += _freshness_errors(payload, now)
    errors += _source_health_errors(payload)
    errors += _rights_errors(payload, mode)
    # demo guard
    is_demo = bool(payload.get("meta", {}).get("is_demo"))
    if is_demo and mode != "LOCAL_DEMO":
        errors.append(f"is_demo=true not allowed in mode {mode}")
    # manifest cross-check
    errors += _manifest_errors(payload, raw, manifest_path, snap_path)
    return errors


def validate_snapshot_file(path, *, mode: str | None = None, now: datetime | None = None,
                           manifest_path=None) -> list[str]:
    snap_path = Path(path)
    mode = mode or get_settings().deployment_mode
    if not snap_path.exists():
        return [f"file not found: {snap_path}"]
    raw = snap_path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except _NaNReject as e:
        return [f"json: {e}"]
    except Exception as e:  # noqa: BLE001
        return [f"json: not parseable: {e}"]
    mp = Path(manifest_path) if manifest_path else None
    return validate_payload(payload, raw, mode=mode, now=now, snap_path=snap_path,
                            manifest_path=mp)


def main(argv=None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="fwdx validate", description="Validate an EOD snapshot")
    p.add_argument("path")
    p.add_argument("--mode", default=None, help="deployment mode override")
    p.add_argument("--manifest", default=None, help="path to latest.json (else sibling)")
    args = p.parse_args(argv)

    errors = validate_snapshot_file(args.path, mode=args.mode, manifest_path=args.manifest)
    if errors:
        print(f"INVALID ({len(errors)} error(s)):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    size = Path(args.path).stat().st_size
    warn = "  [over 500 KiB target]" if size >= TARGET_BYTES else ""
    print(f"VALID: {args.path} ({size} bytes){warn}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
