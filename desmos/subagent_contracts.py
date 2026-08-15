"""Facade: the public SDK surface of desmos.agents.subagent_contracts.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.agents.subagent_contracts import *  # noqa: F401,F403

__all__ = [
    "Any",
    "CheckResult",
    "Claim",
    "EvidenceRef",
    "Judgment",
    "OBSERVABLE_EVIDENCE",
    "RunResult",
    "TaskContract",
    "dataclass",
    "field",
    "judge",
    "parse_run_result",
]
