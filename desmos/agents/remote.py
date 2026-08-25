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


def asker_name() -> str:
    """What a resident agent elsewhere should call whoever is typing here.

    The seat id is a MAC address; being greeted by one reads like being
    addressed by a machine. Settings hold a name when the human gave one.
    """
    try:
        from desmos.transport.settings import resolve

        name = (resolve().user or "").strip()
    except Exception:  # noqa: BLE001 -- a missing name is not an error
        name = ""
    return name or "main"


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


def await_output(
    world: Any, work_id: str, timeout: float = DEFAULT_TIMEOUT_S
) -> tuple[str, str, str]:
    """Wait for one work id: (status, host, output).

    Split out from await_result because a channel reply is the answer itself.
    Prefixing it with "remote work w-... [done]" made a resident sound like a
    job report, which is the thing it is not.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = find_result(world, work_id)
        if payload is not None:
            return (
                str(payload.get("status", "done")),
                str(payload.get("host", "?")),
                str(payload.get("output", "")),
            )
        time.sleep(POLL_S)
    return (
        "timeout", "?",
        f"no result within {int(timeout)}s; it may still land later on sys.work",
    )


def await_result(
    world: Any, work_id: str, timeout: float = DEFAULT_TIMEOUT_S
) -> str:
    status, host, output = await_output(world, work_id, timeout)
    if status == "timeout":
        return f"remote work {work_id}: {output}"
    return f"remote work {work_id} on {host} [{status}]\n{output}"


def request(
    world: Any,
    host: str,
    task: str,
    agent: str = "general",
    timeout: float = DEFAULT_TIMEOUT_S,
    reply_channel: str = "",
    reply_as: str = "",
    asker: str = "",
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
        # A mention is a conversation, so the far side answers as its resident
        # agent -- one long-lived world with a transcript -- rather than as a
        # fresh contract-bound child that forgets the exchange.
        "reply_channel": reply_channel, "resident": bool(reply_channel),
        # Who is talking, as a channel reads it. The seat id is a machine
        # fingerprint -- the resident was being addressed by
        # "Unknown_e2:03:31:61:8c:9b", which is nobody.
        "asker": asker or "",
    })
    from desmos.agents import pending

    def _finish() -> str:
        status, from_host, output = await_output(world, work_id, timeout)
        note = (
            f"remote work {work_id}: {output}"
            if status == "timeout"
            else f"remote work {work_id} on {from_host} [{status}]\n{output}"
        )
        if reply_channel:
            # The channel gets the answer, not the accounting. The work id and
            # the status are for whoever dispatched it; the person reading the
            # channel wants the sentence.
            from desmos.state.persist import channel_post

            try:
                said = output.strip() or f"(no answer -- {status})"
                channel_post(world, said, channel=reply_channel,
                             author=reply_as or from_host)
            except ValueError:
                pass
        return note

    # A channel reply needs no notice: the answer is posted to the channel by
    # the task itself, so waking the chief agent to read "[done]" would spend
    # a turn on somebody else's conversation.
    pending.submit(world, f"remote {work_id} -> {host}", _finish,
                   quiet=bool(reply_channel))
    if reply_channel:
        # A mention is a conversation. The reply lands in this same channel in
        # a moment, so the note says who is answering and nothing about work
        # ids or agent kinds.
        return f"{reply_as or host} is thinking..."
    return (
        f"remote work {work_id} dispatched to {host} ({agent or 'general'});"
        " the result resumes this step as a background task"
    )


def mention_dispatch(world: Any, channel: str, body: str, asker: str = "") -> list[str]:
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
            reply_channel=channel, reply_as=name, asker=asker,
        ))
    return notes
