from __future__ import annotations

from pathlib import Path

from desmos.dispatch import dispatch
from desmos.generations import evolve, gen_dir, rollback
from desmos.loop import attach, bind_step, new_world, reload
from desmos.catalog import header, ns_names, system_prompt
from desmos.scan import scan
from desmos.types import Block


def self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        world = new_world(cwd, state_path=cwd / "harness.json")
        assert "cwd:" in system_prompt(world)
        assert "reload" in system_prompt(world)
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
        reload(world)
        assert any(s.name == "later" for s in world.skills)

        blocks = scan('<python>x = 1+1</python>\n<bash>echo hi</bash>')
        assert [b.tag for b in blocks] == ["python", "bash"]
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
        ev = evolve(w3, "after ping")
        assert "generation 2" in ev
        assert (gen_dir(w3) / "0001.json").is_file()
        assert "wrote" in dispatch(w3, Block("system", "usage line", {}))
        assert w3.notes["note"] == "usage line"
        assert "generation 1" in rollback(w3, 1)
        assert "note" not in w3.notes

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
        assert "doc" in ns_names(w4)
        assert dispatch(w4, Block("python", "len(doc)", {})) == "11"

    print("self-check ok")
