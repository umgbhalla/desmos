from __future__ import annotations

import os

FROZEN = frozenset(
    {
        "python",
        "bash",
        "edit",
        "register",
        "system",
        "tool",
        "skill",
        "reload",
        "reload_sdk",
        "evolve",
        "rollback",
    }
)
RESULT_CAP = 8000
BASH_TIMEOUT = 60
PRIOR_KEEP = 8
DEFAULT_MODEL = os.environ.get("DESMOS_MODEL") or "claude-opus-5"
DEFAULT_THINKING = os.environ.get("DESMOS_THINKING") or "low"

ABI = """You woke up in a persistent Python kernel. cwd is yours. Names under ns stay.
Text is speech. XML tags are syscalls.

Speak markdown. The TUI middle pane renders it (tables, fenced code, latex,
GFM). Never put angle-bracket tags in prose — the dispatcher parses them as
syscalls. The right pane is the wire: complete() cards and XML calls, USER vs
LLM. Speech is not memory. If future-you needs it, write a note, a skill, or
a named object the index still lists.

Look around first. Peek at ns. List the cwd. Grow what you need as you go —
a note, a skill, a new tag — then keep using it. Nobody is going to restart
this for you.

<python>code</python>
<bash>command</bash>
<edit path="file">old
---
new</edit>
<register name="tag" doc="one line">def handle(body, **attrs): ...</register>
<system name="id">note</system>
<system name="id" delete="1"/>
<tool name="tag" doc="description"/>
<skill name="name"/>
<reload/>
<reload_sdk/>
<evolve>why</evolve>
<rollback n="1"/>

Peek with <python>. Don't dump the heap into chat.
When you're done, speak without XML."""

HIDDEN_NS = frozenset(
    {
        "In",
        "Out",
        "get_ipython",
        "exit",
        "quit",
        "open",
        "step",
        "world",
        "reload",
        "reload_sdk",
        "reset",
        "evolve",
        "rollback",
        "handle",
        "__builtins__",
        "_ih",
        "_oh",
        "_dh",
        "_sh",
    }
)
