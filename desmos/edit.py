"""Facade: the public SDK surface of desmos.kernel.edit.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.kernel.edit import *  # noqa: F401,F403

__all__ = [
    "Path",
    "apply_edit",
    "apply_edit_line",
    "handle",
    "parse_edit_body",
    "run",
]
