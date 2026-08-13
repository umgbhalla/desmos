# desmos

A coding agent that owns its harness.

The model lives in a persistent Python kernel. It does not call a closed tool
schema. It emits XML syscalls. Five tags are frozen. Everything else — new
tools, tool descriptions, system notes — it writes itself, and the next turn
sees the change.

```
<python>          exec, names persist
<bash>            shell in cwd
<register>        grow a new tag
<system>          write / delete a system note
<tool>            rewrite a tool description
```

The live prompt is the ABI plus the current catalog plus the agent's notes.
That is the inverted harness: the creature edits the thing that governs it.

## Run

`ANTHROPIC_API_KEY` comes from the environment. Never commit it.

```bash
python3 inverted.py --check

export ANTHROPIC_API_KEY=...
python3 inverted.py "add a --json flag to inverted.py --check"
```

State lands in `.desmos/harness.json` (gitignored): grown tools, their source,
rewritten descriptions, system notes. Traces go under `runs/` (also gitignored).

## Docs

- [docs/inverted-rlm.html](docs/inverted-rlm.html) — kernel as organism
- [docs/kernel-agent.html](docs/kernel-agent.html) — earlier sketch
