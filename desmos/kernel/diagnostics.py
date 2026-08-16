"""Bounded diagnostics for the persistent Python kernel.

The public object is installed as ``diag`` in ``world.ns``.  Every method
returns plain dict/list/scalar data: no traceback frames, exceptions, Popen
objects, or user values are retained across calls.
"""

from __future__ import annotations

import copy
import inspect
import json
import sys
import threading
import traceback
from typing import Any

_MIN_CHARS = 512
_MAX_CHARS = 32_768
_MAX_FRAMES = 32
_MAX_THREADS = 64
_MAX_DEPTH = 32


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _clip(value: Any, limit: int) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _json_len(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def exception_snapshot(
    exc: BaseException,
    *,
    max_frames: int = 20,
    max_chars: int = 8192,
) -> dict[str, Any]:
    """Turn an exception into bounded plain data without retaining frames."""

    frame_cap = _bounded_int(max_frames, 20, 1, _MAX_FRAMES)
    char_cap = _bounded_int(max_chars, 8192, _MIN_CHARS, _MAX_CHARS)
    extracted = traceback.extract_tb(exc.__traceback__, limit=frame_cap + 1)
    clipped = len(extracted) > frame_cap
    frames = [
        {
            "file": _clip(frame.filename, 1000),
            "line": frame.lineno,
            "function": _clip(frame.name, 300),
            "code": _clip(frame.line or "", 300),
        }
        for frame in extracted[-frame_cap:]
    ]
    result: dict[str, Any] = {
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "message": _clip(exc, 2000),
        "frames": frames,
    }
    if isinstance(exc, SyntaxError):
        result["syntax"] = {
            "file": _clip(exc.filename or "<python>", 1000),
            "line": exc.lineno,
            "offset": exc.offset,
            "text": _clip((exc.text or "").rstrip(), 1000),
        }
    linked = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    if linked is not None:
        result["cause"] = {
            "type": type(linked).__name__,
            "module": type(linked).__module__,
            "message": _clip(linked, 1000),
        }

    # Keep leaf frames: they identify the failure site. If metadata alone is
    # oversized, degrade to a compact but still actionable snapshot.
    while result["frames"] and _json_len({**result, "truncated": True}) > char_cap:
        result["frames"].pop(0)
        clipped = True
    if _json_len({**result, "truncated": clipped}) > char_cap:
        clipped = True
        result.pop("cause", None)
        if "syntax" in result:
            result["syntax"]["file"] = _clip(result["syntax"]["file"], 80)
            result["syntax"]["text"] = _clip(result["syntax"]["text"], 120)
        result["message"] = _clip(result["message"], max(64, char_cap // 4))
    result["truncated"] = clipped
    if _json_len(result) > char_cap:
        result = {
            "type": _clip(type(exc).__name__, 80),
            "module": _clip(type(exc).__module__, 80),
            "message": _clip(exc, max(64, char_cap // 3)),
            "frames": [],
            "truncated": True,
        }
    return result


class Diagnostics:
    """Safe, compact introspection for debugging the live kernel."""

    _desmos_diagnostics = 1

    def __init__(self, last_error: dict[str, Any] | None = None) -> None:
        self._last_error = copy.deepcopy(last_error)

    def __repr__(self) -> str:
        kind = None if self._last_error is None else self._last_error.get("type")
        return f"<Diagnostics last_error={kind or 'none'}>"

    def _record(self, exc: BaseException) -> None:
        self._last_error = exception_snapshot(exc)

    def error(self, *, clear: bool = False) -> dict[str, Any] | None:
        """Return the last uncaught Python-block exception as plain data."""

        result = copy.deepcopy(self._last_error)
        if clear:
            self._last_error = None
        return result

    def symbol(
        self,
        obj: Any,
        *,
        source: bool = False,
        max_chars: int = 8192,
    ) -> dict[str, Any]:
        """Return bounded location/signature metadata for an object."""

        char_cap = _bounded_int(max_chars, 8192, _MIN_CHARS, _MAX_CHARS)
        typ = type(obj)
        result: dict[str, Any] = {
            "type": typ.__name__,
            "type_module": typ.__module__,
            "module": getattr(obj, "__module__", None),
            "name": getattr(obj, "__name__", None),
            "qualname": getattr(obj, "__qualname__", None),
            "file": None,
            "line": None,
            "signature": None,
        }
        errors: list[str] = []
        try:
            result["signature"] = _clip(inspect.signature(obj), 2000)
        except (TypeError, ValueError) as exc:
            errors.append(f"signature: {type(exc).__name__}")
        try:
            result["file"] = inspect.getsourcefile(obj) or inspect.getfile(obj)
        except (TypeError, OSError) as exc:
            errors.append(f"file: {type(exc).__name__}")
        if source:
            try:
                lines, line = inspect.getsourcelines(obj)
                result["line"] = line
                result["source"] = _clip("".join(lines), max(256, char_cap // 2))
            except (TypeError, OSError) as exc:
                errors.append(f"source: {type(exc).__name__}")
        else:
            try:
                _, result["line"] = inspect.getsourcelines(obj)
            except (TypeError, OSError):
                pass
        if errors:
            result["unavailable"] = errors
        if _json_len(result) > char_cap:
            result.pop("source", None)
            for key in ("file", "signature", "qualname", "module", "type_module"):
                if result.get(key) is not None:
                    result[key] = _clip(result[key], 120)
            result["truncated"] = True
        if _json_len(result) > char_cap:
            result = {
                "type": _clip(typ.__name__, 80),
                "name": _clip(getattr(obj, "__name__", None), 80),
                "file": _clip(result.get("file"), 120),
                "line": result.get("line"),
                "truncated": True,
            }
        return result

    def threads(
        self,
        pattern: str | None = None,
        *,
        limit: int = 32,
        depth: int = 12,
        max_chars: int = 16_384,
    ) -> list[dict[str, Any]]:
        """Snapshot thread metadata and leaf-first frame locations, never locals."""

        thread_cap = _bounded_int(limit, 32, 1, _MAX_THREADS)
        depth_cap = _bounded_int(depth, 12, 1, _MAX_DEPTH)
        char_cap = _bounded_int(max_chars, 16_384, _MIN_CHARS, _MAX_CHARS)
        needle = (pattern or "").casefold()
        frames = sys._current_frames()
        out: list[dict[str, Any]] = []
        for thread in threading.enumerate():
            if needle and needle not in thread.name.casefold():
                continue
            stack: list[dict[str, Any]] = []
            frame = frames.get(thread.ident) if thread.ident is not None else None
            while frame is not None and len(stack) < depth_cap:
                stack.append(
                    {
                        "file": _clip(frame.f_code.co_filename, 300),
                        "line": frame.f_lineno,
                        "function": _clip(frame.f_code.co_name, 120),
                    }
                )
                frame = frame.f_back
            item = {
                "name": _clip(thread.name, 200),
                "ident": thread.ident,
                "daemon": thread.daemon,
                "alive": thread.is_alive(),
                "stack": stack,
            }
            while item["stack"] and _json_len([*out, item]) > char_cap:
                item["stack"].pop()
            if _json_len([*out, item]) > char_cap:
                break
            out.append(item)
            if len(out) >= thread_cap:
                break
        return out


def install_diagnostics(ns: dict[str, Any]) -> Diagnostics | None:
    """Install or migrate Desmos diagnostics without clobbering user state."""

    if "diag" not in ns:
        diag = Diagnostics()
        ns["diag"] = diag
        return diag
    existing = ns["diag"]
    if getattr(existing, "_desmos_diagnostics", None) == 1:
        try:
            previous = existing.error()
        except Exception:
            previous = None
        diag = Diagnostics(previous)
        ns["diag"] = diag
        return diag
    return None


def record_exception(ns: dict[str, Any], exc: BaseException) -> None:
    """Best-effort recording; diagnostics must never mask the real failure."""

    diag = ns.get("diag")
    if getattr(diag, "_desmos_diagnostics", None) != 1:
        return
    try:
        diag._record(exc)
    except Exception:
        return
