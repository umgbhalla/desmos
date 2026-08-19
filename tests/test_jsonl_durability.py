import fcntl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desmos.kernel.types import World
from desmos.state import decisions, plan


class JsonlDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.world = World(
            cwd=root, state_path=root / ".desmos" / "harness.sqlite3"
        )
        self.addCleanup(self.tmp.cleanup)

    def assert_durable_append(self, module, append, record):
        events = []

        def flock(fh, operation):
            events.append(("lock", operation))

        def fsync(fh):
            events.append(("fsync", fh))

        with (
            patch.object(module.fcntl, "flock", side_effect=flock),
            patch.object(module.os, "fsync", side_effect=fsync),
        ):
            append(self.world, record)

        self.assertEqual(
            [event[0] for event in events], ["lock", "fsync", "lock"]
        )
        self.assertEqual(events[0][1], fcntl.LOCK_EX)
        self.assertEqual(events[-1][1], fcntl.LOCK_UN)

    def test_decision_append_locks_and_fsyncs(self):
        self.assert_durable_append(
            decisions, decisions._append, {"id": "d1", "status": "open"}
        )
        self.assertEqual(len(decisions._all_records(self.world)), 1)

    def test_plan_append_locks_and_fsyncs(self):
        record = {"plan_id": "p1", "rev": 1}
        self.assertIs(plan._append(self.world, record), record)
        # Run the instrumented path separately so the return-value assertion
        # cannot obscure a missing durability call.
        self.assert_durable_append(plan, plan._append, record)
        self.assertEqual(len(plan.revisions(self.world)), 2)


if __name__ == "__main__":
    unittest.main()
