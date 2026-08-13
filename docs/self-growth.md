# Self-growth

This is the hard problem. A smart model in an empty kernel will invent a
REPL session and then forget why. We measured that. Self-growth is not
"tabula rasa plus Opus." It is a closed loop with physics, a frozen
brainstem, and evidence.

## What failed

In the hunger-games runs, Opus 5:

- found the answer and left it in stdout
- registered `<grep>` / `<read>` only when the chat still felt like a session
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

## The inverted version

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
5. `<register>` XML tag — new dialect. Entropy risk. Prefer a skill.
6. Extension `load(api)` — hooks on dispatch. Highest user-code risk.

### What may not grow

`scan`, `turn` / `step`, `complete`, exec policy, budget, the five/six
frozen tags. If the agent can rewrite the loop, it eats itself. Prime
froze the base prompt for the same reason.

### Why this is difficult

**1. Models do not pin.** They print. The ABI has to say: if future-you
needs it, write a skill or a note. Printed text dies with the turn.

**2. Discovery ≠ use.** A catalog of 40 vague skills is worse than none.
Descriptions must be trigger conditions ("Use when…"), not slogans.
Unused skills should be disable-able.

**3. Reload is the missing physics.** Writing `SKILL.md` during a turn
does nothing if `new_world()` already ran. Growth requires rediscovery
*before the next complete()*. Without that, self-growth is a story.

**4. Refine is a different creature.** The working agent is a bad editor
of its own soul while it is failing. A second, narrow pass (or a human
`step("turn that tactic into a skill")`) with "smallest diff + evidence"
is how Prime stays sane.

**5. Evaluation or rot.** A grown handler that is never called, or that
errors twice, should be tombstoned. Otherwise the dialect sludges.

**6. Shared heap is not a mind.** Variables persist. Meaning does not,
unless it is in a note, a skill, or a named object the *index* still
lists.

## Mechanical contract (this repo)

- Frozen ABI lists the write paths. It does not list fifty tools.
- Each `turn()` rediscovers skills and extensions before `complete()`.
- `<skill name>` loads the file. Python `handle`/`run` binds into `ns`
  and may become a tag.
- `reload()` is also a user-callable in the kernel.
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
Editing `inverted.py` is a fork of the species, not a generation.
