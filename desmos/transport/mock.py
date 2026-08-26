"""Anthropic Messages SSE mock. A real HTTP server on a real port.

llmock-shaped: point ANTHROPIC_BASE_URL at this process and complete() talks
to it the same way it talks to api.anthropic.com. Stdlib only. The event
stream is the one transport_check.success() already feeds read_sse, so a
reply that parses here parses on the live wire.

Not a second complete() engine. Scripted text (and optional speech-XML)
comes out as streamed text_delta events. No keys, no network, no model.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


def text_events(text: str, *, chunk: int = 24) -> list[dict[str, Any]]:
    """One assistant text block as the Anthropic SSE event list."""
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "usage": {"input_tokens": 8, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    ]
    body = text or ""
    step = max(1, int(chunk))
    for i in range(0, len(body), step):
        events.append(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": body[i : i + step]},
            }
        )
    if not body:
        events.append(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": ""},
            }
        )
    events.extend(
        [
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": max(1, len(body.split()))},
            },
            {"type": "message_stop"},
        ]
    )
    return events


def encode_sse(events: list[dict[str, Any]]) -> bytes:
    parts = []
    for ev in events:
        parts.append(f"event: {ev.get('type', 'message')}\n")
        parts.append(f"data: {json.dumps(ev, separators=(',', ':'))}\n\n")
    return "".join(parts).encode()


class MockAnthropic:
    """Scripted /v1/messages server. Exhausted scripts 400 so tests fail loud."""

    def __init__(self, replies: list[str], *, host: str = "127.0.0.1", port: int = 0) -> None:
        self.replies = list(replies)
        self._next = 0
        self.hits: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        handler = self._handler()
        self.server = ThreadingHTTPServer((host, port), handler)
        self.host, self.port = self.server.server_address[:2]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> MockAnthropic:
        if self._thread is not None:
            return self
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        self._thread = t
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> MockAnthropic:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _pop_reply(self) -> str | None:
        with self._lock:
            if self._next >= len(self.replies):
                return None
            text = self.replies[self._next]
            self._next += 1
            return text

    def _record(self, hit: dict[str, Any]) -> None:
        with self._lock:
            self.hits.append(hit)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path in {"/", "/health"}:
                    self._send(200, b"ok", "text/plain")
                    return
                if path == "/hits":
                    body = json.dumps(mock.hits).encode()
                    self._send(200, body, "application/json")
                    return
                self._send(404, b"not found", "text/plain")

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                raw = self._read_body()
                key_present = bool(self.headers.get("x-api-key"))
                model = ""
                n_messages = 0
                try:
                    payload = json.loads(raw.decode() or "{}")
                except ValueError:
                    payload = {}
                if isinstance(payload, dict):
                    model = str(payload.get("model") or "")
                    msgs = payload.get("messages") or []
                    n_messages = len(msgs) if isinstance(msgs, list) else 0
                mock._record(
                    {
                        "path": path,
                        "model": model,
                        "n_messages": n_messages,
                        "has_api_key": key_present,
                        "bytes": len(raw),
                    }
                )
                if path != "/v1/messages":
                    self._send(404, b"not found", "text/plain")
                    return
                reply = mock._pop_reply()
                if reply is None:
                    self._send(
                        400,
                        json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "mock script exhausted"}}).encode(),
                        "application/json",
                    )
                    return
                blob = encode_sse(text_events(reply))
                self._send(200, blob, "text/event-stream")

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _send(self, code: int, body: bytes, content_type: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)

        return Handler


def main(argv: list[str] | None = None) -> int:
    import argparse
    import time

    p = argparse.ArgumentParser(prog="desmos mock", description="Local Anthropic Messages SSE mock")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0, help="0 binds an ephemeral port")
    p.add_argument(
        "--reply",
        action="append",
        default=[],
        help="scripted assistant text (repeatable; consumed in order)",
    )
    args = p.parse_args(argv)
    replies = args.reply or ["hello from mock"]
    mock = MockAnthropic(replies, host=args.host, port=args.port).start()
    print(f"ANTHROPIC_BASE_URL={mock.url}", flush=True)
    print(f"port={mock.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    finally:
        mock.stop()


if __name__ == "__main__":
    raise SystemExit(main())
