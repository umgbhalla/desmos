"""State checks: persist, memory, generations, skills."""

from __future__ import annotations

from pathlib import Path

from desmos.dispatch import dispatch
from desmos.generations import evolve, gen_dir, rollback
from desmos.loop import new_world
from desmos.catalog import system_prompt
from desmos.types import Block


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
        memory_world = new_world(memory_dir, state_path=memory_dir / "harness.json")
        memory_prompt = system_prompt(memory_world)
        assert memory_world.tools["memory"].frozen
        assert "Umang prefers actual tools before narration" in memory_prompt
        assert "x" * 500 not in memory_prompt
        assert (memory_dir / "memories" / "legacy_MEMORY.md").read_text(encoding="utf-8") == legacy
        assert (memory_dir / "memories" / "records.jsonl").is_file()
        assert (memory_dir / "memory_summary.md").is_file()

        remembered = dispatch(
            memory_world,
            Block(
                "memory",
                "Umang's name is Umang.",
                {"id": "user.umang.identity", "scope": "user", "kind": "identity"},
            ),
        )
        updated = dispatch(
            memory_world,
            Block(
                "memory",
                "Umang's name is Umang.",
                {"id": "user.umang.identity", "scope": "user", "kind": "identity"},
            ),
        )
        search_result = dispatch(memory_world, Block("memory", "search Umang identity", {}))
        # Writing the same id twice updates the record instead of adding a
        # second one -- searching would otherwise return both and the model
        # would read two versions of the same fact.
        assert search_result.count("user.umang.identity") == 1, search_result
        read_result = dispatch(memory_world, Block("memory", "read user.umang.identity", {}))
        assert '"scope": "user"' in read_result
        dispatch(memory_world, Block("memory", "verify user.umang.identity", {}))

        secret_result = dispatch(
            memory_world,
            Block(
                "memory",
                "api_key=abcdefghijk123456789",
                {"id": "repo.secret-test", "scope": "repo", "kind": "test"},
            ),
        )
        secret_read = dispatch(memory_world, Block("memory", "read repo.secret-test", {}))
        assert "[REDACTED_SECRET]" in secret_read
        assert "abcdefghijk123456789" not in secret_read

        memory_world2 = new_world(memory_dir, state_path=memory_dir / "harness.json")
        assert memory_world2.tools["memory"].frozen
        assert "Umang's name is Umang" in system_prompt(memory_world2)
        dispatch(memory_world2, Block("memory", "forget user.umang.identity", {}))
        gone = dispatch(memory_world2, Block("memory", "search user.umang.identity", {}))
        assert "user.umang.identity" not in gone, gone
        dispatch(memory_world2, Block("memory", "consolidate", {}))

        ping = cwd / ".desmos" / "skills" / "ping"
        ping.mkdir(parents=True)
        (ping / "SKILL.md").write_text(
            "---\nname: ping\ndescription: reply pong\n---\n# ping\nbody\n",
            encoding="utf-8",
        )
        (ping / "skill.py").write_text("def handle(body, **a):\n    return 'pong:' + body\n", encoding="utf-8")
        world = new_world(cwd, state_path=cwd / "harness.json")
        assert dispatch(world, Block("skill", "", {"name": "ping"})).endswith("body\n")
        assert dispatch(world, Block("ping", "hi", {})) == "pong:hi"

        grown = cwd / ".desmos" / "skills" / "later"
        grown.mkdir(parents=True)
        (grown / "SKILL.md").write_text(
            "---\nname: later\ndescription: appeared after start\n---\n# later\nok\n",
            encoding="utf-8",
        )
        assert not any(s.name == "later" for s in world.skills)
        dispatch(world, Block("reload", "", {}))
        assert any(s.name == "later" for s in world.skills)
        assert dispatch(world, Block("skill", "", {"name": "later"})).endswith("ok\n")

        out = dispatch(
            world,
            Block("register", "def handle(body, **a):\n    return body.upper()\n", {"name": "echo", "doc": "uppercase"}),
        )
        assert dispatch(world, Block("echo", "hi", {})) == "HI"

        dispatch(world, Block("system", "prefer tests", {"name": "style"}))
        assert "prefer tests" in system_prompt(world)

        world2 = new_world(cwd, state_path=cwd / "harness.json")
        assert "echo" in world2.tools
        assert world2.notes["style"] == "prefer tests"
        assert (cwd / "harness.json").read_bytes().startswith(b"SQLite format 3")
        import sqlite3 as _sqlite3

        with _sqlite3.connect(cwd / "harness.json") as _db:
            assert _db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
            assert _db.execute("PRAGMA foreign_key_check").fetchall() == []

        # A legacy snapshot imports exactly once and remains available as a backup.
        import json as _json
        legacy_path = cwd / "legacy-state.json"
        legacy_path.write_text(
            _json.dumps(
                {
                    "notes": {"legacy": "kept"},
                    "tools": {},
                    "docs": {},
                    "prior": [{"prompt": "old", "speech": "answer"}],
                    "generation": 3,
                    "gen_reason": "legacy import",
                    "thinking": "high",
                    "messages": [{"role": "user", "content": "before sqlite"}],
                }
            ),
            encoding="utf-8",
        )
        legacy_world = new_world(cwd, state_path=legacy_path)
        assert legacy_world.notes["legacy"] == "kept"
        assert legacy_world.messages == [{"role": "user", "content": "before sqlite"}]
        assert legacy_world.generation == 3
        assert legacy_path.read_bytes().startswith(b"SQLite format 3")
        assert (cwd / "legacy-state.json.migrated").is_file()
        legacy_again = new_world(cwd, state_path=legacy_path)
        assert legacy_again.notes["legacy"] == "kept"

        # The production default migrates .desmos/harness.json to harness.sqlite3.
        default_root = cwd / "default-migration"
        default_legacy = default_root / ".desmos" / "harness.json"
        default_legacy.parent.mkdir(parents=True)
        default_legacy.write_text(_json.dumps({"notes": {"default": "imported"}}), encoding="utf-8")
        default_world = new_world(default_root)
        assert default_world.notes["default"] == "imported"
        assert (default_root / ".desmos" / "harness.sqlite3").is_file()
        assert (default_root / ".desmos" / "harness.json.migrated").is_file()

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
        dispatch(w3, Block("system", "usage line", {}))
        assert w3.notes["note"] == "usage line"
        rollback(w3, 1)
        assert "note" not in w3.notes
