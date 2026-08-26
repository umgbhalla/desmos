"""Environment viewers the ACP agent serves to desk.

These are the same reads the TUI already does: git with optional locks off,
one directory at a time, a bounded file prefix. They are not a second git
engine and not a fake workspace. Paths are jailed to the session cwd because
desk is a loopback web client, not a local TUI.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

GIT_CAP = 200
MAX_ENTRIES = 2000
MAX_BYTES = 512 * 1024
MAX_LINES = 4000


def git_snapshot(cwd: Path) -> dict[str, Any]:
    """TUI `side.rs` `read_repo`: status -b, branches, log. Reader only."""
    snap: dict[str, Any] = {
        "branch": "",
        "status": [],
        "branches": [],
        "log": [],
        "dirty": 0,
        "error": None,
        "read": True,
    }
    porcelain, err = _git(cwd, ["status", "--porcelain", "-b"])
    if err is not None:
        snap["error"] = err
        return snap
    for line in porcelain.splitlines():
        if line.startswith("## "):
            head = line[3:]
            snap["branch"] = head.split()[0] if head.split() else head
            continue
        if len(line) < 4:
            continue
        snap["dirty"] += 1
        if len(snap["status"]) >= GIT_CAP:
            continue
        mark, rest = line[:2], line[2:].strip()
        name = rest.rsplit(" -> ", 1)[-1]
        snap["status"].append({
            "text": name,
            "mark": mark.strip(),
            "path": name,
        })
    branches, _ = _git(cwd, ["branch", "--sort=-committerdate", "-v"])
    if branches:
        for line in branches.splitlines()[:GIT_CAP]:
            current = line.startswith("*")
            snap["branches"].append({
                "text": line.lstrip("*").strip(),
                "mark": "*" if current else "",
                "path": None,
            })
    log, _ = _git(cwd, ["log", "--oneline", "--decorate", "-n", "80"])
    if log:
        for line in log.splitlines():
            sha, _, rest = line.partition(" ")
            snap["log"].append({"text": rest, "mark": sha, "path": None})
    return snap


def _git(cwd: Path, args: list[str]) -> tuple[str, str | None]:
    try:
        env = os.environ.copy()
        env["GIT_OPTIONAL_LOCKS"] = "0"
        completed = subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc)
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "git failed").strip()
        return "", err.splitlines()[0] if err else "git failed"
    return completed.stdout, None


def jail(cwd: Path, rel: str) -> Path:
    """Resolve `rel` under cwd. Symlinks that escape are refused."""
    root = Path(cwd).resolve()
    raw = Path(str(rel) or ".")
    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path {rel!r} is outside {root}") from exc
    return candidate


def fs_list(cwd: Path, rel: str = ".") -> dict[str, Any]:
    root = Path(cwd).resolve()
    directory = jail(cwd, rel)
    if not directory.is_dir():
        return {
            "dir": _rel(root, directory),
            "entries": [],
            "note": "not a directory",
        }
    entries: list[dict[str, Any]] = []
    note = None
    try:
        with os.scandir(directory) as it:
            for i, item in enumerate(it):
                if i >= MAX_ENTRIES:
                    note = f"truncated at {MAX_ENTRIES} entries"
                    break
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                entries.append({"name": item.name, "is_dir": is_dir})
    except OSError as exc:
        return {"dir": _rel(root, directory), "entries": [], "note": str(exc)}
    entries.sort(key=lambda row: (not row["is_dir"], row["name"].lower()))
    if directory != root:
        entries.insert(0, {"name": "..", "is_dir": True})
    return {"dir": _rel(root, directory), "entries": entries, "note": note}


def fs_read(cwd: Path, rel: str) -> dict[str, Any]:
    root = Path(cwd).resolve()
    path = jail(cwd, rel)
    out: dict[str, Any] = {
        "path": _rel(root, path),
        "lines": [],
        "note": None,
        "truncated": False,
        "binary": False,
    }
    try:
        info = path.stat()
    except OSError as exc:
        out["note"] = str(exc)
        return out
    if stat.S_ISDIR(info.st_mode):
        out["note"] = "is a directory"
        return out
    try:
        raw = path.read_bytes()[: MAX_BYTES + 1]
    except OSError as exc:
        out["note"] = str(exc)
        return out
    if len(raw) > MAX_BYTES:
        raw = raw[:MAX_BYTES]
        out["truncated"] = True
    if b"\0" in raw[:4096]:
        out["binary"] = True
        out["note"] = "binary file"
        return out
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        out["truncated"] = True
    out["lines"] = lines
    return out


def bridge_status(cwd: Path) -> dict[str, Any]:
    """Whether a TUI/daemon bridge socket exists. Desk does not attach to it.

    Two live writers on one workspace overwrite each other's transcript.
    peers() is the honest live-front list.
    """
    sock = Path(cwd).resolve() / ".desmos" / "bridge.sock"
    return {
        "socket": str(sock) if sock.exists() else None,
        "attached": False,
        "reason": (
            "desk hosts AcpServer in-process; attaching to a live bridge "
            "would be a second writer on the same persist brain"
            if sock.exists()
            else "no bridge socket in this cwd"
        ),
    }


def _rel(root: Path, path: Path) -> str:
    rel = path.relative_to(root)
    text = rel.as_posix()
    return "." if text == "." else text
