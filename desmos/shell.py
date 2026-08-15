"""Facade: the public SDK surface of desmos.kernel.shell.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.kernel.shell import *  # noqa: F401,F403

__all__ = [
    "Any",
    "DEADLINE",
    "EARLY_EXIT_GRACE",
    "MAX_BYTES",
    "PROMPT_IDLE",
    "Path",
    "QUIET",
    "Shell",
    "close_all",
    "get",
    "head_tail",
    "run",
    "strip_ansi",
]
