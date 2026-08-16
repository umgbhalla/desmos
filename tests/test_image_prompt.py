"""Images typed into the composer must reach the message they belong to.

The Rust side has attached image chips to a prompt for a while: it sends
``{"op": "step", "text": ..., "images": [paths]}``. Nothing on this side read
that key, so the model got the file names as prose and never saw a picture.
These tests drive the real entry points -- ``run_turns`` and the bridge's step
branch -- rather than ``vision.attach``, which was never the broken part.
"""

import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desmos.loop import new_world, run_turns


def _png(dirpath: Path) -> Path:
    data = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )
    p = dirpath / "shot.png"
    p.write_bytes(data)
    return p


class ImagePromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.world = new_world(self.dir, state_path=None, ns={}, persist=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_images_ride_on_the_prompt_they_came_with(self):
        p = _png(self.dir)
        events = []
        run_turns(
            self.world,
            "look at this",
            max_turns=0,
            quiet=True,
            on_event=events.append,
            images=[str(p)],
        )
        content = self.world.messages[0]["content"]
        self.assertIsInstance(content, list, "the prompt never became blocks")
        kinds = [b.get("type") for b in content]
        self.assertIn("image", kinds, f"no image block on the prompt: {kinds}")
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        self.assertIn("look at this", text, "the prompt text was lost")
        self.assertTrue(
            any(e.get("ev") == "attached" for e in events),
            f"no attach event: {[e.get('ev') for e in events]}",
        )

    def test_a_missing_image_is_a_note_not_a_crash(self):
        events = []
        run_turns(
            self.world,
            "look at this",
            max_turns=0,
            quiet=True,
            on_event=events.append,
            images=[str(self.dir / "gone.png")],
        )
        content = self.world.messages[0]["content"]
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        self.assertIn("image attach failed", text)
        self.assertIn("look at this", text)
        self.assertTrue(any(e.get("ev") == "error" for e in events))

    def test_no_images_leaves_the_prompt_a_plain_string(self):
        run_turns(self.world, "just words", max_turns=0, quiet=True)
        self.assertIsInstance(self.world.messages[0]["content"], str)


class BridgeStepTests(unittest.TestCase):
    def test_the_step_branch_forwards_images(self):
        from desmos.front.bridge import _drive

        inbox = queue.Queue()
        inbox.put({"op": "step", "text": "look", "images": ["shot.png", ""]})
        inbox.put(None)
        with (
            patch("desmos.front.bridge.run_turns") as run_turns,
            patch("desmos.front.bridge._emit"),
            patch("desmos.front.bridge._snapshot", return_value={"ev": "snapshot"}),
        ):
            self.assertEqual(_drive(SimpleNamespace(), inbox, threading.Event()), 0)

        self.assertEqual(run_turns.call_args.kwargs["images"], ["shot.png"])


if __name__ == "__main__":
    unittest.main()
