"""The cold store: what leaves the live database is copied, never deleted.

Pruning bounds the working file. Nothing is ever deleted, so a session may
only leave harness.sqlite3 after a verified copy lands here: rows counted in
the cold file must match the rows read from the live one, and a session whose
copy does not match is not pruned at all.
"""

from __future__ import annotations

import gzip
import hashlib
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


def _ensure_files(cold: sqlite3.Connection) -> None:
    cold.execute(
        "CREATE TABLE IF NOT EXISTS cold_files ("
        " name TEXT PRIMARY KEY,"
        " sha256 TEXT NOT NULL,"
        " bytes INTEGER NOT NULL DEFAULT 0,"
        " stowed_at TEXT NOT NULL)"
    )


def stow(path: Path, files: list[Path]) -> dict[str, Any]:
    """Compress dead files into the cold store, original removed last.

    A file leaves the working directory only after its gzip reads back with
    the same sha256, so the bytes are never trusted to have moved.
    """
    out: dict[str, Any] = {"stowed": [], "freed": 0, "kept": []}
    if not files:
        return out
    target = cold_path(path).parent / "quarantine"
    target.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cold_path(path), timeout=5.0)
    try:
        _ensure_files(conn)
        at = datetime.now(timezone.utc).isoformat()
        for src in files:
            if not src.is_file():
                continue
            raw = src.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            dest = target / (src.name + ".gz")
            with gzip.open(dest, "wb") as fh:
                fh.write(raw)
            with gzip.open(dest, "rb") as fh:
                back = fh.read()
            if hashlib.sha256(back).hexdigest() != digest:
                out["kept"].append(src.name)
                continue
            conn.execute(
                "INSERT OR REPLACE INTO cold_files(name, sha256, bytes,"
                " stowed_at) VALUES (?, ?, ?, ?)",
                (src.name, digest, len(raw), at),
            )
            conn.commit()
            src.unlink()
            out["stowed"].append(src.name)
            out["freed"] += len(raw)
    finally:
        conn.close()
    out["path"] = str(target)
    return out


def held(path: Path) -> set[str]:
    """Session ids the cold store already answers for."""
    return {row["session_id"] for row in archived(path)}


def contents(path: Path) -> list[str]:
    """Every archived message body, for callers that must not recover twice."""
    target = cold_path(path)
    if not target.is_file():
        return []
    conn = sqlite3.connect(target, timeout=5.0)
    try:
        return [str(r[0]) for r in conn.execute("SELECT content_json FROM messages")]
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def stowed(path: Path) -> list[dict[str, Any]]:
    """Dead files the cold store now holds, oldest first."""
    target = cold_path(path)
    if not target.is_file():
        return []
    conn = sqlite3.connect(target, timeout=5.0)
    try:
        rows = conn.execute(
            "SELECT name, sha256, bytes, stowed_at FROM cold_files"
            " ORDER BY stowed_at, name"
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()
    return [
        {
            "name": str(r[0]),
            "sha256": str(r[1]),
            "bytes": int(r[2]),
            "stowed_at": str(r[3]),
        }
        for r in rows
    ]
