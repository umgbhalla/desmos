"""Exact single-occurrence replace — Prime's edit, without the host MIME."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def apply_edit(path: str, old_str: str, new_str: str, *, cwd: Path | None = None) -> str:
    if not path:
        return "edit failed: path required"
    if not old_str:
        return "edit failed: old string required"
    filepath = Path(path).expanduser()
    if not filepath.is_absolute() and cwd is not None:
        filepath = cwd / filepath
    if not filepath.is_file():
        return f"edit failed: {path} not found"
    # read_bytes, not read_text: read_text translates newlines, so editing one
    # word in a CRLF file wrote the whole file back as LF and a one-line change
    # showed up as a whole-file diff. And a file that is not text at all has to
    # come back as a result the model can read, not a UnicodeDecodeError thrown
    # out of the syscall.
    try:
        content = filepath.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return f"edit failed: {path} is not utf-8 text — <edit> only replaces text"
    count = content.count(old_str)
    if count == 0:
        return f"edit failed: string not found in {path}"
    if count > 1:
        return f"edit failed: found {count} occurrences in {path}, need exactly 1 — widen the snippet"
    next_text = content.replace(old_str, new_str, 1)
    if filepath.suffix == ".py":
        try:
            compile(next_text, str(filepath), "exec")
        except SyntaxError as exc:
            # A null byte has no line number, and "line None" reads like the
            # gate is broken rather than like the source is.
            where = f" line {exc.lineno}" if exc.lineno else ""
            return f"edit failed: SyntaxError{where}: {exc.msg} — not written"
    # Written beside the target and renamed over it: write_text truncated first,
    # so a crash or a full disk mid-write left the file half-there with no
    # copy of what it used to be. resolve() first, or os.replace would swap a
    # symlink for a regular file instead of editing what it points at.
    target = filepath.resolve()
    tmp = target.with_name(f".{target.name}.desmos-{os.getpid()}")
    try:
        tmp.write_bytes(next_text.encode("utf-8"))
        shutil.copymode(target, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return f"Edited {target}"


def parse_edit_body(body: str, attrs: dict[str, str]) -> tuple[str, str]:
    old = attrs.get("old_str") or attrs.get("old") or ""
    new = attrs.get("new_str") or attrs.get("new") or ""
    text = body.strip("\n")
    if "\n---\n" in text:
        left, right = text.split("\n---\n", 1)
        if "\n---\n" in right or right.startswith("---\n") or right.rstrip().endswith("\n---"):
            raise ValueError(
                "edit body has more than one --- delimiter; the replacement is "
                "ambiguous. Use old_str=/new_str= attrs, or edit in smaller pieces."
            )
        return left, right
    return old, new


def handle(body: str, path: str = "", **attrs: str) -> str:
    old, new = parse_edit_body(body, attrs)
    cwd = None
    try:
        from IPython import get_ipython

        ip = get_ipython()
        if ip is not None:
            cwd = Path(ip.user_ns.get("CWD") or Path.cwd())
    except Exception:
        cwd = Path.cwd()
    return apply_edit(path or attrs.get("path", ""), old, new, cwd=cwd)


def run(path: str, old_str: str, new_str: str) -> str:
    return apply_edit(path, old_str, new_str, cwd=Path.cwd())
