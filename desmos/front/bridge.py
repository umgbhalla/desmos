"""JSONL stdio bridge for the grok-minimal TUI."""

from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from desmos.kernel.catalog import advertised_names, ns_names
from desmos.kernel.loop import (
    new_world,
    reload,
    reload_sdk,
    reset_transcript,
    run_turns,
    switch_session,
)


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
        "tools": advertised_names(world),
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

# Socket writers fanned out alongside stdout, mutated only under _WIRE_LOCK.
# Each is a _Client: a bounded outbound queue drained by that client's own
# writer thread. _emit never does socket I/O -- a blocking send() to a client
# that stopped reading would wedge the whole bridge while holding the lock
# (macOS send() blocks until the full buffer is accepted).
_CLIENTS: list["_Client"] = []


class _Client:
    """One socket client's write side. push() is called under _WIRE_LOCK and
    only enqueues; the writer thread does the blocking sendall. A dead or
    too-slow client (queue full) raises out of push() and the caller drops it.
    ponytail: 4096-line backlog cap, not byte-accounted; size it if events
    ever get pathological."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=4096)
        self.dead = False
        self.writer = threading.Thread(target=self._writer, daemon=True)
        self.writer.start()

    def _writer(self) -> None:
        while True:
            data = self.q.get()
            if data is None:
                return
            try:
                self.sock.sendall(data)
            except OSError:
                self.dead = True
                return

    def push(self, data: bytes) -> None:
        if self.dead:
            raise OSError("client writer dead")
        try:
            self.q.put_nowait(data)
        except queue.Full:
            self.dead = True
            raise OSError("client too slow")

    def push_wait(self, data: bytes, timeout: float) -> None:
        """Blocking push for replay: waits for the writer to drain instead of
        overflowing the bound. Called WITHOUT _WIRE_LOCK -- blocking under it
        would stall every producer behind one attaching client. Raises after
        `timeout` seconds of zero drain: that is a client that stopped
        reading, not a slow one."""
        if self.dead:
            raise OSError("client writer dead")
        try:
            self.q.put(data, timeout=timeout)
        except queue.Full:
            self.dead = True
            raise OSError("client stalled") from None

    def close(self) -> None:
        self.dead = True
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass  # the writer is wedged in sendall; closing the socket frees it
        try:
            self.sock.close()
        except OSError:
            pass

# The event log (contract C2): every event this funnel sees is also appended
# Durable replay lives in the harness database. The wire remains unstamped;
# sequence/time belong to the bridge writer and are added only to SQL rows.
_LOG_WORLD: Any = None
_SEQ = 0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _open_log(world: Any) -> None:
    global _LOG_WORLD, _SEQ
    from desmos.state.persist import record_event

    _LOG_WORLD = world
    _SEQ = record_event(
        world,
        {
            "ev": "session",
            "session_id": os.environ.get("DESMOS_SESSION_ID", ""),
            "cwd": str(world.cwd),
        },
        ts_ms=_now_ms(),
        mono_ns=time.monotonic_ns(),
    )


def _log(ev: dict[str, Any]) -> None:
    """Append one replay-relevant event; caller holds _WIRE_LOCK."""
    global _SEQ
    if _LOG_WORLD is None:
        return
    from desmos.state.persist import record_event

    seq = record_event(
        _LOG_WORLD,
        ev,
        ts_ms=_now_ms(),
        mono_ns=time.monotonic_ns(),
    )
    if seq:
        _SEQ = max(_SEQ, seq)


def _emit(ev: dict[str, Any]) -> None:
    line = json.dumps(ev, default=str) + "\n"
    data = line.encode("utf-8")
    with _WIRE_LOCK:
        _log(ev)
        _WIRE.write(line)
        _WIRE.flush()
        for client in _CLIENTS[:]:
            try:
                client.push(data)
            except OSError:
                # Dead or too slow: dropped, never allowed to block the loop.
                _CLIENTS.remove(client)
                client.close()


# Zero-drain window before an attaching client is declared dead. Backpressure
# means a merely slow reader never trips this: the put only times out when the
# queue stayed full -- no reads at all -- for the whole window.
_REPLAY_STALL = 20.0


def _replay(wire: "_Client", since: int) -> None:
    """Replay compact SQL events, then join the live fan-out gaplessly."""
    from desmos.state.persist import read_events

    last = int(since)
    while True:
        with _WIRE_LOCK:
            if _LOG_WORLD is None or last >= _SEQ:
                if wire not in _CLIENTS:
                    _CLIENTS.append(wire)
                return
            target = _SEQ
            world = _LOG_WORLD
        rows = read_events(
            world,
            since=last,
            limit=max(1, min(target - last, 20_000)),
        )
        if not rows:
            last = target
            continue
        for event in rows:
            last = int(event["seq"])
            if event.get("ev") == "timing":
                continue
            wire_event = dict(event)
            wire_event.pop("payload_bytes", None)
            wire_event.pop("payload_sha256", None)
            if wire_event.get("ev") == "session":
                wire_event.pop("seq", None)
                wire_event.pop("mono_ns", None)
            wire.push_wait(
                (json.dumps(wire_event, default=str) + "\n").encode("utf-8"),
                _REPLAY_STALL,
            )


def _intervene(op: str, msg: dict[str, Any]) -> None:
    """kill_run / rerun (contract C3): answered on the reader thread, never
    queued -- a kill that waits behind the step it is meant to interrupt is
    not an intervention. Both calls return prose for unknown ids, never raise.
    The intervention record goes through the normal funnel, so it lands in
    the event log stamped and on the wire for the TUI to clear its marker."""
    import desmos.agents.subagent as S

    rid = str(msg.get("id") or "")
    result = S.kill_subtree(rid) if op == "kill_run" else S.rerun(rid)
    _emit({"ev": "intervention", "action": op, "id": rid, "result": result})
    _emit({"ev": "notice", "text": result})


def _serve_client(conn: socket.socket, inbox: queue.Queue, cancel: threading.Event) -> None:
    """One accepted unix-socket client: reads ops into the shared inbox (the
    queue is the serialization -- two clients cannot double-drive the world),
    writes the live stream back. A client that wants history sends
    {"op":"attach","since":<seq>} first; otherwise it joins the fan-out at its
    first message."""
    wire = _Client(conn)  # write side; reads stay blocking on this thread
    registered = False

    def register_locked() -> None:
        nonlocal registered
        if not registered:
            _CLIENTS.append(wire)
            registered = True

    try:
        for raw in conn.makefile("r", encoding="utf-8"):
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
            if op == "attach":
                try:
                    since = int(msg.get("since") or 0)
                except (TypeError, ValueError):
                    since = 0
                try:
                    _replay(wire, since)  # registers the client itself
                except OSError:
                    # The client drained nothing for the whole stall window.
                    # Never a silent truncation: the drop is announced on the
                    # funnel (logged, visible to every live client and any
                    # re-attach), and the same line is queued to the culprit
                    # best-effort -- a truly dead client cannot receive it.
                    note = {
                        "ev": "error",
                        "text": "attach dropped: client stopped reading during replay",
                    }
                    try:
                        # The full stall window again: a client that resumes
                        # frees a slot as soon as it drains one line, and then
                        # sees the backlog, the reason, and a clean EOF (the
                        # terminator stops the writer at a line boundary). A
                        # client that never resumes hits Full and gets the
                        # hard close. This blocks only this client's reader
                        # thread, never the funnel.
                        wire.q.put(
                            (json.dumps(note) + "\n").encode("utf-8"),
                            timeout=_REPLAY_STALL,
                        )
                        wire.q.put(None, timeout=1.0)
                    except queue.Full:
                        pass
                    _emit(note)
                    wire.writer.join(_REPLAY_STALL)
                    return
                registered = True
                continue
            if op == "quit":
                # A socket client's quit detaches that client. Only stdio --
                # the owner -- may end the bridge.
                return
            with _WIRE_LOCK:
                register_locked()
            if op == "stop":
                cancel.set()
                continue
            if op in ("kill_run", "rerun"):
                _intervene(op, msg)
                continue
            inbox.put(msg)
    except OSError:
        pass
    finally:
        with _WIRE_LOCK:
            if wire in _CLIENTS:
                _CLIENTS.remove(wire)
        wire.close()


def _bind_socket(cwd: Path) -> tuple[socket.socket | None, bool]:
    """The second transport: <cwd>/.desmos/bridge.sock, 0600, stdlib only.

    An existing path is probed: answering means another bridge owns this cwd
    (return its ownership separately); silence means a stale file from a dead
    process (unlink and take over).
    """
    path = cwd / ".desmos" / "bridge.sock"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        probe = socket.socket(socket.AF_UNIX)
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
        except OSError:
            path.unlink()
        else:
            return None, True
        finally:
            probe.close()
    srv = socket.socket(socket.AF_UNIX)
    old = os.umask(0o177)  # born 0600: no chmod window
    try:
        srv.bind(str(path))
    finally:
        os.umask(old)
    srv.listen()
    return srv, False


def _watch_channel(
    world: Any, inbox: queue.Queue, stop: threading.Event
) -> None:
    """Show shared activity and enqueue each directed peer exchange once."""
    from desmos.state.persist import (
        channel_dismiss,
        channel_inbox,
        peer_channel,
        run_id,
    )

    last_emitted = 0
    last_peer = {"request": 0, "reply": 0}
    while not stop.wait(0.25):
        try:
            info = channel_inbox(world, limit=50)
            fresh = [
                message for message in info["messages"]
                if int(message["id"]) > last_emitted
            ]
            if fresh:
                last_emitted = max(int(message["id"]) for message in fresh)
                latest = fresh[-1]
                preview = " ".join(str(latest["body"]).split())
                if len(preview) > 120:
                    preview = preview[:119].rstrip() + "…"
                _emit({
                    "ev": "channel",
                    "channel": info["channel"],
                    "author": latest["author"],
                    "preview": preview,
                    "unread": info["unread"],
                    "message_id": int(latest["id"]),
                })

            for kind in ("request", "reply"):
                channel = peer_channel(run_id(), kind)
                directed = channel_inbox(world, channel=channel, limit=20)
                for message in directed["messages"]:
                    message_id = int(message["id"])
                    if message_id <= last_peer[kind]:
                        continue
                    sender = str(message["run_id"])
                    body = str(message["body"])
                    preview = " ".join(body.split())
                    if len(preview) > 120:
                        preview = preview[:119].rstrip() + "…"
                    _emit({
                        "ev": "channel",
                        "channel": channel,
                        "author": sender,
                        "preview": preview,
                        "unread": directed["unread"],
                        "message_id": message_id,
                        "directed": kind,
                        "body": body,
                    })
                    if kind == "request":
                        text = (
                            f"[Peer session {sender} requested a one-round exchange]\\n\\n"
                            f"{body}\\n\\n"
                            "Answer the request. Your final response will be returned "
                            "automatically; do not post a separate peer reply."
                        )
                    else:
                        text = (
                            f"[Peer session {sender} replied to your request]\\n\\n"
                            f"{body}\\n\\n"
                            "Report this reply to your user. Do not contact the peer "
                            "again unless the user explicitly asks."
                        )
                    inbox.put({
                        "op": "step",
                        "text": text,
                        "_peer": {
                            "kind": kind,
                            "sender": sender,
                            "message_id": message_id,
                        },
                    })
                    last_peer[kind] = message_id
                    # Queue first, then commit the durable cursor. A failure can
                    # duplicate one exchange after restart, but cannot lose it.
                    channel_dismiss(world, channel=channel, through=message_id)
        except Exception:  # noqa: BLE001 -- peer activity must not kill the bridge
            continue


def serve(cwd: Path) -> int:
    from desmos.state.persist import WorkspaceBusy, claim_workspace

    world = new_world(cwd)
    try:
        claim_workspace(world)
    except WorkspaceBusy as exc:
        # Loading is read-only; refusing here is before the first save, which
        # is the write that would have clobbered the other front.
        _emit({"ev": "notice", "text": str(exc)})
        print(f"desmos: {exc}", file=sys.stderr)
        return 1
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
    channel_stop = threading.Event()
    inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
    # The log first: ready and everything after it must be in the replay file.
    _open_log(world)
    try:
        sock_srv, socket_owned = _bind_socket(cwd)
    except OSError:
        # e.g. the AF_UNIX ~104-byte path limit on a deep cwd. The stdio
        # bridge must survive losing its second transport.
        sock_srv = None
        socket_owned = False

    def acceptor() -> None:
        while True:
            try:
                conn, _addr = sock_srv.accept()
            except OSError:
                return  # closed on exit
            threading.Thread(
                target=_serve_client, args=(conn, inbox, cancel), daemon=True
            ).start()

    if sock_srv is not None:
        threading.Thread(target=acceptor, daemon=True).start()

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
            if op in ("kill_run", "rerun"):
                # Same routing as the socket readers: an intervention answered
                # inline, not queued behind the step it interrupts.
                _intervene(op, msg)
                continue
            inbox.put(msg)
        inbox.put(None)

    threading.Thread(target=reader, daemon=True).start()
    from desmos.transport.settings import picker as _picker

    _emit({
        "ev": "ready",
        **{k: v for k, v in _snapshot(world).items() if k != "ev"},
        **_picker(),
    })
    if sock_srv is None and not socket_owned:
        _emit({
            "ev": "notice",
            "text": "socket transport off: .desmos/bridge.sock could not be bound",
        })
    threading.Thread(
        target=_watch_channel, args=(world, inbox, channel_stop), daemon=True
    ).start()
    try:
        return _drive(world, inbox, cancel)
    finally:
        channel_stop.set()
        if sock_srv is not None:
            sock_srv.close()
            (cwd / ".desmos" / "bridge.sock").unlink(missing_ok=True)
        global _LOG_WORLD
        with _WIRE_LOCK:
            # Null first so a straggler child thread sees no durable writer.
            _LOG_WORLD = None


def _drive(world: Any, inbox: queue.Queue, cancel: threading.Event) -> int:
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
                speech = run_turns(
                    world,
                    text,
                    quiet=True,
                    on_event=_emit,
                    should_stop=cancel.is_set,
                    has_input=lambda: not inbox.empty(),
                    images=images,
                )
                peer = msg.get("_peer")
                if isinstance(peer, dict) and peer.get("kind") == "request":
                    from desmos.state.persist import channel_post, peer_channel

                    response = str(speech).strip() or "(peer session returned no final response)"
                    channel_post(
                        world,
                        response,
                        channel=peer_channel(str(peer.get("sender") or ""), "reply"),
                    )
                _emit(_snapshot(world))
            elif op == "snapshot":
                _emit(_snapshot(world))
            elif op == "reset":
                _emit({"ev": "speech", "text": reset_transcript(world)})
                _emit(_snapshot(world))
            elif op == "session":
                _emit({"ev": "speech", "text": switch_session(world, msg.get("id"))})
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
            elif op == "typed":
                # The TUI queued a follow-up. Nothing to do here: run_turns
                # polls `has_input`, which is `not inbox.empty()`, so landing
                # in the queue at all is what releases a step parked on
                # background work. Draining it here is the no-op that keeps it
                # from being reported as an unknown op.
                pass
            else:
                _emit({"ev": "error", "text": f"unknown op {op!r}"})
        except Exception as exc:  # noqa: BLE001 — keep the TUI alive
            _emit({"ev": "error", "text": f"{type(exc).__name__}: {exc}"})
