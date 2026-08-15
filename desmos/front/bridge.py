"""JSONL stdio bridge for the grok-minimal TUI."""

from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any

from desmos.kernel.catalog import ns_names
from desmos.kernel.loop import new_world, reload, reload_sdk, reset_transcript, run_turns


def _billing(model: str) -> str:
    """A ChatGPT/Codex OAuth token bills a subscription, not tokens."""
    from desmos.transport.auth import openai_credential
    from desmos.transport.settings import provider_of

    if provider_of(model) != "openai":
        return "usage"
    try:
        cred = openai_credential(allow_refresh=False)
    except Exception:  # noqa: BLE001
        return "usage"
    return "plan" if cred is not None and cred.kind == "oauth" else "usage"


def _snapshot(world: Any) -> dict[str, Any]:
    from desmos.transport.settings import provider_of

    return {
        "ev": "snapshot",
        "model": world.model,
        "provider": provider_of(world.model),
        "billing": _billing(world.model),
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

# One NDJSON line per event, and a line only means anything whole. Subagents
# run on their own threads and every one of them reaches this function through
# child_event, so the main loop and up to a poolful of children write here at
# once. TextIOWrapper.write is not documented atomic; two interleaved writes
# are one corrupt line, which the TUI's parser drops -- taking a real event
# with it. Serialize the pair.
_WIRE_LOCK = threading.Lock()


def _emit(ev: dict[str, Any]) -> None:
    line = json.dumps(ev, default=str) + "\n"
    with _WIRE_LOCK:
        _WIRE.write(line)
        _WIRE.flush()


def serve(cwd: Path) -> int:
    world = new_world(cwd)
    from desmos.transport.settings import load as _load_settings

    saved = _load_settings()
    if saved is not None:
        # A saved choice outranks whatever the last session persisted; it is the
        # one the user made on purpose.
        world.model, world.thinking = saved.model, saved.effort
    import desmos.agents.subagent as S

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
    from desmos.transport.settings import picker as _picker

    _emit({
        "ev": "ready",
        **{k: v for k, v in _snapshot(world).items() if k != "ev"},
        **_picker(),
    })
    while True:
        msg = inbox.get()
        if msg is None:
            return 0
        op = msg.get("op")
        try:
            if op == "step":
                text = str(msg.get("text") or "")
                # The composer sends the paths of its image chips alongside the
                # line. They were being dropped here: the pane said "attached
                # 2 image(s)" and the model got the file names as prose.
                raw = msg.get("images") or []
                images = [str(p) for p in raw if str(p).strip()]
                if not text.strip():
                    _emit({"ev": "error", "text": "empty prompt"})
                    continue
                cancel.clear()
                # run_turns emits the terminator itself, on every path.
                # A queued follow-up outranks background work: if the user has
                # already typed the next thing, stop waiting for a task to land
                # and give the turn back.
                run_turns(
                    world,
                    text,
                    quiet=True,
                    on_event=_emit,
                    should_stop=cancel.is_set,
                    has_input=lambda: not inbox.empty(),
                    images=images,
                )
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
            elif op == "model":
                from desmos.transport import settings as _settings

                model = str(msg.get("model") or world.model)
                asked = str(msg.get("effort") or world.thinking)
                # The two ladders are different lengths -- OpenAI has medium and
                # max, Anthropic does not -- so an effort that is fine on the
                # provider being left may not exist on the one being joined. A
                # session on sol at medium could not move to Opus at all: the
                # switch was refused as "unknown model/effort". The model is
                # what was asked for; the effort bends to fit it.
                provider = _settings.provider_of(model)
                effort = _settings.clamp_effort(provider, asked)
                if effort != asked:
                    _emit(
                        {
                            "ev": "notice",
                            "text": f"{provider} has no {asked} effort; using {effort}",
                        }
                    )
                was = _settings.provider_of(str(world.model or ""))
                try:
                    notice = _settings.switch(world, model, effort)
                except ValueError as exc:
                    _emit({"ev": "error", "text": str(exc)})
                    continue
                _emit(_snapshot(world))
                # A provider change drops the old provider's thinking from every
                # later request. Say so; silence reads as the harness losing
                # reasoning for no reason. A same-provider switch needs no line.
                if was != _settings.provider_of(str(world.model or "")):
                    _emit({"ev": "notice", "text": notice})
            elif op == "picker":
                from desmos.transport.settings import picker

                _emit({"ev": "picker", **picker()})
            elif op == "login":
                from desmos.transport import auth as _auth
                from desmos.transport.settings import picker

                method = str(msg.get("method") or "auto")

                def do_login(method: str = method) -> None:
                    # Blocking, and it waits on a human. Off the inbox thread so
                    # the TUI keeps painting; progress lines are the only way the
                    # user learns which URL to open.
                    try:
                        cred = _auth.login_openai(
                            notify=lambda t: _emit({"ev": "login", "text": t}), method=method
                        )
                        _emit({"ev": "login", "text": f"signed in {cred.masked()}", "done": True})
                    except Exception as exc:  # noqa: BLE001
                        _emit({"ev": "login", "text": f"{type(exc).__name__}: {exc}", "failed": True})
                    _emit({"ev": "picker", **picker()})

                threading.Thread(target=do_login, daemon=True).start()
            elif op == "thinking":
                level = str(msg.get("level") or "low").strip()
                world.thinking = level
                _emit(_snapshot(world))
            else:
                _emit({"ev": "error", "text": f"unknown op {op!r}"})
        except Exception as exc:  # noqa: BLE001 — keep the TUI alive
            _emit({"ev": "error", "text": f"{type(exc).__name__}: {exc}"})
