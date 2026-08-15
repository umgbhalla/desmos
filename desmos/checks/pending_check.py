"""End-to-end check: a step returns, then resumes when background work lands."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

from desmos import pending
from desmos.loop import new_world, run_turns

LT = chr(60)


def response(text: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "stop_reason": "end_turn",
    }


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        world = new_world(Path(tmp), state_path=None, persist=False, ns={})
        world.model = "claude-opus-5"
        pending.clear(world)

        def sleeper(body: str, **_a: str) -> str:
            secs = float(body.strip() or "0.2")
            pending.submit(world, "sleep", lambda: (time.sleep(secs), "slept and woke up")[1])
            return f"scheduled; not waiting {secs}s"

        world.tools["sleeper"] = type(world.tools["python"])(
            name="sleeper", doc="sleep in the background", handler=sleeper
        )

        seen: list[str] = []
        turns = [
            response(f"{LT}sleeper>0.2{LT}/sleeper>"),
            response("scheduled it; nothing to wait for"),
            response("the task landed, so here is the outcome"),
        ]

        def complete(_model: str, _system: str, messages: list[dict[str, Any]], _max: int) -> dict[str, Any]:
            last = messages[-1]
            content = last.get("content")
            seen.append(content if isinstance(content, str) else str(content))
            return turns.pop(0)

        world.complete_fn = complete
        events: list[dict[str, Any]] = []
        started = time.monotonic()
        out = run_turns(world, "schedule it", quiet=True, on_event=events.append)
        elapsed = time.monotonic() - started

        kinds = [e.get("ev") for e in events]
        assert "pending" in kinds, kinds
        assert "resumed" in kinds, kinds
        # Turn 2 said nothing and called nothing: without the resume the step
        # would have ended there.
        assert not turns, "the loop never resumed after the background task"
        assert out == "the task landed, so here is the outcome", out
        resumed = next(e for e in events if e.get("ev") == "resumed")
        assert "slept and woke up" in resumed["text"], resumed
        assert "background task finished" in resumed["text"], resumed
        assert elapsed >= 0.2, elapsed
        assert pending.count(world) == 0, pending.outstanding(world)

    # A queued follow-up outranks background work: the wait gives the turn back.
    with tempfile.TemporaryDirectory() as tmp:
        world = new_world(Path(tmp), state_path=None, persist=False, ns={})
        pending.clear(world)
        pending.submit(world, "slow", lambda: (time.sleep(5), "too late")[1])
        landed = pending.wait_next(world, interrupt=lambda: True)
        assert landed == [], landed
        assert pending.count(world) == 1, pending.outstanding(world)
        pending.clear(world)

    print("pending resume check ok")


if __name__ == "__main__":
    self_check()
