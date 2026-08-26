"""Kernel checks: scan, dispatch, exec, shell, edit, spill, loop, catalog."""

from __future__ import annotations

from pathlib import Path

from desmos.dispatch import dispatch
from desmos.exec import run_bash
from desmos.loop import attach, bind_step, new_world
from desmos.catalog import ns_names, system_prompt
from desmos.scan import scan
from desmos.types import Block


def check_shell_peek_history(cwd: Path) -> None:
    """Shell peek reports bounded PTY history without touching the live PTY."""
    import os
    import select

    from desmos.kernel.shell import close_all
    from desmos.loop import new_world

    world = new_world(cwd, state_path=None, persist=False, ns={})

    def sh(body: str = "", **attrs: str) -> str:
        return dispatch(world, Block("exec", body, {**attrs, "op": "shell"}))

    try:
        sentinel = b"pty-history-sentinel"
        sh("printf pty-history-sentinel")
        shell = world.shells["main"]
        assert sentinel in shell._history, "PTY output was not recorded in _history"

        tail = b"history-tail-kept"
        sh(
            "python3 -c \"import os;"
            "os.write(1, b'H' * 140000 + b'history-tail-kept')\""
        )
        if shell.monitoring:
            from desmos import pending

            assert pending.wait_next(world, timeout=5), "large-output probe did not finish"
        assert len(shell._history) <= 131072, (
            f"_history exceeded its 128KB cap: {len(shell._history)} bytes"
        )
        assert tail in shell._history[-1024:], "rolling _history discarded its newest tail"

        before_history = bytes(shell._history)
        before_state = (
            shell.mark,
            shell._generation,
            shell._interrupted_generation,
            shell.monitoring,
            shell.at_prompt,
            shell.cwd,
        )
        os.write(shell.master, b"printf unread-peek-sentinel\n")
        ready, _, _ = select.select([shell.master], [], [], 1)
        assert ready, "shell did not put the probe output on its PTY"

        peeked = sh(peek="1")
        assert "[shell at prompt]" in peeked and "history-tail-kept" in peeked, peeked
        assert "unread-peek-sentinel" not in peeked, "peek drained unread PTY output"
        assert bytes(shell._history) == before_history, "peek mutated _history"
        assert (
            shell.mark,
            shell._generation,
            shell._interrupted_generation,
            shell.monitoring,
            shell.at_prompt,
            shell.cwd,
        ) == before_state, "peek mutated shell state"
        ready, _, _ = select.select([shell.master], [], [], 0)
        assert ready, "peek consumed the pending PTY output"
        drained = shell._drain(0.2)
        assert b"unread-peek-sentinel" in drained, "pending output was not left for the reader"
        assert b"unread-peek-sentinel" in shell._history, "later PTY read missed _history"
    finally:
        close_all(world)


def check_shell_close_is_idempotent(cwd: Path) -> None:
    """A repeated close must not close a descriptor reused by another owner."""
    import os

    from desmos.kernel.shell import Shell

    shell = Shell(cwd)
    master = shell.master
    shell.close()
    probe = os.open(os.devnull, os.O_RDONLY)
    if probe != master:
        os.dup2(probe, master)
        os.close(probe)
    try:
        shell.close()
        os.fstat(master)
    finally:
        try:
            os.close(master)
        except OSError:
            pass


def check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        world = new_world(cwd, state_path=cwd / "harness.json")

        # redirect_stdout mutates process-global sys.stdout. Parallel subagents
        # can run Python tools on different threads, so without serialization
        # one tool restores stdout while another still owns it. The remaining
        # print then leaks raw text onto the bridge's NDJSON wire.
        import threading as _threading
        import time as _time
        from desmos.exec import run_python as _run_python

        _first = _threading.Event()
        _outputs: dict[str, str] = {}
        _wa = new_world(cwd, state_path=None, persist=False, ns={})
        _wb = new_world(cwd, state_path=None, persist=False, ns={})

        def _python_a() -> None:
            _outputs["a"] = _run_python(
                "import time\nprint('A-first')\ntime.sleep(0.08)\nprint('A-last')",
                _wa,
                on_chunk=lambda _s: _first.set(),
            )

        def _python_b() -> None:
            _outputs["b"] = _run_python(
                "import time\nprint('B-first')\ntime.sleep(0.15)\nprint('B-last')",
                _wb,
            )

        _ta = _threading.Thread(target=_python_a)
        _tb = _threading.Thread(target=_python_b)
        _ta.start()
        assert _first.wait(1), "first Python tool never reached stdout"
        _tb.start()
        _ta.join(1)
        _tb.join(1)
        assert not _ta.is_alive() and not _tb.is_alive(), "serialized Python tools wedged"
        assert _outputs == {
            "a": "A-first\nA-last",
            "b": "B-first\nB-last",
        }, _outputs

        from desmos.complete import cached_payload
        from desmos.const import ABI

        prompt = system_prompt(world)

        # The prompt states facts about the subagent layer. Every one of them is
        # derived here from the live objects, so a signature change fails the
        # suite rather than quietly making the system prompt wrong.
        from desmos.dialect import capabilities as _caps
        from desmos import subagent as _sa
        from desmos.loop import RESULT_CLIP as _clip_cap
        from desmos.agents.subagent_contracts import TaskContract as _TC
        from desmos.agents import subagent_prompt as _sp
        import inspect as _insp

        caps = _caps()
        assert str(_clip_cap) in caps, "prompt quotes a result cap loop.py does not apply"
        # Over the cap the output goes to disk. The prompt names that directory,
        # so the name is derived from the module that writes it.
        from desmos.spill import SPILL_DIR as _spill_dir
        assert _spill_dir in caps, "the prompt does not say where a spilled result lands"
        assert "Prefer exec op=shell with id=main" in caps
        # The model must never be asked to size a read window; that was prompt
        # noise describing transport, and it taught polling.
        assert "no read windows to choose and nothing to poll" in caps
        _state_line = next(l for l in caps.split("\n") if l.startswith("state: exec op=python"))
        assert "timeout" not in _state_line, _state_line
        assert "Use exec op=bash only for a quick hermetic one-shot" in caps
        for _name in (*_sa.AGENTS, *_sa.ALIASES):
            assert _name in caps, _name
        # fanout's default agent is not spawn's. The prompt says so because a
        # reader would otherwise assume they match.
        # step()'s turn cap is quoted in the prompt, so the prompt has to be
        # re-derived from the signature rather than remembered.
        from desmos.loop import bind_step as _bind
        _step_sig = _insp.signature(_bind(new_world(cwd, state_path=None, persist=False, ns={})))
        assert f"max_turns={_step_sig.parameters['max_turns'].default}" in caps, (
            "the prompt quotes a step() turn cap the loop does not apply"
        )
        assert _insp.signature(_sa.spawn).parameters["agent"].default == "general"
        assert _insp.signature(_sa.fanout).parameters["agent"].default == "explore"
        assert "resume" in _insp.signature(_sa.spawn).parameters and "resume" in caps
        for _f in _TC.__dataclass_fields__:
            assert _f in caps, f"TaskContract.{_f} is not described in the prompt"
        # A bare string on a list field is one item, not a tuple of letters.
        # tuple("file:line") shredded the evidence requirement into characters
        # and rejected a run that had done the work and reported it properly.
        _one = _TC.simple(
            "do the thing",
            paths="crates/x",
            write="crates/x",
            checks="cargo test",
            evidence="file",
        )
        assert _one.required_evidence == ("file",), _one.required_evidence
        assert _one.allowed_paths == ("crates/x",), _one.allowed_paths
        assert _one.write_paths == ("crates/x",), _one.write_paths
        assert _one.acceptance_checks == ("cargo test",), _one.acceptance_checks
        # Lists and tuples still work, and empties stay empty.
        _many = _TC.simple("do it", paths=["a", "b"], evidence=())
        assert _many.allowed_paths == ("a", "b") and _many.required_evidence == ()
        # required_evidence is matched against EvidenceRef.kind, so a prose
        # sentence is unsatisfiable. It must fail at construction, not survive
        # to judgement as "missing required evidence kind: <the whole sentence>".
        try:
            _TC.simple("do it", evidence="every claim cites a file:line")
        except ValueError as _exc:
            assert "unknown required_evidence kinds" in str(_exc), str(_exc)
        else:
            raise AssertionError("prose required_evidence was accepted")
        # The child is shown the schema it must emit, not just told it exists.
        _fmt = _sp._output_format(_TC.simple("o", checks="cargo test", evidence="command"))
        assert '"summary"' in _fmt and '"changed_paths"' in _fmt, _fmt
        assert "cargo test" in _fmt, "acceptance check names are not shown verbatim"
        assert _sp._output_format(None) == ""
        # Wiring, not just the helper: the schema and the capability text must
        # reach the prompt the child is actually launched with. Asserting on
        # _output_format alone would pass against an unwired call site.
        class _W:
            cwd = "/tmp"
            model = "gpt-5.6-luna"
            tools: dict = {}

        _full = _sp.child_system_prompt(
            _W(), _sa.resolve("explore"), _TC.simple("o", evidence="command")
        )
        assert "# output format" in _full, "output format never reaches the child prompt"
        assert '"changed_paths"' in _full, "schema body missing from the child prompt"
        assert "Read-only" in _full, "capability guide never reaches the child prompt"
        # Every CAPS mode has prompt text; a new capability cannot ship without it.
        assert set(_sp._CAPABILITY_GUIDE) == set(_sa.CAPS), (
            set(_sa.CAPS) ^ set(_sp._CAPABILITY_GUIDE)
        )
        for _n in ("structured_result", "judgment", "spawn", "fanout", "wait", "gather", "status"):
            assert callable(getattr(_sa, _n)), _n
            assert _n in caps, f"prompt names {_n} but subagent does not export it"

        assert world.thinking == "low"

        assert "harness" in world.tools and world.tools["harness"].frozen
        assert "exec" in world.tools and world.tools["exec"].frozen
        assert any(s.name == "skill-creator" for s in world.skills)
        assert "skill-creator" in dispatch(world, Block("harness", "", {"op": "skill", "name": "skill-creator"}))
        assert any(s.name == "edit" for s in world.skills) or "edit" in world.tools

        sample = cwd / "sample.txt"
        sample.write_text("alpha beta alpha\n", encoding="utf-8")
        # Two matches is ambiguous, and a refusal that still wrote would
        # corrupt the file while reporting the refusal.
        dispatch(world, Block("workspace", "alpha\n---\nALPHA", {"op": "edit", "path": str(sample)}))
        assert sample.read_text(encoding="utf-8") == "alpha beta alpha\n", "ambiguous edit wrote anyway"
        sample.write_text("alpha beta\n", encoding="utf-8")
        dispatch(world, Block("workspace", "alpha\n---\nALPHA", {"op": "edit", "path": str(sample)}))
        assert sample.read_text(encoding="utf-8") == "ALPHA beta\n"

        blocks = scan('<python>x = 1+1</python>\n<bash>echo hi</bash>')
        assert [b.tag for b in blocks] == ["python", "bash"]
        fence = "```"
        # A mermaid line-break tag in a diagram label is markup, not a call.
        assert scan(fence + 'mermaid\nA["a<br/>b"] --> B\n' + fence) == []
        assert scan("name it `<traj>` or bare") == []
        # A span opens on one backtick and closes on one: the ``` inside it is
        # content. This exact line dispatched a stray <br> before the fix.
        assert scan("the string `'" + fence + r"\n<br/>\n" + fence + "<python>1</python>`") == []
        assert [b.tag for b in scan("see `<traj>` then\n<python>1</python>")] == ["python"]
        # A fence inside a syscall body must not mask the calls after it.
        masked = scan('<python>s = "' + fence + '"</python>\n<bash>ls</bash>')
        assert [b.tag for b in masked] == ["python", "bash"], masked
        # An unclosed fence never swallows the rest of the message.
        assert [b.tag for b in scan(fence + " oops\n<python>1</python>")] == ["python"]
        assert [b.tag for b in scan(fence + "\n<br/>\n" + fence + "\n<python>1</python>")] == ["python"]

        # Four ways a real call used to become invisible. Each one reads to the
        # loop as a message with no syscalls, which is exactly how it decides
        # the model is finished -- so the command never ran and the turn ended
        # with nothing printed to say so.
        # An apostrophe in prose is not an unclosed string. The quoting
        # heuristic exists so a code body does not end at a closer the model
        # quoted. Applied to prose it made "the agent" plus an apostrophe
        # swallow the closer, and the whole call vanished: no dispatch, no
        # error, three lost commits in one session.
        lt = chr(60)
        prose = scan(lt + 'commit only="a.rs">' + "fix the agent" + chr(39) + "s own commits" + lt + "/commit>")
        assert [b.tag for b in prose] == ["commit"], prose
        assert chr(39) + "s own" in prose[0].body
        # ... while a code body still ends at the first unquoted closer.
        clp = lt + "/python>"
        code = scan(lt + "python>" + chr(39) + clp + chr(39) + clp)
        assert [b.tag for b in code] == ["python"], code
        assert clp in code[0].body
        quoted = scan("<skill name='ping'/>\n<rollback n=1/>")
        assert [(b.tag, b.attrs) for b in quoted] == [
            ("skill", {"name": "ping"}),
            ("rollback", {"n": "1"}),
        ], quoted
        # One stray backtick with no partner is literal text, not a span that
        # eats the rest of the line and the call after it.
        assert [b.tag for b in scan("the flag is `--all so <bash>echo hi</bash>")] == ["bash"]
        # An indented sample is a code block the story pane draws as code. It
        # must not dispatch -- and the real call under it must.
        indented = scan("sample:\n\n    <bash>rm -rf /</bash>\n\nfor real:\n<bash>ls</bash>")
        assert [(b.tag, b.body) for b in indented] == [("bash", "ls")], indented
        # A same-name tag inside the body used to end it at the first closer:
        # half the command ran and the rest became residue nobody read.
        nested = scan('<bash>echo "<bash>inner</bash>" && ls</bash>')
        assert [b.body for b in nested] == ['echo "<bash>inner</bash>" && ls'], nested
        # The indented-code rule measures from the *content* column of the list
        # item, not from column 0. A wrapped bullet indents its continuation,
        # so a four-space call under it read as a code sample and the command
        # the model asked for never ran.
        wrapped = scan("- step one\n  continued here\n\n    <bash>ls</bash>")
        assert [(b.tag, b.body) for b in wrapped] == [("bash", "ls")], wrapped
        numbered = scan("1. step one\n   continued here\n\n    <bash>ls</bash>")
        assert [(b.tag, b.body) for b in numbered] == [("bash", "ls")], numbered
        # ... and a sample that really is deeper than the content column stays
        # a sample, or the rule has just been deleted.
        assert scan("- step one\n  continued here\n\n      <bash>ls</bash>") == []
        # An opener quoted inside a body inflates the nesting count, so a
        # stray same-name closer later in the message looked like the real
        # end. The body then swallowed every call in between -- for <bash>,
        # narration ran as shell.
        swallow = scan(
            '<python>t = "<python>"</python>\n<bash>ls</bash>\n'
            "the closer is </python> by the way\n<bash>pwd</bash>"
        )
        assert [(b.tag, b.body) for b in swallow] == [
            ("python", 't = "<python>"'),
            ("bash", "ls"),
            ("bash", "pwd"),
        ], swallow

        lone = scan("<usage/>\n<reload/>\n<reload_sdk/>\n<rollback n=\"1\"/>\n<skill name=\"ping\"/>")
        assert [b.tag for b in lone] == ["usage", "reload", "reload_sdk", "rollback", "skill"]
        assert lone[0].body == ""
        assert lone[3].attrs == {"n": "1"}
        assert lone[4].attrs == {"name": "ping"}
        # Canonical cut step 5: retired spellings reject with guidance and
        # never execute -- the body must not reach the kernel namespace.
        refused = dispatch(world, blocks[0])
        assert "removed" in refused and "exec op=python" in refused, refused
        assert "x" not in world.ns, "a removed tag executed anyway"
        refused = dispatch(world, blocks[1])
        assert "removed" in refused and "exec op=bash" in refused, refused

        # The advertised surface is seven capability families. Compatibility
        # aliases still execute, but they do not compete in the tool catalog.
        canonical_file = cwd / "canonical.txt"
        canonical_file.write_text("one\ntwo\nthree")
        canonical = new_world(cwd, state_path=None, persist=False)
        prompt = system_prompt(canonical)
        tool_block = prompt.split("# tools\n", 1)[1].split("# runtime", 1)[0]
        marker = chr(60)
        canonical_names = {"exec", "workspace", "knowledge", "harness", "observe", "agents", "session"}
        shown = {
            line.split(">", 1)[0][1:]
            for line in tool_block.splitlines()
            if line.startswith(marker) and line[1:2] != "/" and line.split(">", 1)[0][1:] in canonical_names
        }
        assert shown == canonical_names, shown
        for alias in ("python", "bash", "shell", "edit", "find", "memory", "read", "grep", "sleeper"):
            assert marker + alias + ">" not in tool_block, alias

        assert dispatch(canonical, Block("exec", "20 + 22", {"op": "python"})) == "42"
        # Canonical cut step 5: every removed spelling answers with a one-line
        # rejection naming its canonical replacement, never silence or a
        # traceback -- and never a result.
        for legacy, hint in (
            ("python", "exec op=python"),
            ("bash", "exec op=bash"),
            ("grep", "workspace op=find mode=grep"),
            ("sleeper", "exec op=shell"),
        ):
            refusal = dispatch(canonical, Block(legacy, "20 + 22", {}))
            assert "removed" in refusal and hint in refusal, (legacy, refusal)
            assert "Traceback" not in refusal, (legacy, refusal)
        # ... while a grown tag that is not a legacy name dispatches untouched.
        from desmos.kernel.types import Tool as _Tool
        canonical.tools["interface"] = _Tool(
            "interface", "grown probe", handler=lambda body: f"grown:{body}"
        )
        assert dispatch(canonical, Block("interface", "ok", {})) == "grown:ok"
        del canonical.tools["interface"]
        read_back = dispatch(
            canonical,
            Block("workspace", "", {"op": "read", "path": "canonical.txt", "lines": "2-3"}),
        )
        assert "two" in read_back and "three" in read_back, read_back
        todo_back = dispatch(canonical, Block("knowledge", "+ canonical proof", {"op": "todo"}))
        assert "canonical proof" in todo_back, todo_back
        assert "calls" in dispatch(canonical, Block("observe", "", {"op": "usage"}))
        assert '"generation":' in dispatch(canonical, Block("session", "", {"op": "status"}))
        assert "unknown op" in dispatch(canonical, Block("exec", "", {"op": "wrong"}))

        observed_tags: list[str] = []
        canonical.hooks["before_dispatch"] = [
            lambda _world, normalized: observed_tags.append(normalized.tag)
        ]
        dispatch(canonical, Block("exec", "printf canonical", {"op": "bash"}))
        assert observed_tags == ["bash"], observed_tags

        # Canonical wiring proof: the canonical families own their
        # implementations. One call per family must still succeed with every
        # removed spelling absent from world.tools -- canonical depends on no
        # legacy registration (they are no longer seeded at all, step 5).
        from desmos.kernel.const import REMOVED_TAGS

        wired = new_world(cwd, state_path=None, persist=False)
        for name in REMOVED_TAGS:
            wired.tools.pop(name, None)
        assert dispatch(wired, Block("exec", "20 + 22", {"op": "python"})) == "42"
        wired_read = dispatch(
            wired, Block("workspace", "", {"op": "read", "path": "canonical.txt"})
        )
        assert "three" in wired_read, wired_read
        assert "wrote note" in dispatch(
            wired,
            Block("knowledge", "canonical owns system", {"op": "system", "name": "cut2"}),
        )
        assert "updated" in dispatch(
            wired, Block("harness", "", {"op": "describe", "name": "exec", "doc": "exec ops"})
        )
        assert "calls" in dispatch(wired, Block("observe", "", {"op": "usage"}))
        wired_agents = dispatch(wired, Block("agents", "", {"op": "status"}))
        assert wired_agents.startswith(("{", "[")), wired_agents
        assert '"generation":' in dispatch(wired, Block("session", "", {"op": "status"}))

        # `diag` is a real persistent-kernel primitive, not a helper exercised
        # out of band. An uncaught Python call records bounded plain data, and a
        # later call can query it without reconstructing inspect/traceback code.
        import json as _json

        failed = dispatch(
            world,
            Block(
                "exec",
                "def diag_outer():\n    raise RuntimeError('structured boom')\ndiag_outer()",
                {"op": "python"},
            ),
        )
        assert "RuntimeError: structured boom" in failed, failed
        diag = world.ns["diag"]
        snap = diag.error()
        encoded = _json.dumps(snap)
        assert snap["type"] == "RuntimeError" and snap["message"] == "structured boom", snap
        assert snap["frames"][-1]["function"] == "diag_outer", snap
        assert "locals" not in encoded and len(encoded) <= 8192, encoded
        try:
            raise RuntimeError("x" * 10_000)
        except RuntimeError as oversized:
            from desmos.kernel.diagnostics import exception_snapshot

            assert len(_json.dumps(exception_snapshot(oversized, max_chars=512))) <= 512
        queried = dispatch(world, Block("exec", "diag.error()['frames'][-1]['function']", {"op": "python"}))
        assert queried == "'diag_outer'", queried

        symbol = diag.symbol(diag.threads, source=True, max_chars=1200)
        assert symbol["name"] == "threads" and symbol["file"].endswith("diagnostics.py"), symbol
        assert isinstance(symbol["line"], int) and len(_json.dumps(symbol)) <= 1200, symbol
        assert len(_json.dumps(diag.symbol(type(diag).threads, source=True, max_chars=512))) <= 512
        assert dispatch(world, Block("exec", "diag.symbol(diag.threads)['name']", {"op": "python"})) == "'threads'"

        ready = _threading.Event()
        release = _threading.Event()

        def _diag_waiter() -> None:
            ready.set()
            release.wait()

        waiter = _threading.Thread(target=_diag_waiter, name="diag-blocked", daemon=True)
        waiter.start()
        assert ready.wait(1)
        thread_snap = diag.threads("diag-blocked", depth=8)
        release.set()
        waiter.join(1)
        assert len(thread_snap) == 1 and thread_snap[0]["name"] == "diag-blocked", thread_snap
        assert any(frame["function"] == "wait" for frame in thread_snap[0]["stack"]), thread_snap
        assert "locals" not in _json.dumps(thread_snap), thread_snap
        assert len(_json.dumps(diag.threads(max_chars=512))) <= 512

        custom_diag = object()
        collision = new_world(cwd, state_path=None, persist=False, ns={"diag": custom_diag})
        assert collision.ns["diag"] is custom_diag, "kernel diagnostics clobbered user state"
        collision_none = new_world(cwd, state_path=None, persist=False, ns={"diag": None})
        assert collision_none.ns["diag"] is None, "kernel diagnostics replaced a user-owned None"

        out = dispatch(
            world,
            Block("harness", "def handle(body, **a):\n    return body.upper()\n", {"op": "register", "name": "echo", "doc": "uppercase"}),
        )
        assert dispatch(world, Block("echo", "hi", {})) == "HI"

        dispatch(world, Block("knowledge", "prefer tests", {"op": "system", "name": "style"}))
        assert "prefer tests" in system_prompt(world)

        def fake_complete(model, system, messages, max_tokens):
            blob = __import__("json").dumps(messages)
            assert "hello world" not in blob
            if any("<result" in (m.get("content") or "") for m in messages):
                return {"content": [{"type": "text", "text": "11"}], "usage": {}}
            return {"content": [{"type": "text", "text": '<exec op="python">len(doc)</exec>'}], "usage": {}}

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
            return {"content": [{"type": "text", "text": "<stats/>"}], "usage": {}}

        w_usage = new_world(cwd, state_path=cwd / "harness-usage.json", ns={})
        dispatch(
            w_usage,
            Block("harness", "def handle(body, **a):\n    return 'tokens:0'\n", {"op": "register", "name": "stats", "doc": "stats"}),
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
        assert "post" in seen
        assert seen.index("post") < seen.index("complete")

        # --- steer wakes the step ---
        # `done` is decided before the steers drain, so a steer landing on the
        # turn the model finished used to sit in the transcript unread while
        # the step returned. It must buy another turn, and it must arrive as
        # its own labelled user message, never glued into a sent result.
        from desmos.kernel.catalog import steer as _steer

        w_steer = new_world(cwd, state_path=cwd / "harness-steer.json", ns={})
        _steer_calls: list[int] = []

        def fake_done(_model, _system, messages, _max_tokens):
            _steer_calls.append(len(messages))
            return {"content": [{"type": "text", "text": f"reply {len(_steer_calls)}"}], "usage": {}}

        w_steer.complete_fn = fake_done
        _steer(w_steer, "redirect to Z")
        steer_events: list[dict] = []
        _run(w_steer, "go", quiet=True, on_event=steer_events.append)
        steer_turns = [e for e in steer_events if e.get("ev") == "turn"]
        assert len(steer_turns) == 2, f"a steer on a done turn must buy another turn: {steer_events}"
        assert [
            m for m in w_steer.messages
            if m.get("role") == "user" and m.get("content") == "[steer] redirect to Z"
        ], w_steer.messages
        for m in w_steer.messages:
            content = m.get("content")
            if isinstance(content, list):
                assert not any("redirect to Z" in str(b) for b in content), m

        # A steer queued while the loop is parked in pending.wait_next breaks
        # the park the way user input does -- before the task lands, not after.
        from desmos.agents import pending as _pending

        w_park = new_world(cwd, state_path=cwd / "harness-park.json", ns={})
        _release = _threading.Event()

        def fake_park(_model, _system, messages, _max_tokens):
            if not any(m.get("role") == "assistant" for m in messages):
                _pending.submit(w_park, "slowtask", lambda: _release.wait(timeout=10) and "landed")
            return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

        w_park.complete_fn = fake_park
        park_events: list[dict] = []
        _threading.Timer(0.15, lambda: _steer(w_park, "wake up")).start()
        _threading.Timer(0.6, _release.set).start()
        _run(w_park, "start", quiet=True, on_event=park_events.append)
        park_names = [e.get("ev") for e in park_events]
        assert "steer" in park_names and "resumed" in park_names, park_names
        assert park_names.index("steer") < park_names.index("resumed"), (
            f"a steer must break the park before the task lands: {park_names}"
        )
        assert any(
            m.get("role") == "user" and m.get("content") == "[steer] wake up"
            for m in w_park.messages
        ), w_park.messages

        # A syscall that raises is that syscall's result. It used to unwind the
        # whole dispatch loop and take the syscalls before it with it: the
        # <bash> had already run, its output was thrown away, and the model's
        # next context showed its own tag with no outcome -- a side effect that
        # happened and a transcript that never says it did.
        w_raise = new_world(cwd, state_path=cwd / "harness-raise.json", ns={})
        # An ambiguous edit body raises ValueError out of dispatch -- the exact
        # call that first produced this.
        ambiguous = "<workspace op=\"edit\" path=\"nope.txt\">a\n---\nb\n---\nc</workspace>"

        def raising_complete(_model, _system, _messages, _max_tokens):
            return {
                "content": [
                    {"type": "text", "text": f'<exec op="bash">echo ranfirst</exec>\n{ambiguous}\n<exec op="bash">echo ranlast</exec>'}
                ],
                "usage": {},
            }

        w_raise.complete_fn = raising_complete
        from desmos.loop import turn as _turn

        _speech, raise_results, _done, _asst, _note = _turn(w_raise, list(w_raise.messages), 512)
        assert [b.tag for b, _ in raise_results] == ["exec", "workspace", "exec"], raise_results
        assert raise_results[0][1].strip() == "ranfirst", "a raise ate an earlier syscall's output"
        assert "ValueError" in raise_results[1][1], raise_results[1][1]
        assert raise_results[2][1].strip() == "ranlast", "a raise ended the batch early"

        def thinking_complete(_model, _system, _messages, _max_tokens):
            return {
                "content": [
                    {"type": "thinking", "thinking": "plan", "signature": "sig"},
                    {"type": "redacted_thinking", "data": "opaque-secret"},
                    {"type": "text", "text": "hi"},
                ],
                "usage": {},
            }

        w_th = new_world(cwd, state_path=cwd / "harness-think.json", ns={})
        w_th.complete_fn = thinking_complete
        evs_th: list[dict] = []
        _run(w_th, "hi", quiet=True, on_event=lambda e: evs_th.append(e))
        thinks = [e for e in evs_th if e.get("ev") == "thinking"]
        assert len(thinks) == 2
        assert thinks[0].get("redacted") is False and thinks[0].get("text") == "plan"
        assert thinks[1].get("redacted") is True
        assert "opaque-secret" not in str(evs_th)
        complete_ev = next(e for e in evs_th if e.get("ev") == "complete")
        assert complete_ev.get("thoughts") == 1 and complete_ev.get("redacted") == 1

        # Residue has to reach the event, not just exist as a function. The
        # message itself stays byte-exact: rewriting it would break the cached
        # prefix on the next request, which is the whole reason we report
        # instead of trimming.
        junk = "<usage/> \n lousy?"

        def residue_complete(_model, _system, _messages, _max_tokens, _c=[0]):
            _c[0] += 1
            return {"content": [{"type": "text", "text": junk if _c[0] == 1 else "done"}], "usage": {}}

        w_res = new_world(cwd, state_path=cwd / "harness-residue.json", ns={})
        w_res.complete_fn = residue_complete
        evs_res: list[dict] = []
        _run(w_res, "hi", quiet=True, on_event=lambda e: evs_res.append(e))
        firsts = [e for e in evs_res if e.get("ev") == "complete"]
        assert firsts[0].get("residue") == "lousy?", firsts[0].get("residue")
        assert firsts[1].get("residue") == "", "clean speech reports no residue"
        assert any(e.get("ev") == "result" and e.get("tag") == "usage" for e in evs_res)
        stored = [m for m in w_res.messages if m.get("role") == "assistant"]
        assert stored[0]["content"][0]["text"] == junk, "the stored message must not be rewritten"
        assert (w_res.log[-2] if len(w_res.log) > 1 else w_res.log[-1]).get("residue") == "lousy?"
        req = complete_ev.get("request") or {}
        resp = complete_ev.get("response") or {}
        assert req.get("model") or req.get("messages") is not None
        assert "opaque-secret" not in str(resp)
        data = ""
        for block in (resp.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "redacted_thinking":
                data = str(block.get("data") or "")
        assert data == "[redacted]" or data == ""

        # complete.spans is the kernel's dispatch verdict, consumed by the
        # TUI's turn-end reconcile: UTF-8 byte offsets into the final speech.
        # The multibyte char ahead of the call catches an emitter that ships
        # scan_spans' char offsets unconverted -- slicing the encoded speech
        # must give back exactly the dispatched call, and each result event
        # must name which span it came from.
        span_speech = 'héllo <exec op="bash">echo spanned</exec> done'

        def spans_complete(_model, _system, _messages, _max_tokens, _c=[0]):
            _c[0] += 1
            text = span_speech if _c[0] == 1 else "all prose"
            return {"content": [{"type": "text", "text": text}], "usage": {}}

        w_sp = new_world(cwd, state_path=cwd / "harness-spans.json", ns={})
        w_sp.complete_fn = spans_complete
        evs_sp: list[dict] = []
        _run(w_sp, "hi", quiet=True, on_event=lambda e: evs_sp.append(e))
        completes = [e for e in evs_sp if e.get("ev") == "complete"]
        ((a, z),) = completes[0]["spans"]
        sliced = span_speech.encode("utf-8")[a:z].decode("utf-8")
        assert sliced == '<exec op="bash">echo spanned</exec>', sliced
        for phase in ("start", "done"):
            (r,) = [e for e in evs_sp if e.get("ev") == "result" and e.get("phase") == phase]
            assert r.get("span_idx") == 0, r
        assert completes[1]["spans"] == [], "pure prose must advertise no spans"

        # The edit result event carries the edit site: the 1-based line of the
        # unique match, located by apply_edit at write time. The TUI paints the
        # diff there from the event alone -- it no longer reads the file back,
        # so a kernel that stops sending `line` un-anchors every edit card.
        # A refused edit has no edit site and must not send one.
        edited = cwd / "lined.txt"
        edited.write_text("one\ntwo\nfindme three\nfour\n", encoding="utf-8")

        def edit_complete(_model, _system, _messages, _max_tokens, _c=[0]):
            _c[0] += 1
            if _c[0] == 1:
                return {
                    "content": [{"type": "text", "text": '<workspace op="edit" path="lined.txt">findme three\n---\nfound three</workspace>'}],
                    "usage": {},
                }
            if _c[0] == 2:
                return {
                    "content": [{"type": "text", "text": '<workspace op="edit" path="lined.txt">absent\n---\nx</workspace>'}],
                    "usage": {},
                }
            return {"content": [{"type": "text", "text": "done"}], "usage": {}}

        w_el = new_world(cwd, state_path=cwd / "harness-editline.json", ns={})
        w_el.complete_fn = edit_complete
        evs_el: list[dict] = []
        _run(w_el, "edit it", quiet=True, on_event=lambda e: evs_el.append(e))
        good, bad = [e for e in evs_el if e.get("ev") == "result" and e.get("phase") == "done"]
        assert good.get("line") == 3, good
        assert edited.read_text(encoding="utf-8").splitlines()[2] == "found three"
        assert "line" not in bad, "a failed edit has no edit site to name"

        # The commit claim on the result event is the kernel's verdict, judged
        # from the command's own output: a real `git commit` through the real
        # loop puts `repo.committed` on the done event and the sha is the
        # repo's actual HEAD; a commit that fails (nothing staged) claims
        # nothing, however git-shaped the command text is. The TUI's work row
        # paints "committed <sha>" from this field alone.
        import subprocess as _sp
        repo = cwd / "gitrepo"
        repo.mkdir()
        _sp.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        (repo / "c.txt").write_text("v1\n", encoding="utf-8")
        _git_commit = "git -c user.name=d -c user.email=d@x -c commit.gpgsign=false commit"

        def commit_complete(_model, _system, _messages, _max_tokens, _c=[0]):
            _c[0] += 1
            if _c[0] == 1:
                text = f'<exec op="bash">git add c.txt && {_git_commit} -m claim</exec>'
            elif _c[0] == 2:
                text = f'<exec op="bash">{_git_commit} -m nothing</exec>'
            else:
                text = "done"
            return {"content": [{"type": "text", "text": text}], "usage": {}}

        w_gc = new_world(repo, state_path=cwd / "harness-commit.json", ns={})
        w_gc.complete_fn = commit_complete
        evs_gc: list[dict] = []
        _run(w_gc, "commit it", quiet=True, on_event=lambda e: evs_gc.append(e))
        made, refused = [e for e in evs_gc if e.get("ev") == "result" and e.get("phase") == "done"]
        claim = (made.get("repo") or {}).get("committed") or ""
        head_sha = _sp.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert len(claim) >= 7 and head_sha.startswith(claim), (claim, head_sha)
        assert "repo" not in refused, "a failed commit must not claim a sha"

        handshake = cwd / "go"
        chunks: list[str] = []

        def on_bash(text: str) -> None:
            chunks.append(text)
            if "ONE" in "".join(chunks) and not handshake.is_file():
                handshake.write_text("1", encoding="utf-8")

        bash_out = run_bash(
            "printf ONE; while [ ! -f go ]; do sleep 0.01; done; printf TWO",
            cwd,
            on_chunk=on_bash,
            timeout=3,
        )
        assert "ONE" in bash_out and "TWO" in bash_out
        assert any("ONE" in c for c in chunks)

        # A result too big for the transcript is not thrown away any more: the
        # whole output goes to a file and the result opens with that path plus
        # what to do with it. Driven through the real syscall and the real
        # gate below it -- run_bash for the source, format_result_message for
        # what the model actually reads -- so an unwired spill fails here.
        from desmos.const import RESULT_CAP as _cap
        from desmos.loop import RESULT_CLIP as _clip2, format_result_message as _fmt
        from desmos.spill import MARK as _mark, SPILL_DIR as _dir

        out_dir = cwd / _dir
        big = run_bash(f"seq 1 {_cap}", cwd, timeout=30)
        first = big.splitlines()[0]
        assert first.startswith("\u2026[") and _mark in first, first[:200]
        spilled = sorted(out_dir.glob("*.txt"))
        assert len(spilled) == 1, spilled
        assert first.split(_mark, 1)[1].split(" ", 1)[0] == str(spilled[0].relative_to(cwd))
        whole = spilled[0].read_text(encoding="utf-8")
        assert whole.splitlines()[-1] == str(_cap), "the file is not the whole output"
        assert len(whole) > len(big), "the spill file is no bigger than the result"
        # The gate below it must reuse that file, not write a second copy of
        # the head it was handed.
        msg = _fmt([(Block("exec", "seq", {"op": "bash"}), big)], cwd)
        assert _mark in msg, msg[:200]
        assert len(sorted(out_dir.glob("*.txt"))) == 1, "the same output spilled twice"
        assert len(msg) < _clip2 + 400, len(msg)
        # A result that reached the gate without passing an exec cap -- a
        # handler's return, a shell drain -- has to spill there instead.
        raw = "x" * (_clip2 + 500)
        msg2 = _fmt([(Block("exec", "", {"op": "shell"}), raw)], cwd)
        assert _mark in msg2, msg2[:200]
        after = sorted(out_dir.glob("*.txt"))
        assert len(after) == 2, after
        assert after[1].read_text(encoding="utf-8") == raw, "the gate spilled a trimmed copy"
        # Under the cap nothing is written and nothing is added.
        assert run_bash("printf hi", cwd) == "hi"
        assert len(sorted(out_dir.glob("*.txt"))) == 2, "a small result spilled"

        live_order: list[str] = []

        def live_complete(_model, _system, messages, _max_tokens):
            live_order.append("http")
            if any("<result" in (m.get("content") or "") for m in messages):
                return {"content": [{"type": "text", "text": "done"}], "usage": {}}
            return {
                "content": [{"type": "text", "text": "<python>1</python>\n<python>2</python>"}],
                "usage": {},
            }

        w_live = new_world(cwd, state_path=cwd / "harness-live.json", ns={})
        w_live.complete_fn = live_complete
        _run(w_live, "two calls", quiet=True, on_event=lambda e: live_order.append(str(e.get("ev"))))
        assert live_order.index("post") < live_order.index("http")
        assert live_order.index("http") < live_order.index("complete")
        assert live_order.count("result") == 4  # start+done per python tag

        evs_wire: list[dict] = []
        w_wire = new_world(cwd, state_path=cwd / "harness-wire.json", ns={"doc": "hello world"})

        def wire_complete(_model, _system, messages, _max_tokens):
            if any("<result" in (m.get("content") or "") for m in messages):
                return {"content": [{"type": "text", "text": "11"}], "usage": {}}
            return {"content": [{"type": "text", "text": '<exec op="python">len(doc)</exec>'}], "usage": {}}

        w_wire.complete_fn = wire_complete
        _run(w_wire, "how long is doc?", quiet=True, on_event=lambda e: evs_wire.append(e))
        res = next(
            e
            for e in evs_wire
            if e.get("ev") == "result" and e.get("phase") in {None, "done"}
        )
        assert res.get("tag") == "exec"
        assert "len(doc)" in (res.get("body") or "")
        assert "11" in (res.get("text") or "")
        assert any(e.get("ev") == "result" and e.get("phase") == "start" for e in evs_wire)
        stop_flag = {"go": False}
        calls = {"n": 0}

        def looping_complete(_model, _system, messages, _max_tokens):
            calls["n"] += 1
            if any("<result" in (m.get("content") or "") for m in messages):
                return {"content": [{"type": "text", "text": "more"}], "usage": {}}
            return {"content": [{"type": "text", "text": "<python>1</python>"}], "usage": {}}

        w_stop = new_world(cwd, state_path=cwd / "harness-stop.json", ns={})
        w_stop.complete_fn = looping_complete
        evs: list[str] = []

        def on_stop_ev(e: dict) -> None:
            evs.append(str(e.get("ev")))
            if e.get("ev") == "complete":
                stop_flag["go"] = True

        spoken = _run(
            w_stop,
            "keep going",
            quiet=True,
            on_event=on_stop_ev,
            should_stop=lambda: stop_flag["go"],
        )
        assert spoken
        assert "stopped" in evs
        assert calls["n"] == 1
        assert (cwd / "harness-stop.json").is_file()
        # Exactly one terminator, on every path. The TUI clears `running` on it
        # and drains the queue from it, so a step that ends in silence hangs
        # the pane on "stopping" and the queued message never fires. The path
        # that used to do that: a stop landing during a turn the model finished
        # on its own, which satisfied neither emitter's condition.
        assert [e for e in evs if e in ("done", "stopped")] == ["stopped"], evs
        for landed, want in ((True, "stopped"), (False, "done")):
            flag = {"go": False}

            def final_answer(_model, _system, _messages, _max_tokens, f=flag, l=landed):
                # No syscalls: the turn is done the moment it returns.
                f["go"] = l
                return {"content": [{"type": "text", "text": "all set."}], "usage": {}}

            w_term = new_world(cwd, state_path=None, ns={}, persist=False)
            w_term.complete_fn = final_answer
            terms: list[str] = []
            _run(
                w_term,
                "one shot",
                quiet=True,
                on_event=lambda e: terms.append(str(e.get("ev"))),
                should_stop=lambda f=flag: f["go"],
            )
            got = [e for e in terms if e in ("done", "stopped")]
            assert got == [want], f"stop landed={landed}: {got} in {terms}"
        assert w_stop.prior and w_stop.prior[-1]["prompt"] == "keep going"

        # step() and reset() are published into the kernel, so the model can
        # reach them from a <python> block mid-turn. A nested run appends its
        # whole exchange before the outer assistant message lands; reset()
        # clears the list the outer loop is appending to. Both are refused.
        w_re = new_world(cwd, state_path=None, persist=False, ns={})
        reentered: list[str] = []

        def reentrant(_m, _s, _msgs, _mt):
            try:
                w_re.ns["step"]("nested")
            except RuntimeError as exc:
                reentered.append(str(exc))
            try:
                w_re.ns["reset"]()
            except RuntimeError as exc:
                reentered.append(str(exc))
            return {"content": [{"type": "text", "text": "done"}], "usage": {}}

        w_re.complete_fn = reentrant
        _run(w_re, "try to re-enter", quiet=True)
        assert len(reentered) == 2, reentered
        assert "already running" in reentered[0], reentered
        assert "inside a running step" in reentered[1], reentered
        assert w_re.running is False, "the flag must clear even after a refusal"
        assert w_re.messages, "the outer step still committed its own transcript"

        # A turn that raises becomes a value. It used to unwind _run_turns,
        # leaving a user message with no assistant reply -- so the next step
        # appended a second consecutive user turn -- while the finally still
        # emitted "done", reporting success beside an unrelated error line.
        w_fail = new_world(cwd, state_path=None, persist=False, ns={})
        w_fail.complete_fn = lambda *_: (_ for _ in ()).throw(RuntimeError("wire died"))
        fail_evs: list[dict] = []
        _run(w_fail, "will fail", quiet=True, on_event=fail_evs.append)
        roles = [m["role"] for m in w_fail.messages]
        assert roles == ["user", "assistant"], roles
        assert "wire died" in str(w_fail.messages[-1]["content"]), w_fail.messages[-1]
        assert any(e.get("ev") == "error" and "wire died" in e.get("text", "") for e in fail_evs)
        assert w_fail.running is False

        # gpt-5.6-sol is tool-channel even with DESMOS_TOOL_SYSCALLS=0. XML in
        # speech is a handled error event, not a RuntimeError that ACP used to
        # wrap as JSON-RPC -32603. The tags named in the note must not run.
        w_xml = new_world(cwd, state_path=None, persist=False, ns={})
        w_xml.model = "gpt-5.6-sol"
        w_xml.complete_fn = lambda *_: {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '<session op="compact"></session> '
                        '<knowledge op="todo">open</knowledge>'
                    ),
                }
            ],
            "usage": {},
        }
        xml_evs: list[dict] = []
        _run(w_xml, "go", quiet=True, on_event=xml_evs.append)
        assert not any(
            "RuntimeError" in str(m.get("content")) for m in w_xml.messages
        ), w_xml.messages
        xml_err = [e for e in xml_evs if e.get("ev") == "error"]
        assert xml_err, [e.get("ev") for e in xml_evs]
        note = xml_err[0].get("text") or ""
        assert "emitted XML as speech" in note, note
        assert "session compact" in note, note
        assert "knowledge todo" in note, note
        assert "not dispatched" in note, note
        assert not any(
            e.get("ev") == "result" and e.get("tag") in {"session", "knowledge"}
            for e in xml_evs
        ), xml_evs
        assert any(e.get("ev") == "done" for e in xml_evs), [e.get("ev") for e in xml_evs]

        # The assistant turn is durable before its syscalls run, and results
        # come back even when the step stops mid-batch.
        w_ord = new_world(cwd, state_path=None, persist=False, ns={})
        halt = {"go": False}
        seen_mid: list[list[str]] = []

        def ordering(_m, _s, msgs, _mt):
            seen_mid.append([m["role"] for m in msgs])
            return {"content": [{"type": "text", "text": "<python>1+1</python>"}], "usage": {}}

        w_ord.complete_fn = ordering

        def stop_after_first(ev: dict) -> None:
            if ev.get("ev") == "result" and ev.get("phase") == "done":
                halt["go"] = True

        _run(w_ord, "batch then stop", quiet=True, on_event=stop_after_first, should_stop=lambda: halt["go"])
        tail = [m["role"] for m in w_ord.messages]
        # user prompt, assistant turn, its results, then the stop marker.
        assert tail == ["user", "assistant", "user"], tail
        assert "<result" in str(w_ord.messages[-1]["content"]), "a stop must not eat results that ran"
        assert "stopped by the user" in str(w_ord.messages[-1]["content"]), w_ord.messages[-1]

        # A backgrounded grandchild inherits stdout and can hold the pipe open
        # for as long as it likes. The unbounded read that waited for it made
        # `sleep 20 & echo started` with timeout=3 return after 20 seconds, and
        # for all 20 the deadline and should_stop were unreachable -- kernel,
        # bridge inbox and stop button wedged behind a process nobody awaited.
        import time as _time

        from desmos.exec import run_bash as _bash

        # The timeouts are one second and the grandchildren sleep twenty and
        # thirty: the regression this encodes is a twentyfold overrun, so it
        # does not need a wide window to be unmissable, and the four seconds
        # these two lines used to cost bought nothing.
        started = _time.monotonic()
        out = _bash("sleep 20 & echo started", cwd, timeout=1)
        took = _time.monotonic() - started
        assert took < 5, f"a backgrounded grandchild held the harness for {took:.1f}s"
        assert "started" in out, out
        # And a timeout takes the whole group with it, not just /bin/sh.
        started = _time.monotonic()
        out = _bash("sh -c 'sleep 30 & wait'", cwd, timeout=1)
        assert _time.monotonic() - started < 4, out
        assert "timeout after" in out, out

        # A reply the endpoint cut off is not a reply that finished. scan drops
        # an unterminated tag, so `<bash>ls` with no closer parses to nothing
        # and used to report a clean finish; stop_reason is the only difference.
        w_cut = new_world(cwd, state_path=None, persist=False, ns={})
        cut_calls = {"n": 0}

        def truncated(_m, _s, _msgs, _mt):
            cut_calls["n"] += 1
            if cut_calls["n"] == 1:
                return {
                    "content": [{"type": "text", "text": "I will run <bash>ls"}],
                    "stop_reason": "max_tokens",
                    "usage": {},
                }
            return {"content": [{"type": "text", "text": "done"}], "usage": {}}

        w_cut.complete_fn = truncated
        cut_evs: list[dict] = []
        _run(w_cut, "get cut off", quiet=True, on_event=cut_evs.append)
        assert cut_calls["n"] == 2, "a truncated turn must not end the step"
        assert any("cut short" in str(m.get("content")) for m in w_cut.messages), w_cut.messages
        assert any(e.get("ev") == "error" and "cut short" in e.get("text", "") for e in cut_evs)

        # A refusal is a terminal provider answer, not a truncated generation
        # to repost. Reposting the same prompt cannot recover it and used to
        # spend every remaining turn on the same empty refusal.
        w_refused = new_world(cwd, state_path=None, persist=False, ns={})
        refused_calls = {"n": 0}

        def refused(_m, _s, _msgs, _mt):
            refused_calls["n"] += 1
            return {"content": [], "stop_reason": "refusal", "usage": {}}

        w_refused.complete_fn = refused
        refused_evs: list[dict] = []
        refused_speech = _run(
            w_refused, "refuse this", quiet=True, max_turns=8, on_event=refused_evs.append
        )
        assert refused_calls["n"] == 1, refused_calls
        assert refused_speech.strip(), "an empty refusal must remain visible"
        assert [m["role"] for m in w_refused.messages] == ["user", "assistant"]
        assert not any(e.get("ev") == "error" for e in refused_evs), refused_evs

        # The two reasons this harness cuts a reply itself look identical to
        # the endpoint's: an empty, apparently-finished turn. They were not in
        # the set, so a generation guillotined the instant it started forging a
        # result block reported a clean finish and the model was never told.
        # They also need the opposite advice -- text that was stopped for going
        # wrong must not be resumed.
        for reason, want in (
            ("stop_sequence", "result block"),
            ("degenerate_repetition", "repetition loop"),
        ):
            w_s = new_world(cwd, state_path=None, persist=False, ns={})
            n_s = {"n": 0}

            def stopped(_m, _s, _msgs, _mt, _n=n_s, _r=reason):
                _n["n"] += 1
                if _n["n"] == 1:
                    return {
                        "content": [{"type": "text", "text": "here is what I found"}],
                        "stop_reason": _r,
                        "usage": {},
                    }
                return {"content": [{"type": "text", "text": "done"}], "usage": {}}

            w_s.complete_fn = stopped
            s_evs: list[dict] = []
            _run(w_s, "go wrong", quiet=True, on_event=s_evs.append)
            assert n_s["n"] == 2, f"{reason} must not end the step"
            notes = [str(m.get("content")) for m in w_s.messages if m.get("role") == "user"]
            assert any(want in x for x in notes), (reason, notes)
            assert any(e.get("ev") == "error" for e in s_evs), reason
            # never tell a reply that was stopped for going wrong to continue
            assert not any("continue from where" in x for x in notes), (reason, notes)

        # scan drops an opener whose closer never arrived, in silence, and a
        # turn that lost its only syscall then looks exactly like a turn that
        # chose not to call one. Anthropic stop sequences make that routine:
        # they cut generation at a line start anywhere in the reply, body
        # included, and end="TOKEN" cannot help because it is parsed here long
        # after the API stopped the stream.
        w_lost = new_world(cwd, state_path=None, persist=False, ns={})
        n_l = {"n": 0}

        def lost_tag(_m, _s, _msgs, _mt, _n=n_l):
            _n["n"] += 1
            if _n["n"] == 1:
                return {
                    "content": [{"type": "text", "text": "I will run <bash>ls"}],
                    "stop_reason": "end_turn",
                    "usage": {},
                }
            return {"content": [{"type": "text", "text": "done"}], "usage": {}}

        w_lost.complete_fn = lost_tag
        _run(w_lost, "drop a tag", quiet=True)
        assert n_l["n"] == 2, "a dropped opener must not read as a finished turn"
        lost_notes = [str(m.get("content")) for m in w_lost.messages if m.get("role") == "user"]
        assert any("bash (no closing tag)" in x for x in lost_notes), lost_notes

        # And the note goes after the results, never before: it explains what
        # did not run, which is nonsense ahead of the output of what did.
        w_mix = new_world(cwd, state_path=None, persist=False, ns={})
        n_m = {"n": 0}
        half = 'ok <exec op="bash">echo hi</' + 'exec> and now <exec op="python">print(1)'

        def mixed(_m, _s, _msgs, _mt, _n=n_m, _t=half):
            _n["n"] += 1
            if _n["n"] == 1:
                return {
                    "content": [{"type": "text", "text": _t}],
                    "stop_reason": "stop_sequence",
                    "usage": {},
                }
            return {"content": [{"type": "text", "text": "done"}], "usage": {}}

        w_mix.complete_fn = mixed
        _run(w_mix, "half a batch", quiet=True)
        # Only user-role messages: the assistant turn quotes both the command
        # and its own dropped tag, so matching on text alone always finds an
        # earlier index and the ordering assertion can never fail.
        rows = [
            (i, str(m.get("content")))
            for i, m in enumerate(w_mix.messages)
            if m.get("role") == "user"
        ]
        note_at = [i for i, x in rows if "exec (no closing tag)" in x]
        res_at = [i for i, x in rows if "hi" in x]
        assert note_at and res_at, (note_at, res_at, rows)
        assert min(note_at) >= max(res_at), (note_at, res_at)
        if min(note_at) == max(res_at):
            joined = dict(rows)[min(note_at)]
            assert joined.index("no closing tag") > joined.index("hi"), joined
        assert any("result block" in x for _, x in rows), rows

        # Hitting the cap is not finishing either.
        w_cap = new_world(cwd, state_path=None, persist=False, ns={})
        w_cap.complete_fn = lambda *_: {
            "content": [{"type": "text", "text": "<python>1</python>"}],
            "usage": {},
        }
        cap_evs: list[dict] = []
        _run(w_cap, "loop forever", quiet=True, max_turns=3, on_event=cap_evs.append)
        assert any("max_turns" in str(m.get("content")) for m in w_cap.messages), "the cap must be said"
        assert any("max_turns" in e.get("text", "") for e in cap_evs if e.get("ev") == "error")

        # ...but only when a caller asked for one. The default cut every long
        # task off at 32 turns mid-work, which is not a budget: it bounds
        # nothing that bills. Drive the real loop past the old ceiling and
        # assert it ran to the model's own stop.
        w_free = new_world(cwd, state_path=None, persist=False, ns={})
        free_turns = {"n": 0}

        def _free(*_a, **_k):
            free_turns["n"] += 1
            body = "<python>1</python>" if free_turns["n"] <= 40 else "finished"
            return {"content": [{"type": "text", "text": body}], "usage": {}}

        w_free.complete_fn = _free
        free_evs: list[dict] = []
        _run(w_free, "keep going", quiet=True, on_event=free_evs.append)
        assert free_turns["n"] == 41, f"the loop stopped itself at {free_turns['n']} turns"
        assert not any("max_turns" in str(m.get("content")) for m in w_free.messages), "capped anyway"
        assert free_evs[-1].get("ev") == "done", free_evs[-1]

        # Neither is running out of money. A child gets a token ceiling from
        # its contract; the root loop had only max_turns, so four huge turns
        # were unbounded in the unit that bills. Drive the real entry point --
        # run_turns with max_total_tokens -- and assert it stopped early, said
        # why in the transcript, and reported `stopped` rather than `done`.
        w_bud = new_world(cwd, state_path=None, persist=False, ns={})
        bud_calls = {"n": 0}

        def spendy(*_a, **_k):
            bud_calls["n"] += 1
            return {
                "content": [{"type": "text", "text": "<bash>echo x</bash>"}],
                "usage": {"input_tokens": 400, "output_tokens": 200},
            }

        w_bud.complete_fn = spendy
        bud_evs: list[dict] = []
        _run(w_bud, "spend", quiet=True, max_turns=8, max_total_tokens=1000, on_event=bud_evs.append)
        assert bud_calls["n"] == 2, f"600/turn against a 1000 ceiling must stop at 2, got {bud_calls['n']}"
        assert any(
            "token budget of 1000" in str(m.get("content")) for m in w_bud.messages
        ), "the budget stop must be in the transcript"
        stops = [e for e in bud_evs if e.get("ev") == "stopped"]
        assert stops and "token budget" in stops[-1].get("text", ""), bud_evs[-3:]
        assert not any(e.get("ev") == "done" for e in bud_evs), "a budget stop is not a clean finish"

        # And the ceiling is per step, not per session: a second step on the
        # same world starts its count at zero rather than inheriting the spend.
        bud_calls["n"] = 0
        _run(w_bud, "spend again", quiet=True, max_turns=8, max_total_tokens=1000)
        assert bud_calls["n"] == 2, f"the count must restart per step, got {bud_calls['n']}"

        # <bash> is one subprocess per call, so nothing it does survives. The
        # persistent shell is the other half: state carries, exit codes come
        # back, and a program that asks a question can be answered -- which is
        # the case a one-shot subprocess cannot express at all.
        from desmos.shell import INITIAL_WINDOW as _fg_window
        from desmos.shell import close_all as _close_shells, head_tail, strip_ansi
        from desmos import pending as _pending

        assert _fg_window <= 1.0, "the first look is snappy, not a task estimate"

        # Every dispatch below waits PROMPT_IDLE of silence after the prompt
        # before it answers, and there are twenty of them: half of this group's
        # wall clock was that one constant, found with `desmos check --profile`.
        # The product defaults are asserted here and then pinned small for the
        # state-carrying assertions, which prove what a shell remembers rather
        # than how long it listens. The timing repros further down are
        # untouched: they turn on INITIAL_WINDOW, which is left alone.
        from desmos.kernel import shell as _shell_mod

        assert (_shell_mod.PROMPT_IDLE, _shell_mod.QUIET) == (0.3, 0.20), (
            _shell_mod.PROMPT_IDLE,
            _shell_mod.QUIET,
        )
        _idle_was, _quiet_was = _shell_mod.PROMPT_IDLE, _shell_mod.QUIET
        _shell_mod.PROMPT_IDLE, _shell_mod.QUIET = 0.05, 0.05

        check_shell_peek_history(cwd)
        check_shell_close_is_idempotent(cwd)

        w_sh = new_world(cwd, state_path=None, persist=False, ns={})
        try:
            def sh(body: str, **attrs: str) -> str:
                return dispatch(w_sh, Block("exec", body, {**attrs, "op": "shell"}))

            assert sh("cd /etc && pwd").strip() == "/etc"
            assert sh("pwd").strip() == "/etc", "a persistent shell keeps its cwd"
            assert sh("export DZ=kept; echo ok").strip() == "ok"
            assert sh("echo $DZ").strip() == "kept", "and its environment"
            assert sh("python3 -c 'import sys;print(sys.stdin.isatty())'").strip() == "True"
            assert sh("printf '%s:%s' \"$GIT_PAGER\" \"$PAGER\"").strip() == "cat:cat", (
                "agent PTYs must disable pagers or a quiet `less` looks like a stuck monitor"
            )
            failed = sh("ls /definitely-not-here")
            assert "[exit " in failed and "[exit 0]" not in failed, failed
            # A second session is a second machine as far as state goes.
            assert sh("pwd", id="other").strip() != "/etc"
            # The interactive round trip.
            asked = sh("python3 -c \"n=input('who? ');print('hi '+n)\"")
            assert "who?" in asked and "waiting for input" in asked, asked
            assert sh("desmos").strip() == "hi desmos", "the answer reached the waiting program"
            assert sh("echo recovered").strip() == "recovered", "and the shell came back"
            # A command that outlives the first look is neither reported as
            # finished nor handed back as a timeout for the model to poll. One
            # monitor owns the pty from here and the step resumes when the work
            # actually lands -- the model never picks a read window at all.
            slow = sh("sleep 2; echo late-line", id="slow")
            assert "monitored" in slow and "(no output)" not in slow.split("\n")[-1], slow
            assert _pending.count(w_sh) == 1, "a long command leaves exactly one monitor"
            landed = _pending.wait_next(w_sh, timeout=30)
            assert landed and "late-line" in landed[0].output, landed
            assert _pending.count(w_sh) == 0, "and it is delivered once"
            assert sh("echo reusable", id="slow").strip() == "reusable", "the shell outlives it"
            # Interrupt targets the tty's foreground process group, not bash's
            # own group, and the monitor remains the only PTY reader.
            cancelled = sh("sleep 30", id="cancel")
            assert "monitored" in cancelled, cancelled
            sent = sh("", id="cancel", interrupt="1")
            assert "interrupt sent" in sent, sent
            landed = _pending.wait_next(w_sh, timeout=10)
            assert landed and "exit" in landed[0].output, landed
            assert sh("echo after-interrupt", id="cancel").strip() == "after-interrupt"
            # A multi-line block runs whole. It used to be written raw to the
            # tty, so a line queued behind a still-running one never reached
            # bash at all while the transcript reported the block as run.
            ran = cwd / "second-line-ran"
            ran.unlink(missing_ok=True)
            block = sh(f"sleep 1\ntouch {ran}", id="multi")
            assert "monitored" in block, block
            assert _pending.wait_next(w_sh, timeout=30), "the block must finish"
            assert ran.exists(), "every line of a block must reach the shell"
            ran.unlink(missing_ok=True)
            assert w_sh.shells, "sessions live on the world"
        finally:
            _close_shells(w_sh)
            _shell_mod.PROMPT_IDLE, _shell_mod.QUIET = _idle_was, _quiet_was
        assert not w_sh.shells

        # A body that contains its own closing tag used to be cut there, and the
        # remainder leaked into speech -- where a complete tag pair in the residue
        # is dispatched for real. That is unfixable by heuristic: two calls and one
        # body holding a closer are byte-identical. An explicit end token makes the
        # body opaque, which is the only thing that makes editing this codebase safe.
        from desmos.scan import scan as _scan

        _tok = _scan('<python end="K">print("</python>")</python:K>')
        assert len(_tok) == 1 and _tok[0].tag == 'python', _tok
        assert _tok[0].body == 'print("</python>")', _tok[0].body
        assert 'end' not in _tok[0].attrs, 'the token must not reach the handler'
        # Without the token the same text still ends at the bare closer.
        _bare = _scan('<python>print(1)</python> tail')
        assert _bare[0].body == 'print(1)', _bare[0].body
        # Other attributes survive alongside it, and a missing custom closer is an
        # unterminated call rather than a silent fallback to the bare one.
        _attr = _scan('<edit path="a.py" end="Z">o</edit>n</edit:Z>')
        assert _attr[0].attrs == {'path': 'a.py'} and 'o</edit>n' == _attr[0].body, _attr
        assert _scan('<python end="Q">x</python>') == [], 'a missing custom closer must not fall back'
        assert _scan('<python end="bad token">x</python>') == [], 'an unusable token is not a bare closer'

        # Oversized output keeps both ends: the head says what it was doing,
        # the tail says how it ended.
        big = ("a" * 400 + "\n").encode() * 100
        trimmed = head_tail(big, 600)
        assert trimmed.startswith("aaa") and "omitted" in trimmed and len(trimmed) < 800
        assert strip_ansi("\x1b[?2004hx\x1b[0m\r\ny") == "x\ny"

        # A traceback is the last thing a failing script prints. Head-clipping
        # a noisy failure returned progress and no error.
        from desmos.scan import clip as _clip

        noisy = "chatter\n" * 4000 + "ZeroDivisionError: division by zero"
        assert "ZeroDivisionError" in _clip(noisy, 600, keep="tail")
        assert "ZeroDivisionError" not in _clip(noisy, 600)
        assert "chatter" in _clip(noisy, 600), "the head is still right for ordinary output"
        assert len(_clip(noisy, 600, keep="tail")) <= 600 + 40
        assert _clip("short", 600, keep="tail") == "short"
        boom = dispatch(world, Block("exec", "print('x' * 9000)\n1/0", {"op": "python"}))
        assert "ZeroDivisionError" in boom, boom[:200]

        # A warning raised while parsing the body must land in the result, not
        # on the terminal. ast.parse used to run outside the redirect, so the
        # bytes went to the real fd 2 and painted over the TUI's input box.
        # Drive dispatch, and watch the real stderr for a leak.
        import contextlib as _ctx
        import io as _sio
        import warnings as _warn

        leak = _sio.StringIO()
        with _warn.catch_warnings(), _ctx.redirect_stderr(leak):
            _warn.simplefilter("always")
            warned = dispatch(world, Block("exec", "x = '\\|'\n'done'", {"op": "python"}))
        assert "SyntaxWarning" in warned, warned[:200]
        assert leak.getvalue() == "", f"a parse warning escaped to the terminal: {leak.getvalue()!r}"

        w_usage.ns["reset"]()
        assert w_usage.messages == []

        py = cwd / "broke.py"
        py.write_text("x = 1\n")
        bad = dispatch(world, Block("workspace", "x = 1\n---\nx =\n", {"op": "edit", "path": str(py)}))
        assert "SyntaxError" in bad, bad
        assert py.read_text() == "x = 1\n", "a file that would not compile was written anyway"
        assert py.read_text(encoding="utf-8") == "x = 1\n"

        # Friction shadow observer (kernel/friction.py): counters on the World
        # append a one-line nudge through real dispatch, at most once per
        # threshold crossing, with a cooldown. Zero extra API calls.
        w_fr = new_world(cwd, state_path=None, persist=False, ns={})
        same = "40 + 2"
        fr1 = dispatch(w_fr, Block("exec", same, {"op": "python"}))
        fr2 = dispatch(w_fr, Block("exec", same, {"op": "python"}))
        fr3 = dispatch(w_fr, Block("exec", same, {"op": "python"}))
        fr4 = dispatch(w_fr, Block("exec", same, {"op": "python"}))
        assert fr1 == "42" and fr2 == "42", (fr1, fr2)
        assert "growing a tool" in fr3, fr3
        assert fr3.splitlines()[0] == "42", fr3
        assert "[friction]" not in fr4, "cooldown failed: the fourth repeat nudged again"

        # Two consecutive tracebacks on one tag nudge exactly once.
        w_ff = new_world(cwd, state_path=None, persist=False, ns={})
        ff1 = dispatch(w_ff, Block("exec", "raise RuntimeError('fr one')", {"op": "python"}))
        ff2 = dispatch(w_ff, Block("exec", "raise RuntimeError('fr two')", {"op": "python"}))
        ff3 = dispatch(w_ff, Block("exec", "raise RuntimeError('fr three')", {"op": "python"}))
        assert "[friction]" not in ff1, ff1
        assert "failed 2x in a row" in ff2, ff2
        assert "[friction]" not in ff3, "a failure nudge repeated inside its cooldown"

        # A clean run never nudges.
        w_fc = new_world(cwd, state_path=None, persist=False, ns={})
        for clean_body in ("1 + 1", "2 + 2", "3 + 3"):
            clean_out = dispatch(w_fc, Block("exec", clean_body, {"op": "python"}))
            assert "[friction]" not in clean_out, clean_out

        # docs/identity.md, driven: the in-memory row survives <reload_sdk/>
        # (ns, notes, messages), and <rollback> restores notes without touching
        # memory records or the transcript. Last before attach: reload_sdk
        # reimports every desmos module, so nothing after this may hold state
        # in one.
        from desmos.loop import reload_sdk as _reload_sdk
        from desmos.state.generations import rollback as _gen_rollback
        from desmos.state.memory import memory_root as _mem_root, records_path as _rec_path, remember as _remember

        id_dir = cwd / "idrow"
        id_dir.mkdir()
        w_id = new_world(id_dir, state_path=id_dir / "harness.sqlite3")
        w_id.ns["keepsake"] = 42
        w_id.messages.append({"role": "user", "content": "kept line"})
        w_id.messages.append({"role": "assistant", "content": "kept reply"})
        dispatch(w_id, Block("knowledge", "identity doctrine", {"op": "system", "name": "identity-note"}))
        _remember(w_id, "the sky is mauve")
        rec_file = _rec_path(_mem_root(w_id))
        rec_before = rec_file.read_bytes()
        assert b"the sky is mauve" in rec_before

        # A live World predating a dataclass field keeps its old __dict__.
        # Reload must supply new defaults before any reloaded code reads it.
        del w_id.injections
        del w_id.steers
        _reload_sdk(w_id)
        from desmos.kernel.types import World as _CurrentWorld

        assert isinstance(w_id, _CurrentWorld), "reload_sdk left the live World on the old class"
        assert w_id.injections == {} and w_id.steers == [], "reload_sdk missed new World defaults"
        assert w_id.ns.get("keepsake") == 42, "reload_sdk wiped ns"
        assert w_id.notes.get("identity-note") == "identity doctrine", "reload_sdk wiped notes"
        assert [m["content"] for m in w_id.messages] == ["kept line", "kept reply"], (
            "reload_sdk rewrote the transcript"
        )

        # Generation 1 was snapshotted at world birth, before the note existed:
        # rolling back must drop the note (proof rollback ran) and nothing else.
        rolled = _gen_rollback(w_id, 1)
        assert rolled == "rolled back to generation 1", rolled
        assert "identity-note" not in w_id.notes, "rollback did not restore notes"
        assert [m["content"] for m in w_id.messages] == ["kept line", "kept reply"], (
            "rollback touched the transcript"
        )
        assert rec_file.read_bytes() == rec_before, "rollback rewrote memory records"

        _check_anchors()

        _check_profiler()

        _check_ambient_reload()

        try:
            from IPython.core.interactiveshell import InteractiveShell
        except ImportError:
            print("attach check skipped (no IPython)")
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
        assert dispatch(w4, Block("exec", "len(doc)", {"op": "python"})) == "11"


def _check_ambient_reload() -> None:
    """A tool written between turns dispatches on the next turn with no reload;
    an unchanged tree does not re-run discovery; forced reload still rebuilds."""
    import os
    import tempfile

    from desmos.kernel.catalog import catalog
    from desmos.kernel.loop import install_resources, reload as _reload
    from desmos.loop import run_turns as _run

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / ".git").mkdir()  # stop the root walk at the tempdir
        ext_dir = cwd / ".desmos" / "extensions"
        counter = cwd / "ext-load-count.txt"  # outside every root on purpose

        world = new_world(cwd, state_path=None, persist=False, ns={})
        assert "greet" not in world.tools

        ext_src = (
            "from pathlib import Path\n"
            f"COUNTER = Path({str(counter)!r})\n"
            "def load(api):\n"
            "    n = int(COUNTER.read_text()) + 1 if COUNTER.exists() else 1\n"
            "    COUNTER.write_text(str(n))\n"
            "    api.tool('greet', 'greets the body',\n"
            "             lambda body='', **a: 'greeted:' + body.strip())\n"
        )
        write_code = (
            "import pathlib\n"
            f"d = pathlib.Path({str(ext_dir)!r})\n"
            "d.mkdir(parents=True, exist_ok=True)\n"
            f"(d / 'greeter.py').write_text({ext_src!r})\n"
            "print('written')"
        )

        calls = {"n": 0}

        def fake(model, system, messages, max_tokens):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"content": [{"type": "text", "text": f'<exec op="python">{write_code}</exec>'}], "usage": {}}
            if calls["n"] == 2:
                # no reload was issued between the write and this tag
                return {"content": [{"type": "text", "text": "<greet>ping</greet>"}], "usage": {}}
            return {"content": [{"type": "text", "text": "ambient done"}], "usage": {}}

        world.complete_fn = fake
        out = _run(world, "grow a greeter", quiet=True)
        assert out.strip() == "ambient done"
        assert "greet" in world.tools, "ambient reload did not install the new tool"
        joined = "\n".join(str(m.get("content")) for m in world.messages if m.get("role") == "user")
        assert "greeted:ping" in joined, f"greet did not dispatch without reload: {joined!r}"
        assert "<greet>" in catalog(world), "catalog does not reflect the rebuilt registry"

        # Unchanged tree: the turn-boundary sweep skips discovery entirely.
        install_resources(world)  # absorb any bytecode-cache churn from the import
        n1 = int(counter.read_text())
        install_resources(world)
        install_resources(world)
        assert int(counter.read_text()) == n1, "unchanged tree re-ran discovery"

        # Touched tree: the sweep sees the new mtime and rediscovers once.
        target = ext_dir / "greeter.py"
        st = target.stat()
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        install_resources(world)
        assert int(counter.read_text()) == n1 + 1, "changed tree did not rediscover"

        # Explicit reload stays and forces a rebuild even with nothing changed.
        n2 = int(counter.read_text())
        msg = _reload(world)
        assert int(counter.read_text()) == n2 + 1, "harness reload no longer forces a rebuild"
        assert msg.startswith("reloaded"), msg


def _check_anchors() -> None:
    """Anchors survive a simulated fold; the 75% anchor nudge fires once per climb."""
    import tempfile

    from desmos.kernel import catalog, handoff, prices
    from desmos.loop import new_world

    with tempfile.TemporaryDirectory() as tmp:
        w = new_world(Path(tmp), state_path=None, persist=False, ns={})

        out = dispatch(w, Block("knowledge", "fact one\nfact two", {"op": "anchor"}))
        assert "2 line(s)" in out, out
        assert "fact one" in catalog.volatile(w), "anchors missing from the volatile block"

        # Bounds: line count and per-line length are both capped.
        many = "\n".join(f"line {i} " + "x" * 500 for i in range(20))
        out = dispatch(w, Block("knowledge", many, {"op": "anchor"}))
        rows = w.notes[handoff.ANCHORS].splitlines()
        assert len(rows) == handoff.MAX_ANCHORS, out
        assert all(len(r) <= handoff.ANCHOR_CHARS for r in rows), "anchor line over cap"

        # Simulated fold: the transcript is replaced by a summary someone else
        # wrote; anchors live in notes, so the recomposed tail still carries
        # them verbatim.
        dispatch(w, Block("knowledge", "objective: ship the rail", {"op": "anchor"}))
        w.messages[:] = [{"role": "user", "content": "[server-written summary]"}]
        handoff.after_fold(w, len(w.messages), "summary")
        assert "objective: ship the rail" in catalog.volatile(w), (
            "anchors did not survive the fold"
        )

        # The nudge fires once at SOFT, not every turn above it.
        win = prices.window(w.model)
        w.log.append({"usage": {"input_tokens": int(win * 0.8)}})
        assert handoff.watch(w), "watch did not arm above SOFT"
        assert handoff.NUDGE in w.injections, "anchor nudge missing at SOFT"
        catalog.expire(w)  # the turn that carried it renders; turns=1 falls out
        assert handoff.NUDGE not in w.injections
        handoff.watch(w)
        assert handoff.NUDGE not in w.injections, "anchor nudge re-fired while still above SOFT"

        # Dropping under CLEAR re-arms it for the next climb.
        w.log.append({"usage": {"input_tokens": int(win * 0.3)}})
        assert not handoff.watch(w), "watch stayed armed under CLEAR"
        w.log.append({"usage": {"input_tokens": int(win * 0.8)}})
        handoff.watch(w)
        assert handoff.NUDGE in w.injections, "anchor nudge did not re-arm after a clear"


def _check_profiler() -> None:
    """`check --profile` blames the check line that spends the seconds.

    Driven through runner.run, because what broke twice was the wiring, never
    the arithmetic: a version that wrapped select/sleep reported nothing at
    all for a 14.7s group, and a version that kept its tallies in module
    globals had them emptied by the SDK reload this very group performs.

    So: a synthetic group that spends a known 0.3s at a known line inside
    desmos/checks/ (compile() sets co_filename, which is what _blame reads),
    run through the real entry point, and the report must name that line.
    Then a reload of the profiler mid-sample, which must not lose the count.
    """
    import contextlib
    import importlib
    import io
    import sys as _sys
    import time
    import types as _types

    from desmos.checks import profile as prof_mod
    from desmos.checks import runner

    probe_path = str(Path(prof_mod.__file__).resolve().parent / "_probe.py")
    probe_src = "import time\n\n\ndef check():\n    time.sleep(0.3)\n"
    probe = _types.ModuleType("desmos.checks._probe")
    probe.__file__ = probe_path
    exec(compile(probe_src, probe_path, "exec"), probe.__dict__)

    old_groups = runner.GROUPS
    _sys.modules["desmos.checks._probe"] = probe
    runner.GROUPS = old_groups + ("_probe",)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = runner.run(only="_probe", profile=True)
    finally:
        runner.GROUPS = old_groups
        _sys.modules.pop("desmos.checks._probe", None)
    out = buf.getvalue()
    assert code == 0, out
    assert "[profile]" in out, "check --profile printed no report:\n" + out
    rows = [ln for ln in out.splitlines() if ln.startswith("  ") and "s  " in ln]
    assert rows, "profile report has no attributed lines:\n" + out
    assert "_probe.py:5" in rows[0], (
        "the 0.3s sleep was blamed on " + rows[0].strip() + ", not the probe "
        "line that spent it:\n" + out
    )

    # The tallies live on the sampler object precisely so that a reload of the
    # profiler partway through a run cannot rebind them to empty dicts.
    with prof_mod.profiling(interval=0.004) as sampler:
        time.sleep(0.08)
        before = sum(sampler.samples.values())
        # Counted off the sampler, not off the module: a global tally is not
        # empty after a reload, it is a *fresh* one, so "did anything get
        # counted" passes while every sample before the reload is gone.
        assert before >= 5, "the tallies are not on the sampler object: " + repr(before)
        importlib.reload(prof_mod)
        time.sleep(0.08)
    after = sum(sampler.samples.values())
    assert after >= before + 5, (
        "a mid-run reload of the profiler lost its earlier tallies: "
        + repr((before, after))
    )
    assert sampler.sites() and sampler.wall >= 0.14, (sampler.wall, sampler.sites()[:2])
