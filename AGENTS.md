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

desmos is a self-improving harness. The kernel owns the loop. `complete()` is a gland. XML syscalls are the capability surface, and that surface is writable state: notes, tags, skills and the SDK itself can be rewritten from inside a turn and are live on the next dispatch, bounded by `evolve`/`rollback`. Data lives in kernel variables; the model peeks by name. Growth is files plus a live catalog, not a fatter `messages[]`.

- Frozen ABI: `desmos/const.py` (`ABI`, `FROZEN`). Do not casually rewrite the brainstem.
- Live catalog + `# runtime`: `desmos/catalog.py`. System prompt is ABI + catalog. It must explain how the system actually works (panes, wire calls, markdown, cache, reload, ACP). A path dump is not an explanation.
- Loop: `desmos/loop.py`. `step` / `turn` / `run_turns`. `new_world(persist=False)` for children. `reload_sdk` reimports without wiping ns / notes / messages.
- Transcript: append-only within a session — nothing already sent is rewritten or reordered. Syscall output arrives as user-role `<result>` blocks. Never emit a result block in model speech. Never restate the task into the transcript. Two exceptions exist and both are explicit: what survives a process restart is the tail persist keeps, not the whole chat, and `reset()` / the TUI reset op drops the chat outright so a poisoned turn cannot train the next one. A server-side fold is not a local rewrite — the provider's own compaction block replaces the prefix.
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
| Fake persist / child writing the parent's harness state | `persist=False`, `state_path=None` |
| Fake `<result>` in assistant speech | Dispatcher-owned user `<result>` only |
| Self-closing tags the scanner missed | `scan.py` must see `<tag/>` |
| Trajectory race via `len(dir)+1` | Unique names, atomic replace |
| `# runtime` as a path dump that does not say how the TUI, cache, or complete cards work | Runtime block that teaches the live system |

The pulldown walker is gone. Do not put it back.

## TUI and grok-build

- Default TUI is `python -m desmos tui` → `crates/desmos-tui` hosting grok-build `ScrollbackState` / `ScrollbackPane`. Story = UserPrompt / Thinking / AgentMessage. Calls = ToolCall. Tab/j/k/h/l, click select, double-click fold. Needs `vendor/grok-build`. Do not flatten blocks to an `out` label.
- `--grok` attaches grok-build's pager (`--minimal --no-leader`) with `python -m desmos acp` on stdio. ACP is NDJSON JSON-RPC 2.0, not Content-Length.
- `vendor/grok-build` is committed source, not a clone and not a setup step. A fresh clone must build `cargo build -p desmos-tui` without fetching it. Do not vendor a half-copy of one crate and call that "using grok-build," and do not re-gitignore it: a `path = ` dep that is on your disk and not in the repo compiles here and breaks for whoever clones next, with no compile error anywhere to say so.
- A change to a vendored crate is an ordinary commit in `vendor/`. There is no patch series, so nothing will re-apply it for you: **an edit you leave unstaged is an edit that only exists on your machine**, and the build stays green while it does. Stage the vendor files in the same commit as the code that needs them.
- `python -m desmos check` guards exactly two things here. `_check_path_deps_tracked` asks git — not the filesystem — whether every `path = ` dep in the root `Cargo.toml` is tracked; it exists because a bare `build/` in a global gitignore once swallowed `crates/build/xai-proto-build`. `_check_vendor_patch` asserts the vendored pager still carries our DESMOS_ACP branch (`std::env::var("DESMOS_ACP")` in `acp/mod.rs`, `pub async fn spawn_stdio_acp` in `acp/spawn.rs`), because pulling upstream over that file compiles fine and silently hands `--grok` back to grok's own in-process agent.
- The TUI build is hash-gated and PROTOC-pinned: `python -m desmos tui` hashes our sources and only shells out to cargo when they moved, and `.cargo/config.toml` forces `PROTOC=scripts/protoc` so the pager graph does not rebuild every launch. Do not bypass the wrapper or bolt RUSTFLAGS onto that path.
- A vendored crate is only ownable in `crates/` if every vendored consumer names it `{ workspace = true }` — our root `[workspace.dependencies]` then redirects it (this is how `xai-grok-markdown` works). Crates reached by a hard `path = "../..."` (`xai-grok-pager-diff`, `-render`, `xai-grok-paths`) cannot be redirected, and their types cross the pager API, so a local copy will not typecheck.
- Do not hide syscalls from the human. The right pane exists so the wire is visible.
- `<edit>` is the one syscall that also gets a story card, folded, pushed the moment the result lands (`story_edit_card`). It is the narrative, not just wire traffic. Edits are therefore excluded from the work-run sentence — do not add them back, that prints `edit ×3` above three cards naming the files. Every other tag stays wire-only.
- Call groups in the wire pane are one per `complete()` POST, recorded at push time in `App::call_groups` (`call_push_group`). The pager's own turn navigation keys off `RenderBlock::UserPrompt`, which the wire pane never has, so do not reach for `next_turn`/`prev_turn` there. `[`/`]` step groups; arrows stay fold.
- Do not leak API keys into the TUI, logs, trajectory files, commits, or screenshots.

## Checks

`python -m desmos check` is the floor, not theater. If you change scan, dispatch, cache, persist, isolation, edit, or ACP, extend the check with a real repro, then run it. Do not add an assert that only passes on the happy fixture you just wrote.

### Never assert that a string exists

`assert "batching:" in prompt` is not a test. It asserts that a sentence you
just wrote is still the sentence you wrote. It fails the moment someone
rewords a line that is working perfectly, and it passes while the behaviour it
was supposedly guarding is completely broken. It tests the author's memory,
not the program. Ninety of these were deleted from `check.py` in one commit
and nothing was lost, because they had never once caught anything.

Do not write a check that:

- greps the system prompt, the ABI, or the catalog for wording
- asserts a doc line, a comment, a help string, or a log message is present
- asserts a constant still equals the literal it was defined as
- exists so that a diff "has a test in it"

Write the check that fails when the program is wrong. It runs the thing, and
it asserts on what came back:

- `run_bash("sleep 20 & echo x", timeout=3)` returns in under 8 seconds —
  that one caught a 20-second hang nobody knew about
- a truncated SSE stream raises instead of parsing as a finished answer
- a foreign `signature` never reaches the wire, so a provider switch cannot
  400
- `cd /etc` in one `<shell>` call is still `/etc` in the next

The distinction is not "does it involve a string". Asserting a secret was
redacted out of a result, or that a syscall's output round-tripped into the
transcript, is behaviour that happens to be spelled with characters. The test
is whether the assertion can fail for a reason that matters. If the only way
to break it is to edit prose, delete it.

## Git

Stage explicit paths. Never `git add -A`. Do not sweep someone else's in-progress TUI/ACP into a commit about tags. Do not commit `.desmos/`, keys, or `runs/`. `vendor/` is committed on purpose — see above.
