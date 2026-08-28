"""ForwardGuidex command-line interface (entry point: `fwdx`)."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from . import db
from .config import get_settings
from .serve import telegram
from .transform import marts

# Legacy FRED identifiers to purge in `decommission-fred`.
_FRED_SERIES = [
    "DGS2", "DGS10", "DGS30", "T10Y2Y", "FEDFUNDS", "DFF",
    "IRLTLT01DEM156N", "IRLTLT01JPM156N", "IRLTLT01ITM156N", "IRLTLT01CAM156N",
]


def cmd_init(_args) -> None:
    con = db.connect()
    db.build_dim_ticker(con)
    print(f"Initialized DB at {get_settings().db_path}")


def _ingest(which: str) -> None:
    # Lazy imports: markets pulls in yfinance (heavy) — only load what's needed so
    # `export`/`validate` work without the ingest deps installed.
    con = db.connect()
    if which in ("markets", "all"):
        from .ingest import markets
        print(f"[markets] rows: {markets.ingest_markets(con)}")
    if which in ("rates", "all"):
        from .ingest import rates as rates_ingest
        print(f"[rates] rows: {rates_ingest.ingest_rates(con)}")
    if which in ("news", "all"):
        from .ingest import news as news_ingest
        report = news_ingest.ingest_news_with_report(con)
        print(
            f"[news] rows: {report.rows} "
            f"(status={report.status}, ok={report.successful_queries}/{report.attempted_queries}, "
            f"rate_limited={report.rate_limited_queries})"
        )
    # Earnings + triggers are SUPPLEMENTARY event sources over external APIs
    # (yfinance / Federal Register / SEC). A failure there must never block the
    # core market-data pipeline/deploy, so they are non-fatal in the aggregate run
    # (mirrors the non-fatal brief). Their sections simply stay empty and the
    # frontend hides them until the next successful ingest.
    fatal = which in ("earnings", "triggers", "events")  # explicit single-source runs surface errors
    if which in ("earnings", "events", "all"):
        from .ingest import earnings as earnings_ingest
        try:
            print(f"[earnings] rows: {earnings_ingest.ingest_earnings(con)}")
        except Exception as exc:
            if fatal:
                raise
            print(f"[earnings] skipped — source unavailable: {exc}")
    if which in ("triggers", "events", "all"):
        from .ingest import triggers as triggers_ingest
        try:
            print(f"[triggers] rows: {triggers_ingest.ingest_triggers(con)}")
        except Exception as exc:
            if fatal:
                raise
            print(f"[triggers] skipped — source unavailable: {exc}")


def cmd_ingest(args) -> None:
    _ingest(args.source)


def cmd_marts(_args) -> None:
    marts.build_marts(db.connect())
    print("Marts rebuilt.")


def cmd_brief(_args) -> None:
    from .intelligence.brief import build_brief
    print(build_brief(db.connect()))


def _send_brief(text: str) -> bool:
    # Prefer send_brief (appends Treasury + NY Fed notices) when available.
    sender = getattr(telegram, "send_brief", telegram.send_message)
    return sender(text)


def cmd_send_brief(_args) -> None:
    from .intelligence.brief import build_brief
    _send_brief(build_brief(db.connect()))
    print("Brief sent.")


def cmd_run_daily(_args) -> None:
    from .intelligence.brief import build_brief
    con = db.connect()
    _ingest("all")
    marts.build_marts(con)
    # The brief is one section of the snapshot; an LLM outage (e.g. 429) must not
    # block the market-data export/deploy. Skip it on failure and carry on.
    try:
        _send_brief(build_brief(con))
    except Exception as exc:  # noqa: BLE001
        print(f"[brief] skipped — LLM unavailable: {exc}")
    print("Daily run complete.")


def cmd_export(args) -> None:
    from .serve import snapshot as snap

    if args.demo:
        payload = snap.demo_snapshot()
        manifest = snap.write_bundle(payload, args.out_dir, demo=True)
    else:
        con = db.connect(read_only=True)
        payload = snap.build_snapshot(con, market_state=args.market_state)
        manifest = snap.write_bundle(payload, args.out_dir, demo=False)
    print(f"Exported {manifest['snapshot']} + "
          f"{'latest.demo.json' if args.demo else 'latest.json'} to {args.out_dir}")
    print(f"  artifact_sha256: {manifest['artifact_sha256']}")
    print(f"  content_hash:    {manifest['content_hash']}")


def cmd_validate(args) -> None:
    from .serve import validate as V

    errors = V.validate_snapshot_file(args.path, mode=args.mode, manifest_path=args.manifest)
    if errors:
        print(f"INVALID ({len(errors)} error(s)):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(1)
    print(f"VALID: {args.path}")


def _release_status(value: str) -> str:
    """argparse ``type`` for ``--release-status``.

    Also guards the *default*: argparse runs ``type`` over a string default that
    was not overridden on the command line, so a bogus ``FGX_RELEASE_STATUS``
    fails the parse instead of being written to a permanent, create-only record.
    """
    from .serve.publish import validate_release_status

    try:
        return validate_release_status(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def cmd_publish(args) -> None:
    from pathlib import Path

    from .serve import publish

    manifest = args.manifest
    if manifest is None:
        sibling = Path(args.path).parent / "latest.json"
        manifest = str(sibling) if sibling.exists() else None
    # Re-validate: `cmd_publish` is also reachable without going through the
    # argparse `type` hook, and an unknown status must never reach Firestore.
    release_status = publish.validate_release_status(args.release_status)
    provenance = {
        "deployment_id": os.getenv("FGX_DEPLOYMENT_ID") or os.getenv("GITHUB_RUN_ID", "local"),
        "workflow_run_id": os.getenv("GITHUB_RUN_ID", "local"),
        "git_commit": os.getenv("GITHUB_SHA", "local"),
        "release_status": release_status,
        "deployed_at": datetime.now(timezone.utc).isoformat(),
    }
    print(f"Archiving with release_status={release_status}")
    result = publish.archive(args.path, manifest_path=manifest, provenance=provenance)
    print(f"Archived: {result}")
    if result.status_mismatch:
        # publish.archive() already emitted the machine-readable ::warning::;
        # spell it out here so a human reading the CLI output cannot miss that
        # the archive does NOT say what this run asked it to say.
        adjective = "UNDERSTATES" if result.status_understated else "diverges from"
        print(
            f"WARNING: archived document {result.doc_id} already existed and "
            f"keeps release_status={result.stored_release_status!r}; this run "
            f"requested {result.requested_release_status!r}. The stored record "
            f"{adjective} this run. The writer identity is create-only, so the "
            f"value was NOT and CANNOT be updated (no corrective write is "
            f"attempted, by design)."
        )


def cmd_retrieve_lkg(args) -> None:
    """Materialize the archived last-known-good snapshot into a directory.

    The deploy-app workflow uses this to redeploy app/ with the last snapshot
    that was actually proven live, without rebuilding data. It exists as a CLI
    command so that `publish.retrieve_last_known_good` — which is unit-tested
    and enforces the full record contract — is the ONE implementation of
    "which snapshot is last-known-good", instead of being paraphrased in YAML.

    Fails closed (exit 1) when no record qualifies: per R4-5.b we never fall
    back to DEMO data, and never to a VALIDATED_NOT_DEPLOYED record.
    """
    from .serve import publish

    record = publish.retrieve_last_known_good(collection=args.collection)
    if record is None:
        print(
            "No SMOKE_TESTED, non-demo snapshot found in the Firestore archive. "
            "Refusing to deploy — never falling back to DEMO data.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    manifest = publish.write_last_known_good(record, args.out_dir)
    metadata = record["metadata"]
    print(f"Resolved last-known-good {manifest['snapshot']} into {args.out_dir}")
    print(f"  artifact_sha256: {manifest['artifact_sha256']}")
    print(f"  generated_at:    {manifest['generated_at']} (data)")
    print(f"  deployed_at:     {metadata.get('deployed_at')} (archive)")
    print(f"  git_commit:      {metadata.get('git_commit')}")


def cmd_decommission_fred(args) -> None:
    """One-time FRED decommission (idempotent). Logs what it removed."""
    con = db.connect()
    removed = 0
    if db.table_exists(con, "raw_macro"):
        placeholders = ",".join("?" for _ in _FRED_SERIES)
        before = con.execute("SELECT COUNT(*) FROM raw_macro").fetchone()[0]
        con.execute(
            # `placeholders` is a generated run of `?`; the values themselves are bound.
            f"DELETE FROM raw_macro WHERE source LIKE 'FRED%' OR series_id IN ({placeholders})",  # noqa: S608
            _FRED_SERIES,
        )
        after = con.execute("SELECT COUNT(*) FROM raw_macro").fetchone()[0]
        removed = before - after
    print(f"[fred] raw_macro rows removed: {removed}")

    # brief_history rows may reference FRED-derived context; purge on request.
    if db.table_exists(con, "brief_history"):
        n = con.execute("SELECT COUNT(*) FROM brief_history").fetchone()[0]
        if args.purge_briefs:
            con.execute("DELETE FROM brief_history")
            print(f"[fred] brief_history rows purged: {n}")
        elif n:
            print(f"[fred] brief_history has {n} row(s) predating the source change; "
                  f"re-run with --purge-briefs to remove them")

    # Firestore archives containing FRED (best-effort; needs firebase-admin + WIF).
    try:
        from .serve import publish

        q = getattr(publish, "quarantine_fred_archives", None)
        if q is not None:
            print(f"[fred] firestore archives quarantined: {q()}")
        else:
            print("[fred] firestore quarantine helper not available; skipping")
    except Exception as exc:  # noqa: BLE001
        print(f"[fred] firestore quarantine skipped: {exc}")

    # Residual-source assertion: nothing with source LIKE 'FRED%' may remain.
    residual = 0
    if db.table_exists(con, "raw_macro"):
        residual = con.execute(
            "SELECT COUNT(*) FROM raw_macro WHERE source LIKE 'FRED%'"
        ).fetchone()[0]
    print("[fred] reminder: remove FRED_API_KEY from env/secrets (already dropped from .env.example)")
    if residual:
        print(f"[fred] ERROR: {residual} residual FRED record(s) remain", file=sys.stderr)
        raise SystemExit(1)
    print("[fred] decommission complete (no residual FRED records).")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="fwdx", description="ForwardGuidex CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create DB + ticker dimension").set_defaults(func=cmd_init)
    pi = sub.add_parser("ingest", help="pull source data")
    pi.add_argument("source", nargs="?", default="all",
                    choices=["markets", "rates", "news", "earnings", "triggers", "events", "all"])
    pi.set_defaults(func=cmd_ingest)
    sub.add_parser("marts", help="rebuild gold marts").set_defaults(func=cmd_marts)
    sub.add_parser("brief", help="build + print Morning Brief").set_defaults(func=cmd_brief)
    sub.add_parser("send-brief", help="build + send brief to Telegram").set_defaults(func=cmd_send_brief)
    sub.add_parser("run-daily", help="ingest all -> marts -> brief -> telegram").set_defaults(func=cmd_run_daily)

    pe = sub.add_parser("export", help="build snapshot + manifest into a dir")
    pe.add_argument("--out-dir", required=True)
    pe.add_argument("--demo", action="store_true", help="write the is_demo:true demo bundle")
    pe.add_argument("--market-state", default="PRE_OPEN")
    pe.set_defaults(func=cmd_export)

    pv = sub.add_parser("validate", help="fail-closed validate a snapshot file")
    pv.add_argument("path")
    pv.add_argument("--mode", default=None, help="deployment mode override")
    pv.add_argument("--manifest", default=None, help="path to latest.json (else sibling)")
    pv.set_defaults(func=cmd_validate)

    pp = sub.add_parser("publish", help="archive snapshot to Firestore (create-only)")
    pp.add_argument("path")
    pp.add_argument("--manifest", default=None)
    # The workflow supplies this: SMOKE_TESTED when the deployment was verified
    # live, VALIDATED_NOT_DEPLOYED when the snapshot validated but deploy/smoke
    # failed. Flag wins over env; both are checked against the allowed set (an
    # unknown value would become an uncorrectable permanent record).
    pp.add_argument("--release-status", type=_release_status,
                    default=os.getenv("FGX_RELEASE_STATUS", "SMOKE_TESTED"),
                    help="SMOKE_TESTED (default, also via $FGX_RELEASE_STATUS) "
                         "or VALIDATED_NOT_DEPLOYED")
    pp.set_defaults(func=cmd_publish)

    pr = sub.add_parser("retrieve-lkg",
                        help="write the archived last-known-good snapshot + manifest to a dir")
    pr.add_argument("--out-dir", required=True)
    pr.add_argument("--collection", default=None,
                    help="Firestore collection override (else settings)")
    pr.set_defaults(func=cmd_retrieve_lkg)

    pd = sub.add_parser("decommission-fred", help="one-time FRED cleanup (idempotent)")
    pd.add_argument("--purge-briefs", action="store_true")
    pd.set_defaults(func=cmd_decommission_fred)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
