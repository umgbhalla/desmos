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
`activity`) and `family`. Comet's normalizer maps that wire:

- speech → `TextDelta` (transcript markdown)
- thinking → `ReasoningDelta`
- `complete()` is kind `other`, so the chip is `ToolCall::Unknown { name: complete }`,
  not a WebFetch
- kernel syscalls (`kind: execute`, `rawInput.tag`) → `ToolCall::Exec`
- edit diffs → the Changes pane (Comet's diff surface)

That is Comet craft: one transcript with tool chips, diffs in the right
pane, sessions in the CRDT registry, terminals as engine alacritty PTYs.
It is not the TUI's three-pane layout. Story vs activity is preserved on
the wire; Comet does not grow a second Activity pane.

Steering: initialize advertises `_meta.steering.supported` and
`_session/steering`. The Desmos spec is `SteeringMode::StepBoundary` with
the thought_level ladder (low / medium / high / xhigh). Live models still
come from `session/new` `configOptions`. Mid-turn Comet calls
`_session/steering` with ACP prompt blocks; idle returns
`outcome: promptRequired` so Comet starts the next prompt instead of
injecting into a finished turn.

## Current scope

Supported now:

- hash-gated `zeron` launch over this checkout's ACP server;
- create / resume a Desmos ACP session for a workspace;
- stream assistant text, thought, complete cards, and syscall chips;
- model and thought_level from Desmos `configOptions`;
- mid-turn steering (`_session/steering`);
- Comet session registry + `session/load` of the ACP uuid;
- Comet's own alacritty terminals (engine PTYs, not `world.shells`).

Not the same object as the TUI / desk:

- TUI Story/Activity/POST split as separate GPUI panes;
- kernel PTY (`world.shells` / `_session/term`) inside Comet's terminal dock;
- Zeron CRDT devices page (no Desmos analog — `persist.peers()` is the honest one);
- attaching Comet and the TUI as two writers on `.desmos/bridge.sock`.

Comet is pinned as a Git submodule of the `umgbhalla/comet` fork. Harness
changes land in that repository, then the root gitlink moves. The launcher
does not patch Comet at runtime.
