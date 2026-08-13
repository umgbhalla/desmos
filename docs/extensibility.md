# Extensibility

Modeled on [earendil-works/pi](https://github.com/earendil-works/pi) (the Pi
harness Prime Agent is built on) and Prime's Python-backed skills.

## Mapping

| Pi / Prime | desmos |
|---|---|
| Frozen base system prompt | `ABI` in `inverted.py` |
| Skills: name+description in prompt | `<available_skills>` in the live catalog |
| Full `SKILL.md` on demand via read/ipython | `<skill name="…"/>` |
| Python skill imported in the kernel | `ns[import_name] = module`, optional XML tag |
| Extensions `export default (pi) =>` | `def load(api):` |
| `pi.registerTool` | `api.register_tool(name, doc, handler)` |
| `pi.on("tool_call")` | `api.on("before_dispatch", fn)` |
| `~/.pi/agent/skills`, `.pi/skills` | `~/.desmos/skills`, `.desmos/skills` |
| `~/.agents/skills` interop | same |
| Continual harness notes | `<system name>` + `.desmos/harness.json` |
| Packages (npm/git) | not yet |

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
