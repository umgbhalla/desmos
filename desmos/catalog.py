from __future__ import annotations

import inspect
from pathlib import Path

from desmos.const import ABI, HIDDEN_NS, PRIOR_KEEP
from desmos.persist import state_file
from desmos.scan import clip
from desmos.types import World


def package_root() -> Path:
    return Path(__file__).resolve().parent


def repo_root() -> Path:
    return package_root().parent


def skip_name(name: str) -> bool:
    return name in HIDDEN_NS or name.startswith("_")


def ns_names(world: World) -> list[str]:
    names = []
    for k, v in world.ns.items():
        if skip_name(k) or inspect.ismodule(v):
            continue
        names.append(k)
    return sorted(names)


def shape_of(value: object) -> str:
    if isinstance(value, str):
        return f"str, {len(value)} chars"
    if isinstance(value, (bytes, bytearray)):
        return f"{type(value).__name__}, {len(value)} bytes"
    if isinstance(value, (list, tuple, set, dict)):
        return f"{type(value).__name__}, len={len(value)}"
    shape = getattr(value, "shape", None)
    if shape is not None:
        return f"{type(value).__name__} shape={shape}"
    return type(value).__name__


def ns_index(world: World) -> str:
    names = ns_names(world)
    if not names:
        return "ns: (empty)"
    lines = ["ns:"]
    for name in names:
        lines.append(f"  {name}: {shape_of(world.ns.get(name))}")
    return "\n".join(lines)


def catalog(world: World) -> str:
    from desmos.const import FROZEN

    lines = ["# tools"]
    for name in (*sorted(t for t in world.tools if t in FROZEN), *sorted(t for t in world.tools if t not in FROZEN)):
        tool = world.tools[name]
        flag = " frozen" if tool.frozen else ""
        lines.append(f"<{name}>{flag} {tool.doc}")
    if world.notes:
        lines.append("# your notes")
        for key, note in world.notes.items():
            lines.append(f"[{key}]\n{note}")
    if world.skills:
        from desmos.skills import format_skills_for_prompt

        block = format_skills_for_prompt(world.skills)
        if block:
            lines.append(block)
    lines.append(runtime_block(world))
    return "\n".join(lines)


def runtime_block(world: World) -> str:
    """Live facts so the agent can reload and unstick — Pi puts cwd/docs in system."""
    cwd = str(world.cwd.resolve())
    root = repo_root()
    return "\n".join(
        [
            "# runtime",
            f"cwd: {cwd}",
            f"generation: {world.generation} ({world.gen_reason})",
            f"harness_state: {state_file(world)}",
            f"generations_dir: {state_file(world).parent / 'generations'}",
            f"project_skills: {world.cwd / '.desmos' / 'skills'}",
            f"user_skills: {Path.home() / '.desmos' / 'skills'}",
            f"sdk: {package_root()}",
            f"readme: {root / 'README.md'}",
            f"docs: {root / 'docs'}",
            "transcript: world.messages is append-only. step() continues it. Syscall output arrives as user <result> blocks.",
            "reload: every turn rediscovers skills/extensions. After writing SKILL.md, the next complete() sees it.",
            "reload_sdk(): reimport desmos.* then rebind step/reload/evolve. Use after you (or a human) edit the SDK.",
            "unstick: if a tag errors, read the error, fix attrs, retry. <system name=\"id\"> is required for a named note; nameless writes note.",
            "edit: <edit path=\"file.py\">old\\n---\\nnew</edit> — exactly one occurrence. Or edit.run(path, old, new).",
            "grow: write .desmos/skills/<name>/SKILL.md; keep going. Then <evolve>why</evolve>. Do not edit desmos/*.py to grow.",
            "rollback: <rollback n=\"1\"/>. Docs: docs/self-growth.md, docs/extensibility.md — read if you are changing the harness.",
        ]
    )


def system_prompt(world: World) -> str:
    return ABI + "\n\n" + catalog(world)


def header(world: World, task: str) -> str:
    lines = [f"generation: {world.generation} ({world.gen_reason})", f"cwd: {world.cwd}", ns_index(world)]
    if world.prior:
        lines.append("prior steps:")
        for i, item in enumerate(world.prior[-PRIOR_KEEP:], 1):
            lines.append(f"  {i}. user: {clip(item['prompt'], 240)}")
            lines.append(f"     you: {clip(item['speech'], 400)}")
    lines.append(f"prompt: {task}")
    return "\n".join(lines)
