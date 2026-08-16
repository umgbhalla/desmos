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
        out = recall.handle_recall(world, "search me", {"source": "claude"})
    finally:
        recall.MEMEX = saved
    assert "memex-setup.sh" in out, f"refusal must name the setup script: {out!r}"
    # An empty query never shells out.
    assert "query required" in recall.handle_recall(world, "   ", {})


def _check_sql_roundtrip() -> None:
    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)
    world.messages.extend([
        {"role": "user", "content": "find the cobalt narwhal"},
        {"role": "assistant", "content": "cobalt narwhal acknowledged"},
    ])
    persist.save(world)

    out = recall.handle_recall(world, "cobalt narwhal", {"limit": "2"})
    assert "cobalt narwhal" in out, out
    assert out.lstrip().startswith("["), out

    # A child cannot redirect to another source, but may read this workspace.
    child = new_world(root, persist=False)
    child_out = recall.handle_recall(
        child, "cobalt narwhal", {"source": "claude", "limit": "1"}
    )
    assert "cobalt narwhal" in child_out, child_out


def _check_secret_scrub() -> None:
    """A provider key in a recalled transcript must never surface in the clear.

    A stubbed `memex` on PATH prints a result carrying this harness's own key
    shapes (sk-ant-…, sk-proj-…, Bearer …); handle_recall must redact them
    before the result is spilled. Reverting memory._SECRET_PATTERNS to the
    underscore-only form (or dropping the scrub_secrets call) fails this.
    """
    keys = (
        "sk-ant-api03-" + "DEADbeefDEADbeefDEADbeef",
        "sk-proj-" + "abcdefghijklmnop123456",
    )
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
                w = new_world(Path(cwd), persist=True)
                out = recall.handle_recall(
                    w, "old note", {"source": "claude"}
                )
            for k in keys:
                assert k not in out, f"recall leaked a key in the clear: {k!r} in {out!r}"
            assert "[REDACTED_SECRET]" in out, f"recall did not redact: {out!r}"
        finally:
            os.environ["PATH"] = prior_path


def _check_cross_session_ranking() -> None:
    """History spans sessions, and the live session must not drown it.

    Session one records a decision. Session two asks about it and -- as really
    happens -- repeats the same words while asking, so its rows match *more*
    strongly on raw bm25. Without LIVE_SESSION_PENALTY the question outranks
    the answer, which is the defect this test exists for: recall returning the
    turn that asked instead of the turn that decided.

    Reverting either fix fails this: drop the penalty and the live rows win on
    term frequency; reindex `result` events and the tool output carrying the
    query text comes back as if someone had said it.
    """
    root = Path(tempfile.mkdtemp())
    prior = os.environ.get(persist.SESSION_ID_ENV)
    try:
        os.environ[persist.SESSION_ID_ENV] = "0" * 31 + "1"
        past = new_world(root, persist=True)
        past.messages.extend([
            {"role": "user", "content": "should the kestrel index be sharded"},
            {"role": "assistant", "content": "decision: kestrel index stays single-shard"},
        ])
        persist.save(past)

        os.environ[persist.SESSION_ID_ENV] = "0" * 31 + "2"
        live = new_world(root, persist=True)
        live.messages.append({
            "role": "assistant",
            "content": "kestrel index kestrel index kestrel index -- looking up kestrel index",
        })
        persist.save(live)
        # Tool output that merely echoes the query must never be indexed as
        # something a participant said.
        persist.record_event(
            live,
            {"ev": "result", "text": "kestrel index kestrel index sharded sharded"},
            ts_ms=1,
            mono_ns=1,
        )
        persist.record_event(
            live, {"ev": "notice", "text": "kestrel notice retained"}, ts_ms=2, mono_ns=2
        )

        rows = persist.search_history(live, "kestrel index", limit=10)
        assert rows, "cross-session history was not reachable at all"
        top = rows[0]
        assert top["session_id"].endswith("1"), (
            "the live session outranked prior history: "
            f"{[(r['session_id'][-1], r['kind'], round(r['score'], 2)) for r in rows]}"
        )
        assert any("single-shard" in r["text"] for r in rows), (
            f"the recorded decision was not returned: {[r['text'][:40] for r in rows]}"
        )
        assert not any(r["kind"] == "event:result" for r in rows), (
            f"tool output was indexed as authored history: {[r['kind'] for r in rows]}"
        )
        # The demotion is a penalty, not a filter: the live session stays
        # reachable, which is what a post-fold lookup depends on.
        assert any(r["session_id"].endswith("2") for r in rows), (
            "the live session was excluded rather than demoted"
        )
        assert any(
            r["kind"] == "event:notice" for r in persist.search_history(live, "kestrel notice")
        ), "non-result events must still be indexed"
    finally:
        if prior is None:
            os.environ.pop(persist.SESSION_ID_ENV, None)
        else:
            os.environ[persist.SESSION_ID_ENV] = prior


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
    _check_sql_roundtrip()
    _check_cross_session_ranking()


if __name__ == "__main__":
    check()
    print("recall_check ok")
