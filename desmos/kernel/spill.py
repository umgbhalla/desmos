"""Output too large for the transcript goes to a file, not into the void.

A clipped result loses the part that was not shown, and the part that was not
shown is regularly the part that mattered -- the failing assertion at the end
of a build log, the row of the table being asked about. `spill` keeps the same
budget for the transcript, writes the whole thing to `.desmos/out/`, and puts
the path at the *top* of what comes back, with a line saying what to do with
it. Top, not bottom: anything downstream that trims further trims the tail.
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

from desmos.kernel.scan import clip

#: Where spilled output lands, relative to the world's cwd.
SPILL_DIR = ".desmos/out"

#: What makes a first line a spill pointer. Matched, not just written: a result
#: trimmed twice must reuse the file the first trim wrote instead of writing a
#: second file holding its own head.
MARK = "full output in "


def pointer(path: Path | str, total: int) -> str:
    """The line that leads a spilled result. Carries no count of what is
    shown, so a second, tighter trim can keep it verbatim."""
    return (
        f"\u2026[{total} chars; {MARK}{path} \u2014 grep/sed/head that file for the "
        "part you need, do not cat it back into the transcript]"
    )


def _split_pointer(text: str) -> tuple[str, str]:
    """`(body, pointer)` -- pointer is empty when this text was never spilled."""
    line, sep, rest = text.partition("\n")
    if sep and line.startswith("\u2026[") and MARK in line:
        return rest, line
    return text, ""


def _next_path(out: Path, tag: str) -> Path:
    """A unique name. `max(glob)+1` raced when two spills landed together."""
    stamp = time.time_ns()
    token = secrets.token_hex(3)
    return out / f"{stamp}-{os.getpid()}-{token}-{tag}.txt"


#: How many spilled outputs to keep. Every oversized syscall writes one, so
#: without a ceiling a long session leaves a directory nobody reads.
KEEP = 200


def _prune(out: Path) -> None:
    files = sorted(out.glob("*.txt"))
    for old in files[: max(0, len(files) - KEEP)]:
        old.unlink(missing_ok=True)


def _write(text: str, tag: str, cwd: Path | None) -> Path | None:
    """Write the whole output under cwd, or None if the disk will not take it."""
    try:
        base = Path(cwd) if cwd is not None else Path.cwd()
        out = base / SPILL_DIR
        out.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in tag) or "result"
        path = _next_path(out, safe)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", errors="replace")
        os.replace(tmp, path)
        _prune(out)
        return path.relative_to(base)
    except Exception:  # noqa: BLE001 -- a read-only cwd must not fail the call
        return None


def spill(
    text: str,
    cap: int,
    *,
    tag: str = "result",
    cwd: Path | None = None,
    keep: str = "head",
) -> str:
    """Trim to `cap` for the transcript, keeping the whole output on disk.

    Under the cap this is the identity. Over it, the result is a pointer line
    naming the file, then as much of the output as still fits. Idempotent: a
    result that already carries a pointer is trimmed against that same file
    rather than spilled a second time.
    """
    if len(text) <= cap:
        return text
    body, mark = _split_pointer(text)
    if not mark:
        path = _write(text, tag, cwd)
        if path is None:
            # Nowhere to put it -- clipping is still better than failing a
            # syscall that otherwise worked.
            return clip(text, cap, keep=keep)
        mark = pointer(path, len(text))
        body = text
    room = max(cap - len(mark) - 1, 240)
    return mark + "\n" + clip(body, room, keep=keep)
