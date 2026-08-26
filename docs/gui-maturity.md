# What is built, and what is not

This is the inventory a later turn is supposed to read. It is not a pitch and
it is not a screenshot caption. Function names here are the contract. If a
row disagrees with the code, the code wins and this file is wrong.

Last reconciled against `desmos/front/acp.py`, `desmos/kernel/loop.py`,
`crates/desmos-tui/src/events.rs`, and the surface pages under `docs/`.
Schema is `persist.SCHEMA_VERSION` **17** (`acp_sessions` binds).
`docs/how-desmos-works.md` still says v16 in one table; that line is stale.

---

## 1. One kernel, several paints

Desmos is a coding agent whose **Python process owns the loop**. The model is
a gland. A request is a prompt string into `run_turns(world, prompt)` in
`desmos/kernel/loop.py`. That function POSTs the transcript, scans the reply,
runs syscalls, appends user-role `<result>` blocks, and repeats.

Every UI is paint of that same `World`:

| surface | command | wire | paint |
|---|---|---|---|
| TUI | `python -m desmos tui` | JSONL `desmos/front/bridge.py` on `.desmos/bridge.sock` | `crates/desmos-tui` hosting grok-build `ScrollbackState` |
| ACP | `python -m desmos acp` | NDJSON JSON-RPC 2.0 (not Content-Length) | whoever is on stdio |
| Comet | `python -m desmos comet` | ACP child (`DESMOS_ACP_EXECUTABLE`) | vendored `zeron` in `vendor/comet` |
| Desk | `python -m desmos desk` | in-process `AcpServer` over WebSocket `/acp` | HTML in `desmos/front/desk_static/` |
| GPUIX | `python -m desmos gpuix` | ACP child | `@gpuix/react` `<markdown>` / `<diff>` |
| grok pager | `python -m desmos tui --grok` | ACP stdio | grok-build pager `--minimal --no-leader` |
| headless | `python -m desmos run TASK` | none | print + `summary.json` |
| console | `python -m desmos console` | none | IPython `step()` |

Python is truth. Rust and JS are paint. Two processes must not both write the
same sqlite brain: `persist.claim_workspace` is the single-writer lease. The
bridge takes it. ACP, Desk, and Comet do **not**. Attaching Desk to
`.desmos/bridge.sock` as a second writer is refused on purpose.

Story and Activity are disjoint routes, not one feed with a filter. A
`result` event never reaches Story. Do not flatten everything to `out`.

---

## 2. Kernel — built

This is the product. Frontends sit on top of it; they do not reimplement it.

**Loop.** `step` / `turn` / `run_turns` in `desmos/kernel/loop.py`.
`new_world(persist=False, state_path=None)` for children. `reload_sdk`
reimports without wiping `ns` / notes / messages. Transcript is append-only
within a session. Syscall output arrives as user-role `<result>` blocks the
dispatcher owns. The model must never emit a result block in speech.

**Scan and dispatch.** `desmos/kernel/scan.py` sees `<tag/>`. Dispatch splits
seven `CANONICAL` families (`canonical.normalize` / `run_op`), `REMOVED_TAGS`
(guidance, no run), and grown tools (`world.tools[tag].handler`). Default
wire path is a typed `syscall` tool whose body is the XML string. Speech-XML
is the fallback when `tool_syscalls()` is off.

**Tool-channel vs speech.** All OpenAI models, and Anthropic unless
`DESMOS_TOOL_SYSCALLS=0`, must call via the tool channel. `gpt-5.6-sol` is
always tool-channel even with the flag off. XML written as prose on that path
is **not dispatched**. The loop used to raise
`RuntimeError("the model emitted XML as speech instead of calling syscall")`.
That raise is gone. The live contract:

1. `scan(speech)` finds spoken tags.
2. `fire({"ev": "error", "n": n, "text": f"[{problem}]"})` — extra fields are
   illegal; `crates/desmos-events` `Error { text, n }` has
   `deny_unknown_fields`.
3. `deliver(world, f"[{problem}]")` so the next turn sees the note.
4. `blocks = []` — those tags never run.
5. The step ends (`done=True`). The phrase
   `the model emitted XML as speech instead of calling syscall` stays in the
   note (the anthropic check greps it). Speech still shows the XML. That is
   honest: the model said it, the harness refused it.

**Cache.** Anthropic: ABI system block, catalog system block
(`world.catalog_frozen`), last **user** only. Do not move the breakpoint onto
the assistant. OpenAI: ABI+catalog in `instructions`, volatile as an extra
input item.

**Thinking.** Opus 5 is adaptive (`thinking: {type:adaptive}` +
`output_config.effort`). Default effort is `low`. Older Claude 4 uses a token
budget + interleaved beta. Do not fake thinking blocks.

**Subagents.** Isolated `World`, `persist=False`, depth cap, persona /
capability. Unknown wait ids must not KeyError. Turn-cap must salvage, not
vanish. Child speech is the child's, not the parent's.

**Persist.** Skip load/save when `persist=False`. Trajectory writes are unique
names + `os.replace`, not `len(dir)+1`. Durable state is
`cwd/.desmos/harness.sqlite3` plus JSONL sidecars for memory, plans, decisions.
Events live in the sqlite `events` table.

**Edit.** Compile `.py` before write. Refuse ambiguous `---` bodies. The
kernel locates the unique occurrence and emits `line` on the result.

**Vision.** `vision.attach` + `run_turns(..., images=)`. The TUI and ACP both
hand images into that same path.

**Growth.** Notes, tags, skills, and the SDK itself are writable from inside a
turn and live on the next dispatch, bounded by `evolve` / `rollback`. Speech
is not memory.

**Checks.** `python -m desmos check` is the floor. It runs the thing. It does
not grep the system prompt for wording.

---

## 3. TUI — built (the richest paint)

Default TUI is **not** ACP. `python -m desmos tui` hash-gates
`crates/desmos-tui`, then attaches to `.desmos/bridge.sock` or spawns
`python -m desmos bridge --cwd`. Markdown is
`crates/xai-grok-markdown` via `StreamingMarkdownRenderer::finish` + Syntect
Tokyo Night. The pulldown-cmark walker is gone. Do not put it back.

Live routing (`crates/desmos-tui/src/events.rs`, proved by
`live_events_route_conversation_to_story_and_work_to_activity`):

| Story | Activity |
|---|---|
| user prompt | thinking (`RenderBlock::Thinking`) |
| assistant speech | `complete()` POST groups (`[` / `]` step groups) |
| work-run sentence (syscalls except edit) | syscall `ToolCall` cards |
| subagent cards | edit diffs |
| fold / error as `system` | FOLD notice |

`<edit>` is wire-only. Do not restore story edit cards — that prints
`edit ×3` above three cards naming the files. AGENTS.md still says thinking
is on Story and that `<edit>` gets a story card. Those two sentences are
stale. The tests are the contract.

The frame is a nine-pane cockpit: Story, Activity, Meter, Git, Files, POST
in, POST out, Queue, Input, plus a rail. Tab walks clockwise. Click select,
double-click fold.

The bridge, not ACP, also speaks:

- `ready` / `snapshot` (model, picker, cwd)
- `picker` / `login` / `model_rejected`
- `agents` / `channels` / `roster`
- `workspace_story` / `channel` / `channel_story` / `posted`
- `notice`
- `has_input` parking: while background work is pending, a non-empty inbox
  hands the turn back so a queued prompt is not stuck behind a monitor
- `claim_workspace` at attach

`--grok` is a different paint of the same kernel: grok-build's pager over
ACP. Call groups still live in `App::call_groups`. The pager's own turn
keys off `RenderBlock::UserPrompt`, which the wire pane never has, so `[` /
`]` step groups; arrows stay fold.

---

## 4. ACP — built (the GUI keyhole)

`desmos/front/acp.py` `AcpServer` is the one server. Stdio for Comet / GPUIX
/ `--grok`. In-process for Desk (JSON-RPC over WebSocket, one object per
frame).

**Session.** `session/new` mints a uuid and `persist.acp_bind`s it.
`session/load` restores a persist session id or a previously bound ACP uuid.
Unknown ids are refused, not minted. `loadSession` is advertised true.

**One World per cwd.** Persist keys rows off the directory. Two `World`
objects on one cwd overwrite each other's ns, notes, and tools. Sessions on
the same cwd share the world. Transcript is swapped per prompt
(`self._convo`). Concurrent `session/prompt` on that world is refused.

**Prompt.** `session/prompt` runs `run_turns`. Images: initialize advertises
`promptCapabilities.image: True`. `prompt_text` stays text-only (no base64
dump). `prompt_images` writes temp files / accepts `file:` URIs. Image-only
prompts are allowed. `session/cancel` sets `should_stop`. Finished step
returns `stopReason: end_turn`. Unexpected `Exception` is still JSON-RPC
`-32603`. A handled kernel `error` event is **not**.

**Config.** `session/set_config_option` for `model` and `thought_level` —
the same catalog the TUI picker reads.

**Steer.** `_session/steer` and `_session/steering` call `catalog.steer`. If
`world.running`, outcome is `injected`. If idle, `promptRequired`.

**Environment RPCs** (Desk and Comet consume these; they are not ACP spec):

`_session/git`, `_session/fs`, `_session/sessions`, `_session/peers`,
`_session/roster`, `_session/channels`, `_session/channel_read`,
`_session/post`, `_session/bridge`, `_session/term`.

`_session/term` talks to `desmos.kernel.shell` / `world.shells`. A PTY is
process memory. It does not survive restart.

**Pane tags.** Every `session/update` carries `_meta.desmos.pane`
(`story` | `activity`) and `family`. Clients that ignore the tag flatten
the wire into the thread. Clients that honor it can split Story / Activity
the way the TUI does.

**Subagents.** `S.set_emitter(on_event)` is installed for the duration of
the prompt and restored in `finally`. Without that, ACP children were
silent (the bridge held the process-lifetime emitter). Child thinking /
speech update the subagent card. They do not become parent
`agent_message_chunk`. Child `result` is an Activity syscall with
`extra.child`. Empty title is omitted on updates so child speech does not
overwrite the spawn title with the run id.

---

## 5. Comet / Desk / GPUIX — built as paints, not as second engines

### Comet (`python -m desmos comet`)

Vendored `umgbhalla/comet` git submodule. Hash-gated `zeron`. Create a chat,
choose **Desmos**. Story = user prompts + thinking (`MessagePart::Thought`)
+ assistant GFM. Activity = a right pane (auto-opened on the first Desmos
chat). `complete()` is `ToolCall::Unknown { name: complete }`, not a
WebFetch. Edits also land on Comet's Changes pane.

Steering: initialize advertises `_meta.steering.supported`. Mid-turn injects.
Idle returns `promptRequired`. Kernel PTY on the alacritty dock
(`OpenTerminal` `kind: kernel` → `_session/term`). Extra `+` tabs are still
login PTYs.

Harness changes that need Comet chips land in the comet repository, then the
root gitlink moves. This checkout does not patch Comet at runtime.

`vendor/comet` is often empty until
`git submodule update --init vendor/comet`.

### Desk (`python -m desmos desk`)

HTML viewport. No cargo. Story / Activity, composer with paperclip +
drag-drop images, model/effort chips, git / files / channels / agents rail
(`persist.roster()` is painted), xterm.js on `_session/term` (Tokyo Night),
cancel, new session, resume. Keys: `?`, `N`, `Ctrl/⌘ K`, `Ctrl/⌘ ``, `1–7`.

Markdown is `desk_static/md.js`: a **second grammar** (regex GFM subset +
Tokyo Night token colors). A second HTML renderer is allowed. A second
markdown grammar is the remaining markdown job — WASM or token spans from
`xai-grok-markdown`, not another homemade walker.

Paint is full `innerHTML` rebuilds. There is no incremental DOM reducer.

### GPUIX (`python -m desmos gpuix`)

First-party host so Desmos loads `@gpuix/react` instead of approximating it.
Story `<markdown>` in a `virtual-list`. Edits `<diff wordDiff>`. Model /
effort `Select`. Native is loaded only when `require('@gpuix/native')`
succeeds — file-only probes used to launch xvfb `--tree` and fail the floor.
Does not host Comet session registry, alacritty, steering UI, or desk's git /
files / channel / xterm tabs.

---

## 6. Event matrix

Kernel `ev` → TUI (bridge) → ACP `_emit_event` → Desk / GPUIX paint.

| Kernel `ev` | TUI | ACP | Desk / GPUIX |
|---|---|---|---|
| `thinking` | Activity `Thinking` | `agent_thought_chunk` pane=story | muted thinking on Story |
| `speech` | Story `AgentMessage` | `agent_message_chunk` story | assistant markdown on Story |
| `prompt` | Story `UserPrompt` | skipped (client already has it) | local story row on send |
| `post` / `complete` | Activity POST group; `spans` reconcile speech | `tool_call` complete + `_meta.desmos.spans` / `group` | Activity complete card; spans strip spoken XML from last assistant item |
| `result` | Activity `ToolCall` only | execute / edit `tool_call`; deltas append, done omits full text if streamed | Activity; edit = diff |
| `error` | Story `system` + Activity | Activity `tool_call` title `error`. Does **not** latch `state["error"]` | Activity card + Story `system` |
| `compacted` | Story fold notice + Activity | Activity `compacted` | both |
| `steer` | Story echo | `user_message_chunk` `"[steer] {text}"` family=prompt | Story; do not duplicate local “steer queued” |
| `decision` | real option picker | Activity card + `options` / `decisionId` | clickable options → `decide:<id>: <option>` |
| `pending` | badge | reused tool id, `replace: true` | Activity card + status `pending N` |
| `resumed` / `guidance` / `attached` / `stopped` | TUI notices / cards | Activity cards | Activity (+ Story system for stopped) |
| `subagent` | Story card | Story `tool_call` | Story subagent |
| `child` thinking/speech | update that card | `tool_call_update` on that card | same |
| `child` result | Activity | Activity syscall + `extra.child` | Activity |
| `turn` / `done` | stream finish / stop | skipped (`stopReason` is done) | n/a |
| `prompt` | Story `UserPrompt` | skipped live; `user_message_chunk` on replay | local row on send; replay paints user |
| `picker` / `login` / `ready` / `snapshot` | TUI chrome | **not emitted** | Desk polls `_session/*` / `configOptions` |
| `agents` / `channels` / `roster` | TUI rails | **not emitted** | Desk `_session/roster`, `_session/channels` |
| `workspace_story` / `channel_story` / `posted` | TUI | **not emitted** | Desk polls + `_session/post` |
| `notice` | TUI toast | Activity `notice` | Activity card |
| `model_rejected` | TUI | Activity `model_rejected` | Activity card |

ACP `STORY_UPDATES` = `agent_thought_chunk`, `agent_message_chunk`,
`user_message_chunk`. `STORY_FAMILIES` = `{subagent}`. Everything else with
a card is Activity unless `_push_card(..., pane="story")`.

---

## 7. What recently landed (this branch)

These are done. Do not rebuild them. Do not list them as remaining work.

1. **XML-as-speech is an error event**, not a `RuntimeError`. Comet no longer
   paints `harness protocol error: session/prompt: [turn n failed: …]` for
   that case. `session/prompt` returns `end_turn`. Tags named in the error
   card are not dispatched. Checks:
   `desmos/checks/kernel.py` (gpt-5.6-sol + the screenshot XML),
   `desmos/checks/front.py` `_check_acp_xml_as_speech`,
   `desmos/checks/anthropic_check.py` (phrase still present, no
   `RuntimeError` in messages).
2. **ACP maps the rest of the kernel event vocab** that the keyhole had
   dropped: error, compacted, steer, complete spans+group, streamed result
   done-omits-text, decision, pending reuse-id, subagent on Story, child not
   parent speech, attached, stopped, guidance, resumed.
3. **Images** advertised and attached through `run_turns(images=)`.
4. **`S.set_emitter` for the ACP prompt** so children are not silent.
5. **Desk / GPUIX paint** those families, spans, steer, subagent, images,
   agents rail. `bindCopies` targets `$("#app")`. gpuix native probe is
   `require()`, not “index.js exists”.
6. **Surface docs** `docs/desk-frontend.md`, `docs/comet-frontend.md`.
7. **ACP `claim_workspace`.** `session/new` takes the same lease as the
   bridge. A second interactive front is `-32602` naming the holder.
8. **Event-log resume.** ACP `record_event`s kernel events. `session/load`
   replays `persist.read_events` through `_emit_event`. User story is
   `ev prompt`, not `header(world)+prompt`.
9. **Idle steer.** Running → `injected` + `catalog.steer`. Idle →
   `promptRequired` only.
10. **`has_input` park.** A follow-up `session/prompt` wakes
    `pending.wait_next` the way the TUI inbox does.
11. **`world.on_event`.** Children copy the parent's hook. `_emit` prefers
    `CALLER_WORLD.on_event` before the process global.
12. **notice / model_rejected** Activity cards. Desk/GPUIX decision buttons
    send `decide:<id>: <option>`.
13. **AGENTS.md** thinking/edit-card sentences match the TUI tests.

---

## 8. Yet to build

Each row names the **real object**. A lookalike is not progress. If wiring
that object is blocked, say the block. Do not invent an 80-line stand-in.

### Thinking pane — two paints, one event

TUI tests put thinking on **Activity**. ACP tags thinking as **Story**
(`agent_thought_chunk`), matching Comet `MessagePart::Thought` and Desk's
muted Story block. That split is written down in AGENTS.md. Do not paint
thinking on both panes to make the docs agree.

### `PARENT` is still a module global

`world.on_event` is per-world. `S.PARENT` is still process-global. A child
that emits without `CALLER_WORLD` and without `on_event` copied still
falls through to whichever parent was bound last.

### Desk markdown grammar

`md.js` reimplements GFM. The real remaining job is the grok markdown
contract in the browser (WASM of `xai-grok-markdown-core`, or token spans
the Python side already has). Do not add a third regex. Do not restore a
pulldown walker.

### Comet chip richness

Lives in `umgbhalla/comet`, then the gitlink. Protocol errors already land
as Activity cards. Collapsing “Ran N commands”, paperclip, model chip
copy, and similar chrome are Comet product — commit there, pin here.

### Kernel leftovers that are not GUI, but are real

- `docs/tui-redesign.md` (ARES) — rail / subject-on-events / front-attaches
  not owns. Proposed. Not this checkout's TUI.
- `state/work.py` CAS graph: production never calls `work.add` / `claim` /
  `finish`. `witness.wake` still injects a catalog line from git commits
  even when `work_*` is empty.
- `front/trace.py` still globs `.desmos/events/*.jsonl`; events live in
  sqlite.
- `.desmos/out/NNNN-tag.txt` numbering is still `max+1`.
- Spine / herdr remain opt-in. Seats, channels, roster are on the default
  TUI and are not spine extras.

---

## 9. What must never be built

These shipped as toys. They were rejected. Recreating them is not remaining
work.

| Toy | Real thing |
|---|---|
| Homemade inline viewport / copied `xai-ratatui-inline` as “the TUI” | Three-pane `desmos-tui` hosting grok `ScrollbackState` |
| Same text on Story and Activity | Disjoint routes. `result` never on Story |
| `md_lines` pulldown-cmark walker | `crates/xai-grok-markdown` |
| Fake persist / child writing parent harness state | `persist=False`, `state_path=None` |
| Fake `<result>` in assistant speech | Dispatcher-owned user `<result>` only |
| Self-closing tags the scanner missed | `scan.py` must see `<tag/>` |
| Trajectory race via `len(dir)+1` | Unique names, atomic replace |
| `# runtime` as a path dump | Runtime block that teaches the live system |
| Desk attached to `bridge.sock` | Two writers. Forbidden |
| Cloned Comet devices / alacritty as fake `world.shells` | `_session/term` on the real kernel PTY; `persist.peers()` for presence |
| grok.com auth, leader, mermaid marketplace, voice | Not our product surface |
| `--demo` as a second engine | Same code path, canned events at most |
| Asserting a sentence exists in the system prompt | Run the behaviour |

---

## 10. How to verify

```text
PYTHONPATH=. python3 -m desmos check --only kernel
PYTHONPATH=. python3 -m desmos check --only front
PYTHONPATH=. python3 -m desmos check --only transport
PYTHONPATH=. python3 -m desmos check --fast
```

XML-as-speech on ACP: `_check_acp_xml_as_speech` — no JSON-RPC `error`,
`stopReason == end_turn`, error card names the tags, those tags are not
tool titles, body contains `not dispatched`.

Protocol cards: `_check_acp_protocol_cards`. Images: `_check_acp_images`.
Emitter: `_check_acp_subagent_emitter`. Claim: `_check_acp_workspace_claim`.
Resume: `_check_acp_event_replay`. Park: `_check_acp_has_input`.

A Comet GUI recording is not a substitute for those checks, and it is not
possible until `vendor/comet` is initialized and `zeron` is built.

---

## 11. How to use this file

If you are about to paint a card, read §6 and §8 first. If the kernel
already fires the event, wire `_emit_event` or honor `_meta.desmos.pane`.
If the kernel does not fire it, that is a kernel change, not a CSS change.

If you catch yourself writing “placeholder,” “good enough for now,” or
“we can swap later,” delete the code. Do not commit it.
