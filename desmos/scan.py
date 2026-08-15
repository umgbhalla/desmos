from __future__ import annotations

import re

from desmos.const import RESULT_CAP
from desmos.types import Block

# <tag>, <tag/>, <tag />, <tag attr="v"/>, <tag attr='v'>, <tag attr=v/>.
#
# The attribute region is permissive on purpose. It used to be an enumerated
# list of double-quoted pairs, so `<skill name='ping'/>` and `<rollback n=1/>`
# did not match *at all* -- and an invisible tag reads to the loop as a message
# with no syscalls, which is how it decides the model is finished. A wrong
# attribute now produces a loud unknown-name error instead of a silent stop.
#
# Quoted values may contain `>`; a bare value may contain `/` but not the `/`
# of `/>`, or `<rollback n=1/>` parses as an opener and is then dropped for
# having no closer.
_REGION = r"""(?:"[^"]*"|'[^']*'|/(?!>)|[^<>"'/])*"""
_VALUE = r"""(?:"[^"]*"|'[^']*'|(?:/(?!>)|[^\s"'>/])+)"""
TAG_OPEN = re.compile(rf"<([A-Za-z_][\w.-]*)({_REGION})\s*(/)?>", re.S)
ATTR = re.compile(rf"([A-Za-z_][\w.-]*)\s*=\s*({_VALUE})")
# Openers and closers in one pass, so a same-name tag inside a body can be
# depth-matched instead of ending it early.
TAG_ANY = re.compile(rf"<(/?)([A-Za-z_][\w.-]*)({_REGION})\s*(/)?>", re.S)

# A fence opener: up to three spaces of indent, then three or more backticks
# or tildes, then an optional info string.
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*$", re.M)
# A line indented four spaces (or a tab). A run of them after a blank line is a
# CommonMark indented code block, which the story pane draws as code -- so a
# `<bash>` sample the model only meant to show used to dispatch for real.
INDENTED = re.compile(r"^(?: {4}|\t)[^\n]*$", re.M)
# A list marker, with the run of space that decides where the item's content
# starts. List item bodies are indented too, and masking one would silently
# drop the call in "1. first:\n\n    <bash>ls</bash>".
BULLET = re.compile(r"[ \t]*(?:[-*+]|\d+[.)])([ \t]+|$)")
# Inline spans are matched by backtick run length in `_in_code_span`, not by a
# regex: a span opened by one backtick may contain longer runs, and only an
# equal-length run closes it.


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


def _indent_width(line: str) -> int:
    line = line.expandtabs(4)
    return len(line) - len(line.lstrip(" "))


def _list_col(lines: list[str]) -> int:
    """Content column of the innermost list item still open after `lines`.

    CommonMark measures an indented code block from the enclosing item's
    content column, not from column zero: under `- ` the content starts at 2,
    so code needs six spaces and four is an ordinary paragraph of that item --
    a real call. A blank line and then a shallower line closes the item; a
    shallower line with no blank before it is a lazy continuation and does not,
    which is why this cannot be decided from the previous line alone.
    """
    cols: list[int] = []
    blank = True
    for ln in lines:
        if not ln.strip():
            blank = True
            continue
        ind = _indent_width(ln)
        m = BULLET.match(ln.expandtabs(4))
        if blank or m:
            while cols and ind < cols[-1]:
                cols.pop()
        if m and ind <= (cols[-1] if cols else 0) + 3:
            cols.append(len(m.group(0)) + (0 if m.group(1) else 1))
        blank = False
    return cols[-1] if cols else 0


def _indent_span(text: str, pos: int) -> tuple[int, int] | None:
    """Span of the next indented code block at or after `pos`.

    Only where CommonMark actually starts one: over-masking would re-create the
    failure this whole scanner is careful about, a real call disappearing.
    """
    for m in INDENTED.finditer(text, pos):
        before = text[: m.start()].splitlines()
        if before and before[-1].strip():
            continue  # indented continuation of a paragraph, not a block
        floor = _list_col(before) + 4
        if _indent_width(m.group(0)) < floor:
            continue  # a paragraph of the list item it sits in, not code
        end = m.end()
        for line in text[m.end() :].split("\n")[1:]:
            if line.strip() and _indent_width(line) < floor:
                break
            end += len(line) + 1
        return m.start(), min(end, len(text))
    return None


def _in_code_span(text: str, at: int) -> int | None:
    """End of the inline code span containing `at`, if there is one.

    A span opens on a run of n backticks and closes on the next run of exactly
    n, so ``` inside a single-backtick span is content, not a delimiter. A run
    with no closer is not a span at all -- it is literal text, which is what
    CommonMark says and what the renderer draws. Treating it as code to end of
    line let one stray backtick eat the syscall after it, and a message with no
    syscalls left in it is how the loop decides the model is finished.
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
        if close is None:
            i += n
            continue
        if i <= col < close:
            return start + close
        i = close
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
    indent = _indent_span(text, 0)
    while True:
        m = TAG_OPEN.search(text, pos)
        if not m:
            break
        while fence and fence[1] <= m.start():
            fence = _fence_span(text, fence[1])
        if fence and fence[0] <= m.start() < fence[1]:
            pos = fence[1]
            continue
        while indent and indent[1] <= m.start():
            indent = _indent_span(text, indent[1])
        if indent and indent[0] <= m.start() < indent[1]:
            pos = indent[1]
            continue
        span_end = _in_code_span(text, m.start())
        if span_end is not None:
            pos = span_end
            continue
        tag, raw_attrs, self_close = m.group(1), m.group(2) or "", m.group(3)
        attrs = {
            k: v[1:-1] if v[:1] in ('"', "'") else v
            for k, v in ATTR.findall(raw_attrs)
        }
        if self_close:
            blocks.append((Block(tag, "", attrs), m.start(), m.end()))
            pos = m.end()
            continue
        closer = re.compile(rf"</{re.escape(tag)}\s*>")
        fm = closer.search(text, m.end())
        if not fm:
            pos = m.end()  # unterminated: a cut-off reply is not half-dispatched
            continue
        # A same-name tag inside the body used to end it at the first closer:
        # half a <bash> command ran and the rest became residue nobody read.
        # Depth-matching past that closer is only safe while nothing else is in
        # the way -- one `<python>` mentioned in a string plus one stray
        # `</python>` further down the prose is enough to balance across a whole
        # `<bash>ls</bash>` and eat it, which is the failure this scanner exists
        # to prevent. So the search stops at the first tag past the first closer
        # that is not another closer for this same tag.
        depth, end, stop = 1, fm.start(), fm.end()
        for om in TAG_ANY.finditer(text, m.end()):
            shut = bool(om.group(1)) and om.group(2) == tag
            if om.start() >= stop and not shut:
                break
            if shut:
                depth -= 1
                if depth == 0:
                    end, stop = om.start(), om.end()
                    break
            elif not om.group(1) and om.group(2) == tag and not om.group(4):
                depth += 1
        blocks.append((Block(tag, text[m.end() : end], attrs), m.start(), stop))
        pos = stop
    return blocks
