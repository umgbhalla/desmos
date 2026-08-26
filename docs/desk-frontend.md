# Desk frontend

Desmos ships a first-party desktop/web UI that talks to the live kernel over
ACP. This is not a second agent and not a `--demo` engine: the same
`AcpServer` that `python -m desmos acp` runs on stdio is hosted in-process,
and the browser speaks JSON-RPC 2.0 over a WebSocket (one object per frame,
the NDJSON stdio shape without the newlines).

gpuix is a GPUI/React toolkit, not an agent UI. Comet is already launched by
`python -m desmos comet` as a thin exec of `vendor/comet`. Desk copies their
craft (Waku graphite density, session rail, composer, model/thinking pickers,
markdown, diffs) while keeping Desmos invariants: story is speech and
thinking, activity is the wire.

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

## Current scope

Supported now:

- create an ACP session for the selected workspace;
- send a prompt; Enter submits, Shift+Enter is a newline;
- stream thinking into the story and assistant markdown into the story;
- show complete() POST cards, syscalls, and edit diffs on Activity only;
- cancel the in-flight prompt (Esc, or the stop control);
- new session (`session/new`);
- model and thought_level through `session/set_config_option` (the same
  catalog the TUI picker reads);
- steer while a turn is running (`_session/steer` → `catalog.steer`).

Not yet matched with the native TUI:

- git/files environment viewers;
- channel/peer rail;
- loading a prior transcript by session id (`loadSession` is still false);
- attaching to a daemon bridge socket.

The TUI (`python -m desmos tui`) and Comet (`python -m desmos comet`) stay
as they are. Desk is another viewport onto the same kernel.
