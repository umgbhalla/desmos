from __future__ import annotations

"""Small, action-forcing system prompt for isolated child agents."""

from typing import Any

from desmos.transport.dialect import family
from desmos.agents.subagent_contracts import OBSERVABLE_EVIDENCE, TaskContract

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
Report observed facts directly; label inference and anything blocked or unproven.
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


# One entry per CAPS mode. The child is told what it can do from the same
# table that decides what it can do, so a new capability cannot ship with a
# prompt that still describes the old one.
_CAPABILITY_GUIDE: dict[str, str] = {
    "read": (
        "Read-only: investigate freely and change nothing. You have no edit "
        "tag; do not propose a patch as though you had applied it."
    ),
    "edit": (
        "Edit capability: make the smallest complete change, then run its real "
        "entry point. Report the command you ran and what it printed -- a "
        "change that was never executed is an unproven change."
    ),
    "orchestrator": (
        "Orchestrator: you have no bash, python, edit, or shell -- you cannot "
        "read files or run commands yourself. To look around, fork an explore "
        "child through the agents syscall and read its report; fork worker "
        "children for changes. Your job is decomposition, judgment, and the "
        "integrated final answer."
    ),
    "full": (
        "Full capability: you inherit every syscall the parent holds. Prefer "
        "the narrowest tool that answers the question."
    ),
}

_SCHEMA = """{
  "summary": "one paragraph: what you found or changed",
  "claims": [
    {"text": "a finding",
     "evidence": [{"kind": "file", "reference": "path/to/file.py:42", "detail": ""}]}
  ],
  "checks": [
    {"name": "exact acceptance check name", "passed": true,
     "evidence": [{"kind": "command", "reference": "python -m desmos check",
                   "detail": "239 passed"}]}
  ],
  "artifacts": [],
  "changed_paths": [],
  "failures": [],
  "unresolved": []
}"""


def _output_format(contract: TaskContract | None) -> str:
    """The literal object the child must emit.

    Naming the schema is not enough: the largest single rejection cause was
    `invalid structured result` -- children returning prose because the prompt
    only referred to "the JSON object required by the contract". Show it.
    """
    if contract is None:
        return ""
    lines = [
        "# output format",
        "Your final message is exactly one JSON object and nothing else: no "
        "prose before or after it, no code fence, no commentary.",
        "",
        _SCHEMA,
        "",
        "`kind` must be one of: " + ", ".join(sorted(OBSERVABLE_EVIDENCE)) + ".",
        "`reference` is the file:line, command, or artifact path you observed.",
        "Omit a list rather than inventing entries for it, but `summary` is "
        "never empty -- an empty summary is an automatic rejection.",
    ]
    if contract.acceptance_checks:
        lines.append(
            "Each acceptance check must appear in `checks` with its name copied "
            "EXACTLY as written here:"
        )
        lines.extend(f"  - {name}" for name in contract.acceptance_checks)
    if contract.required_evidence:
        lines.append(
            "At least one evidence entry of each of these kinds is required: "
            + ", ".join(contract.required_evidence)
        )
    return "\n".join(lines)


def _scope(cfg: Any, contract: TaskContract | None) -> str:
    lines = [
        "# scope",
        f"agent: {cfg.agent}",
        f"capability: {cfg.capability}",
    ]
    guide = _CAPABILITY_GUIDE.get(cfg.capability)
    if guide:
        lines.append(guide)
    if contract is None:
        lines.append("Return a cited prose report.")
        return "\n".join(lines)

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
    budget: int = 0,
) -> str:
    lane = _OPENAI if family(getattr(world, "model", "") or "") == "openai" else _ANTHROPIC
    shared = _SHARED
    if budget > 0:
        shared = shared.replace(
            "You cannot spawn children or ask the parent questions.",
            f"You may fork children of your own (remaining depth budget: {budget}); "
            "you cannot ask the parent questions.",
        )
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
    sections = [
        shared,
        _tool_lines(world),
        _scope(cfg, contract),
        _output_format(contract),
        persona + lane,
        runtime,
    ]
    return "\n\n".join(section for section in sections if section)
