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


def _check_release_tui_launcher() -> None:
    """An installed wheel launches its release TUI without Rust or vendored source."""
    import os
    import subprocess
    import sys
    import tempfile

    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "desmos-tui"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
        fake.chmod(0o755)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root)
        env["DESMOS_TUI_BINARY"] = str(fake)
        ran = subprocess.run(
            [sys.executable, "-m", "desmos", "tui", "--demo", "--cwd", tmp],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert ran.stdout.splitlines() == [
            "--python",
            sys.executable,
            "--cwd",
            str(Path(tmp).resolve()),
            "--demo",
        ], ran


# The stubbed gland for the socket checks: the REAL bridge subprocess and the
# REAL loop, with canned responses -- the record-golden pattern, one code path,
# never a second engine. Content-addressed so call counts cannot skew it: the
# child under the kill test loops forever until killed, everything else is one
# plain turn.
_BOOT = '''
import json
import sys
from pathlib import Path

import desmos.front.bridge as B


def scripted(model, system, messages, max_tokens):
    blob = json.dumps(messages, default=str)

    def say(t):
        return {
            "content": [{"type": "text", "text": t}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    if "flood test" in blob:
        # Runs after earlier steps, so "<result" is already in the transcript.
        # The sentinel is printed FIRST because the oversized result is
        # spilled keeping the head -- a tail marker never reaches the blob.
        if "FLOODED" not in blob:
            body = 'print("FLOODED")\\nfor i in range(6000):\\n    print(i)'
            return say("flooding\\n<python>\\n" + body + "\\n</python>")
        return say("flood finished")
    if "kill test" in blob:
        if "<result" not in blob:
            body = "\\n".join([
                "from desmos.subagent import spawn, wait",
                'rid = spawn("loop forever", agent="explore", model="claude-opus-5")',
                "wait(rid, timeout=60)",
                'print("child settled")',
            ])
            return say("spawning\\n<python>\\n" + body + "\\n</python>")
        return say("kill test finished")
    if "loop forever" in blob:
        return say("looping\\n<bash>sleep 0.3</bash>")
    return say("pong")


_real = B.new_world


def stubbed(cwd, **kw):
    w = _real(cwd, **kw)
    w.model = "claude-opus-5"
    w.thinking = "low"
    w.complete_fn = scripted
    return w


B.new_world = stubbed
raise SystemExit(B.serve(Path(sys.argv[1]).resolve()))
'''


class _SockClient:
    """One unix-socket bridge client. Reads are bounded (30s) so a bridge that
    stalls fails the check instead of hanging it."""

    def __init__(self, path: Path) -> None:
        import socket

        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(30)
        self.sock.connect(str(path))
        self.rd = self.sock.makefile("r", encoding="utf-8")

    def send(self, obj: dict) -> None:
        import json

        self.sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    def line(self) -> dict:
        import json

        raw = self.rd.readline()
        assert raw, "bridge closed the socket early"
        return json.loads(raw)

    def until(self, pred, seen: list | None = None, limit: int = 5000) -> dict:
        for _ in range(limit):
            ev = self.line()
            if seen is not None:
                seen.append(ev)
            if pred(ev):
                return ev
        raise AssertionError(f"event never arrived in {limit} lines")

    def close_hard(self) -> None:
        """RST, not FIN: the dead-client case the fan-out must survive."""
        import socket
        import struct

        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.sock.close()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _strip(ev: dict) -> dict:
    return {k: v for k, v in ev.items() if k not in ("seq", "ts")}


def _check_socket() -> None:
    """Track 3.1/3.3: unix-socket fan-out, late attach off the event log,
    double-drive serialization, dead-client survival, kill mid-step."""
    import json
    import os
    import sqlite3
    import subprocess
    import sys
    import tempfile
    import time

    def read_log(path: Path) -> list[dict]:
        # The bridge may be appending while this check reads. A trailing
        # fragment is not a durable JSONL record until its newline lands.
        return [json.loads(line) for line in path.read_bytes().split(b"\n")[:-1] if line]

    root = Path(__file__).resolve().parents[2]
    # Tripwire for the cwd= on the Popen below: the check must not leave a
    # byte in the runner's own .desmos.
    runner_dir = Path.cwd() / ".desmos" / "subagents"
    runner_before = set(runner_dir.glob("*")) if runner_dir.is_dir() else set()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cwd = tmp / "w"
        cwd.mkdir()
        boot = tmp / "boot.py"
        boot.write_text(_BOOT, encoding="utf-8")
        env = dict(os.environ)
        env["DESMOS_SETTINGS"] = str(tmp / "settings.json")
        env["PYTHONPATH"] = str(root)
        env["DESMOS_TOOL_SYSCALLS"] = "0"
        proc = subprocess.Popen(
            [sys.executable, str(boot), str(cwd)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # The real entrypoint chdirs to the workspace; without this the
            # kill-test child's process-cwd-relative .desmos/subagents record
            # lands in whatever repo the check runner happens to sit in.
            text=True, env=env, cwd=str(cwd),
        )
        a = b = None
        try:
            ready = json.loads(proc.stdout.readline())
            assert ready["ev"] == "ready", ready
            # The stdio wire keeps streaming every event; left unread it fills
            # the pipe and blocks _emit, stalling the sockets under test.
            import threading

            threading.Thread(
                target=lambda: [None for _ in proc.stdout], daemon=True
            ).start()

            sock_path = cwd / ".desmos" / "bridge.sock"
            assert sock_path.exists(), "bridge bound no socket"
            mode = sock_path.stat().st_mode & 0o777
            assert mode == 0o600, f"bridge.sock is {oct(mode)}, not 0600"

            # --- client A drives a step; the events fan out to its socket ---
            a = _SockClient(sock_path)
            a_events: list[dict] = []
            a.send({"op": "snapshot"})
            a.until(lambda e: e.get("ev") == "snapshot", seen=a_events)
            with sqlite3.connect(cwd / ".desmos" / "harness.sqlite3") as db:
                db.execute(
                    """
                    INSERT INTO channel_messages(
                        channel, run_id, author, body, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    ("conflicts", "peer-check", "worker-b", "persist.py conflict", "2026-01-01"),
                )
            channel = a.until(
                lambda e: e.get("ev") == "channel", seen=a_events
            )
            assert channel == {
                "ev": "channel",
                "channel": "conflicts",
                "author": "worker-b",
                "preview": "persist.py conflict",
                "unread": 1,
                "message_id": 1,
            }, channel

            a.send({"op": "step", "text": "ping"})
            a.until(lambda e: e.get("ev") == "snapshot", seen=a_events)
            assert any(e.get("ev") == "done" for e in a_events), a_events
            assert all("seq" not in e for e in a_events), (
                "live wire events are stamped; seq/ts belong to the log file only"
            )

            # --- late attach: client B replays the log from seq 0 ---
            b = _SockClient(sock_path)
            b.send({"op": "attach", "since": 0})
            header = b.line()
            assert header["ev"] == "session", header
            assert header["session_id"] and header["cwd"] == str(cwd.resolve()), header
            assert isinstance(header["ts"], int), header
            log_file = cwd / ".desmos" / "events" / f"{header['session_id']}.jsonl"
            assert log_file.is_file(), log_file
            replayed = [b.line() for _ in range(1 + len(a_events))]
            seqs = [e["seq"] for e in replayed]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs
            assert all(isinstance(e["ts"], int) for e in replayed), replayed[0]
            assert _strip(replayed[0])["ev"] == "ready"
            assert [_strip(e) for e in replayed[1:]] == a_events, (
                "replay does not match what client A saw live"
            )

            # --- double-drive: both clients step; the queue serializes ---
            a.send({"op": "step", "text": "ping"})
            b.send({"op": "step", "text": "ping"})
            both: list[dict] = []
            b.until(
                lambda e: sum(x.get("ev") == "snapshot" for x in both) == 2,
                seen=both,
            )
            assert not any(e.get("ev") == "error" for e in both), both
            prompts = [i for i, e in enumerate(both) if e.get("ev") == "prompt"]
            dones = [i for i, e in enumerate(both) if e.get("ev") == "done"]
            assert len(prompts) == 2 and len(dones) == 2, (prompts, dones)
            assert prompts[1] > dones[0], (
                "second step started before the first finished: the inbox "
                "queue no longer serializes the world"
            )
            # drain A to keep its buffer empty for the later steps
            a_drain: list[dict] = []
            a.until(lambda e: sum(x.get("ev") == "snapshot" for x in a_drain) == 2, seen=a_drain)

            # --- a dead client must not stall the step ---
            dead = _SockClient(sock_path)
            dead.send({"op": "attach", "since": 0})
            dead.line()  # one line proves it was attached and receiving
            dead.close_hard()
            a.send({"op": "step", "text": "ping"})
            a_after: list[dict] = []
            a.until(lambda e: e.get("ev") == "snapshot", seen=a_after)
            assert any(e.get("ev") == "done" for e in a_after), a_after

            # --- kill mid-step from client B settles the run ---
            a.send({"op": "step", "text": "kill test"})
            started = b.until(
                lambda e: e.get("ev") == "subagent" and e.get("phase") == "started"
            )
            rid = started["id"]
            b.send({"op": "kill_run", "id": rid})
            kill_events: list[dict] = []
            b.until(lambda e: e.get("ev") == "done", seen=kill_events)
            hits = [e for e in kill_events if e.get("ev") == "intervention"]
            assert hits and hits[0]["action"] == "kill_run" and hits[0]["id"] == rid, kill_events[:5]
            assert isinstance(hits[0]["result"], str) and hits[0]["result"], hits[0]
            assert any(
                e.get("ev") == "subagent" and e.get("phase") == "stopped" and e.get("id") == rid
                for e in kill_events
            ), "the killed run never settled as stopped"
            assert any(e.get("ev") == "notice" for e in kill_events), kill_events[-5:]

            # --- rerun routing: unknown id answers in prose, never raises ---
            b.send({"op": "rerun", "id": "zzzzzzzz"})
            answer = b.until(
                lambda e: e.get("ev") == "intervention" and e.get("action") == "rerun"
            )
            assert answer["id"] == "zzzzzzzz" and "zzzzzzzz" in answer["result"], answer

            # --- attach past the queue bound: replay is backpressured, so a
            # session longer than the 4096-line client queue still replays
            # whole and gapless instead of overflowing and dropping the client
            # (a's queue still holds the kill-test fan-out that was read via
            # b, snapshot included; drain it or the flood wait stops early)
            a.until(lambda e: e.get("ev") == "snapshot")
            before_flood = max(
                event.get("seq") or 0 for event in read_log(log_file)
            )
            a.send({"op": "step", "text": "flood test"})
            # The live queue is deliberately bounded, so a 6,000-event burst
            # may drop even a reading client on a slower runner. The event log
            # is the replay contract under test; wait for its terminal
            # snapshot instead of requiring the live queue to be unbounded.
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                logged_now = read_log(log_file)
                if any(
                    event.get("seq", 0) > before_flood and event.get("ev") == "snapshot"
                    for event in logged_now
                ):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("flood step never reached its logged snapshot")
            stamped_total = sum(e.get("seq") is not None for e in read_log(log_file))
            assert stamped_total > 4096, (
                f"flood produced only {stamped_total} events; the attach "
                f"ceiling under test starts at the 4096 queue bound"
            )
            fresh = _SockClient(sock_path)
            fresh.send({"op": "attach", "since": 0})
            assert fresh.line()["ev"] == "session"
            seqs2 = [fresh.line()["seq"] for _ in range(stamped_total)]
            assert seqs2 == list(range(1, stamped_total + 1)), (
                f"long attach lost events: {len(seqs2)} lines, first gap at "
                f"{next((i for i, s in enumerate(seqs2, 1) if s != i), None)}"
            )
            fresh.close()

            # the log holds everything, stamped, interventions included
            logged = read_log(log_file)
            assert logged[0]["ev"] == "session"
            log_seqs = [e["seq"] for e in logged[1:]]
            assert log_seqs == list(range(1, len(log_seqs) + 1)), "log seq has gaps"
            assert any(e.get("ev") == "intervention" and e.get("action") == "kill_run" for e in logged)
        finally:
            for c in (a, b):
                if c is not None:
                    c.close()
            try:
                proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
                proc.stdin.flush()
                proc.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait(timeout=10)
        # unlinked on exit: a stale path would make the next bridge probe it
        assert not sock_path.exists(), "bridge.sock survived bridge exit"
    runner_after = set(runner_dir.glob("*")) if runner_dir.is_dir() else set()
    assert runner_after <= runner_before, (
        f"the check leaked run records into the runner's cwd: "
        f"{sorted(p.name for p in runner_after - runner_before)}"
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
        bridge_env["OPENAI_API_KEY"] = "check-only"
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

            # The TUI pokes the inbox when a follow-up is queued, so that a step
            # parked on background work sees `has_input` and hands the turn back.
            # The op must be swallowed silently: an "unknown op" error here would
            # paint on the wire after every queued line. The picker reply that
            # follows is the proof nothing was emitted in between.
            proc.stdin.write(json.dumps({"op": "typed"}) + "\n")
            proc.stdin.flush()
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
        _check_release_tui_launcher()
    _check_socket()
