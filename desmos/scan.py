"""Facade: the public SDK surface of desmos.kernel.scan.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.kernel.scan import *  # noqa: F401,F403

__all__ = [
    "ATTR",
    "BULLET",
    "Block",
    "FENCE",
    "INDENTED",
    "RESULT_CAP",
    "TAG_ANY",
    "TAG_OPEN",
    "clip",
    "scan",
    "scan_spans",
    "trailing_residue",
]
