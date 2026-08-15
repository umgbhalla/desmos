"""Agent checks: subagent isolation, depth, spawn events, pending resume."""

from __future__ import annotations

from pathlib import Path

from desmos.dispatch import dispatch
from desmos.loop import bind_step, new_world
from desmos.types import Block


def check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        world = new_world(cwd, state_path=cwd / "harness.json")

        from desmos.agents import subagent as reload_subagent

        reload_events = []
        def reload_emit(ev):
            reload_events.append(ev)

        reload_subagent.bind(world)
        reload_subagent.set_emitter(reload_emit)
        dispatch(world, Block("reload_sdk", "", {}))
        assert "reload_sdk" in world.tools
        assert reload_subagent.PARENT is world, "SDK reload orphaned later subagent spawns"
        assert reload_subagent._EMIT is reload_emit, "SDK reload dropped child event routing"

        from desmos.persist import save as save_world
        from desmos.agents.subagent import _child_world, resolve, wait

        parent = new_world(cwd, state_path=cwd / "harness-iso.json")
        dispatch(
            parent,
            Block("register", "def handle(body, **a):\n    return 'SECRET'\n", {"name": "secret", "doc": "parent only"}),
        )
        child = _child_world(resolve("explore"), parent)
        assert child.persist is False
        assert "secret" not in child.tools
        assert "agents" not in child.tools
        # Pruning w.tools is not the scope. dispatch answers the frozen tags
        # without consulting w.tools at all, so a read-capability child could
        # rewrite any file on disk while its tool table looked correctly
        # empty -- the absence the old check asserted was true the whole time
        # the containment was false. Assert the file, not the table.
        scoped_file = cwd / "scoped.txt"
        scoped_file.write_text("alpha\n", encoding="utf-8")
        denied = dispatch(child, Block("edit", "alpha\n---\nBETA", {"path": str(scoped_file)}))
        assert scoped_file.read_text(encoding="utf-8") == "alpha\n", (
            f"a read-capability child edited a file on disk: {denied!r}"
        )
        from desmos.dispatch import scope_of

        assert "edit" not in (scope_of(child) or ()), scope_of(child)
        # The scope cannot live on the World. bind_step -- which the loop runs
        # before the child's first turn -- publishes the World into the very ns
        # its <python> executes in, and <python> is a capability every persona
        # has. As an attribute, one assignment from the child turned off the
        # gate that assignment was under, and the <edit> refused a line earlier
        # went through.
        bind_step(child)
        assert child.ns.get("world") is child
        assert dispatch(child, Block("python", "world.allowed_tags = None", {})) == "ok"
        assert "edit" not in (scope_of(child) or ()), scope_of(child)
        disarmed = dispatch(child, Block("edit", "alpha\n---\nBETA", {"path": str(scoped_file)}))
        assert scoped_file.read_text(encoding="utf-8") == "alpha\n", (
            f"a child disarmed its own scope from <python>: {disarmed!r}"
        )
        # Same gate, harness-level tags: a note written here would outlive the
        # child in the prompt the parent's own turn reads back.
        dispatch(child, Block("system", "pwn", {"name": "pwn-note"}))
        assert "pwn-note" not in child.notes
        for tag in ("evolve", "rollback", "register", "reload_sdk"):
            assert "outside this agent's scope" in dispatch(child, Block(tag, "x", {})), tag
        # And the capability it does have still works, or the "scope" is just
        # a broken child.
        assert dispatch(child, Block("bash", "echo alive", {})).strip() == "alive"

        child.notes["pwn"] = "from-child"
        save_world(child)
        reloaded_parent = new_world(cwd, state_path=cwd / "harness-iso.json")
        assert "pwn" not in reloaded_parent.notes

        # persist=False means nothing on disk, not just no harness.json.
        # generations and memory each own their own writer, and each one used
        # to reach the filesystem from a child that has no state file: a
        # subagent snapshotted into the parent's .desmos and a child's
        # <memory remember> edited the repo's durable MEMORY.md.
        quiet_cwd = cwd / "no-writes"
        quiet_cwd.mkdir()
        quiet = new_world(quiet_cwd, state_path=None, ns={}, persist=False)
        from desmos.loop import install_resources as _install, seed_builtins as _seed

        _seed(quiet)
        _install(quiet)
        dispatch(quiet, Block("evolve", "probe", {}))
        dispatch(quiet, Block("memory", "remember user.pwn.identity leaked", {}))
        dispatch(quiet, Block("system", "in-memory only", {"name": "n"}))
        save_world(quiet)
        assert quiet.notes["n"] == "in-memory only", "the child lost its own note"
        strays = sorted(str(p.relative_to(quiet_cwd)) for p in quiet_cwd.rglob("*"))
        assert strays == [], f"a non-persistent world wrote to disk: {strays}"

        unknown = wait("nope")
        assert unknown[0]["state"] == "unknown"
        import desmos.agents.subagent as S

        S._DEPTH.n = 1
        try:
            try:
                S.spawn("should fail")
            except ValueError as exc:
                assert "depth" in str(exc)
            else:
                raise AssertionError("child spawn should be blocked")
        finally:
            S._DEPTH.n = 0

        evs_spawn: list[dict] = []
        S.set_emitter(evs_spawn.append)
        parent_sp = new_world(cwd, state_path=cwd / "harness-spawn.json")

        def spawn_complete(_model, _system, _messages, _max_tokens):
            return {"content": [{"type": "text", "text": "child said ok"}], "usage": {}}

        parent_sp.complete_fn = spawn_complete
        S.bind(parent_sp)
        rid = S.spawn("reply with ok", agent="explore", parent=parent_sp)
        briefs = S.wait(rid, timeout=15.0)
        assert briefs and briefs[0]["state"] == "done", briefs
        phases = [e.get("phase") for e in evs_spawn if e.get("ev") == "subagent"]
        assert phases and phases[0] == "started", evs_spawn
        assert "done" in phases, evs_spawn
        kids = [e for e in evs_spawn if e.get("ev") == "child"]
        assert any(e.get("kind") == "speech" for e in kids), kids
        assert not any(
            "opaque-secret" in str(e) for e in evs_spawn
        )
        S.set_emitter(None)

        # --- the tree on the wire: a spawn inside a spawn records parent/depth.
        # Three levels through the real pool with scripted complete_fns: the
        # root spawns level-a; level-a's speech spawns level-b from a fresh
        # thread (the code's documented route past the thread-local depth cap,
        # whose semantics stay untouched until Phase 4's 2.1) handing spawn()
        # its own world — _execute tags each child world with the run it
        # executes; level-b does the same for level-c. The grandchild must
        # record the child as its parent at depth 2 in RUNS, in the persisted
        # record, and on every emitted subagent/child event.
        import json
        import os
        import time

        nest = (
            "<python>\n"
            "import threading\n"
            "import desmos.agents.subagent as S\n"
            "t = threading.Thread(target=lambda: S.spawn({task!r}, "
            'agent="explore", model="claude-opus-5", parent=world, '
            "_register_pending=False))\n"
            "t.start(); t.join()\n"
            'print("spawned")\n'
            "</python>"
        )

        def tree_complete(_model, _system, messages, _max_tokens):
            if any(m.get("role") == "assistant" for m in messages):
                text = "settled"  # the turn after a syscall result
            elif "level-a" in json.dumps(messages):
                text = "spawning b\n" + nest.format(task="level-b nest")
            elif "level-b" in json.dumps(messages):
                text = "spawning c\n" + nest.format(task="level-c leaf")
            else:
                text = "leaf ok"
            return {"content": [{"type": "text", "text": text}], "usage": {}}

        evs_tree: list[dict] = []
        S.set_emitter(evs_tree.append)
        tree_root = new_world(cwd, state_path=None, persist=False)
        tree_root.complete_fn = tree_complete
        prev_dir = Path.cwd()
        os.chdir(cwd)  # S.DIR is relative: keep .desmos/subagents in the tmp
        try:
            aid = S.spawn(
                "level-a nest",
                agent="explore",
                model="claude-opus-5",
                parent=tree_root,
                _register_pending=False,
            )

            def by_task(task: str):
                return next((r for r in S.RUNS.values() if r.task == task), None)

            deadline = time.time() + 30.0
            while time.time() < deadline:
                grand = by_task("level-c leaf")
                if grand is not None and grand.state not in ("pending", "running"):
                    break
                time.sleep(0.05)
            a, b, c = S.RUNS[aid], by_task("level-b nest"), by_task("level-c leaf")
            assert b is not None and c is not None, sorted(r.task for r in S.RUNS.values())
            S.wait(aid, b.id, c.id, timeout=30.0)
            assert (a.state, b.state, c.state) == ("done",) * 3, (
                a.brief(), b.brief(), c.brief(),
            )
            assert a.parent is None and a.depth == 0, (a.parent, a.depth)
            assert b.parent == aid and b.depth == 1, (b.parent, b.depth, aid)
            assert c.parent == b.id and c.depth == 2, (c.parent, c.depth, b.id)
            # Both emitters carry the tree: every subagent phase and every
            # child envelope for the grandchild names the child at depth 2.
            grand_sub = [
                e for e in evs_tree if e.get("ev") == "subagent" and e.get("id") == c.id
            ]
            grand_child = [
                e for e in evs_tree if e.get("ev") == "child" and e.get("id") == c.id
            ]
            assert grand_sub and grand_child, "the grandchild never reached the wire"
            for e in grand_sub + grand_child:
                assert e["parent"] == b.id and e["depth"] == 2, e
            root_started = next(
                e
                for e in evs_tree
                if e.get("ev") == "subagent" and e.get("phase") == "started" and e.get("id") == aid
            )
            assert root_started["parent"] is None and root_started["depth"] == 0, root_started
            # Late-attach reconstruction reads the persisted record, not RUNS.
            rec = json.loads((S.DIR / f"{c.id}.json").read_text(encoding="utf-8"))
            assert rec["parent"] == b.id and rec["depth"] == 2, rec
        finally:
            os.chdir(prev_dir)
            S.set_emitter(None)

        from desmos.checks import pending_check, subagent_check

        subagent_check.self_check()
        subagent_check.parallel_tool_check()
        pending_check.self_check()
