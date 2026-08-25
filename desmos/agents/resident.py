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
        # A fresh world starts on the harness default model, which is not
        # necessarily one this machine can call: the resident's first answer
        # was an empty string because the daemon tried Anthropic on a box that
        # only holds an OpenAI credential. Take the machine's own choice.
        try:
            from desmos.transport.settings import resolve, switch

            chosen = resolve()
            switch(got, chosen.model, chosen.effort)
        except Exception:  # noqa: BLE001 -- a default model is still a model
            pass
        _WORLDS[key] = got
    return got


def _last_text(resident: Any) -> str:
    """The last thing the resident said, for when the step returns nothing."""
    for message in reversed(list(getattr(resident, "messages", []))):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") in (None, "text")
            ]
            joined = "\n".join(part for part in parts if part).strip()
            if joined:
                return joined
    return ""


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
        out = str(resident.ns["step"](prompt)).strip()
        if not out:
            # Silence in a channel is indistinguishable from a machine that
            # is gone. A turn that failed said so in the transcript and
            # nowhere else; say it where it was asked.
            out = _last_text(resident) or "(no reply -- the resident's turn produced nothing)"
    return out[:REPLY_CAP]
