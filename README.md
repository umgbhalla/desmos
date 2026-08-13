# desmos

A coding agent that owns its harness.

You put data in the kernel. You write a prompt. You call `step`. The model
peeks at variables by name. It does not get their contents stuffed into the
chat. Sequential `step` calls see prior prompts and answers.

```python
doc = open("paper.txt").read()
step("what's in doc? don't dump it")
step("ok, now list the functions it defines")
```

Five XML tags are frozen. Everything else — new tools, descriptions, system
notes — the model writes itself, and the next turn sees the change.

```
<python>          exec, names persist
<bash>            shell in cwd
<register>        grow a new tag
<system>          write / delete a system note
<tool>            rewrite a tool description
```

## IPython

```bash
uv venv && uv pip install -e ".[kernel]"
source .venv/bin/activate
python -m desmos console          # IPython with step and world bound
python -m desmos kernel           # install a Jupyter kernelspec named Desmos
```

Ordinary cells stay Python. `step("...")` is the agent. Syscall results
append as user-role `<result>` blocks on the same transcript (Pi-style).
`<evolve>` / `<rollback>` snapshot grown state as numbered generations.
`<edit path="file">old\\n---\\nnew</edit>` is the Prime-style unique replace.

After editing the SDK itself: `reload_sdk()` reimports `desmos.*` and rebinds
`step` without restarting IPython.

## Headless

`ANTHROPIC_API_KEY` comes from the environment. Never commit it.
`DESMOS_MODEL` overrides the default (`claude-opus-5`).

```bash
python -m desmos check
python -m desmos run "add a --json flag to inverted.py --check"
# or
python inverted.py --check
python inverted.py "task"
```

State lands in `.desmos/harness.json` (gitignored): grown tools, notes, prior
steps. Traces go under `runs/`.

## Skills and extensions (Pi / Prime grain)

Same shape as [earendil-works/pi](https://github.com/earendil-works/pi) and Prime
Agent. The base ABI stays frozen. Capability is discovered, not baked in.

**Skills** — Agent Skills `SKILL.md`. Catalog is name + description only.
`<skill name="…"/>` loads the full file. Python-backed skills (a `handle`/`run`
module) are imported into the kernel.

```
~/.desmos/skills/<name>/SKILL.md
.desmos/skills/<name>/SKILL.md
~/.agents/skills/          # shared with other harnesses
.agents/skills/
```

**Extensions** — `load(api)` Python files. They can `api.register_tool` or
`api.on("before_dispatch", …)`.

```
~/.desmos/extensions/*.py
.desmos/extensions/*.py
```

See [docs/extensibility.md](docs/extensibility.md).
