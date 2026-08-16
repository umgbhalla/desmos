# Syscall reference

Models have one external tool, `syscall`. Its input contains one or more XML
calls. Desmos advertises seven canonical capability families; each requires an
`op` attribute.

## Wire rules

- Text is speech; XML calls are syscalls.
- Calls in one response execute in order and return together next turn.
- A failure does not stop later independent calls.
- Results over the inline cap spill under `.desmos/out/`.
- A body containing tag syntax must use `end="TOKEN"`.
- Unknown families and operations fail without raising into the agent loop.

## Canonical families

### exec

`op=python|bash|shell`

- `python` executes in the persistent in-process namespace.
- `bash` is a fresh hermetic command with a bounded timeout.
- `shell` is a named persistent PTY; `id`, `interrupt`, and `close` retain their
  established meanings. Long commands are monitored and resume the loop when
  they finish.

```
<exec op="python">value = 40 + 2</exec>
<exec op="shell" id="main">cargo test</exec>
```

### workspace

`op=find|read|edit|see|commit`

- `find` exposes fff path, glob, grep, symbol, and multi-pattern modes.
- `read` accepts `path`, `lines`, or `head` and returns numbered bounded text.
- `edit` uses the exact old/new body separated by a line containing `---`.
- `see` attaches paths or captures the screen.
- `commit` takes the commit message in the body; `add`, `only`, and `amend`
  preserve the safe message-file behavior.

```
<workspace op="find" mode="symbol">seed_builtins</workspace>
<workspace op="read" path="README.md" head="40"/>
<workspace op="edit" path="file.py">old
---
new</workspace>
```

### knowledge

`op=memory|recall|system|todo`

- `memory` manages curated cross-session facts.
- `recall` searches prior Desmos events.
- `system` writes or deletes always-present doctrine.
- `todo` appends, completes, removes, and lists persistent work items.

### harness

`op=register|describe|skill|reload|reload-sdk|evolve|rollback`

This family owns self-extension and grown-state lifecycle. Register installs an
operation, describe changes its catalog line, skill loads detailed procedure
text, reload refreshes resources, reload-sdk reimports Desmos Python modules,
and evolve/rollback snapshot or restore grown state.

### observe

`op=usage|trajectory|retrace|error|symbol|threads`

Read-only diagnostics and telemetry. Error, symbol, and threads are bounded
views backed by the persistent kernel's `diag` object and never return locals.

### agents

`op=spawn|fanout|status|result|structured-result|judgment|wait`

Spawn accepts the task body and optional agent/model/thinking attributes.
Fanout separates task bodies with a line containing `---`. Result operations
take an id in the body or `id`; wait accepts whitespace- or comma-separated ids.

### session

`op=compact|status|switch|peers|inbox|read|post|dismiss`

Compaction accepts `keep` and `floor`. Status reports model, effort/generation,
and transcript size. Switch takes the model in the body or `model`, with
optional `effort`; it applies from the next turn.

Peers lists live processes in the current checkout; each process holds an OS
lease, so crashed or exited peers are pruned without a heartbeat timeout.
Inbox reports unread messages from other runs without advancing the cursor.
Read returns ordered messages from the `conflicts` channel by default, accepts
`channel`, `since`, and `limit`, and marks returned messages read unless
`mark=false`. Post appends the body and accepts `channel` and `author`. With a
live peer id in `to`, `session_id`, or `run_id`, post instead starts a bounded
one-round exchange: the target wakes, its final response returns automatically,
and the sender wakes once to report it. Replies are never auto-replied. Dismiss
advances the unread cursor without replying. All channel data stays in the
checkout's harness database.

## Compatibility aliases

Earlier transcripts and persisted generations may contain the former names.
They remain accepted by dispatch but are deliberately omitted from the tool
catalog and ABI prompt. Canonical calls normalize before scope checks and
extension hooks, so existing policies continue to see the underlying operation
such as `bash`, `edit`, or `memory`.

The accepted aliases are:

- execution: `python`, `bash`, `shell`
- workspace: `find`, `grep`, `read`, `edit`, `see`, `commit`
- knowledge: `memory`, `recall`, `system`, `todo`
- harness: `register`, `tool`, `skill`, `reload`, `reload_sdk`, `evolve`,
  `rollback`
- observation/session: `usage`, `traj`, `trajectory_retrace`, `compact`
- retired utility: `sleeper`

Compatibility does not imply advertisement. New prompts and documentation use
only the seven families.

## Extensibility

Custom registered and extension tools remain visible alongside the canonical
families unless their name is a compatibility alias. A skill is still loaded
on demand; its metadata appears in the skills catalog rather than becoming
another syscall family.
