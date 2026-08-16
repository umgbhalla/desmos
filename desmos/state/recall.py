"""`<recall>` — SQLite session history by default, memex for external sources.

Desmos-owned history lives in the harness database and is ranked by FTS5.
An explicit non-desmos `source=` still delegates to memex for cross-agent
history. Children are always confined to the local Desmos database.
"""

from __future__ import annotations

import json
import os
import subprocess

from desmos.kernel.const import RESULT_CAP
from desmos.kernel.spill import spill
from desmos.kernel.types import World

# Reuse the one result redaction; do not grow a second secret scrubber.
from desmos.state.memory import _redact as scrub_secrets

#: The binary. Overridable so a check (or a side-installed fork) can point at a
#: specific path without touching PATH.
MEMEX = os.environ.get("DESMOS_MEMEX", "memex")

#: memex's own cold-index ceiling is generous; a warm BM25 query is ms-scale, so
#: 30s past that is a cold or missing index, not a slow query.
TIMEOUT = 30

_SETUP = "scripts/memex-setup.sh"

_REFUSAL = (
    "external recall unavailable: memex is not installed. "
    f"Run {_SETUP}, then retry with the requested external source."
)

_COLD = (
    f"recall timed out after {TIMEOUT}s — the index may be cold. Run "
    "`memex index` (scripts/memex-setup.sh ends with it), then retry."
)


def _limit(raw: str | None) -> int | None:
    """A sane positive limit, or None to take memex's default."""
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return max(1, min(n, 100)) if n > 0 else None


def _build_cmd(world: World, query: str, attrs: dict[str, str]) -> list[str]:
    """The argv for one recall. Split out so a check can assert the child
    source pin without a memex on PATH."""
    cmd = [MEMEX, "search", query, "--json-array"]

    source = (attrs.get("source") or "").strip() or None
    if not world.persist:
        # Child pin: ignore whatever a (possibly prompt-injected) child asked
        # for and confine it to this system's own events.
        source = "desmos"
    if source:
        cmd += ["--source", source]

    limit = _limit(attrs.get("limit"))
    if limit is not None:
        cmd += ["--limit", str(limit)]

    mode = (attrs.get("mode") or "").strip().lower()
    if mode == "hybrid":
        cmd.append("--hybrid")
    elif mode == "semantic":
        cmd.append("--semantic")
    # else lexical (BM25) default: no flag, no ONNX init cost.

    return cmd


def _is_unknown_source(stderr: str) -> bool:
    """stock memex clap-rejecting `--source desmos` (the fork detector)."""
    s = stderr.lower()
    return "desmos" in s and ("invalid value" in s or "possible values" in s)


def handle_recall(
    world: World, body: str, attrs: dict[str, str] | None = None
) -> str:
    query = " ".join((body or "").split())
    if not query:
        return "recall failed: query required (put the search text in the body)."
    attrs = attrs or {}
    source = (attrs.get("source") or "desmos").strip().lower()
    if not world.persist:
        source = "desmos"

    if source == "desmos":
        from desmos.state.persist import search_history

        rows = search_history(world, query, _limit(attrs.get("limit")) or 12)
        out = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        return spill(
            scrub_secrets(out), RESULT_CAP, tag="recall", cwd=world.cwd
        )

    cmd = _build_cmd(world, query, {**attrs, "source": source})
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=str(world.cwd),
        )
    except FileNotFoundError:
        return _REFUSAL
    except subprocess.TimeoutExpired:
        return _COLD

    if proc.returncode != 0:
        err = scrub_secrets(
            (proc.stderr or proc.stdout or "no output").strip()
        )
        return spill(
            f"recall failed (exit {proc.returncode}): {err}",
            RESULT_CAP,
            tag="recall",
            cwd=world.cwd,
        )

    out = proc.stdout.strip() or "[]"
    return spill(scrub_secrets(out), RESULT_CAP, tag="recall", cwd=world.cwd)
