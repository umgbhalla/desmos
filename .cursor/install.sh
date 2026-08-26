#!/usr/bin/env bash
# Cloud Agent bootstrap for desmos.
#
# Idempotent: safe to run repeatedly and against a warm snapshot. It installs
# the two system packages the base image lacks (a protobuf compiler for the
# grok-build pager graph, and the venv module), initializes the one submodule
# the default TUI imports, builds the stdlib-only Python harness into a venv,
# and prebuilds the release TUI so `python -m desmos tui` launches without a
# cold 15-minute cargo build.
set -euo pipefail

cd "$(dirname "$0")/.."
repo_root="$(pwd)"

echo "==> system packages (protobuf-compiler, python venv)"
if ! command -v protoc >/dev/null 2>&1 || ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq protobuf-compiler python3-venv python3.12-venv
fi

echo "==> vendor/grok-build submodule (needed by desmos-tui)"
git submodule update --init vendor/grok-build

echo "==> python venv + editable install (harness has zero runtime deps; kernel extra is IPython)"
if [ ! -x "$repo_root/.venv/bin/python" ]; then
  python3 -m venv "$repo_root/.venv"
fi
# shellcheck disable=SC1091
. "$repo_root/.venv/bin/activate"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e ".[kernel]"

echo "==> prebuild release TUI through the harness hash-gate"
# Build via desmos' own _tui_compile so it writes target/release/.desmos-tui.hash,
# a content hash of the TUI sources. `python -m desmos tui` reuses the prebuilt
# binary when that hash matches, independent of file mtimes -- without the stamp
# a warm snapshot (whose restored mtimes are skewed) triggers a full ~17-minute
# pager rebuild on first launch. Reusing the repo's real gate also makes this
# step a fast no-op on idempotent re-runs.
python - <<'PY'
import shutil
import sys
from pathlib import Path

from desmos.front import cli

root = Path(cli._repo_root())
if cli._tui_binary(root, True) is not None:
    print("desmos-tui already current; reusing target/release/desmos-tui")
    sys.exit(0)
cargo = shutil.which("cargo")
if cargo is None:
    print("cargo not found on PATH", file=sys.stderr)
    sys.exit(1)
sys.exit(cli._tui_compile(cargo, root, cli._tui_build_env(), True))
PY

echo "==> desmos environment ready. Activate with: source .venv/bin/activate"
