import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import desmos.subagent as S
from desmos.loop import new_world
from desmos.subagent_contracts import TaskContract


class SimpleSubagentContractTests(unittest.TestCase):
    def test_simple_expands_to_the_same_security_fields(self):
        contract = TaskContract.simple(
            "audit cache wiring",
            paths=("desmos/complete.py",),
            write=(),
            checks=("request path is traced",),
            tools=("bash",),
            depends=("prior123",),
            evidence=("file_line",),
        )
        self.assertTrue(contract.compact)
        self.assertEqual(contract.allowed_paths, ("desmos/complete.py",))
        self.assertEqual(contract.write_paths, ())
        self.assertEqual(contract.acceptance_checks, ("request path is traced",))
        self.assertEqual(contract.allowed_tools, ("bash",))
        self.assertEqual(contract.dependencies, ("prior123",))
        self.assertEqual(contract.required_evidence, ("file_line",))

    def test_write_scope_still_must_be_inside_read_scope(self):
        with self.assertRaisesRegex(ValueError, "outside allowed_paths"):
            TaskContract.simple("edit", paths=("a.py",), write=("b.py",))

    def test_compact_prompt_is_materially_smaller(self):
        full = TaskContract(
            objective="audit",
            allowed_paths=("desmos",),
            acceptance_checks=("one",),
            allowed_tools=("bash",),
            required_evidence=("file_line",),
        ).prompt()
        compact = TaskContract.simple(
            "audit",
            paths=("desmos",),
            checks=("one",),
            tools=("bash",),
            evidence=("file_line",),
        ).prompt()
        self.assertLess(len(compact), len(full) * 0.6)

    def test_spawn_simple_is_a_structured_judged_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = new_world(Path(tmp), state_path=None, ns={}, persist=False)
            with patch.object(S._POOL, "submit", return_value=None), patch(
                "desmos.pending.register", return_value=None
            ):
                run_id = S.spawn(
                    "inspect",
                    agent="review",
                    simple={
                        "paths": ("desmos",),
                        "write": (),
                        "checks": ("inspection completed",),
                        "tools": ("bash",),
                    },
                    parent=parent,
                )
            run = S.RUNS.pop(run_id)
        self.assertTrue(run.structured)
        self.assertTrue(run.contract.compact)
        self.assertEqual(run.contract.allowed_paths, ("desmos",))

    def test_unknown_simple_field_is_rejected_before_launch(self):
        with self.assertRaisesRegex(ValueError, "unknown simple scope fields"):
            S._contract_for("inspect", {"pathz": ("desmos",)})


if __name__ == "__main__":
    unittest.main()
