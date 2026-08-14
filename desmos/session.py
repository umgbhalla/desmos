from __future__ import annotations

"""Toy session infra: append-only log, derived context.

The transcript in World is a list you mutate — compaction there destroys
the originals. This is the other shape, modelled on Anthropic's
server-side compaction (beta `compact-2026-01-12`):

  - Storage is an append-only JSONL log. Nothing is ever rewritten.
  - The wire prompt is a *projection*: scan back for the newest
    `compaction` block and drop every block before it.
  - The summary is not a new role. It is a content block inside a normal
    assistant message, so the conversation stays user/assistant only.

That last point is what makes append-only work. The server hands the
summary back as part of an assistant turn; you append it like anything
else. Deletion never enters the protocol.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

BETA = "compact-2026-01-12"
STRATEGY = "compact_20260112"
MIN_TRIGGER = 50_000  # API floor: trigger.value must be >= 50k


@dataclass
class Entry:
    """One appended turn. seq/id/ts are assigned by the log, not the caller."""

    seq: int
    id: str
    ts: float
    role: str  # "user" | "assistant"
    content: list[dict[str, Any]]

    def to_json(self) -> str:
        return json.dumps(
            {"seq": self.seq, "id": self.id, "ts": self.ts,
             "role": self.role, "content": self.content},
            default=str,
        )

    @property
    def compaction(self) -> int:
        """Index of a compaction block in this entry, or -1."""
        for i, b in enumerate(self.content):
            if b.get("type") == "compaction":
                return i
        return -1


def blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in content]


@dataclass
class Session:
    """Append-only entry log with a derived message view."""

    path: Path | None = None
    entries: list[Entry] = field(default_factory=list)

    # ---- growth: the only mutation is append ----------------------------

    def append(self, role: str, content: Any) -> Entry:
        if role not in ("user", "assistant"):
            raise ValueError(f"role must be user|assistant, got {role!r}")
        e = Entry(
            seq=len(self.entries),
            id=f"{int(time.time() * 1000):x}-{len(self.entries):04x}",
            ts=time.time(),
            role=role,
            content=blocks(content),
        )
        self.entries.append(e)
        if self.path:
            with open(self.path, "a") as fh:
                fh.write(e.to_json() + "\n")
        return e

    @classmethod
    def load(cls, path: str | Path) -> "Session":
        path = Path(path)
        s = cls(path=path)
        if path.exists():
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn tail line must not kill the session
                s.entries.append(Entry(**d))
        return s

    # ---- projection: what the model actually sees -----------------------

    def cut(self) -> tuple[int, int]:
        """(entry index, block index) of the newest compaction, or (0, 0)."""
        for e in reversed(self.entries):
            i = e.compaction
            if i >= 0:
                return e.seq, i
        return 0, 0

    def messages(self) -> list[dict[str, Any]]:
        """Derive the wire array. Never mutates; safe to call any time."""
        seq, blk = self.cut()
        out = []
        for e in self.entries[seq:]:
            content = e.content[blk:] if e.seq == seq else e.content
            if content:
                out.append({"role": e.role, "content": content})
        return out

    # ---- accounting -----------------------------------------------------

    def tokens(self, msgs: list[dict[str, Any]] | None = None) -> int:
        msgs = self.messages() if msgs is None else msgs
        return sum(len(json.dumps(m, default=str)) for m in msgs) // 4

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self.entries),
            "stored_tokens": sum(len(json.dumps(e.content, default=str)) for e in self.entries) // 4,
            "context_tokens": self.tokens(),
            "compactions": sum(1 for e in self.entries if e.compaction >= 0),
        }

    # ---- compaction -----------------------------------------------------

    def context_management(self, trigger: int = 150_000, pause: bool = False,
                           instructions: str | None = None) -> dict[str, Any]:
        """The request knob. Server does the work; we just keep appending."""
        if trigger < MIN_TRIGGER:
            raise ValueError(f"trigger must be >= {MIN_TRIGGER}")
        edit: dict[str, Any] = {"type": STRATEGY, "trigger": {"type": "input_tokens", "value": trigger}}
        if pause:
            edit["pause_after_compaction"] = True
        if instructions:
            edit["instructions"] = instructions
        return {"edits": [edit]}

    def should_compact(self, trigger: int = 150_000) -> bool:
        return self.tokens() >= trigger

    def compact(self, summarize: Callable[[list[dict[str, Any]]], str]) -> Entry:
        """Local stand-in for the server strategy.

        Appends an assistant entry whose first block is a compaction block.
        Identical shape to the server's, so `messages()` cannot tell the
        difference — and nothing earlier is touched.
        """
        summary = summarize(self.messages())
        return self.append("assistant", [{"type": "compaction", "content": summary}])

    def absorb(self, response_content: Iterable[dict[str, Any]]) -> Entry:
        """Append a real API response verbatim, compaction block and all."""
        return self.append("assistant", list(response_content))


DEFAULT_INSTRUCTIONS = (
    "You have written a partial transcript for the initial task above. Please write a "
    "summary of the transcript. The purpose of this summary is to provide continuity so "
    "you can continue to make progress towards solving the task in a future context, "
    "where the raw history above may not be accessible and will be replaced with this "
    "summary. Write down anything that would be helpful, including the state, next steps, "
    "learnings etc. You must wrap your summary in a <summary></summary> block."
)
