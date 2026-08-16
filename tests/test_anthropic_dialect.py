import unittest

from desmos.agents.subagent_prompt import _ANTHROPIC as CHILD_ANTHROPIC
from desmos.transport.dialect import dialect


class AnthropicDialectTests(unittest.TestCase):
    def test_opus_guidance_has_progress_delegation_and_honest_status(self):
        prompt = dialect("claude-opus-5")

        self.assertIn("brief progress update after a few calls", prompt)
        self.assertIn("start with one subagent", prompt)
        self.assertIn("blocked or still unproven", prompt)
        self.assertIn("Stay within the requested scope", prompt)
        self.assertIn("state the correction and move on", prompt)

    def test_opus_guidance_does_not_prompt_verification(self):
        prompt = dialect("claude-opus-5").lower()

        self.assertNotIn("run the relevant verification", prompt)
        self.assertNotIn("do the checks", prompt)
        self.assertNotIn("verify with a tool", prompt)

    def test_child_lane_reports_evidence_without_extra_review_prompting(self):
        prompt = CHILD_ANTHROPIC.lower()

        self.assertIn("observed facts", prompt)
        self.assertIn("blocked or unproven", prompt)
        self.assertNotIn("verify with a tool", prompt)
        self.assertNotIn("second prose review", prompt)


if __name__ == "__main__":
    unittest.main()
