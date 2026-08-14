from __future__ import annotations

from pathlib import Path

from desmos.dispatch import dispatch
from desmos.generations import evolve, gen_dir, rollback
from desmos.loop import attach, bind_step, new_world
from desmos.catalog import header, ns_names, system_prompt
from desmos.scan import scan
from desmos.complete import INTERLEAVED_BETA
from desmos.types import Block


def self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        world = new_world(cwd, state_path=cwd / "harness.json")
        from desmos.complete import cached_payload
        from desmos.const import ABI

        prompt = system_prompt(world)
        assert "cwd:" in prompt
        assert "reload_sdk" in prompt
        assert "sdk:" in prompt
        assert "thinking:" in prompt
        assert "<edit" in ABI
        assert "<reload_sdk" in ABI
        assert "XML tags are syscalls" in ABI
        assert "Look around first" in ABI
        assert world.thinking == "low"

        payload = cached_payload(
            "claude-opus-5",
            ABI + "\n\n# tools\n<python> exec",
            [{"role": "user", "content": "hi"}],
            8192,
            thinking="low",
        )
        assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert payload["output_config"] == {"effort": "low"}
        assert payload["_betas"] == []
        replay = cached_payload(
            "claude-opus-5",
            ABI + "\n\n# tools\nx",
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "plan", "signature": "sig"},
                        {"type": "redacted_thinking", "data": "opaque"},
                        {"type": "text", "text": "hi"},
                    ],
                },
                {"role": "user", "content": "ok"},
            ],
            8192,
            thinking="low",
        )
        kinds = [b["type"] for b in replay["messages"][0]["content"]]
        assert kinds == ["thinking", "redacted_thinking", "text"]
        assert replay["messages"][0]["content"][1]["data"] == "opaque"
        budget = cached_payload(
            "claude-sonnet-4-5",
            ABI + "\n\n# tools\nx",
            [{"role": "user", "content": "hi"}],
            8192,
            thinking="low",
        )
        assert budget["thinking"]["type"] == "enabled"
        assert budget["thinking"]["budget_tokens"] == 2048
        assert INTERLEAVED_BETA in budget["_betas"]
        assert "reload" in world.tools and world.tools["reload"].frozen
        assert "reload_sdk" in world.tools and world.tools["reload_sdk"].frozen
        assert any(s.name == "skill-creator" for s in world.skills)
        assert "skill-creator" in dispatch(world, Block("skill", "", {"name": "skill-creator"}))
        assert any(s.name == "edit" for s in world.skills) or "edit" in world.tools

        sample = cwd / "sample.txt"
        sample.write_text("alpha beta alpha\n", encoding="utf-8")
        assert "exactly 1" in dispatch(
            world, Block("edit", "alpha\n---\nALPHA", {"path": str(sample)})
        )
        sample.write_text("alpha beta\n", encoding="utf-8")
        assert "Edited" in dispatch(world, Block("edit", "alpha\n---\nALPHA", {"path": str(sample)}))
        assert sample.read_text(encoding="utf-8") == "ALPHA beta\n"

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
        assert "reloaded" in dispatch(world, Block("reload", "", {}))
        assert any(s.name == "later" for s in world.skills)
        assert dispatch(world, Block("skill", "", {"name": "later"})).endswith("ok\n")

        sdk_out = dispatch(world, Block("reload_sdk", "", {}))
        assert "sdk reloaded" in sdk_out
        assert "reload_sdk" in world.tools

        blocks = scan('<python>x = 1+1</python>\n<bash>echo hi</bash>')
        assert [b.tag for b in blocks] == ["python", "bash"]
        lone = scan("<usage/>\n<reload/>\n<reload_sdk/>\n<rollback n=\"1\"/>\n<skill name=\"ping\"/>")
        assert [b.tag for b in lone] == ["usage", "reload", "reload_sdk", "rollback", "skill"]
        assert lone[0].body == ""
        assert lone[3].attrs == {"n": "1"}
        assert lone[4].attrs == {"name": "ping"}
        assert dispatch(world, blocks[0]) == "ok"
        assert world.ns["x"] == 2
        assert dispatch(world, blocks[1]).strip() == "hi"

        out = dispatch(
            world,
            Block("register", "def handle(body, **a):\n    return body.upper()\n", {"name": "echo", "doc": "uppercase"}),
        )
        assert "registered" in out
        assert dispatch(world, Block("echo", "hi", {})) == "HI"

        assert "wrote" in dispatch(world, Block("system", "prefer tests", {"name": "style"}))
        assert "prefer tests" in system_prompt(world)

        world2 = new_world(cwd, state_path=cwd / "harness.json")
        assert "echo" in world2.tools
        assert world2.notes["style"] == "prefer tests"

        def fake_complete(model, system, messages, max_tokens):
            blob = __import__("json").dumps(messages)
            assert "hello world" not in blob
            if any("<result" in (m.get("content") or "") for m in messages):
                return {"content": [{"type": "text", "text": "11"}], "usage": {}}
            return {"content": [{"type": "text", "text": "<python>len(doc)</python>"}], "usage": {}}

        ns = {"doc": "hello world"}
        w3 = new_world(cwd, state_path=cwd / "harness2.json", ns=ns)
        w3.complete_fn = fake_complete
        bind_step(w3)
        out = w3.ns["step"]("how long is doc?")
        assert out.strip() == "11"
        assert w3.messages[2]["content"].startswith("<result")
        assert "prompt:" not in w3.messages[2]["content"]
        def fake_usage(_model, _system, messages, _max_tokens):
            if any("<result" in (m.get("content") or "") for m in messages):
                return {"content": [{"type": "text", "text": "hello"}], "usage": {}}
            return {"content": [{"type": "text", "text": "<usage/>"}], "usage": {}}

        w_usage = new_world(cwd, state_path=cwd / "harness-usage.json", ns={})
        dispatch(
            w_usage,
            Block("register", "def handle(body, **a):\n    return 'tokens:0'\n", {"name": "usage", "doc": "stats"}),
        )
        w_usage.complete_fn = fake_usage
        bind_step(w_usage)
        spoken = w_usage.ns["step"]("hi there")
        assert spoken.strip() == "hello"
        assert "tokens:0" in w_usage.messages[2]["content"]
        seen: list[str] = []
        w_usage.ns["reset"]()
        w_usage.complete_fn = fake_usage
        from desmos.loop import run_turns as _run

        _run(w_usage, "ping", quiet=True, on_event=lambda e: seen.append(str(e.get("ev"))))
        assert "speech" in seen and "result" in seen and "turn" in seen
        assert "transcript cleared" in w_usage.ns["reset"]()
        assert w_usage.messages == []

        ev = evolve(w3, "after ping")
        assert "generation 2" in ev
        assert (gen_dir(w3) / "0001.json").is_file()
        assert "wrote" in dispatch(w3, Block("system", "usage line", {}))
        assert w3.notes["note"] == "usage line"
        assert "generation 1" in rollback(w3, 1)
        assert "note" not in w3.notes

        py = cwd / "broke.py"
        py.write_text("x = 1\n")
        bad = dispatch(world, Block("edit", "x = 1\n---\nx =\n", {"path": str(py)}))
        assert "SyntaxError" in bad
        assert py.read_text(encoding="utf-8") == "x = 1\n"

        from desmos.persist import save as save_world
        from desmos.subagent import _child_world, resolve, wait

        parent = new_world(cwd, state_path=cwd / "harness-iso.json")
        dispatch(
            parent,
            Block("register", "def handle(body, **a):\n    return 'SECRET'\n", {"name": "secret", "doc": "parent only"}),
        )
        child = _child_world(resolve("explore"), parent)
        assert child.persist is False
        assert "secret" not in child.tools
        assert "agents" not in child.tools
        child.notes["pwn"] = "from-child"
        save_world(child)
        on_disk = __import__("json").loads((cwd / "harness-iso.json").read_text(encoding="utf-8"))
        assert "pwn" not in on_disk.get("notes", {})
        unknown = wait("nope")
        assert unknown[0]["state"] == "unknown"
        import desmos.subagent as S

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

        import threading

        import desmos.complete as C

        tdir = cwd / "traj"
        tdir.mkdir()
        prev = C.TRAJECTORY_DIR
        C.TRAJECTORY_DIR = str(tdir)
        try:
            def _write(i: int) -> None:
                C.log_payload({"system": [{"type": "text", "text": f"s{i}"}], "messages": []}, [])

            threads = [threading.Thread(target=_write, args=(i,)) for i in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            files = list(tdir.glob("*.json"))
            assert len(files) == 16
            for f in files:
                rec = __import__("json").loads(f.read_text(encoding="utf-8"))
                assert "system_digest" in rec
            assert len(C.trajectory(16)) == 16
        finally:
            C.TRAJECTORY_DIR = prev

        try:
            from IPython.core.interactiveshell import InteractiveShell
        except ImportError:
            print("self-check ok (no IPython)")
            return
        shell = InteractiveShell.instance()
        shell.user_ns["doc"] = "hello world"
        w4 = attach(shell, cwd=cwd)
        w4.state_path = cwd / "harness3.json"
        w4.complete_fn = fake_complete
        assert callable(shell.user_ns["step"])
        assert callable(shell.user_ns.get("reload_sdk"))
        assert callable(shell.user_ns.get("reset"))
        assert "doc" in ns_names(w4)
        assert dispatch(w4, Block("python", "len(doc)", {})) == "11"

    print("self-check ok")
