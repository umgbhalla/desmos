#!/bin/sh
set -eu

tag=${1:?release tag required}
state=${2:-/srv/desmos-ci}
target=x86_64-unknown-linux-gnu
export CARGO_HOME="$state/cargo"
export RUSTUP_HOME="$state/rustup"
export PATH="$CARGO_HOME/bin:$state/protobuf/bin:$PATH"
rg_path=$(command -v rg)
export GROK_TOOLS_BUNDLE_RG_PATH="$rg_path"
export GROK_SHELL_BUNDLE_RG_PATH="$rg_path"
version=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')

test "$tag" = "v$version"
python3 -m desmos check
python3 -m unittest discover -s tests -q
python3 inverted.py --check

python3 -m venv "$state/venv"
"$state/venv/bin/python" -m pip wheel --quiet --no-deps . -w dist

export CARGO_TARGET_DIR="$state/target"
rustup toolchain install stable --profile minimal --target "$target"
rustup default stable
cargo build --locked --release --target "$target" -p desmos-tui
env -u NO_COLOR COLORTERM=truecolor TERM=xterm-256color \
  cargo test --locked --release -p desmos-tui
cargo test --locked --release -p xai-grok-markdown -p xai-grok-markdown-core

python3 - <<'PY'
import pathlib
import re

bad = []
root = pathlib.Path(".")
for md in [*root.glob("*.md"), *root.glob("docs/*.md"), *root.glob(".github/**/*.md")]:
    for text, link in re.findall(r"\[([^\]]*)\]\(([^)]+)\)", md.read_text()):
        if link.startswith(("http://", "https://", "#", "mailto:")):
            continue
        target = (md.parent / link.split("#")[0]).resolve()
        if not target.exists():
            bad.append(f"{md}: [{text}]({link})")
if bad:
    raise SystemExit("\n".join(bad))
PY

rc=0
git grep -InE --no-recurse-submodules --untracked \
  'sk-ant-[A-Za-z0-9_-]{16,}|sk-proj-[A-Za-z0-9_-]{16,}|(ANTHROPIC|OPENAI|XAI)_API_KEY=[A-Za-z0-9_-]{16,}|(access|refresh|id)_token"?[[:space:]:=]+"?eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{16,}' \
  -- . || rc=$?
case "$rc" in
  0) echo "possible secret in tree" >&2; exit 1 ;;
  1) echo "no key material found" ;;
  *) echo "git grep failed (rc=$rc)" >&2; exit 1 ;;
esac

mkdir -p dist/package
cp "$CARGO_TARGET_DIR/$target/release/desmos-tui" dist/package/desmos-tui
tar -C dist/package -czf "dist/desmos-tui-$target.tar.gz" desmos-tui
(
  cd dist
  sha256sum "desmos-tui-$target.tar.gz" > "desmos-tui-$target.tar.gz.sha256"
  sha256sum "desmos-$version-py3-none-any.whl" > "desmos-$version-py3-none-any.whl.sha256"
)
