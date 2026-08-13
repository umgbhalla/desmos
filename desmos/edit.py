"""Exact single-occurrence replace — Prime's edit, without the host MIME."""

from __future__ import annotations

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
    content = filepath.read_text(encoding="utf-8")
    count = content.count(old_str)
    if count == 0:
        return f"edit failed: string not found in {path}"
    if count > 1:
        return f"edit failed: found {count} occurrences in {path}, need exactly 1 — widen the snippet"
    filepath.write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
    return f"Edited {filepath.resolve()}"


def parse_edit_body(body: str, attrs: dict[str, str]) -> tuple[str, str]:
    old = attrs.get("old_str") or attrs.get("old") or ""
    new = attrs.get("new_str") or attrs.get("new") or ""
    text = body.strip("\n")
    if "\n---\n" in text:
        left, right = text.split("\n---\n", 1)
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
