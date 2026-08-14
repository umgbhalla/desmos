from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import desmos.subagent as subagents
from desmos.loop import new_world
from desmos.subagent_contracts import Budget, TaskContract


def _response(*, evidenced: bool = True, usage: int = 10, summary: str = "proved four") -> dict[str, Any]:
    evidence = [
        {
            "kind": "command",
            "reference": "python -c arithmetic",
            "detail": "observed 2 + 2 == 4",
        }
    ]
    payload = {
        "summary": summary,
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
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "usage": {"input_tokens": usage - 1, "output_tokens": 1},
        "stop_reason": "end_turn",
    }


def self_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        parent = new_world(root, state_path=None, persist=False, ns={})
        events: list[dict[str, Any]] = []
        responses = [_response(), _response(), _response(evidenced=False), _response(usage=12)]

        def complete(_model: str, _system: str, _messages: list[dict[str, Any]], _max: int) -> dict[str, Any]:
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
            assert settled["state"] == "done"
            assert settled["stage"] == "accepted"
            assert settled["stop_reason"] == "completed"
            assert settled["accepted"] is True
            assert settled["budget"]["turns"]["limit"] == 3
            assert settled["budget"]["tokens"] == {"used": 10, "limit": 100}
            assert run.run_result is not None
            assert run.judgment is not None and run.judgment.accepted
            assert any(
                ev.get("phase") == "progress"
                and ev.get("stage") == "executing"
                and ev.get("task") == contract.objective
                for ev in events
            )
            assert any(ev.get("id") == rid and ev.get("accepted") is True for ev in events)

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
            assert subagents.judgment(dependent_id) is not None
            assert subagents.judgment(dependent_id).accepted is True

            rejected_id = subagents.spawn(contract, agent="explore", parent=parent)
            subagents.wait(rejected_id, timeout=5, poll=0.01)
            rejected = subagents.RUNS[rejected_id]
            assert rejected.state == "done"
            assert rejected.stage == "rejected"
            assert rejected.judgment is not None and not rejected.judgment.accepted
            assert any("lacks evidence" in reason for reason in rejected.judgment.reasons)
            blocked = TaskContract(
                objective="Must not run after rejected evidence.",
                dependencies=(rejected_id,),
            )
            try:
                subagents.spawn(blocked, parent=parent)
            except ValueError as exc:
                assert "was not accepted" in str(exc)
            else:
                raise AssertionError("rejected dependency should block its dependent")

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
            assert tiny_run.state == "stopped"
            assert tiny_run.stop_reason == "token_budget"
            assert tiny_run.judgment is not None and not tiny_run.judgment.accepted
            assert tiny_run.brief()["budget"]["tokens"] == {"used": 12, "limit": 5}

            child = subagents._child_world(subagents.resolve("explore"), parent, contract)
            assert set(child.tools) <= {"python"}
            assert "agents" not in child.tools
            assert list((root / "runs").glob("*.json")), "structured runs should persist"
        finally:
            subagents.wait(timeout=5, poll=0.01)
            subagents.RUNS.clear()
            subagents.DIR = old_dir
            subagents.set_emitter(old_emitter)
            subagents.PARENT = old_parent


if __name__ == "__main__":
    self_check()
    print("subagent contract check ok")
