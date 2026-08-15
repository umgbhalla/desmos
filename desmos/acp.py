"""ACP (Agent Client Protocol) stdio server for grok-build's pager.

Newline-delimited JSON-RPC 2.0 — one object per line, flushed after every write.
Not LSP Content-Length framing.
"""

from __future__ import annotations

import itertools
import json
import queue
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, IO, Iterator, TextIO

from desmos.const import DEFAULT_MODEL
from desmos.loop import new_world, run_turns

PROTOCOL_VERSION = 1


class _TurnFailed(Exception):
    """The step ended on an error event. A stopReason would report it as an answer."""


def initialize_result() -> dict[str, Any]:
    model = DEFAULT_MODEL
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "agentCapabilities": {
            "loadSession": False,
            # run_turns takes a prompt string, so an image block has nowhere to
            # go; prompt_text used to drop it and the empty prompt answered
            # end_turn with no model call. Advertise what this agent carries.
            "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
        },
        "authMethods": [{"id": "none", "name": "none"}],
        "_meta": {
            "grokShell": False,
            "cancelRewind": False,
            "sessionRecap": False,
            "availableCommands": [],
            "modelState": {
                "currentModelId": model,
                "availableModels": [{"modelId": model, "name": model}],
            },
        },
    }


def prompt_text(blocks: Any) -> str:
    """Concatenate ACP text (and embedded resource text) into one user prompt."""
    if isinstance(blocks, str):
        return blocks
    parts: list[str] = []
    for raw in blocks or []:
        if isinstance(raw, str):
            if raw:
                parts.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "text" or (kind is None and "text" in raw):
            text = raw.get("text") or ""
            if text:
                parts.append(str(text))
        elif kind == "resource":
            resource = raw.get("resource") or {}
            if isinstance(resource, dict):
                text = resource.get("text") or ""
                if text:
                    parts.append(str(text))
    return "".join(parts)


def rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _meta_prompt_id(params: dict[str, Any]) -> str | None:
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    value = meta.get("promptId")
    if value is None:
        return None
    text = str(value)
    return text or None


def _writer(stdout: IO[str]) -> Callable[[dict[str, Any]], None]:
    lock = threading.Lock()

    def write(obj: dict[str, Any]) -> None:
        stdout.write(json.dumps(obj, default=str) + "\n")
        stdout.flush()

    def locked(obj: dict[str, Any]) -> None:
        with lock:
            write(obj)

    return locked


class AcpServer:
    """In-memory ACP agent. `handle` is the testable request entry point."""

    def __init__(
        self,
        write: Callable[[dict[str, Any]], None],
        *,
        default_cwd: Path | None = None,
    ) -> None:
        self.write = write
        self.default_cwd = Path(default_cwd or Path.cwd()).resolve()
        self.sessions: dict[str, Any] = {}
        # One brain per workspace: persist keys its rows off the cwd, so two
        # Worlds on one directory take turns overwriting each other's ns, notes
        # and tools. Sessions on the same cwd share the world instead. Value is
        # (world, messages-as-loaded, prior-as-loaded).
        self._worlds: dict[Path, tuple[Any, list[dict[str, Any]], list[dict[str, str]]]] = {}
        # The transcript is per session, not per workspace. A shared world's
        # messages list would put session A's prompt and reply in session B's
        # model call verbatim; the pager opens a second session on the same cwd
        # for every new thread. Each session starts from the workspace's saved
        # transcript and diverges from there.
        self._convo: dict[str, tuple[list[dict[str, Any]], list[dict[str, str]]]] = {}
        # toolCallId is per session, not per prompt. Restarting at t1 every
        # prompt collided with the previous prompt's card in the pager's
        # pending_tools map.
        self._tool_ids: dict[str, Iterator[int]] = {}
        self._lock = threading.Lock()
        self._inflight: dict[str, dict[str, Any]] = {}

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        has_id = "id" in msg
        req_id = msg.get("id")

        if method == "initialize":
            return rpc_result(req_id, initialize_result())
        if method == "authenticate":
            return rpc_result(req_id, {})
        if method == "session/new":
            return rpc_result(req_id, self._session_new(params))
        if method == "session/prompt":
            try:
                return rpc_result(req_id, self._session_prompt(params))
            except ValueError as exc:
                return rpc_error(req_id, -32602, str(exc))
            except _TurnFailed as exc:
                # The pager renders an Err PromptResponse as "Turn failed".
                # A stopReason cannot say this; end_turn reads as an answer.
                return rpc_error(req_id, -32603, str(exc))
        if method == "session/cancel":
            self._session_cancel(params)
            return rpc_result(req_id, {}) if has_id else None
        if has_id:
            return rpc_error(req_id, -32601, "Method not found")
        return None

    def handle_line(self, line: str) -> dict[str, Any] | None:
        raw = line.strip()
        if not raw:
            return None
        try:
            msg = json.loads(raw)
        except ValueError as exc:
            return rpc_error(None, -32700, f"Parse error: {exc}")
        if not isinstance(msg, dict):
            return rpc_error(None, -32600, "Invalid Request")
        return self.handle(msg)

    def _session_new(self, params: dict[str, Any]) -> dict[str, str]:
        cwd_raw = params.get("cwd") or self.default_cwd
        cwd = Path(str(cwd_raw)).expanduser()
        if not cwd.is_absolute():
            cwd = (self.default_cwd / cwd).resolve()
        else:
            cwd = cwd.resolve()
        session_id = str(uuid.uuid4())
        with self._lock:
            entry = self._worlds.get(cwd)
        if entry is None:
            # new_world loads the workspace's saved state. loadSession is
            # advertised false because we cannot restore a session by id;
            # the durable brain of a directory is a different thing. It reads
            # SQLite, so build it outside the lock -- the lock guards the dicts,
            # and _session_cancel waits on it -- and double-check on insert.
            world = new_world(cwd)
            entry = (world, list(world.messages), list(world.prior))
        from desmos.settings import load as _load_settings

        saved = _load_settings()
        with self._lock:
            entry = self._worlds.setdefault(cwd, entry)
            world = entry[0]
            if saved is not None:
                # Same rule as the bridge: a saved choice outranks whatever the
                # last session persisted, because the user made it. Re-read on
                # every session/new -- with one world per workspace, applying it
                # only at creation latched the first session's model and effort
                # for the life of the process.
                world.model, world.thinking = saved.model, saved.effort
            self.sessions[session_id] = world
            self._convo[session_id] = (list(entry[1]), list(entry[2]))
            self._tool_ids[session_id] = itertools.count(1)
        return {"sessionId": session_id}

    def _session_prompt(self, params: dict[str, Any]) -> dict[str, str]:
        session_id = str(params.get("sessionId") or "")
        with self._lock:
            world = self.sessions.get(session_id)
            convo = self._convo.get(session_id)
        if world is None or convo is None:
            raise ValueError(f"unknown session {session_id!r}")
        prompt_id = _meta_prompt_id(params)
        text = prompt_text(params.get("prompt"))
        if not text.strip():
            # An image-only or empty prompt used to answer end_turn without
            # calling the model, which the pager draws as a finished turn.
            raise ValueError("prompt had no text this agent can carry")
        state: dict[str, Any] = {"cancelled": False}
        with self._lock:
            # The spec says the client awaits the response or cancels first. A
            # second prompt used to overwrite this entry and its finally popped
            # the key, so session/cancel became a silent no-op for the prompt
            # still running. The world is what is exclusive, not the session:
            # run_turns refuses a concurrent step on the same World with a
            # RuntimeError that handle() does not catch, and sessions on one cwd
            # share a world.
            busy = [k for k in self._inflight if self.sessions.get(k) is world]
            if busy:
                who = "this session" if session_id in busy else f"session {busy[0]!r}"
                raise ValueError(f"{who} is already running a prompt on {world.cwd}")
            state["tools"] = self._tool_ids.setdefault(session_id, itertools.count(1))
            self._inflight[session_id] = state
        import desmos.subagent as S

        prev_parent = S.PARENT
        # Without this, spawn() inside an ACP session gets _parent()'s
        # fallback world -- default model, launcher cwd, no session state.
        S.bind(world)
        # Swap this session's transcript onto the shared world for the run. The
        # busy check above means no other session is stepping this world. It
        # stays on the world afterwards -- nothing here reads it between
        # prompts, and leaving it is what makes world.messages the last turn
        # that actually ran.
        world.messages, world.prior = convo
        try:

            def on_event(ev: dict[str, Any]) -> None:
                self._emit_event(session_id, prompt_id, ev, state)

            # An emitter that raises lands in _run_turns' catch-all, which
            # writes "[turn n failed]" over the real reply and skips the
            # commit. The loop's own stop path saves the step; use it.
            run_turns(
                world,
                text,
                quiet=True,
                on_event=on_event,
                should_stop=lambda: bool(state["cancelled"]),
            )
            if state["cancelled"]:
                return {"stopReason": "cancelled"}
            if state.get("error"):
                raise _TurnFailed(str(state["error"]))
            return {"stopReason": "end_turn"}
        finally:
            with self._lock:
                # rollback() and reset() rebind these, so read them back rather
                # than trusting the objects we swapped in.
                self._convo[session_id] = (world.messages, world.prior)
                self._inflight.pop(session_id, None)
            # ponytail: restoring the global only stops the leak past this
            # prompt. Two prompts on different worlds at once still race --
            # subagent.PARENT has to come off the module global for that.
            S.PARENT = prev_parent

    def _session_cancel(self, params: dict[str, Any]) -> None:
        session_id = str(params.get("sessionId") or "")
        with self._lock:
            state = self._inflight.get(session_id)
            if state is not None:
                state["cancelled"] = True

    def _emit_event(
        self,
        session_id: str,
        prompt_id: str | None,
        ev: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        kind = ev.get("ev")
        if kind == "thinking":
            text = str(ev.get("text") or "")
            if text:
                self._update(session_id, prompt_id, {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": text},
                })
        elif kind == "speech":
            text = str(ev.get("text") or "")
            if text:
                self._update(session_id, prompt_id, {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                })
        elif kind == "turn":
            # A truncated reply ("[reply was cut short]") emits error and then
            # keeps going, so only an error with no turn after it ended the
            # step. Clearing here is what keeps a recovered turn from being
            # reported as a dead one.
            state.pop("error", None)
        elif kind == "error":
            state["error"] = str(ev.get("text") or "")
        elif kind == "result":
            phase = str(ev.get("phase") or "done")
            title = str(ev.get("tag") or "tool")
            tool_id = str(state.get("tool") or "")
            if phase == "start" or not tool_id:
                tool_id = f"t{next(state['tools'])}"
                state["tool"] = tool_id
                self._update(session_id, prompt_id, {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tool_id,
                    "title": title,
                    "kind": "execute",
                    "status": "pending",
                })
                if phase == "start":
                    return
            text = str(ev.get("text") or "")
            if phase == "delta":
                if text:
                    self._update(session_id, prompt_id, {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": tool_id,
                        "status": "in_progress",
                        "content": [{
                            "type": "content",
                            "content": {"type": "text", "text": text},
                        }],
                    })
                return
            self._update(session_id, prompt_id, {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_id,
                "status": "completed",
                "content": [{
                    "type": "content",
                    "content": {"type": "text", "text": text},
                }],
            })

    def _update(self, session_id: str, prompt_id: str | None, update: dict[str, Any]) -> None:
        params: dict[str, Any] = {"sessionId": session_id, "update": update}
        if prompt_id:
            params["_meta"] = {"promptId": prompt_id}
        self.write({"jsonrpc": "2.0", "method": "session/update", "params": params})


def handle_line(server: AcpServer, line: str) -> dict[str, Any] | None:
    """Parse one NDJSON line and dispatch. Extracted so check.py can call it."""
    return server.handle_line(line)


def serve(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    *,
    cwd: str | Path | None = None,
) -> int:
    """Read NDJSON-RPC from stdin, write responses and session/update to stdout."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    server = AcpServer(_writer(stdout), default_cwd=Path(cwd or Path.cwd()).resolve())
    incoming: queue.Queue[str | None] = queue.Queue()

    def _read() -> None:
        try:
            for raw in stdin:
                incoming.put(raw)
        finally:
            incoming.put(None)

    reader = threading.Thread(target=_read, name="acp-stdin", daemon=True)
    reader.start()
    workers: list[threading.Thread] = []
    try:
        while True:
            raw = incoming.get()
            if raw is None:
                break
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError as exc:
                server.write(rpc_error(None, -32700, f"Parse error: {exc}"))
                continue
            if not isinstance(msg, dict):
                server.write(rpc_error(None, -32600, "Invalid Request"))
                continue
            if msg.get("method") == "session/prompt" and "id" in msg:
                def work(m: dict[str, Any] = msg) -> None:
                    try:
                        resp = server.handle(m)
                        if resp is not None:
                            server.write(resp)
                    except Exception as exc:  # noqa: BLE001 — keep the pager alive
                        server.write(rpc_error(m.get("id"), -32603, f"{type(exc).__name__}: {exc}"))

                t = threading.Thread(target=work, name="acp-prompt", daemon=True)
                workers.append(t)
                t.start()
                continue
            try:
                resp = server.handle(msg)
            except Exception as exc:  # noqa: BLE001 — keep the pager alive
                if "id" in msg:
                    server.write(rpc_error(msg.get("id"), -32603, f"{type(exc).__name__}: {exc}"))
                continue
            if resp is not None:
                server.write(resp)
    finally:
        for t in workers:
            t.join()
    return 0
