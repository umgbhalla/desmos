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
    "knowledge": ("todo", "plan"),
    "observe": ("usage", "trajectory", "error", "symbol", "threads"),
    "agents": (
        "spawn", "fanout", "resume", "lineage",
        "status", "result", "structured-result", "judgment", "wait",
    ),
    "session": ("compact", "status", "switch", "peers", "read", "post"),
}


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
        if target not in world.tools:
            return f"{block.tag} op {op!r} is unavailable in this world"
        return Block(target, body, attrs)
    if op not in DIRECT_OPS.get(block.tag, ()):
        return _bad_op(block, op)
    return Block(block.tag, body, {**attrs, "op": op})


def policy_target(block: Block) -> str:
    op, _ = _op(block)
    return DIRECT_TARGETS.get((block.tag, op), block.tag)


def direct(world: World, block: Block) -> str:
    op, attrs = _op(block)
    if block.tag == "workspace":
        return _workspace(world, op, block.body, attrs)
    if block.tag == "knowledge":
        return _knowledge(world, op, block.body, attrs)
    if block.tag == "observe":
        return _observe(world, op, block.body, attrs)
    if block.tag == "agents":
        return _agents(op, block.body, attrs)
    if block.tag == "session":
        return _session(world, op, block.body, attrs)
    return _bad_op(block, op)



def _legacy(world, name, body, attrs):
    tool = world.tools.get(name)
    if tool is None or tool.handler is None:
        return None
    return str(tool.handler(body, **attrs))


def _workspace(world, op, body, attrs):
    existing = _legacy(world, op, body, attrs)
    if existing is not None:
        return existing
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
    result = git(*args)
    if path:
        os.unlink(path)
    if result.returncode:
        return f"commit failed:\n{(result.stdout + result.stderr).strip()}"
    head = git("log", "-1", "--format=%h").stdout.strip()
    landed = git("log", "-1", "--format=%B").stdout.strip()
    stat = git("show", "--stat", "--format=", "HEAD").stdout.strip().splitlines()
    suffix = f"message verified ({len(landed)} chars)" if landed == message or not message else "WARNING message differs"
    return "\n".join([f"HEAD {head}", *stat[-1:], suffix])


def _knowledge(world, op, body, attrs):
    if op == "plan":
        from desmos.state.plan import handle_plan
        return handle_plan(world, body, **attrs)
    existing = _legacy(world, "todo", body, attrs)
    if existing is not None:
        return existing
    from desmos.state.persist import save
    items = [line for line in world.notes.get("todo", "").splitlines() if line.strip()]
    for line in [line.strip() for line in body.splitlines() if line.strip()]:
        command, _, rest = line.partition(" ")
        if command == "+":
            items.append(f"[ ] {rest.strip()}")
        elif command.lower() == "x" and rest.isdigit() and 0 < int(rest) <= len(items):
            items[int(rest) - 1] = items[int(rest) - 1].replace("[ ]", "[x]", 1)
        elif command == "-" and rest.isdigit() and 0 < int(rest) <= len(items):
            items.pop(int(rest) - 1)
    if items:
        world.notes["todo"] = "\n".join(items)
    else:
        world.notes.pop("todo", None)
    save(world)
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1)) or "empty"

def _observe(world, op, body, attrs):
    if op == "usage":
        existing = _legacy(world, "usage", body, attrs)
        if existing is not None:
            return existing
        from desmos.kernel import prices
        from desmos.state import persist
        totals = prices.totals([event.get("usage") or {} for event in world.log])
        cost = sum(prices.cost(event.get("usage") or {}, world.model or "") for event in world.log)
        return f"run {persist.run_id()}  {len(world.log)} calls  in={totals['input_tokens']} out={totals['output_tokens']} cost=${cost:.4f}"
    if op == "trajectory":
        existing = _legacy(world, "traj", body, attrs)
        if existing is not None:
            return existing
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


def _agents(op, body, attrs):
    from desmos.agents import subagent as agents
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
        existing = _legacy(world, "compact", body, attrs)
        if existing is not None:
            return existing
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
    if op in {"peers", "read", "post"}:
        from desmos.state import persist
        if op == "peers":
            return json.dumps(persist.peers(world), default=str)
        channel = attrs.get("channel", "conflicts")
        if op == "read":
            return json.dumps(
                persist.channel_read(
                    world, channel=channel,
                    since=int(attrs.get("since", 0)),
                    limit=int(attrs.get("limit", 50)),
                ),
                default=str,
            )
        try:
            message = persist.channel_post(
                world, body, channel=channel, author=attrs.get("author", "")
            )
        except ValueError as exc:
            return str(exc)
        return json.dumps(message, default=str)
    model = body.strip() or attrs.pop("model", "")
    if not model:
        return "session switch: missing model"
    switch_fn = world.ns.get("switch")
    if not callable(switch_fn):
        return "session switch: unavailable"
    return str(switch_fn(model, attrs.get("effort")))
