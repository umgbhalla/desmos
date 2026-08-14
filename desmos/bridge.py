"""JSONL stdio bridge for the grok-minimal TUI."""

from __future__ import annotations

import json
import sys
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


def _emit(ev: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(ev, default=str) + "\n")
    sys.stdout.flush()


def serve(cwd: Path) -> int:
    world = new_world(cwd)
    _emit({"ev": "ready", **{k: v for k, v in _snapshot(world).items() if k != "ev"}})
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as exc:
            _emit({"ev": "error", "text": f"bad json: {exc}"})
            continue
        op = msg.get("op")
        try:
            if op == "step":
                text = str(msg.get("text") or "")
                if not text.strip():
                    _emit({"ev": "error", "text": "empty prompt"})
                    continue
                run_turns(world, text, quiet=True, on_event=_emit)
                _emit({"ev": "done"})
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
            elif op == "quit":
                return 0
            else:
                _emit({"ev": "error", "text": f"unknown op {op!r}"})
        except Exception as exc:  # noqa: BLE001 — keep the TUI alive
            _emit({"ev": "error", "text": f"{type(exc).__name__}: {exc}"})
    return 0
