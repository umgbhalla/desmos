#!/usr/bin/env bash
# Install the memex-desmos fork and warm its index. SETUP step, never part of
# `python -m desmos tui` launch. Absent fork => the <recall> syscall refuses in
# prose and the model falls back to `rg` over .desmos/events/*.jsonl.
#
# DISTRIBUTION: memex pulls in tantivy + usearch + ort — those must NOT enter
# the desmos build. Install an external binary; there is no vendor pin here.
set -euo pipefail

MEMEX="${DESMOS_MEMEX:-memex}"
FORK_REPO="${MEMEX_DESMOS_REPO:-}"
FORK_REV="${MEMEX_DESMOS_REV:-}"

probe() { "$MEMEX" search __probe__ --source desmos --limit 1 >/dev/null 2>&1; }

if probe; then
  echo "memex-setup: memex-desmos fork already on PATH ($("$MEMEX" --version 2>/dev/null || echo memex))"
elif [ -z "$FORK_REPO" ] || [ -z "$FORK_REV" ]; then
  cat >&2 <<'EOF'
memex-setup: the memex-desmos fork is not installed and no pin is configured.

Set MEMEX_DESMOS_REPO and MEMEX_DESMOS_REV to the fork (memex + SourceKind::Desmos
+ src/sources/desmos.rs) and re-run, e.g.:

  MEMEX_DESMOS_REPO=https://github.com/<owner>/memex-desmos \
  MEMEX_DESMOS_REV=<sha> scripts/memex-setup.sh

Until then <recall> refuses in prose; use `rg` over .desmos/events/*.jsonl.
EOF
  exit 1
else
  if ! command -v cargo >/dev/null 2>&1; then
    echo "memex-setup: cargo not found; install Rust to build the fork" >&2
    exit 1
  fi
  echo "memex-setup: installing memex-desmos from $FORK_REPO @ $FORK_REV"
  cargo install --git "$FORK_REPO" --rev "$FORK_REV" --locked memex
fi

if ! probe; then
  echo "memex-setup: install finished but the desmos source is still missing" >&2
  exit 1
fi

echo "memex-setup: indexing"
exec "$MEMEX" index
