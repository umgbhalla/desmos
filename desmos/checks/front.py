"""Front checks: cli/tui launcher, acp, bridge, vendored-pager guarantees."""

from __future__ import annotations

from pathlib import Path


def _check_path_deps_tracked() -> None:
    """Every `path = ` dep in the root Cargo.toml is committed.

    vendor/grok-build is a committed gitlink, so its path deps are present after
    submodule initialization. The rest of the guarantee is one .gitignore line
    from being false, and it fails silently: `cargo build` works here because
    the files are on disk, then breaks for whoever clones next. It already
    happened once -- a bare `build/` in a global gitignore swallowed
    crates/build/xai-proto-build.

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
    present: list[tuple[str, Path]] = []
    for rel in sorted(deps):
        target = (root / rel / "Cargo.toml").resolve()
        if not target.exists():
            missing.append(f"{rel}/Cargo.toml does not exist")
        else:
            present.append((rel, target))

    # One git call for the whole set. `--error-unmatch` fails the batch if any
    # path is untracked, so the per-path loop below only runs when something is
    # actually wrong -- which is the case that can afford to be slow.
    # A path dep inside a committed submodule is tracked as a gitlink, which
    # ls-files on the file path cannot see. A fresh clone gets those sources
    # with `git submodule update --init`, so the guarantee this check exists
    # for still holds.
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-s"],
        capture_output=True, text=True, check=False,
    ).stdout
    submods = [
        line.split("\t", 1)[1]
        for line in listing.splitlines()
        if line.startswith("160000 ")
    ]
    suspect: list[tuple[str, Path]] = []
    if present:
        batch = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch",
             *[str(path) for _, path in present]],
            capture_output=True, check=False,
        )
        if batch.returncode != 0:
            suspect = present
    for rel, target in suspect:
        if any(rel == sub or rel.startswith(sub + "/") for sub in submods):
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
    """The pinned pager fork still carries our DESMOS_ACP branch.

    Moving the submodule gitlink to an incompatible fork commit can drop the
    branch while the crate still compiles. `--grok` then silently runs grok's
    own in-process agent instead of `python -m desmos acp`. Nothing else in the
    build says a word about it, so assert the two halves are present.
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
            return say('flooding\\n<exec op="python">\\n' + body + "\\n</exec>")
        return say("flood finished")
    if "kill test" in blob:
        if "<result" not in blob:
            body = "\\n".join([
                "from desmos.subagent import spawn, wait",
                'rid = spawn("loop forever", agent="explore", model="claude-opus-5")',
                "wait(rid, timeout=60)",
                'print("child settled")',
            ])
            return say('spawning\\n<exec op="python">\\n' + body + "\\n</exec>")
        return say("kill test finished")
    if "loop forever" in blob:
        return say('looping\\n<exec op="bash">sleep 0.3</exec>')
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
    # "sid" strips with the writer stamps: every live wire line carries its
    # session id (tui-redesign R3, asserted by _check_emit_sid), while SQL
    # replay rows stay unstamped -- so body comparisons drop it on both sides.
    stamped = {
        "seq", "ts", "mono_ns", "payload_bytes", "payload_sha256", "sid"
    }
    return {k: v for k, v in ev.items() if k not in stamped}


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

    from desmos.state.persist import peer_channel

    def read_log(path: Path, session_id: str) -> list[dict]:
        import sqlite3

        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT seq, ts_ms, mono_ns, payload_json,"
                " payload_bytes, payload_sha256"
                " FROM events WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        out = []
        for row in rows:
            event = json.loads(row["payload_json"])
            event.update({
                "seq": row["seq"], "ts": row["ts_ms"],
                "mono_ns": row["mono_ns"],
                "payload_bytes": row["payload_bytes"],
                "payload_sha256": row["payload_sha256"],
            })
            out.append(event)
        return out

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
                owner = db.execute(
                    "SELECT workspace_id, id FROM sessions ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                assert owner is not None
                db.execute(
                    """
                    INSERT INTO channel_messages(
                        workspace_id, session_id, channel, run_id,
                        author, body, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner[0], owner[1], "general", "peer-check",
                        "worker-b", "persist.py conflict", "2026-01-01",
                    ),
                )
            channel = a.until(
                lambda e: e.get("ev") == "channel", seen=a_events
            )
            assert _strip(channel) == {
                "ev": "channel",
                "channel": "general",
                "author": "worker-b",
                "preview": "persist.py conflict",
                "unread": 1,
                "message_id": 1,
            }, channel

            # A directed request must drive the real bridge loop without a local
            # composer submit, then return exactly one final reply to its sender.
            with sqlite3.connect(cwd / ".desmos" / "harness.sqlite3") as db:
                db.execute(
                    """
                    INSERT INTO channel_messages(
                        workspace_id, session_id, channel, run_id,
                        author, body, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner[0], owner[1], peer_channel(owner[1], "request"),
                        "peer-check", "peer-check", "please answer", "2026-01-02",
                    ),
                )
            directed_request = a.until(
                lambda e: e.get("ev") == "channel"
                and e.get("directed") == "request",
                seen=a_events,
            )
            assert directed_request["body"] == "please answer", directed_request
            a.until(lambda e: e.get("ev") == "snapshot", seen=a_events)
            with sqlite3.connect(cwd / ".desmos" / "harness.sqlite3") as db:
                replies = db.execute(
                    "SELECT run_id, body FROM channel_messages WHERE channel = ?",
                    (peer_channel("peer-check", "reply"),),
                ).fetchall()
            assert replies == [(owner[1], "pong")], replies

            # A returned reply wakes the sender to report it, but is not itself
            # auto-replied. That makes the exchange bounded by construction.
            with sqlite3.connect(cwd / ".desmos" / "harness.sqlite3") as db:
                db.execute(
                    """
                    INSERT INTO channel_messages(
                        workspace_id, session_id, channel, run_id,
                        author, body, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner[0], owner[1], peer_channel(owner[1], "reply"),
                        "peer-check", "peer-check", "thanks", "2026-01-03",
                    ),
                )
            directed_reply = a.until(
                lambda e: e.get("ev") == "channel"
                and e.get("directed") == "reply",
                seen=a_events,
            )
            assert directed_reply["body"] == "thanks", directed_reply
            a.until(lambda e: e.get("ev") == "snapshot", seen=a_events)
            with sqlite3.connect(cwd / ".desmos" / "harness.sqlite3") as db:
                reply_count = db.execute(
                    "SELECT COUNT(*) FROM channel_messages WHERE channel = ?",
                    (peer_channel("peer-check", "reply"),),
                ).fetchone()[0]
            assert reply_count == 1, "a peer reply triggered an autonomous loop"

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
            session_id = header["session_id"]
            log_file = cwd / ".desmos" / "harness.sqlite3"
            assert log_file.is_file(), log_file
            replayed = [b.line() for _ in range(1 + len(a_events))]
            seqs = [e["seq"] for e in replayed]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs
            assert all(isinstance(e["ts"], int) for e in replayed), replayed[0]
            assert _strip(replayed[0])["ev"] == "ready"
            assert [_strip(e) for e in replayed[1:]] == [
                _strip(e) for e in a_events
            ], (
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
                event.get("seq") or 0 for event in read_log(log_file, session_id)
            )
            a.send({"op": "step", "text": "flood test"})
            # The live queue is deliberately bounded, so a 6,000-event burst
            # may drop even a reading client on a slower runner. The event log
            # is the replay contract under test; wait for its terminal
            # snapshot instead of requiring the live queue to be unbounded.
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                logged_now = read_log(log_file, session_id)
                if any(
                    event.get("seq", 0) > before_flood and event.get("ev") == "snapshot"
                    for event in logged_now
                ):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("flood step never reached its logged snapshot")
            stamped_total = len(read_log(log_file, session_id))
            assert stamped_total > 4096, (
                f"flood produced only {stamped_total} events; the attach "
                f"ceiling under test starts at the 4096 queue bound"
            )
            fresh = _SockClient(sock_path)
            fresh.send({"op": "attach", "since": 0})
            first = fresh.line()
            assert first["ev"] == "session" and "seq" not in first
            seqs2 = [
                fresh.line()["seq"] for _ in range(stamped_total - 1)
            ]
            assert seqs2 == list(range(2, stamped_total + 1)), (
                f"long attach lost events: {len(seqs2)} lines, first gap at "
                f"{next((i for i, s in enumerate(seqs2, 1) if s != i), None)}"
            )
            fresh.close()

            # the log holds everything, stamped, interventions included
            logged = read_log(log_file, session_id)
            assert logged[0]["ev"] == "session"
            log_seqs = [e["seq"] for e in logged]
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




def _check_socket_peers() -> None:
    """R5 bridge half: {"op":"peers"} on the REAL socket answers the asking
    client (and only it) with one {"ev":"peers"} line listing the live
    workspace sessions -- session_id, kind, parent_id, seat_id, seen_at --
    backed by persist.peers()/active_runs with lease-held fake sessions."""
    import fcntl
    import json
    import os
    import sqlite3
    import subprocess
    import sys
    import tempfile
    from datetime import datetime, timezone

    root = Path(__file__).resolve().parents[2]
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
            text=True, env=env, cwd=str(cwd),
        )
        a = b = None
        leases = []
        try:
            ready = json.loads(proc.stdout.readline())
            assert ready["ev"] == "ready", ready
            import threading

            threading.Thread(
                target=lambda: [None for _ in proc.stdout], daemon=True
            ).start()

            sock_path = cwd / ".desmos" / "bridge.sock"
            assert sock_path.exists(), "bridge bound no socket"

            # Client A drives one turn so the bridge's own session row and
            # the workspace row exist before the fakes reference them.
            a = _SockClient(sock_path)
            a.send({"op": "snapshot"})
            a.until(lambda e: e.get("ev") == "snapshot")

            db_path = cwd / ".desmos" / "harness.sqlite3"
            now = datetime.now(timezone.utc).isoformat()
            with sqlite3.connect(db_path) as db:
                workspace, owner = db.execute(
                    "SELECT workspace_id, id FROM sessions"
                    " ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                fakes = [
                    ("run-peer-1", "sess-peer-1", "fork", owner, "seat-9"),
                    ("run-peer-2", "sess-peer-2", "child", owner, ""),
                ]
                for run, sess, kind, parent, seat in fakes:
                    db.execute(
                        "INSERT INTO sessions(id, workspace_id, parent_id,"
                        " kind, started_at, last_seen_at, model, thinking,"
                        " cache_key, seat_id)"
                        " VALUES (?, ?, ?, ?, ?, ?, '', '', ?, ?)",
                        (sess, workspace, parent, kind, now, now, sess, seat),
                    )
                    db.execute(
                        "INSERT INTO active_runs(run_id, workspace_id,"
                        " session_id, pid, cwd, generation, model,"
                        " started_at, seen_at)"
                        " VALUES (?, ?, ?, 0, ?, 0, 'fake', ?, ?)",
                        (run, workspace, sess, str(cwd), now, now),
                    )
            # Hold the presence leases from THIS process: peers() probes the
            # flock and prunes any row whose lease is free, so an unheld fake
            # would vanish instead of proving the wire shape.
            presence = cwd / ".desmos" / "presence"
            presence.mkdir(parents=True, exist_ok=True)
            for run, *_ in fakes:
                fh = (presence / f"{run}.lock").open("a+")
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                leases.append(fh)

            # Client B joins the fan-out first; the peers answer must miss it.
            b = _SockClient(sock_path)
            b.send({"op": "snapshot"})
            b.until(lambda e: e.get("ev") == "snapshot")

            a.send({"op": "peers"})
            reply = a.until(lambda e: e.get("ev") == "peers")
            assert "sid" in reply, f"peers line missing sid: {reply}"
            by_sess = {p["session_id"]: p for p in reply["peers"]}
            assert "sess-peer-1" in by_sess and "sess-peer-2" in by_sess, by_sess
            p1, p2 = by_sess["sess-peer-1"], by_sess["sess-peer-2"]
            for p in (p1, p2):
                for field in ("session_id", "run_id", "kind", "parent_id",
                              "seat_id", "seen_at", "self"):
                    assert field in p, f"peer entry missing {field}: {p}"
            assert p1["kind"] == "fork" and p1["parent_id"] == owner, p1
            assert p1["seat_id"] == "seat-9" and p1["self"] is False, p1
            assert p2["kind"] == "child" and p2["seat_id"] is None, p2
            assert p1["run_id"] == "run-peer-1" and p1["seen_at"], p1
            assert owner in by_sess and by_sess[owner]["self"] is True, by_sess

            # Isolation: B sees the fan-out either side of the reply but
            # never the peers line itself.
            b_events: list[dict] = []
            b.send({"op": "snapshot"})
            b.until(lambda e: e.get("ev") == "snapshot", seen=b_events)
            leaked = [e for e in b_events if e.get("ev") == "peers"]
            assert not leaked, f"peers answer leaked to the fan-out: {leaked}"
        finally:
            for fh in leases:
                fh.close()
            for c in (a, b):
                if c is not None:
                    c.close()
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _check_decision_events(cwd: Path) -> None:
    """Decision dispatch changes are forwarded and snapshots replay open ones."""
    import os

    from desmos.front.bridge import _snapshot
    from desmos.kernel.loop import new_world, turn
    from desmos.state.decisions import pending

    world = new_world(cwd, state_path=cwd / "decision-harness.json", ns={})
    world.model = "claude-opus-5"
    replies = iter([
        '<knowledge op="decide">ask Deploy now? | yes | no</knowledge>',
        None,
    ])

    def complete(model, system, messages, max_tokens):
        text = next(replies)
        if text is None:
            did = pending(world)[0]["id"]
            text = f'<knowledge op="decide">answer {did} yes</knowledge>'
        return {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    world.complete_fn = complete
    events: list[dict] = []
    messages: list[dict] = []
    old_typed = os.environ.get("DESMOS_TOOL_SYSCALLS")
    os.environ["DESMOS_TOOL_SYSCALLS"] = "0"
    try:
        turn(world, messages, 1024, emit=events.append)
    finally:
        if old_typed is None:
            os.environ.pop("DESMOS_TOOL_SYSCALLS", None)
        else:
            os.environ["DESMOS_TOOL_SYSCALLS"] = old_typed
    opened = [e for e in events if e.get("ev") == "decision"]
    assert len(opened) == 1 and opened[0] == {
        "ev": "decision",
        "id": opened[0]["id"],
        "prompt": "Deploy now?",
        "options": ["yes", "no"],
        "status": "open",
        "answer": None,
    }, opened
    assert _snapshot(world)["decisions"] == pending(world)
    os.environ["DESMOS_TOOL_SYSCALLS"] = "0"
    try:
        turn(world, messages, 1024, n=2, emit=events.append)
    finally:
        if old_typed is None:
            os.environ.pop("DESMOS_TOOL_SYSCALLS", None)
        else:
            os.environ["DESMOS_TOOL_SYSCALLS"] = old_typed
    changed = [e for e in events if e.get("ev") == "decision"]
    assert changed[-1] == {
        **opened[0],
        "status": "answered",
        "answer": "yes",
    }, changed
    assert pending(world) == [], pending(world)

def _check_daemon() -> None:
    """--daemon: detach with a pid file, serve socket-only, clean SIGTERM.

    Driven through the real entry point (`python -m desmos bridge --daemon`),
    never the internal functions.
    """
    import os
    import signal
    import subprocess
    import sys
    import tempfile
    import time

    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cwd = tmp / "w"
        cwd.mkdir()
        env = dict(os.environ)
        env["DESMOS_SETTINGS"] = str(tmp / "settings.json")
        env["PYTHONPATH"] = str(root)
        env["OPENAI_API_KEY"] = "check-only"
        env["DESMOS_TOOL_SYSCALLS"] = "0"
        argv = [sys.executable, "-m", "desmos", "bridge", "--cwd", str(cwd), "--daemon"]
        first = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, env=env, cwd=str(cwd)
        )
        assert first.returncode == 0, (first.returncode, first.stdout, first.stderr)
        pid = int(first.stdout.strip())
        dot = cwd / ".desmos"
        pid_file = dot / "bridge.pid"
        assert pid_file.is_file(), "parent exited 0 but bridge.pid is missing"
        assert int(pid_file.read_text(encoding="utf-8")) == pid

        def log_tail() -> str:
            log = dot / "bridge.log"
            return log.read_text(encoding="utf-8")[-2000:] if log.is_file() else "<no log>"

        # The parent returns before the daemon binds; poll, bounded.
        sock_path = dot / "bridge.sock"
        deadline = time.time() + 30
        while not sock_path.exists():
            assert time.time() < deadline, f"daemon never bound the socket; log: {log_tail()}"
            time.sleep(0.05)
        try:
            client = _SockClient(sock_path)
            try:
                client.send({"op": "attach", "since": 0})
                ready = client.until(lambda e: e.get("ev") == "ready")
                # macOS tempdirs live behind the /var -> /private/var symlink.
                assert Path(ready["cwd"]).resolve() == cwd.resolve(), ready
            finally:
                client.close()

            # A second --daemon in the same cwd must refuse, loudly.
            second = subprocess.run(
                argv, capture_output=True, text=True, timeout=60, env=env, cwd=str(cwd)
            )
            assert second.returncode == 1, (second.returncode, second.stdout, second.stderr)
            resolved = cwd.resolve() / ".desmos" / "bridge.sock"
            assert f"bridge already serving {resolved}" in second.stderr, second.stderr
            assert pid_file.is_file(), "the refused second daemon clobbered bridge.pid"
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.time() + 30
        while pid_file.exists() or sock_path.exists():
            assert time.time() < deadline, (
                f"SIGTERM cleanup incomplete: pid_file={pid_file.exists()} "
                f"sock={sock_path.exists()}; log: {log_tail()}"
            )
            time.sleep(0.05)


def _check_daemon_shutdown() -> None:
    """{"op":"shutdown"} over the socket: quit authority for a daemon bridge.

    A --daemon bridge has no stdio owner, so the socket op must take the
    whole process down cleanly -- pid file removed, owned socket unlinked --
    and a second shutdown while stopping must be harmless.
    """
    import os
    import signal
    import subprocess
    import sys
    import tempfile
    import time

    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cwd = tmp / "w"
        cwd.mkdir()
        env = dict(os.environ)
        env["DESMOS_SETTINGS"] = str(tmp / "settings.json")
        env["PYTHONPATH"] = str(root)
        env["OPENAI_API_KEY"] = "check-only"
        env["DESMOS_TOOL_SYSCALLS"] = "0"
        argv = [sys.executable, "-m", "desmos", "bridge", "--cwd", str(cwd), "--daemon"]
        first = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, env=env, cwd=str(cwd)
        )
        assert first.returncode == 0, (first.returncode, first.stdout, first.stderr)
        pid = int(first.stdout.strip())
        dot = cwd / ".desmos"
        pid_file = dot / "bridge.pid"
        sock_path = dot / "bridge.sock"

        def log_tail() -> str:
            log = dot / "bridge.log"
            return log.read_text(encoding="utf-8")[-2000:] if log.is_file() else "<no log>"

        deadline = time.time() + 30
        while not sock_path.exists():
            assert time.time() < deadline, f"daemon never bound the socket; log: {log_tail()}"
            time.sleep(0.05)
        try:
            client = _SockClient(sock_path)
            second = _SockClient(sock_path)
            try:
                client.send({"op": "attach", "since": 0})
                client.until(lambda e: e.get("ev") == "ready")
                client.send({"op": "shutdown"})
                # Idempotent: a second shutdown while stopping is harmless.
                try:
                    second.send({"op": "shutdown"})
                except OSError:
                    pass  # the bridge may already be gone; that is the point
                # The final notice is best-effort on the wire (process exit
                # races the writer thread) but durable in the event log.
            finally:
                client.close()
                second.close()

            # The daemon process itself must exit...
            deadline = time.time() + 30
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                assert time.time() < deadline, f"daemon survived shutdown; log: {log_tail()}"
                time.sleep(0.05)
            # ...and the SIGTERM-path cleanup must have run.
            deadline = time.time() + 30
            while pid_file.exists() or sock_path.exists():
                assert time.time() < deadline, (
                    f"shutdown cleanup incomplete: pid_file={pid_file.exists()} "
                    f"sock={sock_path.exists()}; log: {log_tail()}"
                )
                time.sleep(0.05)
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def _check_emit_sid() -> None:
    """R3 wire half: every line the real _emit writes carries "sid" equal to
    the session header's session_id, exactly once, for plain events and child
    envelopes alike; the caller's dict and the persisted row stay unstamped."""
    import io
    import json
    import os

    import desmos.front.bridge as B
    import desmos.state.persist as P

    logged: list[dict] = []

    def fake_record(world, ev, **kw):
        logged.append(dict(ev))
        return len(logged)

    buf = io.StringIO()
    old_wire, old_dead = B._WIRE, B._WIRE_DEAD
    old_record = P.record_event
    old_sid = os.environ.get("DESMOS_SESSION_ID")
    plain = {"ev": "notice", "text": "plain"}
    child = {"ev": "agent", "id": "c1", "parent": "root", "depth": 1, "text": "hi"}
    try:
        os.environ["DESMOS_SESSION_ID"] = "sid-check-1234"
        B._WIRE, B._WIRE_DEAD = buf, False
        P.record_event = fake_record
        B._open_log(type("W", (), {"cwd": Path("/tmp")})())
        B._emit(plain)
        B._emit(child)
        B._emit(plain)  # reuse: the same dict emitted again must not double-stamp
    finally:
        B._WIRE, B._WIRE_DEAD = old_wire, old_dead
        B._LOG_WORLD = None
        P.record_event = old_record
        if old_sid is None:
            os.environ.pop("DESMOS_SESSION_ID", None)
        else:
            os.environ["DESMOS_SESSION_ID"] = old_sid

    header = logged[0]
    assert header["ev"] == "session" and header["session_id"] == "sid-check-1234"
    lines = buf.getvalue().splitlines()
    assert len(lines) == 3, lines
    for line in lines:
        ev = json.loads(line)
        assert ev["sid"] == header["session_id"], ev
        assert line.count('"sid"') == 1, line
    envelope = json.loads(lines[1])
    assert envelope["id"] == "c1" and envelope["parent"] == "root"
    assert envelope["depth"] == 1 and envelope["ev"] == "agent"
    # The caller's dict was copied, not mutated, and persistence is unstamped.
    assert "sid" not in plain and "sid" not in child
    assert all("sid" not in row for row in logged[1:])


def _check_replay_sid() -> None:
    """R3 replay half: the REAL _replay path stamps every replayed line with
    the same "sid" live lines carry -- persist events, attach with since=0
    through _replay/_Client, and compare against a live _emit line."""
    import io
    import json
    import os
    import socket
    import time as _time

    import desmos.front.bridge as B
    import desmos.state.persist as P

    logged: list[dict] = []

    def fake_record(world, ev, **kw):
        logged.append(dict(ev))
        return len(logged)

    def fake_read(world, since=0, limit=4096, session=None):
        out = []
        for i, ev in enumerate(logged, start=1):
            if i <= int(since) or len(out) >= limit:
                continue
            row = dict(ev)
            row.update({
                "seq": i,
                "ts": 1,
                "mono_ns": 1,
                "payload_bytes": 0,
                "payload_sha256": "x",
            })
            out.append(row)
        return out

    buf = io.StringIO()
    old_wire, old_dead = B._WIRE, B._WIRE_DEAD
    old_record, old_read = P.record_event, P.read_events
    old_sid = os.environ.get("DESMOS_SESSION_ID")
    a, b = socket.socketpair()
    client = None
    try:
        os.environ["DESMOS_SESSION_ID"] = "sid-replay-5678"
        B._WIRE, B._WIRE_DEAD = buf, False
        P.record_event = fake_record
        P.read_events = fake_read
        B._open_log(type("W", (), {"cwd": Path("/tmp")})())
        B._emit({"ev": "notice", "text": "one"})
        B._emit({"ev": "agent", "id": "c1", "parent": "root", "depth": 1})
        client = B._Client(a)
        B._replay(client, 0)  # the real attach replay path
        assert client in B._CLIENTS  # joined live fan-out gaplessly
        B._emit({"ev": "notice", "text": "live-after-attach"})
        b.settimeout(5.0)
        raw = b""
        deadline = _time.time() + 5.0
        while raw.count(b"\n") < 4 and _time.time() < deadline:
            chunk = b.recv(65536)
            if not chunk:
                break
            raw += chunk
        lines = raw.decode("utf-8").splitlines()
        assert len(lines) == 4, lines  # session header + 2 events + 1 live
        live = json.loads(buf.getvalue().splitlines()[-1])
        assert live["ev"] == "notice" and live["sid"] == "sid-replay-5678"
        header = json.loads(lines[0])
        assert header["ev"] == "session", header
        assert header["session_id"] == live["sid"] and "sid" not in header
        for line in lines[1:]:
            ev = json.loads(line)
            assert ev["sid"] == live["sid"], ev
            assert line.count('"sid"') == 1, line
        for line in lines:
            ev = json.loads(line)
            assert "payload_bytes" not in ev and "payload_sha256" not in ev
        # Persisted rows themselves stay unstamped.
        assert all("sid" not in row for row in logged)
    finally:
        B._WIRE, B._WIRE_DEAD = old_wire, old_dead
        B._LOG_WORLD = None
        P.record_event, P.read_events = old_record, old_read
        if client is not None and client in B._CLIENTS:
            B._CLIENTS.remove(client)
        if client is not None:
            client.close()
        b.close()
        if old_sid is None:
            os.environ.pop("DESMOS_SESSION_ID", None)
        else:
            os.environ["DESMOS_SESSION_ID"] = old_sid


def check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        _check_decision_events(cwd)
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
                    {"type": "text", "text": '<exec op="python">1+1</exec>'},
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
        assert tool["title"] == "exec" and tool["kind"] == "execute"

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

        from desmos.front.bridge import _bind_socket

        owner_dir = cwd / "socket-owner"
        owner, already_owned = _bind_socket(owner_dir)
        assert owner is not None and not already_owned
        try:
            follower, already_owned = _bind_socket(owner_dir)
            assert follower is None and already_owned
        finally:
            owner.close()
            (owner_dir / ".desmos" / "bridge.sock").unlink(missing_ok=True)

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
            # A refusal retires the TUI's "queued" badge; without this event
            # a mid-step switch that fails would stay painted forever.
            gone = json.loads(proc.stdout.readline())
            assert gone["ev"] == "model_rejected" and gone["model"] == "gpt-9-nope", gone

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

            # Switching channels in the TUI sends these three ops. They lived
            # only on the socket loop, so a stdio-attached front got
            # "unknown op 'channel_read'" for every click on the rail. Drive
            # the real stdin reader, not the helper, or the gap comes back.
            proc.stdin.write(json.dumps({"op": "roster"}) + "\n")
            proc.stdin.flush()
            roster = json.loads(proc.stdout.readline())
            assert roster["ev"] == "roster" and roster["version"] == 1, roster
            names = {row["channel"] for row in roster["channels"]}
            assert {"general", "build", "ops"} <= names, names
            kinds = {row["name"]: row["kind"] for row in roster["agents"]}
            assert kinds.get("main") == "chief", kinds
            proc.stdin.write(
                json.dumps({"op": "channel_read", "channel": "build"}) + "\n"
            )
            proc.stdin.flush()
            story = json.loads(proc.stdout.readline())
            assert story["ev"] == "channel_story", story
            assert story["channel"] == "build", story
            # ...and no machine is seeded into it. A bot row exists only after
            # a live host's presence has been ingested, which is what makes
            # the mention route both ways; the state check drives that path.
            assert not [n for n, k in kinds.items() if k == "bot"], kinds
        finally:
            proc.stdin.write(json.dumps({"op": "quit"}) + "\n")
            proc.stdin.flush()
            proc.wait(timeout=20)

        # --- bridge: steer without stop, and pause that freezes ---
        # The REAL reader loop (`_read_ops`, the body of the stdin thread) fed
        # real NDJSON lines. A steer must land in world.steers -- not the inbox,
        # not cancel -- so the running loop delivers it at its next boundary.
        import io as _io
        import queue
        import threading as _th
        import time as _time

        import desmos.front.bridge as _B
        from desmos.kernel.catalog import drain_steers as _drain

        steer_cwd = cwd / "steercwd"
        steer_cwd.mkdir(exist_ok=True)
        sworld = _B.new_world(steer_cwd)
        sinbox: "queue.Queue[dict | None]" = queue.Queue()
        scancel, spause = _th.Event(), _th.Event()
        lines = "".join(
            json.dumps(m) + "\n"
            for m in (
                {"op": "steer", "text": "redirect to X"},
                {"op": "pause"},
                {"op": "steer", "text": "  "},
                {"op": "steer", "text": "and then Y"},
                {"op": "resume"},
                {"op": "snapshot"},
            )
        )
        wire = _B._WIRE
        _B._WIRE = _io.StringIO()
        try:
            _B._read_ops(_io.StringIO(lines), sworld, sinbox, scancel, spause)
            emitted = [json.loads(x) for x in _B._WIRE.getvalue().splitlines() if x.strip()]
        finally:
            _B._WIRE = wire
        assert not scancel.is_set(), "a steer must never touch cancel"
        assert not spause.is_set(), "resume must clear the pause"
        assert sworld.steers == ["redirect to X", "and then Y"], sworld.steers
        # Only the ordinary op queues; the out-of-band ones were answered inline.
        queued = []
        while not sinbox.empty():
            queued.append(sinbox.get_nowait())
        assert queued == [{"op": "snapshot"}, None], queued
        assert any(e.get("ev") == "error" and "empty steer" in e.get("text", "") for e in emitted), emitted
        assert [e["text"] for e in emitted if e.get("ev") == "notice"] == [
            "steer queued", "session paused", "steer queued", "session resumed",
        ], emitted
        # The kernel seam: the loop's drain is what delivers them.
        assert _drain(sworld) == ["redirect to X", "and then Y"]
        assert sworld.steers == []

        # Pause freezes at the turn boundary: the should_stop gate blocks while
        # paused and returns False (not a stop) once resumed.
        gate = _B._pause_gate(scancel, spause)
        assert gate() is False
        spause.set()
        landed: list[bool] = []
        waiter = _th.Thread(target=lambda: landed.append(gate()), daemon=True)
        waiter.start()
        _time.sleep(0.3)
        assert waiter.is_alive() and not landed, "pause did not block the step"
        spause.clear()
        waiter.join(timeout=5)
        assert landed == [False], landed  # released, and never reported a stop

        # The committed gitlink pins grok-build after submodule initialization.
        # What can still go missing is the DESMOS_ACP branch if that gitlink is
        # moved to an incompatible fork commit -- and that is silent, because
        # the pager compiles either way and runs grok's agent instead of ours.
        _check_path_deps_tracked()
        _check_vendor_patch()
        _check_release_tui_launcher()
    _check_emit_sid()
    _check_replay_sid()
    _check_socket()
    _check_socket_peers()
    _check_daemon()
    _check_daemon_shutdown()
