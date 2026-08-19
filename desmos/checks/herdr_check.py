"""Herdr reporter check: the real emit path must light the pane sidebar.

Binds a temp AF_UNIX server, points HERDR_SOCKET_PATH at it, and drives
record_event -- the funnel every wire event already passes through -- so the
frames prove the wiring, not just the reporter function.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

_ENV = ("HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_PANE_ID")


def check() -> None:
    import desmos.front.herdr as herdr
    from desmos.loop import new_world
    from desmos.state.persist import record_event

    with tempfile.TemporaryDirectory() as tmp:
        sock_path = str(Path(tmp) / "herdr.sock")
        frames: list[dict] = []
        rejected: list[dict] = []
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(8)
        closing = threading.Event()

        def serve() -> None:
            while not closing.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    if data:
                        frame = json.loads(data)
                        # Mirror the real herdr server: a JSON-RPC id must
                        # be a string; anything else is rejected.
                        if isinstance(frame.get("id"), str):
                            frames.append(frame)
                            reply = b'{"result":{"type":"ok"}}\n'
                        else:
                            rejected.append(frame)
                            reply = (
                                b'{"error":{"code":"invalid_request",'
                                b'"message":"invalid type: expected a string"}}\n'
                            )
                    else:
                        reply = b"{}\n"
                    try:
                        conn.sendall(reply)
                    except OSError:
                        pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        world = new_world(Path(tmp), state_path=Path(tmp) / "harness.sqlite3")

        def emit(ev: dict) -> None:
            record_event(
                world, ev,
                ts_ms=int(time.time() * 1000),
                mono_ns=time.monotonic_ns(),
            )

        saved = {k: os.environ.get(k) for k in _ENV}
        herdr._last_state = None
        try:
            # Inert without the env triple: the emit path must open no socket.
            for key in _ENV:
                os.environ.pop(key, None)
            emit({"ev": "prompt", "text": "hi"})
            assert frames == [], f"inert reporter sent frames: {frames}"

            os.environ["HERDR_ENV"] = "1"
            os.environ["HERDR_SOCKET_PATH"] = sock_path
            os.environ["HERDR_PANE_ID"] = "pane-7"

            emit({"ev": "prompt", "text": "hi"})
            emit({"ev": "turn", "n": 1})          # duplicate working: suppressed
            emit({"ev": "notice", "text": "x"})   # unmapped ev: ignored
            emit({"ev": "decision", "text": "pick one"})
            emit({"ev": "done"})
            emit({"ev": "error", "text": "boom"})  # duplicate idle: suppressed

            deadline = time.monotonic() + 5.0
            while len(frames) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)

            states = [f["params"]["state"] for f in frames]
            assert states == ["working", "blocked", "idle"], states
            first = frames[0]
            assert first["method"] == "pane.report_agent", first
            assert isinstance(first["id"], str) and first["id"], first
            assert rejected == [], f"server rejected non-string ids: {rejected}"
            p = first["params"]
            assert p["pane_id"] == "pane-7", p
            assert p["source"] == "herdr:desmos", p
            assert p["agent"] == "desmos", p
            assert isinstance(p["seq"], int) and p["seq"] > 0, p
            assert p["message"] == "hi", p
            seqs = [f["params"]["seq"] for f in frames]
            assert seqs == sorted(seqs), seqs
            assert frames[1]["params"]["message"] == "pick one", frames[1]
            assert "message" not in frames[2]["params"], frames[2]
        finally:
            for key, val in saved.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
            herdr._last_state = None
            closing.set()
            srv.close()
    print("[check] herdr_check: reporter frames + dedupe over the real emit path")
