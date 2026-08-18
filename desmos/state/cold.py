"""The cold store: what leaves the live database is copied, never deleted.

Pruning bounds the working file. Nothing is ever deleted, so a session may
only leave harness.sqlite3 after a verified copy lands here: rows counted in
the cold file must match the rows read from the live one, and a session whose
copy does not match is not pruned at all.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Presence and derived indexes are not history.
SKIP_TABLES = {"active_runs", "history_fts"}


def cold_path(path: Path) -> Path:
    """The archive beside the live database it drains."""
    return path.parent / "cold" / "history.sqlite3"


def _key(table: str) -> str:
    return "id" if table == "sessions" else "session_id"


def _cols(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    return [(str(r[1]), str(r[2])) for r in conn.execute(f"PRAGMA table_info({table})")]


def _session_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table a deleted session takes with it."""
    out: list[str] = []
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    for row in sorted(str(r[0]) for r in rows):
        if row in SKIP_TABLES or row.startswith("sqlite_"):
            continue
        names = {name for name, _ in _cols(conn, row)}
        if row == "sessions" or "session_id" in names:
            out.append(row)
    return out


def _ensure(conn: sqlite3.Connection, cold: sqlite3.Connection, table: str) -> None:
    """Mirror the live shape, widening an archive written by an older build."""
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if ddl and ddl[0]:
        cold.execute(str(ddl[0]))
    have = {name for name, _ in _cols(cold, table)}
    for name, decl in _cols(conn, table):
        if name not in have:
            cold.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl or 'TEXT'}")


def _ensure_manifest(cold: sqlite3.Connection) -> None:
    cold.execute(
        "CREATE TABLE IF NOT EXISTS cold_sessions ("
        " session_id TEXT PRIMARY KEY,"
        " archived_at TEXT NOT NULL,"
        " rows INTEGER NOT NULL DEFAULT 0)"
    )


def _copy_session(
    conn: sqlite3.Connection,
    cold: sqlite3.Connection,
    tables: list[str],
    sid: str,
) -> int | None:
    """Copy one session. None means the copy could not be proven complete."""
    total = 0
    for table in tables:
        key = _key(table)
        names = [name for name, _ in _cols(conn, table)]
        cols = ", ".join(names)
        marks = ", ".join("?" * len(names))
        src = conn.execute(
            f"SELECT {cols} FROM {table} WHERE {key} = ?", (sid,)
        ).fetchall()
        cold.executemany(
            f"INSERT OR REPLACE INTO {table}({cols}) VALUES ({marks})",
            [tuple(row) for row in src],
        )
        got = cold.execute(
            f"SELECT count(*) FROM {table} WHERE {key} = ?", (sid,)
        ).fetchone()[0]
        if int(got) < len(src):
            return None
        total += len(src)
    return total


def archive(
    path: Path, conn: sqlite3.Connection, doomed: list[str]
) -> dict[str, Any]:
    """Copy sessions out of the live database; return the ids proven safe."""
    out: dict[str, Any] = {"path": "", "archived": [], "rows": 0}
    if not doomed:
        return out
    target = cold_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    cold = sqlite3.connect(target, timeout=5.0)
    try:
        cold.execute("PRAGMA journal_mode = WAL")
        tables = _session_tables(conn)
        for table in tables:
            _ensure(conn, cold, table)
        _ensure_manifest(cold)
        at = datetime.now(timezone.utc).isoformat()
        for sid in doomed:
            moved = _copy_session(conn, cold, tables, sid)
            if moved is None:
                continue
            cold.execute(
                "INSERT OR REPLACE INTO cold_sessions(session_id, archived_at,"
                " rows) VALUES (?, ?, ?)",
                (sid, at, moved),
            )
            out["archived"].append(sid)
            out["rows"] += moved
        cold.commit()
    finally:
        cold.close()
    out["path"] = str(target)
    return out


def archived(path: Path) -> list[dict[str, Any]]:
    """What the cold store holds for this database, oldest first."""
    target = cold_path(path)
    if not target.is_file():
        return []
    cold = sqlite3.connect(target, timeout=5.0)
    try:
        rows = cold.execute(
            "SELECT session_id, archived_at, rows FROM cold_sessions"
            " ORDER BY archived_at, session_id"
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        cold.close()
    return [
        {"session_id": str(r[0]), "archived_at": str(r[1]), "rows": int(r[2])}
        for r in rows
    ]
