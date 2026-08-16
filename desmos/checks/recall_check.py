"""Checks for the recall lane: the ~/.desmos/registry append and the <recall>
syscall. All state is temp (temp root + temp HOME); nothing touches the real
registry. The seeded round-trip silent-skips unless a memex-desmos fork is on
PATH — the common case is no fork, and then only the refusal path is live.

Every assertion here fails when its fix is reverted:
  - drop the dedup/prune in _append_registry -> registry counts break
  - drop the child source pin in recall._build_cmd -> child argv leaks source
  - drop the FileNotFoundError arm in handle_recall -> absent memex raises
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from desmos.loop import new_world
from desmos.state import persist, recall


def _probe_fork() -> bool:
    """True iff a memex-desmos fork answers `--source desmos` (exit 0)."""
    try:
        return (
            subprocess.run(
                # A query token is required: without it the fork also exits
                # nonzero (missing QUERY), so the probe could never see it. The
                # fork answers `[]` for any query; stock clap-rejects --source.
                [recall.MEMEX, "search", "__probe__", "--source", "desmos", "--limit", "1"],
                capture_output=True,
                timeout=recall.TIMEOUT,
            ).returncode
            == 0
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _check_registry(reg: Path) -> None:
    os.environ["DESMOS_REGISTRY"] = str(reg)
    assert persist.registry_path() == reg
    assert not reg.exists(), "temp registry must start empty"

    root_a = Path(tempfile.mkdtemp())
    wa = new_world(root_a, persist=True)
    persist.save(wa)
    persist.save(wa)  # dedup: two saves, one line
    lines = reg.read_text(encoding="utf-8").split()
    assert lines.count(str(root_a.resolve())) == 1, f"root A not deduped: {lines}"
    assert len(lines) == 1, f"expected one root, got {lines}"

    # A child never writes the registry (persist=False short-circuits save).
    child = new_world(root_a, persist=False)
    persist.save(child)
    assert reg.read_text(encoding="utf-8").split() == [str(root_a.resolve())], (
        "a child (persist=False) must not touch the registry"
    )

    # A second root joins; the set grows, still deduped.
    root_b = Path(tempfile.mkdtemp())
    persist.save(new_world(root_b, persist=True))
    both = set(reg.read_text(encoding="utf-8").split())
    assert both == {str(root_a.resolve()), str(root_b.resolve())}, both

    # Lazy prune: root_b's directory vanishes, then any save rewrites the
    # registry without it.
    import shutil

    shutil.rmtree(root_b)
    persist.save(wa)
    survivors = reg.read_text(encoding="utf-8").split()
    assert str(root_b.resolve()) not in survivors, f"dead root not pruned: {survivors}"
    assert str(root_a.resolve()) in survivors


def _check_child_source_pin() -> None:
    root = Path(tempfile.mkdtemp())
    parent = new_world(root, persist=True)
    child = new_world(root, persist=False)

    # A child asking for the user's claude history is confined to desmos.
    cmd = recall._build_cmd(child, "anything", {"source": "claude"})
    assert "--source" in cmd, cmd
    assert cmd[cmd.index("--source") + 1] == "desmos", f"child source not pinned: {cmd}"

    # The parent is not restricted: its requested source is honored.
    pcmd = recall._build_cmd(parent, "anything", {"source": "claude"})
    assert pcmd[pcmd.index("--source") + 1] == "claude", f"parent source overridden: {pcmd}"


def _check_absent_refusal() -> None:
    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)
    saved = recall.MEMEX
    recall.MEMEX = "/nonexistent/desmos-memex-does-not-exist"
    try:
        out = recall.handle_recall(world, "search me", {})
    finally:
        recall.MEMEX = saved
    assert "memex-setup.sh" in out, f"refusal must name the setup script: {out!r}"
    # An empty query never shells out.
    assert "query required" in recall.handle_recall(world, "   ", {})


def _check_roundtrip_if_fork() -> None:
    if not _probe_fork():
        print("[recall_check] no memex-desmos fork on PATH; round-trip skipped")
        return
    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)
    out = recall.handle_recall(world, "the", {"source": "desmos", "limit": "1"})
    # A live fork returns a JSON array (possibly empty), never our refusal.
    assert "memex-setup.sh" not in out, out
    assert out.lstrip().startswith("["), f"expected --json-array output: {out[:120]!r}"


def _check_secret_scrub() -> None:
    """A provider key in a recalled transcript must never surface in the clear.

    A stubbed `memex` on PATH prints a result carrying this harness's own key
    shapes (sk-ant-…, sk-proj-…, Bearer …); handle_recall must redact them
    before the result is spilled. Reverting memory._SECRET_PATTERNS to the
    underscore-only form (or dropping the scrub_secrets call) fails this.
    """
    keys = ("sk-ant-api03-DEADbeefDEADbeefDEADbeef", "sk-proj-abcdefghijklmnop123456")
    payload = f'[{{"text":"old note with {keys[0]} and {keys[1]}"}}]'
    with tempfile.TemporaryDirectory() as d:
        stub = Path(d) / "memex"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "search" ]; then printf %s ' + repr(payload) + "; exit 0; fi\n"
            "exit 0\n"
        )
        stub.chmod(0o755)
        prior_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{d}:{prior_path}"
        try:
            from desmos.loop import new_world

            with tempfile.TemporaryDirectory() as cwd:
                w = new_world(Path(cwd), persist=False)
                out = recall.handle_recall(w, "old note", {})
            for k in keys:
                assert k not in out, f"recall leaked a key in the clear: {k!r} in {out!r}"
            assert "[REDACTED_SECRET]" in out, f"recall did not redact: {out!r}"
        finally:
            os.environ["PATH"] = prior_path


def check() -> None:
    prior = os.environ.get("DESMOS_REGISTRY")
    with tempfile.TemporaryDirectory() as home:
        try:
            _check_registry(Path(home) / "registry")
        finally:
            if prior is None:
                os.environ.pop("DESMOS_REGISTRY", None)
            else:
                os.environ["DESMOS_REGISTRY"] = prior
    _check_child_source_pin()
    _check_absent_refusal()
    _check_secret_scrub()
    _check_roundtrip_if_fork()


if __name__ == "__main__":
    check()
    print("recall_check ok")
