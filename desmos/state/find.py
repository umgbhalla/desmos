"""The <find> syscall: fuzzy path search over the world's cwd via fff.

Path search only — content grep already has an owner (bash + rg). One live
``FileFinder`` per ``world.cwd`` lives in a module-global dict built lazily on
the first <find> and kept across ``reload_sdk`` (the ``globals().get`` rail
from ``dispatch._SCOPES``: re-executing this module must not orphan the native
scan threads a live engine owns). The frecency LMDB under ``cwd/.desmos/fff``
is fed by the kernel's own <edit> results through :func:`touch`, so a
recently-edited file ranks higher without the model asking.

An absent extension module is a loud refusal naming the build script, never a
second search implementation as a fallback: the model uses bash/rg instead.
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
    "find unavailable: fff extension module not built "
    "(scripts/build-fff-python.sh); use bash/rg for path/content search"
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


def _new_finder(fff: Any, cwd: Path, *, watch: bool) -> Any:
    # enable_content_indexing=False is explicit: <find> is path search only,
    # so the content index (memory + scan cost) is never built.
    return fff.FileFinder(
        str(cwd),
        frecency_db_path=str(cwd / FRECENCY_DB),
        watch=watch,
        ai_mode=True,
        enable_content_indexing=False,
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


def find(world: Any, query: str, limit: Any = None) -> str:
    """Rank cwd paths against a fuzzy query. One line per hit: path<TAB>score."""
    fff = _import_fff()
    if fff is None:
        return REFUSAL
    q = (query or "").strip()
    if not q:
        return "find: empty query — give a path fragment to search for"
    cwd = Path(world.cwd)
    key = str(cwd.resolve())
    try:
        finder = _engine(fff, key, cwd)
        # The first query (or any query landing during a rescan) waits for the
        # scan and *says so* if it is still going, rather than silently ranking
        # a half-built index the way shared.rs's true-on-uninitialized would.
        note = ""
        if finder.is_scanning():
            finder.wait_for_scan_blocking(SCAN_WAIT_MS)
            if finder.is_scanning():
                note = "(still scanning — results may be incomplete)\n"
        res = finder.search(q, page_size=_limit(limit))
    except Exception:
        # A dead engine (closed handle, bad mmap) is dropped so the next <find>
        # rebuilds it instead of failing forever.
        _ENGINES.pop(key, None)
        return traceback.format_exc()
    if not res.items:
        return f"{note}no matches for {q!r}"
    lines = [
        f"{item.relative_path}\t{score.total}"
        for item, score in zip(res.items, res.scores)
    ]
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
        finder = _new_finder(fff, cwd, watch=False)
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
