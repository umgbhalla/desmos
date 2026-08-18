"""Saved plans: durable work state that remembers where it came from.

A todo is a line of text. A plan is a body of reasoning that has a source, a
set of steps, and a record of every revision it went through.

Provenance is the point. A plan captured from a transcript message stores the
message index and a digest of the text captured, so a later reader can ask
whether the source is still there and still says the same thing. After a
server-side fold the index may point at different text, or at nothing at all;
verify() says which, instead of pretending the citation still holds.

Storage is an append-only JSONL of revisions under the state directory. The
latest revision of a plan_id wins and nothing is ever rewritten in place, so
"never falsify the record" is a property of the file format rather than an API
convention -- which is the exact defect the seat audit found in seat_events,
where append-only was enforced only by everyone agreeing to behave.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from desmos.state.persist import state_file
from desmos.kernel.types import World

PLANS_SUBDIR = "plans"
PLANS_FILENAME = "plans.jsonl"
STATUSES = ("draft", "active", "blocked", "done", "dropped")
STEP_STATUSES = ("todo", "doing", "done", "dropped")
STEP_MARKS = {"todo": " ", "doing": ">", "done": "x", "dropped": "-"}
MAX_BODY = 20000
MAX_RENDER = 4000
TITLE_LIMIT = 140


def plans_root(world: World) -> Path:
    return state_file(world).parent / PLANS_SUBDIR


def plans_path(world: World) -> Path:
    return plans_root(world) / PLANS_FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _new_id(seed: str) -> str:
    return hashlib.sha256(f"{seed}{time.time_ns()}".encode("utf-8")).hexdigest()[:8]


def _gen(world: World) -> int:
    try:
        return int(getattr(world, "generation", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _clean(text: str, limit: int = TITLE_LIMIT) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = text.replace("`", "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


# ---------------------------------------------------------------- storage

def revisions(world: World) -> list[dict[str, Any]]:
    """Every revision ever appended, oldest first. A torn final line is
    skipped rather than allowed to poison the whole file."""
    path = plans_path(world)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("plan_id"):
            out.append(rec)
    return out


def _append(world: World, rec: dict[str, Any]) -> dict[str, Any]:
    path = plans_path(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def latest(world: World) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for rec in revisions(world):
        out[rec["plan_id"]] = rec
    return out


def history(world: World, plan_id: str) -> list[dict[str, Any]]:
    return [r for r in revisions(world) if r["plan_id"] == plan_id]


def read(world: World, plan_id: str) -> dict[str, Any]:
    rec = latest(world).get(plan_id)
    if rec is None:
        raise KeyError(f"no plan {plan_id}")
    return rec


# ------------------------------------------------------------ provenance

def message_text(world: World, index: int) -> str:
    """The speech of one transcript message.

    Only text blocks. Thinking is not speech, is not replayed to later
    models, and must not be laundered into a saved plan as if it were.
    """
    msgs = list(world.messages)
    if index < 0:
        index += len(msgs)
    if not 0 <= index < len(msgs):
        raise IndexError(f"message {index} out of range (0..{len(msgs) - 1})")
    content = msgs[index].get("content")
    if isinstance(content, str):
        return content
    parts = [
        block.get("text", "")
        for block in (content or [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p)


_ORDERED = re.compile(r"^\s{0,3}(\d+)[.)]\s+(.+?)\s*$")


def steps_from_text(text: str) -> list[str]:
    """The longest well-formed ordered list in a markdown body.

    A plan written as prose usually ends in a numbered list of what to do.
    Runs must start at 1 and increment, so a stray "2024." or a table row
    cannot masquerade as a step, and the longest run wins when a document
    contains several lists.
    """
    runs: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        m = _ORDERED.match(line)
        if not m:
            continue
        n, item = int(m.group(1)), m.group(2)
        if n == len(cur) + 1:
            cur.append(item)
            continue
        if cur:
            runs.append(cur)
        cur = [item] if n == 1 else []
    if cur:
        runs.append(cur)
    return max(runs, key=len) if runs else []


def verify(world: World, plan_id: str) -> str:
    rec = read(world, plan_id)
    src = rec.get("source")
    if not src or src.get("kind") != "message":
        return f"{plan_id}: no message source, nothing to verify"
    try:
        text = message_text(world, int(src["index"]))
    except (IndexError, KeyError, TypeError, ValueError):
        return (
            f"{plan_id}: source message {src.get('index')} is gone "
            f"(transcript is now {len(world.messages)} messages) -- "
            f"the saved body is the only copy"
        )
    if _digest(text) == src.get("digest"):
        return f"{plan_id}: source message {src['index']} intact ({src.get('chars')} chars)"
    return (
        f"{plan_id}: source message {src['index']} CHANGED since capture "
        f"(likely a fold) -- the saved body is the only copy"
    )


# --------------------------------------------------------------- mutation

def create(
    world: World,
    title: str,
    body: str = "",
    *,
    source_index: int | None = None,
    steps: list[str] | None = None,
    status: str = "draft",
) -> dict[str, Any]:
    source = None
    if source_index is not None:
        text = message_text(world, int(source_index))
        source = {
            "kind": "message",
            "index": int(source_index),
            "digest": _digest(text),
            "chars": len(text),
            "captured_at": _now(),
        }
        if not body:
            body = text
        if steps is None:
            steps = steps_from_text(text)
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    rec = {
        "plan_id": _new_id(title),
        "rev": 1,
        "at": _now(),
        "title": _clean(title) or "untitled",
        "status": status,
        "body": body[:MAX_BODY],
        "source": source,
        "steps": [
            {"step_id": i, "title": _clean(t), "status": "todo", "note": ""}
            for i, t in enumerate(steps or [], 1)
        ],
        "generation": _gen(world),
    }
    return _append(world, rec)


_MUTABLE = ("title", "status", "body", "steps", "source", "blocked")


def revise(world: World, plan_id: str, **fields: Any) -> dict[str, Any]:
    cur = read(world, plan_id)
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    rec = dict(cur)
    rec.update({k: v for k, v in fields.items() if k in _MUTABLE})
    rec["rev"] = int(cur.get("rev", 1)) + 1
    rec["at"] = _now()
    rec["generation"] = _gen(world)
    return _append(world, rec)


def add_steps(world: World, plan_id: str, titles: list[str]) -> dict[str, Any]:
    steps = list(read(world, plan_id).get("steps") or [])
    nxt = max([s["step_id"] for s in steps], default=0) + 1
    for i, t in enumerate(titles, nxt):
        steps.append({"step_id": i, "title": _clean(t), "status": "todo", "note": ""})
    return revise(world, plan_id, steps=steps)


def set_step(
    world: World, plan_id: str, step_id: int, status: str, note: str = ""
) -> dict[str, Any]:
    if status not in STEP_STATUSES:
        raise ValueError(f"step status must be one of {STEP_STATUSES}")
    steps = [dict(s) for s in read(world, plan_id).get("steps") or []]
    for s in steps:
        if s["step_id"] == int(step_id):
            s["status"] = status
            if note:
                s["note"] = note
            break
    else:
        raise KeyError(f"plan {plan_id} has no step {step_id}")
    return revise(world, plan_id, steps=steps)


# --------------------------------------------------------------- rendering

def render(rec: dict[str, Any], full: bool = False) -> str:
    steps = rec.get("steps") or []
    done = sum(1 for s in steps if s.get("status") == "done")
    head = (
        f"{rec['plan_id']}  {rec['status']}  rev{rec['rev']}  "
        f"[{done}/{len(steps)}]  {rec['title']}"
    )
    lines = [head]
    if rec.get("blocked"):
        lines.append(f"  blocked: {rec['blocked']}")
    src = rec.get("source")
    if src:
        lines.append(
            f"  from message {src['index']} ({src['chars']} chars, digest {src['digest']})"
        )
    for s in steps:
        mark = STEP_MARKS.get(s.get("status", "todo"), "?")
        note = f"   -- {s['note']}" if s.get("note") else ""
        lines.append(f"  [{mark}] {s['step_id']}. {s['title']}{note}")
    if full and rec.get("body"):
        lines.append("")
        lines.append(rec["body"][:MAX_RENDER])
    return "\n".join(lines)


def listing(world: World, status: str = "") -> str:
    recs = [
        r
        for r in latest(world).values()
        if not status or r.get("status") == status
    ]
    if not recs:
        return "no plans"
    recs.sort(key=lambda r: r.get("at", ""))
    return "\n\n".join(render(r) for r in recs)


# ------------------------------------------------------------- syscall op

USAGE = (
    "plan ops (first line is the command):\n"
    "  (empty)                list every plan\n"
    "  list [status]          list plans, optionally filtered\n"
    "  new TITLE              create a draft; remaining lines become the body\n"
    "  from N [| TITLE]       capture transcript message N as the body and\n"
    "                         lift its numbered list into steps\n"
    "  show ID                full render including the body\n"
    "  step ID + TITLE        append a step\n"
    "  step ID x N [note]     mark step N done\n"
    "  step ID > N [note]     mark step N in progress\n"
    "  step ID - N [note]     drop step N\n"
    "  status ID STATUS       draft | active | blocked | done | dropped\n"
    "  block ID REASON        pause the plan and stop the stop-reminders\n"
    "  unblock ID             clear the block and go back to active\n"
    "  verify ID              is the source message still what was captured\n"
    "  history ID             every revision of this plan"
)


def handle_plan(world: World, body: str = "", **attrs: Any) -> str:
    body = (body or "").strip()
    if not body:
        return listing(world)
    head, _, rest = body.partition("\n")
    cmd, _, arg = head.strip().partition(" ")
    cmd, arg, rest = cmd.lower(), arg.strip(), rest.strip("\n")

    if cmd in ("help", "?"):
        return USAGE

    if cmd == "list":
        return listing(world, arg)

    if cmd == "new":
        if not arg:
            return "new needs a title"
        return render(create(world, arg, rest), full=bool(rest))

    if cmd == "from":
        spec, _, title = arg.partition("|")
        try:
            index = int(spec.strip())
        except ValueError:
            return f"from needs a message index, got {spec.strip()!r}"
        try:
            rec = create(
                world,
                title.strip() or f"captured from message {index}",
                source_index=index,
            )
        except IndexError as exc:
            return str(exc)
        return render(rec)

    if cmd == "show":
        try:
            return render(read(world, arg), full=True)
        except KeyError as exc:
            return str(exc)

    if cmd == "verify":
        try:
            return verify(world, arg)
        except KeyError as exc:
            return str(exc)

    if cmd == "history":
        try:
            recs = history(world, arg)
        except KeyError as exc:
            return str(exc)
        if not recs:
            return f"no plan {arg}"
        return "\n".join(
            f"rev{r['rev']}  {r['at']}  {r['status']}  gen{r.get('generation', 0)}  "
            f"{len(r.get('steps') or [])} steps"
            for r in recs
        )

    if cmd == "block":
        pid, _, reason = arg.partition(" ")
        try:
            return render(block(world, pid.strip(), reason.strip() or rest))
        except (KeyError, ValueError) as exc:
            return str(exc)

    if cmd == "unblock":
        try:
            return render(unblock(world, arg.strip()))
        except (KeyError, ValueError) as exc:
            return str(exc)

    if cmd == "status":
        pid, _, value = arg.partition(" ")
        try:
            return render(revise(world, pid.strip(), status=value.strip()))
        except (KeyError, ValueError) as exc:
            return str(exc)

    if cmd == "step":
        pid, _, tail = arg.partition(" ")
        verb, _, payload = tail.strip().partition(" ")
        payload = payload.strip()
        try:
            if verb == "+":
                return render(add_steps(world, pid.strip(), [payload]))
            marks = {"x": "done", ">": "doing", "-": "dropped", "o": "todo"}
            if verb in marks:
                num, _, note = payload.partition(" ")
                return render(
                    set_step(world, pid.strip(), int(num), marks[verb], note.strip())
                )
        except (KeyError, ValueError) as exc:
            return str(exc)
        return f"unknown step verb {verb!r}; try + x > - o"

    return f"unknown plan command {cmd!r}\n\n{USAGE}"


# ------------------------------------------------------------------- rail

#: How many consecutive reminders one stopped step may take before the turn
#: goes back to the user anyway. A rail that never yields is a hang.
NUDGE_LIMIT = 4


def nudge_limit() -> int:
    """The cap, overridable per run without touching the code."""
    raw = os.environ.get("DESMOS_PLAN_NUDGES")
    try:
        value = int(raw) if raw else NUDGE_LIMIT
    except ValueError:
        return NUDGE_LIMIT
    return max(0, value)


def active(world: World) -> dict[str, Any] | None:
    """The plan the reminders speak for: the newest one marked active."""
    recs = [r for r in latest(world).values() if r.get("status") == "active"]
    if not recs:
        return None
    recs.sort(key=lambda r: (r.get("at", ""), r.get("rev", 0)))
    return recs[-1]


def current_step(rec: dict[str, Any]) -> dict[str, Any] | None:
    """What to work on: whatever is in progress, else the first open step."""
    steps = rec.get("steps") or []
    for wanted in ("doing", "todo"):
        for step in steps:
            if step.get("status") == wanted:
                return step
    return None


def nudge(world: World) -> str | None:
    """What to say to a step that stopped while a plan is still open.

    A stop is not a decision. The loop ends a step the moment the model writes
    no syscall, which is right when the work is done and wrong when the model
    merely narrated. So when a plan is active with open steps, the stop is
    answered with the plan instead of with the turn, and the only ways out are
    finishing the steps or blocking the plan with a reason someone can read.
    """
    try:
        rec = active(world)
    except Exception:  # noqa: BLE001 -- a broken plan file cannot end a step
        return None
    if not rec:
        return None
    step = current_step(rec)
    if step is None:
        return None
    steps = rec.get("steps") or []
    left = sum(1 for s in steps if s.get("status") in ("todo", "doing"))
    return (
        f"[plan {rec['plan_id']} is still active: {rec['title']}]\n"
        f"next step {step['step_id']}: {step['title']}  "
        f"({left} of {len(steps)} open)\n"
        "Carry on with that step now. If it truly cannot proceed, block the "
        f"plan with the reason -- knowledge op=plan, body: block {rec['plan_id']} "
        "<why> -- which stops these reminders and hands the turn back."
    )


def block(world: World, plan_id: str, reason: str) -> dict[str, Any]:
    """Pause a plan with a reason. The reason is the whole point."""
    text = _clean(str(reason or "").strip(), 300)
    if not text:
        raise ValueError("block needs a reason")
    return revise(world, plan_id, status="blocked", blocked=text)


def unblock(world: World, plan_id: str) -> dict[str, Any]:
    """Clear the block and go back to active."""
    return revise(world, plan_id, status="active", blocked="")
