"""Remote subagents: dispatch work to another machine over the spine.

One protocol, three message shapes on the reserved sys.work channel:

  {"t": "request", "work_id", "target", "agent", "task", "origin"}
  {"t": "claim",   "work_id", "host"}
  {"t": "result",  "work_id", "host", "status", "output"}

The DO's per-channel total order arbitrates claims. Execution is the
target daemon's job (desmos.front.spine._serve_work); this module is the
requester half: validate the target against live presence, post the
request to sys.work, and park a pending task that resumes the step when
the result message lands in channel_messages.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from desmos.state.persist import _open, state_file

RESULT_CAP = 4000
POLL_S = 1.0
DEFAULT_TIMEOUT_S = 3600.0
STALE_PRESENCE_S = 1800.0


def _seat() -> str:
    from desmos.front.spine import _seat as seat

    return seat()


def known_hosts(world: Any) -> set[str]:
    """Hosts with presence fresh enough to trust (seen within 30 minutes)."""
    from datetime import datetime, timezone

    db = _open(state_file(world))
    try:
        try:
            rows = db.execute(
                "SELECT host, MAX(seen_at) AS seen FROM spine_peers"
                " GROUP BY host"
            ).fetchall()
        except Exception:
            return set()
    finally:
        db.close()
    now = datetime.now(timezone.utc)
    live: set[str] = set()
    for r in rows:
        seen = str(r["seen"] or "")
        try:
            at = datetime.fromisoformat(seen.replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if (now - at).total_seconds() > STALE_PRESENCE_S:
                continue
        except ValueError:
            pass  # unparseable timestamps stay trusted rather than vanish
        live.add(str(r["host"]))
    return live


def find_result(world: Any, work_id: str) -> dict[str, Any] | None:
    from desmos.front.spine import SYS_WORK

    db = _open(state_file(world))
    try:
        try:
            rows = db.execute(
                "SELECT body FROM channel_messages WHERE channel = ?"
                " AND body LIKE ? ORDER BY id",
                (SYS_WORK, f'%{work_id}%'),
            ).fetchall()
        except Exception:
            return None
    finally:
        db.close()
    for row in rows:
        try:
            payload = json.loads(str(row["body"]))
        except ValueError:
            continue
        if (isinstance(payload, dict)
                and str(payload.get("t", "")) == "result"
                and str(payload.get("work_id", "")) == work_id):
            return payload
    return None


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
        " it may still land later on sys.work"
    )


def request(
    world: Any,
    host: str,
    task: str,
    agent: str = "general",
    timeout: float = DEFAULT_TIMEOUT_S,
    reply_channel: str = "",
    reply_as: str = "",
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
    from desmos.front.spine import post_work

    post_work(world, {
        "t": "request", "work_id": work_id, "target": host,
        "agent": agent or "general", "task": task, "origin": seat,
    })
    from desmos.agents import pending

    def _finish() -> str:
        out = await_result(world, work_id, timeout)
        if reply_channel:
            # The asker posted in a channel, so the bot answers there too.
            from desmos.state.persist import channel_post

            try:
                channel_post(world, out, channel=reply_channel,
                             author=reply_as or host)
            except ValueError:
                pass
        return out

    pending.submit(world, f"remote {work_id} -> {host}", _finish)
    # Say where the answer will surface. "(general)" is the agent kind, and
    # read in a channel it looked like the name of the channel the reply was
    # going to -- which was never true: the bot answers where it was asked.
    where = (
        f" the answer lands in #{reply_channel}"
        if reply_channel
        else " the result resumes this step as a background task"
    )
    return f"remote work {work_id} dispatched to {host} ({agent or 'general'});{where}"


def mention_dispatch(world: Any, channel: str, body: str) -> list[str]:
    """@bot mentions in a channel post become remote work on the bot's host.

    Returns one dispatch note per live bot mentioned; unknown names and sys
    channels are left alone. The bot's answer is posted back to the channel
    by the pending task that awaits it.
    """
    import re

    if channel.startswith("sys."):
        return []
    names = list(dict.fromkeys(re.findall(r"@([\w.-]+)", body)))
    if not names:
        return []
    db = _open(state_file(world))
    try:
        try:
            rows = db.execute(
                "SELECT name, host FROM agents"
                " WHERE kind = 'bot' AND status = 'active'",
            ).fetchall()
        except Exception:
            return []
    finally:
        db.close()
    bots = {str(r["name"]): str(r["host"]) or str(r["name"]) for r in rows}
    notes = []
    for name in names:
        host = bots.get(name)
        if not host:
            continue
        task = re.sub(rf"@{re.escape(name)}\b", "", body).strip()
        if not task:
            continue
        notes.append(request(
            world, host, task,
            reply_channel=channel, reply_as=name,
        ))
    return notes
