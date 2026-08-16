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
