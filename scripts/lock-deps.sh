#!/usr/bin/env bash
# Regenerate the hash-pinned dependency locks (P1.2).
#
# The locks MUST be resolved for the exact CI target — cp311 on manylinux
# x86_64 — regardless of the machine running this script. `uv pip compile`
# resolves for a requested version/platform without needing that interpreter
# installed, which is why we use it instead of pip-compile.
#
# Run after ANY change to [project.dependencies] or the optional-dependency
# groups in pyproject.toml, then commit requirements/*.lock in the same commit:
# CI installs with --require-hashes, so an unlocked new dependency fails the
# build closed rather than silently resolving at run time.
#
#   ./scripts/lock-deps.sh
#
# Requires: uv (`pip install uv`).
set -euo pipefail

cd "$(dirname "$0")/.."

PY_VERSION="3.11"
PLATFORM="x86_64-unknown-linux-gnu"

command -v uv >/dev/null 2>&1 || { echo "uv not found: pip install uv" >&2; exit 1; }

compile() {
  local out="$1"; shift
  echo "==> $out"
  uv pip compile pyproject.toml \
    --python-version "$PY_VERSION" \
    --python-platform "$PLATFORM" \
    --generate-hashes \
    --no-annotate \
    --quiet \
    -o "$out" "$@"
}

mkdir -p requirements
compile requirements/core.lock
compile requirements/publish.lock --extra publish
compile requirements/dev.lock     --extra dev

echo "OK. Review the diff, then commit requirements/*.lock together with pyproject.toml."
