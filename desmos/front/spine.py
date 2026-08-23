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

#: Reserved channels: structured machine facts, not chat.
SYS_PRESENCE = "sys.presence"
SYS_COLD = "sys.cold"
PRESENCE_BUCKET_MIN = 10


def url() -> str:
    return os.environ.get("DESMOS_SPINE_URL", DEFAULT_URL).strip()


def _dotenv(key: str, world: World | None = None) -> str:
    root = world.cwd if world is not None else None
    for base in filter(None, [root, os.getcwd()]):
        try:
            for line in open(os.path.join(str(base), ".env")):
                got, _, value = line.strip().partition("=")
                if got == key and value:
                    return value.strip()
        except OSError:
            continue
    return ""


def token(world: World | None = None) -> str:
    """The shared spine secret: env first, then the workspace .env file."""
    return os.environ.get("DESMOS_SPINE_TOKEN", "").strip() or _dotenv(
        "DESMOS_SPINE_TOKEN", world
    )


def enabled(world: World | None = None) -> bool:
    """Spine opt-in: DESMOS_SPINE in the env or the workspace .env file."""
    got = os.environ.get("DESMOS_SPINE", "").strip() or _dotenv("DESMOS_SPINE", world)
    return got not in {"", "0", "off"}


def _ensure(db) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS spine_cursors("
        " channel TEXT PRIMARY KEY, seq INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS spine_peers("
        " host TEXT NOT NULL, run_id TEXT NOT NULL,"
        " session_id TEXT NOT NULL DEFAULT '', pid INTEGER NOT NULL DEFAULT 0,"
        " cwd TEXT NOT NULL DEFAULT '', generation INTEGER NOT NULL DEFAULT 0,"
        " model TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL DEFAULT '',"
        " seen_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(host, run_id))"
    )


def _seat() -> str:
    return os.environ.get("DESMOS_SEAT", "") or _socket.gethostname() or "seat"


def ingest(world: World, events: list[dict[str, Any]]) -> int:
    """Insert events by their global channel sequence; skip our own outbox loop."""
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
                mark = str(ev.get("fingerprint", ""))
                ours = mark and db.execute(
                    "SELECT 1 FROM outbox WHERE fingerprint = ?", (mark,)
                ).fetchone()
                if not ours and channel == SYS_PRESENCE:
                    written += _ingest_presence(db, ev)
                elif not ours:
                    cur = db.execute(
                        "INSERT OR IGNORE INTO channel_messages(workspace_id,"
                        " session_id, channel, run_id, author, body, created_at,"
                        " spine_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            workspace, session, channel,
                            str(ev.get("seat", "") or "spine"),
                            str(ev.get("author", "") or "spine"),
                            str(ev.get("body", "")),
                            str(ev.get("ts", "")),
                            seq,
                        ),
                    )
                    written += max(cur.rowcount, 0)
                db.execute(
                    "INSERT INTO spine_cursors(channel, seq) VALUES (?, ?)"
                    " ON CONFLICT(channel) DO UPDATE SET"
                    " seq = MAX(spine_cursors.seq, excluded.seq)",
                    (channel, seq),
                )
    finally:
        db.close()
    return written


def _append_frame(row: dict[str, Any]) -> dict[str, Any] | None:
    """The wire frame for one outbox row; None for kinds that stay local."""
    payload = json.loads(row["payload_json"])
    kind = str(row["kind"])
    if kind == "channel_post":
        return {
            "op": "append",
            "channel": str(payload.get("channel") or "conflicts"),
            "fingerprint": row["fingerprint"],
            "author": str(payload.get("author") or "anon"),
            "seat": _seat(),
            "body": str(payload.get("body") or " "),
        }
    if kind in ("cold_session", "presence"):
        return {
            "op": "append",
            "channel": SYS_COLD if kind == "cold_session" else SYS_PRESENCE,
            "fingerprint": row["fingerprint"],
            "author": _seat(),
            "seat": _seat(),
            "body": json.dumps(payload, default=str) or " ",
        }
    return None


def _enqueue_presence(world: World) -> None:
    """Publish this machine's live sessions, deduped per time bucket.

    The payload fingerprints identically within one bucket while the session
    set is unchanged, so ``INSERT OR IGNORE`` rate-limits the heartbeat for
    free: at most one presence append per bucket per real change.
    """
    from datetime import datetime, timezone

    db = _open(state_file(world))
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return
        rows = db.execute(
            "SELECT run_id, session_id, pid, cwd, generation, model,"
            " started_at FROM active_runs WHERE workspace_id = ?"
            " ORDER BY run_id",
            (workspace,),
        ).fetchall()
    finally:
        db.close()
    if not rows:
        return
    now = datetime.now(timezone.utc)
    minute = (now.minute // PRESENCE_BUCKET_MIN) * PRESENCE_BUCKET_MIN
    payload = {
        "host": _seat(),
        "bucket": now.strftime("%Y-%m-%dT%H:") + f"{minute:02d}",
        "sessions": [dict(r) for r in rows],
    }
    outbox.enqueue(world, "presence", payload)


def _ingest_presence(db, ev: dict[str, Any]) -> int:
    """Replace one remote host's peer rows with its latest presence payload."""
    try:
        payload = json.loads(str(ev.get("body", "")))
    except ValueError:
        return 0
    host = str(payload.get("host", ""))
    if not host or host == _seat():
        return 0
    db.execute("DELETE FROM spine_peers WHERE host = ?", (host,))
    written = 0
    for s in payload.get("sessions", []):
        db.execute(
            "INSERT OR REPLACE INTO spine_peers(host, run_id, session_id,"
            " pid, cwd, generation, model, started_at, seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                host,
                str(s.get("run_id", "")),
                str(s.get("session_id", "")),
                int(s.get("pid") or 0),
                str(s.get("cwd", "")),
                int(s.get("generation") or 0),
                str(s.get("model", "")),
                str(s.get("started_at", "")),
                str(ev.get("ts", "")) or str(payload.get("bucket", "")),
            ),
        )
        written += 1
    return written


def _connect(world: World, timeout: float):
    from websockets.sync.client import connect

    headers = {"Authorization": f"Bearer {token(world)}"}
    try:
        return connect(
            url(), additional_headers=headers, open_timeout=timeout, close_timeout=5
        )
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status is None:
            status = getattr(exc, "status_code", None)
        if status == 401:
            raise RuntimeError(
                "spine authentication failed (401): check DESMOS_SPINE_TOKEN"
            ) from exc
        raise


def _recv_until(ws, want: str, timeout: float,
                events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    while True:
        frame = json.loads(ws.recv(timeout=timeout))
        if frame.get("op") == "event" and events is not None:
            events.append(frame)
            continue
        if frame.get("op") == want:
            return frame
        if frame.get("op") == "error":
            raise RuntimeError(f"spine: {frame.get('error')}")


def sync(world: World, timeout: float = RECV_TIMEOUT) -> dict[str, Any]:
    """One full exchange: sub, snapshot-ingest, drain the outbox, return counts."""
    report: dict[str, Any] = {"ingested": 0, "sent": 0, "failed": 0, "error": ""}
    with _connect(world, timeout) as ws:
        ws.send(json.dumps({"op": "sub", "channels": ["*"]}))
        events: list[dict[str, Any]] = []
        _recv_until(ws, "subbed", timeout, events)
        ws.send(json.dumps({"op": "snapshot"}))
        snap = _recv_until(ws, "snapshot", timeout, events)
        for entry in snap.get("channels", []):
            events.extend(entry.get("tail", []))

        _enqueue_presence(world)
        rows = []
        frames = []
        for r in outbox.pending(world, 500):
            frame = _append_frame(r)
            if frame is None:
                continue
            rows.append(r)
            frames.append(frame)
        for frame in frames:
            ws.send(json.dumps(frame))
        wanted = {r["fingerprint"] for r in rows}
        acks: dict[str, int] = {}
        while acks.keys() != wanted:
            ack = _recv_until(ws, "ack", timeout, events)
            mark = str(ack.get("fingerprint", ""))
            if mark not in wanted:
                raise RuntimeError("spine: ack for unknown fingerprint")
            seq = int(ack.get("seq", 0))
            if seq <= 0:
                raise RuntimeError("spine: ack has invalid seq")
            acks[mark] = seq
        if acks:
            db = _open(state_file(world))
            try:
                with db:
                    for mark, seq in sorted(acks.items()):
                        row = db.execute(
                            "SELECT kind, payload_json FROM outbox"
                            " WHERE fingerprint = ?",
                            (mark,),
                        ).fetchone()
                        if row is None:
                            raise RuntimeError("spine: ack outbox row disappeared")
                        if row["kind"] == "channel_post":
                            message_id = int(json.loads(row["payload_json"])["id"])
                            db.execute(
                                "UPDATE channel_messages SET spine_seq = ?"
                                " WHERE id = ? AND spine_seq IS NULL",
                                (seq, message_id),
                            )
                        db.execute(
                            "UPDATE outbox SET sent_at = ?, attempts = attempts + 1"
                            " WHERE fingerprint = ? AND sent_at IS NULL",
                            (outbox._now(), mark),
                        )
            finally:
                db.close()
        report["sent"] = len(acks)
        report["ingested"] = ingest(world, events)
    return report


def _local_contiguous_seq(world: World, channel: str) -> int:
    """Largest k for which every channel sequence from 1 through k exists."""
    db = _open(state_file(world))
    try:
        row = db.execute(
            "WITH ordered AS ("
            " SELECT spine_seq,"
            " ROW_NUMBER() OVER (ORDER BY spine_seq) AS expected"
            " FROM (SELECT DISTINCT spine_seq FROM channel_messages"
            " WHERE channel = ? AND spine_seq > 0)"
            ") SELECT COALESCE("
            " MIN(CASE WHEN spine_seq != expected THEN expected - 1 END),"
            " COALESCE(MAX(spine_seq), 0)) AS seq FROM ordered",
            (channel,),
        ).fetchone()
        return int(row["seq"])
    finally:
        db.close()


def bootstrap(world: World, timeout: float = RECV_TIMEOUT) -> dict[str, int]:
    """Rebuild every advertised channel from D1/hot-log replay pages."""
    ingested = 0
    with _connect(world, timeout) as ws:
        ws.send(json.dumps({"op": "snapshot"}))
        snap = _recv_until(ws, "snapshot", timeout)
        channels = [
            str(entry.get("channel", ""))
            for entry in snap.get("channels", [])
            if entry.get("channel")
        ]
        for channel in channels:
            since = _local_contiguous_seq(world, channel)
            while True:
                ws.send(json.dumps({
                    "op": "replay", "channel": channel,
                    "since": since, "limit": 500,
                }))
                page = _recv_until(ws, "replay", timeout)
                page_events = list(page.get("events", []))
                ingested += ingest(world, page_events)
                next_seq = page.get("next")
                if next_seq is None:
                    break
                since = int(next_seq)
    return {"channels": len(channels), "ingested": ingested}

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
