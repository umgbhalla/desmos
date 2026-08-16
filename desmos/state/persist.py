from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import tempfile
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

SCHEMA_VERSION = 5
SESSION_ID = "default"
#: One attach of the process. `sessions` is a singleton keyed to the cwd --
#: it is the durable world, not a sitting -- so nothing named the thing a
#: person means by "this session", and per-call usage lived only in
#: `world.log`, in memory, gone on restart. The id goes in the environment
#: rather than a module global so `reload_sdk` (which re-imports this module)
#: keeps the same run, and a fresh process gets a fresh one.
RUN_ID_ENV = "DESMOS_RUN_ID"
DB_FILENAME = "harness.sqlite3"
LEGACY_FILENAME = "harness.json"
KEEP_MESSAGES = 80
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
        CREATE TABLE IF NOT EXISTS calls (
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            ts TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (session_id, run_id, seq)
        );
        CREATE TABLE IF NOT EXISTS active_runs (
            run_id TEXT PRIMARY KEY,
            pid INTEGER NOT NULL,
            cwd TEXT NOT NULL,
            generation INTEGER NOT NULL,
            model TEXT NOT NULL,
            started_at TEXT NOT NULL,
            seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS channel_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            run_id TEXT NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        DROP TABLE IF EXISTS seat;
        DROP TABLE IF EXISTS seat_events;
        CREATE INDEX IF NOT EXISTS idx_messages_session_seq
            ON messages(session_id, seq);
        CREATE INDEX IF NOT EXISTS idx_calls_run
            ON calls(session_id, run_id, seq);
        CREATE INDEX IF NOT EXISTS idx_channel_messages_channel_id
            ON channel_messages(channel, id);
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
        "messages": turn_aligned(world.messages),
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
    """The id of this attach of the process. Stable across `reload_sdk`."""
    existing = os.environ.get(RUN_ID_ENV)
    if existing:
        return existing
    fresh = f"{int(datetime.now(timezone.utc).timestamp())}-{os.getpid()}"
    os.environ[RUN_ID_ENV] = fresh
    return fresh


def _presence_path(world: World, run: str) -> Path:
    return state_file(world).parent / "presence" / f"{run}.lock"


def announce(world: World, conn: sqlite3.Connection | None = None) -> None:
    """Advertise this live process using a lock the OS releases on exit."""
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
            db.execute(
                """
                INSERT INTO active_runs(
                    run_id, pid, cwd, generation, model, started_at, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    pid=excluded.pid, cwd=excluded.cwd,
                    generation=excluded.generation, model=excluded.model,
                    seen_at=excluded.seen_at
                """,
                (run, os.getpid(), str(world.cwd), int(world.generation),
                 str(world.model), now, now),
            )
    finally:
        if own:
            db.close()


def peers(world: World) -> list[dict[str, Any]]:
    """Live runs in this checkout; stale rows are pruned by probing their leases."""
    if not world.persist:
        return []
    announce(world)
    db = _open(state_file(world))
    current = run_id()
    live: list[dict[str, Any]] = []
    try:
        rows = db.execute(
            "SELECT * FROM active_runs ORDER BY started_at, run_id"
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
                    db.execute("DELETE FROM active_runs WHERE run_id = ?", (item["run_id"],))
            finally:
                probe.close()
    finally:
        db.close()
    return live


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
            cur = db.execute(
                """
                INSERT INTO channel_messages(channel, run_id, author, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (channel or "conflicts", run, author.strip() or run, text, now),
            )
            message_id = int(cur.lastrowid)
    finally:
        db.close()
    return {
        "id": message_id, "channel": channel or "conflicts", "run_id": run,
        "author": author.strip() or run, "body": text, "created_at": now,
    }


def channel_read(
    world: World, channel: str = "conflicts", since: int = 0, limit: int = 50
) -> list[dict[str, Any]]:
    if not world.persist:
        return []
    announce(world)
    db = _open(state_file(world))
    try:
        rows = db.execute(
            """
            SELECT id, channel, run_id, author, body, created_at
            FROM channel_messages
            WHERE channel = ? AND id > ?
            ORDER BY id LIMIT ?
            """,
            (channel or "conflicts", int(since), max(1, min(int(limit), 200))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def record_call(world: World, entry: dict[str, Any]) -> None:
    """Append one model round-trip to the durable ledger.

    Called from the loop the moment a response lands, not from `save`: a run
    that is killed mid-turn still spent the money, and the whole point of the
    table is that the number survives the process. Never raises -- a billing
    row is not worth losing a turn over.
    """
    if not world.persist or not entry:
        return
    usage = entry.get("usage") or {}
    if not any(usage.get(key) for key in prices.USAGE_KEYS):
        return
    model = str(world.model or "")
    run = run_id()
    try:
        conn = _open(state_file(world))
    except Exception:  # noqa: BLE001
        return
    try:
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions(
                    id, cwd, generation, gen_reason, thinking, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    SESSION_ID,
                    str(world.cwd),
                    int(world.generation),
                    str(world.gen_reason),
                    str(world.thinking),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM calls WHERE session_id = ? AND run_id = ?",
                (SESSION_ID, run),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO calls(
                    session_id, run_id, seq, ts, model,
                    input_tokens, cache_read_input_tokens,
                    cache_creation_input_tokens, output_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    SESSION_ID,
                    run,
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
    """Per-run rollups, newest last: what each attach of the process spent."""
    if not world.persist:
        return []
    try:
        conn = _open(state_file(world))
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = conn.execute(
            """
            SELECT run_id, COUNT(*) AS calls, MIN(ts) AS started, MAX(ts) AS ended,
                   SUM(input_tokens) AS fresh,
                   SUM(cache_read_input_tokens) AS read,
                   SUM(cache_creation_input_tokens) AS write,
                   SUM(output_tokens) AS out,
                   SUM(cost_usd) AS cost,
                   GROUP_CONCAT(DISTINCT model) AS models
            FROM calls WHERE session_id = ?
            GROUP BY run_id ORDER BY started DESC LIMIT ?
            """,
            (SESSION_ID, int(limit)),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()
    return [dict(row) for row in reversed(rows)]


def save(world: World) -> None:
    if not world.persist:
        return
    conn = _open(state_file(world))
    try:
        _save_data(conn, world, _data_from_world(world))
        announce(world, conn)
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
        world.messages = repair_orphan_calls(
            turn_aligned(
                [
                    {"role": item["role"], "content": item["content"]}
                    for item in raw_msgs
                    if isinstance(item, dict)
                    and item.get("role") in {"user", "assistant"}
                    and isinstance(item.get("content"), (str, list))
                ]
            )
        )


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
        announce(world, conn)
        conn.close()
