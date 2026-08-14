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
    "researcher": "Read widely before concluding. Cite file:line for every claim.",
    "critic": "Look for what is wrong, missing, or unproven. Do not praise.",
}

# capability modes: which tags the child may use
CAPS: dict[str, tuple[str, ...]] = {
    "read": ("python", "bash", "skill"),
    "edit": ("python", "bash", "edit", "skill", "reload"),
    "full": (),  # empty tuple == inherit everything
}

AGENTS: dict[str, dict[str, Any]] = {
    # A cap is a deadline, and a child that hits one answers from half a probe
    # rather than saying it ran out. These are a runaway guard, not a budget:
    # the real stop is the child deciding it is done.
    "general": {"capability": "edit", "max_turns": 500},
    "explore": {"persona": "researcher", "capability": "read", "max_turns": 500},
    "review": {"persona": "critic", "capability": "read", "max_turns": 300},
}


@dataclass
class EffectiveConfig:
    agent: str = "general"
    persona: str | None = None
    persona_instructions: str | None = None
    capability: str = "edit"
    model: str | None = None
    thinking: str | None = None
    max_turns: int = 500
    cwd: str | None = None
    context: str = "new"  # new | resumed


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
    return EffectiveConfig(
        agent=agent,
        persona=persona,
        persona_instructions=PERSONAS.get(persona or ""),
        capability=cap,
        model=d.get("model"),
        thinking=d.get("thinking"),
        max_turns=int(d.get("max_turns", 500)),
        cwd=d.get("cwd"),
        context=d.get("context", "new"),
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
    run_result: RunResult | None = None
    judgment: Judgment | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def secs(self) -> float:
        end = self.ended or time.time()
        return round(end - self.started, 1) if self.started else 0.0

    def brief(self) -> dict[str, Any]:
        budget = self.contract.budget if self.contract is not None else None
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
            "budget": {
                "turns": {"used": self.turns, "limit": budget.max_turns if budget else self.cfg.max_turns},
                "tokens": {"used": _token_total(self.usage), "limit": budget.max_tokens if budget else None},
                "seconds": {"used": self.secs, "limit": budget.wall_seconds if budget else None},
            },
            "out": (self.result or self.error)[:120],
        }


def _token_total(usage: dict[str, int]) -> int:
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    return sum(
        value
        for key, value in usage.items()
        if key in {"input_tokens", "output_tokens"} and isinstance(value, int)
    )


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


def _child_world(cfg: EffectiveConfig, parent: Any, contract: TaskContract | None = None):
    from desmos.loop import new_world, seed_builtins

    cwd = Path(cfg.cwd) if cfg.cwd else parent.cwd
    # persist=False: do not load or write the parent's harness.json
    w = new_world(cwd, state_path=None, ns={}, persist=False)
    seed_builtins(w)
    w.model = cfg.model or parent.model
    w.thinking = cfg.thinking or parent.thinking
    allowed = CAPS[cfg.capability]
    if allowed:
        for name in list(w.tools):
            if name not in allowed:
                del w.tools[name]
    if contract is not None and contract.allowed_tools:
        permitted = set(contract.allowed_tools)
        for name in list(w.tools):
            if name not in permitted:
                del w.tools[name]
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
    w.complete_fn = getattr(parent, "complete_fn", None)
    return w


def _execute(run: Run, parent: Any) -> None:
    from desmos.loop import run_turns

    run.state = "running"
    run.stage = "starting"
    run.progress = "building isolated child world"
    run.started = time.time()
    budget = run.contract.budget if run.contract is not None else None
    budget_stop = [""]
    _persist(run)

    def should_stop() -> bool:
        if budget is None:
            return False
        if time.time() - run.started >= budget.wall_seconds:
            budget_stop[0] = budget_stop[0] or "wall_time_budget"
        elif _token_total(run.usage) >= budget.max_tokens:
            budget_stop[0] = budget_stop[0] or "token_budget"
        return bool(budget_stop[0])

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
                "budget": run.brief()["budget"],
            }
        )

    try:
        w = _child_world(run.cfg, parent, run.contract if run.structured else None)
        if run.cfg.context == "resumed" and run.messages:
            w.messages = list(run.messages)

        def child_event(ev: dict[str, Any]) -> None:
            kind = ev.get("ev")
            if kind == "turn":
                run.turns = int(ev.get("n") or run.turns)
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
                run.progress = f"collected {ev.get('tag') or 'tool'} evidence"
                publish_progress()
            payload = {k: v for k, v in ev.items() if k != "ev"}
            _emit({"ev": "child", "id": run.id, "kind": kind, **payload})

        prompt = run.contract.prompt() if run.structured and run.contract is not None else run.task
        max_turns = min(
            run.cfg.max_turns,
            budget.max_turns if budget is not None else run.cfg.max_turns,
        )
        _DEPTH.n = 1
        try:
            out = run_turns(
                w,
                prompt,
                max_turns=max_turns,
                quiet=True,
                on_event=child_event,
                should_stop=should_stop,
            )
        finally:
            _DEPTH.n = 0
        from desmos.scan import scan

        forced_turn_cap = False
        if scan(out) and not budget_stop[0]:
            # Preserve the old compatibility behavior: a child that spends its
            # last turn on a tool call gets one toolless chance to summarize.
            forced_turn_cap = True
            _DEPTH.n = 1
            try:
                out = run_turns(
                    w,
                    "Out of turns. Answer now from what you already found. No syscalls.",
                    max_turns=1,
                    quiet=True,
                )
            finally:
                _DEPTH.n = 0
            if scan(out):
                run.result = "turn cap: child ended on a syscall"
                run.error = "turn cap: forced summary still had syscalls"
            else:
                run.result = out
                run.error = "turn cap: forced summary"
        else:
            run.result = out

        run.turns = len(w.log)
        run.messages = w.messages
        total: dict[str, int] = {}
        for entry in w.log:
            for key, value in (entry.get("usage") or {}).items():
                if isinstance(value, int):
                    total[key] = total.get(key, 0) + value
        run.usage = total

        if budget_stop[0]:
            run.state = "stopped"
            run.stop_reason = budget_stop[0]
        elif forced_turn_cap:
            run.state = "stopped"
            run.stop_reason = "turn_budget"
        else:
            run.state = "done"
            run.stop_reason = "completed"

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
            run.judgment = judge(run.contract, run.run_result)
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
                "budget": run.brief()["budget"],
                "result": (run.result or "")[:800],
                "error": run.error,
            }
        )

def spawn(
    task: str | TaskContract,
    agent: str = "general",
    *,
    resume: str | None = None,
    **over: Any,
) -> str:
    """Start a child immediately after its typed dependencies are accepted."""
    if getattr(_DEPTH, "n", 0) >= 1:
        raise ValueError("subagent depth cap: children cannot spawn")
    parent = over.pop("parent", None) or _parent()
    structured = isinstance(task, TaskContract)
    contract = task if structured else TaskContract.legacy(str(task))
    for dependency in contract.dependencies:
        prior = RUNS.get(dependency)
        if prior is None:
            raise ValueError(f"unknown dependency {dependency!r}")
        if prior.state in {"pending", "running"}:
            raise ValueError(f"dependency {dependency!r} has not settled")
        if prior.judgment is None or not prior.judgment.accepted:
            raise ValueError(f"dependency {dependency!r} was not accepted")

    cfg = resolve(agent, **over)
    if structured:
        cfg.max_turns = min(cfg.max_turns, contract.budget.max_turns)
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
            "budget": run.brief()["budget"],
            "model": cfg.model or getattr(parent, "model", ""),
        }
    )
    _POOL.submit(_execute, run, parent)
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

    return new_world(Path.cwd())


def wait(*ids: str, timeout: float = 600.0, poll: float = 0.5) -> list[dict[str, Any]]:
    """Block until the named runs settle (all of them if none named)."""
    targets = list(ids) or list(RUNS)
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = [i for i in targets if i in RUNS and RUNS[i].state in ("pending", "running")]
        if not pending:
            break
        time.sleep(poll)
    out = []
    for i in targets:
        if i in RUNS:
            out.append(RUNS[i].brief())
        else:
            out.append({"id": i, "agent": "", "state": "unknown", "secs": 0.0, "turns": 0, "out": ""})
    return out


def status() -> list[dict[str, Any]]:
    return [r.brief() for r in RUNS.values()]


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


def fanout(tasks: list[str | TaskContract], agent: str = "explore", **over: Any) -> list[str]:
    """Spawn one child per task. Returns ids in order."""
    return [spawn(t, agent, **over) for t in tasks]


def gather(ids: list[str], timeout: float = 600.0) -> str:
    """Wait, then concatenate each child's final answer."""
    wait(*ids, timeout=timeout)
    return "\n\n".join(f"--- {i} [{RUNS[i].cfg.agent}] ---\n{result(i)}" for i in ids)
