# Comet frontend

Desmos can run behind the vendored Comet desktop frontend through ACP. This is
an intentionally small integration: Comet starts `desmos acp`, sends the
selected workspace in `session/new`, and renders the streamed agent events.

## Setup and launch

Install Desmos in the active environment and initialize the Comet submodule:

```text
git submodule update --init vendor/comet
```

Then launch the frontend with:

```text
python -m desmos comet --cwd .
```

The first launch builds Comet's debug binary. Later launches can use
`--no-build`. The launcher sets `DESMOS_ACP_EXECUTABLE` to the active Desmos
console script, so Comet's **Desmos** harness starts the same checkout and
environment.

Inside Comet, create a chat and choose **Desmos** as its harness. The chat's
workspace path becomes the Desmos session working directory.

For direct Comet development, build `zeron` in `vendor/comet` and set
`DESMOS_ACP_EXECUTABLE` to an absolute `desmos` console-script path before
opening the binary.

## Current scope

Supported now:

- create a Desmos ACP session for a selected workspace;
- send prompts;
- stream assistant text, thought summaries, and tool activity;
- use the provider/model configured by Desmos.

Not yet matched with the native Desmos TUI:

- loading an existing Desmos session into a new Comet chat;
- Desmos account, provider, model, and effort controls in Comet;
- exact Desmos Story/Activity/POST/Meta pane semantics;
- every rich diff, terminal, queue, and subagent interaction.

Comet is pinned as a Git submodule from the `umgbhalla/comet` fork. Its Desmos
harness integration lives in that repository; the root launcher does not patch
Comet at runtime.
