# Desk frontend

Desmos ships a first-party desktop/web UI that talks to the live kernel over
ACP. This is not a second agent and not a `--demo` engine: the same
`AcpServer` that `python -m desmos acp` runs on stdio is hosted in-process,
and the browser speaks JSON-RPC 2.0 over a WebSocket (one object per frame,
the NDJSON stdio shape without the newlines).

gpuix is a GPUI/React toolkit, not an agent UI. Comet is the native GPUI
frontend: `python -m desmos comet` hash-gates `vendor/comet`'s `zeron` and
points `DESMOS_ACP_EXECUTABLE` at `python -m desmos acp`. `python -m desmos
gpuix` is the first-party host that actually loads `@gpuix/react`
`<markdown>` / `<diff>`. Desk is the HTML viewport onto the same ACP server:
story is speech and thinking, activity is the wire.

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
- show complete() POSTs, syscalls, edit diffs, folds, protocol errors,
  pending work, decisions, and child results on Activity; subagent cards
  and error/fold/stop notices also land on Story (xml-as-speech is an
  error card and `end_turn`, not a JSON-RPC crash);
- attach images through the composer (ACP image blocks → `run_turns(images=)`,
  the same path the TUI uses);
- named agents from `persist.roster()` on the rail;
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
  list / peek / **bytes** / run / interrupt / close). Activity `$` paints
  that history through vendored xterm.js (Tokyo Night); it is not a second
  login PTY. Comet's dock paints the same object through alacritty when
  `OpenTerminal` is `kind: kernel`. A PTY is process memory and does not
  survive restart.
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

- attaching the in-process ACP world to `<cwd>/.desmos/bridge.sock`
  (that would be two writers on one persist brain);
- Comet CRDT session registry and Zeron devices;
- Comet engine alacritty PTYs (desk xterm paints `world.shells` history,
  which is line-oriented `op=run`, not a byte-for-byte login PTY). Comet
  itself now has a kernel tab on the same `_session/term` bytes.

Native `<markdown>` / `<diff>` is `python -m desmos gpuix`, not desk.
The TUI (`python -m desmos tui`) is the ratatui/grok pager. Comet
(`python -m desmos comet`) is the native GPUI binary. Desk is the HTML
viewport onto the same kernel. Capability map:
[docs/design.md](design.md) (Surfaces).
