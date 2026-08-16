"""Desmos SDK. A coding agent that rebuilds its own harness.

Data stays in the kernel and the model peeks by name; notes, syscalls, skills
and this SDK are writable from inside a turn, live on the next dispatch, and
reversible through evolve/rollback.
"""

from desmos.loop import attach, bind_step, new_world, reload, reload_sdk, run_turns
from desmos.types import World

__all__ = ["World", "attach", "bind_step", "new_world", "reload", "reload_sdk", "run_turns"]
