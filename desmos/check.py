from __future__ import annotations

from pathlib import Path

from desmos.dispatch import dispatch
from desmos.generations import evolve, gen_dir, rollback
from desmos.loop import attach, bind_step, new_world
from desmos.catalog import header, ns_names, system_prompt
from desmos.scan import scan
from desmos.complete import INTERLEAVED_BETA, text_of
from desmos.types import Block


def _fake_id_token(*, plan: str, account: str, ttl: int = 3600) -> str:
    """A JWT-shaped string carrying the claims auth.py reads. Unsigned on purpose."""
    import base64 as _b64
    import json as _json
    import time as _time

    def seg(obj: dict) -> str:
        return _b64.urlsafe_b64encode(_json.dumps(obj).encode()).decode().rstrip("=")

    head = seg({"alg": "none", "typ": "JWT"})
    body = seg(
        {
            "exp": int(_time.time()) + ttl,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account,
                "chatgpt_plan_type": plan,
            },
        }
    )
    return f"{head}.{body}.sig-not-checked"


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

        # Durable memory is a progressive-disclosure store, not a newest-tail
        # log. Migration keeps an exact backup, promotes old high-priority facts
        # into the routing summary, and leaves details available through bounded
        # tool retrieval.
        memory_dir = cwd / "memory-check"
        memory_dir.mkdir()
        legacy = (
            "# MEMORY\n\n## 2025-01-01\n"
            "- Umang prefers actual tools before narration.\n"
            "## 2026-01-01\n"
            "- newest noise " + "x" * 4000 + "\n"
        )
        (memory_dir / "MEMORY.md").write_text(legacy, encoding="utf-8")
        memory_world = new_world(memory_dir, state_path=memory_dir / "harness.json")
        memory_prompt = system_prompt(memory_world)
        assert memory_world.tools["memory"].frozen
        assert "Umang prefers actual tools before narration" in memory_prompt
        assert "x" * 500 not in memory_prompt
        assert (memory_dir / "memories" / "legacy_MEMORY.md").read_text(encoding="utf-8") == legacy
        assert (memory_dir / "memories" / "records.jsonl").is_file()
        assert (memory_dir / "memory_summary.md").is_file()

        remembered = dispatch(
            memory_world,
            Block(
                "memory",
                "Umang's name is Umang.",
                {"id": "user.umang.identity", "scope": "user", "kind": "identity"},
            ),
        )
        assert "remembered user.umang.identity" in remembered
        updated = dispatch(
            memory_world,
            Block(
                "memory",
                "Umang's name is Umang.",
                {"id": "user.umang.identity", "scope": "user", "kind": "identity"},
            ),
        )
        assert "updated user.umang.identity" in updated
        search_result = dispatch(memory_world, Block("memory", "search Umang identity", {}))
        assert "user.umang.identity" in search_result
        read_result = dispatch(memory_world, Block("memory", "read user.umang.identity", {}))
        assert '"scope": "user"' in read_result
        assert "verified user.umang.identity" in dispatch(
            memory_world, Block("memory", "verify user.umang.identity", {})
        )

        secret_result = dispatch(
            memory_world,
            Block(
                "memory",
                "api_key=abcdefghijk123456789",
                {"id": "repo.secret-test", "scope": "repo", "kind": "test"},
            ),
        )
        assert "remembered repo.secret-test" in secret_result
        secret_read = dispatch(memory_world, Block("memory", "read repo.secret-test", {}))
        assert "[REDACTED_SECRET]" in secret_read
        assert "abcdefghijk123456789" not in secret_read

        memory_world2 = new_world(memory_dir, state_path=memory_dir / "harness.json")
        assert memory_world2.tools["memory"].frozen
        assert "Umang's name is Umang" in system_prompt(memory_world2)
        assert "forgot user.umang.identity" in dispatch(
            memory_world2, Block("memory", "forget user.umang.identity", {})
        )
        assert "no match" == dispatch(memory_world2, Block("memory", "search user.umang.identity", {}))
        assert "consolidated" in dispatch(memory_world2, Block("memory", "consolidate", {}))

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
        # Capabilities the code has and the catalog used to leave unsaid. Each
        # of these is reachable today; the model just had no way to know.
        for owed in ("batching:", "6000", "spawn(", "fanout(", "gather(", "reset()", "before_dispatch", "old_str"):
            assert owed in prompt, owed

        # Two dialects, opposite directions. A conciseness instruction cuts
        # Opus 5's length ~20%; the same words make GPT-5.6 return a shorter
        # artifact instead of a shorter explanation. Averaging them is wrong
        # for both, so assert they actually differ.
        from desmos.dialect import capabilities, dialect, family

        assert family("claude-opus-5") == "anthropic"
        assert family("gpt-5.6-sol") == "openai"
        assert family("codex-mini") == "openai"
        assert family("") == "anthropic", "unknown model falls back to anthropic"
        anth, oai = dialect("claude-opus-5"), dialect("gpt-5.6-sol")
        assert anth != oai
        # The load-bearing difference: ask Opus 5 for brevity and responses get
        # ~20% shorter; ask GPT-5.6 and it returns a shorter *artifact* instead.
        # ("brief initial plan" is fine on openai -- that is about the plan,
        # not about how much of the deliverable to hand back.)
        assert "responses focused and brief" in anth
        assert "concise" not in oai and "responses focused and brief" not in oai
        assert len(oai) < len(anth), "openai dialect must stay the shorter one"
        # The factual half is shared; only the register changes.
        assert capabilities() in system_prompt(world)

        # Cross-provider round trip. Switching to OpenAI and back used to brick
        # the session: openai.py puts its item id in "signature" as a provenance
        # marker, wire_content saw a truthy signature and replayed it, and
        # Anthropic answered 400 "Invalid `signature` in `thinking` block".
        # Found by switching providers mid-session in a live TUI, not by reading.
        from desmos.complete import wire_content as _wire

        oai_turn = [
            {"type": "thinking", "thinking": "pondered", "signature": "rs_abc123", "openai": {"type": "reasoning"}},
            {"type": "text", "text": "said out loud", "openai": {"type": "message"}},
            {"type": "compaction", "summary": "folded by openai", "openai": {"type": "compaction"}},
        ]
        replayed = _wire(oai_turn)
        assert all(b.get("type") != "compaction" for b in replayed), replayed
        assert not any("openai" in b for b in replayed), "no foreign field may reach the wire"
        for b in replayed:
            assert b.get("type") != "thinking", "a foreign thought must not replay as thinking"
            assert "signature" not in b, b
        assert any(b["type"] == "text" and b["text"] == "said out loud" for b in replayed), replayed
        # Our own signed thought still replays -- the fix must not cost that.
        ours = _wire([{"type": "thinking", "thinking": "mine", "signature": "sig"}])
        assert ours == [{"type": "thinking", "thinking": "mine", "signature": "sig"}], ours

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
        # Adaptive thinking interleaves on its own and asks for no beta of its
        # own. Compaction is the only header an adaptive model carries.
        from desmos.complete import COMPACT_BETA as _CB, INTERLEAVED_BETA

        assert INTERLEAVED_BETA not in payload["_betas"], payload["_betas"]
        assert payload["_betas"] == [_CB], payload["_betas"]
        # Without these the model keeps writing past its own syscall and
        # invents the reply to it, then reasons from the invention. Both
        # markers are anchored to a line start so prose can still name them.
        stops = payload["stop_sequences"]
        assert len(stops) == 2, stops
        assert all(x.startswith("\n") for x in stops), stops
        assert any("res" in x for x in stops), stops
        assert any("user" in x for x in stops), stops
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
        # A stream that runs dry is not a finished answer. Returned as one, the
        # half-written reply's last syscall has no closing tag, scan() skips
        # unterminated blocks, the turn reports done, and a dropped socket gets
        # committed as the step's result. Ctrl+C is the one legitimate early
        # exit -- sse_stop above must keep working, which is why this is not
        # simply "no message_stop means raise".
        try:
            read_sse(
                [
                    'data: {"type":"message_start","message":{"role":"assistant"}}',
                    "",
                    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                    "",
                    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"half a <bash>ls"}}',
                    "",
                ]
            )
            raise AssertionError("a truncated stream must not read as a finished answer")
        except RuntimeError as exc:
            assert "message_stop" in str(exc), exc
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
                # A real stream terminates. Without this the fixture was a
                # truncated response that the parser accepted as a finished one.
                _chunk(conn, b'data: {"type":"message_stop"}\n\n')
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
        # Exactly one terminator, on every path. The TUI clears `running` on it
        # and drains the queue from it, so a step that ends in silence hangs
        # the pane on "stopping" and the queued message never fires. The path
        # that used to do that: a stop landing during a turn the model finished
        # on its own, which satisfied neither emitter's condition.
        assert [e for e in evs if e in ("done", "stopped")] == ["stopped"], evs
        for landed, want in ((True, "stopped"), (False, "done")):
            flag = {"go": False}

            def final_answer(_model, _system, _messages, _max_tokens, f=flag, l=landed):
                # No syscalls: the turn is done the moment it returns.
                f["go"] = l
                return {"content": [{"type": "text", "text": "all set."}], "usage": {}}

            w_term = new_world(cwd, state_path=None, ns={}, persist=False)
            w_term.complete_fn = final_answer
            terms: list[str] = []
            _run(
                w_term,
                "one shot",
                quiet=True,
                on_event=lambda e: terms.append(str(e.get("ev"))),
                should_stop=lambda f=flag: f["go"],
            )
            got = [e for e in terms if e in ("done", "stopped")]
            assert got == [want], f"stop landed={landed}: {got} in {terms}"
        assert w_stop.prior and w_stop.prior[-1]["prompt"] == "keep going"
        # Compaction. The server folds old turns and hands back a `compaction`
        # block; that block is the cut point the next request replays. Both
        # allowlists it has to cross drop unknown block types by default, and
        # dropping it fails silently -- the run still answers, the transcript
        # just never folds. So assert the whole round trip, not the request knob.
        from desmos.complete import (
            COMPACT_BETA,
            COMPACT_STRATEGY,
            assistant_content,
            cached_payload,
            compaction_block,
            wire_content,
        )

        fold = {"type": "compaction", "id": "cmp_1", "content": "folded 40 turns"}
        kept = assistant_content({"content": [fold, {"type": "text", "text": "ok"}]})
        assert compaction_block(kept) == fold, kept
        assert wire_content(kept)[0] == fold, "a fold must survive the replay path too"

        built = cached_payload("claude-opus-5", "abi", [{"role": "user", "content": "hi"}], 256)
        assert built["context_management"] == {"edits": [{"type": COMPACT_STRATEGY}]}
        assert COMPACT_BETA in built["_betas"]
        # A model without server-side compaction must not carry the knob or the
        # beta -- an unsupported pair is a 400, not a no-op.
        old = cached_payload("claude-3-haiku-20240307", "abi", [{"role": "user", "content": "hi"}], 256)
        assert "context_management" not in old
        assert COMPACT_BETA not in old["_betas"]

        # A fold reaches the wire pane. Without the event the only symptom is
        # the context bar dropping with nothing on screen to explain it.
        w_fold = new_world(cwd, state_path=cwd / "harness-fold.json", ns={})
        w_fold.complete_fn = lambda *_: {
            "content": [fold, {"type": "text", "text": "done"}],
            "usage": {},
        }
        fold_evs: list[dict] = []
        _run(w_fold, "long run", quiet=True, on_event=fold_evs.append)
        assert any(e.get("ev") == "compacted" for e in fold_evs), [e.get("ev") for e in fold_evs]
        assert compaction_block(w_fold.messages[-1]["content"]) == fold, w_fold.messages[-1]

        # step() and reset() are published into the kernel, so the model can
        # reach them from a <python> block mid-turn. A nested run appends its
        # whole exchange before the outer assistant message lands; reset()
        # clears the list the outer loop is appending to. Both are refused.
        w_re = new_world(cwd, state_path=None, persist=False, ns={})
        reentered: list[str] = []

        def reentrant(_m, _s, _msgs, _mt):
            try:
                w_re.ns["step"]("nested")
            except RuntimeError as exc:
                reentered.append(str(exc))
            try:
                w_re.ns["reset"]()
            except RuntimeError as exc:
                reentered.append(str(exc))
            return {"content": [{"type": "text", "text": "done"}], "usage": {}}

        w_re.complete_fn = reentrant
        _run(w_re, "try to re-enter", quiet=True)
        assert len(reentered) == 2, reentered
        assert "already running" in reentered[0], reentered
        assert "inside a running step" in reentered[1], reentered
        assert w_re.running is False, "the flag must clear even after a refusal"
        assert w_re.messages, "the outer step still committed its own transcript"

        # Overload is routine and used to kill a whole multi-turn step.
        from desmos.complete import RETRY_CAP, RETRY_STATUS, _retry_after

        assert {429, 529, 503}.issubset(RETRY_STATUS)
        assert 400 not in RETRY_STATUS and 401 not in RETRY_STATUS, "a payload bug never heals"

        class _Err:
            def __init__(self, **h):
                self.headers = h

        assert _retry_after(_Err(**{"retry-after": "2"}), 0) == 2.0
        assert _retry_after(_Err(**{"retry-after-ms": "1500"}), 0) == 1.5
        assert _retry_after(_Err(**{"retry-after": "99999"}), 0) == RETRY_CAP, "an hour is not a wait"
        assert _retry_after(_Err(**{"retry-after": "junk"}), 0) == 0.5
        assert _retry_after(_Err(), 3) == 4.0, "backoff when the endpoint says nothing"

        # A turn that raises becomes a value. It used to unwind _run_turns,
        # leaving a user message with no assistant reply -- so the next step
        # appended a second consecutive user turn -- while the finally still
        # emitted "done", reporting success beside an unrelated error line.
        w_fail = new_world(cwd, state_path=None, persist=False, ns={})
        w_fail.complete_fn = lambda *_: (_ for _ in ()).throw(RuntimeError("wire died"))
        fail_evs: list[dict] = []
        _run(w_fail, "will fail", quiet=True, on_event=fail_evs.append)
        roles = [m["role"] for m in w_fail.messages]
        assert roles == ["user", "assistant"], roles
        assert "wire died" in str(w_fail.messages[-1]["content"]), w_fail.messages[-1]
        assert any(e.get("ev") == "error" and "wire died" in e.get("text", "") for e in fail_evs)
        assert w_fail.running is False

        # The assistant turn is durable before its syscalls run, and results
        # come back even when the step stops mid-batch.
        w_ord = new_world(cwd, state_path=None, persist=False, ns={})
        halt = {"go": False}
        seen_mid: list[list[str]] = []

        def ordering(_m, _s, msgs, _mt):
            seen_mid.append([m["role"] for m in msgs])
            return {"content": [{"type": "text", "text": "<python>1+1</python>"}], "usage": {}}

        w_ord.complete_fn = ordering

        def stop_after_first(ev: dict) -> None:
            if ev.get("ev") == "result" and ev.get("phase") == "done":
                halt["go"] = True

        _run(w_ord, "batch then stop", quiet=True, on_event=stop_after_first, should_stop=lambda: halt["go"])
        tail = [m["role"] for m in w_ord.messages]
        assert tail == ["user", "assistant", "user"], tail
        assert "<result" in str(w_ord.messages[-1]["content"]), "a stop must not eat results that ran"

        # A traceback is the last thing a failing script prints. Head-clipping
        # a noisy failure returned progress and no error.
        from desmos.scan import clip as _clip

        noisy = "chatter\n" * 4000 + "ZeroDivisionError: division by zero"
        assert "ZeroDivisionError" in _clip(noisy, 600, keep="tail")
        assert "ZeroDivisionError" not in _clip(noisy, 600)
        assert "chatter" in _clip(noisy, 600), "the head is still right for ordinary output"
        assert len(_clip(noisy, 600, keep="tail")) <= 600 + 40
        assert _clip("short", 600, keep="tail") == "short"
        boom = dispatch(world, Block("python", "print('x' * 9000)\n1/0", {}))
        assert "ZeroDivisionError" in boom, boom[:200]

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

        # --- auth: file schema, credential precedence, masking (no network) ---
        import base64
        import json
        import os
        import time
        import urllib.parse

        from desmos import auth as _auth

        old_env = {k: os.environ.get(k) for k in ("OPENAI_API_KEY", "DESMOS_AUTH", "CODEX_HOME", "ANTHROPIC_API_KEY")}
        try:
            authdir = cwd / "authhome"
            authdir.mkdir()
            os.environ["DESMOS_AUTH"] = str(authdir / "auth.json")
            os.environ["CODEX_HOME"] = str(cwd / "nocodex")
            os.environ.pop("OPENAI_API_KEY", None)
            assert _auth.desmos_auth_path() == authdir / "auth.json"
            assert _auth.openai_credential() is None
            try:
                _auth.credential("openai")
                raise AssertionError("expected NeedsAuth")
            except _auth.NeedsAuth:
                pass

            # an oauth file we wrote ourselves, in Codex's own schema
            fake_jwt = _fake_id_token(plan="pro", account="acct-42")
            _auth.write_auth_file(
                _auth.desmos_auth_path(),
                {
                    "access_token": fake_jwt,
                    "refresh_token": "rt-1",
                    "id_token": fake_jwt,
                    "expires_at": int(time.time()) + 3600,
                },
            )
            raw = json.loads(_auth.desmos_auth_path().read_text())
            assert "tokens" in raw and raw["tokens"]["refresh_token"] == "rt-1", raw
            assert oct(_auth.desmos_auth_path().stat().st_mode)[-3:] == "600"
            cred = _auth.openai_credential()
            assert cred is not None and cred.kind == "oauth"
            assert cred.account_id == "acct-42" and cred.plan == "pro"
            assert not cred.expired()
            assert fake_jwt not in cred.masked() and "…" in cred.masked()

            # env key wins over the stored oauth token
            os.environ["OPENAI_API_KEY"] = "sk-openai-test-key"
            cred = _auth.openai_credential()
            assert cred.kind == "env" and cred.source == "OPENAI_API_KEY"
            rows = {r["provider"]: r for r in _auth.status()}
            assert rows["openai"]["ok"] and "openai" in rows and "anthropic" in rows
            assert "sk-openai-test-key" not in json.dumps(rows)

            assert _auth.logout_openai() == [str(_auth.desmos_auth_path())]
            os.environ.pop("OPENAI_API_KEY", None)
            assert _auth.openai_credential() is None
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        # --- browser login: pkce, consent url, and a real localhost callback ---
        import hashlib
        import socket
        import threading
        import urllib.request

        verifier, challenge = _auth._pkce()
        assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=") == challenge
        url = _auth.authorize_url(challenge, "st-1")
        assert url.startswith(_auth.AUTH_BASE + "/oauth/authorize?")
        for want in ("code_challenge_method=S256", "code_challenge=" + challenge, "state=st-1",
                     urllib.parse.quote(_auth.LOCAL_REDIRECT_URI, safe="")):
            assert want in url, want

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        real_port = _auth.LOCAL_PORT
        _auth.LOCAL_PORT = free_port
        try:
            for state, query, expect in (
                ("st-2", "code=ac-9&state=st-2", "ac-9"),
                ("st-3", "code=ac-9&state=wrong", None),
            ):
                out: dict = {}

                def serve(state=state, out=out):
                    try:
                        out["code"] = _auth.wait_for_callback(state, timeout=10)
                    except Exception as e:  # NeedsAuth on mismatch
                        out["err"] = str(e)

                t = threading.Thread(target=serve, daemon=True)
                t.start()
                body = b""
                hit = f"http://127.0.0.1:{free_port}{_auth.CALLBACK_PATH}?{query}"
                for _ in range(100):
                    try:
                        body = urllib.request.urlopen(hit, timeout=2).read()
                        break
                    except OSError:
                        time.sleep(0.05)
                t.join(12)
                assert not t.is_alive(), "callback server never returned"
                assert b"signed in" in body, body[:80]
                if expect:
                    assert out.get("code") == expect, out
                else:
                    assert "code" not in out and "state mismatch" in out.get("err", ""), out
        finally:
            _auth.LOCAL_PORT = real_port




        # --- bridge: the picker and the model op, driven as a real subprocess ---
        import subprocess as _sp
        import sys

        bridge_env = dict(os.environ)
        bridge_env["DESMOS_SETTINGS"] = str(cwd / "settings.json")
        bridge_env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        (cwd / "bridgecwd").mkdir(exist_ok=True)
        proc = _sp.Popen(
            [sys.executable, "-m", "desmos", "bridge", "--cwd", str(cwd / "bridgecwd")],
            stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE, text=True, env=bridge_env,
        )
        try:
            ready = json.loads(proc.stdout.readline())
            assert ready["ev"] == "ready", ready
            assert ready["onboarding"] is True and ready["current"] is None, ready
            names = [p["provider"] for p in ready["providers"]]
            assert names == ["anthropic", "openai"], names
            oai = next(p for p in ready["providers"] if p["provider"] == "openai")
            # The full 5.6 ladder. Offering three of six rungs meant `medium`
            # -- the everyday balance -- could not be selected, and `max`
            # collapsed onto xhigh in effort_of, so the top could not be asked
            # for at all.
            assert "gpt-5.6-sol" in oai["models"], oai["models"]
            assert oai["efforts"] == ["low", "medium", "high", "xhigh", "max"], oai["efforts"]
            from desmos.openai import effort_of as _eff

            assert [_eff(x) for x in oai["efforts"]] == oai["efforts"], "every offered rung must survive the mapping"
            assert _eff("off") == "none" and _eff("nonsense") == "low"
            assert oai["can_login"] is True
            assert ready["provider"] in ("anthropic", "openai")

            proc.stdin.write(json.dumps({"op": "model", "model": "gpt-5.6-luna", "effort": "xhigh"}) + "\n")
            proc.stdin.flush()
            snap = json.loads(proc.stdout.readline())
            assert snap["ev"] == "snapshot" and snap["model"] == "gpt-5.6-luna", snap
            assert snap["provider"] == "openai" and snap["thinking"] == "xhigh", snap
            saved = json.loads((cwd / "settings.json").read_text())
            assert saved == {"provider": "openai", "model": "gpt-5.6-luna", "effort": "xhigh"}, saved

            proc.stdin.write(json.dumps({"op": "model", "model": "gpt-9-nope"}) + "\n")
            proc.stdin.flush()
            bad = json.loads(proc.stdout.readline())
            assert bad["ev"] == "error" and "gpt-9-nope" in bad["text"], bad

            proc.stdin.write(json.dumps({"op": "picker"}) + "\n")
            proc.stdin.flush()
            pick = json.loads(proc.stdout.readline())
            assert pick["ev"] == "picker" and pick["onboarding"] is False, pick
            assert pick["current"]["model"] == "gpt-5.6-luna", pick
        finally:
            proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
            proc.stdin.flush()
            proc.wait(timeout=20)

        from desmos import settings as _st

        assert _st.provider_of("gpt-5.6-sol") == "openai"
        assert _st.provider_of("claude-opus-5") == "anthropic"

        # --- device login: the poll loop, driven with no network and no sleeping ---
        calls: list = []
        replies = [
            (403, {"error": {"code": "deviceauth_authorization_pending"}}),
            (429, {"error": {"code": "slow_down"}}),
            (200, {"authorization_code": "dev-code", "code_verifier": "dev-verifier"}),
        ]
        real_post, real_sleep = _auth._post_json, _auth._sleep
        try:
            _auth._post_json = lambda url, body, timeout=30: (calls.append((url, body)), replies.pop(0))[1]
            _auth._sleep = lambda s: calls.append(("slept", s))
            dev = _auth.DeviceCode("dev-1", "ABCD-EFGH", interval=5)
            got = _auth.poll_device_login(dev)
            assert got == {"code": "dev-code", "verifier": "dev-verifier"}, got
            slept = [s for tag, s in calls if tag == "slept"]
            assert slept == [5, 7], slept  # slow_down actually backs the poll off
            assert all(url == _auth.DEVICE_TOKEN_URL for url, _ in calls if url != "slept")

            replies[:] = [(400, {"error": {"code": "expired_token"}})]
            calls.clear()
            try:
                _auth.poll_device_login(_auth.DeviceCode("dev-2", "X", interval=1))
                raise AssertionError("expected NeedsAuth on a hard device error")
            except _auth.NeedsAuth as e:
                assert "expired_token" in str(e), e
        finally:
            _auth._post_json, _auth._sleep = real_post, real_sleep

        # start_device_login must reject a malformed response instead of polling forever
        try:
            _auth._post_json = lambda *a, **kw: (200, {"user_code": "X"})
            try:
                _auth.start_device_login()
                raise AssertionError("expected NeedsAuth on a malformed device code")
            except _auth.NeedsAuth:
                pass
        finally:
            _auth._post_json = real_post

        # --- openai provider: replay, streaming, usage, and the dispatch seam ---
        from desmos import openai as _oai
        from desmos.complete import assistant_content as _ac

        assert _oai.is_openai("gpt-5.6-sol") and not _oai.is_openai("claude-opus-5")
        assert _oai.effort_of("xhigh") == "xhigh" and _oai.effort_of("off") == "none"
        assert set(_oai.EFFORTS) == {"low", "medium", "high", "xhigh", "max"}
        assert _oai.effort_of("max") == "max", "max is its own rung above xhigh, not an alias for it"
        assert _oai.effort_of("medium") == "medium"
        assert "gpt-5.6-sol" in _oai.MODELS and "gpt-5.6-luna" in _oai.MODELS

        reasoning_item = {
            "id": "rs_1",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "weighing it"}],
            "encrypted_content": "ENC-OPAQUE",
        }
        msg_item = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        }
        events = [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1"}},
            {"type": "response.reasoning_summary_text.delta", "delta": "weigh"},
            {"type": "response.reasoning_summary_text.delta", "delta": "ing it"},
            {"type": "response.output_item.done", "item": reasoning_item},
            {"type": "response.output_text.delta", "delta": "do"},
            {"type": "response.output_text.delta", "delta": "ne"},
            {"type": "response.output_item.done", "item": msg_item},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [reasoning_item, msg_item],
                    "usage": {
                        "input_tokens": 1000,
                        "input_tokens_details": {"cached_tokens": 900},
                        "output_tokens": 40,
                        "output_tokens_details": {"reasoning_tokens": 25},
                    },
                },
            },
        ]
        sse = []
        for ev in events:
            sse.append("event: " + ev["type"])
            sse.append("data: " + json.dumps(ev))
            sse.append("")
        seen = []
        resp_oai = _oai.read_sse(iter(sse), "gpt-5.6-sol", on_event=seen.append)
        assert "".join(e["text"] for e in seen if e["kind"] == "thinking_delta") == "weighing it"
        assert "".join(e["text"] for e in seen if e["kind"] == "text_delta") == "done"
        assert text_of(resp_oai) == "done"
        u = resp_oai["usage"]
        assert u["cache_read_input_tokens"] == 900 and u["input_tokens"] == 100, u
        assert u["output_tokens"] == 40 and u["reasoning_tokens"] == 25

        kept_oai = _ac(resp_oai)
        assert kept_oai[0]["openai"]["encrypted_content"] == "ENC-OPAQUE", kept_oai[0]
        assert kept_oai[0]["thinking"] == "weighing it"

        # replayed verbatim, not rebuilt: the encrypted item goes back as-is
        back = _oai.to_input([{"role": "user", "content": "hi"}, {"role": "assistant", "content": kept_oai}])
        assert back[0]["content"][0]["type"] == "input_text"
        assert reasoning_item in back, back
        assert msg_item in back, back
        # An attached screenshot has to survive the crossing. Anthropic's block
        # shape goes in, Responses' flat data-URL input_image comes out -- the
        # only image shape the Codex backend takes.
        shot = _oai.to_input([{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAB"}},
        ]}])
        assert shot[0]["content"] == [
            {"type": "input_text", "text": "what is this"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAB"},
        ], shot
        assert _oai.to_input([{"role": "user", "content": "plain"}])[0]["content"][0]["text"] == "plain"

        # ...and a foreign thought (no provider item) degrades to speech, not a crash
        foreign = _oai.to_input([{"role": "assistant", "content": [{"type": "thinking", "thinking": "x"}]}])
        assert foreign[0]["content"][0]["text"] == "x"

        body = _oai.payload_for("gpt-5.6-sol", "SYS", [{"role": "user", "content": "hi"}], 4096,
                                thinking="xhigh", compact_threshold=250000, cache_key="k1")
        assert body["instructions"].startswith("SYS") and body["store"] is False
        # the ABI alone let the model narrate a command it never ran
        assert "you have not run anything" in body["instructions"].lower()
        assert body["reasoning"] == {"effort": "xhigh", "summary": "auto"}
        assert body["include"] == ["reasoning.encrypted_content"]
        assert body["context_management"] == [{"type": "compaction", "compact_threshold": 250000}]
        assert not any(i.get("role") == "system" for i in body["input"])

        url_oauth, h_oauth = _oai.headers_for(_auth.Credential(provider="openai", kind="oauth",
                                                               token="t", account_id="acct-1"))
        assert url_oauth == _oai.CHATGPT_URL and h_oauth["chatgpt-account-id"] == "acct-1"
        # One session per process, not per request: the backend routes on this
        # header and the prompt cache lives behind that routing. A fresh uuid
        # each time halved the hit rate against the live endpoint.
        _, h_again = _oai.headers_for(_auth.Credential(provider="openai", kind="oauth",
                                                       token="t", account_id="acct-1"))
        assert h_oauth["session_id"] == h_again["session_id"] != ""
        assert h_oauth["originator"] and h_oauth["Authorization"] == "Bearer t"
        url_key, h_key = _oai.headers_for(_auth.Credential(provider="openai", kind="env", token="sk-x"))
        assert url_key == _oai.API_URL and "chatgpt-account-id" not in h_key

        # the dispatch seam: a gpt model must never reach the Anthropic call site
        import desmos.complete as _cmp

        routed = {}
        real_oai_complete = _oai.complete
        def _routed_complete(*a, **kw):
            routed["hit"] = (a[0], kw.get("thinking"))
            return resp_oai

        _oai.complete = _routed_complete
        old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            out = _cmp.complete("gpt-5.6-luna", "SYS", [{"role": "user", "content": "hi"}], 4096, thinking="high")
            assert routed["hit"] == ("gpt-5.6-luna", "high"), routed
            assert text_of(out) == "done"
        finally:
            _oai.complete = real_oai_complete
            if old_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = old_key

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
