from __future__ import annotations

from pathlib import Path

from desmos.dispatch import dispatch
from desmos.generations import evolve, gen_dir, rollback
from desmos.loop import attach, bind_step, new_world
from desmos.catalog import header, ns_names, system_prompt
from desmos.scan import scan
from desmos.complete import INTERLEAVED_BETA, text_of
from desmos.types import Block


def self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        world = new_world(cwd, state_path=cwd / "harness.json")
        from desmos.complete import cached_payload
        from desmos.const import ABI

        prompt = system_prompt(world)
        assert "cwd:" in prompt
        assert "reload_sdk" in prompt
        assert "sdk:" in prompt
        assert "thinking:" in prompt
        assert "middle:" in prompt
        assert "xai_grok_markdown" in prompt
        assert "AgentMessageBlock" in prompt or "speech markdown" in prompt
        assert "redacted" in prompt
        assert "angle-bracket" in prompt
        assert "SubagentBlock" in prompt
        assert "BlockViewerPane" in prompt
        assert "spawn session" in prompt
        assert "POST in" in prompt
        assert "mid popup" in prompt
        from desmos.cli import (
            _repo_root,
            _tui_binary,
            _tui_build_cmd,
            _tui_build_env,
            _tui_stabilize_fingerprints,
            _tui_stale,
            _tui_watch_roots,
        )

        roots = _tui_watch_roots(cwd)
        assert any("desmos-tui" in str(p) for p in roots)
        assert not any("vendor" in str(p) for p in roots)
        cargo_cmd = _tui_build_cmd("cargo")
        assert cargo_cmd == ["cargo", "build", "-p", "desmos-tui", "--release"]
        assert _tui_build_cmd("cargo", release=False) == [
            "cargo",
            "build",
            "-p",
            "desmos-tui",
        ]
        assert "--quiet" not in cargo_cmd
        launch_env = _tui_build_env({"PATH": "/bin", "HOME": str(cwd)})
        assert "CARGO_TERM_QUIET" not in launch_env
        assert "RUSTFLAGS" not in launch_env
        assert launch_env["RUSTUP_TOOLCHAIN"] == "1.97.1"
        protoc = Path(launch_env["PROTOC"])
        assert protoc.is_file() and protoc.is_absolute()
        for head in _tui_stabilize_fingerprints(_repo_root()):
            assert head.is_file(), head
        kept = _tui_build_env({"RUSTFLAGS": "-C debuginfo=1", "CARGO_TERM_QUIET": "true"})
        assert kept["RUSTFLAGS"] == "-C debuginfo=1"
        assert "CARGO_TERM_QUIET" not in kept
        assert "float_literal_f32_fallback" not in kept.get("RUSTFLAGS", "")
        assert kept["PROTOC"] == str(protoc)
        crate = cwd / "crates" / "desmos-tui"
        crate.mkdir(parents=True)
        src = crate / "main.rs"
        src.write_text("fn main() {}\n", encoding="utf-8")
        fake_bin = cwd / "target" / "release" / "desmos-tui"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.write_bytes(b"bin")
        older = src.stat().st_mtime - 30
        import os as _os

        # No stamp yet: fall back to mtime, so a source newer than the binary
        # is stale and an older one is adopted (and stamped).
        _os.utime(fake_bin, (older, older))
        assert _tui_stale(cwd, fake_bin) is True
        assert _tui_binary(cwd) is None
        _os.utime(src, (older - 30, older - 30))
        assert _tui_stale(cwd, fake_bin) is False
        assert _tui_binary(cwd) == fake_bin
        # Stamped now: a touch is not a rebuild, changed bytes are.
        src.touch()
        assert _tui_stale(cwd, fake_bin) is False
        src.write_text("fn main() { let _ = 1; }\n", encoding="utf-8")
        assert _tui_stale(cwd, fake_bin) is True
        src.write_text("fn main() {}\n", encoding="utf-8")
        assert _tui_stale(cwd, fake_bin) is False
        assert "emitted before" in prompt
        assert "stream" in prompt
        assert "stdout streams" in prompt or "Execute card" in prompt
        assert "[redacted]" in prompt
        assert "pending_prompts" in prompt or "follow-up" in prompt
        assert "enter queues" in prompt
        assert "turn-status" in prompt or "turn status" in prompt
        assert "ready snapshot" in prompt
        assert "height 0" in prompt or "skipped" in prompt
        assert "<edit" in ABI
        assert "<reload_sdk" in ABI
        assert "XML tags are syscalls" in ABI
        assert "Speak markdown" in ABI
        assert "Look around first" in ABI
        assert world.thinking == "low"

        payload = cached_payload(
            "claude-opus-5",
            ABI + "\n\n# tools\n<python> exec",
            [{"role": "user", "content": "hi"}],
            8192,
            thinking="low",
        )
        assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert payload["output_config"] == {"effort": "low"}
        assert payload["_betas"] == []
        replay = cached_payload(
            "claude-opus-5",
            ABI + "\n\n# tools\nx",
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "plan", "signature": "sig"},
                        {"type": "redacted_thinking", "data": "opaque"},
                        {"type": "text", "text": "hi"},
                    ],
                },
                {"role": "user", "content": "ok"},
            ],
            8192,
            thinking="low",
        )
        kinds = [b["type"] for b in replay["messages"][0]["content"]]
        assert kinds == ["thinking", "redacted_thinking", "text"]
        assert replay["messages"][0]["content"][1]["data"] == "opaque"
        budget = cached_payload(
            "claude-sonnet-4-5",
            ABI + "\n\n# tools\nx",
            [{"role": "user", "content": "hi"}],
            8192,
            thinking="low",
        )
        assert budget["thinking"]["type"] == "enabled"
        assert budget["thinking"]["budget_tokens"] == 2048
        assert INTERLEAVED_BETA in budget["_betas"]
        assert "reload" in world.tools and world.tools["reload"].frozen
        assert "reload_sdk" in world.tools and world.tools["reload_sdk"].frozen
        assert any(s.name == "skill-creator" for s in world.skills)
        assert "skill-creator" in dispatch(world, Block("skill", "", {"name": "skill-creator"}))
        assert any(s.name == "edit" for s in world.skills) or "edit" in world.tools

        sample = cwd / "sample.txt"
        sample.write_text("alpha beta alpha\n", encoding="utf-8")
        assert "exactly 1" in dispatch(
            world, Block("edit", "alpha\n---\nALPHA", {"path": str(sample)})
        )
        sample.write_text("alpha beta\n", encoding="utf-8")
        assert "Edited" in dispatch(world, Block("edit", "alpha\n---\nALPHA", {"path": str(sample)}))
        assert sample.read_text(encoding="utf-8") == "ALPHA beta\n"

        ping = cwd / ".desmos" / "skills" / "ping"
        ping.mkdir(parents=True)
        (ping / "SKILL.md").write_text(
            "---\nname: ping\ndescription: reply pong\n---\n# ping\nbody\n",
            encoding="utf-8",
        )
        (ping / "skill.py").write_text("def handle(body, **a):\n    return 'pong:' + body\n", encoding="utf-8")
        world = new_world(cwd, state_path=cwd / "harness.json")
        assert dispatch(world, Block("skill", "", {"name": "ping"})).endswith("body\n")
        assert dispatch(world, Block("ping", "hi", {})) == "pong:hi"

        grown = cwd / ".desmos" / "skills" / "later"
        grown.mkdir(parents=True)
        (grown / "SKILL.md").write_text(
            "---\nname: later\ndescription: appeared after start\n---\n# later\nok\n",
            encoding="utf-8",
        )
        assert not any(s.name == "later" for s in world.skills)
        assert "reloaded" in dispatch(world, Block("reload", "", {}))
        assert any(s.name == "later" for s in world.skills)
        assert dispatch(world, Block("skill", "", {"name": "later"})).endswith("ok\n")

        sdk_out = dispatch(world, Block("reload_sdk", "", {}))
        assert "sdk reloaded" in sdk_out
        assert "reload_sdk" in world.tools

        blocks = scan('<python>x = 1+1</python>\n<bash>echo hi</bash>')
        assert [b.tag for b in blocks] == ["python", "bash"]
        lone = scan("<usage/>\n<reload/>\n<reload_sdk/>\n<rollback n=\"1\"/>\n<skill name=\"ping\"/>")
        assert [b.tag for b in lone] == ["usage", "reload", "reload_sdk", "rollback", "skill"]
        assert lone[0].body == ""
        assert lone[3].attrs == {"n": "1"}
        assert lone[4].attrs == {"name": "ping"}
        assert dispatch(world, blocks[0]) == "ok"
        assert world.ns["x"] == 2
        assert dispatch(world, blocks[1]).strip() == "hi"

        out = dispatch(
            world,
            Block("register", "def handle(body, **a):\n    return body.upper()\n", {"name": "echo", "doc": "uppercase"}),
        )
        assert "registered" in out
        assert dispatch(world, Block("echo", "hi", {})) == "HI"

        assert "wrote" in dispatch(world, Block("system", "prefer tests", {"name": "style"}))
        assert "prefer tests" in system_prompt(world)

        world2 = new_world(cwd, state_path=cwd / "harness.json")
        assert "echo" in world2.tools
        assert world2.notes["style"] == "prefer tests"

        def fake_complete(model, system, messages, max_tokens):
            blob = __import__("json").dumps(messages)
            assert "hello world" not in blob
            if any("<result" in (m.get("content") or "") for m in messages):
                return {"content": [{"type": "text", "text": "11"}], "usage": {}}
            return {"content": [{"type": "text", "text": "<python>len(doc)</python>"}], "usage": {}}

        ns = {"doc": "hello world"}
        w3 = new_world(cwd, state_path=cwd / "harness2.json", ns=ns)
        w3.complete_fn = fake_complete
        bind_step(w3)
        out = w3.ns["step"]("how long is doc?")
        assert out.strip() == "11"
        assert w3.messages[2]["content"].startswith("<result")
        assert "prompt:" not in w3.messages[2]["content"]
        def fake_usage(_model, _system, messages, _max_tokens):
            if any("<result" in (m.get("content") or "") for m in messages):
                return {"content": [{"type": "text", "text": "hello"}], "usage": {}}
            return {"content": [{"type": "text", "text": "<usage/>"}], "usage": {}}

        w_usage = new_world(cwd, state_path=cwd / "harness-usage.json", ns={})
        dispatch(
            w_usage,
            Block("register", "def handle(body, **a):\n    return 'tokens:0'\n", {"name": "usage", "doc": "stats"}),
        )
        w_usage.complete_fn = fake_usage
        bind_step(w_usage)
        spoken = w_usage.ns["step"]("hi there")
        assert spoken.strip() == "hello"
        assert "tokens:0" in w_usage.messages[2]["content"]
        seen: list[str] = []
        w_usage.ns["reset"]()
        w_usage.complete_fn = fake_usage
        from desmos.loop import run_turns as _run

        _run(w_usage, "ping", quiet=True, on_event=lambda e: seen.append(str(e.get("ev"))))
        assert "speech" in seen and "result" in seen and "turn" in seen
        assert "post" in seen
        assert seen.index("post") < seen.index("complete")

        def thinking_complete(_model, _system, _messages, _max_tokens):
            return {
                "content": [
                    {"type": "thinking", "thinking": "plan", "signature": "sig"},
                    {"type": "redacted_thinking", "data": "opaque-secret"},
                    {"type": "text", "text": "hi"},
                ],
                "usage": {},
            }

        w_th = new_world(cwd, state_path=cwd / "harness-think.json", ns={})
        w_th.complete_fn = thinking_complete
        evs_th: list[dict] = []
        _run(w_th, "hi", quiet=True, on_event=lambda e: evs_th.append(e))
        thinks = [e for e in evs_th if e.get("ev") == "thinking"]
        assert len(thinks) == 2
        assert thinks[0].get("redacted") is False and thinks[0].get("text") == "plan"
        assert thinks[1].get("redacted") is True
        assert "opaque-secret" not in str(evs_th)
        complete_ev = next(e for e in evs_th if e.get("ev") == "complete")
        assert complete_ev.get("thoughts") == 1 and complete_ev.get("redacted") == 1
        req = complete_ev.get("request") or {}
        resp = complete_ev.get("response") or {}
        assert req.get("model") or req.get("messages") is not None
        assert "opaque-secret" not in str(resp)
        data = ""
        for block in (resp.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "redacted_thinking":
                data = str(block.get("data") or "")
        assert data == "[redacted]" or data == ""

        from desmos.complete import apply_stream_event, assemble_message, read_sse

        stream_state: dict = {"message": {}, "blocks": []}
        stream_deltas: list[dict] = []
        apply_stream_event(
            stream_state,
            {
                "type": "message_start",
                "message": {"id": "m", "role": "assistant", "usage": {"input_tokens": 9}},
            },
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "ab"}},
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {"type": "content_block_stop", "index": 0},
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "redacted_thinking", "data": "opaque-secret"},
            },
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "hi"}},
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 3},
            },
            stream_deltas.append,
        )
        streamed = assemble_message(stream_state)
        assert streamed["content"][0]["thinking"] == "ab"
        assert streamed["content"][1]["data"] == "opaque-secret"
        assert streamed["content"][2]["text"] == "hi"
        assert streamed["stop_reason"] == "end_turn"
        assert streamed["usage"]["output_tokens"] == 3
        assert "opaque-secret" not in str(stream_deltas)
        assert any(d.get("kind") == "thinking_delta" and d.get("text") == "ab" for d in stream_deltas)
        assert any(d.get("kind") == "thinking" and d.get("redacted") for d in stream_deltas)
        assert any(d.get("kind") == "text_delta" and d.get("text") == "hi" for d in stream_deltas)
        sse_msg = read_sse(
            [
                "event: message_start",
                'data: {"type":"message_start","message":{"role":"assistant"}}',
                "",
                "event: content_block_start",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}',
                "",
                "event: message_stop",
                'data: {"type":"message_stop"}',
                "",
            ]
        )
        assert text_of(sse_msg) == "ok"
        halted = {"go": False}

        def on_first_delta(delta: dict) -> None:
            if delta.get("kind") == "text_delta":
                halted["go"] = True

        sse_stop = read_sse(
            [
                'data: {"type":"message_start","message":{"role":"assistant"}}',
                "",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                "",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"one"}}',
                "",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"two"}}',
                "",
            ],
            on_event=on_first_delta,
            should_stop=lambda: halted["go"],
        )
        assert "one" in text_of(sse_stop)
        assert "two" not in text_of(sse_stop)

        from desmos.complete import iter_sse_lines
        from desmos.exec import run_bash

        import socket
        import threading
        import urllib.request

        got_live = threading.Event()

        def _chunk(conn: socket.socket, payload: bytes) -> None:
            conn.sendall(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")

        def _sse_server(sock: socket.socket) -> None:
            conn, _ = sock.accept()
            try:
                buf = b""
                while b"\r\n\r\n" not in buf:
                    more = conn.recv(4096)
                    if not more:
                        return
                    buf += more
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                )
                _chunk(
                    conn,
                    b'data: {"type":"message_start","message":{"role":"assistant","usage":{}}}\n\n',
                )
                _chunk(
                    conn,
                    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
                )
                _chunk(
                    conn,
                    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"LIVE"}}\n\n',
                )
                if not got_live.wait(2):
                    return
                _chunk(
                    conn,
                    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n',
                )
                conn.sendall(b"0\r\n\r\n")
            finally:
                conn.close()
                sock.close()

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)
        threading.Thread(target=_sse_server, args=(srv,), daemon=True).start()
        live_seen: list[str] = []

        def on_live(delta: dict) -> None:
            if delta.get("kind") == "text_delta" and delta.get("text") == "LIVE":
                live_seen.append("LIVE")
                got_live.set()

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            streamed = read_sse(iter_sse_lines(resp), on_event=on_live)
        assert live_seen == ["LIVE"], "SSE delta must fire before the server closes"
        assert text_of(streamed) == "LIVE"

        handshake = cwd / "go"
        chunks: list[str] = []

        def on_bash(text: str) -> None:
            chunks.append(text)
            if "ONE" in "".join(chunks) and not handshake.is_file():
                handshake.write_text("1", encoding="utf-8")

        bash_out = run_bash(
            "printf ONE; while [ ! -f go ]; do sleep 0.01; done; printf TWO",
            cwd,
            on_chunk=on_bash,
            timeout=3,
        )
        assert "ONE" in bash_out and "TWO" in bash_out
        assert any("ONE" in c for c in chunks)

        live_order: list[str] = []

        def live_complete(_model, _system, messages, _max_tokens):
            live_order.append("http")
            if any("<result" in (m.get("content") or "") for m in messages):
                return {"content": [{"type": "text", "text": "done"}], "usage": {}}
            return {
                "content": [{"type": "text", "text": "<python>1</python>\n<python>2</python>"}],
                "usage": {},
            }

        w_live = new_world(cwd, state_path=cwd / "harness-live.json", ns={})
        w_live.complete_fn = live_complete
        _run(w_live, "two calls", quiet=True, on_event=lambda e: live_order.append(str(e.get("ev"))))
        assert live_order.index("post") < live_order.index("http")
        assert live_order.index("http") < live_order.index("complete")
        assert live_order.count("result") == 4  # start+done per python tag

        evs_wire: list[dict] = []
        w_wire = new_world(cwd, state_path=cwd / "harness-wire.json", ns={"doc": "hello world"})

        def wire_complete(_model, _system, messages, _max_tokens):
            if any("<result" in (m.get("content") or "") for m in messages):
                return {"content": [{"type": "text", "text": "11"}], "usage": {}}
            return {"content": [{"type": "text", "text": "<python>len(doc)</python>"}], "usage": {}}

        w_wire.complete_fn = wire_complete
        _run(w_wire, "how long is doc?", quiet=True, on_event=lambda e: evs_wire.append(e))
        res = next(
            e
            for e in evs_wire
            if e.get("ev") == "result" and e.get("phase") in {None, "done"}
        )
        assert res.get("tag") == "python"
        assert "len(doc)" in (res.get("body") or "")
        assert "11" in (res.get("text") or "")
        assert any(e.get("ev") == "result" and e.get("phase") == "start" for e in evs_wire)
        stop_flag = {"go": False}
        calls = {"n": 0}

        def looping_complete(_model, _system, messages, _max_tokens):
            calls["n"] += 1
            if any("<result" in (m.get("content") or "") for m in messages):
                return {"content": [{"type": "text", "text": "more"}], "usage": {}}
            return {"content": [{"type": "text", "text": "<python>1</python>"}], "usage": {}}

        w_stop = new_world(cwd, state_path=cwd / "harness-stop.json", ns={})
        w_stop.complete_fn = looping_complete
        evs: list[str] = []

        def on_stop_ev(e: dict) -> None:
            evs.append(str(e.get("ev")))
            if e.get("ev") == "complete":
                stop_flag["go"] = True

        spoken = _run(
            w_stop,
            "keep going",
            quiet=True,
            on_event=on_stop_ev,
            should_stop=lambda: stop_flag["go"],
        )
        assert spoken
        assert "stopped" in evs
        assert calls["n"] == 1
        assert (cwd / "harness-stop.json").is_file()
        assert w_stop.prior and w_stop.prior[-1]["prompt"] == "keep going"
        assert "transcript cleared" in w_usage.ns["reset"]()
        assert w_usage.messages == []

        ev = evolve(w3, "after ping")
        assert "generation 2" in ev
        assert (gen_dir(w3) / "0001.json").is_file()
        assert "wrote" in dispatch(w3, Block("system", "usage line", {}))
        assert w3.notes["note"] == "usage line"
        assert "generation 1" in rollback(w3, 1)
        assert "note" not in w3.notes

        py = cwd / "broke.py"
        py.write_text("x = 1\n")
        bad = dispatch(world, Block("edit", "x = 1\n---\nx =\n", {"path": str(py)}))
        assert "SyntaxError" in bad
        assert py.read_text(encoding="utf-8") == "x = 1\n"

        from desmos.persist import save as save_world
        from desmos.subagent import _child_world, resolve, wait

        parent = new_world(cwd, state_path=cwd / "harness-iso.json")
        dispatch(
            parent,
            Block("register", "def handle(body, **a):\n    return 'SECRET'\n", {"name": "secret", "doc": "parent only"}),
        )
        child = _child_world(resolve("explore"), parent)
        assert child.persist is False
        assert "secret" not in child.tools
        assert "agents" not in child.tools
        child.notes["pwn"] = "from-child"
        save_world(child)
        on_disk = __import__("json").loads((cwd / "harness-iso.json").read_text(encoding="utf-8"))
        assert "pwn" not in on_disk.get("notes", {})
        unknown = wait("nope")
        assert unknown[0]["state"] == "unknown"
        import desmos.subagent as S

        S._DEPTH.n = 1
        try:
            try:
                S.spawn("should fail")
            except ValueError as exc:
                assert "depth" in str(exc)
            else:
                raise AssertionError("child spawn should be blocked")
        finally:
            S._DEPTH.n = 0

        evs_spawn: list[dict] = []
        S.set_emitter(evs_spawn.append)
        parent_sp = new_world(cwd, state_path=cwd / "harness-spawn.json")

        def spawn_complete(_model, _system, _messages, _max_tokens):
            return {"content": [{"type": "text", "text": "child said ok"}], "usage": {}}

        parent_sp.complete_fn = spawn_complete
        S.bind(parent_sp)
        rid = S.spawn("reply with ok", agent="explore", parent=parent_sp)
        briefs = S.wait(rid, timeout=15.0)
        assert briefs and briefs[0]["state"] == "done", briefs
        phases = [e.get("phase") for e in evs_spawn if e.get("ev") == "subagent"]
        assert phases and phases[0] == "started", evs_spawn
        assert "done" in phases, evs_spawn
        kids = [e for e in evs_spawn if e.get("ev") == "child"]
        assert any(e.get("kind") == "speech" for e in kids), kids
        assert not any(
            "opaque-secret" in str(e) for e in evs_spawn
        )
        S.set_emitter(None)

        import threading

        import desmos.complete as C

        tdir = cwd / "traj"
        tdir.mkdir()
        prev = C.TRAJECTORY_DIR
        C.TRAJECTORY_DIR = str(tdir)
        try:
            def _write(i: int) -> None:
                C.log_payload({"system": [{"type": "text", "text": f"s{i}"}], "messages": []}, [])

            threads = [threading.Thread(target=_write, args=(i,)) for i in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            files = list(tdir.glob("*.json"))
            assert len(files) == 16
            for f in files:
                rec = __import__("json").loads(f.read_text(encoding="utf-8"))
                assert "system_digest" in rec
            assert len(C.trajectory(16)) == 16
        finally:
            C.TRAJECTORY_DIR = prev

        import json
        from io import StringIO

        from desmos.acp import AcpServer, serve as acp_serve

        acp_in = StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "authenticate", "params": {"methodId": "none"}})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "session/new", "params": {"cwd": str(cwd)}})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 4, "method": "nope", "params": {}})
            + "\n"
        )
        acp_out = StringIO()
        assert acp_serve(acp_in, acp_out, cwd=cwd) == 0
        acp_replies = [json.loads(line) for line in acp_out.getvalue().splitlines() if line.strip()]
        assert [r.get("id") for r in acp_replies] == [1, 2, 3, 4]
        init = acp_replies[0]["result"]
        assert init["protocolVersion"] == 1
        assert init["authMethods"][0]["id"] == "none"
        assert init["agentCapabilities"]["loadSession"] is False
        assert init["agentCapabilities"]["promptCapabilities"]["image"] is True
        assert init["_meta"]["grokShell"] is False
        assert acp_replies[1]["result"] == {}
        assert acp_replies[2]["result"]["sessionId"]
        assert acp_replies[3]["error"]["code"] == -32601

        notes: list[dict] = []
        acp = AcpServer(notes.append, default_cwd=cwd)
        created = acp.handle({"jsonrpc": "2.0", "id": 10, "method": "session/new", "params": {"cwd": str(cwd)}})
        assert created is not None
        sid = created["result"]["sessionId"]

        def fake_acp(_model, _system, messages, _max_tokens):
            blob = json.dumps(messages)
            if "<result" in blob:
                return {"content": [{"type": "text", "text": "done"}], "usage": {}}
            return {
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                    {"type": "text", "text": "<python>1+1</python>"},
                ],
                "usage": {},
            }

        acp.sessions[sid].complete_fn = fake_acp
        prompted = acp.handle({
            "jsonrpc": "2.0",
            "id": 11,
            "method": "session/prompt",
            "params": {
                "sessionId": sid,
                "prompt": [{"type": "text", "text": "add one"}],
                "_meta": {"promptId": "p-check"},
            },
        })
        assert prompted == {"jsonrpc": "2.0", "id": 11, "result": {"stopReason": "end_turn"}}
        kinds = [n["params"]["update"]["sessionUpdate"] for n in notes if n.get("method") == "session/update"]
        assert "agent_thought_chunk" in kinds
        assert "agent_message_chunk" in kinds
        assert "tool_call" in kinds
        assert "tool_call_update" in kinds
        assert all(n["params"].get("_meta", {}).get("promptId") == "p-check" for n in notes if n.get("method") == "session/update")
        tool = next(n["params"]["update"] for n in notes if n.get("method") == "session/update" and n["params"]["update"]["sessionUpdate"] == "tool_call")
        assert tool["title"] == "python" and tool["kind"] == "execute"

        try:
            from IPython.core.interactiveshell import InteractiveShell
        except ImportError:
            print("self-check ok (no IPython)")
            return
        shell = InteractiveShell.instance()
        shell.user_ns["doc"] = "hello world"
        w4 = attach(shell, cwd=cwd)
        w4.state_path = cwd / "harness3.json"
        w4.complete_fn = fake_complete
        assert callable(shell.user_ns["step"])
        assert callable(shell.user_ns.get("reload_sdk"))
        assert callable(shell.user_ns.get("reset"))
        assert "doc" in ns_names(w4)
        assert dispatch(w4, Block("python", "len(doc)", {})) == "11"

    print("self-check ok")
