from __future__ import annotations

import traceback

from desmos.const import FROZEN
from desmos.edit import apply_edit, parse_edit_body
from desmos.exec import register_tag, run_bash, run_python
from desmos.generations import evolve, rollback
from desmos.persist import save
from desmos.scan import clip
from desmos.types import Block, World


def set_system(world: World, body: str, name: str, delete: bool) -> str:
    if not name:
        name = "note"
    if delete:
        existed = world.notes.pop(name, None)
        save(world)
        return f"deleted note {name}" if existed is not None else f"no note {name}"
    world.notes[name] = body.strip()
    save(world)
    return f"wrote note {name} ({len(world.notes[name])} chars)"


def set_tool_doc(world: World, name: str, doc: str) -> str:
    if name not in world.tools:
        return f"unknown tool {name!r}"
    if not doc.strip():
        return "tool failed: doc required"
    world.tools[name].doc = doc.strip()
    save(world)
    return f"updated <{name}> doc"


def dispatch(world: World, block: Block) -> str:
    for hook in world.hooks.get("before_dispatch", []):
        verdict = hook(world, block)
        if isinstance(verdict, str):
            return verdict
    if block.tag == "python":
        return run_python(block.body, world)
    if block.tag == "bash":
        return run_bash(block.body, world.cwd)
    if block.tag == "edit":
        old, new = parse_edit_body(block.body, block.attrs)
        return apply_edit(block.attrs.get("path", ""), old, new, cwd=world.cwd)
    if block.tag == "register":
        return register_tag(world, block.body, block.attrs.get("name", ""), block.attrs.get("doc", ""))
    if block.tag == "system":
        delete = block.attrs.get("delete", "") in {"1", "true", "yes"}
        return set_system(world, block.body, block.attrs.get("name", ""), delete)
    if block.tag == "tool":
        return set_tool_doc(world, block.attrs.get("name", ""), block.attrs.get("doc", "") or block.body)
    if block.tag == "skill":
        from desmos.skills import load_skill_body

        name = (block.attrs.get("name") or block.body).strip()
        skill = next((s for s in world.skills if s.name == name), None)
        if skill is None:
            return f"unknown skill {name!r}"
        return load_skill_body(skill)
    if block.tag == "reload":
        from desmos.loop import reload

        return reload(world)
    if block.tag == "reload_sdk":
        from desmos.loop import reload_sdk

        return reload_sdk(world)
    if block.tag == "evolve":
        return evolve(world, (block.body or block.attrs.get("reason") or "").strip() or "unspecified")
    if block.tag == "rollback":
        raw = block.attrs.get("n") or block.body.strip() or "1"
        try:
            n = int(raw)
        except ValueError:
            return f"rollback failed: bad n {raw!r}"
        return rollback(world, n)
    tool = world.tools.get(block.tag)
    if tool is None or tool.handler is None:
        known = ", ".join(sorted(world.tools) or sorted(FROZEN))
        return f"unknown tag <{block.tag}> — not a syscall. Known: {known}. Speak without XML when done."
    try:
        return clip(str(tool.handler(block.body, **block.attrs)))
    except TypeError:
        try:
            return clip(str(tool.handler(block.body)))
        except Exception:
            return traceback.format_exc()
    except Exception:
        return traceback.format_exc()
