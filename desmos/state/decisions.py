"""Decision queue: durable questions that persist until the user answers.

Storage is an append-only JSONL at .desmos/decisions/decisions.jsonl.
The latest record for each decision id wins; answering appends rather than
editing, so the record of the question is never rewritten in place.

Records carry: id (short hex), prompt, options (list), default (str|None),
urgency ("normal"|"high"), status ("open"|"answered"), answer (str|None), ts.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from desmos.state.persist import state_file
from desmos.kernel.types import World

DECISIONS_SUBDIR = "decisions"
DECISIONS_FILENAME = "decisions.jsonl"


def _decisions_path(world: World) -> Path:
    return state_file(world).parent / DECISIONS_SUBDIR / DECISIONS_FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prompt: str) -> str:
    return hashlib.sha256(f"{prompt}{time.time_ns()}".encode()).hexdigest()[:8]


def _append(world: World, rec: dict[str, Any]) -> None:
    # A subagent runs in the parent's cwd with persist=False. Ungated, its
    # <knowledge op=decide> wrote .desmos/decisions/decisions.jsonl into the
    # parent's repo -- save() refused, this did not.
    if not world.persist:
        return
    path = _decisions_path(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _all_records(world: World) -> list[dict[str, Any]]:
    """Every record ever appended, oldest first. Torn final lines are skipped."""
    path = _decisions_path(world)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return out


def _latest(world: World) -> dict[str, dict[str, Any]]:
    """Latest record per id."""
    out: dict[str, dict[str, Any]] = {}
    for rec in _all_records(world):
        out[rec["id"]] = rec
    return out


# ---------------------------------------------------------------- public API

def push(
    world: World,
    prompt: str,
    options: list[str],
    default: str | None = None,
    urgency: str = "normal",
) -> str:
    """Append a new open decision; return its id."""
    if not world.persist:
        return ""
    did = _new_id(prompt)
    _append(world, {
        "id": did,
        "prompt": prompt,
        "options": list(options),
        "default": default,
        "urgency": urgency,
        "status": "open",
        "answer": None,
        "ts": _now(),
    })
    return did


def answer(world: World, did: str, choice: str) -> None:
    """Close an open decision by appending an answered record."""
    if not world.persist:
        raise KeyError("decide disabled for this non-persistent world")
    records = _latest(world)
    if did not in records:
        raise KeyError(f"no decision {did!r}")
    rec = dict(records[did])
    rec["status"] = "answered"
    rec["answer"] = choice
    rec["ts"] = _now()
    _append(world, rec)


def pending(world: World) -> list[dict[str, Any]]:
    """Return all open (unanswered) decisions, oldest first."""
    return [r for r in _latest(world).values() if r.get("status") == "open"]
