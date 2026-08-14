"""JSONL stdio bridge for the grok-minimal TUI."""

from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any

from desmos.catalog import ns_names
from desmos.loop import new_world, reload, reload_sdk, reset_transcript, run_turns


def _snapshot(world: Any) -> dict[str, Any]:
    return {
        "ev": "snapshot",
        "model": world.model,
        "thinking": world.thinking,
        "generation": world.generation,
        "cwd": str(world.cwd),
        "ns": ns_names(world),
        "tools": sorted(world.tools),
    }


# The wire handle, bound once at import.
#
# `sys.stdout` is NOT the wire during a <python> syscall: run_python swaps it
# for exec._ChunkWriter so prints stream into the Execute card. A dynamic
# `sys.stdout` lookup here writes each event back into that writer, whose
# write() calls on_chunk -> fire -> _emit again — an exponential self-feed that
# wedges the bridge on any <python> that prints. Write to the real handle.
_WIRE = sys.stdout


def _emit(ev: dict[str, Any]) -> None:
    _WIRE.write(json.dumps(ev, default=str) + "\n")
    _WIRE.flush()


def serve(cwd: Path) -> int:
    world = new_world(cwd)
    import desmos.subagent as S

    S.bind(world)
    S.set_emitter(_emit)
    cancel = threading.Event()
    inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def reader() -> None:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError as exc:
                _emit({"ev": "error", "text": f"bad json: {exc}"})
                continue
            if not isinstance(msg, dict):
                _emit({"ev": "error", "text": "bad json: not an object"})
                continue
            op = msg.get("op")
            if op == "stop":
                cancel.set()
                continue
            if op == "quit":
                cancel.set()
                inbox.put(None)
                return
            inbox.put(msg)
        inbox.put(None)

    threading.Thread(target=reader, daemon=True).start()
    _emit({"ev": "ready", **{k: v for k, v in _snapshot(world).items() if k != "ev"}})
    while True:
        msg = inbox.get()
        if msg is None:
            return 0
        op = msg.get("op")
        try:
            if op == "step":
                text = str(msg.get("text") or "")
                if not text.strip():
                    _emit({"ev": "error", "text": "empty prompt"})
                    continue
                cancel.clear()
                # run_turns emits the terminator itself, on every path.
                run_turns(world, text, quiet=True, on_event=_emit, should_stop=cancel.is_set)
                _emit(_snapshot(world))
            elif op == "snapshot":
                _emit(_snapshot(world))
            elif op == "reset":
                _emit({"ev": "speech", "text": reset_transcript(world)})
                _emit(_snapshot(world))
            elif op == "reload":
                _emit({"ev": "speech", "text": reload_sdk(world)})
                _emit({"ev": "speech", "text": reload(world)})
                _emit(_snapshot(world))
            elif op == "thinking":
                level = str(msg.get("level") or "low").strip()
                world.thinking = level
                _emit(_snapshot(world))
            else:
                _emit({"ev": "error", "text": f"unknown op {op!r}"})
        except Exception as exc:  # noqa: BLE001 — keep the TUI alive
            _emit({"ev": "error", "text": f"{type(exc).__name__}: {exc}"})
