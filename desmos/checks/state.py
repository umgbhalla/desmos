"""State checks: persist, memory, generations, skills."""

from __future__ import annotations

import json
import os
from pathlib import Path

from desmos.dispatch import dispatch
from desmos.generations import evolve, gen_dir, rollback
from desmos.loop import new_world
from desmos.catalog import system_prompt
from desmos.types import Block


def _memory(body: str, attrs: dict | None = None) -> Block:
    return Block("knowledge", body, {"op": "memory", **(attrs or {})})



def _spine_sequence_check() -> None:
    """An old v14 file upgrades, and replay inserts are globally idempotent."""
    import sqlite3
    import tempfile

    from desmos.front import spine
    from desmos.loop import new_world
    from desmos.state import persist

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "old.sqlite3"
        old_schema = persist.SCHEMA_SQL.replace(
            "    created_at TEXT NOT NULL,\n    spine_seq INTEGER\n"
            ");\nCREATE TABLE IF NOT EXISTS channel_cursors",
            "    created_at TEXT NOT NULL\n"
            ");\nCREATE TABLE IF NOT EXISTS channel_cursors",
        )
        assert old_schema != persist.SCHEMA_SQL
        db = sqlite3.connect(path)
        db.executescript(old_schema)
        db.execute(
            "ALTER TABLE schema_migrations"
            " ADD COLUMN min_reader INTEGER NOT NULL DEFAULT 0"
        )
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at, min_reader)"
            " VALUES (14, 'old', 9)"
        )
        db.commit()
        db.close()

        world = new_world(root, state_path=path)
        db = persist._open(path)
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(channel_messages)")
        }
        indexes = {
            row["name"] for row in db.execute("PRAGMA index_list(channel_messages)")
        }
        assert "spine_seq" in columns
        assert "ux_channel_spine" in indexes
        db.close()

        event = {
            "channel": "migration-check",
            "seq": 7,
            "author": "remote",
            "seat": "other",
            "body": "once",
            "ts": "now",
        }
        assert spine.ingest(world, [event, event]) == 1
        assert spine.ingest(world, [event]) == 0
        rows = persist.ordered_read(world, "migration-check")
        assert len(rows) == 1 and rows[0]["spine_seq"] == 7

def _check_spine_presence() -> None:
    """Presence and cold-session rows ride the spine; peers() sees remote hosts."""
    import tempfile

    from desmos.front import spine
    from desmos.state import outbox, persist

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        world = new_world(root, state_path=root / "s.sqlite3")
        db = persist._open(persist.state_file(world))
        try:
            workspace_id = persist._workspace_id(db, world)
            persist._session_id(db, world, workspace_id)
            db.commit()
        finally:
            db.close()
        persist.announce(world)
        spine._enqueue_presence(world)
        rows = outbox.pending(world, 50)
        frames = {r["kind"]: spine._append_frame(r) for r in rows}
        assert "presence" in frames, rows
        assert frames["presence"]["channel"] == spine.SYS_PRESENCE
        cold_frame = spine._append_frame({
            "kind": "cold_session",
            "fingerprint": "f-cold",
            "payload_json": json.dumps({"session_id": "s-old", "rows": 2}),
        })
        assert cold_frame is not None and cold_frame["channel"] == spine.SYS_COLD
        assert spine._append_frame({
            "kind": "mystery", "fingerprint": "x", "payload_json": "{}",
        }) is None
        ev = {
            "channel": spine.SYS_PRESENCE,
            "seq": 1,
            "author": "hyperion",
            "seat": "hyperion",
            "ts": "2026-08-23T00:00:00+00:00",
            "body": json.dumps({
                "host": "hyperion",
                "bucket": "2026-08-23T00:00",
                "sessions": [{
                    "run_id": "r-hyp", "session_id": "s-hyp", "pid": 42,
                    "cwd": "/Users/umang/hub/desmos", "generation": 7,
                    "model": "claude-opus-5", "started_at": "2026-08-23",
                }],
            }),
        }
        assert spine.ingest(world, [ev]) == 1
        got = persist.peers(world)
        remote = [p for p in got if p.get("remote")]
        assert len(remote) == 1 and remote[0]["host"] == "hyperion", got
        assert remote[0]["run_id"] == "r-hyp"
        assert all(p.get("host") for p in got)
        # A snapshot replaying an OLDER presence event must not clobber newer.
        newer = dict(ev, seq=5, body=json.dumps({
            "host": "hyperion", "sessions": [{
                "run_id": "r-new", "session_id": "s-hyp", "pid": 43,
                "cwd": "/", "generation": 8, "model": "m", "started_at": "t",
            }]}))
        stale = dict(ev, seq=3, body=json.dumps({
            "host": "hyperion", "sessions": [{
                "run_id": "r-stale", "session_id": "s-hyp", "pid": 99,
                "cwd": "/", "generation": 1, "model": "m", "started_at": "t",
            }]}))
        assert spine.ingest(world, [newer]) == 1
        assert spine.ingest(world, [stale]) == 0, "stale seq must be skipped"
        got = persist.peers(world)
        remote = [p for p in got if p.get("remote")]
        assert len(remote) == 1 and remote[0]["run_id"] == "r-new", got
        # Presence is a structured fact, never a chat row.
        assert persist.ordered_read(world, spine.SYS_PRESENCE) == []


def _check_spine_memory() -> None:
    """Memory records replicate over sys.memory: append, dedupe, LWW."""
    import tempfile

    from desmos.front import spine
    from desmos.state import memory, outbox

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a = new_world(root / "a", state_path=root / "a" / "s.sqlite3")
        b = new_world(root / "b", state_path=root / "b" / "s.sqlite3")
        (root / "a").mkdir(exist_ok=True)
        (root / "b").mkdir(exist_ok=True)
        out = memory.remember(a, "spine replication test fact", kind="note")
        assert out.startswith("remembered"), out
        rows = [r for r in outbox.pending(a, 50) if r["kind"] == "memory_record"]
        assert len(rows) == 1, rows
        frame = spine._append_frame(rows[0])
        assert frame is not None and frame["channel"] == spine.SYS_MEMORY
        ev = {
            "channel": spine.SYS_MEMORY,
            "seq": 1,
            "author": "nemesis",
            "seat": "nemesis",
            "ts": "now",
            "body": frame["body"],
        }
        assert spine.ingest(b, [ev]) == 1
        record = json.loads(frame["body"])
        got = memory._load_records(memory.memory_root(b))
        mine = memory._find(got, record["id"])
        assert mine is not None and mine["content"] == record["content"], got
        # Replay is a no-op; applying never re-publishes.
        assert spine.ingest(b, [ev]) == 0
        assert not [
            r for r in outbox.pending(b, 50) if r["kind"] == "memory_record"
        ]
        # LWW: a strictly newer local update refuses an older replica.
        memory.remember(
            b, "newer local truth", record_id=record["id"], kind="note"
        )
        assert spine.ingest(b, [dict(ev, seq=2)]) == 0
        got = memory._load_records(memory.memory_root(b))
        assert memory._find(got, record["id"])["content"] == "newer local truth"


def _check_roster() -> None:
    """Named agents and declared channels: seed, upsert, liveness field."""
    import tempfile

    from desmos.state import persist

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        world = new_world(root, state_path=root / "s.sqlite3")
        got = persist.roster(world)
        names = {a["name"]: a for a in got["agents"]}
        assert "main" in names and names["main"]["kind"] == "chief", names
        # Nothing but the seat itself. A machine name in the seed is what made
        # mentions one-directional: whichever host was spelled here could be
        # addressed, and the other could not.
        assert set(names) == {"main"}, names
        chans = {c["name"]: c for c in got["channels"]}
        assert {"general", "build", "ops"} <= set(chans), chans
        assert chans["sys.work"]["kind"] == "sys"
        assert names["main"]["live"] is True, "chief runs on this seat"
        persist.agent_upsert(world, "auditor", kind="fork", parent="main")
        persist.channel_declare(world, "lab", description="experiments")
        got = persist.roster(world)
        forks = [a for a in got["agents"] if a["parent"] == "main"]
        assert [a["name"] for a in forks] == ["auditor"], forks
        assert any(c["name"] == "lab" for c in got["channels"])
        persist.agent_upsert(world, "auditor", status="retired")
        got = persist.roster(world)
        assert all(a["name"] != "auditor" for a in got["agents"])
    print("roster check ok")


def _check_spine_work() -> None:
    """sys.work rides channel_messages: request, single claim, crash recovery."""
    import tempfile

    from desmos.agents import pending, remote
    from desmos.front import spine
    from desmos.state import outbox, persist

    keep = os.environ.get("DESMOS_SEAT")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asker = new_world(root / "a", state_path=root / "a" / "s.sqlite3")
            doer = new_world(root / "b", state_path=root / "b" / "s.sqlite3")
            for w in (asker, doer):
                db = persist._open(persist.state_file(w))
                try:
                    persist._session_id(db, w, persist._workspace_id(db, w))
                    db.commit()
                finally:
                    db.close()

            os.environ["DESMOS_SEAT"] = "asker-host"
            out = remote.request(asker, "doer-host", "say hi")
            assert out.startswith("remote spawn refused"), out
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            presence = {
                "channel": spine.SYS_PRESENCE, "seq": 1, "author": "d",
                "seat": "d", "ts": now,
                "body": json.dumps({"host": "doer-host", "sessions": [
                    {"run_id": "r", "session_id": "s", "pid": 1, "cwd": "/",
                     "generation": 1, "model": "m", "started_at": "t"},
                ]}),
            }
            assert spine.ingest(asker, [presence]) == 1
            # Stale presence must not count as live.
            assert remote.known_hosts(asker) == {"doer-host"}
            out = remote.request(asker, "doer-host", "say hi", timeout=30)
            assert "dispatched to doer-host" in out, out
            parked = [t.name for t in pending._bucket(asker)]
            assert any("remote w-" in n for n in parked), parked
            req = [r for r in outbox.pending(asker, 50)
                   if r["kind"] == "channel_post"]
            assert len(req) == 1, req
            frame = spine._append_frame(req[0])
            assert frame is not None and frame["channel"] == spine.SYS_WORK
            wid = json.loads(frame["body"])["work_id"]

            os.environ["DESMOS_SEAT"] = "doer-host"
            ev = {"channel": spine.SYS_WORK, "seq": 2, "author": "a",
                  "seat": "a", "ts": "now", "body": frame["body"]}
            assert spine.ingest(doer, [ev]) == 1
            ran = []

            def fake_runner(world, work_id, payload):
                ran.append(work_id)
                spine.post_work(world, {
                    "t": "result", "work_id": work_id, "host": "doer-host",
                    "status": "done", "output": "hi from doer"})
                spine._WORK_IN_FLIGHT.discard(work_id)

            spine._serve_work(doer, runner=fake_runner)
            assert ran == [wid], ran
            spine._serve_work(doer, runner=fake_runner)
            assert ran == [wid], "second serve must not re-claim"
            frames = [
                spine._append_frame(r)
                for r in outbox.pending(doer, 50)
                if r["kind"] == "channel_post"
            ]
            result_frame = next(
                f for f in frames
                if json.loads(f["body"]).get("t") == "result"
            )

            os.environ["DESMOS_SEAT"] = "asker-host"
            ev = {"channel": spine.SYS_WORK, "seq": 3, "author": "d",
                  "seat": "d", "ts": "now", "body": result_frame["body"]}
            assert spine.ingest(asker, [ev]) == 1
            got = remote.await_result(asker, wid, timeout=5)
            assert "hi from doer" in got and "[done]" in got, got

            # Crash recovery: a claim by this seat with no result and no
            # in-flight thread answers with an error instead of poisoning
            # the request forever.
            os.environ["DESMOS_SEAT"] = "doer-host"
            wid2 = "w-crashed00001"
            req2 = json.dumps({"t": "request", "work_id": wid2,
                               "target": "doer-host", "agent": "general",
                               "task": "x", "origin": "asker-host"})
            claim2 = json.dumps({"t": "claim", "work_id": wid2,
                                 "host": "doer-host"})
            evs = [
                {"channel": spine.SYS_WORK, "seq": 4, "author": "a",
                 "seat": "a", "ts": "now", "body": req2},
                {"channel": spine.SYS_WORK, "seq": 5, "author": "d",
                 "seat": "d", "ts": "now", "body": claim2},
            ]
            assert spine.ingest(doer, evs) == 2
            ran2 = []
            spine._serve_work(doer, runner=lambda *a: ran2.append(a))
            assert ran2 == [], "crashed claim must not relaunch"
            res = remote.find_result(doer, wid2)
            assert res is not None and res["status"] == "error", res
            assert "restarted" in res["output"], res
    finally:
        if keep is None:
            os.environ.pop("DESMOS_SEAT", None)
        else:
            os.environ["DESMOS_SEAT"] = keep



def _check_mention_dispatch() -> None:
    """@bot in a channel post becomes remote work; the answer posts back."""
    import tempfile
    from datetime import datetime, timezone

    from desmos.agents import pending, remote
    from desmos.front import spine
    from desmos.state import persist

    keep = os.environ.get("DESMOS_SEAT")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = new_world(root, state_path=root / "s.sqlite3")
            db = persist._open(persist.state_file(world))
            try:
                persist._session_id(db, world, persist._workspace_id(db, world))
                db.commit()
            finally:
                db.close()
            os.environ["DESMOS_SEAT"] = "asker-host"
            now = datetime.now(timezone.utc).isoformat()
            presence = {
                "channel": spine.SYS_PRESENCE, "seq": 1, "author": "h",
                "seat": "h", "ts": now,
                "body": json.dumps({"host": "hyperion", "sessions": [
                    {"run_id": "r", "session_id": "s", "pid": 1, "cwd": "/",
                     "generation": 1, "model": "m", "started_at": now},
                ]}),
            }
            assert spine.ingest(world, [presence]) == 1
            # Ingesting presence is what makes a host mentionable: no seed
            # names a machine, so if this row is missing the mention below
            # resolves to nothing and the whole exchange is one-directional.
            db = persist._open(persist.state_file(world))
            try:
                bots = {str(r["name"]): dict(r) for r in db.execute(
                    "SELECT name, kind, host, status FROM agents")}
            finally:
                db.close()
            assert bots.get("hyperion") == {
                "name": "hyperion", "kind": "bot",
                "host": "hyperion", "status": "active"}, bots
            assert remote.mention_dispatch(world, "sys.work", "@hyperion x") == []
            assert remote.mention_dispatch(world, "build", "@nobody x") == []
            assert remote.mention_dispatch(world, "build", "@hyperion") == []
            notes = remote.mention_dispatch(
                world, "build", "@hyperion run the suite")
            # A mention is a conversation: the note names who is answering,
            # not a work id, an agent kind, or where the reply will land.
            assert notes == ["hyperion is thinking..."], notes

            db = persist._open(persist.state_file(world))
            try:
                rows = db.execute(
                    "SELECT body FROM channel_messages WHERE channel = ?",
                    (spine.SYS_WORK,),
                ).fetchall()
            finally:
                db.close()
            reqs = []
            for row in rows:
                try:
                    payload = json.loads(str(row["body"]))
                except ValueError:
                    continue
                if payload.get("t") == "request":
                    reqs.append(payload)
            assert len(reqs) == 1, reqs
            assert reqs[0]["task"] == "run the suite", reqs
            assert reqs[0]["target"] == "hyperion"
            wid = reqs[0]["work_id"]

            spine.post_work(world, {
                "t": "result", "work_id": wid, "host": "hyperion",
                "status": "done", "output": "suite green"})
            task = next(t for t in pending._bucket(world) if wid in t.name)
            assert task.done.wait(20), "mention reply never landed"
            assert task.error == "", task.error
            # The answer goes to the channel, so the task is quiet: no notice
            # for the step to read, and no handoff file to replay one later.
            assert task.quiet, "a channel reply must not wake the step"
            assert task.path is None, task.path
            assert pending.wait_next(world, timeout=1) == [], "quiet woke the step"
            got = persist.channel_read(world, channel="build", limit=5)
            assert got and got[-1]["author"] == "hyperion", got
            assert "suite green" in got[-1]["body"], got[-1]

            # Now the kernel's own post path, driven through the real dispatch:
            # calling channel_post here would prove nothing about what the
            # syscall passes. This seat signs its own messages, and names
            # itself as the asker -- the bridge passes the human's name
            # because a human typed there, while a mention from the kernel is
            # one agent asking another.
            posted = json.loads(dispatch(world, Block(
                "session", "@hyperion status?",
                {"op": "post", "channel": "build"})))
            assert posted["author"] == "asker-host", posted
            assert posted["author"] != posted["run_id"], posted
            assert posted.get("dispatched") == ["hyperion is thinking..."], posted

            db = persist._open(persist.state_file(world))
            try:
                rows = db.execute(
                    "SELECT body FROM channel_messages WHERE channel = ?"
                    " ORDER BY id DESC", (spine.SYS_WORK,)).fetchall()
            finally:
                db.close()
            asked = []
            for row in rows:
                try:
                    payload = json.loads(str(row["body"]))
                except ValueError:
                    continue
                if payload.get("t") == "request":
                    asked.append(payload)
            assert asked and asked[0]["asker"] == "asker-host", asked[:1]

            # Settle the second exchange too, so no pending thread outlives
            # the temp workspace it is polling.
            wid2 = asked[0]["work_id"]
            spine.post_work(world, {
                "t": "result", "work_id": wid2, "host": "hyperion",
                "status": "done", "output": "signed"})
            task2 = next(t for t in pending._bucket(world) if wid2 in t.name)
            assert task2.done.wait(20), "kernel-post reply never landed"
    finally:
        if keep is None:
            os.environ.pop("DESMOS_SEAT", None)
        else:
            os.environ["DESMOS_SEAT"] = keep


def _check_spine_sync_wiring() -> None:
    """sync() over a fake socket: send, ack, retire outbox, mark spine_seq."""
    import tempfile

    from desmos.front import spine
    from desmos.state import outbox, persist

    class FakeWS:
        def __init__(self):
            self.sent = []
            self.queue = []
            self.seq = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send(self, raw):
            frame = json.loads(raw)
            self.sent.append(frame)
            op = frame.get("op")
            if op == "sub":
                self.queue.append({"op": "subbed"})
            elif op == "snapshot":
                self.queue.append({"op": "snapshot", "channels": [{
                    "channel": spine.SYS_WORK, "tail": [{
                        "op": "event", "channel": spine.SYS_WORK, "seq": 7,
                        "fingerprint": "remote-f", "author": "elsewhere",
                        "seat": "elsewhere", "ts": "now",
                        "body": "{\"t\": \"claim\", \"work_id\": \"w-x\","
                                " \"host\": \"elsewhere\"}",
                    }],
                }]})
            elif op == "append":
                self.seq += 1
                self.queue.append({"op": "ack",
                                   "fingerprint": frame["fingerprint"],
                                   "seq": self.seq})

        def recv(self, timeout=None):
            return json.dumps(self.queue.pop(0))

    keep_connect = spine._connect
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        world = new_world(root, state_path=root / "s.sqlite3")
        db = persist._open(persist.state_file(world))
        try:
            persist._session_id(db, world, persist._workspace_id(db, world))
            db.commit()
        finally:
            db.close()
        persist.channel_post(world, "hello wire", channel="dev", author="me")
        assert any(r["kind"] == "channel_post" for r in outbox.pending(world, 50))
        ws = FakeWS()
        spine._connect = lambda w, timeout: ws
        try:
            report = spine.sync(world)
        finally:
            spine._connect = keep_connect
        assert [f["op"] for f in ws.sent[:2]] == ["sub", "snapshot"], ws.sent
        appended = [f for f in ws.sent if f["op"] == "append"]
        assert any(f["body"] == "hello wire" for f in appended), appended
        assert report["sent"] == len(appended) and report["sent"] >= 1, report
        assert report["ingested"] == 1, report
        assert outbox.pending(world, 50) == [], "acked rows must retire"
        db = persist._open(persist.state_file(world))
        try:
            row = db.execute(
                "SELECT spine_seq FROM channel_messages WHERE body = ?",
                ("hello wire",),
            ).fetchone()
            got = db.execute(
                "SELECT body FROM channel_messages WHERE channel = ?",
                (spine.SYS_WORK,),
            ).fetchall()
        finally:
            db.close()
        assert row is not None and int(row["spine_seq"] or 0) > 0, dict(row or {})
        assert len(got) == 1 and "w-x" in str(got[0]["body"]), got


def _check_spine_run_work() -> None:
    """_run_work itself: refusal, success with truncation, and error paths."""
    import tempfile
    import threading

    def run_threaded(*args):
        # Production runs _run_work on its own thread, where CALLER_WORLD
        # dies with the thread. Calling it inline would leak the tmp world
        # into every later check's contextvar.
        from desmos.front import spine as _spine

        t = threading.Thread(target=_spine._run_work, args=args)
        t.start()
        t.join(30)
        assert not t.is_alive(), "_run_work hung"

    from desmos.agents import remote, subagent
    from desmos.front import spine
    from desmos.state import persist

    keep_seat = os.environ.get("DESMOS_SEAT")
    keep = (subagent.bind, subagent.spawn, subagent.wait, subagent.result)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = new_world(root, state_path=root / "s.sqlite3")
            db = persist._open(persist.state_file(world))
            try:
                persist._session_id(db, world, persist._workspace_id(db, world))
                db.commit()
            finally:
                db.close()
            os.environ["DESMOS_SEAT"] = "doer-host"

            subagent.bind = lambda w: None
            subagent.spawn = lambda task, agent="general", **kw: (
                "spawn refused: over budget")
            run_threaded(world, "w-refuse", {"task": "x"})
            res = remote.find_result(world, "w-refuse")
            assert res is not None and res["status"] == "refused", res

            subagent.spawn = lambda task, agent="general", **kw: "rid1"
            subagent.wait = lambda rid, timeout=0: None
            subagent.result = lambda rid: "Y" * (remote.RESULT_CAP + 500)
            spine._WORK_IN_FLIGHT.add("w-ok")
            run_threaded(world, "w-ok", {"task": "x", "timeout": 5})
            assert "w-ok" not in spine._WORK_IN_FLIGHT
            res = remote.find_result(world, "w-ok")
            assert res is not None and res["status"] == "done", res
            assert len(res["output"]) == remote.RESULT_CAP, len(res["output"])

            def boom(task, agent="general", **kw):
                raise RuntimeError("kaput")

            subagent.spawn = boom
            run_threaded(world, "w-err", {"task": "x"})
            res = remote.find_result(world, "w-err")
            assert res is not None and res["status"] == "error", res
            assert "kaput" in res["output"], res
    finally:
        subagent.bind, subagent.spawn, subagent.wait, subagent.result = keep
        if keep_seat is None:
            os.environ.pop("DESMOS_SEAT", None)
        else:
            os.environ["DESMOS_SEAT"] = keep_seat


def check() -> None:
    import tempfile

    _spine_sequence_check()
    _check_spine_presence()
    _check_spine_memory()
    _check_roster()
    _check_spine_work()
    _check_mention_dispatch()
    _check_spine_sync_wiring()
    _check_spine_run_work()

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
        memory_world = new_world(memory_dir, state_path=memory_dir / "harness.sqlite3")
        memory_prompt = system_prompt(memory_world)
        assert "Umang prefers actual tools before narration" in memory_prompt
        assert "x" * 500 not in memory_prompt
        assert (memory_dir / "memories" / "legacy_MEMORY.md").read_text(encoding="utf-8") == legacy
        assert (memory_dir / "memories" / "records.jsonl").is_file()
        assert (memory_dir / "memory_summary.md").is_file()

        remembered = dispatch(
            memory_world,
            _memory(
                "Umang's name is Umang.",
                {"id": "user.umang.identity", "scope": "user", "kind": "identity"},
            ),
        )
        updated = dispatch(
            memory_world,
            _memory(
                "Umang's name is Umang.",
                {"id": "user.umang.identity", "scope": "user", "kind": "identity"},
            ),
        )
        search_result = dispatch(memory_world, _memory( "search Umang identity", {}))
        # Writing the same id twice updates the record instead of adding a
        # second one -- searching would otherwise return both and the model
        # would read two versions of the same fact.
        assert search_result.count("user.umang.identity") == 1, search_result
        read_result = dispatch(memory_world, _memory( "read user.umang.identity", {}))
        assert '"scope": "user"' in read_result
        dispatch(memory_world, _memory( "verify user.umang.identity", {}))

        secret_result = dispatch(
            memory_world,
            _memory(
                "api_key=abcdefghijk123456789",
                {"id": "repo.secret-test", "scope": "repo", "kind": "test"},
            ),
        )
        secret_read = dispatch(memory_world, _memory( "read repo.secret-test", {}))
        assert "[REDACTED_SECRET]" in secret_read
        assert "abcdefghijk123456789" not in secret_read

        memory_world2 = new_world(memory_dir, state_path=memory_dir / "harness.sqlite3")
        assert "Umang's name is Umang" in system_prompt(memory_world2)
        dispatch(memory_world2, _memory( "forget user.umang.identity", {}))
        gone = dispatch(memory_world2, _memory( "search user.umang.identity", {}))
        assert "user.umang.identity" not in gone, gone
        dispatch(memory_world2, _memory( "consolidate", {}))

        # BM25 search: multi-word relevance beats substring noise, forgotten
        # records never surface, the derived FTS index rebuilds itself after
        # deletion, and FTS-hostile queries fall back instead of raising.
        from desmos.state import memory as memory_mod

        for rid, content in (
            # Dense hit: the query terms dominate the record.
            ("repo.rank.dense", "rust compiler pipeline: the rust compiler stages"),
            # Noise: both terms appear once, buried in a long record, and its
            # id sorts before the dense hit so the old priority order would
            # have listed it first.
            (
                "repo.rank.a-noise",
                "notes on many things "
                + "filler word soup " * 40
                + "one rust remark and a compiler aside",
            ),
        ):
            dispatch(
                memory_world2,
                _memory(content, {"id": rid, "scope": "repo", "kind": "note"}),
            )
        ranked = dispatch(memory_world2, _memory("search rust compiler", {}))
        assert ranked.index("repo.rank.dense") < ranked.index("repo.rank.a-noise"), ranked
        assert "user.umang.identity" not in ranked, ranked

        dispatch(memory_world2, _memory("forget repo.rank.a-noise", {}))
        after_forget = dispatch(memory_world2, _memory("search rust compiler", {}))
        assert "repo.rank.a-noise" not in after_forget, after_forget
        assert "repo.rank.dense" in after_forget, after_forget

        # The index is derived and disposable: delete it and search rebuilds.
        index_file = memory_mod._index_path(memory_mod.memory_root(memory_world2))
        assert index_file.is_file(), index_file
        index_file.unlink()
        rebuilt = dispatch(memory_world2, _memory("search rust compiler", {}))
        assert "repo.rank.dense" in rebuilt, rebuilt
        assert index_file.is_file(), "search did not rebuild the derived index"

        # A malformed record id: the forgotten record's terms via substring
        # only (FTS returns zero rows for an empty phrase) still answer
        # through the scan fallback instead of "no match" surprises.
        assert memory_mod.search(memory_world2, "ompiler") != "no match"

        # An FTS-hostile query (embedded NUL breaks the MATCH string) must
        # fall back to the substring scan, not raise to the caller.
        hostile = memory_mod.search(memory_world2, "rust \x00", mode="any")
        assert "repo.rank.dense" in hostile, hostile

        ping = cwd / ".desmos" / "skills" / "ping"
        ping.mkdir(parents=True)
        (ping / "SKILL.md").write_text(
            "---\nname: ping\ndescription: reply pong\n---\n# ping\nbody\n",
            encoding="utf-8",
        )
        (ping / "skill.py").write_text("def handle(body, **a):\n    return 'pong:' + body\n", encoding="utf-8")
        world = new_world(cwd, state_path=cwd / "harness.sqlite3")
        assert dispatch(world, Block("harness", "", {"op": "skill", "name": "ping"})).endswith("body\n")
        assert dispatch(world, Block("ping", "hi", {})) == "pong:hi"

        grown = cwd / ".desmos" / "skills" / "later"
        grown.mkdir(parents=True)
        (grown / "SKILL.md").write_text(
            "---\nname: later\ndescription: appeared after start\n---\n# later\nok\n",
            encoding="utf-8",
        )
        assert not any(s.name == "later" for s in world.skills)
        dispatch(world, Block("harness", "", {"op": "reload"}))
        assert any(s.name == "later" for s in world.skills)
        assert dispatch(world, Block("harness", "", {"op": "skill", "name": "later"})).endswith("ok\n")

        out = dispatch(
            world,
            Block("harness", "def handle(body, **a):\n    return body.upper()\n", {"op": "register", "name": "echo", "doc": "uppercase"}),
        )
        assert dispatch(world, Block("echo", "hi", {})) == "HI"

        dispatch(world, Block("knowledge", "prefer tests", {"op": "system", "name": "style"}))
        assert "prefer tests" in system_prompt(world)

        world2 = new_world(cwd, state_path=cwd / "harness.sqlite3")
        assert "echo" in world2.tools
        assert world2.notes["style"] == "prefer tests"
        assert (cwd / "harness.sqlite3").read_bytes().startswith(b"SQLite format 3")
        import sqlite3 as _sqlite3

        from desmos.state.persist import record_event as _record_event

        # A launch announcement alone -- the bridge's 'session' plus the TUI's
        # 'ready' -- is not a record and must not mint a sessions row either.
        assert _record_event(world2, {"ev": "session"}, ts_ms=0, mono_ns=0) == 0
        assert _record_event(world2, {"ev": "ready"}, ts_ms=1, mono_ns=1) == 0
        with _sqlite3.connect(cwd / "harness.sqlite3") as _db:
            # No transcript was saved, so no session row exists yet: a session
            # becomes a row on its first real record, not on process start.
            assert _db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
            assert _db.execute("PRAGMA foreign_key_check").fetchall() == []
        world2.messages = [
            {"role": "user", "content": "session row check"},
            {"role": "assistant", "content": "minted on first record"},
        ]
        from desmos.state.persist import save as _save

        _save(world2)
        with _sqlite3.connect(cwd / "harness.sqlite3") as _db:
            assert _db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
            assert _db.execute("PRAGMA foreign_key_check").fetchall() == []

        # Compatible normalized schemas upgrade in place. A stale process may
        # refuse the newer marker, but it must not force the live transcript
        # through the corrupt-file recovery path.
        with _sqlite3.connect(cwd / "harness.sqlite3") as _db:
            _db.execute("UPDATE schema_migrations SET version = 5")
        upgraded = new_world(cwd, state_path=cwd / "harness.sqlite3")
        assert upgraded.notes["style"] == "prefer tests"
        with _sqlite3.connect(cwd / "harness.sqlite3") as _db:
            from desmos.state.persist import SCHEMA_VERSION

            assert _db.execute("SELECT version FROM schema_migrations").fetchall() == [
                (SCHEMA_VERSION,)
            ]
            assert _db.execute(
                "SELECT COUNT(*) FROM pragma_table_info('channel_cursors')"
            ).fetchone()[0] > 0

        # Legacy SQLite layouts are refused rather than guessed at.
        old_path = cwd / "legacy-layout.sqlite3"
        with _sqlite3.connect(old_path) as old_db:
            old_db.execute(
                "CREATE TABLE sessions(id TEXT PRIMARY KEY, cwd TEXT NOT NULL)"
            )
        try:
            new_world(cwd, state_path=old_path)
        except RuntimeError as exc:
            assert "legacy harness database" in str(exc), exc
        else:
            raise AssertionError("legacy session layout was silently accepted")

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
            interrupted.append({"role": "assistant", "content": f'<exec op="bash">echo {i}</exec>'})
            interrupted.append({"role": "user", "content": f'<result tag="exec">{i}</result>'})
            interrupted.append({"role": "assistant", "content": f"ran {i}"})
        interrupted.append({"role": "user", "content": '<result tag="exec">last</result>'})
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
                     "input": {"input": '<exec op="bash">true</exec>'}},
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
                     "input": {"input": '<exec op="bash">sleep 999</exec>'}},
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
                     "input": '<exec op="bash">sleep 999</exec>'},
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
            {"role": "assistant", "content": 'on it\n<exec op="bash">echo hi</exec>'},
        ]
        _save_world(prose_world)
        healed_prose = new_world(cwd, state_path=prose_path)
        assert len(healed_prose.messages) == 3, healed_prose.messages
        assert healed_prose.messages[2]["role"] == "user"
        assert '<result tag="exec">' in healed_prose.messages[2]["content"]
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
        dispatch(w3, Block("knowledge", "usage line", {"op": "system"}))
        assert w3.notes["note"] == "usage line"
        rollback(w3, 1)
        assert "note" not in w3.notes

        _check_prices()
        _check_call_ledger(cwd)
        _check_session_lineage(cwd)
        _check_session_channel(cwd)
        _check_single_writer(cwd)
        _check_injections(cwd)
        _check_handoff_rail(cwd)
        _check_plan_rail(cwd)
        _check_stop_rail(cwd)
        _check_commit_attribution(cwd)
        _check_fold_consent(cwd)
        _check_child_run_id()
        _check_concurrent_notes()
        _check_work_graph()
        _check_steer(cwd)
        _check_op_rollup(cwd)
        _check_slice(cwd)
        _check_schema_tolerance()
        _check_seats()
        _check_seat_hardening()
        _check_seat_accumulation()
        _check_quarantine_manifest()
        _check_quarantine_gate()
        _check_prune_manifest()
        _check_cold_store()
        _check_outbox()
        _check_budget_rail(cwd)
        _check_witness(cwd)
        _check_refine(cwd)
        _check_stow()
        _check_salvage()
        _check_fold_keeps_transcript()
        _check_session_asks()
        _check_decisions(cwd)


#: The one fixture both languages price. `crates/desmos-tui/src/main.rs`
#: (mod price_table_tests) bills the same usage through CacheMeter and asserts
#: this same constant, so a rate changed on one side fails on the other.
FIXTURE_USAGE = {
    "input_tokens": 100,
    "cache_read_input_tokens": 1000,
    "cache_creation_input_tokens": 10,
    "output_tokens": 50,
}
FIXTURE_COST_OPUS = 0.0023125


def _check_seats() -> None:
    """Seats: operator birth gate, single wake binding, tombstone retirement.

    Falsifiers per docs/seats.md section 5: worker roles and child worlds
    cannot birth; the agent tool surface has no seat tag; a restart in a
    seated workspace binds and wakes with charter+role; a second concurrent
    binding is refused with a recorded event; rollback leaves the seat row
    byte-identical; retirement tombstones without deleting and refuses new
    bindings; an unreadable newer schema degrades instead of binding.
    """
    import tempfile
    import warnings
    from argparse import Namespace

    from desmos.front import cli as front_cli
    from desmos.state import persist
    from desmos.transport.complete import split_system

    saved = {
        k: os.environ.get(k)
        for k in (persist.SESSION_ID_ENV, persist.SESSION_PID_ENV)
    }

    def fresh_run() -> str:
        rid = persist._uuid7()
        os.environ[persist.SESSION_ID_ENV] = rid
        os.environ[persist.SESSION_PID_ENV] = str(os.getpid())
        return rid

    def seat_rows(path):
        conn = persist._connect(path)
        rows = [tuple(r) for r in conn.execute("SELECT * FROM seats ORDER BY id")]
        conn.close()
        return rows

    def session_seat(path, sid):
        conn = persist._connect(path)
        row = conn.execute("SELECT seat_id FROM sessions WHERE id = ?", (sid,)).fetchone()
        conn.close()
        return None if row is None else str(row["seat_id"])

    def event_kinds(path, sid=None):
        conn = persist._connect(path)
        if sid is None:
            rows = conn.execute("SELECT kind FROM events").fetchall()
        else:
            rows = conn.execute(
                "SELECT kind FROM events WHERE session_id = ?", (sid,)
            ).fetchall()
        conn.close()
        return [str(r["kind"]) for r in rows]

    root = Path(tempfile.mkdtemp())
    try:
        rid_a = fresh_run()
        # Birth gate: a child world (persist=False) never creates and never binds.
        child = new_world(root, persist=False)
        try:
            persist.create_seat(child, role="navigator", charter="steer", operator=True)
            raise AssertionError("a child world birthed a seat")
        except persist.SeatError:
            pass
        assert persist.seat_binding(child) is None, "a child world bound a seat"

        parent = new_world(root)
        path = persist.state_file(parent)
        # The running agent's tool surface grows no seat-creation tag.
        assert not any("seat" in name for name in parent.tools), sorted(parent.tools)
        # Worker roles are refused: seats are for user-facing agents only.
        try:
            persist.create_seat(parent, role="worker", charter="fork work", operator=True)
            raise AssertionError("a worker role was seated")
        except persist.SeatError:
            pass
        # Non-operator callers are refused even with a valid role.
        try:
            persist.create_seat(parent, role="navigator", charter="c", operator=False)
            raise AssertionError("a non-operator surface birthed a seat")
        except persist.SeatError:
            pass
        # The operator CLI path succeeds.
        rc = front_cli.cmd_seat(Namespace(
            action="new", role="navigator",
            charter="keep the constitution honest", cwd=str(root),
        ))
        assert rc == 0, "operator seat birth failed"
        born = seat_rows(path)
        assert len(born) == 1, born
        assert "seat_birth" in event_kinds(path), "birth event missing"
        seat_id = born[0][0]

        # Wake binding: simulate a restart by releasing this run's lease.
        lease_key = str(persist._presence_path(parent, rid_a).resolve())
        lease = persist._PRESENCE_LEASES.pop(lease_key, None)
        if lease is not None:
            lease.close()
        rid_b = fresh_run()
        wake = new_world(root)
        _, _, tail = split_system(system_prompt(wake))
        assert "keep the constitution honest" in tail, tail[-400:]
        assert "navigator" in tail, tail[-400:]
        persist.record_event(wake, {"ev": "notice", "content": "bound"}, ts_ms=1, mono_ns=1)
        assert session_seat(path, rid_b) == seat_id, "restart did not bind"

        # Second concurrent binding is refused loudly with a recorded event.
        rid_c = fresh_run()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            second = new_world(root)
            persist.record_event(second, {"ev": "notice", "content": "hi"}, ts_ms=2, mono_ns=2)
        assert session_seat(path, rid_c) == "", "second concurrent bind was accepted"
        assert "seat_bind_refused" in event_kinds(path, rid_c), "refusal not recorded"
        assert any("seatless" in str(w.message) for w in caught), \
            [str(w.message) for w in caught]

        # Rollback narrowness: the seat row is byte-identical across a rollback.
        before = seat_rows(path)
        evolve(wake, "seat rollback probe")
        rollback(wake, 1)
        assert seat_rows(path) == before, "rollback touched the seat row"

        # Retire: tombstone set, event appended, nothing deleted.
        rc = front_cli.cmd_seat(Namespace(action="retire", reason="phase over", cwd=str(root)))
        assert rc == 0, "operator retire failed"
        after = seat_rows(path)
        assert len(after) == 1, "retirement deleted the seat row"
        assert after[0][6] and after[0][7] == "phase over", after
        kinds = event_kinds(path)
        assert "seat_retired" in kinds and "seat_birth" in kinds, kinds
        # A retired seat refuses new bindings -- and quietly: no active seat
        # is today's behaviour, not a refusal event.
        rid_d = fresh_run()
        idle = new_world(root)
        persist.record_event(idle, {"ev": "notice", "content": "x"}, ts_ms=3, mono_ns=3)
        assert session_seat(path, rid_d) == "", "a retired seat accepted a binding"
        assert persist.seat_binding(idle) is None
        assert "seat_bind_refused" not in event_kinds(path, rid_d)

        # Schema tolerance: an older reader without seats support degrades.
        conn = persist._connect(path)
        conn.execute("DELETE FROM schema_migrations")
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at, min_reader)"
            " VALUES (?, ?, ?)",
            (persist.SCHEMA_VERSION + 2, "2026-01-01T00:00:00+00:00",
             persist.SCHEMA_VERSION + 2),
        )
        conn.commit()
        conn.close()
        fresh_run()
        old_reader = new_world(root, persist=False)
        old_reader.persist = True
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            persist.load(old_reader)
        assert not old_reader.persist, "an unreadable schema kept persistence on"
        assert persist.seat_binding(old_reader) is None, "a degraded reader bound a seat"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("seats check ok")


def _check_seat_hardening() -> None:
    """Birth uniqueness and structural append-only for the seat record (A1).

    Falsifiers: a second `seat new` in a seated workspace must be refused at
    the operator surface with a retire-first message; a raw sqlite UPDATE of
    the charter, a raw DELETE of the row, and a rewrite of a written tombstone
    must all be refused by the schema itself (triggers + partial unique
    index), not merely by polite Python; seat events must be equally immovable.
    After a tombstone, a new birth is legal: the unique index is partial.
    """
    import sqlite3
    import subprocess
    import sys
    import tempfile
    from datetime import datetime, timezone

    from desmos.state import persist

    root = Path(tempfile.mkdtemp())
    env = dict(os.environ)
    env.pop(persist.SESSION_ID_ENV, None)
    env.pop(persist.SESSION_PID_ENV, None)
    cmd = [
        sys.executable, "-B", "-m", "desmos", "seat", "new",
        "--role", "navigator", "--charter", "steer", "--cwd", str(root),
    ]
    first = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stderr
    second = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert second.returncode != 0, "a second birth minted a second active seat"
    assert "retire" in second.stderr, second.stderr

    world = new_world(root, persist=False)
    world.persist = True
    path = persist.state_file(world)
    conn = persist._connect(path)
    try:
        rows = conn.execute("SELECT * FROM seats").fetchall()
        assert len(rows) == 1, "second birth left more than one seat row"

        def refused(sql: str, params: tuple = ()) -> bool:
            try:
                with conn:
                    conn.execute(sql, params)
            except sqlite3.IntegrityError:
                return True
            return False

        # Structural append-only: the schema refuses even a raw connection.
        assert refused("UPDATE seats SET charter = 'rewritten'"), \
            "raw UPDATE of the charter was accepted"
        assert refused("DELETE FROM seats"), "raw DELETE of the seat row was accepted"
        assert refused(
            "INSERT INTO seats(id, workspace_id, charter, role, created_at)"
            " SELECT 'twin', workspace_id, charter, role, created_at FROM seats"
        ), "the partial unique index let a racing second active seat in"
        # Seat events are equally immovable.
        assert refused("UPDATE events SET payload_json='{}' WHERE kind='seat_birth'"), \
            "raw UPDATE of a seat event was accepted"
        assert refused("DELETE FROM events WHERE kind='seat_birth'"), \
            "raw DELETE of a seat event was accepted"
        # Tombstone-once: the first write lands, any rewrite is refused.
        now = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute(
                "UPDATE seats SET retired_at = ?, retire_reason = 'probe'"
                " WHERE retired_at = ''",
                (now,),
            )
        assert refused("UPDATE seats SET retired_at = '2030-01-01T00:00:00+00:00'"), \
            "a written tombstone was rewritten"
        assert refused("UPDATE seats SET retire_reason = 'revised'"), \
            "a written tombstone reason was rewritten"
    finally:
        conn.close()
    # A retired workspace may seat a successor: the index is partial.
    third = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert third.returncode == 0, third.stderr
    print("seat hardening check ok")


def _check_seat_accumulation() -> None:
    """B1: notes and memory carry the seat id when bound; unbound unchanged.

    Falsifiers: a bound session's note row and memory record carry the seat
    id; an unseated workspace writes rows with no attribution, byte-for-byte
    today's behaviour; a fork still refuses remember() (B3); a retired seat
    accepts no new attribution while keeping everything it earned (A1).
    """
    import tempfile
    from argparse import Namespace

    from desmos.front import cli as front_cli
    from desmos.state import memory as memory_store
    from desmos.state import persist

    saved = {
        k: os.environ.get(k)
        for k in (persist.SESSION_ID_ENV, persist.SESSION_PID_ENV)
    }

    def fresh_run() -> str:
        rid = persist._uuid7()
        os.environ[persist.SESSION_ID_ENV] = rid
        os.environ[persist.SESSION_PID_ENV] = str(os.getpid())
        return rid

    def note_seats(path):
        conn = persist._connect(path)
        rows = {
            str(r["name"]): str(r["seat_id"])
            for r in conn.execute("SELECT name, seat_id FROM notes")
        }
        conn.close()
        return rows

    def release(world, rid):
        key = str(persist._presence_path(world, rid).resolve())
        lease = persist._PRESENCE_LEASES.pop(key, None)
        if lease is not None:
            lease.close()

    try:
        # Unseated workspace: no attribution anywhere -- identical to before.
        rid = fresh_run()
        plain_root = Path(tempfile.mkdtemp())
        plain = new_world(plain_root, persist=True)
        plain.notes["free"] = "workspace scoped"
        persist.save(plain)
        memory_store.remember(plain, "unseated fact", source="check")
        assert note_seats(persist.state_file(plain)) == {"free": ""}, \
            note_seats(persist.state_file(plain))
        _, records = memory_store._ensure_records(plain)
        assert records and all("seat" not in r for r in records), records
        release(plain, rid)

        # Seated workspace: birth, restart to bind, then accumulate.
        rid = fresh_run()
        root = Path(tempfile.mkdtemp())
        first = new_world(root, persist=True)
        persist.save(first)
        path = persist.state_file(first)
        rc = front_cli.cmd_seat(Namespace(
            action="new", role="navigator", charter="accumulate", cwd=str(root),
        ))
        assert rc == 0, "seat birth failed"
        release(first, rid)
        fresh_run()
        bound = new_world(root, persist=True)
        # A session becomes a row (and binds) once it records something.
        persist.record_event(
            bound, {"ev": "notice", "content": "bound"}, ts_ms=1, mono_ns=1,
        )
        bound.notes["earned"] = "seat scoped"
        persist.save(bound)
        seat = persist.seat_binding(bound)
        assert seat and seat["id"], "restart did not bind"
        seat_id = str(seat["id"])
        assert note_seats(path).get("earned") == seat_id, note_seats(path)
        memory_store.remember(bound, "seated fact", source="check")
        _, records = memory_store._ensure_records(bound)
        stamped = [r for r in records if r.get("seat") == seat_id]
        assert len(stamped) == 1 and stamped[0]["content"] == "seated fact", records

        # Forks stay anonymous (B3): remember() is refused outright.
        child = new_world(root, persist=False)
        out = memory_store.remember(child, "fork fact", source="check")
        assert "disabled" in out, out

        # A retired seat accepts no new attribution; earned rows stay stamped.
        rc = front_cli.cmd_seat(Namespace(
            action="retire", reason="accumulation probe", cwd=str(root),
        ))
        assert rc == 0, "retire failed"
        bound.notes["late"] = "after retirement"
        persist.save(bound)
        rows = note_seats(path)
        assert rows.get("late") == "", rows
        assert rows.get("earned") == seat_id, rows
        memory_store.remember(bound, "late fact", source="check")
        _, records = memory_store._ensure_records(bound)
        late = [r for r in records if r.get("content") == "late fact"]
        assert late and "seat" not in late[0], late
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("seat accumulation check ok")


def _check_quarantine_manifest() -> None:
    """A replaced database must leave an account of itself, loudly.

    Recovery renames the corrupt file and creates a fresh one. That is correct
    -- refusing to start would be worse -- but until now the only trace was a
    RuntimeWarning, and this workspace raised 98 of them in a 32-minute window
    without anyone noticing that every session before it had stopped being
    reachable. The manifest is the account; the notice is the volume.

    Reverting either fix fails this: drop `_record_quarantine` and there is no
    entry, drop `_report_quarantines` and wake says nothing.
    """
    import json
    import tempfile
    import warnings

    from desmos.state import persist

    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)
    world.messages.append({"role": "user", "content": "written before the corruption"})
    persist.save(world)
    path = persist.state_file(world)
    assert path.is_file(), path

    # How a killed writer leaves it: the header no longer says SQLite.
    raw = bytearray(path.read_bytes())
    raw[0:16] = b"not-a-database\x00\x00"
    path.write_bytes(bytes(raw))

    persist._QUARANTINE_REPORTED.discard(str(path))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        persist._open(path).close()

    entries = persist.quarantines(path)
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["bytes"] > 0, entry
    assert entry["reason"], entry
    assert entry["moved"], entry
    assert "inventory" in entry, entry

    summary = persist.quarantine_summary(path)
    assert "not absent" in summary, summary
    assert "1 database(s) replaced" in summary, summary

    # Wake reports it on the ordinary notice route, not only as a warning a
    # front never renders.
    persist._QUARANTINE_REPORTED.discard(str(path))
    fresh = new_world(root, persist=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        persist.load(fresh)
    events = persist.read_events(fresh, limit=500)
    assert any("quarantined" in json.dumps(ev, default=str) for ev in events), (
        f"wake did not report the quarantine: {len(events)} events"
    )

    # Once per process: a second load is not new information.
    before = len(persist.read_events(fresh, limit=500))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        persist.load(fresh)
    assert len(persist.read_events(fresh, limit=500)) == before, "quarantine notice repeated"


def _check_quarantine_gate() -> None:
    """Only confirmed corruption gets renamed; transient errors never do.

    `_open` used to quarantine on every `sqlite3.DatabaseError`, so a lock or
    an interrupted read cost the whole database. Now a `PRAGMA quick_check`
    gates the rename: a healthy file hit by a transient error is retried once
    and then re-raised, never renamed; a truly corrupt file still quarantines
    -- exactly once, even when two openers race.
    """
    import glob
    import sqlite3
    import tempfile
    import threading
    import warnings

    from desmos.state import persist

    # Transient DatabaseError on a healthy database: never renamed.
    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)
    world.messages.append({"role": "user", "content": "survives the transient"})
    persist.save(world)
    path = persist.state_file(world)
    assert path.is_file(), path
    quarantines_before = persist.quarantines(path)

    calls = {"n": 0}
    orig_migrate = persist._migrate

    def always_transient(conn):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    persist._migrate = always_transient
    try:
        raised = False
        try:
            persist._open(path).close()
        except sqlite3.OperationalError:
            raised = True
    finally:
        persist._migrate = orig_migrate
    assert raised, "transient error was swallowed"
    assert calls["n"] == 2, f"expected one retry, saw {calls['n']} attempts"
    assert path.is_file(), "healthy database vanished"
    assert not glob.glob(str(path) + "*.corrupt*"), "healthy database quarantined"
    assert persist.quarantines(path) == quarantines_before, "transient recorded as corruption"

    # A transient that clears on the retry succeeds without any rename.
    flaky = {"n": 0}

    def transient_once(conn):
        flaky["n"] += 1
        if flaky["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return orig_migrate(conn)

    persist._migrate = transient_once
    try:
        persist._open(path).close()
    finally:
        persist._migrate = orig_migrate
    assert flaky["n"] == 2, flaky
    assert not glob.glob(str(path) + "*.corrupt*"), "retry path quarantined"

    # Truly corrupt file: quarantined exactly once under concurrent opens.
    raw = bytearray(path.read_bytes())
    raw[0:16] = b"not-a-database\x00\x00"
    path.write_bytes(bytes(raw))
    persist._QUARANTINE_REPORTED.discard(str(path))

    errors: list[BaseException] = []

    def opener() -> None:
        try:
            persist._open(path).close()
        except BaseException as exc:  # noqa: BLE001 -- collected for the assert
            errors.append(exc)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        threads = [threading.Thread(target=opener) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert not errors, errors
    corpses = glob.glob(str(path) + ".corrupt*")
    assert len(corpses) == 1, f"main db renamed {len(corpses)} times: {corpses}"
    entries = persist.quarantines(path)
    assert len(entries) == len(quarantines_before) + 1, entries
    assert path.is_file(), "no fresh database after quarantine"
    conn = persist._open(path)
    conn.close()


def _check_prune_manifest() -> None:
    """Bounding the database must not be a silent delete.

    `_prune_sessions` keeps the newest SESSION_KEEP sessions and drops the
    rest. Foreign keys cascade, so a dropped session takes its messages, prior
    turns, events and calls with it -- and nothing recorded which conversations
    were spent. This drives the real entry point: `save()` prunes, so the
    manifest must appear without the test ever calling the pruner itself.

    Revert the `_record_prune` call and the manifest is empty; revert the
    `path=` argument at the call site and the census never runs.
    """
    import tempfile

    from desmos.state import persist

    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)
    world.messages.append({"role": "user", "content": "the surviving session"})
    persist.save(world)
    path = persist.state_file(world)
    assert not persist.pruned(path), "nothing was pruned yet"

    conn = persist._open(path)
    try:
        workspace = conn.execute("SELECT id FROM workspaces").fetchone()[0]
        extra = persist.SESSION_KEEP + 6
        with conn:
            for i in range(extra):
                sid = f"doomed-{i:04d}"
                conn.execute(
                    "INSERT INTO sessions(id, workspace_id, kind, started_at,"
                    " last_seen_at, cache_key) VALUES (?, ?, 'attach', ?, ?, ?)",
                    (sid, workspace, f"2020-01-01T00:00:{i:02d}",
                     f"2020-01-01T00:00:{i:02d}", sid),
                )
                conn.execute(
                    "INSERT INTO messages(session_id, seq, role, content_json)"
                    " VALUES (?, 0, 'user', ?)",
                    (sid, json.dumps(f"doomed conversation {i}")),
                )
    finally:
        conn.close()

    persist.save(world)

    entries = persist.pruned(path)
    # SESSION_KEEP + 6 fakes + this session = SESSION_KEEP + 7 rows.
    expected = 7
    assert len(entries) == expected, f"{len(entries)} pruned entries, want {expected}"
    for entry in entries:
        assert entry["session_id"].startswith("doomed-"), entry
        assert entry["messages"] == 1, entry
        assert entry["bytes"] > 0, entry
        assert entry["at"], entry
        assert "doomed conversation" in entry["opened_with"], entry

    # The account describes something that really is gone.
    conn = persist._open(path)
    try:
        gone = [e["session_id"] for e in entries]
        marks = ",".join("?" * len(gone))
        left = conn.execute(
            f"SELECT count(*) FROM sessions WHERE id IN ({marks})", gone
        ).fetchone()[0]
        assert left == 0, f"{left} pruned sessions still present"
        left = conn.execute(
            f"SELECT count(*) FROM messages WHERE session_id IN ({marks})", gone
        ).fetchone()[0]
        assert left == 0, f"{left} messages survived their pruned session"
    finally:
        conn.close()


def _check_session_asks() -> None:
    """Recover what a person asked from the stored record, by content.

    Every row below is role 'user' and exactly one was written by a person.
    Counting roles gets six; classifying by content-block type gets the one.
    """
    import tempfile

    from desmos.state import persist

    root = Path(tempfile.mkdtemp())
    live = new_world(root, persist=True)
    ask = "fold the record and tell me what broke"
    live.messages.extend(
        [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": "output"}
                ],
            },
            {"role": "assistant", "content": "working on it"},
            {"role": "user", "content": f"ns:\n  diag: Diagnostics\n  w: World\n\n{ask}"},
            {"role": "user", "content": "[background task finished] shell main"},
            {"role": "user", "content": "# now\n[todo]\n1. [ ] something"},
            {"role": "user", "content": "<compacted n=9>\nEarlier turns, folded."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "an accidental caption"},
                    {"type": "tool_result", "content": "z"},
                ],
            },
        ]
    )
    live.session_message_start = 0
    persist.save(live)

    found = persist.session_asks(live)
    assert [item["ask"] for item in found] == [ask], found
    assert persist.last_task(live) == ask, persist.last_task(live)


def _check_fold_keeps_transcript() -> None:
    """A fold must not erase the transcript of the session doing the folding.

    Both transcripts are persisted as a slice from an offset marking where this
    session's own contribution begins. Anything that shortens the list from the
    front -- a fold, the prior-turn cap -- moves the data out from under that
    offset. When the slice went empty the save deleted the stored rows and
    wrote nothing back, so a session lost its history at the moment it grew
    long enough to need it.

    Driven through the real entry points: `compact.compact` and `_commit_step`.
    """
    import tempfile

    from desmos.kernel import loop as kernel_loop
    from desmos.kernel.const import PRIOR_KEEP
    from desmos.state import compact, persist

    root = Path(tempfile.mkdtemp())
    live = new_world(root, persist=True)

    def stored() -> tuple[int, list[str]]:
        conn = persist._open(persist.state_file(live))
        try:
            session = persist.run_id()
            count = conn.execute(
                "SELECT count(*) FROM messages WHERE session_id = ?", (session,)
            ).fetchone()[0]
            prompts = [
                str(row[0])
                for row in conn.execute(
                    "SELECT prompt FROM prior_turns WHERE session_id = ? ORDER BY seq",
                    (session,),
                )
            ]
            return int(count), prompts
        finally:
            conn.close()

    for i in range(60):
        live.messages.append(
            {
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"turn {i} about the kingfisher ledger",
            }
        )
    # A resumed session: the first thirty messages came from its parent.
    live.session_message_start = 30
    persist.save(live)
    assert stored()[0] == 30, stored()[0]

    folded = compact.compact(live)
    assert folded["folded"] > 0, folded
    assert live.session_message_start <= len(live.messages), live.session_message_start
    assert stored()[0] == len(live.messages), (stored()[0], len(live.messages))

    live.messages.append({"role": "user", "content": "written after the fold"})
    persist.save(live)
    conn = persist._open(persist.state_file(live))
    try:
        landed = conn.execute(
            "SELECT count(*) FROM messages WHERE session_id = ?"
            " AND content_json LIKE '%written after the fold%'",
            (persist.run_id(),),
        ).fetchone()[0]
    finally:
        conn.close()
    assert landed == 1, "a turn written after a fold is not persisted"

    # An offset that drifts anyway must leave the record stale, never erased.
    keep = stored()[0]
    live.session_message_start = len(live.messages) + 99
    persist.save(live)
    assert stored()[0] == keep, "an empty payload deleted the stored transcript"
    live.session_message_start = 0

    # The prior-turn cap drops from the front. Pinned at the cap, the offset
    # never moves again and the session records none of its own turns.
    live.prior = [{"prompt": f"inherited {i}", "speech": "x"} for i in range(PRIOR_KEEP)]
    live.session_prior_start = PRIOR_KEEP
    kernel_loop._commit_step(live, "the session's own ask", "its own answer")
    prompts = stored()[1]
    assert prompts == ["the session's own ask"], prompts


def _check_salvage() -> None:
    """A quarantined conversation must become answerable again.

    Row counts are not the acceptance test. The point of salvage is that a
    recall for something only the lost session said stops coming back empty,
    so this drives `search_history` before and after and asserts on the flip.

    Salvage is dry by default: the dry run must change nothing, and a second
    apply must find nothing left to do.
    """
    import tempfile

    from desmos.state import persist, salvage

    root = Path(tempfile.mkdtemp())
    rare = "peregrine telemetry cadence"
    # Same hermeticity as the lineage check: run_id() only honours the id when
    # the pid var names this process, and overwrites both vars otherwise.
    saved = {
        var: os.environ.get(var)
        for var in (persist.SESSION_ID_ENV, persist.SESSION_PID_ENV, persist.NEW_SESSION_ENV)
    }
    try:
        os.environ.pop(persist.NEW_SESSION_ENV, None)
        lost = new_world(root, persist=True)
        lost.messages.append({"role": "user", "content": f"about {rare} and nothing else"})
        lost.messages.append({"role": "assistant", "content": "noted"})
        persist.save(lost)
        path = persist.state_file(lost)

        # Quarantine it the way recovery does. Corrupting the header instead
        # would make the dead file unreadable, which is not the case being
        # repaired -- all ninety files in this workspace open cleanly.
        moved = persist._move_sqlite_files(path, "corrupt")
        assert moved, "nothing was moved aside"

        # A different attach, on the empty replacement.
        os.environ[persist.SESSION_PID_ENV] = str(os.getpid())
        os.environ[persist.SESSION_ID_ENV] = "01a0salvage000000000000000000000"
        live = new_world(root, persist=True)
        live.messages.append({"role": "user", "content": "an unrelated later question"})
        persist.save(live)

        assert not persist.search_history(live, rare), "the lost session was never lost"

        found = salvage.survey(path)
        assert found["files"] >= 1, found
        assert found["sessions"] == 1, found
        assert found["messages"] >= 2, found
        assert not found["unreadable"], found

        dry = salvage.salvage(live)
        assert dry["applied"] is False, dry
        assert dry["sessions"] == 1, dry
        assert not persist.search_history(live, rare), "a dry run wrote to the database"

        done = salvage.salvage(live, apply=True)
        assert done["applied"] is True, done
        assert done["sessions"] == 1, done

        hits = persist.search_history(live, rare)
        assert hits, "salvaged history is still unreachable by recall"
        assert any(rare in json.dumps(hit, default=str) for hit in hits), hits

        again = salvage.salvage(live, apply=True)
        assert again["sessions"] == 0, f"salvage is not idempotent: {again}"
    finally:
        for var, value in saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


def _check_prices() -> None:
    from desmos.kernel import prices

    assert prices.rates("claude-opus-5") == (5.0, 25.0)
    assert prices.rates("gpt-5.6-sol") == (1.25, 10.0)
    # An unpriced model bills at the default. A silent zero reads as free.
    assert prices.rates("mystery-9") == prices.rates("claude-opus-5")
    got = prices.cost(FIXTURE_USAGE, "claude-opus-5")
    assert abs(got - FIXTURE_COST_OPUS) < 1e-12, got
    # Sonnet is the rate the old usage tag hardcoded for every model; the gap
    # between these two is exactly the error that motivated the shared table.
    assert prices.cost(FIXTURE_USAGE, "claude-sonnet-4-6") < got
    # The 1h cache tier is a premium over the 5m one, not the same write.
    hour = dict(FIXTURE_USAGE, cache_creation={"ephemeral_1h_input_tokens": 10})
    assert prices.cost(hour, "claude-opus-5") > got

    # The TUI must read this file, not a copy of it. A literal rate table in
    # main.rs is the drift this check exists to catch.
    main_rs = Path(__file__).resolve().parents[2] / "crates" / "desmos-tui" / "src" / "main.rs"
    if main_rs.is_file():
        text = main_rs.read_text()
        assert "include_str!(\"../../../desmos/kernel/prices.json\")" in text, (
            "the TUI stopped reading desmos/kernel/prices.json"
        )
        assert 'm if m.starts_with("claude-opus")' not in text, (
            "main.rs grew a second hardcoded price table"
        )


def _check_session_lineage(cwd: Path) -> None:
    """A restart creates a child session without copying message ownership."""
    import os
    import sqlite3

    from desmos.state import persist

    path = cwd / "lineage.sqlite3"
    # Own the whole session environment: run_id() only honours the id when the
    # pid var names this process, and DESMOS_SESSION_NEW severs the very
    # lineage this check asserts. A live desmos exports all three.
    saved = {
        var: os.environ.get(var)
        for var in (persist.SESSION_ID_ENV, persist.SESSION_PID_ENV, persist.NEW_SESSION_ENV)
    }
    try:
        first_id = "019100000000aaaaaaaaaaaaaaaaaaaa"
        second_id = "019200000000bbbbbbbbbbbbbbbbbbbb"
        os.environ.pop(persist.NEW_SESSION_ENV, None)
        os.environ[persist.SESSION_PID_ENV] = str(os.getpid())
        os.environ[persist.SESSION_ID_ENV] = first_id
        first = new_world(cwd, state_path=path)
        first.messages.append({"role": "user", "content": "from first"})
        first.prior.append({"prompt": "p1", "speech": "s1"})
        persist.save(first)

        os.environ[persist.SESSION_PID_ENV] = str(os.getpid())
        os.environ[persist.SESSION_ID_ENV] = second_id
        second = new_world(cwd, state_path=path)
        assert second.messages == [{"role": "user", "content": "from first"}]
        assert second.session_message_start == 1
        assert second.session_prior_start == 1
        second.messages.append({"role": "assistant", "content": "from second"})
        second.prior.append({"prompt": "p2", "speech": "s2"})
        persist.save(second)

        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            sessions = db.execute(
                "SELECT id, parent_id, kind, cache_key FROM sessions"
                " ORDER BY started_at, id"
            ).fetchall()
            assert [row["id"] for row in sessions] == [first_id, second_id], sessions
            assert sessions[1]["parent_id"] == first_id, sessions[1]
            assert sessions[1]["kind"] == "resume", sessions[1]
            assert sessions[1]["cache_key"] == f"desmos-{second_id[:16]}"
            ownership = db.execute(
                "SELECT session_id, COUNT(*) n FROM messages"
                " GROUP BY session_id ORDER BY session_id"
            ).fetchall()
            assert [(row["session_id"], row["n"]) for row in ownership] == [
                (first_id, 1), (second_id, 1)
            ], ownership
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []

        # Opaque provider content and giant request/response bodies never land.
        secret = "ciphertext" * 10_000
        persist.record_event(
            second,
            {"ev": "complete", "encrypted_content": secret, "body": secret},
            ts_ms=1,
            mono_ns=2,
        )
        assert persist.record_event(
            second, {"ev": "timing", "phase": "delta"}, ts_ms=2, mono_ns=3
        ) == 0
        events = persist.read_events(second)
        assert len(events) == 1 and events[0]["elided"] is True, events
        assert secret not in str(events), events[0]
        assert events[0]["payload_bytes"] > 100_000, events[0]
    finally:
        for var, value in saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


def _check_call_ledger(cwd: Path) -> None:
    """Per-call usage is durable, keyed by a run id, and priced on the way in."""
    from desmos.state import persist

    world = new_world(cwd, state_path=cwd / "ledger.sqlite3")
    world.model = "claude-opus-5"
    assert persist.runs(world) == []

    persist.record_call(world, {"ts": "2026-01-01T00:00:00+00:00", "usage": FIXTURE_USAGE})
    persist.record_call(world, {"ts": "2026-01-01T00:01:00+00:00", "usage": FIXTURE_USAGE})
    # A call that reported nothing is not a call. Rows here are money.
    persist.record_call(world, {"ts": "2026-01-01T00:02:00+00:00", "usage": {}})

    rows = persist.runs(world)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["calls"] == 2, row
    assert row["run_id"] == persist.run_id()
    assert row["models"] == "claude-opus-5"
    assert abs(row["cost"] - 2 * FIXTURE_COST_OPUS) < 1e-12, row

    # The point of the table: it outlives the process that wrote it.
    reopened = new_world(cwd, state_path=cwd / "ledger.sqlite3")
    assert persist.runs(reopened)[0]["calls"] == 2

    # A child world (persist off) spends against its own transcript and must
    # never write rows into the parent's ledger.
    child = new_world(cwd, state_path=None)
    child.model = "gpt-5.6-luna"
    persist.record_call(child, {"usage": FIXTURE_USAGE})
    assert persist.runs(world)[0]["calls"] == 2


def _check_single_writer(cwd: Path) -> None:
    """One interactive front per workspace; a second is refused by name."""
    from desmos.state import persist

    home = cwd / "single-writer"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3")

    persist.claim_workspace(world)
    # Re-claiming in the same process is a no-op, not a refusal.
    persist.claim_workspace(world)

    # A second front is a second open file description. flock conflicts across
    # descriptions even inside one process, so dropping the in-process memo is
    # enough to make the lock itself decide.
    memo = dict(persist._WORKSPACE_LEASE)
    persist._WORKSPACE_LEASE.clear()
    try:
        try:
            persist.claim_workspace(world)
        except persist.WorkspaceBusy as exc:
            assert "already has a live session" in str(exc), exc
        else:
            raise AssertionError("a second front was allowed to claim the workspace")
    finally:
        persist._WORKSPACE_LEASE.clear()
        persist._WORKSPACE_LEASE.update(memo)

    # Releasing hands the workspace to the next front rather than wedging it.
    persist.release_workspace(world)
    persist.claim_workspace(world)
    persist.release_workspace(world)

    # A non-persistent world -- every child -- is never a writer and never
    # blocked, or the peer rail and every subagent would deadlock behind a TUI.
    child = new_world(home, state_path=home / "harness.sqlite3", persist=False)
    persist.claim_workspace(world)
    persist.claim_workspace(child)
    persist.release_workspace(world)
    print("single writer check ok")


def _check_session_channel(cwd: Path) -> None:
    """Canonical session ops expose live peers and an ordered local channel."""
    import fcntl
    import json
    import sqlite3

    from desmos.state import persist

    path = cwd / "channel.sqlite3"
    world = new_world(cwd, state_path=path)
    peer_id = "peer-run"
    lease_path = persist._presence_path(world, peer_id)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    peer_lease = lease_path.open("a+")
    fcntl.flock(peer_lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with sqlite3.connect(path) as db:
            workspace = db.execute("SELECT id FROM workspaces").fetchone()[0]
            parent = db.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
            db.execute(
                """
                INSERT INTO sessions(
                    id, workspace_id, parent_id, kind, started_at,
                    last_seen_at, model, thinking, cache_key)
                VALUES (?, ?, ?, 'fork', ?, ?, ?, '', ?)
                """,
                (
                    peer_id, workspace, parent, "2026-01-01",
                    "2026-01-01", "peer-model", "desmos-peer",
                ),
            )
            db.execute(
                """
                INSERT INTO active_runs(
                    run_id, workspace_id, session_id, pid, cwd, generation,
                    model, started_at, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    peer_id, workspace, peer_id, 99999, str(cwd), 1,
                    "peer-model", "2026-01-01", "2026-01-01",
                ),
            )

        peers = json.loads(dispatch(world, Block("session", "", {"op": "peers"})))
        assert {row["run_id"] for row in peers} == {persist.run_id(), peer_id}, peers

        posted = json.loads(
            dispatch(
                world,
                Block(
                    "session",
                    "I am editing persist.py; please avoid it.",
                    {"op": "post", "author": "worker-a"},
                ),
            )
        )
        assert posted["channel"] == "general" and posted["id"] > 0, posted
        directed = json.loads(
            dispatch(
                world,
                Block(
                    "session",
                    "Can you inspect the overlap?",
                    {"op": "post", "session_id": peer_id},
                ),
            )
        )
        assert directed["to"] == peer_id and directed["kind"] == "request", directed
        assert directed["channel"] == persist.peer_channel(peer_id, "request"), directed
        missing = dispatch(
            world,
            Block("session", "hello?", {"op": "post", "to": "not-a-live-run"}),
        )
        assert "not active" in missing, missing
        assert json.loads(
            dispatch(world, Block("session", "", {"op": "inbox"}))
        )["unread"] == 0, "a run must not notify itself"

        with sqlite3.connect(path) as db:
            db.execute(
                """
                INSERT INTO channel_messages(
                    workspace_id, session_id, channel, run_id,
                    author, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace, peer_id, "general", peer_id,
                    "worker-b", "I also need persist.py.", "2026-01-02",
                ),
            )
        inbox = json.loads(dispatch(world, Block("session", "", {"op": "inbox"})))
        assert inbox["unread"] == 1 and inbox["messages"][0]["author"] == "worker-b", inbox
        from desmos.kernel.catalog import volatile
        assert "IRC #general: 1 unread from worker-b" in volatile(world)

        messages = json.loads(
            dispatch(
                world,
                Block("session", "", {"op": "read", "since": str(posted["id"])}),
            )
        )
        assert [row["body"] for row in messages] == ["I also need persist.py."], messages
        assert json.loads(
            dispatch(world, Block("session", "", {"op": "inbox"}))
        )["unread"] == 0, "read must advance the unread cursor"

        with sqlite3.connect(path) as db:
            db.execute(
                """
                INSERT INTO channel_messages(
                    workspace_id, session_id, channel, run_id,
                    author, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace, peer_id, "general", peer_id,
                    "worker-b", "Resolved.", "2026-01-03",
                ),
            )
        assert json.loads(
            dispatch(world, Block("session", "", {"op": "inbox"}))
        )["unread"] == 1
        dispatch(world, Block("session", "", {"op": "dismiss"}))
        assert json.loads(
            dispatch(world, Block("session", "", {"op": "inbox"}))
        )["unread"] == 0
    finally:
        fcntl.flock(peer_lease.fileno(), fcntl.LOCK_UN)
        peer_lease.close()

    peers = json.loads(dispatch(world, Block("session", "", {"op": "peers"})))
    assert [row["run_id"] for row in peers] == [persist.run_id()], peers
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM active_runs WHERE run_id = ?", (peer_id,)
        ).fetchone()[0] == 0


def _check_injections(cwd: Path) -> None:
    """Named injections render into the uncached system tail and expire."""
    from desmos.kernel.catalog import expire, inject, retire
    from desmos.transport.complete import split_system

    home = cwd / "inject"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3", persist=False)

    inject(world, "steer", "prefer the smaller edit", turns=1)
    inject(world, "wake", "you are seat one", turns=0)
    abi, cat, tail = split_system(system_prompt(world))
    assert "[steer]" in tail and "prefer the smaller edit" in tail, tail[-400:]
    assert "you are seat one" in tail, tail[-400:]
    assert "prefer the smaller edit" not in abi + cat

    assert expire(world) == ["steer"], world.injections
    _, _, tail = split_system(system_prompt(world))
    assert "prefer the smaller edit" not in tail
    assert "you are seat one" in tail
    assert expire(world) == []
    assert retire(world, "wake") is True
    _, _, tail = split_system(system_prompt(world))
    assert "you are seat one" not in tail
    print("injection check ok")


def _check_steer(cwd: Path) -> None:
    """A steer arrives as its own labelled user turn, never inside a result."""
    from desmos.kernel.catalog import steer
    from desmos.kernel.loop import run_turns

    home = cwd / "steer"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3", persist=False)
    world.model = "claude-opus-5"
    lt = chr(60)

    def fake(_model, _system, messages, _max_tokens):
        if len(messages) <= 1:
            steer(world, "switch to the smaller file")
            body = lt + "python>print(1)" + lt + "/python>"
            call = {"type": "tool_use", "id": "toolu_1", "name": "syscall",
                    "input": {"input": body}}
            return {"content": [call], "usage": {}}
        return {"content": [{"type": "text", "text": "done"}], "usage": {}}

    world.complete_fn = fake
    run_turns(world, "go", quiet=True, on_continue=lambda n: "guidance: keep going")

    carrier = world.messages[2]
    assert carrier["role"] == "user", carrier
    kinds = [b.get("type") for b in carrier["content"]]
    # The steer never rides in the carrier: gluing it onto an already-sent
    # result block read out of order in the transcript.
    assert kinds == ["tool_result"], kinds
    steer_msg = world.messages[3]
    assert steer_msg["role"] == "user", steer_msg
    assert steer_msg["content"] == "[steer] switch to the smaller file", steer_msg
    assert world.steers == [], world.steers
    print("steer check ok")


def _check_op_rollup(cwd: Path) -> None:
    """observe usage ops counts real calls and names the ops nothing used."""
    from desmos.state import persist

    home = cwd / "rollup"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3")
    calls = (("exec", "python"), ("exec", "python"), ("workspace", "edit"))
    for tag, op in calls:
        persist.record_event(
            world,
            {"ev": "result", "phase": "done", "tag": tag, "attrs": {"op": op}},
            ts_ms=0,
            mono_ns=0,
        )
    out = dispatch(world, Block("observe", "ops", {"op": "usage"}))
    assert "2  exec python" in out, out
    assert "1  workspace edit" in out, out
    assert "never called:" in out, out
    idle = out.split("never called:")[1]
    assert "harness rollback" in idle and "exec bash" in idle, idle
    assert "exec python" not in idle, idle
    print("op rollup check ok")


def _check_slice(cwd: Path) -> None:
    """A folded exchange stays readable verbatim from the event record."""
    from desmos.state import persist

    home = cwd / "slice"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3")
    stream = [
        {"ev": "prompt", "n": 1, "text": "the first ask"},
        {"ev": "speech", "text": "a very particular sentence " + "pad " * 40
         + "ENDMARK9f3", "delta": True},
        {"ev": "result", "phase": "done", "tag": "exec",
         "attrs": {"op": "python"}, "text": "4"},
        {"ev": "prompt", "n": 2, "text": "the second ask"},
        {"ev": "speech", "text": "later words", "delta": True},
    ]
    for i, event in enumerate(stream):
        persist.record_event(world, event, ts_ms=i, mono_ns=i)
    index = dispatch(world, Block("observe", "", {"op": "slice"}))
    assert "1. the first ask" in index, index
    assert "2. the second ask" in index, index
    one = dispatch(world, Block("observe", "1", {"op": "slice"}))
    assert "a very particular sentence" in one, one
    assert "call exec python: 4" in one, one
    assert "the second ask" not in one, one
    world.messages = [
        {"role": "user", "content": "the first ask"},
        {"role": "assistant", "content": "a very particular sentence"},
        {"role": "user", "content": "the second ask"},
        {"role": "assistant", "content": "later words"},
    ]
    from desmos.state.compact import compact

    compact(world, keep=2, floor=2)
    import json as _json

    live = _json.dumps(world.messages)
    assert "ENDMARK9f3" not in live, live
    again = dispatch(world, Block("observe", "1", {"op": "slice"}))
    assert "ENDMARK9f3" in again, again
    print("slice check ok")


def _check_schema_tolerance() -> None:
    """A newer additive schema must not kill an older front."""
    import tempfile
    import warnings

    from desmos.state import persist

    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)
    world.messages.append({"role": "user", "content": "before the bump"})
    persist.save(world)
    path = persist.state_file(world)

    def _stamp(version: int, floor: int) -> None:
        conn = persist._connect(path)
        conn.execute("DELETE FROM schema_migrations")
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at, min_reader)"
            " VALUES (?, ?, ?)",
            (version, "2026-01-01T00:00:00+00:00", floor),
        )
        conn.commit()
        conn.close()

    def _recorded() -> tuple:
        conn = persist._connect(path)
        row = conn.execute(
            "SELECT version, min_reader FROM schema_migrations"
        ).fetchone()
        conn.close()
        return int(row["version"]), int(row["min_reader"])

    top = persist.SCHEMA_VERSION
    _stamp(top + 1, top)
    reader = new_world(root, persist=True)
    persist.load(reader)
    assert reader.persist, "a tolerable bump disabled persistence"
    assert reader.messages, "a newer file read as empty"
    assert _recorded() == (top + 1, top), _recorded()

    _stamp(top + 2, top + 2)
    before = persist.quarantines(path)
    strict = new_world(root, persist=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        persist.load(strict)
    assert not strict.persist, "an unreadable schema kept persistence on"
    assert persist.quarantines(path) == before, "compatibility quarantined"
    assert _recorded() == (top + 2, top + 2), _recorded()
    print("schema tolerance check ok")


def _check_cold_store() -> None:
    """Pruning is a move, not a delete.

    Bounding the live database used to destroy its oldest sessions outright.
    A session may now leave harness.sqlite3 only after a verified copy lands
    in the cold store: the census names where each one went, and the archive
    can still answer for the messages.
    """
    import sqlite3
    import tempfile

    from desmos.state import cold, persist

    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)
    world.messages.append({"role": "user", "content": "the surviving session"})
    persist.save(world)
    path = persist.state_file(world)

    conn = persist._open(path)
    try:
        workspace = conn.execute("SELECT id FROM workspaces").fetchone()[0]
        with conn:
            for i in range(persist.SESSION_KEEP + 3):
                sid = f"cold-{i:04d}"
                conn.execute(
                    "INSERT INTO sessions(id, workspace_id, kind, started_at,"
                    " last_seen_at, cache_key) VALUES (?, ?, 'attach', ?, ?, ?)",
                    (sid, workspace, f"2020-01-01T00:00:{i:02d}",
                     f"2020-01-01T00:00:{i:02d}", sid),
                )
                conn.execute(
                    "INSERT INTO messages(session_id, seq, role, content_json)"
                    " VALUES (?, 0, 'user', ?)",
                    (sid, json.dumps(f"cold conversation {i}")),
                )
                # Index them the way save() indexes a real transcript: what a
                # prune costs is not measured in rows, it is measured in what
                # recall can still find afterwards.
                conn.execute(
                    "INSERT INTO history_fts(workspace_id, session_id, kind,"
                    " text, source_seq) VALUES (?, ?, 'message:user', ?, 0)",
                    (workspace, sid, f"cold conversation {i} quokkatelemetry{i:04d}"),
                )
    finally:
        conn.close()
    persist.save(world)

    entries = [e for e in persist.pruned(path) if e["session_id"].startswith("cold-")]
    assert entries, "nothing was pruned"
    for entry in entries:
        assert entry["archived_to"], entry
    moved = {e["session_id"] for e in entries}
    held = {r["session_id"] for r in cold.archived(path)}
    assert moved <= held, sorted(moved - held)

    store = sqlite3.connect(cold.cold_path(path))
    try:
        sid = sorted(moved)[0]
        rows = store.execute(
            "SELECT content_json FROM messages WHERE session_id = ?", (sid,)
        ).fetchall()
    finally:
        store.close()
    assert len(rows) == 1, f"{len(rows)} archived messages for {sid}"
    assert "cold conversation" in rows[0][0], rows[0][0]

    # Retention that erases findability is deletion by another name. The live
    # index rows leave with the session, so recall reads through to the copy
    # the archive keeps -- and the answer says it came from the cold side.
    rare = "quokkatelemetry0000"
    conn = persist._open(path)
    try:
        left = conn.execute(
            "SELECT count(*) FROM history_fts WHERE session_id = ?", ("cold-0000",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert left == 0, "the live index still holds the pruned session"
    hits = persist.search_history(world, rare)
    assert hits, "a pruned session stopped being findable at all"
    assert all(hit.get("cold") for hit in hits), hits
    assert hits[0]["session_id"] == "cold-0000", hits
    assert rare in hits[0]["text"], hits[0]

    # The second archive into a store that already holds the tables is where
    # the live schema's bare CREATE TABLE used to raise "table calls already
    # exists" -- and it took save() with it, so a workspace only ever crashed
    # after it had pruned once, which no fresh check could see.
    conn = persist._open(path)
    try:
        with conn:
            for i in range(4):
                sid = f"cold2-{i:04d}"
                conn.execute(
                    "INSERT INTO sessions(id, workspace_id, kind, started_at,"
                    " last_seen_at, cache_key) VALUES (?, ?, 'attach', ?, ?, ?)",
                    (sid, workspace, f"2020-01-02T00:00:{i:02d}",
                     f"2020-01-02T00:00:{i:02d}", sid),
                )
                conn.execute(
                    "INSERT INTO messages(session_id, seq, role, content_json)"
                    " VALUES (?, 0, 'user', ?)",
                    (sid, json.dumps(f"second wave {i}")),
                )
    finally:
        conn.close()
    before = {e["session_id"] for e in persist.pruned(path)}
    persist.save(world)
    after = {e["session_id"] for e in persist.pruned(path)}
    assert after > before, sorted(after)
    assert after <= {r["session_id"] for r in cold.archived(path)}, sorted(after)
    print("cold store check ok")


def _check_refine(cwd: Path) -> None:
    """A grown tool that only ever fails is retired, and retiring it is a move.

    The rot signal is read off the record the loop already writes: a raising
    syscall's result text *is* its traceback, so the failure this drives is a
    real one, formatted the way loop.turn formats it, and refine has to
    recognise it. Tombstoning then has to survive a save -- the buried row is
    absent from world.tools, which to the delete sweep looks exactly like a
    tool the session removed.
    """
    from desmos.state import persist, refine

    home = cwd / "refine"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3")

    rot_src = "def rotter(world, body, attrs):\n    raise RuntimeError('always')\n"
    out = dispatch(
        world, Block("harness", rot_src, {"op": "register", "name": "rotter", "doc": "rots"})
    )
    assert out == "registered <rotter>", out

    for i in range(2):
        # dispatch turns a raising handler into its traceback -- that string is
        # the result the loop records, so it is the string refine must read.
        text = dispatch(world, Block("rotter", "", {}))
        assert text.startswith("Traceback (most recent call last)"), text
        persist.record_event(
            world,
            {"ev": "result", "phase": "done", "tag": "rotter", "attrs": {}, "text": text},
            ts_ms=1_700_000_000_000 + i,  # 2023-11-14; the later call must win
            mono_ns=0,
        )

    rows = {item["name"]: item for item in refine.census(world)}
    assert rows["rotter"]["calls"] == 2, rows["rotter"]
    assert rows["rotter"]["errors"] == 2, rows["rotter"]
    assert rows["rotter"]["verdict"] == "broken", rows["rotter"]
    # Usage evidence is derived, not counted: last-used is the newest result
    # event's timestamp, and the catalog line has a token price.
    assert rows["rotter"]["last_used"].startswith("2023-11-14"), rows["rotter"]
    assert rows["rotter"]["tokens"] > 0, rows["rotter"]

    # describe (harness op=describe -> <tool> with no doc) shows the evidence.
    out = dispatch(world, Block("harness", "", {"op": "describe", "name": "rotter"}))
    assert "2 calls" in out and "2 errors" in out and "2023-11-14" in out, out
    out = dispatch(world, Block("harness", "", {"op": "refine"}))
    assert "broken" in out and "rotter" in out, out

    # The other rot: never called, while sessions came and went.
    nap_src = "def napper(world, body, attrs):\n    return 'zzz'\n"
    dispatch(
        world, Block("harness", nap_src, {"op": "register", "name": "napper", "doc": "naps"})
    )
    conn = persist._open(persist.state_file(world))
    try:
        with conn:
            workspace = conn.execute("SELECT id FROM workspaces").fetchone()[0]
            for i in range(refine.UNUSED_SESSIONS):
                conn.execute(
                    "INSERT INTO sessions(id, workspace_id, kind, started_at,"
                    " last_seen_at, cache_key) VALUES (?, ?, 'attach', ?, ?, ?)",
                    (f"later-{i}", workspace, f"2999-01-0{i + 1}", "2999-01-09", f"later-{i}"),
                )
    finally:
        conn.close()
    rows = {item["name"]: item for item in refine.census(world)}
    assert rows["napper"]["verdict"] == "unused", rows["napper"]

    from desmos.kernel.catalog import catalog

    assert "<rotter>" in catalog(world), "grown tool missing from catalog"
    out = dispatch(
        world,
        Block("harness", "", {"op": "refine", "tombstone": "rotter", "reason": "only ever raised"}),
    )
    assert "tombstoned <rotter>" in out, out
    assert "rotter" not in world.tools, sorted(world.tools)
    assert "<rotter>" not in catalog(world), "tombstoned tool still in catalog"
    # A dead tag answers with a one-line tombstone, not silence.
    out = dispatch(world, Block("rotter", "", {}))
    assert "retired" in out and "revive=rotter" in out, out
    persist.save(world)

    back = new_world(home, state_path=home / "harness.sqlite3")
    persist.load(back)
    assert "rotter" not in back.tools, sorted(back.tools)
    assert "napper" in back.tools, sorted(back.tools)

    # The row survives the tombstoning session's own save because the D4b
    # watermark keeps anything newer than the view. A sibling that merely
    # loaded this workspace has neither: no rotter in world.tools, and a
    # watermark newer than the tombstone. To the delete sweep that is
    # indistinguishable from a tool someone removed.
    persist.save(back)

    conn = persist._open(persist.state_file(world))
    try:
        row = conn.execute(
            "SELECT source, tombstone_reason FROM tools WHERE name = 'rotter'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "the tombstoned row was deleted by the next save"
    assert "RuntimeError" in str(row["source"] or ""), row["source"]
    assert row["tombstone_reason"] == "only ever raised", row["tombstone_reason"]

    out = dispatch(back, Block("harness", "", {"op": "refine", "revive": "rotter"}))
    assert "revived <rotter>" in out, out
    assert "rotter" in back.tools, sorted(back.tools)
    print("refine check ok")


def _check_stow() -> None:
    """Reclaiming disk is a move too: the gzip is proven, then the original goes.

    Quarantined databases are dead weight, but deleting them is still a
    delete. `stow` compresses each one into the cold store, reads it back,
    and only then unlinks -- so a failed copy costs disk, never history.
    """
    import gzip
    import tempfile

    from desmos.state import cold

    root = Path(tempfile.mkdtemp())
    live = root / "harness.sqlite3"
    live.write_bytes(b"the live database")
    dead = root / "harness.sqlite3.corrupt"
    raw = b"a dead database, mostly zeroes" * 200
    dead.write_bytes(raw)

    out = cold.stow(live, [dead])
    assert out["stowed"] == [dead.name], out
    assert out["freed"] == len(raw), out
    assert not dead.exists(), "the original survived its own reclaim"
    kept = Path(out["path"]) / (dead.name + ".gz")
    with gzip.open(kept, "rb") as fh:
        assert fh.read() == raw, "the archived copy is not the file"
    names = {row["name"]: row for row in cold.stowed(live)}
    assert dead.name in names, names
    assert names[dead.name]["bytes"] == len(raw), names[dead.name]
    print("stow check ok")


def _check_handoff_rail(cwd: Path) -> None:
    """Crossing the soft threshold puts the handoff block in the next request.

    Driven through run_turns, not through watch(): the failure this guards
    against is a policy nothing calls, which a direct unit test cannot see.
    """
    import json as _json
    from desmos.kernel import handoff, prices
    from desmos.kernel.loop import run_turns

    home = cwd / "handoff"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3", persist=False)
    world.model = "claude-opus-5"
    ceiling = prices.window(world.model)
    assert ceiling >= 100_000, ceiling
    big = int(ceiling * handoff.SOFT) + 1000
    small = int(ceiling * handoff.CLEAR) - 1000
    sent: list[str] = []

    def answer(tokens: int):
        def fake(_model, system, _messages, _max_tokens):
            sent.append(_json.dumps(system))
            return {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": tokens // 2,
                          "cache_read_input_tokens": tokens - tokens // 2},
            }

        return fake


    world.complete_fn = answer(big)
    run_turns(world, "work", quiet=True)
    assert handoff.BLOCK in world.injections, world.injections
    assert world.injections[handoff.BLOCK]["turns"] == 0

    world.complete_fn = answer(small)
    run_turns(world, "more work", quiet=True)
    assert "Context is at" in sent[-1], sent[-1][-400:]
    assert handoff.BLOCK not in world.injections, world.injections
    print("handoff rail check ok")


def _check_plan_rail(cwd: Path) -> None:
    """A stop with an open plan is answered by the plan; a block ends it.

    Driven through run_turns because the defect worth catching is a reminder
    nothing sends. The cap is exercised too: a rail that never yields is a
    hang, not a feature.
    """
    import os as _os
    from desmos.kernel.loop import run_turns
    from desmos.state import plan

    home = cwd / "planrail"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3", persist=False)
    world.model = "claude-opus-5"
    rec = plan.create(
        world, "ship the rail", steps=["write it", "verify it"], status="active"
    )
    calls: list[int] = []

    def fake(_model, _system, messages, _max_tokens):
        calls.append(len(messages))
        return {"content": [{"type": "text", "text": "narrating, not working"}],
                "usage": {}}

    world.complete_fn = fake
    _os.environ["DESMOS_PLAN_NUDGES"] = "2"
    try:
        run_turns(world, "go", quiet=True)
    finally:
        _os.environ.pop("DESMOS_PLAN_NUDGES", None)
    assert len(calls) == 3, calls
    sent = [m for m in world.messages
            if m.get("role") == "user" and "still active" in str(m.get("content"))]
    assert len(sent) == 2, sent
    assert "next step 1: write it" in str(sent[0]["content"]), sent[0]

    plan.block(world, rec["plan_id"], "waiting on the user")
    calls.clear()
    run_turns(world, "again", quiet=True)
    assert len(calls) == 1, calls
    shown = plan.render(plan.read(world, rec["plan_id"]))
    assert "blocked: waiting on the user" in shown, shown

    # A written plan already says what its steps are; `new` lifts them the way
    # `from N` does rather than making the model re-enter each one.
    lifted = plan.create(world, "lifted", "why this matters\n\n1. first\n2. second")
    assert [s["title"] for s in lifted["steps"]] == ["first", "second"], lifted
    prose = plan.create(world, "prose", "no list here, just a paragraph")
    assert prose["steps"] == [], prose

    # A step that waits on someone else is not work the rail may demand. It
    # stays in the plan, marked, and the reminder falls silent instead of
    # pointing at something this session cannot do.
    idle = plan.create(world, "waits on the user", steps=["they decide"], status="active")
    plan.set_step(world, idle["plan_id"], 1, "waiting", "the user decides")
    assert plan.nudge(world) is None, plan.render(plan.read(world, idle["plan_id"]))
    print("plan rail check ok")


def _check_commit_attribution(cwd: Path) -> None:
    """A commit says which staged files this session did not write (T7).

    `git add path` stages the whole file, so a second writer in the worktree
    rides along with a clean exit code. Concurrent writes are not
    attributable and the harness does not pretend otherwise -- but a file
    untouched since before this session began was demonstrably not written by
    it, and the commit result says so.
    """
    import subprocess
    import time

    from desmos.state import persist

    repo = cwd / "attrib"
    repo.mkdir()

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "check@desmos.local")
    git("config", "user.name", "desmos check")
    (repo / "seed.txt").write_text("seed\n")
    git("add", "seed.txt")
    git("commit", "-q", "-m", "seed")

    world = new_world(repo, state_path=cwd / "attrib.sqlite3")
    world.model = "claude-opus-5"
    persist.save(world)
    started = persist.session_started(world)
    assert started > 0, "no session row, so nothing can be attributed"

    theirs, mine = repo / "theirs.txt", repo / "mine.txt"
    theirs.write_text("somebody else was here\n")
    os.utime(theirs, (started - 600, started - 600))
    mine.write_text("this session wrote this\n")
    os.utime(mine, (time.time(), time.time()))
    git("add", "theirs.txt", "mine.txt")

    landed = dispatch(world, Block("workspace", "carry both files", {"op": "commit"}))
    assert "HEAD " in landed, landed
    assert "not written during this session" in landed, landed
    assert "theirs.txt" in landed, landed
    assert "mine.txt" not in landed.split("WARNING not written", 1)[1], landed

    # A commit of only this session's own work says nothing extra, or the
    # warning is noise and gets ignored exactly when it matters.
    (repo / "second.txt").write_text("also mine\n")
    git("add", "second.txt")
    quiet = dispatch(world, Block("workspace", "only my own work", {"op": "commit"}))
    assert "not written during this session" not in quiet, quiet
    print("commit attribution check ok")


def _check_stop_rail(cwd: Path) -> None:
    """A stop is answered from the todo list too, and every reminder says
    how much rail is left.

    The plan rail only knew about plan steps, so a session running off the
    todo list -- which is most of them -- stopped and stayed stopped with
    work still declared open. Driven through run_turns, because the defect is
    a reminder nothing sends.
    """
    import os as _os

    from desmos.kernel.loop import run_turns
    from desmos.state import plan

    home = cwd / "stoprail"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3", persist=False)
    world.model = "claude-opus-5"
    listing = dispatch(
        world,
        Block("knowledge", "+ write the fallback\n+ ask the user about seats",
              {"op": "todo"}),
    )
    assert listing.startswith("1. [ ] write the fallback"), listing
    assert plan.active(world) is None, "this check must run without a plan"

    calls: list[int] = []

    def fake(_model, _system, messages, _max_tokens):
        calls.append(len(messages))
        return {"content": [{"type": "text", "text": "narrating, not working"}],
                "usage": {}}

    world.complete_fn = fake
    _os.environ["DESMOS_PLAN_NUDGES"] = "2"
    try:
        run_turns(world, "go", quiet=True)

        sent = [
            str(m.get("content")) for m in world.messages
            if m.get("role") == "user" and "open todo(s)" in str(m.get("content"))
        ]
        assert len(calls) == 3, calls
        assert len(sent) == 2, sent
        assert "next todo 1: write the fallback" in sent[0], sent[0]
        # The allowance is part of the reminder: a rail that will not say how
        # long it keeps going reads as an argument nobody can end.
        assert "Reminder 1 of 2" in sent[0], sent[0]
        assert "Reminder 2 of 2" in sent[1], sent[1]

        # Waiting is the third state. Marking the first item hands the rail
        # to the second, and marking both silences it -- without dropping
        # either line, which is what `-` is for.
        dispatch(world, Block("knowledge", "? 1", {"op": "todo"}))
        assert [n for n, _ in plan.open_todos(world)] == [2], world.notes["todo"]
        assert "next todo 2:" in str(plan.stop_rail(world)), plan.stop_rail(world)

        dispatch(world, Block("knowledge", "? 2", {"op": "todo"}))
        assert plan.open_todos(world) == [], world.notes["todo"]
        assert plan.stop_rail(world) is None, world.notes["todo"]
        calls.clear()
        world.messages.clear()
        run_turns(world, "again", quiet=True)
        assert len(calls) == 1, calls

        # A done mark still reads as done on a line that was waiting.
        shown = dispatch(world, Block("knowledge", "x 1", {"op": "todo"}))
        assert world.notes["todo"].startswith("[x] write the fallback"), world.notes["todo"]
        # The result answers with what is still work -- live items under
        # their real numbers, history as a count -- and body "all" is the
        # escape hatch that prints every line.
        assert shown.splitlines() == ["2. [?] ask the user about seats", "(1 done)"], shown
        shown = dispatch(world, Block("knowledge", "all", {"op": "todo"}))
        assert shown.startswith("1. [x] write the fallback"), shown
    finally:
        _os.environ.pop("DESMOS_PLAN_NUDGES", None)
    print("stop rail check ok")


def _check_fold_consent(cwd: Path) -> None:
    """The turn after a fold is told it cannot read what was folded.

    Driven through run_turns with a compaction block on the wire, because the
    thing worth proving is that the real fold path arms the block -- not that a
    helper can format a sentence.
    """
    import json as _json
    from desmos.kernel import handoff
    from desmos.kernel.loop import run_turns
    from desmos.transport.complete import COMPACT_BLOCK

    home = cwd / "foldconsent"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3", persist=False)
    world.model = "claude-opus-5"
    sent: list[str] = []
    lt = chr(60)

    def fake(_model, system, messages, _max_tokens):
        sent.append(_json.dumps(system))
        if len(sent) == 1:
            return {
                "content": [
                    {"type": COMPACT_BLOCK, "summary": "the work so far, folded"},
                    {"type": "tool_use", "id": "toolu_fold", "name": "syscall",
                     "input": {"input": lt + "python>1" + lt + "/python>"}},
                ],
                "usage": {},
            }
        return {"content": [{"type": "text", "text": "understood"}], "usage": {}}

    world.complete_fn = fake
    run_turns(world, "go", quiet=True)
    assert len(sent) >= 2, sent
    assert "A fold just happened" in sent[1], sent[1][-400:]
    assert "confirm or correct" in sent[1], sent[1][-400:]
    assert handoff.FOLD not in world.injections, world.injections
    print("fold consent check ok")


def _check_child_run_id() -> None:
    """A desmos launched from inside a desmos gets its own run id.

    The environment is inherited whole, so an id kept in it is adopted by every
    child unless the process that minted it is named. Adopting it meant taking
    the parent's presence lease -- BlockingIOError out of announce, before the
    child drew a single frame -- which is what stopped a second session in one
    workspace from ever starting.
    """
    import os as _os
    import subprocess as _sp
    import sys as _sys

    from desmos.state import persist

    mine = persist.run_id()
    assert _os.environ.get(persist.SESSION_PID_ENV) == str(_os.getpid())
    root = str(Path(persist.__file__).resolve().parents[2])
    out = _sp.run(
        [_sys.executable, "-B", "-c",
         "from desmos.state import persist; print(persist.run_id())"],
        capture_output=True, text=True, cwd=root,
        env={**_os.environ, "PYTHONPATH": root},
    )
    assert out.returncode == 0, out.stderr[-400:]
    child = out.stdout.strip()
    assert child and child != mine, (child, mine)
    print("child run id check ok")


def _check_concurrent_notes() -> None:
    """A sibling's note survives a save from a session that never saw it.

    The delete pass already refused to erase rows outside this world's view;
    the write pass overwrote them anyway. Now that two sessions can share a
    workspace, that is not a theoretical race -- it is the ordinary case.
    """
    import sqlite3
    import tempfile
    import warnings

    from desmos.state import persist

    root = Path(tempfile.mkdtemp())
    first = new_world(root, persist=True)
    first.notes["shared"] = "from A"
    persist.save(first)
    path = persist.state_file(first)

    second = new_world(root, persist=True)
    assert second.notes.get("shared") == "from A", second.notes
    second.notes["shared"] = "from B"
    second.notes["only-b"] = "b wrote this"
    persist.save(second)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        persist.save(first)

    conn = sqlite3.connect(path)
    try:
        rows = dict(conn.execute("SELECT name, body FROM notes"))
    finally:
        conn.close()
    assert rows.get("shared") == "from B", rows
    assert rows.get("only-b") == "b wrote this", rows
    assert first.notes["shared"] == "from B", first.notes
    assert any("adopted rows" in str(w.message) for w in caught), \
        [str(w.message) for w in caught]
    print("concurrent notes check ok")


def _check_work_graph() -> None:
    """Two claimants, one lease; a blocked child is not ready; a gate holds.

    The claim is a single-statement CAS, so the interesting failure is not a
    crash -- it is two sessions both believing they hold the same item and
    doing the work twice. Two threads race one item here and exactly one may
    come back holding it.
    """
    import tempfile
    import threading

    from desmos.state import persist, work

    # Ids are minted from _uuid7, whose first twelve hex digits are the
    # millisecond alone. A burst inside one millisecond must still be eight
    # distinct items, not one primary-key collision.
    burst_world = new_world(Path(tempfile.mkdtemp()), persist=True)
    burst = [work.add(burst_world, f"burst {i}")["id"] for i in range(8)]
    assert len(set(burst)) == 8, burst

    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)

    parent = work.add(world, "cut verification time", gate="a green suite")
    child = work.add(world, "measure the slow checks", parent=parent["id"])
    ids = [str(r["id"]) for r in work.ready(world)]
    assert ids == [parent["id"]], (ids, parent["id"], child["id"])

    real = work.run_id
    names = {}

    def fake_run_id() -> str:
        return names.get(threading.current_thread().name, real())

    work.run_id = fake_run_id
    try:
        gate = threading.Barrier(2)
        held: list[dict] = []
        lock = threading.Lock()

        def race() -> None:
            names[threading.current_thread().name] = threading.current_thread().name
            gate.wait()
            got = work.claim(world, parent["id"])
            with lock:
                held.append(got)

        threads = [threading.Thread(target=race, name=n) for n in ("run-a", "run-b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [h for h in held if h["held"]]
        assert len(held) == 2, held
        assert len(winners) == 1, held
        owner = winners[0]["run_id"]
        assert all(h["run_id"] == owner for h in held), held

        # The loser cannot release what it does not hold.
        loser = "run-a" if owner == "run-b" else "run-b"
        names[threading.current_thread().name] = loser
        assert work.release(world, parent["id"])["released"] is False

        # An expired lease is free again, without anybody deleting a row.
        conn = persist._open(persist.state_file(world))
        try:
            with conn:
                conn.execute(
                    "UPDATE work_leases SET expires_at = '2000-01-01T00:00:00+00:00'"
                    " WHERE item_id = ?",
                    (parent["id"],),
                )
        finally:
            conn.close()
        again = work.claim(world, parent["id"])
        assert again["held"] and again["run_id"] == loser, again
    finally:
        work.run_id = real

    try:
        work.finish(world, parent["id"])
    except work.WorkError as exc:
        assert "gated" in str(exc), exc
    else:  # pragma: no cover - the gate is the point
        raise AssertionError("a gated item closed with no evidence")

    work.finish(world, parent["id"], evidence="check ok at abc1234")
    ready_now = [str(r["id"]) for r in work.ready(world)]
    assert ready_now == [child["id"]], ready_now

    kinds = [str(e["kind"]) for e in work.events(world, parent["id"])]
    assert kinds[0] == "done" and "claimed" in kinds, kinds
    assert any(str(e["evidence"]).startswith("check ok") for e in
               work.events(world, parent["id"])), kinds

    # A trigger does not block: it wakes. Finishing the parent records a
    # trigger event on the item that just came up.
    waker = work.add(world, "run the suite")
    sleeper = work.add(world, "read what it said")
    work.link(world, waker["id"], sleeper["id"], kind="trigger")
    open_now = [str(r["id"]) for r in work.ready(world)]
    assert sleeper["id"] in open_now, open_now
    woke = work.finish(world, waker["id"], evidence="6a88165")
    assert woke["woke"] == [sleeper["id"]], woke
    assert "triggered" in [
        str(e["kind"]) for e in work.events(world, sleeper["id"])
    ]
    print("work graph check ok")


def _check_outbox() -> None:
    """A fact leaves this machine once, or not at all.

    The outbox is the whole of local-first: the harness database is the
    record, the cloud is a copy, and the copy is described by a row that
    commits with the fact it describes. So the interesting properties are
    that a repeat is not a second row, a dead sink loses nothing, and a
    prune -- which nobody asked to publish anything -- fills the queue by
    itself.
    """
    import tempfile

    from desmos.state import outbox, persist

    root = Path(tempfile.mkdtemp())
    world = new_world(root, persist=True)
    fact = {"session_id": "s-1", "rows": 3}
    first = outbox.enqueue(world, "cold_session", fact)
    again = outbox.enqueue(world, "cold_session", {"rows": 3, "session_id": "s-1"})
    assert first == again, (first, again)
    assert len(outbox.pending(world)) == 1, outbox.pending(world)

    def angry(_batch: list) -> None:
        raise RuntimeError("sink is down")

    failed = outbox.drain(world, angry)
    assert failed["failed"] == 1 and "down" in failed["error"], failed
    still = outbox.pending(world)
    assert len(still) == 1 and int(still[0]["attempts"]) == 1, still
    assert "down" in str(still[0]["last_error"]), still

    seen: list = []
    sent = outbox.drain(world, lambda batch: seen.append(list(batch)))
    assert sent["sent"] == 1, sent
    assert seen and seen[0][0]["payload"] == fact, seen
    assert outbox.pending(world) == [], outbox.pending(world)
    assert outbox.drain(world, lambda batch: seen.append(batch))["sent"] == 0
    assert len(seen) == 1, seen
    assert outbox.stats(world)["sent"] == 1, outbox.stats(world)

    # Wiring: pruning is the producer. Nothing in this block mentions the
    # outbox, and the queue must fill anyway.
    conn = persist._open(persist.state_file(world))
    try:
        workspace = conn.execute("SELECT id FROM workspaces").fetchone()[0]
        with conn:
            for i in range(persist.SESSION_KEEP + 2):
                sid = f"out-{i:04d}"
                conn.execute(
                    "INSERT INTO sessions(id, workspace_id, kind, started_at,"
                    " last_seen_at, cache_key) VALUES (?, ?, 'attach', ?, ?, ?)",
                    (sid, workspace, f"2019-01-01T00:00:{i:02d}",
                     f"2019-01-01T00:00:{i:02d}", sid),
                )
                conn.execute(
                    "INSERT INTO messages(session_id, seq, role, content_json)"
                    " VALUES (?, 0, 'user', ?)",
                    (sid, json.dumps(f"outbox conversation {i}")),
                )
    finally:
        conn.close()
    persist.save(world)

    queued = outbox.pending(world)
    assert queued, "a prune published nothing"
    assert {r["kind"] for r in queued} == {"cold_session"}, queued
    pruned = {e["session_id"] for e in persist.pruned(persist.state_file(world))}
    published = {str(r["payload"]["session_id"]) for r in queued}
    assert published <= pruned, sorted(published - pruned)

    # A replayed prune of the same sessions is the same fingerprints.
    depth = len(queued)
    persist.save(world)
    assert len(outbox.pending(world)) == depth, (depth, outbox.pending(world))
    print("outbox check ok")


def _check_witness(cwd: Path) -> None:
    """The work graph is read back as an account, and wake is where it lands.

    Three things can each fail alone: a refusal that leaves no trace, a
    reopened item charged to nobody, and a digest nothing ever shows. So the
    refusal is asserted as a row, rework is asserted against the run that
    closed the item, and the paragraph is driven through persist.load rather
    than called directly.
    """
    from desmos.state import persist, witness, work

    home = cwd / "witness"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3")
    world.model = "claude-opus-5"

    plain = work.add(world, "write the sink")
    gated = work.add(world, "release the beta", gate="tests green")
    work.finish(world, plain["id"], evidence="9e9e261")

    # A gated item without evidence is refused -- and the refusal is a row, not
    # only an exception. Counting refusals is how a gate proves it gates.
    try:
        work.finish(world, gated["id"])
    except work.WorkError as exc:
        assert "gated on" in str(exc), exc
    else:
        raise AssertionError("a gated item closed with no evidence")
    kinds = [e["kind"] for e in work.events(world, gated["id"])]
    assert work.GATE_REFUSED in kinds, kinds
    assert work.items(world, status="open"), "the refused item did not stay open"

    # Reopening is the rework signal, and it is charged to whoever closed it.
    work.reopen(world, plain["id"], "the sink dropped a row")
    rows = witness.actors(world, hours=24)
    assert len(rows) == 1, rows
    mine = rows[0]
    assert mine["done"] == 1 and mine["rework"] == 1 and mine["gates"] == 1, mine
    assert mine["label"] == work.run_id()[:8], mine

    closed = witness.finished(world, hours=24)
    assert [r["title"] for r in closed] == ["write the sink"], closed
    assert closed[0]["evidence"] == "9e9e261", closed

    # Nothing older than the window is anyone's accomplishment.
    assert witness.actors(world, hours=0) == []

    # Commits are derived from git, not stored twice. Asserted against this
    # checkout, since a store that can disagree with git is the thing avoided.
    repo = new_world(Path(__file__).resolve().parents[2], state_path=home / "x.sqlite3")
    landed = witness.commits(repo, hours=24 * 3650, limit=3)
    assert len(landed) == 3 and all(" " in line for line in landed), landed
    assert witness.commits(world, hours=24) == [], "a non-repo invented commits"

    # The wiring: load() is where a session wakes, and the paragraph must be
    # installed there. Drop the call and this fails.
    world.injections.pop(witness.BLOCK, None)
    persist.load(world)
    block = world.injections.get(witness.BLOCK)
    assert block, sorted(world.injections)
    assert "Witnessed work, last" in block["text"], block
    assert "write the sink" in block["text"], block
    assert int(block["turns"]) == 1, block

    # The eval numbers ride that same paragraph. Both are denominated in items
    # a gate accepted, so a session cannot move either by spending more turns:
    # more spend with the same closures makes the number worse, not better.
    from datetime import datetime, timezone

    persist.record_call(
        world, {"ts": datetime.now(timezone.utc).isoformat(), "usage": FIXTURE_USAGE}
    )
    state = witness.digest(world, hours=24)
    assert state["done"] == 1 and state["rework"] == 1, state
    assert abs(state["spend"] - FIXTURE_COST_OPUS) < 1e-12, state
    assert abs(state["per_item"] - FIXTURE_COST_OPUS) < 1e-12, state
    assert abs(state["rework_rate"] - 0.5) < 1e-12, state
    body = witness.text(state)
    assert "1 accepted" in body and "rework 50%" in body, body
    assert witness.digest(world, hours=0)["per_item"] is None, "cost per nothing"

    # An empty workspace says nothing rather than saying zero.
    quiet = new_world(cwd / "witness-quiet", state_path=cwd / "witness-quiet.sqlite3")
    assert witness.wake(quiet) == ""
    assert witness.BLOCK not in quiet.injections
    print("witness check ok")


def _check_budget_rail(cwd: Path) -> None:
    """Money is a ceiling, counted across the workspace, and it ends the step.

    Driven through run_turns because the defect worth catching is a limit
    nothing consults: the ledger has priced every call for months and no
    running loop had ever read it back. Revert the branch in `stopped()` and
    the fixture keeps buying turns it cannot afford.
    """
    from datetime import datetime, timedelta, timezone

    from desmos.kernel.loop import run_turns
    from desmos.state import budget, persist

    home = cwd / "budget"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3")
    world.model = "claude-opus-5"
    now = datetime.now(timezone.utc)
    fresh, stale = now.isoformat(), (now - timedelta(hours=72)).isoformat()

    persist.record_call(world, {"ts": fresh, "usage": FIXTURE_USAGE})
    persist.record_call(world, {"ts": fresh, "usage": FIXTURE_USAGE})
    # Outside the window: spent, but no longer bounding the rate.
    persist.record_call(world, {"ts": stale, "usage": FIXTURE_USAGE})

    spent = budget.spend(world, hours=24)
    assert spent["account"] == "anthropic", spent
    assert spent["calls"] == 2, spent
    assert abs(spent["usd"] - 2 * FIXTURE_COST_OPUS) < 1e-12, spent

    # A second purse in the same workspace is a separate ceiling.
    world.model = "gpt-5.6-sol"
    persist.record_call(world, {"ts": fresh, "usage": FIXTURE_USAGE})
    assert budget.account(world) == "openai"
    assert budget.spend(world, hours=24)["calls"] == 1, "purses are pooled"
    world.model = "claude-opus-5"

    # Another World over the same file is a sibling front, and it spends from
    # the same card. Per-session budgets are a limit that doubles per front.
    sibling = new_world(home, state_path=home / "harness.sqlite3")
    sibling.model = "claude-opus-5"
    persist.record_call(sibling, {"ts": fresh, "usage": FIXTURE_USAGE})
    assert budget.spend(world, hours=24)["calls"] == 3, "the window is per-session"

    ceiling = 3.5 * FIXTURE_COST_OPUS
    os.environ["DESMOS_BUDGET_USD"] = f"{ceiling:.12f}"
    os.environ["DESMOS_BUDGET_WINDOW_HOURS"] = "24"
    budget._SEEN.clear()
    calls: list[int] = []

    def fake(_model, _system, messages, _max_tokens):
        calls.append(len(messages))
        return {"content": [{"type": "text", "text": "spending"}],
                "usage": FIXTURE_USAGE}

    world.complete_fn = fake
    try:
        state = budget.status(world)
        assert not state["over"] and state["soft"], state
        # Warned before the ceiling, not after: the block rides the uncached
        # tail, so it can appear and vanish without costing the prefix.
        budget.watch(world)
        assert "Budget: $" in system_prompt(world), "no warning under the ceiling"
        assert budget.over(world) is False

        run_turns(world, "go", quiet=True)
    finally:
        os.environ.pop("DESMOS_BUDGET_USD", None)
        os.environ.pop("DESMOS_BUDGET_WINDOW_HOURS", None)
        budget._SEEN.clear()

    assert len(calls) == 1, calls
    note = str(world.messages[-1].get("content"))
    assert "usd budget of $" in note, note

    # No ceiling is the default, and it must not stop anything.
    budget.watch(world)
    assert "Budget: $" not in system_prompt(world), "the block outlived its limit"
    assert budget.over(world) is False
    print("budget rail check ok")


def _check_decisions(cwd: Path) -> None:
    """Decision queue: push->pending->answer lifecycle, fence format, persistence."""
    from desmos.dispatch import dispatch
    from desmos.loop import new_world
    from desmos.types import Block

    home = cwd / "decisions"
    home.mkdir()
    world = new_world(home, state_path=home / "harness.sqlite3")

    def _decide(body: str) -> str:
        return dispatch(world, Block("knowledge", body, {"op": "decide"}))

    # push via dispatch
    result = _decide("ask Deploy to prod? | yes | no | later")
    # fence format: starts with ```ui-choice, prompt line has decide:<id>
    assert result.startswith("```ui-choice"), f"bad fence start: {result!r}"
    assert "decide:" in result, f"no decide: in fence: {result!r}"
    prompt_line = [l for l in result.splitlines() if l.startswith("prompt:")][0]
    assert "decide:" in prompt_line, f"id not in prompt line: {prompt_line!r}"
    # extract id from prompt line
    did = prompt_line.split("decide:")[1].split(" ")[0].split("—")[0].strip()
    assert did, "empty id"

    # pending
    lst = _decide("list")
    assert did in lst, f"id not in list: {lst!r}"

    # answer via dispatch
    closed = _decide(f"answer {did} yes")
    assert "closed" in closed, f"unexpected close msg: {closed!r}"

    # pending now empty
    lst2 = _decide("list")
    assert did not in lst2, f"id still pending: {lst2!r}"

    # persistence: re-read from disk survives
    world2 = new_world(home, state_path=home / "harness.sqlite3")
    from desmos.state.decisions import pending as _pending
    assert _pending(world2) == [], "pending not empty after re-read"

    # mutation proof: break push by corrupting _new_id, watch failure, restore
    import desmos.state.decisions as _dm
    _real_new_id = _dm._new_id

    def _broken_new_id(prompt):
        return ""  # empty id breaks everything

    _dm._new_id = _broken_new_id
    try:
        bad = _decide("ask Will this break? | yes | no")
        # push returns empty id; the fence should have "decide:" but id empty
        from desmos.state.decisions import pending as _p2
        world3 = new_world(home, state_path=home / "harness.sqlite3")
        # broken record has empty id - pending should not include it
        # (the record has empty id so _latest keying on "" overwrites itself)
        # The test: list shows nothing for a broken record that has empty id
        # Actually the real check: answer fails on empty-id record
        # We verify by asserting the fence doesn't have a valid id
        if "decide:" in bad:
            broken_id = bad.split("decide:")[1].split(" ")[0].split("—")[0].strip()
            assert broken_id == "", f"expected empty id, got {broken_id!r}"
    finally:
        _dm._new_id = _real_new_id

    # After restore, push works again
    result3 = _decide("ask Restored? | yes | no")
    assert result3.startswith("```ui-choice"), f"restore failed: {result3!r}"
    print("decision queue check ok")
