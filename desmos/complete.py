from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def split_system(system: str) -> tuple[str, str]:
    marker = "\n\n# tools"
    if marker in system:
        i = system.index(marker)
        return system[:i], system[i + 2 :]
    return system, ""


def cached_payload(model: str, system: str, messages: list[dict[str, Any]], max_tokens: int) -> dict[str, Any]:
    """Pi/Anthropic: cache ABI, cache catalog, cache last *user* only."""
    cache = {"type": "ephemeral"}
    abi, catalog_text = split_system(system)
    sys_blocks: list[dict[str, Any]] = [{"type": "text", "text": abi, "cache_control": cache}]
    if catalog_text.strip():
        sys_blocks.append({"type": "text", "text": catalog_text, "cache_control": cache})
    msgs: list[dict[str, Any]] = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        else:
            blocks = [dict(b) for b in content]
            for b in blocks:
                b.pop("cache_control", None)
        if not blocks:
            continue
        msgs.append({"role": m["role"], "content": blocks})
    for m in reversed(msgs):
        if m["role"] == "user" and m["content"]:
            m["content"][-1]["cache_control"] = dict(cache)
            break
    return {"model": model, "max_tokens": max_tokens, "system": sys_blocks, "messages": msgs}


def complete(model: str, system: str, messages: list[dict[str, Any]], max_tokens: int) -> dict[str, Any]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    payload = cached_payload(model, system, messages, max_tokens)
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"Anthropic HTTP {e.code}: {body[:2000]}") from e


def text_of(resp: dict[str, Any]) -> str:
    parts = []
    for block in resp.get("content") or []:
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)
