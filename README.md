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

Frozen XML tags are the brainstem. Everything else — new tools, descriptions,
system notes, skills — the model writes itself from inside the kernel. The
next `complete()` sees the change. No restart.

```
<python>          exec, names persist
<bash>            shell in cwd
<edit>            unique replace (old --- new)
<register>        grow a new tag
<system>          write / delete a catalog note
<tool>            rewrite a tool description
<skill>           load a full SKILL.md
<reload>          rediscover skills/extensions now
<reload_sdk>      reimport desmos.* and rebind step
<evolve>          snapshot grown state
<rollback>        restore generation n=
```

## IPython

```bash
uv venv && uv pip install -e ".[kernel]"
source .venv/bin/activate
python -m desmos console          # IPython with step and world bound
python -m desmos tui              # story | calls | input  (needs cargo + vendor/grok-build)
python -m desmos tui --demo       # same layout, no API key
python -m desmos tui --grok       # grok-build pager, desmos as ACP agent
python -m desmos kernel           # install a Jupyter kernelspec named Desmos
```

Default TUI: middle is the turn story, right is wire calls (`complete()` and
syscalls, USER vs LLM). `--grok` launches grok-build's pager-bin (`--minimal
--no-leader`) with `python -m desmos acp` on stdio.

Ordinary cells stay Python. `step("...")` is the agent. Syscall results
append as user-role `<result>` blocks on the same transcript (Pi-style).
`<evolve>` / `<rollback>` snapshot grown state as numbered generations.
`<edit path="file">old\\n---\\nnew</edit>` is the Prime-style unique replace.

The agent updates itself: write a skill or a note, then `<reload/>`. After
editing the SDK: `<reload_sdk/>` (or `reload_sdk()` in a cell) reimports
`desmos.*` and rebinds `step` without restarting IPython. New ABI/loop apply
on the next `complete()`.

## Headless

`ANTHROPIC_API_KEY` comes from the environment. Never commit it.
`DESMOS_MODEL` overrides the default (`claude-opus-5`).
`DESMOS_THINKING` is the effort floor (`low` by default). Opus 5 uses adaptive
thinking; older Claude 4 models use a token budget plus interleaved thinking.

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

## Demos

Recorded with [termctrl](https://github.com/zeu5/termctrl) driving the offline
`--demo` TUI, so a capture needs no API key.

    ./scripts/record-demo.sh panes     # story vs wire, folding, POST tree
    ./scripts/record-demo.sh wire      # tail stays open, overflow counted
    ./scripts/record-demo.sh zoom      # zoom a block into the pager, search

All three export **2600x1448 at 60fps**, verified with ffprobe. Resolution is
cell geometry, not a flag:

    width  = (cols * cell_width  + 2 * padding) * pixel_ratio
    height = (rows * cell_height + 2 * padding) * pixel_ratio

Defaults are `140x38` cells at `9x18`, padding `20`, ratio `2`. Override per run:

    COLS=160 ROWS=45 RATIO=3 ./scripts/record-demo.sh panes   # 4320x2532

Raise `RATIO` instead of upscaling afterwards: termctrl rasterises at the target
size, so glyph edges stay sharp. Exact 16:9 is not reachable with integer cells
-- 2600x1448 is 1.795.

`--record` keeps the raw `.termctrl` (original timing, bytes, input, markers), so
re-cutting a clip never re-runs the session:

    termctrl markers captures/panes.termctrl
    termctrl video captures/panes.termctrl --edit plan.json --footer

Two things that bite: input atoms need `text:<char>` (a bare `j` is rejected),
and `termctrl start` is not idempotent, so the script stops and prunes the
session name first.

### What is worth filming

One idea per clip. The offline demo is static content, so navigation scenes
yield only 3-5 unique screens. Streaming, queueing, subagents and the diff card
need a live session (`--live`, which does spend tokens).

| scene | shows | offline |
|---|---|---|
| `panes` | prose left, the syscalls that produced it right | yes |
| `wire` | newest cards open, older folded, `N more up` | yes |
| `zoom` | block viewer: search, wrap, raw | yes |
| stream | a syscall card opening, stdout arriving into it | no |
| queue | Enter stacking follow-ups mid-step, `[` `]` reorder | no |
| spawn | SubagentBlock, Enter into the child session | no |
| diff | an edit card rendering hunks | no |
| lag | `POST out #5  waiting #6` mid-step | no |
