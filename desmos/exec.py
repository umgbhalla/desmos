from __future__ import annotations

import ast
import contextlib
import ctypes
import io
import os
import select
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from desmos.const import BASH_TIMEOUT, FROZEN, RESULT_CAP
from desmos.spill import spill
from desmos.types import Tool, World

OnChunk = Callable[[str], None]
ShouldStop = Callable[[], bool]


# redirect_stdout and redirect_stderr replace process-global objects. Concurrent
# Python tools would otherwise restore each other's writers out of order and
# leak ordinary text onto the bridge's NDJSON stdout.
_PYTHON_STDIO_LOCK = threading.RLock()


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


# A <python> block runs on the harness's own thread, so `while True: pass` did
# not hang a call, it hung the kernel: the deadline nobody set never arrived,
# the TUI's stop button set a flag nothing was reading, and there is no prompt
# left to press ctrl-C at. Long enough that real work is never cut short.
PYTHON_TIMEOUT = 300.0
# How often the watchdog re-throws. Once is not enough: a bare `except:` around
# the wedged line swallows the first one, and then it is wedged again.
_WATCH_TICK = 0.05


class PythonStopped(BaseException):
    """Thrown into a <python> block that outran its deadline or was cancelled.

    BaseException, like the ctrl-C it stands in for. As an Exception, an
    ordinary `try: ... except Exception: pass` inside the block caught every
    throw and went straight back round the loop -- the abort was swallowed by
    defensive code that was not even trying to resist it.
    """


def _in_python_block(ident: int) -> bool:
    """True while a frame compiled from a <python> block is on that thread.

    The one thing the watcher can see from outside that an async exception
    cannot skip: a frame is off the stack because the interpreter took it off.
    Everything the kernel thread could set for us -- an Event, a flag -- can be
    interrupted on the bytecode before it.
    """
    frame = sys._current_frames().get(ident)
    while frame is not None:
        if frame.f_code.co_filename == "<python>":
            return True
        frame = frame.f_back
    return False


@contextlib.contextmanager
def _watchdog(should_stop: ShouldStop | None, timeout: float) -> Any:
    """Raise PythonStopped into this thread once it has run too long.

    An async exception lands on an arbitrary bytecode boundary, so a block cut
    this way can leave half-done state. That is the price against a kernel that
    never comes back, and it is only paid after we have decided to abort. It
    also cannot reach into a C call: `time.sleep(300)` runs its full 300s and
    reports the stop when it returns. Nothing short of a subprocess can, and a
    subprocess is a different tag -- <python> exists to touch world.ns.
    """
    ident = threading.get_ident()
    tid = ctypes.c_ulong(ident)
    setexc = ctypes.pythonapi.PyThreadState_SetAsyncExc
    reason: list[str] = []
    finished = threading.Event()

    def watch() -> None:
        deadline = time.monotonic() + timeout
        thrown = False
        while not finished.wait(_WATCH_TICK):
            if not reason:
                if should_stop is not None and should_stop():
                    reason.append("cancelled")
                elif time.monotonic() >= deadline:
                    reason.append(f"timed out after {timeout:g}s")
                else:
                    continue
            if thrown and not _in_python_block(ident):
                # A throw has landed and the block is off the stack, so there
                # is nothing left to stop. Waiting for finished instead would
                # leak this thread whenever the throw arrived in the two
                # bytecodes between the yield returning and the finally that
                # sets it -- and a leaked watcher re-arms PythonStopped into
                # whatever the kernel does next, for as long as it lives.
                return
            setexc(tid, ctypes.py_object(PythonStopped))
            thrown = True

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        yield reason
    finally:
        finished.set()
        try:
            watcher.join()
        finally:
            # A re-arm the watcher fired just before it stopped may still be
            # pending, undelivered, on this thread. Clear it or the kernel gets
            # a PythonStopped one call later, out of nowhere.
            setexc(tid, None)


def run_python(
    body: str,
    world: World,
    *,
    on_chunk: OnChunk | None = None,
    should_stop: ShouldStop | None = None,
    timeout: float | None = None,
) -> str:
    src = body.strip()
    if not src:
        return "(empty)"
    buf = _ChunkWriter(on_chunk)
    ns = world.ns
    # Bound before the with: a stop thrown while the watchdog is still starting
    # never reaches `as stopped`, and the handler below read it anyway.
    stopped: list[str] = []
    try:
        with _watchdog(should_stop, PYTHON_TIMEOUT if timeout is None else timeout) as stopped:
            # ast.parse ran outside the redirect, so a warning raised while
            # *parsing* -- SyntaxWarning for an invalid escape like '\|', or for
            # `assert(x, y)` -- went to the real fd 2. Under the TUI that is the
            # terminal: the bytes painted over whatever cell the cursor happened
            # to be in, usually inside the input box, and nothing scheduled a
            # redraw to clear them. The model never saw the warning either.
            # Parse inside the capture, and name the file what the model wrote.
            with _PYTHON_STDIO_LOCK, contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                tree = ast.parse(src, filename="<python>")
                if not tree.body:
                    return "ok"
                *head, last = tree.body
                if head:
                    exec(compile(ast.Module(head, []), "<python>", "exec"), ns)
                if isinstance(last, ast.Expr):
                    val = eval(compile(ast.Expression(last.value), "<python>", "eval"), ns)
                    out = buf.getvalue()
                    extra = "" if val is None else repr(val)
                    return spill((out + extra).strip() or "ok", RESULT_CAP, tag="python", cwd=world.cwd)
                exec(compile(ast.Module([last], []), "<python>", "exec"), ns)
        return spill(buf.getvalue().strip() or "ok", RESULT_CAP, tag="python", cwd=world.cwd)
    except PythonStopped:
        why = stopped[0] if stopped else "stopped"
        return spill(
            f"{buf.getvalue()}\n[python {why} — the block was cut here]".strip(),
            RESULT_CAP,
            tag="python",
            cwd=world.cwd,
            keep="tail",
        )
    except Exception:
        return spill(
            (buf.getvalue() + traceback.format_exc()).strip(),
            RESULT_CAP,
            tag="python",
            cwd=world.cwd,
            keep="tail",
        )


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
        return spill(f"timeout after {limit}s\n{out}".strip(), RESULT_CAP, tag="bash", cwd=cwd, keep="tail")
    if proc.returncode:
        return spill(f"exit {proc.returncode}\n{out}".strip(), RESULT_CAP, tag="bash", cwd=cwd, keep="tail")
    return spill(out.strip() or "ok", RESULT_CAP, tag="bash", cwd=cwd)


def callable_from_source(world: World, source: str, name: str) -> Callable[..., Any]:
    # The handler is whichever `def` *this* source wrote, named for the tag or
    # named handle. Nothing else counts. Taking whatever the namespace happened
    # to hold registered a `handle` some older <python> block had left behind;
    # taking the first callable in the exec locals registered the private
    # helper above the handler, or `pathlib.Path` off an import line -- and the
    # tag answered "registered <loud>" either way while dispatching that.
    #
    # An assignment counts too, as long as this source wrote it: `handle =
    # functools.partial(...)` and `handle = lambda body, **a: ...` are handlers
    # the model writes, and looking only at `def` refused them -- or, with one
    # helper def beside the assignment, silently registered the helper.
    defs = []
    bound = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append(node.name)
        elif isinstance(node, ast.Assign):
            bound += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.append(node.target.id)
    if name in defs or name in bound:
        pick = name
    elif "handle" in defs or "handle" in bound:
        pick = "handle"
    elif len(defs) == 1:
        # One def and no ambiguity about which one it is.
        pick = defs[0]
    elif defs:
        raise ValueError(
            f"<register name={name!r}> body defines {', '.join(defs)} and none of "
            f"them is the handler; name it `{name}` or `handle`"
        )
    else:
        raise ValueError(
            f"<register name={name!r}> body defined no function; write "
            f"`def {name}(body, **attrs): ...` or `def handle(body, **attrs): ...`"
        )
    # One namespace, not globals + a throwaway locals dict: a helper the body
    # defines has to still be there when the handler runs and calls it.
    exec(compile(source, f"<register:{name}>", "exec"), world.ns)
    fn = world.ns.get(pick)
    if not callable(fn):
        raise ValueError(f"<register name={name!r}>: {pick} is not callable")
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
