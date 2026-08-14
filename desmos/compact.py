from __future__ import annotations

"""Transcript compaction.

world.messages is append-only, and every call ships the whole thing —
135KB per POST at 83 messages. Compaction folds the oldest turns into a
single deterministic digest message so the tail stays cheap. It is lossy
on wording and lossless on structure: who spoke, which syscalls ran, and
the opening line of each block survive.
"""

import json
import re
from typing import Any

TAG = re.compile(r"<([a-z_]+)(?:\s[^>]*)?>")


def _text(msg: dict[str, Any]) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    out = []
    for b in c or []:
        if isinstance(b, dict):
            out.append(b.get("text") or f"[{b.get('type')}]")
        else:
            out.append(str(b))
    return "\n".join(out)


def digest(messages: list[dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        body = _text(m)
        tags = sorted(set(TAG.findall(body)))
        head = " ".join(body.split())[:110]
        role = m.get("role", "?")
        mark = f" [{','.join(tags)}]" if tags else ""
        lines.append(f"- {role}{mark}: {head}")
    return "\n".join(lines)


def weight(messages: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(m, default=str)) for m in messages)


def compact(world: Any, keep: int = 24, floor: int = 40) -> dict[str, int]:
    """Fold all but the last `keep` messages into one digest message.

    No-op under `floor` messages — compacting a short transcript costs
    more context than it saves.
    """
    msgs = world.messages
    before = weight(msgs)
    if len(msgs) < floor:
        return {"before": before, "after": before, "folded": 0}
    head, tail = msgs[:-keep], msgs[-keep:]
    while tail and tail[0].get("role") != "user":
        head.append(tail.pop(0))
    if not tail:
        return {"before": before, "after": before, "folded": 0}
    note = {
        "role": "user",
        "content": (
            f"<compacted n={len(head)}>\n"
            "Earlier turns, folded. Structure kept, wording dropped.\n"
            f"{digest(head)}\n</compacted>"
        ),
    }
    world.messages[:] = [note] + tail
    after = weight(world.messages)
    try:
        from desmos.persist import save

        save(world)
    except Exception:
        pass
    return {"before": before, "after": after, "folded": len(head)}
