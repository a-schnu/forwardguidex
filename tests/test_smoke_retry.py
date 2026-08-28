"""Tests for the smoke script's propagation-retry machinery.

Cloudflare's edge propagates per-PoP, not atomically, so for ~10s after a deploy
two back-to-back requests to the SAME URL can land on nodes running different
versions of the project. The smoke script must be patient with that shape and
ONLY that shape: a 200 where the password gate should have said 401 has to fail
the build on attempt 1, every time.

These tests pin both halves of that contract:

1. ``_transient_status`` — what counts as propagation noise vs. a real verdict.
2. ``_retrying``          — bounded patience, immediate short-circuit on a real
                            verdict, and the ORIGINAL error text on timeout.
3. the probes end-to-end against a scripted ``_request``, replaying the exact
   404-then-OK sequences observed in runs 30775364843 and 33180302742.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest


def _load_smoke():
    p = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "smoke.py"
    spec = importlib.util.spec_from_file_location("smoke", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture()
def smoke(monkeypatch):
    """The smoke module with sleeping stubbed out (tests must not take 90s)."""
    mod = _load_smoke()
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    return mod


# --------------------------------------------------------------------------- #
# _transient_status
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [404, 403, 408, 429, 500, 502, 503, 504,
                                    521, 522, 523, 525, 526, 530])
def test_propagation_shaped_statuses_are_transient(smoke, status):
    # A route we know is deployed answering 404/5xx = an edge node that has not
    # caught up yet, not a broken contract.
    assert smoke._transient_status(status, expected=401) is True


def test_connection_error_is_transient(smoke):
    # _request() reports transport failures as status None.
    assert smoke._transient_status(None, expected=200) is True


@pytest.mark.parametrize("expected", [400, 401, 403, 405])
def test_success_where_rejection_expected_is_never_transient(smoke, expected):
    # THE assertion that must never be retried away: the gate is open.
    assert smoke._transient_status(200, expected=expected) is False


def test_401_with_correct_password_is_not_transient(smoke):
    # DASHBOARD_PASSWORD is a project-level binding — identical on every PoP,
    # so waiting cannot turn a mismatch into a match. Fail fast and precisely.
    assert smoke._transient_status(401, expected=200) is False


def test_404_where_200_expected_is_transient(smoke):
    # The static-asset propagation race (auth GET / -> 404 right after deploy).
    assert smoke._transient_status(404, expected=200) is True


# --------------------------------------------------------------------------- #
# _retrying
# --------------------------------------------------------------------------- #
def test_retrying_recovers_from_a_transient_failure(smoke, capsys):
    calls = []

    def probe(label):
        calls.append(label)
        if len(calls) == 1:
            smoke._fail("unauth POST /api/chat expected 401, got 404.")

    smoke._retrying(probe, label="unique", what="/api/chat probe", max_wait_s=90.0)

    assert calls == ["unique attempt 1", "unique attempt 2"]
    out = capsys.readouterr().out
    # Retried attempts are warnings, never ::error:: (GitHub counts those).
    assert "::warning::unique: /api/chat probe not yet stable" in out
    assert "::error::" not in out


def test_retrying_short_circuits_a_non_transient_failure(smoke, capsys):
    calls = []

    def probe(label):
        calls.append(label)
        smoke._fail("SECURITY — unauthenticated GET / returned 200.", transient=False)

    with pytest.raises(smoke.SmokeFailure, match="SECURITY"):
        smoke._retrying(probe, label="unique", what="auth-gate probe", max_wait_s=90.0)

    # Exactly one attempt: an open gate is not something to be patient about.
    assert calls == ["unique attempt 1"]
    assert "not yet stable" not in capsys.readouterr().out


def test_retrying_gives_up_and_reraises_the_original_error(smoke, monkeypatch):
    clock = iter([0.0] + [1000.0] * 20)  # first call sets the deadline, then expired
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(clock))
    calls = []

    def probe(label):
        calls.append(label)
        smoke._fail("authenticated GET / expected 200, got 404.")

    with pytest.raises(smoke.SmokeFailure) as exc:
        smoke._retrying(probe, label="alias", what="auth-gate probe", max_wait_s=90.0)

    # The build fails carrying the real reason, not a vague "timed out".
    assert str(exc.value) == "authenticated GET / expected 200, got 404."
    assert calls == ["alias attempt 1"]


def test_retrying_is_bounded(smoke, monkeypatch):
    """A permanently-transient failure must stop, not spin forever."""
    ticks = iter([float(i) for i in range(0, 400, 6)])  # 6s of "wall clock" per attempt
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(ticks))
    calls = []

    def probe(label):
        calls.append(label)
        smoke._fail("GET /data/latest.json -> 404.")

    with pytest.raises(smoke.SmokeFailure):
        smoke._retrying(probe, label="alias", what="probe", max_wait_s=90.0)

    assert 2 <= len(calls) <= 20


# --------------------------------------------------------------------------- #
# Probes end-to-end against a scripted edge
# --------------------------------------------------------------------------- #
_CHALLENGE = {"WWW-Authenticate": 'Basic realm="ForwardGuidex"'}


def _script(smoke, responses):
    """Replace _request with a scripted sequence of (status, headers) tuples."""
    seq = list(responses)
    seen = []

    def fake_request(url, *, method="GET", headers=None, body=None):
        seen.append((method, url))
        status, hdrs = seq.pop(0)
        return status, dict(hdrs), b""

    smoke._request = fake_request
    return seen


def test_auth_gate_survives_a_mid_propagation_404(smoke, capsys):
    # Attempt 1: the unauth GET lands on a PoP that has no deployment -> 404.
    # Attempt 2: same URL, a caught-up PoP -> the real contract holds.
    _script(smoke, [
        (404, {}),                                    # attempt 1, unauth GET /
        (401, _CHALLENGE), (200, {}), (401, {}),      # attempt 2, all three
    ])
    smoke._retrying(lambda lbl: smoke.probe_auth_gate("https://x.example", "pw", lbl),
                    label="unique", what="auth-gate probe", max_wait_s=90.0)

    out = capsys.readouterr().out
    assert "unauthenticated GET / expected 401, got 404" in out  # as a warning
    assert "OK: unique attempt 2: unauth GET / -> 401 with challenge." in out
    assert "OK: unique attempt 2: auth GET / -> 200." in out
    assert "::error::" not in out


def test_auth_gate_open_gate_fails_immediately_even_under_retry(smoke):
    # The regression that must never be papered over: gate not enforced.
    seen = _script(smoke, [(200, {})] * 6)
    with pytest.raises(smoke.SmokeFailure, match="SECURITY"):
        smoke._retrying(lambda lbl: smoke.probe_auth_gate("https://x.example", "pw", lbl),
                        label="unique", what="auth-gate probe", max_wait_s=90.0)
    assert len(seen) == 1  # no second attempt, no waiting it out


def test_auth_gate_wrong_password_accepted_fails_immediately(smoke):
    seen = _script(smoke, [(401, _CHALLENGE), (200, {}), (200, {})] * 3)
    with pytest.raises(smoke.SmokeFailure, match="WRONG password"):
        smoke._retrying(lambda lbl: smoke.probe_auth_gate("https://x.example", "pw", lbl),
                        label="unique", what="auth-gate probe", max_wait_s=90.0)
    assert len(seen) == 3  # one attempt only (3 requests), then stop


def test_auth_gate_password_mismatch_is_not_retried(smoke):
    # Correct password rejected -> project secret mismatch; retrying is futile.
    seen = _script(smoke, [(401, _CHALLENGE), (401, {}), (401, {})] * 3)
    with pytest.raises(smoke.SmokeFailure, match="authenticated GET / expected 200, got 401"):
        smoke._retrying(lambda lbl: smoke.probe_auth_gate("https://x.example", "pw", lbl),
                        label="unique", what="auth-gate probe", max_wait_s=90.0)
    assert len(seen) == 2


def test_chat_api_survives_the_observed_404_then_ok_sequence(smoke, capsys):
    # Exactly what run 33180302742 logged: unauth POST 404 on attempt 1, then a
    # clean pass on attempt 2 five seconds later.
    no_store = {"Cache-Control": "no-store"}
    _script(smoke, [
        (404, {}),                                             # attempt 1
        (401, {}), (405, {}), (400, no_store), (403, {}),      # attempt 2
    ])
    smoke._retrying(lambda lbl: smoke.probe_chat_api("https://x.example", "pw", lbl),
                    label="unique", what="/api/chat probe", max_wait_s=90.0)

    out = capsys.readouterr().out
    assert "::warning::unique: /api/chat probe not yet stable" in out
    assert "OK: unique attempt 2: foreign-Origin POST -> 403." in out


def test_chat_api_public_endpoint_fails_immediately(smoke):
    # Unauthenticated POST answered 200 -> the LLM proxy is open to the world.
    seen = _script(smoke, [(200, {})] * 6)
    with pytest.raises(smoke.SmokeFailure, match="unauth POST /api/chat expected 401, got 200"):
        smoke._retrying(lambda lbl: smoke.probe_chat_api("https://x.example", "pw", lbl),
                        label="unique", what="/api/chat probe", max_wait_s=90.0)
    assert len(seen) == 1


def test_chat_api_missing_openrouter_key_is_named(smoke):
    # chat.js checks the key before parsing the body, so the malformed-JSON
    # probe sees 503 rather than 400 when the secret is unset on CF Pages.
    _script(smoke, [(401, {}), (405, {}), (503, {})] * 4)
    with pytest.raises(smoke.SmokeFailure, match="OPENROUTER_API_KEY is not set"):
        smoke._retrying(lambda lbl: smoke.probe_chat_api("https://x.example", "pw", lbl),
                        label="unique", what="/api/chat probe", max_wait_s=0.0)


def test_live_snapshot_404_is_transient_but_bad_hash_still_fails(smoke):
    # The alias-lag retry that already existed must keep working...
    assert smoke._transient_status(404, expected=200) is True
    # ...while a wrong hash is a genuine verdict that the budget cannot rescue:
    # it stays transient (the alias may still be promoting) but always re-raises.
    exc = smoke.SmokeFailure("SHA-256(snapshot.x.json) aaa != expected bbb.")
    assert exc.transient is True
