from __future__ import annotations

import ast
import contextlib
import io
import subprocess
import traceback
from pathlib import Path
from typing import Any, Callable

from desmos.const import BASH_TIMEOUT, FROZEN
from desmos.scan import clip
from desmos.types import Tool, World


def run_python(body: str, world: World) -> str:
    src = body.strip()
    if not src:
        return "(empty)"
    buf = io.StringIO()
    ns = world.ns
    try:
        tree = ast.parse(src)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if not tree.body:
                return "ok"
            *head, last = tree.body
            if head:
                exec(compile(ast.Module(head, []), "<python>", "exec"), ns)
            if isinstance(last, ast.Expr):
                val = eval(compile(ast.Expression(last.value), "<python>", "eval"), ns)
                out = buf.getvalue()
                extra = "" if val is None else repr(val)
                return clip((out + extra).strip() or "ok")
            exec(compile(ast.Module([last], []), "<python>", "exec"), ns)
        return clip(buf.getvalue().strip() or "ok")
    except Exception:
        return clip((buf.getvalue() + traceback.format_exc()).strip())


def run_bash(body: str, cwd: Path) -> str:
    cmd = body.strip()
    if not cmd:
        return "(empty)"
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=BASH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"timeout after {BASH_TIMEOUT}s"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode:
        return clip(f"exit {proc.returncode}\n{out}".strip())
    return clip(out.strip() or "ok")


def callable_from_source(world: World, source: str, name: str) -> Callable[..., Any]:
    local: dict[str, Any] = {}
    exec(compile(source, f"<register:{name}>", "exec"), world.ns, local)
    fn = local.get("handle") or world.ns.get("handle")
    if not callable(fn):
        for v in local.values():
            if callable(v):
                fn = v
                break
    if not callable(fn):
        raise ValueError("no callable handle")
    world.ns[f"handle_{name}"] = fn
    return fn


def register_tag(world: World, body: str, name: str, doc: str) -> str:
    from desmos.persist import save

    if not name or not name.isidentifier():
        return f"register failed: bad name {name!r}"
    if name in FROZEN:
        return f"register failed: {name} is frozen"
    try:
        fn = callable_from_source(world, body, name)
    except Exception:
        return traceback.format_exc()
    world.tools[name] = Tool(name=name, doc=doc or f"user tag <{name}>", source=body, handler=fn)
    save(world)
    return f"registered <{name}>"
