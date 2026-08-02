"""Sanity checks for the smoke script's snapshot filename validation.

We can't spin up a real Cloudflare deploy in unit tests, but we CAN exercise the
regex the smoke script uses to reject unsafe / traversal-y filenames served in
``latest.json`` (P0.2: "Reject absolute URLs, path traversal, or unexpected
filename formats").
"""
from __future__ import annotations

import importlib.util
import pathlib


def _load_smoke():
    p = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "smoke.py"
    spec = importlib.util.spec_from_file_location("smoke", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_regex_accepts_valid_snapshot_name():
    smoke = _load_smoke()
    ok = "snapshot." + "a" * 64 + ".json"
    assert smoke._SNAP_NAME_RE.match(ok)


def test_regex_rejects_traversal_and_absolute():
    smoke = _load_smoke()
    bad = [
        "../etc/passwd",
        "snapshot.demo.json",
        "snapshot.short.json",
        "SNAPSHOT.XXX.JSON",
        "snapshot." + "a" * 64 + ".JSON",  # case-sensitive
        "https://evil/snap." + "a" * 64 + ".json",
        "",
    ]
    for name in bad:
        assert not smoke._SNAP_NAME_RE.match(name), name
