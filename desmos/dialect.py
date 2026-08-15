"""Facade: the public SDK surface of desmos.transport.dialect.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.transport.dialect import *  # noqa: F401,F403

__all__ = [
    "Any",
    "block",
    "capabilities",
    "dialect",
    "family",
    "growth",
]
