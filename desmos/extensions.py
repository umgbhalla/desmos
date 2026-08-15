"""Facade: the public SDK surface of desmos.state.extensions.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.state.extensions import *  # noqa: F401,F403

__all__ = [
    "Any",
    "Callable",
    "ExtAPI",
    "Path",
    "extension_roots",
    "load_extensions",
]
