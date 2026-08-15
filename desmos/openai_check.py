"""Checks for the OpenAI Responses input builder that the main suite does not cover."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from desmos.openai import to_input

PY_CLOSE = "</python>"
SH_CLOSE = "</bash>"


def _call(call_id: str, body: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "",
                "openai": {
                    "type": "custom_tool_call",
                    "name": "syscall",
                    "call_id": call_id,
                    "input": body,
                },
            }
        ],
    }


def _output(call_id: str, text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "custom_tool_call_output", "call_id": call_id, "output": text}
        ],
    }


def _kinds(items: list[dict[str, Any]]) -> list[str]:
    return [item.get("type", "") for item in items]


def self_check() -> None:
    py_call = "<python>1" + PY_CLOSE
    sh_call = "<bash>true" + SH_CLOSE

    paired = to_input([_call("call_a", py_call), _output("call_a", "one")])
    ids = [i["call_id"] for i in paired if i.get("type") == "custom_tool_call_output"]
    assert ids == ["call_a"], paired

    # The head of the transcript was trimmed: the call is gone, the output is not.
    # Emitting it unpaired is a fatal 400 that poisons every later request.
    orphan = to_input([_output("call_gone", "stranded output text")])
    assert "custom_tool_call_output" not in _kinds(orphan), orphan
    assert "stranded output text" in str(orphan), orphan
    assert any(i.get("role") == "user" for i in orphan), orphan

    # A later paired call still works when an earlier one was orphaned.
    mixed = to_input(
        [
            _output("call_gone", "stranded"),
            _call("call_b", sh_call),
            _output("call_b", "ok"),
        ]
    )
    live = [i["call_id"] for i in mixed if i.get("type") == "custom_tool_call_output"]
    assert live == ["call_b"], mixed

    # An output must never be matched by a call that comes after it: the stray
    # output degrades to text, and the call gets its own synthetic answer.
    backwards = to_input([_output("call_c", "early"), _call("call_c", "late")])
    assert "early" in str(backwards[0]), backwards
    assert _kinds(backwards)[1:] == ["custom_tool_call", "custom_tool_call_output"], backwards

    # The mirror of the orphan: a call the transcript never answered. Left bare
    # it is a fatal "No tool output found for custom tool call", and it wedges
    # every later request in the session -- which is exactly how one malformed
    # syscall body killed a live run. Every call must leave here answered.
    from desmos.openai import UNANSWERED_CALL

    wedged = to_input([_call("call_d", py_call), {"role": "user", "content": "next"}])
    assert _kinds(wedged)[:2] == ["custom_tool_call", "custom_tool_call_output"], wedged
    assert wedged[1]["call_id"] == "call_d" and wedged[1]["output"] == UNANSWERED_CALL, wedged
    calls = [i["call_id"] for i in wedged if i.get("type") == "custom_tool_call"]
    answers = [i["call_id"] for i in wedged if i.get("type") == "custom_tool_call_output"]
    assert sorted(calls) == sorted(answers), wedged

    # A properly answered call must not gain a second output.
    once = to_input([_call("call_e", py_call), _output("call_e", "ok")])
    assert _kinds(once).count("custom_tool_call_output") == 1, once

    # Exercise the normal step entry point and then the actual Responses input
    # conversion. History is already in world.messages, while runtime facts are
    # already in the system prompt; only live namespace shape belongs beside
    # the current task.
    from desmos.loop import bind_step, new_world

    task = "CURRENT_TASK_UNIQUE_7d39"
    with TemporaryDirectory() as td:
        cwd = Path(td)
        world = new_world(cwd, state_path=cwd / "state.sqlite3", ns={"live_marker": [1, 2]})
        world.model = "gpt-5.6-sol"
        world.prior = [{"prompt": "OLD_TASK_SHOULD_NOT_RECAP", "speech": "OLD_REPLY_SHOULD_NOT_RECAP"}]
        payloads: list[list[dict[str, Any]]] = []

        def complete_once(_model: str, _system: str, messages: list[dict[str, Any]], _max: int) -> dict[str, Any]:
            payloads.append(to_input(messages))
            return {"content": [{"type": "text", "text": "done"}], "usage": {}}

        world.complete_fn = complete_once
        bind_step(world)
        assert world.ns["step"](task).strip() == "done"

        assert len(payloads) == 1, payloads
        wire = str(payloads[0])
        assert wire.count(task) == 1, wire
        assert "prior steps:" not in wire and "OLD_TASK_SHOULD_NOT_RECAP" not in wire, wire
        envelope = world.messages[0]["content"]
        assert "generation:" not in envelope and "cwd:" not in envelope, envelope
        assert "prior steps:" not in envelope and "prompt:" not in envelope, envelope
        assert "ns:" in envelope and "live_marker: list, len=2" in envelope, envelope
        assert envelope.count(task) == 1, envelope

    # A malformed custom-tool payload is still a valid provider call and must
    # receive one typed output. Nothing inside the mixed payload may run, and
    # the loop must continue so the model can issue a corrected call.
    from desmos.loop import run_turns

    with TemporaryDirectory() as td:
        cwd = Path(td)
        world = new_world(cwd, state_path=cwd / "recover.sqlite3", ns={"live_marker": [1, 2]})
        world.model = "gpt-5.6-sol"
        attempts = 0
        bad_input = "<python>live_marker.append(3)</python> stray prose"

        def complete_recover(
            _model: str, _system: str, messages: list[dict[str, Any]], _max: int
        ) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                provider_call = {
                    "type": "custom_tool_call",
                    "name": "syscall",
                    "call_id": "bad_call_1",
                    "input": bad_input,
                }
                return {
                    "content": [{**provider_call, "openai": provider_call}],
                    "usage": {},
                }
            output = messages[-1]["content"]
            assert isinstance(output, list) and output[0]["type"] == "custom_tool_call_output", output
            assert output[0]["call_id"] == "bad_call_1", output
            assert "syscall input rejected" in output[0]["output"], output
            assert "nothing ran" in output[0]["output"], output
            return {"content": [{"type": "text", "text": "recovered"}], "usage": {}}

        world.complete_fn = complete_recover
        events: list[dict[str, Any]] = []
        answer = run_turns(world, "recover malformed call", quiet=True, on_event=events.append)
        assert answer == "recovered", answer
        assert attempts == 2, attempts
        assert world.ns["live_marker"] == [1, 2], "a partial malformed payload was dispatched"
        outputs = [
            block
            for message in world.messages
            if message.get("role") == "user" and isinstance(message.get("content"), list)
            for block in message["content"]
            if block.get("type") == "custom_tool_call_output"
        ]
        assert len(outputs) == 1 and outputs[0]["call_id"] == "bad_call_1", outputs
        assert any(
            event.get("ev") == "result"
            and event.get("phase") == "done"
            and event.get("tag") == "syscall"
            for event in events
        ), events

    print("openai input check ok")


if __name__ == "__main__":
    self_check()
