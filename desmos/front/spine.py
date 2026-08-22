"""Spine client: drain the transactional outbox to the Durable Object, ingest back.

The write path is local (channel_post commits the message and its outbox row in
one transaction); this module is the asynchronous copy to the cloud sequencer
and the ingest of every other machine's appends. Offline it does nothing and
loses nothing -- the outbox holds. Idempotency is the outbox fingerprint: the
DO deduplicates appends on it, and ingest skips events whose fingerprint is in
our own outbox (they started here).

Per-channel total order comes from the DO's seq; ``spine_cursors`` records the
highest seq ingested per channel so a reconnect resumes instead of replaying.
"""

from __future__ import annotations

import json
import os
import socket as _socket
from typing import Any

from desmos.kernel.types import World
from desmos.state import outbox
from desmos.state.persist import _open, _session_id, _workspace_id, state_file

DEFAULT_URL = "wss://desmos-spine.umg-bhalla88.workers.dev/ws"
RECV_TIMEOUT = 20.0


def url() -> str:
    return os.environ.get("DESMOS_SPINE_URL", DEFAULT_URL).strip()


def token(world: World | None = None) -> str:
    """The shared spine secret: env first, then the workspace .env file."""
    got = os.environ.get("DESMOS_SPINE_TOKEN", "").strip()
    if got:
        return got
    root = world.cwd if world is not None else None
    for base in filter(None, [root, os.getcwd()]):
        try:
            for line in open(os.path.join(str(base), ".env")):
                key, _, value = line.strip().partition("=")
                if key == "DESMOS_SPINE_TOKEN" and value:
                    return value.strip()
        except OSError:
            continue
    return ""


def _ensure(db) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS spine_cursors("
        " channel TEXT PRIMARY KEY, seq INTEGER NOT NULL)"
    )


def _seat() -> str:
    return os.environ.get("DESMOS_SEAT", "") or _socket.gethostname() or "seat"


def ingest(world: World, events: list[dict[str, Any]]) -> int:
    """Insert remote events beyond each channel's cursor; skip our own."""
    if not events:
        return 0
    db = _open(state_file(world))
    written = 0
    try:
        with db:
            _ensure(db)
            workspace = _workspace_id(db, world)
            session = _session_id(db, world, workspace)
            for ev in events:
                channel = str(ev.get("channel", ""))
                seq = int(ev.get("seq", 0))
                if not channel or seq <= 0:
                    continue
                row = db.execute(
                    "SELECT seq FROM spine_cursors WHERE channel = ?",
                    (channel,),
                ).fetchone()
                cursor = int(row["seq"]) if row else 0
                if seq <= cursor:
                    continue
                mark = str(ev.get("fingerprint", ""))
                ours = mark and db.execute(
                    "SELECT 1 FROM outbox WHERE fingerprint = ?", (mark,)
                ).fetchone()
                if not ours:
                    db.execute(
                        "INSERT INTO channel_messages(workspace_id, session_id,"
                        " channel, run_id, author, body, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            workspace, session, channel,
                            str(ev.get("seat", "") or "spine"),
                            str(ev.get("author", "") or "spine"),
                            str(ev.get("body", "")),
                            str(ev.get("ts", "")),
                        ),
                    )
                    written += 1
                db.execute(
                    "INSERT INTO spine_cursors(channel, seq) VALUES (?, ?)"
                    " ON CONFLICT(channel) DO UPDATE SET seq = excluded.seq",
                    (channel, seq),
                )
    finally:
        db.close()
    return written


def _append_frame(row: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    return {
        "op": "append",
        "channel": str(payload.get("channel") or "conflicts"),
        "fingerprint": row["fingerprint"],
        "author": str(payload.get("author") or "anon"),
        "seat": _seat(),
        "body": str(payload.get("body") or " "),
    }


def sync(world: World, timeout: float = RECV_TIMEOUT) -> dict[str, Any]:
    """One full exchange: sub, snapshot-ingest, drain the outbox, return counts."""
    from websockets.sync.client import connect

    report: dict[str, Any] = {"ingested": 0, "sent": 0, "failed": 0, "error": ""}
    headers = {"Authorization": f"Bearer {token(world)}"}
    with connect(
        url(), additional_headers=headers, open_timeout=timeout, close_timeout=5
    ) as ws:
        ws.send(json.dumps({"op": "sub", "channels": ["*"]}))
        events: list[dict[str, Any]] = []

        def recv_until(want: str) -> dict[str, Any]:
            while True:
                frame = json.loads(ws.recv(timeout=timeout))
                if frame.get("op") == "event":
                    events.append(frame)
                    continue
                if frame.get("op") == want:
                    return frame
                if frame.get("op") == "error":
                    raise RuntimeError(f"spine: {frame.get('error')}")

        recv_until("subbed")
        ws.send(json.dumps({"op": "snapshot"}))
        snap = recv_until("snapshot")
        for entry in snap.get("channels", []):
            events.extend(entry.get("tail", []))

        rows = [
            r for r in outbox.pending(world, 500) if r["kind"] == "channel_post"
        ]
        for row in rows:
            ws.send(json.dumps(_append_frame(row)))
        wanted = {r["fingerprint"] for r in rows}
        acked: set[str] = set()
        while acked != wanted:
            ack = recv_until("ack")
            mark = str(ack.get("fingerprint", ""))
            if mark not in wanted:
                raise RuntimeError("spine: ack for unknown fingerprint")
            acked.add(mark)
        if acked:
            db = _open(state_file(world))
            try:
                with db:
                    db.executemany(
                        "UPDATE outbox SET sent_at = ?, attempts = attempts + 1"
                        " WHERE fingerprint = ? AND sent_at IS NULL",
                        [(outbox._now(), mark) for mark in sorted(acked)],
                    )
            finally:
                db.close()
        report["sent"] = len(acked)
        report["ingested"] = ingest(world, events)
    return report


def run_forever(world: World, interval: float = 5.0) -> None:
    """Drain and ingest until the process dies; offline is a wait, not an error."""
    import time

    delay = interval
    while True:
        try:
            report = sync(world)
            delay = interval
            if report["sent"] or report["ingested"]:
                continue  # something moved; look again immediately
        except Exception:
            delay = min(delay * 2, 300.0)
        time.sleep(delay)
