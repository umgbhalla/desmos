"""Checks for the OpenAI Responses input builder that the main suite does not cover."""

from __future__ import annotations

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

    print("openai input check ok")


if __name__ == "__main__":
    self_check()
