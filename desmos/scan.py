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


def clip(text: str, cap: int = RESULT_CAP) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 24] + f"\n…[{len(text) - cap + 24} chars clipped]"


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
