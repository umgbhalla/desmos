# Desk frontend

Desmos ships a first-party desktop/web UI that talks to the live kernel over
ACP. This is not a second agent and not a `--demo` engine: the same
`AcpServer` that `python -m desmos acp` runs on stdio is hosted in-process,
and the browser speaks JSON-RPC 2.0 over a WebSocket (one object per frame,
the NDJSON stdio shape without the newlines).

gpuix is a GPUI/React toolkit, not an agent UI. Comet is already launched by
`python -m desmos comet` as a thin exec of `vendor/comet`. Desk copies their
craft (Waku graphite density, session rail, composer chips, Tokyo Night
markdown, diffs, a PTY panel) while keeping Desmos invariants: story is speech
and thinking, activity is the wire.

## Setup and launch

```text
python -m desmos desk --cwd .
```

The server binds `127.0.0.1:7734` by default and opens a browser. Use
`--no-browser` in a headless environment, `--port 0` to take an ephemeral
port, `--host` only if you know you want a non-loopback bind.

```text
python -m desmos desk --cwd . --port 7734 --no-browser
```

No cargo build and no hash gate: the UI is package data under
`desmos/front/desk_static/`, served by `desmos/front/desk.py`.

`vendor/grok-build` and `vendor/comet` are git submodules. A fresh checkout
must `git submodule update --init vendor/grok-build vendor/comet` before a
source TUI/Comet build. Desk itself is HTML and does not load those crates
into the browser.

## Current scope

Supported now:

- create an ACP session for the selected workspace;
- send a prompt; Enter submits, Shift+Enter is a newline;
- stream thinking into the story and assistant markdown into the story;
- show complete() POSTs, syscalls, and edit diffs on Activity only;
- cancel the in-flight prompt (Esc, or the stop control);
- new session (`session/new`);
- resume a persist session (`session/load` with the sqlite `sessions.id`
  from `_session/sessions` — the same rows the TUI picker reads);
- resume a previous ACP uuid (`session/load` of the uuid `session/new`
  issued; `persist.acp_sessions` is the join, schema 17, same sqlite file);
- model and thought_level through `session/set_config_option` (the same
  catalog the TUI picker reads), as chips inside the composer;
- steer while a turn is running (`_session/steer` → `catalog.steer`);
- git status / branches / log (`_session/git`, same `--no-optional-locks`
  reader as the TUI);
- files listing and bounded read (`_session/fs`, jailed to the session cwd);
- live peers and named roster (`_session/peers`, `_session/roster`);
- channel list, read, and post (`persist.channel_*`, same as the bridge);
- kernel PTY (`_session/term` → `desmos.kernel.shell` / `world.shells`,
  the same object `<shell>` uses);
- bridge socket presence (`_session/bridge`). Desk does not attach as a
  second writer on a live TUI daemon.

`loadSession` is advertised true for persist session ids and for ACP uuids
bound at `session/new`. An unknown id is refused rather than minted.

Keys: `?` overlay, `N` new session, `Ctrl/⌘ K` filter, `Ctrl/⌘ `` terminal,
`1–7` activity tabs, Enter send, Esc cancel.

## What desk cannot host

Desk is HTML. It cannot instantiate grok-build's
`StreamingMarkdownRenderer` (ratatui + Syntect) or Comet/gpuix GPUI
`<markdown>` / `<diff>` / alacritty_terminal views in the browser. The
`crates/xai-grok-markdown` crate emits terminal spans, not HTML. Desk
raises the HTML renderer to that contract instead: Tokyo Night tokens
(`#1a1b26` / `#51597d` / `#a9b1d6`), streaming re-render, indented fences,
autolinks, fence copy, LCS diffs.

Comet **devices** (`vendor/comet/crates/ui/src/settings/devices.rs`) are a
Zeron CRDT/RPC workspace device registry (`zeron_rpc`, presence dots,
rename). Desmos has no that object. The honest analog is
`persist.peers()`, already on the rail. There is no devices page.

A PTY is process memory. `World.shells` cannot be reloaded from JSON after
a restart; the term panel talks to the live kernel only.

Not yet matched with the native TUI / Comet / gpuix:

- native GPUI `<markdown>` / `<diff>` / Metal-Vulkan host;
- attaching the in-process ACP world to `<cwd>/.desmos/bridge.sock`
  (that would be two writers on one persist brain);
- Comet CRDT session registry and devices;
- xterm.js / alacritty glyph rendering (desk shows the kernel peek text).

The TUI (`python -m desmos tui`) and Comet (`python -m desmos comet`) stay
as they are. Desk is another viewport onto the same kernel.
