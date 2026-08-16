from __future__ import annotations

"""Subagents: fan out isolated child worlds, wait on them, collect results.

Shape borrowed from grok-build's xai-grok-subagent-resolution: a bundle of
persona/role/agent definitions, an EffectiveConfig resolved by precedence
(spawn override > agent definition > parent inheritance), an isolation mode,
and resume-from-a-completed-peer. Implemented against desmos' own loop.
"""

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from desmos.agents.subagent_contracts import Judgment, RunResult, TaskContract, judge, parse_run_result
from desmos.kernel import prices

# --- bundle -----------------------------------------------------------------

PERSONAS: dict[str, str] = {
    "researcher": "Map evidence before concluding. Cite file:line for every claim.",
    "builder": "Implement the smallest complete change, run its real entry point, and report artifacts.",
    "critic": "Look for what is wrong, missing, or unproven. Do not praise.",
    "security": "Threat-model trust boundaries, seek concrete exploit paths, and separate likelihood from impact.",
    "planner": "Compare viable designs, expose constraints and irreversible choices, then give an ordered plan.",
    "debugger": "Reproduce first, minimize the failure, localize the first wrong state, and distinguish cause from symptom.",
    "orchestrator": (
        "Decompose the task, fork children for evidence and edits, judge their "
        "reports, and integrate. Never guess at repository contents: you have no "
        "read tools of your own, so spawn an explore child to look around first."
    ),
}

# capability modes: which tags the child may use
CAPS: dict[str, tuple[str, ...]] = {
    "read": ("python", "bash", "skill", "todo", "find", "recall"),
    "edit": ("python", "bash", "edit", "skill", "reload", "todo", "find", "recall"),
    # No bash, python, edit, or shell: an orchestrator delegates. It looks at
    # the world only through children it forks (until read-only probes land).
    # <find> is a read-only probe it may run without a child.
    "orchestrator": ("agents", "memory", "system", "skill", "find"),
    "full": (),  # empty tuple == inherit everything
}

# Sol is the default for advanced judgment/implementation; Luna handles cheap,
# bounded discovery. Every field remains a spawn-time override. The legacy
# names are aliases so existing callers keep working.
AGENTS: dict[str, dict[str, Any]] = {
    "general": {"persona": "builder", "capability": "edit", "model": "gpt-5.6-sol"},
    "worker": {"persona": "builder", "capability": "edit", "model": "gpt-5.6-sol"},
    "explore": {"persona": "researcher", "capability": "read", "model": "gpt-5.6-luna"},
    "scout": {"persona": "researcher", "capability": "read", "model": "gpt-5.6-luna"},
    "review": {"persona": "critic", "capability": "read", "model": "gpt-5.6-sol"},
    "reviewer": {"persona": "critic", "capability": "read", "model": "gpt-5.6-sol"},
    "security": {"persona": "security", "capability": "read", "model": "gpt-5.6-sol"},
    "planner": {"persona": "planner", "capability": "read", "model": "gpt-5.6-sol"},
    "sniffer": {"persona": "debugger", "capability": "read", "model": "gpt-5.6-luna"},
    # budget 1 by default: an orchestrator that cannot fork is useless.
    "orchestrator": {"persona": "orchestrator", "capability": "orchestrator", "model": "gpt-5.6-sol", "budget": 1},
}

ROLE_GUIDE: dict[str, str] = {
    "scout": "fast repository reconnaissance and evidence maps",
    "worker": "implementation and verification with edit capability",
    "reviewer": "independent spec, diff, and evidence criticism",
    "security": "trust-boundary, abuse-case, and vulnerability audit",
    "planner": "architecture options, constraints, and ordered design plans",
    "sniffer": "failure reproduction, minimization, and first-wrong-state localization",
}


@dataclass
class EffectiveConfig:
    agent: str = "general"
    persona: str | None = None
    persona_instructions: str | None = None
    capability: str = "edit"
    model: str | None = None
    thinking: str | None = None
    cwd: str | None = None
    context: str = "new"  # new | resumed
    # Requested depth budget for the spawned run: how many spawn levels may
    # exist below it. None inherits (spawner's remaining budget - 1; 0 at root).
    budget: int | None = None
    # Launch-time prompt controls. system_prompt replaces the generated child
    # prompt; system_append augments it. user_input replaces the rendered task,
    # while task_template transforms it with a required {task} placeholder.
    system_prompt: str | None = None
    system_append: str | None = None
    user_input: str | None = None
    task_template: str | None = None
    # Re-anchor long runs without imposing a ceiling. Zero/None disables.
    guidance_every_turns: int | None = 8
    guidance_reminder: str | None = None
    # None lets legacy free-text tasks infer whether they claim observations.
    # Typed contracts carry their own explicit requirement.
    require_tool_use: bool | None = None


def resolve(agent: str = "general", **over: Any) -> EffectiveConfig:
    """override > agent definition > default. Unknown agent is a hard error."""
    if agent not in AGENTS:
        raise KeyError(f"unknown agent {agent!r}; have {sorted(AGENTS)}")
    d = dict(AGENTS[agent])
    d.update({k: v for k, v in over.items() if v is not None})
    persona = d.get("persona")
    if persona and persona not in PERSONAS:
        raise KeyError(f"unknown persona {persona!r}; have {sorted(PERSONAS)}")
    cap = d.get("capability", "edit")
    if cap not in CAPS:
        raise KeyError(f"unknown capability {cap!r}; have {sorted(CAPS)}")
    guidance_every = d.get("guidance_every_turns", 8)
    if guidance_every is not None and int(guidance_every) < 0:
        raise ValueError("guidance_every_turns must be non-negative or None")
    budget = d.get("budget")
    if budget is not None and int(budget) < 0:
        raise ValueError("budget must be non-negative or None")
    return EffectiveConfig(
        agent=agent,
        persona=persona,
        persona_instructions=PERSONAS.get(persona or ""),
        capability=cap,
        model=d.get("model"),
        thinking=d.get("thinking"),
        cwd=d.get("cwd"),
        context=d.get("context", "new"),
        budget=(int(budget) if budget is not None else None),
        system_prompt=d.get("system_prompt"),
        system_append=d.get("system_append"),
        user_input=d.get("user_input"),
        task_template=d.get("task_template"),
        guidance_every_turns=(int(guidance_every) if guidance_every else None),
        guidance_reminder=d.get("guidance_reminder"),
        require_tool_use=(
            bool(d["require_tool_use"])
            if d.get("require_tool_use") is not None
            else None
        ),
    )


# --- runtime ----------------------------------------------------------------

@dataclass
class Run:
    id: str
    task: str
    cfg: EffectiveConfig
    contract: TaskContract | None = None
    structured: bool = False
    # Tree coordinates, fixed at spawn time from the spawning run: `parent` is
    # that run's id (None when the root world spawned this run) and `depth` is
    # spawner.depth + 1 (root spawns are 0). `budget` is the remaining spawn
    # depth below this run — data on the run, and spawn() discovers the run
    # from the calling world dispatch() bound, so it holds for a bare spawn,
    # the <agents> tag, and <python>; a detached thread resolves to nothing
    # and is refused outright. At 0 the child world carries no <agents> scope
    # and spawn() answers with a refusal string. `generation` is the parent
    # world's generation at spawn.
    parent: str | None = None
    depth: int = 0
    budget: int = 0
    generation: int = 0
    killed: bool = False
    state: str = "pending"  # pending | running | done | stopped | failed
    stage: str = "queued"
    progress: str = ""
    stop_reason: str = ""
    result: str = ""
    error: str = ""
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    #: The model the child actually ran on, and what its usage priced out at.
    #: `cfg.model` is None whenever the child inherits the parent's model --
    #: which was 184 of 289 runs on disk, every one of them unpriceable after
    #: the fact. Resolve it once, at world creation, and keep the number with
    #: the run that spent it.
    model: str = ""
    cost_usd: float = 0.0
    started: float = 0.0
    ended: float = 0.0
    retries: int = 0
    steers: int = 0
    guidance_reminders: int = 0
    observed_tools: list[str] = field(default_factory=list)
    run_result: RunResult | None = None
    judgment: Judgment | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def secs(self) -> float:
        end = self.ended or time.time()
        return round(end - self.started, 1) if self.started else 0.0

    def brief(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.cfg.agent,
            "task": self.task[:160],
            "parent": self.parent,
            "depth": self.depth,
            "budget": self.budget,
            "state": self.state,
            "stage": self.stage,
            "progress": self.progress[:160],
            "stop_reason": self.stop_reason,
            "accepted": self.judgment.accepted if self.judgment is not None else None,
            "secs": self.secs,
            "turns": self.turns,
            "steers": self.steers,
            "guidance_reminders": self.guidance_reminders,
            "observed_tools": list(self.observed_tools),
            "model": self.model,
            "cost_usd": self.cost_usd,
            "usage": dict(self.usage),
            "out": (self.result or self.error)[:120],
        }


RUNS: dict[str, Run] = {}
# Parent world per run id, for rerun(): the world donates model/complete_fn/cwd
# and receives the pending notice. Not on Run: asdict/_persist must stay JSON.
# ponytail: lost on reload_sdk (loop.py copies RUNS only); rerun then falls
# back to the bound PARENT world.
_WORLDS: dict[str, Any] = {}
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="subagent")
_LOCK = threading.Lock()
DIR = Path(".desmos/subagents")


#: How many finished run records to keep on disk. The directory had 289 files
#: and no policy: nothing read the old ones, nothing summed them, and nothing
#: ever deleted one. The ledger below is what survives pruning.
RUNS_KEEP = 200
LEDGER = DIR / "ledger.jsonl"
#: `_persist` runs on every state transition and a finished run is persisted
#: more than once (result, then judgment), so the ledger needs a once-per-run
#: guard or the same dollars are counted twice.
_LEDGERED: set[str] = set()


def _append_ledger(run: Run) -> None:
    """One line per finished run: the part worth keeping after the file goes.

    Pruning a run record must not lose what it cost. This is append-only and
    tiny -- id, agent, model, turns, tokens, dollars -- so a year of children
    still summarises in one pass.
    """
    row = {
        "id": run.id,
        "ts": run.ended or run.started or time.time(),
        "agent": run.cfg.agent,
        "model": run.model,
        "state": run.state,
        "turns": run.turns,
        "secs": run.secs,
        "usage": dict(run.usage),
        "cost_usd": run.cost_usd,
    }
    with LEDGER.open("a") as handle:
        handle.write(json.dumps(row, default=str) + "\n")


def _prune_runs(keep: int | None = None) -> int:
    """Drop the oldest finished run files. Returns how many were removed.

    `keep` is read at call time, not bound as a default: a default argument
    freezes RUNS_KEEP at import and no test (or setting) can move it after.
    """
    keep = RUNS_KEEP if keep is None else keep
    files = sorted(DIR.glob("*.json"), key=lambda f: f.stat().st_mtime)
    excess = len(files) - keep
    if excess <= 0:
        return 0
    removed = 0
    for path in files[:excess]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _persist(run: Run) -> None:
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        rec = {k: v for k, v in asdict(run).items() if k != "messages"}
        rec["cfg"] = asdict(run.cfg)
        (DIR / f"{run.id}.json").write_text(json.dumps(rec, indent=2, default=str))
        if run.state in ("done", "stopped", "failed") and run.id not in _LEDGERED:
            _LEDGERED.add(run.id)
            _append_ledger(run)
            _prune_runs()
    except OSError:
        pass


def _scoped_tags(capability: str, contract: TaskContract | None) -> set[str] | None:
    """Tags the child may run: capability ∩ contract. None means every tag.

    Raises when the two share nothing. An empty scope is not a strict child,
    it is a child with no syscalls at all: its prompt lists none while still
    demanding one, so the run can only end in no_tool_evidence. Spawn-time is
    where that is cheap to say; the model discovering it costs a real run.
    """
    allowed: set[str] | None = set(CAPS[capability]) or None
    if contract is not None and contract.allowed_tools:
        permitted = set(contract.allowed_tools)
        allowed = permitted if allowed is None else allowed & permitted
        if not allowed:
            raise ValueError(
                f"contract allowed_tools {sorted(permitted)} share no tag with capability "
                f"{capability!r} ({', '.join(sorted(CAPS[capability]))}): the child would "
                "have no syscalls"
            )
    return allowed


def _child_world(
    cfg: EffectiveConfig,
    parent: Any,
    contract: TaskContract | None = None,
    *,
    todo_actor: str | None = None,
    budget: int = 0,
):
    from desmos.kernel.loop import new_world, seed_builtins

    cwd = Path(cfg.cwd) if cfg.cwd else parent.cwd
    # persist=False: do not load or write the parent's harness.json
    w = new_world(cwd, state_path=None, ns={}, persist=False)
    seed_builtins(w)
    w.model = cfg.model or parent.model
    w.thinking = cfg.thinking or parent.thinking
    allowed = _scoped_tags(cfg.capability, contract)
    if budget <= 0 and allowed is not None:
        # An exhausted budget removes the spawn surface itself, not just the
        # spawn() answer: the tag is outside the frozen dispatch scope.
        allowed.discard("agents")
    if allowed is not None:
        # The parent-todo bridge is a separate append-only capability. Typed
        # task contracts still receive it even when their ordinary tool list is
        # narrower; dispatch intercepts it before any parent handler is reached.
        if todo_actor is not None:
            allowed.add("todo")
        from desmos.kernel.dispatch import set_scope

        # The prune keeps the child's prompt truthful (subagent_prompt reads
        # w.tools) and keeps evidence counting honest. set_scope is what
        # actually enforces the scope: dispatch answers the frozen tags without
        # consulting w.tools, and install_resources -- which runs at the top of
        # every turn, not only on <reload> -- refills that dict from disk.
        for name in list(w.tools):
            if name not in allowed:
                del w.tools[name]
        set_scope(w, allowed)
    # <agents> is how a world reaches spawn(). In the root it is a grown tool,
    # which a fresh persist=False child never loads -- so a capability that
    # scopes 'agents' must also supply the tag here, or the child's prompt
    # teaches a syscall that answers unknown-tag. Same handler the root's
    # grown tool wraps; spawn() attributes the launch to this world's run via
    # dispatch()'s caller binding, so budget and depth nest without the child
    # cooperating. At budget 0 the tag is removed instead (install_resources
    # may re-add one from an extension at the top of every turn; the scope
    # above keeps refusing it). The budget itself is enforced in spawn() on
    # every path — the tag, <python>, a detached thread.
    if budget <= 0:
        w.tools.pop("agents", None)
    elif allowed is None or "agents" in allowed:
        from desmos.agents import subagent_tool
        from desmos.kernel.types import Tool

        w.tools["agents"] = Tool(
            name="agents",
            doc=(
                "subagents: body 'spawn <agent> [model=..] : task' forks a child; "
                "'status', 'wait', 'result <id>'; or a JSON "
                "spawn_many/spawn/wait/status/result command"
            ),
            handler=subagent_tool.handle,
        )
    if cfg.persona_instructions:
        w.notes["persona"] = cfg.persona_instructions
    w.notes["subagent"] = (
        "You are a subagent. Finish the task and report. Your final message is "
        "the only thing the parent sees, so put the answer there, not in a syscall."
    )
    if contract is not None:
        w.notes["task-contract"] = (
            "The typed task contract is authoritative. Stay inside its tool and path scope. "
            "Return the required JSON result with evidence for every claim and passed check."
        )
    from desmos.agents.subagent_prompt import child_system_prompt

    generated = child_system_prompt(w, cfg, contract, budget=budget)
    w.system_override = cfg.system_prompt if cfg.system_prompt is not None else generated
    if cfg.system_append:
        w.system_override = w.system_override.rstrip() + "\n\n" + cfg.system_append.strip()
    w.complete_fn = getattr(parent, "complete_fn", None)
    parent_todo = parent.tools.get("todo")
    if todo_actor is not None and parent_todo is not None and parent_todo.handler is not None:
        from desmos.kernel.dispatch import set_child_todo_handler
        from desmos.kernel.types import Tool

        # This marker makes the syscall visible in the child prompt and scope;
        # dispatch intercepts it before the marker can run.
        w.tools["todo"] = Tool(
            name="todo",
            doc=(
                "append a parent todo: action=append; body is the new item. "
                "Existing parent rows cannot be changed"
            ),
            handler=lambda _body: "child todo dispatch marker",
        )
        set_child_todo_handler(w, parent_todo.handler, actor=f"subagent:{todo_actor}")
    return w


def _user_prompt(run: Run) -> str:
    """Render the initial child user block from launch-time controls."""
    rendered_task = (
        run.contract.prompt()
        if run.structured and run.contract is not None
        else (
            f"{run.task}\n\n"
            "Return concise prose covering the summary, evidence, unresolved items, "
            "and checks. Do not return JSON."
        )
    )
    if run.cfg.task_template is not None:
        if "{task}" not in run.cfg.task_template:
            raise ValueError("task_template must contain {task}")
        rendered_task = run.cfg.task_template.format(task=rendered_task)
    return run.cfg.user_input if run.cfg.user_input is not None else rendered_task


def _guidance_prompt(run: Run) -> str:
    """Concise re-anchor for a child that is still working after N turns."""
    if run.cfg.guidance_reminder:
        return run.cfg.guidance_reminder
    lines = [
        "Task guidance reminder: continue from the evidence already collected; do not restart.",
        f"Objective: {run.task}",
    ]
    if run.contract is not None:
        if run.contract.acceptance_checks:
            lines.append("Acceptance checks still to satisfy:")
            lines.extend(f"- {check}" for check in run.contract.acceptance_checks)
        lines.append(f"Required deliverable: {run.contract.deliverable_schema}")
        if run.contract.non_goals:
            lines.append("Stay outside these non-goals:")
            lines.extend(f"- {item}" for item in run.contract.non_goals)
    lines.append(
        "Use tools for remaining evidence. When complete, stop calling tools and return the entire final deliverable."
    )
    return "\n".join(lines)


def _execute(run: Run, parent: Any) -> None:
    from desmos.kernel.loop import run_turns

    run.state = "running"
    run.stage = "starting"
    run.progress = "building isolated child world"
    run.started = time.time()
    _persist(run)

    def publish_progress() -> None:
        _persist(run)
        _emit(
            {
                "ev": "subagent",
                "phase": "progress",
                "id": run.id,
                "parent": run.parent,
                "depth": run.depth,
                "task": run.task,
                "stage": run.stage,
                "progress": run.progress,
                "turns": run.turns,
                "usage": dict(run.usage),
            }
        )

    try:
        if run.killed:
            run.state = "stopped"
            run.stop_reason = "killed"
            run.stage = "stopped"
            run.progress = "killed before start"
            return
        w = _child_world(
            run.cfg,
            parent,
            run.contract if run.structured else None,
            todo_actor=run.id,
            budget=run.budget,
        )
        # The world remembers which run it executes. spawn() reads this off the
        # `parent` world it is handed, so a nested spawn lands in the tree with
        # the spawning run as parent and its depth + 1.
        w.subagent_run = run.id
        # Resolve the inherited model here, where it is known. cfg.model is
        # None for every child that takes the parent's model, and a run record
        # with no model cannot be priced afterwards.
        run.model = str(w.model or "")
        if run.cfg.context == "resumed" and run.messages:
            w.messages = list(run.messages)

        def child_event(ev: dict[str, Any]) -> None:
            kind = ev.get("ev")
            if kind == "turn":
                # run_turns resets its local n for each guidance segment; the
                # child log is the stable cumulative turn count.
                run.turns = len(w.log) + 1
                run.stage = "executing"
                run.progress = f"model turn {run.turns}"
                publish_progress()
            elif kind == "complete":
                for key, value in (ev.get("usage") or {}).items():
                    if isinstance(value, int):
                        run.usage[key] = run.usage.get(key, 0) + value
                run.progress = f"completed model turn {ev.get('n') or run.turns}"
                publish_progress()
            elif kind == "result" and ev.get("phase") == "done":
                tag = str(ev.get("tag") or "tool")
                if tag in w.tools:
                    run.observed_tools.append(tag)
                    run.progress = f"collected {tag} evidence"
                else:
                    run.progress = f"ignored unknown {tag} tag"
                publish_progress()
            payload = {k: v for k, v in ev.items() if k != "ev"}
            _emit(
                {
                    "ev": "child",
                    "id": run.id,
                    "parent": run.parent,
                    "depth": run.depth,
                    "kind": kind,
                    **payload,
                }
            )

        def guidance_after_turn(n: int) -> str | None:
            interval = run.cfg.guidance_every_turns
            if interval is None or n % interval:
                return None
            run.guidance_reminders += 1
            run.stage = "guidance"
            run.progress = f"task guidance reminder {run.guidance_reminders}"
            publish_progress()
            return _guidance_prompt(run)

        out = run_turns(
            w,
            _user_prompt(run),
            quiet=True,
            on_event=child_event,
            on_continue=guidance_after_turn,
            should_stop=lambda: run.killed,
        )
        if run.structured and run.contract is not None:
            require_tool = run.contract.require_tool_use
        elif run.cfg.require_tool_use is not None:
            require_tool = run.cfg.require_tool_use
        else:
            # The shared child prompt states this unconditionally: "A run with
            # no observed syscall is rejected, regardless of how plausible its
            # report sounds." It used to be conditional on whether the task
            # text happened to contain one of 19 trigger words, so "summarise
            # the design" skipped the steer while "check the design" got it.
            # Keep the promise the child was given.
            require_tool = True
        no_tool_failure = False
        if require_tool and not run.observed_tools and not run.killed:
            run.steers += 1
            run.stage = "steering"
            run.progress = "no syscall observed; requiring action"
            publish_progress()
            out = run_turns(
                w,
                "You finished without using any tool. That result is unevidenced and will "
                "be rejected. Use one of your available syscalls now, inspect the task with "
                "a real call, read its result, and only then return the complete answer.",
                quiet=True,
                on_event=child_event,
                on_continue=guidance_after_turn,
                should_stop=lambda: run.killed,
            )
            if not run.observed_tools:
                no_tool_failure = True

        run.result = out

        run.turns = len(w.log)
        run.messages = w.messages
        total: dict[str, int] = {}
        for entry in w.log:
            for key, value in (entry.get("usage") or {}).items():
                if isinstance(value, int):
                    total[key] = total.get(key, 0) + value
        run.usage = total
        run.cost_usd = round(prices.cost(total, run.model), 6)

        if run.killed:
            run.state = "stopped"
            run.stop_reason = "killed"
            run.stage = "stopped"
            run.progress = "killed by intervention"
            return
        if no_tool_failure:
            run.state = "failed"
            run.stop_reason = "no_tool_evidence"
            run.error = "child completed twice without using a syscall"
        else:
            run.state = "done"
            run.stop_reason = "completed"

        if no_tool_failure:
            run.stage = "failed"
            run.progress = run.error
        else:
            run.stage = "judging" if run.structured else "completed"
            run.progress = "validating declared evidence" if run.structured else "child finished"
        if run.structured and run.contract is not None:
            run.run_result = parse_run_result(
                run.result,
                terminal_state=run.state,
                stop_reason=run.stop_reason,
                usage=run.usage,
                duration=run.secs,
                retries=run.retries,
            )
            run.judgment = judge(
                run.contract,
                run.run_result,
                tuple(run.observed_tools),
            )
            if run.run_result.stop_reason == "invalid_result":
                run.stop_reason = "invalid_result"
            run.stage = "accepted" if run.judgment.accepted else "rejected"
            run.progress = (
                "all acceptance checks have evidence"
                if run.judgment.accepted
                else "; ".join(run.judgment.reasons[:3])
            )
    except Exception as exc:  # noqa: BLE001 - a dead child must not kill the parent
        run.error = f"{type(exc).__name__}: {exc}"
        run.state = "failed"
        run.stage = "failed"
        run.stop_reason = "exception"
        run.progress = run.error
    finally:
        run.ended = time.time()
        _persist(run)
        _emit(
            {
                "ev": "subagent",
                "phase": run.state,
                "id": run.id,
                "parent": run.parent,
                "depth": run.depth,
                "task": run.task,
                "stage": run.stage,
                "progress": run.progress,
                "stop_reason": run.stop_reason,
                "accepted": run.judgment.accepted if run.judgment is not None else None,
                "secs": run.secs,
                "turns": run.turns,
                "usage": dict(run.usage),
                "result": (run.result or "")[:800],
                "error": run.error,
            }
        )

def _contract_for(
    task: str | TaskContract,
    simple: dict[str, Any] | None = None,
) -> tuple[TaskContract, bool]:
    if simple is None:
        return (task, True) if isinstance(task, TaskContract) else (TaskContract.legacy(str(task)), False)
    if isinstance(task, TaskContract):
        raise ValueError("simple scope cannot be combined with a TaskContract")
    if not isinstance(simple, dict):
        raise TypeError("simple scope must be an object")
    allowed = {"paths", "write", "checks", "tools", "depends", "evidence"}
    unknown = sorted(set(simple) - allowed)
    if unknown:
        raise ValueError(f"unknown simple scope fields: {unknown}")
    return TaskContract.simple(str(task), **simple), True


def spawn(
    task: str | TaskContract,
    agent: str = "general",
    *,
    resume: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    budget: int | None = None,
    system_prompt: str | None = None,
    system_append: str | None = None,
    user_input: str | None = None,
    task_template: str | None = None,
    guidance_every_turns: int | None = None,
    guidance_reminder: str | None = None,
    simple: dict[str, Any] | None = None,
    parent: Any = None,
    _register_pending: bool = True,
    **over: Any,
) -> str:
    """Start a child immediately after its typed dependencies are accepted.

    Returns the run id — or, when the spawning run's depth budget is exhausted
    (or it was killed, or the caller has no run context at all), a refusal
    string. A refused spawn is a result the parent reads, never an exception.
    """
    # The spawning run is discovered from the CALLING world — the one bound by
    # dispatch() around the executing syscall — never from the parent kwarg: a
    # budget gate keyed on an argument the caller chooses is a gate a bare
    # spawn() walks around. _execute tags each child world with the run it
    # executes; a root world carries no tag, so a root spawn is never
    # budget-refused. A caller that resolves to nothing (a detached thread
    # outside any dispatched turn) cannot prove a budget, so it gets none.
    caller = _caller_world()
    if caller is _UNRESOLVED:
        return (
            "spawn refused: this call carries no run context (a detached thread "
            "outside any dispatched turn), so its depth budget cannot be proven. "
            "Spawn from your own turn instead."
        )
    spawner = RUNS.get(getattr(caller, "subagent_run", None) or "")
    if spawner is not None and (spawner.killed or spawner.budget <= 0):
        why = "the spawning run was killed" if spawner.killed else "depth budget exhausted"
        return (
            f"spawn refused: {why} (run {spawner.id} at depth {spawner.depth}, "
            f"budget {spawner.budget}). Finish the task yourself and report."
        )
    # The kwarg's remaining job: donate model/complete_fn/cwd and receive the
    # pending notice. It defaults to the calling world itself, so a child's
    # spawn resumes the child's own loop, not the root's.
    parent = parent if parent is not None else (caller if caller is not None else _parent())
    explicit = {
        "model": model,
        "thinking": thinking,
        "budget": budget,
        "system_prompt": system_prompt,
        "system_append": system_append,
        "user_input": user_input,
        "task_template": task_template,
        "guidance_every_turns": guidance_every_turns,
        "guidance_reminder": guidance_reminder,
    }
    over.update({key: value for key, value in explicit.items() if value is not None})
    contract, structured = _contract_for(task, simple)
    for dependency in contract.dependencies:
        prior = RUNS.get(dependency)
        if prior is None:
            raise ValueError(f"unknown dependency {dependency!r}")
        if prior.state in {"pending", "running"}:
            raise ValueError(f"dependency {dependency!r} has not settled")
        if prior.structured:
            if prior.judgment is None or not prior.judgment.accepted:
                raise ValueError(f"dependency {dependency!r} was not accepted")
        elif prior.state != "done":
            # A legacy child is never judged -- judgment is only set on the
            # structured branch -- so demanding a verdict made every dependency
            # on a plain spawn() fail. Finishing cleanly is its whole verdict.
            raise ValueError(
                f"dependency {dependency!r} did not finish: {prior.state}/{prior.stop_reason}"
            )

    cfg = resolve(agent, **over)
    # Raises on a contract whose tool scope and capability do not overlap, here
    # rather than in the pool thread that would otherwise build the world.
    _scoped_tags(cfg.capability, contract if structured else None)
    # Budget: inherited from the spawner, decremented one level; an explicit
    # request can only narrow it (a child cannot mint budget). Root spawns get
    # exactly what was asked for, default 0 (children that cannot fork).
    if spawner is not None:
        inherited = spawner.budget - 1
        run_budget = min(cfg.budget, inherited) if cfg.budget is not None else inherited
    else:
        run_budget = cfg.budget if cfg.budget is not None else 0
    run = Run(
        id=uuid.uuid4().hex[:8],
        task=contract.objective,
        cfg=cfg,
        contract=contract,
        structured=structured,
        parent=spawner.id if spawner is not None else None,
        depth=spawner.depth + 1 if spawner is not None else 0,
        budget=run_budget,
        generation=int(getattr(parent, "generation", 0) or 0),
    )
    if resume:
        src = RUNS.get(resume)
        if src is None or src.state != "done":
            raise ValueError(f"cannot resume from {resume!r}: not a completed run")
        if src.cfg.agent != cfg.agent:
            raise ValueError(f"resume identity mismatch: {src.cfg.agent} != {cfg.agent}")
        run.messages = list(src.messages)
        run.cfg.context = "resumed"
    _launch(run, parent, _register_pending)
    return run.id


def _launch(run: Run, parent: Any, register_pending: bool) -> None:
    """Register, announce, and submit a run. Shared by spawn() and rerun()."""
    with _LOCK:
        RUNS[run.id] = run
    _WORLDS[run.id] = parent
    _emit(
        {
            "ev": "subagent",
            "phase": "started",
            "id": run.id,
            "parent": run.parent,
            "depth": run.depth,
            "agent": run.cfg.agent,
            "persona": run.cfg.persona or "",
            "task": run.task,
            "structured": run.structured,
            "model": run.cfg.model or getattr(parent, "model", ""),
            "generation": run.generation,
        }
    )
    _POOL.submit(_execute, run, parent)
    # Nothing in the parent turn waits on this. The step ends normally and the
    # loop resumes it when the child settles, so a spawn costs no idle turn and
    # a queued follow-up is never stuck behind a sleeping call.
    if register_pending:
        from desmos.agents import pending as _pending

        _pending.register(parent, f"subagent {run.cfg.agent} {run.id}", lambda: _settle(run))


def _settle(run: Run) -> str:
    while run.state in ("pending", "running"):
        time.sleep(0.05)
    return child_notice(run)


def child_notice(run: Run) -> str:
    """The parent's pending notice for a settled child (contract C5).

    The compression lives here, never in the child: the raw result stays whole
    on the run record, in .desmos/subagents/<id>.json, and in the trajectory.
    """
    if run.judgment is None:
        verdict = "unjudged"
    else:
        verdict = "accepted" if run.judgment.accepted else "rejected"
    summary = ""
    if run.run_result is not None:
        summary = run.run_result.summary
    summary = summary or run.result or run.error or run.stop_reason
    summary = " ".join(summary.split())[:200]
    head = f"[{run.id} {run.state} depth={run.depth}] {run.task[:80]} — {verdict}: {summary}"
    hint = _inspect_hint(run)
    return f"{head[:400]}\n{hint}" if hint else head[:400]


def _inspect_hint(run: Run) -> str:
    """How the parent opens this run's substructure, from the python kernel.

    The notice is 200 characters of summary standing in for a run that may hold
    claims, evidence, checks and a verdict. Without this line the parent reads
    the summary and stops -- which is how a rejected run gets reported upward
    as a success.
    """
    rid = run.id
    if run.judgment is not None and not run.judgment.accepted:
        return (
            f"REJECTED. python: S.judgment('{rid}').reasons for why, "
            f"S.structured_result('{rid}') for claims/checks, S.result('{rid}') raw."
        )
    if run.structured:
        return (
            f"python: S.structured_result('{rid}') for claims/checks/evidence, "
            f"S.judgment('{rid}') for the verdict, S.result('{rid}') raw."
        )
    return f"python: S.result('{rid}') for the full report."


def kill_subtree(rid: str) -> str:
    """Intervention (contract C3): cancel a run and every descendant.

    Never raises; an unknown id is answered in prose. The kill is a flag each
    run's own loop reads (run_turns should_stop), so a running child settles
    as state=stopped / stop_reason=killed through the normal terminal path.
    """
    with _LOCK:
        if rid not in RUNS:
            return f"unknown run {rid}"
        # Walk RUNS by parent, transitively.
        # ponytail: O(tree * runs) scan; index children if RUNS ever gets big.
        ids = [rid]
        seen = {rid}
        i = 0
        while i < len(ids):
            for r in RUNS.values():
                if r.parent == ids[i] and r.id not in seen:
                    seen.add(r.id)
                    ids.append(r.id)
            i += 1
        live = [RUNS[x] for x in ids if RUNS[x].state in ("pending", "running")]
        for r in live:
            r.killed = True
    if not live:
        return f"kill {rid}: nothing running in a subtree of {len(ids)} run(s)"
    return f"kill {rid}: stopping {len(live)} of {len(ids)} run(s): " + " ".join(r.id for r in live)


def rerun(rid: str) -> str:
    """Intervention (contract C3): respawn a settled run's contract as a fresh
    run wired to the same parent world. Returns the new id or a refusal string;
    never raises."""
    with _LOCK:
        src = RUNS.get(rid)
    if src is None:
        return f"unknown run {rid}"
    if src.state in ("pending", "running"):
        return f"run {rid} is still {src.state}; kill it before rerunning"
    parent = _WORLDS.get(rid) or _parent()
    run = Run(
        id=uuid.uuid4().hex[:8],
        task=src.task,
        cfg=replace(src.cfg),
        contract=src.contract,
        structured=src.structured,
        parent=src.parent,
        depth=src.depth,
        budget=src.budget,
        generation=int(getattr(parent, "generation", 0) or 0),
    )
    _launch(run, parent, True)
    return run.id


PARENT: Any = None
_EMIT: Any = None


def set_emitter(fn: Any) -> None:
    """Bridge/TUI hook. Child threads call this; never send redacted ciphertext."""
    global _EMIT
    _EMIT = fn


def _emit(ev: dict[str, Any]) -> None:
    fn = _EMIT
    if fn is None:
        return
    try:
        fn(ev)
    except Exception:
        pass


def bind(world: Any) -> str:
    """Point subagents at the live parent world (model, cwd, thinking)."""
    global PARENT
    PARENT = world
    return f"subagents bound to {world.cwd}"


def _parent() -> Any:
    if PARENT is not None:
        return PARENT
    from desmos.kernel.loop import new_world

    # An unbound parent is a fallback, not a session. The persist=True default
    # loaded and saved cwd/.desmos/harness.sqlite3 -- a child process writing
    # the real harness's state.
    return new_world(Path.cwd(), state_path=None, persist=False)


#: spawn()'s answer for a caller that proves nothing: not a world, not None.
_UNRESOLVED = globals().get("_UNRESOLVED") or object()


def _caller_world() -> Any:
    """The world whose turn is executing this call, or _UNRESOLVED.

    Resolution the caller does not control: dispatch() binds the executing
    world around every syscall, so <python> and <agents> in any world -- root
    or child, on any thread -- resolve to that world. Without a binding, only
    the main thread is trusted: child runs execute on pool threads and threads
    they detach, never on the interpreter's main thread, so a main-thread call
    is the embedding root (checks, programmatic use). Anything else is
    _UNRESOLVED -- it cannot prove a budget, and spawn() refuses it.
    """
    from desmos.kernel.dispatch import CALLER_WORLD

    world = CALLER_WORLD.get()
    if world is not None:
        return world
    if threading.current_thread() is threading.main_thread():
        return PARENT  # may be None: an unbound root; _parent() supplies one
    return _UNRESOLVED


def wait(*ids: str, timeout: float = 600.0, poll: float = 0.5) -> list[dict[str, Any]]:
    """Block until the named runs settle (all of them if none named)."""
    with _LOCK:
        # spawn() inserts under _LOCK; iterating RUNS unlocked raised
        # "dictionary changed size during iteration" mid-fanout.
        targets = list(ids) or list(RUNS)
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = [i for i in targets if i in RUNS and RUNS[i].state in ("pending", "running")]
        if not pending:
            break
        time.sleep(poll)
    out = []
    for i in targets:
        run = RUNS.get(i)
        # A synthetic Run, not a hand-written literal: the unknown branch used
        # to return six keys while every sibling was a full brief(), so a caller
        # reading w["budget"] KeyErrored on exactly the case it exists for.
        out.append(
            run.brief()
            if run is not None
            else Run(id=i, task="", cfg=EffectiveConfig(agent=""), state="unknown", stage="unknown").brief()
        )
    return out


def status() -> list[dict[str, Any]]:
    with _LOCK:
        runs = list(RUNS.values())
    # brief() outside the lock: it reads time.time() and copies lists, and
    # spawn() must not queue behind a TUI status render.
    return [r.brief() for r in runs]


def result(rid: str) -> str:
    r = RUNS.get(rid)
    if r is None:
        return f"<unknown {rid}>"
    return r.result or r.error or f"<{r.state}>"


def structured_result(rid: str) -> RunResult | None:
    run = RUNS.get(rid)
    return run.run_result if run is not None else None


def judgment(rid: str) -> Judgment | None:
    run = RUNS.get(rid)
    return run.judgment if run is not None else None


def spawn_many(specs: list[dict[str, Any]], *, parent: Any = None) -> list[str]:
    """Validate a batch, then enqueue every child without waiting between them.

    Each item requires ``task`` and may carry any normal spawn override plus
    ``agent`` and ``resume``. Validation is completed for the whole batch
    before the first child starts, avoiding a half-launched malformed batch.
    The shared executor provides bounded real concurrency.
    """
    prepared: list[tuple[str | TaskContract, str, str | None, dict[str, Any]]] = []
    for index, raw in enumerate(specs):
        if not isinstance(raw, dict):
            raise TypeError(f"spawn batch item {index} must be an object")
        item = dict(raw)
        if "task" not in item:
            raise ValueError(f"spawn batch item {index} has no task")
        task = item.pop("task")
        if not isinstance(task, (str, TaskContract)):
            raise TypeError(f"spawn batch item {index} task must be text or TaskContract")
        simple = item.pop("simple", None)
        contract, structured = _contract_for(task, simple)
        normalized: str | TaskContract = contract if structured else task
        agent = str(item.pop("agent", "general"))
        resume = item.pop("resume", None)
        cfg = resolve(agent, **item)
        _scoped_tags(cfg.capability, contract if structured else None)
        prepared.append((normalized, agent, resume, item))
    if parent is None:
        # Same default as spawn(): the calling world, so a child's batch
        # resumes the child's own loop. Budget attribution never comes from
        # this value -- spawn() re-resolves the caller itself.
        caller = _caller_world()
        parent = caller if caller is not None and caller is not _UNRESOLVED else _parent()
    ids = [
        spawn(
            task,
            agent=agent,
            resume=resume,
            parent=parent,
            _register_pending=False,
            **over,
        )
        for task, agent, resume, over in prepared
    ]

    # A batch is one parent decision. Child lifecycle events remain individual
    # for the TUI, but the model loop resumes once, after the complete batch is
    # available, rather than once per child with siblings still running.
    # A refused spawn (depth budget) returns its refusal string in place of an
    # id; only real runs are waited on.
    live = [i for i in ids if i in RUNS]
    if not live:
        return ids
    from desmos.agents import pending as _pending

    def _settle_group() -> str:
        rows = [_settle(RUNS[i]) for i in live]
        reads = ", ".join(f'result("{i}")' for i in live)
        return "subagent group settled:\n" + "\n".join(rows) + f"\nRead: {reads}"

    _pending.register(parent, "subagent group " + " ".join(live), _settle_group)
    return ids


def fanout(tasks: list[str | TaskContract], agent: str = "explore", **over: Any) -> list[str]:
    """Spawn one child per task concurrently. Returns ids in input order."""
    parent = over.pop("parent", None)
    return spawn_many(
        [{"task": task, "agent": agent, **over} for task in tasks],
        parent=parent,
    )


def gather(ids: list[str], timeout: float = 600.0) -> str:
    """Wait, then concatenate each child's final answer."""
    wait(*ids, timeout=timeout)
    # RUNS[i] KeyErrored on an id that never existed; result() already answers
    # "<unknown id>". Loop over ids, not over wait()'s briefs: wait() with no
    # ids means every run, which would turn gather([]) into "dump everything".
    parts = []
    for i in ids:
        run = RUNS.get(i)
        parts.append(f"--- {i} [{run.cfg.agent if run else 'unknown'}] ---\n{result(i)}")
    return "\n\n".join(parts)
