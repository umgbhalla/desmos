"""Run the check groups: the floor behind `python -m desmos check`.

Each group lives next to its subsystem (desmos/checks/<layer>.py) and is
independent: one group failing does not stop the others, and the report says
which group broke. `--only <group>` runs one; `--fast` runs the seconds tier.
"""

from __future__ import annotations

import contextlib
import fcntl
import importlib
import os
import time
import traceback
from pathlib import Path

GROUPS = ("layering", "kernel", "transport", "state", "find_check", "recall_check", "agents", "front", "herdr_check", "conformance", "evals_smoke", "e2e")

# The seconds tier: the scan/dispatch/persist/edit repros, with no localhost
# SSE or auth-callback servers (transport), no subagent waits (agents), no
# bridge subprocess (front). Measured 2026-08-18 (M-series laptop): layering
# 0.2s + kernel 10.4s + state 1.0s ~= 12s; transport 0.2s, find_check 0.3s,
# recall_check 0.6s, agents 2.0s, front 6.6s, conformance 0.2s; whole floor
# 21s, down from 33s.
#
# Do not guess where a new second went -- `--profile` attributes it to the
# check line that holds it. Today: 5.1s in the kernel shell dispatch helper
# (~20 state-carrying dispatches, already at a 0.05s prompt-idle floor), 4.3s
# in the front bridge flood poll, 2.0s in the two bash timeout repros, which
# really sleep. If the floor grows past the 30s ceiling, split a group, do
# not skip it.
FAST = ("layering", "kernel", "state")

PINNED_MODEL = "claude-opus-5"


def _run_groups(names: tuple[str, ...]) -> int:
    failed: list[str] = []
    for name in names:
        started = time.monotonic()
        try:
            mod = importlib.import_module(f"desmos.checks.{name}")
            fn = getattr(mod, "check", None) or mod.self_check
            fn()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            traceback.print_exc()
            print(f"[check] {name}: FAILED ({time.monotonic() - started:.1f}s)")
            failed.append(name)
        else:
            print(f"[check] {name}: ok ({time.monotonic() - started:.1f}s)")
    if failed:
        print(f"[check] failed groups: {', '.join(failed)}")
        return 1
    print("self-check ok")
    return 0


def _pinned(names: tuple[str, ...]) -> int:
    """The floor, run against a fixed model on a machine with no settings.

    Two doors let the developer's own configuration into this run. `session/new`
    applies the user's saved ~/.desmos/settings.json to the world it hands
    back, and `World.model` defaults to $DESMOS_MODEL -- so anyone whose last
    switch() was to an OpenAI model ran the whole check in a dialect the fake
    responses here are not written in (loop.turn does not scan XML out of
    openai speech), and it failed for reasons that had nothing to do with the
    thing being checked. Point both at a file this process wrote.
    """
    import json as _json
    import os as _os
    import tempfile
    from pathlib import Path

    from desmos import acp as _acp, const as _const, types as _types

    with tempfile.TemporaryDirectory() as home:
        pin = Path(home) / "settings.json"
        pin.write_text(
            _json.dumps({"provider": "anthropic", "model": PINNED_MODEL, "effort": "low"}),
            encoding="utf-8",
        )
        # The fake responses below are written in the prose dialect: an
        # assistant message whose text is a tag. That is the flag-off path now,
        # and it is still supported, so pin it here and let
        # desmos.anthropic_check drive the tool path with the flag on.
        env = {
            "DESMOS_SETTINGS": str(pin),
            "DESMOS_MODEL": PINNED_MODEL,
            "DESMOS_TOOL_SYSCALLS": "0",
            # save() appends world.cwd here; pin it into the temp dir so no
            # check group touches the developer's real ~/.desmos/registry.
            "DESMOS_REGISTRY": str(Path(home) / "registry"),
        }
        old_env = {k: _os.environ.get(k) for k in env}
        # DEFAULT_MODEL is read from the environment at import, which already
        # happened, so the constant has to be pinned as well as the variable.
        # Every module that reads it by name gets the same value.
        mods = (_const, _types, _acp)
        old_default = [m.DEFAULT_MODEL for m in mods]
        _os.environ.update(env)
        for mod in mods:
            mod.DEFAULT_MODEL = PINNED_MODEL
        try:
            return _run_groups(names)
        finally:
            for key, value in old_env.items():
                if value is None:
                    _os.environ.pop(key, None)
                else:
                    _os.environ[key] = value
            for mod, was in zip(mods, old_default):
                mod.DEFAULT_MODEL = was


# One exclusive advisory lock so a mutation run (edit source, run suite,
# restore) and any other suite run never overlap: an overlapping run would see
# the mutated tree (phantom regression) or the mid-flight restore (phantom
# pass). Same flock pattern as desmos/state/decisions.py and plan.py.
LOCK_PATH = Path(".desmos") / "check.lock"

# Reentrant within one process: the kernel group drives runner.run(only=...)
# through the real entry point (_check_profiler), and flock contends across
# open file descriptions even inside one process. The marker lives in
# os.environ, not a module global, because the kernel group reloads this
# module mid-run and a global would silently reset. Keyed by pid, so a child
# process inherits the variable but fails the comparison and still contends.
_HELD_ENV = "DESMOS_CHECK_LOCK_PID"


@contextlib.contextmanager
def _check_lock(path: Path = LOCK_PATH):
    """Hold the whole-run lock; refuse loudly (SystemExit 3) if another
    process already holds it. Reentrant within this process."""
    if os.environ.get(_HELD_ENV) == str(os.getpid()):
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip() or "unknown holder"
            print(f"[check] another check run holds {path} ({holder}); refusing to overlap it")
            raise SystemExit(3)
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()}\n")
        fh.flush()
        os.environ[_HELD_ENV] = str(os.getpid())
        yield
    finally:
        os.environ.pop(_HELD_ENV, None)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def run(only: str | None = None, fast: bool = False, profile: bool = False) -> int:
    if only is not None:
        if only not in GROUPS:
            print(f"unknown check group {only!r}; groups: {', '.join(GROUPS)}")
            return 2
        names: tuple[str, ...] = (only,)
    elif fast:
        names = FAST
    else:
        names = GROUPS
    with _check_lock():
        if not profile:
            return _pinned(names)
        # Measure before optimising, and measure the line rather than the
        # group: a group timing says kernel is half the floor and cannot say
        # which repro.
        from desmos.checks import profile as _profile

        # `prof` is a local: the kernel group reloads the SDK mid-run, and a
        # tally kept in that module's globals is silently rebound to empty
        # when it does.
        with _profile.profiling() as prof:
            code = _pinned(names)
        print(prof.report())
        return code


def self_check() -> None:
    """The whole floor, loud on failure: what `--check` callers rely on."""
    if run() != 0:
        raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(run())
