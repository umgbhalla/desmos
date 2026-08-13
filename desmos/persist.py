from __future__ import annotations

import json
from pathlib import Path

from desmos.const import FROZEN, PRIOR_KEEP
from desmos.exec import callable_from_source
from desmos.types import Tool, World


def state_file(world: World) -> Path:
    if world.state_path:
        return world.state_path
    return world.cwd / ".desmos" / "harness.json"


def save(world: World) -> None:
    path = state_file(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "notes": world.notes,
        "tools": {
            name: {"doc": tool.doc, "source": tool.source}
            for name, tool in world.tools.items()
            if not tool.frozen
        },
        "docs": {name: tool.doc for name, tool in world.tools.items() if tool.frozen},
        "prior": world.prior[-PRIOR_KEEP:],
        "generation": world.generation,
        "gen_reason": world.gen_reason,
        "thinking": world.thinking,
        "messages": world.messages[-80:],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(world: World) -> None:
    path = state_file(world)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    notes = data.get("notes")
    if isinstance(notes, dict):
        world.notes = {str(k): str(v) for k, v in notes.items() if isinstance(v, str)}
    docs = data.get("docs")
    if isinstance(docs, dict):
        for name, doc in docs.items():
            if name in world.tools and isinstance(doc, str) and doc.strip():
                world.tools[name].doc = doc
    tools = data.get("tools")
    if isinstance(tools, dict):
        for name, spec in tools.items():
            if name in FROZEN or not isinstance(spec, dict):
                continue
            source = spec.get("source")
            doc = spec.get("doc") or f"user tag <{name}>"
            if not isinstance(source, str) or not isinstance(doc, str):
                continue
            try:
                fn = callable_from_source(world, source, name)
            except Exception:
                continue
            world.tools[name] = Tool(name=name, doc=doc, source=source, handler=fn)
    raw_prior = data.get("prior")
    if isinstance(raw_prior, list):
        world.prior = []
        for item in raw_prior[-PRIOR_KEEP:]:
            if isinstance(item, dict) and isinstance(item.get("prompt"), str) and isinstance(item.get("speech"), str):
                world.prior.append({"prompt": item["prompt"], "speech": item["speech"]})
    if isinstance(data.get("generation"), int) and data["generation"] > 0:
        world.generation = data["generation"]
    if isinstance(data.get("gen_reason"), str) and data["gen_reason"]:
        world.gen_reason = data["gen_reason"]
    if isinstance(data.get("thinking"), str) and data["thinking"].strip():
        world.thinking = data["thinking"].strip()
    raw_msgs = data.get("messages")
    if isinstance(raw_msgs, list):
        world.messages = []
        for item in raw_msgs[-80:]:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            content = item.get("content")
            if isinstance(content, str) or isinstance(content, list):
                world.messages.append({"role": item["role"], "content": content})
