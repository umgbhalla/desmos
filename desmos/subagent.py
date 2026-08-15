"""Facade: the public SDK surface of desmos.agents.subagent.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.agents.subagent import *  # noqa: F401,F403

__all__ = [
    "AGENTS",
    "Any",
    "CAPS",
    "DIR",
    "EffectiveConfig",
    "Judgment",
    "PARENT",
    "PERSONAS",
    "Path",
    "ROLE_GUIDE",
    "RUNS",
    "Run",
    "RunResult",
    "TaskContract",
    "ThreadPoolExecutor",
    "asdict",
    "bind",
    "dataclass",
    "fanout",
    "field",
    "gather",
    "judge",
    "judgment",
    "parse_run_result",
    "resolve",
    "result",
    "set_emitter",
    "spawn",
    "spawn_many",
    "status",
    "structured_result",
    "wait",
]
