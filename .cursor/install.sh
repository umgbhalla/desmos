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

echo "==> prebuild release TUI (hash-gated; only rebuilds when sources move)"
cargo build -p desmos-tui --release

echo "==> desmos environment ready. Activate with: source .venv/bin/activate"
