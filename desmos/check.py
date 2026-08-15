"""Facade: the public surface of the check floor.

`python -m desmos check` and `inverted.py --check` reach the suite through
this name; the split groups and the runner live in desmos/checks/.
"""

from desmos.checks.runner import *  # noqa: F401,F403

__all__ = [
    "FAST",
    "GROUPS",
    "PINNED_MODEL",
    "run",
    "self_check",
]
