#!/usr/bin/env bash
# Build the vendored fff-python extension module (fff._fff_python) into the
# desmos venv. This is a SETUP step (like scripts/vendor-setup.sh), never part
# of `python -m desmos tui` launch. Absent module => the <find> syscall refuses
# in prose and the model falls back to bash/rg.
#
# Default features only: the pure-Rust `ripgrep` walker. The `zlob` feature is
# NEVER built here (it needs a Zig toolchain).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${DESMOS_VENV:-/Users/zeus/hub/desmos/.venv}"
PY="$VENV/bin/python"
PKG="$REPO/vendor/fff/packages/fff-python"

if [ ! -x "$PY" ]; then
  echo "build-fff-python: no venv python at $PY (set DESMOS_VENV)" >&2
  exit 1
fi
if [ ! -f "$PKG/pyproject.toml" ]; then
  echo "build-fff-python: vendored fff-python missing at $PKG" >&2
  exit 1
fi

# maturin is the build backend named in the package pyproject; install on demand
# into this venv (uv-managed venvs have no pip module, so prefer uv).
if ! "$PY" -m maturin --version >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" "maturin>=1.0,<2.0"
  else
    "$PY" -m pip install --quiet "maturin>=1.0,<2.0"
  fi
fi

# `develop` builds crates/fff-python (abi3-py310, module fff._fff_python via the
# pyproject) and installs the .so plus the `fff` python source into this venv.
cd "$PKG"
exec "$PY" -m maturin develop --release
