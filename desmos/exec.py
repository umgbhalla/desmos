from __future__ import annotations

import ast
import contextlib
import io
import select
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from desmos.const import BASH_TIMEOUT, FROZEN
from desmos.scan import clip
from desmos.types import Tool, World

OnChunk = Callable[[str], None]
ShouldStop = Callable[[], bool]


class _ChunkWriter(io.TextIOBase):
    """Capture stdout/stderr and forward each write to the TUI."""

    def __init__(self, on_chunk: OnChunk | None) -> None:
        self.buf = io.StringIO()
        self.on_chunk = on_chunk

    def write(self, s: str) -> int:  # type: ignore[override]
        if not s:
            return 0
        self.buf.write(s)
        if self.on_chunk is not None:
            self.on_chunk(s)
        return len(s)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        return self.buf.getvalue()


def run_python(
    body: str,
    world: World,
    *,
    on_chunk: OnChunk | None = None,
) -> str:
    src = body.strip()
    if not src:
        return "(empty)"
    buf = _ChunkWriter(on_chunk)
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


def run_bash(
    body: str,
    cwd: Path,
    *,
    on_chunk: OnChunk | None = None,
    should_stop: ShouldStop | None = None,
    timeout: float | None = None,
) -> str:
    cmd = body.strip()
    if not cmd:
        return "(empty)"
    limit = BASH_TIMEOUT if timeout is None else timeout
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except OSError as exc:
        return f"bash failed: {exc}"
    assert proc.stdout is not None
    parts: list[bytes] = []
    deadline = time.monotonic() + limit
    timed_out = False
    try:
        while True:
            if should_stop is not None and should_stop():
                proc.kill()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                timed_out = True
                break
            ready, _, _ = select.select([proc.stdout], [], [], min(0.1, remaining))
            if ready:
                chunk = proc.stdout.read(256)
                if not chunk:
                    break
                parts.append(chunk)
                if on_chunk is not None:
                    on_chunk(chunk.decode("utf-8", errors="replace"))
            elif proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    parts.append(rest)
                    if on_chunk is not None:
                        on_chunk(rest.decode("utf-8", errors="replace"))
                break
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    finally:
        proc.stdout.close()
    out = b"".join(parts).decode("utf-8", errors="replace")
    if timed_out:
        return clip(f"timeout after {limit}s\n{out}".strip())
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
