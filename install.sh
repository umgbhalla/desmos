#!/bin/sh
set -eu

repo=${DESMOS_REPO:-umgbhalla/desmos}
bin_dir=${DESMOS_BIN_DIR:-"$HOME/.local/bin"}
data_dir=${DESMOS_DATA_DIR:-"${XDG_DATA_HOME:-$HOME/.local/share}/desmos"}
tag=${1:-${DESMOS_VERSION:-}}

if [ -z "$tag" ]; then
  tag=$(curl -fsSL "https://api.github.com/repos/$repo/releases?per_page=1" | python3 -c 'import json, sys; print(json.load(sys.stdin)[0]["tag_name"])')
fi
version=${tag#v}

case "$(uname -s):$(uname -m)" in
  Darwin:arm64) target=aarch64-apple-darwin ;;
  Darwin:x86_64) target=x86_64-apple-darwin ;;
  Linux:x86_64) target=x86_64-unknown-linux-gnu ;;
  Linux:aarch64|Linux:arm64) target=aarch64-unknown-linux-gnu ;;
  *) echo "desmos: unsupported platform $(uname -s) $(uname -m)" >&2; exit 1 ;;
esac

base="https://github.com/$repo/releases/download/$tag"
archive="desmos-tui-$target.tar.gz"
wheel="desmos-$version-py3-none-any.whl"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

curl -fsSL "$base/$archive" -o "$tmp/$archive"
curl -fsSL "$base/$archive.sha256" -o "$tmp/$archive.sha256"
curl -fsSL "$base/$wheel" -o "$tmp/$wheel"
curl -fsSL "$base/$wheel.sha256" -o "$tmp/$wheel.sha256"

if command -v shasum >/dev/null 2>&1; then
  (cd "$tmp" && shasum -a 256 -c "$archive.sha256" && shasum -a 256 -c "$wheel.sha256")
else
  (cd "$tmp" && sha256sum -c "$archive.sha256" && sha256sum -c "$wheel.sha256")
fi

python3 -m venv "$data_dir/venv"
"$data_dir/venv/bin/python" -m pip install --quiet --upgrade "$tmp/$wheel"
tar -C "$tmp" -xzf "$tmp/$archive"
mkdir -p "$bin_dir"
install -m 755 "$tmp/desmos-tui" "$bin_dir/desmos-tui"
ln -sf "$data_dir/venv/bin/desmos" "$bin_dir/desmos"

echo "installed desmos $tag in $bin_dir"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "add $bin_dir to PATH" ;;
esac
echo "run: desmos tui"
