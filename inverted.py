#!/usr/bin/env python3
"""Back-compat entry. Implementation lives in the desmos SDK."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from desmos.check import self_check
from desmos.const import DEFAULT_MODEL
from desmos.loop import attach, bind_step, new_world, reload, reload_sdk, run, run_turns

__all__ = [
    "attach",
    "bind_step",
    "new_world",
    "reload",
    "reload_sdk",
    "run",
    "run_turns",
]


def main() -> int:
    p = argparse.ArgumentParser(description="Inverted coding harness")
    p.add_argument("task", nargs="?", default="", help="coding task")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--max-turns", type=int, default=32)
    p.add_argument("--max-tokens", type=int, default=8192)
    p.add_argument("--cwd", default=".")
    p.add_argument("--out", default="")
    p.add_argument("--check", action="store_true")
    args = p.parse_args()
    if args.check:
        self_check()
        return 0
    if not args.task:
        p.error("task required (or --check)")
    if not args.out:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.out = str(Path("runs") / f"task-{stamp}")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
