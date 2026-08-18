# Seats: a conductor and the agents it conjures

Status: design, not schema. Evidence base is thirteen read-only researchers
over `vendor/exo` and this repo; raw reports with file:line citations are in
`.desmos/out/seat-research.md` and `.desmos/out/seat-research-2.md`.

This is the layer under `docs/constitution.md`. The constitution decided what
may change and who decides; this decides what a durable working party *is*.

## 1. The shape being built

A **conductor** holds a seat and almost no tools. It does not read files, run
code, or edit. It delegates, consults durable memory, and talks to the user.
It can conjure **members**: named agents that persist, accumulate their own
memory and character, and are addressable later by name. Growing the active
team is gated by the user; waking an existing member is not.

Two claims in that paragraph were assumed to come from exo. Only one does.

## 2. What exo actually provides

Exo's agent record is three fields — a uuid7 id, a slug, and a
caller-chosen name (`exoharness/src/types.rs:166-171`). No charter, no
persona, no prompt, no model, no parent pointer. Exo has durable named
agents; the name is a nameplate, not a self.

What is genuinely ahead of us:

- **Durable agents.** `agents/<id>/record.json` plus an `agents/by-slug`
  index, with per-agent directories for conversations, bindings, secrets and
  artifacts. Re-addressable by id or slug; removed only by explicit delete
  (`basic.rs:937-966`, `921-934`, `1433-1450`).
- **Model pinned to the agent** at creation, overridable per conversation
  (`cli/src/main.rs:440-465`, `1819-1827`).
- **Conjuring works, in an unexpected way.** There is no shipped
  create-agent tool. The built-ins are shell, inspect_tools, manage_tool,
  install_agent_tool, uninstall_agent_tool
  (`typescript/harness/built-in-tools.ts:26-31`). An agent writes a
  TypeScript tool whose handler calls `exoharness.newAgent({slug, name})`
  and installs it (`harness/index.ts:281-286`). The model authors its own
  conjuring instrument.

What exo does not have, at all:

- **A gate.** `enable_agent_tool_creation` gates *tool authoring*, not agent
  creation; there is no flag check on `newAgent`, which forwards straight to
  Rust (`executor/src/typescript.rs:415-423`, `exoharness/src/server.rs:45-49`).
- **A way to make a member work.** Authored tools can `listAgents()` and
  `getAgent(id)` globally, with no ownership check, and can read another
  agent's conversations — but no send, run, or delegate method exists on
  Agent or Conversation (`harness/index.ts:244-279`). You can create a
  colleague and read their mail. You cannot ask them to do anything.
- **A roster, budget, approval, or evolving trust.** Cost is observational:
  recorded per model call, totalled per conversation, attributed to no agent
  (`cli/src/tui.rs:852-889`). The only per-agent budget is
  `max_tool_round_trips` (`executor_types.rs:31-33`).
- **Retention policy.** No summarization, no forgetting; only coarse prefix
  delete of a whole conversation or agent (`basic.rs:1266-1303`).

Its containment model differs from ours structurally. A conversation is the
durable log; session and turn are *tags on events*, not nested records
(`types.rs:386-395`). The event is the smallest replayable unit. Fork copies
events through a chosen point plus bindings and artifacts, assigns new ids,
appends provenance, and leaves the source untouched (`basic.rs:2403-2474`).

## 3. What desmos already provides

- **Real per-agent capability enforcement**, in two layers exo has no
  equivalent of: the child's tool table is pruned so the prompt cannot
  advertise what it lacks, then dispatch scope blocks execution even if a
  tool is reinstalled (`agents/subagent.py:320-338`, `357-378`).
- **The peer rail.** One live session waking another in the same workspace,
  one turn injected, final speech waking the sender back
  (`state/persist.py:948-997`, `front/bridge.py:404-452`). This is the
  member-to-member channel exo lacks entirely.
- **The conductor role, already written.** `orchestrator` holds
  agents, memory, system, skill, find — no exec, no edit, budget 1
  (`subagent.py:43-48`, `60-61`). It is also half-broken: `memory` refuses a
  non-persistent world, so the one role meant to accumulate judgment is the
  one that cannot remember, and its runtime block advertises `workspace edit`
  and `reload` that it does not hold. The prompt lies to it.
- **Schema room.** `sessions.parent_id` is a nullable self-FK and `kind`
  already permits `child`, though only attach and resume are ever written
  (`state/persist.py:449-460`, `699-714`).

## 4. Definitions

- **Seat** — the enduring party. Name, slug, charter, birth generation,
  append-only model lineage, memory, standing. Does not exist yet.
- **Session** — one incarnation of a seat: a transcript, a model binding, a
  cost, a start and an end.
- **Run** — one dispatched unit of model work.
- **Conductor** — the seat that holds the roster and no execution tools.
- **Member** — a seat conjured by the conductor.

Sessions are days. Seats are people. A model is something a seat wears.

## 5. What the research settles

**T2 — a seat survives a model change: yes.** Exo pins the model at agent
creation and lets a conversation override it. The durable party and the
model binding are already separate there. A seat therefore owns an
append-only lineage of model bindings, and switching model starts a new
binding rather than a new party.

**The conductor must not persist its children's worlds.** This reverses my
earlier build step. Setting `state_path` on a child does nothing while
`persist=False`, and turning persistence on is actively unsafe: every World
reads the process-global `DESMOS_SESSION_ID`, so children would reuse the
root session row, and concurrent saves overwrite the root transcript, merge
calls and events, and delete-then-rewrite workspace-wide notes and tools
(`state/persist.py:28-32`, `675-690`, `758-839`). Durability comes from the
run record, not from handing a child a database handle.

**Memory should not move wholesale into SQL.** This also reverses an earlier
step. The losses are real: line-oriented inspection, salvage from malformed
lines, file-copy portability, and recovery independent of the database file
(`state/memory.py:74-91`). This session is the argument — a corrupt harness
database was survived precisely because a separate file-shaped record
existed. Keep JSONL as the record of truth, add a seat field to each record,
and index into the existing FTS table for ranked search, which today is a
linear unranked substring scan (`memory.py:301-326`) against BM25 for
history (`persist.py:1504-1544`). Git-diffability is not a dependency:
`.desmos/` is ignored.

**The minimum conductor toolset is smaller than the orchestrator role.**
Delegation plus durable consultation: agents, and recall or memory. Speech
needs no syscall. Note that delegation self-destructs at budget zero,
because the agents tag is removed when the budget is spent
(`subagent.py:358-361`).

**Exo's best idea belongs to the conductor.** The RLM executor treats
context as data: a flattened conversation is loaded into a persistent
sandboxed JavaScript workspace, the root model emits repl_execute or
subquery actions, recursive submodels run with tools disabled, and the run
ends when JS sets a Final value (`executor/src/rlm.rs:70-98`, `309-348`,
`687-746`). The REPL deliberately has no filesystem or network. The
principle transfers directly: a party that owns a large record should query
slices of it rather than page all of it into a prompt. Our version of that
is delegation — which is what a toolless conductor is forced into anyway.

## 6. What it does not settle

- **T1, retention.** Neither system has a policy. Exo can only delete whole
  conversations; we fold. Still the user's call, and it still blocks the
  salvage.
- **Standing.** Whether a member's scope can widen with demonstrated work,
  or is fixed at conjuring. Exo has static sandbox scopes only.
- **Cross-repo seats.** Everything here is workspace-local. A seat that
  follows a person across checkouts is unspecified.
- **Attribution under concurrent writers.** Already open as T5/T7.

## 7. Build order

Each step is independently shippable, and each names the check that proves
it and the mutation that proves the check.

1. **Run records carry their transcript.** *Shipped in 65a787d.* Include
   messages in the per-run
   JSON and hydrate on resume from disk when the run is absent from memory
   (`subagent.py:306-315`, `838-846`). Check: finish a child holding a
   unique marker, restart the process, resume its id, assert the marker
   survived and no root harness row changed. Mutation: drop messages from
   the saved record — today's behaviour — and the check must fail.
2. **Seats table.** `seats(id, workspace_id, slug, name, charter, born_gen,
   status)` plus `sessions.seat_id`. No behaviour change yet; a seat is
   created for the existing session so nothing is orphaned.
3. **Model lineage.** Append-only bindings per seat, written on switch.
   Settles T2 in the schema rather than in prose.
4. **Named conjuring with a gate.** Resolving an existing seat is free;
   minting a new one requires user approval. This is the deliberate
   divergence from exo, whose `newAgent` is ungated. Model tier is gated the same
   way: re-pointing a seat at a cheaper model is free, escalating it to an
   expensive one is not.
5. **Seat-scoped memory.** A seat field on each JSONL record and an FTS
   index for ranked retrieval. No storage migration.
6. **Members address each other over the existing peer rail.** Do not build
   a second channel; exo's absence of one is a gap, not a design.

## 8. Rejected

- **Persisting child worlds.** Unsafe for the reasons in section 5.
- **Moving memory to SQL.** Loses the independent recovery path that this
  session depended on.
- **A new inter-agent transport.** The peer rail exists.
- **Copying exo's ungated creation.** The user asked for a gate, and the
  absence of one is exo's weakest point, not its strength.

## 9. The declaration (B2, and the ARES 3 answer)

Sections 1-8 are research and shape. This section is the thing the
constitution's B2 demands before any seat row exists: fields, lifecycle,
reset behaviour, and who gets one. It supersedes nothing above; it commits.

### 9.1 Who gets a seat

**A seat is a user-facing party, and nothing else is.** The test is not
durability, or persistence, or having a transcript. It is whether the user
names it, addresses it by that name, and holds it to a charter.

- **Seated:** a party the user named. It has a slug they can type, a charter
  they wrote or approved, and a history they can ask it about.
- **Seatless — a sibling session.** A second `desmos run` on this workspace,
  headless, doing work in parallel, is a *session*. It has its own process,
  its own run id (272a28c), and it saves into the same harness db. It is not
  a person and needs no name. Giving every concurrent process a seat would
  make the roster a process list, which is exactly the failure the seat is
  meant to avoid.
- **Seatless — a subagent child.** A fork is anonymous by B3 and cannot write
  a seat even if it wanted one.

The consequence is structural, not editorial: **`sessions.seat_id` is
nullable and no code path may require it.** Most sessions will have none.

### 9.2 Fields

`seats` — one row per party, per workspace.

| field | type | meaning |
| --- | --- | --- |
| `id` | TEXT PK | opaque, minted at birth, never reused |
| `workspace_id` | TEXT FK | seats are workspace-local; cross-repo is unspecified |
| `slug` | TEXT | what the user types; unique per workspace, never freed |
| `name` | TEXT | what the user says |
| `charter` | TEXT | what this party is for, in the user's words |
| `born_gen` | INTEGER | the generation at birth; a birth record, not a lease |
| `status` | TEXT | `active`, `dormant`, `retired` |
| `created_at` / `updated_at` | TEXT | ISO, as everywhere else |

`seat_models` — append-only, one row per binding. Settles T2 in schema: a
seat survives a model change because the binding is a row, not a column.

| field | meaning |
| --- | --- |
| `seat_id`, `model`, `thinking` | the binding |
| `bound_at`, `reason` | when, and why it changed |

`sessions.seat_id` — nullable FK, `ON DELETE SET NULL`. Which party this
incarnation belongs to, or nothing.

### 9.3 Lifecycle

1. **Birth** is gated. Minting a slug that does not exist requires the
   user's approval, in a real user turn — not the model asserting it has it.
   This is the deliberate divergence from exo, whose `newAgent` is ungated.
2. **Resolution is free.** Attaching a session to an existing slug needs no
   approval and no ceremony; it is how a party wakes up.
3. **Model rebinding** appends to `seat_models`. Moving to a cheaper tier is
   free; escalating to a more expensive one is gated like birth.
4. **Dormancy** is the absence of a live session. It is observed, not
   written: no timer retires anything.
5. **Retirement** sets `status='retired'`. Rows are never deleted and the
   slug is never freed, so old history keeps resolving to the party that
   made it.

### 9.4 Reset behaviour

Read against `docs/identity.md`, which is the inventory of what survives
what. A seat is **repo-durable**: it lives in the harness db beside notes,
tools and generations.

| event | effect on the seat |
| --- | --- |
| `reset()` — transcript dropped | none. The party outlives the conversation; that is the entire point of it. |
| `harness op=rollback` | none. Rollback restores notes, tools and prior only. |
| process restart | none. The next session re-resolves the same slug. |
| fork / subagent child | none, and none possible: B3 makes it unwritable. |
| generation bump | none. `born_gen` is a birth record and is never rewritten. |
| `rm -rf .desmos` | the seats die with the workspace. They are repo-durable, not machine-global. A seat that follows a person across checkouts remains unspecified. |

A seat therefore has exactly one way to end deliberately — retirement — and
one way to end by accident: deleting the workspace db. Nothing else in the
harness may remove one.
