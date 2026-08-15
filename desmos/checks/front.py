"""Front checks: cli/tui launcher, acp, bridge, vendored-pager guarantees."""

from __future__ import annotations

from pathlib import Path


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
    import subprocess
    import tomllib

    root = Path(__file__).resolve().parents[2]
    manifest = root / "Cargo.toml"
    if not manifest.exists() or not (root / ".git").exists():
        return

    # Parsed, not grepped. A regex over the raw text read `path = "../.."` out
    # of a prose comment about vendored crates and failed on a manifest that
    # was completely correct.
    deps: set[str] = set()

    def collect(node: object) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("path"), str):
                deps.add(node["path"])
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(tomllib.loads(manifest.read_text()))
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
        Path(__file__).resolve().parents[2]
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



def check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        import os

        from desmos.front.cli import (
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
        # Against a temp root, not _repo_root(): pointed at the real checkout
        # this check *wrote* the .git/HEAD files it then asserted, so it
        # mutated the tree it was checking and passed no matter what the
        # function did. Here the files start absent, so the assert is about
        # the function creating them and about the contents it copies over.
        fake_root = cwd / "fingerprint-root"
        (fake_root / "vendor" / "grok-build" / ".git").mkdir(parents=True)
        (fake_root / "vendor" / "grok-build" / ".git" / "HEAD").write_text(
            "ref: refs/heads/desmos\n", encoding="utf-8"
        )
        heads = _tui_stabilize_fingerprints(fake_root)
        assert heads, "no rerun-if-changed stand-in was created"
        for head in heads:
            # Missing, cargo rebuilds the pager rlib on every single launch.
            assert head.is_file(), head
            assert head.read_text(encoding="utf-8") == "ref: refs/heads/desmos\n", head
        assert _repo_root().is_dir()
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
        # What we advertise has to be what prompt_text carries. Claiming image
        # support the loop cannot take made the pager send an image block,
        # prompt_text drop it, and the empty prompt answer end_turn with no
        # model call at all.
        from desmos.acp import prompt_text as _prompt_text

        carries_image = bool(
            _prompt_text([{"type": "image", "data": "aGk=", "mimeType": "image/png"}])
        )
        assert init["agentCapabilities"]["promptCapabilities"]["image"] is carries_image
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
        # session/new applies the *user's* saved settings, so this ran against
        # whatever model the developer last switched to -- and on an OpenAI one
        # the loop rejects XML in speech and the whole round trip fails here for
        # a reason that has nothing to do with ACP. Pin the dialect the fake
        # response is written in.
        acp.sessions[sid].model = "claude-opus-5"
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

        # The pager opens a second session on the same cwd for every new
        # thread. Sessions on one workspace share the World -- persist keys its
        # rows off the cwd, so two of them take turns overwriting each other's
        # ns, notes and tools -- but they must not share the transcript: the
        # shared messages list put this session's prompt and reply verbatim
        # into the next session's model call.
        second = acp.handle({"jsonrpc": "2.0", "id": 12, "method": "session/new", "params": {"cwd": str(cwd)}})
        sid2 = second["result"]["sessionId"]
        assert acp.sessions[sid2] is acp.sessions[sid], "one world per workspace"
        seen_prompts: list[str] = []

        def watching(_model, _system, messages, _max_tokens):
            seen_prompts.append(json.dumps(messages))
            return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

        acp.sessions[sid2].complete_fn = watching
        answered = acp.handle({
            "jsonrpc": "2.0",
            "id": 13,
            "method": "session/prompt",
            "params": {"sessionId": sid2, "prompt": [{"type": "text", "text": "second thread"}]},
        })
        assert answered["result"] == {"stopReason": "end_turn"}, answered
        assert seen_prompts, "the second session never reached the model"
        assert "add one" not in seen_prompts[0], seen_prompts[0][:400]
        assert "second thread" in seen_prompts[0], seen_prompts[0][:400]

        # --- bridge: the picker and the model op, driven as a real subprocess ---
        import subprocess as _sp
        import sys

        bridge_env = dict(os.environ)
        bridge_env["DESMOS_SETTINGS"] = str(cwd / "settings.json")
        bridge_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
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

        # vendor/grok-build is committed now, so the DESMOS_ACP branch cannot
        # go missing on a fresh clone. What can still go missing is the branch
        # itself, if a sync overwrites it -- and that is silent, because the
        # pager compiles either way and just runs grok's agent instead of ours.
        _check_path_deps_tracked()
        _check_vendor_patch()
