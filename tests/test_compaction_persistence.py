import tempfile
import unittest
from pathlib import Path

from desmos.loop import new_world
from desmos.persist import save, turn_aligned


def transcript(count=120, checkpoint=21):
    messages = []
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        content = f"message {index}"
        if index == checkpoint:
            content = [
                {
                    "type": "compaction",
                    "summary": "",
                    "openai": {
                        "type": "compaction",
                        "id": "cmp_test",
                        "encrypted_content": "opaque",
                    },
                }
            ]
        messages.append({"role": role, "content": content})
    return messages


class CompactionPersistenceTests(unittest.TestCase):
    def test_tail_retention_widens_to_latest_compaction_checkpoint(self):
        messages = transcript()
        kept = turn_aligned(messages, keep=20)
        self.assertEqual(kept[0]["role"], "user")
        self.assertEqual(kept[1]["content"][0]["type"], "compaction")
        self.assertEqual(kept[-1], messages[-1])
        self.assertGreater(len(kept), 20)

    def test_newest_checkpoint_wins(self):
        messages = transcript(checkpoint=21)
        messages[81]["content"] = [
            {"type": "compaction", "openai": {"type": "compaction", "id": "new"}}
        ]
        kept = turn_aligned(messages, keep=20)
        self.assertEqual(kept[1]["content"][0]["openai"]["id"], "new")
        self.assertNotIn("cmp_test", str(kept))

    def test_sqlite_round_trip_keeps_opaque_checkpoint_beyond_eighty_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "state.sqlite3"
            world = new_world(root, state_path=db, ns={}, persist=True)
            world.messages = transcript()
            save(world)

            resumed = new_world(root, state_path=db, ns={}, persist=True)
            self.assertGreater(len(resumed.messages), 80)
            self.assertIn("cmp_test", str(resumed.messages))
            self.assertEqual(resumed.messages[-1], world.messages[-1])


if __name__ == "__main__":
    unittest.main()
