from __future__ import annotations

import traceback
import weakref
from inspect import signature
from typing import Any, Callable, Iterable

from desmos.const import FROZEN
from desmos.edit import apply_edit, parse_edit_body
from desmos.exec import register_tag, run_bash, run_python
from desmos.generations import evolve, rollback
from desmos.persist import save
from desmos.const import RESULT_CAP
from desmos.spill import spill
from desmos.types import Block, World

# Which tags a scoped world may run, keyed by id(world). It lives here and not
# on the World because bind_step publishes the World into the world's own ns:
# as an attribute, `world.allowed_tags = None` from one <python> line turned
# off the gate that <python> line was under, and the <edit> refused a moment
# earlier then went through.
#
# This is a scope rail, not a sandbox. Every capability grants <python> and
# <bash>, and both can write any file and import any desmos module, so nothing
# here contains a determined child in-process. What it does contain is the
# harness-level tags -- <system>, <evolve>, <rollback>, <register>,
# <reload_sdk> -- which a scoped child now cannot reach by emitting them.
#
# globals().get: reload_sdk re-executes this module in its own namespace, so a
# fresh {} on this line would unscope every child running in a pool thread at
# that moment. finalize drops the entry when the world dies, so a recycled
# id() cannot scope an unrelated later world.
_SCOPES: dict[int, frozenset[str]] = globals().get("_SCOPES", {})

# A child gets a narrow reference to its parent's todo handler, never the
# parent World or persistence layer. Entries disappear with the child world.
_CHILD_TODOS: dict[int, Callable[[str], Any]] = globals().get("_CHILD_TODOS", {})


def set_child_todo_handler(
    world: World, handler: Callable[..., Any], *, actor: str
) -> None:
    """Let a child append through a parent handler, with stable attribution."""
    key = id(world)

    def append(body: str) -> Any:
        # The durable todo handler is a line-command parser: only `+ text`
        # appends. Flatten the child body to one item so a second line such as
        # `x 1` cannot smuggle an existing-row mutation through the parent.
        text = " ".join(body.split())
        if not text:
            return "todo append rejected: item required"
        return handler(f"+ [{actor}] {text}")

    _CHILD_TODOS[key] = append
    weakref.finalize(world, _CHILD_TODOS.pop, key, None)


def set_scope(world: World, tags: Iterable[str] | None) -> None:
    """Restrict `world` to `tags`. None means every tag, as for the kernel."""
    if tags is None:
        _SCOPES.pop(id(world), None)
        return
    _SCOPES[id(world)] = frozenset(tags)
    weakref.finalize(world, _SCOPES.pop, id(world), None)


def scope_of(world: World) -> frozenset[str] | None:
    """The tags `world` may run, or None if it is unscoped."""
    return _SCOPES.get(id(world))


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


def dispatch(
    world: World,
    block: Block,
    *,
    on_chunk: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    # Before the hooks and before the frozen chain: a denied tag must not reach
    # third-party code and must not run. Refuse in prose, never raise -- a raise
    # here kills the child's turn instead of teaching it what it may call.
    # Only tags that exist are refused: telling a child that hallucinated <grep>
    # it is "outside your scope" says the tag is real and withheld, and costs it
    # the unknown-tag answer below, which is where "speak when done" lives.
    scope = scope_of(world)
    if scope is not None and block.tag not in scope:
        if block.tag in FROZEN or block.tag in world.tools:
            return (
                f"<{block.tag}> is outside this agent's scope. "
                f"Allowed: {', '.join(sorted(scope)) or 'none'}."
            )
    for hook in world.hooks.get("before_dispatch", []):
        verdict = hook(world, block)
        if isinstance(verdict, str):
            return verdict
    if block.tag == "todo" and id(world) in _CHILD_TODOS:
        append = _CHILD_TODOS[id(world)]
        action = (block.attrs.get("action") or block.attrs.get("op") or "append").lower()
        if action != "append":
            return (
                f"todo {action!r} rejected: subagents may append parent todos "
                "but may not mutate existing rows"
            )
        try:
            return spill(
                str(append(block.body)),
                RESULT_CAP,
                tag=block.tag,
                cwd=world.cwd,
            )
        except Exception:
            return traceback.format_exc()
    if block.tag == "python":
        return run_python(block.body, world, on_chunk=on_chunk)
    if block.tag == "bash":
        return run_bash(
            block.body,
            world.cwd,
            on_chunk=on_chunk,
            should_stop=should_stop,
        )
    if block.tag == "shell":
        from desmos.shell import run as run_shell

        return run_shell(world, block.body, block.attrs)
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
        return load_skill_body(skill, world.model)
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
    if block.tag == "memory":
        from desmos.memory import handle_memory

        return handle_memory(world, block.body, block.attrs)
    tool = world.tools.get(block.tag)
    if tool is None or tool.handler is None:
        known = ", ".join(sorted(world.tools) or sorted(FROZEN))
        return f"unknown tag <{block.tag}> — not a syscall. Known: {known}. Speak without XML when done."
    try:
        return spill(
            str(_invoke(tool.handler, block.body, block.attrs)),
            RESULT_CAP,
            tag=block.tag,
            cwd=world.cwd,
        )
    except Exception:
        return traceback.format_exc()


def _invoke(handler: Callable[..., Any], body: str, attrs: dict[str, str]) -> Any:
    """Call a handler with attrs if it takes them, plain if it does not.

    This used to be `try handler(body, **attrs) except TypeError: handler(body)`
    -- an exception used as a feature test. A handler that raised TypeError
    *itself*, anywhere after its first side effect, was then run a second time:
    the file written twice, the request sent twice. Ask the signature instead;
    it answers before anything runs.
    """
    if not attrs:
        return handler(body)
    try:
        signature(handler).bind(body, **attrs)
    except TypeError:
        # The attrs genuinely do not fit this handler's parameters.
        return handler(body)
    except (ValueError, KeyError):
        # No introspectable signature (a builtin, a C callable). Prefer the
        # richer call and let a real TypeError surface as the error it is.
        pass
    return handler(body, **attrs)
