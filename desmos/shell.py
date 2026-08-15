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
import signal
import secrets
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any

# Output the shell produced but nobody will read. Keeping both ends matters:
# the head is what the command echoed and started doing, the tail is the error
# or the prompt it is sitting at. Dropping either loses the half that mattered.
MAX_BYTES = 12_000
# Stop reading once the shell has said nothing for this long. Long enough that
# a command pausing to think does not look finished, short enough that an
# interactive prompt comes back promptly.
QUIET = 0.35
# Hard ceiling for one read, not for the command. Five seconds keeps the agent
# loop responsive; a build that runs longer keeps running and the next shell
# call picks up where the output left off. Callers can explicitly use 15s for a
# quiet test, 30s for a build, or 60s only for a known quiet heavyweight job.
DEADLINE = 5.0
# A process that exits immediately still has output in the pty buffer.
EARLY_EXIT_GRACE = 0.15
# How long output must sit unterminated before it counts as a prompt rather
# than a command that is merely slow.
PROMPT_IDLE = 1.0
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
        self.mark = f"__desmos_{secrets.token_hex(4)}_rc:"
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
        argv = [command] if command else ["/bin/bash", "--norc", "--noprofile"]
        env = dict(os.environ)
        env["PS1"] = ""
        env["PS2"] = ""
        # No colours, no cursor tricks, no paste brackets to strip back out.
        env["TERM"] = "dumb"
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

    def _read_until_marker(
        self, quiet: float, deadline: float, *, expect_marker: bool
    ) -> tuple[bytes, bool]:
        """Read until the shell says it is done. Returns (output, finished).

        The marker is the real signal. Quiet only ends the read when there is
        no marker coming -- a multi-line block, or a command sitting at a
        prompt -- and even then only after the shell has actually gone silent.
        """
        out = bytearray()
        hard = time.monotonic() + deadline
        silent_since: float | None = None
        while time.monotonic() < hard:
            chunk = self._drain(min(quiet, hard - time.monotonic()))
            if chunk:
                out.extend(chunk)
                silent_since = None
                if expect_marker and self.mark.encode() in bytes(out):
                    return bytes(out), True
                continue
            if not self.alive():
                out.extend(self._drain(EARLY_EXIT_GRACE))
                return bytes(out), True
            if not expect_marker:
                return bytes(out), True
            # Silence with a marker still owed. Waiting is usually right --
            # `python -m venv` says nothing for seconds and returning here
            # reported it as still running while its output turned up attached
            # to the next command. The exception is a prompt: a program asking
            # a question leaves the cursor after it, so the output does not end
            # in a newline. That is the one silence worth believing.
            now = time.monotonic()
            silent_since = silent_since or now
            waiting_at_prompt = bool(out) and not bytes(out).rstrip(b" ").endswith(b"\n")
            if waiting_at_prompt and now - silent_since >= PROMPT_IDLE:
                return bytes(out), False
        return bytes(out), False

    # ---------------------------------------------------------------- writing

    def send(self, text: str, *, quiet: float = QUIET, deadline: float = DEADLINE) -> str:
        """Write a command and return its output.

        Reading until the output goes quiet is not enough on its own: `python
        -m venv` says nothing for several seconds, so a quiet window returns
        before the command has produced anything and its output turns up
        attached to whatever runs next. So the shell is asked to announce that
        it finished. A single-line command gets a marker appended to the same
        line -- one line, so bash parses it whole and a command that reads
        stdin still reads from the tty rather than eating the marker.

        When the marker never arrives and the shell has gone quiet, the
        command is waiting for input. That is the case that has no completion
        signal, and returning what arrived is the only honest answer: the
        model reads the prompt and replies with another <shell>.
        """
        if not self.alive():
            return "shell exited"
        one_line = "\n" not in text.strip() and self.at_prompt
        payload = (
            f'{text.strip()}; echo "{self.mark}$?"\n' if one_line else text.rstrip("\n") + "\n"
        )
        try:
            os.write(self.master, payload.encode())
        except OSError as exc:
            return f"shell write failed: {exc}"
        raw, done = self._read_until_marker(quiet, deadline, expect_marker=one_line)
        self.at_prompt = done
        body = head_tail(raw)
        # Everything from the marker onward is bookkeeping, not output.
        code = None
        if self.mark in body:
            body, _, rest = body.partition(self.mark)
            code = rest.splitlines()[0].strip() if rest.strip() else None
        body = body.strip()
        if code not in (None, "0"):
            return f"{body}\n[exit {code}]".strip()
        if not done and self.alive():
            # Still running or waiting on input. Say so, or the model reads an
            # empty result as "it finished and printed nothing".
            note = "[still running — send more input, or <shell interrupt=\"1\"/>]"
            return f"{body}\n{note}".strip()
        return body or "(no output)"

    def interrupt(self) -> str:
        """Ctrl-C, for a call that is stuck and a next call that should not be."""
        if not self.alive():
            return "shell exited"
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
        except OSError as exc:
            return f"interrupt failed: {exc}"
        return (head_tail(self._drain(0.5)).strip() or "interrupted")

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


def run(world: Any, body: str, attrs: dict[str, str]) -> str:
    """Dispatch entry for <shell>."""
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
    try:
        deadline = float(attrs.get("timeout") or DEADLINE)
    except ValueError:
        deadline = DEADLINE
    return shell.send(text, deadline=max(0.5, min(deadline, 600.0)))
