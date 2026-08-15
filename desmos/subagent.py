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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from desmos.subagent_contracts import Judgment, RunResult, TaskContract, judge, parse_run_result

# --- bundle -----------------------------------------------------------------

PERSONAS: dict[str, str] = {
    "terse": "Answer in the fewest words that are still correct. No preamble.",
    "researcher": "Map evidence before concluding. Cite file:line for every claim.",
    "builder": "Implement the smallest complete change, run its real entry point, and report artifacts.",
    "critic": "Look for what is wrong, missing, or unproven. Do not praise.",
    "security": "Threat-model trust boundaries, seek concrete exploit paths, and separate likelihood from impact.",
    "planner": "Compare viable designs, expose constraints and irreversible choices, then give an ordered plan.",
    "debugger": "Reproduce first, minimize the failure, localize the first wrong state, and distinguish cause from symptom.",
}

# capability modes: which tags the child may use
CAPS: dict[str, tuple[str, ...]] = {
    "read": ("python", "bash", "skill"),
    "edit": ("python", "bash", "edit", "skill", "reload"),
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
    return EffectiveConfig(
        agent=agent,
        persona=persona,
        persona_instructions=PERSONAS.get(persona or ""),
        capability=cap,
        model=d.get("model"),
        thinking=d.get("thinking"),
        cwd=d.get("cwd"),
        context=d.get("context", "new"),
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
    state: str = "pending"  # pending | running | done | stopped | failed
    stage: str = "queued"
    progress: str = ""
    stop_reason: str = ""
    result: str = ""
    error: str = ""
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
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
            "usage": dict(self.usage),
            "out": (self.result or self.error)[:120],
        }


def _legacy_requires_tool(task: str) -> bool:
    """Whether a free-text task makes claims that require observation."""
    words = {
        "analyze",
        "audit",
        "check",
        "debug",
        "edit",
        "explore",
        "find",
        "fix",
        "implement",
        "inspect",
        "map",
        "read",
        "research",
        "review",
        "run",
        "search",
        "test",
        "trace",
        "verify",
    }
    normalized = "".join(ch if ch.isalnum() else " " for ch in task.lower())
    return bool(words & set(normalized.split()))


RUNS: dict[str, Run] = {}
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="subagent")
_LOCK = threading.Lock()
DIR = Path(".desmos/subagents")


def _persist(run: Run) -> None:
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        rec = {k: v for k, v in asdict(run).items() if k != "messages"}
        rec["cfg"] = asdict(run.cfg)
        (DIR / f"{run.id}.json").write_text(json.dumps(rec, indent=2, default=str))
    except OSError:
        pass


_DEPTH = threading.local()


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


def _child_world(cfg: EffectiveConfig, parent: Any, contract: TaskContract | None = None):
    from desmos.loop import new_world, seed_builtins

    cwd = Path(cfg.cwd) if cfg.cwd else parent.cwd
    # persist=False: do not load or write the parent's harness.json
    w = new_world(cwd, state_path=None, ns={}, persist=False)
    seed_builtins(w)
    w.model = cfg.model or parent.model
    w.thinking = cfg.thinking or parent.thinking
    allowed = _scoped_tags(cfg.capability, contract)
    if allowed is not None:
        from desmos.dispatch import set_scope

        # The prune keeps the child's prompt truthful (subagent_prompt reads
        # w.tools) and keeps evidence counting honest. set_scope is what
        # actually enforces the scope: dispatch answers the frozen tags without
        # consulting w.tools, and install_resources -- which runs at the top of
        # every turn, not only on <reload> -- refills that dict from disk.
        for name in list(w.tools):
            if name not in allowed:
                del w.tools[name]
        set_scope(w, allowed)
    # Not cleanup: <agents> is how a world reaches spawn(). It is a grown tool,
    # not a frozen tag, so this pop only holds for the world as built --
    # install_resources re-registers extension and skill tools from cwd at the
    # top of every turn, and it is an extension that supplies <agents>.
    # What holds afterwards is the scope for a scoped child ('agents' is in no
    # CAPS entry) and, for an unscoped 'full' child, the depth cap: _DEPTH.n is
    # 1 for the whole run_turns call in the child's worker thread, so an
    # in-thread spawn() raises there. The cap is thread-local, so a child that
    # starts its own thread or shells out to a new process is past it -- which
    # is why the pop and the cap are both kept, and why anyone handing 'full'
    # an explicit tag set must leave 'agents' out of it.
    w.tools.pop("agents", None)
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
    from desmos.subagent_prompt import child_system_prompt

    generated = child_system_prompt(w, cfg, contract)
    w.system_override = cfg.system_prompt if cfg.system_prompt is not None else generated
    if cfg.system_append:
        w.system_override = w.system_override.rstrip() + "\n\n" + cfg.system_append.strip()
    w.complete_fn = getattr(parent, "complete_fn", None)
    return w


def _user_prompt(run: Run) -> str:
    """Render the initial child user block from launch-time controls."""
    rendered_task = (
        run.contract.prompt()
        if run.structured and run.contract is not None
        else run.task
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
    from desmos.loop import run_turns

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
                "task": run.task,
                "stage": run.stage,
                "progress": run.progress,
                "turns": run.turns,
                "usage": dict(run.usage),
            }
        )

    try:
        w = _child_world(run.cfg, parent, run.contract if run.structured else None)
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
            _emit({"ev": "child", "id": run.id, "kind": kind, **payload})

        def guidance_after_turn(n: int) -> str | None:
            interval = run.cfg.guidance_every_turns
            if interval is None or n % interval:
                return None
            run.guidance_reminders += 1
            run.stage = "guidance"
            run.progress = f"task guidance reminder {run.guidance_reminders}"
            publish_progress()
            return _guidance_prompt(run)

        _DEPTH.n = 1
        try:
            out = run_turns(
                w,
                _user_prompt(run),
                quiet=True,
                on_event=child_event,
                on_continue=guidance_after_turn,
            )
        finally:
            _DEPTH.n = 0
        if run.structured and run.contract is not None:
            require_tool = run.contract.require_tool_use
        elif run.cfg.require_tool_use is not None:
            require_tool = run.cfg.require_tool_use
        else:
            require_tool = _legacy_requires_tool(run.task)
        no_tool_failure = False
        if require_tool and not run.observed_tools:
            run.steers += 1
            run.stage = "steering"
            run.progress = "no syscall observed; requiring action"
            publish_progress()
            _DEPTH.n = 1
            try:
                out = run_turns(
                    w,
                    "You finished without using any tool. That result is unevidenced and will "
                    "be rejected. Use one of your available syscalls now, inspect the task with "
                    "a real call, read its result, and only then return the complete answer.",
                    quiet=True,
                    on_event=child_event,
                    on_continue=guidance_after_turn,
                )
            finally:
                _DEPTH.n = 0
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

def spawn(
    task: str | TaskContract,
    agent: str = "general",
    *,
    resume: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    system_prompt: str | None = None,
    system_append: str | None = None,
    user_input: str | None = None,
    task_template: str | None = None,
    guidance_every_turns: int | None = None,
    guidance_reminder: str | None = None,
    parent: Any = None,
    _register_pending: bool = True,
    **over: Any,
) -> str:
    """Start a child immediately after its typed dependencies are accepted."""
    if getattr(_DEPTH, "n", 0) >= 1:
        raise ValueError("subagent depth cap: children cannot spawn")
    parent = parent or _parent()
    explicit = {
        "model": model,
        "thinking": thinking,
        "system_prompt": system_prompt,
        "system_append": system_append,
        "user_input": user_input,
        "task_template": task_template,
        "guidance_every_turns": guidance_every_turns,
        "guidance_reminder": guidance_reminder,
    }
    over.update({key: value for key, value in explicit.items() if value is not None})
    structured = isinstance(task, TaskContract)
    contract = task if structured else TaskContract.legacy(str(task))
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
    run = Run(
        id=uuid.uuid4().hex[:8],
        task=contract.objective,
        cfg=cfg,
        contract=contract,
        structured=structured,
    )
    if resume:
        src = RUNS.get(resume)
        if src is None or src.state != "done":
            raise ValueError(f"cannot resume from {resume!r}: not a completed run")
        if src.cfg.agent != cfg.agent:
            raise ValueError(f"resume identity mismatch: {src.cfg.agent} != {cfg.agent}")
        run.messages = list(src.messages)
        run.cfg.context = "resumed"
    with _LOCK:
        RUNS[run.id] = run
    _emit(
        {
            "ev": "subagent",
            "phase": "started",
            "id": run.id,
            "agent": cfg.agent,
            "persona": cfg.persona or "",
            "task": run.task,
            "structured": structured,
            "model": cfg.model or getattr(parent, "model", ""),
        }
    )
    _POOL.submit(_execute, run, parent)
    # Nothing in the parent turn waits on this. The step ends normally and the
    # loop resumes it when the child settles, so a spawn costs no idle turn and
    # a queued follow-up is never stuck behind a sleeping call.
    from desmos import pending as _pending

    def _settle() -> str:
        while run.state in ("pending", "running"):
            time.sleep(0.05)
        brief = run.brief()
        return (
            f"{cfg.agent} {run.id}: {brief['state']}/{brief['stage']}"
            f" after {brief['turns']} turns."
            f' Read it with result("{run.id}") or judgment("{run.id}").'
        )

    if _register_pending:
        _pending.register(parent, f"subagent {cfg.agent} {run.id}", _settle)
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
    from desmos.loop import new_world

    # An unbound parent is a fallback, not a session. The persist=True default
    # loaded and saved cwd/.desmos/harness.sqlite3 -- a child process writing
    # the real harness's state.
    return new_world(Path.cwd(), state_path=None, persist=False)


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
        agent = str(item.pop("agent", "general"))
        resume = item.pop("resume", None)
        cfg = resolve(agent, **item)
        _scoped_tags(cfg.capability, task if isinstance(task, TaskContract) else None)
        prepared.append((task, agent, resume, item))
    parent = parent or _parent()
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
    from desmos import pending as _pending

    def _settle_group() -> str:
        while any(RUNS[i].state in ("pending", "running") for i in ids):
            time.sleep(0.05)
        rows = [
            f"{RUNS[i].cfg.agent} {i}: {RUNS[i].state}/{RUNS[i].stage}"
            f" after {RUNS[i].turns} turns"
            for i in ids
        ]
        reads = ", ".join(f'result("{i}")' for i in ids)
        return "subagent group settled:\n" + "\n".join(rows) + f"\nRead: {reads}"

    _pending.register(parent, "subagent group " + " ".join(ids), _settle_group)
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
