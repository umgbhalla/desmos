"""Canonical capability-family routing."""

from __future__ import annotations

import json

from desmos.kernel.types import Block, World

ROUTES = {
    "exec": {"python": "python", "bash": "bash", "shell": "shell"},
    "workspace": {"find": "find", "edit": "edit"},
    "knowledge": {"memory": "memory", "recall": "recall", "system": "system"},
    "harness": {
        "register": "register", "describe": "tool", "skill": "skill",
        "reload": "reload", "reload-sdk": "reload_sdk", "evolve": "evolve", "rollback": "rollback",
        "refine": "refine",
    },
    "observe": {"retrace": "trajectory_retrace"},
}

DIRECT_TARGETS = {
    ("workspace", "read"): "read",
    ("workspace", "see"): "see",
    ("workspace", "commit"): "commit",
    ("knowledge", "todo"): "todo",
    ("observe", "usage"): "usage",
    ("observe", "trajectory"): "traj",
    ("session", "compact"): "compact",
}


DIRECT_OPS = {
    "workspace": ("read", "see", "commit"),
    "knowledge": ("todo", "plan", "decide", "anchor"),
    "observe": ("usage", "slice", "trajectory", "error", "symbol", "threads"),
    "agents": (
        "spawn", "fanout", "resume", "lineage",
        "status", "result", "structured-result", "judgment", "wait",
    ),
    "session": ("compact", "status", "switch", "peers", "agents", "inbox", "read", "post", "dismiss"),
}


FAMILIES = tuple(dict.fromkeys(tuple(ROUTES) + tuple(DIRECT_OPS)))

#: dispatch-scope target -> owning canonical family, for grant translation
#: (canonical cut step 3). Ops without an explicit target answer as the family.
TARGET_FAMILY = {target: fam for fam, ops in ROUTES.items() for target in ops.values()}
TARGET_FAMILY.update({target: fam for (fam, _o), target in DIRECT_TARGETS.items()})


def family_targets(fam):
    """Every dispatch-scope target the family's ops route to, plus the family."""
    targets = {fam}
    targets.update(ROUTES.get(fam, {}).values())
    for op in DIRECT_OPS.get(fam, ()):
        targets.add(DIRECT_TARGETS.get((fam, op), fam))
    return targets


def operations(family):
    return tuple(ROUTES.get(family, ())) + DIRECT_OPS.get(family, ())


def _op(block):
    attrs = dict(block.attrs)
    op = (attrs.pop("op", "") or attrs.pop("action", "")).strip().lower()
    return op, attrs


def _bad_op(block, op):
    return f"{block.tag}: unknown op {op!r}; expected {'|'.join(operations(block.tag))}"


def normalize(world: World, block: Block):
    """Normalize a canonical block to one legacy block or a direct result."""
    op, attrs = _op(block)
    body = block.body
    if not op and block.tag == "agents":
        import shlex
        head, sep, tail = body.partition(":")
        parts = shlex.split(head)
        if parts:
            op = parts.pop(0).lower()
            if parts and "=" not in parts[0]:
                attrs["agent"] = parts.pop(0)
            for part in parts:
                key, equals, value = part.partition("=")
                if equals:
                    attrs[key] = value
            body = tail.strip() if sep else ""
    if not op:
        return _bad_op(block, op)
    target = ROUTES.get(block.tag, {}).get(op)
    if target is not None:
        if target in GROWN_TARGETS:
            if target not in world.tools:
                return f"{block.tag} op {op!r} is unavailable in this world"
            return Block(target, body, attrs)
        # The canonical family owns this implementation (run_op below);
        # no other registration in world.tools is consulted or required.
        return Block(block.tag, body, {**attrs, "op": op})
    if op not in DIRECT_OPS.get(block.tag, ()):
        return _bad_op(block, op)
    return Block(block.tag, body, {**attrs, "op": op})


def policy_target(block: Block) -> str:
    op, _ = _op(block)
    routed = ROUTES.get(block.tag, {}).get(op)
    if routed is not None:
        return routed
    return DIRECT_TARGETS.get((block.tag, op), block.tag)


#: ROUTES targets that live outside run_op: grown tools an op forwards to.
#: Every other ROUTES target is a scope-policy name whose implementation the
#: canonical family owns outright (canonical cut step 5: the legacy
#: compatibility forwarders are gone).
GROWN_TARGETS = frozenset({"trajectory_retrace"})


def direct(world: World, block: Block, *, on_chunk=None, should_stop=None, meta=None) -> str:
    op, attrs = _op(block)
    return run_op(
        world, block.tag, op, block.body, attrs,
        on_chunk=on_chunk, should_stop=should_stop, meta=meta,
    )


def run_op(world, family, op, body, attrs, *, on_chunk=None, should_stop=None, meta=None):
    """Run one canonical family operation: the implementations live here."""
    if family == "exec":
        return _exec(world, op, body, attrs, on_chunk, should_stop)
    if family == "workspace":
        return _workspace(world, op, body, attrs, meta=meta)
    if family == "knowledge":
        return _knowledge(world, op, body, attrs)
    if family == "harness":
        return _harness(world, op, body, attrs)
    if family == "observe":
        return _observe(world, op, body, attrs)
    if family == "agents":
        return _agents(world, op, body, attrs)
    if family == "session":
        return _session(world, op, body, attrs)
    return f"{family}: unknown op {op!r}; expected {'|'.join(operations(family))}"


def _exec(world, op, body, attrs, on_chunk, should_stop):
    if op == "python":
        from desmos.kernel.exec import run_python
        return run_python(body, world, on_chunk=on_chunk)
    if op == "bash":
        from desmos.kernel.exec import run_bash
        return run_bash(body, world.cwd, on_chunk=on_chunk, should_stop=should_stop)
    from desmos.kernel.shell import run as run_shell
    return run_shell(world, body, attrs, on_chunk=on_chunk)


def _harness(world, op, body, attrs):
    if op == "register":
        from desmos.kernel.exec import register_tag
        return register_tag(world, body, attrs.get("name", ""), attrs.get("doc", ""))
    if op == "describe":
        from desmos.kernel.dispatch import set_tool_doc
        return set_tool_doc(world, attrs.get("name", ""), attrs.get("doc", "") or body)
    if op == "skill":
        from desmos.skills import load_skill_body
        name = (attrs.get("name") or body).strip()
        skill = next((s for s in world.skills if s.name == name), None)
        if skill is None:
            known = ", ".join(sorted(s.name for s in world.skills)) or "none"
            return f"unknown skill {name!r}. Available: {known}. Write .desmos/skills/<name>/SKILL.md then <reload/> to add one."
        return load_skill_body(skill, world.model)
    if op == "reload":
        from desmos.kernel.loop import reload
        return reload(world)
    if op == "reload-sdk":
        from desmos.kernel.loop import reload_sdk
        return reload_sdk(world)
    if op == "evolve":
        from desmos.state.generations import evolve
        return evolve(world, (body or attrs.get("reason") or "").strip() or "unspecified")
    if op == "rollback":
        from desmos.state.generations import rollback
        raw = attrs.get("n") or body.strip() or "1"
        try:
            n = int(raw)
        except ValueError:
            return f"rollback failed: bad n {raw!r}"
        return rollback(world, n)
    from desmos.state.refine import handle_refine
    return handle_refine(world, body, attrs)


def _workspace(world, op, body, attrs, meta=None):
    if op == "find":
        from desmos.state.find import find
        query = (body or "").strip() or str(attrs.pop("query", ""))
        attrs.pop("query", None)
        return find(world, query, **attrs)
    if op == "edit":
        from desmos.kernel.edit import apply_edit_line, parse_edit_body
        old, new = parse_edit_body(body, attrs)
        # meta is the caller's out-channel for facts only the syscall can know
        # at run time; the loop lifts them onto the result done event. A failed
        # edit has no edit site, so it sets nothing.
        msg, line = apply_edit_line(attrs.get("path", ""), old, new, cwd=world.cwd)
        if meta is not None and line is not None:
            meta["line"] = line
        # Feed the <find> frecency index at the one edit choke point every
        # world (root and child) routes through; touch() is silent on a
        # missing/broken engine.
        if line is not None:
            from desmos.state.find import touch
            touch(world, attrs.get("path", ""))
        return msg
    if op == "read":
        from pathlib import Path
        path = Path(attrs.get("path") or body.strip()).expanduser()
        if not path.is_absolute():
            path = world.cwd / path
        if not path.is_file():
            return f"not a file: {path}"
        rows = path.read_text(encoding="utf-8", errors="replace").split("\n")
        lo, hi = 1, len(rows)
        if attrs.get("lines"):
            a, _, b = attrs["lines"].partition("-")
            lo, hi = int(a or 1), int(b or a or 1)
        elif attrs.get("head"):
            hi = int(attrs["head"])
        lo, hi = max(1, lo), min(len(rows), hi)
        shown = "\n".join(f"{i:>5}| {rows[i - 1]}" for i in range(lo, hi + 1))
        return f"{path} [{lo}-{hi} of {len(rows)}]\n{shown}"
    if op == "see":
        from desmos.kernel import vision
        paths = [part.strip() for part in body.splitlines() if part.strip()]
        if not paths or paths == ["screen"]:
            return vision.shot(world, note=attrs.get("note") or "screen")
        return vision.attach(world, *paths, note=attrs.get("note", ""))
    return _commit(world, body, attrs)


def _commit(world, body, attrs):
    import os
    import subprocess
    import tempfile
    message = body.strip()
    if not message and not attrs.get("amend"):
        return "commit: empty message"
    def git(*args):
        return subprocess.run(["git", *args], cwd=world.cwd, capture_output=True, text=True)
    if attrs.get("add"):
        added = git("add", *attrs["add"].split())
        if added.returncode:
            return f"git add failed: {added.stderr.strip()}"
    path = None
    args = ["commit"]
    if attrs.get("amend"):
        args.append("--amend")
    if message:
        fd, path = tempfile.mkstemp(suffix=".gitmsg")
        with os.fdopen(fd, "w") as stream:
            stream.write(message + "\n")
        args += ["-F", path]
    else:
        args.append("--no-edit")
    if attrs.get("only"):
        args += ["--", *attrs["only"].split()]
    carried = _carried_in(world, git)
    result = git(*args)
    if path:
        os.unlink(path)
    if result.returncode:
        return f"commit failed:\n{(result.stdout + result.stderr).strip()}"
    head = git("log", "-1", "--format=%h").stdout.strip()
    landed = git("log", "-1", "--format=%B").stdout.strip()
    stat = git("show", "--stat", "--format=", "HEAD").stdout.strip().splitlines()
    suffix = f"message verified ({len(landed)} chars)" if landed == message or not message else "WARNING message differs"
    lines = [f"HEAD {head}", *stat[-1:], suffix]
    if carried:
        lines.append(
            "WARNING not written during this session: " + ", ".join(carried)
            + " -- another writer's changes are in this commit; say so or amend"
        )
    return "\n".join(lines)


def _carried_in(world, git):
    """Staged files whose contents predate this session, newest-mtime first.

    `git add path` stages the whole file, so a worktree with a second writer
    in it puts their in-flight work inside my commit with a clean exit code.
    The harness cannot attribute a *concurrent* write and should not pretend
    to -- but a file that has not been touched since before this session
    began was demonstrably not written by it, and that is worth saying out
    loud before the commit message claims otherwise.
    """
    from desmos.state.persist import session_started

    started = session_started(world)
    if not started:
        return []
    staged = git("diff", "--cached", "--name-only")
    if staged.returncode:
        return []
    older = []
    for name in staged.stdout.split("\n"):
        name = name.strip()
        if not name:
            continue
        target = world.cwd / name
        try:
            if target.stat().st_mtime < started:
                older.append(name)
        except OSError:
            continue  # deleted or unreadable: git's record, not the harness's
    return older[:8]


def _knowledge(world, op, body, attrs):
    if op == "memory":
        from desmos.state.memory import handle_memory
        return handle_memory(world, body, attrs)
    if op == "recall":
        from desmos.state.recall import handle_recall
        return handle_recall(world, body, attrs)
    if op == "system":
        from desmos.kernel.dispatch import set_system
        delete = attrs.get("delete", "") in {"1", "true", "yes"}
        return set_system(world, body, attrs.get("name", ""), delete)
    if op == "plan":
        from desmos.state.plan import handle_plan
        return handle_plan(world, body, **attrs)
    if op == "decide":
        return _decide(world, body)
    if op == "anchor":
        from desmos.kernel import handoff
        from desmos.state.persist import save

        result = handoff.set_anchors(world, body)
        save(world)
        return result
    from desmos.state.persist import save
    items = [line for line in world.notes.get("todo", "").splitlines() if line.strip()]
    for line in [line.strip() for line in body.splitlines() if line.strip()]:
        command, _, rest = line.partition(" ")
        if command == "+":
            items.append(f"[ ] {rest.strip()}")
        elif command.lower() in {"x", "?"} and rest.isdigit() and 0 < int(rest) <= len(items):
            # Three states, not two: an item waiting on somebody else is
            # neither open nor done, and the stop rail skips it rather than
            # demanding work this session cannot do.
            mark = "[x]" if command.lower() == "x" else "[?]"
            item = items[int(rest) - 1]
            head = item[:3]
            items[int(rest) - 1] = (
                mark + item[3:] if head in ("[ ]", "[x]", "[?]") else mark + " " + item
            )
        elif command == "-" and rest.isdigit() and 0 < int(rest) <= len(items):
            items.pop(int(rest) - 1)
    if items:
        world.notes["todo"] = "\n".join(items)
    else:
        world.notes.pop("todo", None)
    save(world)
    # The list is mostly history. Answer with the part that is still work --
    # open and waiting items under their real numbers -- and count the rest,
    # or the result outgrows the list it reports on. Body "all" shows history.
    show_all = body.strip().lower() == "all"
    shown, done = [], 0
    for i, item in enumerate(items, 1):
        if item.startswith("[x]") and not show_all:
            done += 1
            continue
        shown.append(f"{i}. {item}")
    if done:
        shown.append(f"({done} done)")
    return "\n".join(shown) or "empty"


def _decide(world, body: str) -> str:
    """Handle knowledge op=decide: ask | answer | list."""
    from desmos.state.decisions import answer as _answer, pending as _pending, push as _push

    stripped = body.strip()
    low = stripped.lower()

    # "list" or bare body
    if not stripped or low == "list":
        items = _pending(world)
        if not items:
            return "no pending decisions"
        lines = []
        for r in items:
            opts = " | ".join(r["options"])
            lines.append(f"decide:{r['id']} — {r['prompt']}  [{opts}]")
        return "\n".join(lines)

    # "answer <id> <choice>"
    if low.startswith("answer "):
        rest = stripped[7:].strip()
        did, _, choice = rest.partition(" ")
        did = did.strip()
        choice = choice.strip()
        if not did or not choice:
            return "usage: answer <id> <choice>"
        try:
            _answer(world, did, choice)
        except KeyError as exc:
            return str(exc)
        return f"decision {did} closed: {choice}"

    # "ask <prompt> | opt1 | opt2 | ..."
    if low.startswith("ask "):
        rest = stripped[4:]
    else:
        rest = stripped
    parts = [p.strip() for p in rest.split("|")]
    prompt = parts[0]
    options = parts[1:] if len(parts) > 1 else ["yes", "no"]
    did = _push(world, prompt, options)
    # Build ui-choice fence so the TUI renders it as an interactive block.
    opt_lines = "\n".join(f"- {o}" for o in options)
    fence = (
        f"```ui-choice\n"
        f"prompt: decide:{did} — {prompt}\n"
        f"{opt_lines}\n"
        f"```"
    )
    return fence


def _observe(world, op, body, attrs):
    if op == "usage":
        from desmos.kernel import prices
        from desmos.state import persist
        if body.strip().lower() in {"ops", "op"}:
            return _op_rollup(world, persist)
        totals = prices.totals([event.get("usage") or {} for event in world.log])
        cost = sum(prices.cost(event.get("usage") or {}, world.model or "") for event in world.log)
        return f"run {persist.run_id()}  {len(world.log)} calls  in={totals['input_tokens']} out={totals['output_tokens']} cost=${cost:.4f}"
    if op == "slice":
        return _slice(world, body)
    if op == "trajectory":
        from desmos.transport import complete
        raw = body.strip()
        if raw.isdigit():
            return "\n".join(repr(row) for row in complete.trajectory(int(raw)))
        return repr(complete.payload_diff())
    diag = world.ns.get("diag")
    if getattr(diag, "_desmos_diagnostics", None) != 1:
        return "observe: diagnostics unavailable"
    if op == "error":
        value = diag.error(clear=attrs.get("clear", "").lower() in {"1", "true", "yes"})
    elif op == "threads":
        value = diag.threads(
            body.strip() or attrs.get("pattern"),
            limit=attrs.get("limit", 32), depth=attrs.get("depth", 12),
            max_chars=attrs.get("max_chars", 16_384),
        )
    else:
        name = body.strip() or attrs.get("name", "")
        if not name or name not in world.ns:
            return f"observe symbol: name {name!r} is not in ns"
        value = diag.symbol(
            world.ns[name],
            source=attrs.get("source", "").lower() in {"1", "true", "yes"},
            max_chars=attrs.get("max_chars", 8192),
        )
    return json.dumps(value, ensure_ascii=False)


#: Contract scope, written as tag attributes with comma-separated values.
#: Without these the tag could only produce a legacy prose contract -- so the
#: one rule that makes delegation honest (name the paths, the checks and the
#: evidence *before* you spawn) was unwritable in the interface that spawns most
#: children. 136 of 205 recorded runs were unstructured for exactly that reason.
_SCOPE = ("paths", "write", "checks", "tools", "depends", "evidence")


def _simple(attrs):
    scope = {}
    for key in _SCOPE:
        items = tuple(part.strip() for part in attrs.pop(key, "").split(",") if part.strip())
        if items:
            scope[key] = items
    return scope or None


def _agents(world, op, body, attrs):
    from desmos.agents import subagent as agents
    if op == "spawn" and attrs.get("host", "").strip():
        from desmos.agents import remote
        return remote.request(
            world, attrs.pop("host"), body,
            agent=attrs.pop("agent", "general"),
            timeout=float(attrs.pop("timeout", 3600.0)),
        )
    if op == "lineage":
        rid = body.strip() or attrs.get("id", "")
        if not rid:
            return "agents lineage: missing id"
        return json.dumps(agents.lineage(rid))
    if op == "status":
        return json.dumps(agents.status(), default=str)
    if op in {"result", "structured-result", "judgment"}:
        rid = body.strip() or attrs.get("id", "")
        if not rid:
            return f"agents {op}: missing id"
        fn = {"result": agents.result, "structured-result": agents.structured_result, "judgment": agents.judgment}[op]
        return str(fn(rid))
    if op == "wait":
        ids = [part for part in body.replace(",", " ").split() if part]
        if not ids:
            return "agents wait: missing ids"
        return json.dumps(agents.wait(*ids, timeout=float(attrs.get("timeout", 600))), default=str)
    simple = _simple(attrs)
    for key in ("budget", "guidance_every_turns"):
        if key in attrs:
            attrs[key] = int(attrs[key])
    if op == "fanout":
        tasks = [part.strip() for part in body.split("\n---\n") if part.strip()]
        if not tasks:
            return "agents fanout: missing tasks"
        return json.dumps(
            agents.fanout(tasks, agent=attrs.pop("agent", "explore"), simple=simple, **attrs)
        )
    if op == "resume":
        src = attrs.pop("from", "") or attrs.pop("id", "")
        if not src:
            return "agents resume: missing from=<run id>"
        if not body.strip():
            return "agents resume: missing the next task"
        # Identity does not travel, so the source's agent is the default rather
        # than "general": a resume that has to restate the seat it already sits
        # in is a resume that mostly raises.
        run = agents.RUNS.get(src)
        agent = attrs.pop("agent", "") or (run.cfg.agent if run is not None else "general")
        return agents.spawn(body, agent=agent, resume=src, simple=simple, **attrs)
    return agents.spawn(body, agent=attrs.pop("agent", "general"), simple=simple, **attrs)


def _session(world, op, body, attrs):
    if op == "compact":
        from desmos.state import compact
        result = compact.compact(
            world,
            keep=int(attrs.get("keep", 24)),
            floor=int(attrs.get("floor", 40)),
        )
        return f"compacted {result['folded']} messages: {result['before']//1024}KB -> {result['after']//1024}KB"
    if op == "status":
        return json.dumps({
            "model": world.model, "thinking": world.thinking,
            "generation": world.generation, "messages": len(world.messages),
        })
    if op in {"peers", "agents", "inbox", "read", "post", "dismiss"}:
        from desmos.state import persist
        if op == "peers":
            return json.dumps(persist.peers(world), default=str)
        if op == "agents":
            name = str(attrs.get("name", "")).strip()
            if name:
                return json.dumps(persist.agent_upsert(
                    world, name,
                    kind=str(attrs.get("kind", "fork")),
                    host=str(attrs.get("host", "")),
                    parent=str(attrs.get("parent", "")),
                    session_id=str(attrs.get("session_id", "")),
                    status=str(attrs.get("status", "active")),
                ), default=str)
            return json.dumps(persist.roster(world), default=str)
        channel = attrs.get("channel", "general")
        if op == "inbox":
            return json.dumps(
                persist.channel_inbox(
                    world, channel=channel, limit=int(attrs.get("limit", 20))
                ),
                default=str,
            )
        if op == "read":
            messages = persist.channel_read(
                world, channel=channel,
                since=int(attrs.get("since", 0)),
                limit=int(attrs.get("limit", 50)),
            )
            mark = str(attrs.get("mark", "true")).lower() not in {"0", "false", "no", "off"}
            if mark and messages:
                persist.channel_dismiss(world, channel=channel, through=messages[-1]["id"])
            return json.dumps(messages, default=str)
        if op == "dismiss":
            return json.dumps(
                persist.channel_dismiss(
                    world, channel=channel, through=int(attrs.get("through", 0))
                ),
                default=str,
            )
        target = str(
            attrs.get("to") or attrs.get("session_id") or attrs.get("run_id") or ""
        ).strip()
        if target:
            if target == persist.run_id():
                return "session post: target is this session"
            live = {row["run_id"] for row in persist.peers(world)}
            if target not in live:
                return f"session post: target {target!r} is not active"
            channel = persist.peer_channel(target, "request")
        try:
            message = persist.channel_post(
                world, body, channel=channel, author=attrs.get("author", "")
            )
        except ValueError as exc:
            return str(exc)
        if target:
            message.update({"to": target, "kind": "request"})
        else:
            from desmos.agents import remote as _remote
            dispatched = _remote.mention_dispatch(world, channel, body)
            if dispatched:
                message["dispatched"] = dispatched
        return json.dumps(message, default=str)
    model = body.strip() or attrs.pop("model", "")
    if not model:
        return "session switch: missing model"
    switch_fn = world.ns.get("switch")
    if not callable(switch_fn):
        return "session switch: unavailable"
    return str(switch_fn(model, attrs.get("effort")))


def _op_rollup(world, persist):
    """Which advertised ops earn their catalog line, and which never ran."""
    rows = persist.op_rollup(world)
    seen = {name for name, _ in rows}
    idle = [
        f"{family} {name}"
        for family in FAMILIES
        for name in operations(family)
        if f"{family} {name}" not in seen
    ]
    lines = [f"{count:>5}  {name}" for name, count in rows]
    if idle:
        lines.append("never called: " + ", ".join(idle))
        # Rarity is not a retirement criterion: rollback and judgment are
        # insurance, and their value is the tail. docs/self-growth.md,
        # "Retiring an op", is the rule this list feeds.
        lines.append(
            "  (idle is a question, not a verdict -- see docs/self-growth.md,"
            " retiring an op: subsumed, unreachable or misrouting, never rare)"
        )
    return "\n".join(lines) or "no calls recorded"


def _attrs(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        import ast
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _render_exchange(events):
    lines = []
    speech = []

    def flush():
        if speech:
            lines.append("said: " + " ".join("".join(speech).split())[:600])
            speech.clear()

    for event in events:
        kind = event.get("kind")
        text = str(event.get("text") or "")
        if kind == "prompt":
            lines.append("user: " + " ".join(text.split())[:400])
        elif kind == "speech":
            speech.append(text)
        elif kind == "result" and event.get("phase") == "done":
            flush()
            attrs = _attrs(event.get("attrs"))
            op = str(attrs.get("op") or "")
            head = " ".join(str(event.get("text") or "").split())[:300]
            lines.append(f"call {event.get('tag')} {op}".rstrip() + ": " + head)
    flush()
    return "\n".join(lines) or "exchange recorded no speech or calls"


def _slice(world, body):
    """The folded record, by exchange. Empty body is the index."""
    from desmos.state import persist

    raw = body.strip()
    index = persist.exchange_index(world)
    if not raw:
        if not index:
            return "no exchanges recorded"
        return "\n".join(f"{item['n']:>3}. {item['text'][:110]}" for item in index)
    if not raw.isdigit():
        return "observe slice: body is an exchange number, or empty for the index"
    n = int(raw)
    events = persist.exchange_events(world, n)
    if not events:
        return f"observe slice: no exchange {n} (the index holds {len(index)})"
    return _render_exchange(events)
