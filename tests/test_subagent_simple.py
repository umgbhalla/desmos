import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import desmos.agents.subagent as S
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
            evidence=("file",),
        )
        self.assertTrue(contract.compact)
        self.assertEqual(contract.allowed_paths, ("desmos/complete.py",))
        self.assertEqual(contract.write_paths, ())
        self.assertEqual(contract.acceptance_checks, ("request path is traced",))
        self.assertEqual(contract.allowed_tools, ("bash",))
        self.assertEqual(contract.dependencies, ("prior123",))
        self.assertEqual(contract.required_evidence, ("file",))

    def test_write_scope_still_must_be_inside_read_scope(self):
        with self.assertRaisesRegex(ValueError, "outside allowed_paths"):
            TaskContract.simple("edit", paths=("a.py",), write=("b.py",))

    def test_write_paths_under_an_allowed_directory_pass_post_init(self):
        contract = TaskContract(
            objective="edit",
            allowed_paths=("crates/desmos-tui",),
            write_paths=("crates/desmos-tui/src",),
        )
        self.assertEqual(contract.write_paths, ("crates/desmos-tui/src",))

    def _judge_scope(self, write_paths, changed):
        from desmos.subagent_contracts import RunResult, judge

        contract = TaskContract(
            objective="edit",
            allowed_paths=("crates",),
            write_paths=write_paths,
            require_tool_use=False,
        )
        result = RunResult(
            terminal_state="done",
            stop_reason="completed",
            summary="edited",
            changed_paths=(changed,),
        )
        return judge(contract, result)

    def test_judge_directory_write_scope_accepts_nested_file(self):
        judgment = self._judge_scope(("crates/desmos-tui/src",),
                                     "crates/desmos-tui/src/main.rs")
        self.assertNotIn(
            "changed path is outside write scope: crates/desmos-tui/src/main.rs",
            judgment.reasons,
        )

    def test_judge_rejects_sibling_dir_with_shared_string_prefix(self):
        judgment = self._judge_scope(("crates/src",), "crates/src-evil/main.rs")
        self.assertIn(
            "changed path is outside write scope: crates/src-evil/main.rs",
            judgment.reasons,
        )

    def test_judge_exact_file_write_entry_still_works(self):
        judgment = self._judge_scope(("crates/lib.rs",), "crates/lib.rs")
        self.assertNotIn(
            "changed path is outside write scope: crates/lib.rs",
            judgment.reasons,
        )

    def test_compact_prompt_is_materially_smaller(self):
        full = TaskContract(
            objective="audit",
            allowed_paths=("desmos",),
            acceptance_checks=("one",),
            allowed_tools=("bash",),
            required_evidence=("file",),
        ).prompt()
        compact = TaskContract.simple(
            "audit",
            paths=("desmos",),
            checks=("one",),
            tools=("bash",),
            evidence=("file",),
        ).prompt()
        self.assertLess(len(compact), len(full) * 0.6)

    def test_spawn_simple_is_a_structured_judged_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = new_world(Path(tmp), state_path=None, ns={}, persist=False)
            with patch.object(S._POOL, "submit", return_value=None), patch(
                "desmos.agents.pending.register", return_value=None
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


class SubagentResultPromptTests(unittest.TestCase):
    def _run(self, task, *, simple=None, **config):
        contract, structured = S._contract_for(task, simple)
        return S.Run(
            id="prompt",
            task=contract.objective,
            cfg=S.resolve("general", **config),
            contract=contract,
            structured=structured,
        )

    def test_ordinary_prompt_requests_concise_prose_but_structured_prompts_do_not(self):
        ordinary = self._run("Inspect the cache.")
        prompt = S._user_prompt(ordinary)
        self.assertTrue(prompt.startswith("Inspect the cache.\n\n"))
        self.assertIn("summary, evidence, unresolved items, and checks", prompt)
        self.assertIn("Do not return JSON.", prompt)

        explicit = TaskContract(objective="Inspect the cache.")
        structured = self._run(explicit)
        self.assertEqual(S._user_prompt(structured), explicit.prompt())

        simple = self._run("Inspect the cache.", simple={})
        self.assertEqual(S._user_prompt(simple), simple.contract.prompt())

    def test_ordinary_prompt_preserves_template_and_user_input_precedence(self):
        templated = self._run("Inspect.", task_template="TASK::{task}")
        self.assertTrue(S._user_prompt(templated).startswith("TASK::Inspect.\n\n"))
        templated.cfg.user_input = "CUSTOM USER INPUT"
        self.assertEqual(S._user_prompt(templated), "CUSTOM USER INPUT")

    def test_result_returns_ordinary_prose_unchanged(self):
        run = self._run("Inspect.")
        run.result = "Summary: done.\nEvidence: test passed.\nUnresolved: none."
        S.RUNS[run.id] = run
        try:
            self.assertEqual(S.result(run.id), run.result)
        finally:
            S.RUNS.pop(run.id)


if __name__ == "__main__":
    unittest.main()
