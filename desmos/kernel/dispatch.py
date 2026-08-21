from __future__ import annotations

import contextvars
import traceback
import weakref
from inspect import signature
from typing import Any, Callable, Iterable

from desmos.kernel.const import CANONICAL, FROZEN
from desmos.kernel.edit import apply_edit, apply_edit_line, parse_edit_body  # noqa: F401  (apply_edit: facade re-export)
from desmos.kernel.exec import register_tag, run_bash, run_python
from desmos.kernel.const import RESULT_CAP
from desmos.kernel.spill import spill
from desmos.kernel.types import Block, World

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

# The world whose syscall is executing right now, bound for the duration of
# dispatch(). subagent.spawn() resolves its caller from this instead of
# trusting a parent= kwarg: a budget gate keyed on an argument the caller
# chooses is a gate the caller can walk around by passing nothing. A thread a
# child detaches carries no binding (contextvars do not cross Thread on this
# interpreter), so such a caller resolves to nothing and spawn refuses it.
# globals().get for the same reason as _SCOPES: reload_sdk re-executes this
# module, and a fresh ContextVar here would blind every child mid-turn.
CALLER_WORLD: contextvars.ContextVar[Any] = globals().get("CALLER_WORLD") or contextvars.ContextVar(
    "desmos_caller_world", default=None
)


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
    from desmos.state.persist import save

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
    from desmos.state.persist import save

    if name not in world.tools:
        known = ", ".join(sorted(world.tools)) or "none"
        return f"unknown tool {name!r}. Existing: {known}. <tool> rewrites a doc; <register> installs a new tag."
    if not doc.strip():
        # Empty doc is a describe, not a rewrite: a grown tool answers with
        # its usage evidence read off the calls record (constitution D3).
        if not world.tools[name].frozen:
            from desmos.state.refine import describe

            return describe(world, name, world.tools[name].doc)
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
    meta: dict[str, Any] | None = None,
) -> str:
    token = CALLER_WORLD.set(world)
    try:
        result = _dispatch(world, block, on_chunk=on_chunk, should_stop=should_stop, meta=meta)
    finally:
        CALLER_WORLD.reset(token)
    # Shadow observer (kernel/friction.py): bump this world's in-memory
    # friction counters and maybe append one nudge line to the result --
    # the todo_nudge channel, with zero extra API calls.
    from desmos.kernel.friction import observe as friction_observe

    return friction_observe(world, block, result)


def _dispatch(
    world: World,
    block: Block,
    *,
    on_chunk: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    canonical_direct = None
    if block.tag in CANONICAL:
        from desmos.kernel.canonical import normalize, policy_target

        normalized = normalize(world, block)
        if isinstance(normalized, str):
            return normalized
        if normalized.tag in CANONICAL:
            canonical_direct = normalized
            block = Block(policy_target(normalized), normalized.body, normalized.attrs)
        else:
            block = normalized

    # Before the hooks and before the frozen chain: a denied tag must not reach
    # third-party code and must not run. Refuse in prose, never raise -- a raise
    # here kills the child's turn instead of teaching it what it may call.
    # Only tags that exist are refused: telling a child that hallucinated <grep>
    # it is "outside your scope" says the tag is real and withheld, and costs it
    # the unknown-tag answer below, which is where "speak when done" lives.
    scope = scope_of(world)
    if scope is not None and block.tag not in scope:
        if canonical_direct is not None or block.tag in FROZEN or block.tag in world.tools:
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
    if canonical_direct is not None:
        from desmos.kernel.canonical import direct

        return direct(
            world, canonical_direct,
            on_chunk=on_chunk, should_stop=should_stop, meta=meta,
        )
    from desmos.kernel.canonical import LEGACY_TO_CANONICAL, run_op

    legacy = LEGACY_TO_CANONICAL.get(block.tag)
    if legacy is not None:
        # Inverted ownership (canonical cut step 2): every legacy frozen
        # spelling is a thin forwarder into the canonical family operation,
        # which owns the implementation. Results are byte-identical for both
        # spellings; the attrs copy keeps run_op's pops off the caller's Block.
        family, op = legacy
        return run_op(
            world, family, op, block.body, dict(block.attrs),
            on_chunk=on_chunk, should_stop=should_stop, meta=meta,
        )
    tool = world.tools.get(block.tag)
    if tool is None or tool.handler is None:
        if tool is None:
            # A retired grown tag answers with its tombstone, never silence:
            # the row outlives the registry entry (constitution A1/D2).
            from desmos.state.refine import epitaph

            note = epitaph(world, block.tag)
            if note:
                return note
        from desmos.kernel.catalog import advertised_names

        known = ", ".join(advertised_names(world)) or "none"
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
