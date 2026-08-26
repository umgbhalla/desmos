# Comet frontend

Desmos's native GPUI frontend is the vendored Comet (`vendor/comet`) binary
`zeron`, launched over ACP. This is not a second agent: Comet starts
`python -m desmos acp` (NDJSON JSON-RPC 2.0) and renders the streamed events
with its own markdown, diff, session registry, and alacritty terminals.

## Setup and launch

```text
git submodule update --init vendor/comet vendor/grok-build
python -m desmos comet --cwd .
```

The first launch hash-gates a debug `zeron` build (same idea as
`python -m desmos tui`): sources under `vendor/comet/{apps,crates}` plus the
lockfile. Unchanged bytes skip cargo. `--release` builds the release profile.
`--no-build` launches whatever binary is already there. Extra argv after `--`
is forwarded to `zeron` (`python -m desmos comet -- status`).

The launcher writes `vendor/comet/target/desmos-acp`, a wrapper that execs
`python -m desmos "$@"`. Comet's Desmos spec is `{executable} acp` with
`DESMOS_ACP_EXECUTABLE` pointing at that wrapper, so a console script named
`desmos` is not required. `DESMOS_CWD` is the `--cwd` you passed.

`DESMOS_COMET_BINARY` launches a prebuilt `zeron` and still installs the
wrapper so the binary's Desmos harness talks to this checkout.

Inside Comet, create a chat and choose **Desmos**. The chat's workspace is
the ACP `session/new` cwd. Resume uses `session/load` of the stored ACP
session id (the same uuid `acp_sessions` binds).

## What Comet actually paints

Desmos tags every `session/update` with `_meta.desmos.pane` (`story` |
`activity`) and `family`. Comet's normalizer maps that wire, and the GPUI
shell splits it into two panes:

- speech → `TextDelta` (story markdown)
- thinking → `ReasoningDelta` → `MessagePart::Thought` on **Story** (muted
  markdown, left rail, "thinking" label on the first block). Empty deltas
  stay heartbeats. Activity omits thought.
- `complete()` is kind `other`, so the chip is `ToolCall::Unknown { name: complete }`,
  not a WebFetch. Activity pane only.
- kernel syscalls (`kind: execute`, `rawInput.tag`) → `ToolCall::Exec` (Activity)
- edit diffs → the Changes pane (Comet's diff surface) and Activity chips

Story is the main transcript: user prompts, thinking, assistant markdown.
Activity is a right-pane surface (auto-opened on the first Desmos chat in a
run). Other harnesses still chip tools in the thread.

Steering: initialize advertises `_meta.steering.supported` and
`_session/steering`. The Desmos spec is `SteeringMode::StepBoundary` with
the thought_level ladder (low / medium / high / xhigh). Live models still
come from `session/new` `configOptions`. Mid-turn Comet calls
`_session/steering` with ACP prompt blocks; idle returns
`outcome: promptRequired` so Comet starts the next prompt instead of
injecting into a finished turn.

The bottom dock (⌘J) on a Desmos chat opens an alacritty tab over
`world.shells` (`OpenTerminal` `kind: kernel` → `_session/term` bytes /
run / interrupt). Extra `+` tabs are still login PTYs. The kernel tab
needs a live ACP child, so send a prompt once; after that the session
parks and the mailbox stays warm. Line-oriented, same contract as desk
xterm, not a raw login PTY.

## Current scope

Supported now:

- hash-gated `zeron` launch over this checkout's ACP server;
- create / resume a Desmos ACP session for a workspace;
- stream assistant markdown and thinking on **Story** (`MessagePart::Thought`);
- `complete()` cards and syscall chips on **Activity** (separate GPUI pane);
- model and thought_level from Desmos `configOptions`;
- mid-turn steering (`_session/steering`);
- Comet session registry + `session/load` of the ACP uuid;
- kernel PTY in the alacritty dock (`world.shells` / `_session/term`);
- extra login-shell tabs from the dock `+`.

Not the same object as the TUI / desk:

- gpuix React-on-GPUI markdown/diff (Comet paints with pulldown-cmark + its
  own Changes pane; grok `StreamingMarkdownRenderer` is ratatui/Syntect —
  hosting either in Comet without a grok-build half-copy is still a gap);
- Zeron CRDT devices page (no Desmos analog. `persist.peers()` is the honest one);
- attaching Comet and the TUI as two writers on `.desmos/bridge.sock`.

Headed Linux without a GPU: `mesa-vulkan-drivers` (lavapipe). This VM's
`vkCreateInstance` used to die with "Found no drivers"; with
`VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.json` wgpu selects
`llvmpipe` and `python -m desmos comet` maps a real zeron window.

Comet is pinned as a Git submodule of the `umgbhalla/comet` fork. Harness
changes land in that repository, then the root gitlink moves. The launcher
does not patch Comet at runtime.
