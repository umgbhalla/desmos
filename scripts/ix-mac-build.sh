#!/usr/bin/env bash
# Cross-build desmos-tui for aarch64-apple-darwin on an ix VM and bring it home.
#
#   scripts/ix-mac-build.sh                 live worktree, cranelift dev profile
#   scripts/ix-mac-build.sh --release       llvm release profile
#   scripts/ix-mac-build.sh --committed     build HEAD instead of the live worktree
#   scripts/ix-mac-build.sh --slot NAME     build slot (default: worktree dir name)
#   scripts/ix-mac-build.sh --stop          stop the VM when finished
#
# Source travels as a git push over the VM's public IPv6. Only git-visible files
# move: ignored trees (target/, .desmos/out, 15G of vendor build junk) are excluded
# by construction; untracked-but-not-ignored files DO move.
#
# Each slot gets its own branch, worktree and CARGO_TARGET_DIR on the VM, so two
# local worktrees never invalidate each other's cache. Registry, toolchains, SDK
# and zig cache are shared. Builds are serialised by flock: the VM has 3G of RAM.
set -euo pipefail

VM="${IX_VM:-desmos-lab}"
PORT=9418
PROFILE=dev
SOURCE=live
STOP=0
SLOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --release)   PROFILE=release ;;
    --committed) SOURCE=committed ;;
    --stop)      STOP=1 ;;
    --slot)      shift; SLOT="${1:-}" ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$PROFILE" = release ] && DIR=release || DIR=debug

export PATH="$HOME/.local/bin:$PATH"
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
SLOT="${SLOT:-${IX_SLOT:-$(basename "$ROOT")}}"
SLOT=$(printf '%s' "$SLOT" | tr -c 'A-Za-z0-9._-' '-')
OUT="${IX_OUT:-$ROOT/.desmos/out/ix}"
mkdir -p "$OUT"
step() { printf "\n== %s\n" "$1"; }

step "start $VM (slot $SLOT)"
ix start "$VM" > /dev/null 2>&1 || true
VM6=$(ix ls --output json | python3 -c '
import json, sys
want = sys.argv[1]
for v in json.load(sys.stdin):
    if v["name"] == want:
        print(v["ipv6"])
        break
' "$VM")
[ -n "$VM6" ] || { echo "no ipv6 for $VM" >&2; exit 1; }

step "ensure receive endpoint"
ix shell "$VM" --noninteractive -- sh -lc '
export PATH=$HOME/.nix-profile/bin:$PATH
cd /srv/desmos
git config receive.denyCurrentBranch updateInstead
nft add rule inet nixos-fw input tcp dport 9418 accept 2>/dev/null || true
pgrep -f "[g]it-daemon" > /dev/null ||
  setsid git daemon --base-path=/srv --export-all --enable=receive-pack \
    --listen=:: --port=9418 --detach --reuseaddr
' > /dev/null 2>&1

step "compose $SOURCE commit"
if [ "$SOURCE" = live ]; then
  IDX=$(mktemp -u /tmp/ixidx.XXXXXX)
  COMMIT=$(GIT_INDEX_FILE="$IDX" sh -c '
    git read-tree HEAD
    git add -A . 2>/dev/null || true
    git commit-tree "$(git write-tree)" -p HEAD -m "live worktree"')
  rm -f "$IDX"
else
  COMMIT=$(git rev-parse HEAD)
fi
echo "commit $COMMIT"

step "push source"
git push "git://[$VM6]:$PORT/desmos" "$COMMIT:refs/heads/slot-$SLOT" --force 2>&1 | tail -2

step "build $PROFILE"
ix shell "$VM" --noninteractive -- env IX_KIND="$PROFILE" IX_SLOT="$SLOT" sh -lc '
set -e
export PATH=$HOME/.nix-profile/bin:$PATH
WT=/srv/wt/$IX_SLOT
if [ -d "$WT" ]; then
  git -C "$WT" reset --hard "slot-$IX_SLOT" > /dev/null
else
  mkdir -p /srv/wt
  git -C /srv/desmos worktree add --force "$WT" "slot-$IX_SLOT" > /dev/null
fi
# vendor/grok-build is a submodule now: the push moves only the gitlink, so
# the sources come from GitHub over https (the VM holds no ssh key). A bare
# reference clone under /srv makes that a delta fetch instead of a cold
# clone, shared by every slot; it is bootstrapped once and survives stops.
REF=/srv/grok-build.git
if [ ! -d "$REF" ]; then
  git clone --bare https://github.com/umgbhalla/grok-build.git "$REF"
fi
git -C "$REF" fetch -q origin '+refs/heads/*:refs/heads/*' || true
git -C "$WT" -c url."https://github.com/".insteadOf="git@github.com:" \
  submodule update --init --reference "$REF" vendor/grok-build
export RUSTUP_HOME=/srv/rustup CARGO_HOME=/srv/cargo
export PATH="/srv/rustup/toolchains/nightly-x86_64-unknown-linux-gnu/bin:/srv/cargo/bin:$PATH"
export CARGO_TARGET_DIR=/srv/target/$IX_SLOT
export ZIG_GLOBAL_CACHE_DIR=/srv/zig-cache
export SDKROOT=/srv/sdk/MacOSX26.5.sdk COREAUDIO_SDK_PATH=/srv/sdk/MacOSX26.5.sdk
export LIBCLANG_PATH=$(dirname $(find /nix/store -maxdepth 3 -name "libclang.so*" | head -1))
export BINDGEN_EXTRA_CLANG_ARGS="--target=arm64-apple-macos -isysroot $SDKROOT"
export ZIGCC_REAL=$(find /root/.cache/cargo-zigbuild -name "zigcc-aarch64-apple-darwin*.sh" | head -1)
cd "$WT"
if [ "$IX_KIND" = release ]; then
  export RUSTFLAGS="-Clinker=/srv/zigcc-dedupe"
  set -- cargo zigbuild --release --target aarch64-apple-darwin --bin desmos-tui -j 8
else
  export RUSTFLAGS="-Zthreads=8 -Clinker=/srv/zigcc-dedupe"
  export CARGO_PROFILE_DEV_CODEGEN_BACKEND=cranelift
  export CARGO_PROFILE_DEV_DEBUG=line-tables-only
  export CARGO_PROFILE_DEV_PANIC=abort
  set -- cargo zigbuild -Zcodegen-backend --target aarch64-apple-darwin --bin desmos-tui -j 8
fi
if command -v flock > /dev/null; then
  time flock /srv/build.lock "$@" 2>&1 | tail -2
else
  time "$@" 2>&1 | tail -2
fi'

step "fetch artifact"
ix shell "$VM" --noninteractive -- \
  sh -lc "gzip -c /srv/target/$SLOT/aarch64-apple-darwin/$DIR/desmos-tui" \
  > "$OUT/desmos-tui-$SLOT.gz" 2> /dev/null
gunzip -f -c "$OUT/desmos-tui-$SLOT.gz" > "$OUT/desmos-tui-$SLOT"
chmod +x "$OUT/desmos-tui-$SLOT"
rm -f "$OUT/desmos-tui-$SLOT.gz"

step "verify"
file "$OUT/desmos-tui-$SLOT" | sed 's/^.*: //'
DUPES=$(otool -L "$OUT/desmos-tui-$SLOT" | tail -n +2 | awk '{print $1}' | sort | uniq -d)
[ -z "$DUPES" ] || { echo "FAIL duplicate dylibs: $DUPES" >&2; exit 1; }
"$OUT/desmos-tui-$SLOT" --version
echo "artifact: $OUT/desmos-tui-$SLOT"

[ "$STOP" = 1 ] && { step "stop $VM"; ix stop "$VM" > /dev/null 2>&1; echo stopped; }
exit 0
