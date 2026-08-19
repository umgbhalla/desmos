import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from desmos.kernel.loop import new_world
from desmos.state import persist
from desmos.state.persist import save


def _new_incarnation():
    """Forget this process's minted run id so the next attach is a resume."""
    os.environ.pop(persist.SESSION_ID_ENV, None)
    os.environ.pop(persist.SESSION_PID_ENV, None)
    # An inherited fresh-session flag would sever the lineage under test.
    os.environ.pop(persist.NEW_SESSION_ENV, None)


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT id, parent_id,"
            " (SELECT count(*) FROM messages WHERE session_id = sessions.id)"
            " FROM sessions ORDER BY started_at, id"
        ).fetchall()
    finally:
        conn.close()


class SessionRowDeferredTests(unittest.TestCase):
    def test_row_appears_on_first_record_not_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state.sqlite3"

            # (a) a bare attach that writes nothing leaves no sessions row.
            _new_incarnation()
            world = new_world(root, state_path=db, ns={}, persist=True)
            self.assertEqual(_rows(db), [])

            # (b) the first saved message mints exactly one row, ours.
            world.messages = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
            save(world)
            rows = _rows(db)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], persist.run_id())
            self.assertEqual(rows[0][2], 2)

            # (c) attribution: a later bare resume inherits the transcript as
            # history, not as its own contribution, and still adds no row.
            first_id = rows[0][0]
            _new_incarnation()
            resumed = new_world(root, state_path=db, ns={}, persist=True)
            self.assertEqual(len(resumed.messages), 2)
            self.assertEqual(resumed.session_message_start, 2)
            self.assertEqual(len(_rows(db)), 1)

            # Its first real save mints its own row, parented on the first,
            # holding only its own slice of the transcript.
            resumed.messages.append({"role": "user", "content": "again"})
            resumed.messages.append({"role": "assistant", "content": "sure"})
            save(resumed)
            rows = _rows(db)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][2], 2)
            self.assertEqual(rows[1][1], first_id)
            self.assertEqual(rows[1][2], 2)

    def test_launch_events_alone_mint_no_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state.sqlite3"

            # (a) the real launch path -- bridge._open_log announces 'session'
            # and the TUI 'ready' -- must not mint a sessions row by itself.
            _new_incarnation()
            world = new_world(root, state_path=db, ns={}, persist=True)
            seq = persist.record_event(
                world,
                {"ev": "session", "session_id": persist.run_id(), "cwd": str(root)},
                ts_ms=1,
                mono_ns=1,
            )
            self.assertEqual(seq, 0)
            self.assertEqual(
                persist.record_event(world, {"ev": "ready"}, ts_ms=2, mono_ns=2), 0
            )
            self.assertEqual(_rows(db), [])

            # (b) the first saved message mints exactly one row, ours.
            world.messages = [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
            save(world)
            rows = _rows(db)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], persist.run_id())

            # (c) the buffered launch was flushed as the row's first events,
            # so replay still opens on 'session' then 'ready', FK intact.
            first_id = rows[0][0]
            conn = sqlite3.connect(db)
            try:
                head = conn.execute(
                    "SELECT session_id, kind FROM events ORDER BY seq LIMIT 2"
                ).fetchall()
                self.assertEqual(head, [(first_id, "session"), (first_id, "ready")])
                self.assertEqual(
                    conn.execute("PRAGMA foreign_key_check").fetchall(), []
                )
            finally:
                conn.close()

            # (d) once the row exists, launch events append directly.
            self.assertGreater(
                persist.record_event(world, {"ev": "ready"}, ts_ms=3, mono_ns=3), 0
            )
            self.assertEqual(len(_rows(db)), 1)

            # (e) a later bare resume that only announces still adds no row.
            _new_incarnation()
            resumed = new_world(root, state_path=db, ns={}, persist=True)
            self.assertEqual(
                persist.record_event(resumed, {"ev": "session"}, ts_ms=4, mono_ns=4),
                0,
            )
            self.assertEqual(len(_rows(db)), 1)


if __name__ == "__main__":
    unittest.main()
