# ARES plan: from harness to constellation

Derived from Yegge Pt1/Pt2, docs/constitution.md, docs/seat-design.md and four
scout reports (2026-08-17). Every decision below is the user's unless marked
*mine*.

## Decisions taken
1. Our own in-kernel work graph, our own SQL, a Cloudflare D1 backend later.
   No external Beads.
2. Sibling seats, not subagents: independent, persistent, addressable, and
   never throwaway.
3. Roles come from charter and prompt, not crew/fleet machinery.
4. Minting a seat is gated by a tool (working name `birth`); waking one is not.
5. The green build gate stays; verification time must come down.
6. Consented handoff first; provider-compatible compaction is the fallback.
7. Nothing is ever deleted. Retention becomes a loading policy plus a cold
   store.
8. The user names seats. The first is **ares** — this session.
9. Witnessed work gets a real mechanism.
10. No token-tap hack. Real API integration with a usage monitor.

## What the scouts established

- **Sessions already carry lineage.** `sessions` has `parent_id` and a `kind`
  of `attach|resume|fork|child` (persist.py:450-462), but the only insert path
  is `_session_id` (persist.py:740-755) driven by a process-wide
  `DESMOS_SESSION_ID`, and nothing ever writes `fork` or `child`.
- **The peer rail works** — `channel_messages`/`channel_cursors`
  (persist.py:535-550), bridge polls at 250ms and queues a step
  (front/bridge.py:376-459). Waking requires a live bridge; there is no DB
  notification.
- **plan.py is the right philosophy, wrong substrate.** Append-only JSONL with
  `latest()` per plan_id (plan.py:79-122); records hold title/status/body/steps
  and one transcript pointer (plan.py:226-240). Edges, parent/child, claim,
  lease, gates and triggers are all ABSENT, and `_append()` has no lock
  (plan.py:99-104). Claiming across sibling seats wants a transaction, so the
  graph moves to SQL — which is also what makes D1 trivial later.
- **The fold cannot be pre-empted, only anticipated.** Anthropic compaction is
  requested by `apply_compaction()` (transport/complete.py:102-107); the server
  decides. The first definitive signal is the returned compaction block
  (loop.py:466-483). There is no preflight context estimate; usage arrives only
  after the response (loop.py:417-434). Correction to an earlier belief of
  mine: no code rewrites the handoff note at a fold. Notes are written through
  `set_system()` (dispatch.py:84-95).
- **Pruning currently deletes.** `calls` rows cascade away
  (persist.py:1414-1419) and `record_call` swallows its own failures
  (persist.py:1327-1372). Decision 7 makes both defects.

## Build order

Each phase names the gate that proves it. Cheapest and most decision-free
first.
### Phase A — the record stops deleting (decision-free)
- A1 pruning becomes archival: rows move to a cold store, never vanish.
  *Gate:* prune a session in a temp workspace, then read its messages back.
- A2 apply the salvage (todo 13): 22 sessions / 692 messages imported cold,
  deduped by fingerprint, provenance recorded. *Gate:* a marker present only in
  a quarantined db is returned by recall afterwards.
- A3 write the loading policy — what reaches a session, per store — into the
  constitution. *Gate:* reviewed.
### Phase B — the handoff rail
- B1 soft threshold: compare the last response's input tokens against the
  model's context and deliver one "write your handoff" steer before the server
  folds. *Gate:* a synthetic usage sequence crossing the line produces exactly
  one prompt, and none below it.
- B2 post-fold consent: propagate a fold flag from `turn()` (loop.py:466) into
  `_run_turns()` (loop.py:979-987) and ask for the handoff after result
  delivery, before the termination test. *Gate:* a fake compaction block forces
  another turn and the note lands.
- B3 `reset()` (loop.py:1118-1128) stays for a poisoned turn but must record
  what it dropped. Never falsify the record. *Mine, unless overruled.*

### Phase C — the work graph in SQL (ARES 1)
- C1 tables: `work_items`, `work_edges`, `work_events` (append-only),
  `work_leases`. Costs a SCHEMA_VERSION bump, which kills every live front on
  the workspace — schedule it deliberately.
- C2 claim is a single-statement CAS on an expiring lease. *Gate:* two
  concurrent claimants, exactly one wins.
- C3 gates and triggers are rows, not code: a gate blocks a node until an
  evidence pointer exists; a trigger wakes a seat.
- C4 todos migrate in; the todo op becomes a view over items held by my seat.
  *Gate:* this build order lives in the graph and drives the rest.

### Phase D — seats (ARES 2/3, the point of all of it)
- D1 `seats(id, workspace_id, slug, name, charter, born_gen, status)` plus
  `sessions.seat_id`. The scaffold retired in 6734fd5 returns, this time with
  the seat declared first.
- D2 append-only model bindings per seat, written on switch. Settles T2 in
  schema rather than prose.
- D3 `birth`: minting a seat needs the user; resolving an existing one is free.
- D4 a sibling session gets its own state path, `persist=True`, its own session
  row with `kind='child'` and a real `parent_id`, and its own session id —
  today's children are `state_path=None, persist=False`
  (subagent.py:386-400). **Blocked on the single-writer lock** shipped for todo
  18: a sibling needs a home of its own, or the lock needs scoping.
- D5 standing seats need a headless runner. The peer rail can wake a seat, but
  only through a live bridge. Without this there are no unattended agents.

### Phase E — cloud and money (ARES 6/7)
- E1 transactional outbox, fingerprint `sha256(canonical_json(...))`, push-only
  and idempotent, verified against a fake sink before any network.
- E2 a Worker plus D1 behind it. Every published limit is UNVERIFIED until read
  from Cloudflare's own docs.
- E3 usage monitor: account identity on `calls`, USD and window budgets,
  cross-session aggregation, a stop callback. `record_call` stops swallowing
  failures.

### Phase F — witnessed work and evaluation (ARES 8, todo 10)
- F1 a per-seat accomplishment record, append-only, derived from work events
  and commits, surfaced at wake. Being seen is the mechanism, not decoration.
- F2 the same record is the eval substrate: items completed per seat, rework
  rate, gate failures. That answers todo 10 without inventing a task set.

### Cross-cutting — ARES 9
Measure before optimising: publish per-group check timings, then cut the
dominant group. The green gate holds until then.

## Open, needing the user
- **When to bump SCHEMA_VERSION.** Phase C and D both need it, and it kills
  every live front on this workspace, including the other session writing here
  now. One bump for both, deliberately scheduled.
- **Does a sibling seat get a home of its own** — its own workspace dir and
  worktree — or does the single-writer lock get scoped instead? *My
  recommendation: a home of its own, with the work graph as the meeting point.*

## Next action
Phase A1. Decision-free, and it unblocks the salvage.
