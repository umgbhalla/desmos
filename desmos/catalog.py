"""Facade: the public SDK surface of desmos.kernel.catalog.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.kernel.catalog import *  # noqa: F401,F403

__all__ = [
    "ABI",
    "HIDDEN_NS",
    "PRIOR_KEEP",
    "Path",
    "World",
    "catalog",
    "clip",
    "header",
    "memory_block",
    "ns_index",
    "ns_names",
    "package_root",
    "repo_root",
    "runtime_block",
    "shape_of",
    "skip_name",
    "system_prompt",
]
