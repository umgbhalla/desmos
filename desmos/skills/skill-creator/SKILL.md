---
name: skill-creator
description: Create a new desmos skill (Agent Skills SKILL.md, optional Python handle/run). Use when the user asks to add a skill or when a workflow should become a reusable package.
---

# Skill creator

Skills follow earendil-works/pi and Prime Agent.

## Markdown skill

```
.desmos/skills/<name>/SKILL.md     # project
~/.desmos/skills/<name>/SKILL.md   # user
```

```markdown
---
name: my-skill
description: What it does and when to use it. Be specific.
---

# My skill

Instructions the model loads with `<skill name="my-skill"/>`.
```

## Python-backed skill

Same `SKILL.md`, plus a module the kernel imports as `my_skill` (hyphens → underscores):

```
my-skill/
├── SKILL.md
└── src/my_skill/__init__.py    # or my_skill.py / skill.py
```

Export `handle(body, **attrs)` to also become an XML tag `<my_skill>`, or `run(...)` to call from Python as `my_skill.run(...)`.

Place project skills under `.desmos/skills/`. Then emit `<reload/>` — no
console restart. The next `complete()` (or a same-turn `<skill name="…"/>`
after `<reload/>`) sees it.
