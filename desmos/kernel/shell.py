from __future__ import annotations

"""A shell that stays alive between syscalls.

`<shell>` is the default for command workflows. One PTY per named session stays
alive until the process exits, so cd, exports, builds, tests, and interactive
prompts behave the way they do in a terminal -- because it *is* one.

`<bash>` is the deliberately hermetic alternative: one subprocess per call,
with no state kept. Use it for a quick isolated probe where that reset is useful,
not for a workflow that may continue, ask a question, or outlive one read.

The hard part is not keeping it alive, it is knowing when to stop reading. A
shell that is waiting at a prompt looks exactly like a shell still working:
both are a file descriptor with nothing on it yet. There is no completion
signal to wait for, so this does what Codex's unified_exec does -- read until
the output goes quiet, or until a deadline, and return what arrived. The model
reads the tail, sees a prompt, and answers it with another <shell>.
"""

import errno
import fcntl
import os
import pty
import re
import select
import shlex
import signal
import secrets
import struct
import subprocess
import tempfile
import termios
import threading
import time
from pathlib import Path
from typing import Any, Callable

# Output the shell produced but nobody will read. Keeping both ends matters:
# the head is what the command echoed and started doing, the tail is the error
# or the prompt it is sitting at. Dropping either loses the half that mattered.
MAX_BYTES = 12_000
# Commands get one short foreground observation window. If they outlive it,
# a monitor becomes the sole PTY reader and resumes the agent on completion.
QUIET = 0.20
INITIAL_WINDOW = 0.75
# Kept as a compatibility alias for callers that imported the old constant.
# It is no longer a model-selected read deadline.
DEADLINE = INITIAL_WINDOW
EARLY_EXIT_GRACE = 0.15
# An explicit question with the cursor left on it is the one silence worth
# believing. Short, because the foreground window is short: a prompt that loses
# the race is answered by the monitor instead, which is correct but slower.
PROMPT_IDLE = 0.3
# What the shell prints to say a command finished, carrying its exit code.
# Plain ASCII, and random per shell: a control byte in the command line is
# something bash itself chokes on, and a fixed string could appear in real
# output. Randomising means the only way to see it is for us to have asked.


# CSI/OSC and the stray single-character escapes a pty can still deliver.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    """Drop terminal control sequences a transcript has no use for.

    TERM=dumb stops most of it, but anything the command itself emits still
    arrives -- a progress bar's cursor moves, a linter's colours. They are
    invisible in a terminal and pure noise in a result block.
    """
    return _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def head_tail(data: bytes, cap: int = MAX_BYTES) -> str:
    """Keep both ends of oversized output, name what went missing."""
    text = strip_ansi(data.decode("utf-8", errors="replace"))
    if len(text) <= cap:
        return text
    half = cap // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n…[{dropped} chars omitted]…\n{text[-half:]}"


class Shell:
    """One login-ish shell on a pty, addressed by name."""

    def __init__(self, cwd: Path, command: str | None = None) -> None:
        self.cwd = cwd
        self.mark = ""
        self._lock = threading.RLock()
        self._generation = 0
        self.monitoring = False
        # False while a command is still running and reading stdin. Appending
        # the marker then does not reach bash at all -- the waiting program
        # reads it as its own input, which is how `; echo "...$?"` ended up
        # inside a python input() answer.
        self.at_prompt = True
        self.master, slave = pty.openpty()
        # Not $SHELL. The user's shell loads the user's rc, which means the
        # result of a syscall depends on someone's dotfiles -- on this machine
        # zsh answered `pwd` with a themed prompt and bracketed-paste escapes
        # wrapped around the answer. --norc --noprofile is the same shell on
        # every machine, and without -i it runs the line and says nothing else.
        # A pty echoes what is typed, and wraps that echo at the window width
        # with backspaces and padding. A long command therefore came back as
        # shredded copies of itself before any real output. Turn the echo off
        # and the problem stops existing rather than needing to be unpicked.
        try:
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ECHO  # lflag
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            # Wide enough that a program formatting to the terminal width does
            # not hard-wrap its own output into the transcript.
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        except (OSError, termios.error):
            pass
        # interrupt() targets the tty's foreground process group. Bash only
        # gives foreground jobs their own group with monitor mode enabled;
        # without it Linux puts bash and the job in one group, so Ctrl-C also
        # aborts the sourced completion marker and the monitor never settles.
        argv = [command] if command else ["/bin/bash", "--norc", "--noprofile", "-m"]
        env = dict(os.environ)
        env["PS1"] = ""
        env["PS2"] = ""
        # No colours, no cursor tricks, no paste brackets to strip back out.
        env["TERM"] = "dumb"
        # A PTY makes tools believe a human can answer a pager. There is no
        # second input loop while a monitor owns the terminal, so `git diff`
        # launching `less` waits forever and looks exactly like a broken
        # monitor. Agent shells always stream output; explicit interactive
        # programs can still be invoked by name.
        for key in ("PAGER", "GIT_PAGER", "SYSTEMD_PAGER", "MANPAGER"):
            env[key] = "cat"
        env.pop("PROMPT_COMMAND", None)
        self.proc = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(cwd),
            env=env,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave)
        # Whatever the shell says on startup is not an answer to anything.
        self._drain(0.4)

    # ---------------------------------------------------------------- reading

    def _drain(self, window: float) -> bytes:
        out = bytearray()
        end = time.monotonic() + window
        while time.monotonic() < end:
            ready, _, _ = select.select([self.master], [], [], max(0.0, end - time.monotonic()))
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                # The child closed its end; nothing more is coming.
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                raise
            if not chunk:
                break
            out.extend(chunk)
        return bytes(out)

    def _read_chunk(self, window: float) -> bytes:
        return self._drain(window)

    @staticmethod
    def _is_prompt(data: bytes) -> bool:
        """Recognise explicit interactive questions, not generic silence."""
        if not data or data.rstrip(b" ").endswith(b"\n"):
            return False
        line = strip_ansi(data.decode("utf-8", errors="replace")).splitlines()[-1].strip()
        return bool(
            re.search(
                r"(?i)(?:password|passphrase|enter\b|input\b|continue\?|proceed\?|"
                r"are you sure|\[[yn]/[yn]\]|\(y/n\)|\?)\s*$",
                line,
            )
        )

    def _command_payload(self, text: str) -> bytes:
        """Source a temporary script so multiline commands retain shell state."""
        fd, raw_path = tempfile.mkstemp(prefix="desmos-shell-", suffix=".sh")
        path = Path(raw_path)
        try:
            os.write(fd, text.rstrip("\n").encode() + b"\n")
        finally:
            os.close(fd)
        self.mark = f"__desmos_{secrets.token_hex(8)}_rc:"
        quoted = shlex.quote(str(path))
        payload = (
            f"source {quoted}; __desmos_rc=$?; rm -f {quoted}; "
            f"printf '\\n%s%s\\n' {shlex.quote(self.mark)} \"$__desmos_rc\"\n"
        )
        return payload.encode()

    def _format(self, raw: bytes, *, waiting: bool = False) -> str:
        body = head_tail(raw)
        code = None
        if self.mark and self.mark in body:
            body, _, rest = body.partition(self.mark)
            code = rest.splitlines()[0].strip() if rest.strip() else None
        body = body.strip()
        if code not in (None, "0"):
            return f"{body}\n[exit {code}]".strip()
        if waiting:
            note = "[waiting for input — reply through this shell, or interrupt it]"
            return f"{body}\n{note}".strip()
        return body or "(no output)"

    def _observe(
        self,
        *,
        window: float | None,
        on_chunk: Callable[[str], None] | None = None,
        generation: int,
    ) -> tuple[bytes, str]:
        """Read until completion, an explicit prompt, or the foreground window."""
        out = bytearray()
        deadline = None if window is None else time.monotonic() + window
        silent_since: float | None = None
        marker = self.mark.encode()
        while True:
            if generation != self._generation:
                return bytes(out), "replaced"
            span = QUIET if deadline is None else min(QUIET, max(0.0, deadline - time.monotonic()))
            if deadline is not None and span <= 0:
                return bytes(out), "running"
            chunk = self._read_chunk(span)
            if chunk:
                out.extend(chunk)
                # Bound monitor memory while retaining both diagnostic ends.
                if len(out) > MAX_BYTES * 2:
                    half = MAX_BYTES // 2
                    out[:] = out[:half] + b"\n...[monitor output omitted]...\n" + out[-half:]
                silent_since = None
                if on_chunk is not None:
                    visible = strip_ansi(chunk.decode("utf-8", errors="replace"))
                    if self.mark not in visible:
                        on_chunk(visible)
                if marker in out:
                    return bytes(out), "done"
                continue
            if not self.alive():
                out.extend(self._drain(EARLY_EXIT_GRACE))
                return bytes(out), "done"
            now = time.monotonic()
            silent_since = silent_since or now
            if self._is_prompt(bytes(out)) and now - silent_since >= PROMPT_IDLE:
                return bytes(out), "prompt"
            if deadline is not None and now >= deadline:
                return bytes(out), "running"

    def _monitor(
        self,
        world: Any,
        name: str,
        generation: int,
        on_chunk: Callable[[str], None] | None,
    ) -> str:
        raw, state = self._observe(window=None, on_chunk=on_chunk, generation=generation)
        with self._lock:
            if generation != self._generation:
                return f"shell {name} monitor superseded"
            self.monitoring = False
            self.at_prompt = state == "done" and self.alive()
        if state == "prompt":
            return self._format(raw, waiting=True)
        if state == "replaced":
            return f"shell {name} monitor superseded"
        return self._format(raw)

    def _start_monitor(
        self,
        world: Any,
        name: str,
        generation: int,
        on_chunk: Callable[[str], None] | None,
    ) -> None:
        with self._lock:
            if self.monitoring:
                return
            self.monitoring = True
        # The monitor hands the finished command to the pending queue -- the
        # same upward seam kernel/loop.py uses for resume, function-level for
        # the same reason: kernel code must not import the agents layer at
        # module scope.
        from desmos.agents import pending

        pending.register(
            world,
            f"shell {name}",
            lambda: self._monitor(world, name, generation, on_chunk),
        )

    # ---------------------------------------------------------------- writing

    def send(
        self,
        world: Any,
        name: str,
        text: str,
        *,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        """Start a command or send input; long work is monitored automatically."""
        if not self.alive():
            return "shell exited"
        with self._lock:
            if not self.at_prompt:
                try:
                    os.write(self.master, text.rstrip("\n").encode() + b"\n")
                except OSError as exc:
                    return f"shell write failed: {exc}"
                if self.monitoring:
                    return "[input sent; the existing shell monitor will report the next event]"
                generation = self._generation
            else:
                self._generation += 1
                generation = self._generation
                payload = self._command_payload(text)
                self.at_prompt = False
                try:
                    os.write(self.master, payload)
                except OSError as exc:
                    self.at_prompt = True
                    return f"shell write failed: {exc}"

        raw, state = self._observe(
            window=INITIAL_WINDOW,
            on_chunk=on_chunk,
            generation=generation,
        )
        with self._lock:
            if state == "done":
                self.at_prompt = self.alive()
                return self._format(raw)
            if state == "prompt":
                return self._format(raw, waiting=True)
        self._start_monitor(world, name, generation, on_chunk)
        body = self._format(raw)
        note = "[running; monitored automatically — do not poll this shell]"
        return f"{body}\n{note}".strip()

    def interrupt(self) -> str:
        """Ctrl-C the foreground job, leaving the persistent shell alive."""
        if not self.alive():
            return "shell exited"
        try:
            # Bash gives each foreground job its own process group. Signalling
            # the shell's group misses `less`, cargo, and every other real job;
            # the tty's foreground pgrp is the authoritative target.
            pgid = os.tcgetpgrp(self.master)
            if pgid <= 0:
                pgid = os.getpgid(self.proc.pid)
            os.killpg(pgid, signal.SIGINT)
        except OSError as exc:
            return f"interrupt failed: {exc}"
        # Once monitoring begins it is the sole PTY reader. Competing here can
        # steal the completion marker and turn a successful interrupt into a
        # permanently pending task.
        if self.monitoring:
            return "interrupt sent; the shell monitor will report completion"
        return head_tail(self._drain(0.5)).strip() or "interrupted"

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> str:
        if self.alive():
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except OSError:
                pass
            self.proc.wait(timeout=5)
        try:
            os.close(self.master)
        except OSError:
            pass
        return "closed"


# -------------------------------------------------------------------- registry


def get(world: Any, name: str) -> Shell:
    """The named shell, started on first use."""
    shells = world.shells
    live = shells.get(name)
    if live is not None and live.alive():
        return live
    if live is not None:
        # It died. Replacing it silently beats handing back a corpse.
        live.close()
    shells[name] = Shell(world.cwd)
    return shells[name]


def close_all(world: Any) -> None:
    for shell in list(world.shells.values()):
        try:
            shell.close()
        except Exception:  # noqa: BLE001 — teardown must not raise
            pass
    world.shells.clear()


def run(
    world: Any,
    body: str,
    attrs: dict[str, str],
    *,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """Dispatch entry for the persistent monitored shell."""
    name = (attrs.get("id") or "main").strip() or "main"
    if attrs.get("close") in {"1", "true", "yes"}:
        live = world.shells.pop(name, None)
        return live.close() if live is not None else f"no shell {name!r}"
    shell = get(world, name)
    if attrs.get("interrupt") in {"1", "true", "yes"}:
        return shell.interrupt()
    text = body.strip("\n")
    if not text.strip():
        return "(empty)"
    return shell.send(world, name, text, on_chunk=on_chunk)
