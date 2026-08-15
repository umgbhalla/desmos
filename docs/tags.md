# Tag reference

Text a model writes is speech. An XML tag is a syscall. This page lists every
tag the harness ships with, what it accepts, and what comes back.

## The medium

- **Results are user-role messages.** Each call comes back as a
  `<result tag="..."&gt;...</result>` block on the same transcript. There is no
  separate tool channel.
- **Every tag in a reply runs**, in written order, and all results arrive
  together in the next message. A failing call does not stop the ones after it.
  A call that needs an earlier result belongs in the next turn.
- **A body ends at its first closing tag.** If the body must contain tag text,
  declare an end token: any tag accepts `end="TOKEN"`, and the body then runs to
  `&lt;/tag:TOKEN&gt;`. This is not optional when editing this codebase — its
  sources are full of literal tag text.
- **An unclosed tag is dropped in silence.** No result, no error; the turn looks
  like nothing was called.
- **Results are capped** at 6000 characters. Anything longer is written whole to
  `.desmos/out/NNNN-tag.txt` and the result opens with a pointer to that file.
  Nothing is discarded; it just is not spent on context.
- **Nothing raises.** A denied tag, an unknown tag, a handler that throws — all
  come back as readable text.

## Frozen tags

Thirteen tags are the ABI (`FROZEN` in `desmos/const.py`). They are present in
every session on every machine and never change.

### python

Executes in the persistent kernel. Names bound in one call are there in the
next, this turn and in later turns. stdout streams into the call's card as it is
produced.

```
<python>x = load(); print(len(x))</python>
```

Prints go to the result; the value of the last expression does not. Peek at
shapes, not contents — dumping the heap into chat is what this harness exists to
avoid.

### bash

A one-shot subprocess in the world's cwd. No state survives it: not the cwd, not
an export, not a background job. Timeout is 60s.

```
<bash>git status --short</bash>
```

Use it for a quick hermetic command where a fresh process is the point.

### shell

The preferred way to run commands. A named persistent pty whose cwd,
environment, virtualenv, interactive process and unfinished build survive across
calls.

| attribute | meaning |
|---|---|
| `id` | session name, default `main`. Different ids are different terminals. |
| `interrupt` | `1` sends an interrupt to whatever is running on that session. |
| `close` | `1` ends the session and frees the pty. |

```
<shell id="build">cargo test -p desmos-tui</shell>
```

There is no polling and no read window to choose. A command that outlives the
first look is taken over by a monitor that owns the terminal, and the step is
resumed with its output when the command actually finishes — a result that says
it is monitored means the work is still running. A program that asks a question
comes back saying so; answer it with another call on the same id.

Shells are process-lifetime only. They are deliberately not persisted: a pty
cannot be restored from JSON, and pretending otherwise would hand back a session
whose `cd` silently did not survive.

### edit

Replace exactly one occurrence in a file. Fails loudly if the old text is absent
or appears more than once — that assertion is the whole point, because a stale
read then aborts instead of clobbering a file someone else moved.

```
<edit path="desmos/const.py">BASH_TIMEOUT = 60
---
BASH_TIMEOUT = 90</edit>
```

The body is `old`, a line containing only `---`, then `new`. When that shape is
awkward — the body itself contains a `---` line — pass `old_str=` and `new_str=`
as attributes instead. Paths are relative to the world's cwd.

### register

Installs a new tag, live on the very next dispatch, persisted into state and
present in later sessions.

```
<register name="wc" doc="line count of a path">
def handle(body, **attrs):
    return str(len(open(body.strip()).read().splitlines()))
</register>
```

The body defines `handle(body, **attrs)`. `name` must be an identifier and must
not be a frozen tag. The source is stored, so the tool is rebuilt on load.

Register a tag when the same call has been written a third time, or when a task
has many units differing only by an argument. Do not register one for something
that happens once: the price is a catalog line in every request, forever.

### system

Writes or deletes a note. Notes are doctrine — they ride verbatim in every
prompt from then on, so they are the most expensive kind of memory and should
hold only what must shape every turn.

```
<system name="verify-dont-read">A dump is not verification.</system>
<system name="verify-dont-read" delete="1"/>
```

### tool

Rewrites a tool's one-line catalog description without touching its handler.

```
<tool name="wc" doc="count lines, words, bytes of a path"/>
```

### skill

Loads a full `SKILL.md` into the transcript. The catalog carries only names and
descriptions; the body is fetched on demand, which is why a skill is the cheap
place to put a long procedure.

```
<skill name="subagent-brief"/>
```

### reload

Rediscovers skills and extensions now, instead of at the next turn boundary.
Emit it after writing a `SKILL.md` if you want to load that skill in the same
turn.

### reload_sdk

Reimports `desmos.*`, reseeds missing builtins and rebinds `step` without
restarting the process. `ns`, notes and the transcript survive. The new ABI and
loop apply on the next `complete()` — not to the reply being written.

Required after editing anything under `desmos/`: without it the live kernel
keeps running the module it imported.

### evolve

Snapshots the grown state — tools, notes, descriptions — as the next numbered
generation under `.desmos/generations/`. The body is the reason, and it is worth
writing properly; it is what a future rollback reads.

```
<evolve>added the shell monitor and retired the polling guidance</evolve>
```

### rollback

Restores generation `n` counting back from now.

```
<rollback n="1"/>
```

### memory

Durable memory across sessions, kept out of the prompt except for a short
routing summary. The action comes from the `action` attribute, or from the first
word of the body.

| body | effect |
|---|---|
| `fact` | remember (default action) |
| `show` | print the index |
| `search PAT` | search (`grep` is an alias) |
| `read ID` | read one record |
| `forget ID` | drop one record |
| `verify ID` | re-check a record against reality |
| `consolidate` | fold the store |

Attributes: `action`, `id`, `scope` (default `repo`), `kind`, `confidence`,
`source`, plus `max` and `mode` for search.

## Grown tags

Everything else a live session shows is not in this repo. It was registered at
runtime by the agent and lives in `.desmos/harness.sqlite3` on that machine.
A fresh clone starts with the thirteen frozen tags and grows its own.

A mature session typically carries some of: `read`, `grep`, `commit`, `todo`,
`usage`, `traj`, `compact`, `see`, `sleeper`. None of them are guaranteed, and
the catalog in the system prompt is always the authoritative list — a tag not
named there does not exist in that session.

## Extension tags

An extension is a Python file under `.desmos/extensions/` or
`~/.desmos/extensions/` with a `load(api)` function. It can register tags and
hook dispatch:

```python
def load(api):
    api.tool("hello", "say hello", lambda body, **a: "hello " + body.strip())
    api.hook("before_dispatch", veto)

def veto(world, block):
    if block.tag == "bash" and "rm -rf /" in block.body:
        return "refused"   # a string replaces the result; the call never runs
```

The hook runs before the handler and can veto any call. See
[extensibility.md](extensibility.md).

## Subagents

`spawn`, `fanout`, `spawn_many`, `wait`, `gather`, `status`, `result`,
`structured_result` and `judgment` are Python functions on `desmos.subagent`,
usually reached through an `agents` tag a session has grown. A child is an
isolated `World` with its own transcript, a scoped tag set, and no ability to
write parent state. Depth is capped at 1.

Under a contract, `result(id)` is the child's story about its own work and
`judgment(id)` is the harness's verdict on that story. Read the verdict.
Details in [subagents.md](subagents.md).

## Choosing where a capability goes

| lifetime | put it in |
|---|---|
| this one call | nothing — just write the call |
| an operation you will run again | a grown tag (register) |
| a procedure with real detail | a skill (SKILL.md, loaded on demand) |
| doctrine that must shape every turn | a note (system) |
| behaviour for every session on this machine | an extension |

The deciding question is not effort. It is how many turns pay for it: a note and
a tag cost tokens on every request from now on, a skill costs one catalog line
until someone asks for it.
