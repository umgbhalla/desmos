"""Remote subagents: dispatch work to another machine over the spine.

One protocol, three message shapes on the reserved sys.work channel:

  {"t": "request", "work_id", "target", "agent", "task", "origin"}
  {"t": "claim",   "work_id", "host"}
  {"t": "result",  "work_id", "host", "status", "output"}

The DO's per-channel total order arbitrates claims. Execution is the
target daemon's job (desmos.front.spine._serve_work); this module is the
requester half: validate the target against live presence, enqueue the
request, and park a pending task that resumes the step when the result
row lands in spine_work.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from desmos.state import outbox
from desmos.state.persist import _open, state_file

RESULT_CAP = 4000
POLL_S = 1.0
DEFAULT_TIMEOUT_S = 3600.0


def _seat() -> str:
    from desmos.front.spine import _seat as seat

    return seat()


def known_hosts(world: Any) -> set[str]:
    db = _open(state_file(world))
    try:
        try:
            rows = db.execute("SELECT DISTINCT host FROM spine_peers").fetchall()
        except Exception:
            return set()
        return {str(r["host"]) for r in rows}
    finally:
        db.close()


def find_result(world: Any, work_id: str) -> dict[str, Any] | None:
    db = _open(state_file(world))
    try:
        try:
            row = db.execute(
                "SELECT body FROM spine_work WHERE work_id = ? AND t = 'result'",
                (work_id,),
            ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        try:
            return json.loads(row["body"])
        except ValueError:
            return None
    finally:
        db.close()


def await_result(
    world: Any, work_id: str, timeout: float = DEFAULT_TIMEOUT_S
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = find_result(world, work_id)
        if payload is not None:
            status = str(payload.get("status", "done"))
            host = str(payload.get("host", "?"))
            output = str(payload.get("output", ""))
            return f"remote work {work_id} on {host} [{status}]\n{output}"
        time.sleep(POLL_S)
    return (
        f"remote work {work_id}: no result within {int(timeout)}s;"
        " it may still land later in spine_work"
    )


def request(
    world: Any,
    host: str,
    task: str,
    agent: str = "general",
    timeout: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Dispatch one task to a live remote host; the reply is a pending task."""
    host = host.strip()
    task = task.strip()
    if not task:
        return "remote spawn: missing task"
    seat = _seat()
    if host == seat:
        return "remote spawn: host is this machine; spawn locally instead"
    hosts = known_hosts(world)
    if host not in hosts:
        return (
            f"remote spawn refused: no live presence for host '{host}'"
            f" (known: {sorted(hosts) or 'none'})"
        )
    work_id = f"w-{uuid.uuid4().hex[:12]}"
    outbox.enqueue(world, "work", {
        "t": "request", "work_id": work_id, "target": host,
        "agent": agent or "general", "task": task, "origin": seat,
    })
    from desmos.agents import pending

    pending.submit(
        world, f"remote {work_id} -> {host}",
        lambda: await_result(world, work_id, timeout),
    )
    return (
        f"remote work {work_id} dispatched to {host} ({agent or 'general'});"
        " the result resumes this step as a background task"
    )
