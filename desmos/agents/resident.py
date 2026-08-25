"""The resident agent: one long-lived world per host, addressed in a channel.

A mention used to become a subagent run -- fresh world, contract, report --
which is the right shape for "audit these forty files" and the wrong one for
"hi". A child is told to prove itself, so a greeting came back as `pwd`, a
`git status` and a Summary/Evidence/Checks block, and nothing it learned
survived the reply.

A resident is the other thing: the same world every time, its transcript kept
on disk beside the harness state, answering the way someone in a channel
answers. It runs the same kernel the chief does -- every syscall, its own
notes and memory -- which is what makes "and now do the other one" mean
something.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

# One resident per process, and one message at a time: two mentions arriving
# together must not interleave turns in a single transcript.
_LOCK = threading.Lock()
_WORLDS: dict[str, Any] = {}

REPLY_CAP = 4000


def resident_world(world: Any) -> Any:
    """This host's resident world, created once and then kept."""
    key = str(world.cwd)
    got = _WORLDS.get(key)
    if got is None:
        from desmos.kernel.loop import bind_step, new_world
        from desmos.state.persist import state_file

        path = Path(state_file(world)).parent / "resident.sqlite3"
        got = new_world(cwd=Path(world.cwd), state_path=path)
        bind_step(got)
        _WORLDS[key] = got
    return got


def respond(world: Any, task: str, *, asker: str = "", channel: str = "") -> str:
    """Answer one channel message as the resident; returns what to post."""
    text = str(task or "").strip()
    if not text:
        return ""
    who = str(asker or "").strip() or "someone"
    where = f"#{channel}" if channel else "the channel"
    prompt = (
        f"{who} says in {where}:\n\n{text}\n\n"
        "You are the resident agent on this machine, and this is a"
        " conversation rather than a work order. Answer the way you would"
        " speak: no report headings, no evidence section, no restating the"
        " question. Reach for a syscall only when the answer actually needs"
        " one -- a greeting needs none. What you say next is what lands in"
        " the channel, so say it and stop."
    )
    with _LOCK:
        resident = resident_world(world)
        out = resident.ns["step"](prompt)
    return str(out).strip()[:REPLY_CAP]
