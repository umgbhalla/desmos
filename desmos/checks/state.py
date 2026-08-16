"""State checks: persist, memory, generations, skills."""

from __future__ import annotations

import json
from pathlib import Path

from desmos.dispatch import dispatch
from desmos.generations import evolve, gen_dir, rollback
from desmos.loop import new_world
from desmos.catalog import system_prompt
from desmos.types import Block


def _memory(body: str, attrs: dict | None = None) -> Block:
    return Block("knowledge", body, {"op": "memory", **(attrs or {})})


def check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)

        # Durable memory is a progressive-disclosure store, not a newest-tail
        # log. Migration keeps an exact backup, promotes old high-priority facts
        # into the routing summary, and leaves details available through bounded
        # tool retrieval.
        memory_dir = cwd / "memory-check"
        memory_dir.mkdir()
        legacy = (
            "# MEMORY\n\n## 2025-01-01\n"
            "- Umang prefers actual tools before narration.\n"
            "## 2026-01-01\n"
            "- newest noise " + "x" * 4000 + "\n"
        )
        (memory_dir / "MEMORY.md").write_text(legacy, encoding="utf-8")
        memory_world = new_world(memory_dir, state_path=memory_dir / "harness.sqlite3")
        memory_prompt = system_prompt(memory_world)
        assert "Umang prefers actual tools before narration" in memory_prompt
        assert "x" * 500 not in memory_prompt
        assert (memory_dir / "memories" / "legacy_MEMORY.md").read_text(encoding="utf-8") == legacy
        assert (memory_dir / "memories" / "records.jsonl").is_file()
        assert (memory_dir / "memory_summary.md").is_file()

        remembered = dispatch(
            memory_world,
            _memory(
                "Umang's name is Umang.",
                {"id": "user.umang.identity", "scope": "user", "kind": "identity"},
            ),
        )
        updated = dispatch(
            memory_world,
            _memory(
                "Umang's name is Umang.",
                {"id": "user.umang.identity", "scope": "user", "kind": "identity"},
            ),
        )
        search_result = dispatch(memory_world, _memory( "search Umang identity", {}))
        # Writing the same id twice updates the record instead of adding a
        # second one -- searching would otherwise return both and the model
        # would read two versions of the same fact.
        assert search_result.count("user.umang.identity") == 1, search_result
        read_result = dispatch(memory_world, _memory( "read user.umang.identity", {}))
        assert '"scope": "user"' in read_result
        dispatch(memory_world, _memory( "verify user.umang.identity", {}))

        secret_result = dispatch(
            memory_world,
            _memory(
                "api_key=abcdefghijk123456789",
                {"id": "repo.secret-test", "scope": "repo", "kind": "test"},
            ),
        )
        secret_read = dispatch(memory_world, _memory( "read repo.secret-test", {}))
        assert "[REDACTED_SECRET]" in secret_read
        assert "abcdefghijk123456789" not in secret_read

        memory_world2 = new_world(memory_dir, state_path=memory_dir / "harness.sqlite3")
        assert "Umang's name is Umang" in system_prompt(memory_world2)
        dispatch(memory_world2, _memory( "forget user.umang.identity", {}))
        gone = dispatch(memory_world2, _memory( "search user.umang.identity", {}))
        assert "user.umang.identity" not in gone, gone
        dispatch(memory_world2, _memory( "consolidate", {}))

        ping = cwd / ".desmos" / "skills" / "ping"
        ping.mkdir(parents=True)
        (ping / "SKILL.md").write_text(
            "---\nname: ping\ndescription: reply pong\n---\n# ping\nbody\n",
            encoding="utf-8",
        )
        (ping / "skill.py").write_text("def handle(body, **a):\n    return 'pong:' + body\n", encoding="utf-8")
        world = new_world(cwd, state_path=cwd / "harness.sqlite3")
        assert dispatch(world, Block("harness", "", {"op": "skill", "name": "ping"})).endswith("body\n")
        assert dispatch(world, Block("ping", "hi", {})) == "pong:hi"

        grown = cwd / ".desmos" / "skills" / "later"
        grown.mkdir(parents=True)
        (grown / "SKILL.md").write_text(
            "---\nname: later\ndescription: appeared after start\n---\n# later\nok\n",
            encoding="utf-8",
        )
        assert not any(s.name == "later" for s in world.skills)
        dispatch(world, Block("harness", "", {"op": "reload"}))
        assert any(s.name == "later" for s in world.skills)
        assert dispatch(world, Block("harness", "", {"op": "skill", "name": "later"})).endswith("ok\n")

        out = dispatch(
            world,
            Block("harness", "def handle(body, **a):\n    return body.upper()\n", {"op": "register", "name": "echo", "doc": "uppercase"}),
        )
        assert dispatch(world, Block("echo", "hi", {})) == "HI"

        dispatch(world, Block("knowledge", "prefer tests", {"op": "system", "name": "style"}))
        assert "prefer tests" in system_prompt(world)

        world2 = new_world(cwd, state_path=cwd / "harness.sqlite3")
        assert "echo" in world2.tools
        assert world2.notes["style"] == "prefer tests"
        assert (cwd / "harness.sqlite3").read_bytes().startswith(b"SQLite format 3")
        import sqlite3 as _sqlite3

        with _sqlite3.connect(cwd / "harness.sqlite3") as _db:
            assert _db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
            assert _db.execute("PRAGMA foreign_key_check").fetchall() == []

        # Compatible normalized schemas upgrade in place. A stale process may
        # refuse the newer marker, but it must not force the live transcript
        # through the corrupt-file recovery path.
        with _sqlite3.connect(cwd / "harness.sqlite3") as _db:
            _db.execute("UPDATE schema_migrations SET version = 5")
        upgraded = new_world(cwd, state_path=cwd / "harness.sqlite3")
        assert upgraded.notes["style"] == "prefer tests"
        with _sqlite3.connect(cwd / "harness.sqlite3") as _db:
            from desmos.state.persist import SCHEMA_VERSION

            assert _db.execute("SELECT version FROM schema_migrations").fetchall() == [
                (SCHEMA_VERSION,)
            ]
            assert _db.execute(
                "SELECT COUNT(*) FROM pragma_table_info('channel_cursors')"
            ).fetchone()[0] > 0

        # Legacy SQLite layouts are refused rather than guessed at.
        old_path = cwd / "legacy-layout.sqlite3"
        with _sqlite3.connect(old_path) as old_db:
            old_db.execute(
                "CREATE TABLE sessions(id TEXT PRIMARY KEY, cwd TEXT NOT NULL)"
            )
        try:
            new_world(cwd, state_path=old_path)
        except RuntimeError as exc:
            assert "legacy harness database" in str(exc), exc
        else:
            raise AssertionError("legacy session layout was silently accepted")

        # Corruption is backed up and reported instead of masquerading as empty state.
        corrupt_path = cwd / "corrupt-state.sqlite3"
        corrupt_path.write_bytes(b"not a database")
        import warnings as _warnings

        with _warnings.catch_warnings(record=True) as _seen_warnings:
            _warnings.simplefilter("always")
            corrupt_world = new_world(cwd, state_path=corrupt_path)
        assert any("corrupt harness database" in str(item.message) for item in _seen_warnings)
        assert list(cwd.glob("corrupt-state.sqlite3.corrupt*"))
        assert corrupt_path.read_bytes().startswith(b"SQLite format 3")
        assert corrupt_world.messages == []

        disabled_path = cwd / "disabled.sqlite3"
        new_world(cwd, state_path=disabled_path, persist=False)
        assert not disabled_path.exists()

        # What a Ctrl+C leaves behind: a step is [prompt, assistant, result,
        # assistant ...] and the stop note is a second user message straight
        # after the last result. A turn_aligned that searched *forward* for
        # that two-user pair to find a turn boundary landed on the stop note
        # itself and persisted one message out of 124 -- the session gone, at
        # the exact moment the user interrupted it. Alignment may widen past
        # the flat tail; it may never cut below it, and the head it lands on
        # has to be a user message or Anthropic rejects the next request.
        from desmos.persist import KEEP_MESSAGES, save as _save_world, turn_aligned

        interrupted = []
        for i in range(31):
            interrupted.append({"role": "user", "content": f"prompt {i}"})
            interrupted.append({"role": "assistant", "content": f"<bash>echo {i}</bash>"})
            interrupted.append({"role": "user", "content": f'<result tag="bash">{i}</result>'})
            interrupted.append({"role": "assistant", "content": f"ran {i}"})
        interrupted.append({"role": "user", "content": '<result tag="bash">last</result>'})
        interrupted.append({"role": "user", "content": "[stopped by the user after turn 1]"})
        for shape in (interrupted, interrupted[:-1], interrupted[:-2], interrupted[2:]):
            aligned = turn_aligned(shape)
            assert len(aligned) >= min(len(shape), KEEP_MESSAGES), (
                f"alignment cut below the flat tail: {len(shape)} -> {len(aligned)}"
            )
            assert aligned[0]["role"] == "user", aligned[0]

        stop_path = cwd / "interrupted.sqlite3"
        stopped_world = new_world(cwd, state_path=stop_path)
        stopped_world.messages = list(interrupted)
        _save_world(stopped_world)
        reloaded_stop = new_world(cwd, state_path=stop_path)
        assert len(reloaded_stop.messages) >= KEEP_MESSAGES, (
            f"a save/load round trip kept {len(reloaded_stop.messages)} of {len(interrupted)}"
        )
        assert reloaded_stop.messages == interrupted[-len(reloaded_stop.messages):], (
            "the tail came back reordered or rewritten"
        )
        assert reloaded_stop.messages[0]["role"] == "user", reloaded_stop.messages[0]

        # Orphan-call repair at load (Track 1.2). turn() appends the assistant
        # call before dispatch runs, so a kill mid-syscall saves a transcript
        # whose last call has no output. The typed shape is a hard 400 when
        # replayed; load must pair every dangling call with an interrupted
        # result -- in the transcript itself, so the wire never has to invent
        # an answer per POST and the model reads what happened.
        from desmos.state.persist import INTERRUPTED_CALL
        from desmos.transport.complete import UNANSWERED_CALL, cached_payload

        orphan_path = cwd / "orphan.sqlite3"
        orphan_world = new_world(cwd, state_path=orphan_path)
        orphan_world.messages = [
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "tool_use", "id": "toolu_ok", "name": "syscall",
                     "input": {"input": "<bash>true</bash>"}},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_ok", "content": "fine"}],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "now the slow one"},
                    {"type": "tool_use", "id": "toolu_dangling", "name": "syscall",
                     "input": {"input": "<bash>sleep 999</bash>"}},
                ],
            },
            {"role": "user", "content": "a later prompt the crash never answered"},
        ]
        _save_world(orphan_world)
        healed = new_world(cwd, state_path=orphan_path)
        # An answered call is left alone; the dangling one gains a user
        # message immediately after it (providers demand adjacency), and the
        # message that followed the crash is still there behind the repair.
        assert len(healed.messages) == 6, healed.messages
        assert healed.messages[2]["content"][0]["content"] == "fine", healed.messages[2]
        repair_msg = healed.messages[4]
        assert repair_msg["role"] == "user", healed.messages
        (repair_block,) = repair_msg["content"]
        assert repair_block["type"] == "tool_result", repair_block
        assert repair_block["tool_use_id"] == "toolu_dangling", repair_block
        assert "interrupted" in repair_block["content"], repair_block
        assert healed.messages[5]["content"] == "a later prompt the crash never answered"
        # transport's own pairing accepts the loaded history as-is: the
        # payload carries the load-time repair, not the per-POST
        # UNANSWERED_CALL patch it falls back to for a dangling call.
        # Structural, not a substring scan of json.dumps -- ensure_ascii
        # escaped UNANSWERED_CALL's em dash, so `not in flat` could never
        # fail, repair reverted or not.
        payload = cached_payload("claude-opus-5", "abi\n\ncatalog", healed.messages, 512)
        wire_calls: list[str] = []
        wire_answers: dict[str, object] = {}
        for wire_msg in payload["messages"]:
            for wire_block in wire_msg["content"]:
                if wire_block.get("type") == "tool_use":
                    wire_calls.append(str(wire_block["id"]))
                elif wire_block.get("type") == "tool_result":
                    wire_answers[str(wire_block["tool_use_id"])] = wire_block.get("content")
        assert set(wire_calls) == {"toolu_ok", "toolu_dangling"}, wire_calls
        assert set(wire_calls) <= set(wire_answers), (wire_calls, sorted(wire_answers))
        assert wire_answers["toolu_dangling"] == INTERRUPTED_CALL, wire_answers
        assert UNANSWERED_CALL not in wire_answers.values(), (
            "the wire had to patch a dangling call load should have repaired"
        )

        # Same repair for the Responses shape (custom_tool_call, keyed by
        # call_id): the one that 400s on that wire.
        openai_path = cwd / "orphan-openai.sqlite3"
        openai_world = new_world(cwd, state_path=openai_path)
        openai_world.messages = [
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "content": [
                    {"type": "custom_tool_call", "call_id": "call_9", "name": "syscall",
                     "input": "<bash>sleep 999</bash>"},
                ],
            },
        ]
        _save_world(openai_world)
        healed_oa = new_world(cwd, state_path=openai_path)
        assert len(healed_oa.messages) == 3, healed_oa.messages
        (oa_block,) = healed_oa.messages[2]["content"]
        assert oa_block["type"] == "custom_tool_call_output", oa_block
        assert oa_block["call_id"] == "call_9" and "interrupted" in oa_block["output"], oa_block

        # Prose dialect: a transcript that ends on speech that scans to a
        # syscall got no <result> back. The synthesized block is user-role --
        # the only place a result block may appear. Speech with no tags is a
        # finished step and must not grow one.
        prose_path = cwd / "orphan-prose.sqlite3"
        prose_world = new_world(cwd, state_path=prose_path)
        prose_world.messages = [
            {"role": "user", "content": "run it"},
            {"role": "assistant", "content": "on it\n<bash>echo hi</bash>"},
        ]
        _save_world(prose_world)
        healed_prose = new_world(cwd, state_path=prose_path)
        assert len(healed_prose.messages) == 3, healed_prose.messages
        assert healed_prose.messages[2]["role"] == "user"
        assert '<result tag="bash">' in healed_prose.messages[2]["content"]
        assert "interrupted" in healed_prose.messages[2]["content"]
        prose_world2 = new_world(cwd, state_path=cwd / "orphan-prose-done.sqlite3")
        prose_world2.messages = [
            {"role": "user", "content": "run it"},
            {"role": "assistant", "content": "all done, nothing left to run"},
        ]
        _save_world(prose_world2)
        finished = new_world(cwd, state_path=cwd / "orphan-prose-done.sqlite3")
        assert len(finished.messages) == 2, finished.messages

        w3 = new_world(cwd, state_path=cwd / "harness2.json", ns={"doc": "hello world"})

        evolve(w3, "after ping")
        assert (gen_dir(w3) / "0001.json").is_file()
        dispatch(w3, Block("knowledge", "usage line", {"op": "system"}))
        assert w3.notes["note"] == "usage line"
        rollback(w3, 1)
        assert "note" not in w3.notes

        _check_prices()
        _check_call_ledger(cwd)
        _check_session_lineage(cwd)
        _check_session_channel(cwd)
        _check_quarantine_manifest()
        _check_prune_manifest()


#: The one fixture both languages price. `crates/desmos-tui/src/main.rs`
#: (mod price_table_tests) bills the same usage through CacheMeter and asserts
#: this same constant, so a rate changed on one side fails on the other.
FIXTURE_USAGE = {
    "input_tokens": 100,
    "cache_read_input_tokens": 1000,
    "cache_creation_input_tokens": 10,
    "output_tokens": 50,
}
FIXTURE_COST_OPUS = 0.0023125


def _check_quarantine_manifest() -> None:
    """A replaced database must leave an account of itself, loudly.

    Recovery renames the corrupt file and creates a fresh one. That is correct
    -- refusing to start would be worse -- but until now the only trace was a
    RuntimeWarning, and this workspace raised 98 of them in a 32-minute window
    without anyone noticing that every session before it had stopped being
    reachable. The manifest is the account; the notice is the volume.

    Reverting either fix fails this: drop `_record_quarantine` and there is no
    entry, drop `_report_quarantines` and wake says nothing.
    """
    import json
    import tempfile
    import warnings

    from desmos.state import persist

    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)
    world.messages.append({"role": "user", "content": "written before the corruption"})
    persist.save(world)
    path = persist.state_file(world)
    assert path.is_file(), path

    # How a killed writer leaves it: the header no longer says SQLite.
    raw = bytearray(path.read_bytes())
    raw[0:16] = b"not-a-database\x00\x00"
    path.write_bytes(bytes(raw))

    persist._QUARANTINE_REPORTED.discard(str(path))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        persist._open(path).close()

    entries = persist.quarantines(path)
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["bytes"] > 0, entry
    assert entry["reason"], entry
    assert entry["moved"], entry
    assert "inventory" in entry, entry

    summary = persist.quarantine_summary(path)
    assert "not absent" in summary, summary
    assert "1 database(s) replaced" in summary, summary

    # Wake reports it on the ordinary notice route, not only as a warning a
    # front never renders.
    persist._QUARANTINE_REPORTED.discard(str(path))
    fresh = new_world(root, persist=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        persist.load(fresh)
    events = persist.read_events(fresh, limit=500)
    assert any("quarantined" in json.dumps(ev, default=str) for ev in events), (
        f"wake did not report the quarantine: {len(events)} events"
    )

    # Once per process: a second load is not new information.
    before = len(persist.read_events(fresh, limit=500))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        persist.load(fresh)
    assert len(persist.read_events(fresh, limit=500)) == before, "quarantine notice repeated"


def _check_prune_manifest() -> None:
    """Bounding the database must not be a silent delete.

    `_prune_sessions` keeps the newest SESSION_KEEP sessions and drops the
    rest. Foreign keys cascade, so a dropped session takes its messages, prior
    turns, events and calls with it -- and nothing recorded which conversations
    were spent. This drives the real entry point: `save()` prunes, so the
    manifest must appear without the test ever calling the pruner itself.

    Revert the `_record_prune` call and the manifest is empty; revert the
    `path=` argument at the call site and the census never runs.
    """
    import tempfile

    from desmos.state import persist

    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)
    world.messages.append({"role": "user", "content": "the surviving session"})
    persist.save(world)
    path = persist.state_file(world)
    assert not persist.pruned(path), "nothing was pruned yet"

    conn = persist._open(path)
    try:
        workspace = conn.execute("SELECT id FROM workspaces").fetchone()[0]
        extra = persist.SESSION_KEEP + 6
        with conn:
            for i in range(extra):
                sid = f"doomed-{i:04d}"
                conn.execute(
                    "INSERT INTO sessions(id, workspace_id, kind, started_at,"
                    " last_seen_at, cache_key) VALUES (?, ?, 'attach', ?, ?, ?)",
                    (sid, workspace, f"2020-01-01T00:00:{i:02d}",
                     f"2020-01-01T00:00:{i:02d}", sid),
                )
                conn.execute(
                    "INSERT INTO messages(session_id, seq, role, content_json)"
                    " VALUES (?, 0, 'user', ?)",
                    (sid, json.dumps(f"doomed conversation {i}")),
                )
    finally:
        conn.close()

    persist.save(world)

    entries = persist.pruned(path)
    # SESSION_KEEP + 6 fakes + this session = SESSION_KEEP + 7 rows.
    expected = 7
    assert len(entries) == expected, f"{len(entries)} pruned entries, want {expected}"
    for entry in entries:
        assert entry["session_id"].startswith("doomed-"), entry
        assert entry["messages"] == 1, entry
        assert entry["bytes"] > 0, entry
        assert entry["at"], entry
        assert "doomed conversation" in entry["opened_with"], entry

    # The account describes something that really is gone.
    conn = persist._open(path)
    try:
        gone = [e["session_id"] for e in entries]
        marks = ",".join("?" * len(gone))
        left = conn.execute(
            f"SELECT count(*) FROM sessions WHERE id IN ({marks})", gone
        ).fetchone()[0]
        assert left == 0, f"{left} pruned sessions still present"
        left = conn.execute(
            f"SELECT count(*) FROM messages WHERE session_id IN ({marks})", gone
        ).fetchone()[0]
        assert left == 0, f"{left} messages survived their pruned session"
    finally:
        conn.close()


def _check_prices() -> None:
    from desmos.kernel import prices

    assert prices.rates("claude-opus-5") == (5.0, 25.0)
    assert prices.rates("gpt-5.6-sol") == (1.25, 10.0)
    # An unpriced model bills at the default. A silent zero reads as free.
    assert prices.rates("mystery-9") == prices.rates("claude-opus-5")
    got = prices.cost(FIXTURE_USAGE, "claude-opus-5")
    assert abs(got - FIXTURE_COST_OPUS) < 1e-12, got
    # Sonnet is the rate the old usage tag hardcoded for every model; the gap
    # between these two is exactly the error that motivated the shared table.
    assert prices.cost(FIXTURE_USAGE, "claude-sonnet-4-6") < got
    # The 1h cache tier is a premium over the 5m one, not the same write.
    hour = dict(FIXTURE_USAGE, cache_creation={"ephemeral_1h_input_tokens": 10})
    assert prices.cost(hour, "claude-opus-5") > got

    # The TUI must read this file, not a copy of it. A literal rate table in
    # main.rs is the drift this check exists to catch.
    main_rs = Path(__file__).resolve().parents[2] / "crates" / "desmos-tui" / "src" / "main.rs"
    if main_rs.is_file():
        text = main_rs.read_text()
        assert "include_str!(\"../../../desmos/kernel/prices.json\")" in text, (
            "the TUI stopped reading desmos/kernel/prices.json"
        )
        assert 'm if m.starts_with("claude-opus")' not in text, (
            "main.rs grew a second hardcoded price table"
        )


def _check_session_lineage(cwd: Path) -> None:
    """A restart creates a child session without copying message ownership."""
    import os
    import sqlite3

    from desmos.state import persist

    path = cwd / "lineage.sqlite3"
    prior_env = os.environ.get(persist.SESSION_ID_ENV)
    try:
        first_id = "019100000000aaaaaaaaaaaaaaaaaaaa"
        second_id = "019200000000bbbbbbbbbbbbbbbbbbbb"
        os.environ[persist.SESSION_ID_ENV] = first_id
        first = new_world(cwd, state_path=path)
        first.messages.append({"role": "user", "content": "from first"})
        first.prior.append({"prompt": "p1", "speech": "s1"})
        persist.save(first)

        os.environ[persist.SESSION_ID_ENV] = second_id
        second = new_world(cwd, state_path=path)
        assert second.messages == [{"role": "user", "content": "from first"}]
        assert second.session_message_start == 1
        assert second.session_prior_start == 1
        second.messages.append({"role": "assistant", "content": "from second"})
        second.prior.append({"prompt": "p2", "speech": "s2"})
        persist.save(second)

        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            sessions = db.execute(
                "SELECT id, parent_id, kind, cache_key FROM sessions"
                " ORDER BY started_at, id"
            ).fetchall()
            assert [row["id"] for row in sessions] == [first_id, second_id], sessions
            assert sessions[1]["parent_id"] == first_id, sessions[1]
            assert sessions[1]["kind"] == "resume", sessions[1]
            assert sessions[1]["cache_key"] == f"desmos-{second_id[:16]}"
            ownership = db.execute(
                "SELECT session_id, COUNT(*) n FROM messages"
                " GROUP BY session_id ORDER BY session_id"
            ).fetchall()
            assert [(row["session_id"], row["n"]) for row in ownership] == [
                (first_id, 1), (second_id, 1)
            ], ownership
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []

        # Opaque provider content and giant request/response bodies never land.
        secret = "ciphertext" * 10_000
        persist.record_event(
            second,
            {"ev": "complete", "encrypted_content": secret, "body": secret},
            ts_ms=1,
            mono_ns=2,
        )
        assert persist.record_event(
            second, {"ev": "timing", "phase": "delta"}, ts_ms=2, mono_ns=3
        ) == 0
        events = persist.read_events(second)
        assert len(events) == 1 and events[0]["elided"] is True, events
        assert secret not in str(events), events[0]
        assert events[0]["payload_bytes"] > 100_000, events[0]
    finally:
        if prior_env is None:
            os.environ.pop(persist.SESSION_ID_ENV, None)
        else:
            os.environ[persist.SESSION_ID_ENV] = prior_env


def _check_call_ledger(cwd: Path) -> None:
    """Per-call usage is durable, keyed by a run id, and priced on the way in."""
    from desmos.state import persist

    world = new_world(cwd, state_path=cwd / "ledger.sqlite3")
    world.model = "claude-opus-5"
    assert persist.runs(world) == []

    persist.record_call(world, {"ts": "2026-01-01T00:00:00+00:00", "usage": FIXTURE_USAGE})
    persist.record_call(world, {"ts": "2026-01-01T00:01:00+00:00", "usage": FIXTURE_USAGE})
    # A call that reported nothing is not a call. Rows here are money.
    persist.record_call(world, {"ts": "2026-01-01T00:02:00+00:00", "usage": {}})

    rows = persist.runs(world)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["calls"] == 2, row
    assert row["run_id"] == persist.run_id()
    assert row["models"] == "claude-opus-5"
    assert abs(row["cost"] - 2 * FIXTURE_COST_OPUS) < 1e-12, row

    # The point of the table: it outlives the process that wrote it.
    reopened = new_world(cwd, state_path=cwd / "ledger.sqlite3")
    assert persist.runs(reopened)[0]["calls"] == 2

    # A child world (persist off) spends against its own transcript and must
    # never write rows into the parent's ledger.
    child = new_world(cwd, state_path=None)
    child.model = "gpt-5.6-luna"
    persist.record_call(child, {"usage": FIXTURE_USAGE})
    assert persist.runs(world)[0]["calls"] == 2


def _check_session_channel(cwd: Path) -> None:
    """Canonical session ops expose live peers and an ordered local channel."""
    import fcntl
    import json
    import sqlite3

    from desmos.state import persist

    path = cwd / "channel.sqlite3"
    world = new_world(cwd, state_path=path)
    peer_id = "peer-run"
    lease_path = persist._presence_path(world, peer_id)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    peer_lease = lease_path.open("a+")
    fcntl.flock(peer_lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with sqlite3.connect(path) as db:
            workspace = db.execute("SELECT id FROM workspaces").fetchone()[0]
            parent = db.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
            db.execute(
                """
                INSERT INTO sessions(
                    id, workspace_id, parent_id, kind, started_at,
                    last_seen_at, model, thinking, cache_key)
                VALUES (?, ?, ?, 'fork', ?, ?, ?, '', ?)
                """,
                (
                    peer_id, workspace, parent, "2026-01-01",
                    "2026-01-01", "peer-model", "desmos-peer",
                ),
            )
            db.execute(
                """
                INSERT INTO active_runs(
                    run_id, workspace_id, session_id, pid, cwd, generation,
                    model, started_at, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    peer_id, workspace, peer_id, 99999, str(cwd), 1,
                    "peer-model", "2026-01-01", "2026-01-01",
                ),
            )

        peers = json.loads(dispatch(world, Block("session", "", {"op": "peers"})))
        assert {row["run_id"] for row in peers} == {persist.run_id(), peer_id}, peers

        posted = json.loads(
            dispatch(
                world,
                Block(
                    "session",
                    "I am editing persist.py; please avoid it.",
                    {"op": "post", "author": "worker-a"},
                ),
            )
        )
        assert posted["channel"] == "conflicts" and posted["id"] > 0, posted
        directed = json.loads(
            dispatch(
                world,
                Block(
                    "session",
                    "Can you inspect the overlap?",
                    {"op": "post", "session_id": peer_id},
                ),
            )
        )
        assert directed["to"] == peer_id and directed["kind"] == "request", directed
        assert directed["channel"] == persist.peer_channel(peer_id, "request"), directed
        missing = dispatch(
            world,
            Block("session", "hello?", {"op": "post", "to": "not-a-live-run"}),
        )
        assert "not active" in missing, missing
        assert json.loads(
            dispatch(world, Block("session", "", {"op": "inbox"}))
        )["unread"] == 0, "a run must not notify itself"

        with sqlite3.connect(path) as db:
            db.execute(
                """
                INSERT INTO channel_messages(
                    workspace_id, session_id, channel, run_id,
                    author, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace, peer_id, "conflicts", peer_id,
                    "worker-b", "I also need persist.py.", "2026-01-02",
                ),
            )
        inbox = json.loads(dispatch(world, Block("session", "", {"op": "inbox"})))
        assert inbox["unread"] == 1 and inbox["messages"][0]["author"] == "worker-b", inbox
        from desmos.kernel.catalog import volatile
        assert "IRC #conflicts: 1 unread from worker-b" in volatile(world)

        messages = json.loads(
            dispatch(
                world,
                Block("session", "", {"op": "read", "since": str(posted["id"])}),
            )
        )
        assert [row["body"] for row in messages] == ["I also need persist.py."], messages
        assert json.loads(
            dispatch(world, Block("session", "", {"op": "inbox"}))
        )["unread"] == 0, "read must advance the unread cursor"

        with sqlite3.connect(path) as db:
            db.execute(
                """
                INSERT INTO channel_messages(
                    workspace_id, session_id, channel, run_id,
                    author, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace, peer_id, "conflicts", peer_id,
                    "worker-b", "Resolved.", "2026-01-03",
                ),
            )
        assert json.loads(
            dispatch(world, Block("session", "", {"op": "inbox"}))
        )["unread"] == 1
        dispatch(world, Block("session", "", {"op": "dismiss"}))
        assert json.loads(
            dispatch(world, Block("session", "", {"op": "inbox"}))
        )["unread"] == 0
    finally:
        fcntl.flock(peer_lease.fileno(), fcntl.LOCK_UN)
        peer_lease.close()

    peers = json.loads(dispatch(world, Block("session", "", {"op": "peers"})))
    assert [row["run_id"] for row in peers] == [persist.run_id()], peers
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM active_runs WHERE run_id = ?", (peer_id,)
        ).fetchone()[0] == 0
