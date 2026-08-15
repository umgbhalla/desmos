"""Facade: the public SDK surface of desmos.kernel.spill.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.kernel.spill import *  # noqa: F401,F403

__all__ = [
    "KEEP",
    "MARK",
    "Path",
    "SPILL_DIR",
    "clip",
    "pointer",
    "spill",
]
