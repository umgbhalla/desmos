# Seats: declaration of fields, lifecycle, and reset behaviour

Status: declaration for review, no schema. This is the B2 gate document: the
constitution (docs/constitution.md, section 3 B2) forbids seat storage before
the seat's fields, lifecycle and reset behaviour are written down and
reviewed. A premature `seats` table was retired by `_drop_seat_scaffold`
(desmos/state/persist.py:668); nothing lands in SQL until this is accepted.

Operator decision, already made: **seats are for user-facing agents only.**
Birth stays gated by the operator; sibling and child workers run seatless and
report through their parent (ARES 3, todo 40). B3 stays airtight.

## 1. Fields of a seat row

Each field is justified against a B-invariant. No SQL here; names only.

- **id** -- stable opaque identifier, never reused, never rewritten.
  *Justifies B1:* accumulation needs one durable key that outlives every
  session; a session id cannot be that key.
- **workspace** -- the workspace the seat works in (FK to the existing
  workspaces row). *Justifies B1:* a seat is "the enduring party that works
  in a workspace" (constitution section 2); accumulation is scoped to where
  the work happened, and a restart in the same repo must find the same party.
- **charter** -- what this seat is for, in prose, set at birth and amendable
  only append-style (amendments recorded, original kept). *Justifies B1 and
  C3:* wake-with-purpose needs a charter to hand the session; B1 says the
  charter hangs off the seat, never off a session.
- **role** -- the capability role the seat's sessions run under (today:
  user-facing roles only). *Justifies B3:* the role is how the seatless rule
  is checked -- worker roles (sibling/child) are not valid seat roles, so a
  seat row can never describe a fork.
- **created** -- birth timestamp plus the generation at birth. *Justifies
  A1/B2:* birth is an event in the record, not a mutable attribute; it also
  anchors lineage ordering.
- **retired** -- nullable tombstone timestamp and reason. *Justifies A1:*
  retirement writes a tombstone; it never deletes (same rule as D3 for
  tools). A seat with `retired` set accepts no new session bindings.

What accumulation keys off the seat id (B1), each stored where it already
lives, re-keyed by seat rather than by session:

- **notes** -- currently workspace-scoped; become seat-scoped so a restart
  produces a party with its own notes, not an anonymous pile.
- **memory** -- records in `.desmos/memories/` gain a seat attribution;
  `remember()` continues to refuse in non-persistent (seatless) worlds.
- **lineage** -- the append-only model-binding history (constitution T2: a
  seat survives a model change; a switch appends a binding, never edits one).
- **recognition** -- standing earned across sessions (accepted items,
  tombstoned tools, handoffs honoured), derived from `calls`/`events`/work
  records, attributed to the seat per D2.

## 2. Lifecycle

**Birth.** Gated by the operator, and only the operator: a seat is created by
an explicit operator action (a reviewed command or TUI op), never by a
running agent, never as a side effect of attach. The gate is the enforcement
point for the operator decision above: only user-facing roles are accepted at
birth. Birth writes the seat row and one birth event (A1: the event is
committed, not rewritable).

**Wake / session binding (C3).** When a session attaches in a workspace that
has an active seat, the session binds to that seat at wake: the session row
records the seat id, and the wake report carries the seat's charter, role,
and current work item. A session that wakes with no charter, role and current
work item is a defect, not a neutral state (C3). One live binding per seat at
a time; a second attach to a bound seat is refused loudly, not queued
silently.

**Session end (C1/C2).** Every bound session ends in exactly one of three
recorded states: completed, handed off, or interrupted (C1). A handoff writes
a note -- objective, what is established, what is open, the exact next action
-- keyed to the seat so the next incarnation reads it at wake (C2). An
interruption is a recorded state visible at the next wake, not an absence:
the next binding of the seat reports it.

**Retirement.** A seat retires by tombstone: `retired` is set with a reason,
a retirement event is appended, and every accumulated record stays exactly
where it is (A1: never delete). A retired seat refuses new bindings; its
history remains reachable by content (A3).

## 3. Reset behaviour

Aligned with docs/identity.md's reset ladder, mildest to hardest:

- **reload_sdk** -- must not touch seat state. Pure reimport.
- **reset (TUI reset op, `reset_transcript`)** -- must not touch seat state.
  It drops messages and prior turns -- session-owned state. The seat, its
  charter, notes, memory, lineage and recognition survive: sessions are days,
  seats are people.
- **harness rollback (rollback n)** -- must not touch the seat row, lineage,
  or memory. Rollback swaps notes/tools/prior from a generation snapshot
  (identity.md: "rollback is narrow"). Because notes become seat-keyed,
  snapshots carry the seat's notes and rollback replaces them -- that is the
  point of rollback -- but it never rewinds identity: the seat row, birth,
  bindings and tombstones are append-only (A1).
- **process restart** -- must not lose seat state; `load` rebuilds the
  binding from the db, and the new session binds to the same seat (B1: a
  restart must not produce a party with no history).
- **fork (child world, `persist=False`, `state_path=None`)** -- touches
  nothing, by construction. A fork loads no seat, binds no seat, and cannot
  write the parent's seat, notes, memory, or generation (B3).
- **`rm -rf .desmos`** -- destroys seat state along with the rest of the
  repo-local store. This is the one reset that kills a seat, and it must be
  loud (A2): the next attach reports that seat history before T is gone, not
  absent-as-if-never-there. Nothing under `~/.desmos` holds seat state, so
  no ghost seat survives to contradict the record.

## 4. Seatless siblings

**Rule.** Only user-facing agents hold seats. Sibling and child workers --
every subagent World -- run seatless: no seat row, no seat binding on their
run, no writes to any seat-keyed accumulation. They report results through
their parent, and only the parent's seat accumulates.

**Enforcement point.** The same boundary that already makes B3 airtight:
child worlds are created `persist=False, state_path=None`
(desmos/agents/subagent.py `_child_world`), so they load nothing from the db
and write nothing back -- `remember()` already refuses there. Seat binding
happens only in the attach path in `desmos/state/persist.py`, which children
never execute. The birth gate (section 2) is the second lock: worker roles
are not accepted at seat creation, so a seat that could describe a fork
cannot exist.

## 5. Checks, constitution style

Each rule above is falsifiable:

- **Fields/B1:** restart a workspace with a seated seat; the new session's
  wake report carries the seat's charter, notes and memory. A wake with no
  history for an existing seat falsifies B1.
- **Birth gate:** attempt seat creation from a running agent's tool surface
  and from a worker role; both must refuse. A seat row whose birth event is
  not an operator action falsifies the gate.
- **Binding/C3:** attach twice to one seat concurrently; the second attach
  must be refused with a recorded event, not silently queued.
- **End states/C1:** kill a bound session mid-turn; the next wake must
  report "interrupted", not nothing.
- **Rollback narrowness:** roll back a generation; the seat row, lineage and
  memory records must be byte-identical before and after; only notes/tools/
  prior may differ.
- **Fork anonymity/B3:** spawn a child, have it attempt every seat-touching
  write; zero rows keyed by the parent's seat may change. Grep-level check:
  no seat write path is reachable from a `persist=False` world.
- **Tombstone/A1:** retire a seat; no DELETE against the seat row or its
  accumulation may occur, and its history must remain queryable (A3).

DONE when reviewed; only then may schema land (B2).
