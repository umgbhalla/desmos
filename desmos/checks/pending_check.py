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
        # Meta reads these to say whether anything will resume the session on
        # its own. Named, and emitted on both edges: a count with no names is
        # not actionable, and a set that only ever grows is a stuck row.
        pend = [e for e in events if e.get("ev") == "pending"]
        assert any(e["tasks"] and e["tasks"][0].startswith("sleep [") for e in pend), pend
        assert pend[-1]["tasks"] == [] and pend[-1]["n"] == 0, pend[-1]
        # One emit per change, not one per turn.
        assert [e["n"] for e in pend] == [1, 0], pend
        # Turn 2 said nothing and called nothing: without the resume the step
        # would have ended there.
        assert not turns, "the loop never resumed after the background task"
        assert out == "the task landed, so here is the outcome", out
        resumed = next(e for e in events if e.get("ev") == "resumed")
        assert "slept and woke up" in resumed["text"], resumed
        assert "background task finished" in resumed["text"], resumed
        assert elapsed >= 0.2, elapsed
        assert pending.count(world) == 0, pending.outstanding(world)

    # A steer that lands while the step is parked on background work is
    # delivered through the park's own drain, not the live turn loop -- and
    # the TUI badge only clears when the kernel echoes {"ev":"steer"}. The
    # echo is owned by delivery itself (deliver_steer), so this path must
    # emit it exactly once with the delivered text.
    with tempfile.TemporaryDirectory() as tmp:
        import threading

        from desmos.kernel.catalog import steer as queue_steer

        world = new_world(Path(tmp), state_path=None, persist=False, ns={})
        world.model = "claude-opus-5"
        pending.clear(world)

        def sleeper2(body: str, **_a: str) -> str:
            pending.submit(world, "sleep", lambda: (time.sleep(1.0), "slept")[1])
            return "scheduled"

        world.tools["sleeper"] = type(world.tools["python"])(
            name="sleeper", doc="sleep in the background", handler=sleeper2
        )
        turns = [
            response(f"{LT}sleeper>1.0{LT}/sleeper>"),
            response("nothing to wait for"),
            response("answering the steer"),
            response("task landed, done"),
            response("spare"),
        ]
        world.complete_fn = lambda _m, _s, _msgs, _x: turns.pop(0)
        events = []
        threading.Timer(0.3, lambda: queue_steer(world, "go left")).start()
        run_turns(world, "schedule it", quiet=True, on_event=events.append)
        steer_evs = [e for e in events if e.get("ev") == "steer"]
        assert len(steer_evs) == 1, steer_evs
        assert steer_evs[0]["text"] == "go left", steer_evs
        assert isinstance(steer_evs[0]["n"], int) and steer_evs[0]["n"] >= 1, steer_evs
        delivered = [
            m for m in world.messages
            if m.get("role") == "user" and m.get("content") == "[steer] go left"
        ]
        assert len(delivered) == 1, delivered
        pending.clear(world)

    # The live-turn path still echoes exactly once per steer: the emit moved
    # into deliver_steer, so the loop's drain must not double-echo.
    with tempfile.TemporaryDirectory() as tmp:
        from desmos.kernel.catalog import steer as queue_steer

        world = new_world(Path(tmp), state_path=None, persist=False, ns={})
        world.model = "claude-opus-5"
        pending.clear(world)
        turns = [
            response("first answer"),
            response("answered the steer"),
        ]

        def complete_live(_m: str, _s: str, _msgs: list[dict[str, Any]], _x: int) -> dict[str, Any]:
            if len(turns) == 2:
                queue_steer(world, "turn left")
            return turns.pop(0)

        world.complete_fn = complete_live
        events = []
        out = run_turns(world, "say something", quiet=True, on_event=events.append)
        steer_evs = [e for e in events if e.get("ev") == "steer"]
        assert steer_evs == [{"ev": "steer", "n": 1, "text": "turn left"}], steer_evs
        assert out == "answered the steer", out

    # A steer queued while the kernel is idle is delivered before turn one.
    with tempfile.TemporaryDirectory() as tmp:
        from desmos.kernel.catalog import steer as queue_steer

        world = new_world(Path(tmp), state_path=None, persist=False, ns={})
        world.model = "claude-opus-5"
        pending.clear(world)
        queue_steer(world, "idle steer")
        world.complete_fn = lambda _m, _s, _msgs, _x: response("one turn")
        events = []
        run_turns(
            world,
            "start the next step",
            max_turns=1,
            quiet=True,
            on_event=events.append,
        )
        steer_evs = [e for e in events if e.get("ev") == "steer"]
        assert steer_evs == [{"ev": "steer", "n": 0, "text": "idle steer"}], steer_evs
        delivered = [
            m for m in world.messages
            if m.get("role") == "user" and m.get("content") == "[steer] idle steer"
        ]
        assert len(delivered) == 1, delivered

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
