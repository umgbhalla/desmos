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
import warnings
from pathlib import Path
from typing import Any, Callable, IO, Iterator, TextIO

from desmos.kernel.const import DEFAULT_MODEL
from desmos.kernel.loop import new_world, run_turns

PROTOCOL_VERSION = 1

# Story is speech and thinking. Activity is the wire. The TUI enforces this by
# never routing a result event into the story pane; ACP tags the same split so
# a web/desktop client cannot accidentally flatten everything to `out`.
STORY_UPDATES = frozenset({"agent_thought_chunk", "agent_message_chunk"})
ACTIVITY_UPDATES = frozenset({"tool_call", "tool_call_update"})


class _TurnFailed(Exception):
    """The step ended on an error event. A stopReason would report it as an answer."""


def _available_models() -> list[dict[str, str]]:
    from desmos.transport.settings import CATALOG

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in CATALOG.values():
        for model in entry.get("models") or []:
            if model in seen:
                continue
            seen.add(model)
            rows.append({"modelId": model, "name": model})
    if not rows:
        rows.append({"modelId": DEFAULT_MODEL, "name": DEFAULT_MODEL})
    return rows


def _efforts_for(model: str) -> list[str]:
    from desmos.transport.settings import CATALOG, provider_of

    entry = CATALOG.get(provider_of(model)) or {}
    return list(entry.get("efforts") or ["low"])


def config_options(world: Any | None = None) -> list[dict[str, Any]]:
    """ACP session config surface: model + thought_level from the live catalog.

    Comet/gpuix-class clients set these through session/set_config_option.
    The values are the same objects the TUI picker reads — not a second list.
    """
    model = str(getattr(world, "model", None) or DEFAULT_MODEL)
    effort = str(getattr(world, "thinking", None) or "low")
    models = _available_models()
    efforts = _efforts_for(model)
    if effort not in efforts and efforts:
        effort = efforts[0]
    return [
        {
            "id": "model",
            "type": "select",
            "category": "model",
            "name": "Model",
            "currentValue": model,
            "options": [{"value": row["modelId"], "name": row["name"]} for row in models],
        },
        {
            "id": "thought_level",
            "type": "select",
            "category": "thought_level",
            "name": "Thinking",
            "currentValue": effort,
            "options": [{"value": item, "name": item} for item in efforts],
        },
    ]


def session_models(world: Any | None = None) -> dict[str, Any]:
    model = str(getattr(world, "model", None) or DEFAULT_MODEL)
    return {
        "currentModelId": model,
        "availableModels": _available_models(),
    }


def initialize_result() -> dict[str, Any]:
    model = DEFAULT_MODEL
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "agentCapabilities": {
            # Persist session ids from `_session/sessions` / the TUI picker,
            # and ACP uuids bound in `acp_sessions` at session/new.
            "loadSession": True,
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
            "steering": {"supported": True},
            "modelState": {
                "currentModelId": model,
                "availableModels": _available_models(),
            },
            "desmos": {
                "loadSession": "persist+acp",
                "extensions": [
                    "_session/steer",
                    "_session/git",
                    "_session/fs",
                    "_session/sessions",
                    "_session/peers",
                    "_session/roster",
                    "_session/channels",
                    "_session/channel_read",
                    "_session/post",
                    "_session/bridge",
                    "_session/term",
                ],
            },
        },
    }


def pane_of(update: dict[str, Any]) -> str:
    """Which Desmos pane an ACP sessionUpdate belongs on."""
    kind = str(update.get("sessionUpdate") or "")
    if kind in STORY_UPDATES:
        return "story"
    return "activity"


def family_of(update: dict[str, Any]) -> str:
    """Syscall family for activity cards. Story chunks are not families."""
    meta = update.get("_meta")
    if isinstance(meta, dict):
        desmos = meta.get("desmos")
        if isinstance(desmos, dict) and desmos.get("family"):
            return str(desmos["family"])
    title = str(update.get("title") or "")
    if title == "complete":
        return "complete"
    if title in {"edit", "workspace"} or title.endswith(" edit"):
        return "edit"
    kind = str(update.get("sessionUpdate") or "")
    if kind in STORY_UPDATES:
        return "speech" if kind == "agent_message_chunk" else "thinking"
    return "syscall"


def _clip_text(text: str, cap: int = 8000) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 16] + "\n…[truncated]"


def _is_edit(ev: dict[str, Any]) -> bool:
    tag = str(ev.get("tag") or "")
    attrs = ev.get("attrs") if isinstance(ev.get("attrs"), dict) else {}
    return tag == "edit" or (tag == "workspace" and str(attrs.get("op") or "") == "edit")


def _result_title(ev: dict[str, Any]) -> str:
    """ACP toolCall title is the kernel tag. A prettier label rides _meta."""
    return str(ev.get("tag") or "tool")


def _result_label(ev: dict[str, Any]) -> str:
    tag = str(ev.get("tag") or "tool")
    attrs = ev.get("attrs") if isinstance(ev.get("attrs"), dict) else {}
    op = str(attrs.get("op") or "")
    path = str(attrs.get("path") or "")
    if _is_edit(ev):
        name = path.rsplit("/", 1)[-1] if path else ""
        return f"edit {name}".strip() if name else "edit"
    if op and op != tag:
        return f"{tag} {op}"
    return tag


def _result_locations(ev: dict[str, Any]) -> list[dict[str, Any]]:
    attrs = ev.get("attrs") if isinstance(ev.get("attrs"), dict) else {}
    path = str(attrs.get("path") or "")
    if not path:
        return []
    loc: dict[str, Any] = {"path": path}
    line = ev.get("line")
    if line is not None:
        try:
            loc["line"] = int(line)
        except (TypeError, ValueError):
            pass
    return [loc]


def _result_content(ev: dict[str, Any], text: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    if _is_edit(ev):
        diff = _edit_diff(ev)
        if diff is not None:
            chunks.append(diff)
    if text:
        chunks.append({
            "type": "content",
            "content": {"type": "text", "text": text},
        })
    return chunks


def _edit_diff(ev: dict[str, Any]) -> dict[str, Any] | None:
    attrs = ev.get("attrs") if isinstance(ev.get("attrs"), dict) else {}
    path = str(attrs.get("path") or "")
    body = str(ev.get("body") or "")
    old = new = ""
    try:
        from desmos.kernel.edit import parse_edit_body

        old, new = parse_edit_body(body, attrs)
    except Exception:  # noqa: BLE001 — a refused/ambiguous body still has text
        if "\n---\n" in body:
            old, _, new = body.partition("\n---\n")
    if not old and not new:
        return None
    return {
        "type": "diff",
        "path": path or "edit",
        "oldText": old,
        "newText": new,
    }


def _complete_card(ev: dict[str, Any]) -> str:
    """Activity-pane summary of one complete() POST. Never the raw key."""
    req = ev.get("request") if isinstance(ev.get("request"), dict) else {}
    messages = req.get("messages") if isinstance(req.get("messages"), list) else []
    summary = {
        "model": ev.get("model"),
        "thinking": ev.get("thinking"),
        "origin": ev.get("origin"),
        "n": ev.get("n"),
        "messages": len(messages),
        "thoughts": ev.get("thoughts"),
        "redacted": ev.get("redacted"),
        "usage": ev.get("usage") or {},
        "spans": ev.get("spans") or [],
        "residue": ev.get("residue") or "",
    }
    return json.dumps(summary, default=str, indent=2)


def _story_from_messages(messages: Any) -> list[dict[str, str]]:
    """User/assistant speech from a persist transcript. Results stay off story."""
    story: list[dict[str, str]] = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = item.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str) and part:
                    parts.append(part)
                elif isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text") or ""
                    if text:
                        parts.append(str(text))
            text = "".join(parts)
        else:
            text = str(content or "")
        if not text.strip():
            continue
        if role == "user" and text.lstrip().startswith("<result"):
            continue
        if role == "user":
            story.append({"kind": "user", "text": text})
        elif role == "assistant":
            story.append({"kind": "assistant", "text": text})
    return story


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
        if method == "session/load":
            try:
                return rpc_result(req_id, self._session_load(params))
            except ValueError as exc:
                return rpc_error(req_id, -32602, str(exc))
        env = {
            "_session/git": self._session_git,
            "_session/fs": self._session_fs,
            "_session/sessions": self._session_sessions,
            "_session/peers": self._session_peers,
            "_session/roster": self._session_roster,
            "_session/channels": self._session_channels,
            "_session/channel_read": self._session_channel_read,
            "_session/post": self._session_post,
            "_session/bridge": self._session_bridge,
            "_session/term": self._session_term,
        }
        if method in env:
            try:
                return rpc_result(req_id, env[method](params))
            except ValueError as exc:
                return rpc_error(req_id, -32602, str(exc))
        if method == "session/set_config_option":
            try:
                return rpc_result(req_id, self._set_config_option(params))
            except ValueError as exc:
                return rpc_error(req_id, -32602, str(exc))
        if method == "_session/steer":
            try:
                return rpc_result(req_id, self._session_steer(params))
            except ValueError as exc:
                return rpc_error(req_id, -32602, str(exc))
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

    def _cwd_of(self, params: dict[str, Any]) -> Path:
        cwd_raw = params.get("cwd") or self.default_cwd
        cwd = Path(str(cwd_raw)).expanduser()
        if not cwd.is_absolute():
            cwd = (self.default_cwd / cwd).resolve()
        else:
            cwd = cwd.resolve()
        return cwd

    def _world_for_cwd(self, cwd: Path) -> Any:
        with self._lock:
            entry = self._worlds.get(cwd)
        if entry is None:
            # new_world loads the workspace's saved state. It reads SQLite, so
            # build it outside the lock -- the lock guards the dicts, and
            # _session_cancel waits on it -- and double-check on insert.
            world = new_world(cwd)
            entry = (world, list(world.messages), list(world.prior))
        from desmos.transport.settings import load as _load_settings

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
        return world

    def _require_world(self, params: dict[str, Any]) -> Any:
        session_id = str(params.get("sessionId") or "")
        with self._lock:
            world = self.sessions.get(session_id)
        if world is None:
            raise ValueError(f"unknown session {session_id!r}")
        return world

    def _session_payload(self, session_id: str, world: Any, *, loaded: bool = False) -> dict[str, Any]:
        from desmos.state import persist

        persist_id = persist.run_id()
        meta: dict[str, Any] = {
            "desmos": {
                "persistSessionId": persist_id,
                "cwd": str(world.cwd),
            }
        }
        payload: dict[str, Any] = {
            "sessionId": session_id,
            "configOptions": config_options(world),
            "models": session_models(world),
            "_meta": meta,
        }
        if loaded:
            payload["turns"] = persist.session_turns(world, persist_id)
            payload["story"] = _story_from_messages(world.messages)
        return payload

    def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = self._cwd_of(params)
        session_id = str(uuid.uuid4())
        world = self._world_for_cwd(cwd)
        with self._lock:
            entry = self._worlds[cwd]
            self.sessions[session_id] = world
            self._convo[session_id] = (list(entry[1]), list(entry[2]))
            self._tool_ids[session_id] = itertools.count(1)
        from desmos.state import persist as _persist

        try:
            _persist.acp_bind(world, session_id)
        except Exception as exc:  # noqa: BLE001 — a missed bind still issues the uuid
            warnings.warn(f"acp_bind failed: {exc}", RuntimeWarning, stacklevel=2)
        return self._session_payload(session_id, world)

    def _session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        """Restore a persist session or a previously bound ACP uuid.

        Live ACP uuids in this process are returned as-is. After restart,
        persist.acp_lookup maps the uuid Comet stored. Persist session ids
        from the TUI picker still work. An unknown id is refused rather than
        minted: switch_session would otherwise create an empty attach row.
        """
        session_id = str(params.get("sessionId") or "")
        if not session_id:
            raise ValueError("session/load needs sessionId")
        with self._lock:
            live = self.sessions.get(session_id)
        if live is not None:
            return self._session_payload(session_id, live)
        cwd = self._cwd_of(params)
        world = self._world_for_cwd(cwd)
        if world.running:
            raise ValueError("cannot load a session while a prompt is running")
        from desmos.kernel.loop import switch_session
        from desmos.state import persist

        if not persist.session_id_ok(session_id):
            raise ValueError(f"unknown session {session_id!r}")
        bound = persist.acp_lookup(world, session_id)
        known = {row["id"] for row in persist.session_list(world)}
        if bound:
            target = bound["session_id"]
        elif session_id in known:
            target = session_id
        else:
            raise ValueError(f"unknown session {session_id!r}")
        switch_session(world, target)
        with self._lock:
            resolved = Path(world.cwd).resolve()
            entry = self._worlds.get(resolved)
            if entry is not None:
                self._worlds[resolved] = (
                    world, list(world.messages), list(world.prior),
                )
            self.sessions[session_id] = world
            self._convo[session_id] = (list(world.messages), list(world.prior))
            self._tool_ids.setdefault(session_id, itertools.count(1))
        try:
            persist.acp_bind(world, session_id)
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"acp_bind failed: {exc}", RuntimeWarning, stacklevel=2)
        return self._session_payload(session_id, world, loaded=True)

    def _session_git(self, params: dict[str, Any]) -> dict[str, Any]:
        from desmos.front.acp_env import git_snapshot

        world = self._require_world(params)
        return git_snapshot(Path(world.cwd))

    def _session_fs(self, params: dict[str, Any]) -> dict[str, Any]:
        from desmos.front.acp_env import fs_list, fs_read

        world = self._require_world(params)
        op = str(params.get("op") or "list")
        rel = str(params.get("path") or ".")
        if op == "read":
            return fs_read(Path(world.cwd), rel)
        if op in {"list", "ls", ""}:
            return fs_list(Path(world.cwd), rel)
        raise ValueError(f"unknown fs op {op!r}")

    def _session_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        from desmos.state import persist

        world = self._require_world(params)
        return {
            "sessions": persist.session_list(world),
            "persistSessionId": persist.run_id(),
        }

    def _session_peers(self, params: dict[str, Any]) -> dict[str, Any]:
        from desmos.state.persist import peers

        world = self._require_world(params)
        return {"peers": peers(world)}

    def _session_roster(self, params: dict[str, Any]) -> dict[str, Any]:
        from desmos.state.persist import channel_list, roster

        world = self._require_world(params)
        named = roster(world)
        return {
            "agents": named["agents"],
            "channels": channel_list(world),
        }

    def _session_channels(self, params: dict[str, Any]) -> dict[str, Any]:
        from desmos.state.persist import channel_list

        world = self._require_world(params)
        return {"channels": channel_list(world)}

    def _session_channel_read(self, params: dict[str, Any]) -> dict[str, Any]:
        from desmos.state.persist import channel_dismiss, channel_workspace

        world = self._require_world(params)
        channel = str(params.get("channel") or "general")
        workspace = channel_workspace(world, channel)
        messages = list(workspace["messages"])
        through = max((int(item["id"]) for item in messages), default=0)
        channel_dismiss(world, channel=channel, through=through)
        return {
            "channel": channel,
            "messages": messages,
            "participants": workspace["participants"],
            "activity": workspace["activity"],
            "unread": workspace["unread"],
            "max_seq": workspace["max_seq"],
            "pending_delivery": workspace["pending_delivery"],
        }

    def _session_post(self, params: dict[str, Any]) -> dict[str, Any]:
        from desmos.agents import remote
        from desmos.state.persist import channel_post

        world = self._require_world(params)
        channel = str(params.get("channel") or "general")
        body = str(params.get("body") or params.get("text") or "")
        target = str(params.get("target") or "").strip()
        if not body.strip():
            raise ValueError("post needs a body")
        row = channel_post(world, body, channel=channel, author="main")
        dispatch_body = (
            body if not target or f"@{target}" in body
            else f"@{target} {body}"
        )
        dispatched: list[str] = []
        try:
            dispatched = remote.mention_dispatch(
                world, channel, dispatch_body, asker=remote.asker_name(),
            )
        except Exception:  # noqa: BLE001 — the post already landed
            dispatched = []
        return {
            "channel": channel,
            "author": "main",
            "body": body,
            "id": row.get("id", 0),
            "dispatched": dispatched,
            "created_at": row.get("created_at"),
        }

    def _session_bridge(self, params: dict[str, Any]) -> dict[str, Any]:
        from desmos.front.acp_env import bridge_status
        from desmos.state.persist import peers

        world = self._require_world(params)
        status = bridge_status(Path(world.cwd))
        status["peers"] = peers(world)
        return status

    def _session_term(self, params: dict[str, Any]) -> dict[str, Any]:
        """Named PTY from world.shells — the same object `<shell>` uses."""
        from desmos.kernel import shell as sh

        world = self._require_world(params)
        op = str(params.get("op") or "list")
        name = str(params.get("name") or "main").strip() or "main"
        if op == "list":
            shells = []
            for key, item in world.shells.items():
                shells.append({
                    "name": key,
                    "alive": bool(item.alive()),
                    "at_prompt": bool(getattr(item, "at_prompt", False)),
                    "monitoring": bool(getattr(item, "monitoring", False)),
                })
            return {"shells": shells}
        if op == "peek":
            live = world.shells.get(name)
            if live is None:
                return {"name": name, "text": f"no shell {name!r}"}
            return {"name": name, "text": live.peek()}
        if op == "close":
            return {"name": name, "text": sh.run(world, "", {"id": name, "close": "1"})}
        if op == "interrupt":
            return {"name": name, "text": sh.run(world, "", {"id": name, "interrupt": "1"})}
        if op == "run":
            body = str(params.get("body") or params.get("text") or "")
            return {"name": name, "text": sh.run(world, body, {"id": name})}
        raise ValueError(f"unknown term op {op!r}")

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
        import desmos.agents.subagent as S

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

    def _set_config_option(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId") or "")
        with self._lock:
            world = self.sessions.get(session_id)
        if world is None:
            raise ValueError(f"unknown session {session_id!r}")
        config_id = str(params.get("configId") or params.get("id") or "")
        raw = params.get("value")
        if isinstance(raw, dict):
            value = raw.get("value")
            if value is None and "boolean" in raw:
                value = raw.get("boolean")
        else:
            value = raw
        if config_id in {"thought_level", "thinking", "effort"}:
            text = str(value or "").strip()
            if not text:
                raise ValueError("thought_level needs a value")
            from desmos.transport.settings import clamp_effort, provider_of

            world.thinking = clamp_effort(provider_of(str(world.model or "")), text)
            return {"configOptions": config_options(world), "models": session_models(world)}
        if config_id in {"model"}:
            model = str(value or "").strip()
            if not model:
                raise ValueError("model needs a value")
            from desmos.transport.settings import clamp_effort, provider_of, switch

            effort = clamp_effort(provider_of(model), str(world.thinking or "low"))
            switch(world, model, effort)
            return {"configOptions": config_options(world), "models": session_models(world)}
        raise ValueError(f"unknown config option {config_id!r}")

    def _session_steer(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId") or "")
        with self._lock:
            world = self.sessions.get(session_id)
        if world is None:
            raise ValueError(f"unknown session {session_id!r}")
        text = str(params.get("text") or params.get("content") or "").strip()
        if not text:
            raise ValueError("steer needs text")
        from desmos.kernel.catalog import steer as _steer

        _steer(world, text)
        return {}

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
                }, family="thinking")
        elif kind == "speech":
            text = str(ev.get("text") or "")
            if text:
                self._update(session_id, prompt_id, {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                }, family="speech")
        elif kind == "turn":
            # A truncated reply ("[reply was cut short]") emits error and then
            # keeps going, so only an error with no turn after it ended the
            # step. Clearing here is what keeps a recovered turn from being
            # reported as a dead one.
            state.pop("error", None)
        elif kind == "error":
            state["error"] = str(ev.get("text") or "")
        elif kind == "post":
            tool_id = f"c{next(state['tools'])}"
            state["complete"] = tool_id
            model = str(ev.get("model") or "")
            self._update(session_id, prompt_id, {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_id,
                "title": "complete",
                "kind": "fetch",
                "status": "pending",
                "rawInput": {
                    "model": model,
                    "origin": ev.get("origin"),
                    "n": ev.get("n"),
                },
            }, family="complete")
        elif kind == "complete":
            tool_id = str(state.get("complete") or "")
            if not tool_id:
                tool_id = f"c{next(state['tools'])}"
                state["complete"] = tool_id
                self._update(session_id, prompt_id, {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tool_id,
                    "title": "complete",
                    "kind": "fetch",
                    "status": "pending",
                }, family="complete")
            self._update(session_id, prompt_id, {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_id,
                "status": "completed",
                "title": "complete",
                "kind": "fetch",
                "content": [{
                    "type": "content",
                    "content": {"type": "text", "text": _complete_card(ev)},
                }],
            }, family="complete")
        elif kind == "result":
            phase = str(ev.get("phase") or "done")
            title = _result_title(ev)
            family = "edit" if _is_edit(ev) else "syscall"
            tool_id = str(state.get("tool") or "")
            if phase == "start" or not tool_id:
                tool_id = f"t{next(state['tools'])}"
                state["tool"] = tool_id
                raw: dict[str, Any] = {
                    "tag": ev.get("tag"),
                    "attrs": ev.get("attrs") or {},
                    "body": _clip_text(str(ev.get("body") or "")),
                }
                self._update(session_id, prompt_id, {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tool_id,
                    "title": title,
                    "kind": "edit" if family == "edit" else "execute",
                    "status": "pending",
                    "locations": _result_locations(ev),
                    "rawInput": raw,
                }, family=family, label=_result_label(ev))
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
                    }, family=family, label=_result_label(ev))
                return
            self._update(session_id, prompt_id, {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_id,
                "status": "completed",
                "title": title,
                "kind": "edit" if family == "edit" else "execute",
                "content": _result_content(ev, text),
            }, family=family, label=_result_label(ev))

    def _update(
        self,
        session_id: str,
        prompt_id: str | None,
        update: dict[str, Any],
        *,
        family: str | None = None,
        label: str | None = None,
    ) -> None:
        pane = pane_of(update)
        desmos_meta: dict[str, Any] = {"pane": pane}
        if family:
            desmos_meta["family"] = family
        if label:
            desmos_meta["label"] = label
        nested = update.get("_meta")
        if not isinstance(nested, dict):
            nested = {}
        nested["desmos"] = desmos_meta
        update["_meta"] = nested
        params: dict[str, Any] = {"sessionId": session_id, "update": update}
        meta: dict[str, Any] = {"desmos": desmos_meta}
        if prompt_id:
            meta["promptId"] = prompt_id
        params["_meta"] = meta
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
