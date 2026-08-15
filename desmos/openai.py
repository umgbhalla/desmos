"""Facade: the public SDK surface of desmos.transport.openai.

Stored state (grown tools, skills, extensions) imports desmos-level names;
this module re-exports the implementation so those imports never break.
Internal code imports the real subpackage path, never this facade.
"""

from desmos.transport.openai import *  # noqa: F401,F403

__all__ = [
    "API_URL",
    "Any",
    "CHATGPT_URL",
    "COMPACT_BLOCK",
    "COMPACT_URL",
    "CONTRACT",
    "Callable",
    "EFFORTS",
    "Iterable",
    "MODELS",
    "OPENAI_ALIASES",
    "OPENAI_PREFIXES",
    "ORIGINATOR",
    "apply_stream_event",
    "assemble",
    "compact_window",
    "complete",
    "effort_of",
    "headers_for",
    "is_openai",
    "iter_sse_lines",
    "log_payload",
    "payload_for",
    "read_sse",
    "redact_wire",
    "session_id",
    "to_input",
    "unsupported_field",
]
