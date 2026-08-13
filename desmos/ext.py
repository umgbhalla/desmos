"""IPython extension: bind step and world into the live namespace."""

from __future__ import annotations

from desmos.loop import attach


def load_ipython_extension(ip) -> None:
    attach(ip)
    print('desmos ready — bind data, then step("prompt"). reload_sdk() / reset() if a turn goes dead.')


def unload_ipython_extension(ip) -> None:
    ns = getattr(ip, "user_ns", None)
    if not isinstance(ns, dict):
        return
    ns.pop("step", None)
    # leave world; user may still want the heap
