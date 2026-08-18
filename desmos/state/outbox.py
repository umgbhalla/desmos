"""The transactional outbox: work leaves this machine only after it landed here.

Local-first means the harness database is the record and the cloud is a copy,
never the other way round. So nothing here talks to a network. A row is
enqueued in the *same transaction* as the fact it describes -- archive a
session and its outbox row commits with it, or neither exists -- and a
separate drain hands batches to whatever sink the caller passes.

Idempotency is a fingerprint, not a flag: ``sha256`` over canonical JSON of
the kind and payload, ``UNIQUE`` in the schema, inserted with ``OR IGNORE``.
Enqueueing the same fact twice is one row, whether the repeat comes from a
retry, a second session, or a replayed archive.

The drain is push-only and it never deletes. A sent row keeps its payload and
gains ``sent_at``; a failed one keeps its place, counts an attempt, and stores
the error. Nothing in this module can lose a row, which is the only property
worth having before a real sink exists.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from desmos.kernel.types import World
from desmos.state.persist import _open, _workspace_id, state_file

BATCH = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(kind: str, payload: Any) -> str:
    """Byte-stable JSON: sorted keys, no incidental whitespace."""
    return json.dumps(
        {"kind": kind, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def fingerprint(kind: str, payload: Any) -> str:
    return hashlib.sha256(canonical(kind, payload).encode("utf-8")).hexdigest()


def enqueue_conn(
    conn: sqlite3.Connection, workspace: str, kind: str, payload: Any
) -> str:
    """Enqueue on the caller's connection, inside the caller's transaction."""
    mark = fingerprint(kind, payload)
    conn.execute(
        "INSERT OR IGNORE INTO outbox(workspace_id, kind, fingerprint,"
        " payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (workspace, kind, mark, json.dumps(payload, default=str), _now()),
    )
    return mark


def enqueue(world: World, kind: str, payload: Any) -> str:
    db = _open(state_file(world))
    try:
        with db:
            workspace = _workspace_id(db, world)
            assert workspace is not None
            return enqueue_conn(db, workspace, kind, payload)
    finally:
        db.close()


def _rows(cursor: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    out = []
    for row in cursor:
        item = {k: row[k] for k in row.keys()}
        try:
            item["payload"] = json.loads(item["payload_json"])
        except Exception:
            item["payload"] = None
        out.append(item)
    return out


def pending(world: World, limit: int = BATCH) -> list[dict[str, Any]]:
    db = _open(state_file(world))
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return []
        return _rows(db.execute(
            "SELECT * FROM outbox WHERE workspace_id = ? AND sent_at IS NULL"
            " ORDER BY id LIMIT ?",
            (workspace, max(1, int(limit))),
        ))
    finally:
        db.close()


def stats(world: World) -> dict[str, int]:
    db = _open(state_file(world))
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return {"pending": 0, "sent": 0, "attempts": 0}
        row = db.execute(
            "SELECT count(*) FILTER (WHERE sent_at IS NULL) AS pending,"
            " count(*) FILTER (WHERE sent_at IS NOT NULL) AS sent,"
            " coalesce(sum(attempts), 0) AS attempts"
            " FROM outbox WHERE workspace_id = ?",
            (workspace,),
        ).fetchone()
        return {k: int(row[k]) for k in row.keys()}
    finally:
        db.close()


def drain(
    world: World,
    sink: Callable[[list[dict[str, Any]]], Any],
    limit: int = BATCH,
) -> dict[str, Any]:
    """Hand one batch to the sink; mark it sent only if the sink returned.

    A sink that raises leaves every row of the batch exactly where it was,
    with one more attempt and the error recorded. A sink that returns is
    trusted for that batch and nothing else -- the next batch is a separate
    decision, so a partial outage costs a retry rather than the queue.
    """
    batch = pending(world, limit)
    out: dict[str, Any] = {"sent": 0, "failed": 0, "error": ""}
    if not batch:
        return out
    ids = [int(r["id"]) for r in batch]
    marks = ",".join("?" * len(ids))
    db = _open(state_file(world))
    try:
        try:
            sink(batch)
        except Exception as exc:
            with db:
                db.execute(
                    f"UPDATE outbox SET attempts = attempts + 1, last_error = ?"
                    f" WHERE id IN ({marks})",
                    [str(exc)[:400], *ids],
                )
            out["failed"] = len(ids)
            out["error"] = str(exc)[:400]
            return out
        with db:
            db.execute(
                f"UPDATE outbox SET sent_at = ?, attempts = attempts + 1"
                f" WHERE id IN ({marks}) AND sent_at IS NULL",
                [_now(), *ids],
            )
        out["sent"] = len(ids)
        return out
    finally:
        db.close()
