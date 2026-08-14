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
    "general": {"capability": "edit", "max_turns": 12},
    "explore": {"persona": "researcher", "capability": "read", "max_turns": 10},
    "review": {"persona": "critic", "capability": "read", "max_turns": 8},
}


@dataclass
class EffectiveConfig:
    agent: str = "general"
    persona: str | None = None
    persona_instructions: str | None = None
    capability: str = "edit"
    model: str | None = None
    thinking: str | None = None
    max_turns: int = 12
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
        max_turns=int(d.get("max_turns", 12)),
        cwd=d.get("cwd"),
        context=d.get("context", "new"),
    )


# --- runtime ----------------------------------------------------------------

@dataclass
class Run:
    id: str
    task: str
    cfg: EffectiveConfig
    state: str = "pending"  # pending | running | done | failed
    result: str = ""
    error: str = ""
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    started: float = 0.0
    ended: float = 0.0
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def secs(self) -> float:
        end = self.ended or time.time()
        return round(end - self.started, 1) if self.started else 0.0

    def brief(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.cfg.agent,
            "state": self.state,
            "secs": self.secs,
            "turns": self.turns,
            "out": (self.result or self.error)[:120],
        }


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


def _child_world(cfg: EffectiveConfig, parent: Any):
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
    w.tools.pop("agents", None)
    if cfg.persona_instructions:
        w.notes["persona"] = cfg.persona_instructions
    w.notes["subagent"] = (
        "You are a subagent. Finish the task and report. Your final message is "
        "the only thing the parent sees, so put the answer there, not in a syscall."
    )
    w.complete_fn = getattr(parent, "complete_fn", None)
    return w


def _execute(run: Run, parent: Any) -> None:
    from desmos.loop import run_turns

    run.state = "running"
    run.started = time.time()
    _persist(run)
    try:
        w = _child_world(run.cfg, parent)
        if run.cfg.context == "resumed" and run.messages:
            w.messages = list(run.messages)
        def child_event(ev: dict[str, Any]) -> None:
            kind = ev.get("ev")
            if kind == "turn":
                _emit({"ev": "subagent", "phase": "progress", "id": run.id, "turns": ev.get("n")})
            payload = {k: v for k, v in ev.items() if k != "ev"}
            _emit({"ev": "child", "id": run.id, "kind": kind, **payload})

        _DEPTH.n = 1
        try:
            out = run_turns(
                w,
                run.task,
                max_turns=run.cfg.max_turns,
                quiet=True,
                on_event=child_event,
            )
        finally:
            _DEPTH.n = 0
        from desmos.scan import scan

        if scan(out):
            # Turn cap hit mid-syscall. Force one toolless turn so the parent
            # gets an answer instead of a dangling tool call.
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
        tot: dict[str, int] = {}
        for entry in w.log:
            for k, v in (entry.get("usage") or {}).items():
                if isinstance(v, int):
                    tot[k] = tot.get(k, 0) + v
        run.usage = tot
        run.state = "done"
    except Exception as e:  # noqa: BLE001 - a dead child must not kill the parent
        run.error = f"{type(e).__name__}: {e}"
        run.state = "failed"
    finally:
        run.ended = time.time()
        _persist(run)
        _emit(
            {
                "ev": "subagent",
                "phase": run.state,
                "id": run.id,
                "secs": run.secs,
                "turns": run.turns,
                "result": (run.result or "")[:800],
                "error": run.error,
            }
        )


def spawn(task: str, agent: str = "general", *, resume: str | None = None, **over: Any) -> str:
    """Start a child agent. Returns its id immediately; nothing blocks."""
    if getattr(_DEPTH, "n", 0) >= 1:
        raise ValueError("subagent depth cap: children cannot spawn")
    parent = over.pop("parent", None) or _parent()
    cfg = resolve(agent, **over)
    run = Run(id=uuid.uuid4().hex[:8], task=task, cfg=cfg)
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
            "task": task,
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


def fanout(tasks: list[str], agent: str = "explore", **over: Any) -> list[str]:
    """Spawn one child per task. Returns ids in order."""
    return [spawn(t, agent, **over) for t in tasks]


def gather(ids: list[str], timeout: float = 600.0) -> str:
    """Wait, then concatenate each child's final answer."""
    wait(*ids, timeout=timeout)
    return "\n\n".join(f"--- {i} [{RUNS[i].cfg.agent}] ---\n{result(i)}" for i in ids)
