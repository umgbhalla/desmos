# GPUIX frontend

Desmos's first-party GPUIX host is `python -m desmos gpuix`. It is not a
second agent and not the gpuix chat example: the process starts
`python -m desmos acp` (NDJSON JSON-RPC 2.0) and paints the streamed
events with the published `@gpuix/react` custom elements.

Story speech and thinking go through native `<markdown source={...} />`
(GFM: headings, lists, tables, `~~` strikethrough, task lists, autolinks).
Edit patches go through native `<diff patch={unified} wordDiff />`. Those
are gpuix's elements, not a copy of Comet's parser and not desk's HTML
`md.js`. Comet stays the zeron GPUI binary; this host exists so Desmos
loads `@gpuix/react` instead of approximating it.

## Setup and launch

```text
python -m desmos gpuix --cwd .
```

The launcher `npm install`s `desmos/front/gpuix` when
`node_modules/@gpuix/native` is missing, then `execve`s `node src/main.js`.
`--no-install` skips npm. Node 18+ is required. The ACP child is
`{python} -m desmos acp` with `PYTHONPATH` pointing at this checkout.

Headed Linux without a GPU: the same lavapipe ICD Comet uses
(`VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/lvp_icd.json`). This VM's
`DISPLAY=:1` rejects `X_CreateWindow`; use xvfb.

```text
python -m desmos gpuix --probe      # retained tree of <markdown> / <diff>
python -m desmos gpuix --acp-probe  # initialize + session/new, then exit
```

## What it actually paints

ACP tags every `session/update` with `_meta.desmos.pane` (`story` |
`activity`). The host maps that wire the same way desk does:

- user prompts are local story rows (session/prompt does not echo them)
- thinking → `<markdown>` with a muted theme
- speech → `<markdown source>`
- `complete()` and syscalls → activity text
- edit `oldText`/`newText` → `unifiedPatch` → `<diff wordDiff>`

The composer is gpuix `<textarea>`; Enter is `session/prompt`.

## Current scope

Supported now:

- live ACP initialize / authenticate / session/new / session/prompt;
- story markdown and thinking through `@gpuix/react` `<markdown>`;
- activity edit cards through `<diff wordDiff>`;
- probe that asserts the GPUI retained tree (custom props on those
  elements) and an ACP session id from a real child.

Not this host:

- Comet session registry, alacritty kernel PTY, steering UI;
- desk git/files/channel/xterm tabs;
- Zeron CRDT devices (`persist.peers()` is still the honest analog);
- attaching as a second writer on `.desmos/bridge.sock`.

`vendor/comet` and `vendor/grok-build` are untouched. A change to gpuix
itself is an npm version bump of `@gpuix/react` / `@gpuix/native`, not a
half-copy under `crates/`.
