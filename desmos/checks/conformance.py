"""Cross-language conformance (Phase 5.3): the real bridge's NDJSON parses
into the typed Rust Event enum.

The Python side of the pair is `crates/desmos-events`: its test suite parses
the committed golden fixtures, and its `desmos-events-validate` bin is the
line filter this check drives. Here the REAL bridge subprocess is spawned and
driven through the ops whose events the golden fixtures do not cover — ready,
snapshot, picker, notice, error, speech (reset confirmation) — and its whole
captured stream is fed to the validate bin, together with every golden
fixture so both corpora go through one parser. A stubbed `step` is not
drivable over the wire (`world.complete_fn` is in-process only), so the loop
kinds are covered by the fixtures, not a live model call.

Vendor-check pattern: SILENT SKIP when the validate bin was never built
(cargo not run on this machine), LOUD when it exists and disagrees with what
the bridge actually said.
"""

from __future__ import annotations

import json
import os
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


def _validate(binary: Path, ndjson: str, what: str) -> None:
    ran = subprocess.run(
        [str(binary)], input=ndjson, capture_output=True, text=True, check=False
    )
    assert ran.returncode == 0, (
        f"{what} does not parse into the typed Event enum "
        f"(crates/desmos-events):\n{ran.stderr.strip()}"
    )


def _drive_bridge(tmp: Path) -> str:
    """Run the real bridge over temp state and return its captured NDJSON."""
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
        ):
            proc.stdin.write(json.dumps(op) + "\n")
            proc.stdin.flush()
            for _ in range(expect):
                lines.append(proc.stdout.readline())
    finally:
        proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
        proc.stdin.flush()
        proc.wait(timeout=20)
    assert all(line.endswith("\n") for line in lines), (
        f"bridge closed early: {lines}\n{proc.stderr.read()}"
    )
    kinds = [json.loads(line)["ev"] for line in lines]
    for wanted in ("ready", "snapshot", "picker", "notice", "error", "speech"):
        assert wanted in kinds, f"bridge run never emitted {wanted!r}: {kinds}"
    return "".join(lines)


def check() -> None:
    binary = _validate_bin()
    if binary is None:
        return  # cargo never built here; the Rust half runs where cargo does
    fixtures = sorted((_ROOT / "golden").glob("*.jsonl"))
    assert fixtures, "golden/ fixtures missing"
    for fixture in fixtures:
        _validate(binary, fixture.read_text(encoding="utf-8"), str(fixture))
    with tempfile.TemporaryDirectory() as tmp:
        stream = _drive_bridge(Path(tmp))
    _validate(binary, stream, "the live bridge stream")
    # The bin still rejects: feeding it a kind outside the vocabulary must
    # fail, or every assert above was vacuous.
    bogus = subprocess.run(
        [str(binary)], input='{"ev": "not_a_kind"}\n',
        capture_output=True, text=True, check=False,
    )
    assert bogus.returncode != 0, "validate bin accepted an unknown ev kind"
