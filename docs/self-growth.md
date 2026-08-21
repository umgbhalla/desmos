# Self-growth

This is the hard problem. A smart model in an empty kernel will invent a
REPL session and then forget why. We measured that. Self-growth is not
"tabula rasa plus Opus." It is a closed loop with physics, a frozen
brainstem, and evidence.

## What failed

In the hunger-games runs, Opus 5:

- found the answer and left it in stdout
- registered ad-hoc grep/read tools only when the chat still felt like a session
- never wrote `SURVIVAL`
- after a wipe, re-oriented instead of using heap objects it already had

Lesson: **speech is not memory.** Growth that is not a syscall that writes a
file does not exist for future-you.

## What Prime / Pi actually do

They do **not** let the model rewrite the loop.

| Frozen | Grown |
|---|---|
| base system prompt | skill *descriptions* in the prompt |
| tool impls for builtins | full `SKILL.md` loaded on demand |
| compaction / session / auth | Python skill modules in the kernel |
| | `/refine`: a *second* pass, evidence-backed, small diffs |
| | `/reload`: pick up new files without a new process |

`refine` is not the working agent mutating itself mid-thought. It is a
reviewer: look at the trajectory, write the smallest harness entry
(memory, prompt note, skill spec), record before/after, allow rollback.
The base prompt never moves.

Progressive disclosure is load-bearing. The catalog is an index. The body
of a skill enters context only when chosen. If every grown thing is dumped
into the system prompt, the creature drowns in its own organs.

## What the loop actually is

The kernel is the organism. `complete()` is a gland. Growth is files plus
a live catalog, not a fatter `messages[]`.

```
do work
  → notice a repeated miss or a reusable tactic
    → write the smallest durable artifact
      → reload (catalog / ns / handlers update)
        → next step() sees it
          → use it once
            → keep, or delete
```

### What may grow (risk order)

1. `<system name>` note — doctrine, one page. Lowest risk.
2. `<tool name doc>` — rewrite a description so future-you routes better.
3. `SKILL.md` under `.desmos/skills/<name>/` — instructions, progressive.
4. Python skill `handle` / `run` — real code in the kernel. Medium-high.
5. `harness op=register` — new dialect. Entropy risk. Prefer a skill.
6. Extension `load(api)` — hooks on dispatch. Highest user-code risk.

### What may not grow

`scan`, `turn` / `step`, `complete`, exec policy, budget, the frozen tags.
The agent *can* edit `desmos/*.py` and `<reload_sdk/>` — that is a species
fork applied live, not growth. Prefer a skill. If the agent rewrites the
loop casually, it eats itself. Prime froze the base prompt for the same
reason.

### Why this is difficult

**1. Models do not pin.** They print. The ABI has to say: if future-you
needs it, write a skill or a note. Printed text dies with the turn.

**2. Discovery ≠ use.** A catalog of 40 vague skills is worse than none.
Descriptions must be trigger conditions ("Use when…"), not slogans.
Unused skills should be disable-able.

**3. Reload is the missing physics.** Writing `SKILL.md` during a turn
does nothing until rediscovery. `<reload/>` does that mid-turn; every
`turn()` also rediscovers before `complete()`. Without that, self-growth
is a story.

**4. Refine is a different creature.** The working agent is a bad editor
of its own soul while it is failing. A second, narrow pass (or a human
`step("turn that tactic into a skill")`) with "smallest diff + evidence"
is how Prime stays sane.

**5. Evaluation or rot.** A grown handler that is never called, or that
errors twice, should be tombstoned. Otherwise the dialect sludges.

**6. Shared heap is not a mind.** Variables persist. Meaning does not,
unless it is in a note, a skill, or a named object the *index* still
lists.

### Retiring an op (the frozen side)

Refine governs grown tags. The frozen ABI is retired under a stricter rule,
because its ops are advertised to every request and some of them exist for
the day they are needed rather than the day they are used.

**Call count is not the criterion.** `rollback`, `judgment`, `resume` and
`error` are insurance; their whole value is the tail. A workspace can run for
months without one and still be well served by it. `observe op=usage` names
the ops nothing has called: that list is a question, never a verdict.

An op may be *proposed* for retirement only when one of these holds, and
never merely because it is idle:

1. **Subsumed.** Another op does its job, and the record shows the successor
   used on this op's own case. Retiring without a successor is not
   simplification, it is a capability gap.
2. **Unreachable.** No dispatch path can produce it, or its handler has been
   broken for a whole generation and nothing noticed.
3. **Misrouting.** Two lines read as the same thing and calls land on the
   wrong one. Rewrite the description first (`harness op=describe`) -- a doc
   is cheaper than an ABI change -- and retire only if the confusion survives
   the rewrite.

Before any of that, ask the discoverability question: did the *situation* the
op serves ever occur? A regression rolled back by hand while
`harness op=rollback` sat unused is a routing failure, not a dead op.

Retirement is a tombstone here too. The tag stays in `COMPAT_ALIASES` forever
so old transcripts and generations still replay; only the advertised line goes.

## Mechanical contract (this repo)

- Frozen ABI lists the write paths. It does not list fifty tools.
- Each `turn()` rediscovers skills and extensions before `complete()`.
- `<skill name>` loads the file. Python `handle`/`run` binds into `ns`
  and may become a tag.
- `<reload/>` / `reload()` rediscover mid-turn (after writing a skill).
- `<reload_sdk/>` / `reload_sdk()` reimports the SDK and rebinds `step`.
  New ABI applies on the next `complete()`. Heap, notes, messages stay.
- Artifacts live on disk (`.desmos/skills`, `.desmos/harness.json`).
  The prompt only holds the index.

## Trajectory (Pi / Anthropic)

The chat is append-only. Tool results expand on the **user** side as
`<result>` only — never a restated task. `step()` appends a new user
utterance onto the same `world.messages`.

Cache breakpoints copy earendil-works/pi:

1. frozen ABI (system block 1)
2. live catalog (system block 2)
3. last **user** / result block — never assistant

Catalog changes miss breakpoint 2; ABI can still hit. A new `step()` that
throws the list away would miss everything; we do not do that.

## Generations

Gen 1 is the starting snapshot of grown state (notes, registered tools,
docs). `<evolve>why</evolve>` writes gen N+1. `<rollback n="1"/>` restores.
Editing `desmos/*.py` is a fork of the species, not a generation. Apply
it live with `<reload_sdk/>`. Snapshot first with `<evolve>` if you want
a way back.
