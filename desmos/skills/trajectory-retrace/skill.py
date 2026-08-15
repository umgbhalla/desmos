from __future__ import annotations

import re
from collections import Counter
from typing import Any

_GENERATED_PREFIXES = (
    "execute this typed task contract.",
    "task guidance reminder:",
    "[background task finished",
    "subagent group settled:",
    "<result",
)
_PRIOR_USER = re.compile(r"(?m)^\s+\d+\. user: (.*)$")


def _blocks_text(content: Any, *, request: bool = False) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return content, ["str"]
    if not isinstance(content, list):
        return "", [type(content).__name__]
    parts: list[str] = []
    types: list[str] = []
    allowed = {"text", "input_text"} if request else {"text"}
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type", "?"))
        types.append(kind)
        if kind in allowed:
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts), types


def _collapse_transport_repeat(text: str) -> str:
    words = text.split()
    if len(words) >= 2 and len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] == words[half:]:
            return " ".join(words[:half])
    return " ".join(words)


def _generated(text: str) -> str | None:
    low = text.strip().lower()
    for prefix in _GENERATED_PREFIXES:
        if low.startswith(prefix):
            return prefix.rstrip(":.[")
    return None


def _from_envelope(text: str) -> list[str]:
    found = [m.group(1).strip() for m in _PRIOR_USER.finditer(text)]
    if "\nprompt:" in text:
        found.append(text.rsplit("\nprompt:", 1)[-1].strip())
    return [x for x in found if x]


def _request_user_texts(request: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("input", "messages"):
        rows = request.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            text, _ = _blocks_text(item.get("content", ""), request=True)
            if text.strip():
                out.append(text)
    return out


def run(world: Any, *, max_items: int = 250) -> dict[str, Any]:
    """Recover genuine human prompts from a Desmos World without trusting role."""
    excluded: Counter[str] = Counter()
    records: dict[str, dict[str, Any]] = {}

    def add(text: str, source: str) -> None:
        text = text.strip()
        if not text:
            excluded["empty_user_shape"] += 1
            return
        why = _generated(text)
        if why is not None:
            excluded["generated_" + why.replace(" ", "_")] += 1
            return
        candidates = _from_envelope(text) if text.startswith("generation:") else [text]
        for candidate in candidates:
            normalized = _collapse_transport_repeat(candidate.strip())
            if not normalized:
                continue
            why = _generated(normalized)
            if why is not None:
                excluded["generated_" + why.replace(" ", "_")] += 1
                continue
            key = normalized.casefold()
            row = records.setdefault(
                key,
                {"text": normalized, "sources": [], "first_seen": len(records)},
            )
            if source not in row["sources"]:
                row["sources"].append(source)

    for idx, message in enumerate(getattr(world, "messages", []) or []):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        text, types = _blocks_text(content)
        type_set = set(types)
        if type_set & {"custom_tool_call_output", "tool_result"}:
            excluded["tool_result"] += 1
            continue
        if not text.strip() and isinstance(content, str):
            text = content
        if not text.strip():
            excluded["non_text_user_shape"] += 1
            continue
        add(text, f"messages:{idx}")

    for idx, entry in enumerate(getattr(world, "log", []) or []):
        if not isinstance(entry, dict):
            continue
        request = entry.get("request")
        if not isinstance(request, dict):
            continue
        for text in _request_user_texts(request):
            add(text, f"log:{idx}")

    ordered = sorted(records.values(), key=lambda row: row["first_seen"])
    truncated = len(ordered) > max_items
    ordered = ordered[:max_items]
    for row in ordered:
        row.pop("first_seen", None)

    return {
        "prompts": ordered,
        "human_prompt_count": len(records),
        "excluded": dict(sorted(excluded.items())),
        "truncated": truncated,
        "limitations": [
            "Only messages, persisted request bodies, and chained prior-step summaries still visible to this World can be recovered.",
            "Server-compacted verbatim wording may survive only as a truncated prior-step summary.",
            "The helper does not decide completion; verify each request against commits, tests, todos, upstream state, and runtime freshness.",
        ],
    }


__all__ = ["run"]
