"""Fail-closed smoke test for the ForwardGuidex production deploy.

Contract (P0.2 remediation):
  * runs against the UNIQUE deployment URL first (steps.deploy_cf output),
    then re-checks the stable APP_HOST alias;
  * every mandatory probe is one of PASS / FAIL / INCONCLUSIVE, and
    INCONCLUSIVE fails the gate (rollback) — never a silent pass;
  * fetches the LIVE served snapshot bytes and compares SHA-256 against the
    export step's ``artifact_sha256``;
  * verifies /api/chat authentication, method gate, malformed-JSON handling,
    cross-origin CSRF guard, and ``Cache-Control: no-store``.

Environment (all required unless noted):
  APP_HOST                      — stable production URL (e.g. https://x.pages.dev)
  UNIQUE_DEPLOY_URL             — deployment-specific URL from wrangler-action
  DASHBOARD_PASSWORD            — the Basic-auth password (same as CF Pages)
  EXPECTED_ARTIFACT_SHA256      — hex sha256 the exporter produced
  EXPECTED_SNAPSHOT_NAME        — file name embedded in latest.json (safety guard)

Exits 0 on PASS; non-zero on FAIL / INCONCLUSIVE. Emits ``::error::`` /
``::warning::`` annotations recognised by GitHub Actions.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
TIMEOUT_S = 30

# Filenames returned by the exporter look like ``snapshot.<hex>.json``.
_SNAP_NAME_RE = re.compile(r"^snapshot\.[0-9a-f]{64}\.json$")


def _fail(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"::error::{msg}", flush=True)
    sys.exit(1)


def _pass(msg: str) -> None:
    print(f"OK: {msg}", flush=True)


def _mark_bust(url: str, tag: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_smoke={tag}_{int(time.time() * 1000)}"


def _request(url: str, *, method: str = "GET", headers: dict | None = None,
             body: bytes | None = None):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", UA)
    headers.setdefault("Cache-Control", "no-cache")
    req = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), (e.read() or b"")
    except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError) as e:
        return None, {"__err__": f"{type(e).__name__}: {e}"}, b""


def _basic(pw: str) -> str:
    return "Basic " + base64.b64encode(f"ci:{pw}".encode()).decode()


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #
def probe_auth_gate(host: str, pw_correct: str, label: str) -> None:
    """/ must be 401 unauth, 200 with correct pw, 401 with wrong pw."""
    # unauth
    status, hdrs, _ = _request(_mark_bust(host + "/", "unauth"))
    if status is None:
        _fail(f"{label}: unauthenticated GET / unreachable ({hdrs.get('__err__')}). Inconclusive -> fail.")
    if status == 200:
        _fail(f"{label}: SECURITY — unauthenticated GET / returned 200; password gate NOT enforced (dashboard is PUBLIC).")
    if status == 503:
        _fail(f"{label}: gate Function runs but DASHBOARD_PASSWORD is not set on Cloudflare Pages Production.")
    if status != 401:
        _fail(f"{label}: unauthenticated GET / expected 401, got {status}. Inconclusive -> fail.")
    if "WWW-Authenticate" not in hdrs and "www-authenticate" not in {k.lower() for k in hdrs}:
        _fail(f"{label}: 401 without WWW-Authenticate challenge header.")
    _pass(f"{label}: unauth GET / -> 401 with challenge.")

    # authenticated (correct)
    status, _, _ = _request(_mark_bust(host + "/", "authok"),
                            headers={"Authorization": _basic(pw_correct)})
    if status is None:
        _fail(f"{label}: authenticated GET / unreachable.")
    if status != 200:
        _fail(f"{label}: authenticated GET / expected 200, got {status}.")
    _pass(f"{label}: auth GET / -> 200.")

    # authenticated (wrong password) — must be 401
    status, _, _ = _request(_mark_bust(host + "/", "wrongpw"),
                            headers={"Authorization": _basic("intentionally-wrong-password")})
    if status is None:
        _fail(f"{label}: wrong-password GET / unreachable.")
    if status != 401:
        _fail(f"{label}: wrong-password GET / expected 401, got {status}.")
    _pass(f"{label}: wrong-pw GET / -> 401.")


def probe_live_snapshot(host: str, pw: str, *, expected_sha: str,
                        expected_name: str, label: str) -> None:
    """Fetch /data/latest.json and the referenced snapshot; verify SHA-256."""
    auth = {"Authorization": _basic(pw)}
    status, _, body = _request(_mark_bust(host + "/data/latest.json", "manifest"),
                               headers=auth)
    if status != 200:
        _fail(f"{label}: GET /data/latest.json -> {status}.")
    try:
        manifest = json.loads(body)
    except Exception as e:  # noqa: BLE001
        _fail(f"{label}: /data/latest.json not JSON ({e}).")
    snap = manifest.get("snapshot") or ""
    if not _SNAP_NAME_RE.match(str(snap)) or "/" in snap or "\\" in snap or snap.startswith(".."):
        _fail(f"{label}: unsafe or unexpected snapshot filename {snap!r}.")
    if snap != expected_name:
        _fail(f"{label}: live snapshot {snap!r} != expected {expected_name!r} — stale/cached deploy?")
    if manifest.get("artifact_sha256") != expected_sha:
        _fail(f"{label}: manifest.artifact_sha256 {manifest.get('artifact_sha256')!r} != expected {expected_sha!r}.")

    status, _, snap_bytes = _request(_mark_bust(host + "/data/" + snap, "bytes"),
                                     headers=auth)
    if status != 200:
        _fail(f"{label}: GET /data/{snap} -> {status}.")
    digest = hashlib.sha256(snap_bytes).hexdigest()
    if digest != expected_sha:
        _fail(f"{label}: SHA-256({snap}) {digest} != expected {expected_sha}.")
    try:
        payload = json.loads(snap_bytes)
    except Exception as e:  # noqa: BLE001
        _fail(f"{label}: live {snap} not JSON ({e}).")
    meta = payload.get("meta") or {}
    if meta.get("schema_version") != 1:
        _fail(f"{label}: live schema_version={meta.get('schema_version')!r}, expected 1.")
    if meta.get("is_demo") is not False:
        _fail(f"{label}: live is_demo={meta.get('is_demo')!r}, expected false.")
    if not meta.get("freshness") or not meta.get("quality"):
        _fail(f"{label}: live meta.freshness/quality missing.")
    if meta.get("quality") == "FAILED":
        _fail(f"{label}: live meta.quality=FAILED — validator should have blocked this.")
    if meta.get("freshness") == "STALE_FALLBACK":
        _fail(f"{label}: live meta.freshness=STALE_FALLBACK — validator should have blocked this.")
    _pass(f"{label}: live snapshot verified (SHA-256, schema, quality={meta.get('quality')}).")


def probe_chat_api(host: str, pw: str, label: str) -> None:
    """Contract probes for /api/chat (fails closed on unauth / method / origin / body)."""
    endpoint = _mark_bust(host + "/api/chat", "chat")
    good = {"Authorization": _basic(pw)}

    # 1) Unauthenticated POST -> 401 (middleware gate).
    status, _, _ = _request(endpoint, method="POST",
                            headers={"content-type": "application/json"},
                            body=b'{"messages":[]}')
    if status is None:
        _fail(f"{label}: unauth POST /api/chat unreachable.")
    if status != 401:
        _fail(f"{label}: unauth POST /api/chat expected 401, got {status}.")
    _pass(f"{label}: unauth POST /api/chat -> 401.")

    # 2) Authenticated GET -> 405 (method not allowed).
    status, hdrs, _ = _request(endpoint, method="GET", headers=good)
    if status is None:
        _fail(f"{label}: auth GET /api/chat unreachable.")
    if status != 405:
        _fail(f"{label}: auth GET /api/chat expected 405, got {status}.")
    _pass(f"{label}: auth GET /api/chat -> 405.")

    # 3) Authenticated malformed JSON -> 400.
    status, hdrs, _ = _request(endpoint, method="POST",
                               headers={**good, "content-type": "application/json"},
                               body=b"{not-json")
    if status is None:
        _fail(f"{label}: auth malformed POST unreachable.")
    if status != 400:
        _fail(f"{label}: auth malformed POST expected 400, got {status}.")
    # Response must be uncacheable.
    cc = (hdrs.get("Cache-Control") or hdrs.get("cache-control") or "").lower()
    if "no-store" not in cc:
        _fail(f"{label}: /api/chat 400 missing Cache-Control: no-store (got {cc!r}).")
    _pass(f"{label}: auth malformed POST -> 400, no-store.")

    # 4) Cross-origin POST with foreign Origin -> 403 (CSRF guard).
    origin = "https://evil.example"
    status, _, _ = _request(endpoint, method="POST",
                            headers={**good, "content-type": "application/json",
                                     "Origin": origin},
                            body=b'{"messages":[{"role":"user","content":"hi"}]}')
    if status is None:
        _fail(f"{label}: foreign-Origin POST unreachable.")
    if status != 403:
        _fail(f"{label}: foreign-Origin POST expected 403, got {status}.")
    _pass(f"{label}: foreign-Origin POST -> 403.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    try:
        app_host = os.environ["APP_HOST"].rstrip("/")
        unique = (os.environ.get("UNIQUE_DEPLOY_URL") or "").strip().rstrip("/")
        pw = os.environ["DASHBOARD_PASSWORD"]
        expected_sha = os.environ["EXPECTED_ARTIFACT_SHA256"]
        expected_name = os.environ["EXPECTED_SNAPSHOT_NAME"]
    except KeyError as e:
        _fail(f"required env var missing: {e}")

    # Local staged bytes sanity check (fail-closed pre-flight).
    try:
        manifest = json.load(open("app/data/latest.json"))
        snap_file = os.path.join("app/data", manifest["snapshot"])
        raw = open(snap_file, "rb").read()
    except Exception as e:  # noqa: BLE001
        _fail(f"staged snapshot unreadable: {e}")
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        _fail(f"staged {snap_file} hash != export artifact_sha256.")
    _pass("staged prod artifact hash matches export.")

    # PRIMARY: unique deployment URL — this is the exact deploy under test.
    if unique:
        primary_label = f"unique[{unique}]"
        probe_auth_gate(unique, pw, primary_label)
        probe_live_snapshot(unique, pw,
                            expected_sha=expected_sha,
                            expected_name=expected_name,
                            label=primary_label)
        probe_chat_api(unique, pw, primary_label)
    else:
        _fail("wrangler-action did not emit deployment-url; cannot verify the unique deploy under test.")

    # SECONDARY: stable alias — must reflect the same approved artifact.
    # A persistent mismatch means the promotion silently didn't happen; the
    # brief mandates the workflow must not report full success in that case.
    if app_host and app_host != unique:
        alias_label = f"alias[{app_host}]"
        probe_auth_gate(app_host, pw, alias_label)
        probe_live_snapshot(app_host, pw,
                            expected_sha=expected_sha,
                            expected_name=expected_name,
                            label=alias_label)

    print("SMOKE PASSED (unique deploy: auth gate, live bytes, /api/chat all verified).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
