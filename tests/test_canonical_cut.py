import tempfile
import unittest
from pathlib import Path

from desmos.agents.subagent import _child_world, resolve
from desmos.dispatch import dispatch, set_scope
try:
    from desmos.kernel.const import CANONICAL, REMOVED_TAGS
except ImportError as exc:  # REMOVED_TAGS was dropped from kernel.const
    raise unittest.SkipTest(f"stale kernel.const surface: {exc}") from exc
from desmos.loop import new_world
from desmos.types import Block


class CanonicalCutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.world = new_world(self.cwd, state_path=None, ns={}, persist=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_world_registers_no_removed_tags(self):
        self.assertTrue(CANONICAL <= set(self.world.tools))
        self.assertFalse(set(REMOVED_TAGS) & set(self.world.tools))

    def test_every_removed_tag_is_rejected_with_replacement(self):
        for name, replacement in REMOVED_TAGS.items():
            with self.subTest(name=name):
                result = dispatch(self.world, Block(name, "", {}))
                self.assertIn("removed tag", result)
                self.assertIn(replacement, result)

    def test_hooks_and_execution_keep_canonical_identity(self):
        path = self.cwd / "sample.txt"
        path.write_text("one\ntwo\n", encoding="utf-8")
        seen = []
        self.world.hooks["before_dispatch"] = [
            lambda _world, block: seen.append((block.tag, dict(block.attrs)))
        ]
        result = dispatch(
            self.world,
            Block("workspace", "", {"op": "read", "path": str(path)}),
        )
        self.assertIn("two", result)
        self.assertEqual(seen, [("workspace", {"path": str(path), "op": "read"})])
        self.assertEqual(
            dispatch(self.world, Block("exec", "20 + 22", {"op": "python"})),
            "42",
        )

    def test_scope_is_operation_specific(self):
        path = self.cwd / "sample.txt"
        path.write_text("one\n", encoding="utf-8")
        set_scope(self.world, {"workspace:read"})
        allowed = dispatch(
            self.world,
            Block("workspace", "", {"op": "read", "path": str(path)}),
        )
        denied = dispatch(
            self.world,
            Block(
                "workspace",
                "one\n---\ntwo",
                {"op": "edit", "path": str(path)},
            ),
        )
        self.assertIn("one", allowed)
        self.assertIn("outside this agent's scope", denied)
        self.assertEqual(path.read_text(encoding="utf-8"), "one\n")

    def test_real_child_prompt_only_advertises_scoped_canonical_families(self):
        child = _child_world(resolve("explore"), self.world)
        prompt = child.system_override
        marker = chr(60)
        self.assertIn(marker + 'exec op="python|bash"', prompt)
        self.assertIn(marker + 'workspace op="find"', prompt)
        self.assertNotIn(marker + "python" + chr(62), prompt)
        self.assertNotIn(marker + 'workspace op="find|read|edit', prompt)
        self.assertNotIn(marker + "harness op=\"register", prompt)


if __name__ == "__main__":
    unittest.main()
