"""The hash-pinned locks must stay consistent with pyproject.toml (P1.2).

Deliberately NOT implemented as "regenerate the locks and diff": resolution is
a function of the live PyPI index, so any unrelated upstream release would turn
a green PR red. These checks are offline, deterministic, and catch the failure
mode that actually matters — a dependency changed in pyproject.toml without
re-running `scripts/lock-deps.sh`, which production would only discover when
`pip install --require-hashes` refused the unlisted package.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomllib
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
LOCKS = {
    "core": ROOT / "requirements" / "core.lock",
    "publish": ROOT / "requirements" / "publish.lock",
    "dev": ROOT / "requirements" / "dev.lock",
}
# Which optional-dependency groups each lock is expected to cover, on top of
# [project.dependencies]. Mirrors scripts/lock-deps.sh.
LOCK_EXTRAS = {"core": [], "publish": ["publish"], "dev": ["dev"]}

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _parse_lock(path: Path) -> dict[str, str]:
    """Return ``{canonical_name: version}``; assert every pin carries hashes."""
    pins: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith(("#", " ", "-")) or not line.strip():
            continue
        m = _PIN_RE.match(line)
        assert m, f"{path.name}:{i + 1}: unparseable requirement line {line!r}"
        # `--require-hashes` rejects the whole file if a single pin lacks hashes,
        # so an unhashed line is a hard failure, not a style nit.
        assert line.rstrip().endswith("\\"), (
            f"{path.name}:{i + 1}: {m.group(1)} is pinned without hashes"
        )
        pins[_canonical(m.group(1))] = m.group(2)
    return pins


@pytest.mark.parametrize("lock_name", sorted(LOCKS))
def test_lock_exists_and_is_fully_hashed(lock_name):
    pins = _parse_lock(LOCKS[lock_name])
    assert len(pins) > 10, f"{lock_name}.lock looks truncated: {len(pins)} pins"


@pytest.mark.parametrize("lock_name", sorted(LOCKS))
def test_lock_covers_declared_dependencies_at_a_satisfying_version(lock_name):
    pj = _pyproject()
    specs = list(pj["project"]["dependencies"])
    for extra in LOCK_EXTRAS[lock_name]:
        specs += pj["project"]["optional-dependencies"][extra]

    pins = _parse_lock(LOCKS[lock_name])
    missing, unsatisfied = [], []
    for spec in specs:
        req = Requirement(spec)
        name = _canonical(req.name)
        if name not in pins:
            missing.append(req.name)
            continue
        if req.specifier and not req.specifier.contains(Version(pins[name]), prereleases=True):
            unsatisfied.append(f"{req.name}: lock has {pins[name]}, pyproject wants {req.specifier}")

    assert not missing, (
        f"{lock_name}.lock is missing {missing} — run ./scripts/lock-deps.sh and commit the result"
    )
    assert not unsatisfied, f"{lock_name}.lock is stale: {unsatisfied}"


def test_locks_agree_on_shared_packages():
    """core/publish/dev must not disagree on a shared package's version.

    A divergence means one lock was regenerated and the others were not, so the
    code under test in CI would not be the code that runs in production.
    """
    parsed = {name: _parse_lock(path) for name, path in LOCKS.items()}
    core = parsed["core"]
    conflicts = []
    for name, pins in parsed.items():
        if name == "core":
            continue
        for pkg, version in pins.items():
            if pkg in core and core[pkg] != version:
                conflicts.append(f"{pkg}: core={core[pkg]} {name}={version}")
    assert not conflicts, f"lockfiles disagree: {conflicts}"


def test_every_extra_has_a_lock_or_is_documented():
    """A new optional-dependency group must not silently ship unlocked."""
    declared = set(_pyproject()["project"]["optional-dependencies"])
    covered = {e for extras in LOCK_EXTRAS.values() for e in extras}
    # `dashboard` is the retired Streamlit dev UI — intentionally unlocked
    # because no workflow installs it.
    assert declared - covered - {"dashboard"} == set(), (
        f"unlocked extras: {declared - covered - {'dashboard'}} — add them to "
        "scripts/lock-deps.sh and LOCK_EXTRAS"
    )
