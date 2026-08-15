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
import re
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Iterable

from desmos import auth
from desmos.complete import COMPACT_BLOCK, _open_with_retry, iter_sse_lines, log_payload, redact_wire

# The ABI was written for a model that emits XML in prose. A Responses model
# defaults to assuming it has real function tools, so without this it narrates
# a shell command it never ran and then reports the output it imagined. Blunt
# on purpose: measured, the ABI alone produced a hallucinated result, and this
# paragraph produced the tag.
CONTRACT = """

# how you act here

You have one tool, `syscall`. The only way to make anything happen is to call
it with the raw XML syscalls documented above. Put one or more complete XML
tags in its input and no prose. Do not write XML in an assistant message. The
harness runs every tag in order and returns their result blocks as that tool's
output.

The `syscall` call ends your response. Generate no assistant message after it.
Do not print a stop word or attempt to write a result. Wait for the typed tool
output, then continue on the next turn.

Never describe a command as done, and never state its output, unless that
output arrived in a result you were given. If you have not passed the tag to
`syscall` yet, you have not run anything.

This is not a chat interface with a tool API that might be switched off. The
XML tags are the interface, they are always available, and nothing in a turn
can disable them. So do not say a dispatcher is unavailable, that tools cannot
be invoked here, that XML cannot be passed to `syscall`, or that an
operation is impossible from inside a reply. If you can put the tag in
`syscall`, you can run it, and calling `syscall` is the whole mechanism.

Announcing an action and then declining it in the same reply is the failure to
avoid. If you write that you are about to read a file, trace a path, or switch
a model, the `syscall` call belongs in that same response. Either call it or do
not announce the action.

A response with no `syscall` call ends the step -- the harness reads it as "the
work is finished". So a message that says you are unable to proceed does not
pause anything; it stops the task and hands back control. If you are genuinely
blocked, say what is missing in one line, having first used a tag to find out.
"""

API_URL = "https://api.openai.com/v1/responses"
CHATGPT_URL = "https://chatgpt.com/backend-api/codex/responses"
COMPACT_URL = "https://api.openai.com/v1/responses/compact"
ORIGINATOR = os.environ.get("DESMOS_OPENAI_ORIGINATOR") or "codex_cli_rs"

# What the model picker offers. Order is the order the menu shows.
MODELS = ("gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra")
# The 5.6 ladder is none/low/medium/high/xhigh/max. Offering three of six meant
# `medium` -- the rung OpenAI calls the everyday balance -- was unreachable, and
# `max` collapsed onto xhigh below, so the top of the ladder could not be asked
# for at all.
EFFORTS = ("low", "medium", "high", "xhigh", "max")


# Aliases are here because DESMOS_MODEL takes any string and people write
# "sol", not "gpt-5.6-sol". This is the one predicate: dialect.family and
# settings.provider_of both call it. They used to answer separately, and
# DESMOS_MODEL=sol put OpenAI dialect prose on a body sent to
# api.anthropic.com while is_openai routed it to Anthropic. Prefixes for the
# o-series, because a two-character substring is the wrong thing to route a
# whole prompt dialect on.
OPENAI_PREFIXES = ("gpt-", "o3", "o4", "codex-")
OPENAI_ALIASES = frozenset({"gpt", "sol", "terra", "luna", "daybreak", "codex"})


def is_openai(model: str) -> bool:
    name = (model or "").lower()
    if name.startswith(OPENAI_PREFIXES):
        return True
    # Whole words, not substrings: dialect.py matched "sol" anywhere in the id,
    # which routes anything containing "console" or "resolve" to OpenAI.
    return bool(OPENAI_ALIASES.intersection(re.split(r"[^a-z0-9]+", name)))


def effort_of(thinking: str | None) -> str:
    """Map the harness's thinking dial onto reasoning.effort."""
    raw = (thinking or "low").strip().lower()
    if raw in {"off", "none", "0", "false", "minimal"}:
        return "none"
    if raw in {"low", "medium", "high", "xhigh", "max"}:
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


def _user_content(content: Any) -> list[dict[str, Any]]:
    """Anthropic user blocks -> Responses input parts, images included.

    An image arrives here in Anthropic's shape -- a source dict with a media
    type and base64 -- because that is what vision.attach writes into the
    transcript. Responses wants one flat `input_image` whose `image_url` is a
    data URL, which is also the only shape the Codex backend accepts (it has
    no `input_file` and no uploaded ids). Collapsing the whole message to text,
    which is what this used to do, silently dropped every screenshot the
    moment the session was on an OpenAI model.
    """
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}] if content.strip() else []
    parts: list[dict[str, Any]] = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, str):
            if block.strip():
                parts.append({"type": "input_text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "image":
            src = block.get("source") or {}
            if src.get("type") == "base64" and src.get("data"):
                media = src.get("media_type") or "image/png"
                parts.append(
                    {"type": "input_image", "image_url": f"data:{media};base64,{src['data']}"}
                )
            elif src.get("url"):
                parts.append({"type": "input_image", "image_url": src["url"]})
            continue
        if kind == "text" and isinstance(block.get("text"), str) and block["text"].strip():
            parts.append({"type": "input_text", "text": block["text"]})
    return parts


def to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-shaped transcript -> Responses input array.

    Assistant blocks that came from this provider are replayed as their own raw
    item. Blocks from another provider (a transcript that switched models mid
    session) survive as plain text, which is lossy but never fatal.
    """
    items: list[dict[str, Any]] = []
    # A custom_tool_call_output whose call was trimmed off the head of the
    # transcript is a fatal 400 ("No tool call found for custom tool call
    # output"), and it poisons every later request. Emit an output only when
    # its call is present in this same input array; otherwise degrade it to
    # ordinary user text so the result content survives.
    seen_calls: set[str] = set()
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, list):
                orphaned: list[dict[str, Any]] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "custom_tool_call_output":
                        call_id = block.get("call_id") or ""
                        if call_id and call_id in seen_calls:
                            items.append(
                                {
                                    "type": "custom_tool_call_output",
                                    "call_id": call_id,
                                    "output": block.get("output") or "",
                                }
                            )
                        else:
                            text = block.get("output") or ""
                            if isinstance(text, str) and text.strip():
                                orphaned.append({"type": "text", "text": text})
                content = orphaned + [
                    block
                    for block in content
                    if not isinstance(block, dict)
                    or block.get("type") != "custom_tool_call_output"
                ]
            parts = _user_content(content)
            if parts:
                items.append({"type": "message", "role": "user", "content": parts})
            continue
        if role != "assistant":
            continue
        spoken: list[str] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            raw = block.get("openai")
            if isinstance(raw, dict):
                if raw.get("type") == "custom_tool_call" and raw.get("call_id"):
                    seen_calls.add(str(raw["call_id"]))
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
        # No stop-sequence equivalent here: the Responses body has no `stop` and
        # no `stop_sequences` (`stop` was a Chat Completions field and did not
        # carry over), so the Anthropic guard in complete.cached_payload has no
        # analogue. It is fenced differently instead -- a syscall is a typed
        # custom_tool_call, so the tag is not prose the model can keep writing
        # past into a result it wrote itself, and CONTRACT says so in words for
        # the case where it tries.
        "tools": [
            {
                "type": "custom",
                "name": "syscall",
                "description": (
                    "Execute one or more Desmos XML syscalls. Input must contain only "
                    "complete XML tags from the system prompt. Tags run in order and "
                    "return result blocks."
                ),
            }
        ],
        "parallel_tool_calls": False,
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
                (part.get("text") if part.get("type") in {"output_text", "text"} else part.get("refusal"))
                or ""
                for part in item.get("content") or []
                if isinstance(part, dict)
                and part.get("type") in {"output_text", "text", "refusal"}
            )
            # sol splits a turn into a `commentary` preamble and a
            # `final_answer`. Carrying the phase through means the harness can
            # tell narration from answer instead of seeing one fused blob.
            block = {"type": "text", "text": text, "openai": item}
            if item.get("phase"):
                block["phase"] = item["phase"]
            blocks.append(block)
        elif kind == "custom_tool_call" and item.get("name") == "syscall":
            blocks.append(
                {
                    "type": "custom_tool_call",
                    "name": "syscall",
                    "call_id": item.get("call_id") or "",
                    "input": item.get("input") or "",
                    "openai": item,
                }
            )
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
    if kind == "response.reasoning_text.delta":
        # Not the same event as the summary. Models that return reasoning text
        # verbatim (rather than a summarised paragraph) emit only this one, and
        # ignoring it left the thinking pane empty while tokens were billed.
        chunk = ev.get("delta") or ""
        if chunk and on_event is not None:
            on_event({"kind": "thinking_delta", "text": chunk})
        return
    if kind == "response.output_text.delta":
        chunk = ev.get("delta") or ""
        if chunk and on_event is not None:
            on_event({"kind": "text_delta", "text": chunk})
        return
    if kind == "response.refusal.delta":
        # A refusal is the answer, not an error. Dropped, the turn looked like
        # an empty reply and the loop treated it as "the model is done".
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
    saw_end = False

    def flush() -> None:
        nonlocal parts, saw_end
        if not parts:
            return
        raw = "\n".join(parts)
        parts = []
        if raw.strip() in {"", "[DONE]"}:
            return
        ev = json.loads(raw)
        if isinstance(ev, dict):
            if str(ev.get("type") or "").startswith(
                ("response.completed", "response.incomplete", "response.failed")
            ):
                saw_end = True
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
    # The Anthropic path learned this tonight and the OpenAI path never did: a
    # stream that simply runs out is not a finished answer. Returned as one,
    # its half-written syscall has no closing tag, scan() drops it, and the
    # truncated reply is committed as the step's result.
    if not saw_end and not (should_stop is not None and should_stop()):
        raise RuntimeError("OpenAI stream ended before response.completed")
    return assemble(state, model)


# ------------------------------------------------------------------ transport


_SESSION_ID = ""


def session_id() -> str:
    """One id for the life of the process -- a conversation, not a request.

    The ChatGPT backend routes on this header, and the prompt cache lives
    behind that routing. Measured: with a fresh uuid per request an identical
    3417-token prefix hit the cache about half the time; pinned, it was one
    cold miss followed by 2816 cached tokens on every call after it.
    """
    global _SESSION_ID
    if not _SESSION_ID:
        _SESSION_ID = os.environ.get("DESMOS_SESSION_ID") or str(uuid.uuid4())
    return _SESSION_ID


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
        base["session_id"] = session_id()
        return CHATGPT_URL, base
    return API_URL, base


def unsupported_field(detail: str) -> str | None:
    """The parameter name in an 'Unsupported parameter: x' style 400, if any."""
    for pat in (
        r"[Uu]nsupported parameter:?\s*'?([A-Za-z0-9_.]+)",
        r"[Uu]nknown parameter:?\s*'?([A-Za-z0-9_.]+)",
        r"[Uu]nrecognized (?:request )?argument:?\s*'?([A-Za-z0-9_.]+)",
    ):
        m = re.search(pat, detail)
        if m:
            # The character class includes '.', so an unquoted name at the end
            # of a sentence captures the full stop too. Strip it, but keep the
            # interior dots: the path is the thing being dropped.
            return m.group(1).strip(".")
    return None


def _drop_field(body: dict[str, Any], path: str) -> bool:
    """Remove what the 400 named, most precisely first. True if anything went.

    'reasoning.summary' names the summary, not the reasoning object. Popping
    the parent turned a complaint about the summary into thinking switched off
    for the rest of the session, while the meta pane went on reporting the
    configured effort.

    But not every name resolves to a dict leaf, and a name that does not must
    not be fatal. 'reasoning.encrypted_content' is an entry in the `include`
    list, so it lives under no `reasoning` key at all -- and dropping
    `reasoning` would leave the entry in place for the next attempt to 400 on
    again. Deeper or indexed names ('reasoning.summary.kind',
    'context_management.0.type') resolve to nothing either. Those fall back to
    the coarse top-level pop, which is what kept the session alive before.
    """
    parts = path.split(".")
    node: Any = body
    for p in parts[:-1]:
        if not isinstance(node, dict) or p not in node:
            node = None
            break
        node = node[p]
    if isinstance(node, dict) and parts[-1] in node:
        node.pop(parts[-1])
        return True
    for key, val in body.items():
        if isinstance(val, list) and path in val:
            body[key] = [v for v in val if v != path]
            return True
    if parts[0] in body:
        body.pop(parts[0])
        return True
    return False


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
    # Measured against the live Codex backend, A/B/A on one warm prefix: with
    # prompt_cache_key the response reports cached_tokens 0, without it 2816
    # of 3254. The key is a routing hint on api.openai.com and a cache miss
    # here, so it only goes out on the API-key endpoint. Keyed off the url that
    # headers_for already picked, not off cred.kind: the old test was
    # `kind != "api_key"`, a kind auth.py has never produced, so the key was
    # suppressed on both endpoints and the measurement above was moot.
    if url != API_URL:
        cache_key = None
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
        # The body the drop loop below may still edit, frozen per attempt. The
        # kernel used to re-read the complete.LAST global after this returned,
        # which a subagent POST from the thread pool could overwrite in between,
        # putting another agent's request on this call's wire card.
        sent = redact_wire(body)
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            # Same retry the Anthropic path uses. This loop only ever retried a
            # 400 naming an unsupported field, so a 429 -- routine on a plan --
            # or any 5xx raised straight out and killed the whole step.
            with _open_with_retry(req, on_event=on_event, should_stop=should_stop) as resp:
                out = read_sse(
                    iter_sse_lines(resp), model, on_event=on_event, should_stop=should_stop
                )
                out["_request"] = sent
                return out
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            # The two endpoints do not accept the same body -- the Codex backend
            # rejects max_output_tokens, for one -- and the accepted set moves.
            # Drop exactly the field it names and try again; a session that
            # keeps working beats a correct-looking request that 400s.
            # `tools` is a prefix test so a complaint about "tools.0.name"
            # cannot amputate the syscall tool and leave the model unable to act.
            field = unsupported_field(detail)
            if e.code == 400 and field and not field.startswith("tools") and _drop_field(body, field):
                dropped.append(field)
                log_payload(body, [])
                # Dropping a field is a silent downgrade otherwise: a 400 naming
                # reasoning.summary used to leave the session with no thinking
                # while the meta pane still reported the configured effort.
                if on_event is not None:
                    on_event(
                        {
                            "kind": "retry",
                            "attempt": len(dropped),
                            "delay": 0.0,
                            "reason": f"OpenAI 400: dropped {field}",
                        }
                    )
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
