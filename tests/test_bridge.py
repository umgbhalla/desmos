import json
import queue
import socket
import threading
import unittest
from unittest.mock import patch

from desmos.front import bridge


class BridgeSocketTests(unittest.TestCase):
    def test_socket_routes_control_ops_out_of_band(self):
        server, client = socket.socketpair()
        self.addCleanup(client.close)
        inbox = queue.Queue()
        cancel = threading.Event()
        pause = threading.Event()
        events = []
        world = object()

        with (
            patch.object(bridge, "_emit", side_effect=events.append),
            patch("desmos.kernel.catalog.steer") as steer,
        ):
            reader = threading.Thread(
                target=bridge._serve_client,
                args=(server, world, inbox, cancel, pause),
            )
            reader.start()
            for message in (
                {"op": "steer", "text": " redirect "},
                {"op": "pause"},
                {"op": "resume"},
                {"op": "quit"},
            ):
                client.sendall((json.dumps(message) + "\n").encode())
            reader.join(2)

        self.assertFalse(reader.is_alive())
        steer.assert_called_once_with(world, "redirect")
        self.assertTrue(cancel.is_set())
        self.assertFalse(pause.is_set())
        self.assertEqual([inbox.get_nowait()], [None])
        self.assertEqual(
            [event["text"] for event in events],
            ["steer queued", "session paused", "session resumed"],
        )


if __name__ == "__main__":
    unittest.main()
