"""`<recall>` — search prior-session history through the memex-desmos fork.

memex is an EXTERNAL binary (tantivy + usearch + ort; it must never enter our
build). We shell one `memex search ... --json-array` per call — no kernel-owned
daemon; warm calls are ms-scale BM25, freshness is memex's own TTL+flock lease.

memex upstream has no adapter mechanism (SourceKind is a closed clap enum), so
the desmos events live only in a fork that adds `SourceKind::Desmos`. The probe
that tells fork from stock is exactly `memex search --source desmos ...`: stock
memex rejects the label with a clap error, the fork answers. Absent binary or
stock memex => a refusal in prose naming scripts/memex-setup.sh; the model then
falls back to `<bash>` + rg over `.desmos/events/*.jsonl` (reuse of the same
data, not a second search engine).

Doctrine: what the kernel learns arrives as a syscall result on the record.
Results are spill-capped user-role text, and the same secret scrub every other
result gets runs before they spill — recall reads the user's cross-agent
history, so a leaked key in an old transcript must not surface here in the
clear. A child (persist=False) is pinned to `source=desmos`: a prompt-injected
subagent must not query the user's whole machine history.
"""

from __future__ import annotations

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
    "recall unavailable: the memex-desmos fork is not installed (stock memex "
    f"has no `desmos` source). Run {_SETUP} — it installs the pinned fork and "
    "ends with `memex index` so first use is warm. Until then, search the raw "
    "history with <bash>: rg over .desmos/events/*.jsonl."
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


def handle_recall(world: World, body: str, attrs: dict[str, str] | None = None) -> str:
    query = " ".join((body or "").split())
    if not query:
        return "recall failed: query required (put the search text in the body)."
    attrs = attrs or {}
    cmd = _build_cmd(world, query, attrs)
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
        if _is_unknown_source(proc.stderr):
            return _REFUSAL
        # Scrub the diagnostic too: memex may echo the query or partial index
        # content, and doctrine is that nothing recall spills bypasses the scrub.
        err = scrub_secrets((proc.stderr or proc.stdout or "no output").strip())
        return spill(f"recall failed (exit {proc.returncode}): {err}", RESULT_CAP,
                     tag="recall", cwd=world.cwd)

    out = proc.stdout.strip() or "[]"
    return spill(scrub_secrets(out), RESULT_CAP, tag="recall", cwd=world.cwd)
