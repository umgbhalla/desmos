# Extensibility

Modeled on [earendil-works/pi](https://github.com/earendil-works/pi) (the Pi
harness Prime Agent is built on) and Prime's Python-backed skills.

## Mapping

| Pi / Prime | desmos |
|---|---|
| Frozen base system prompt | `ABI` in `desmos/const.py` (system block 1) |
| Live catalog + `# runtime` | `desmos/catalog.py` (system block 2) |
| Skills: name+description in prompt | `<available_skills>` in the live catalog |
| Mid-turn rediscover | `<reload/>` |
| Live-reload the SDK | `<reload_sdk/>` |
| Full `SKILL.md` on demand via read/ipython | `<skill name="…"/>` |
| Python skill imported in the kernel | `ns[import_name] = module`, optional XML tag |
| Extensions `export default (pi) =>` | `def load(api):` |
| `pi.registerTool` | `api.register_tool(name, doc, handler)` |
| `pi.on("tool_call")` | `api.on("before_dispatch", fn)` |
| `~/.pi/agent/skills`, `.pi/skills` | `~/.desmos/skills`, `.desmos/skills` |
| `~/.agents/skills` interop | same |
| Continual harness notes | `<system name>` + `.desmos/harness.json` |
| Packages (npm/git) | not yet |

## Persistent-kernel diagnostics

Every world gets a collision-safe `diag` object in its Python namespace. It
returns bounded plain data so inspecting a failure never retains traceback
frames, locals, subprocesses, or arbitrary user values.

- `diag.error(clear=False)` returns the last uncaught Python-block exception,
  recorded automatically with type, message, and leaf-oriented frame metadata.
- `diag.symbol(obj, source=False, max_chars=8192)` returns location, signature,
  and optional clipped source metadata without resolving dotted names.
- `diag.threads(pattern=None, limit=32, depth=12, max_chars=16384)` returns
  thread state and bounded leaf-first file/function/line stacks, never locals.

Installation uses `setdefault` semantics for user-owned names: if a caller
already supplied a different `diag`, Desmos leaves it untouched. SDK reloads
migrate Desmos' own diagnostic object and preserve its last error.

## Skill

```
my-skill/
├── SKILL.md
└── src/my_skill/__init__.py   # optional; handle() or run()
```

```markdown
---
name: my-skill
description: When to load this. Be specific.
---
```

## Extension

```python
# .desmos/extensions/guard.py
def load(api):
    def before_dispatch(world, block):
        if block.tag == "bash" and "rm -rf /" in block.body:
            return "blocked"
        return None
    api.on("before_dispatch", before_dispatch)
```

`api.hook` and `api.tool` are accepted spellings of `api.on` and
`api.register_tool`. A file that raises on import no longer disappears: the
error is collected and printed by `reload`.

## Hook points

| Event | Fired | Signature | Return |
| --- | --- | --- | --- |
| `before_dispatch` | `kernel/dispatch.py`, before a syscall runs | `(world, block)` | a string replaces the result and the call never runs |
| `compacted` | `kernel/loop.py`, when the server folds the transcript | `(world, info)` with `n`, `kept`, `text` | ignored; a raise is recorded on the log entry |

A session can register a hook straight from the kernel without writing a file,
and it now survives `install_resources` — which runs at the top of every run:

```python
world.hooks.setdefault("compacted", []).append(fn)
```

The loader retires only the hooks it installed, so a reload replaces extension
hooks and leaves session-registered ones alone.

## Pattern: handing off across a fold

A fold destroys earlier turns for the model that wakes up after it, and that
model cannot read what went. Notes ride in every request, so a note survives a
fold by construction — which makes a note the only place a pre-fold self can
leave something for a post-fold self.

Splitting the note in two is what makes this work. The agent writes what
matters by hand and the hook never touches it; the hook writes only what
cannot be known in advance — that a fold happened, and the volatile state that
was true at that moment.

```python
# .desmos/extensions/handoff.py
MARK = "--- above: written at each fold. below: mine, never rewritten ---"

def on_fold(world, info):
    body = world.notes.get("handoff", "")
    mine = body.split(MARK, 1)[1].lstrip("\n") if MARK in body else body
    world.notes["handoff"] = stamp(world, info) + "\n\n" + MARK + "\n" + mine
    from desmos.state.persist import save
    save(world)

def load(api):
    api.hook("compacted", on_fold)
```

`stamp` is free to collect whatever the transcript was carrying and the note
was not: the fold counts, `HEAD`, the dirty files, the open todo rows, the
server's summary. The general shape — *a hook fires at the moment state is
destroyed, and copies the perishable part into something that outlives it* —
is not specific to folds.
