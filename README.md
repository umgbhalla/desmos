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
<bash>            isolated one-shot in cwd, no state kept
<shell>           preferred persistent pty; 5s read windows, commands survive
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
./scripts/vendor-setup.sh         # clone vendor/grok-build at the pinned rev + apply patches/
git submodule update --init vendor/comet
python -m desmos console          # IPython with step and world bound
python -m desmos tui              # story | calls | input  (needs cargo + vendor/grok-build)
python -m desmos tui --demo       # same layout, no API key
python -m desmos tui --grok       # grok-build pager, desmos as ACP agent
python -m desmos comet            # Comet desktop frontend via ACP
python -m desmos kernel           # install a Jupyter kernelspec named Desmos
```

Default TUI, left column then right:

```
story        the turn: your prompt, thinking, speech as markdown,
             and each <edit> as a folded diff card (→ opens, ⏎ zooms)
POST in/out  the last complete() request and reply as a folding JSON tree
queue        follow-ups stacked while a step runs (hidden when empty)
input        the composer

calls        the wire: complete() cards and every syscall with body + result
meta         context bar, cache hit, spend
git          status / branches / log, tabbed
files        the file the git cursor points at, or the filesystem
keys         what the focused pane's keys do
```

`ctrl+p` opens the settings picker (provider, model, effort). `ctrl+g` and
`ctrl+b` toggle the git and file panes. Tab cycles panes; in every pane the
arrows mean the same thing — up/down moves the cursor, left/right drives that
pane's second axis (fold, tab, directory, order).

In calls, `[` and `]` step whole groups — one group per `complete()` POST,
holding the syscalls that POST produced. The pane title counts them (`#2/5`).
Arrows still mean fold, so the group step gets its own pair of keys.

`--grok` launches grok-build's pager-bin (`--minimal --no-leader`) with
`python -m desmos acp` on stdio. `desmos comet` builds and launches the
vendored Comet desktop frontend with Desmos registered as an ACP harness; see
[the Comet frontend guide](docs/comet-frontend.md) for scope and setup.

`vendor/grok-build` is gitignored, but `patches/` is not: `DESMOS_ACP` is our
patch on the vendored pager, not upstream. Run `scripts/vendor-setup.sh` after
any vendor pull or the ACP bridge goes missing with no compile error.

Ordinary cells stay Python. `step("...")` is the agent. Syscall results
append as user-role `<result>` blocks on the same transcript (Pi-style).
`<evolve>` / `<rollback>` snapshot grown state as numbered generations.
`<edit path="file">old\\n---\\nnew</edit>` is the Prime-style unique replace.

The agent updates itself: write a skill or a note, then `<reload/>`. After
editing the SDK: `<reload_sdk/>` (or `reload_sdk()` in a cell) reimports
`desmos.*` and rebinds `step` without restarting IPython. New ABI/loop apply
on the next `complete()`.

## Two providers

Anthropic and OpenAI, one transcript, switchable mid-session.

```
anthropic   claude-opus-5, claude-sonnet-4-6      ANTHROPIC_API_KEY
openai      gpt-5.6-sol, -luna, -terra            device login, ~/.desmos/auth.json
```

`ctrl+p` in the TUI picks provider → model → effort and saves the choice to
`~/.desmos/settings.json` (`DESMOS_SETTINGS` moves that file). The saved choice
outranks whatever the last session persisted. A machine with no settings file
has not been onboarded, so the TUI opens the picker instead of guessing.

Switching keeps the transcript. Blocks the other provider made survive as
plain text — lossy, never fatal — because a reasoning item is opaque to
anything but the endpoint that produced it. Nothing is compacted or discarded
to make a switch work.

The system prompt adapts to the family it is driving (`desmos/dialect.py`): the
capability half is identical, the working-style half is not. Asking Opus 5 for
brevity shortens its answers; asking GPT-5.6 the same thing shortens the
artifact instead, so it is not asked.

Both providers fold the transcript server-side once it grows past the trigger —
Anthropic via `compact_20260112`, OpenAI via Responses `context_management`.
The returned block is opaque, replayed verbatim, and is the cut point for
everything before it. desmos never rewrites history locally; a fold paints a
`FOLD` card on the wire pane.

`ANTHROPIC_API_KEY` comes from the environment. Never commit it.
`DESMOS_MODEL` overrides the default (`claude-opus-5`).
`DESMOS_THINKING` is the effort floor (`low` by default). Opus 5 uses adaptive
thinking; older Claude 4 models use a token budget plus interleaved thinking.
On OpenAI the same dial becomes `reasoning.effort`.

## Headless

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
