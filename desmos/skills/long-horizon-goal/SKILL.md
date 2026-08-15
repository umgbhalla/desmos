---
name: long-horizon-goal
description: Run an explicitly requested long-horizon objective as a durable evidence-driven control loop that survives interruption, compaction, and resume.
---

# Long-horizon goal

Use this only when the user explicitly asks for goal mode, sustained autonomous
work, or an outcome that needs multiple inspect-change-verify cycles. Ordinary
tasks remain ordinary tasks.

## State

Keep three concepts separate:

1. **Goal:** the unchanged user outcome and its completion evidence.
2. **Plan:** the current todo sequence; it may change as evidence changes.
3. **Turn:** one sensor-controller-actuator iteration.

At activation, write an `active-goal` system note containing:

- status: active
- the exact objective without narrowing it
- explicit constraints and non-goals
- a requirement-to-evidence ledger
- the latest verified state and next action
- a repeated-blocker count

Use the persistent todo for the current plan. The note is the durable controller
state; the todo is only the replaceable route.

## Control loop

For each iteration:

1. **Sense:** inspect current files, runtime state, tests, artifacts, and external
   dependencies. Current state outranks remembered conversation.
2. **Compare:** determine which original requirements are proved, contradicted,
   weakly evidenced, or missing.
3. **Act:** take the smallest action that makes the full requested end state more
   true. Do not optimize for an easier substitute objective.
4. **Verify:** run the authoritative check for the requirement just changed.
5. **Record:** update the goal note and todo from observed evidence.
6. **Continue:** while the goal is active and unfinished, make the next tool call
   instead of ending with a progress-only answer.

Do not use token, turn, or wall-time countdowns. The user stops the loop. A
queued user prompt always takes priority over automatic continuation.

## Completion and blocking

Completion is initially unproven. Mark the goal complete only after every
explicit requirement has current authoritative evidence and no required work
remains. Then remove the `active-goal` note, clear its todos, and report the
outcome.

Do not call a goal blocked on the first obstacle. Record the same concrete
blocker across three consecutive iterations while attempting all independent
work. Only then pause the goal and ask for the missing user input or external
change. Hard, slow, uncertain, or incomplete work is not itself a blocker.

## Resume and model switches

After interruption or restart, read the `active-goal` note and todo, then
re-inspect current state before acting. Compacted conversation and prior model
reasoning are orientation, not proof.

After a model switch, preserve the shared goal and plan but reload any
model-dialected skill needed for the next action. Never rely on an overlay
loaded for a different model family.
