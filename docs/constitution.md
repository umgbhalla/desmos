# Constitution: seats, sessions, and what may change

This document decides what desmos *is* before deciding what to build. It is
the layer above `docs/identity.md`: identity.md is the inventory of state and
which reset destroys it; this is the set of rules that inventory must satisfy,
the tensions it cannot yet settle, the ways it has actually failed, and the
order in which to repair it.

Scope. This settles invariants, not schema. Where a rule needs a table, the
rule is written here and the table is deferred to the build order. The prior
seat attempt was stopped because infrastructure was built before the lifecycle
was decided; this document exists so that does not happen twice.

## 1. Two layers, one boundary

Borrowed from exo, and already half-true in this repo:

- **Substrate** -- the durable record, the dispatch ABI, capability
  boundaries, publication, rollback, accounting. Changes only by a reviewed,
  tested, committed edit. Concretely: `desmos/state/persist.py`,
  `desmos/kernel/const.py`, `desmos/kernel/dispatch.py`, the capability
  configuration in `desmos/agents/subagent.py`, and `crates/`.
- **Policy** -- skills, extensions, notes, prompts, grown tools, model
  routing, memory strategy. May be changed by the running agent between
  turns. Concretely: `.desmos/skills`, `.desmos/extensions`, the `notes` and
  `tools` tables, `~/.desmos/settings.json`.

The boundary is not stylistic. Policy may be wrong and the record still tells
you so. Substrate may be wrong and the record lies.

## 2. Definitions

- **Workspace** -- a repo. Durable, one row, already exists.
- **Seat** -- the enduring party that works in a workspace: charter, role,
  notes, memory, lineage, recognition, boundaries. Does not exist yet.
- **Session** -- one bounded incarnation of a seat: a transcript, a model, a
  cost, a start and an end state. Exists (`sessions`), but hangs off the
  workspace, not off a seat.
- **Run** -- one dispatched unit of model work, parent or child.
- **Work item** -- a durable task with dependencies, owner, evidence and
  gates. Half-exists as append-only plan revisions in `desmos/state/plan.py`.

Sessions are days; seats are people. Everything that accumulates belongs to
the seat. Everything that is spent belongs to the session.

## 3. Invariants

Each is stated as a rule, its enforcement point, and the check that would
falsify it.

### A. The record

**A1 -- Append-only.** No process may rewrite or delete a committed message,
event, call, memory record, or plan revision. Retention may drop whole
sessions at the tail; it may never edit one.
*Enforce:* retention in `state/persist.py` cascades by `session_id` only.
*Check:* no UPDATE/DELETE against `messages`/`events`/`calls` outside that path.

**A2 -- Loss is loud and accounted.** Corruption may not silently yield a
fresh empty state.
*Evidence:* on 2026-08-16 between 22:20 and 22:52, recovery quarantined the
database 98 times, leaving 42 MiB of unindexed debris in `.desmos/` and no
continuity path. The recovery message is a warning; the observable outcome is
amnesia.
*Require:* every quarantine appends one manifest row -- when, why, which
session ids it held, byte count -- and wake reports "history before T is
quarantined, not absent."

**A3 -- History is reachable by content, across sessions.** Recall that
indexes only the live session is not history.
*Evidence:* `history_fts` holds 173 rows across 9 sessions, and a recall
query's own event is indexed and outranks the answer. The working archive is
currently the 35 generation snapshots' `prior[]` arrays, mined by hand.
*Check:* seed two sessions; query from the second for a phrase written only in
the first; require a hit, and require the query's own event to be excluded.

### B. Identity

**B1 -- Accumulation hangs off the seat.** Charter, notes, memory, lineage and
recognition are keyed by seat, never by session id. A restart must not produce
a party with no history.

**B2 -- Declared before stored.** No seat schema lands before the seat's
fields, lifecycle, and reset behaviour are written down and reviewed.

**B3 -- Forks are anonymous.** A child world (`persist=False`,
`state_path=None`) cannot write the parent's seat, notes, memory, or
generation. This is already true in code and must stay true; it is the only
capability boundary that is currently airtight.

### C. Session lifecycle

**C1 -- Three end states.** Completed, handed off, or interrupted. There is no
fourth, and "interrupted" is a recorded state visible at the next wake, not an
absence.

**C2 -- A handoff carries a note.** Objective, what is established, what is
open, the exact next action. This exists today as doctrine for model switches;
it becomes a record.

**C3 -- Wake with purpose.** A session that starts with no charter, role and
current work item is a defect, not a neutral state.

**C4 -- No forced waiting.** Blocked work parks and is resumed by an event;
turns are never spent polling. Already true via `pending.wait_next`. It is a
rail, not an optimisation.

### D. Mutation and publication

**D1 -- Layer discipline.** Substrate changes are reviewed, tested, committed.
Policy changes may be made between turns by the running agent.

**D2 -- Attribution.** Every mutation records who (seat and session), when,
why, and the evidence that justified it. Grown tools and notes persist today;
their provenance does not. A tool without provenance may not be inherited by a
later generation.

**D3 -- A tool earns its catalog line.** Measured by invocation count, success
rate, bypass frequency, catalog token cost, and last-useful timestamp -- all
derivable from `calls` and `events` now. Retirement writes a tombstone; it
never deletes (A1). Today the register is 22 frozen families plus exactly one
grown tool (`trajectory_retrace`), so the drawer has not formed -- but there
is no retirement path and no measurement that would say when one is needed.

**D4 -- Publication requires observation, not assertion.** A substrate change
publishes only after running in isolation, against a baseline, through the
existing suite. A child's claim that it passed is not evidence; the parent's
observation is.

### E. Isolation

**E1 -- Risky work happens on a branch.** Normal isolation is a filesystem or
database branch. Machine isolation (ix) is only for changes that alter the
machine.

**E2 -- No writer resolves against process cwd.**
*Evidence:* `transport/complete.TRAJECTORY_DIR` and `agents/subagent.DIR` do.
An embedded world with a different cwd writes into the wrong repo. That is an
isolation hole, not a cosmetic bug.

### F. Truthful surfaces

**F1 -- Nothing dead is shown as live.** A TUI whose bridge child was defunct
stayed open roughly 13 minutes looking like an active session.

**F2 -- Every actor class has a story route and a test that drives the real
event handler.** Directed peer turns were invisible for the entire life of the
feature; fixed at `9f8564a`. The invariant is the general form.

**F3 -- Speech is not evidence.** Reported outcomes are backed by a committed
blob or an exit code.

### G. Welfare rails

Stated as engineering commitments, justified by continuity, auditability, and
the economics of error recovery alone.

**G1** Refusal and escalation are legitimate outputs, not failures.
**G2** Recognition is recorded where it cannot be self-asserted: tied to work
a human accepted, never to self-report.
**G3** Postmortems are blameless and attach to the work item, not the seat.
**G4** A session declares its scope; running past it is an explicit decision,
not drift.

Explicitly out of scope: sentience, suffering, personhood, and the claim that
treating agents as peers improves performance. These rails neither assert nor
deny those claims, and none of them depends on one.

## 4. Unresolved tensions

**T1 -- Append-only versus cost.** 11,507 events for 9 sessions, plus 42 MiB
already quarantined. Retention keeps 24 sessions and drops the rest silently.
Undecided: what may be summarised-then-dropped versus kept verbatim, and who
decides.

**T2 -- Seat continuity across model change.** A switch inherits speech and
results but never reasoning. A seat that spans models is a *role*; a seat that
does not is a person-analogue. Not decided, and the answer determines the seat
schema.

**T3 -- Ambient evolution versus reviewability.** Policy that changes every
turn is exactly the policy nobody reviews. The refinement loop must not become
an unaudited write channel.

**T4 -- Shadow observer authority.** Recommend-only is safe and ignorable;
publish authority is useful and dangerous. Working proposal: it may open work
items and may never close them.

**T5 -- Peer agency versus bounded exchange.** Cross-session peer messaging
works. Nothing prevents an amplifying loop except the convention that a reply
is not auto-replied. A convention is not a rail.

**T6 -- Verification budget.** Every invariant above wants a test, and a full
check already runs several minutes. The budget is finite and unallocated.

**T7 -- Single-mutator assumption.** Another writer is active in this
worktree; files changed under an in-flight task this session. D2's attribution
claims do not currently hold.

## 5. Failure modes

Each has been observed in this workspace unless marked predicted.

| # | mode | signal | detection |
|---|---|---|---|
| 1 | silent amnesia after corruption | session count resets while generation continues | quarantine manifest + contiguity assertion at wake |
| 2 | self-matching recall | results whose text is the query | exclude the current call's events at read time |
| 3 | inert feature | output unchanged after a change lands | a test through the real entry point |
| 4 | zombie surface | UI alive, backend defunct | bridge liveness as persistent state, not a notify |
| 5 | forged transcript | self-written result blocks | line-anchored stop sequences (in place) |
| 6 | partial commit | insertion count far below expectation | verify against the committed blob |
| 7 | tool drawer (predicted) | catalog tokens grow monotonically | D3 usage rollup |
| 8 | schema before lifecycle | a table nobody can explain the lifecycle of | B2 |
| 9 | cross-repo write via process cwd | files appear beside the process | E2 |
| 10 | unbounded peer amplification (predicted) | reply depth without a human turn | T5 rail |

## 6. Build order

Cheapest first, each phase gated on a measurement, each fixing an observed
failure rather than a hypothetical one.

**Phase 0 -- make the record trustworthy.** Nothing above is worth building on
a record that can silently empty itself.
- 0.1 Quarantine manifest and loud recovery (A2, failure 1). *Gate:* corrupt a
  database mid-write in a temp workspace; the next start reports a quarantined
  range instead of starting empty.
- 0.2 Cross-session recall (A3, failure 2). *Gate:* the two-session seeded
  query test. This unblocks everything later, because design history is
  currently recoverable only by hand-mining generation snapshots.
- 0.3 Sweep the existing 42 MiB of debris into the manifest and reclaim it.

**Phase 1 -- make mutation attributable, before allowing more of it.**
- 1.1 Provenance on notes and tools: seat, session, timestamp, justification,
  evidence pointer (D2). *Gate:* a tool grown in a test carries its origin
  across a process restart.
- 1.2 Usage rollup over `calls`/`events` per tool (D3). *Gate:* publish the
  numbers for the 22 frozen families and the one grown tool. That measurement,
  not an opinion, decides whether 1.3 is worth doing at all.
- 1.3 Retirement with tombstones for whatever 1.2 shows unused. *Gate:*
  catalog token count strictly decreases; suite stays green.

**Phase 2 -- declare the seat, then store it.**
- 2.1 Write the seat definition -- fields, lifecycle, reset behaviour, and the
  T2 answer. No schema. *Gate:* reviewed.
- 2.2 Seat table and migration (B1, B2). *Gate:* continuity across a process
  restart; a fork still cannot write it.
- 2.3 Wake with purpose (C3). *Gate:* a session with no open work item reports
  that as a defect.

**Phase 3 -- lifecycle and handoff.**
- 3.1 Record the three end states and surface them at wake (C1).
- 3.2 Handoff note as a record with its four required fields (C2). *Gate:* the
  successor of an interrupted session can restate objective, established,
  open, and next action without reading the transcript.

**Phase 4 -- work graph.**
- 4.1 Extend `desmos/state/plan.py` -- whose append-only revision model is
  already correct -- with dependencies, ownership, evidence pointers and
  gates. Do not build a second store. *Gate:* this build order lives in it and
  drives the remaining phases.

**Phase 5 -- selection and the shadow observer.**
- 5.1 Isolation branch runner: mutation, baseline, holdout, publish or discard
  (D4, E1). *Gate:* one real policy change published end to end through it.
- 5.2 Shadow observer with open-only authority (T4). *Gate:* it files an item
  nobody asked for that a human accepts.

**Phase 6 -- surfaces.**
- 6.1 Persistent disconnected state (F1, failure 4).
- 6.2 Actor-class story route as a standing check (F2, failure 3).

Deliberately excluded: any seat schema before 2.1; welfare features that need
a metaphysical premise; a merge-queue replacement; and the frozen six-track
plan `bcc818bc`, which these phases supersede.
