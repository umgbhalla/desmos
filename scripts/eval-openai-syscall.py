#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desmos.complete import text_of  # noqa: E402
from desmos.loop import new_world, run_turns  # noqa: E402
from desmos.scan import scan  # noqa: E402


MODELS = ("gpt-5.6-luna", "gpt-5.6-sol")
PROMPT = """This is a syscall boundary evaluation. Do not edit files.
On the first work turn, call syscall with exactly one <python> tag that prints 21 * 2.
After its result, call syscall again with exactly one <python> tag that prints 6 * 7.
After the second result, reply with exactly EVAL_DONE and make no tool call."""


def evaluate(model: str) -> None:
    world = new_world(ROOT, persist=False)
    world.model = model
    world.thinking = "medium"
    events: list[dict] = []
    answer = run_turns(
        world,
        PROMPT,
        max_turns=4,
        max_total_tokens=20_000,
        quiet=True,
        on_event=events.append,
    )
    calls = [
        block
        for message in world.messages
        if message.get("role") == "assistant" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "custom_tool_call"
    ]
    outputs = [
        block
        for message in world.messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "custom_tool_call_output"
    ]
    assert len(calls) == 2, f"{model}: expected 2 syscall calls, got {len(calls)}"
    assert len(outputs) == 2, f"{model}: expected 2 typed outputs, got {len(outputs)}"
    assert [c["call_id"] for c in calls] == [o["call_id"] for o in outputs]
    for call in calls:
        raw = call["input"].strip()
        assert raw.startswith("<python>") and raw.endswith("</python>"), raw
        assert len(scan(raw)) == 1, raw
    assert all(">42<" in output["output"] for output in outputs), outputs
    assert answer.strip() == "EVAL_DONE", f"{model}: final speech was {answer!r}"
    assert not [event for event in events if event.get("ev") == "error"], events
    assert "lousy?" not in "".join(
        text_of({"content": message.get("content") or []})
        for message in world.messages
        if message.get("role") == "assistant"
    )
    print(f"PASS {model}: 2 typed calls, 2 matched outputs, clean final")


if __name__ == "__main__":
    for candidate in MODELS:
        evaluate(candidate)
