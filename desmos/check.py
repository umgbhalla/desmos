from __future__ import annotations

from pathlib import Path

from desmos.dispatch import dispatch
from desmos.generations import evolve, gen_dir, rollback
from desmos.loop import attach, bind_step, new_world
from desmos.catalog import header, ns_names, system_prompt
from desmos.scan import scan
from desmos.complete import INTERLEAVED_BETA, text_of
from desmos.types import Block


def _fake_id_token(*, plan: str, account: str, ttl: int = 3600) -> str:
    """A JWT-shaped string carrying the claims auth.py reads. Unsigned on purpose."""
    import base64 as _b64
    import json as _json
    import time as _time

    def seg(obj: dict) -> str:
        return _b64.urlsafe_b64encode(_json.dumps(obj).encode()).decode().rstrip("=")

    head = seg({"alg": "none", "typ": "JWT"})
    body = seg(
        {
            "exp": int(_time.time()) + ttl,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account,
                "chatgpt_plan_type": plan,
            },
        }
    )
    return f"{head}.{body}.sig-not-checked"


def _check_path_deps_tracked() -> None:
    """Every `path = ` dep in the root Cargo.toml is committed.

    vendor/grok-build is in the repo so a clone builds without fetching
    anything. That guarantee is one .gitignore line from being false, and it
    fails silently: `cargo build` works here because the files are on disk, and
    breaks only for whoever clones next. It already happened once -- a bare
    `build/` in a global gitignore swallowed crates/build/xai-proto-build.

    Asks git what is tracked rather than what exists, because the whole failure
    mode is a file that exists locally and is not in the repo.
    """
    import re
    import subprocess

    root = Path(__file__).resolve().parent.parent
    manifest = root / "Cargo.toml"
    if not manifest.exists() or not (root / ".git").exists():
        return

    deps = {m for m in re.findall(r'path\s*=\s*"([^"]+)"', manifest.read_text())}
    missing = []
    for rel in sorted(deps):
        target = (root / rel / "Cargo.toml").resolve()
        if not target.exists():
            missing.append(f"{rel}/Cargo.toml does not exist")
            continue
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", str(target)],
            capture_output=True, check=False,
        )
        if tracked.returncode != 0:
            why = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "-v", str(target)],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
            missing.append(f"{rel} is not committed" + (f" ({why})" if why else ""))

    assert not missing, (
        "root Cargo.toml points at crates a fresh clone will not have:\n  "
        + "\n  ".join(missing)
    )


def _check_vendor_patch() -> None:
    """The vendored pager still carries our DESMOS_ACP branch.

    vendor/grok-build is committed, so this is not about a missing clone. It
    is about a sync: pulling upstream over the pager drops the branch, the
    crate still compiles, and `--grok` silently runs grok's own in-process
    agent instead of `python -m desmos acp`. Nothing else in the build says a
    word about it, so assert the two halves of the branch are present.
    """
    pager = (
        Path(__file__).resolve().parent.parent
        / "vendor/grok-build/crates/codegen/xai-grok-pager/src/acp"
    )
    if not pager.is_dir():
        return

    for name, needle in (
        ("mod.rs", 'std::env::var("DESMOS_ACP")'),
        ("spawn.rs", "pub async fn spawn_stdio_acp"),
    ):
        src = (pager / name).read_text()
        assert needle in src, (
            f"vendor/grok-build pager acp/{name} lost {needle!r} -- a sync "
            f"overwrote our DESMOS_ACP branch, so --grok now runs grok's agent "
            f"instead of desmos. Restore it before shipping."
        )


def self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        world = new_world(cwd, state_path=cwd / "harness.json")
        from desmos.complete import cached_payload
        from desmos.const import ABI

        prompt = system_prompt(world)

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

        from desmos.cli import (
            _repo_root,
            _tui_binary,
            _tui_build_cmd,
            _tui_build_env,
            _tui_stabilize_fingerprints,
            _tui_stale,
            _tui_watch_roots,
        )

        roots = _tui_watch_roots(cwd)
        assert any("desmos-tui" in str(p) for p in roots)
        assert not any("vendor" in str(p) for p in roots)
        cargo_cmd = _tui_build_cmd("cargo")
        assert cargo_cmd == ["cargo", "build", "-p", "desmos-tui", "--release"]
        assert _tui_build_cmd("cargo", release=False) == [
            "cargo",
            "build",
            "-p",
            "desmos-tui",
        ]
        assert "--quiet" not in cargo_cmd
        launch_env = _tui_build_env({"PATH": "/bin", "HOME": str(cwd)})
        assert "CARGO_TERM_QUIET" not in launch_env
        assert "RUSTFLAGS" not in launch_env
        assert launch_env["RUSTUP_TOOLCHAIN"] == "1.97.1"
        protoc = Path(launch_env["PROTOC"])
        assert protoc.is_file() and protoc.is_absolute()
        for head in _tui_stabilize_fingerprints(_repo_root()):
            assert head.is_file(), head
        kept = _tui_build_env({"RUSTFLAGS": "-C debuginfo=1", "CARGO_TERM_QUIET": "true"})
        assert kept["RUSTFLAGS"] == "-C debuginfo=1"
        assert "CARGO_TERM_QUIET" not in kept
        assert "float_literal_f32_fallback" not in kept.get("RUSTFLAGS", "")
        assert kept["PROTOC"] == str(protoc)
        crate = cwd / "crates" / "desmos-tui"
        crate.mkdir(parents=True)
        src = crate / "main.rs"
        src.write_text("fn main() {}\n", encoding="utf-8")
        fake_bin = cwd / "target" / "release" / "desmos-tui"
        fake_bin.parent.mkdir(parents=True)
        fake_bin.write_bytes(b"bin")
        older = src.stat().st_mtime - 30
        import os as _os

        # No stamp yet: fall back to mtime, so a source newer than the binary
        # is stale and an older one is adopted (and stamped).
        _os.utime(fake_bin, (older, older))
        assert _tui_stale(cwd, fake_bin) is True
        assert _tui_binary(cwd) is None
        _os.utime(src, (older - 30, older - 30))
        assert _tui_stale(cwd, fake_bin) is False
        assert _tui_binary(cwd) == fake_bin
        # Stamped now: a touch is not a rebuild, changed bytes are.
        src.touch()
        assert _tui_stale(cwd, fake_bin) is False
        src.write_text("fn main() { let _ = 1; }\n", encoding="utf-8")
        assert _tui_stale(cwd, fake_bin) is True
        src.write_text("fn main() {}\n", encoding="utf-8")
        assert _tui_stale(cwd, fake_bin) is False
        # Two dialects, opposite directions. A conciseness instruction cuts
        # Opus 5's length ~20%; the same words make GPT-5.6 return a shorter
        # artifact instead of a shorter explanation. Averaging them is wrong
        # for both, so assert they actually differ.
        from desmos.dialect import dialect, family

        assert family("claude-opus-5") == "anthropic"
        assert family("gpt-5.6-sol") == "openai"
        assert family("codex-mini") == "openai"
        assert family("") == "anthropic", "unknown model falls back to anthropic"
        assert dialect("claude-opus-5") != dialect("gpt-5.6-sol"), "one block cannot serve both"
        assert "implementation and verification" in dialect("gpt-5.6-sol"), (
            "the OpenAI lane can stop after inspection on an implementation request"
        )

        # The prompt states facts about the subagent layer. Every one of them is
        # derived here from the live objects, so a signature change fails the
        # suite rather than quietly making the system prompt wrong.
        from desmos.dialect import capabilities as _caps
        from desmos import subagent as _sa
        from desmos.loop import RESULT_CLIP as _clip_cap
        from desmos.subagent_contracts import Budget as _Budget, TaskContract as _TC
        import inspect as _insp

        caps = _caps()
        assert str(_clip_cap) in caps, "prompt quotes a result cap loop.py does not apply"
        # Over the cap the output goes to disk. The prompt names that directory,
        # so the name is derived from the module that writes it.
        from desmos.spill import SPILL_DIR as _spill_dir
        assert _spill_dir in caps, "the prompt does not say where a spilled result lands"
        for _name, _cfg in _sa.AGENTS.items():
            assert _name in caps, _name
            assert str(_cfg["max_turns"]) in caps, f"{_name} turn cap not in the prompt"
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
        assert "defaults to explore" in caps
        assert "resume" in _insp.signature(_sa.spawn).parameters and "resume" in caps
        for _f in _TC.__dataclass_fields__:
            assert _f in caps, f"TaskContract.{_f} is not described in the prompt"
        for _f in _Budget.__dataclass_fields__:
            assert _f in caps, f"Budget.{_f} is not described in the prompt"
        for _n in ("structured_result", "judgment", "spawn", "fanout", "wait", "gather", "status"):
            assert callable(getattr(_sa, _n)), _n
            assert _n in caps, f"prompt names {_n} but subagent does not export it"


        # Cross-provider round trip. Switching to OpenAI and back used to brick
        # the session: openai.py puts its item id in "signature" as a provenance
        # marker, wire_content saw a truthy signature and replayed it, and
        # Anthropic answered 400 "Invalid `signature` in `thinking` block".
        # Found by switching providers mid-session in a live TUI, not by reading.
        from desmos.complete import wire_content as _wire

        oai_turn = [
            {"type": "thinking", "thinking": "pondered", "signature": "rs_abc123", "openai": {"type": "reasoning"}},
            {"type": "text", "text": "said out loud", "openai": {"type": "message"}},
            {"type": "compaction", "summary": "folded by openai", "openai": {"type": "compaction"}},
        ]
        replayed = _wire(oai_turn)
        assert all(b.get("type") != "compaction" for b in replayed), replayed
        assert not any("openai" in b for b in replayed), "no foreign field may reach the wire"
        for b in replayed:
            assert b.get("type") != "thinking", "a foreign thought must not replay as thinking"
            assert "signature" not in b, b
        assert any(b["type"] == "text" and b["text"] == "said out loud" for b in replayed), replayed
        # Our own signed thought still replays -- the fix must not cost that.
        ours = _wire([{"type": "thinking", "thinking": "mine", "signature": "sig"}])
        assert ours == [{"type": "thinking", "thinking": "mine", "signature": "sig"}], ours

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
        # Adaptive thinking interleaves on its own and asks for no beta of its
        # own. Compaction is the only header an adaptive model carries.
        from desmos.complete import COMPACT_BETA as _CB, INTERLEAVED_BETA

        assert INTERLEAVED_BETA not in payload["_betas"], payload["_betas"]
        assert payload["_betas"] == [_CB], payload["_betas"]
        # Without these the model keeps writing past its own syscall and
        # invents the reply to it, then reasons from the invention. Both
        # markers are anchored to a line start so prose can still name them.
        stops = payload["stop_sequences"]
        assert len(stops) == 2, stops
        assert all(x.startswith("\n") for x in stops), stops
        assert any("res" in x for x in stops), stops
        assert any("user" in x for x in stops), stops
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
        # Two matches is ambiguous, and a refusal that still wrote would
        # corrupt the file while reporting the refusal.
        dispatch(world, Block("edit", "alpha\n---\nALPHA", {"path": str(sample)}))
        assert sample.read_text(encoding="utf-8") == "alpha beta alpha\n", "ambiguous edit wrote anyway"
        sample.write_text("alpha beta\n", encoding="utf-8")
        dispatch(world, Block("edit", "alpha\n---\nALPHA", {"path": str(sample)}))
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
        dispatch(world, Block("reload", "", {}))
        assert any(s.name == "later" for s in world.skills)
        assert dispatch(world, Block("skill", "", {"name": "later"})).endswith("ok\n")

        dispatch(world, Block("reload_sdk", "", {}))
        assert "reload_sdk" in world.tools

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
        assert "post" in seen
        assert seen.index("post") < seen.index("complete")

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

        from desmos.complete import apply_stream_event, assemble_message, read_sse

        stream_state: dict = {"message": {}, "blocks": []}
        stream_deltas: list[dict] = []
        apply_stream_event(
            stream_state,
            {
                "type": "message_start",
                "message": {"id": "m", "role": "assistant", "usage": {"input_tokens": 9}},
            },
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "ab"}},
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {"type": "content_block_stop", "index": 0},
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "redacted_thinking", "data": "opaque-secret"},
            },
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "hi"}},
            stream_deltas.append,
        )
        apply_stream_event(
            stream_state,
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 3},
            },
            stream_deltas.append,
        )
        streamed = assemble_message(stream_state)
        assert streamed["content"][0]["thinking"] == "ab"
        assert streamed["content"][1]["data"] == "opaque-secret"
        assert streamed["content"][2]["text"] == "hi"
        assert streamed["stop_reason"] == "end_turn"
        assert streamed["usage"]["output_tokens"] == 3
        assert "opaque-secret" not in str(stream_deltas)
        assert any(d.get("kind") == "thinking_delta" and d.get("text") == "ab" for d in stream_deltas)
        assert any(d.get("kind") == "thinking" and d.get("redacted") for d in stream_deltas)
        assert any(d.get("kind") == "text_delta" and d.get("text") == "hi" for d in stream_deltas)
        sse_msg = read_sse(
            [
                "event: message_start",
                'data: {"type":"message_start","message":{"role":"assistant"}}',
                "",
                "event: content_block_start",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                "",
                "event: content_block_delta",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}',
                "",
                "event: message_stop",
                'data: {"type":"message_stop"}',
                "",
            ]
        )
        assert text_of(sse_msg) == "ok"
        halted = {"go": False}

        def on_first_delta(delta: dict) -> None:
            if delta.get("kind") == "text_delta":
                halted["go"] = True

        sse_stop = read_sse(
            [
                'data: {"type":"message_start","message":{"role":"assistant"}}',
                "",
                'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                "",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"one"}}',
                "",
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"two"}}',
                "",
            ],
            on_event=on_first_delta,
            should_stop=lambda: halted["go"],
        )
        assert "one" in text_of(sse_stop)
        # A stream that runs dry is not a finished answer. Returned as one, the
        # half-written reply's last syscall has no closing tag, scan() skips
        # unterminated blocks, the turn reports done, and a dropped socket gets
        # committed as the step's result. Ctrl+C is the one legitimate early
        # exit -- sse_stop above must keep working, which is why this is not
        # simply "no message_stop means raise".
        try:
            read_sse(
                [
                    'data: {"type":"message_start","message":{"role":"assistant"}}',
                    "",
                    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
                    "",
                    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"half a <bash>ls"}}',
                    "",
                ]
            )
            raise AssertionError("a truncated stream must not read as a finished answer")
        except RuntimeError as exc:
            assert "message_stop" in str(exc), exc
        assert "two" not in text_of(sse_stop)

        from desmos.complete import iter_sse_lines
        from desmos.exec import run_bash

        import socket
        import threading
        import urllib.request

        got_live = threading.Event()

        def _chunk(conn: socket.socket, payload: bytes) -> None:
            conn.sendall(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")

        def _sse_server(sock: socket.socket) -> None:
            conn, _ = sock.accept()
            try:
                buf = b""
                while b"\r\n\r\n" not in buf:
                    more = conn.recv(4096)
                    if not more:
                        return
                    buf += more
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                )
                _chunk(
                    conn,
                    b'data: {"type":"message_start","message":{"role":"assistant","usage":{}}}\n\n',
                )
                _chunk(
                    conn,
                    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
                )
                _chunk(
                    conn,
                    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"LIVE"}}\n\n',
                )
                if not got_live.wait(2):
                    return
                _chunk(
                    conn,
                    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n',
                )
                # A real stream terminates. Without this the fixture was a
                # truncated response that the parser accepted as a finished one.
                _chunk(conn, b'data: {"type":"message_stop"}\n\n')
                conn.sendall(b"0\r\n\r\n")
            finally:
                conn.close()
                sock.close()

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)
        threading.Thread(target=_sse_server, args=(srv,), daemon=True).start()
        live_seen: list[str] = []

        def on_live(delta: dict) -> None:
            if delta.get("kind") == "text_delta" and delta.get("text") == "LIVE":
                live_seen.append("LIVE")
                got_live.set()

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            streamed = read_sse(iter_sse_lines(resp), on_event=on_live)
        assert live_seen == ["LIVE"], "SSE delta must fire before the server closes"
        assert text_of(streamed) == "LIVE"

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
        msg = _fmt([(Block("bash", "seq", {}), big)], cwd)
        assert _mark in msg, msg[:200]
        assert len(sorted(out_dir.glob("*.txt"))) == 1, "the same output spilled twice"
        assert len(msg) < _clip2 + 400, len(msg)
        # A result that reached the gate without passing an exec cap -- a
        # handler's return, a shell drain -- has to spill there instead.
        raw = "x" * (_clip2 + 500)
        msg2 = _fmt([(Block("shell", "", {}), raw)], cwd)
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
            return {"content": [{"type": "text", "text": "<python>len(doc)</python>"}], "usage": {}}

        w_wire.complete_fn = wire_complete
        _run(w_wire, "how long is doc?", quiet=True, on_event=lambda e: evs_wire.append(e))
        res = next(
            e
            for e in evs_wire
            if e.get("ev") == "result" and e.get("phase") in {None, "done"}
        )
        assert res.get("tag") == "python"
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
        # Compaction. The server folds old turns and hands back a `compaction`
        # block; that block is the cut point the next request replays. Both
        # allowlists it has to cross drop unknown block types by default, and
        # dropping it fails silently -- the run still answers, the transcript
        # just never folds. So assert the whole round trip, not the request knob.
        from desmos.complete import (
            COMPACT_BETA,
            COMPACT_STRATEGY,
            assistant_content,
            cached_payload,
            compaction_block,
            wire_content,
        )

        fold = {"type": "compaction", "id": "cmp_1", "content": "folded 40 turns"}
        kept = assistant_content({"content": [fold, {"type": "text", "text": "ok"}]})
        assert compaction_block(kept) == fold, kept
        assert wire_content(kept)[0] == fold, "a fold must survive the replay path too"

        built = cached_payload("claude-opus-5", "abi", [{"role": "user", "content": "hi"}], 256)
        assert built["context_management"] == {"edits": [{"type": COMPACT_STRATEGY}]}
        assert COMPACT_BETA in built["_betas"]
        # A model without server-side compaction must not carry the knob or the
        # beta -- an unsupported pair is a 400, not a no-op.
        old = cached_payload("claude-3-haiku-20240307", "abi", [{"role": "user", "content": "hi"}], 256)
        assert "context_management" not in old
        assert COMPACT_BETA not in old["_betas"]

        # A fold reaches the wire pane. Without the event the only symptom is
        # the context bar dropping with nothing on screen to explain it.
        w_fold = new_world(cwd, state_path=cwd / "harness-fold.json", ns={})
        w_fold.complete_fn = lambda *_: {
            "content": [fold, {"type": "text", "text": "done"}],
            "usage": {},
        }
        fold_evs: list[dict] = []
        _run(w_fold, "long run", quiet=True, on_event=fold_evs.append)
        assert any(e.get("ev") == "compacted" for e in fold_evs), [e.get("ev") for e in fold_evs]
        assert compaction_block(w_fold.messages[-1]["content"]) == fold, w_fold.messages[-1]

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

        # Overload is routine and used to kill a whole multi-turn step.
        from desmos.complete import RETRY_CAP, RETRY_STATUS, _retry_after

        assert {429, 529, 503}.issubset(RETRY_STATUS)
        assert 400 not in RETRY_STATUS and 401 not in RETRY_STATUS, "a payload bug never heals"

        class _Err:
            def __init__(self, **h):
                self.headers = h

        assert _retry_after(_Err(**{"retry-after": "2"}), 0) == 2.0
        assert _retry_after(_Err(**{"retry-after-ms": "1500"}), 0) == 1.5
        assert _retry_after(_Err(**{"retry-after": "99999"}), 0) == RETRY_CAP, "an hour is not a wait"
        assert _retry_after(_Err(**{"retry-after": "junk"}), 0) == 0.5
        assert _retry_after(_Err(), 3) == 4.0, "backoff when the endpoint says nothing"

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
        assert tail == ["user", "assistant", "user", "user"], tail
        assert "<result" in str(w_ord.messages[-2]["content"]), "a stop must not eat results that ran"
        assert "stopped by the user" in str(w_ord.messages[-1]["content"]), w_ord.messages[-1]

        # The two effort ladders are different lengths, so an effort that is
        # fine on one provider does not exist on the other. Refusing the switch
        # on that basis meant a session on sol at medium or max simply could
        # not move to Opus. The model is what was asked for; the effort bends.
        from desmos.settings import CATALOG as _CAT, clamp_effort

        for provider, table in _CAT.items():
            for effort in ("none", "low", "medium", "high", "xhigh", "max", "nonsense"):
                got = clamp_effort(provider, effort)
                assert got in table["efforts"], f"{provider}/{effort} -> {got}"
        # An effort the target does have is never moved.
        assert clamp_effort("anthropic", "high") == "high"
        assert clamp_effort("openai", "medium") == "medium"
        # A tie goes up: thinking less than asked is the worse surprise. Both
        # live providers offer the same five rungs now, so a tie only exists
        # against a ladder with a hole in it -- build one rather than assert
        # nothing.
        _CAT["_gap"] = {"models": [], "efforts": ["low", "high"]}
        try:
            assert clamp_effort("_gap", "medium") == "high"
            assert clamp_effort("_gap", "xhigh") == "high"
        finally:
            del _CAT["_gap"]

        # Every rung the picker offers has to survive to the wire as itself.
        # xhigh was being rewritten to max, so choosing it ran a level the user
        # did not pick and the settings file disagreed with the request. The
        # API rejects anything outside these five, which is why the list is
        # exactly this and why a silent rewrite is not harmless.
        from desmos.complete import apply_thinking as _apply

        for _level in _CAT["anthropic"]["efforts"]:
            _payload: dict = {}
            _apply(_payload, "claude-opus-5", _level)
            assert _payload["output_config"]["effort"] == _level, (_level, _payload)

        # A backgrounded grandchild inherits stdout and can hold the pipe open
        # for as long as it likes. The unbounded read that waited for it made
        # `sleep 20 & echo started` with timeout=3 return after 20 seconds, and
        # for all 20 the deadline and should_stop were unreachable -- kernel,
        # bridge inbox and stop button wedged behind a process nobody awaited.
        import time as _time

        from desmos.exec import run_bash as _bash

        started = _time.monotonic()
        out = _bash("sleep 20 & echo started", cwd, timeout=3)
        took = _time.monotonic() - started
        assert took < 8, f"a backgrounded grandchild held the harness for {took:.1f}s"
        assert "started" in out, out
        # And a timeout takes the whole group with it, not just /bin/sh.
        started = _time.monotonic()
        out = _bash("sh -c 'sleep 30 & wait'", cwd, timeout=2)
        assert _time.monotonic() - started < 6, out
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

        # The OpenAI stream needs the terminal check the Anthropic one grew.
        from desmos import openai as _oai_stream

        try:
            _oai_stream.read_sse(
                [
                    'data: {"type":"response.output_item.added","item":{"type":"message","id":"m"}}',
                    "",
                ],
                "gpt-5.6-sol",
            )
            raise AssertionError("a truncated Responses stream must not read as finished")
        except RuntimeError as exc:
            assert "response.completed" in str(exc), exc

        # <bash> is one subprocess per call, so nothing it does survives. The
        # persistent shell is the other half: state carries, exit codes come
        # back, and a program that asks a question can be answered -- which is
        # the case a one-shot subprocess cannot express at all.
        from desmos.shell import close_all as _close_shells, head_tail, strip_ansi

        w_sh = new_world(cwd, state_path=None, persist=False, ns={})
        try:
            def sh(body: str, **attrs: str) -> str:
                return dispatch(w_sh, Block("shell", body, attrs))

            assert sh("cd /etc && pwd").strip() == "/etc"
            assert sh("pwd").strip() == "/etc", "a persistent shell keeps its cwd"
            assert sh("export DZ=kept; echo ok").strip() == "ok"
            assert sh("echo $DZ").strip() == "kept", "and its environment"
            assert sh("python3 -c 'import sys;print(sys.stdin.isatty())'").strip() == "True"
            failed = sh("ls /definitely-not-here")
            assert "[exit " in failed and "[exit 0]" not in failed, failed
            # A second session is a second machine as far as state goes.
            assert sh("pwd", id="other").strip() != "/etc"
            # The interactive round trip.
            asked = sh("python3 -c \"n=input('who? ');print('hi '+n)\"")
            assert "who?" in asked and "still running" in asked, asked
            assert sh("desmos").strip() == "hi desmos", "the answer reached the waiting program"
            assert sh("echo recovered").strip() == "recovered", "and the shell came back"
            assert w_sh.shells, "sessions live on the world"
        finally:
            _close_shells(w_sh)
        assert not w_sh.shells

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
        boom = dispatch(world, Block("python", "print('x' * 9000)\n1/0", {}))
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
            warned = dispatch(world, Block("python", "x = '\\|'\n'done'", {}))
        assert "SyntaxWarning" in warned, warned[:200]
        assert leak.getvalue() == "", f"a parse warning escaped to the terminal: {leak.getvalue()!r}"

        w_usage.ns["reset"]()
        assert w_usage.messages == []

        evolve(w3, "after ping")
        assert (gen_dir(w3) / "0001.json").is_file()
        dispatch(w3, Block("system", "usage line", {}))
        assert w3.notes["note"] == "usage line"
        rollback(w3, 1)
        assert "note" not in w3.notes

        py = cwd / "broke.py"
        py.write_text("x = 1\n")
        bad = dispatch(world, Block("edit", "x = 1\n---\nx =\n", {"path": str(py)}))
        assert "SyntaxError" in bad, bad
        assert py.read_text() == "x = 1\n", "a file that would not compile was written anyway"
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
        reloaded_parent = new_world(cwd, state_path=cwd / "harness-iso.json")
        assert "pwn" not in reloaded_parent.notes
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

        import json
        from io import StringIO

        from desmos.acp import AcpServer, serve as acp_serve

        acp_in = StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "authenticate", "params": {"methodId": "none"}})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 3, "method": "session/new", "params": {"cwd": str(cwd)}})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 4, "method": "nope", "params": {}})
            + "\n"
        )
        acp_out = StringIO()
        assert acp_serve(acp_in, acp_out, cwd=cwd) == 0
        acp_replies = [json.loads(line) for line in acp_out.getvalue().splitlines() if line.strip()]
        assert [r.get("id") for r in acp_replies] == [1, 2, 3, 4]
        init = acp_replies[0]["result"]
        assert init["protocolVersion"] == 1
        assert init["authMethods"][0]["id"] == "none"
        assert init["agentCapabilities"]["loadSession"] is False
        assert init["agentCapabilities"]["promptCapabilities"]["image"] is True
        assert init["_meta"]["grokShell"] is False
        assert acp_replies[1]["result"] == {}
        assert acp_replies[2]["result"]["sessionId"]
        assert acp_replies[3]["error"]["code"] == -32601

        notes: list[dict] = []
        acp = AcpServer(notes.append, default_cwd=cwd)
        created = acp.handle({"jsonrpc": "2.0", "id": 10, "method": "session/new", "params": {"cwd": str(cwd)}})
        assert created is not None
        sid = created["result"]["sessionId"]

        def fake_acp(_model, _system, messages, _max_tokens):
            blob = json.dumps(messages)
            if "<result" in blob:
                return {"content": [{"type": "text", "text": "done"}], "usage": {}}
            return {
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                    {"type": "text", "text": "<python>1+1</python>"},
                ],
                "usage": {},
            }

        acp.sessions[sid].complete_fn = fake_acp
        prompted = acp.handle({
            "jsonrpc": "2.0",
            "id": 11,
            "method": "session/prompt",
            "params": {
                "sessionId": sid,
                "prompt": [{"type": "text", "text": "add one"}],
                "_meta": {"promptId": "p-check"},
            },
        })
        assert prompted == {"jsonrpc": "2.0", "id": 11, "result": {"stopReason": "end_turn"}}
        kinds = [n["params"]["update"]["sessionUpdate"] for n in notes if n.get("method") == "session/update"]
        assert "agent_thought_chunk" in kinds
        assert "agent_message_chunk" in kinds
        assert "tool_call" in kinds
        assert "tool_call_update" in kinds
        assert all(n["params"].get("_meta", {}).get("promptId") == "p-check" for n in notes if n.get("method") == "session/update")
        tool = next(n["params"]["update"] for n in notes if n.get("method") == "session/update" and n["params"]["update"]["sessionUpdate"] == "tool_call")
        assert tool["title"] == "python" and tool["kind"] == "execute"

        # --- auth: file schema, credential precedence, masking (no network) ---
        import base64
        import json
        import os
        import time
        import urllib.parse

        from desmos import auth as _auth

        old_env = {k: os.environ.get(k) for k in ("OPENAI_API_KEY", "DESMOS_AUTH", "CODEX_HOME", "ANTHROPIC_API_KEY")}
        try:
            authdir = cwd / "authhome"
            authdir.mkdir()
            os.environ["DESMOS_AUTH"] = str(authdir / "auth.json")
            os.environ["CODEX_HOME"] = str(cwd / "nocodex")
            os.environ.pop("OPENAI_API_KEY", None)
            assert _auth.desmos_auth_path() == authdir / "auth.json"
            assert _auth.openai_credential() is None
            try:
                _auth.credential("openai")
                raise AssertionError("expected NeedsAuth")
            except _auth.NeedsAuth:
                pass

            # an oauth file we wrote ourselves, in Codex's own schema
            fake_jwt = _fake_id_token(plan="pro", account="acct-42")
            _auth.write_auth_file(
                _auth.desmos_auth_path(),
                {
                    "access_token": fake_jwt,
                    "refresh_token": "rt-1",
                    "id_token": fake_jwt,
                    "expires_at": int(time.time()) + 3600,
                },
            )
            raw = json.loads(_auth.desmos_auth_path().read_text())
            assert "tokens" in raw and raw["tokens"]["refresh_token"] == "rt-1", raw
            assert oct(_auth.desmos_auth_path().stat().st_mode)[-3:] == "600"
            cred = _auth.openai_credential()
            assert cred is not None and cred.kind == "oauth"
            assert cred.account_id == "acct-42" and cred.plan == "pro"
            assert not cred.expired()
            assert fake_jwt not in cred.masked() and "…" in cred.masked()

            # env key wins over the stored oauth token
            os.environ["OPENAI_API_KEY"] = "sk-openai-test-key"
            cred = _auth.openai_credential()
            assert cred.kind == "env" and cred.source == "OPENAI_API_KEY"
            rows = {r["provider"]: r for r in _auth.status()}
            assert rows["openai"]["ok"] and "openai" in rows and "anthropic" in rows
            assert "sk-openai-test-key" not in json.dumps(rows)

            assert _auth.logout_openai() == [str(_auth.desmos_auth_path())]
            os.environ.pop("OPENAI_API_KEY", None)
            assert _auth.openai_credential() is None
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        # --- browser login: pkce, consent url, and a real localhost callback ---
        import hashlib
        import socket
        import threading
        import urllib.request

        verifier, challenge = _auth._pkce()
        assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=") == challenge
        url = _auth.authorize_url(challenge, "st-1")
        assert url.startswith(_auth.AUTH_BASE + "/oauth/authorize?")
        for want in ("code_challenge_method=S256", "code_challenge=" + challenge, "state=st-1",
                     urllib.parse.quote(_auth.LOCAL_REDIRECT_URI, safe="")):
            assert want in url, want

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        real_port = _auth.LOCAL_PORT
        _auth.LOCAL_PORT = free_port
        try:
            for state, query, expect in (
                ("st-2", "code=ac-9&state=st-2", "ac-9"),
                ("st-3", "code=ac-9&state=wrong", None),
            ):
                out: dict = {}

                def serve(state=state, out=out):
                    try:
                        out["code"] = _auth.wait_for_callback(state, timeout=10)
                    except Exception as e:  # NeedsAuth on mismatch
                        out["err"] = str(e)

                t = threading.Thread(target=serve, daemon=True)
                t.start()
                body = b""
                hit = f"http://127.0.0.1:{free_port}{_auth.CALLBACK_PATH}?{query}"
                for _ in range(100):
                    try:
                        body = urllib.request.urlopen(hit, timeout=2).read()
                        break
                    except OSError:
                        time.sleep(0.05)
                t.join(12)
                assert not t.is_alive(), "callback server never returned"
                assert b"signed in" in body, body[:80]
                if expect:
                    assert out.get("code") == expect, out
                else:
                    assert "code" not in out and "state mismatch" in out.get("err", ""), out
        finally:
            _auth.LOCAL_PORT = real_port




        # --- bridge: the picker and the model op, driven as a real subprocess ---
        import subprocess as _sp
        import sys

        bridge_env = dict(os.environ)
        bridge_env["DESMOS_SETTINGS"] = str(cwd / "settings.json")
        bridge_env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        (cwd / "bridgecwd").mkdir(exist_ok=True)
        proc = _sp.Popen(
            [sys.executable, "-m", "desmos", "bridge", "--cwd", str(cwd / "bridgecwd")],
            stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE, text=True, env=bridge_env,
        )
        try:
            ready = json.loads(proc.stdout.readline())
            assert ready["ev"] == "ready", ready
            assert ready["onboarding"] is True and ready["current"] is None, ready
            names = [p["provider"] for p in ready["providers"]]
            assert names == ["anthropic", "openai"], names
            oai = next(p for p in ready["providers"] if p["provider"] == "openai")
            # The full 5.6 ladder. Offering three of six rungs meant `medium`
            # -- the everyday balance -- could not be selected, and `max`
            # collapsed onto xhigh in effort_of, so the top could not be asked
            # for at all.
            assert "gpt-5.6-sol" in oai["models"], oai["models"]
            assert oai["efforts"] == ["low", "medium", "high", "xhigh", "max"], oai["efforts"]
            from desmos.openai import effort_of as _eff

            assert [_eff(x) for x in oai["efforts"]] == oai["efforts"], "every offered rung must survive the mapping"
            assert _eff("off") == "none" and _eff("nonsense") == "low"
            assert oai["can_login"] is True
            assert ready["provider"] in ("anthropic", "openai")

            proc.stdin.write(json.dumps({"op": "model", "model": "gpt-5.6-luna", "effort": "xhigh"}) + "\n")
            proc.stdin.flush()
            snap = json.loads(proc.stdout.readline())
            assert snap["ev"] == "snapshot" and snap["model"] == "gpt-5.6-luna", snap
            assert snap["provider"] == "openai" and snap["thinking"] == "xhigh", snap
            saved = json.loads((cwd / "settings.json").read_text())
            assert saved == {"provider": "openai", "model": "gpt-5.6-luna", "effort": "xhigh"}, saved

            # Crossing providers drops the other provider's thinking blocks from
            # every later request. That is a real loss of context, so the bridge
            # says so instead of letting it look like a glitch.
            fence = json.loads(proc.stdout.readline())
            assert fence["ev"] == "notice", fence
            assert fence["text"].strip(), fence

            proc.stdin.write(json.dumps({"op": "model", "model": "gpt-9-nope"}) + "\n")
            proc.stdin.flush()
            bad = json.loads(proc.stdout.readline())
            assert bad["ev"] == "error" and "gpt-9-nope" in bad["text"], bad

            proc.stdin.write(json.dumps({"op": "picker"}) + "\n")
            proc.stdin.flush()
            pick = json.loads(proc.stdout.readline())
            assert pick["ev"] == "picker" and pick["onboarding"] is False, pick
            assert pick["current"]["model"] == "gpt-5.6-luna", pick
        finally:
            proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
            proc.stdin.flush()
            proc.wait(timeout=20)

        from desmos import settings as _st

        assert _st.provider_of("gpt-5.6-sol") == "openai"
        assert _st.provider_of("claude-opus-5") == "anthropic"

        # --- device login: the poll loop, driven with no network and no sleeping ---
        calls: list = []
        replies = [
            (403, {"error": {"code": "deviceauth_authorization_pending"}}),
            (429, {"error": {"code": "slow_down"}}),
            (200, {"authorization_code": "dev-code", "code_verifier": "dev-verifier"}),
        ]
        real_post, real_sleep = _auth._post_json, _auth._sleep
        try:
            _auth._post_json = lambda url, body, timeout=30: (calls.append((url, body)), replies.pop(0))[1]
            _auth._sleep = lambda s: calls.append(("slept", s))
            dev = _auth.DeviceCode("dev-1", "ABCD-EFGH", interval=5)
            got = _auth.poll_device_login(dev)
            assert got == {"code": "dev-code", "verifier": "dev-verifier"}, got
            slept = [s for tag, s in calls if tag == "slept"]
            assert slept == [5, 7], slept  # slow_down actually backs the poll off
            assert all(url == _auth.DEVICE_TOKEN_URL for url, _ in calls if url != "slept")

            replies[:] = [(400, {"error": {"code": "expired_token"}})]
            calls.clear()
            try:
                _auth.poll_device_login(_auth.DeviceCode("dev-2", "X", interval=1))
                raise AssertionError("expected NeedsAuth on a hard device error")
            except _auth.NeedsAuth as e:
                assert "expired_token" in str(e), e
        finally:
            _auth._post_json, _auth._sleep = real_post, real_sleep

        # start_device_login must reject a malformed response instead of polling forever
        try:
            _auth._post_json = lambda *a, **kw: (200, {"user_code": "X"})
            try:
                _auth.start_device_login()
                raise AssertionError("expected NeedsAuth on a malformed device code")
            except _auth.NeedsAuth:
                pass
        finally:
            _auth._post_json = real_post

        # --- openai provider: replay, streaming, usage, and the dispatch seam ---
        from desmos import openai as _oai
        from desmos.complete import assistant_content as _ac

        assert _oai.is_openai("gpt-5.6-sol") and not _oai.is_openai("claude-opus-5")
        assert _oai.effort_of("xhigh") == "xhigh" and _oai.effort_of("off") == "none"
        assert set(_oai.EFFORTS) == {"low", "medium", "high", "xhigh", "max"}
        assert _oai.effort_of("max") == "max", "max is its own rung above xhigh, not an alias for it"
        assert _oai.effort_of("medium") == "medium"
        assert "gpt-5.6-sol" in _oai.MODELS and "gpt-5.6-luna" in _oai.MODELS

        reasoning_item = {
            "id": "rs_1",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "weighing it"}],
            "encrypted_content": "ENC-OPAQUE",
        }
        msg_item = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "done"}],
        }
        events = [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1"}},
            {"type": "response.reasoning_summary_text.delta", "delta": "weigh"},
            {"type": "response.reasoning_summary_text.delta", "delta": "ing it"},
            {"type": "response.output_item.done", "item": reasoning_item},
            {"type": "response.output_text.delta", "delta": "do"},
            {"type": "response.output_text.delta", "delta": "ne"},
            {"type": "response.output_item.done", "item": msg_item},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [reasoning_item, msg_item],
                    "usage": {
                        "input_tokens": 1000,
                        "input_tokens_details": {"cached_tokens": 900},
                        "output_tokens": 40,
                        "output_tokens_details": {"reasoning_tokens": 25},
                    },
                },
            },
        ]
        sse = []
        for ev in events:
            sse.append("event: " + ev["type"])
            sse.append("data: " + json.dumps(ev))
            sse.append("")
        seen = []
        resp_oai = _oai.read_sse(iter(sse), "gpt-5.6-sol", on_event=seen.append)
        assert "".join(e["text"] for e in seen if e["kind"] == "thinking_delta") == "weighing it"
        assert "".join(e["text"] for e in seen if e["kind"] == "text_delta") == "done"
        assert text_of(resp_oai) == "done"
        u = resp_oai["usage"]
        assert u["cache_read_input_tokens"] == 900 and u["input_tokens"] == 100, u
        assert u["output_tokens"] == 40 and u["reasoning_tokens"] == 25

        kept_oai = _ac(resp_oai)
        assert kept_oai[0]["openai"]["encrypted_content"] == "ENC-OPAQUE", kept_oai[0]
        assert kept_oai[0]["thinking"] == "weighing it"

        # replayed verbatim, not rebuilt: the encrypted item goes back as-is
        back = _oai.to_input([{"role": "user", "content": "hi"}, {"role": "assistant", "content": kept_oai}])
        assert back[0]["content"][0]["type"] == "input_text"
        assert reasoning_item in back, back
        assert msg_item in back, back

        call_item = {
            "id": "ct_1",
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": "call_1",
            "name": "syscall",
            "input": "<python>OPENAI_SYSCALL_EVAL = 40 + 2\nprint(OPENAI_SYSCALL_EVAL)</python>",
        }
        call_resp = {
            "role": "assistant",
            "model": "gpt-5.6-sol",
            "content": _oai._blocks_from_items([call_item]),
            "usage": {},
            "stop_reason": "end_turn",
        }
        kept_call = _ac(call_resp)
        assert text_of(call_resp) == "", "typed syscall input is not assistant speech"
        assert kept_call[0]["input"].endswith("</python>")
        replay = _oai.to_input(
            [
                {"role": "assistant", "content": kept_call},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "custom_tool_call_output",
                            "call_id": "call_1",
                            "output": '<result tag="python">42</result>',
                        }
                    ],
                },
            ]
        )
        assert replay == [
            call_item,
            {
                "type": "custom_tool_call_output",
                "call_id": "call_1",
                "output": '<result tag="python">42</result>',
            },
        ], replay
        tools = _oai.payload_for("gpt-5.6-sol", "system", [], 100)["tools"]
        assert len(tools) == 1 and tools[0]["type"] == "custom" and tools[0]["name"] == "syscall"

        final_resp = {
            "role": "assistant",
            "model": "gpt-5.6-sol",
            "content": [{"type": "text", "text": "done"}],
            "usage": {},
            "stop_reason": "end_turn",
        }
        replies = iter([call_resp, final_resp])
        w_call = new_world(cwd, persist=False, ns={})
        w_call.model = "gpt-5.6-sol"
        w_call.complete_fn = lambda *_args: next(replies)
        from desmos.loop import run_turns as _run_openai

        assert _run_openai(w_call, "calculate", max_turns=3, quiet=True) == "done"
        assert w_call.ns["OPENAI_SYSCALL_EVAL"] == 42
        typed_results = [
            b
            for m in w_call.messages
            if m.get("role") == "user" and isinstance(m.get("content"), list)
            for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "custom_tool_call_output"
        ]
        assert typed_results[0]["call_id"] == "call_1" and ">42<" in typed_results[0]["output"]

        bad_item = dict(call_item, id="ct_bad", call_id="call_bad", input=call_item["input"] + " lousy?")
        w_bad = new_world(cwd, persist=False, ns={})
        w_bad.model = "gpt-5.6-sol"
        w_bad.complete_fn = lambda *_args: {
            **call_resp,
            "content": _oai._blocks_from_items([bad_item]),
        }
        bad_events: list[dict] = []
        assert _run_openai(
            w_bad, "calculate", max_turns=1, quiet=True, on_event=bad_events.append
        ) == ""
        assert "OPENAI_SYSCALL_EVAL" not in w_bad.ns, "invalid typed input must not dispatch"
        assert any(
            e.get("ev") == "error" and "only complete XML" in e.get("text", "")
            for e in bad_events
        ), bad_events

        # sol splits a turn into a commentary preamble and a final_answer, and
        # some models stream reasoning verbatim rather than as a summary. Both
        # events were unhandled: the thinking pane stayed empty while reasoning
        # tokens were billed, and a refusal arrived as an empty reply the loop
        # read as "the model is done".
        raw_items = [
            {
                "id": "msg_c",
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "I'll look first."}],
            },
            {
                "id": "msg_f",
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "refusal", "refusal": "I can't help with that."}],
            },
        ]
        raw_events = [
            {"type": "response.reasoning_text.delta", "delta": "step one"},
            {"type": "response.refusal.delta", "delta": "I can't help with that."},
            {
                "type": "response.completed",
                "response": {"id": "r2", "status": "completed", "output": raw_items, "usage": {}},
            },
        ]
        sse2 = []
        for ev in raw_events:
            sse2.append("data: " + json.dumps(ev))
            sse2.append("")
        seen2: list[dict] = []
        resp2 = _oai.read_sse(iter(sse2), "gpt-5.6-sol", on_event=seen2.append)
        assert [e["text"] for e in seen2 if e["kind"] == "thinking_delta"] == ["step one"]
        assert [e["text"] for e in seen2 if e["kind"] == "text_delta"] == ["I can't help with that."]
        phases = [b.get("phase") for b in resp2["content"]]
        assert phases == ["commentary", "final_answer"], phases
        assert "I can't help with that." in text_of(resp2), "a refusal is the answer, not nothing"

        # gpt-5.6-sol ended every message from the seventeenth on with a stray
        # token after the closing tag. Nothing rewrites the message -- the
        # stored bytes must stay exact for the cached prefix -- but the parser
        # now reports what it left outside the calls.
        from desmos.scan import trailing_residue as _residue

        sol_tail = "<bash>rg -n data .</" + "bash> \n lousy? token. \n"
        assert _residue(sol_tail) == "lousy? token.", _residue(sol_tail)
        assert [b.tag for b in scan(sol_tail)] == ["bash"], "the call still dispatches"
        assert _residue("<usage/>") == "" and _residue("just prose") == ""
        assert _residue("prose before <usage/>") == "", "only what follows the last call counts"

        # An attached screenshot has to survive the crossing. Anthropic's block
        # shape goes in, Responses' flat data-URL input_image comes out -- the
        # only image shape the Codex backend takes.
        shot = _oai.to_input([{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAB"}},
        ]}])
        assert shot[0]["content"] == [
            {"type": "input_text", "text": "what is this"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAB"},
        ], shot
        assert _oai.to_input([{"role": "user", "content": "plain"}])[0]["content"][0]["text"] == "plain"

        # ...and a foreign thought (no provider item) degrades to speech, not a crash
        foreign = _oai.to_input([{"role": "assistant", "content": [{"type": "thinking", "thinking": "x"}]}])
        assert foreign[0]["content"][0]["text"] == "x"

        body = _oai.payload_for("gpt-5.6-sol", "SYS", [{"role": "user", "content": "hi"}], 4096,
                                thinking="xhigh", compact_threshold=250000, cache_key="k1")
        assert body["instructions"].startswith("SYS") and body["store"] is False
        assert body["reasoning"] == {"effort": "xhigh", "summary": "auto"}
        assert body["include"] == ["reasoning.encrypted_content"]
        assert body["context_management"] == [{"type": "compaction", "compact_threshold": 250000}]
        assert not any(i.get("role") == "system" for i in body["input"])

        url_oauth, h_oauth = _oai.headers_for(_auth.Credential(provider="openai", kind="oauth",
                                                               token="t", account_id="acct-1"))
        assert url_oauth == _oai.CHATGPT_URL and h_oauth["chatgpt-account-id"] == "acct-1"
        # One session per process, not per request: the backend routes on this
        # header and the prompt cache lives behind that routing. A fresh uuid
        # each time halved the hit rate against the live endpoint.
        _, h_again = _oai.headers_for(_auth.Credential(provider="openai", kind="oauth",
                                                       token="t", account_id="acct-1"))
        assert h_oauth["session_id"] == h_again["session_id"] != ""
        assert h_oauth["originator"] and h_oauth["Authorization"] == "Bearer t"
        url_key, h_key = _oai.headers_for(_auth.Credential(provider="openai", kind="env", token="sk-x"))
        assert url_key == _oai.API_URL and "chatgpt-account-id" not in h_key

        # the dispatch seam: a gpt model must never reach the Anthropic call site
        import desmos.complete as _cmp

        routed = {}
        real_oai_complete = _oai.complete
        def _routed_complete(*a, **kw):
            routed["hit"] = (a[0], kw.get("thinking"))
            return resp_oai

        _oai.complete = _routed_complete
        old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            out = _cmp.complete("gpt-5.6-luna", "SYS", [{"role": "user", "content": "hi"}], 4096, thinking="high")
            assert routed["hit"] == ("gpt-5.6-luna", "high"), routed
            assert text_of(out) == "done"
        finally:
            _oai.complete = real_oai_complete
            if old_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = old_key

        # Live model switching is a real operation the model can perform, and
        # the failure it replaces was a model insisting it could not. So this
        # asserts the switch reaches the wire -- the model id the NEXT complete()
        # is called with -- not that some sentence is in the prompt.
        import desmos.settings as _st

        seen: list[str] = []

        def recording_complete(model, system, messages, max_tokens, **_kw):
            seen.append(model)
            if len(seen) == 1:
                return {
                    "content": [
                        {"type": "text", "text": '<python>switch("claude-sonnet-4-6")</python>'}
                    ],
                    "usage": {},
                }
            return {"content": [{"type": "text", "text": "done"}], "usage": {}}

        w_sw = new_world(cwd, state_path=cwd / "switch.json")
        w_sw.model = "claude-opus-5"
        w_sw.complete_fn = recording_complete
        bind_step(w_sw)

        # Neither a real credential nor a write to ~/.desmos is what this proves.
        stub_path = cwd / "settings-not-written.json"
        real_usable, real_save = _st.usable, _st.save
        _st.usable = lambda _p: True
        _st.save = lambda _c: stub_path
        try:
            w_sw.ns["step"]("switch to sonnet")
        finally:
            _st.usable, _st.save = real_usable, real_save

        assert len(seen) >= 2, f"the switch turn never produced a second call: {seen}"
        assert seen[0] == "claude-opus-5", f"first turn used the wrong model: {seen}"
        assert seen[1] == "claude-sonnet-4-6", (
            f"switch() did not reach the wire -- turn 2 still called {seen[1]!r}. "
            "The model can only be believed about its own capabilities if they work."
        )
        assert w_sw.model == "claude-sonnet-4-6"

        # And it refuses a choice that is not real, rather than half-applying it.
        for bad in ("no-such-model-9", "claude-opus-5"):
            try:
                _st.switch(w_sw, bad, "not-an-effort")
            except ValueError:
                pass
            else:
                raise AssertionError(f"switch accepted {bad!r} with a bogus effort")
        assert w_sw.model == "claude-sonnet-4-6", "a rejected switch still mutated the world"

        # vendor/grok-build is committed now, so the DESMOS_ACP branch cannot
        # go missing on a fresh clone. What can still go missing is the branch
        # itself, if a sync overwrites it -- and that is silent, because the
        # pager compiles either way and just runs grok's agent instead of ours.
        _check_path_deps_tracked()
        _check_vendor_patch()

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
