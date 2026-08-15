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
# Inline spans are matched by backtick run length in `_in_code_span`, the same
# way the TUI's `inline_code_spans` does it. A regex cannot: a span opened by
# one backtick may contain longer runs, and only an equal-length run closes it.


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
    """End of the inline code span containing `at`, if there is one.

    A span opens on a run of n backticks and closes on the next run of exactly
    n, so ``` inside a single-backtick span is content, not a delimiter. An
    unclosed opener spans to end of line -- bounded damage, and it is what the
    renderer already does, so what is displayed and what dispatches agree.
    """
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    line = text[start:] if end < 0 else text[start:end]
    col = at - start
    i = 0
    while i < len(line):
        if line[i] != "`":
            i += 1
            continue
        n = 0
        while i + n < len(line) and line[i + n] == "`":
            n += 1
        j, close = i + n, None
        while j < len(line):
            if line[j] == "`":
                m = 0
                while j + m < len(line) and line[j + m] == "`":
                    m += 1
                if m == n:
                    close = j + m
                    break
                j += m
            else:
                j += 1
        stop = close if close is not None else len(line)
        if i <= col < stop:
            return start + stop
        i = stop
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
    return [b for b, _, _ in scan_spans(text)]


def trailing_residue(text: str) -> str:
    """Speech left over after the last syscall in a reply.

    The scanner is a finder, not a partitioner: anything it does not recognise
    as a call is speech by default, so a degenerate token appended after the
    final closing tag is accepted in silence and replayed forever. gpt-5.6-sol
    did exactly that for a whole session -- every message from the seventeenth
    on ended in a stray `lousy?`, and nothing in the harness could see it
    because nothing ever asked what was outside the tags.

    This reports; it never rewrites. The stored message has to stay byte-exact
    or the cached prefix breaks on the next request.
    """
    spans = scan_spans(text)
    if not spans:
        return ""
    return text[spans[-1][2] :].strip()


def scan_spans(text: str) -> list[tuple[Block, int, int]]:
    """Every syscall with the character range it occupied."""
    blocks: list[tuple[Block, int, int]] = []
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
            blocks.append((Block(tag, "", attrs), m.start(), m.end()))
            pos = m.end()
            continue
        close = f"</{tag}>"
        end = text.find(close, m.end())
        if end < 0:
            pos = m.end()
            continue
        blocks.append((Block(tag, text[m.end() : end], attrs), m.start(), end + len(close)))
        pos = end + len(close)
    return blocks
