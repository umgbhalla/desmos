from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

INTERLEAVED_BETA = "interleaved-thinking-2025-05-14"
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
        effort = "max" if mode in {"xhigh", "max"} else mode
        if effort == "minimal":
            effort = "low"
        payload["thinking"] = {"type": "adaptive", "display": "summarized"}
        payload["output_config"] = {"effort": effort}
        return []
    budget = BUDGETS.get(mode, 2048)
    if int(payload.get("max_tokens") or 0) <= budget:
        payload["max_tokens"] = budget + 1024
    payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    return [INTERLEAVED_BETA]


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
        if kind == "thinking":
            text = raw.get("thinking") or ""
            signature = raw.get("signature") or ""
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
        elif kind == "text":
            text = raw.get("text") or ""
            if text:
                blocks.append({"type": "text", "text": text})
    return blocks


def assistant_content(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep thinking / redacted_thinking / text. Drop everything else."""
    blocks: list[dict[str, Any]] = []
    for raw in resp.get("content") or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "thinking":
            item = {"type": "thinking", "thinking": raw.get("thinking") or ""}
            if raw.get("signature"):
                item["signature"] = raw["signature"]
            blocks.append(item)
        elif kind == "redacted_thinking":
            blocks.append({"type": "redacted_thinking", "data": raw.get("data") or ""})
        elif kind == "text":
            text = raw.get("text") or ""
            if text:
                blocks.append({"type": "text", "text": text})
    return blocks or [{"type": "text", "text": ""}]


def thinking_text(blocks: list[dict[str, Any]]) -> str:
    parts = []
    for block in blocks:
        if block.get("type") == "thinking" and (block.get("thinking") or "").strip():
            parts.append(block["thinking"])
        elif block.get("type") == "redacted_thinking":
            parts.append("[redacted thinking]")
    return "\n".join(parts)


def cached_payload(
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    thinking: str | None = "low",
) -> dict[str, Any]:
    """Pi/Anthropic: cache ABI, cache catalog, cache last *user* only. Replay thinking."""
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
            blocks = [{"type": "text", "text": m["content"]}] if m["content"] else []
        elif isinstance(m.get("content"), list):
            blocks = []
            for raw in m["content"]:
                if isinstance(raw, dict):
                    block = {k: v for k, v in raw.items() if k != "cache_control"}
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
    }
    payload["_betas"] = apply_thinking(payload, model, thinking)
    return payload


def complete(
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    *,
    thinking: str | None = "low",
) -> dict[str, Any]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    payload = cached_payload(model, system, messages, max_tokens, thinking=thinking)
    betas = payload.pop("_betas", [])
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
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"Anthropic HTTP {e.code}: {body[:2000]}") from e


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
    parts = []
    for block in resp.get("content") or []:
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)
