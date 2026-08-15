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


def _list_cols(text: str) -> list[tuple[int, int, bool]]:
    """For each line: its start offset, the open list item's content column, and
    whether the line before it was blank.

    CommonMark measures an indented code block from the enclosing item's
    content column, not from column zero: under `- ` the content starts at 2,
    so code needs six spaces and four is an ordinary paragraph of that item --
    a real call. A blank line and then a shallower line closes the item; a
    shallower line with no blank before it is a lazy continuation and does not,
    which is why this cannot be decided from the previous line alone.

    One forward pass, because the answer for every line is wanted. Recomputing
    it per candidate tag was quadratic on ordinary markdown -- a 28 KB reply
    made of a list with indented continuations took 1.7 seconds to scan, and
    the loop scans each message more than once.
    """
    rows: list[tuple[int, int, bool]] = []
    cols: list[int] = []
    blank = True
    off = 0
    for ln in text.split("\n"):
        rows.append((off, cols[-1] if cols else 0, blank))
        off += len(ln) + 1
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
    return rows


def _indent_span(
    text: str, pos: int, rows: list[tuple[int, int, bool]] | None = None
) -> tuple[int, int] | None:
    """Span of the next indented code block at or after `pos`.

    Only where CommonMark actually starts one: over-masking would re-create the
    failure this whole scanner is careful about, a real call disappearing.
    """
    if rows is None:
        rows = _list_cols(text)
    from bisect import bisect_right

    starts = [r[0] for r in rows]
    for m in INDENTED.finditer(text, pos):
        i = bisect_right(starts, m.start()) - 1
        _, col, blank = rows[i]
        if not blank:
            continue  # indented continuation of a paragraph, not a block
        floor = col + 4
        if _indent_width(m.group(0)) < floor:
            continue  # a paragraph of the list item it sits in, not code
        end = m.end()
        for line in text[m.end() :].split("\n")[1:]:
            if line.strip() and _indent_width(line) < floor:
                break
            end += len(line) + 1
        return m.start(), min(end, len(text))
    return None


# Tags whose body is executed verbatim. Only for these is the closing-tag
# quoting heuristic worth its cost: ending a body early there runs half a
# program, or runs the prose after it. For every other tag the body is prose or
# a patch, and an apostrophe in "the agent's own commits" used to make the
# whole call vanish -- no dispatch, no error, three lost commits in one session.
_QUOTED_BODY = frozenset({"python", "bash", "shell", "register"})


def _in_string(body: str) -> bool:
    """Is the end of `body` inside an unclosed quote?

    Bash and Python agree on the part that matters here: a run of `'` or `"`
    opens, the same run closes, and a backslash escapes the next character
    inside a double quote. Triple quotes are handled by the run length, so a
    docstring mentioning a closing tag does not end the block.
    """
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            i += 2
            continue
        if c not in "\"'":
            i += 1
            continue
        run = 3 if body[i : i + 3] in ('"""', "'''") else 1
        quote = body[i : i + run]
        j = i + run
        while j < n:
            if body[j] == "\\" and run == 1 and quote == '"':
                j += 2
                continue
            if body[j : j + run] == quote:
                break
            j += 1
        if j >= n:
            return True  # opened and never closed: everything after is string
        i = j + run
    return False


def _run(line: str, i: int) -> int:
    """Length of the backtick run starting at `i`."""
    j = i
    while j < len(line) and line[j] == "`":
        j += 1
    return j - i


def _in_code_span(text: str, at: int) -> int | None:
    """End of the inline code span containing `at`, if there is one.

    A span opens on a run of n backticks and closes on the next run of exactly
    n, so ``` inside a single-backtick span is content, not a delimiter. A run
    with no closer is not a span at all -- it is literal text, which is what
    CommonMark says and what the renderer draws. Treating it as code to end of
    line let one stray backtick eat the syscall after it, and a message with no
    syscalls left in it is how the loop decides the model is finished.

    Backticks are found with `str.find`, not by walking characters: this runs
    once per candidate tag over that tag's whole line, and a 326 KB reply with
    no newline in it -- a pasted JSX dump -- stalled the turn for 26 seconds
    walking the same line in Python for every tag on it.
    """
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    line = text[start:] if end < 0 else text[start:end]
    col = at - start
    i = line.find("`")
    while i >= 0:
        n = _run(line, i)
        j, close = i + n, None
        while (j := line.find("`", j)) >= 0:
            m = _run(line, j)
            if m == n:
                close = j + m
                break
            j += m
        if close is None:
            i = line.find("`", i + n)
            continue
        if i <= col < close:
            return start + close
        i = line.find("`", close)
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


def dropped_openers(text: str) -> list[str]:
    """Tags that opened in this reply and were skipped instead of dispatched.

    scan_spans is deliberately conservative: an opener whose closer never
    arrives is stepped over, because half-dispatching a cut-off reply is worse
    than dispatching nothing. But it does that in complete silence, and a turn
    that dropped its only syscall is indistinguishable from a turn that chose
    not to call one. That silence has cost this harness three commits and a
    rollback to generation 1.

    Anthropic stop sequences make it routine rather than exotic: they cut
    generation at a line start anywhere in the reply, body included, so a
    <python> writing about this harness can be guillotined mid-body and vanish.
    end="TOKEN" cannot save it -- that is parsed here, long after the API
    already stopped the stream.
    """
    dropped: list[str] = []
    scan_spans(text, dropped=dropped)
    return dropped


def scan_spans(
    text: str, *, dropped: list[str] | None = None
) -> list[tuple[Block, int, int]]:
    """Every syscall with the character range it occupied.

    Openers that never resolved to a call are reported through `dropped`.
    """
    def drop(tag: str, why: str) -> None:
        if dropped is not None:
            dropped.append(f"{tag} ({why})")

    blocks: list[tuple[Block, int, int]] = []
    pos = 0
    fence = _fence_span(text, 0)
    rows = _list_cols(text)
    indent = _indent_span(text, 0, rows)
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
            indent = _indent_span(text, indent[1], rows)
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
        # An explicit end token makes the body opaque: `<python end="X">` runs
        # to `</python:X>` and nothing else. Every other rule here is a
        # heuristic about *which* `</python>` the model meant, and the one case
        # no heuristic can solve is editing this codebase, whose sources are
        # full of literal tag text. Declare a token and the question stops
        # existing -- the body may contain as many bare closers as it likes.
        token = attrs.pop("end", "")
        if token:
            if not re.fullmatch(r"[\w.-]+", token):
                pos = m.end()  # an unusable token is not a silent bare closer
                drop(tag, f"unusable end token {token!r}")
                continue
            custom = re.compile(rf"</{re.escape(tag)}\s*:\s*{re.escape(token)}\s*>")
            cm = custom.search(text, m.end())
            if not cm:
                pos = m.end()
                drop(tag, f"no closing </{tag}:{token}>")
                continue
            blocks.append((Block(tag, text[m.end() : cm.start()], attrs), m.start(), cm.end()))
            pos = cm.end()
            continue
        closer = re.compile(rf"</{re.escape(tag)}\s*>")
        fm = closer.search(text, m.end())
        if not fm:
            pos = m.end()  # unterminated: a cut-off reply is not half-dispatched
            drop(tag, "no closing tag")
            continue
        # The body ends at the first closer that is not inside a string, because
        # the only closer the model writes early is one it quoted: `print("
        # </python>")`, `echo "</bash>"`. Ending there truncated the body and ran
        # half the program.
        #
        # Depth-matching past it is worse. An opener quoted anywhere in the body
        # inflates the count, so one stray `</bash>` further down the prose
        # balances across everything between -- and for <bash> that means the
        # narration in between is executed. `<bash>echo "<bash>"</bash>` then a
        # line of prose then `</bash>` ran the prose. Quoting is what tells the
        # two apart, so ask about quoting rather than counting tags.
        end, stop = fm.start(), fm.end()
        while tag in _QUOTED_BODY and _in_string(text[m.end() : end]):
            fm = closer.search(text, stop)
            if not fm:
                end, stop = m.end(), m.end()  # unterminated once quotes are honoured
                break
            end, stop = fm.start(), fm.end()
        if stop == m.end():
            pos = m.end()
            drop(tag, "every closing tag looked quoted")
            continue
        blocks.append((Block(tag, text[m.end() : end], attrs), m.start(), stop))
        pos = stop
    return blocks
