"""The <find> syscall: fff-backed path, glob, content, and symbol search.

One live content-capable ``FileFinder`` per ``world.cwd`` lives in a
module-global dict built lazily on first use and kept across ``reload_sdk``
(the ``globals().get`` rail from ``dispatch._SCOPES`` prevents orphaned native
watch threads). The same engine provides typo-resistant fuzzy paths, query
constraints, SIMD plain/regex/fuzzy grep, multi-pattern grep, and definition
classification. The frecency LMDB under ``cwd/.desmos/fff`` is fed by the
kernel's own <edit> results through :func:`touch`.

An absent extension module is a loud refusal naming the build script, never a
second search implementation as a fallback: the model can use bash/rg instead.
"""

from __future__ import annotations

import os
import traceback
from pathlib import Path
from typing import Any

from desmos.kernel.const import RESULT_CAP
from desmos.kernel.spill import spill

DEFAULT_LIMIT = 20
SCAN_WAIT_MS = 5000
#: The frecency LMDB, relative to a world's cwd. The engine and touch() must
#: name the same path or an edit's touch is invisible to a later <find>.
FRECENCY_DB = ".desmos/fff"
REFUSAL = (
    "find unavailable: fff extension module not built; use bash/rg for path/content search"
)

# One FileFinder per resolved cwd. globals().get so reload_sdk (which
# re-executes this module) keeps the live engines instead of orphaning their
# native scan threads and rebuilding on the next call — same rail as
# dispatch._SCOPES.
_ENGINES: dict[str, Any] = globals().get("_ENGINES", {})


def _import_fff() -> Any:
    try:
        import fff

        return fff
    except Exception:
        return None


def _new_finder(fff: Any, cwd: Path, *, watch: bool, content: bool = True) -> Any:
    return fff.FileFinder(
        str(cwd),
        frecency_db_path=str(cwd / FRECENCY_DB),
        watch=watch,
        ai_mode=True,
        enable_content_indexing=content,
    )


def _engine(fff: Any, key: str, cwd: Path) -> Any:
    finder = _ENGINES.get(key)
    if finder is None:
        finder = _new_finder(fff, cwd, watch=True)
        _ENGINES[key] = finder
    return finder


def _limit(raw: Any) -> int:
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return n if n > 0 else DEFAULT_LIMIT


_MODE_ALIASES = {
    "path": "path",
    "file": "path",
    "files": "path",
    "glob": "glob",
    "grep": "grep",
    "content": "grep",
    "symbol": "symbol",
    "symbols": "symbol",
    "multi": "multi",
    "multi_grep": "multi",
}


def _context(raw: Any) -> int:
    try:
        return max(0, min(20, int(str(raw).strip())))
    except (TypeError, ValueError):
        return 0


def _format_grep(res: Any, limit: int, *, definitions_first: bool) -> list[str]:
    items = list(res.items)
    if definitions_first:
        items.sort(key=lambda item: not item.is_definition)
    lines: list[str] = []
    for item in items[:limit]:
        marker = " [def]" if item.is_definition else ""
        text = item.line_content.strip()
        lines.append(
            f"{item.relative_path}:{item.line_number}:{item.col + 1}{marker}\t{text}"
        )
        lines.extend(f"  | {line.rstrip()}" for line in item.context_before)
        lines.extend(f"  | {line.rstrip()}" for line in item.context_after)
    if len(items) > limit or res.next_file_offset:
        lines.append("(more matches available; raise limit or narrow the query)")
    return lines


def find(
    world: Any,
    query: str,
    limit: Any = None,
    mode: Any = None,
    match: Any = None,
    context: Any = None,
    constraints: Any = None,
    **_attrs: Any,
) -> str:
    """Search cwd with fff; mode is path, glob, grep, symbol, or multi."""
    fff = _import_fff()
    if fff is None:
        return REFUSAL
    q = (query or "").strip()
    if not q:
        return "find: empty query — give a path fragment, identifier, or pattern"

    requested_mode = str(mode or "path").strip().lower()
    operation = _MODE_ALIASES.get(requested_mode)
    if operation is None:
        return "find: invalid mode — use path, glob, grep, symbol, or multi"

    matcher = str(match or "plain").strip().lower()
    if matcher not in {"plain", "regex", "fuzzy"}:
        return "find: invalid match — use plain, regex, or fuzzy"

    cwd = Path(world.cwd)
    key = str(cwd.resolve())
    n = _limit(limit)
    try:
        finder = _engine(fff, key, cwd)
        # The first query (or any query landing during a rescan) waits for the
        # scan and says so if it is still going instead of silently searching
        # a half-built index.
        note = ""
        if finder.is_scanning():
            finder.wait_for_scan_blocking(SCAN_WAIT_MS)
            if finder.is_scanning():
                note = "(still scanning — results may be incomplete)\n"

        if operation == "path":
            res = finder.search(q, page_size=n)
            lines = [
                f"{item.relative_path}\t{score.total}"
                for item, score in zip(res.items, res.scores)
            ]
        elif operation == "glob":
            res = finder.glob(q, page_size=n)
            lines = [
                f"{item.relative_path}\t{score.total}"
                for item, score in zip(res.items, res.scores)
            ]
        else:
            kwargs = {
                "mode": matcher,
                "max_matches_per_file": n,
                "page_limit": n,
                "before_context": _context(context),
                "after_context": _context(context),
                "classify_definitions": True,
            }
            if operation == "multi":
                patterns = [line.strip() for line in q.splitlines() if line.strip()]
                res = finder.multi_grep(
                    patterns,
                    constraints=str(constraints).strip() if constraints else None,
                    **kwargs,
                )
            else:
                res = finder.grep(q, **kwargs)
            lines = _format_grep(
                res,
                n,
                definitions_first=operation in {"grep", "symbol", "multi"},
            )
    except Exception:
        # A dead engine (closed handle, bad mmap) is dropped so the next call
        # rebuilds it instead of failing forever.
        _ENGINES.pop(key, None)
        return traceback.format_exc()

    if not lines:
        return f"{note}no matches for {q!r}"
    return spill(note + "\n".join(lines), RESULT_CAP, tag="find", cwd=cwd)


def touch(world: Any, path: str) -> None:
    """Record a just-edited path in the frecency LMDB.

    A best-effort side channel fired at the dispatch edit choke point, so every
    world — root or child — feeds the ranking by construction. The model called
    <edit>, not <find>: a missing or broken engine is silent here, never a
    refusal on the edit result. Reuses the live engine when one exists;
    otherwise opens the frecency DB alone (watch=False, no retained index) so
    recording a touch never hydrates a full scan.
    """
    if not path:
        return
    fff = _import_fff()
    if fff is None:
        return
    cwd = Path(world.cwd)
    abs_path = Path(path) if os.path.isabs(path) else (cwd / path)
    key = str(cwd.resolve())
    try:
        live = _ENGINES.get(key)
        if live is not None:
            live.track_access(str(abs_path))
            return
        finder = _new_finder(fff, cwd, watch=False, content=False)
        try:
            finder.track_access(str(abs_path))
        finally:
            finder.close()
    except Exception:
        pass


def reset() -> None:
    """Close every live engine and forget them. For teardown and checks."""
    while _ENGINES:
        _key, finder = _ENGINES.popitem()
        try:
            finder.close()
        except Exception:
            pass
