"""Content-hash gate for cargo binaries.

TUI, Comet, and desmos-md-html all ask the same question: is the binary we
have the one these source bytes would produce? mtime is only the fallback
when a stamp file was never written (an out-of-band `cargo build`).
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path


def collect_sources(watch: Sequence[Path]) -> list[Path]:
    """Every file under watch, skipping `.git` and `target`."""
    files: list[Path] = []
    for path in watch:
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                f
                for f in path.rglob("*")
                if f.is_file() and ".git" not in f.parts and "target" not in f.parts
            )
    return sorted(files)


def content_hash(root: Path, files: Sequence[Path]) -> str:
    """sha256 of relative path + bytes. Same bytes, same digest."""
    h = hashlib.sha256()
    for f in files:
        h.update(str(f.relative_to(root)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def write_stamp(stamp: Path, digest: str) -> None:
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(digest, encoding="utf-8")


def stale(binary: Path, stamp: Path, sources: Sequence[Path], digest: str) -> bool:
    """True when the binary was built from different source bytes than digest.

    Missing stamp: if every source is older than the binary, adopt it and
    write the stamp so the next launch does not cargo for want of a file.
    """
    if not binary.is_file():
        return True
    if not stamp.is_file():
        newest = max((f.stat().st_mtime for f in sources), default=0.0)
        try:
            built_at = binary.stat().st_mtime
        except OSError:
            return True
        if newest <= built_at:
            write_stamp(stamp, digest)
            return False
        return True
    try:
        return stamp.read_text(encoding="utf-8").strip() != digest
    except OSError:
        return True


def cargo_offline_then(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> int:
    """`--offline` first so a warm cache does not hit the network, then retry."""
    built = subprocess.call([*cmd, "--offline"], cwd=str(cwd), env=env)
    if built != 0:
        built = subprocess.call(cmd, cwd=str(cwd), env=env)
    return built
