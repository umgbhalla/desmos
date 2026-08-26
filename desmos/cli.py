"""Facade: the public SDK surface of desmos.front.cli.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.front.cli import *  # noqa: F401,F403

__all__ = [
    "MAX_TOKENS",
    "Path",
    "cmd_acp",
    "cmd_auth",
    "cmd_bridge",
    "cmd_check",
    "cmd_comet",
    "cmd_console",
    "cmd_desk",
    "cmd_kernel",
    "cmd_run",
    "cmd_tui",
    "main",
]
