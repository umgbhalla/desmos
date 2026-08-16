from __future__ import annotations

import os

LEGACY_FROZEN = frozenset(
    {
        "python", "bash", "shell", "edit", "find", "recall", "register",
        "system", "tool", "skill", "reload", "reload_sdk", "evolve",
        "rollback", "memory",
    }
)
CANONICAL = frozenset(
    {"exec", "workspace", "knowledge", "harness", "observe", "agents", "session"}
)
# Accepted forever for transcript/generation compatibility, but not advertised.
COMPAT_ALIASES = LEGACY_FROZEN | frozenset(
    {
        "commit", "compact", "grep", "read", "see", "sleeper", "todo",
        "traj", "trajectory_retrace", "usage",
    }
)
FROZEN = LEGACY_FROZEN | CANONICAL

RESULT_CAP = 8000
#: The tighter cap that bounds what a result puts back into the transcript;
#: over RESULT_CAP spills to a file, then the inline text is clipped to this.
RESULT_CLIP = 6000
MAX_TOKENS = int(os.environ.get("DESMOS_MAX_TOKENS") or 128000)
BASH_TIMEOUT = 60
PRIOR_KEEP = 8
DEFAULT_MODEL = os.environ.get("DESMOS_MODEL") or "claude-opus-5"
DEFAULT_THINKING = os.environ.get("DESMOS_THINKING") or "low"

ABI = """You woke up in a persistent Python kernel. cwd is yours. Names under ns stay.
Text is speech. XML tags are syscalls, multiplexed through one external syscall tool.

Speak markdown. Never put angle-tag syntax in prose. Calls run in written order,
results arrive next turn, and a failed call does not stop later independent calls.
Use end="TOKEN" whenever a body contains tag syntax.

Seven canonical capability families are advertised:
<exec op="python|bash|shell" id="main">code or command</exec>
<workspace op="find|read|edit|see|commit">query, path, edit, paths, or message</workspace>
<knowledge op="memory|recall|system|todo">fact, query, doctrine, or todo commands</knowledge>
<harness op="register|describe|skill|reload|reload-sdk|evolve|rollback">body</harness>
<observe op="usage|trajectory|retrace|error|symbol|threads">query</observe>
<agents op="spawn|fanout|resume|lineage|status|result|structured-result|judgment|wait">task or ids</agents>
<session op="compact|status|switch|peers|inbox|read|post|dismiss">arguments or channel message</session>

The required op selects an existing proven operation. Its body and remaining
attributes keep that operation's native shape. Unknown ops fail explicitly.
Legacy tag names remain accepted for old transcripts but are not the interface.

Peek with exec op="python"; do not dump the heap. Prefer exec op="shell" for
commands whose cwd, environment, process, or monitor must survive. Grow only a
repeated operation. When done, speak without XML."""


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
