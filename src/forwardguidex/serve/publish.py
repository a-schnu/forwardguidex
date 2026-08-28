"""Firestore snapshot-history archive: create-only writer + read-only reader.

Server-only persistence of validated snapshots into the `snapshots_history`
collection. This module is the *archive* leg of the release pipeline and is
deliberately decoupled from the deploy leg:

    BUILT -> VALIDATED -> DEPLOYED -> SMOKE_TESTED ------------> ARCHIVED
                 |                    (deploy done)              (this module)
                 |
                 +--> deploy or smoke FAILED
                            `--------> VALIDATED_NOT_DEPLOYED -> ARCHIVED

**Deploy != archive.** `archive(...)` runs after the candidate has been through
the deploy leg — whatever the outcome of that leg was. An archive failure (hash
conflict, or transient errors exhausted) is therefore an **alert + retry**
condition for the CI job — it MUST NOT trigger a deploy rollback. `archive(...)`
signals failure by raising; the caller alerts/retries and leaves the live
deployment (or the rolled-back one) in place.

Terminal states (`release_status`)
----------------------------------
Two — and only two — statuses ever reach the archive, and they are NOT
interchangeable:

``SMOKE_TESTED``
    The artifact was deployed to Cloudflare and verified live by the
    authenticated smoke test. **The only status eligible for rollback
    selection** (`_record_passes_contract` / `retrieve_last_known_good`, and the
    mirrored Firestore query in ``.github/workflows/deploy-app.yml``).

``VALIDATED_NOT_DEPLOYED``
    The artifact passed `fwdx validate` (fail-closed) but never made it live:
    the Cloudflare deploy failed, or the smoke test failed and the deployment
    was rolled back. The *data* was good; the *delivery* failed. Archiving it
    closes the hole in the history — otherwise a day on which Cloudflare
    misbehaved simply vanishes from the record — without touching the guarantee
    about what we actually serve: such a record can never be selected as a
    rollback target, because the record contract accepts only ``SMOKE_TESTED``.

Nothing that failed *validation* is ever archived: the CI archive job is gated on
`fwdx validate` having succeeded, and an unknown `release_status` is rejected
outright by `validate_release_status` — a typo must never become a permanent
record (it would be invisible to every consumer that filters on the known set,
and the create-only writer could never correct it).

Credentials / identity
-----------------------
No key files. `firebase_admin.initialize_app()` runs with Application Default
Credentials, which in CI resolve to a **Workload Identity Federation** (WIF)
GitHub-OIDC service account. The *writer* SA has create-only Firestore rights and
is used by `archive(...)`; the *reader* SA is read-only and is used by
`retrieve_last_known_good(...)`. Least-privilege is enforced by IAM on the
identity, not by this code — the code simply uses whatever ADC identity is bound.

`firebase-admin` is imported **lazily** inside functions so that
`import forwardguidex.serve.publish` succeeds in environments where the package
is not installed (e.g. local dev without the archive dependency).

Idempotent create
-----------------
Documents are written with `DocumentReference.create()` (create-only; never
overwrites). Retry semantics:
* create succeeds                       -> CREATED.
* AlreadyExists, same hash + content    -> IDEMPOTENT_MATCH (treated as success).
* AlreadyExists, different hash/content -> conflict -> raise `ArchiveConflictError`
                                           (alert; a stable id must be immutable).
* transient error                       -> bounded exponential backoff, then
                                           raise `ArchiveTransientError`.

Known limitation: release_status understatement on IDEMPOTENT_MATCH
-------------------------------------------------------------------
The doc id is `{date}_{artifact_sha256}` — it identifies *the bytes*, not the
run. So a same-day re-run that produces a **byte-identical** snapshot lands on
the same document. If the first run archived `VALIDATED_NOT_DEPLOYED` (deploy
broke) and a later run of the same bytes DID deploy and pass smoke, `.create()`
raises AlreadyExists, `_hashes_match` succeeds, and the call returns
`IDEMPOTENT_MATCH` — with the stored `release_status` still pinned at
`VALIDATED_NOT_DEPLOYED` even though that artifact eventually went live.

This is **not fixed here, by design**: the writer identity is create-only in IAM
and *cannot* update the document; attempting an update (or a shadow/corrective
write under a different id) would either fail or quietly break the "a stable id
is immutable" invariant that `ArchiveConflictError` exists to protect. Instead
the mismatch is made loud: `archive(...)` emits a `::warning::` line naming the
doc id, the stored status and the status we tried to write, and reports it on
`ArchiveResult` (`status_understated`, `stored_release_status`,
`requested_release_status`) so the CLI can repeat it in human terms.

The understatement always errs on the *safe* side: the record is treated as less
trustworthy than it really is, so at worst a genuinely-live artifact is skipped
as a rollback candidate. It is never the other way round.

payload_json
------------
`payload_json` stores the **exact snapshot file text** (UTF-8). The snapshot file
is already written by `serve.snapshot` in the canonical convention (sort_keys,
compact `","`/`":"`, ensure_ascii=False, allow_nan=False), so the stored string is
both the canonical string AND byte-identical to the deployed artifact — a reader
can re-verify `sha256(payload_json.encode("utf-8")) == artifact_sha256`. The field
is excluded from Firestore indexing via `firestore.indexes.json` (R6 (k)).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
SUPPORTED_SCHEMA_VERSION = 1

# --- release_status vocabulary --------------------------------------------- #
# Deployed AND verified live. The ONLY status a rollback may ever select.
SMOKE_TESTED = "SMOKE_TESTED"
# Validated (fail-closed) but never served: deploy or smoke failed. Archived so
# the history has no hole, and deliberately NOT rollback-eligible.
VALIDATED_NOT_DEPLOYED = "VALIDATED_NOT_DEPLOYED"

#: Every value `metadata.release_status` may legally take. Anything else is
#: rejected by `validate_release_status` before a single byte is written.
RELEASE_STATUSES = (SMOKE_TESTED, VALIDATED_NOT_DEPLOYED)

#: Ordering used ONLY to describe an IDEMPOTENT_MATCH mismatch: is the value we
#: found already stored weaker than the one this run tried to write?
_STATUS_RANK = {VALIDATED_NOT_DEPLOYED: 0, SMOKE_TESTED: 1}

# Bounded exponential backoff for transient Firestore errors.
_MAX_ATTEMPTS = 4                 # total create attempts before giving up
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_FACTOR = 2.0
_BACKOFF_MAX_SECONDS = 8.0

# Cap on how many archive records the deterministic reader will scan (in
# deployed_at-descending order) before concluding none qualify.
_RETRIEVE_SCAN_LIMIT = 200

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# google.api_core transient exception class names (matched by name so we do not
# hard-import google.api_core, which is absent without firebase-admin).
_TRANSIENT_EXC_NAMES = frozenset({
    "ServiceUnavailable", "DeadlineExceeded", "InternalServerError",
    "Aborted", "TooManyRequests", "ResourceExhausted", "GatewayTimeout",
    "RetryError", "ServerError",
})

# Provenance keys the caller must supply (deployed_at defaults to now UTC).
_REQUIRED_PROVENANCE = ("deployment_id", "workflow_run_id", "git_commit", "release_status")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class PublishError(Exception):
    """Base class for archive failures (alert + retry, never a deploy rollback)."""


class ArchiveConflictError(PublishError):
    """Same document id already exists with a DIFFERENT hash/content.

    A stable id (`{date}_{artifact_sha256}`) must be immutable; a conflict means
    two distinct artifacts collided on one id, which is an integrity alarm — not
    something to silently overwrite. Signal an alert.
    """


class ArchiveTransientError(PublishError):
    """Transient Firestore errors persisted past the retry budget. Retry later."""


# --------------------------------------------------------------------------- #
# Result contract
# --------------------------------------------------------------------------- #
@dataclass
class ArchiveResult:
    """Outcome of `archive(...)`. `ok` is always True here (failures raise)."""
    status: str                      # "CREATED" | "IDEMPOTENT_MATCH"
    doc_id: str
    collection: str
    date: str
    artifact_sha256: str
    content_hash: str
    is_demo: bool
    attempts: int
    metadata: dict[str, Any] = field(default_factory=dict)

    # --- release_status reconciliation (only meaningful on IDEMPOTENT_MATCH) --
    #: What this run asked to record.
    requested_release_status: str | None = None
    #: What the pre-existing document actually says (== requested on CREATED).
    stored_release_status: str | None = None
    #: True when the stored status is WEAKER than the requested one, i.e. the
    #: history understates this artifact and the create-only writer cannot fix
    #: it. See the module docstring; a `::warning::` has already been emitted.
    status_understated: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("CREATED", "IDEMPOTENT_MATCH")

    @property
    def status_mismatch(self) -> bool:
        """Stored release_status differs from the one this run tried to write."""
        return (
            self.stored_release_status is not None
            and self.stored_release_status != self.requested_release_status
        )


# --------------------------------------------------------------------------- #
# Pure helpers (no side effects; testable without firebase)
# --------------------------------------------------------------------------- #
def validate_release_status(value: Any) -> str:
    """Return `value` unchanged if it is a known release_status, else raise.

    Rejecting loudly is the whole point: the archive is append-only under a
    create-only identity, so a typo (``"SMOKE_TESTD"``) would become a permanent
    record that no consumer recognises and no writer can repair. Called by
    `_build_document` (before any Firestore I/O) and by the `fwdx publish` CLI
    (before the WIF token is even used).
    """
    if isinstance(value, str) and value in RELEASE_STATUSES:
        return value
    raise ValueError(
        f"unknown release_status {value!r}; allowed: {', '.join(RELEASE_STATUSES)}"
    )


def _emit_warning(message: str) -> None:
    """Emit one CI-consumable warning line.

    Written to stdout as a GitHub Actions ``::warning::`` workflow command so the
    run surfaces it in the annotations, not only in the raw log. Kept as a single
    seam so callers/tests have exactly one place to look.
    """
    print(f"::warning::{message}")


def _reconcile_release_status(result: ArchiveResult, existing_meta: dict,
                              ours_meta: dict) -> ArchiveResult:
    """Record (and, on mismatch, loudly announce) the stored vs requested status.

    Called on the IDEMPOTENT_MATCH path, where the document we wanted to write
    already exists with byte-identical content. `.create()` is the only verb the
    writer identity holds, so a differing `release_status` CANNOT be corrected —
    see "Known limitation" in the module docstring. We therefore make the
    divergence impossible to miss instead of pretending it did not happen.
    """
    stored = existing_meta.get("release_status")
    ours = ours_meta.get("release_status")
    result.stored_release_status = stored
    result.requested_release_status = ours
    if stored == ours:
        return result

    result.status_understated = (
        _STATUS_RANK.get(stored, -1) < _STATUS_RANK.get(ours, -1)
    )
    common = (
        f"archive {result.doc_id!r}: release_status NOT updated — the document "
        f"already existed with byte-identical content and stores "
        f"release_status={stored!r}, but this run tried to record {ours!r}. "
        f"The archive writer is create-only by IAM and cannot update it; no "
        f"corrective write is attempted, by design."
    )
    if result.status_understated:
        _emit_warning(
            common
            + " The stored value UNDERSTATES this artifact: it did reach"
            " production, yet it will never be picked as a rollback target"
            " (only SMOKE_TESTED records qualify). Safe direction, but real —"
            " re-export (which changes the bytes, hence the doc id) if you need"
            " this day represented as SMOKE_TESTED."
        )
    else:
        _emit_warning(
            common
            + " The stored value is the STRONGER of the two, so the history is"
            " not understated; surfaced only so the divergence is visible."
        )
    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_part(value: Any) -> str:
    """Extract the calendar-date part (YYYY-MM-DD) from an ISO date/datetime."""
    if value is None:
        raise ValueError("meta.data_as_of is missing; cannot derive snapshot date")
    s = str(value)
    head = s.split("T", 1)[0]
    # Validate it parses as a real date; raise a clear error otherwise.
    datetime.strptime(head, "%Y-%m-%d")
    return head


def _strip_sha_prefix(content_hash: str | None) -> str | None:
    if not content_hash:
        return None
    return content_hash[len("sha256:"):] if content_hash.startswith("sha256:") else content_hash


def _load_inputs(snapshot_path, manifest_path) -> tuple[str, dict, dict]:
    """Read snapshot text + manifest, verify the manifest references THIS file.

    Returns (snapshot_text, snapshot_obj, manifest). Recomputes the artifact hash
    from the raw file bytes and asserts it matches the manifest so we never
    archive a snapshot/manifest pair that has drifted apart.
    """
    snap_bytes = Path(snapshot_path).read_bytes()
    snapshot_text = snap_bytes.decode("utf-8")
    snapshot_obj = json.loads(snapshot_text)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))

    computed = hashlib.sha256(snap_bytes).hexdigest()
    declared = manifest.get("artifact_sha256")
    if declared and declared != computed:
        raise ValueError(
            f"manifest artifact_sha256 {declared!r} != snapshot bytes hash {computed!r}"
        )
    return snapshot_text, snapshot_obj, manifest


def _build_document(snapshot_text: str, snapshot_obj: dict, manifest: dict,
                    provenance: dict) -> tuple[str, dict, dict]:
    """Assemble (doc_id, document, metadata) from inputs. Refuses demo payloads.

    Document shape is EXACT:
        { "metadata": {artifact_sha256, content_hash, deployed_at, deployment_id,
                       workflow_run_id, git_commit, release_status, is_demo},
          "payload_json": "<exact canonical snapshot text>" }
    """
    meta = snapshot_obj.get("meta", {}) or {}
    is_demo = bool(meta.get("is_demo"))
    if is_demo:
        raise ValueError("refusing to archive a DEMO snapshot (meta.is_demo == true)")

    artifact = hashlib.sha256(snapshot_text.encode("utf-8")).hexdigest()
    # Prefer the manifest's declared values (already cross-checked in _load_inputs).
    if manifest.get("artifact_sha256"):
        artifact = manifest["artifact_sha256"]
    content_hash = manifest.get("content_hash") or meta.get("content_hash") or ""
    date = _date_part(meta.get("data_as_of"))

    missing = [k for k in _REQUIRED_PROVENANCE if not provenance.get(k)]
    if missing:
        raise ValueError(f"provenance missing required key(s): {', '.join(missing)}")
    # Fail before any Firestore I/O: an unknown status must never be persisted.
    release_status = validate_release_status(provenance["release_status"])

    metadata = {
        "artifact_sha256": artifact,
        "content_hash": content_hash,
        "deployed_at": provenance.get("deployed_at") or _now_iso(),
        "deployment_id": provenance["deployment_id"],
        "workflow_run_id": provenance["workflow_run_id"],
        "git_commit": provenance["git_commit"],
        "release_status": release_status,
        "is_demo": is_demo,
    }
    document = {"metadata": metadata, "payload_json": snapshot_text}
    doc_id = f"{date}_{artifact}"
    return doc_id, document, metadata


def _hashes_match(existing_meta: dict, ours_meta: dict) -> bool:
    """Idempotency test: same artifact_sha256 AND same content_hash."""
    return (
        existing_meta.get("artifact_sha256") == ours_meta.get("artifact_sha256")
        and _strip_sha_prefix(existing_meta.get("content_hash"))
        == _strip_sha_prefix(ours_meta.get("content_hash"))
    )


def _is_already_exists(exc: Exception) -> bool:
    if type(exc).__name__ == "AlreadyExists":
        return True
    return getattr(exc, "code", None) == 409


def _is_transient(exc: Exception) -> bool:
    if type(exc).__name__ in _TRANSIENT_EXC_NAMES:
        return True
    code = getattr(exc, "code", None)
    return code in (429, 500, 503, 504)


def _backoff_seconds(attempt: int) -> float:
    """Delay before retry `attempt` (1-based): 0.5, 1.0, 2.0, ... capped."""
    return min(_BACKOFF_BASE_SECONDS * (_BACKOFF_FACTOR ** (attempt - 1)),
               _BACKOFF_MAX_SECONDS)


def _record_passes_contract(metadata: dict, snapshot_obj: dict) -> bool:
    """Deterministic last-known-good record contract (plan R4-R5 (b)).

    `SMOKE_TESTED` is the *only* accepted `release_status`, and that exclusivity
    is load-bearing: `VALIDATED_NOT_DEPLOYED` records exist precisely because
    their artifact was never proven live, so promoting one to a rollback target
    would mean rolling production forward onto bytes nobody ever served. Widen
    this set only if you are prepared to defend that.
    """
    if metadata.get("release_status") != SMOKE_TESTED:
        return False
    if metadata.get("is_demo") is not False:  # must be explicitly False, never demo
        return False
    artifact = metadata.get("artifact_sha256")
    if not (isinstance(artifact, str) and _HEX64.match(artifact)):
        return False
    meta = (snapshot_obj or {}).get("meta", {}) or {}
    if meta.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        return False
    for key in ("deployment_id", "git_commit", "workflow_run_id"):
        if not metadata.get(key):
            return False
    return True


# --------------------------------------------------------------------------- #
# Firestore client (lazy firebase import; ADC / WIF)
# --------------------------------------------------------------------------- #
def _client(project: str | None = None):
    """Return a Firestore client using ADC (WIF in CI). Lazy firebase import.

    Idempotent app init: reuses the default app if one already exists. The read
    vs write privilege is decided by the ADC identity, not by this call.
    """
    import firebase_admin
    from firebase_admin import firestore

    settings = config.get_settings()
    project = project or settings.firestore_project
    try:
        app = firebase_admin.get_app()
    except ValueError:
        options = {"projectId": project} if project else None
        app = firebase_admin.initialize_app(options=options)
    return firestore.client(app)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def archive(snapshot_path, manifest_path, *, provenance: dict) -> ArchiveResult:
    """Create-only archive of a validated snapshot into Firestore.

    Args:
        snapshot_path: path to the exported ``snapshot.<hash>.json`` file.
        manifest_path: path to the ``latest.json`` manifest referencing it.
        provenance: dict with ``deployment_id``, ``workflow_run_id``,
            ``git_commit``, ``release_status`` (one of `RELEASE_STATUSES`:
            ``SMOKE_TESTED`` when the snapshot was deployed and smoke-tested,
            ``VALIDATED_NOT_DEPLOYED`` when it validated but deploy/smoke failed)
            and optional ``deployed_at`` (ISO; defaults to now UTC).

    Behaviour:
        * Refuses to archive when ``meta.is_demo`` is true (raises ValueError).
        * Refuses an unknown ``release_status`` (raises ValueError) *before* any
          Firestore call — see `validate_release_status`.
        * Doc id ``{date}_{artifact_sha256}`` in ``settings.firestore_collection``.
        * Writes with ``.create()`` (create-only) via ADC/WIF (no key file).
        * Idempotent: an existing doc with the SAME artifact hash + content_hash
          is a success (``IDEMPOTENT_MATCH``); a CONFLICTING one raises
          ``ArchiveConflictError``. Transient errors are retried with bounded
          exponential backoff, then raise ``ArchiveTransientError``.
        * On ``IDEMPOTENT_MATCH`` the stored ``release_status`` wins (create-only
          identity: we literally cannot update it). If it differs from the one
          requested, a ``::warning::`` is emitted and the result carries
          ``status_understated`` / ``stored_release_status`` /
          ``requested_release_status``. See the module docstring.

    Returns:
        ArchiveResult (``status`` = "CREATED" or "IDEMPOTENT_MATCH").

    Note:
        Any raise here is an **alert + retry** signal for the CI archive job. It
        is NOT a deploy rollback: whatever is live (the new deployment, or the
        rolled-back previous one) stays exactly as the deploy leg left it.
    """
    snapshot_text, snapshot_obj, manifest = _load_inputs(snapshot_path, manifest_path)
    doc_id, document, metadata = _build_document(snapshot_text, snapshot_obj, manifest, provenance)

    settings = config.get_settings()
    collection = settings.firestore_collection
    client = _client(settings.firestore_project)
    doc_ref = client.collection(collection).document(doc_id)

    def _result(status: str, attempts: int, meta: dict) -> ArchiveResult:
        return ArchiveResult(
            status=status, doc_id=doc_id, collection=collection,
            date=doc_id.split("_", 1)[0], artifact_sha256=metadata["artifact_sha256"],
            content_hash=metadata["content_hash"], is_demo=metadata["is_demo"],
            attempts=attempts, metadata=meta,
            requested_release_status=metadata["release_status"],
            stored_release_status=metadata["release_status"],
        )

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            doc_ref.create(document)
            return _result("CREATED", attempt, metadata)
        except Exception as exc:
            if _is_already_exists(exc):
                existing = doc_ref.get()
                existing_data = existing.to_dict() if existing.exists else {}
                existing_meta = (existing_data or {}).get("metadata", {}) or {}
                if _hashes_match(existing_meta, metadata):
                    return _reconcile_release_status(
                        _result("IDEMPOTENT_MATCH", attempt, existing_meta),
                        existing_meta, metadata,
                    )
                raise ArchiveConflictError(
                    f"archive id {doc_id!r} already exists with a different artifact: "
                    f"existing artifact_sha256={existing_meta.get('artifact_sha256')!r} "
                    f"content_hash={existing_meta.get('content_hash')!r} vs "
                    f"ours artifact_sha256={metadata['artifact_sha256']!r} "
                    f"content_hash={metadata['content_hash']!r} (ALERT)"
                ) from exc
            if _is_transient(exc) and attempt < _MAX_ATTEMPTS:
                last_exc = exc
                time.sleep(_backoff_seconds(attempt))
                continue
            if _is_transient(exc):
                raise ArchiveTransientError(
                    f"archive {doc_id!r} failed after {attempt} attempts: {exc} (retry later)"
                ) from exc
            raise  # non-transient, non-AlreadyExists: surface as-is

    # Loop exhausted on transient errors.
    raise ArchiveTransientError(
        f"archive {doc_id!r} failed after {_MAX_ATTEMPTS} attempts: {last_exc} (retry later)"
    )


def retrieve_last_known_good(*, project: str | None = None,
                             collection: str | None = None) -> dict | None:
    """Deterministic last-known-good record for rollback selection (R4-R5 (b)).

    Streams archive records in DESCENDING ``metadata.deployed_at`` order (uses the
    automatic single-field index; ``payload_json`` is index-exempt) and returns
    the first record that satisfies the full record contract:
        release_status == "SMOKE_TESTED"  AND
        is_demo == false                  AND
        artifact_sha256 is 64 lowercase hex  AND
        payload_json parses with meta.schema_version == 1  AND
        provenance present (deployment_id, git_commit, workflow_run_id).

    Read-only path (meant to run under a read-only reader identity in CI; lazy
    firebase import). Returns ``{"metadata", "payload_json", "snapshot"}`` for the
    winning record, or ``None`` if none qualify. **Never returns a demo record,
    and never a ``VALIDATED_NOT_DEPLOYED`` one** — those archive a snapshot that
    was validated but never actually served, so they close the hole in the
    history without ever becoming something we roll production back onto. The
    caller decides what to do when ``None`` (fail, per the plan — never DEMO).
    """
    from firebase_admin import firestore

    settings = config.get_settings()
    collection = collection or settings.firestore_collection
    client = _client(project or settings.firestore_project)

    query = (
        client.collection(collection)
        .order_by("metadata.deployed_at", direction=firestore.Query.DESCENDING)
        .limit(_RETRIEVE_SCAN_LIMIT)
    )

    for doc in query.stream():
        data = doc.to_dict() or {}
        metadata = data.get("metadata", {}) or {}
        payload_json = data.get("payload_json")
        if not isinstance(payload_json, str):
            continue
        try:
            snapshot_obj = json.loads(payload_json)
        except (ValueError, TypeError):
            continue
        if _record_passes_contract(metadata, snapshot_obj):
            return {
                "metadata": metadata,
                "payload_json": payload_json,
                "snapshot": snapshot_obj,
            }
    return None
