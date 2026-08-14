"""ACP (Agent Client Protocol) stdio server for grok-build's pager.

Newline-delimited JSON-RPC 2.0 — one object per line, flushed after every write.
Not LSP Content-Length framing.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, IO, TextIO

from desmos.const import DEFAULT_MODEL
from desmos.loop import new_world, run_turns

PROTOCOL_VERSION = 1


class _Cancelled(Exception):
    """Prompt aborted by session/cancel after the current model call."""


def initialize_result() -> dict[str, Any]:
    model = DEFAULT_MODEL
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "agentCapabilities": {
            "loadSession": False,
            "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
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
        world = new_world(cwd)
        with self._lock:
            self.sessions[session_id] = world
        return {"sessionId": session_id}

    def _session_prompt(self, params: dict[str, Any]) -> dict[str, str]:
        session_id = str(params.get("sessionId") or "")
        with self._lock:
            world = self.sessions.get(session_id)
        if world is None:
            raise ValueError(f"unknown session {session_id!r}")
        prompt_id = _meta_prompt_id(params)
        text = prompt_text(params.get("prompt"))
        state: dict[str, Any] = {"cancelled": False, "n": 0}
        with self._lock:
            self._inflight[session_id] = state
        try:
            if not text.strip():
                return {"stopReason": "cancelled" if state["cancelled"] else "end_turn"}

            def on_event(ev: dict[str, Any]) -> None:
                if state["cancelled"]:
                    raise _Cancelled()
                self._emit_event(session_id, prompt_id, ev, state)

            run_turns(world, text, quiet=True, on_event=on_event)
            return {"stopReason": "cancelled" if state["cancelled"] else "end_turn"}
        except _Cancelled:
            return {"stopReason": "cancelled"}
        finally:
            with self._lock:
                self._inflight.pop(session_id, None)

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
        elif kind == "result":
            state["n"] = int(state["n"]) + 1
            tool_id = f"t{state['n']}"
            title = str(ev.get("tag") or "tool")
            self._update(session_id, prompt_id, {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_id,
                "title": title,
                "kind": "execute",
                "status": "pending",
            })
            self._update(session_id, prompt_id, {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_id,
                "status": "completed",
                "content": [{
                    "type": "content",
                    "content": {"type": "text", "text": str(ev.get("text") or "")},
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
