from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from desmos.kernel.const import DEFAULT_MODEL, DEFAULT_THINKING


@dataclass
class Block:
    tag: str
    body: str
    attrs: dict[str, str]


@dataclass
class Tool:
    name: str
    doc: str
    source: str | None = None
    handler: Callable[..., Any] | None = None
    frozen: bool = False


@dataclass
class World:
    ns: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Tool] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    cwd: Path = field(default_factory=lambda: Path.cwd())
    state_path: Path | None = None
    shell: Any = None
    model: str = field(default_factory=lambda: DEFAULT_MODEL)
    thinking: str = field(default_factory=lambda: DEFAULT_THINKING)
    complete_fn: Callable[..., dict[str, Any]] | None = None
    prior: list[dict[str, str]] = field(default_factory=list)
    skills: list[Any] = field(default_factory=list)
    hooks: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Prefixes inherited from prior sessions. save() writes only the suffix
    # born in this attach, preserving message provenance across restarts.
    session_message_start: int = 0
    session_prior_start: int = 0
    # The catalog text as first sent this run. Frozen so a mid-run <register>,
    # <tool>, <system> or <evolve> ships as a small delta at the tail instead of
    # editing the cached system block and re-writing every token behind it.
    # In memory only: a new process rebuilds it from the live objects.
    catalog_frozen: str = ""
    generation: int = 1
    gen_reason: str = "gen-1"
    persist: bool = True
    # Replaces the generated system prompt for this world. Subagents set it;
    # loop.py reads it through getattr, which is why it was never declared.
    system_override: str | None = None
    # True while run_turns owns this world. bind_step publishes step/reset into
    # the kernel, so a <python> block can call them from inside a turn -- and a
    # nested run appends its whole exchange before the outer assistant message
    # lands, writing a transcript whose causality is wrong and then replaying it
    # forever. reset() is worse: it clears the list the outer loop is appending
    # to. Neither is worth supporting; both are worth refusing.
    running: bool = False
    # Named live shells, kept for the life of this process. Not persisted:
    # a pty cannot be reloaded from JSON, and pretending otherwise would hand
    # back a session whose cd and exports silently did not survive.
    shells: dict[str, Any] = field(default_factory=dict)
