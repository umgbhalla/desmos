"""Witnessed work: what this workspace actually finished, and who did it.

The work graph records every state change an item goes through, and until now
nothing ever read those events back as an account of a session. A run would
claim, finish and gate items for an hour and then wake with no memory of it --
which is the same failure as an unread ledger, one layer up: the record exists,
nobody is shown it.

This module is the reading. It derives, it does not write: work_events is
already append-only and durable, and git already holds the commits, so a second
store here would be a copy that can disagree with both. What it adds is
attribution and a window -- per actor, inside a span -- and one paragraph
delivered at wake, where it changes what the next turn does.

Attribution is by run id, because that is the identity the work graph already
stamps on every event. When seats land (ARES 3) a seat id is a label on top of
this, not a replacement for it: a run is what actually did the work, and a seat
is who the user thinks did.

Four numbers per actor, chosen because each one can go wrong on its own:

- **done** and **dropped** -- what closed, and how.
- **rework** -- items that were finished and then reopened. A high count is not
  failure, it is a gate that was not gating.
- **gates** -- refusals: a gated item whose finish had no evidence pointer. The
  refusal is recorded rather than only raised, because a number nobody counts
  is a rule nobody obeys.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

from desmos.kernel import catalog
from desmos.kernel.types import World
from desmos.state.persist import _open, _workspace_id, state_file
from desmos.state.work import GATE_REFUSED

#: Injection name for the wake paragraph. Idempotent by name.
BLOCK = "witness"

DEFAULT_HOURS = 168.0
#: Events that mean an item went back to being work after it had closed.
REOPEN = ("open", "claimed", "blocked")
#: GATE_REFUSED is imported above: one spelling, defined where finish writes it.


def _since(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=float(hours))).isoformat()


def _short(run: str) -> str:
    return run[:8] if run else "unattributed"


def _blank(run: str, ts: str) -> dict:
    return {"actor": run, "label": _short(run), "done": 0, "dropped": 0,
            "rework": 0, "gates": 0, "first": ts, "last": ts}


def actors(world: World, hours: float = DEFAULT_HOURS) -> list[dict[str, Any]]:
    """Per-actor counts inside the window, busiest first."""
    if not getattr(world, "persist", False):
        return []
    try:
        conn = _open(state_file(world))
    except Exception:  # noqa: BLE001 - no ledger is not a claim about work
        return []
    try:
        workspace = _workspace_id(conn, world, create=False)
        if workspace is None:
            return []
        rows = conn.execute(
            """
            SELECT e.run_id AS run, e.kind AS kind, e.item_id AS item, e.ts AS ts
            FROM work_events AS e JOIN work_items AS i ON i.id = e.item_id
            WHERE i.workspace_id = ? AND e.ts >= ?
            ORDER BY e.id
            """,
            (workspace, _since(hours)),
        ).fetchall()
    finally:
        conn.close()

    tally: dict[str, dict[str, Any]] = {}
    closed: dict[str, str] = {}
    for row in rows:
        run, kind = str(row["run"] or ""), str(row["kind"])
        item, ts = str(row["item"]), str(row["ts"])
        slot = tally.setdefault(run, _blank(run, ts))
        slot["last"] = ts
        if kind == "done":
            slot["done"] += 1
            closed[item] = run
        elif kind == "dropped":
            slot["dropped"] += 1
        elif kind == GATE_REFUSED:
            slot["gates"] += 1
        elif kind in REOPEN and item in closed:
            # Charged to whoever closed it, not to whoever reopened it: the
            # number is about the gate that let it through.
            owner = closed.pop(item)
            tally.setdefault(owner, _blank(owner, ts))["rework"] += 1
    out = list(tally.values())
    out.sort(key=lambda r: (-(r["done"] + r["dropped"]), r["label"]))
    return out


def finished(
    world: World, hours: float = DEFAULT_HOURS, limit: int = 8
) -> list[dict[str, Any]]:
    """The items that closed in the window, newest first, with their evidence."""
    if not getattr(world, "persist", False):
        return []
    try:
        conn = _open(state_file(world))
    except Exception:  # noqa: BLE001
        return []
    try:
        workspace = _workspace_id(conn, world, create=False)
        if workspace is None:
            return []
        rows = conn.execute(
            """
            SELECT i.id AS id, i.title AS title, e.kind AS kind,
                   e.evidence AS evidence, e.run_id AS run, e.ts AS ts
            FROM work_events AS e JOIN work_items AS i ON i.id = e.item_id
            WHERE i.workspace_id = ? AND e.ts >= ? AND e.kind IN (?, ?)
            ORDER BY e.id DESC LIMIT ?
            """,
            (workspace, _since(hours), "done", "dropped", max(1, int(limit))),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def commits(world: World, hours: float = DEFAULT_HOURS, limit: int = 8) -> list[str]:
    """What landed in git in the window. Empty is a fine answer."""
    root = getattr(world, "cwd", None)
    if root is None or not (root / ".git").exists():
        return []
    try:
        done = subprocess.run(
            ["git", "log", "--since=" + _since(hours),
             "--max-count=" + str(int(limit)), "--format=%h %s"],
            cwd=str(root), capture_output=True, text=True, timeout=5.0,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except Exception:  # noqa: BLE001 - a slow or missing git says nothing here
        return []
    if done.returncode != 0:
        return []
    return [line for line in done.stdout.splitlines() if line.strip()][:limit]


def digest(world: World, hours: float = DEFAULT_HOURS) -> dict[str, Any]:
    who = actors(world, hours)
    return {
        "hours": float(hours),
        "since": _since(hours),
        "actors": who,
        "finished": finished(world, hours),
        "commits": commits(world, hours),
        "done": sum(int(a["done"]) for a in who),
        "rework": sum(int(a["rework"]) for a in who),
        "gates": sum(int(a["gates"]) for a in who),
    }


def text(state: dict[str, Any]) -> str:
    lines = [
        "Witnessed work, last {:.0f}h: {} item(s) closed by {} run(s), {} "
        "reopened after being called done, {} gate refusal(s).".format(
            state["hours"], state["done"], len(state["actors"]),
            state["rework"], state["gates"],
        )
    ]
    for row in state["finished"][:4]:
        mark = "x" if row["kind"] == "done" else "-"
        note = " [" + str(row["evidence"]) + "]" if row["evidence"] else ""
        lines.append("  [" + mark + "] " + str(row["title"]) + note)
    if state["commits"]:
        lines.append("  landed: " + "; ".join(state["commits"][:3]))
    lines.append(
        "This is the record another session reads as what you did. An item "
        "closed with no evidence pointer reads, later, as a claim."
    )
    return chr(10).join(lines)


def wake(world: World, hours: float = DEFAULT_HOURS) -> str:
    """Show the last window's account once, in the next request's tail."""
    state = digest(world, hours)
    if not state["actors"] and not state["commits"]:
        catalog.retire(world, BLOCK)
        return ""
    body = text(state)
    catalog.inject(world, BLOCK, body, turns=1)
    return body


def render(world: World, hours: float = DEFAULT_HOURS) -> str:
    state = digest(world, hours)
    if not state["actors"] and not state["commits"]:
        return "no witnessed work in the last {:.0f}h".format(hours)
    lines = [text(state), ""]
    for row in state["actors"]:
        lines.append(
            "  {}  done {}  dropped {}  rework {}  gates {}".format(
                row["label"], row["done"], row["dropped"],
                row["rework"], row["gates"],
            )
        )
    return chr(10).join(lines)
