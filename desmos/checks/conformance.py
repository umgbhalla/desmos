"""Cross-language conformance (Phase 5.3): the real bridge's NDJSON parses
into the typed Rust Event enum.

The Python side of the pair is `crates/desmos-events`: its test suite parses
the committed golden fixtures, and its `desmos-events-validate` bin is the
line filter this check drives. Here the REAL bridge subprocess is spawned and
driven through the ops whose events the golden fixtures do not cover — ready,
snapshot, picker, notice, error, speech (reset confirmation), intervention
(kill_run on an unknown id) — and its whole captured stream is fed to the
validate bin, together with every golden fixture so both corpora go through
one parser. A stubbed `step` is not drivable over the wire
(`world.complete_fn` is in-process only), so the loop kinds are covered by
the fixtures, not a live model call.

Track 1.3 / 3.1 forms are validated through the same bin's `--log` mode: the
event-log file the bridge writes (`.desmos/events/<session>.jsonl`) and the
attach replay served over the unix socket are both the wire events stamped
with `seq`+`ts` behind one `session` header, and both must parse as such —
with the stamped bodies byte-equal to what stdout carried, in order, because
the file IS the replay substrate.

Vendor-check pattern: SILENT SKIP when the validate bin was never built
(cargo not run on this machine), LOUD when it exists and disagrees with what
the bridge actually said.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _validate_bin() -> Path | None:
    candidates = [
        _ROOT / "target" / profile / "desmos-events-validate"
        for profile in ("debug", "release")
    ]
    built = [p for p in candidates if p.is_file()]
    if not built:
        return None
    return max(built, key=lambda p: p.stat().st_mtime)


def _validate(binary: Path, ndjson: str, what: str, *, log: bool = False) -> None:
    ran = subprocess.run(
        [str(binary)] + (["--log"] if log else []),
        input=ndjson, capture_output=True, text=True, check=False,
    )
    assert ran.returncode == 0, (
        f"{what} does not parse into the typed "
        f"{'LogLine' if log else 'Event'} form (crates/desmos-events):\n"
        f"{ran.stderr.strip()}"
    )


def _attach_replay(sock_path: Path, expect_events: int) -> str:
    """Attach to the live bridge's socket from seq 0 and read the full replay:
    one session header plus every stamped event so far."""
    conn = socket.socket(socket.AF_UNIX)
    conn.settimeout(10)
    conn.connect(str(sock_path))
    try:
        conn.sendall(b'{"op": "attach", "since": 0}\n')
        reader = conn.makefile("r", encoding="utf-8")
        return "".join(reader.readline() for _ in range(1 + expect_events))
    finally:
        conn.close()


def _drive_bridge(tmp: Path) -> tuple[str, str]:
    """Run the real bridge over temp state; return (stdout NDJSON, attach replay)."""
    cwd = tmp / "bridgecwd"
    cwd.mkdir()
    env = dict(os.environ)
    env["DESMOS_SETTINGS"] = str(tmp / "settings.json")  # onboarding: no file yet
    env["PYTHONPATH"] = str(_ROOT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "desmos", "bridge", "--cwd", str(cwd)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    replay = ""
    try:
        # ready arrives unprompted; then one op per event class the golden
        # fixtures cannot contain. Reads are per expected event, so a bridge
        # that stops emitting one of them fails here, not in the parser.
        lines = [proc.stdout.readline()]
        for op, expect in (
            ({"op": "snapshot"}, 1),                       # snapshot
            ({"op": "picker"}, 1),                         # picker
            # Crossing providers: snapshot + the thinking-fence notice.
            ({"op": "model", "model": "gpt-5.6-luna", "effort": "low"}, 2),
            ({"op": "model", "model": "gpt-9-nope"}, 1),   # error
            ({"op": "reset"}, 2),                          # speech + snapshot
            ({"op": "thinking", "level": "low"}, 1),       # snapshot
            # C3 on an unknown id: intervention + its prose notice twin.
            ({"op": "kill_run", "id": "deadbeef"}, 2),
        ):
            proc.stdin.write(json.dumps(op) + "\n")
            proc.stdin.flush()
            for _ in range(expect):
                lines.append(proc.stdout.readline())
        # Late attach while the bridge is live: the socket serves the whole
        # stamped log so far, gapless under the wire lock.
        sock_path = cwd / ".desmos" / "bridge.sock"
        assert sock_path.exists(), (
            f"bridge bound no socket at {sock_path} (AF_UNIX path too long?)"
        )
        replay = _attach_replay(sock_path, len(lines))
    finally:
        proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
        proc.stdin.flush()
        proc.wait(timeout=20)
    assert all(line.endswith("\n") for line in lines), (
        f"bridge closed early: {lines}\n{proc.stderr.read()}"
    )
    kinds = [json.loads(line)["ev"] for line in lines]
    for wanted in ("ready", "snapshot", "picker", "notice", "error", "speech",
                   "intervention"):
        assert wanted in kinds, f"bridge run never emitted {wanted!r}: {kinds}"
    hit = next(json.loads(l) for l in lines if json.loads(l)["ev"] == "intervention")
    assert hit["action"] == "kill_run" and hit["id"] == "deadbeef", hit
    assert "deadbeef" in hit["result"], hit
    return "".join(lines), replay


def _check_log_forms(binary: Path, tmp: Path, stream: str, replay: str) -> None:
    """The event-log file and the attach replay both parse as the stamped
    LogLine form, open with the session header, and carry exactly the wire
    stream's events in order (the file is the replay substrate)."""
    cwd = tmp / "bridgecwd"
    logs = sorted((cwd / ".desmos" / "events").glob("*.jsonl"))
    assert len(logs) == 1, f"expected one event log, found {logs}"
    log_text = logs[0].read_text(encoding="utf-8")

    for what, text in ((str(logs[0]), log_text), ("the attach replay", replay)):
        _validate(binary, text, what, log=True)
        head = json.loads(text.splitlines()[0])
        assert head.get("ev") == "session", f"{what} does not open with the header: {head}"
        # resolve(): the bridge canonicalizes its cwd (macOS /var -> /private/var)
        assert head.get("session_id"), head
        assert Path(head.get("cwd", "")).resolve() == cwd.resolve(), head
        assert logs[0].stem == head["session_id"], (logs[0], head)
        stamped = [json.loads(l) for l in text.splitlines()[1:]]
        wire = [json.loads(l) for l in stream.splitlines()]
        bodies = [{k: v for k, v in e.items() if k not in ("seq", "ts")} for e in stamped]
        assert bodies == wire, (
            f"{what} is not the wire stream: {len(bodies)} stamped vs {len(wire)} wire events"
        )


def check() -> None:
    binary = _validate_bin()
    if binary is None:
        return  # cargo never built here; the Rust half runs where cargo does
    fixtures = sorted((_ROOT / "golden").glob("*.jsonl"))
    assert fixtures, "golden/ fixtures missing"
    for fixture in fixtures:
        _validate(binary, fixture.read_text(encoding="utf-8"), str(fixture))
    with tempfile.TemporaryDirectory() as tmp:
        stream, replay = _drive_bridge(Path(tmp))
        _validate(binary, stream, "the live bridge stream")
        _check_log_forms(binary, Path(tmp), stream, replay)
    # The bin still rejects: feeding it a kind outside the vocabulary must
    # fail, or every assert above was vacuous — in both forms.
    for args, payload in (
        ([], '{"ev": "not_a_kind"}\n'),
        # a wire event is not a log line: the stamps are the writer's
        (["--log"], '{"ev": "done"}\n'),
        # seq must move: a replayed duplicate is a broken writer
        (["--log"], '{"ev": "done", "seq": 3, "ts": 1}\n{"ev": "done", "seq": 3, "ts": 2}\n'),
    ):
        bogus = subprocess.run(
            [str(binary), *args], input=payload,
            capture_output=True, text=True, check=False,
        )
        assert bogus.returncode != 0, f"validate bin accepted {payload!r} with {args}"
