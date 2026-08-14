from __future__ import annotations

"""Vision: get images onto the wire.

cached_payload already passes unfiltered dict blocks through for user
messages, so the wire has always accepted images — what was missing was a
way to put one into world.messages. This does that, and only that.
"""

import base64
import mimetypes
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

MAX_BYTES = 3_500_000  # Anthropic caps a single image near 5MB base64
OK = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _shrink(path: Path, width: int = 1400) -> Path:
    """macOS sips downscale into a temp file. No-op if sips is absent."""
    out = Path(tempfile.mkdtemp()) / path.name
    try:
        subprocess.run(
            ["sips", "--resampleWidth", str(width), str(path), "--out", str(out)],
            capture_output=True,
            check=True,
        )
        return out if out.exists() else path
    except (OSError, subprocess.CalledProcessError):
        return path


def _resolve(p: Path) -> Path:
    """Find a file whose name differs only in which spaces it uses.

    macOS writes screenshot names with U+202F before AM/PM. Any round trip
    through text normalises that to a plain space, so an exact match fails on
    a file that is plainly sitting there. Compare whitespace-collapsed names.
    """
    if p.is_file():
        return p
    want = " ".join(p.name.split())
    if p.parent.is_dir():
        for c in p.parent.iterdir():
            if " ".join(c.name.split()) == want:
                return c
    return p


def image_block(path: str | Path) -> dict[str, Any]:
    """One Anthropic image content block, base64, downscaled if oversized."""
    p = _resolve(Path(path).expanduser())
    if not p.is_file():
        raise FileNotFoundError(p)
    media, _ = mimetypes.guess_type(p.name)
    if media not in OK:
        raise ValueError(f"{media or p.suffix!r} not supported; want {sorted(OK)}")
    if p.stat().st_size > MAX_BYTES:
        p = _shrink(p)
    data = base64.standard_b64encode(p.read_bytes()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media, "data": data},
    }


def attach(world: Any, *paths: str | Path, note: str = "") -> str:
    """Attach images to the most recent user message so the next call sees them.

    Appending a fresh user message would break role alternation mid-turn, so
    the blocks ride along on the last user turn instead.
    """
    blocks = [image_block(p) for p in paths]
    if not blocks:
        return "no images"
    target = None
    for m in reversed(world.messages):
        if m.get("role") == "user":
            target = m
            break
    if target is None:
        world.messages.append({"role": "user", "content": []})
        target = world.messages[-1]
    if isinstance(target.get("content"), str):
        target["content"] = [{"type": "text", "text": target["content"]}]
    elif not isinstance(target.get("content"), list):
        target["content"] = []
    label = note or f"attached {len(blocks)} image(s)"
    target["content"].append({"type": "text", "text": f"[{label}]"})
    target["content"].extend(blocks)
    sizes = [f"{len(b['source']['data']) // 1024}KB" for b in blocks]
    return f"attached {len(blocks)} image(s): {', '.join(str(p) for p in paths)} [{', '.join(sizes)}]"


def shot(world: Any, note: str = "screen") -> str:
    """Capture the screen and attach it. macOS screencapture."""
    out = Path(tempfile.mkdtemp()) / "screen.png"
    subprocess.run(["screencapture", "-x", str(out)], check=True)
    return attach(world, out, note=note)
