"""Recover conversation out of quarantined databases.

`persist` replaces a database it cannot open and leaves the original beside it.
That is the right call at wake -- refusing to start would be worse -- but the
replacement starts empty, so every session before the fault stops being
reachable while its bytes sit on disk, unread and unaccounted for. This
workspace accumulated ninety such files holding twenty-two sessions and several
hundred messages that exist nowhere else.

Salvage is read-then-insert and never deletes. It is dry by default: `survey`
says what is recoverable, `salvage(world)` reports what it would write, and only
`salvage(world, apply=True)` writes. Reclaiming the disk is a separate act that
must follow a verified salvage, never accompany it.

The acceptance test is not row counts. It is that a recovered conversation
answers a `recall` for something only it said.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from desmos.kernel.types import World
from desmos.state import persist


def dead_databases(path: Path) -> list[Path]:
    """The quarantined main database files beside `path`, oldest first."""
    out = [
        candidate
        for candidate in path.parent.glob(path.name + ".corrupt*")
        if candidate.is_file()
        and not candidate.name.endswith(("-shm", "-wal", "-journal"))
    ]
    return sorted(out, key=lambda p: (p.stat().st_mtime, p.name))


def _fingerprint(content_json: str) -> str:
    """Identity of a message, independent of per-request provider ciphertext.

    The same transcript is re-persisted into each fresh database, so raw JSON
    comparison reports thousands of unique messages where there are hundreds.
    """
    try:
        value: Any = persist._strip_opaque(json.loads(content_json))
    except ValueError:
        value = content_json
    encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def _read_sessions(db: Path) -> list[dict[str, Any]] | None:
    """Every session in one quarantined file. None when it cannot be read.

    A quarantined file keeps its write-ahead log, which was moved alongside it
    and still holds the last turns written before the fault. Read-only replays
    that log; immutable ignores it, which is the only way to read a file whose
    companions are gone or unreplayable. Try the faithful reading first.

    A connection can open and only fail on the first query, so each attempt has
    to run the read, not just the connect.
    """
    for uri in (f"file:{db}?mode=ro", f"file:{db}?mode=ro&immutable=1"):
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            return _extract(conn, db)
        except sqlite3.Error:
            continue
        finally:
            if conn is not None:
                conn.close()
    return None


def _extract(conn: sqlite3.Connection, db: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if True:
        rows = conn.execute(
            "SELECT id, started_at, last_seen_at, model FROM sessions"
        ).fetchall()
        for sid, started, seen, model in rows:
            session = str(sid)
            messages = [
                (int(seq), str(role), str(content))
                for seq, role, content in conn.execute(
                    "SELECT seq, role, content_json FROM messages"
                    " WHERE session_id = ? ORDER BY seq",
                    (session,),
                )
                if role in ("user", "assistant")
            ]
            prior = [
                (int(seq), str(prompt), str(speech))
                for seq, prompt, speech in conn.execute(
                    "SELECT seq, prompt, speech FROM prior_turns"
                    " WHERE session_id = ? ORDER BY seq",
                    (session,),
                )
            ]
            out.append(
                {
                    "id": session,
                    "started_at": str(started or ""),
                    "last_seen_at": str(seen or started or ""),
                    "model": str(model or ""),
                    "source": db.name,
                    "messages": messages,
                    "prior": prior,
                }
            )
    return out


def _gather(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Best surviving copy of each session, oldest first.

    A session appears in every snapshot taken after it started, each time with
    more of it written. The longest copy is the one to keep.
    """
    best: dict[str, dict[str, Any]] = {}
    unreadable: list[str] = []
    for db in dead_databases(path):
        found = _read_sessions(db)
        if found is None:
            unreadable.append(db.name)
            continue
        for record in found:
            kept = best.get(record["id"])
            if kept is None or len(record["messages"]) > len(kept["messages"]):
                best[record["id"]] = record
    ordered = sorted(best.values(), key=lambda r: (r["started_at"], r["id"]))
    return ordered, unreadable


def _candidates(
    path: Path, conn: sqlite3.Connection
) -> tuple[list[dict[str, Any]], list[str]]:
    """Sessions worth recovering: not already present, and not all duplicate."""
    ordered, unreadable = _gather(path)
    live_ids = {str(row[0]) for row in conn.execute("SELECT id FROM sessions")}
    seen = {
        _fingerprint(str(row[0]))
        for row in conn.execute("SELECT content_json FROM messages")
    }
    out: list[dict[str, Any]] = []
    for record in ordered:
        if not record["messages"]:
            continue
        prints = [_fingerprint(content) for _, _, content in record["messages"]]
        novel = sum(1 for mark in prints if mark not in seen)
        # Count against every earlier candidate too, so a later snapshot of the
        # same conversation is not recovered twice.
        seen.update(prints)
        if novel and record["id"] not in live_ids:
            record["novel"] = novel
            record["bytes"] = sum(len(c) for _, _, c in record["messages"])
            out.append(record)
    return out, unreadable


def survey(path: Path) -> dict[str, Any]:
    """What the quarantined files hold that the live database does not."""
    conn = persist._open(path)
    try:
        candidates, unreadable = _candidates(path, conn)
        live_sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
        live_messages = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    return {
        "files": len(dead_databases(path)),
        "unreadable": unreadable,
        "live_sessions": int(live_sessions),
        "live_messages": int(live_messages),
        "sessions": len(candidates),
        "messages": sum(int(c["novel"]) for c in candidates),
        "bytes": sum(int(c["bytes"]) for c in candidates),
        "detail": [
            {
                "session_id": c["id"],
                "started_at": c["started_at"],
                "model": c["model"],
                "source": c["source"],
                "messages": len(c["messages"]),
                "novel": int(c["novel"]),
            }
            for c in candidates
        ],
    }


def _insert(
    conn: sqlite3.Connection, workspace: str, record: dict[str, Any]
) -> None:
    session = record["id"]
    conn.execute(
        "INSERT INTO sessions(id, workspace_id, parent_id, kind, started_at,"
        " last_seen_at, model, thinking, cache_key, title)"
        " VALUES (?, ?, NULL, 'attach', ?, ?, ?, '', ?, ?)",
        (
            session,
            workspace,
            record["started_at"],
            record["last_seen_at"],
            record["model"],
            f"recovered-{session[:16]}",
            f"recovered from {record['source']}",
        ),
    )
    for seq, role, content in record["messages"]:
        conn.execute(
            "INSERT INTO messages(session_id, seq, role, content_json)"
            " VALUES (?, ?, ?, ?)",
            (session, seq, role, content),
        )
        try:
            text = persist._content_text(json.loads(content))
        except ValueError:
            text = ""
        if text.strip():
            conn.execute(
                "INSERT INTO history_fts("
                " workspace_id, session_id, kind, text, source_seq)"
                " VALUES (?, ?, ?, ?, ?)",
                (workspace, session, f"message:{role}", text, str(seq)),
            )
    for seq, prompt, speech in record["prior"]:
        conn.execute(
            "INSERT INTO prior_turns(session_id, seq, prompt, speech)"
            " VALUES (?, ?, ?, ?)",
            (session, seq, prompt, speech),
        )
        conn.execute(
            "INSERT INTO history_fts("
            " workspace_id, session_id, kind, text, source_seq)"
            " VALUES (?, ?, 'prior', ?, ?)",
            (workspace, session, prompt + "\n" + speech, str(seq)),
        )


def salvage(world: World, *, apply: bool = False) -> dict[str, Any]:
    """Return recovered sessions to the live record. Dry unless asked."""
    path = persist.state_file(world)
    conn = persist._open(path)
    try:
        candidates, unreadable = _candidates(path, conn)
        report: dict[str, Any] = {
            "applied": False,
            "unreadable": unreadable,
            "sessions": len(candidates),
            "messages": sum(int(c["novel"]) for c in candidates),
            "bytes": sum(int(c["bytes"]) for c in candidates),
            "recovered": [c["id"] for c in candidates],
        }
        if not apply or not candidates:
            return report
        conn.execute("BEGIN IMMEDIATE")
        try:
            workspace = persist._workspace_id(conn, world)
            assert workspace is not None
            for record in candidates:
                _insert(conn, workspace, record)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        report["applied"] = True
        return report
    finally:
        conn.close()
