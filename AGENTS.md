# AGENTS.md

This file is binding for every coding agent that touches this repo (Grok, Claude, Codex, Cursor, or anything else). It is not optional flavor text.

## You are not allowed to build toy state of anything

Not markdown. Not the TUI. Not persist. Not the loop. Not cache. Not ACP. Not events. Not subagents. Not thinking. Not scroll. Not "just for now." Not a demo that pretends.

If a real implementation exists, you wire that. If it does not exist, you build the real contract, or you stop and say what is missing. You do not ship a lookalike.

### What "toy state" means here

A thing is toy if any of these are true:

- It compiles, renders, or demos, but does not carry the real contract.
- It reimplements a subset of something that already exists (a walker, a viewport, a fake event stream, a fake persist, a fake syscall result).
- It stores meaning in speech, comments, screenshots, or in-memory theater instead of the durable place the next turn / next process will actually read.
- It duplicates data across surfaces so two panes or two logs look "full" while only one is real.
- It is a stub, a hardcoded fixture, a pulldown walker, a homemade theme, a fake ACP handshake, a `len(dir)+1` id, or a `try/except: pass` that swallows the failure the user needed to see.
- You would have to explain it as "placeholder," "good enough for now," "we can swap later," or "just to show the layout."

If you catch yourself writing that sentence, delete the code. Do not commit it.

### What to do instead

1. Find the real object. Read it. Call it. Do not paraphrase it into a smaller thing.
2. If the real object lives in `vendor/grok-build`, use it as the source of truth for behavior. Do not copy grok.com product surface (auth, leader, mermaid marketplace, voice). Do copy contracts: markdown rendering, pager ACP shape, cache breakpoints, event semantics.
3. If wiring the real object is blocked (workspace deps, gitignore, missing crate membership), say the block. Do not invent a 80-line stand-in so the screen still looks busy.
4. A `--demo` flag may drive the same code path with canned events. It may not be a second engine.

## This repo

desmos is an inverted harness. The kernel owns the loop. `complete()` is a gland. XML tags are syscalls. Data lives in kernel variables; the model peeks by name. Growth is files plus a live catalog, not a fatter `messages[]`.

- Frozen ABI: `desmos/const.py` (`ABI`, `FROZEN`). Do not casually rewrite the brainstem.
- Live catalog + `# runtime`: `desmos/catalog.py`. System prompt is ABI + catalog. It must explain how the system actually works (panes, wire calls, markdown, cache, reload, ACP). A path dump is not an explanation.
- Loop: `desmos/loop.py`. `step` / `turn` / `run_turns`. `new_world(persist=False)` for children. `reload_sdk` reimports without wiping ns / notes / messages.
- Transcript: append-only. Syscall output arrives as user-role `<result>` blocks. Never emit a result block in model speech. Never restate the task into the transcript.
- Cache (Pi / Anthropic): ABI system block, catalog system block, last **user** only. Do not move the breakpoint onto the assistant.
- Thinking: Opus 5 is adaptive (`thinking: {type:adaptive}` + `output_config.effort`). Default effort is `low`. Older Claude 4 uses a token budget + interleaved beta. Do not fake thinking blocks.
- Subagents: isolated `World`, `persist=False`, depth cap, persona/capability. Unknown wait ids must not KeyError. Turn-cap must salvage, not vanish.
- Persist: skip load/save when `persist=False`. Trajectory writes are unique names + `os.replace`, not `len(dir)+1`.
- Edit: compile `.py` before write. Refuse ambiguous `---` bodies.
- Speech is not memory. If future-you needs it, it is a note, a skill, or a named object the index still lists.

## Surfaces that have already been built as toys (do not repeat)

These happened. They were rejected. Do not recreate them.

| Toy that got shipped | Real thing |
|---|---|
| Homemade inline viewport / copied `xai-ratatui-inline` as "the TUI" | Three-pane `desmos-tui`: middle = turn story, right = wire calls, bottom = input |
| Same text on the story pane and the calls pane | Story is grok `UserPrompt` / `Thinking` / `AgentMessage`. Calls are grok `ToolCall` for `complete()` and syscalls. Do not mirror. Do not stamp everything `out`. |
| `md_lines` pulldown-cmark walker in `crates/desmos-tui` | `crates/xai-grok-markdown` (vendored grok-build crate) via `StreamingMarkdownRenderer::finish` + Syntect Tokyo Night + grok `md_style` colors. Do not put the walker back. |
| Fake persist / child writing parent `harness.json` | `persist=False`, `state_path=None` |
| Fake `<result>` in assistant speech | Dispatcher-owned user `<result>` only |
| Self-closing tags the scanner missed | `scan.py` must see `<tag/>` |
| Trajectory race via `len(dir)+1` | Unique names, atomic replace |
| `# runtime` as a path dump that does not say how the TUI, cache, or complete cards work | Runtime block that teaches the live system |

The pulldown walker is gone. Do not put it back.

## TUI and grok-build

- Default TUI is `python -m desmos tui` → `crates/desmos-tui` hosting grok-build `ScrollbackState` / `ScrollbackPane`. Story = UserPrompt / Thinking / AgentMessage. Calls = ToolCall. Tab/j/k/h/l, click select, double-click fold. Needs `vendor/grok-build`. Do not flatten blocks to an `out` label.
- `--grok` attaches grok-build's pager (`--minimal --no-leader`) with `python -m desmos acp` on stdio. ACP is NDJSON JSON-RPC 2.0, not Content-Length.
- `vendor/grok-build` is a gitignored analysis clone. Do not commit it. Do not vendor a half-copy of one crate and call that "using grok-build."
- Our changes to vendored crates live in `patches/`, applied by `scripts/vendor-setup.sh` (pinned rev in that script). `python -m desmos check` asserts the clone sits at the pin with every patch applied — silent when there is no clone, loud when there is one and it disagrees. Never leave a vendor edit only in the working tree — it is gitignored, so it dies on the next clone with no compile error. Re-run the script after any vendor pull, and refresh the patch (`git -C vendor/grok-build diff > patches/NNNN-*.patch`) after any vendor edit.
- A vendored crate is only ownable in `crates/` if every vendored consumer names it `{ workspace = true }` — our root `[workspace.dependencies]` then redirects it (this is how `xai-grok-markdown` works). Crates reached by a hard `path = "../..."` (`xai-grok-pager-diff`, `-render`, `xai-grok-paths`) cannot be redirected, and their types cross the pager API, so a local copy will not typecheck.
- Do not hide syscalls from the human. The right pane exists so the wire is visible.
- `<edit>` is the one syscall that also gets a story card, folded, pushed the moment the result lands (`story_edit_card`). It is the narrative, not just wire traffic. Edits are therefore excluded from the work-run sentence — do not add them back, that prints `edit ×3` above three cards naming the files. Every other tag stays wire-only.
- Call groups in the wire pane are one per `complete()` POST, recorded at push time in `App::call_groups` (`call_push_group`). The pager's own turn navigation keys off `RenderBlock::UserPrompt`, which the wire pane never has, so do not reach for `next_turn`/`prev_turn` there. `[`/`]` step groups; arrows stay fold.
- Do not leak API keys into the TUI, logs, trajectory files, commits, or screenshots.

## Checks

`python -m desmos check` is the floor, not theater. If you change scan, dispatch, cache, persist, isolation, edit, or ACP, extend the check with a real repro, then run it. Do not add an assert that only passes on the happy fixture you just wrote.

## Git

Stage explicit paths. Never `git add -A`. Do not sweep someone else's in-progress TUI/ACP into a commit about tags. Do not commit `.desmos/`, keys, or `vendor/`.
