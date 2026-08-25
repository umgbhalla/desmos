#!/usr/bin/env bash
# Build a desmos crate natively on hyperion (arm64 Mac) and bring the binary back.
#
# Same arch as this Mac, so there is no cross toolchain, no zig linker, no
# codesign step and no VM to start -- the artifact just runs. What makes it
# worth the ssh hop is hyperion's warm target dir: the build worktree shares
# it, so every registry dependency stays cached and only our own crates rebuild.
set -euo pipefail

HOST=${DESMOS_BUILD_HOST:-hyperion}
REMOTE_MAIN=${DESMOS_BUILD_REMOTE:-hub/desmos}
REMOTE_BUILD=${REMOTE_MAIN}-build
CRATE=desmos-tui
PROFILE=release
PROFILE_SET=0
COMMITTED=0
TEST=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dev) PROFILE=dev; PROFILE_SET=1 ;;
    --release) PROFILE=release; PROFILE_SET=1 ;;
    --committed) COMMITTED=1 ;;
    --test) TEST=1 ;;
    --crate) CRATE=$2; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "hyperion-build: unknown arg $1" >&2; exit 2 ;;
  esac
  shift
done

# Tests want the dev profile: hyperion's release target dir is warm for the
# binary we ship, and a release test build would compile the world again.
if [ "$TEST" = 1 ] && [ "$PROFILE_SET" = 0 ]; then PROFILE=dev; fi

cd "$(git rev-parse --show-toplevel)"
SLOT=$(basename "$PWD")

# The tree to build. A dirty worktree is captured through a temp index so the
# real index is never touched, and unlike `git stash create` this also carries
# untracked files. Ignored paths never travel, so target/ and .desmos/out stay
# home by construction.
if [ "$COMMITTED" = 1 ]; then
  SHA=$(git rev-parse HEAD)
else
  IDX=$(mktemp -t hyperion-build-index)
  trap 'rm -f "$IDX"' EXIT
  export GIT_INDEX_FILE="$IDX"
  git read-tree HEAD
  git add -A .
  SHA=$(git commit-tree "$(git write-tree)" -p HEAD -m "hyperion-build $SLOT")
  unset GIT_INDEX_FILE
fi

REF="refs/heads/build-$SLOT"
git push -q --force "$HOST:$REMOTE_MAIN" "$SHA:$REF"

if [ "$PROFILE" = release ]; then FLAGS=--release; DIR=release; else FLAGS=; DIR=debug; fi

# The build checkout is always detached, so pushing to build-$SLOT is never
# refused for being someone's current branch, and hyperion's own checkout stays
# on main for the spine daemon that lives there.
ssh "$HOST" "set -e
  if [ ! -d \"\$HOME/$REMOTE_BUILD\" ]; then
    git -C \"\$HOME/$REMOTE_MAIN\" worktree add --detach \"\$HOME/$REMOTE_BUILD\" $SHA
  fi
  cd \"\$HOME/$REMOTE_BUILD\"
  git checkout -q -f --detach $SHA
  git submodule update --init --recursive -q
  . \"\$HOME/.cargo/env\"
  export CARGO_TARGET_DIR=\"\$HOME/$REMOTE_MAIN/target\"
  if [ $TEST = 1 ]; then
    if cargo test $FLAGS -p $CRATE > /tmp/hyperion-test.log 2>&1; then
      grep -E '^test result:' /tmp/hyperion-test.log
    else
      grep -E '^test result:|^    tests::' /tmp/hyperion-test.log \
        || tail -30 /tmp/hyperion-test.log
      exit 1
    fi
  else
    cargo build $FLAGS -p $CRATE 2>&1 | tail -3
  fi
"

# A test run has no artifact to bring home. Its whole point is the second
# machine: the same source under a different HOME, which is how four
# card-layout tests were caught depending on the user's own pager.toml.
if [ "$TEST" = 1 ]; then exit 0; fi

OUT=.desmos/out/hyperion
mkdir -p "$OUT"
rsync -q "$HOST:$REMOTE_MAIN/target/$DIR/$CRATE" "$OUT/$CRATE"
file "$OUT/$CRATE" | grep -q 'Mach-O 64-bit executable arm64' \
  || { echo "hyperion-build: artifact is not a local-arch binary" >&2; exit 1; }
echo "hyperion-build: $OUT/$CRATE  $("$OUT/$CRATE" --version 2>/dev/null || true)"
