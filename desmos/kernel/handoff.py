"""Ask for the handoff while the turns that hold it are still on the wire.

A server-side fold is not something the model gets to prepare for: the API
folds earlier turns and hands back a summary someone else wrote. Whatever the
next context needs -- the objective, what is established, the exact next
action -- has to be written down before that, not after.

So watch the fill. The last response's usage is the real size of the prompt
that produced it (fresh input plus both cache tiers), and the window is in
prices.json. Crossing SOFT injects a named block, which lands on the *next*
request -- the one before the fold. Dropping back under retires it.
"""

from __future__ import annotations

import os
from typing import Any

from desmos.kernel import catalog, prices
from desmos.kernel.types import World

def _share(name: str, default: float) -> float:
    """A threshold, overridable by env so a demo or a small model can move it."""
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if 0.0 < value <= 1.0 else default


#: Fraction of the window that opens the handoff rail.
SOFT = _share("DESMOS_HANDOFF_SOFT", 0.75)

#: Hysteresis: retire a little below SOFT so a flat context cannot flap.
CLEAR = _share("DESMOS_HANDOFF_CLEAR", SOFT - 0.05)

#: Injection name. Idempotent by name -- re-injecting refreshes the number.
BLOCK = "handoff"

#: Note key for agent-authored anchors: facts that must survive a fold. The
#: note rides the volatile system tail (catalog.VOLATILE_NOTES), which is
#: recomposed every request from world.notes -- a fold rewrites messages, not
#: notes, so anchors cross it verbatim.
ANCHORS = "anchors"

#: Injection name for the one-shot "write your anchors" nudge at SOFT.
NUDGE = "anchor-nudge"

#: Bounds. Anchors are a handoff, not a transcript: a few lines, each short.
MAX_ANCHORS = 8
ANCHOR_CHARS = 200


def set_anchors(world: World, body: str) -> str:
    """Replace the anchor set with the lines of body, bounded; empty clears.

    Whole-set replacement, not append: the agent re-states what still matters
    each time, which is exactly the discipline a fold demands.
    """
    lines = [line.strip()[:ANCHOR_CHARS] for line in str(body).splitlines() if line.strip()]
    dropped = max(0, len(lines) - MAX_ANCHORS)
    lines = lines[:MAX_ANCHORS]
    if lines:
        world.notes[ANCHORS] = "\n".join(lines)
    else:
        world.notes.pop(ANCHORS, None)
    if not lines:
        return "anchors cleared"
    tail = f" ({dropped} over the cap of {MAX_ANCHORS} dropped)" if dropped else ""
    return (
        f"anchors: {len(lines)} line(s) pinned{tail}; they ride the volatile "
        "system block every turn and survive a fold verbatim"
    )


def prompt_tokens(usage: dict[str, Any] | None) -> int:
    """How large the prompt actually was: fresh input plus both cache tiers.

    Output tokens are not in the next prompt as output -- they come back as
    assistant content, which the next request bills as input. Counting them
    here would double them once the turn is replayed.
    """
    if not usage:
        return 0
    total = 0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total


def fill(world: World) -> float:
    """Where the last real request sat in the model's window, 0.0 if unknown."""
    log = getattr(world, "log", None) or []
    if not log:
        return 0.0
    size = prompt_tokens(log[-1].get("usage"))
    if size <= 0:
        return 0.0
    ceiling = prices.window(getattr(world, "model", None))
    return size / float(ceiling) if ceiling > 0 else 0.0


def text(share: float) -> str:
    """The block itself. The number is in it because "soon" is not actionable."""
    return (
        f"Context is at {share * 100:.0f}% of this model's window. The fold is "
        "close, and it takes the turns your handoff would have been written "
        "from. Before the next syscall, write the handoff where it survives: "
        "the objective, what is established with its evidence, what is still "
        "open, and the exact next action. A note, a memory record or a file "
        "-- never only speech. Then carry on with the work."
    )


def watch(world: World) -> bool:
    """Install or retire the handoff block for the fill right now.

    Returns whether the block is installed after this call. Injections land in
    the uncached tail, so this never invalidates the cached prefix.
    """
    share = fill(world)
    if share >= SOFT:
        catalog.inject(world, BLOCK, text(share), turns=0)
        # One nudge per climb, not one per turn: turns=1 makes it fall out
        # after a single render, and the flag stops re-arming until the fill
        # drops back under CLEAR.
        if not getattr(world, "anchor_nudged", False):
            world.anchor_nudged = True
            catalog.inject(
                world,
                NUDGE,
                (
                    f"Context is at {share * 100:.0f}%: write or refresh your anchors NOW "
                    f'(<knowledge op="anchor">one fact per line</knowledge>, max {MAX_ANCHORS} '
                    f"lines x {ANCHOR_CHARS} chars) -- they ride the system tail and survive "
                    "the coming fold verbatim."
                ),
                turns=1,
            )
        return True
    if share < CLEAR:
        catalog.retire(world, BLOCK)
        world.anchor_nudged = False
        return False
    return BLOCK in getattr(world, "injections", {})


#: Injection name for the turn that wakes up after a fold.
FOLD = "fold"


def consent_text(kept: int, summary: str) -> str:
    """What the turn after a fold is told before it does anything else.

    The model that wakes up here cannot read what was folded. It can read a
    summary someone else wrote, and it has no way to know what that summary
    dropped -- so the first thing it does is say what it believes and let the
    person who does remember correct it.
    """
    size = f"{len(summary)} chars" if summary else "no summary text"
    return (
        f"A fold just happened: {kept} messages remain, and the turns before "
        f"them are gone from the wire ({size}). You cannot read what was "
        "folded. Before any irreversible step -- a commit, a push, a delete, a "
        "release -- restate from the summary, in your own words: the "
        "objective, what is established and by what evidence, what is still "
        "open, and the exact next action. Then ask the user to confirm or "
        "correct it. Reading a syscall result or a file to re-establish a fact "
        "is fine and does not need permission."
    )


def after_fold(world: World, kept: int, summary: str = "") -> str:
    """Arm the consent turn and drop the pre-fold rail it replaces."""
    catalog.retire(world, BLOCK)
    return catalog.inject(world, FOLD, consent_text(kept, summary), turns=1)
