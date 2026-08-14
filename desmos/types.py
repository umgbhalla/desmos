from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from desmos.const import DEFAULT_MODEL, DEFAULT_THINKING


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
    generation: int = 1
    gen_reason: str = "gen-1"
    persist: bool = True
    # True while run_turns owns this world. bind_step publishes step/reset into
    # the kernel, so a <python> block can call them from inside a turn -- and a
    # nested run appends its whole exchange before the outer assistant message
    # lands, writing a transcript whose causality is wrong and then replaying it
    # forever. reset() is worse: it clears the list the outer loop is appending
    # to. Neither is worth supporting; both are worth refusing.
    running: bool = False
