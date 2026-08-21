from __future__ import annotations

import os

CANONICAL = frozenset(
    {"exec", "workspace", "knowledge", "harness", "observe", "agents", "session"}
)
#: Retired tag -> canonical replacement (canonical cut step 5). The single map
#: the dispatch rejection reads from: a removed spelling answers with guidance,
#: never silence and never a traceback. trajectory_retrace is exempt: it is a
#: grown tool that observe op=retrace still routes to.
REMOVED_TAGS = {
    "python": "exec op=python",
    "bash": "exec op=bash",
    "shell": "exec op=shell",
    "sleeper": "exec op=shell (monitored commands resume you when they land)",
    "find": "workspace op=find",
    "grep": "workspace op=find mode=grep",
    "read": "workspace op=read",
    "edit": "workspace op=edit",
    "see": "workspace op=see",
    "commit": "workspace op=commit",
    "memory": "knowledge op=memory",
    "recall": "knowledge op=recall",
    "system": "knowledge op=system",
    "todo": "knowledge op=todo",
    "register": "harness op=register",
    "tool": "harness op=describe",
    "skill": "harness op=skill",
    "reload": "harness op=reload",
    "reload_sdk": "harness op=reload-sdk",
    "evolve": "harness op=evolve",
    "rollback": "harness op=rollback",
    "refine": "harness op=refine",
    "usage": "observe op=usage",
    "traj": "observe op=trajectory",
    "compact": "session op=compact",
}
FROZEN = CANONICAL

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
<knowledge op="memory|recall|system|todo|decide">fact, query, doctrine, todo, or decision commands</knowledge>
<harness op="register|describe|skill|reload|reload-sdk|evolve|rollback">body</harness>
<observe op="usage|trajectory|retrace|error|symbol|threads">query</observe>
<agents op="spawn|fanout|resume|lineage|status|result|structured-result|judgment|wait">task or ids</agents>
<session op="compact|status|switch|peers|inbox|read|post|dismiss">arguments or channel message</session>

The required op selects an existing proven operation. Its body and remaining
attributes keep that operation's native shape. Unknown ops fail explicitly.
Legacy tag names are removed; a retired spelling answers with its replacement.

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
