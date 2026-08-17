from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import tempfile
import time
import traceback
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from desmos.kernel import prices
from desmos.kernel.const import FROZEN, PRIOR_KEEP
from desmos.kernel.exec import callable_from_source
from desmos.kernel.types import Tool, World

# There is no getumask, so it is read by setting it -- which is why it happens
# once here, at import, while the process is still single-threaded. Doing it
# inside the write left the whole process at umask 0 for two syscalls, and any
# file another thread or a forked <shell> created in that window landed
# world-writable.
_UMASK = os.umask(0)
os.umask(_UMASK)

SCHEMA_VERSION = 9
#: One attach, one id, across SQL, provider routing, cache, presence, and wire.
#: The environment survives reload_sdk; a new process gets a new session.
SESSION_ID_ENV = "DESMOS_SESSION_ID"
NEW_SESSION_ENV = "DESMOS_SESSION_NEW"
RUN_ID_ENV = SESSION_ID_ENV  # compatibility name for the public run_id() API
DB_FILENAME = "harness.sqlite3"
KEEP_MESSAGES = 80
SESSION_KEEP = 24
_PRESENCE_LEASES: dict[str, Any] = {}


def atomic_write(path: Path, text: str) -> None:
    """Unique temp in the destination directory, then replace.

    A temp named after the destination (`records.jsonl.tmp`) is one file for
    every desmos process in the repo: two writers interleave into it and both
    replace, so the survivor is a splice of two states.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        # mkstemp is 0600, which narrows every file that goes through here:
        # a rewrite would drop the mode someone already set, and a first write
        # would land private where the plain open() this replaced left umask
        # (MEMORY.md is meant to be read by humans, including a second account
        # sharing the checkout).
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o666 & ~_UMASK)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _has_compaction(item: dict[str, Any]) -> bool:
    content = item.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and (
            block.get("type") == "compaction"
            or (
                isinstance(block.get("openai"), dict)
                and block["openai"].get("type") == "compaction"
            )
        )
        for block in content
    )


def turn_aligned(
    messages: list[dict[str, Any]], keep: int = KEEP_MESSAGES
) -> list[dict[str, Any]]:
    """Keep the last `keep`, then widen backwards until the head is a user turn.

    Anthropic rejects a payload whose first message is an assistant turn, so a
    flat count is not safe to persist. Widening is the only direction allowed:
    a version of this that searched *forward* for a boundary cut a 124-message
    transcript to one message, because the pair it looked for -- two user
    messages in a row -- only occurs where `_run_turns` appends a stop note or
    a max_turns note after a result, which is the tail of a step, not its head.
    A step's real head, the user message carrying `header(world, prompt)`, is
    not distinguishable from a `<result>` by role or content shape, so this
    lands on whichever user message is nearest, and never drops below `keep`.
    """
    if not messages:
        return []
    start = max(0, len(messages) - keep)
    # A provider compaction item is the only representation of everything it
    # folded. If it ages out while post-fold messages survive, resume has a
    # plausible-looking tail with its historical context silently missing.
    # Preserve the newest checkpoint and every message after it, then widen to
    # a role-safe user boundary for Anthropic.
    checkpoint = next(
        (i for i in range(len(messages) - 1, -1, -1) if _has_compaction(messages[i])),
        None,
    )
    if checkpoint is not None and checkpoint < start:
        start = checkpoint
    while start > 0 and messages[start].get("role") != "user":
        start -= 1
    if messages[start].get("role") != "user":
        # The whole transcript opens on an assistant turn: nothing to widen
        # into, so skip forward to the first user message instead.
        for i, item in enumerate(messages):
            if item.get("role") == "user":
                return messages[i:]
        return []
    return messages[start:]


#: What the model reads in place of the output a killed process never
#: produced. Same register as transport's UNANSWERED_CALL, but written into
#: the transcript itself at load, so the interruption is durable and the
#: wire never has to invent an answer per POST.
INTERRUPTED_CALL = (
    "[interrupted — the process ended before this syscall returned a result; "
    "nothing is known about whether it ran]"
)


def _call_ids(content: Any) -> list[tuple[str, str]]:
    """(wire type, id) of each syscall call block in assistant content."""
    out: list[tuple[str, str]] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id"):
                out.append(("tool_use", str(block["id"])))
            elif block.get("type") == "custom_tool_call" and block.get("call_id"):
                out.append(("custom_tool_call", str(block["call_id"])))
    return out


def _answered_ids(content: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result" and block.get("tool_use_id"):
                ids.add(str(block["tool_use_id"]))
            elif block.get("type") == "custom_tool_call_output" and block.get("call_id"):
                ids.add(str(block["call_id"]))
    return ids


def repair_orphan_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Synthesize the failed result a killed process never appended.

    A transcript saved mid-turn carries an assistant call with no output --
    deliberate: turn() makes the assistant message durable before dispatch
    runs. Replayed as-is, the typed shape is a hard 400 (a tool_use nothing
    answered), so every dangling call gets an interrupted result paired here,
    in the load path -- never rebuilt from events, and never rewritten later:
    the repaired transcript is the byte-stable prefix from now on.

    The prose dialect cannot 400, so only its cheap case is repaired: a
    transcript that ENDS on an assistant message whose speech scans to
    syscalls got no <result> back. Load runs before anything is appended, so
    "last message" is exactly "dispatch never answered". The synthesized
    block is user-role, the only place a result block may ever appear.
    """
    from desmos.kernel.scan import scan

    repaired: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        repaired.append(msg)
        if msg.get("role") != "assistant":
            continue
        calls = _call_ids(msg.get("content"))
        if calls:
            nxt = messages[i + 1] if i + 1 < len(messages) else {}
            answered = _answered_ids(nxt.get("content")) if nxt.get("role") == "user" else set()
            blocks: list[dict[str, Any]] = []
            for kind, call_id in calls:
                if call_id in answered:
                    continue
                if kind == "tool_use":
                    blocks.append(
                        {"type": "tool_result", "tool_use_id": call_id, "content": INTERRUPTED_CALL}
                    )
                else:
                    blocks.append(
                        {"type": "custom_tool_call_output", "call_id": call_id, "output": INTERRUPTED_CALL}
                    )
            if blocks:
                repaired.append({"role": "user", "content": blocks})
        elif i == len(messages) - 1:
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    block.get("text") or ""
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                text = ""
            tags = scan(text) if text else []
            if tags:
                repaired.append(
                    {
                        "role": "user",
                        "content": "\n\n".join(
                            f'<result tag="{b.tag}">{INTERRUPTED_CALL}</result>' for b in tags
                        ),
                    }
                )
    return repaired


def _broken_handler(name: str, detail: str) -> Callable[..., str]:
    def handler(*_a: Any, **_k: Any) -> str:
        return f"<{name}> failed to load from stored source:\n{detail}"

    return handler


def load_grown(world: World, name: str, doc: str, source: str) -> Tool:
    """Compile a stored tool, or keep it as a handler that reports the failure.

    This used to `continue` on a bad compile, and the next save() rebuilds the
    tools table from world.tools -- so a grown tool whose import went stale was
    deleted, source and all, by the very reload that noticed. Keep the row and
    fail at the call, the way register_tag already returns the traceback.
    """
    try:
        fn: Callable[..., Any] = callable_from_source(world, source, name)
    except Exception:
        detail = traceback.format_exc()
        warnings.warn(
            f"grown tool <{name}> failed to load: {detail.strip().splitlines()[-1]}",
            RuntimeWarning,
            stacklevel=2,
        )
        fn = _broken_handler(name, detail)
    return Tool(name=name, doc=doc, source=source, handler=fn)


def state_file(world: World) -> Path:
    if world.state_path:
        return world.state_path
    return world.cwd / ".desmos" / DB_FILENAME


def open_db(path: Path) -> sqlite3.Connection:
    """A migrated connection for callers outside this module.

    `_open` is the only correct way in: it creates the parent, runs the
    migration, and recovers a corrupt file instead of raising. Anything that
    reaches for `sqlite3.connect` directly gets an unmigrated database.
    """
    return _open(path)


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


#: One line per quarantine, beside the database it describes. A replaced
#: database cannot account for its own replacement -- that is the entire
#: failure -- so the account lives outside it. This workspace quarantined 98
#: databases inside one 32-minute window and nothing recorded that it had.
QUARANTINE_LOG = "quarantine.jsonl"

#: Reported once per process per database. A second `load()` in the same run
#: is not new information.
_QUARANTINE_REPORTED: set[str] = set()


def quarantine_log_path(path: Path) -> Path:
    return path.parent / QUARANTINE_LOG


def _inventory(dead: Path) -> dict[str, Any]:
    """Best-effort census of a database that just failed to open."""
    try:
        conn = sqlite3.connect(f"file:{dead}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return {"readable": False, "error": str(exc)[:200]}
    out: dict[str, Any] = {}
    try:
        for table in ("sessions", "messages", "events", "calls"):
            try:
                out[table] = int(
                    conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                )
            except sqlite3.DatabaseError:
                out[table] = None
        try:
            row = conn.execute(
                "SELECT min(started_at), max(last_seen_at) FROM sessions"
            ).fetchone()
            out["earliest"], out["latest"] = (row[0], row[1]) if row else (None, None)
        except sqlite3.DatabaseError:
            out["earliest"] = out["latest"] = None
    finally:
        conn.close()
    out["readable"] = any(isinstance(v, int) for v in out.values())
    return out


def _record_quarantine(
    path: Path, exc: BaseException, moved: list[Path]
) -> dict[str, Any]:
    """Append the account of one replaced database. Never raises."""
    entry: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "reason": f"{type(exc).__name__}: {exc}"[:300],
        "moved": [str(p) for p in moved],
        "bytes": 0,
        "inventory": None,
    }
    try:
        entry["bytes"] = sum(p.stat().st_size for p in moved if p.exists())
        if moved:
            entry["inventory"] = _inventory(moved[0])
        log = quarantine_log_path(path)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        # An unwritable manifest must not turn a recovered database into a
        # crash. The caller still warns.
        pass
    return entry


def quarantines(path: Path) -> list[dict[str, Any]]:
    """Every recorded quarantine for this database, oldest first."""
    log = quarantine_log_path(path)
    if not log.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def quarantine_summary(path: Path) -> str:
    """What wake says. Empty when nothing was ever replaced."""
    entries = quarantines(path)
    if not entries:
        return ""
    last = entries[-1]
    inv = last.get("inventory")
    held = ""
    if isinstance(inv, dict) and inv.get("readable"):
        held = (
            f", holding {inv.get('sessions')} sessions"
            f" / {inv.get('messages')} messages"
            f" spanning {inv.get('earliest')} to {inv.get('latest')}"
        )
    total = sum(int(e.get("bytes") or 0) for e in entries)
    return (
        f"history quarantined, not absent: {len(entries)} database(s) replaced,"
        f" {total // 1024} KiB still on disk; most recent {last.get('at')}"
        f" -- {last.get('reason')}{held}"
    )


def _report_quarantines(world: World, path: Path) -> None:
    """Say it on the route ordinary notices take, once per process.

    A RuntimeWarning is not loud. Ninety-eight of them were raised in this
    workspace and no one saw one. A notice event reaches the story pane and the
    history index, so a later recall for "quarantined" finds it as well.
    """
    key = str(path)
    if key in _QUARANTINE_REPORTED:
        return
    _QUARANTINE_REPORTED.add(key)
    summary = quarantine_summary(path)
    if not summary:
        return
    try:
        record_event(
            world,
            {"ev": "notice", "text": summary},
            ts_ms=int(time.time() * 1000),
            mono_ns=time.monotonic_ns(),
        )
    except Exception:
        pass
    warnings.warn(summary, RuntimeWarning, stacklevel=2)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    cwd TEXT NOT NULL UNIQUE,
    generation INTEGER NOT NULL DEFAULT 0,
    gen_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK (kind IN ('attach', 'resume', 'fork', 'child')),
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ended_at TEXT,
    model TEXT NOT NULL DEFAULT '',
    thinking TEXT NOT NULL DEFAULT '',
    cache_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT ''
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
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (workspace_id, name)
);
CREATE TABLE IF NOT EXISTS tools (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    doc TEXT NOT NULL,
    source TEXT,
    frozen INTEGER NOT NULL CHECK (frozen IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (workspace_id, name)
);
CREATE TABLE IF NOT EXISTS calls (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    ts_ms INTEGER NOT NULL,
    mono_ns INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    payload_bytes INTEGER NOT NULL DEFAULT 0,
    payload_sha256 TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, seq)
);
CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
    workspace_id UNINDEXED,
    session_id UNINDEXED,
    kind UNINDEXED,
    text,
    source_seq UNINDEXED,
    tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS active_runs (
    run_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    pid INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    run_id TEXT NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_cursors (
    run_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    last_seen INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, channel)
);
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_calls_session
    ON calls(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_session
    ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace
    ON sessions(workspace_id, started_at);
CREATE INDEX IF NOT EXISTS idx_channel_messages
    ON channel_messages(workspace_id, channel, id);
"""


def _execute_schema(conn: sqlite3.Connection) -> None:
    statement = ""
    for line in SCHEMA_SQL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.DatabaseError("incomplete persistence schema")


def _add_column(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> bool:
    """Add a column that CREATE TABLE IF NOT EXISTS cannot reach.

    The schema is declarative and re-executed on every open, which creates new
    tables but never widens an existing one. An additive column is the only
    migration shape this database supports; anything else still means removing
    the file.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if not existing or column in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


def _drop_seat_scaffold(conn: sqlite3.Connection) -> None:
    """Retire a seat schema that landed before the seat was declared.

    The constitution (B2) forbids seat storage before the seat's fields,
    lifecycle and reset behaviour are written down and reviewed. The plural
    `seats` table and `sessions.seat_id` arrived on a shared file with none of
    that, no CRUD anywhere, and no rows. They go back out until the design is.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "seat_id" in columns:
        conn.execute("ALTER TABLE sessions DROP COLUMN seat_id")
    conn.execute("DROP INDEX IF EXISTS idx_seats_workspace")
    conn.execute("DROP TABLE IF EXISTS seats")


def _migrate(conn: sqlite3.Connection) -> None:
    """Create the current schema; old layouts are intentionally unsupported."""
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(sessions)")
    }
    if existing and "workspace_id" not in existing:
        raise RuntimeError(
            "legacy harness database: remove it; automatic migration was retired"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        _execute_schema(conn)
        _drop_seat_scaffold(conn)
        for shared in ("notes", "tools"):
            _add_column(conn, shared, "updated_at", "TEXT NOT NULL DEFAULT ''")
        versions = [
            int(row[0])
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        if versions and max(versions) > SCHEMA_VERSION:
            raise RuntimeError(
                f"harness schema versions {versions}, expected [{SCHEMA_VERSION}]"
            )
        if versions != [SCHEMA_VERSION]:
            conn.execute("DELETE FROM schema_migrations")
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


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
        _record_quarantine(path, exc, moved)
        warnings.warn(
            f"backed up corrupt harness database ({exc}); fresh state will be created"
            + (f" at {moved[0]}" if moved else "")
            + f"; accounted in {quarantine_log_path(path)}",
            RuntimeWarning,
            stacklevel=2,
        )
        conn = _connect(path)
        _migrate(conn)
        return conn


def _uuid7() -> str:
    """Time-ordered 128-bit id: 48-bit epoch milliseconds plus randomness."""
    ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    return f"{ms:012x}{os.urandom(10).hex()}"


def _data_from_world(world: World) -> dict[str, Any]:
    return {
        "notes": world.notes,
        "tools": {
            name: {"doc": tool.doc, "source": tool.source}
            for name, tool in world.tools.items()
            if not tool.frozen
        },
        "docs": {name: tool.doc for name, tool in world.tools.items() if tool.frozen},
        "prior": world.prior[max(0, int(world.session_prior_start)):],
        "generation": world.generation,
        "gen_reason": world.gen_reason,
        "thinking": world.thinking,
        "messages": world.messages[
            max(0, int(world.session_message_start)):
        ],
    }


def _workspace_id(
    conn: sqlite3.Connection, world: World, *, create: bool = True
) -> str | None:
    cwd = str(world.cwd.resolve())
    row = conn.execute("SELECT id FROM workspaces WHERE cwd = ?", (cwd,)).fetchone()
    if row is not None:
        return str(row["id"])
    if not create:
        return None
    now = datetime.now(timezone.utc).isoformat()
    workspace = _uuid7()
    conn.execute(
        "INSERT INTO workspaces(id, cwd, generation, gen_reason, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (workspace, cwd, int(world.generation), str(world.gen_reason), now, now),
    )
    return workspace


def _session_id(
    conn: sqlite3.Connection, world: World, workspace: str, *, create: bool = True
) -> str | None:
    current = run_id()
    row = conn.execute(
        "SELECT id FROM sessions WHERE id = ? AND workspace_id = ?",
        (current, workspace),
    ).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    if row is not None:
        conn.execute(
            "UPDATE sessions SET last_seen_at = ?, model = ?, thinking = ?"
            " WHERE id = ?",
            (now, str(world.model), str(world.thinking), current),
        )
        return current
    if not create:
        return None
    fresh = str(os.environ.get(NEW_SESSION_ENV, "")).strip().lower()
    parent = None
    if fresh not in {"1", "true", "yes", "on"}:
        parent = conn.execute(
            "SELECT id FROM sessions WHERE workspace_id = ?"
            " ORDER BY started_at DESC, id DESC LIMIT 1",
            (workspace,),
        ).fetchone()
    parent_id = str(parent["id"]) if parent else None
    conn.execute(
        "INSERT INTO sessions(id, workspace_id, parent_id, kind, started_at,"
        " last_seen_at, model, thinking, cache_key)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            current,
            workspace,
            parent_id,
            "resume" if parent_id else "attach",
            now,
            now,
            str(world.model),
            str(world.thinking),
            f"desmos-{current[:16]}",
        ),
    )
    return current


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            for key in ("text", "thinking", "content", "result"):
                value = block.get(key)
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)
    return ""


def _save_data(conn: sqlite3.Connection, world: World, data: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        workspace = _workspace_id(conn, world)
        assert workspace is not None
        session = _session_id(conn, world, workspace)
        assert session is not None
        conn.execute(
            "UPDATE workspaces SET generation = ?, gen_reason = ?, updated_at = ?"
            " WHERE id = ?",
            (int(data["generation"]), str(data["gen_reason"]), now, workspace),
        )
        conn.execute(
            "UPDATE sessions SET model = ?, thinking = ?, last_seen_at = ?"
            " WHERE id = ?",
            (str(world.model), str(data["thinking"]), now, session),
        )
        # Both transcripts are written as a slice from an offset marking this
        # session's own contribution, and an offset can drift past the end of a
        # list that was folded or truncated. When it does, the slice is empty --
        # and an empty slice is not an instruction to forget. Deleting on it
        # destroyed the stored transcript and then saved nothing over it, every
        # turn, in silence. Stale is recoverable; erased is not.
        if data["messages"]:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session,))
            conn.execute(
                "DELETE FROM history_fts WHERE session_id = ?"
                " AND kind LIKE 'message:%'",
                (session,),
            )
        if data["prior"]:
            conn.execute("DELETE FROM prior_turns WHERE session_id = ?", (session,))
            conn.execute(
                "DELETE FROM history_fts WHERE session_id = ? AND kind = 'prior'",
                (session,),
            )
        # A workspace is shared. Deleting every row and rewriting only the ones
        # this world happens to hold erases whatever another session wrote
        # since this one loaded, in silence. Retire what this world could have
        # seen and has dropped; leave anything newer than its view alone.
        watermark = str(world.synced_at or now)
        for table, held in (
            ("notes", set(data["notes"])),
            ("tools", set(data["docs"]) | set(data["tools"])),
        ):
            rows = conn.execute(
                f"SELECT name, updated_at FROM {table} WHERE workspace_id = ?",
                (workspace,),
            ).fetchall()
            for row in rows:
                name = str(row["name"])
                if name in held or str(row["updated_at"] or "") > watermark:
                    continue
                conn.execute(
                    f"DELETE FROM {table} WHERE workspace_id = ? AND name = ?",
                    (workspace, name),
                )

        for seq, item in enumerate(data["messages"]):
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            content = item.get("content")
            if not isinstance(content, (str, list)):
                continue
            conn.execute(
                "INSERT INTO messages(session_id, seq, role, content_json)"
                " VALUES (?, ?, ?, ?)",
                (
                    session,
                    seq,
                    item["role"],
                    json.dumps(content, separators=(",", ":")),
                ),
            )
            text = _content_text(content)
            if text.strip():
                conn.execute(
                    "INSERT INTO history_fts("
                    " workspace_id, session_id, kind, text, source_seq)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (workspace, session, f"message:{item['role']}", text, str(seq)),
                )
        for seq, item in enumerate(data["prior"]):
            if (
                isinstance(item, dict)
                and isinstance(item.get("prompt"), str)
                and isinstance(item.get("speech"), str)
            ):
                conn.execute(
                    "INSERT INTO prior_turns(session_id, seq, prompt, speech)"
                    " VALUES (?, ?, ?, ?)",
                    (session, seq, item["prompt"], item["speech"]),
                )
                conn.execute(
                    "INSERT INTO history_fts("
                    " workspace_id, session_id, kind, text, source_seq)"
                    " VALUES (?, ?, 'prior', ?, ?)",
                    (
                        workspace, session,
                        item["prompt"] + "\n" + item["speech"], str(seq),
                    ),
                )
        for name, body in data["notes"].items():
            if isinstance(body, str):
                conn.execute(
                    "INSERT OR REPLACE INTO"
                    " notes(workspace_id, name, body, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (workspace, str(name), body, now),
                )
        for name, doc in data["docs"].items():
            if isinstance(doc, str):
                conn.execute(
                    "INSERT OR REPLACE INTO"
                    " tools(workspace_id, name, doc, source, frozen, updated_at)"
                    " VALUES (?, ?, ?, NULL, 1, ?)",
                    (workspace, str(name), doc, now),
                )
        for name, spec in data["tools"].items():
            if not isinstance(spec, dict):
                continue
            doc, source = spec.get("doc"), spec.get("source")
            if isinstance(doc, str) and (isinstance(source, str) or source is None):
                conn.execute(
                    "INSERT OR REPLACE INTO"
                    " tools(workspace_id, name, doc, source, frozen, updated_at)"
                    " VALUES (?, ?, ?, ?, 0, ?)",
                    (workspace, str(name), doc, source, now),
                )
        world.synced_at = now
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def registry_path() -> Path:
    """The list of live desmos roots, one absolute cwd per line.

    DESMOS_REGISTRY overrides, same seam as DESMOS_SETTINGS / DESMOS_AUTH: the
    check floor points it at a temp file so a save() never touches the real
    ~/.desmos/registry.
    """
    return Path(os.environ.get("DESMOS_REGISTRY") or (Path.home() / ".desmos" / "registry"))


def _append_registry(cwd: Path) -> None:
    """Record `cwd` as a resumable root: deduped, dead roots lazy-pruned.

    Only reached from save(), which returns early when persist is False, so a
    child never writes here by construction.

    ponytail: read-modify-write with no lock, so two saves racing can lose one
    append — the registry is a best-effort resume hint, and the loser reappears
    on its next save. Add a flock lease only if a launcher starts trusting it as
    authoritative.
    """
    reg = registry_path()
    entry = str(Path(cwd).resolve())
    old = reg.read_text(encoding="utf-8").splitlines() if reg.exists() else []
    kept: list[str] = []
    seen: set[str] = set()
    for line in old:
        line = line.strip()
        # Drop blanks, dupes, and roots whose directory is gone (lazy prune).
        if not line or line in seen or not Path(line).is_dir():
            continue
        seen.add(line)
        kept.append(line)
    if entry not in seen:
        kept.append(entry)
    text = "\n".join(kept) + "\n"
    if old == kept:  # nothing changed; do not churn the file every save
        return
    atomic_write(reg, text)


def run_id() -> str:
    """The id of this attach, shared by every subsystem."""
    existing = os.environ.get(SESSION_ID_ENV)
    if existing:
        return existing
    fresh = _uuid7()
    os.environ[SESSION_ID_ENV] = fresh
    return fresh


def _presence_path(world: World, run: str) -> Path:
    return state_file(world).parent / "presence" / f"{run}.lock"


def announce(world: World, conn: sqlite3.Connection | None = None) -> None:
    """Advertise this live session using a lock the OS releases on exit."""
    if not world.persist:
        return
    run = run_id()
    path = _presence_path(world, run)
    key = str(path.resolve())
    if key not in _PRESENCE_LEASES:
        path.parent.mkdir(parents=True, exist_ok=True)
        lease = path.open("a+")
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _PRESENCE_LEASES[key] = lease
    own = conn is None
    db = conn or _open(state_file(world))
    now = datetime.now(timezone.utc).isoformat()
    try:
        with db:
            workspace = _workspace_id(db, world)
            assert workspace is not None
            session = _session_id(db, world, workspace)
            assert session is not None
            db.execute(
                """
                INSERT INTO active_runs(
                    run_id, workspace_id, session_id, pid, cwd, generation,
                    model, started_at, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    workspace_id=excluded.workspace_id,
                    session_id=excluded.session_id,
                    pid=excluded.pid,
                    cwd=excluded.cwd,
                    generation=excluded.generation,
                    model=excluded.model,
                    seen_at=excluded.seen_at
                """,
                (
                    run, workspace, session, os.getpid(), str(world.cwd.resolve()),
                    int(world.generation), str(world.model), now, now,
                ),
            )
    finally:
        if own:
            db.close()


class WorkspaceBusy(RuntimeError):
    """Another live front already owns this workspace."""

    def __init__(self, holder: dict[str, Any] | None = None) -> None:
        self.holder = dict(holder or {})
        pid = self.holder.get("pid")
        started = str(self.holder.get("started_at") or "")[:19]
        model = self.holder.get("model") or "unknown model"
        where = f"pid {pid}" if pid else "another process"
        super().__init__(
            "this workspace already has a live session: "
            f"{where}, {model}, started {started or 'unknown'}. "
            "Two fronts writing one workspace overwrite each other's "
            "transcript rather than interleaving; close the other one first."
        )


#: One interactive front per workspace. Held for process lifetime; the OS
#: releases it on exit, so a killed front never leaves a stale claim behind.
_WORKSPACE_LEASE: dict[str, Any] = {}


def _workspace_lock_path(world: World) -> Path:
    return state_file(world).parent / "presence" / "workspace.lock"


def _workspace_holder(world: World) -> dict[str, Any]:
    """The live active_runs row for this workspace, if one is readable."""
    try:
        db = _open(state_file(world))
    except Exception:  # noqa: BLE001 -- naming the holder is best effort
        return {}
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return {}
        row = db.execute(
            "SELECT run_id, session_id, pid, model, started_at FROM active_runs"
            " WHERE workspace_id = ? ORDER BY started_at DESC LIMIT 1",
            (workspace,),
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        db.close()


def claim_workspace(world: World) -> None:
    """Take the single-writer claim for this workspace, or refuse by name.

    A world is loaded as one concatenation of every session in the workspace,
    and every save rewrites that whole view -- so two live fronts do not
    interleave, they overwrite. Interactive fronts take this claim. Headless
    runs, checks and children deliberately do not: the peer rail exists to let
    two live sessions in one workspace talk, and this must not forbid it.
    """
    if not world.persist:
        return
    path = _workspace_lock_path(world)
    key = str(path.resolve())
    if key in _WORKSPACE_LEASE:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lease = path.open("a+")
    try:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lease.close()
        raise WorkspaceBusy(_workspace_holder(world)) from None
    _WORKSPACE_LEASE[key] = lease


def release_workspace(world: World) -> None:
    """Drop the claim early. Process exit releases it anyway."""
    key = str(_workspace_lock_path(world).resolve())
    lease = _WORKSPACE_LEASE.pop(key, None)
    if lease is not None:
        fcntl.flock(lease.fileno(), fcntl.LOCK_UN)
        lease.close()


def peers(world: World) -> list[dict[str, Any]]:
    """Live sessions in this workspace; stale rows are lease-pruned."""
    if not world.persist:
        return []
    announce(world)
    db = _open(state_file(world))
    current = run_id()
    live: list[dict[str, Any]] = []
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return []
        rows = db.execute(
            "SELECT * FROM active_runs WHERE workspace_id = ?"
            " ORDER BY started_at, run_id",
            (workspace,),
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["self"] = item["run_id"] == current
            if item["self"]:
                live.append(item)
                continue
            path = _presence_path(world, item["run_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.open("a+")
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                live.append(item)
            else:
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                with db:
                    db.execute(
                        "DELETE FROM active_runs WHERE run_id = ?",
                        (item["run_id"],),
                    )
                    db.execute(
                        "UPDATE sessions SET ended_at = COALESCE(ended_at, ?)"
                        " WHERE id = ?",
                        (
                            datetime.now(timezone.utc).isoformat(),
                            item["session_id"],
                        ),
                    )
            finally:
                probe.close()
    finally:
        db.close()
    return live


def peer_channel(target_run: str, kind: str) -> str:
    """Private directed channel for one bounded peer request or reply."""
    target = str(target_run).strip()
    if not target:
        raise ValueError("session post: target is empty")
    if kind not in {"request", "reply"}:
        raise ValueError(f"session peer channel: unknown kind {kind!r}")
    return f"peer:{target}:{kind}"


def channel_post(
    world: World, body: str, channel: str = "conflicts", author: str = ""
) -> dict[str, Any]:
    text = body.strip()
    if not text:
        raise ValueError("session post: message is empty")
    announce(world)
    db = _open(state_file(world))
    now = datetime.now(timezone.utc).isoformat()
    run = run_id()
    try:
        with db:
            workspace = _workspace_id(db, world)
            assert workspace is not None
            session = _session_id(db, world, workspace)
            assert session is not None
            cur = db.execute(
                """
                INSERT INTO channel_messages(
                    workspace_id, session_id, channel, run_id,
                    author, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace, session, channel or "conflicts", run,
                    author.strip() or run, text, now,
                ),
            )
            message_id = int(cur.lastrowid)
    finally:
        db.close()
    return {
        "id": message_id,
        "channel": channel or "conflicts",
        "run_id": run,
        "author": author.strip() or run,
        "body": text,
        "created_at": now,
    }


def channel_read(
    world: World, channel: str = "conflicts", since: int = 0, limit: int = 50
) -> list[dict[str, Any]]:
    if not world.persist:
        return []
    announce(world)
    db = _open(state_file(world))
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return []
        rows = db.execute(
            """
            SELECT id, channel, run_id, author, body, created_at
            FROM channel_messages
            WHERE workspace_id = ? AND channel = ? AND id > ?
            ORDER BY id LIMIT ?
            """,
            (
                workspace,
                channel or "conflicts",
                int(since),
                max(1, min(int(limit), 200)),
            ),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def channel_inbox(
    world: World, channel: str = "conflicts", limit: int = 20
) -> dict[str, Any]:
    """Unread messages from other runs, without advancing the durable cursor."""
    if not world.persist:
        return {"channel": channel, "unread": 0, "last_seen": 0, "messages": []}
    announce(world)
    db = _open(state_file(world))
    run = run_id()
    name = channel or "conflicts"
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return {"channel": name, "unread": 0, "last_seen": 0, "messages": []}
        cursor = db.execute(
            "SELECT last_seen FROM channel_cursors WHERE run_id = ? AND channel = ?",
            (run, name),
        ).fetchone()
        last_seen = int(cursor[0]) if cursor is not None else 0
        unread = int(
            db.execute(
                """
                SELECT COUNT(*) FROM channel_messages
                WHERE workspace_id = ? AND channel = ? AND id > ?
                  AND run_id != ?
                """,
                (workspace, name, last_seen, run),
            ).fetchone()[0]
        )
        rows = db.execute(
            """
            SELECT id, channel, run_id, author, body, created_at
            FROM channel_messages
            WHERE workspace_id = ? AND channel = ? AND id > ?
              AND run_id != ?
            ORDER BY id LIMIT ?
            """,
            (
                workspace, name, last_seen, run,
                max(1, min(int(limit), 200)),
            ),
        ).fetchall()
        return {
            "channel": name, "unread": unread, "last_seen": last_seen,
            "messages": [dict(row) for row in rows],
        }
    finally:
        db.close()


def channel_dismiss(
    world: World, channel: str = "conflicts", through: int = 0
) -> dict[str, Any]:
    """Advance this run's unread cursor, defaulting to the newest message."""
    if not world.persist:
        return {"channel": channel, "last_seen": 0}
    announce(world)
    db = _open(state_file(world))
    run = run_id()
    name = channel or "conflicts"
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return {"channel": name, "last_seen": 0}
        if int(through) <= 0:
            through = int(
                db.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM channel_messages"
                    " WHERE workspace_id = ? AND channel = ?",
                    (workspace, name),
                ).fetchone()[0]
            )
        with db:
            db.execute(
                """
                INSERT INTO channel_cursors(run_id, channel, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id, channel) DO UPDATE SET
                    last_seen=MAX(channel_cursors.last_seen, excluded.last_seen)
                """,
                (run, name, int(through)),
            )
        return {"channel": name, "last_seen": int(through)}
    finally:
        db.close()


def channel_notice(world: World, channel: str = "conflicts") -> str:
    inbox = channel_inbox(world, channel=channel, limit=1)
    if not inbox["unread"]:
        return ""
    message = inbox["messages"][0]
    preview = " ".join(str(message["body"]).split())
    if len(preview) > 120:
        preview = preview[:119].rstrip() + "…"
    return (
        f"IRC #{inbox['channel']}: {inbox['unread']} unread from "
        f"{message['author']}: {preview}. Use session inbox/read/post/dismiss."
    )


def record_call(world: World, entry: dict[str, Any]) -> None:
    """Append one priced model round to the current session."""
    if not world.persist or not entry:
        return
    usage = entry.get("usage") or {}
    if not any(usage.get(key) for key in prices.USAGE_KEYS):
        return
    model = str(world.model or "")
    try:
        conn = _open(state_file(world))
    except Exception:  # noqa: BLE001
        return
    try:
        with conn:
            workspace = _workspace_id(conn, world)
            assert workspace is not None
            session = _session_id(conn, world, workspace)
            assert session is not None
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM calls WHERE session_id = ?",
                (session,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO calls(
                    session_id, seq, ts, model, input_tokens,
                    cache_read_input_tokens, cache_creation_input_tokens,
                    output_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session,
                    int(row[0]) + 1,
                    str(entry.get("ts") or datetime.now(timezone.utc).isoformat()),
                    model,
                    int(usage.get("input_tokens") or 0),
                    int(usage.get("cache_read_input_tokens") or 0),
                    int(usage.get("cache_creation_input_tokens") or 0),
                    int(usage.get("output_tokens") or 0),
                    float(prices.cost(usage, model)),
                ),
            )
    except Exception:  # noqa: BLE001
        pass
    finally:
        conn.close()


def runs(world: World, limit: int = 20) -> list[dict[str, Any]]:
    """Per-session usage rollups, newest last."""
    if not world.persist:
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
            SELECT sessions.id AS run_id,
                   COUNT(*) AS calls,
                   MIN(calls.ts) AS started,
                   MAX(calls.ts) AS ended,
                   SUM(calls.input_tokens) AS fresh,
                   SUM(calls.cache_read_input_tokens) AS read,
                   SUM(calls.cache_creation_input_tokens) AS write,
                   SUM(calls.output_tokens) AS out,
                   SUM(calls.cost_usd) AS cost,
                   GROUP_CONCAT(DISTINCT calls.model) AS models
            FROM calls
            JOIN sessions ON sessions.id = calls.session_id
            WHERE sessions.workspace_id = ?
            GROUP BY sessions.id
            ORDER BY started DESC LIMIT ?
            """,
            (workspace, int(limit)),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()
    return [dict(row) for row in reversed(rows)]


#: One line per dropped session, beside the database that dropped it. Pruning
#: is the second silent-loss path: foreign keys cascade, so removing a session
#: row also removes its messages, prior turns, events and calls, and until now
#: nothing recorded which conversations were spent to keep the file small. Same
#: rule as a quarantine -- the account of a loss cannot live only inside the
#: thing that lost it.
PRUNE_LOG = "pruned.jsonl"


def prune_log_path(path: Path) -> Path:
    return path.parent / PRUNE_LOG


def _prune_census(
    conn: sqlite3.Connection, doomed: list[str]
) -> list[dict[str, Any]]:
    """Describe sessions about to be deleted, while they still exist."""
    out: list[dict[str, Any]] = []
    for sid in doomed:
        entry: dict[str, Any] = {"session_id": sid}
        try:
            row = conn.execute(
                "SELECT started_at, last_seen_at, model, title, kind"
                " FROM sessions WHERE id = ?",
                (sid,),
            ).fetchone()
            if row is not None:
                entry.update(
                    started_at=row[0],
                    last_seen_at=row[1],
                    model=row[2],
                    title=row[3],
                    kind=row[4],
                )
            counts = conn.execute(
                "SELECT count(*), coalesce(sum(length(content_json)), 0)"
                " FROM messages WHERE session_id = ?",
                (sid,),
            ).fetchone()
            entry["messages"] = int(counts[0])
            entry["bytes"] = int(counts[1])
            for table in ("prior_turns", "events", "calls"):
                entry[table] = int(
                    conn.execute(
                        f"SELECT count(*) FROM {table} WHERE session_id = ?", (sid,)
                    ).fetchone()[0]
                )
            first = conn.execute(
                "SELECT content_json FROM messages"
                " WHERE session_id = ? AND role = 'user' ORDER BY seq LIMIT 1",
                (sid,),
            ).fetchone()
            if first is not None:
                entry["opened_with"] = _content_text(json.loads(first[0]))[:300]
        except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
            entry["census_error"] = f"{type(exc).__name__}: {exc}"[:200]
        out.append(entry)
    return out


def _record_prune(path: Path, entries: list[dict[str, Any]]) -> None:
    """Append the account of one pruning pass. Never raises."""
    if not entries:
        return
    try:
        log = prune_log_path(path)
        log.parent.mkdir(parents=True, exist_ok=True)
        at = datetime.now(timezone.utc).isoformat()
        with log.open("a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(
                    json.dumps({"at": at, **entry}, separators=(",", ":")) + "\n"
                )
    except Exception:
        # Bounding the database must not fail because its account cannot be
        # written. The pruning itself already happened.
        pass


def pruned(path: Path) -> list[dict[str, Any]]:
    """Every session this database dropped to stay bounded, oldest first."""
    log = prune_log_path(path)
    if not log.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _prune_sessions(
    conn: sqlite3.Connection,
    workspace: str,
    current: str,
    keep: int = SESSION_KEEP,
    path: Path | None = None,
) -> int:
    """Bound history without deleting a live peer or this attach."""
    rows = conn.execute(
        "SELECT id FROM sessions WHERE workspace_id = ?"
        " ORDER BY started_at DESC, id DESC",
        (workspace,),
    ).fetchall()
    live = {
        str(row["run_id"])
        for row in conn.execute(
            "SELECT run_id FROM active_runs WHERE workspace_id = ?",
            (workspace,),
        )
    }
    doomed = [
        str(row["id"])
        for row in rows[max(1, int(keep)):]
        if str(row["id"]) != current and str(row["id"]) not in live
    ]
    if doomed:
        # Census first: after the delete there is nothing left to describe.
        census = _prune_census(conn, doomed) if path is not None else []
        conn.executemany(
            "DELETE FROM history_fts WHERE session_id = ?", [(sid,) for sid in doomed]
        )
        conn.executemany("DELETE FROM sessions WHERE id = ?", [(sid,) for sid in doomed])
        if path is not None:
            _record_prune(path, census)
    return len(doomed)


def _strip_opaque(value: Any) -> Any:
    """Remove provider ciphertext recursively; it is replay-only and enormous."""
    if isinstance(value, dict):
        return {
            str(key): ("" if key == "encrypted_content" else _strip_opaque(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_strip_opaque(item) for item in value]
    return value


#: bm25 returns negative scores, more negative being a better match. Adding a
#: positive offset to the live session's rows demotes them without hiding them:
#: a strong current-session match still outranks a weak older one, but the turn
#: that merely *asked* the question no longer outranks the history it asked
#: about. Exclusion was the first design and it was wrong -- after a server-side
#: fold, the current session's early turns are precisely what the caller can no
#: longer see, so they must stay reachable.
LIVE_SESSION_PENALTY = 10.0


_HARNESS_TURNS = (
    "<compacted",
    "<result",
    "[background task",
    "[stopped by the user",
    "# now",
)


def _strip_preamble(text: str) -> str:
    """Drop the namespace dump the harness prepends to a human turn."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "ns:":
        index = 1
        while index < len(lines) and (
            not lines[index].strip() or lines[index].startswith("  ")
        ):
            index += 1
        lines = lines[index:]
    ask = "\n".join(lines).strip()
    return "" if ask.startswith(_HARNESS_TURNS) else ask


def _human_ask(content_json: str) -> str:
    """The text a person wrote, or empty for any other user-shaped row."""
    try:
        value = json.loads(content_json)
    except ValueError:
        return ""
    blocks = [{"type": "text", "text": value}] if isinstance(value, str) else value
    if not isinstance(blocks, list) or not blocks:
        return ""
    parts = []
    for block in blocks:
        # A tool result anywhere in the row means the harness spoke, not a
        # person. One non-text block disqualifies the whole message.
        if not isinstance(block, dict) or block.get("type") != "text":
            return ""
        parts.append(str(block.get("text", "")))
    return _strip_preamble("\n".join(parts))


def session_asks(
    world: World, *, session: str | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """The human asks of one session, read back out of the stored record.

    API role is not authorship. Most user-shaped rows are tool results, and a
    genuine turn arrives behind a namespace dump. Counting `role == 'user'`
    returned thirty-six rows here, of which three were written by a person and
    two were asks. Classifying by content-block type returns those two.

    Oldest first, so the last entry is the current task. Reading the store
    rather than `world.messages` is the point: this survives a fold, a restart,
    and a model switch, none of which the live list does.
    """
    conn = _open(state_file(world))
    try:
        rows = conn.execute(
            "SELECT seq, content_json FROM messages"
            " WHERE session_id = ? AND role = 'user' ORDER BY seq",
            (session or run_id(),),
        ).fetchall()
    finally:
        conn.close()
    found = [
        {"seq": int(seq), "ask": ask}
        for seq, content in rows
        if (ask := _human_ask(str(content)))
    ]
    return found[-limit:] if limit and limit > 0 else found


def last_task(world: World, *, session: str | None = None) -> str:
    """The most recent thing a person actually asked for."""
    asks = session_asks(world, session=session, limit=1)
    return str(asks[-1]["ask"]) if asks else ""


def search_history(
    world: World, query: str, limit: int = 12
) -> list[dict[str, Any]]:
    """Rank this workspace's durable session history with SQLite FTS5.

    `save()` reindexes the whole current transcript on every save, so the live
    session dominates the table by row count (237 of 314 when measured). Its
    rows are demoted by `LIVE_SESSION_PENALTY` rather than dropped.
    """
    path = state_file(world)
    if not path.is_file():
        return []
    terms = [term for term in query.split() if term]
    if not terms:
        return []
    match = " AND ".join(
        '"' + term.replace('"', '""') + '"' for term in terms
    )
    conn = _open(path)
    try:
        workspace = _workspace_id(conn, world, create=False)
        if workspace is None:
            return []
        rows = conn.execute(
            """
            SELECT session_id, kind, text, source_seq,
                   bm25(history_fts)
                   + (CASE WHEN session_id = ? THEN ? ELSE 0 END) AS score
            FROM history_fts
            WHERE history_fts MATCH ? AND workspace_id = ?
            ORDER BY score LIMIT ?
            """,
            (
                run_id(),
                LIVE_SESSION_PENALTY,
                match,
                workspace,
                max(1, min(int(limit), 100)),
            ),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def record_event(
    world: World,
    event: dict[str, Any],
    *,
    ts_ms: int,
    mono_ns: int,
) -> int:
    """Append one compact wire event and return its session-local sequence."""
    if not world.persist:
        return 0
    kind = str(event.get("ev") or "unknown")
    # These are live animation/telemetry streams, not replay state. Persisting
    # them created 848k rows and hundreds of MB without helping late attach.
    if kind in {"child", "timing"}:
        return 0
    import hashlib

    raw = json.dumps(event, default=str, separators=(",", ":"))
    clean = _strip_opaque(event)
    if kind in {"post", "complete"} and len(raw) > 32_768:
        clean = {
            "ev": kind,
            "model": event.get("model"),
            "provider": event.get("provider"),
            "message_count": event.get("message_count"),
            "elided": True,
        }
    payload = json.dumps(clean, default=str, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    conn = _open(state_file(world))
    try:
        with conn:
            workspace = _workspace_id(conn, world)
            assert workspace is not None
            session = _session_id(conn, world, workspace)
            assert session is not None
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id = ?",
                (session,),
            ).fetchone()
            seq = int(row[0]) + 1
            conn.execute(
                "INSERT INTO events(session_id, seq, ts_ms, mono_ns, kind,"
                " payload_json, payload_bytes, payload_sha256)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session, seq, int(ts_ms), int(mono_ns), kind,
                    payload, len(raw.encode("utf-8")), digest,
                ),
            )
            # A `result` is tool output: derived, already stored verbatim
            # in `events`, and a quarter of this index by row count.
            # Ranking it as authored history buries the turn that decided
            # something under the output that turn happened to print.
            # Index what someone said, not what a command returned.
            if kind in {"prompt", "speech", "notice", "error"}:
                text = _content_text(clean) or payload
                conn.execute(
                    "INSERT INTO history_fts("
                    " workspace_id, session_id, kind, text, source_seq)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (workspace, session, f"event:{kind}", text, str(seq)),
                )
        return seq
    finally:
        conn.close()


def read_events(
    world: World,
    since: int = 0,
    limit: int = 4096,
    session: str | None = None,
) -> list[dict[str, Any]]:
    """Read one session's compact replay stream."""
    if not world.persist:
        return []
    conn = _open(state_file(world))
    try:
        sid = session or run_id()
        rows = conn.execute(
            """
            SELECT seq, ts_ms AS ts, mono_ns, kind, payload_json,
                   payload_bytes, payload_sha256
            FROM events
            WHERE session_id = ? AND seq > ?
            ORDER BY seq LIMIT ?
            """,
            (sid, int(since), max(1, min(int(limit), 20_000))),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = json.loads(row["payload_json"])
            item.update({
                "seq": int(row["seq"]),
                "ts": int(row["ts"]),
                "mono_ns": int(row["mono_ns"]),
                "payload_bytes": int(row["payload_bytes"]),
                "payload_sha256": row["payload_sha256"],
            })
            out.append(item)
        return out
    finally:
        conn.close()


def save(world: World) -> None:
    if not world.persist:
        return
    conn = _open(state_file(world))
    try:
        _save_data(conn, world, _data_from_world(world))
        announce(world, conn)
        with conn:
            workspace = _workspace_id(conn, world)
            assert workspace is not None
            session = _session_id(conn, world, workspace)
            assert session is not None
            _prune_sessions(conn, workspace, session, path=state_file(world))
    finally:
        conn.close()
    try:
        _append_registry(world.cwd)
    except Exception:  # noqa: BLE001 -- a resume hint must never fail a save
        pass


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
            world.tools[name] = load_grown(world, name, doc, source)
    raw_prior = data.get("prior")
    if isinstance(raw_prior, list):
        world.prior = [
            {"prompt": item["prompt"], "speech": item["speech"]}
            for item in raw_prior[-PRIOR_KEEP:]
            if isinstance(item, dict)
            and isinstance(item.get("prompt"), str)
            and isinstance(item.get("speech"), str)
        ]
        world.session_prior_start = max(
            0, len(world.prior) - int(data.get("_current_prior") or 0)
        )
    if isinstance(data.get("generation"), int) and data["generation"] > 0:
        world.generation = data["generation"]
    if isinstance(data.get("gen_reason"), str) and data["gen_reason"]:
        world.gen_reason = data["gen_reason"]
    if isinstance(data.get("thinking"), str) and data["thinking"].strip():
        world.thinking = data["thinking"].strip()
    raw_msgs = data.get("messages")
    if isinstance(raw_msgs, list):
        # Align after the role filter, not before: a dropped junk row shifts
        # every boundary test that ran ahead of it. Repair after alignment,
        # so the head alignment landed on cannot orphan a call the repair
        # already answered.
        aligned = turn_aligned(
            [
                {"role": item["role"], "content": item["content"]}
                for item in raw_msgs
                if isinstance(item, dict)
                and item.get("role") in {"user", "assistant"}
                and isinstance(item.get("content"), (str, list))
            ]
        )
        current_count = min(
            len(aligned), int(data.get("_current_messages") or 0)
        )
        world.session_message_start = len(aligned) - current_count
        world.messages = repair_orphan_calls(aligned)


def _lineage(conn: sqlite3.Connection, session: str) -> list[str]:
    """This session and its ancestors, oldest first."""
    chain: list[str] = []
    seen: set[str] = set()
    cur: str | None = session
    while cur and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        row = conn.execute("SELECT parent_id FROM sessions WHERE id = ?", (cur,)).fetchone()
        cur = str(row["parent_id"]) if row and row["parent_id"] else None
    chain.reverse()
    return chain


def _read_data(
    conn: sqlite3.Connection, world: World
) -> dict[str, Any] | None:
    workspace = _workspace_id(conn, world, create=False)
    if workspace is None:
        return None
    current = _session_id(conn, world, workspace)
    assert current is not None
    workspace_row = conn.execute(
        "SELECT generation, gen_reason FROM workspaces WHERE id = ?",
        (workspace,),
    ).fetchone()
    current_row = conn.execute(
        "SELECT thinking FROM sessions WHERE id = ?", (current,)
    ).fetchone()

    lineage = _lineage(conn, current)
    slots = ",".join("?" * len(lineage))
    message_rows = conn.execute(
        f"""
        SELECT messages.session_id, messages.role, messages.content_json
        FROM messages
        JOIN sessions ON sessions.id = messages.session_id
        WHERE messages.session_id IN ({slots})
        ORDER BY sessions.started_at, sessions.id, messages.seq
        """,
        lineage,
    ).fetchall()
    messages = [
        {"role": row["role"], "content": json.loads(row["content_json"])}
        for row in message_rows
    ]
    current_messages = sum(
        1 for row in message_rows if str(row["session_id"]) == current
    )

    prior_rows = conn.execute(
        f"""
        SELECT prior_turns.session_id, prior_turns.prompt, prior_turns.speech
        FROM prior_turns
        JOIN sessions ON sessions.id = prior_turns.session_id
        WHERE prior_turns.session_id IN ({slots})
        ORDER BY sessions.started_at, sessions.id, prior_turns.seq
        """,
        lineage,
    ).fetchall()
    kept_prior = prior_rows[-PRIOR_KEEP:]
    prior = [
        {"prompt": row["prompt"], "speech": row["speech"]}
        for row in kept_prior
    ]
    current_prior = sum(
        1 for row in kept_prior if str(row["session_id"]) == current
    )

    notes = {
        row["name"]: row["body"]
        for row in conn.execute(
            "SELECT name, body FROM notes WHERE workspace_id = ?", (workspace,)
        )
    }
    docs: dict[str, str] = {}
    tools: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT name, doc, source, frozen FROM tools WHERE workspace_id = ?",
        (workspace,),
    ):
        if row["frozen"]:
            docs[str(row["name"])] = row["doc"]
        else:
            tools[str(row["name"])] = {
                "doc": row["doc"], "source": row["source"]
            }
    return {
        "generation": int(workspace_row["generation"]),
        "gen_reason": str(workspace_row["gen_reason"]),
        "thinking": str(current_row["thinking"]),
        "messages": messages,
        "prior": prior,
        "notes": notes,
        "docs": docs,
        "tools": tools,
        "_current_messages": current_messages,
        "_current_prior": current_prior,
    }


def load(world: World) -> None:
    if not world.persist:
        return
    path = state_file(world)
    conn = _open(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        workspace = _workspace_id(conn, world)
        assert workspace is not None
        session = _session_id(conn, world, workspace)
        assert session is not None
        data = _read_data(conn, world)
        conn.commit()
        if data is not None:
            _apply_data(world, data)
        world.synced_at = datetime.now(timezone.utc).isoformat()
        announce(world, conn)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    # After the write lock is released: record_event opens its own connection.
    _report_quarantines(world, path)


def op_rollup(world: World) -> list[tuple[str, int]]:
    """Calls per family/op across this session's line of descent."""
    if not world.persist:
        return []
    import ast
    from collections import Counter

    db = _open(state_file(world))
    try:
        workspace = _workspace_id(db, world, create=False)
        if workspace is None:
            return []
        chain = _lineage(db, _session_id(db, world, workspace))
        slots = ",".join("?" for _ in chain)
        rows = db.execute(
            "SELECT payload_json FROM events WHERE kind = 'result'"
            f" AND session_id IN ({slots})",
            chain,
        ).fetchall()
    finally:
        db.close()
    counts: Counter[str] = Counter()
    for row in rows:
        try:
            payload = json.loads(row[0])
        except ValueError:
            continue
        if payload.get("phase") != "done":
            continue
        attrs = payload.get("attrs") or {}
        if isinstance(attrs, str):
            try:
                attrs = ast.literal_eval(attrs)
            except (ValueError, SyntaxError):
                attrs = {}
        tag = str(payload.get("tag") or "?")
        op = str((attrs or {}).get("op") or "")
        counts[f"{tag} {op}".strip()] += 1
    return counts.most_common()


def _lineage_chain(conn, world):
    """This world's session line of descent, oldest first."""
    workspace = _workspace_id(conn, world, create=False)
    if workspace is None:
        return []
    session = _session_id(conn, world, workspace, create=False)
    return _lineage(conn, session) if session else []


def exchange_index(world: World) -> list[dict[str, Any]]:
    """Every prompt in this line of descent, folded out of memory or not."""
    if not world.persist:
        return []
    conn = _open(state_file(world))
    try:
        chain = _lineage_chain(conn, world)
        if not chain:
            return []
        slots = ",".join("?" for _ in chain)
        rows = conn.execute(
            "SELECT events.session_id AS sid, events.seq AS seq,"
            " events.payload_json AS payload"
            " FROM events JOIN sessions ON sessions.id = events.session_id"
            f" WHERE events.kind = 'prompt' AND events.session_id IN ({slots})"
            " ORDER BY sessions.started_at, sessions.id, events.seq",
            chain,
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        text = " ".join(str(payload.get("text") or "").split())
        out.append({
            "n": len(out) + 1,
            "session": row["sid"],
            "seq": int(row["seq"]),
            "text": text,
        })
    return out


def exchange_events(world: World, n: int) -> list[dict[str, Any]]:
    """The verbatim event record of one exchange, by index position."""
    index = exchange_index(world)
    if not index or n < 1 or n > len(index):
        return []
    item = index[n - 1]
    stop = None
    for later in index[n:]:
        if later["session"] == item["session"]:
            stop = later["seq"]
            break
    conn = _open(state_file(world))
    try:
        sql = "SELECT kind, payload_json FROM events WHERE session_id = ? AND seq >= ?"
        args: list[Any] = [item["session"], item["seq"]]
        if stop is not None:
            sql += " AND seq < ?"
            args.append(stop)
        rows = conn.execute(sql + " ORDER BY seq", args).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except ValueError:
            continue
        payload["kind"] = row["kind"]
        out.append(payload)
    return out
