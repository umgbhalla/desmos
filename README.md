# desmos

A coding agent that owns its harness.

You put data in the kernel. You write a prompt. You call `step`. The model
peeks at variables by name. It does not get their contents stuffed into the
chat.

```python
doc = open("paper.txt").read()
step("what's in doc? don't dump it")
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
uv pip install -e ".[kernel]"
python -m desmos console          # IPython with step and world bound
python -m desmos kernel           # install a Jupyter kernelspec named Desmos
```

Ordinary cells stay Python. `step("...")` is the agent.

## Headless

`ANTHROPIC_API_KEY` comes from the environment. Never commit it.

```bash
python3 inverted.py --check
python3 inverted.py "add a --json flag to inverted.py --check"
```

State lands in `.desmos/harness.json` (gitignored). Traces go under `runs/`.

