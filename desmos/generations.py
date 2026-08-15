from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from desmos.const import FROZEN, PRIOR_KEEP
from desmos.persist import atomic_write, load_grown, save, state_file
from desmos.types import World


def gen_dir(world: World) -> Path:
    return state_file(world).parent / "generations"


def grown_snapshot(world: World) -> dict[str, Any]:
    return {
        "generation": world.generation,
        "reason": world.gen_reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notes": world.notes,
        "tools": {
            name: {"doc": tool.doc, "source": tool.source}
            for name, tool in world.tools.items()
            if not tool.frozen
        },
        "docs": {name: tool.doc for name, tool in world.tools.items() if tool.frozen},
        "prior": world.prior[-PRIOR_KEEP:],
    }


def write_generation(world: World) -> Path | None:
    # A subagent runs in the parent's cwd with persist=False. Ungated, its
    # <evolve> wrote .desmos/generations/NNNN.json into the parent's repo --
    # save() refused, this did not.
    if not world.persist:
        return None
    path = gen_dir(world) / f"{world.generation:04d}.json"
    atomic_write(path, json.dumps(grown_snapshot(world), indent=2))
    return path


def apply_snapshot(world: World, data: dict[str, Any]) -> None:
    notes = data.get("notes")
    world.notes = {str(k): str(v) for k, v in notes.items() if isinstance(v, str)} if isinstance(notes, dict) else {}
    docs = data.get("docs")
    if isinstance(docs, dict):
        for name, doc in docs.items():
            if name in world.tools and isinstance(doc, str) and doc.strip():
                world.tools[name].doc = doc
    grown_names: set[str] = set()
    tools = data.get("tools")
    if isinstance(tools, dict):
        for name, spec in tools.items():
            if name in FROZEN or not isinstance(spec, dict):
                continue
            source = spec.get("source")
            doc = spec.get("doc") or f"user tag <{name}>"
            if not isinstance(source, str) or not isinstance(doc, str):
                continue
            world.tools[name] = load_grown(world, name, doc, source)
            grown_names.add(name)
    for name in list(world.tools):
        if not world.tools[name].frozen and name not in grown_names:
            del world.tools[name]
    world.prior = []
    raw_prior = data.get("prior")
    if isinstance(raw_prior, list):
        for item in raw_prior[-PRIOR_KEEP:]:
            if isinstance(item, dict) and isinstance(item.get("prompt"), str) and isinstance(item.get("speech"), str):
                world.prior.append({"prompt": item["prompt"], "speech": item["speech"]})


def evolve(world: World, reason: str = "") -> str:
    world.generation += 1
    world.gen_reason = reason or f"gen-{world.generation}"
    wrote = write_generation(world)
    save(world)
    line = f"generation {world.generation}: {world.gen_reason}"
    # Say it. A silent no-op here reads as a snapshot the model can roll back to.
    return line if wrote else line + " (not snapshotted: non-persistent world)"


def rollback(world: World, n: int) -> str:
    path = gen_dir(world) / f"{n:04d}.json"
    if not path.is_file():
        return f"rollback failed: no generation {n}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"rollback failed: {exc}"
    if not isinstance(data, dict):
        return "rollback failed: bad snapshot"
    apply_snapshot(world, data)
    # Rewinding the counter made the next <evolve> write n+1 over a snapshot
    # that already existed -- and what it overwrote was the state you had just
    # rolled back from, so <rollback n> afterwards replayed the abandoned work.
    # A generation number is a snapshot id; it only goes up.
    world.generation = max(world.generation, n)
    world.gen_reason = f"rolled back to {n}: {data.get('reason') or f'gen-{n}'}"
    save(world)
    return f"rolled back to generation {n}"


def ensure_gen1(world: World) -> None:
    path = gen_dir(world) / "0001.json"
    if not path.is_file():
        world.generation = 1
        world.gen_reason = world.gen_reason or "gen-1"
        write_generation(world)
