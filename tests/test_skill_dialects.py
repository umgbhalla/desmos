import tempfile
import unittest
from pathlib import Path

from desmos.dispatch import dispatch
from desmos.loop import new_world
from desmos.skills import (
    Skill,
    discover_skills,
    filter_skill_dialects,
    format_skills_for_prompt,
    load_skill_body,
)
from desmos.types import Block


class SkillDialectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "demo"
        (root / "dialects").mkdir(parents=True)
        (root / "SKILL.md").write_text("shared core\n", encoding="utf-8")
        (root / "dialects" / "gpt-5.6.md").write_text(
            "openai delta\n", encoding="utf-8"
        )
        (root / "dialects" / "claude-opus-5.md").write_text(
            "anthropic delta\n", encoding="utf-8"
        )
        self.skill = Skill("demo", "demo skill", root / "SKILL.md", "markdown")

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_model_families_select_only_their_overlay(self):
        openai = load_skill_body(self.skill, "gpt-5.6-sol")
        self.assertIn("shared core", openai)
        self.assertIn("Model dialect overlay: GPT-5.6", openai)
        self.assertIn("openai delta", openai)
        self.assertIn("[desmos-skill-dialect:gpt-5.6]", openai)
        self.assertNotIn("anthropic delta", openai)

        anthropic = load_skill_body(self.skill, "claude-opus-5")
        self.assertIn("shared core", anthropic)
        self.assertIn("Model dialect overlay: Claude Opus 5", anthropic)
        self.assertIn("anthropic delta", anthropic)
        self.assertNotIn("openai delta", anthropic)

    def test_unknown_model_and_missing_overlay_fall_back_to_core(self):
        self.assertEqual(load_skill_body(self.skill, "other-model"), "shared core\n")
        (self.skill.file_path.parent / "dialects" / "gpt-5.6.md").unlink()
        self.assertEqual(load_skill_body(self.skill, "gpt-5.6-luna"), "shared core\n")

    def test_foreign_overlay_is_removed_after_a_model_switch(self):
        openai = load_skill_body(self.skill, "gpt-5.6-sol")
        self.assertIn("openai delta", filter_skill_dialects(openai, "gpt-5.6-luna"))
        switched = filter_skill_dialects(openai, "claude-opus-5")
        self.assertIn("shared core", switched)
        self.assertNotIn("openai delta", switched)
        self.assertNotIn("desmos-skill-dialect", switched)

        anthropic = load_skill_body(self.skill, "claude-opus-5")
        self.assertNotIn(
            "anthropic delta", filter_skill_dialects(anthropic, "gpt-5.6-sol")
        )

    def test_provider_payloads_fence_foreign_loaded_overlays(self):
        from desmos.complete import cached_payload
        from desmos.openai import payload_for

        openai = load_skill_body(self.skill, "gpt-5.6-sol")
        anthropic_body = cached_payload(
            "claude-opus-5",
            "system",
            [{"role": "user", "content": "loaded skill\\n" + openai}],
            2048,
        )
        self.assertNotIn("openai delta", str(anthropic_body))
        self.assertIn("shared core", str(anthropic_body))

        anthropic = load_skill_body(self.skill, "claude-opus-5")
        openai_body = payload_for(
            "gpt-5.6-sol",
            "system",
            [{"role": "user", "content": "loaded skill\\n" + anthropic}],
            2048,
        )
        self.assertNotIn("anthropic delta", str(openai_body))
        self.assertIn("shared core", str(openai_body))

    def test_catalog_prefix_does_not_include_overlay_content(self):
        catalog = format_skills_for_prompt([self.skill])
        self.assertIn("demo skill", catalog)
        self.assertNotIn("openai delta", catalog)
        self.assertNotIn("anthropic delta", catalog)

    def test_dispatch_passes_the_live_model_to_the_loader(self):
        world = new_world(Path(self.tmp.name), state_path=None, ns={}, persist=False)
        world.model = "gpt-5.6-terra"
        world.skills = [self.skill]
        loaded = dispatch(world, Block("skill", "", {"name": "demo"}))
        self.assertIn("openai delta", loaded)
        self.assertNotIn("anthropic delta", loaded)

    def test_pilot_skills_have_distinct_real_overlays(self):
        skills = {skill.name: skill for skill in discover_skills(Path.cwd())}
        for name in ("edit", "show-me", "long-horizon-goal"):
            with self.subTest(name=name):
                openai = load_skill_body(skills[name], "gpt-5.6-sol")
                anthropic = load_skill_body(skills[name], "claude-opus-5")
                self.assertIn("Model dialect overlay: GPT-5.6", openai)
                self.assertIn("Model dialect overlay: Claude Opus 5", anthropic)
                self.assertNotEqual(openai, anthropic)


if __name__ == "__main__":
    unittest.main()
