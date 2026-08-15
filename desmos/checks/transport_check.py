from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import desmos.transport.complete as complete


class Response:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        lines: list[bytes] = []
        for event in events:
            lines.extend(
                [
                    f"data: {json.dumps(event)}\n".encode(),
                    b"\n",
                ]
            )
        self.lines = lines

    def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def error(error_type: str = "overloaded_error", message: str = "Overloaded") -> dict[str, Any]:
    return {"type": "error", "error": {"type": error_type, "message": message}}


def success(text: str = "ok") -> list[dict[str, Any]]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "usage": {"input_tokens": 3, "output_tokens": 0},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]


def self_check() -> None:
    old_open = complete._open_with_retry
    old_wait = complete._wait_for_retry
    old_key = os.environ.get("ANTHROPIC_API_KEY")
    old_traj = complete.TRAJECTORY_DIR
    with tempfile.TemporaryDirectory() as tmp:
        complete.TRAJECTORY_DIR = str(Path(tmp) / "trajectory")
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        try:
            opened: list[Response] = []
            queue = [Response([error()]), Response(success("recovered"))]
            retry_events: list[dict[str, Any]] = []

            def fake_open(*_args: Any, **_kwargs: Any) -> Response:
                response = queue.pop(0)
                opened.append(response)
                return response

            complete._open_with_retry = fake_open
            complete._wait_for_retry = lambda *_args, **_kwargs: None
            got = complete.complete(
                "claude-opus-5",
                "system",
                [{"role": "user", "content": "hello"}],
                100,
                on_event=retry_events.append,
            )
            assert got["content"][0]["text"] == "recovered", got
            assert len(opened) == 2, "an HTTP-200 overload must reopen the request"
            retries = [event for event in retry_events if event.get("kind") == "retry"]
            assert len(retries) == 1 and "overloaded_error" in retries[0]["reason"], retries

            # A permanent stream error is not retried.
            queue[:] = [Response([error("invalid_request_error", "bad payload")])]
            opened.clear()
            try:
                complete.complete("claude-opus-5", "system", [], 100)
            except complete.AnthropicStreamError as exc:
                assert not exc.retryable and not exc.had_output
            else:
                raise AssertionError("invalid_request_error should escape")
            assert len(opened) == 1

            # Even a transient error cannot be replayed after visible output:
            # the story pane has no retraction mechanism.
            queue[:] = [
                Response(
                    [
                        {
                            "type": "message_start",
                            "message": {"id": "partial", "usage": {}},
                        },
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": "part"},
                        },
                        error(),
                    ]
                )
            ]
            opened.clear()
            try:
                complete.complete("claude-opus-5", "system", [], 100)
            except RuntimeError as exc:
                assert "partial output was already emitted" in str(exc), exc
            else:
                raise AssertionError("partial output must not be duplicated by retry")
            assert len(opened) == 1

            # Backoff remains cancelable.
            complete._wait_for_retry = old_wait
            try:
                complete._wait_for_retry(
                    1.0,
                    "test overload",
                    should_stop=lambda: True,
                )
            except RuntimeError as exc:
                assert "stopped while retrying" in str(exc)
            else:
                raise AssertionError("cancel must interrupt retry backoff")
        finally:
            complete._open_with_retry = old_open
            complete._wait_for_retry = old_wait
            complete.TRAJECTORY_DIR = old_traj
            if old_key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = old_key


if __name__ == "__main__":
    self_check()
    print("transport check ok")
