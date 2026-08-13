from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from desmos.catalog import header, ns_names, system_prompt
from desmos.complete import complete, text_of
from desmos.const import FROZEN, PRIOR_KEEP
from desmos.dispatch import dispatch
from desmos.generations import ensure_gen1, evolve, rollback
from desmos.persist import load, save
from desmos.scan import clip, scan
from desmos.types import Block, Tool, World


def format_results(results: list[tuple[Block, str]]) -> str:
    chunks = []
    for b, r in results:
        attr = " ".join(f'{k}="{v}"' for k, v in b.attrs.items())
        label = f"<{b.tag} {attr}>".strip() if attr else f"<{b.tag}>"
        chunks.append(f"{label} ->\n{r}")
    return "\n\n".join(chunks)


def format_result_message(results: list[tuple[Block, str]]) -> str:
    parts = []
    for b, r in results:
        parts.append(f'<result tag="{b.tag}">{clip(r, 6000)}</result>')
    return "\n\n".join(parts)


def seed_builtins(world: World) -> None:
    world.tools["python"] = Tool("python", "exec Python in the persistent kernel", frozen=True)
    world.tools["bash"] = Tool("bash", "run a shell command in cwd", frozen=True)
    world.tools["edit"] = Tool("edit", 'replace one occurrence: path= and body old\\n---\\nnew', frozen=True)
    world.tools["register"] = Tool("register", "install a tag: name= and doc=, body is def handle", frozen=True)
    world.tools["system"] = Tool("system", "write or delete a system note (name=, optional delete=1)", frozen=True)
    world.tools["tool"] = Tool("tool", "rewrite a tool description: name= and doc=", frozen=True)
    world.tools["skill"] = Tool("skill", "load full SKILL.md: name=", frozen=True)
    world.tools["evolve"] = Tool("evolve", "snapshot grown state as the next generation", frozen=True)
    world.tools["rollback"] = Tool("rollback", "restore generation n=", frozen=True)


def install_resources(world: World) -> None:
    from desmos.extensions import load_extensions
    from desmos.skills import bind_python_skill, discover_skills

    world.skills = discover_skills(world.cwd)
    for skill in world.skills:
        fn = bind_python_skill(world.ns, skill)
        if callable(fn) and skill.import_name and skill.import_name not in FROZEN:
            world.tools[skill.import_name] = Tool(
                name=skill.import_name,
                doc=skill.description or f"skill {skill.name}",
                handler=fn,
            )
    api = load_extensions(world.cwd)
    world.hooks = api.hooks
    for name, doc, handler in api.tools:
        if name not in FROZEN:
            world.tools[name] = Tool(name=name, doc=doc, handler=handler)


def reload(world: World) -> str:
    install_resources(world)
    return f"reloaded {len(world.skills)} skills, {len(world.tools)} tools"


def turn(world: World, messages: list[dict[str, str]], max_tokens: int) -> tuple[str, list[tuple[Block, str]], bool]:
    install_resources(world)
    fn = world.complete_fn or complete
    resp = fn(world.model, system_prompt(world), messages, max_tokens)
    speech = text_of(resp)
    world.log.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "usage": resp.get("usage") or {},
            "stop": resp.get("stop_reason"),
            "text": speech,
        }
    )
    blocks = scan(speech)
    results = [(b, dispatch(world, b)) for b in blocks]
    return speech, results, not blocks


def run_turns(
    world: World,
    prompt: str,
    *,
    max_turns: int = 32,
    max_tokens: int = 8192,
    quiet: bool = False,
) -> str:
    world.messages.append({"role": "user", "content": header(world, prompt) + "\n\n" + prompt})
    last = ""
    for n in range(1, max_turns + 1):
        if not quiet:
            print(f"\n===== turn {n} =====")
        speech, results, done = turn(world, world.messages, max_tokens)
        last = speech
        if not quiet:
            print(speech)
        last_results = format_results(results) if results else ""
        if last_results and not quiet:
            print("\n--- results ---")
            print(last_results)
        world.messages.append({"role": "assistant", "content": speech})
        if done:
            world.prior.append({"prompt": prompt, "speech": speech})
            world.prior = world.prior[-PRIOR_KEEP:]
            save(world)
            return speech
        world.messages.append({"role": "user", "content": format_result_message(results)})
    if not quiet:
        print(f"\n[hit max_turns={max_turns}]")
    world.prior.append({"prompt": prompt, "speech": last})
    world.prior = world.prior[-PRIOR_KEEP:]
    save(world)
    return last


def new_world(cwd: Path, state_path: Path | None = None, *, ns: dict[str, Any] | None = None) -> World:
    world = World(cwd=cwd, state_path=state_path)
    if ns is not None:
        world.ns = ns
    world.ns.setdefault("CWD", str(cwd))
    seed_builtins(world)
    install_resources(world)
    load(world)
    ensure_gen1(world)
    return world


def bind_step(world: World) -> Callable[..., str]:
    def step(prompt: str, *, max_turns: int = 32, max_tokens: int = 8192) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise TypeError("step(prompt) needs a non-empty string")
        from desmos.loop import run_turns as _run

        return _run(world, prompt, max_turns=max_turns, max_tokens=max_tokens)

    world.ns["step"] = step
    world.ns["world"] = world
    world.ns["reload"] = lambda: reload(world)
    world.ns["reload_sdk"] = lambda: reload_sdk(world)
    world.ns["evolve"] = lambda reason="": evolve(world, str(reason))
    world.ns["rollback"] = lambda n=1: rollback(world, int(n))
    return step


def reload_sdk(world: World | None = None) -> str:
    """Reimport desmos.* then rebind. Safe to call from the kernel after editing the SDK."""
    import importlib
    import sys

    order = [
        "desmos.const",
        "desmos.types",
        "desmos.scan",
        "desmos.edit",
        "desmos.exec",
        "desmos.persist",
        "desmos.generations",
        "desmos.complete",
        "desmos.catalog",
        "desmos.dispatch",
        "desmos.skills",
        "desmos.extensions",
        "desmos.loop",
    ]
    reloaded = []
    for name in order:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        importlib.reload(mod)
        reloaded.append(name)
    if world is not None:
        # re-bind against the new loop/catalog functions
        from desmos.loop import bind_step as _bind
        from desmos.loop import reload as _reload

        _reload(world)
        _bind(world)
    return "sdk reloaded: " + ", ".join(reloaded)


def attach(shell: Any = None, *, cwd: str | Path | None = None, model: str | None = None) -> World:
    if shell is None:
        try:
            from IPython import get_ipython
        except ImportError as exc:
            raise RuntimeError("IPython is not installed") from exc
        shell = get_ipython()
    if shell is None:
        raise RuntimeError("no IPython shell — use python -m desmos console")
    path = Path(cwd or Path.cwd()).resolve()
    world = new_world(path, ns=shell.user_ns)
    world.shell = shell
    if model:
        world.model = model
    bind_step(world)
    return world


def run(args: Any) -> int:
    import json
    import os

    cwd = Path(args.cwd).resolve()
    os.chdir(cwd)
    world = new_world(cwd)
    run_dir = Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)
    world.model = args.model
    print(f"model={world.model} max_turns={args.max_turns} cwd={cwd}")
    print(system_prompt(world))
    print("--------------")
    run_turns(world, args.task, max_turns=args.max_turns, max_tokens=args.max_tokens)
    summary = {
        "task": args.task,
        "ns": ns_names(world),
        "tools": {n: t.doc for n, t in world.tools.items()},
        "notes": world.notes,
        "turns": len(world.log),
        "usage": [e.get("usage") for e in world.log],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("\n===== summary =====")
    print(json.dumps(summary, indent=2))
    return 0
