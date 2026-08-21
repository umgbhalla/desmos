"""The refine pass: a grown tool is kept because the record says it works.

`docs/self-growth.md` names the rot: "a grown handler that is never called, or
that errors twice, should be tombstoned. Otherwise the dialect sludges." The
catalog line of a dead tag is paid for on every request forever, so growth
without a second pass is a ratchet.

Nothing here is a new table. A grown tool's whole career is already on the
record -- every dispatch fires a `result` event with the tag and the text it
returned, and a raising syscall's text *is* its traceback -- so this module
reads that back per tool, exactly as `witness` reads work events per actor.

A tombstone is not a delete. The row keeps its source and gains the date and
the reason; the loader stops binding it, the catalog stops advertising it, and
`revive` puts it back. Nothing is ever deleted here either.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from desmos.kernel.types import World

#: Two failures with nothing working in between is the spec's rot signal.
BROKEN_ERRORS = 2

#: Sessions that came and went without ever calling it. One later session is
#: noise -- a tool grown for a task nobody returned to that day. Two is a
#: pattern the catalog is paying for.
UNUSED_SESSIONS = 2

_TRACEBACK = "Traceback (most recent call last)"


def _failed(text: str) -> bool:
    """Did this dispatch come back a failure? The result text is the evidence."""
    head = text.lstrip()
    return head.startswith(_TRACEBACK) or head.startswith("grown tool <")


def census(world: World) -> list[dict[str, Any]]:
    """Every grown tool in this workspace with the record of its use."""
    from desmos.state import persist

    if not world.persist:
        return []
    path = persist.state_file(world)
    if not path.is_file():
        return []
    conn = persist._open(path)
    try:
        workspace = persist._workspace_id(conn, world, create=False)
        if workspace is None:
            return []
        rows = conn.execute(
            "SELECT name, doc, updated_at, tombstoned_at, tombstone_reason"
            " FROM tools WHERE workspace_id = ? AND frozen = 0 ORDER BY name",
            (workspace,),
        ).fetchall()
        if not rows:
            return []
        chain = persist._lineage_chain(conn, world)
        events: list[tuple[str, str]] = []
        if chain:
            slots = ",".join("?" for _ in chain)
            events = [
                (str(r[0]), str(r[1] or ""))
                for r in conn.execute(
                    "SELECT payload_json, ts_ms FROM events WHERE kind = 'result'"
                    f" AND session_id IN ({slots})",
                    chain,
                )
            ]
        starts = [
            str(r[0] or "")
            for r in conn.execute(
                "SELECT started_at FROM sessions WHERE workspace_id = ?",
                (workspace,),
            )
        ]
    finally:
        conn.close()

    calls: dict[str, dict[str, Any]] = {}
    for payload_json, raw_ts in events:
        try:
            payload = json.loads(payload_json)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("phase") != "done":
            continue
        tag = str(payload.get("tag") or "")
        if not tag:
            continue
        slot = calls.setdefault(tag, {"calls": 0, "errors": 0, "last_ms": 0})
        slot["calls"] += 1
        if _failed(str(payload.get("text") or "")):
            slot["errors"] += 1
        try:
            slot["last_ms"] = max(slot["last_ms"], int(raw_ts or 0))
        except ValueError:
            pass

    out: list[dict[str, Any]] = []
    for row in rows:
        name = str(row["name"])
        seen = calls.get(name, {"calls": 0, "errors": 0, "last_ms": 0})
        born = str(row["updated_at"] or "")
        later = sum(1 for started in starts if born and started > born)
        doc = str(row["doc"] or "")
        last_ms = int(seen.get("last_ms", 0))
        item = {
            "name": name,
            "doc": doc,
            "calls": int(seen["calls"]),
            "errors": int(seen["errors"]),
            "last_used": (
                datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).isoformat()
                if last_ms > 0
                else ""
            ),
            # The catalog line this tool costs on every request, in the same
            # chars/4 estimate the budget code uses. Paid whether or not the
            # tag is ever dispatched -- that is why an unused line is rot.
            "tokens": (len(f"<{name}> {doc}") + 3) // 4,
            "sessions_since": later,
            "tombstoned_at": str(row["tombstoned_at"] or ""),
            "reason": str(row["tombstone_reason"] or ""),
        }
        item["verdict"] = _verdict(item)
        out.append(item)
    return out


def _verdict(item: dict[str, Any]) -> str:
    if item["tombstoned_at"]:
        return "tombstoned"
    errors, calls = item["errors"], item["calls"]
    # Errors alone are not rot: a tag that fails once and then works is a tag
    # that was called wrong. Rot is failing and never working.
    if errors >= BROKEN_ERRORS and errors == calls:
        return "broken"
    if calls == 0 and item["sessions_since"] >= UNUSED_SESSIONS:
        return "unused"
    return "keep"


def evidence_line(item: dict[str, Any]) -> str:
    """One line of usage evidence: what the record says this tool did."""
    last = item["last_used"][:10] if item["last_used"] else "never"
    return (
        f"{item['calls']} calls, {item['errors']} errors, last used {last},"
        f" ~{item['tokens']} catalog tokens/turn"
    )


def describe(world: World, name: str, doc: str) -> str:
    """A grown tool's doc plus its evidence row, for the describe path."""
    for item in census(world):
        if item["name"] == name:
            return f"<{name}> {doc}\nevidence: {evidence_line(item)} — {item['verdict']}"
    return f"<{name}> {doc}\nevidence: not on the record for this workspace."


def epitaph(world: World, tag: str) -> str | None:
    """One-line tombstone for a retired grown tag, or None if it never lived.

    Dispatch calls this only on its unknown-tag path, never on a hit, so the
    live catalog costs nothing and a dead tag answers instead of vanishing.
    """
    from desmos.state import persist

    if not world.persist:
        return None
    path = persist.state_file(world)
    if not path.is_file():
        return None
    conn = persist._open(path)
    try:
        workspace = persist._workspace_id(conn, world, create=False)
        if workspace is None:
            return None
        row = conn.execute(
            "SELECT tombstoned_at, tombstone_reason FROM tools"
            " WHERE workspace_id = ? AND name = ? AND frozen = 0"
            " AND tombstoned_at <> ''",
            (workspace, tag),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return (
        f"<{tag}> was retired {str(row['tombstoned_at'])[:10]}:"
        f" {row['tombstone_reason']}. Its source stays on the record;"
        f" harness op=refine revive={tag} restores it."
    )


def report(world: World) -> str:
    rows = census(world)
    if not rows:
        return "no grown tools in this workspace."
    lines = [f"grown tools: {len(rows)}"]
    for item in rows:
        detail = f"earning: {evidence_line(item)}" if item["calls"] else evidence_line(item)
        if item["verdict"] == "unused":
            detail = (
                f"never used across {item['sessions_since']} later sessions,"
                f" ~{item['tokens']} catalog tokens/turn wasted"
            )
        elif item["verdict"] == "broken":
            detail = evidence_line(item)
        elif item["verdict"] == "tombstoned":
            detail = f"tombstoned {item['tombstoned_at'][:10]}: {item['reason']}"
        lines.append(f"  {item['verdict']:<10} {item['name']:<16} {detail}")
    rot = [i["name"] for i in rows if i["verdict"] in ("broken", "unused")]
    if rot:
        lines.append(
            "candidates: " + ", ".join(rot) + " -- harness op=refine "
            "tombstone=NAME reason=why (the source is kept; revive=NAME undoes it)"
        )
    return "\n".join(lines)


def _set(world: World, name: str, at: str, reason: str) -> str | None:
    """Write the tombstone columns. None means there is no such grown tool."""
    from desmos.state import persist

    conn = persist._open(persist.state_file(world))
    try:
        with conn:
            workspace = persist._workspace_id(conn, world, create=False)
            if workspace is None:
                return None
            row = conn.execute(
                "SELECT source FROM tools WHERE workspace_id = ? AND name = ?"
                " AND frozen = 0",
                (workspace, name),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE tools SET tombstoned_at = ?, tombstone_reason = ?,"
                " updated_at = ? WHERE workspace_id = ? AND name = ?",
                (at, reason, datetime.now(timezone.utc).isoformat(), workspace, name),
            )
        return str(row["source"] or "")
    finally:
        conn.close()


def tombstone(world: World, name: str, reason: str = "") -> str:
    if name in getattr(world, "tools", {}) and world.tools[name].frozen:
        return f"{name} is a frozen tag, not grown: it cannot be tombstoned."
    at = datetime.now(timezone.utc).isoformat()
    source = _set(world, name, at, reason.strip() or "unspecified")
    if source is None:
        return f"no grown tool named {name!r} in this workspace."
    world.tools.pop(name, None)
    return (
        f"tombstoned <{name}>: {reason.strip() or 'unspecified'}. It is out of "
        "the catalog from the next request; its source is kept, revive=NAME "
        "puts it back."
    )


def revive(world: World, name: str) -> str:
    from desmos.state import persist

    source = _set(world, name, "", "")
    if source is None:
        return f"no grown tool named {name!r} in this workspace."
    doc = ""
    for item in census(world):
        if item["name"] == name:
            doc = item["doc"]
    world.tools[name] = persist.load_grown(world, name, doc or f"user tag <{name}>", source)
    return f"revived <{name}>: back in the catalog on the next request."


def handle_refine(world: World, body: str, attrs: dict[str, str]) -> str:
    """`harness op=refine` -- the census, or one tombstone/revive."""
    name = (attrs.get("tombstone") or "").strip()
    if name:
        return tombstone(world, name, (attrs.get("reason") or body or "").strip())
    name = (attrs.get("revive") or "").strip()
    if name:
        return revive(world, name)
    return report(world)
