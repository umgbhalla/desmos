from __future__ import annotations

import json
import os
import sqlite3
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from desmos.const import FROZEN, PRIOR_KEEP
from desmos.exec import callable_from_source
from desmos.types import Tool, World

SCHEMA_VERSION = 1
SESSION_ID = "default"
DB_FILENAME = "harness.sqlite3"
LEGACY_FILENAME = "harness.json"


def state_file(world: World) -> Path:
    if world.state_path:
        return world.state_path
    return world.cwd / ".desmos" / DB_FILENAME


def _legacy_file(world: World) -> Path:
    if world.state_path:
        return world.state_path
    return world.cwd / ".desmos" / LEGACY_FILENAME


def _backup_name(path: Path, label: str) -> Path:
    candidate = path.with_name(path.name + f".{label}")
    n = 1
    while candidate.exists():
        candidate = path.with_name(path.name + f".{label}.{n}")
        n += 1
    return candidate


def _move_sqlite_files(path: Path, label: str) -> list[Path]:
    moved: list[Path] = []
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(path) + suffix)
        if not source.exists():
            continue
        target = _backup_name(source, label)
        os.replace(source, target)
        moved.append(target)
    return moved


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            generation INTEGER NOT NULL,
            gen_reason TEXT NOT NULL,
            thinking TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content_json TEXT NOT NULL,
            PRIMARY KEY (session_id, seq)
        );
        CREATE TABLE IF NOT EXISTS prior_turns (
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            prompt TEXT NOT NULL,
            speech TEXT NOT NULL,
            PRIMARY KEY (session_id, seq)
        );
        CREATE TABLE IF NOT EXISTS notes (
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            body TEXT NOT NULL,
            PRIMARY KEY (session_id, name)
        );
        CREATE TABLE IF NOT EXISTS tools (
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            doc TEXT NOT NULL,
            source TEXT,
            frozen INTEGER NOT NULL CHECK (frozen IN (0, 1)),
            PRIMARY KEY (session_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session_seq
            ON messages(session_id, seq);
        """
    )
    versions = [int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")]
    if versions and max(versions) > SCHEMA_VERSION:
        raise RuntimeError(
            f"harness database schema {max(versions)} is newer than supported {SCHEMA_VERSION}"
        )
    if SCHEMA_VERSION not in versions:
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path)
        _migrate(conn)
        return conn
    except sqlite3.DatabaseError as exc:
        if conn is not None:
            conn.close()
        moved = _move_sqlite_files(path, "corrupt")
        warnings.warn(
            f"backed up corrupt harness database ({exc}); fresh state will be created"
            + (f" at {moved[0]}" if moved else ""),
            RuntimeWarning,
            stacklevel=2,
        )
        conn = _connect(path)
        _migrate(conn)
        return conn


def _legacy_payload(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if raw.startswith(b"SQLite format 3"):
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _data_from_world(world: World) -> dict[str, Any]:
    return {
        "notes": world.notes,
        "tools": {
            name: {"doc": tool.doc, "source": tool.source}
            for name, tool in world.tools.items()
            if not tool.frozen
        },
        "docs": {name: tool.doc for name, tool in world.tools.items() if tool.frozen},
        "prior": world.prior[-PRIOR_KEEP:],
        "generation": world.generation,
        "gen_reason": world.gen_reason,
        "thinking": world.thinking,
        "messages": world.messages[-80:],
    }


def _save_data(conn: sqlite3.Connection, world: World, data: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        conn.execute(
            """
            INSERT INTO sessions(id, cwd, generation, gen_reason, thinking, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                cwd=excluded.cwd,
                generation=excluded.generation,
                gen_reason=excluded.gen_reason,
                thinking=excluded.thinking,
                updated_at=excluded.updated_at
            """,
            (
                SESSION_ID,
                str(world.cwd),
                int(data["generation"]),
                str(data["gen_reason"]),
                str(data["thinking"]),
                now,
            ),
        )
        for table in ("messages", "prior_turns", "notes", "tools"):
            conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (SESSION_ID,))
        for seq, item in enumerate(data["messages"]):
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            content = item.get("content")
            if not isinstance(content, (str, list)):
                continue
            conn.execute(
                "INSERT INTO messages(session_id, seq, role, content_json) VALUES (?, ?, ?, ?)",
                (SESSION_ID, seq, item["role"], json.dumps(content, separators=(",", ":"))),
            )
        for seq, item in enumerate(data["prior"]):
            if isinstance(item, dict) and isinstance(item.get("prompt"), str) and isinstance(item.get("speech"), str):
                conn.execute(
                    "INSERT INTO prior_turns(session_id, seq, prompt, speech) VALUES (?, ?, ?, ?)",
                    (SESSION_ID, seq, item["prompt"], item["speech"]),
                )
        for name, body in data["notes"].items():
            if isinstance(body, str):
                conn.execute(
                    "INSERT INTO notes(session_id, name, body) VALUES (?, ?, ?)",
                    (SESSION_ID, str(name), body),
                )
        docs = data["docs"]
        for name, doc in docs.items():
            if isinstance(doc, str):
                conn.execute(
                    "INSERT INTO tools(session_id, name, doc, source, frozen) VALUES (?, ?, ?, NULL, 1)",
                    (SESSION_ID, str(name), doc),
                )
        for name, spec in data["tools"].items():
            if not isinstance(spec, dict):
                continue
            doc, source = spec.get("doc"), spec.get("source")
            if isinstance(doc, str) and (isinstance(source, str) or source is None):
                conn.execute(
                    "INSERT INTO tools(session_id, name, doc, source, frozen) VALUES (?, ?, ?, ?, 0)",
                    (SESSION_ID, str(name), doc, source),
                )


def save(world: World) -> None:
    if not world.persist:
        return
    conn = _open(state_file(world))
    try:
        _save_data(conn, world, _data_from_world(world))
    finally:
        conn.close()


def _apply_data(world: World, data: dict[str, Any]) -> None:
    notes = data.get("notes")
    if isinstance(notes, dict):
        world.notes = {str(k): str(v) for k, v in notes.items() if isinstance(v, str)}
    docs = data.get("docs")
    if isinstance(docs, dict):
        for name, doc in docs.items():
            if name in world.tools and isinstance(doc, str) and doc.strip():
                world.tools[name].doc = doc
    tools = data.get("tools")
    if isinstance(tools, dict):
        for name, spec in tools.items():
            if name in FROZEN or not isinstance(spec, dict):
                continue
            source = spec.get("source")
            doc = spec.get("doc") or f"user tag <{name}>"
            if not isinstance(source, str) or not isinstance(doc, str):
                continue
            try:
                fn = callable_from_source(world, source, name)
            except Exception:
                continue
            world.tools[name] = Tool(name=name, doc=doc, source=source, handler=fn)
    raw_prior = data.get("prior")
    if isinstance(raw_prior, list):
        world.prior = [
            {"prompt": item["prompt"], "speech": item["speech"]}
            for item in raw_prior[-PRIOR_KEEP:]
            if isinstance(item, dict)
            and isinstance(item.get("prompt"), str)
            and isinstance(item.get("speech"), str)
        ]
    if isinstance(data.get("generation"), int) and data["generation"] > 0:
        world.generation = data["generation"]
    if isinstance(data.get("gen_reason"), str) and data["gen_reason"]:
        world.gen_reason = data["gen_reason"]
    if isinstance(data.get("thinking"), str) and data["thinking"].strip():
        world.thinking = data["thinking"].strip()
    raw_msgs = data.get("messages")
    if isinstance(raw_msgs, list):
        world.messages = [
            {"role": item["role"], "content": item["content"]}
            for item in raw_msgs[-80:]
            if isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), (str, list))
        ]


def _read_data(conn: sqlite3.Connection) -> dict[str, Any] | None:
    session = conn.execute(
        "SELECT generation, gen_reason, thinking FROM sessions WHERE id = ?",
        (SESSION_ID,),
    ).fetchone()
    if session is None:
        return None
    messages = [
        {"role": row["role"], "content": json.loads(row["content_json"])}
        for row in conn.execute(
            "SELECT role, content_json FROM messages WHERE session_id = ? ORDER BY seq",
            (SESSION_ID,),
        )
    ]
    prior = [
        {"prompt": row["prompt"], "speech": row["speech"]}
        for row in conn.execute(
            "SELECT prompt, speech FROM prior_turns WHERE session_id = ? ORDER BY seq",
            (SESSION_ID,),
        )
    ]
    notes = {
        row["name"]: row["body"]
        for row in conn.execute("SELECT name, body FROM notes WHERE session_id = ?", (SESSION_ID,))
    }
    docs: dict[str, str] = {}
    tools: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT name, doc, source, frozen FROM tools WHERE session_id = ?",
        (SESSION_ID,),
    ):
        if row["frozen"]:
            docs[row["name"]] = row["doc"]
        else:
            tools[row["name"]] = {"doc": row["doc"], "source": row["source"]}
    return {
        "generation": session["generation"],
        "gen_reason": session["gen_reason"],
        "thinking": session["thinking"],
        "messages": messages,
        "prior": prior,
        "notes": notes,
        "docs": docs,
        "tools": tools,
    }


def load(world: World) -> None:
    if not world.persist:
        return
    db_path = state_file(world)
    legacy_path = _legacy_file(world)
    legacy = _legacy_payload(legacy_path) if not db_path.exists() or legacy_path == db_path else None

    if legacy is not None and legacy_path == db_path:
        backup = _backup_name(legacy_path, "migrated")
        os.replace(legacy_path, backup)

    conn = _open(db_path)
    try:
        if legacy is not None:
            _apply_data(world, legacy)
            _save_data(conn, world, _data_from_world(world))
            if legacy_path != db_path and legacy_path.exists():
                os.replace(legacy_path, _backup_name(legacy_path, "migrated"))
            return
        data = _read_data(conn)
        if data is not None:
            _apply_data(world, data)
    finally:
        conn.close()
