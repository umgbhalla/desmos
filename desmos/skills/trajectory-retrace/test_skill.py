import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

HERE = Path(__file__).with_name("skill.py")
spec = importlib.util.spec_from_file_location("trajectory_retrace_tested", HERE)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


class RetraceTests(unittest.TestCase):
    def test_content_provenance_beats_user_role(self):
        envelope = """generation: 1
prior steps:
  1. user: fix the renderer
     you: done
prompt: push it
"""
        world = SimpleNamespace(
            messages=[
                {"role": "user", "content": [{"type": "custom_tool_call_output", "output": "noise"}]},
                {"role": "user", "content": envelope},
                {"role": "user", "content": [{"type": "text", "text": "inspect screenshot"}, {"type": "image"}]},
                {"role": "user", "content": "[background task finished while you were away]"},
            ],
            log=[
                {"request": {"input": [
                    {"type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": "Execute this typed task contract. Treat the contract as authoritative."}
                    ]}
                ]}},
                {"request": {"messages": [
                    {"role": "user", "content": [{"type": "text", "text": "fix the renderer"}]}
                ]}},
                {"request": {"messages": [
                    {"role": "user", "content": [{"type": "text", "text": "<" + "result tag='shell'>noise</" + "result>"}]}
                ]}},
            ],
        )
        audit = mod.run(world)
        self.assertEqual(
            [row["text"] for row in audit["prompts"]],
            ["fix the renderer", "push it", "inspect screenshot"],
        )
        self.assertEqual(audit["excluded"]["tool_result"], 1)
        self.assertTrue(any(key.startswith("generated_") for key in audit["excluded"]))


if __name__ == "__main__":
    unittest.main()
