from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable

INTERLEAVED_BETA = "interleaved-thinking-2025-05-14"
# Server-side compaction. The API folds earlier turns into a `compaction`
# block once the input crosses the trigger, and uses that block on the next
# request to replace everything before it. The transcript stays append-only:
# nothing local is rewritten, so the ABI and catalog cache blocks -- which sit
# ahead of every message -- are never touched by a fold.
COMPACT_BETA = "compact-2026-01-12"
COMPACT_STRATEGY = "compact_20260112"
COMPACT_BLOCK = "compaction"
ADAPTIVE_MARKERS = (
    "opus-5",
    "opus-4-6",
    "opus-4-7",
    "opus-4-8",
    "sonnet-4-6",
    "sonnet-5",
    "fable-5",
    "mythos",
)
BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 16384,
}


def split_system(system: str) -> tuple[str, str]:
    marker = "\n\n# tools"
    if marker in system:
        i = system.index(marker)
        return system[:i], system[i + 2 :]
    return system, ""


def adaptive_model(model: str) -> bool:
    name = model.lower()
    return any(token in name for token in ADAPTIVE_MARKERS)


def thinking_level(value: str | None) -> str:
    raw = (value or "low").strip().lower()
    if raw in {"off", "none", "0", "false"}:
        return "off"
    if raw in {"minimal", "low", "medium", "high", "xhigh", "max"}:
        return raw
    return "low"


def apply_thinking(payload: dict[str, Any], model: str, level: str | None) -> list[str]:
    """Mutate payload. Return extra anthropic-beta tokens (Pi/tau)."""
    mode = thinking_level(level)
    if mode == "off":
        return []
    if adaptive_model(model):
        # Opus 5 / 4.6+: adaptive thinking already interleaves. No beta header.
        # xhigh is a real rung the API accepts, not a synonym for max. Folding
        # them together meant picking xhigh silently ran max. minimal is the
        # only level with no wire equivalent.
        effort = "low" if mode == "minimal" else mode
        payload["thinking"] = {"type": "adaptive", "display": "summarized"}
        payload["output_config"] = {"effort": effort}
        return []
    budget = BUDGETS.get(mode, 2048)
    if int(payload.get("max_tokens") or 0) <= budget:
        payload["max_tokens"] = budget + 1024
    payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    return [INTERLEAVED_BETA]


# Where the Responses endpoint should start folding. Anthropic picks its own
# trigger; OpenAI wants a number, and without one the fold never happens.
OPENAI_COMPACT_TRIGGER = int(os.environ.get("DESMOS_COMPACT_TRIGGER") or 300_000)
_CACHE_KEY: str | None = None


def session_cache_key() -> str:
    """A stable key for this process, so the endpoint can reuse its prefix.

    Stable is the whole point: a fresh key per request is the same as no key.
    It lives for the life of the bridge, which is also the life of the
    transcript it is caching.
    """
    global _CACHE_KEY
    if _CACHE_KEY is None:
        import uuid

        _CACHE_KEY = f"desmos-{uuid.uuid4().hex[:16]}"
    return _CACHE_KEY


def apply_compaction(payload: dict[str, Any], model: str) -> list[str]:
    """Ask the server to fold old turns. Same model set as adaptive thinking."""
    if not adaptive_model(model):
        return []
    payload["context_management"] = {"edits": [{"type": COMPACT_STRATEGY}]}
    return [COMPACT_BETA]


def wire_content(content: Any) -> list[dict[str, Any]]:
    """Replay assistant blocks the way Pi convertMessages / tau _anthropic_message do."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for raw in content:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        # Only provenance across *providers* matters here. A signature Opus 5
        # produced replays to Sonnet 4.6 and is accepted -- checked against the
        # live endpoint, not assumed -- so switching inside the Anthropic list
        # needs no fence and the signature is replayed as-is.
        #
        # A block another provider produced. openai.py stamps every one of its
        # blocks with the raw item under "openai", and it puts that item's id in
        # "signature" as a provenance marker -- not as an Anthropic thinking
        # signature. Replaying one here is a hard 400 ("Invalid `signature` in
        # `thinking` block"), which bricks any session that switched to OpenAI
        # and back. Speech still replays; the provider-shaped parts do not.
        foreign = raw.get("openai") is not None
        if kind == "thinking":
            text = raw.get("thinking") or ""
            signature = raw.get("signature") or ""
            if foreign:
                if text.strip():
                    blocks.append({"type": "text", "text": text})
                continue
            if raw.get("redacted") and (raw.get("data") or signature):
                blocks.append({"type": "redacted_thinking", "data": raw.get("data") or signature})
                continue
            if not signature:
                if text.strip():
                    blocks.append({"type": "text", "text": text})
                continue
            blocks.append({"type": "thinking", "thinking": text, "signature": signature})
        elif kind == "redacted_thinking":
            data = raw.get("data") or ""
            if data:
                blocks.append({"type": "redacted_thinking", "data": data})
        elif kind == COMPACT_BLOCK:
            # Replay verbatim. This block is the server's pointer to the turns
            # it folded; drop it on the way back out and the next request
            # carries no cut point, so the fold silently un-does itself and the
            # transcript grows again with nothing on screen to say why.
            # A fold another provider made is not a cut point here, though --
            # it names turns this endpoint never folded.
            if not foreign:
                blocks.append(dict(raw))
        elif kind == "custom_tool_call":
            text = raw.get("input") or ""
            if text:
                blocks.append({"type": "text", "text": text})
        elif kind == "text":
            text = raw.get("text") or ""
            if text:
                blocks.append({"type": "text", "text": text})
    return blocks


def assistant_content(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep thinking / redacted_thinking / compaction / text. Drop everything else."""
    blocks: list[dict[str, Any]] = []
    for raw in resp.get("content") or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == COMPACT_BLOCK:
            blocks.append(dict(raw))
        elif kind == "thinking":
            item = {"type": "thinking", "thinking": raw.get("thinking") or ""}
            if raw.get("signature"):
                item["signature"] = raw["signature"]
            # A provider's own item, kept whole so the next request can replay
            # it verbatim. OpenAI reasoning is encrypted and id-bearing: rebuild
            # it from the summary and the model loses the thought.
            if isinstance(raw.get("openai"), dict):
                item["openai"] = raw["openai"]
            blocks.append(item)
        elif kind == "redacted_thinking":
            blocks.append({"type": "redacted_thinking", "data": raw.get("data") or ""})
        elif kind == "custom_tool_call":
            item = {
                "type": "custom_tool_call",
                "name": raw.get("name") or "syscall",
                "call_id": raw.get("call_id") or "",
                "input": raw.get("input") or "",
            }
            if isinstance(raw.get("openai"), dict):
                item["openai"] = raw["openai"]
            blocks.append(item)
        elif kind == "text":
            text = raw.get("text") or ""
            provider_item = raw.get("openai") if isinstance(raw.get("openai"), dict) else None
            # An empty text block carrying a provider item is not empty speech:
            # it is a Responses item this harness does not render (a
            # function_call, its output, a web search). Dropped here it never
            # reached to_input, while the reasoning item that produced it did --
            # and Responses 400s on a reasoning item replayed without its
            # required following item, which poisons the rest of the session.
            if text or provider_item:
                item = {"type": "text", "text": text}
                if provider_item:
                    item["openai"] = provider_item
                blocks.append(item)
    return blocks or [{"type": "text", "text": ""}]


def compaction_block(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The fold marker in an assembled assistant message, if the server sent one."""
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == COMPACT_BLOCK:
            return block
    return None


def thinking_text(blocks: list[dict[str, Any]]) -> str:
    parts = []
    for block in thought_blocks(blocks):
        parts.append("[redacted thinking]" if block["redacted"] else block["text"])
    return "\n".join(parts)


def redact_wire(obj: Any) -> Any:
    """Copy a POST/response tree for the TUI. Never includes redacted ciphertext or keys."""
    if isinstance(obj, dict):
        if obj.get("type") == "redacted_thinking":
            return {"type": "redacted_thinking", "data": "[redacted]"}
        return {
            k: redact_wire(v)
            for k, v in obj.items()
            if k not in {"x-api-key", "api_key", "authorization"}
        }
    if isinstance(obj, list):
        return [redact_wire(v) for v in obj]
    return obj


def thought_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per wire thinking block. Never includes redacted ciphertext."""
    out: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "thinking" and (block.get("thinking") or "").strip():
            out.append({"redacted": False, "text": block["thinking"]})
        elif kind == "thinking" and block.get("openai") is not None:
            # A reasoning item whose summary came back empty -- routine at low
            # effort, where the summary is "auto" and often absent. The thought
            # happened and was billed; counting it as nothing made the wire card
            # read "thinking 0" for a turn that visibly spent seconds thinking.
            # Unreadable is what redacted already means here.
            out.append({"redacted": True, "text": ""})
        elif kind == "redacted_thinking":
            out.append({"redacted": True, "text": ""})
    return out


def cached_payload(
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    thinking: str | None = "low",
) -> dict[str, Any]:
    """Pi/Anthropic: cache ABI, cache catalog, cache last *user* only. Replay thinking."""
    from desmos.skills import filter_skill_dialects

    cache = {"type": "ephemeral"}
    abi, catalog_text = split_system(system)
    sys_blocks: list[dict[str, Any]] = [{"type": "text", "text": abi, "cache_control": cache}]
    if catalog_text.strip():
        sys_blocks.append({"type": "text", "text": catalog_text, "cache_control": cache})
    msgs: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            blocks = wire_content(m.get("content"))
        elif isinstance(m.get("content"), str):
            text = filter_skill_dialects(m["content"], model)
            blocks = [{"type": "text", "text": text}] if text else []
        elif isinstance(m.get("content"), list):
            blocks = []
            for raw in m["content"]:
                if isinstance(raw, dict):
                    if raw.get("type") == "custom_tool_call_output":
                        block = {
                            "type": "text",
                            "text": filter_skill_dialects(raw.get("output") or "", model),
                        }
                    else:
                        block = {k: v for k, v in raw.items() if k != "cache_control"}
                        if block.get("type") == "text" and isinstance(block.get("text"), str):
                            block["text"] = filter_skill_dialects(block["text"], model)
                    blocks.append(block)
                elif isinstance(raw, str) and raw:
                    blocks.append({"type": "text", "text": raw})
        else:
            blocks = []
        if not blocks:
            continue
        msgs.append({"role": role, "content": blocks})
    for m in reversed(msgs):
        if m["role"] == "user" and m["content"]:
            m["content"][-1]["cache_control"] = dict(cache)
            break
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": sys_blocks,
        "messages": msgs,
        # A turn is over the moment the model hands back a syscall. Left to
        # run, it keeps writing -- and what it writes next is the reply to its
        # own call: an invented result block, then conclusions drawn from the
        # invention. Measured on one session, 67% of assistant output was
        # self-written transcript, and every false commit hash and phantom test
        # count came from reading that back as fact. These cut generation at
        # the moment it starts impersonating the harness.
        "stop_sequences": ["\n<result", "\nuser<"],
    }
    payload["_betas"] = apply_thinking(payload, model, thinking) + apply_compaction(payload, model)
    return payload


RETRY_STREAM_ERROR_TYPES = frozenset(
    {"overloaded_error", "rate_limit_error", "api_error", "timeout_error"}
)


class AnthropicStreamError(RuntimeError):
    """An error event delivered inside an HTTP-200 Anthropic SSE stream."""

    def __init__(self, error_type: str, message: str, *, had_output: bool) -> None:
        super().__init__(f"Anthropic stream error: {message}")
        self.error_type = error_type
        self.had_output = had_output
        self.retryable = error_type in RETRY_STREAM_ERROR_TYPES


def _stream_budget() -> float:
    """Total seconds the stream-retry ladder is willing to wait."""
    return sum(min(0.5 * (2**i), STREAM_RETRY_CAP) for i in range(STREAM_RETRIES - 1))


def _stream_has_output(state: dict[str, Any]) -> bool:
    # message_start is metadata and emits nothing. content_block_start is the
    # first event that can create visible TUI state (including redacted thought),
    # so any block means replay would risk duplication.
    return bool(state.get("blocks"))


def apply_stream_event(
    state: dict[str, Any],
    ev: dict[str, Any],
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Fold one Anthropic SSE event into an assembled message.

    Fires thinking_delta / text_delta / redacted thinking. Never puts
    redacted ciphertext on on_event. The assembled message keeps wire data
    so assistant_content can replay it; redact_wire strips it for the TUI.
    """
    kind = ev.get("type")
    if kind == "error":
        err = ev.get("error") or {}
        error_type = str(err.get("type") or "stream_error") if isinstance(err, dict) else "stream_error"
        msg = err.get("message") if isinstance(err, dict) else ev.get("message")
        raise AnthropicStreamError(
            error_type,
            str(msg or ev),
            had_output=_stream_has_output(state),
        )
    if kind == "message_start":
        message = dict(ev.get("message") or {})
        message["content"] = []
        state["message"] = message
        state["blocks"] = []
        return
    if kind == "content_block_start":
        idx = int(ev.get("index") or 0)
        block = dict(ev.get("content_block") or {})
        _pad_blocks(state, idx)
        state["blocks"][idx] = block
        if block.get("type") == "redacted_thinking" and on_event is not None:
            on_event({"kind": "thinking", "redacted": True, "text": ""})
        return
    if kind == "content_block_delta":
        idx = int(ev.get("index") or 0)
        _pad_blocks(state, idx)
        block = state["blocks"][idx]
        delta = ev.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "thinking_delta":
            chunk = delta.get("thinking") or ""
            block["thinking"] = (block.get("thinking") or "") + chunk
            if chunk and on_event is not None:
                on_event({"kind": "thinking_delta", "text": chunk})
        elif dtype == "text_delta":
            chunk = delta.get("text") or ""
            block["text"] = (block.get("text") or "") + chunk
            if chunk and on_event is not None:
                on_event({"kind": "text_delta", "text": chunk})
            seen = block.get("text") or ""
            marks = state.setdefault("_degen_checked", {})
            if len(seen) - marks.get(idx, 0) >= DEGEN_CHECK_EVERY:
                marks[idx] = len(seen)
                cut = degenerate_cut(seen)
                if cut is not None:
                    block["text"] = seen[:cut]
                    state["degenerate"] = True
        elif dtype == "signature_delta":
            block["signature"] = (block.get("signature") or "") + (delta.get("signature") or "")
        else:
            # A delta for a block type this harness does not special-case still
            # belongs to that block. Append its string fields rather than
            # dropping them, so a block we only pass through (a compaction
            # summary) assembles whole instead of arriving empty.
            for field, value in delta.items():
                if field != "type" and isinstance(value, str):
                    block[field] = (block.get(field) or "") + value
        return
    if kind == "content_block_stop":
        return
    if kind == "message_delta":
        message = state.setdefault("message", {})
        delta = ev.get("delta") or {}
        if "stop_reason" in delta:
            message["stop_reason"] = delta.get("stop_reason")
        if "stop_sequence" in delta:
            message["stop_sequence"] = delta.get("stop_sequence")
        usage = ev.get("usage") or {}
        if usage:
            merged = dict(message.get("usage") or {})
            merged.update(usage)
            message["usage"] = merged
        return


def assemble_message(state: dict[str, Any]) -> dict[str, Any]:
    message = dict(state.get("message") or {})
    blocks = list(state.get("blocks") or [])
    message["content"] = blocks or [{"type": "text", "text": ""}]
    return message


def iter_sse_lines(resp: Any) -> Iterable[Any]:
    """Yield HTTP/SSE lines as they arrive. Never slurp the body.

    HTTPResponse.readline() is the chunk-decoded path. ``for line in resp``
    can wait for EOF on some urllib wrappers, which is why the TUI painted
    thinking/speech only after complete() returned.
    """
    readline = getattr(resp, "readline", None)
    if not callable(readline):
        yield from resp
        return
    while True:
        line = readline()
        if not line:
            return
        yield line


def read_sse(
    lines: Iterable[Any],
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Parse an Anthropic event-stream into one assembled message."""
    state: dict[str, Any] = {"message": {}, "blocks": []}
    data_parts: list[str] = []
    saw_stop = False

    def flush() -> None:
        nonlocal data_parts, saw_stop
        if not data_parts:
            return
        raw = "\n".join(data_parts)
        data_parts = []
        if raw.strip() in {"", "[DONE]"}:
            return
        ev = json.loads(raw)
        if isinstance(ev, dict):
            if ev.get("type") == "message_stop":
                saw_stop = True
            apply_stream_event(state, ev, on_event)

    for line in lines:
        if state.get("degenerate"):
            break
        if should_stop is not None and should_stop():
            break
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.rstrip("\r\n")
        if line == "":
            flush()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            continue
        if line.startswith("data:"):
            data_parts.append(line[5:].lstrip())
    flush()
    # A stream that just runs dry is not a finished answer. Without this the
    # half-written reply is returned as if the model had chosen to stop: its
    # last syscall has no closing tag, scan() skips unterminated blocks, the
    # turn reports done, and a dropped socket gets committed as the step's
    # result. Ctrl+C is the one legitimate early exit, so it is not an error.
    if state.get("degenerate"):
        # Not an error: the reply up to the cut is real, and whatever syscalls
        # it opened still dispatch. Only the stuck tail is dropped.
        msg = assemble_message(state)
        msg["stop_reason"] = "degenerate_repetition"
        return msg
    if not saw_stop and not (should_stop is not None and should_stop()):
        raise RuntimeError("Anthropic stream ended before message_stop")
    return assemble_message(state)


# A decoder can fall into a repetition attractor and emit the same short unit
# until the connection dies. One session took 43,815 copies of "url": 220KB
# painted into the story pane a line at a time, appended to the transcript,
# and carried into every later POST until compaction folded it away. Nothing
# caught it because it was ordinary assistant speech. Catch it in the stream.
# Sized against false positives, not against reaction time. Truncating real
# output is worse than painting eighty junk lines, so the bar is eight
# identical copies filling 384 characters -- a sentence repeated four times is
# someone making a point, eight is a stuck decoder.
DEGEN_WINDOW = 384
DEGEN_MAX_PERIOD = 48
DEGEN_MIN_REPEATS = 8
DEGEN_CHECK_EVERY = 64


def degenerate_cut(text: str) -> int | None:
    """Index where a repetition attractor begins, or None.

    Only the tail is examined: if the last DEGEN_WINDOW characters are a whole
    number of copies of one unit no longer than DEGEN_MAX_PERIOD, walk back
    through the earlier text to the first copy and return that index. Text
    before the cut is whatever the model wrote before it got stuck, and is
    kept.
    """
    if len(text) < DEGEN_WINDOW:
        return None
    tail = text[-DEGEN_WINDOW:]
    for period in range(1, DEGEN_MAX_PERIOD + 1):
        repeats = DEGEN_WINDOW // period
        if repeats < DEGEN_MIN_REPEATS:
            break
        unit = tail[-period:]
        span = period * repeats
        if unit * repeats != text[-span:]:
            continue
        cut = len(text) - span
        while cut - period >= 0 and text[cut - period : cut] == unit:
            cut -= period
        return cut
    return None


def _pad_blocks(state: dict[str, Any], idx: int) -> None:
    blocks = state.setdefault("blocks", [])
    while len(blocks) <= idx:
        blocks.append({})


# Statuses worth trying again. A 400 is a payload bug and will fail forever;
# 429 and 5xx are the endpoint asking for a moment. Overload is routine, and
# one of them used to kill a twenty-turn step.
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
RETRIES = 4
# An in-stream overload is a different animal from a connect-time blip. The
# endpoint is out of capacity, and that lasts minutes -- so four attempts with
# an 8s ceiling spent 3.5 seconds in total and then reported a hard failure,
# which reads exactly like having no retry at all. This budget is ~50s.
STREAM_RETRIES = 8
STREAM_RETRY_CAP = 20.0
# Honour retry-after, but cap it. Parking for an hour with no cancel point is
# worse than failing and letting the human decide.
RETRY_CAP = 30.0


def _retry_after(err: Any, attempt: int) -> float:
    """Seconds to wait: the endpoint's number if it sent one, else backoff."""
    headers = getattr(err, "headers", None)
    for field, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(field) if headers is not None else None
        if raw:
            try:
                return min(float(raw) * scale, RETRY_CAP)
            except ValueError:
                pass
    return min(0.5 * (2**attempt), 8.0)


def _wait_for_retry(
    delay: float,
    reason: str,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Cancelable backoff shared by HTTP and in-stream retries."""
    import time

    waited = 0.0
    while waited < delay:
        if should_stop is not None and should_stop():
            raise RuntimeError(f"stopped while retrying after {reason}")
        step = min(0.25, delay - waited)
        time.sleep(step)
        waited += step


def _open_with_retry(
    req: Any,
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Any:
    """urlopen, retried on the failures that are worth retrying."""
    import time

    for attempt in range(RETRIES):
        try:
            return urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS or attempt == RETRIES - 1:
                raise
            delay = _retry_after(exc, attempt)
            reason = f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            if attempt == RETRIES - 1:
                raise
            delay = min(0.5 * (2**attempt), 8.0)
            reason = str(getattr(exc, "reason", exc))
        if on_event is not None:
            on_event({"kind": "retry", "attempt": attempt + 1, "delay": delay, "reason": reason})
        # Sleep in slices so Ctrl+C still lands during a long backoff.
        _wait_for_retry(delay, reason, should_stop=should_stop)
    raise RuntimeError("unreachable")


def complete(
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    thinking: str | None = "low",
    on_event: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    from desmos.openai import is_openai

    if is_openai(model):
        from desmos import openai as openai_provider

        # payload_for has accepted both of these from the start and nothing
        # ever passed them, so server-side folding was unreachable on OpenAI
        # and prompt_cache_key never went out. A long session grew until the
        # endpoint refused it.
        return openai_provider.complete(
            model,
            system,
            messages,
            max_tokens,
            thinking=thinking,
            on_event=on_event,
            should_stop=should_stop,
            compact_threshold=OPENAI_COMPACT_TRIGGER,
            cache_key=session_cache_key(),
        )
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    payload = cached_payload(model, system, messages, max_tokens, thinking=thinking)
    betas = payload.pop("_betas", [])
    payload["stream"] = True
    log_payload(payload, betas)
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    # The payload this response actually came from; it does not change between
    # stream attempts. The kernel used to re-read the LAST global after
    # complete() returned, which a subagent POST from the thread pool could
    # overwrite in between, putting another agent's request on this wire card.
    sent = redact_wire(payload)
    for stream_attempt in range(STREAM_RETRIES):
        try:
            with _open_with_retry(req, on_event=on_event, should_stop=should_stop) as resp:
                out = read_sse(
                    iter_sse_lines(resp),
                    on_event=on_event,
                    should_stop=should_stop,
                )
                out["_request"] = sent
                return out
        except AnthropicStreamError as exc:
            final = stream_attempt == STREAM_RETRIES - 1
            if not exc.retryable or exc.had_output or final:
                if exc.had_output and exc.retryable:
                    raise RuntimeError(
                        f"{exc}; not retried because partial output was already emitted"
                    ) from exc
                if final:
                    # Say that it was retried. "Overloaded" on its own reads as
                    # a harness that never tried, and that is the wrong thing to
                    # go debug.
                    raise RuntimeError(
                        f"{exc}; gave up after {STREAM_RETRIES} attempts over ~{int(_stream_budget())}s"
                    ) from exc
                raise
            delay = min(0.5 * (2**stream_attempt), STREAM_RETRY_CAP)
            reason = f"Anthropic SSE {exc.error_type}"
            if on_event is not None:
                on_event(
                    {
                        "kind": "retry",
                        "attempt": stream_attempt + 1,
                        "delay": delay,
                        "reason": reason,
                    }
                )
            _wait_for_retry(delay, reason, should_stop=should_stop)
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"Anthropic HTTP {e.code}: {body[:2000]}") from e
    raise RuntimeError("Anthropic stream retry loop exhausted")


TRAJECTORY_DIR = os.environ.get("DESMOS_TRAJECTORY", ".desmos/trajectory")
LAST: dict[str, Any] = {}
_TRAJ_LOCK = __import__("threading").Lock()


def log_payload(payload: dict[str, Any], betas: list[str]) -> str:
    """Persist the exact outgoing POST body so a reload can be verified."""
    import hashlib
    import tempfile
    import threading
    import time

    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "betas": betas,
        "payload": payload,
    }
    sysblocks = payload.get("system") or []
    record["system_digest"] = [
        {
            "chars": len(b.get("text") or ""),
            "sha1": hashlib.sha1((b.get("text") or "").encode()).hexdigest()[:12],
            "cached": "cache_control" in b,
            "head": (b.get("text") or "")[:60],
        }
        for b in sysblocks
    ]
    record["n_messages"] = len(payload.get("messages") or [])
    record["n_chars"] = sum(len(json.dumps(m)) for m in payload.get("messages") or [])
    with _TRAJ_LOCK:
        LAST.clear()
        LAST.update(record)
    try:
        os.makedirs(TRAJECTORY_DIR, exist_ok=True)
        name = f"{time.time_ns()}-{os.getpid()}-{threading.get_ident()}.json"
        dest = os.path.join(TRAJECTORY_DIR, name)
        fd, tmp = tempfile.mkstemp(prefix="traj-", suffix=".tmp", dir=TRAJECTORY_DIR)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(record, fh, indent=2)
            os.replace(tmp, dest)
            prune_trajectory()
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return dest
    except OSError:
        return ""


FULL_KEEP = int(os.environ.get("DESMOS_TRAJ_FULL", "12"))
MAX_FILES = int(os.environ.get("DESMOS_TRAJ_MAX", "400"))


def prune_trajectory(full_keep: int = FULL_KEEP, max_files: int = MAX_FILES) -> dict[str, int]:
    """Digests are cheap and forever; whole payloads are not.

    Every record embeds the full transcript, so the directory grows
    quadratically. Strip the payload out of anything older than the newest
    `full_keep` records, and delete files past `max_files` entirely.
    """
    stripped = deleted = 0
    try:
        files = sorted(f for f in os.listdir(TRAJECTORY_DIR) if f.endswith(".json"))
    except OSError:
        return {"stripped": 0, "deleted": 0}
    for name in files[:-max_files] if len(files) > max_files else []:
        try:
            os.unlink(os.path.join(TRAJECTORY_DIR, name))
            deleted += 1
        except OSError:
            pass
    files = files[-max_files:]
    for name in files[:-full_keep] if len(files) > full_keep else []:
        path = os.path.join(TRAJECTORY_DIR, name)
        try:
            with open(path) as fh:
                rec = json.load(fh)
            if "payload" not in rec:
                continue
            rec.pop("payload", None)
            rec["payload_stripped"] = True
            with open(path, "w") as fh:
                json.dump(rec, fh, indent=2)
            stripped += 1
        except (OSError, ValueError):
            continue
    return {"stripped": stripped, "deleted": deleted}


def trajectory(n: int = 1) -> list[dict[str, Any]]:
    """Digests of the last n logged payloads, newest last."""
    if not os.path.isdir(TRAJECTORY_DIR):
        return []
    files = sorted(f for f in os.listdir(TRAJECTORY_DIR) if f.endswith(".json"))
    out = []
    for f in files[-n:]:
        try:
            with open(os.path.join(TRAJECTORY_DIR, f)) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        out.append(
            {
                "file": f,
                "ts": rec.get("ts"),
                "n_messages": rec.get("n_messages"),
                "system": rec.get("system_digest"),
            }
        )
    return out


def payload_diff() -> dict[str, Any]:
    """Did the system prompt actually change between the last two calls?"""
    recs = trajectory(2)
    if len(recs) < 2:
        return {"changed": None, "reason": "need 2 logged calls"}
    before = [b["sha1"] for b in recs[0]["system"]]
    after = [b["sha1"] for b in recs[1]["system"]]
    return {
        "changed": before != after,
        "before": before,
        "after": after,
        "chars_before": [x["chars"] for x in recs[0]["system"]],
        "chars_after": [x["chars"] for x in recs[1]["system"]],
    }


def text_of(resp: dict[str, Any]) -> str:
    """Join the text blocks of a reply into the speech the harness scans.

    Anthropic streams one text block per message, so concatenating was always
    a no-op. A Responses model does not: sol emits a `phase: "commentary"`
    preamble and a `phase: "final_answer"` as two separate message items, and
    gluing them edge to edge produced "…checking the workspace first.Hey! What
    would you like to work on?" -- two sentences fused mid-line. They are
    separate utterances and are joined as such.
    """
    parts = [
        (block.get("text") or "")
        for block in resp.get("content") or []
        if block.get("type") == "text" and (block.get("text") or "").strip()
    ]
    return "\n\n".join(parts)
