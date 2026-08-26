"""HTML from Grok's markdown parser (`xai-grok-markdown-core` via desmos-md-html).

Desk paint is HTML. The grammar is the same `offset_events` stream the TUI
renders. Fence colors are syntect Tokyo Night in that binary, not a second
parser. Hash-gate is `desmos.front.hashgate`, the same module the TUI uses.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from desmos.front import hashgate

_CACHE: dict[str, str] = {}
_CACHE_MAX = 64


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _watch(root: Path) -> list[Path]:
    return [
        root / "crates" / "desmos-md-html",
        root / "crates" / "xai-grok-markdown-core",
        root / "crates" / "xai-grok-markdown" / "assets" / "tokyo-night.tmTheme",
        root / "Cargo.toml",
        root / "Cargo.lock",
    ]


def _stamp_path(root: Path, release: bool) -> Path:
    profile = "release" if release else "debug"
    return root / "target" / profile / ".desmos-md-html.hash"


def _binary(root: Path, release: bool) -> Path:
    profile = "release" if release else "debug"
    return root / "target" / profile / "desmos-md-html"


def ensure(release: bool = False) -> Path:
    """Return the desmos-md-html binary, compiling when our sources moved."""
    import shutil

    root = _repo_root()
    path = _binary(root, release)
    sources = hashgate.collect_sources(_watch(root))
    digest = hashgate.content_hash(root, sources)
    if not hashgate.stale(path, _stamp_path(root, release), sources, digest):
        return path
    cargo = shutil.which("cargo")
    if cargo is None:
        raise FileNotFoundError("cargo is required to build desmos-md-html")
    cmd = [cargo, "build", "-p", "desmos-md-html"]
    if release:
        cmd.append("--release")
    env = os.environ.copy()
    env.pop("CARGO_TERM_QUIET", None)
    env.pop("RUSTFLAGS", None)
    built = hashgate.cargo_offline_then(cmd, root, env)
    if built != 0 or not path.is_file():
        raise RuntimeError("desmos-md-html failed to build")
    hashgate.write_stamp(_stamp_path(root, release), digest)
    return path


def render(src: str, *, release: bool = False) -> str:
    """Render markdown to HTML through the grok parser binary."""
    text = str(src or "")
    hit = _CACHE.get(text)
    if hit is not None:
        return hit
    path = ensure(release=release)
    ran = subprocess.run(
        [str(path)],
        input=text,
        capture_output=True,
        text=True,
        check=False,
    )
    if ran.returncode != 0:
        raise RuntimeError(ran.stderr.strip() or "desmos-md-html failed")
    html = ran.stdout
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[text] = html
    return html
