"""Offline Chrome Trace export from the bridge event journal."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


_INSTANT_EVENTS = {
    "prompt",
    "turn",
    "error",
    "stopped",
    "compacted",
    "pending",
    "resumed",
    "guidance",
    "done",
    "ready",
    "snapshot",
    "picker",
    "notice",
    "intervention",
}
_ARG_FIELDS = (
    "ev",
    "n",
    "model",
    "origin",
    "tag",
    "phase",
    "span_idx",
    "id",
    "action",
    "kind",
    "status",
    "stage",
    "depth",
    "parent",
)


def latest_event_log(cwd: Path) -> Path | None:
    paths = list((cwd / ".desmos" / "events").glob("*.jsonl"))
    return max(paths, key=lambda p: p.stat().st_mtime_ns, default=None)


def _args(row: dict[str, Any], seq: int, **extra: Any) -> dict[str, Any]:
    out = {key: row[key] for key in _ARG_FIELDS if key in row}
    out["source_seq"] = seq
    out.update(extra)
    return out


def _stamp(row: dict[str, Any], mono0: int | None, wall0: int) -> tuple[float, bool]:
    mono = row.get("mono_ns")
    if isinstance(mono, int) and mono0 is not None:
        return (mono - mono0) / 1_000_000.0, False
    wall = row.get("ts")
    if isinstance(wall, int):
        return max(0.0, (wall - wall0) * 1000.0), True
    return 0.0, True


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a percentile without turns")
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def timing_report(rows: list[dict[str, Any]]) -> dict[str, dict[str, float | int | None]]:
    values: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.get("ev") != "timing":
            continue
        name = row.get("name")
        duration = row.get("duration_ns")
        if isinstance(name, str) and isinstance(duration, int) and duration >= 0:
            values[name].append(duration / 1_000_000.0)
    return {
        name: {
            "count": len(durations),
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
        }
        for name, durations in sorted(values.items())
    }


def _monotonic_timing(rows: list[dict[str, Any]]) -> bool:
    stamps = [row.get("mono_ns") for row in rows if isinstance(row.get("seq"), int)]
    return bool(stamps) and all(isinstance(stamp, int) for stamp in stamps) and all(
        left <= right for left, right in zip(stamps, stamps[1:])
    )


def _payload_free(payload: dict[str, Any]) -> bool:
    forbidden = {"body", "content", "prompt", "request", "response", "result", "text"}

    def walk(value: Any) -> bool:
        if isinstance(value, dict):
            return all(key not in forbidden and walk(child) for key, child in value.items())
        if isinstance(value, list):
            return all(walk(child) for child in value)
        return True

    return walk(payload)


def critical_path_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Report prompt-to-done paths paired within one serial bridge session."""
    turns: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    for row in rows:
        if not isinstance(row.get("seq"), int) or not isinstance(row.get("mono_ns"), int):
            continue
        ev = row.get("ev")
        if ev == "prompt":
            if active is not None:
                raise ValueError("overlapping prompt critical paths")
            active = {
                "start_seq": row["seq"],
                "start_ns": row["mono_ns"],
                "syscalls": 0,
            }
        elif ev == "result" and row.get("phase") == "start":
            if active is None:
                raise ValueError("syscall starts outside a prompt critical path")
            active["syscalls"] += 1
        elif ev == "done":
            if active is None:
                raise ValueError("done has no prompt critical path")
            end_ns = row["mono_ns"]
            if end_ns < active["start_ns"]:
                raise ValueError("critical path clock moved backwards")
            turns.append(
                {
                    "start_seq": active["start_seq"],
                    "end_seq": row["seq"],
                    "syscalls": active["syscalls"],
                    "duration_ms": (end_ns - active["start_ns"]) / 1_000_000.0,
                }
            )
            active = None
    if active is not None:
        raise ValueError("prompt critical path has no done event")
    durations = [turn["duration_ms"] for turn in turns]
    by_shape: dict[str, list[float]] = {"no_syscall": [], "single_syscall": []}
    for turn in turns:
        if turn["syscalls"] == 0:
            by_shape["no_syscall"].append(turn["duration_ms"])
        elif turn["syscalls"] == 1:
            by_shape["single_syscall"].append(turn["duration_ms"])
    if any(turn["syscalls"] > 1 for turn in turns):
        raise ValueError("critical path contains more than one syscall")
    return {
        "pairing": "prompt_to_same_session_done",
        "turns": turns,
        "count": len(turns),
        "p50_ms": _percentile(durations, 0.50) if durations else None,
        "p95_ms": _percentile(durations, 0.95) if durations else None,
        "by_shape": {
            name: {
                "count": len(values),
                "p50_ms": _percentile(values, 0.50) if values else None,
                "p95_ms": _percentile(values, 0.95) if values else None,
            }
            for name, values in by_shape.items()
        },
    }


def _boundary(name: str, owner: str, start: dict[str, Any], end: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "owner": owner,
        "start_seq": start["seq"],
        "end_seq": end["seq"],
        "duration_ms": (end["mono_ns"] - start["mono_ns"]) / 1_000_000.0,
    }


def ownership_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Attribute adjacent, non-overlapping boundaries inside each serial turn."""
    turns: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None

    for row in rows:
        if not isinstance(row.get("seq"), int) or not isinstance(row.get("mono_ns"), int):
            continue
        ev = row.get("ev")
        if ev == "prompt":
            if active is not None:
                raise ValueError("overlapping prompt critical paths")
            active = {"prompt": row, "events": [], "syscalls": 0}
        elif ev == "post":
            if active is None:
                raise ValueError("provider post outside a prompt critical path")
            active["events"].append(row)
        elif ev in {"thinking", "speech"}:
            continue
        elif ev == "complete":
            if active is None:
                raise ValueError("provider completion outside a prompt critical path")
            active["events"].append(row)
        elif ev == "result":
            if active is None:
                raise ValueError("syscall result outside a prompt critical path")
            if row.get("phase") == "start":
                active["syscalls"] += 1
                active["events"].append(row)
            elif row.get("phase") == "done":
                active["events"].append(row)
        elif ev == "done":
            if active is None:
                raise ValueError("done has no prompt critical path")
            active["events"].append(row)
            boundaries = []
            owners = {
                ("prompt", "post"): ("prompt_to_post", "desmos"),
                ("post", "complete"): ("post_to_complete", "provider"),
                ("complete", "result"): ("complete_to_dispatch", "desmos"),
                ("result", "result"): ("dispatch", "actual_tool"),
                ("result", "post"): ("result_to_post", "desmos"),
                ("complete", "done"): ("complete_to_done", "desmos"),
            }
            previous = active["prompt"]
            previous_kind = "prompt"
            for event in active["events"]:
                kind = "result" if event.get("ev") == "result" else str(event.get("ev"))
                if kind == "result" and event.get("phase") == "done":
                    kind = "result"
                if (previous_kind, kind) in owners:
                    name, owner = owners[(previous_kind, kind)]
                    boundaries.append(_boundary(name, owner, previous, event))
                previous, previous_kind = event, kind
            turns.append({"start_seq": active["prompt"]["seq"], "end_seq": row["seq"], "syscalls": active["syscalls"], "boundaries": boundaries})
            active = None
    if active is not None:
        raise ValueError("prompt critical path has no done event")
    if any(turn["syscalls"] not in {0, 1} for turn in turns):
        raise ValueError("turn has more than one syscall")
    return {
        "pairing": "prompt_to_same_session_done",
        "turns": turns,
        "unmatched": [],
    }


def export_event_log(input_path: Path, output_path: Path | None = None) -> dict[str, Any]:
    rows = [
        json.loads(line)
        for line in input_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stamped = [row for row in rows if isinstance(row, dict) and "seq" in row]
    if not stamped:
        raise ValueError(f"event log has no stamped events: {input_path}")

    monos = [row["mono_ns"] for row in stamped if isinstance(row.get("mono_ns"), int)]
    mono0 = min(monos) if monos else None
    walls = [row["ts"] for row in stamped if isinstance(row.get("ts"), int)]
    wall0 = min(walls, default=0)
    trace: list[dict[str, Any]] = []
    pending: dict[tuple[str, str], deque[tuple[float, int, dict[str, Any]]]] = defaultdict(deque)
    output_wait: deque[tuple[float, int, dict[str, Any]]] = deque()
    dispatch_wait: deque[tuple[float, int, dict[str, Any]]] = deque()
    unmatched = 0
    explained_unmatched: list[dict[str, Any]] = []
    approximate = mono0 is None

    def instant(name: str, stamp: float, args: dict[str, Any]) -> None:
        trace.append({"name": name, "cat": "desmos", "ph": "i", "s": "t", "ts": stamp, "pid": 1, "tid": 1, "args": args})

    def close_span(
        key: tuple[str, str],
        stamp: float,
        end_seq: int,
        name: str,
        args: dict[str, Any],
    ) -> None:
        nonlocal unmatched
        starts = pending[key]
        if not starts:
            unmatched += 1
            instant(name, stamp, {**args, "unmatched": "end", "end_seq": end_seq})
            return
        start, start_seq, start_args = starts.popleft()
        trace.append(
            {
                "name": name,
                "cat": "desmos",
                "ph": "X",
                "ts": start,
                "dur": max(0.0, stamp - start),
                "pid": 1,
                "tid": 1,
                "args": {**start_args, **args, "start_seq": start_seq, "end_seq": end_seq},
            }
        )

    for row in stamped:
        seq = int(row["seq"])
        stamp, used_wall = _stamp(row, mono0, wall0)
        approximate = approximate or used_wall
        ev = str(row.get("ev") or "")
        if ev == "timing":
            duration_ns = row.get("duration_ns")
            if isinstance(duration_ns, int) and duration_ns >= 0:
                trace.append(
                    {
                        "name": f"desmos.{row.get('name', 'timing')}",
                        "cat": "desmos",
                        "ph": "X",
                        "ts": max(0.0, stamp - duration_ns / 1_000_000.0),
                        "dur": duration_ns / 1_000_000.0,
                        "pid": 1,
                        "tid": 1,
                        "args": {"source_seq": seq},
                    }
                )
            continue
        if ev == "post":
            key = ("post", str(row.get("n", "")))
            args = _args(row, seq)
            pending[key].append((stamp, seq, args))
            output_wait.append((stamp, seq, args))
            dispatch_wait.clear()
            continue
        if ev == "complete":
            key = ("post", str(row.get("n", "")))
            close_span(key, stamp, seq, "provider.turn", _args(row, seq))
            dispatch_wait.append((stamp, seq, _args(row, seq)))
            continue
        if ev in {"thinking", "speech"}:
            if output_wait:
                start, start_seq, args = output_wait.pop()
                trace.append(
                    {
                        "name": "provider.first_output",
                        "cat": "desmos",
                        "ph": "X",
                        "ts": start,
                        "dur": max(0.0, stamp - start),
                        "pid": 1,
                        "tid": 1,
                        "args": {**args, "start_seq": start_seq, "end_seq": seq},
                    }
                )
            continue
        if ev == "result":
            tag = str(row.get("tag") or "syscall")
            index = str(row.get("span_idx", ""))
            key = ("result", f"{tag}:{index}")
            phase = row.get("phase")
            if phase == "start":
                args = _args(row, seq)
                pending[key].append((stamp, seq, args))
                if dispatch_wait:
                    start, start_seq, complete_args = dispatch_wait.popleft()
                    trace.append(
                        {
                            "name": "harness.complete_to_dispatch",
                            "cat": "desmos",
                            "ph": "X",
                            "ts": start,
                            "dur": max(0.0, stamp - start),
                            "pid": 1,
                            "tid": 1,
                            "args": {**complete_args, "start_seq": start_seq, "end_seq": seq},
                        }
                    )
            elif phase == "done":
                close_span(key, stamp, seq, f"syscall.{tag}", _args(row, seq))
            continue
        if ev == "subagent":
            key = ("subagent", str(row.get("id") or ""))
            phase = row.get("phase")
            if phase == "started":
                pending[key].append((stamp, seq, _args(row, seq)))
            elif phase in {"done", "failed", "stopped"}:
                close_span(key, stamp, seq, "subagent", _args(row, seq))
            continue
        if ev in _INSTANT_EVENTS:
            instant(f"event.{ev}", stamp, _args(row, seq))

    while output_wait:
        stamp, seq, args = output_wait.popleft()
        unmatched += 1
        instant("unmatched.provider_first_output", stamp, {**args, "unmatched": "start", "start_seq": seq})

    for key, starts in pending.items():
        while starts:
            stamp, seq, args = starts.popleft()
            unmatched += 1
            instant(f"unmatched.{key[0]}", stamp, {**args, "unmatched": "start", "start_seq": seq})

    # Event order is useful for the viewer and stable enough for equal stamps.
    trace.sort(key=lambda item: (float(item.get("ts", 0)), int(item.get("args", {}).get("source_seq", 0))))
    if output_path is None:
        output_path = input_path.parent.parent / "traces" / f"{input_path.stem}.trace.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "traceEvents": trace,
        "displayTimeUnit": "ms",
        "metadata": {
            "source": str(input_path),
            "approximate_timing": approximate,
            "unmatched_spans": unmatched,
        },
    }
    critical_path = critical_path_report(stamped)
    payload["metadata"]["critical_path"] = critical_path
    ownership = ownership_report(stamped)
    explained_unmatched = []
    unexplained_unmatched = []
    for event in trace:
        name = event.get("name")
        if name == "unmatched.provider_first_output":
            reason = "provider_completed_without_observed_first_output"
            event["args"]["reason"] = reason
            explained_unmatched.append({"kind": "provider_first_output", "start_seq": event["args"].get("start_seq"), "reason": reason})
        elif isinstance(name, str) and name.startswith("unmatched."):
            unexplained_unmatched.append({"kind": name.removeprefix("unmatched."), "start_seq": event["args"].get("start_seq")})
    ownership["unmatched"] = explained_unmatched
    ownership["unexplained_unmatched"] = unexplained_unmatched
    payload["metadata"]["ownership"] = ownership
    payload["metadata"]["timing_attribution"] = timing_report(stamped)
    turns = ownership["turns"]
    shape = {
        "ten_turns": len(turns) == 10,
        "five_no_syscall": sum(turn["syscalls"] == 0 for turn in turns) == 5,
        "five_single_syscall": sum(turn["syscalls"] == 1 for turn in turns) == 5,
    }
    monotonic_timing = _monotonic_timing(stamped) and not approximate
    payload_free = _payload_free(payload)
    unexplained_count = len(unexplained_unmatched)
    payload["metadata"]["guardrails"] = {
        "verdict": "pass" if all((*shape.values(), monotonic_timing, payload_free, unexplained_count == 0)) else "fail",
        "payload_free": payload_free,
        "approximate_timing": approximate,
        "monotonic_timing": monotonic_timing,
        "unexplained_unmatched_spans": unexplained_count,
        **shape,
    }
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return {
        "input": str(input_path),
        "output": str(output_path),
        "events": len(trace),
        "unmatched_spans": unmatched,
        "approximate_timing": approximate,
        "critical_path": critical_path,
    }
