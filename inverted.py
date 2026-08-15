#!/usr/bin/env python3
"""Back-compat entry. Implementation lives in the desmos SDK."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from desmos.check import self_check
from desmos.const import DEFAULT_MODEL, MAX_TOKENS
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
    # Same defaults as `desmos run`. This entry used to cap turns at 32 and
    # max_tokens at 8192, so the back-compat path truncated replies the
    # documented one finished and cut long tasks off mid-edit.
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    p.add_argument(
        "--max-total-tokens",
        type=int,
        default=None,
        help="stop the run once this many prompt+completion tokens are billed",
    )
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
