"""Offline trace export checks."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from desmos.front.trace import (
    _stamp,
    critical_path_report,
    export_event_log,
    export_world,
    latest_event_source,
    ownership_report,
)
from desmos.kernel.loop import new_world
from desmos.state import persist


def check() -> None:
    rows = [
        {"ev": "prompt", "seq": 1, "mono_ns": 1_000_000},
        {"ev": "done", "seq": 2, "mono_ns": 3_000_000},
        {"ev": "prompt", "seq": 3, "mono_ns": 4_000_000},
        {"ev": "result", "phase": "start", "seq": 4, "mono_ns": 5_000_000},
        {"ev": "result", "phase": "done", "seq": 5, "mono_ns": 6_000_000},
        {"ev": "done", "seq": 6, "mono_ns": 9_000_000},
    ]
    report = critical_path_report(rows)
    assert report["count"] == 2
    assert report["p50_ms"] == 3.5
    assert report["p95_ms"] == 4.85
    assert report["by_shape"]["no_syscall"]["count"] == 1
    assert report["by_shape"]["single_syscall"]["count"] == 1
    ownership = ownership_report(rows)
    assert any(boundary["owner"] == "actual_tool" for boundary in ownership["turns"][1]["boundaries"])
    assert not ownership.get("unexplained_unmatched", [])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "events.jsonl"
        source.write_text(
            "\n".join(
                [
                    json.dumps({"ev": "session", "session_id": "s", "cwd": str(root), "ts": 1}),
                    json.dumps({"ev": "prompt", "seq": 1, "ts": 1, "mono_ns": 1_000, "text": "secret prompt"}),
                    json.dumps({"ev": "post", "seq": 2, "ts": 1, "mono_ns": 2_000, "n": 1, "model": "m", "request": "secret request"}),
                    json.dumps({"ev": "thinking", "seq": 3, "ts": 1, "mono_ns": 5_000, "text": "secret thinking"}),
                    json.dumps({"ev": "complete", "seq": 4, "ts": 1, "mono_ns": 8_000, "n": 1, "model": "m", "response": "secret response"}),
                    json.dumps({"ev": "result", "phase": "start", "seq": 5, "ts": 1, "mono_ns": 9_000, "tag": "bash", "span_idx": 0, "body": "secret body"}),
                    json.dumps({"ev": "result", "phase": "done", "seq": 6, "ts": 1, "mono_ns": 15_000, "tag": "bash", "span_idx": 0, "text": "secret result"}),
                    json.dumps({"ev": "done", "seq": 7, "ts": 1, "mono_ns": 16_000}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        result = export_event_log(source)
        payload = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
        names = {event["name"] for event in payload["traceEvents"]}
        assert {"provider.turn", "provider.first_output", "syscall.bash", "harness.complete_to_dispatch"} <= names
        assert payload["metadata"]["approximate_timing"] is False
        guardrails = payload["metadata"]["guardrails"]
        assert guardrails["verdict"] == "fail"
        assert guardrails["payload_free"] is True
        assert guardrails["monotonic_timing"] is True
        assert guardrails["ten_turns"] is False
        assert guardrails["five_no_syscall"] is False
        assert guardrails["five_single_syscall"] is False
        assert payload["metadata"]["ownership"]["unexplained_unmatched"] == []
        assert all(event["ts"] >= 0 for event in payload["traceEvents"])
        assert all(event.get("dur", 0) >= 0 for event in payload["traceEvents"])
        encoded = json.dumps(payload)
        assert "secret" not in encoded

        source.write_text(
            "\n".join(
                json.dumps({"ev": ev, "seq": seq, "ts": seq})
                for ev, seq in (("prompt", 1), ("done", 2))
            )
            + "\n",
            encoding="utf-8",
        )
        result = export_event_log(source)
        guardrails = json.loads(Path(result["output"]).read_text(encoding="utf-8"))["metadata"]["guardrails"]
        assert guardrails["verdict"] == "fail"
        assert guardrails["monotonic_timing"] is False

    stamp_ms, approx_ms = _stamp({"ts": 1_700_000_000_500}, None, 1_700_000_000_000)
    assert approx_ms is True
    assert stamp_ms == 500.0, stamp_ms
    stamp_s, approx_s = _stamp({"ts": 2}, None, 1)
    assert approx_s is True
    assert stamp_s == 1000.0, stamp_s
    stamp_mono, approx_mono = _stamp({"mono_ns": 5_000_000, "ts": 9}, 1_000_000, 0)
    assert approx_mono is False
    assert stamp_mono == 4.0, stamp_mono

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        world = new_world(cwd, state_path=cwd / ".desmos" / "harness.sqlite3")
        stream = [
            {"ev": "prompt", "text": "secret prompt"},
            {"ev": "post", "n": 1, "model": "m", "request": "secret request"},
            {"ev": "thinking", "text": "secret thinking"},
            {"ev": "complete", "n": 1, "model": "m", "response": "secret response"},
            {"ev": "result", "phase": "start", "tag": "bash", "span_idx": 0, "body": "secret body"},
            {"ev": "result", "phase": "done", "tag": "bash", "span_idx": 0, "text": "secret result"},
            {"ev": "done"},
        ]
        for i, event in enumerate(stream, start=1):
            persist.record_event(world, event, ts_ms=1_700_000_000_000 + i, mono_ns=i * 1_000)
        src = latest_event_source(cwd)
        assert src is not None and src.suffix == ".sqlite3", src
        out = cwd / "trace.json"
        result = export_world(world, out)
        payload = json.loads(Path(result["output"]).read_text(encoding="utf-8"))
        names = {event["name"] for event in payload["traceEvents"]}
        assert {"provider.turn", "provider.first_output", "syscall.bash", "harness.complete_to_dispatch"} <= names, names
        assert payload["metadata"]["approximate_timing"] is False
        encoded = json.dumps(payload)
        assert "secret" not in encoded
        assert all(event["ts"] >= 0 for event in payload["traceEvents"])
        assert all(event.get("dur", 0) >= 0 for event in payload["traceEvents"])
