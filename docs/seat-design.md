# Seat design

Status: **proposal**. Nothing in this document is built. It exists so it can be
rejected cheaply.

It answers one question: what is the smallest set of changes that turns desmos
subagents into a durable, named, user-gated team with a conductor at its head.

Evidence is thirteen read-only research runs against `vendor/exo` and this
repository; raw reports with file:line citations are in
`.desmos/out/seat-research.md`.

## The gap in one paragraph

Desmos can already do the hard parts. It prunes a child's tool table so the
prompt cannot advertise capability the child does not have, and it blocks the
call at dispatch even if the tool is reinstalled (`subagent.py:357-378`). It
has a peer rail: one live session can wake another in the same workspace. Exo
has neither — it hard-codes a tool list per executor and has no agent-to-agent
messaging at all. What desmos does not have is **durability of a party**. A
child gets a fresh world with `state_path=None, persist=False`
(`subagent.py:341-354`), its transcript is deliberately excluded from the saved
record (`subagent.py:306-311`), and when the process dies the child dies with
it. Only the result survives. There is no one to come back to.

## Definitions

**Seat** — a durable named party with a charter, a memory scope, and a tool
grant. Survives process restart. Survives a model change. Has lineage.

**Session** — one live occupancy of a seat by a model. Ends. The seat does not.

**Run** — one delegated unit of work inside a session.

A subagent today is a run with no seat and no session. That is the whole defect.

## Decision T2 — a seat survives a model change

**Decided: yes.** The seat is the durable party; the model is a binding of one
session; lineage is the append-only list of those bindings.

This is now evidence-backed rather than taste. Exo pins the model at agent
creation and lets a conversation override it per launch, which is the same
shape arrived at independently. It is also the only reading under which the
stated goal — advanced, fast and cheap models as controlled agents — is
expressible: you re-point a seat at a cheaper model without destroying who it
is.

Consequence for the schema: `model` is an attribute of a session, not of a
seat, and a change is an event in the seat's own history.

## Three findings that constrain the build

### 1. The conductor already exists, and is half-broken

The `orchestrator` role holds close to the minimum already: `agents`, `memory`,
`system`, `skill`, `find` — no exec, no edit, budget 1. Two defects:

- `memory` refuses a non-persistent world. The one role whose entire purpose is
  to accumulate judgment across time is the one role that cannot remember
  anything.
- It inherits a runtime block advertising `workspace edit` and `reload`, which
  it does not hold. The prompt lies to it.

The true minimum is smaller than the current role: `agents` to delegate, and
`recall` or `memory` to consult. Speech needs no syscall. Note that delegation
self-destructs at budget zero — `agents` is removed when the budget runs out,
which is why `orchestrator` defaults to 1.

### 2. Turning on child persistence against the parent database is unsafe

The tempting one-line fix — give the child a `state_path` — does nothing while
`persist=False`, and is actively destructive if both are set. Every World reads
the process-global `DESMOS_SESSION_ID` (`persist.py:28-32,675-690`), so children
would reuse the *root* session rather than create their own rows. Concurrent
saves then overwrite the same transcript and FTS rows, merge calls and events,
and delete-then-rewrite workspace-wide notes and tools (`persist.py:771-839`).
WAL and `BEGIN IMMEDIATE` prevent corruption; they do not prevent one session
semantically erasing another. A separate database avoids the collision but
produces an unlinked attach, not a child.

This is the same class of bug as the fold that erased this session's own
transcript. It should not be re-introduced deliberately.

**The smallest durable change is therefore not to persist the child World at
all.** Include the child's messages in the per-run JSON record that already
exists, and on `spawn(resume=id)` read and validate that record from disk when
it is absent from memory. Final state already copies the messages
(`subagent.py:616-619`); they are simply dropped before writing.

### 3. Memory cannot be keyed to a seat today

Notes and grown tools are workspace-keyed in SQL and could take a seat foreign
key tomorrow. Memory records are a JSONL file keyed by a content hash
(`memory.py:69-71`) — no foreign key is possible without moving them into the
database.

Moving them buys a second thing worth having on its own: memory search today is
a substring scan over every active record, sorted by scope, kind and ID rather
than by relevance (`memory.py:301-326`), while history search is already FTS5
with BM25 ranking (`persist.py:1504-1544`). The move is reversible — rows export
back to canonical JSONL — but no migration exists.

## What the schema needs

`sessions.parent_id` is already a nullable foreign key and `kind` already
permits `'child'` (`persist.py:449-460`). Nothing inserts one: the only kinds
ever written are `attach` and `resume` (`persist.py:699-714`). The anchor is
built and unused.

Additions, in dependency order:

| change | why |
|---|---|
| `seats` table | the durable party: id, name, charter, tool grant, parent seat, created, retired |
| `sessions.seat_id` | binds an occupancy to a seat; `model` on the session already carries the binding |
| child messages in the run record | a finished child becomes resumable after a restart |
| memory rows in SQL | lets a seat own its memory, and replaces substring scan with BM25 |
| named spawn | resolve an existing seat, or ask the user to create one |

## Gating

Exo is no help here: its cost accounting is observational only, with no
budgets, no approval gates and no trust system. This is ours to invent.

Proposed, and open to correction:

- Creating a **new named seat** requires the user's explicit yes. Every time.
- **Re-pointing an existing seat at a cheaper model** does not.
- **Escalating a seat to an expensive model** does.
- A seat may never write another seat's memory, and no child may write the root
  harness. `persist=False` is today's hard boundary and stays the boundary.

## Build order

Smallest first, each independently useful, each with a check that fails if the
wiring is absent rather than only if the function is wrong:

1. `seats` table and `sessions.seat_id`. Nothing uses them yet.
2. Persist child messages in the run record; make `spawn(resume=id)` survive a
   process restart. Test: finish a child carrying a unique marker, restart the
   process, resume by id, assert the marker is present and root harness rows are
   unchanged. Mutation: drop `messages` from the saved record — the test must
   fail.
3. Move memory into SQL, keyed by seat, exporting back to JSONL to prove
   reversibility.
4. Named spawn: resolving an existing seat is free, creating one asks.
5. Reuse the peer rail for seat-to-seat traffic rather than inventing a second
   channel.

## What was deliberately not copied from exo

Exo's durable unit is an `Event`; sessions and turns are labels attached to
events rather than nested durable records. Desmos models sessions, prior turns
and messages separately. Adopting the event log would be a rewrite of
persistence for no gain, and the two shapes do not compose.

Exo's RLM executor — context loaded into a sandboxed JS workspace, explored and
recursively sub-queried by the root model — is a genuinely different idea and
is not part of this proposal. It is close to what the desmos kernel already
does with `ns`, and the comparison deserves its own document.

## Still open

These block nothing in step 1 but must be settled before step 3:

- **T1 retention** — what is kept verbatim and what is summarized, and who
  decides. Also blocks the salvage of 22 recoverable sessions.
- **T3/T4** — what a seat may change without review.
- **Evaluation** — what measures this harness improving itself. There is no
  eval today, so "proven against real work" currently means one agent's
  judgment and nothing more.
