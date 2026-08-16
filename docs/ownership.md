# Ownership: the Python/Rust boundary

Python is truth, Rust is paint (docs/upgrade-paths.md, Doctrine). This file is
the contract: which side owns which fact today, and the exact event vocabulary
the bridge speaks. It is the source for the Phase 5.3 typed Rust `Event` enum.
Every claim below is anchored to a producer line and a consumer line as of this
branch; a field listed in neither place does not exist.

All Rust paths are `crates/desmos-tui/src/`, all Python paths are `desmos/`.

## Part 1 — facts the TUI computes itself

Facts Rust re-derives from event text, the filesystem, or a forked process,
that the kernel already knows. "Phase 3 field" is the event field the fact
will arrive as; the first four are the moves ordered by upgrade-paths.md.

| Fact | Rust computes it today | Kernel already knows it | Phase 3 field |
|---|---|---|---|
| Syscall span classification (which stretch of streamed speech is a call, not prose) | **MOVED (Phase 3).** The grammar port (`stream.rs spoken_prefix` / `strip_syscalls` / `next_tag` / `find_close` / `in_string`) is now mid-stream hold and stop/error fallback only; a completed turn is reconciled from the kernel's verdict (`stream.rs finish_speech_spans`, consumed via `events.rs kernel_spans`) | `kernel/scan.py scan_spans` is the one scan; `kernel/loop.py` converts its char offsets to UTF-8 bytes and emits them where speech is final | landed as `spans` on `complete` (byte ranges of the final speech that dispatched) + `span_idx` on `result` start/done (index into that turn's dispatch order = into `spans` when non-empty) |
| `<edit>` card start line | **MOVED (Phase 3).** `edit_start_line` (the `fs::read_to_string` + whole-file search) is deleted; `main.rs result_block` reads `line` off the event and `wire_syscall` anchors the diff there. No field → no hunks, no invented anchor (failed edit, start phase) | `kernel/edit.py apply_edit_line` locates the unique occurrence at write time and returns its 1-based line; `kernel/dispatch.py` lifts it into the dispatch `meta` out-channel; `kernel/loop.py` spreads `meta` onto the result done event | landed as `line` on the edit `result` done event (success only — a refused edit has no edit site) |
| Work-row commit attribution ("· committed abc123") | **MOVED (Phase 3).** The HEAD-snapshot dance (`work.rs head_at_start` + the `git_tail` before/after compare, racing the git pane worker) is deleted; `events.rs` reads `repo.committed` off the result done event into `work.rs WorkRun::commit`, and the row claims a commit only when the kernel reported one. The dirty-count tail still reads the pane snapshot (generation-gated in `settle`); the git *pane* stays a Rust-side environment viewer | `kernel/loop.py committed_sha` judges from what the kernel alone holds — the command text and its output: a successful `git commit` names the short sha in its summary line, a failed one prints no such line, so a failed commit cannot claim | landed as `repo: {committed}` on the bash/shell `result` done event (present only when the output proved a commit) |
| Cost and cache savings | `main.rs:1152 model_price` (hardcoded $/Mtok table), `main.rs:1265 bill`, `main.rs:1281` cost formula, 0.1x/1.25x/2x cache multipliers | kernel holds the usage (`kernel/loop.py:213-223`, emitted on `complete` at `kernel/loop.py:280`) but has no price table; Rust is the single implementation | stays in Rust while it remains the single implementation (upgrade-paths Phase 0) |
| Syscall failure classification (red card or not) | `main.rs:7303 looks_failed` greps result text for `Traceback`/`SyntaxError`/`exit `; `main.rs:7216 edit_failed` greps for `error`/`no match`/…; applied at `main.rs:6312` and `main.rs:7145,7174` | the kernel produced the failure: `kernel/shell.py:232` appends `[exit N]`, `kernel/loop.py:340` captures the traceback as the result, `kernel/edit.py:11-42` returns `edit failed: …` strings | `ok`/`exit` on the `result` done event. (Latent bug the move erases: shell writes `[exit N]`, `looks_failed` tests `starts_with("exit ")` — brackets never match.) |
| Edit body split into old/new | `main.rs:7232 split_edit_body` re-parses the `---` body, silently treating a no-separator body as pure insertion | `kernel/edit.py:66 parse_edit_body` is the authority and *rejects* ambiguous bodies the Rust splitter accepts | `old`/`new` (or hunks) on the edit `result` event |
| Call target (the program/path a call was aimed at, for the work-row sentence) | `main.rs:588 call_target` re-parses the bash body: splits on `&&`/`;`, skips `cd`/`export`/assignments, takes the program word | shell semantics live in `kernel/shell.py`; tag + attrs already ride the `result` event (`kernel/loop.py:305-311`) — the body parse duplicates what the kernel executed | `target` on the `result` event |
| Which transcript chunk is syscall output vs prose vs thinking (context-bar roles) | `main.rs:1173 observe_roles` scans the request JSON for `<result` substrings and block types, twice (Anthropic and OpenAI shapes) | `kernel/loop.py:64 result_content` *builds* those messages; `kernel/loop.py:553-556` appends them — the kernel knows each message's kind at creation | role/segment annotation on the `post`/`complete` request, or kernel-computed totals |
| Context window ceiling (denominator of the context bar) | `main.rs:1143 model_window` hardcodes 400k/200k by model prefix | nobody: `transport/settings.py:25 CATALOG` knows models and efforts but not windows. Today this fact has no owner; the kernel is its natural home | `window` on `ready`/`snapshot`/`complete` |
| Cache TTL bucket (5m vs 1h countdown) | `main.rs:1311-1325` infers from `usage.cache_creation.ephemeral_1h_input_tokens` | `transport/complete.py:269 cached_payload` sets the breakpoints; the kernel chose the TTL it requested | `ttl` on `complete` (the usage inference is at least wire-derived, so lowest priority) |
| Model in effect mid-step | `main.rs:2674` re-reads `request.model` out of the `post` body to catch a kernel-side switch | `world.model`, already emitted at `kernel/loop.py:157` (`post.model`) and `front/bridge.py:35` (`snapshot.model`) | already a field; `post.model` (`kernel/loop.py:157`) is authoritative — the body re-read should die with Phase 3, not be enshrined |
| Subagent story title | `main.rs:2326 task_title` re-derives a headline from the task text (paren stripping, sentence cut, 52-char elision) | `agents/subagent.py:623` holds `run.task`; the contract objective is structured | `title` on the `subagent` started event |
| Prior session turns (resume picker) | `session.rs:150 load_turns` forks `sqlite3` (`session.rs:154`) against `.desmos/harness.sqlite3` and reads the kernel's own table | `state/persist.py:27 DB_FILENAME`, schema at `state/persist.py:209`, loaded by the kernel itself at `state/persist.py:444` | over the bridge (`ready` payload or a new op), not a second reader of the kernel's database |

### Rust-owned surfaces (not facts to move)

| Surface | Where | Why it stays |
|---|---|---|
| Git pane | `side.rs:269` (forks `git` on a worker thread) | declared environment viewer, exempt from paint-from-events (upgrade-paths, instrument 4) |
| Files pane | `side.rs:390 read_dir`, `side.rs:503 fs::read` | environment viewer |
| Pane layout persistence | `main.rs:362-416` (`.desmos/tui.json`) | pixels; Rust decides where pixels go |
| grok pager appearance | `main.rs:1828` reads `util::pager_toml_path()` | grok-build's own config |
| Bridge process spawn | `main.rs:426-455` | the transport itself |

## Part 2 — event vocabulary

Wire = one NDJSON object per line, `_emit` (`front/bridge.py`). Every event from
the loop and subagents funnels through the bridge's `on_event=_emit`
or `S.set_emitter(_emit)`, which under one lock writes stdout, fans out to
every attached socket client, and appends the seq/ts-stamped copy to the
event log (see "Durable bridge surfaces"). The wire itself never carries
`seq`/`ts`. Consumer is `handle_event` (`main.rs:2581`) unless said
otherwise; `front/acp.py:305 _emit_event` is the second consumer and reads
only `thinking`, `speech`, `turn`, `error`, `result`. The typed mirror is
`crates/desmos-events` (`Event` for the wire, `LogLine` for the stamped
file/replay form), enforced by `desmos/checks/conformance.py`.

Field types: `str`, `int`, `f64`, `bool`, `obj`, `[str]`, `?` = sometimes
absent.

### `ready` — front/bridge.py:114-118
Snapshot fields plus picker fields, once at startup.

| field | type | produced | consumed |
|---|---|---|---|
| `model` | str | front/bridge.py:35 | main.rs:2603; picker.rs (via observe, main.rs:2596) |
| `provider` | str | front/bridge.py:36 | main.rs:2600 (`ephemeral = anthropic`) |
| `billing` | `"plan"\|"usage"` | front/bridge.py:37 (`_billing`, front/bridge.py:16) | main.rs:2597 (`cache.plan`) |
| `thinking` | str | front/bridge.py:38 | main.rs:2611 |
| `generation` | int | front/bridge.py:39 | main.rs:2614 (accepts int or str) |
| `cwd` | str | front/bridge.py:40 | **nobody** (dead) |
| `ns` | [str] | front/bridge.py:41 | **nobody** (dead) |
| `tools` | [str] | front/bridge.py:42 | **nobody** (dead) |
| `onboarding` | bool | transport/settings.py:168 (via front/bridge.py:117) | picker.rs:119 |
| `current` | obj\|null | transport/settings.py:169 | picker.rs:111-113 |
| `providers` | [obj] | transport/settings.py:151-166 | picker.rs:88-102 |

`providers[]` entry: `provider` str (picker.rs:95), `ok` bool (picker.rs:96),
`detail` str (picker.rs:97), `account` str (picker.rs:98), `plan` str
(picker.rs:99), `can_login` bool (picker.rs:100), `models` [str]
(picker.rs:101), `efforts` [str] (picker.rs:102), `source` str
(transport/settings.py:161) — **`source` consumed by nobody** (dead).

### `snapshot` — front/bridge.py:30-43; emitted at 143, 144(op), 148, 152, 179, 212
Same fields as `ready` minus the picker block. Consumer: same
`"ready" | "snapshot"` arm, main.rs:2595-2623.

### `picker` — front/bridge.py:188, 206
`onboarding`/`current`/`providers` as above. Consumer: main.rs:2588 →
`picker.observe`.

### `login` — front/bridge.py:201, 203, 205
| field | type | produced | consumed |
|---|---|---|---|
| `text` | str | front/bridge.py:201/203/205 | main.rs:2590 |
| `done` | bool? | front/bridge.py:203 only | main.rs:2591 |
| `failed` | bool? | front/bridge.py:205 only | main.rs:2592 (OR-ed with `done`) |

### `notice` — front/bridge.py:167-172 (effort clamp), front/bridge.py:184 (provider switch)
`text` str → main.rs:2750-2755 (story system row).

### `channel` — front/bridge.py `_watch_channel`
| field | type | produced | consumed |
|---|---|---|---|
| `channel` | str | durable SQLite inbox channel | TUI transient notice |
| `author` | str | newest unread peer message | TUI transient notice |
| `preview` | str | whitespace-folded body, capped at 120 chars | TUI transient notice |
| `unread` | int | unread peer-message count | TUI transient notice |
| `message_id` | int | newest emitted durable message id | typed cursor/dedup contract |

The popup never becomes a Story row and never marks a message read. The agent
sees the same unread state through the volatile prompt notice and chooses
`session` inbox, read, post, or dismiss.

### `error` — front/bridge.py:95, 98, 128, 177, 214, 216; kernel/loop.py:361, 530, 591
| field | type | produced | consumed |
|---|---|---|---|
| `text` | str | all sites | main.rs:2764; front/acp.py:327 |
| `n` | int? | kernel/loop.py:361, 530 only | **nobody** (dead) |

Also synthesized Rust-side for an unparseable NDJSON line (main.rs:448).
Never a terminator (main.rs:2756-2761 comment; loop keeps going after a
cut-short reply).

### `speech` — kernel/loop.py:186 (stream), kernel/loop.py:248 (unstreamed), front/bridge.py:147, 150, 151 (reset/reload confirmations)
| field | type | produced | consumed |
|---|---|---|---|
| `text` | str | all sites | main.rs:2641 → `apply_speech` (main.rs:6370); front/acp.py:314 |
| `delta` | bool? | kernel/loop.py:186 only (`true`) | main.rs:2640 |

### `thinking` — kernel/loop.py:168-175 (delta), 176-184 (whole block), 241-247 (unstreamed replay)
| field | type | produced | consumed |
|---|---|---|---|
| `redacted` | bool | all three | main.rs:2627 → `apply_thinking` (main.rs:6338) |
| `text` | str | all three | main.rs:2629; front/acp.py:307 |
| `delta` | bool? | 168-184 only | main.rs:2628 |

The `kind` deltas feeding this (`thinking_delta`/`thinking`/`text_delta`) are
produced by transport/complete.py:396-413 and transport/openai.py:392-418 and consumed by
`kernel/loop.py:163-186 on_delta`; they are transport-internal, not wire events.

### `prompt` — kernel/loop.py `_run_turns` (contract C1)
Emitted once per step, immediately before the user message is appended to the
transcript — the user's message text at injection time, never re-derived from
POST bodies (those carry the header and cache dressing). Every path emits it,
so a child's copy rides the `child` envelope as `kind:"prompt"`.

| field | type | produced | consumed |
|---|---|---|---|
| `text` | str | the `prompt` argument, verbatim | **no UI** (main.rs falls to `_ => {}`); typed in desmos-events; the event-log file is the intended reader (replay, and the Phase 6.5 memex precondition) |
| `n` | int | `world.prompt_ordinal`, counted per `run_turns` call on that world, starts at 1 | same |

### `intervention` — front/bridge.py `_intervene` (contract C3)
One per `kill_run`/`rerun` op arriving on any transport (stdio reader or a
socket client), answered inline on the reader thread — never queued behind
the step it interrupts. Its prose twin rides the existing `notice` kind.

| field | type | produced | consumed |
|---|---|---|---|
| `action` | `"kill_run"\|"rerun"` | the op name | typed in desmos-events; **no TUI reader** (the tree row's confirmation is the terminal `subagent` phase `stopped`, not this event) |
| `id` | str | the op's `id`, verbatim | same |
| `result` | str | `S.kill_subtree(id)` / `S.rerun(id)` — an unknown id is a refusal string here, never an `error` event | same, plus the human via the `notice` twin |

### `post` — kernel/loop.py:152-160
| field | type | produced | consumed |
|---|---|---|---|
| `n` | int | kernel/loop.py:155 | main.rs:2667 |
| `origin` | `"user"\|"llm"` | kernel/loop.py:156 | **nobody on `post`** (dead here; consumed on `complete`) |
| `model` | str | kernel/loop.py:157 | main.rs:2674 (mid-step switch detection) |
| `request` | obj (redacted) | kernel/loop.py:158 | main.rs:2682 `set_last_post`; child copy main.rs:2536-2541 |

### `complete` — kernel/loop.py:271-285
| field | type | produced | consumed |
|---|---|---|---|
| `n` | int | kernel/loop.py:274 | main.rs:2701; agents/subagent.py:413 |
| `origin` | `"user"\|"llm"` | kernel/loop.py:275 | main.rs:2702 (PostArgs) |
| `model` | str | kernel/loop.py:276 | main.rs:2703, 2706 |
| `thinking` | str | kernel/loop.py:277 | main.rs:2704 |
| `thoughts` | int | kernel/loop.py:278 | main.rs:2710 |
| `redacted` | int | kernel/loop.py:279 | main.rs:2711 |
| `usage` | obj | kernel/loop.py:280 | main.rs:2705-2706 (`cache.observe`); child bill main.rs:2503-2506; agents/subagent.py:410-412 |
| `residue` | str | kernel/loop.py:281 | **nobody** (dead) |
| `spans` | [[int,int]] | kernel/loop.py (byte-converted from `scan_spans(speech)` just above the fire; empty on the OpenAI family, whose calls ride the tool channel) | events.rs `kernel_spans` → stream.rs `finish_speech_spans` (turn-end story reconcile) |
| `request` | obj | kernel/loop.py:282 | main.rs:2707-2708 (`observe_roles`), 2716 |
| `response` | obj | kernel/loop.py:283 | main.rs:2717 |

### `result` — kernel/loop.py:303-312 (start), 314-324 (delta), 342-351 (done)
| field | type | produced | consumed |
|---|---|---|---|
| `phase` | `"start"\|"delta"\|"done"` | all | main.rs:2646, apply_result main.rs:6277; front/acp.py:329; agents/subagent.py:415 |
| `tag` | str | all | main.rs:2648, 6287, 7047; front/acp.py:330; agents/subagent.py:416 |
| `attrs` | obj{str:str} | start, done | main.rs:590 (`call_target`), 7051 (`result_block`) |
| `body` | str (clipped) | start, done | main.rs:605, 7048 |
| `text` | str | start(`""`), delta, done(clipped) | main.rs:6294, 6301; front/acp.py:344 |
| `delta` | bool? | delta phase only (kernel/loop.py:321) | **nobody** (dead; `phase` decides) |
| `span_idx` | int | start, done — the call's position in its turn's dispatch order; `complete.spans[span_idx]` is its speech range when that list is non-empty | typed in desmos-events; TUI correlation consumer pending (the reconcile strips whole turns, not per card) |
| `line` | int? | done, tag=edit, success only — 1-based line of the unique match, located by kernel/edit.py `apply_edit_line` at write time and lifted through dispatch's `meta` out-channel | main.rs `result_block` → `wire_syscall` anchors the diff hunks; absent means the card carries no hunks and claims no line |
| `repo` | obj? `{committed: str}` | done, tag=bash/shell, only when the command's own output carried git's commit summary line (kernel/loop.py `committed_sha` — command text and output both required, so a failed commit never claims) | events.rs → work.rs `WorkRun::commit`: the work row's "· committed <sha>" tail; absent means the row makes no commit claim |

### `turn` — kernel/loop.py:509
| field | type | produced | consumed |
|---|---|---|---|
| `n` | int | kernel/loop.py:509 | **nobody** (dead; main.rs:2722 sets status only, front/acp.py:320 clears error only, agents/subagent.py:402-405 recounts from `w.log` instead) |

### `compacted` — kernel/loop.py:264
| field | type | produced | consumed |
|---|---|---|---|
| `n` | int | kernel/loop.py:264 | main.rs:2688 |
| `kept` | int | kernel/loop.py:264 | main.rs:2689 |
| `text` | str (server summary) | kernel/loop.py:260-264 | main.rs:2690 |

### `done` — kernel/loop.py:459
No fields. Terminator; main.rs:2726-2733 clears `running`, drains queue.

### `stopped` — kernel/loop.py:455 (budget), 457 (user stop)
`text` str → main.rs:2738-2741. Terminator; exactly one of `done`/`stopped`
per step, on every path including exceptions (kernel/loop.py:409-428 docstring, the
`finally` at 451-459).

### `pending` — kernel/loop.py:572
`n` int (count of outstanding background tasks, `agents/pending.py:74 count`).
Consumed by **no UI**: main.rs falls to `_ => {}` (main.rs:2767), acp ignores.
Only pending_check.py:58-59 asserts it exists.

### `resumed` — kernel/loop.py:577
`n` int, `text` str (`agents/pending.py notice`; for a settled subagent the
notice body is the C5 brief — `agents/subagent.py child_notice`, `[<id>
<state> depth=<d>] <objective:80> — <verdict>: <summary:200>`, ≤400 chars,
the raw result staying in `.desmos/subagents/<id>.json` and the trajectory).
Consumed by **no UI**; only pending_check.py. A step that parks on background
work and wakes up paints nothing in the TUI for either transition.

### `guidance` — kernel/loop.py:585
`n` int, `text` str (the reminder injected by `on_continue`). Consumed by
**nobody anywhere** (dead event).

### `subagent` — agents/subagent.py, emitted via `_emit` bound at front/bridge.py:83
Three shapes, keyed by `phase`. Consumer: main.rs:2624 → `handle_subagent`
(main.rs:2395); check.py:1729 reads phases.

Every phase carries the Phase 3 tree fields, fixed at spawn time from the
spawning run (`Run.parent`/`Run.depth`, agents/subagent.py): `parent` is the
spawner's run id — null when the root world spawned it — and `depth` is
spawner depth + 1 (root spawns are 0). `_persist(run)` writes both (plus
`budget`, `generation`, `killed`), so late-attach reconstruction has the
tree. Consumer: events.rs `set_tree` → `ChildSess.parent`/`ChildSess.depth`,
rendered by the Track 3.2 tree view (tree.rs `order`/`row_text`, toggled with
`t`).

`phase:"started"` — agents/subagent.py `spawn()`/`rerun()` via `_launch`:
| field | type | consumed |
|---|---|---|
| `id` | str (8-hex) | main.rs:2397 |
| `parent` | str\|null | events.rs `set_tree` → `ChildSess.parent` |
| `depth` | int | events.rs `set_tree` → `ChildSess.depth` |
| `agent` | str | main.rs:2404; events.rs → `ChildSess.agent` (tree row) |
| `persona` | str | main.rs:2405-2409 |
| `task` | str | main.rs:2403 (→ `task_title`) |
| `structured` | bool | **nobody** (dead) |
| `model` | str | main.rs:2410-2414 |
| `generation` | int | Track 4.3 lineage: the parent world's generation at spawn (rerun records the generation at rerun time). Typed in desmos-events; **no TUI reader yet** |

`phase:"progress"` — agents/subagent.py (`publish_progress`):
| field | type | consumed |
|---|---|---|
| `id` | str | main.rs:2397 |
| `parent` | str\|null | events.rs `set_tree` (every phase feeds the tree row now) |
| `depth` | int | same |
| `task` | str | **nobody on this phase** (dead) |
| `stage` | str | main.rs:2422; events.rs `set_run_facts` → tree row |
| `progress` | str | main.rs:2365-2372 (`subagent_status`) |
| `turns` | int | events.rs `set_run_facts` → tree row `tN` |
| `usage` | obj | events.rs `set_run_facts` (input/output_tokens) → tree row |

`phase:` terminal — agents/subagent.py `_execute`'s `finally`; `phase` is
`run.state`: `"done"`, `"failed"`, or `"stopped"` — the last produced when a
`kill_run` intervention cancels the run (contract C3: `kill_subtree` flags the
run, its own loop reads the flag via `should_stop`, and a queued run settles
as "killed before start"), always with stage `stopped`, stop_reason `killed`,
accepted null. The Rust arm at main.rs:2438 finally has its producer.
| field | type | consumed |
|---|---|---|
| `id` | str | main.rs:2397 |
| `parent` | str\|null | events.rs `set_tree` |
| `depth` | int | same |
| `task` | str | **nobody on this phase** (dead) |
| `stage` | str | main.rs:2382-2390 (verdict fallback); events.rs `set_run_facts` |
| `progress` | str | main.rs:2365-2372 |
| `stop_reason` | str | main.rs:2382-2390 |
| `accepted` | bool\|null | main.rs:2379; events.rs → `ChildSess.accepted` (tree verdict) |
| `secs` | f64 | main.rs:2439 |
| `turns` | int | events.rs `set_run_facts` → tree row |
| `usage` | obj | events.rs `set_run_facts` → tree row (the parent still bills from `child` complete events, main.rs:2503) |
| `result` | str (clipped :800) | **nobody** (dead) |
| `error` | str\|null | main.rs:2441-2445, 2483-2487 |

The terminal event is also the TUI's confirmation for any intervention it
sent: events.rs clears `ChildSess.op_sent` (the "sent (unconfirmed)" marker)
on done/failed/stopped, never on the `intervention` event itself.

### `child` — agents/subagent.py (`child_event`)
`{ev:"child", id: run.id, parent: run.parent, depth: run.depth, kind:
<inner ev>, **inner fields minus ev}` — the child's whole event stream
re-enveloped, stamped with the Phase 3 tree fields on every kind. Consumer:
main.rs:2625 → `handle_child` (events.rs), which stores `parent`/`depth` on
the `ChildSess` via `set_tree` (a late attach that never saw `started` still
learns the tree), handles `kind` ∈ `thinking`, `speech`, `post`, `complete`,
`result`, `turn` and **drops** `prompt` (C1 at child level: the injected task
text, present since the loop emits it on every path), `error`, `done`,
`stopped`, `compacted`, `pending`, `resumed`, `guidance` (`_ => {}`) — a
child's error text reaches the human only via the terminal `subagent` event's
`error` field.

### Transport-internal delta channel (not `ev` events)
`complete()`/OpenAI stream callbacks use `kind`, consumed only by
`kernel/loop.py:163-186 on_delta`:
- `thinking_delta` `{text}` — transport/complete.py:408, transport/openai.py:393/398/406
- `thinking` `{redacted, text}` — transport/complete.py:396
- `text_delta` `{text}` — transport/complete.py:413, transport/openai.py:411/417
- `retry` `{attempt int, delay f64, reason str}` — transport/complete.py:594, 683;
  transport/openai.py:659-664 — **consumed by nobody**: `on_delta` matches only the
  three kinds above, so every retry wait is invisible to the user (dead event,
  and a real gap: the UI freezes for up to ~50s with no stated reason).

`front/acp.py` and `agents/pending.py` emit no `ev` events of their own: ACP consumes
(front/acp.py:305-365) and re-speaks JSON-RPC `session/update`; pending's activity
is narrated by kernel/loop.py (`pending`/`resumed` above).

## Ops the bridge accepts — front/bridge.py:100-214 (`serve` match)

| op | fields | handled | sent by |
|---|---|---|---|
| `stop` | — | front/bridge.py:101-103 (reader thread, jumps the queue) | main.rs:2787, 3453 |
| `quit` | — | front/bridge.py:104-107 | main.rs:2776, 2782; check.py:2051 |
| `step` | `text` str | front/bridge.py:125-143 | main.rs:3490 |
| `snapshot` | — | front/bridge.py:144-145 | **nobody** (dead op) |
| `reset` | — | front/bridge.py:146-148 | main.rs:2812, 3594 |
| `reload` | — | front/bridge.py:149-152 | main.rs:3613 |
| `model` | `model` str, `effort` str (both optional, default current) | front/bridge.py:153-184 | main.rs:2844, 3580; check.py:2025 |
| `picker` | — | front/bridge.py:185-188 | check.py:2045 only (no TUI sender) |
| `login` | `method` str (default `"auto"`) | front/bridge.py:189-208 | main.rs:2839 |
| `thinking` | `level` str (default `"low"`) | front/bridge.py:209-212 | main.rs:3587 |
| `kill_run` | `id` str | front/bridge.py `_intervene` — reader thread, inline, never queued behind the step it interrupts; answers with `intervention` + `notice` | tree.rs `kill_op` (`x` on a tree row); checks |
| `rerun` | `id` str | front/bridge.py `_intervene` (same path; `S.rerun` respawns the contract as a fresh id) | tree.rs `rerun_op` (`r` on a tree row); checks |
| `attach` | `since` int (seq, exclusive; ≤0 replays from the header) | front/bridge.py `_serve_client` — **socket clients only**; replays the stamped log under the wire lock, then joins the live fan-out gapless | checks/front.py, checks/conformance.py (no TUI sender yet) |
| anything else | — | front/bridge.py → `error` event | — |

Both transports accept the same ops and feed one inbox queue (the queue is
the serialization); `stop`, `kill_run`, and `rerun` are handled on the reader
threads themselves. A socket client's `quit` detaches that client only —
ending the bridge is the stdio owner's alone.

## Durable bridge surfaces (files, not wire events)

| surface | writer | reader |
|---|---|---|
| `<cwd>/.desmos/events/<session_id>.jsonl` — the sequenced event log (contract C2): line 1 `{"ev":"session","session_id","cwd","ts"}`, then every wire event PLUS `{"seq","ts"}` stamped by front/bridge.py `_log` under the wire lock (producers never stamp; the wire stays seq-less). Append-only, no rotation. | front/bridge.py `_open_log`/`_log` | attach replay (front/bridge.py `_replay`); typed as `LogLine` in desmos-events (`--log` mode of the validate bin); the Phase 6.5 memex source |
| `<cwd>/.desmos/bridge.sock` — the unix-socket transport (Track 3.1), born 0600, stdlib only. A live owner is probed before takeover; unbindable → one startup `notice` and stdio continues alone. Unlinked on exit. | front/bridge.py `_bind_socket` | any local client (`_serve_client` per connection) |
| `<cwd>/.desmos/pending/<task>-<uuid>.json` — Track 1.1 durable handoff: a settled background task's whole notice, written before `done` is visible, renamed into `pending/delivered/` in the same step that appends it to the transcript (the rename is the commit point), replayed exactly once by `pending.replay` at load. Root persistent world only — a child's tasks stay in memory by contract. | agents/pending.py `submit`/`_deliver_file` | agents/pending.py `replay` (kernel/loop.py `new_world` load path) |
| `<cwd>/.desmos/subagents/<id>.json` — the run record: brief fields plus `parent`, `depth`, `budget`, `generation`, `killed`, and the raw `result` (what the C5 brief compressed). | agents/subagent.py `_persist` | late-attach reconstruction; checks |

## Durable kernel state (files, not wire events)

Kernel-owned files the bridge never touches. The fff LMDB is the one
cross-process ranking channel: the kernel writes it (`<edit>` → `touch`), and a
reader — the `<find>` engine today, the TUI fuzzy picker once wired — reads it
one-way. It never flows the other direction.

| surface | writer | reader |
|---|---|---|
| `<cwd>/.desmos/fff` — the fff frecency LMDB (path-search ranking). Recently-`<edit>`ed paths rank higher without the model asking. | `state/find.py touch()` at the `<edit>` dispatch choke point (`kernel/dispatch.py`, `line is not None` branch) → `track_access`; a live `FileFinder` writes it under `<find>` | `state/find.py find()` (`<find>` ranking); **one-way** to the TUI — `crates/desmos-tui/src/fuzzy.rs` reads scores cached at scan time (frame wiring pending), never writes |
| `~/.desmos/registry` (`DESMOS_REGISTRY`) — resume hints: the cwds that ever hosted a persistent root world. | `state/persist.py _append_registry` via `save()` — root worlds only (children `persist=False` short-circuit), deduped + atomic, dead roots lazily pruned | the resume flow (fork template `cd {cwd} && python -m desmos tui`) |

## Search & recall engines

| engine | syscall | provenance | fault boundary |
|---|---|---|---|
| **fff** — fuzzy path search | `<find>` (`state/find.py`, dispatched at `kernel/dispatch.py`); `result` event, tag=`find` | **vendored extension module** — `fff._fff_python` (maturin, abi3-py310) built from `vendor/fff` by `scripts/build-fff-python.sh`, imported in-process | in-process with the kernel by design; an abort in fff-core's unsafe SIMD/mmap is uncatchable and kills the kernel (accepted, named risk — decision record) |
| **memex** — BM25 history recall | `<recall>` (`state/recall.py`, dispatched at `kernel/dispatch.py`); `result` event, tag=`recall` | **external binary** — the `memex-desmos` fork (`scripts/memex-setup.sh`); shelled per call (`memex search --json-array`), tantivy+usearch+ort **never enter our build** | a separate process; absent/stock memex is a prose refusal, children pinned to `source=desmos`, output secret-scrubbed before it spills |

Both ride the existing `result` event (no new `ev` kind); an absent engine is a
refusal in prose naming its setup script, never a second search implementation.

## Dead fields and dead events (findings, not formatting)

Produced and consumed by no UI (checks noted where they are the only reader):

- `ready`/`snapshot`.`ns`, `.tools`, `.cwd` — front/bridge.py:40-42; no Rust reader.
- `picker.providers[].source` — transport/settings.py:161; picker.rs never reads it.
- `complete.residue` — kernel/loop.py:281; no reader.
- `post.origin` — kernel/loop.py:156; read only on `complete`.
- `result.delta` (delta phase) — kernel/loop.py:321; `phase` already decides.
- `turn.n` — kernel/loop.py:509; status-only consumers, subagent recounts from `w.log`.
- `error.n` — kernel/loop.py:361, 530; only `text` is read.
- `subagent` started.`structured`; progress/terminal `task`; terminal
  `result` — agents/subagent.py; handle_subagent never reads them. (`turns`,
  `usage`, and terminal `stopped` came alive with the Track 3.2 tree view and
  C3 kills — see the `subagent` section; `generation` is typed but has no TUI
  reader yet.)
- `prompt` (kernel/loop.py `_run_turns`) — no UI consumer; the event-log
  file and its replay are the intended readers.
- `intervention` (front/bridge.py `_intervene`) — no TUI reader; the `notice`
  twin carries the prose, the terminal `subagent` event carries confirmation.
- `pending` (kernel/loop.py:572), `resumed` (kernel/loop.py:577) — no UI/ACP consumer; only
  pending_check.py:58-67. `guidance` (kernel/loop.py:585) — no consumer at all.
- `retry` transport deltas (transport/complete.py:594/683, transport/openai.py:659) — dropped by
  `kernel/loop.py on_delta`; retry waits are invisible.
- `child` envelope kinds `prompt`/`error`/`done`/`stopped`/`compacted` —
  produced (every child event is forwarded, agents/subagent.py:423-424),
  dropped by handle_child (main.rs:2568).
- op `snapshot` — accepted (front/bridge.py:144), sent by nobody.

The Phase 5.3 enum must still parse every produced field
(`deny_unknown_fields` cuts the other way: the enum lists them, the dead ones
above are candidates for deletion at the producer instead — decide per field
at Phase 3, do not silently widen the enum).
