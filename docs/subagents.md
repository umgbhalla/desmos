# Subagent launch policy

Subagents are isolated child worlds. The parent owns the task, model, prompt
blocks, allowed tools, acceptance checks, guidance,
and integration decision. Children cannot spawn children.

## Models and roles

| role | default model | capability | use it for |
|---|---|---|---|
| `scout` (`explore`) | `gpt-5.6-luna` | read | fast repository reconnaissance and evidence maps |
| `worker` (`general`) | `gpt-5.6-sol` | edit | implementation plus verification through real entry points |
| `reviewer` (`review`) | `gpt-5.6-sol` | read | independent criticism of specs, diffs, and evidence |
| `security` | `gpt-5.6-sol` | read | trust boundaries, abuse cases, exploit paths, and impact |
| `planner` | `gpt-5.6-sol` | read | architecture options, constraints, and ordered plans |
| `sniffer` | `gpt-5.6-luna` | read | reproduce, minimize, and localize the first wrong state |

Sol is the default for advanced implementation and judgment. Luna is the
default for cheap, bounded discovery. These are policies, not locks: every
launch can override `model` and `thinking`.

## Completion

Subagents have no turn, token, usage, or wall-time budget and no
budget-triggered stop state. They run until they return a final answer, fail,
or are cancelled by the parent. Long work is kept aligned with guidance
instead of being cut off.

## Guidance reminders

Long runs are re-anchored every eight turns by default without stopping them.
Set `guidance_every_turns` on a launch to choose another interval; zero
disables reminders. `guidance_reminder` replaces the generated reminder text.
The generated reminder preserves collected evidence, repeats the objective,
acceptance checks, deliverable, and non-goals, and tells the child how to end
with the complete deliverable.

## Prompt controls

A launch can independently set:

- `system_prompt`: replace the generated child system prompt.
- `system_append`: append project-specific instructions to that prompt.
- `task_template`: transform the rendered task; it must contain `{task}`.
- `user_input`: replace the complete initial user block.
- `guidance_every_turns`: set the re-anchoring interval; zero disables it.
- `guidance_reminder`: replace the generated re-anchoring message.

`user_input` has final precedence over `task_template`. Typed contracts remain
the recommended task input because the parent judge can verify their declared
evidence and acceptance checks.

## Parallel launch

`spawn_many` validates every item before enqueuing the first, then submits the
whole batch to the bounded shared executor. The `agents` XML tool exposes this
as one JSON command with `op` set to `spawn_many` and a `tasks` array. Each
item may contain a text `task` or a typed `contract`, an `agent`, and any
launch override above. IDs are returned in input order; execution is concurrent. The parent pending
loop receives one grouped completion callback after every child settles;
individual child lifecycle events still update the TUI as they happen.
