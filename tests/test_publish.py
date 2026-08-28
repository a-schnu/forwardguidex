"""Archive contract: release_status vocabulary, rollback eligibility, warnings.

Covers the "archive even when the deploy failed" behaviour:
  * a VALIDATED_NOT_DEPLOYED snapshot archives successfully,
  * and is NEVER returned by ``retrieve_last_known_good`` (rollback safety),
  * an unknown release_status is rejected before anything is written,
  * the IDEMPOTENT_MATCH status-understatement warning fires (the create-only
    writer cannot update a pre-existing document, so we make it loud instead).

``firebase-admin`` is not installed for the test extra, so the Firestore client
is replaced wholesale by an in-memory fake and the ``firebase_admin.firestore``
module (imported lazily by the reader path) is stubbed into ``sys.modules``.
"""
from __future__ import annotations

import copy
import sys
import types
from pathlib import Path

import pytest

from forwardguidex import cli
from forwardguidex.serve import publish as P
from forwardguidex.serve import snapshot as S

VND = P.VALIDATED_NOT_DEPLOYED
SMOKE = P.SMOKE_TESTED


# --------------------------------------------------------------------------- #
# In-memory Firestore stand-in
# --------------------------------------------------------------------------- #
class AlreadyExists(Exception):
    """Matched by ``publish._is_already_exists`` on class NAME, like api_core's."""


def _deployed_at(snap) -> str:
    return ((snap.to_dict() or {}).get("metadata") or {}).get("deployed_at", "")


class _FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store, doc_id):
        self._store = store
        self._id = doc_id

    def create(self, document):
        if self._id in self._store:  # create-only, exactly like Firestore
            raise AlreadyExists(f"document {self._id} already exists")
        self._store[self._id] = copy.deepcopy(document)

    def get(self):
        return _FakeSnapshot(self._id, self._store.get(self._id))


class _FakeQuery:
    def __init__(self, docs):
        self._docs = docs

    def order_by(self, field_path, direction=None):
        assert field_path == "metadata.deployed_at"
        return _FakeQuery(sorted(self._docs, key=_deployed_at,
                                 reverse=direction == "DESCENDING"))

    def limit(self, n):
        return _FakeQuery(self._docs[:n])

    def stream(self):
        return iter(self._docs)


class _FakeCollection(_FakeQuery):
    def __init__(self, store):
        super().__init__([_FakeSnapshot(k, v) for k, v in store.items()])
        self._store = store

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id)


class _FakeClient:
    def __init__(self, store):
        self._store = store

    def collection(self, _name):
        return _FakeCollection(self._store)


@pytest.fixture
def store(monkeypatch):
    """The fake ``snapshots_history`` collection: {doc_id: document}."""
    data: dict[str, dict] = {}
    monkeypatch.setattr(P, "_client", lambda *a, **k: _FakeClient(data))
    # The reader path does `from firebase_admin import firestore` lazily.
    firestore = types.SimpleNamespace(
        Query=types.SimpleNamespace(DESCENDING="DESCENDING"))
    pkg = types.ModuleType("firebase_admin")
    pkg.firestore = firestore
    monkeypatch.setitem(sys.modules, "firebase_admin", pkg)
    return data


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _bundle(out_dir, *, data_as_of="2026-08-28", marker="baseline"):
    """Write a real prod snapshot + manifest via the production writer."""
    payload = {
        "meta": {
            "schema_version": 1,
            "is_demo": False,
            "data_as_of": data_as_of,
            "generated_at": f"{data_as_of}T06:00:00+00:00",
        },
        "marker": marker,
    }
    manifest = S.write_bundle(payload, out_dir)
    out = Path(out_dir)
    return out / manifest["snapshot"], out / "latest.json", manifest


def _prov(status, *, deployed_at="2026-08-28T07:00:00+00:00", run="1"):
    return {
        "deployment_id": run,
        "workflow_run_id": run,
        "git_commit": "a" * 40,
        "release_status": status,
        "deployed_at": deployed_at,
    }


@pytest.fixture
def bundle(tmp_path):
    return _bundle(tmp_path)


# --------------------------------------------------------------------------- #
# release_status vocabulary
# --------------------------------------------------------------------------- #
def test_release_statuses_are_first_class_constants():
    assert SMOKE == "SMOKE_TESTED"
    assert VND == "VALIDATED_NOT_DEPLOYED"
    assert set(P.RELEASE_STATUSES) == {SMOKE, VND}


def test_validate_release_status_accepts_the_known_set():
    for value in P.RELEASE_STATUSES:
        assert P.validate_release_status(value) == value


@pytest.mark.parametrize("bad", ["SMOKE_TESTD", "smoke_tested", "DEPLOYED", "", None, 1])
def test_validate_release_status_rejects_anything_else(bad):
    with pytest.raises(ValueError, match="unknown release_status"):
        P.validate_release_status(bad)


# --------------------------------------------------------------------------- #
# Archiving a snapshot that validated but never went live
# --------------------------------------------------------------------------- #
def test_validated_not_deployed_snapshot_archives(store, bundle):
    snap, manifest, meta = bundle

    result = P.archive(snap, manifest, provenance=_prov(VND))

    assert result.status == "CREATED"
    assert result.ok
    assert result.doc_id == f"2026-08-28_{meta['artifact_sha256']}"
    stored = store[result.doc_id]["metadata"]
    assert stored["release_status"] == VND
    assert stored["is_demo"] is False
    # The payload is still the exact deployed-candidate bytes.
    assert store[result.doc_id]["payload_json"] == snap.read_text(encoding="utf-8")


def test_smoke_tested_snapshot_still_archives_as_before(store, bundle):
    snap, manifest, _ = bundle
    result = P.archive(snap, manifest, provenance=_prov(SMOKE))
    assert result.status == "CREATED"
    assert store[result.doc_id]["metadata"]["release_status"] == SMOKE
    assert result.status_mismatch is False
    assert result.status_understated is False


def test_unknown_release_status_is_rejected_before_any_write(store, bundle):
    snap, manifest, _ = bundle
    with pytest.raises(ValueError, match="unknown release_status 'SMOKE_TESTD'"):
        P.archive(snap, manifest, provenance=_prov("SMOKE_TESTD"))
    assert store == {}, "a typo must never become a permanent record"


def test_demo_payload_is_still_refused_whatever_the_status(tmp_path, store):
    payload = S.demo_snapshot()
    S.write_bundle(payload, tmp_path, demo=True)
    snap = tmp_path / "snapshot.demo.json"
    manifest = tmp_path / "latest.demo.json"
    with pytest.raises(ValueError, match="DEMO snapshot"):
        P.archive(snap, manifest, provenance=_prov(VND))
    assert store == {}


# --------------------------------------------------------------------------- #
# Rollback eligibility: only SMOKE_TESTED may ever be selected
# --------------------------------------------------------------------------- #
def test_record_contract_rejects_validated_not_deployed():
    snapshot_obj = {"meta": {"schema_version": 1}}
    metadata = {
        "release_status": SMOKE, "is_demo": False, "artifact_sha256": "b" * 64,
        "deployment_id": "1", "git_commit": "c" * 40, "workflow_run_id": "1",
    }
    assert P._record_passes_contract(metadata, snapshot_obj) is True
    assert P._record_passes_contract({**metadata, "release_status": VND},
                                     snapshot_obj) is False


def test_retrieve_last_known_good_skips_validated_not_deployed(tmp_path, store):
    """The newest record is VALIDATED_NOT_DEPLOYED; the older SMOKE_TESTED wins."""
    old_snap, old_manifest, old_meta = _bundle(tmp_path / "old", data_as_of="2026-08-27",
                                               marker="old")
    new_snap, new_manifest, new_meta = _bundle(tmp_path / "new", data_as_of="2026-08-28",
                                               marker="new")
    P.archive(old_snap, old_manifest,
              provenance=_prov(SMOKE, deployed_at="2026-08-27T07:00:00+00:00"))
    P.archive(new_snap, new_manifest,
              provenance=_prov(VND, deployed_at="2026-08-28T07:00:00+00:00"))
    assert len(store) == 2

    found = P.retrieve_last_known_good()

    assert found is not None
    assert found["metadata"]["release_status"] == SMOKE
    assert found["metadata"]["artifact_sha256"] == old_meta["artifact_sha256"]
    assert found["metadata"]["artifact_sha256"] != new_meta["artifact_sha256"]
    assert found["snapshot"]["marker"] == "old"


def test_retrieve_last_known_good_returns_none_when_only_never_deployed(store, bundle):
    snap, manifest, _ = bundle
    P.archive(snap, manifest, provenance=_prov(VND))
    assert len(store) == 1
    assert P.retrieve_last_known_good() is None


# --------------------------------------------------------------------------- #
# IDEMPOTENT_MATCH: the status we asked for is NOT the status that is stored
# --------------------------------------------------------------------------- #
def test_idempotent_match_warns_when_stored_status_understates(store, bundle, capsys):
    """Same bytes archived as VND, then re-run after a successful deploy.

    `.create()` raises AlreadyExists, the hashes match, so we return
    IDEMPOTENT_MATCH with the stored status still VALIDATED_NOT_DEPLOYED. The
    create-only writer cannot fix that — it must be loudly visible instead.
    """
    snap, manifest, _ = bundle
    first = P.archive(snap, manifest, provenance=_prov(VND, run="11"))
    capsys.readouterr()  # drop anything from the first (clean) archive

    second = P.archive(snap, manifest, provenance=_prov(SMOKE, run="22"))

    assert second.status == "IDEMPOTENT_MATCH"
    assert second.ok
    assert second.doc_id == first.doc_id
    assert second.stored_release_status == VND
    assert second.requested_release_status == SMOKE
    assert second.status_mismatch is True
    assert second.status_understated is True

    # No corrective write was attempted: the document is byte-for-byte untouched.
    assert store[second.doc_id]["metadata"]["release_status"] == VND
    assert store[second.doc_id]["metadata"]["deployment_id"] == "11"

    out = capsys.readouterr().out
    warnings = [ln for ln in out.splitlines() if ln.startswith("::warning::")]
    assert len(warnings) == 1, out
    assert second.doc_id in warnings[0]
    assert VND in warnings[0] and SMOKE in warnings[0]
    assert "UNDERSTATES" in warnings[0]
    assert "create-only" in warnings[0]


def test_idempotent_match_is_silent_when_the_status_agrees(store, bundle, capsys):
    snap, manifest, _ = bundle
    P.archive(snap, manifest, provenance=_prov(SMOKE))
    capsys.readouterr()

    again = P.archive(snap, manifest, provenance=_prov(SMOKE))

    assert again.status == "IDEMPOTENT_MATCH"
    assert again.status_mismatch is False
    assert again.status_understated is False
    assert "::warning::" not in capsys.readouterr().out


def test_idempotent_match_reports_a_stronger_stored_status_without_understating(
        store, bundle, capsys):
    snap, manifest, _ = bundle
    P.archive(snap, manifest, provenance=_prov(SMOKE))
    capsys.readouterr()

    again = P.archive(snap, manifest, provenance=_prov(VND))

    assert again.stored_release_status == SMOKE
    assert again.requested_release_status == VND
    assert again.status_mismatch is True
    assert again.status_understated is False, "stored value is the stronger one"
    warning = capsys.readouterr().out
    assert "::warning::" in warning
    assert "STRONGER" in warning


# --------------------------------------------------------------------------- #
# CLI wiring (`fwdx publish --release-status` / $FGX_RELEASE_STATUS)
# --------------------------------------------------------------------------- #
def test_cli_publish_defaults_to_smoke_tested(store, bundle, monkeypatch):
    monkeypatch.delenv("FGX_RELEASE_STATUS", raising=False)
    snap, manifest, _ = bundle
    cli.main(["publish", str(snap), "--manifest", str(manifest)])
    (document,) = store.values()
    assert document["metadata"]["release_status"] == SMOKE


def test_cli_publish_reads_release_status_from_the_environment(store, bundle,
                                                               monkeypatch):
    monkeypatch.setenv("FGX_RELEASE_STATUS", VND)
    snap, manifest, _ = bundle
    cli.main(["publish", str(snap), "--manifest", str(manifest)])
    (document,) = store.values()
    assert document["metadata"]["release_status"] == VND


def test_cli_publish_flag_overrides_the_environment(store, bundle, monkeypatch):
    monkeypatch.setenv("FGX_RELEASE_STATUS", SMOKE)
    snap, manifest, _ = bundle
    cli.main(["publish", str(snap), "--manifest", str(manifest),
              "--release-status", VND])
    (document,) = store.values()
    assert document["metadata"]["release_status"] == VND


@pytest.mark.parametrize("via_env", [True, False])
def test_cli_publish_rejects_an_unknown_release_status(store, bundle, monkeypatch,
                                                       capsys, via_env):
    snap, manifest, _ = bundle
    argv = ["publish", str(snap), "--manifest", str(manifest)]
    if via_env:
        monkeypatch.setenv("FGX_RELEASE_STATUS", "SMOKE_TESTD")
    else:
        monkeypatch.delenv("FGX_RELEASE_STATUS", raising=False)
        argv += ["--release-status", "SMOKE_TESTD"]

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)

    assert exc.value.code == 2
    assert "unknown release_status" in capsys.readouterr().err
    assert store == {}


def test_cli_publish_spells_out_the_understatement(store, bundle, monkeypatch, capsys):
    monkeypatch.delenv("FGX_RELEASE_STATUS", raising=False)
    snap, manifest, _ = bundle
    P.archive(snap, manifest, provenance=_prov(VND))
    capsys.readouterr()

    cli.main(["publish", str(snap), "--manifest", str(manifest),
              "--release-status", SMOKE])

    out = capsys.readouterr().out
    assert "::warning::" in out                    # machine-readable, for CI
    assert "WARNING: archived document" in out     # human-readable, for the log
    assert VND in out and SMOKE in out
    assert "create-only" in out
