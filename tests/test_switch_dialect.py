import unittest
from types import SimpleNamespace

from desmos.transport.dialect import block, dialect


class SwitchSteeringTests(unittest.TestCase):
    """Both families must be told when to switch, not just that they can.

    `capabilities()` has always stated the mechanism. It never said a switch
    is an ordinary move, and it never said the next model reads the
    conversation without your reasoning -- so the handoff has to be written
    before the call or it does not exist.
    """

    families = ("claude-opus-5", "gpt-5.6-sol")

    def test_every_family_gets_switch_steering(self):
        for model in self.families:
            with self.subTest(model=model):
                prompt = dialect(model)
                self.assertIn("Switching model mid-task is an ordinary move", prompt)
                self.assertIn("not to dodge a hard step", prompt)
                self.assertIn("write the handoff", prompt)

    def test_steering_names_the_reasoning_loss(self):
        for model in self.families:
            with self.subTest(model=model):
                prompt = dialect(model)
                self.assertIn("inherits speech and results but never your", prompt)

    def test_reaches_the_assembled_block(self):
        """The wiring, not the string: block() is what catalog.py appends."""
        for model in self.families:
            with self.subTest(model=model):
                prompt = block(SimpleNamespace(model=model))
                self.assertIn("Switching model mid-task is an ordinary move", prompt)
                self.assertIn("switching model or effort mid-session", prompt)


if __name__ == "__main__":
    unittest.main()
