from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from desmos.catalog import header, ns_names, system_prompt
from desmos.complete import (
    LAST,
    assistant_content,
    cached_payload,
    compaction_block,
    complete,
    redact_wire,
    text_of,
    thought_blocks,
    thinking_text,
)
from desmos.const import FROZEN, MAX_TOKENS, PRIOR_KEEP
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


#: How much of a syscall's output reaches the model. dispatch clips a handler
#: at scan.RESULT_CAP first; this is the tighter cap that actually bounds the
#: transcript, and it is the number the prompt quotes -- check.py ties the two
#: together so the prompt cannot drift off it.
RESULT_CLIP = 6000


def format_result_message(results: list[tuple[Block, str]]) -> str:
    parts = []
    for b, r in results:
        parts.append(f'<result tag="{b.tag}">{clip(r, RESULT_CLIP)}</result>')
    return "\n\n".join(parts)


_BUILTIN_DOCS = (
    ("python", "exec Python in the persistent kernel"),
    ("bash", "run a shell command in cwd — one subprocess, no state kept"),
    ("shell", "persistent pty shell: id= names the session, cd/env/venv survive, answers prompts; timeout=, interrupt=1, close=1"),
    ("edit", "replace one occurrence: path= and body old\\n---\\nnew"),
    ("register", "install a tag: name= and doc=, body is def handle"),
    ("system", "write or delete a system note (name=, optional delete=1)"),
    ("tool", "rewrite a tool description: name= and doc="),
    ("skill", "load full SKILL.md: name="),
    ("reload", "rediscover skills and extensions now"),
    ("reload_sdk", "reimport desmos.* and rebind step; next complete() uses the new ABI"),
    ("evolve", "snapshot grown state as the next generation"),
    ("rollback", "restore generation n="),
    (
        "memory",
        "structured durable memory: body remembers; actions show/search/read/forget/verify/consolidate",
    ),
)


def seed_builtins(world: World) -> None:
    for name, doc in _BUILTIN_DOCS:
        existing = world.tools.get(name)
        if existing is None:
            world.tools[name] = Tool(name, doc, frozen=True)
        else:
            existing.frozen = True


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


def turn(
    world: World,
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    n: int = 1,
    emit: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[str, list[tuple[Block, str]], bool, list[dict[str, Any]]]:
    def fire(ev: dict[str, Any]) -> None:
        if emit is not None:
            emit(ev)

    def stopped() -> bool:
        return should_stop is not None and should_stop()

    install_resources(world)
    if world.ns.get("world") is not world:
        bind_step(world)  # ns lost its handles (cleanup, reload, stale exec globals)
    system = getattr(world, "system_override", "") or system_prompt(world)
    built = cached_payload(
        world.model, system, messages, max_tokens, thinking=world.thinking
    )
    req = {k: v for k, v in built.items() if k != "_betas"}
    fire(
        {
            "ev": "post",
            "n": n,
            "origin": "user" if n == 1 else "llm",
            "model": world.model,
            "request": redact_wire(req),
        }
    )
    streamed = False

    def on_delta(delta: dict[str, Any]) -> None:
        nonlocal streamed
        streamed = True
        kind = delta.get("kind")
        if kind == "thinking_delta":
            fire(
                {
                    "ev": "thinking",
                    "redacted": False,
                    "text": delta.get("text") or "",
                    "delta": True,
                }
            )
        elif kind == "thinking":
            fire(
                {
                    "ev": "thinking",
                    "redacted": bool(delta.get("redacted")),
                    "text": delta.get("text") or "",
                    "delta": False,
                }
            )
        elif kind == "text_delta":
            fire({"ev": "speech", "text": delta.get("text") or "", "delta": True})

    if world.complete_fn:
        resp = world.complete_fn(world.model, system, messages, max_tokens)
    else:
        resp = complete(
            world.model,
            system,
            messages,
            max_tokens,
            thinking=world.thinking,
            on_event=on_delta,
            should_stop=should_stop,
        )
        req = dict(LAST.get("payload") or {})
    speech = text_of(resp)
    assistant = assistant_content(resp)
    world.log.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "usage": resp.get("usage") or {},
            "stop": resp.get("stop_reason"),
            "text": speech,
            "thinking": thinking_text(assistant),
            "request": redact_wire(req),
            "response": redact_wire(resp),
        }
    )
    # Durable before anything runs. This used to be appended by the caller
    # after the dispatch loop, so a crash or a kill during a <bash> lost the
    # assistant turn that ordered it: the side effect had happened and the
    # transcript never asked for it.
    messages.append({"role": "assistant", "content": assistant})
    parts = thought_blocks(assistant)
    if not streamed:
        for part in parts:
            fire(
                {
                    "ev": "thinking",
                    "redacted": part["redacted"],
                    "text": part["text"],
                }
            )
        fire({"ev": "speech", "text": speech})
    n_thoughts = sum(1 for p in parts if not p["redacted"])
    n_redacted = sum(1 for p in parts if p["redacted"])
    usage = (world.log[-1].get("usage") if world.log else {}) or {}
    # A fold rewrites what the model remembers. That is the largest thing the
    # harness does to itself in a run, and without this event the only trace is
    # the context bar dropping for no stated reason.
    fold = compaction_block(assistant)
    if fold is not None:
        # The block's summary field is the server's, not ours. Read whichever
        # string it carries rather than asserting a shape; the trajectory log
        # has the exact wire block if this ever needs pinning down.
        summary = next(
            (v for k, v in fold.items() if k != "type" and isinstance(v, str) and v.strip()),
            "",
        )
        fire({"ev": "compacted", "n": n, "kept": len(messages), "text": summary})
    fire(
        {
            "ev": "complete",
            "n": n,
            "origin": "user" if n == 1 else "llm",
            "model": world.model,
            "thinking": world.thinking,
            "thoughts": n_thoughts,
            "redacted": n_redacted,
            "usage": usage,
            "request": (world.log[-1] or {}).get("request") or {},
            "response": (world.log[-1] or {}).get("response") or {},
        }
    )
    results: list[tuple[Block, str]] = []
    blocks = scan(speech)
    if not stopped():
        for b in blocks:
            if stopped():
                break
            fire(
                {
                    "ev": "result",
                    "phase": "start",
                    "tag": b.tag,
                    "attrs": dict(b.attrs),
                    "body": clip(b.body),
                    "text": "",
                }
            )

            def on_chunk(text: str, tag: str = b.tag) -> None:
                if text:
                    fire(
                        {
                            "ev": "result",
                            "phase": "delta",
                            "tag": tag,
                            "delta": True,
                            "text": text,
                        }
                    )

            r = dispatch(
                world,
                b,
                on_chunk=on_chunk,
                should_stop=should_stop,
            )
            results.append((b, r))
            fire(
                {
                    "ev": "result",
                    "phase": "done",
                    "tag": b.tag,
                    "attrs": dict(b.attrs),
                    "body": clip(b.body),
                    "text": clip(r),
                }
            )
    # No syscalls usually means the model finished. It also looks exactly like
    # a reply the endpoint cut off mid-tag: scan() drops an unterminated block,
    # so `<bash>ls` with no closing tag parses to nothing. stop_reason is the
    # only thing that tells the two apart, and it was written to world.log and
    # read by nobody. A cut-off turn is not a finished one.
    cut = resp.get("stop_reason") in {"max_tokens", "refusal"}
    if cut and not blocks:
        note = f"[reply was cut short: {resp.get('stop_reason')} — continue from where it stopped]"
        messages.append({"role": "user", "content": note})
        fire({"ev": "error", "n": n, "text": note})
        return speech, results, False, assistant
    return speech, results, not blocks, assistant


def _commit_step(world: World, prompt: str, last: str) -> None:
    world.prior.append({"prompt": prompt, "speech": last})
    world.prior = world.prior[-PRIOR_KEEP:]
    save(world)


def _spent_tokens(world: World, since: int) -> int:
    """Prompt + completion tokens billed by this step so far."""
    total = 0
    for entry in world.log[since:]:
        usage = entry.get("usage") or {}
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                total += value
    return total


def run_turns(
    world: World,
    prompt: str,
    *,
    max_turns: int = 32,
    max_tokens: int = MAX_TOKENS,
    quiet: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    max_total_tokens: int | None = None,
) -> str:
    """Run a step to its end, and always say how it ended.

    Exactly one terminating event leaves here on every path, exception
    included: ``stopped`` if the cancel flag is up, ``done`` otherwise. The TUI
    clears ``running`` on that event and drains its queue from it, so a step
    that returns in silence leaves the pane stuck on "stopping" with the queued
    message never firing.

    There was one such path. A stop that landed during a turn the model
    finished on its own satisfied neither ``stopped() and not done`` in the
    loop nor ``not cancel.is_set()`` in the bridge, so nothing was emitted at
    all. Two emitters with complementary conditions is how a gap like that
    hides; now there is one.
    """
    if world.running:
        raise RuntimeError(
            "a step is already running on this world; call step() from a new "
            "turn, or spawn() a subagent, which gets its own world"
        )
    world.running = True
    hit: list[str] = []
    try:
        return _run_turns(
            world,
            prompt,
            max_turns=max_turns,
            max_tokens=max_tokens,
            quiet=quiet,
            on_event=on_event,
            should_stop=should_stop,
            max_total_tokens=max_total_tokens,
            budget_hit=hit,
        )
    finally:
        world.running = False
        if on_event is not None:
            if hit:
                on_event({"ev": "stopped", "text": f"{hit[0]}, saved"})
            elif should_stop is not None and should_stop():
                on_event({"ev": "stopped", "text": "stopped, saved"})
            else:
                on_event({"ev": "done"})


def _run_turns(
    world: World,
    prompt: str,
    *,
    max_turns: int = 32,
    max_tokens: int = MAX_TOKENS,
    quiet: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    max_total_tokens: int | None = None,
    budget_hit: list[str] | None = None,
) -> str:
    def emit(ev: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(ev)

    # A child gets a token ceiling from its contract's budget. The root loop
    # only ever had max_turns, so a run that burned its whole context in four
    # enormous turns was unbounded in the one unit that costs money. Count from
    # where this step began, so a nested step() is charged for itself rather
    # than for the session in front of it.
    spent_from = len(world.log)
    hit = budget_hit if budget_hit is not None else []

    def stopped() -> bool:
        if max_total_tokens is not None and not hit:
            if _spent_tokens(world, spent_from) >= max_total_tokens:
                hit.append(f"token budget of {max_total_tokens} reached")
        return bool(hit) or (should_stop is not None and should_stop())

    def stop_note(n: int) -> str:
        if hit:
            return f"[stopped: {hit[0]} after turn {n}]"
        return f"[stopped by the user after turn {n}]"

    # Tag handlers reach the wire through here.
    world.emit = emit

    world.messages.append({"role": "user", "content": header(world, prompt) + "\n\n" + prompt})
    last = ""
    for n in range(1, max_turns + 1):
        if stopped():
            if n > 1:
                world.messages.append({"role": "user", "content": stop_note(n - 1)})
            _commit_step(world, prompt, last)
            return last
        emit({"ev": "turn", "n": n})
        if not quiet:
            print(f"\n===== turn {n} =====")
        try:
            speech, results, done, assistant = turn(
                world,
                world.messages,
                max_tokens,
                n=n,
                emit=emit,
                should_stop=stopped,
            )
        except Exception as exc:  # noqa: BLE001
            # A failure is a value, not an unwind. Letting it propagate left a
            # user message with no assistant reply -- so the next step appended
            # a second consecutive user turn -- while run_turns' finally still
            # emitted "done", telling the TUI the step succeeded next to an
            # unrelated error line. Write what happened where the model will
            # read it, say so once, and stop this step.
            note = f"[turn {n} failed: {type(exc).__name__}: {exc}]"
            world.messages.append({"role": "assistant", "content": [{"type": "text", "text": note}]})
            emit({"ev": "error", "n": n, "text": note})
            if not quiet:
                print(note)
            _commit_step(world, prompt, last)
            return last
        last = speech
        thoughts = thinking_text(assistant)
        if thoughts and not quiet:
            print("--- thinking ---")
            print(thoughts)
            print("--------------")
        if not quiet:
            print(speech)
        last_results = format_results(results) if results else ""
        if last_results and not quiet:
            print("\n--- results ---")
            print(last_results)
        # Whatever ran, its output goes back. The stop path used to return
        # before this, so a Ctrl+C landing after the first of three syscalls
        # threw away the results of the ones that had already finished -- the
        # model's next context showed its own tags with no outcome and no
        # marker that they had been executed.
        if results:
            world.messages.append({"role": "user", "content": format_result_message(results)})
        if done or stopped():
            if stopped():
                # A stop left no trace in the transcript, so the next step read
                # the model's own tags, whichever results happened to run, and
                # nothing saying it had been interrupted -- which reads as work
                # that finished.
                world.messages.append({"role": "user", "content": stop_note(n)})
            _commit_step(world, prompt, speech)
            return speech
    # Same for the cap: it was printed, and the bridge runs quiet=True, so the
    # only signal was a `done` event identical to a clean finish.
    note = f"[hit max_turns={max_turns} — the task was not finished]"
    world.messages.append({"role": "user", "content": note})
    emit({"ev": "error", "text": note})
    if not quiet:
        print(f"\n{note}")
    _commit_step(world, prompt, last)
    return last


def new_world(
    cwd: Path,
    state_path: Path | None = None,
    *,
    ns: dict[str, Any] | None = None,
    persist: bool = True,
) -> World:
    world = World(cwd=cwd, state_path=state_path, persist=persist)
    if ns is not None:
        world.ns = ns
    world.ns.setdefault("CWD", str(cwd))
    seed_builtins(world)
    install_resources(world)
    if persist:
        load(world)
        ensure_gen1(world)
    return world


def bind_step(world: World) -> Callable[..., str]:
    def step(
        prompt: str,
        *,
        max_turns: int = 32,
        max_tokens: int = MAX_TOKENS,
        max_total_tokens: int | None = None,
    ) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise TypeError("step(prompt) needs a non-empty string")
        from desmos.loop import run_turns as _run

        return _run(
            world,
            prompt,
            max_turns=max_turns,
            max_tokens=max_tokens,
            max_total_tokens=max_total_tokens,
        )

    world.ns["step"] = step
    world.ns["world"] = world
    world.ns["reload"] = lambda: reload(world)
    world.ns["reload_sdk"] = lambda: reload_sdk(world)
    world.ns["reset"] = lambda: reset_transcript(world)
    world.ns["evolve"] = lambda reason="": evolve(world, str(reason))
    world.ns["rollback"] = lambda n=1: rollback(world, int(n))
    world.ns["switch"] = lambda model, effort=None: _switch(world, model, effort)
    return step


def _switch(world: World, model: str, effort: str | None = None) -> str:
    """`switch(...)` in the kernel. Same call the TUI picker makes."""
    from desmos.settings import switch as _do

    return _do(world, str(model), str(effort) if effort else None)


def reset_transcript(world: World) -> str:
    """Drop the append-only chat so a poisoned turn cannot train the next one."""
    if world.running:
        raise RuntimeError("cannot reset the transcript from inside a running step")
    n = len(world.messages)
    world.messages.clear()
    world.prior.clear()
    save(world)
    return f"transcript cleared ({n} messages)"


def reload_sdk(world: World | None = None) -> str:
    """Reimport desmos.* then rebind. Safe from the kernel or <reload_sdk/> after editing the SDK."""
    import importlib
    import sys

    importlib.invalidate_caches()
    for name in list(sys.modules):
        if name == "edit" or name.startswith("desmos_skill_"):
            del sys.modules[name]
    order = [
        "desmos.const",
        "desmos.types",
        "desmos.scan",
        "desmos.edit",
        "desmos.exec",
        "desmos.persist",
        "desmos.memory",
        "desmos.subagent_contracts",
        "desmos.subagent_prompt",
        "desmos.subagent",
        "desmos.generations",
        "desmos.auth",
        "desmos.openai",
        "desmos.settings",
        "desmos.complete",
        "desmos.catalog",
        "desmos.dispatch",
        "desmos.skills",
        "desmos.extensions",
        "desmos.loop",
        "desmos.ext",
        "desmos.cli",
        "desmos",
        "inverted",
    ]
    reloaded = []
    for name in order:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        importlib.reload(mod)
        reloaded.append(name)
    if world is not None:
        from desmos.loop import bind_step as _bind
        from desmos.loop import reload as _reload
        from desmos.loop import seed_builtins as _seed

        _seed(world)
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
    print(f"model={world.model} thinking={world.thinking} max_turns={args.max_turns} cwd={cwd}")
    print(system_prompt(world))
    print("--------------")
    run_turns(
        world,
        args.task,
        max_turns=args.max_turns,
        max_tokens=args.max_tokens,
        max_total_tokens=getattr(args, "max_total_tokens", None),
    )
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
