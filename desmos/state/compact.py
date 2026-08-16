from __future__ import annotations
import json
import re
from typing import Any

_TAG = re.compile(r"<([a-z_]+)(?:\\s[^>]*)?>")

def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\\n".join(
        block.get("text") or f"[{block.get('type')}]" if isinstance(block, dict) else str(block)
        for block in content or []
    )

def _turn_start(message: dict[str, Any]) -> bool:
    return message.get("role") == "user" and "<result" not in _text(message)

def _weight(messages: list[dict[str, Any]]) -> int:
    return sum(len(json.dumps(message, default=str)) for message in messages)

def compact(world: Any, keep: int = 24, floor: int = 40) -> dict[str, int]:
    messages = world.messages
    before = _weight(messages)
    if len(messages) < floor:
        return {"before": before, "after": before, "folded": 0}
    head, tail = messages[:-keep], messages[-keep:]
    while tail and not _turn_start(tail[0]):
        head.append(tail.pop(0))
    if not tail:
        return {"before": before, "after": before, "folded": 0}
    rows = []
    for message in head:
        text = _text(message)
        tags = sorted(set(_TAG.findall(text)))
        mark = f" [{','.join(tags)}]" if tags else ""
        rows.append(f"- {message.get('role', '?')}{mark}: {' '.join(text.split())[:110]}")
    note = {"role": "user", "content": f"<compacted n={len(head)}>\\nEarlier turns, folded.\\n" + "\\n".join(rows) + "\\n</compacted>"}
    messages[:] = [note, *tail]
    from desmos.state.persist import save
    save(world)
    return {"before": before, "after": _weight(messages), "folded": len(head)}
