"""The work graph: items, edges, leases and an append-only event log.

A todo is a line. A plan is a body of reasoning with steps. Neither survives
being worked on by more than one session, and neither can say *why* an item is
finished. This is the third thing: work as rows, in the harness database, where
a sibling session can see it, claim it, and be refused if it claims what
somebody else already holds.

Four tables (persist.SCHEMA_SQL):

- ``work_items``  a node. Title, body, status, and an optional ``gate``.
- ``work_edges``  parent -> child, kind ``blocks`` by default.
- ``work_events`` append-only. Every claim, release, note and finish, with the
  run that did it and an optional evidence pointer.
- ``work_leases`` at most one row per item. A claim is a *single statement*
  CAS: the upsert only fires when the existing lease has expired or is already
  ours, so two concurrent claimants cannot both win no matter how the
  scheduler interleaves them.

A gate is a row, not code: an item with a non-empty ``gate`` cannot reach
``done`` without an evidence pointer, and the refusal names what it wants.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from desmos.kernel.types import World
from desmos.state.persist import (
    _open,
    _session_id,
    _uuid7,
    _workspace_id,
    run_id,
    state_file,
)

STATUSES = ("open", "claimed", "blocked", "done", "dropped")
EDGE_KINDS = ("blocks", "child", "trigger")
LEASE_SECONDS = 900
MAX_TITLE = 200
MAX_BODY = 8000
#: The event a refused finish leaves behind: gated, and no evidence pointer.
#: Read back by desmos.state.witness as this workspace's gate-failure count.
GATE_REFUSED = "gate-refused"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _later(seconds: int) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds)))
    return when.isoformat(timespec="seconds")


def _row(item: sqlite3.Row) -> dict[str, Any]:
    return {k: item[k] for k in item.keys()}


class WorkError(RuntimeError):
    """A refusal the caller should read, not a defect."""


def _scope(conn: sqlite3.Connection, world: World) -> str:
    workspace = _workspace_id(conn, world)
    assert workspace is not None
    return workspace


def _fetch(conn: sqlite3.Connection, workspace: str, item_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM work_items WHERE id = ? AND workspace_id = ?",
        (item_id, workspace),
    ).fetchone()
    if row is None:
        raise WorkError(f"no work item {item_id!r} in this workspace")
    return row


def _record(
    conn: sqlite3.Connection,
    item_id: str,
    kind: str,
    detail: str = "",
    evidence: str = "",
) -> None:
    conn.execute(
        "INSERT INTO work_events(item_id, ts, run_id, kind, detail, evidence)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, _now(), run_id(), kind, detail[:MAX_BODY], evidence[:MAX_TITLE]),
    )


def add(
    world: World,
    title: str,
    body: str = "",
    kind: str = "task",
    parent: str = "",
    gate: str = "",
) -> dict[str, Any]:
    """Create a node, optionally hung under a parent that blocks it."""
    text = " ".join(title.split())[:MAX_TITLE]
    if not text:
        raise WorkError("work add: a title is required")
    db = _open(state_file(world))
    try:
        with db:
            workspace = _scope(db, world)
            session = _session_id(db, world, workspace)
            # _uuid7() is 48 bits of millisecond then randomness: a
            # 12-char slice is the timestamp alone, so two items added
            # inside one millisecond collide on the primary key.
            item_id = _uuid7()[:20]
            now = _now()
            db.execute(
                "INSERT INTO work_items(id, workspace_id, session_id, title, body,"
                " kind, gate, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                (
                    item_id, workspace, session or "", text, body[:MAX_BODY],
                    kind, gate[:MAX_TITLE], now, now,
                ),
            )
            if parent:
                _fetch(db, workspace, parent)
                db.execute(
                    "INSERT OR IGNORE INTO work_edges(parent_id, child_id, kind,"
                    " created_at) VALUES (?, ?, 'blocks', ?)",
                    (parent, item_id, now),
                )
            _record(db, item_id, "added", text)
            row = _fetch(db, workspace, item_id)
            return _row(row)
    finally:
        db.close()


def link(
    world: World, parent: str, child: str, kind: str = "blocks"
) -> dict[str, Any]:
    """Add an edge. ``blocks`` gates readiness; ``trigger`` only records."""
    if kind not in EDGE_KINDS:
        raise WorkError(f"work link: kind must be one of {EDGE_KINDS}")
    if parent == child:
        raise WorkError("work link: an item cannot block itself")
    db = _open(state_file(world))
    try:
        with db:
            workspace = _scope(db, world)
            _fetch(db, workspace, parent)
            _fetch(db, workspace, child)
            db.execute(
                "INSERT OR IGNORE INTO work_edges(parent_id, child_id, kind,"
                " created_at) VALUES (?, ?, ?, ?)",
                (parent, child, kind, _now()),
            )
            _record(db, child, "linked", f"{kind} from {parent}")
    finally:
        db.close()
    return {"parent": parent, "child": child, "kind": kind}


def items(
    world: World, status: str = "", limit: int = 50
) -> list[dict[str, Any]]:
    db = _open(state_file(world))
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return []
        sql = "SELECT * FROM work_items WHERE workspace_id = ?"
        args: list[Any] = [workspace]
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY created_at, id LIMIT ?"
        args.append(max(1, int(limit)))
        return [_row(r) for r in db.execute(sql, args)]
    finally:
        db.close()


def ready(world: World, limit: int = 20) -> list[dict[str, Any]]:
    """Open items with no unfinished blocker and no live lease."""
    now = _now()
    db = _open(state_file(world))
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return []
        rows = db.execute(
            """
            SELECT i.* FROM work_items AS i
            WHERE i.workspace_id = ?
              AND i.status = 'open'
              AND NOT EXISTS (
                    SELECT 1 FROM work_edges AS e
                    JOIN work_items AS p ON p.id = e.parent_id
                    WHERE e.child_id = i.id AND e.kind = 'blocks'
                      AND p.status NOT IN ('done', 'dropped'))
              AND NOT EXISTS (
                    SELECT 1 FROM work_leases AS l
                    WHERE l.item_id = i.id AND l.expires_at > ?)
            ORDER BY i.created_at, i.id LIMIT ?
            """,
            (workspace, now, max(1, int(limit))),
        )
        return [_row(r) for r in rows]
    finally:
        db.close()


def claim(
    world: World, item_id: str, seconds: int = LEASE_SECONDS, holder: str = ""
) -> dict[str, Any]:
    """Take the lease, or report who holds it. One statement, one winner.

    The CAS lives in the ``WHERE`` of the upsert's DO UPDATE: it fires only if
    the stored lease has already expired, or if it is ours to renew. SQLite
    runs the whole statement inside the write transaction, so a second
    claimant either sees no row (and inserts) or sees a live one (and is
    refused). There is no read-then-write window to lose.
    """
    run = run_id()
    now = _now()
    db = _open(state_file(world))
    try:
        with db:
            workspace = _scope(db, world)
            row = _fetch(db, workspace, item_id)
            if row["status"] in ("done", "dropped"):
                raise WorkError(f"work claim: {item_id} is {row['status']}")
            db.execute(
                """
                INSERT INTO work_leases(item_id, run_id, holder, claimed_at,
                                        expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    holder = excluded.holder,
                    claimed_at = excluded.claimed_at,
                    expires_at = excluded.expires_at
                WHERE work_leases.expires_at <= excluded.claimed_at
                   OR work_leases.run_id = excluded.run_id
                """,
                (item_id, run, holder or run, now, _later(seconds)),
            )
            lease = db.execute(
                "SELECT * FROM work_leases WHERE item_id = ?", (item_id,)
            ).fetchone()
            mine = lease is not None and str(lease["run_id"]) == run
            if mine:
                db.execute(
                    "UPDATE work_items SET status = 'claimed', updated_at = ?"
                    " WHERE id = ?",
                    (now, item_id),
                )
                _record(db, item_id, "claimed", holder or run)
            return {
                "item": item_id,
                "held": mine,
                "run_id": str(lease["run_id"]) if lease is not None else "",
                "holder": str(lease["holder"]) if lease is not None else "",
                "expires_at": str(lease["expires_at"]) if lease is not None else "",
            }
    finally:
        db.close()


def release(world: World, item_id: str) -> dict[str, Any]:
    """Drop our own lease. Someone else's lease is not ours to drop."""
    run = run_id()
    db = _open(state_file(world))
    try:
        with db:
            workspace = _scope(db, world)
            row = _fetch(db, workspace, item_id)
            cur = db.execute(
                "DELETE FROM work_leases WHERE item_id = ? AND run_id = ?",
                (item_id, run),
            )
            freed = cur.rowcount > 0
            if freed:
                if row["status"] == "claimed":
                    db.execute(
                        "UPDATE work_items SET status = 'open', updated_at = ?"
                        " WHERE id = ?",
                        (_now(), item_id),
                    )
                _record(db, item_id, "released")
            return {"item": item_id, "released": freed}
    finally:
        db.close()


def note(
    world: World, item_id: str, detail: str, evidence: str = ""
) -> dict[str, Any]:
    db = _open(state_file(world))
    try:
        with db:
            workspace = _scope(db, world)
            _fetch(db, workspace, item_id)
            _record(db, item_id, "note", detail, evidence)
    finally:
        db.close()
    return {"item": item_id, "detail": detail, "evidence": evidence}


TRIGGER_CHANNEL = "work"


def _fire_triggers(
    conn: sqlite3.Connection,
    world: World,
    workspace: str,
    item_id: str,
    title: str,
) -> list[str]:
    """A trigger is a row: finishing this item wakes whoever waits on it.

    The wake goes out on the peer channel that already exists, in the same
    transaction as the finish, so a trigger cannot fire for work that rolled
    back. Waking a *seat* is the eventual shape; until seats are stored, the
    workspace channel reaches every live sibling and none of the dead ones.
    """
    woken: list[str] = []
    rows = conn.execute(
        "SELECT e.child_id AS child, i.title AS title FROM work_edges AS e"
        " JOIN work_items AS i ON i.id = e.child_id"
        " WHERE e.parent_id = ? AND e.kind = 'trigger'"
        " AND i.status NOT IN ('done', 'dropped')",
        (item_id,),
    ).fetchall()
    if not rows:
        return woken
    session = _session_id(conn, world, workspace)
    now = _now()
    run = run_id()
    for row in rows:
        child = str(row["child"])
        _record(conn, child, "triggered", f"{item_id} finished: {title}")
        conn.execute(
            "INSERT INTO channel_messages(workspace_id, session_id, channel,"
            " run_id, author, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                workspace, session or "", TRIGGER_CHANNEL, run, "work",
                f"{item_id} finished ({title}) -- {child} is up: {row['title']}",
                now,
            ),
        )
        woken.append(child)
    return woken


def finish(
    world: World, item_id: str, evidence: str = "", status: str = "done"
) -> dict[str, Any]:
    """Close an item. A gated item needs its evidence pointer first."""
    if status not in ("done", "dropped"):
        raise WorkError("work finish: status must be done or dropped")
    db = _open(state_file(world))
    try:
        with db:
            workspace = _scope(db, world)
            row = _fetch(db, workspace, item_id)
            gate = str(row["gate"] or "")
            refused = bool(status == "done" and gate and not evidence.strip())
            if refused:
                # Recorded, then raised. A refusal that leaves no trace is a
                # rule nobody can count, and that count is what tells a later
                # session whether its gates are gating anything. The event
                # commits when this block exits; the raise is deliberately
                # outside it, because raising here would roll it back.
                _record(db, item_id, GATE_REFUSED, gate)
            else:
                db.execute(
                    "UPDATE work_items SET status = ?, updated_at = ? WHERE id = ?",
                    (status, _now(), item_id),
                )
                db.execute("DELETE FROM work_leases WHERE item_id = ?", (item_id,))
                _record(db, item_id, status, "", evidence)
                done = _row(_fetch(db, workspace, item_id))
                if status == "done":
                    done["woke"] = _fire_triggers(
                        db, world, workspace, item_id, str(row["title"])
                    )
                return done
        raise WorkError(
            f"work finish: {item_id} is gated on {gate!r};"
            " give an evidence pointer"
        )
    finally:
        db.close()


def reopen(world: World, item_id: str, reason: str = "") -> dict[str, Any]:
    """Put a closed item back to work.

    The event this writes is the rework signal: an item called done that was
    not. Witness counts it against whoever closed it, which is the only way a
    gate that never gates shows up as a number rather than as a feeling.
    """
    db = _open(state_file(world))
    try:
        with db:
            workspace = _scope(db, world)
            row = _fetch(db, workspace, item_id)
            if row["status"] not in ("done", "dropped"):
                raise WorkError(f"work reopen: {item_id} is {row['status']}")
            db.execute(
                "UPDATE work_items SET status = 'open', updated_at = ? WHERE id = ?",
                (_now(), item_id),
            )
            _record(db, item_id, "open", reason)
            return _row(_fetch(db, workspace, item_id))
    finally:
        db.close()


def events(world: World, item_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
    db = _open(state_file(world))
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return []
        sql = (
            "SELECT e.* FROM work_events AS e JOIN work_items AS i"
            " ON i.id = e.item_id WHERE i.workspace_id = ?"
        )
        args: list[Any] = [workspace]
        if item_id:
            sql += " AND e.item_id = ?"
            args.append(item_id)
        sql += " ORDER BY e.id DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return [_row(r) for r in db.execute(sql, args)]
    finally:
        db.close()


MARKS = {
    "open": " ", "claimed": ">", "blocked": "!", "done": "x", "dropped": "-",
}


def render(world: World, limit: int = 30) -> str:
    rows = items(world, limit=limit)
    if not rows:
        return "no work items"
    live = {r["item"]: r for r in []}
    del live
    open_ids = {str(r["id"]) for r in ready(world, limit=limit)}
    lines = []
    for row in rows:
        mark = MARKS.get(str(row["status"]), "?")
        flag = " ready" if str(row["id"]) in open_ids else ""
        gate = f" gate:{row['gate']}" if row["gate"] else ""
        lines.append(f"[{mark}] {row['id']}  {row['title']}{gate}{flag}")
    return "\n".join(lines)
