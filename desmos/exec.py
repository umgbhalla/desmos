from __future__ import annotations

import ast
import contextlib
import io
import os
import select
import signal
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
        return clip((buf.getvalue() + traceback.format_exc()).strip(), keep="tail")


# How long to keep reading after the command itself exits. Long enough to
# catch output still in flight, short enough that a backgrounded grandchild
# holding the pipe open cannot hold the harness with it.
IO_DRAIN = 2.0


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the command and anything it started, not just the shell."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        proc.kill()
        return
    try:
        proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()


def _drain(
    stream: Any, window: float, on_chunk: OnChunk | None, deadline: float
) -> list[bytes]:
    """Read what is left, bounded. Never blocks on a pipe nobody will close."""
    parts: list[bytes] = []
    end = min(time.monotonic() + window, deadline)
    while time.monotonic() < end:
        ready, _, _ = select.select([stream], [], [], max(0.0, end - time.monotonic()))
        if not ready:
            break
        chunk = stream.read(4096)
        if not chunk:
            break
        parts.append(chunk)
        if on_chunk is not None:
            on_chunk(chunk.decode("utf-8", errors="replace"))
    return parts


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
            # Its own process group. Without this, killing the timed-out
            # process kills `/bin/sh -c` and orphans whatever it started, so a
            # runaway survives the timeout that was supposed to end it.
            start_new_session=True,
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
                _kill_group(proc)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_group(proc)
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
                # The command exited, but a process it backgrounded inherited
                # this pipe and can hold it open for as long as it likes. An
                # unbounded read() here waits for *that* process: `sleep 20 &
                # echo started` with timeout=3 returned after 20 seconds, and
                # for those 20 seconds the deadline check above and the
                # should_stop poll were both unreachable -- the kernel, the
                # bridge's inbox and the TUI's stop button all wedged behind a
                # grandchild nobody was waiting for. Drain briefly, then leave.
                parts.extend(_drain(proc.stdout, IO_DRAIN, on_chunk, deadline))
                break
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
    finally:
        proc.stdout.close()
    out = b"".join(parts).decode("utf-8", errors="replace")
    if timed_out:
        return clip(f"timeout after {limit}s\n{out}".strip(), keep="tail")
    if proc.returncode:
        return clip(f"exit {proc.returncode}\n{out}".strip(), keep="tail")
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
