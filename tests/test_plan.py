import json
import tempfile
import unittest
from pathlib import Path

from desmos.kernel import canonical
from desmos.kernel.dispatch import dispatch
from desmos.kernel.types import Block, World
from desmos.state import plan


SPEECH = """All 18 landed. The short version.

Some prose with a decoy list:

1. decoy one
2. decoy two

More prose.

What I want approval on:

1. **Seat**: revert the schema bump, then commit.
2. **Ambient reload**: rebuild the registry per turn.
3. **Refine loop**: retire tools that stop earning their line.
4. **Shadow observer**: counter on the World.
5. **Evals**: a harbor adapter, four methods.
6. **memex**: swap the linear scan for BM25.
"""


def message(text, thinking="private reasoning"):
    return {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": thinking},
            {"type": "text", "text": text},
        ],
    }


class PlanTests(unittest.TestCase):
    """A plan cites its source and can tell you when the citation went stale.

    The routing test drives `dispatch` rather than `handle_plan`, because a
    handler that works but is never reached looks exactly like a shipped
    feature.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.world = World(
            cwd=self.root, state_path=self.root / ".desmos" / "harness.sqlite3"
        )
        self.world.messages = [
            {"role": "user", "content": "go"},
            message(SPEECH),
        ]
        self.addCleanup(self.tmp.cleanup)

    def call(self, body):
        return dispatch(self.world, Block("knowledge", body, {"op": "plan"}))

    # -- provenance ----------------------------------------------------

    def test_capture_lifts_body_and_steps_from_a_message(self):
        rec = plan.create(self.world, "six tracks", source_index=1)
        self.assertEqual(rec["source"]["index"], 1)
        self.assertEqual(rec["source"]["chars"], len(SPEECH))
        self.assertEqual(len(rec["steps"]), 6)
        self.assertEqual(rec["steps"][0]["title"], "Seat: revert the schema bump, then commit.")
        self.assertEqual(rec["steps"][5]["step_id"], 6)
        self.assertTrue(all(s["status"] == "todo" for s in rec["steps"]))

    def test_thinking_never_enters_a_plan(self):
        rec = plan.create(self.world, "six tracks", source_index=1)
        self.assertNotIn("private reasoning", rec["body"])
        self.assertIn("All 18 landed", rec["body"])

    def test_longest_ordered_run_wins_over_a_decoy(self):
        self.assertEqual(len(plan.steps_from_text(SPEECH)), 6)
        self.assertEqual(plan.steps_from_text("no lists here"), [])

    def test_verify_reports_intact_then_changed_then_gone(self):
        rec = plan.create(self.world, "six tracks", source_index=1)
        pid = rec["plan_id"]
        self.assertIn("intact", plan.verify(self.world, pid))
        self.world.messages[1] = message("folded away")
        self.assertIn("CHANGED", plan.verify(self.world, pid))
        self.world.messages = self.world.messages[:1]
        self.assertIn("gone", plan.verify(self.world, pid))

    def test_negative_index_and_out_of_range(self):
        self.assertEqual(plan.message_text(self.world, -1), SPEECH)
        with self.assertRaises(IndexError):
            plan.message_text(self.world, 99)

    # -- append-only ---------------------------------------------------

    def test_revisions_are_appended_never_rewritten(self):
        rec = plan.create(self.world, "six tracks", source_index=1)
        pid = rec["plan_id"]
        plan.set_step(self.world, pid, 1, "done", "committed")
        plan.revise(self.world, pid, status="active")
        revs = plan.history(self.world, pid)
        self.assertEqual([r["rev"] for r in revs], [1, 2, 3])
        self.assertEqual(revs[0]["steps"][0]["status"], "todo")
        self.assertEqual(plan.read(self.world, pid)["status"], "active")
        self.assertEqual(plan.read(self.world, pid)["steps"][0]["note"], "committed")

    def test_a_torn_final_line_is_skipped_not_fatal(self):
        rec = plan.create(self.world, "six tracks", source_index=1)
        with plan.plans_path(self.world).open("a", encoding="utf-8") as fh:
            fh.write('{"plan_id": "trunc", "rev"')
        self.assertEqual(len(plan.revisions(self.world)), 1)
        self.assertIn(rec["plan_id"], plan.latest(self.world))

    def test_bad_status_is_refused(self):
        pid = plan.create(self.world, "x")["plan_id"]
        with self.assertRaises(ValueError):
            plan.revise(self.world, pid, status="nearly")
        with self.assertRaises(KeyError):
            plan.set_step(self.world, pid, 9, "done")

    # -- wiring --------------------------------------------------------

    def test_dispatch_routes_knowledge_op_plan(self):
        self.assertIn("plan", canonical.operations("knowledge"))
        self.assertEqual(self.call(""), "no plans")
        out = self.call("from 1 | six tracks")
        self.assertIn("six tracks", out)
        self.assertIn("[0/6]", out)
        self.assertIn("from message 1", out)
        pid = plan.listing(self.world).split()[0]
        self.assertIn("[x] 1.", self.call(f"step {pid} x 1 done at 5b16cd0"))
        self.assertIn("active", self.call(f"status {pid} active"))
        self.assertIn("intact", self.call(f"verify {pid}"))
        self.assertIn("All 18 landed", self.call(f"show {pid}"))

    def test_dispatch_does_not_fall_through_to_todo(self):
        self.call("new keep todo out of this")
        self.assertNotIn("todo", self.world.notes)

    def test_unknown_command_explains_itself(self):
        self.assertIn("unknown plan command", self.call("frobnicate"))
        self.assertIn("first line is the command", self.call("help"))


if __name__ == "__main__":
    unittest.main()
