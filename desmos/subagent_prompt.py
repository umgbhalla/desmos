"""Facade: the public SDK surface of desmos.agents.subagent_prompt.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.agents.subagent_prompt import *  # noqa: F401,F403

__all__ = [
    "Any",
    "GT",
    "LT",
    "TaskContract",
    "child_system_prompt",
    "family",
]
