#!/usr/bin/env bash
# Clone vendor/grok-build at the pinned rev and apply our patches.
#
# vendor/ is gitignored (analysis clone, never committed), but the patches in
# patches/ are load-bearing: without them DESMOS_ACP is dead and `--grok` runs
# grok's in-process agent instead of `python -m desmos acp`. Re-run after any
# vendor pull.
set -euo pipefail

REPO=https://github.com/xai-org/grok-build
REV=eb267feff13129e568df38fb6fdf0ceb65f735d6

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
dst=$root/vendor/grok-build

if [ ! -d "$dst/.git" ]; then
    echo "cloning grok-build -> $dst"
    git clone "$REPO" "$dst"
fi

cd "$dst"
if [ "$(git rev-parse HEAD)" != "$REV" ]; then
    git fetch origin
    git checkout --detach "$REV"
fi

for p in "$root"/patches/*.patch; do
    [ -e "$p" ] || continue
    if git apply --reverse --check "$p" 2>/dev/null; then
        echo "already applied: $(basename "$p")"
    else
        echo "applying: $(basename "$p")"
        git apply "$p"
    fi
done

echo "vendor/grok-build ready at $REV with $(ls -1 "$root"/patches/*.patch 2>/dev/null | wc -l | tr -d ' ') patch(es)"
