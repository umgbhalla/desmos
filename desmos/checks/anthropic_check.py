from __future__ import annotations

"""The Anthropic syscall-tool wire.

openai_check.py does this for Responses. Same contract, a different wire: the
call is a `tool_use` block, its answer is a `tool_result` keyed by that call's
id, and either one arriving without the other is a hard 400 that poisons every
later request.

Everything here forces DESMOS_TOOL_SYSCALLS on, because desmos.check pins it
off for its own run -- the fake responses there are written in the prose
dialect this replaced, and prose parsing is still what the flag-off path does.
"""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from desmos.complete import (
    SYSCALL_TOOL,
    apply_stream_event,
    assemble_message,
    assistant_content,
    cached_payload,
    read_sse,
    tool_result_text,
    wire_content,
)
from desmos.dialect import dialect, tool_syscalls
from desmos.loop import result_content, set_syscall_body, syscall_body, syscall_call
from desmos.types import Block

MODEL = "claude-opus-5"
SYSTEM = "abi text\n\n# tools\ncatalog text"


@contextmanager
def _flag(value: str) -> Iterator[None]:
    was = os.environ.get("DESMOS_TOOL_SYSCALLS")
    os.environ["DESMOS_TOOL_SYSCALLS"] = value
    try:
        yield
    finally:
        if was is None:
            os.environ.pop("DESMOS_TOOL_SYSCALLS", None)
        else:
            os.environ["DESMOS_TOOL_SYSCALLS"] = was


def _call(body: str, call_id: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": "syscall", "input": {"input": body}}


def _check_flag() -> None:
    with _flag("1"):
        assert tool_syscalls(MODEL)
        assert tool_syscalls("gpt-5.6-sol")
    with _flag("0"):
        assert not tool_syscalls(MODEL)
        # The escape hatch is for the Anthropic side only. Responses has no
        # prose path to fall back to.
        assert tool_syscalls("gpt-5.6-sol")


def _check_payload() -> None:
    messages = [{"role": "user", "content": "hi"}]
    with _flag("1"):
        on = cached_payload(MODEL, SYSTEM, messages, 100)
    with _flag("0"):
        off = cached_payload(MODEL, SYSTEM, messages, 100)
    assert [t["name"] for t in on["tools"]] == ["syscall"], on.get("tools")
    assert on["tools"][0]["input_schema"]["required"] == ["input"], on["tools"]
    assert on["tool_choice"]["disable_parallel_tool_use"] is True, on["tool_choice"]
    assert "tools" not in off and "tool_choice" not in off, off.keys()
    # The tool is static, so it must not be rebuilt per call in a way that
    # lets a caller mutate the module constant through the payload.
    on["tools"][0]["name"] = "mutated"
    assert SYSCALL_TOOL["name"] == "syscall"


def _check_dialect() -> None:
    with _flag("1"):
        assert "You have one tool, `syscall`" in dialect(MODEL)
    with _flag("0"):
        assert "You have one tool, `syscall`" not in dialect(MODEL)


def _check_call_extraction() -> None:
    call = _call("<python>1</python>")
    assert syscall_call([call]) is call
    assert syscall_body(call) == "<python>1</python>"
    set_syscall_body(call, "<python>2</python>")
    assert call["input"] == {"input": "<python>2</python>"}
    # A tool_use for something that is not the syscall tool is not a syscall.
    assert syscall_call([{"type": "tool_use", "id": "x", "name": "other", "input": {}}]) is None
    # Missing id is fatal: the wire cannot pair a result to it.
    try:
        syscall_call([{"type": "tool_use", "id": "", "name": "syscall", "input": {}}])
    except RuntimeError as exc:
        assert "without id" in str(exc), exc
    else:
        raise AssertionError("a tool_use with no id must not pass")


def _check_result_pairing() -> None:
    call = _call("<python>1</python>")
    out = result_content([(Block("python", "1", {}), "1")], [call], Path("."))
    assert isinstance(out, list) and out[0]["type"] == "tool_result", out
    assert out[0]["tool_use_id"] == "toolu_1", out
    assert "<result" in out[0]["content"], out
    # No call in the turn: the result is plain text, as it always was.
    plain = result_content([(Block("python", "1", {}), "1")], [], Path("."))
    assert isinstance(plain, str), plain


def _check_replay() -> None:
    call = _call("<python>1</python>")
    kept = assistant_content({"content": [call, {"type": "text", "text": "ok"}]})
    assert [b["type"] for b in kept] == ["tool_use", "text"], kept
    wire = wire_content(kept)
    assert wire[0] == call, wire[0]


def _check_orphans() -> None:
    call = _call("<python>1</python>")
    answer = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "<result/>"}
    with _flag("1"):
        paired = cached_payload(
            MODEL,
            SYSTEM,
            [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": [call]},
                {"role": "user", "content": [answer]},
            ],
            100,
        )
        kinds = [[b["type"] for b in m["content"]] for m in paired["messages"]]
        assert kinds == [["text"], ["tool_use"], ["tool_result"]], kinds

        # A result whose call was folded off the head degrades to text rather
        # than 400ing the request.
        orphan = cached_payload(MODEL, SYSTEM, [{"role": "user", "content": [answer]}], 100)
        assert [b["type"] for b in orphan["messages"][0]["content"]] == ["text"], orphan
        assert "<result/>" in orphan["messages"][0]["content"][0]["text"]

        # A call nothing answered -- the harness raised between appending the
        # assistant turn and appending its result -- is answered here.
        unanswered = cached_payload(
            MODEL,
            SYSTEM,
            [{"role": "user", "content": "go"}, {"role": "assistant", "content": [call]}],
            100,
        )
        tail = unanswered["messages"][-1]
        assert tail["role"] == "user", tail
        assert tail["content"][0]["type"] == "tool_result", tail
        assert "nothing was executed" in tail["content"][0]["content"], tail

        # ...and it rides in the next user message when there is one, so the
        # request does not grow a second consecutive user turn.
        merged = cached_payload(
            MODEL,
            SYSTEM,
            [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": [call]},
                {"role": "user", "content": "next"},
            ],
            100,
        )
        roles = [m["role"] for m in merged["messages"]]
        assert roles == ["user", "assistant", "user"], roles
        last = [b["type"] for b in merged["messages"][-1]["content"]]
        assert last == ["tool_result", "text"], last

    assert tool_result_text({"content": [{"type": "text", "text": "a"}]}) == "a"


def _check_stream() -> None:
    state: dict[str, Any] = {}
    apply_stream_event(state, {"type": "message_start", "message": {"role": "assistant"}})
    apply_stream_event(
        state,
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_9", "name": "syscall", "input": {}},
        },
    )
    body = json.dumps({"input": "<python>1</python>"})
    for chunk in (body[:7], body[7:]):
        apply_stream_event(
            state,
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": chunk},
            },
        )
    apply_stream_event(state, {"type": "content_block_stop", "index": 0})
    message = assemble_message(state)
    block = message["content"][0]
    assert block["input"] == {"input": "<python>1</python>"}, block
    assert "_partial_json" not in block, block
    assert syscall_body(syscall_call(assistant_content(message))) == "<python>1</python>"

    # Malformed JSON leaves input empty rather than raising: loop.turn answers
    # the call with the rejection note and asks for a corrected one.
    broken: dict[str, Any] = {}
    apply_stream_event(broken, {"type": "message_start", "message": {}})
    apply_stream_event(
        broken,
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t", "name": "syscall", "input": {}},
        },
    )
    apply_stream_event(
        broken,
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"input": "<py'},
        },
    )
    apply_stream_event(broken, {"type": "content_block_stop", "index": 0})
    assert assemble_message(broken)["content"][0]["input"] == {}


def _check_loop() -> None:
    """The real turn loop, driven by tool_use replies."""
    import tempfile

    from desmos.loop import new_world, run_turns

    with tempfile.TemporaryDirectory() as tmp, _flag("1"):
        cwd = Path(tmp)
        world = new_world(cwd, state_path=None, persist=False, ns={"doc": "hello world"})
        world.model = MODEL

        def replies(_model, _system, messages, _max_tokens):
            answered = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for m in messages
                if isinstance(m.get("content"), list)
                for b in m["content"]
            )
            if answered:
                return {"content": [{"type": "text", "text": "11"}], "usage": {}}
            return {"content": [_call("<python>len(doc)</python>")], "usage": {}}

        world.complete_fn = replies
        spoken = run_turns(world, "how long is doc?", quiet=True)
        assert spoken.strip() == "11", spoken
        answer = world.messages[2]["content"]
        assert answer[0]["type"] == "tool_result", answer
        assert answer[0]["tool_use_id"] == "toolu_1", answer
        assert "11" in answer[0]["content"], answer

        # XML in an assistant message is not dispatched. It ends the step with
        # a note instead of running, which is the whole point of the tool.
        prose = new_world(cwd, state_path=None, persist=False, ns={"doc": "x"})
        prose.model = MODEL
        prose.complete_fn = lambda *_: {
            "content": [{"type": "text", "text": "<python>1</python>"}],
            "usage": {},
        }
        run_turns(prose, "go", quiet=True)
        assert any(
            "emitted XML as speech" in str(m.get("content")) for m in prose.messages
        ), prose.messages

        # A call whose body is not dispatchable is recoverable: the tool gets a
        # typed answer and the loop asks again.
        bad = new_world(cwd, state_path=None, persist=False, ns={})
        bad.model = MODEL
        seen = {"n": 0}

        def malformed(_model, _system, messages, _max_tokens):
            seen["n"] += 1
            if seen["n"] == 1:
                return {"content": [_call("just prose, no tags")], "usage": {}}
            return {"content": [{"type": "text", "text": "sorry"}], "usage": {}}

        bad.complete_fn = malformed
        run_turns(bad, "go", quiet=True)
        assert seen["n"] == 2, seen
        assert any("syscall input rejected" in str(m.get("content")) for m in bad.messages)


def self_check() -> None:
    _check_flag()
    _check_payload()
    _check_dialect()
    _check_call_extraction()
    _check_result_pairing()
    _check_replay()
    _check_orphans()
    _check_stream()
    _check_abort()
    _check_loop()
    print("anthropic tool syscall check ok")


def _abort_events(closed: bool) -> list[dict[str, Any]]:
    lt = chr(60)
    evs: list[dict[str, Any]] = [
        {"type": "message_start", "message": {"role": "assistant", "content": []}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": "running "}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "the check"}},
        {"type": "content_block_stop", "index": 0},
    ]
    evs.append({"type": "content_block_start", "index": 1,
                "content_block": {"type": "tool_use", "id": "toolu_5",
                                  "name": "syscall", "input": {}}})
    partial = json.dumps({"input": lt + "python>1" + lt + "/python>"})
    if not closed:
        partial = partial[:14]
    evs.append({"type": "content_block_delta", "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": partial}})
    if closed:
        evs.append({"type": "content_block_stop", "index": 1})
    return evs


def _check_abort() -> None:
    """A stop mid-call cuts back to the last content_block_stop."""

    def run(evs: list[dict[str, Any]]) -> dict[str, Any]:
        lines: list[str] = []
        for ev in evs:
            lines.append("event: " + ev["type"])
            lines.append("data: " + json.dumps(ev))
            lines.append("")
        cut = {"now": False}

        def feed() -> Iterator[str]:
            for i, line in enumerate(lines):
                if i == len(lines) - 1:
                    cut["now"] = True
                yield line

        return read_sse(feed(), should_stop=lambda: cut["now"])

    cut = run(_abort_events(closed=False))
    assert [b.get("type") for b in cut["content"]] == ["text"], cut
    assert cut["content"][0]["text"] == "running the check", cut
    assert syscall_call(cut["content"]) is None, cut

    whole = run(_abort_events(closed=True))
    kinds = [b.get("type") for b in whole["content"]]
    assert kinds == ["text", "tool_use"], kinds
    assert syscall_body(syscall_call(whole["content"])).startswith(chr(60)), whole
