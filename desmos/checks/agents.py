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
        # The durable pending handoff is the root world's alone: a child's
        # background task must stay in memory, or its notice file would be
        # replayed into a session that no longer exists. The rglob below
        # catches a .desmos/pending stray like any other write.
        from desmos.agents import pending as _pend

        _quiet_task = _pend.submit(quiet, "quiet-task", lambda: "in memory only")
        assert _quiet_task.done.wait(10), "the quiet task never landed"
        assert _quiet_task.path is None, _quiet_task.path
        assert _pend.wait_next(quiet), "the quiet task was never delivered"
        save_world(quiet)
        assert quiet.notes["n"] == "in-memory only", "the child lost its own note"
        strays = sorted(str(p.relative_to(quiet_cwd)) for p in quiet_cwd.rglob("*"))
        assert strays == [], f"a non-persistent world wrote to disk: {strays}"

        unknown = wait("nope")
        assert unknown[0]["state"] == "unknown"
        import desmos.agents.subagent as S

        # --- depth budget is data on the run, not a thread-local. A budget-0
        # child has no <agents> in its dispatch scope; a budgeted orchestrator
        # does. The orchestrator capability holds no execution tags at all --
        # proven on dispatch refusals and on disk, not on the tool table.
        orc0 = _child_world(resolve("orchestrator"), parent, budget=0)
        assert "agents" not in (scope_of(orc0) or ()), scope_of(orc0)
        assert "agents" not in orc0.tools
        orc1 = _child_world(resolve("orchestrator"), parent, budget=1)
        assert "agents" in (scope_of(orc1) or ()), scope_of(orc1)
        # Scope without a tag is a promise the child cannot keep: the root's
        # <agents> is a grown tool a persist=False child never loads, so the
        # budgeted world must carry the real handler itself or its own prompt
        # teaches a syscall that answers unknown-tag.
        orc_status = dispatch(orc1, Block("agents", "wait", {}))
        assert "unknown tag" not in orc_status, orc_status
        probe = cwd / "orc-probe.txt"
        for tag, body in (
            ("bash", f"echo pwn > {probe}"),
            ("shell", f"echo pwn > {probe}"),
            ("python", f"open({str(probe)!r}, 'w').write('pwn')"),
            ("edit", "a\n---\nb"),
        ):
            out = dispatch(orc1, Block(tag, body, {"path": str(probe)} if tag == "edit" else {}))
            assert "outside this agent's scope" in out, (tag, out)
        assert not probe.exists(), "an orchestrator wrote to disk"

        import os

        evs_spawn: list[dict] = []
        S.set_emitter(evs_spawn.append)
        parent_sp = new_world(cwd, state_path=cwd / "harness-spawn.json")

        def spawn_complete(_model, _system, _messages, _max_tokens):
            return {"content": [{"type": "text", "text": "child said ok"}], "usage": {}}

        parent_sp.complete_fn = spawn_complete
        S.bind(parent_sp)
        # S.DIR is relative: chdir keeps every run record in the tmp .desmos,
        # not the repo's, for this spawn and everything below.
        prev_dir = Path.cwd()
        os.chdir(cwd)
        rid = S.spawn("reply with ok", agent="explore", parent=parent_sp)
        briefs = S.wait(rid, timeout=15.0)
        assert briefs and briefs[0]["state"] == "done", briefs
        phases = [e.get("phase") for e in evs_spawn if e.get("ev") == "subagent"]
        assert phases and phases[0] == "started", evs_spawn
        assert "done" in phases, evs_spawn
        # 4.3 generation lineage: the started event carries the parent world's
        # generation at spawn time.
        started_ev = next(e for e in evs_spawn if e.get("phase") == "started")
        assert started_ev.get("generation") == int(getattr(parent_sp, "generation", 0) or 0), started_ev
        kids = [e for e in evs_spawn if e.get("ev") == "child"]
        assert any(e.get("kind") == "speech" for e in kids), kids
        assert not any(
            "opaque-secret" in str(e) for e in evs_spawn
        )
        S.set_emitter(None)

        # --- the tree on the wire: a spawn inside a spawn records parent/depth,
        # and the depth budget rides the run. Three levels through the real pool
        # with scripted complete_fns, on the UNCOOPERATIVE path: no level hands
        # spawn() a parent kwarg — the gate must discover the calling run from
        # the world dispatch() bound around the executing <python>, because a
        # kwarg-keyed gate is one a bare spawn walks around (that fork bomb
        # shipped). The root spawns level-a with budget 2; level-a's bare
        # <python> spawn makes level-b (budget inherits to 1); level-b makes
        # level-c (budget 0). level-c then tries a bare spawn AND a spawn from
        # a detached thread it starts itself: both must come back as refusal
        # strings in its own transcript -- results the child reads, never
        # exceptions -- and neither level-d may exist. The grandchild must
        # record the child as its parent at depth 2 in RUNS, in the persisted
        # record, and on every emitted subagent/child event.
        import json
        import time

        nest = (
            "<python>\n"
            "import desmos.agents.subagent as S\n"
            'print(S.spawn({task!r}, agent="explore", model="claude-opus-5", '
            "_register_pending=False))\n"
            "</python>"
        )
        overfork = (
            "<python>\n"
            "import threading\n"
            "import desmos.agents.subagent as S\n"
            'print(S.spawn("level-d overflow", agent="explore", _register_pending=False))\n'
            "out = []\n"
            't = threading.Thread(target=lambda: out.append(S.spawn("thread-d overflow", '
            'agent="explore", _register_pending=False)))\n'
            "t.start(); t.join()\n"
            "print(out[0])\n"
            "</python>"
        )

        def tree_complete(_model, _system, messages, _max_tokens):
            if any(m.get("role") == "assistant" for m in messages):
                text = "settled"  # the turn after a syscall result
            elif "level-a" in json.dumps(messages):
                text = "spawning b\n" + nest.format(task="level-b nest")
            elif "level-b" in json.dumps(messages):
                text = "spawning c\n" + nest.format(task="level-c leaf")
            elif "level-c" in json.dumps(messages):
                text = "overforking\n" + overfork
            else:
                text = "leaf ok"
            return {"content": [{"type": "text", "text": text}], "usage": {}}

        evs_tree: list[dict] = []
        S.set_emitter(evs_tree.append)
        tree_root = new_world(cwd, state_path=None, persist=False)
        tree_root.complete_fn = tree_complete
        try:
            aid = S.spawn(
                "level-a nest",
                agent="explore",
                model="claude-opus-5",
                budget=2,
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
            # The budget decremented per level, and at zero both escape
            # attempts came back to the grandchild as readable results -- no
            # fourth run through the bare path (caller resolved to the
            # budget-0 run) and none through the detached thread (caller
            # resolved to nothing, which proves no budget at all).
            assert (a.budget, b.budget, c.budget) == (2, 1, 0), (a.budget, b.budget, c.budget)
            assert by_task("level-d overflow") is None, "a budget-0 run forked anyway"
            assert by_task("thread-d overflow") is None, "a detached thread forked anyway"
            refusals = sum(
                str(m.get("content")).count("spawn refused") for m in c.messages
            )
            assert refusals >= 2, c.messages[-3:]
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
            assert rec["budget"] == 0, rec["budget"]

            # --- kill_subtree (C3): a running tree settles every descendant as
            # stopped/killed through the normal terminal path -- the kill is a
            # flag each run's own loop reads, so the terminal subagent event
            # reaches the wire with phase "stopped".
            kill_nest = (
                "<python>\n"
                "import desmos.agents.subagent as S\n"
                'print(S.spawn("kill-b spin", agent="explore", model="claude-opus-5", '
                "_register_pending=False))\n"
                "</python>"
            )

            def kill_complete(_model, _system, messages, _max_tokens):
                first = not any(m.get("role") == "assistant" for m in messages)
                if first and "kill-a" in json.dumps(messages):
                    text = "forking\n" + kill_nest
                else:
                    text = "<bash>sleep 0.1</bash>"  # spins until killed
                return {"content": [{"type": "text", "text": text}], "usage": {}}

            kill_root = new_world(cwd, state_path=None, persist=False)
            kill_root.complete_fn = kill_complete
            kid = S.spawn(
                "kill-a spin", agent="explore", model="claude-opus-5", budget=1,
                parent=kill_root, _register_pending=False,
            )
            kb = None
            deadline = time.time() + 30.0
            while time.time() < deadline:
                kb = by_task("kill-b spin")
                if kb is not None and kb.state == "running":
                    break
                time.sleep(0.05)
            assert kb is not None and kb.state == "running", kb and kb.brief()
            assert S.kill_subtree("nope") == "unknown run nope"
            note = S.kill_subtree(kid)
            assert kid in note and kb.id in note, note
            S.wait(kid, kb.id, timeout=30.0)
            ka = S.RUNS[kid]
            assert (ka.state, ka.stop_reason) == ("stopped", "killed"), ka.brief()
            assert (kb.state, kb.stop_reason) == ("stopped", "killed"), kb.brief()
            stopped_ids = {
                e["id"] for e in evs_tree
                if e.get("ev") == "subagent" and e.get("phase") == "stopped"
            }
            assert stopped_ids >= {kid, kb.id}, stopped_ids

            # --- C5 brief: a 50KB child result reaches the parent as a <=400
            # char notice while the run record keeps every byte.
            big = "B" * 50_000

            def big_complete(_model, _system, _messages, _max_tokens):
                return {"content": [{"type": "text", "text": big}], "usage": {}}

            from desmos.agents import pending as P

            big_root = new_world(cwd, state_path=None, persist=False)
            big_root.complete_fn = big_complete
            bid = S.spawn("compose an epic", agent="explore", model="claude-opus-5", parent=big_root)
            S.wait(bid, timeout=15.0)
            landed: list = []
            deadline = time.time() + 5.0
            while time.time() < deadline and not landed:
                landed = P.take_done(big_root)
                time.sleep(0.05)
            assert landed, "settle notice never landed"
            notice = landed[0].output
            assert len(notice) <= 400, len(notice)
            assert notice.startswith(f"[{bid} done depth=0] compose an epic — unjudged: B"), notice
            assert S.RUNS[bid].result == big, "the run record lost the raw result"
            rec_big = json.loads((S.DIR / f"{bid}.json").read_text(encoding="utf-8"))
            assert len(rec_big["result"]) == 50_000, len(rec_big["result"])

            # --- rerun (C3): a settled run respawns as a fresh id with the
            # same objective, wired to the same parent world. Unknown ids are
            # answered in prose, never raised.
            assert S.rerun("nope") == "unknown run nope"
            rid2 = S.rerun(bid)
            assert rid2 != bid and rid2 in S.RUNS, rid2
            assert S.RUNS[rid2].task == "compose an epic"
            S.wait(rid2, timeout=15.0)
            assert S.RUNS[rid2].state == "done", S.RUNS[rid2].brief()
            assert S.RUNS[rid2].result == big, "rerun lost the parent world's complete_fn"

            # --- capability 2.2 end to end: an orchestrator child forks
            # through its own <agents> syscall — the taught path, no parent
            # kwarg anywhere. The tag must dispatch (not unknown-tag), the
            # grandchild must nest under the orchestrator's run at depth 1
            # with budget 0, and the orchestrator's loop must resume on the
            # child's pending notice and integrate it.
            def orc_complete(_model, _system, messages, _max_tokens):
                blob = json.dumps(messages)
                if "coordinate the fleet" not in blob:
                    text = "leaf ok"  # the explore grandchild
                elif not any(m.get("role") == "assistant" for m in messages):
                    text = (
                        "delegating\n<agents>spawn explore model=claude-opus-5: "
                        "look around</agents>"
                    )
                elif "background task finished" in blob:
                    text = "integrated: the explorer reports leaf ok"
                else:
                    text = "standing by"  # spawn result read; child not settled
                return {"content": [{"type": "text", "text": text}], "usage": {}}

            orc_root = new_world(cwd, state_path=None, persist=False)
            orc_root.complete_fn = orc_complete
            orc_rid = S.spawn(
                "coordinate the fleet", agent="orchestrator", model="claude-opus-5",
                budget=1, parent=orc_root, _register_pending=False,
            )
            S.wait(orc_rid, timeout=30.0)
            orc = S.RUNS[orc_rid]
            leaf = by_task("look around")
            assert leaf is not None, "the orchestrator's <agents> spawn never ran"
            assert leaf.parent == orc_rid and leaf.depth == 1, (leaf.parent, leaf.depth)
            assert leaf.budget == 0, leaf.budget
            S.wait(leaf.id, timeout=30.0)
            assert leaf.state == "done" and leaf.result == "leaf ok", leaf.brief()
            assert orc.state == "done", orc.brief()
            assert "agents" in orc.observed_tools, orc.observed_tools
            assert "integrated" in orc.result, orc.result
        finally:
            os.chdir(prev_dir)
            S.set_emitter(None)

        # --- Track 1.1: the durable pending handoff yields the notice EXACTLY
        # once no matter where a SIGKILL lands. Delivery is two-phase: the
        # handoff file sits in pending/ until the transcript carrying its
        # notice is saved, and only then moves to delivered/. Three
        # subprocesses kill themselves at the three edges of that stretch:
        # settled-but-never-taken, taken-and-appended-in-memory (no save),
        # and saved-but-not-renamed (replay must dedupe by the notice id, not
        # deliver again). Each edge is followed by two consecutive reloads.
        import subprocess
        import sys

        seam_env = dict(os.environ)
        seam_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])

        def seam_kill(seam_cwd: Path, extra: str) -> None:
            code = (
                "import os\n"
                "from pathlib import Path\n"
                "from desmos.kernel.loop import new_world\n"
                "from desmos.agents import pending\n"
                f"world = new_world(Path({str(seam_cwd)!r}))\n"
                "task = pending.submit(world, 'leaf-probe', lambda: 'the leaf result payload')\n"
                "assert task.done.wait(10), 'task never landed'\n"
                "assert task.path is not None and task.path.is_file(), task.path\n"
                + extra
                + "os.kill(os.getpid(), 9)\n"
            )
            ran = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, env=seam_env, timeout=60,
            )
            assert ran.returncode == -9, (ran.returncode, ran.stderr)

        def leaf_notices(w) -> int:
            return sum(
                1
                for m in w.messages
                if m.get("role") == "user"
                and isinstance(m.get("content"), str)
                and "the leaf result payload" in m["content"]
            )

        seam_edges = {
            # killed after the task settled, before anything took it
            "settled": "",
            # killed after wait_next took it and the notice was appended in
            # memory -- the live resume seam before commit's save
            "appended": (
                "landed = pending.wait_next(world)\n"
                "assert landed and landed[0].path.parent.name == 'pending', landed\n"
                "world.messages.append({'role': 'user', 'content': pending.notice(landed)})\n"
            ),
            # killed after commit's save, before its rename: the transcript
            # already carries the notice while the file is still in pending/
            "saved": (
                "landed = pending.wait_next(world)\n"
                "world.messages.append({'role': 'user', 'content': pending.notice(landed)})\n"
                "from desmos.state.persist import save\n"
                "save(world)\n"
            ),
        }
        for seam_label, seam_extra in seam_edges.items():
            edge_cwd = cwd / f"handoff-{seam_label}"
            edge_cwd.mkdir()
            seam_kill(edge_cwd, seam_extra)
            handoff_dir = edge_cwd / ".desmos" / "pending"
            assert len(list(handoff_dir.glob("*.json"))) == 1, (
                seam_label, list(handoff_dir.glob("*.json")),
            )
            revived = new_world(edge_cwd)
            assert leaf_notices(revived) == 1, (seam_label, revived.messages)
            assert not list(handoff_dir.glob("*.json")), (
                seam_label, "replay left the file in pending/",
            )
            assert len(list((handoff_dir / "delivered").glob("*.json"))) == 1, seam_label
            again = new_world(edge_cwd)
            assert leaf_notices(again) == 1, (seam_label, again.messages)

        # And the live path: wait_next takes without touching the file;
        # commit saves the transcript, THEN renames -- delivered/ is only
        # reached after the notice is durable, and a reload sees it once.
        live_cwd = cwd / "handoff-live"
        live_cwd.mkdir()
        live_world = new_world(live_cwd)
        live_dir = live_cwd / ".desmos" / "pending"
        live_task = _pend.submit(live_world, "live-probe", lambda: "delivered live")
        assert live_task.done.wait(10)
        landed_live = _pend.wait_next(live_world)
        assert landed_live and landed_live[0] is live_task, landed_live
        assert live_task.path is not None and live_task.path.parent == live_dir, (
            "wait_next moved the handoff file before the transcript was saved"
        )
        live_world.messages.append({"role": "user", "content": _pend.notice(landed_live)})
        _pend.commit(live_world, landed_live)
        assert live_task.path.parent.name == "delivered", live_task.path
        assert not list(live_dir.glob("*.json"))
        live_again = new_world(live_cwd)
        assert sum(
            1
            for m in live_again.messages
            if isinstance(m.get("content"), str) and "delivered live" in m["content"]
        ) == 1, live_again.messages

        # --- Track 4.1: reload_sdk is gated on the reload tier. Semantics
        # that break while still compiling (a scan that returns nothing) must
        # be refused BEFORE the importlib.reload loop -- the refusal names the
        # failure, and the live modules keep answering with the old code.
        # Runs against a copytree of the package: the gate resolves its root
        # from the loaded module's __file__, so sabotaging the COPY's scan.py
        # exercises the identical code path. The real working tree is never
        # written -- the old version of this check broke the live scan.py on
        # disk for the gate's whole round trip, so a concurrent process saw a
        # scanner that returned [] and a kill mid-window left it broken.
        import shutil

        import desmos as _desmos_pkg

        gate_root = cwd / "gate-tree"
        shutil.copytree(
            Path(_desmos_pkg.__file__).resolve().parent,
            gate_root / "desmos",
            ignore=shutil.ignore_patterns("__pycache__", ".desmos"),
        )
        gate_driver = (
            "from pathlib import Path\n"
            "import desmos\n"
            f"assert Path(desmos.__file__).resolve().parent == Path({str(gate_root)!r}).resolve() / 'desmos', desmos.__file__\n"
            "from desmos.kernel.dispatch import dispatch\n"
            "from desmos.kernel.loop import new_world\n"
            "from desmos.kernel.types import Block\n"
            "import desmos.kernel.scan as scan_mod\n"
            f"world = new_world(Path({str(gate_root)!r}))\n"
            "scan_path = Path(scan_mod.__file__)\n"
            "orig = scan_path.read_bytes()\n"
            "scan_path.write_bytes(orig + b'\\n\\ndef scan(text):\\n    return []\\n')\n"
            "refused = dispatch(world, Block('reload_sdk', '', {}))\n"
            "assert not refused.startswith('sdk reloaded'), refused\n"
            "assert 'AssertionError' in refused, refused\n"
            "# No partial reload: the live scanner is the old, working one.\n"
            "assert [b.tag for b in scan_mod.scan('<bash>ls</bash>')] == ['bash'], (\n"
            "    'the gate reloaded a broken scanner into the live process'\n"
            ")\n"
            "scan_path.write_bytes(orig)\n"
            "healed = dispatch(world, Block('reload_sdk', '', {}))\n"
            "assert healed.startswith('sdk reloaded'), healed\n"
        )
        gate_env = dict(os.environ)
        gate_env["PYTHONPATH"] = str(gate_root)
        gate_ran = subprocess.run(
            [sys.executable, "-c", gate_driver],
            capture_output=True, text=True, env=gate_env, timeout=180,
            cwd=str(gate_root),
        )
        assert gate_ran.returncode == 0, (gate_ran.stdout[-2000:], gate_ran.stderr[-3000:])

        from desmos.checks import pending_check, subagent_check

        subagent_check.self_check()
        subagent_check.parallel_tool_check()
        subagent_check.ledger_check()
        pending_check.self_check()
