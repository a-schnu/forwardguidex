"""Snapshot hashing (content_hash + artifact_sha256) + bundle write."""
import copy
import hashlib
import json

import pytest

from forwardguidex.serve import snapshot as S


def test_content_hash_deterministic_and_excludes_itself():
    p = S.demo_snapshot()
    h = S.compute_content_hash(p)
    # reordered / re-parsed copy hashes identically
    assert S.compute_content_hash(json.loads(json.dumps(p))) == h
    # meta.content_hash is excluded from the hash input
    p2 = copy.deepcopy(p)
    p2["meta"]["content_hash"] = "sha256:" + "0" * 64
    assert S.compute_content_hash(p2) == h
    assert h.startswith("sha256:") and len(h) == len("sha256:") + 64


def test_finalize_artifact_matches_bytes():
    p = S.demo_snapshot()
    payload, final_bytes, artifact, content_hash = S.finalize(p)
    assert hashlib.sha256(final_bytes).hexdigest() == artifact
    assert payload["meta"]["content_hash"] == content_hash
    # bytes contain no NaN/Infinity (allow_nan=False path)
    json.loads(final_bytes.decode("utf-8"))  # standard JSON parses


def test_write_bundle_demo_roundtrip(tmp_path):
    man = S.write_bundle(S.demo_snapshot(), tmp_path, demo=True)
    assert man["snapshot"] == "snapshot.demo.json"
    raw = (tmp_path / man["snapshot"]).read_bytes()
    # browser check: raw-bytes SHA-256 == manifest artifact
    assert hashlib.sha256(raw).hexdigest() == man["artifact_sha256"]
    obj = json.loads(raw)
    assert obj["meta"]["content_hash"] == man["content_hash"]
    assert obj["meta"]["is_demo"] is True
    assert (tmp_path / "latest.demo.json").exists()


def test_prod_bundle_refuses_demo(tmp_path):
    with pytest.raises(ValueError):
        S.write_bundle(S.demo_snapshot(), tmp_path, demo=False)


def test_demo_meta_timestamps_are_tz_aware():
    from datetime import datetime

    meta = S.demo_snapshot()["meta"]
    for k in ("generated_at", "data_as_of", "oldest_required_source_as_of",
              "source_received_at", "freshness_checked_at"):
        assert datetime.fromisoformat(meta[k]).tzinfo is not None


def test_demo_under_size_target():
    _, final_bytes, _, _ = S.finalize(S.demo_snapshot())
    assert len(final_bytes) < 500 * 1024
