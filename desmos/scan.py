from __future__ import annotations

import re

from desmos.const import RESULT_CAP
from desmos.types import Block

# <tag>, <tag/>, <tag />, <tag attr="v"/>, <tag attr="v">
TAG_OPEN = re.compile(
    r'<([A-Za-z_][\w.-]*)((?:\s+[A-Za-z_][\w.-]*\s*=\s*"[^"]*")*)\s*(/)?>',
    re.S,
)
ATTR = re.compile(r'([A-Za-z_][\w.-]*)\s*=\s*"([^"]*)"')

# A fence opener: up to three spaces of indent, then three or more backticks
# or tildes, then an optional info string.
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*$", re.M)
# An inline code span, confined to one line so an unbalanced backtick in prose
# can never swallow the rest of the message.
CODE_SPAN = re.compile(r"`+[^`\n]*`+")


def _fence_span(text: str, pos: int) -> tuple[int, int] | None:
    """Span of the next *closed* fenced block at or after `pos`.

    An unclosed opener returns None rather than masking to end of text. Silently
    dropping every syscall after a stray fence is a far worse failure than
    leaving one fence unprotected.
    """
    m = FENCE.search(text, pos)
    if not m:
        return None
    marker = m.group(1)
    closer = re.compile(
        rf"^[ \t]{{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$", re.M
    )
    c = closer.search(text, m.end())
    return (m.start(), c.end()) if c else None


def _in_code_span(text: str, at: int) -> int | None:
    """End of the inline code span containing `at`, if there is one."""
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    line = text[start:] if end < 0 else text[start:end]
    for m in CODE_SPAN.finditer(line):
        if m.start() <= at - start < m.end():
            return start + m.end()
    return None


def clip(text: str, cap: int = RESULT_CAP, *, keep: str = "head") -> str:
    """Trim to `cap`, keeping the head by default or the tail on demand.

    Which end matters depends on what the text is. Output reads top-down, so
    the head is right. A traceback is the last thing printed, so a script that
    logged its way past the cap before dying returned a result with the error
    trimmed off -- the model saw a wall of progress and no reason for failure.
    """
    if len(text) <= cap:
        return text
    room = cap - 24
    if keep == "tail":
        return f"…[{len(text) - room} chars clipped]\n" + text[-room:]
    return text[:room] + f"\n…[{len(text) - room} chars clipped]"


def scan(text: str) -> list[Block]:
    """Find the syscalls in a message.

    Angle brackets inside a fenced block or an inline code span are display
    text, not a call: a mermaid line-break tag in a diagram label used to
    dispatch as `<br>` and come back as an unknown-tag error. A tag body is
    stepped over whole, so a fence *inside* a syscall never masks the calls
    that follow it.
    """
    blocks: list[Block] = []
    pos = 0
    fence = _fence_span(text, 0)
    while True:
        m = TAG_OPEN.search(text, pos)
        if not m:
            break
        while fence and fence[1] <= m.start():
            fence = _fence_span(text, fence[1])
        if fence and fence[0] <= m.start() < fence[1]:
            pos = fence[1]
            continue
        span_end = _in_code_span(text, m.start())
        if span_end is not None:
            pos = span_end
            continue
        tag, raw_attrs, self_close = m.group(1), m.group(2) or "", m.group(3)
        attrs = {k: v for k, v in ATTR.findall(raw_attrs)}
        if self_close:
            blocks.append(Block(tag, "", attrs))
            pos = m.end()
            continue
        close = f"</{tag}>"
        end = text.find(close, m.end())
        if end < 0:
            pos = m.end()
            continue
        blocks.append(Block(tag, text[m.end() : end], attrs))
        pos = end + len(close)
    return blocks
