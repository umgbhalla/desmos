# Steering: execution control instead of more tags

The goal is not a richer tag vocabulary. It is the ability to change a run
that is already in flight — inject context, redirect, correct — without
cancelling it and without paying for the correction in cache.

The design rule that follows: **add stages, not tags.** Every capability
below is a hook point or a carrier on a message that was already going to be
sent. None of it is a new syscall family.

## 1. The stages that already exist

Measured in this tree, not proposed.

| Stage | Where | What it can do today |
| --- | --- | --- |
| S0 volatile system block | `complete.py:38-51`, `catalog.py:76` | rewritten every turn, **explicitly not cached** |
| S1 mid-stream | `complete.py:515-587` | abort only; there is no channel into an open response |
| S2 pre-dispatch | `dispatch.py:160` | `before_dispatch` returning a string replaces the result and the call never runs |
| S3 result carrier | `complete.py:348-361`, `openai.py:202-271` | a `tool_result` / `custom_tool_call_output` the harness must send anyway |
| S4 between turns | `loop.py:1002-1006` | `on_continue(n)` appends a user message, emits `ev guidance` |
| S5 fold | `loop.py`, compacted hook | rewrite what survives the fold |

S4 is already used for real, but only by children: `subagent.py:612-620`
re-anchors a long run every `guidance_every_turns`. The parent loop accepts
`on_continue` and nothing supplies one. Steering the main session is
therefore not a missing mechanism — it is an unwired one.

## 2. The two laws

**Cache.** `cached_payload` caches the ABI block, the catalog block and the
last user message. Appending at the tail is cheap; editing the middle
destroys the prefix for every later turn in the session. So context injection
happens in S0 or on the tail, and history is never rewritten to steer.

**Pairing.** A `tool_result` whose `tool_use` is gone is a hard 400, and so is
a `tool_use` nothing answered — both dialects, both directions
(`complete.py:348`, `openai.py:202-271`). Any steer that adds or drops a
message must keep that pairing intact. This is what makes S3 attractive: the
message already exists and is already paired.

## 3. Per dialect, the correct stage

The question "where can a steer land" has a different answer per dialect only
at S1 and S3.

| | Anthropic tool | OpenAI | prose (`DESMOS_TOOL_SYSCALLS=0`) |
| --- | --- | --- | --- |
| syscall arrives as | `tool_use` | `custom_tool_call` | XML in speech |
| result returns as | `tool_result` in a user msg | `custom_tool_call_output` | plain user msg |
| steer carrier | extra text in the tool_result | same in the output item | appended to the user msg |
| parallel calls | one at a time | pinned off (`openai.py:357`) | one at a time |

Because both tool dialects serialise calls, a steer written into result *k*
is read before call *k+1* is chosen. That is the tightest correction loop
available without touching the stream.

## 4. Plan

### Phase A — make the volatile block writable

`volatile(world, delta)` is harness-authored today. Give the world an ordered
`inject` map rendered into that block: a name, a body, and a lifetime (this
turn, N turns, or sticky). The agent writes its own context; the user's
mid-run message lands there for the next turn without interrupting anything.

*Gate:* cached block token counts unchanged; a sticky entry is visible on
turn n+1 with no new user message; a one-turn entry is gone on n+2.

### Phase B — steering rides the result channel

A steer queue on the world, drained when the loop builds the result for a
dispatched call and appended into that payload, per the table in section 3.
The model reads the correction at the moment it is reading the outcome of its
own action, and before it has committed to the next call.

*Gate:* a steer queued during dispatch appears in that same turn's result and
changes the next call. The test drives the real dispatch loop — asserting on
the formatter would pass against a queue nothing drains.

### Phase C — mid-stream abort to a clean boundary

Not cancel-and-resend. On an urgent steer: stop reading the SSE, truncate the
assistant message to the last `content_block_stop` (`complete.py:566`),
append it as a real assistant turn, then inject via A or B. The partial work
survives and no half-written `tool_use` is left to orphan.

*Gate:* interrupt mid-stream, then assert the stored message replays through
`cached_payload` with pairing intact and the partial text still present.

### Phase D — retire tags into stages

The "fewer tags" half, decided by measurement rather than taste. Use the
usage rollup (constitution Phase 1.2) over `calls`/`events` per op. Anything
never called is retired behind the stages above.

*Gate:* catalog token count strictly decreases; suite stays green.

## 5. Open, and honest

- **S1 has no injection point and cannot be given one.** A single HTTP
  response is not a duplex channel. Phase C is the whole of what mid-stream
  control can be.
- **Do not build a second path.** Phase B must generalise `on_continue`, not
  sit beside it. The constitution's own rule; the peer rail and the plan
  store were both nearly duplicated this way.
- **T1 touches this.** A sticky injection is retained context, so its
  lifetime is a retention decision, not a UI default.
- **T7 touches this.** A steer is a write. Under two writers in one worktree
  it needs provenance like any other mutation.
