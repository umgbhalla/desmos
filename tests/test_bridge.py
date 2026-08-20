import io
import json
import os
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


class BridgeOrphanSurvivalTests(unittest.TestCase):
    def test_stdin_eof_with_live_socket_keeps_serving(self):
        # Closed pipe as stdin: iterating it hits EOF immediately.
        r, w = os.pipe()
        os.close(w)
        stdin = os.fdopen(r, "r")
        self.addCleanup(stdin.close)
        inbox = queue.Queue()
        cancel = threading.Event()
        pause = threading.Event()
        events = []
        world = object()

        with patch.object(bridge, "_emit", side_effect=events.append):
            bridge._read_ops(stdin, world, inbox, cancel, pause, socket_up=True)
            # No None posted: the drive loop stays alive.
            self.assertTrue(inbox.empty())
            self.assertEqual(
                [e["text"] for e in events if e["ev"] == "notice"],
                ["stdio client gone; serving socket only"],
            )
            # A socket client still gets answers after the stdio EOF.
            server, client = socket.socketpair()
            self.addCleanup(client.close)
            reader = threading.Thread(
                target=bridge._serve_client,
                args=(server, world, inbox, cancel, pause),
            )
            reader.start()
            client.sendall(b'{"op": "pause"}\n{"op": "quit"}\n')
            reader.join(2)
        self.assertFalse(reader.is_alive())
        self.assertIn("session paused", [e.get("text") for e in events])

    def test_dead_stdout_disables_wire_but_keeps_log_and_clients(self):
        dead = io.StringIO()
        dead.close()
        server, client = socket.socketpair()
        client.settimeout(2)
        wire = bridge._Client(server)
        bridge._CLIENTS.append(wire)

        def cleanup():
            with bridge._WIRE_LOCK:
                if wire in bridge._CLIENTS:
                    bridge._CLIENTS.remove(wire)
            wire.close()
            client.close()
            bridge._LOG_WORLD = None
            bridge._WIRE_DEAD = False

        self.addCleanup(cleanup)
        world = object()
        with (
            patch.object(bridge, "_WIRE", dead),
            patch.object(bridge, "_WIRE_DEAD", False),
            patch.object(bridge, "_LOG_WORLD", world),
            patch("desmos.state.persist.record_event", return_value=1) as rec,
        ):
            bridge._emit({"ev": "notice", "text": "one"})
            bridge._emit({"ev": "notice", "text": "two"})
            self.assertTrue(bridge._WIRE_DEAD)
            # The durable log grew: one record per event.
            self.assertEqual(rec.call_count, 2)
        buf = b""
        while buf.count(b"\n") < 2:
            buf += client.recv(4096)
        lines = [json.loads(l) for l in buf.decode().splitlines()]
        self.assertEqual([l["text"] for l in lines], ["one", "two"])

    def test_stdio_quit_still_stops_drive(self):
        inbox = queue.Queue()
        cancel = threading.Event()
        pause = threading.Event()
        stdin = io.StringIO('{"op": "quit"}\n')
        world = object()
        with patch.object(bridge, "_emit"):
            bridge._read_ops(stdin, world, inbox, cancel, pause, socket_up=True)
        self.assertTrue(cancel.is_set())
        self.assertEqual(bridge._drive(world, inbox, cancel), 0)


if __name__ == "__main__":
    unittest.main()
