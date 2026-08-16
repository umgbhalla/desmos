"""Transport checks: complete, streaming, cache, dialect, settings, auth, openai."""

from __future__ import annotations

from pathlib import Path

from desmos.complete import text_of
from desmos.loop import bind_step, new_world
from desmos.loop import run_turns as _run
from desmos.scan import scan


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


def check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)

        from desmos.complete import cached_payload
        from desmos.const import ABI

        # Two dialects, opposite directions. A conciseness instruction cuts
        # Opus 5's length ~20%; the same words make GPT-5.6 return a shorter
        # artifact instead of a shorter explanation. Averaging them is wrong
        # for both, so assert they actually differ.
        from desmos.dialect import dialect, family

        assert family("claude-opus-5") == "anthropic"
        assert family("gpt-5.6-sol") == "openai"
        assert family("codex-mini") == "openai"
        assert family("") == "anthropic", "unknown model falls back to anthropic"
        assert dialect("claude-opus-5") != dialect("gpt-5.6-sol"), "one block cannot serve both"
        assert "implementation and verification" in dialect("gpt-5.6-sol"), (
            "the OpenAI lane can stop after inspection on an implementation request"
        )

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
        # Deliberately absent -- see the note in cached_payload. The markers
        # were pure text with no notion of intent, so a docs page that merely
        # named one was guillotined mid-fence. Asserting their absence keeps
        # the removal a decision rather than a regression: put them back and
        # this fails until someone edits the reason next to it.
        assert "stop_sequences" not in payload, payload["stop_sequences"]

        # Three cache breakpoints and no fourth: ABI, catalog, last user. Every
        # one of them is a prefix that stops moving, which is the whole point --
        # a breakpoint on the assistant moves with every reply, so the segment
        # it marks is never the segment the next request asks for and the cache
        # is paid for and never read. Nothing in the suite watched this, so the
        # one move the ABI forbids used to pass.
        def _cached(blocks):
            return [i for i, b in enumerate(blocks) if "cache_control" in b]

        assert _cached(payload["system"]) == [0, 1], payload["system"]
        abi_block, cat_block = payload["system"]
        assert abi_block["text"] == ABI, "the first cached system block is not the ABI"
        assert "<python> exec" in cat_block["text"], "the catalog is not its own cached block"
        assert _cached(payload["messages"][0]["content"]) == [0]

        # A transcript that ends on the assistant: the breakpoint has to walk
        # back to the last user turn, not land on the tail.
        tail_assistant = cached_payload(
            "claude-opus-5",
            ABI + "\n\n# tools\nx",
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
                {"role": "user", "content": "second"},
                {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
            ],
            8192,
            thinking="low",
        )
        marked = [
            (m["role"], b.get("text"))
            for m in tail_assistant["messages"]
            for b in m["content"]
            if "cache_control" in b
        ]
        assert marked == [("user", "second")], marked

        # A todo tick must not move the cached prefix. Volatile notes ride
        # behind every breakpoint, so mutating one rewrites a single trailing
        # block instead of the whole 130k-token prefix in front of it -- the
        # measured difference is ~$0.85 per tick late in a session.
        from desmos.loop import new_world
        from desmos.catalog import system_prompt as _sysprompt

        vol = new_world(cwd, state_path=cwd / "volatile.json")
        vol.notes["todo"] = "[ ] alpha\n[ ] beta"
        history = [{"role": "user", "content": "go"}]
        before = cached_payload("claude-opus-5", _sysprompt(vol), history, 8192, thinking="low")
        vol.notes["todo"] = "[x] alpha\n[ ] beta"
        after = cached_payload("claude-opus-5", _sysprompt(vol), history, 8192, thinking="low")
        assert before["system"] == after["system"], "a todo tick rewrote the cached system blocks"
        assert not any("[ ] beta" in b["text"] for b in before["system"]), (
            "the todo body is still inside the cached prefix"
        )
        last = after["messages"][-1]["content"]
        assert "cache_control" not in last[-1], last[-1]
        # Done rows are dropped and the survivors keep their real numbers: the
        # tail is re-sent uncached every turn, so it pays for itself each time.
        tail = last[-1]["text"]
        assert "2. [ ] beta" in tail and "alpha" not in tail and "(1 done)" in tail, tail
        assert "1. [ ] alpha" in before["messages"][-1]["content"][-1]["text"]
        marks = [i for i, b in enumerate(last) if "cache_control" in b]
        assert marks == [len(last) - 2], marks

        # Nothing open, nothing sent.
        vol.notes["todo"] = "[x] alpha\n[x] beta"
        empty = cached_payload("claude-opus-5", _sysprompt(vol), history, 8192, thinking="low")
        assert empty["messages"][-1]["content"][-1].get("cache_control"), (
            "a fully ticked todo still appended a block"
        )

        # A mid-run <register>, <tool>, <system> or <evolve> must not move the
        # cached catalog block either. It is frozen at first use and the
        # difference ships in the same trailing block the todo uses.
        from desmos.types import Tool as _Tool
        from desmos.complete import split_system as _split
        from desmos.catalog import CATALOG_DELTA_LIMIT

        first_cat = _split(_sysprompt(vol))[1]
        vol.tools["zzz"] = _Tool(name="zzz", doc="do a thing")
        vol.notes["style"] = "prefer tests"
        grown = _sysprompt(vol)
        assert _split(grown)[1] == first_cat, "a new tool rewrote the cached catalog block"
        delta = _split(grown)[2]
        assert "+<zzz> do a thing" in delta and "+prefer tests" in delta, delta
        assert "prefer tests" in grown, "the new note never reached the prompt at all"
        grown_payload = cached_payload("claude-opus-5", grown, history, 8192, thinking="low")
        assert grown_payload["system"][1]["text"] == first_cat
        grown_tail = grown_payload["messages"][-1]["content"][-1]
        assert "<zzz>" in grown_tail["text"] and "cache_control" not in grown_tail, grown_tail

        # Past the limit the frozen copy refreshes: one rewrite beats a delta
        # nobody can reconstruct the catalog from.
        vol.notes["big"] = "x" * (CATALOG_DELTA_LIMIT + 1)
        refreshed = _sysprompt(vol)
        assert _split(refreshed)[1] != first_cat, "the frozen catalog never refreshes"
        assert _split(refreshed)[2] == "", _split(refreshed)[2]

        # The OpenAI lane makes the same trade: instructions hold still and the
        # volatile text becomes a trailing input item.
        from desmos.openai import payload_for as _payload_for

        vol.notes["todo"] = "[ ] alpha"
        oai = _payload_for("gpt-5.6-sol", _sysprompt(vol), history, 8192, thinking="low")
        assert "[ ] alpha" not in oai["instructions"], "volatile state rode in instructions"
        assert "[ ] alpha" in oai["input"][-1]["content"][-1]["text"], oai["input"][-1]

        # An inbound cache_control on a user block is dropped and re-derived,
        # or a replayed transcript accumulates one breakpoint per turn and
        # blows the four-block limit the API enforces.
        restamped = cached_payload(
            "claude-opus-5",
            ABI + "\n\n# tools\nx",
            [
                {"role": "user", "content": [{"type": "text", "text": "old", "cache_control": {"type": "ephemeral"}}]},
                {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
                {"role": "user", "content": [{"type": "text", "text": "new"}]},
            ],
            8192,
            thinking="low",
        )
        stamped = [
            b.get("text")
            for m in restamped["messages"]
            for b in m["content"]
            if "cache_control" in b
        ]
        assert stamped == ["new"], stamped

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

        from desmos.complete import (
            apply_stream_event,
            assemble_message,
            degenerate_cut,
            read_sse,
        )

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
        # A decoder stuck in a repetition attractor is ordinary assistant
        # speech, so nothing downstream filters it: one session streamed 43,815
        # copies of "url" into the story pane and the transcript. The stream
        # must cut itself off, keep the real prefix, and stop feeding the pane.
        painted: list[str] = []
        degen_lines = [
            'data: {"type":"message_start","message":{"role":"assistant"}}',
            "",
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            "",
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"a real sentence before it got stuck."}}',
            "",
        ]
        for _ in range(400):
            degen_lines.append(
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"url\\n\\n"}}'
            )
            degen_lines.append("")
        degen_lines += ['data: {"type":"message_stop"}', ""]
        degen = read_sse(
            degen_lines,
            on_event=lambda d: painted.append(d.get("text", ""))
            if d.get("kind") == "text_delta"
            else None,
        )
        degen_text = text_of(degen)
        assert "a real sentence before it got stuck." in degen_text, degen_text[:200]
        assert degen.get("stop_reason") == "degenerate_repetition", degen.get("stop_reason")
        assert degen_text.count("url") <= 8, degen_text.count("url")
        # and the pane must stop receiving, not merely have the tail trimmed
        # after the fact -- an append-only pane has no retraction.
        assert len(painted) <= 80, len(painted)
        # No false positive on ordinary prose, nor on a phrase a writer
        # genuinely repeats for effect. Cutting real output is worse than
        # painting a few junk lines, so the bar is eight identical copies.
        prose = (
            "Sizing this guard is a tradeoff between reaction time and the risk "
            "of truncating output that the model actually meant to write, and "
            "the second cost is by far the larger of the two in an append-only "
            "pane where nothing can be retracted once it has been painted."
        )
        assert degenerate_cut(prose) is None
        assert degenerate_cut("short") is None
        assert degenerate_cut("Never again. " * 4) is None
        assert degenerate_cut("Never again. " * 40) is not None

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

        # Overload is routine and used to kill a whole multi-turn step.
        from desmos.transport.complete import RETRY_CAP, RETRY_STATUS, _retry_after

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

        # The two effort ladders are different lengths, so an effort that is
        # fine on one provider does not exist on the other. Refusing the switch
        # on that basis meant a session on sol at medium or max simply could
        # not move to Opus. The model is what was asked for; the effort bends.
        from desmos.settings import CATALOG as _CAT, clamp_effort

        for provider, table in _CAT.items():
            for effort in ("none", "low", "medium", "high", "xhigh", "max", "nonsense"):
                got = clamp_effort(provider, effort)
                assert got in table["efforts"], f"{provider}/{effort} -> {got}"
        # An effort the target does have is never moved.
        assert clamp_effort("anthropic", "high") == "high"
        assert clamp_effort("openai", "medium") == "medium"
        # A tie goes up: thinking less than asked is the worse surprise. Both
        # live providers offer the same five rungs now, so a tie only exists
        # against a ladder with a hole in it -- build one rather than assert
        # nothing.
        _CAT["_gap"] = {"models": [], "efforts": ["low", "high"]}
        try:
            assert clamp_effort("_gap", "medium") == "high"
            assert clamp_effort("_gap", "xhigh") == "high"
        finally:
            del _CAT["_gap"]

        # Every rung the picker offers has to survive to the wire as itself.
        # xhigh was being rewritten to max, so choosing it ran a level the user
        # did not pick and the settings file disagreed with the request. The
        # API rejects anything outside these five, which is why the list is
        # exactly this and why a silent rewrite is not harmless.
        from desmos.complete import apply_thinking as _apply

        for _level in _CAT["anthropic"]["efforts"]:
            _payload: dict = {}
            _apply(_payload, "claude-opus-5", _level)
            assert _payload["output_config"]["effort"] == _level, (_level, _payload)

        # The OpenAI stream needs the terminal check the Anthropic one grew.
        from desmos import openai as _oai_stream

        try:
            _oai_stream.read_sse(
                [
                    'data: {"type":"response.output_item.added","item":{"type":"message","id":"m"}}',
                    "",
                ],
                "gpt-5.6-sol",
            )
            raise AssertionError("a truncated Responses stream must not read as finished")
        except RuntimeError as exc:
            assert "response.completed" in str(exc), exc

        import threading

        import desmos.transport.complete as C

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

        # --- auth: file schema, credential precedence, masking (no network) ---
        import base64
        import json
        import os
        import time
        import urllib.parse

        from desmos.transport import auth as _auth

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
        from desmos.transport import openai as _oai
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

        call_item = {
            "id": "ct_1",
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": "call_1",
            "name": "syscall",
            "input": '<exec op="python">OPENAI_SYSCALL_EVAL = 40 + 2\nprint(OPENAI_SYSCALL_EVAL)</exec>',
        }
        call_resp = {
            "role": "assistant",
            "model": "gpt-5.6-sol",
            "content": _oai._blocks_from_items([call_item]),
            "usage": {},
            "stop_reason": "end_turn",
        }
        kept_call = _ac(call_resp)
        assert text_of(call_resp) == "", "typed syscall input is not assistant speech"
        assert kept_call[0]["input"].endswith("</" + "exec>")
        replay = _oai.to_input(
            [
                {"role": "assistant", "content": kept_call},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "custom_tool_call_output",
                            "call_id": "call_1",
                            "output": '<result tag="exec">42</result>',
                        }
                    ],
                },
            ]
        )
        assert replay == [
            call_item,
            {
                "type": "custom_tool_call_output",
                "call_id": "call_1",
                "output": '<result tag="exec">42</result>',
            },
        ], replay
        tools = _oai.payload_for("gpt-5.6-sol", "system", [], 100)["tools"]
        assert len(tools) == 1 and tools[0]["type"] == "custom" and tools[0]["name"] == "syscall"

        final_resp = {
            "role": "assistant",
            "model": "gpt-5.6-sol",
            "content": [{"type": "text", "text": "done"}],
            "usage": {},
            "stop_reason": "end_turn",
        }
        replies = iter([call_resp, final_resp])
        w_call = new_world(cwd, persist=False, ns={})
        w_call.model = "gpt-5.6-sol"
        w_call.complete_fn = lambda *_args: next(replies)
        from desmos.loop import run_turns as _run_openai

        assert _run_openai(w_call, "calculate", max_turns=3, quiet=True) == "done"
        assert w_call.ns["OPENAI_SYSCALL_EVAL"] == 42
        typed_results = [
            b
            for m in w_call.messages
            if m.get("role") == "user" and isinstance(m.get("content"), list)
            for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "custom_tool_call_output"
        ]
        assert typed_results[0]["call_id"] == "call_1" and ">42<" in typed_results[0]["output"]

        bad_raw = call_item["input"] + " lousy?"
        bad_item = dict(call_item, id="ct_bad", call_id="call_bad", input=bad_raw)
        w_bad = new_world(cwd, persist=False, ns={})
        w_bad.model = "gpt-5.6-sol"
        bad_resp = {
            **call_resp,
            "content": _oai._blocks_from_items([bad_item]),
        }
        bad_replies = iter([bad_resp, final_resp])
        w_bad.complete_fn = lambda *_args: next(bad_replies)
        bad_events: list[dict] = []
        assert _run_openai(
            w_bad, "calculate", max_turns=3, quiet=True, on_event=bad_events.append
        ) == "done"
        assert "OPENAI_SYSCALL_EVAL" not in w_bad.ns, "invalid typed input must not dispatch"
        bad_outputs = [
            b
            for m in w_bad.messages
            if m.get("role") == "user" and isinstance(m.get("content"), list)
            for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "custom_tool_call_output"
        ]
        assert len(bad_outputs) == 1 and bad_outputs[0]["call_id"] == "call_bad", bad_outputs
        rejection_output = bad_outputs[0]["output"]
        assert "syscall input rejected" in rejection_output, bad_outputs
        assert (
            f'preserved as ns["rejects"][-1] ({len(bad_raw)} chars)' in rejection_output
        ), rejection_output
        assert w_bad.ns["rejects"][-1] == bad_raw, w_bad.ns["rejects"][-1]
        assert any(
            e.get("ev") == "result"
            and e.get("phase") == "done"
            and e.get("tag") == "syscall"
            for e in bad_events
        ), bad_events

        # sol splits a turn into a commentary preamble and a final_answer, and
        # some models stream reasoning verbatim rather than as a summary. Both
        # events were unhandled: the thinking pane stayed empty while reasoning
        # tokens were billed, and a refusal arrived as an empty reply the loop
        # read as "the model is done".
        raw_items = [
            {
                "id": "msg_c",
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "I'll look first."}],
            },
            {
                "id": "msg_f",
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "refusal", "refusal": "I can't help with that."}],
            },
        ]
        raw_events = [
            {"type": "response.reasoning_text.delta", "delta": "step one"},
            {"type": "response.refusal.delta", "delta": "I can't help with that."},
            {
                "type": "response.completed",
                "response": {"id": "r2", "status": "completed", "output": raw_items, "usage": {}},
            },
        ]
        sse2 = []
        for ev in raw_events:
            sse2.append("data: " + json.dumps(ev))
            sse2.append("")
        seen2: list[dict] = []
        resp2 = _oai.read_sse(iter(sse2), "gpt-5.6-sol", on_event=seen2.append)
        assert [e["text"] for e in seen2 if e["kind"] == "thinking_delta"] == ["step one"]
        assert [e["text"] for e in seen2 if e["kind"] == "text_delta"] == ["I can't help with that."]
        phases = [b.get("phase") for b in resp2["content"]]
        assert phases == ["commentary", "final_answer"], phases
        assert "I can't help with that." in text_of(resp2), "a refusal is the answer, not nothing"

        # gpt-5.6-sol ended every message from the seventeenth on with a stray
        # token after the closing tag. Nothing rewrites the message -- the
        # stored bytes must stay exact for the cached prefix -- but the parser
        # now reports what it left outside the calls.
        from desmos.scan import trailing_residue as _residue

        sol_tail = "<bash>rg -n data .</" + "bash> \n lousy? token. \n"
        assert _residue(sol_tail) == "lousy? token.", _residue(sol_tail)
        assert [b.tag for b in scan(sol_tail)] == ["bash"], "the call still dispatches"
        assert _residue("<usage/>") == "" and _residue("just prose") == ""
        assert _residue("prose before <usage/>") == "", "only what follows the last call counts"

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
        import desmos.transport.complete as _cmp

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

        # Live model switching is a real operation the model can perform, and
        # the failure it replaces was a model insisting it could not. So this
        # asserts the switch reaches the wire -- the model id the NEXT complete()
        # is called with -- not that some sentence is in the prompt.
        import desmos.transport.settings as _st

        seen: list[str] = []

        def recording_complete(model, system, messages, max_tokens, **_kw):
            seen.append(model)
            if len(seen) == 1:
                return {
                    "content": [
                        {"type": "text", "text": '<python>switch("claude-sonnet-4-6")</python>'}
                    ],
                    "usage": {},
                }
            return {"content": [{"type": "text", "text": "done"}], "usage": {}}

        w_sw = new_world(cwd, state_path=cwd / "switch.json")
        w_sw.model = "claude-opus-5"
        w_sw.complete_fn = recording_complete
        bind_step(w_sw)

        # Neither a real credential nor a write to ~/.desmos is what this proves.
        stub_path = cwd / "settings-not-written.json"
        real_usable, real_save = _st.usable, _st.save
        _st.usable = lambda _p: True
        _st.save = lambda _c: stub_path
        try:
            w_sw.ns["step"]("switch to sonnet")
        finally:
            _st.usable, _st.save = real_usable, real_save

        assert len(seen) >= 2, f"the switch turn never produced a second call: {seen}"
        assert seen[0] == "claude-opus-5", f"first turn used the wrong model: {seen}"
        assert seen[1] == "claude-sonnet-4-6", (
            f"switch() did not reach the wire -- turn 2 still called {seen[1]!r}. "
            "The model can only be believed about its own capabilities if they work."
        )
        assert w_sw.model == "claude-sonnet-4-6"

        # And it refuses a choice that is not real, rather than half-applying it.
        for bad in ("no-such-model-9", "claude-opus-5"):
            try:
                _st.switch(w_sw, bad, "not-an-effort")
            except ValueError:
                pass
            else:
                raise AssertionError(f"switch accepted {bad!r} with a bogus effort")
        assert w_sw.model == "claude-sonnet-4-6", "a rejected switch still mutated the world"

        from desmos.checks import anthropic_check, openai_check, transport_check

        transport_check.self_check()
        openai_check.self_check()
        anthropic_check.self_check()
