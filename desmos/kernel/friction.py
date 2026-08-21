"""Shadow observer: friction counters on the World, nudging with zero API calls.

The harness quietly counts signals of friction where dispatch already stands
-- consecutive failures per tag, near-identical <python> bodies, dispatch
rejections, turns since the last recall/memory op -- and, past a threshold,
appends a single line to the syscall result, the same channel
desmos/state/plan.py todo_nudge already uses. No model call, no new thread,
no polling, no persistence: all state is one in-memory dict on the World and
resets with the process. Each nudge fires at most once per threshold
crossing, with a cooldown so it never becomes noise.
"""

from __future__ import annotations

from collections import deque
from typing import Any

#: Consecutive tracebacks on one tag before the observe/diag nudge.
FAIL_THRESHOLD = 2
#: Near-identical <python> bodies (within the recent window) before the
#: grow-a-tool nudge (constitution D3: the census exists to be grown into).
REPEAT_THRESHOLD = 3
#: History-adjacent dispatches with no recall before the recall nudge.
RECALL_THRESHOLD = 8
#: Dispatches on this world before the same nudge kind may fire again.
COOLDOWN = 10
#: How many recent python-body hashes count toward "near-identical".
WINDOW = 8

_TRACEBACK = "Traceback (most recent call last)"
_HISTORY_WORDS = ("history", "memory", "recall", "transcript", "prior session")
_HISTORY_TAGS = frozenset({"python", "bash", "edit"})


def _state(world: Any) -> dict[str, Any]:
    st = getattr(world, "_friction", None)
    if st is None:
        st = {
            "turn": 0,
            "fails": {},  # tag -> consecutive traceback results
            "bodies": deque(maxlen=WINDOW),  # normalized python-body hashes
            "rejections": 0,  # scope refusals + unknown tags, for observe
            "since_recall": 0,  # dispatches since the last recall/memory op
            "history_work": 0,  # history-adjacent dispatches since that op
            "fired": {},  # nudge kind -> turn it last fired (cooldown)
        }
        world._friction = st  # in-memory only: never persisted, no schema
    return st


def _ready(st: dict[str, Any], kind: str, turn: int) -> bool:
    """One nudge per threshold crossing, silenced for COOLDOWN dispatches."""
    last = st["fired"].get(kind)
    if last is not None and turn - last < COOLDOWN:
        return False
    st["fired"][kind] = turn
    return True


def observe(world: Any, block: Any, result: str) -> str:
    """Bump the counters for one dispatch; maybe append one nudge line."""
    if not isinstance(result, str):
        return result
    st = _state(world)
    st["turn"] += 1
    turn = st["turn"]
    tag = block.tag
    nudge: str | None = None

    # 1) Consecutive failures on one tag: the second traceback in a row says
    # retrying blind is not working -- look at the error before the next try.
    if _TRACEBACK in result:
        n = st["fails"].get(tag, 0) + 1
        st["fails"][tag] = n
        if n >= FAIL_THRESHOLD and _ready(st, f"fail:{tag}", turn):
            st["fails"][tag] = 0  # a fresh crossing is needed to fire again
            nudge = (
                f"[friction] <{tag}> has failed {n}x in a row -- inspect before "
                "retrying: diag.error() holds the frames, or observe state "
                "with a smaller probe."
            )
    else:
        st["fails"][tag] = 0

    # Rejections are counted, not nudged: they already answer in prose.
    if "outside this agent's scope" in result or result.startswith("unknown tag <"):
        st["rejections"] += 1

    # 2) The third near-identical <python> body is a tool asking to be grown
    # (D3: the census records grown-tool usage). One cheap normalized hash.
    if tag == "python":
        digest = hash(" ".join(block.body.split()))
        st["bodies"].append(digest)
        if (
            nudge is None
            and list(st["bodies"]).count(digest) >= REPEAT_THRESHOLD
            and _ready(st, "grow", turn)
        ):
            st["bodies"].clear()  # the next crossing needs three fresh repeats
            nudge = (
                "[friction] third near-identical <python> body -- consider "
                "growing a tool with <register> so this becomes one call "
                "(refine op=census tracks grown-tool usage)."
            )

    # 3) A long stretch of history-touching work that never once asked the
    # store: recall is cheaper than re-deriving what a prior session knew.
    if tag in {"recall", "memory"} or (block.attrs.get("op") or "").lower() == "recall":
        st["since_recall"] = 0
        st["history_work"] = 0
    else:
        st["since_recall"] += 1
        if tag in _HISTORY_TAGS:
            lowered = block.body.lower()
            if any(word in lowered for word in _HISTORY_WORDS):
                st["history_work"] += 1
                if (
                    nudge is None
                    and st["history_work"] >= RECALL_THRESHOLD
                    and _ready(st, "recall", turn)
                ):
                    st["history_work"] = 0
                    nudge = (
                        "[friction] a long stretch of history-adjacent work "
                        "with no recall -- <recall>query</recall> may already "
                        "hold the answer."
                    )

    if nudge:
        return result + "\n" + nudge
    return result
