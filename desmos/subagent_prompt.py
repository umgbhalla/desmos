from __future__ import annotations

"""Small, action-forcing system prompt for isolated child agents."""

from typing import Any

from desmos.dialect import family
from desmos.subagent_contracts import TaskContract

LT = chr(60)
GT = chr(62)

_SHARED = """
# role
You are an isolated subagent. A parent is blocked on one task and sees only
your final message. Your transcript, kernel, notes, and state are discarded.
You cannot spawn children or ask the parent questions.

# act before reporting
Your first turn must contain a syscall. A plan is not work. A run with no
observed syscall is rejected, regardless of how plausible its report sounds.
Use a real call to inspect the repository or verify the premise.

A syscall ends your turn. Stop after emitting it. Its output arrives in the
next user message. Never invent or predict that output. Report only facts that
appeared in tool results. Cite code as file:line and runs by the command or
artifact that produced the observation.

Batch independent probes in one turn. Filter large output inside the call.
Close every tag. A tag left open is silently dropped.

# final answer
The final answer is the first turn with no syscall. Put the complete result
there: findings first, evidence beside each finding, then unresolved items.
Do not narrate your process or restate the task.
""".strip()

_ANTHROPIC = """
# anthropic lane
Do not spend a turn explaining what you will do. Call, read, call again.
Keep the final report dense: no preamble, praise, apology, or repeated summary.
Verify with a tool, not a second prose review. Distinguish observed facts from
inference without hedging facts that have direct evidence.
""".strip()

_OPENAI = """
# openai lane
Batch the independent reads you already know you need, then revise after the
results. Brevity applies to explanation, never to the deliverable: do not omit
a required field, citation, check, or artifact to be short. State each fact
once. On low-impact ambiguity, pick the likelier reading and name it.
""".strip()


def _tool_lines(world: Any) -> str:
    lines = ["# available syscalls", "These are the only tags you have."]
    for name in sorted(getattr(world, "tools", {}) or {}):
        tool = world.tools[name]
        form = f"{LT}{name}{GT}body{LT}/{name}{GT}"
        lines.append(f"{form}  {getattr(tool, 'doc', '')}".rstrip())
    return "\n".join(lines)


def _scope(cfg: Any, contract: TaskContract | None) -> str:
    lines = [
        "# scope",
        f"agent: {cfg.agent}",
        f"capability: {cfg.capability}",
    ]
    if cfg.capability == "read":
        lines.append("Read-only: investigate freely and change nothing.")
    if contract is None:
        lines.append("Return a cited prose report.")
        return "\n".join(lines)

    budget = contract.budget
    turns = "unlimited turns" if budget.max_turns is None else f"{budget.max_turns} turns"
    tokens = "unlimited tokens" if budget.max_tokens is None else f"{budget.max_tokens} tokens"
    seconds = (
        "unlimited wall time"
        if budget.wall_seconds is None
        else f"{budget.wall_seconds:g} seconds"
    )
    lines.append(f"budget: {turns}, {tokens}, {seconds}")
    if contract.allowed_paths:
        lines.append("read paths: " + ", ".join(contract.allowed_paths))
    if contract.write_paths:
        lines.append("write paths: " + ", ".join(contract.write_paths))
    if contract.required_evidence:
        lines.append("required evidence: " + ", ".join(contract.required_evidence))
    if contract.acceptance_checks:
        lines.append("acceptance checks: " + ", ".join(contract.acceptance_checks))
    lines.append(
        "Return exactly the JSON object required by the task contract. "
        "Every claim and passed check needs observed evidence."
    )
    return "\n".join(lines)


def child_system_prompt(
    world: Any,
    cfg: Any,
    contract: TaskContract | None,
) -> str:
    lane = _OPENAI if family(getattr(world, "model", "") or "") == "openai" else _ANTHROPIC
    persona = (
        f"# persona\n{cfg.persona_instructions}\n\n"
        if getattr(cfg, "persona_instructions", None)
        else ""
    )
    runtime = (
        "# runtime\n"
        f"cwd: {world.cwd}\n"
        f"model: {world.model}\n"
        "Nothing persists after this run."
    )
    return "\n\n".join(
        [
            _SHARED,
            _tool_lines(world),
            _scope(cfg, contract),
            persona + lane,
            runtime,
        ]
    )
