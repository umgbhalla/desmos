"""OpenAI provider: Responses API, streamed, with reasoning kept verbatim.

Two ways in, decided by the credential:

  api key   POST https://api.openai.com/v1/responses
  chatgpt   POST https://chatgpt.com/backend-api/codex/responses  (Codex OAuth)

The harness speaks Anthropic-shaped messages internally, so everything here is
translation. The one rule that matters: a reasoning item is opaque. Its summary
is for the reader, its encrypted_content is for the model, and the item has to
go back verbatim on the next request or the model loses the thought it just
had. Every assistant block we emit therefore carries the raw item under
"openai", and to_input replays that instead of rebuilding it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Iterable

from desmos import auth
from desmos.complete import COMPACT_BLOCK, iter_sse_lines, log_payload

# The ABI was written for a model that emits XML in prose. A Responses model
# defaults to assuming it has real function tools, so without this it narrates
# a shell command it never ran and then reports the output it imagined. Blunt
# on purpose: measured, the ABI alone produced a hallucinated result, and this
# paragraph produced the tag.
CONTRACT = """

# how you act here

You have no built-in tools. The only way to make anything happen is to write
the XML syscall in your reply, exactly as documented above, and then stop
generating. The harness runs it and sends the output back as the next message.

Never describe a command as done, and never state its output, unless that
output arrived in a result you were given. If you have not emitted the tag
yet, you have not run anything.
"""

API_URL = "https://api.openai.com/v1/responses"
CHATGPT_URL = "https://chatgpt.com/backend-api/codex/responses"
COMPACT_URL = "https://api.openai.com/v1/responses/compact"
ORIGINATOR = os.environ.get("DESMOS_OPENAI_ORIGINATOR") or "codex_cli_rs"

# What the model picker offers. Order is the order the menu shows.
MODELS = ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra")
EFFORTS = ("low", "high", "xhigh")


def is_openai(model: str) -> bool:
    name = (model or "").lower()
    return name.startswith(("gpt-", "o3", "o4", "codex-"))


def effort_of(thinking: str | None) -> str:
    """Map the harness's thinking dial onto reasoning.effort."""
    raw = (thinking or "low").strip().lower()
    if raw in {"off", "none", "0", "false", "minimal"}:
        return "none"
    if raw in {"max", "xhigh"}:
        return "xhigh"
    if raw in {"low", "medium", "high"}:
        return raw
    return "low"


# ------------------------------------------------------------------- messages


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for raw in content or []:
        if isinstance(raw, dict) and raw.get("type") == "text":
            parts.append(raw.get("text") or "")
        elif isinstance(raw, str):
            parts.append(raw)
    return "".join(parts)


def to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-shaped transcript -> Responses input array.

    Assistant blocks that came from this provider are replayed as their own raw
    item. Blocks from another provider (a transcript that switched models mid
    session) survive as plain text, which is lossy but never fatal.
    """
    items: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            text = _text_of(content)
            if text:
                items.append(
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
                )
            continue
        if role != "assistant":
            continue
        spoken: list[str] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            raw = block.get("openai")
            if isinstance(raw, dict):
                items.append(dict(raw))
                continue
            kind = block.get("type")
            if kind == "text" and (block.get("text") or "").strip():
                spoken.append(block["text"])
            elif kind == "thinking" and not block.get("signature"):
                # foreign thinking, unusable as reasoning: keep it as speech
                if (block.get("thinking") or "").strip():
                    spoken.append(block["thinking"])
        if spoken:
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "\n".join(spoken)}],
                }
            )
    return items


def payload_for(
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    thinking: str | None = "low",
    compact_threshold: int | None = None,
    cache_key: str | None = None,
) -> dict[str, Any]:
    effort = effort_of(thinking)
    body: dict[str, Any] = {
        "model": model,
        # The system prompt is instructions, not the first input item. Put it in
        # input and it becomes a turn the model can compact away.
        "instructions": system + CONTRACT,
        "input": to_input(messages),
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "max_output_tokens": max_tokens,
        "text": {"verbosity": "medium"},
    }
    if effort == "none":
        body["reasoning"] = {"effort": "none"}
    else:
        body["reasoning"] = {"effort": effort, "summary": "auto"}
    if cache_key:
        body["prompt_cache_key"] = cache_key
    if compact_threshold:
        # Server-side fold, same bargain as Anthropic's: the returned item is
        # opaque and must be replayed, and it is the cut point for everything
        # before it.
        body["context_management"] = [{"type": "compaction", "compact_threshold": compact_threshold}]
    return body


# -------------------------------------------------------------------- streaming


def _blocks_from_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in items:
        kind = item.get("type")
        if kind == "reasoning":
            summary = "".join(
                part.get("text") or "" for part in item.get("summary") or [] if isinstance(part, dict)
            )
            blocks.append(
                {
                    # signature is what the replay path uses to tell a real
                    # thought from a downgraded one; the item id is ours.
                    "type": "thinking",
                    "thinking": summary,
                    "signature": item.get("id") or "openai",
                    "openai": item,
                }
            )
        elif kind == "message":
            text = "".join(
                part.get("text") or ""
                for part in item.get("content") or []
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
            )
            blocks.append({"type": "text", "text": text, "openai": item})
        elif kind == "compaction":
            blocks.append({"type": COMPACT_BLOCK, "summary": item.get("summary") or "", "openai": item})
        else:
            blocks.append({"type": "text", "text": "", "openai": item})
    return blocks or [{"type": "text", "text": ""}]


def _usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Map Responses usage onto the field names the context meter reads."""
    if not isinstance(raw, dict):
        return {}
    cached = int((raw.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    total_in = int(raw.get("input_tokens") or 0)
    out = {
        "input_tokens": max(0, total_in - cached),
        "output_tokens": int(raw.get("output_tokens") or 0),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
    }
    reasoning = int((raw.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)
    if reasoning:
        out["reasoning_tokens"] = reasoning
    return out


def apply_stream_event(
    state: dict[str, Any],
    ev: dict[str, Any],
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Fold one Responses SSE event into the assembled message."""
    kind = ev.get("type") or ""
    if kind in {"error", "response.failed"}:
        err = ev.get("error") or (ev.get("response") or {}).get("error") or ev
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(f"OpenAI stream error: {msg}")
    if kind == "response.reasoning_summary_text.delta":
        chunk = ev.get("delta") or ""
        if chunk and on_event is not None:
            on_event({"kind": "thinking_delta", "text": chunk})
        return
    if kind == "response.reasoning_summary_part.added" and state.get("saw_summary"):
        # a second summary paragraph: keep the reader's blank line
        if on_event is not None:
            on_event({"kind": "thinking_delta", "text": "\n\n"})
        return
    if kind == "response.output_text.delta":
        chunk = ev.get("delta") or ""
        if chunk and on_event is not None:
            on_event({"kind": "text_delta", "text": chunk})
        return
    if kind == "response.output_item.added":
        item = ev.get("item") or {}
        if item.get("type") == "reasoning":
            state["saw_summary"] = True
        return
    if kind == "response.output_item.done":
        item = ev.get("item")
        if isinstance(item, dict):
            state.setdefault("items", []).append(item)
        return
    if kind in {"response.completed", "response.incomplete"}:
        resp = ev.get("response") or {}
        state["usage"] = _usage(resp.get("usage") or {})
        state["status"] = resp.get("status") or ""
        if resp.get("id"):
            state["id"] = resp["id"]
        # output is authoritative: prefer it over the items we accumulated
        out = resp.get("output")
        if isinstance(out, list) and out:
            state["items"] = [i for i in out if isinstance(i, dict)]
        return


def assemble(state: dict[str, Any], model: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "model": model,
        "content": _blocks_from_items(list(state.get("items") or [])),
        "usage": state.get("usage") or {},
        "stop_reason": "max_tokens" if state.get("status") == "incomplete" else "end_turn",
        "id": state.get("id") or "",
    }


def read_sse(
    lines: Iterable[Any],
    model: str,
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {"items": []}
    parts: list[str] = []

    def flush() -> None:
        nonlocal parts
        if not parts:
            return
        raw = "\n".join(parts)
        parts = []
        if raw.strip() in {"", "[DONE]"}:
            return
        ev = json.loads(raw)
        if isinstance(ev, dict):
            apply_stream_event(state, ev, on_event)

    for line in lines:
        if should_stop is not None and should_stop():
            break
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = line.rstrip("\n").rstrip("\r")
        if not line:
            flush()
            continue
        if line.startswith(":") or line.startswith("event:"):
            continue
        if line.startswith("data:"):
            parts.append(line[5:].lstrip())
    flush()
    return assemble(state, model)


# ------------------------------------------------------------------ transport


def headers_for(cred: auth.Credential) -> tuple[str, dict[str, str]]:
    """URL and headers. The ChatGPT backend needs the account id and an originator."""
    base = {
        "Authorization": f"Bearer {cred.token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if cred.kind == "oauth":
        base["chatgpt-account-id"] = cred.account_id or ""
        base["originator"] = ORIGINATOR
        base["OpenAI-Beta"] = "responses=experimental"
        base["session_id"] = str(uuid.uuid4())
        return CHATGPT_URL, base
    return API_URL, base


def unsupported_field(detail: str) -> str | None:
    """The parameter name in an 'Unsupported parameter: x' style 400, if any."""
    import re

    for pat in (
        r"[Uu]nsupported parameter:?\s*'?([A-Za-z0-9_.]+)",
        r"[Uu]nknown parameter:?\s*'?([A-Za-z0-9_.]+)",
        r"[Uu]nrecognized (?:request )?argument:?\s*'?([A-Za-z0-9_.]+)",
    ):
        m = re.search(pat, detail)
        if m:
            return m.group(1).split(".")[0]
    return None


def complete(
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    thinking: str | None = "low",
    on_event: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    compact_threshold: int | None = None,
    cache_key: str | None = None,
) -> dict[str, Any]:
    cred = auth.credential("openai")
    url, headers = headers_for(cred)
    body = payload_for(
        model,
        system,
        messages,
        max_tokens,
        thinking=thinking,
        compact_threshold=compact_threshold,
        cache_key=cache_key,
    )
    log_payload(body, [])
    dropped: list[str] = []
    for _ in range(6):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return read_sse(
                    iter_sse_lines(resp), model, on_event=on_event, should_stop=should_stop
                )
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            # The two endpoints do not accept the same body -- the Codex backend
            # rejects max_output_tokens, for one -- and the accepted set moves.
            # Drop exactly the field it names and try again; a session that
            # keeps working beats a correct-looking request that 400s.
            field = unsupported_field(detail)
            if e.code == 400 and field and field in body:
                body.pop(field, None)
                dropped.append(field)
                log_payload(body, [])
                continue
            note = f" (dropped {', '.join(dropped)})" if dropped else ""
            raise RuntimeError(f"OpenAI HTTP {e.code}{note}: {detail[:2000]}") from e
    raise RuntimeError(f"OpenAI kept rejecting fields: dropped {', '.join(dropped)}")


def compact_window(model: str, items: list[dict[str, Any]], *, instructions: str = "") -> list[dict[str, Any]]:
    """Standalone fold: hand over a window, get the canonical next one back.

    The result is passed through untouched. Pruning it is how a fold gets
    silently undone.
    """
    cred = auth.credential("openai")
    if cred.kind == "oauth":
        raise RuntimeError("the standalone compact endpoint needs an OPENAI_API_KEY")
    body: dict[str, Any] = {"model": model, "input": items}
    if instructions:
        body["instructions"] = instructions
    req = urllib.request.Request(
        COMPACT_URL,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {cred.token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            out = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenAI compact HTTP {e.code}: {e.read().decode()[:2000]}") from e
    got = out.get("output") if isinstance(out, dict) else None
    return got if isinstance(got, list) else []
