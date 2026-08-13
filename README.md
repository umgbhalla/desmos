# desmos

Inverted harness experiment. The model is a gland. The kernel is the organism.

A frontier model lives in a persistent Python namespace. It does not call tools. It emits XML syscalls. The only frozen tags are `<python>` and `<register>`. Everything else it must grow. The chat can be wiped mid-run. The heap cannot.

```
host.complete()     # Anthropic only, key from the environment
scan / step         # frozen
<python>            # exec in a surviving namespace
<register>          # install a new tag
messages[]          # optional, starve-able
```

## Run

Needs `ANTHROPIC_API_KEY` in the environment. Do not put it in a file that gets committed.

```bash
python3 inverted.py --check

export ANTHROPIC_API_KEY=...   # not in git
python3 inverted.py --model claude-opus-5 --starve-after 4 --max-turns 12
```

`--starve-after N` wipes conversational memory after N turns and leaves the kernel standing. `--starve-after 0` is the control (chat lives).

Writes under `runs/` (gitignored).

## What we already saw

Opus 5 will invent appendages (`<grep>`, `<read>`) if the transcript is still alive. It will find the thesis in this repo and still not write `SURVIVAL`. Starve makes it re-orient instead of pin. Tabula rasa grows a REPL, not a mind.

## Docs

- [docs/inverted-rlm.html](docs/inverted-rlm.html) — kernel as organism
- [docs/kernel-agent.html](docs/kernel-agent.html) — earlier sketch
