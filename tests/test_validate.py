"""Fail-closed validator: valid demo + every documented failure mode."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from forwardguidex.serve import snapshot as S
from forwardguidex.serve import validate as V

NOW = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def demo_dir(tmp_path):
    S.write_bundle(S.demo_snapshot(), tmp_path, demo=True)
    return tmp_path


def _errs(payload, mode="LOCAL_DEMO"):
    raw = S._canonical_bytes(payload)
    return V.validate_payload(payload, raw, mode=mode, now=NOW,
                              snap_path=Path("x.json"), manifest_path=None)


def test_valid_demo(demo_dir):
    errs = V.validate_snapshot_file(demo_dir / "snapshot.demo.json", mode="LOCAL_DEMO",
                                    now=NOW, manifest_path=demo_dir / "latest.demo.json")
    assert errs == [], errs


def test_demo_rejected_in_private(demo_dir):
    errs = V.validate_snapshot_file(demo_dir / "snapshot.demo.json", mode="PRIVATE_PERSONAL",
                                    now=NOW, manifest_path=demo_dir / "latest.demo.json")
    assert any("is_demo" in e for e in errs)


def test_stale_fails(demo_dir):
    errs = V.validate_snapshot_file(demo_dir / "snapshot.demo.json", mode="LOCAL_DEMO",
                                    now=datetime(2026, 9, 1, tzinfo=timezone.utc),
                                    manifest_path=demo_dir / "latest.demo.json")
    assert any(e.startswith("freshness") for e in errs)


def test_nan_rejected(tmp_path):
    text = (tmp_path / "s.json")
    good = S.demo_snapshot()
    raw = S._canonical_bytes(good).decode("utf-8").replace('"is_demo":true', '"is_demo":true', 1)
    # inject a NaN literal into a numeric field
    raw = raw.replace('"schema_version":1', '"schema_version":1', 1)
    raw = raw.replace('"value":4.28', '"value":NaN', 1)
    text.write_text(raw, encoding="utf-8")
    errs = V.validate_snapshot_file(text, mode="LOCAL_DEMO", now=NOW)
    assert any("non-finite" in e or e.startswith("json") for e in errs)


def test_corrupt_bytes_fail_manifest(demo_dir):
    raw = bytearray((demo_dir / "snapshot.demo.json").read_bytes())
    raw[80] ^= 0x01
    (demo_dir / "snapshot.demo.json").write_bytes(raw)
    errs = V.validate_snapshot_file(demo_dir / "snapshot.demo.json", mode="LOCAL_DEMO",
                                    now=NOW, manifest_path=demo_dir / "latest.demo.json")
    assert any("artifact_sha256" in e for e in errs)


def test_non_https_headline_fails():
    p = S.demo_snapshot()
    p["headlines"][0]["url"] = "http://insecure.example/x"
    assert any("not https" in e for e in _errs(p))


def test_additional_property_fails():
    p = S.demo_snapshot()
    p["indices"][0]["evil"] = 1
    assert any("schema" in e for e in _errs(p))


def test_absurd_rate_fails():
    p = S.demo_snapshot()
    p["rates"][0]["value"] = 999
    assert _errs(p)


def test_negative_price_anomaly():
    p = S.demo_snapshot()
    p["indices"][0]["last"] = -5
    assert any("non-positive" in e or "schema" in e for e in _errs(p))


# --------------------------------------------------------------------------- #
# Manifest cross-checks beyond the hashes.
#
# The deploy workflow once hand-built `latest.json` and copied `generated_at`
# from the archive record's `deployed_at`. Same bytes, same artifact_sha256,
# same content_hash — so the validator waved it through and production served a
# manifest that overstated how fresh the data was. These lock that shut.
# --------------------------------------------------------------------------- #
def _rewrite_manifest(manifest_path, **overrides):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(overrides)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def test_manifest_generated_at_drift_is_rejected(demo_dir):
    """Exactly the production bug: generated_at replaced by a deploy timestamp."""
    _rewrite_manifest(demo_dir / "latest.demo.json",
                      generated_at="2026-08-28T17:15:34.947469+00:00")
    errs = V.validate_snapshot_file(demo_dir / "snapshot.demo.json", mode="LOCAL_DEMO",
                                    now=NOW, manifest_path=demo_dir / "latest.demo.json")
    assert any(e.startswith("manifest.generated_at") for e in errs), errs


def test_manifest_schema_version_drift_is_rejected(demo_dir):
    _rewrite_manifest(demo_dir / "latest.demo.json", schema_version=2)
    errs = V.validate_snapshot_file(demo_dir / "snapshot.demo.json", mode="LOCAL_DEMO",
                                    now=NOW, manifest_path=demo_dir / "latest.demo.json")
    assert any(e.startswith("manifest.schema_version") for e in errs), errs


def test_manifest_missing_generated_at_is_rejected(demo_dir):
    """A manifest that simply omits the field must not pass as 'equal'."""
    manifest_path = demo_dir / "latest.demo.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["generated_at"]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    errs = V.validate_snapshot_file(demo_dir / "snapshot.demo.json", mode="LOCAL_DEMO",
                                    now=NOW, manifest_path=manifest_path)
    assert any(e.startswith("manifest.generated_at") for e in errs), errs
