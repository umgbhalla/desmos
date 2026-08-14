from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import desmos.subagent as subagents
from desmos.loop import new_world
from desmos.subagent_contracts import Budget, TaskContract

LT = chr(60)


def structured(*, evidenced: bool = True, usage: int = 8) -> dict[str, Any]:
    evidence = [
        {
            "kind": "command",
            "reference": "python arithmetic probe",
            "detail": "observed 2 + 2 == 4",
        }
    ]
    payload = {
        "summary": "proved four",
        "claims": [{"text": "two plus two is four", "evidence": evidence if evidenced else []}],
        "artifacts": [],
        "changed_paths": [],
        "checks": [
            {
                "name": "arithmetic is correct",
                "passed": True,
                "evidence": evidence if evidenced else [],
            }
        ],
        "failures": [],
        "unresolved": [],
    }
    return response(json.dumps(payload), usage)


def response(text: str, usage: int = 1) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": max(0, usage - 1), "output_tokens": 1},
        "stop_reason": "end_turn",
    }


def tool_call() -> dict[str, Any]:
    return response(f"{LT}python>print(2 + 2){LT}/python>", 2)


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        parent = new_world(root, state_path=None, persist=False, ns={})
        parent.model = "claude-opus-5"
        events: list[dict[str, Any]] = []
        systems: list[str] = []
        responses = [
            tool_call(),
            structured(),
            tool_call(),
            structured(),
            tool_call(),
            structured(evidenced=False),
            structured(usage=12),
            response("I would inspect the repository, but no results were available."),
            tool_call(),
            response("recovered report with observed arithmetic"),
            response("I plan to inspect it."),
            response("I still cannot inspect it."),
        ]

        def complete(_model: str, system: str, _messages: list[dict[str, Any]], _max: int) -> dict[str, Any]:
            systems.append(system)
            return responses.pop(0)

        parent.complete_fn = complete
        old_dir = subagents.DIR
        old_emitter = subagents._EMIT
        old_parent = subagents.PARENT
        subagents.DIR = root / "runs"
        subagents.RUNS.clear()
        subagents.set_emitter(events.append)
        subagents.bind(parent)
        try:
            contract = TaskContract(
                objective="Prove that two plus two is four.",
                non_goals=("Do not edit files.",),
                deliverable_schema="One supported arithmetic claim.",
                required_evidence=("command",),
                acceptance_checks=("arithmetic is correct",),
                allowed_tools=("python",),
                budget=Budget(max_turns=3, max_tokens=100, wall_seconds=5),
            )
            rid = subagents.spawn(contract, agent="explore", parent=parent)
            settled = subagents.wait(rid, timeout=5, poll=0.01)[0]
            run = subagents.RUNS[rid]
            assert settled["task"] == contract.objective
            assert settled["state"] == "done" and settled["stage"] == "accepted"
            assert settled["stop_reason"] == "completed" and settled["accepted"] is True
            assert settled["budget"]["tokens"] == {"used": 10, "limit": 100}
            assert settled["observed_tools"] == ["python"]
            assert run.judgment is not None and run.judgment.accepted
            assert systems[0] == systems[1], "one child keeps one dedicated system prompt"
            assert "# act before reporting" in systems[0]
            assert "# anthropic lane" in systems[0]
            assert "tui:" not in systems[0] and "memory summary" not in systems[0]
            assert len(systems[0]) < 4000, "child prompt regressed toward the parent catalog"
            assert any(
                ev.get("phase") == "progress"
                and ev.get("stage") == "executing"
                and ev.get("task") == contract.objective
                for ev in events
            )

            dependent = TaskContract(
                objective="Reuse the accepted arithmetic result.",
                required_evidence=("command",),
                acceptance_checks=("arithmetic is correct",),
                allowed_tools=("python",),
                budget=Budget(max_turns=2, max_tokens=100, wall_seconds=5),
                dependencies=(rid,),
            )
            dependent_id = subagents.spawn(dependent, agent="explore", parent=parent)
            subagents.wait(dependent_id, timeout=5, poll=0.01)
            assert subagents.judgment(dependent_id).accepted is True

            rejected_id = subagents.spawn(contract, agent="explore", parent=parent)
            subagents.wait(rejected_id, timeout=5, poll=0.01)
            rejected = subagents.RUNS[rejected_id]
            assert rejected.state == "done" and rejected.stage == "rejected"
            assert rejected.judgment is not None and not rejected.judgment.accepted
            assert any("lacks evidence" in reason for reason in rejected.judgment.reasons)

            tiny = TaskContract(
                objective="Exceed the token budget.",
                required_evidence=("command",),
                acceptance_checks=("arithmetic is correct",),
                allowed_tools=("python",),
                budget=Budget(max_turns=2, max_tokens=5, wall_seconds=5),
            )
            tiny_id = subagents.spawn(tiny, agent="explore", parent=parent)
            subagents.wait(tiny_id, timeout=5, poll=0.01)
            tiny_run = subagents.RUNS[tiny_id]
            assert tiny_run.state == "stopped" and tiny_run.stop_reason == "token_budget"
            assert tiny_run.judgment is not None and not tiny_run.judgment.accepted

            # The observed production failure: narration with zero calls. The
            # controller gives exactly one corrective step, then accepts only
            # because a real syscall/result follows.
            recovery_id = subagents.spawn("Inspect the repository.", agent="explore", parent=parent)
            subagents.wait(recovery_id, timeout=5, poll=0.01)
            recovery = subagents.RUNS[recovery_id]
            assert recovery.state == "done" and recovery.result.startswith("recovered report")
            assert recovery.steers == 1 and recovery.observed_tools == ["python"]
            assert any(
                ev.get("id") == recovery_id
                and ev.get("stage") == "steering"
                and "requiring action" in ev.get("progress", "")
                for ev in events
            )

            failure_id = subagents.spawn("Inspect but ignore your tools.", agent="explore", parent=parent)
            subagents.wait(failure_id, timeout=5, poll=0.01)
            failure = subagents.RUNS[failure_id]
            assert failure.state == "failed"
            assert failure.stop_reason == "no_tool_evidence"
            assert failure.steers == 1 and not failure.observed_tools

            child = subagents._child_world(subagents.resolve("explore"), parent, contract)
            assert set(child.tools) <= {"python"} and "agents" not in child.tools
            parent.model = "gpt-5.6-sol"
            openai_child = subagents._child_world(subagents.resolve("explore"), parent, contract)
            assert "# openai lane" in openai_child.system_override
            assert "# anthropic lane" not in openai_child.system_override
            assert list((root / "runs").glob("*.json"))
            assert not responses, f"unused fake responses: {len(responses)}"
        finally:
            subagents.wait(timeout=5, poll=0.01)
            subagents.RUNS.clear()
            subagents.DIR = old_dir
            subagents.set_emitter(old_emitter)
            subagents.PARENT = old_parent


if __name__ == "__main__":
    self_check()
    print("subagent contract check ok")
