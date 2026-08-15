from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import desmos.agents.subagent as subagents
from desmos.loop import new_world
from desmos.subagent_contracts import TaskContract

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
            assert subagents.resolve("worker").model == "gpt-5.6-sol"
            assert subagents.resolve("scout").model == "gpt-5.6-luna"
            assert subagents.resolve("security").capability == "read"
            assert subagents.resolve("scout").guidance_every_turns == 8
            assert subagents.resolve("scout", guidance_every_turns=0).guidance_every_turns is None
            try:
                subagents.resolve("scout", guidance_every_turns=-1)
                raise AssertionError("negative guidance interval accepted")
            except ValueError:
                pass
            assert set(subagents.ROLE_GUIDE) == {
                "scout", "worker", "reviewer", "security", "planner", "sniffer"
            }

            contract = TaskContract(
                objective="Prove that two plus two is four.",
                non_goals=("Do not edit files.",),
                deliverable_schema="One supported arithmetic claim.",
                required_evidence=("command",),
                acceptance_checks=("arithmetic is correct",),
                allowed_tools=("python",),
            )
            rid = subagents.spawn(contract, agent="explore", parent=parent, model="claude-opus-5")
            settled = subagents.wait(rid, timeout=5, poll=0.01)[0]
            run = subagents.RUNS[rid]
            assert settled["task"] == contract.objective
            assert settled["state"] == "done" and settled["stage"] == "accepted", settled
            assert settled["stop_reason"] == "completed" and settled["accepted"] is True
            assert settled["usage"]["input_tokens"] + settled["usage"]["output_tokens"] == 10
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
                dependencies=(rid,),
            )
            dependent_id = subagents.spawn(dependent, agent="explore", parent=parent, model="claude-opus-5")
            subagents.wait(dependent_id, timeout=5, poll=0.01)
            assert subagents.judgment(dependent_id).accepted is True

            rejected_id = subagents.spawn(contract, agent="explore", parent=parent, model="claude-opus-5")
            subagents.wait(rejected_id, timeout=5, poll=0.01)
            rejected = subagents.RUNS[rejected_id]
            assert rejected.state == "done" and rejected.stage == "rejected"
            assert rejected.judgment is not None and not rejected.judgment.accepted
            assert any("lacks evidence" in reason for reason in rejected.judgment.reasons)

            # The observed production failure: narration with zero calls. The
            # controller gives exactly one corrective step, then accepts only
            # because a real syscall/result follows.
            recovery_id = subagents.spawn("Inspect the repository.", agent="explore", parent=parent, model="claude-opus-5")
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

            failure_id = subagents.spawn("Inspect but ignore your tools.", agent="explore", parent=parent, model="claude-opus-5")
            subagents.wait(failure_id, timeout=5, poll=0.01)
            failure = subagents.RUNS[failure_id]
            assert failure.state == "failed"
            assert failure.stop_reason == "no_tool_evidence"
            assert failure.steers == 1 and not failure.observed_tools

            child = subagents._child_world(subagents.resolve("explore"), parent, contract)
            assert set(child.tools) <= {"python"} and "agents" not in child.tools
            custom_cfg = subagents.resolve(
                "planner",
                model="gpt-5.6-luna",
                system_prompt="CUSTOM SYSTEM",
                system_append="CUSTOM APPEND",
                task_template="wrapped: {task}",
            )
            custom_child = subagents._child_world(custom_cfg, parent, contract)
            assert custom_child.model == "gpt-5.6-luna"
            assert custom_child.system_override == "CUSTOM SYSTEM\n\nCUSTOM APPEND"
            custom_run = subagents.Run(
                id="prompt", task=contract.objective, cfg=custom_cfg,
                contract=contract, structured=True,
            )
            assert subagents._user_prompt(custom_run).startswith("wrapped: Execute this typed task")
            custom_run.cfg.user_input = "REPLACEMENT USER BLOCK"
            assert subagents._user_prompt(custom_run) == "REPLACEMENT USER BLOCK"
            parent.model = "gpt-5.6-sol"
            openai_child = subagents._child_world(subagents.resolve("explore"), parent, contract)
            assert "# openai lane" in openai_child.system_override
            assert "# anthropic lane" not in openai_child.system_override
            assert list((root / "runs").glob("*.json"))
            assert not responses, f"unused fake responses: {len(responses)}"

            reminder_responses = [
                tool_call(),
                tool_call(),
                response("finished after the reminder"),
            ]
            reminder_inputs: list[str] = []

            def complete_with_reminder(
                _model: str, _system: str, messages: list[dict[str, Any]], _max: int
            ) -> dict[str, Any]:
                content = messages[-1].get("content", "")
                reminder_inputs.append(content if isinstance(content, str) else json.dumps(content))
                return reminder_responses.pop(0)

            parent.complete_fn = complete_with_reminder
            reminder_id = subagents.spawn(
                "Inspect the repository and report.",
                agent="explore",
                parent=parent,
                model="claude-opus-5",
                guidance_every_turns=2,
            )
            subagents.wait(reminder_id, timeout=5, poll=0.01)
            reminder_run = subagents.RUNS[reminder_id]
            assert reminder_run.state == "done", reminder_run.brief()
            assert reminder_run.turns == 3 and reminder_run.guidance_reminders == 1
            assert reminder_run.result == "finished after the reminder"
            assert any("Task guidance reminder:" in item for item in reminder_inputs)
            assert any(
                ev.get("id") == reminder_id
                and ev.get("stage") == "guidance"
                and ev.get("progress") == "task guidance reminder 1"
                for ev in events
            )
            assert not any("max_turns" in str(ev) for ev in events)
            assert not any("max_turns" in str(message) for message in reminder_run.messages)
            assert not reminder_responses
        finally:
            subagents.wait(timeout=5, poll=0.01)
            # Every spawn above registered a settle notice on `parent`, and
            # pending buckets by id(world): once this local world is freed, a
            # later world can reuse its address and inherit the undelivered
            # tasks — parallel_tool_check's notice count flaked exactly there.
            from desmos.agents import pending as _pending

            _pending.clear(parent)
            subagents.RUNS.clear()
            subagents.DIR = old_dir
            subagents.set_emitter(old_emitter)
            subagents.PARENT = old_parent


def parallel_tool_check() -> None:
    """The XML-facing batch waits at a barrier, proving both children started."""
    import threading
    from desmos import pending
    from desmos.agents.subagent_tool import handle

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        parent = new_world(root, state_path=None, persist=False, ns={})
        barrier = threading.Barrier(2)
        seen: list[tuple[str, str, str]] = []

        def complete(model: str, system: str, messages: list[dict[str, Any]], _max: int) -> dict[str, Any]:
            user = str(messages[-1]["content"])
            seen.append((model, system, user))
            barrier.wait(timeout=3)
            return response("parallel done")

        parent.complete_fn = complete
        old_dir = subagents.DIR
        old_parent = subagents.PARENT
        subagents.DIR = root / "runs"
        subagents.RUNS.clear()
        subagents.bind(parent)
        try:
            malformed = {
                "op": "spawn_many",
                "tasks": [
                    {"task": "would otherwise start", "agent": "scout"},
                    {"task": "invalid", "agent": "not-a-role"},
                ],
            }
            try:
                handle(json.dumps(malformed))
                raise AssertionError("malformed batch launched")
            except KeyError:
                pass
            assert not subagents.RUNS, "batch validation was not atomic"

            command = {
                "op": "spawn_many",
                "tasks": [
                    {
                        "task": "alpha",
                        "agent": "worker",
                        "model": "gpt-5.6-sol",
                        "system_prompt": "SYS-A",
                        "system_append": "APP-A",
                        "task_template": "TASK::{task}",
                        "guidance_every_turns": 3,
                        "guidance_reminder": "KEEP ALPHA",
                        "require_tool_use": False,
                    },
                    {
                        "task": "beta",
                        "agent": "scout",
                        "model": "gpt-5.6-luna",
                        "system_prompt": "SYS-B",
                        "user_input": "USER-B",
                        "require_tool_use": False,
                    },
                ],
            }
            launched = json.loads(handle(json.dumps(command)))
            assert launched["count"] == 2 and len(launched["ids"]) == 2
            notices = pending.outstanding(parent)
            assert len(notices) == 1, "spawn_many registered one callback per child"
            assert notices[0].name.startswith("subagent group "), notices[0].name
            settled = subagents.wait(*launched["ids"], timeout=5, poll=0.01)
            assert all(row["state"] == "done" for row in settled), settled
            assert {row[0] for row in seen} == {"gpt-5.6-sol", "gpt-5.6-luna"}
            assert any(row[1] == "SYS-A\n\nAPP-A" and "TASK::alpha" in row[2] for row in seen)
            assert any(row[1] == "SYS-B" and "USER-B" in row[2] for row in seen)
            alpha = subagents.RUNS[launched["ids"][0]]
            assert alpha.cfg.guidance_every_turns == 3
            assert alpha.cfg.guidance_reminder == "KEEP ALPHA"
        finally:
            subagents.wait(timeout=5, poll=0.01)
            pending.clear(parent)
            subagents.RUNS.clear()
            subagents.DIR = old_dir
            subagents.PARENT = old_parent




if __name__ == "__main__":
    self_check()
    parallel_tool_check()
    print("subagent contract check ok")
