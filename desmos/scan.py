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
    blocks: list[Block] = []
    pos = 0
    while True:
        m = TAG_OPEN.search(text, pos)
        if not m:
            break
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
