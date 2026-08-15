import tempfile
import unittest
from pathlib import Path

from desmos.dispatch import dispatch
from desmos.loop import new_world, seed_builtins
from desmos.agents.subagent import _child_world, resolve
from desmos.subagent_contracts import TaskContract
from desmos.types import Block, Tool


class SubagentTodoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rows = [{"text": "existing", "done": False}]
        self.calls = []

        def parent_todo(body: str, **attrs: str) -> str:
            self.calls.append((body, dict(attrs)))
            # Match the real frozen todo handler: attrs are ignored and each
            # body line is a command. Only a leading plus appends.
            for line in body.splitlines():
                op, _, rest = line.strip().partition(" ")
                if op == "+":
                    self.rows.append({"text": rest, "done": False})
                elif op == "x" and rest.isdigit():
                    self.rows[int(rest) - 1]["done"] = True
                elif op == "-" and rest.isdigit():
                    self.rows.pop(int(rest) - 1)
            return "updated"

        self.parent = new_world(
            Path(self.tmp.name), state_path=None, ns={}, persist=False
        )
        seed_builtins(self.parent)
        self.parent.tools["todo"] = Tool(
            "todo", "parent todo operations", handler=parent_todo
        )
        self.child = _child_world(
            resolve("general"), self.parent, todo_actor="run-123"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_child_context_dispatch_appends_with_attribution(self):
        result = dispatch(
            self.child,
            Block("todo", "child subtask\nx 1", {"action": "append", "row_id": "0"}),
        )
        self.assertEqual(result, "updated")
        self.assertEqual(
            self.rows,
            [
                {"text": "existing", "done": False},
                {"text": "[subagent:run-123] child subtask x 1", "done": False},
            ],
        )
        self.assertEqual(
            self.calls,
            [
                (
                    "+ [subagent:run-123] child subtask x 1",
                    {},
                )
            ],
        )

    def test_child_complete_drop_edit_reject_without_state_change(self):
        for action in ("complete", "drop", "edit", "reorder", "mutate"):
            with self.subTest(action=action):
                before = [dict(row) for row in self.rows]
                result = dispatch(
                    self.child, Block("todo", "replacement", {"action": action})
                )
                self.assertIn("rejected", result)
                self.assertEqual(self.rows, before)
                self.assertEqual(self.calls, [])

    def test_typed_contract_scope_still_exposes_only_the_append_bridge(self):
        contract = TaskContract(
            objective="read and add a discovered follow-up",
            allowed_tools=("python",),
        )
        child = _child_world(
            resolve("general"), self.parent, contract, todo_actor="scoped-run"
        )
        result = dispatch(child, Block("todo", "follow up", {}))
        self.assertEqual(result, "updated")
        self.assertEqual(
            self.rows[-1],
            {"text": "[subagent:scoped-run] follow up", "done": False},
        )

    def test_parent_todo_operations_are_unchanged(self):
        result = dispatch(self.parent, Block("todo", "x 1", {}))
        self.assertEqual(result, "updated")
        self.assertEqual(self.rows, [{"text": "existing", "done": True}])
        self.assertEqual(self.calls, [("x 1", {})])


if __name__ == "__main__":
    unittest.main()
