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
| Syscall span classification (which stretch of streamed speech is a call, not prose) | `main.rs:6181 spoken_prefix`, `main.rs:6775 strip_syscalls`, `main.rs:6658 next_tag`, `main.rs:6706 find_close`, `main.rs:6726 in_string` — a full port of scan.py's grammar, run per frame on the stream | `scan.py:228 scan`, `scan.py:259 scan_spans`, `scan.py:133 _in_string`; the loop computes the authoritative blocks per turn at `loop.py:287-298` | `spans` on the turn-end/`result` event; TUI keeps only a conservative mid-stream hold and reconciles |
| `<edit>` card start line | `main.rs:7255 edit_start_line` re-reads the target file (`std::fs::read_to_string`, `main.rs:7259`) and searches for the unique match; wrong answer whenever the file changed again before the event painted | `edit.py:29-34 apply_edit` locates the unique occurrence at write time (`content.count` / `content.replace`) | `line` on the edit `result` event |
| Work-row commit attribution ("· committed abc123") | `main.rs:633 git_tail` + `main.rs:709 note_repo` + `main.rs:796 settle` compare HEAD snapshots from the git pane worker (`side.rs:269` forks `git`), generation-gated because the snapshot races the commit | the kernel ran the syscall and holds its result: `loop.py:342-351` (result done), exit code written by `shell.py:232` | repo claim on the `result` event; the git *pane* stays a Rust-side environment viewer |
| Cost and cache savings | `main.rs:1152 model_price` (hardcoded $/Mtok table), `main.rs:1265 bill`, `main.rs:1281` cost formula, 0.1x/1.25x/2x cache multipliers | kernel holds the usage (`loop.py:213-223`, emitted on `complete` at `loop.py:280`) but has no price table; Rust is the single implementation | stays in Rust while it remains the single implementation (upgrade-paths Phase 0) |
| Syscall failure classification (red card or not) | `main.rs:7303 looks_failed` greps result text for `Traceback`/`SyntaxError`/`exit `; `main.rs:7216 edit_failed` greps for `error`/`no match`/…; applied at `main.rs:6312` and `main.rs:7145,7174` | the kernel produced the failure: `shell.py:232` appends `[exit N]`, `loop.py:340` captures the traceback as the result, `edit.py:11-42` returns `edit failed: …` strings | `ok`/`exit` on the `result` done event. (Latent bug the move erases: shell writes `[exit N]`, `looks_failed` tests `starts_with("exit ")` — brackets never match.) |
| Edit body split into old/new | `main.rs:7232 split_edit_body` re-parses the `---` body, silently treating a no-separator body as pure insertion | `edit.py:66 parse_edit_body` is the authority and *rejects* ambiguous bodies the Rust splitter accepts | `old`/`new` (or hunks) on the edit `result` event |
| Call target (the program/path a call was aimed at, for the work-row sentence) | `main.rs:588 call_target` re-parses the bash body: splits on `&&`/`;`, skips `cd`/`export`/assignments, takes the program word | shell semantics live in `shell.py`; tag + attrs already ride the `result` event (`loop.py:305-311`) — the body parse duplicates what the kernel executed | `target` on the `result` event |
| Which transcript chunk is syscall output vs prose vs thinking (context-bar roles) | `main.rs:1173 observe_roles` scans the request JSON for `<result` substrings and block types, twice (Anthropic and OpenAI shapes) | `loop.py:64 result_content` *builds* those messages; `loop.py:553-556` appends them — the kernel knows each message's kind at creation | role/segment annotation on the `post`/`complete` request, or kernel-computed totals |
| Context window ceiling (denominator of the context bar) | `main.rs:1143 model_window` hardcodes 400k/200k by model prefix | nobody: `settings.py:25 CATALOG` knows models and efforts but not windows. Today this fact has no owner; the kernel is its natural home | `window` on `ready`/`snapshot`/`complete` |
| Cache TTL bucket (5m vs 1h countdown) | `main.rs:1311-1325` infers from `usage.cache_creation.ephemeral_1h_input_tokens` | `complete.py:269 cached_payload` sets the breakpoints; the kernel chose the TTL it requested | `ttl` on `complete` (the usage inference is at least wire-derived, so lowest priority) |
| Model in effect mid-step | `main.rs:2674` re-reads `request.model` out of the `post` body to catch a kernel-side switch | `world.model`, already emitted at `loop.py:157` (`post.model`) and `bridge.py:35` (`snapshot.model`) | already a field; `post.model` (`loop.py:157`) is authoritative — the body re-read should die with Phase 3, not be enshrined |
| Subagent story title | `main.rs:2326 task_title` re-derives a headline from the task text (paren stripping, sentence cut, 52-char elision) | `subagent.py:623` holds `run.task`; the contract objective is structured | `title` on the `subagent` started event |
| Prior session turns (resume picker) | `session.rs:150 load_turns` forks `sqlite3` (`session.rs:154`) against `.desmos/harness.sqlite3` and reads the kernel's own table | `persist.py:27 DB_FILENAME`, schema at `persist.py:209`, loaded by the kernel itself at `persist.py:444` | over the bridge (`ready` payload or a new op), not a second reader of the kernel's database |

### Rust-owned surfaces (not facts to move)

| Surface | Where | Why it stays |
|---|---|---|
| Git pane | `side.rs:269` (forks `git` on a worker thread) | declared environment viewer, exempt from paint-from-events (upgrade-paths, instrument 4) |
| Files pane | `side.rs:390 read_dir`, `side.rs:503 fs::read` | environment viewer |
| Pane layout persistence | `main.rs:362-416` (`.desmos/tui.json`) | pixels; Rust decides where pixels go |
| grok pager appearance | `main.rs:1828` reads `util::pager_toml_path()` | grok-build's own config |
| Bridge process spawn | `main.rs:426-455` | the transport itself |

## Part 2 — event vocabulary

Wire = one NDJSON object per line, `_emit` (`bridge.py:64`). Every event from
the loop and subagents funnels through the bridge's `on_event=_emit`
(`bridge.py:139`) or `S.set_emitter(_emit)` (`bridge.py:83`). Consumer is
`handle_event` (`main.rs:2581`) unless said otherwise; `acp.py:305 _emit_event`
is the second consumer and reads only `thinking`, `speech`, `turn`, `error`,
`result`.

Field types: `str`, `int`, `f64`, `bool`, `obj`, `[str]`, `?` = sometimes
absent.

### `ready` — bridge.py:114-118
Snapshot fields plus picker fields, once at startup.

| field | type | produced | consumed |
|---|---|---|---|
| `model` | str | bridge.py:35 | main.rs:2603; picker.rs (via observe, main.rs:2596) |
| `provider` | str | bridge.py:36 | main.rs:2600 (`ephemeral = anthropic`) |
| `billing` | `"plan"\|"usage"` | bridge.py:37 (`_billing`, bridge.py:16) | main.rs:2597 (`cache.plan`) |
| `thinking` | str | bridge.py:38 | main.rs:2611 |
| `generation` | int | bridge.py:39 | main.rs:2614 (accepts int or str) |
| `cwd` | str | bridge.py:40 | **nobody** (dead) |
| `ns` | [str] | bridge.py:41 | **nobody** (dead) |
| `tools` | [str] | bridge.py:42 | **nobody** (dead) |
| `onboarding` | bool | settings.py:168 (via bridge.py:117) | picker.rs:119 |
| `current` | obj\|null | settings.py:169 | picker.rs:111-113 |
| `providers` | [obj] | settings.py:151-166 | picker.rs:88-102 |

`providers[]` entry: `provider` str (picker.rs:95), `ok` bool (picker.rs:96),
`detail` str (picker.rs:97), `account` str (picker.rs:98), `plan` str
(picker.rs:99), `can_login` bool (picker.rs:100), `models` [str]
(picker.rs:101), `efforts` [str] (picker.rs:102), `source` str
(settings.py:161) — **`source` consumed by nobody** (dead).

### `snapshot` — bridge.py:30-43; emitted at 143, 144(op), 148, 152, 179, 212
Same fields as `ready` minus the picker block. Consumer: same
`"ready" | "snapshot"` arm, main.rs:2595-2623.

### `picker` — bridge.py:188, 206
`onboarding`/`current`/`providers` as above. Consumer: main.rs:2588 →
`picker.observe`.

### `login` — bridge.py:201, 203, 205
| field | type | produced | consumed |
|---|---|---|---|
| `text` | str | bridge.py:201/203/205 | main.rs:2590 |
| `done` | bool? | bridge.py:203 only | main.rs:2591 |
| `failed` | bool? | bridge.py:205 only | main.rs:2592 (OR-ed with `done`) |

### `notice` — bridge.py:167-172 (effort clamp), bridge.py:184 (provider switch)
`text` str → main.rs:2750-2755 (story system row).

### `error` — bridge.py:95, 98, 128, 177, 214, 216; loop.py:361, 530, 591
| field | type | produced | consumed |
|---|---|---|---|
| `text` | str | all sites | main.rs:2764; acp.py:327 |
| `n` | int? | loop.py:361, 530 only | **nobody** (dead) |

Also synthesized Rust-side for an unparseable NDJSON line (main.rs:448).
Never a terminator (main.rs:2756-2761 comment; loop keeps going after a
cut-short reply).

### `speech` — loop.py:186 (stream), loop.py:248 (unstreamed), bridge.py:147, 150, 151 (reset/reload confirmations)
| field | type | produced | consumed |
|---|---|---|---|
| `text` | str | all sites | main.rs:2641 → `apply_speech` (main.rs:6370); acp.py:314 |
| `delta` | bool? | loop.py:186 only (`true`) | main.rs:2640 |

### `thinking` — loop.py:168-175 (delta), 176-184 (whole block), 241-247 (unstreamed replay)
| field | type | produced | consumed |
|---|---|---|---|
| `redacted` | bool | all three | main.rs:2627 → `apply_thinking` (main.rs:6338) |
| `text` | str | all three | main.rs:2629; acp.py:307 |
| `delta` | bool? | 168-184 only | main.rs:2628 |

The `kind` deltas feeding this (`thinking_delta`/`thinking`/`text_delta`) are
produced by complete.py:396-413 and openai.py:392-418 and consumed by
`loop.py:163-186 on_delta`; they are transport-internal, not wire events.

### `post` — loop.py:152-160
| field | type | produced | consumed |
|---|---|---|---|
| `n` | int | loop.py:155 | main.rs:2667 |
| `origin` | `"user"\|"llm"` | loop.py:156 | **nobody on `post`** (dead here; consumed on `complete`) |
| `model` | str | loop.py:157 | main.rs:2674 (mid-step switch detection) |
| `request` | obj (redacted) | loop.py:158 | main.rs:2682 `set_last_post`; child copy main.rs:2536-2541 |

### `complete` — loop.py:271-285
| field | type | produced | consumed |
|---|---|---|---|
| `n` | int | loop.py:274 | main.rs:2701; subagent.py:413 |
| `origin` | `"user"\|"llm"` | loop.py:275 | main.rs:2702 (PostArgs) |
| `model` | str | loop.py:276 | main.rs:2703, 2706 |
| `thinking` | str | loop.py:277 | main.rs:2704 |
| `thoughts` | int | loop.py:278 | main.rs:2710 |
| `redacted` | int | loop.py:279 | main.rs:2711 |
| `usage` | obj | loop.py:280 | main.rs:2705-2706 (`cache.observe`); child bill main.rs:2503-2506; subagent.py:410-412 |
| `residue` | str | loop.py:281 | **nobody** (dead) |
| `request` | obj | loop.py:282 | main.rs:2707-2708 (`observe_roles`), 2716 |
| `response` | obj | loop.py:283 | main.rs:2717 |

### `result` — loop.py:303-312 (start), 314-324 (delta), 342-351 (done)
| field | type | produced | consumed |
|---|---|---|---|
| `phase` | `"start"\|"delta"\|"done"` | all | main.rs:2646, apply_result main.rs:6277; acp.py:329; subagent.py:415 |
| `tag` | str | all | main.rs:2648, 6287, 7047; acp.py:330; subagent.py:416 |
| `attrs` | obj{str:str} | start, done | main.rs:590 (`call_target`), 7051 (`result_block`) |
| `body` | str (clipped) | start, done | main.rs:605, 7048 |
| `text` | str | start(`""`), delta, done(clipped) | main.rs:6294, 6301; acp.py:344 |
| `delta` | bool? | delta phase only (loop.py:321) | **nobody** (dead; `phase` decides) |

### `turn` — loop.py:509
| field | type | produced | consumed |
|---|---|---|---|
| `n` | int | loop.py:509 | **nobody** (dead; main.rs:2722 sets status only, acp.py:320 clears error only, subagent.py:402-405 recounts from `w.log` instead) |

### `compacted` — loop.py:264
| field | type | produced | consumed |
|---|---|---|---|
| `n` | int | loop.py:264 | main.rs:2688 |
| `kept` | int | loop.py:264 | main.rs:2689 |
| `text` | str (server summary) | loop.py:260-264 | main.rs:2690 |

### `done` — loop.py:459
No fields. Terminator; main.rs:2726-2733 clears `running`, drains queue.

### `stopped` — loop.py:455 (budget), 457 (user stop)
`text` str → main.rs:2738-2741. Terminator; exactly one of `done`/`stopped`
per step, on every path including exceptions (loop.py:409-428 docstring, the
`finally` at 451-459).

### `pending` — loop.py:572
`n` int (count of outstanding background tasks, `pending.py:74 count`).
Consumed by **no UI**: main.rs falls to `_ => {}` (main.rs:2767), acp ignores.
Only pending_check.py:58-59 asserts it exists.

### `resumed` — loop.py:577
`n` int, `text` str (`pending.py:126 notice`). Consumed by **no UI**; only
pending_check.py:65-67. A step that parks on background work and wakes up
paints nothing in the TUI for either transition.

### `guidance` — loop.py:585
`n` int, `text` str (the reminder injected by `on_continue`). Consumed by
**nobody anywhere** (dead event).

### `subagent` — subagent.py, emitted via `_emit` bound at bridge.py:83
Three shapes, keyed by `phase`. Consumer: main.rs:2624 → `handle_subagent`
(main.rs:2395); check.py:1729 reads phases.

`phase:"started"` — subagent.py:638-649:
| field | type | consumed |
|---|---|---|
| `id` | str (8-hex) | main.rs:2397 |
| `agent` | str | main.rs:2404 |
| `persona` | str | main.rs:2405-2409 |
| `task` | str | main.rs:2403 (→ `task_title`) |
| `structured` | bool | **nobody** (dead) |
| `model` | str | main.rs:2410-2414 |

`phase:"progress"` — subagent.py:377-388 (`publish_progress`):
| field | type | consumed |
|---|---|---|
| `id` | str | main.rs:2397 |
| `task` | str | **nobody on this phase** (dead) |
| `stage` | str | main.rs:2422 |
| `progress` | str | main.rs:2365-2372 (`subagent_status`) |
| `turns` | int | **nobody** (dead) |
| `usage` | obj | **nobody** (dead) |

`phase:` terminal — subagent.py:531-547; `phase` is `run.state`, which is only
ever `"done"` or `"failed"` (subagent.py:487, 491, 524). The Rust arm also
matches `"stopped"` (main.rs:2438) — **no producer emits it**; it is a
speculative branch until Track 3 interventions exist.
| field | type | consumed |
|---|---|---|
| `id` | str | main.rs:2397 |
| `task` | str | **nobody on this phase** (dead) |
| `stage` | str | main.rs:2382-2390 (verdict fallback) |
| `progress` | str | main.rs:2365-2372 |
| `stop_reason` | str | main.rs:2382-2390 |
| `accepted` | bool\|null | main.rs:2379 |
| `secs` | f64 | main.rs:2439 |
| `turns` | int | **nobody** (dead) |
| `usage` | obj | **nobody** (dead; the parent bills from `child` complete events instead, main.rs:2503) |
| `result` | str (clipped :800) | **nobody** (dead) |
| `error` | str\|null | main.rs:2441-2445, 2483-2487 |

### `child` — subagent.py:423-424
`{ev:"child", id: run.id, kind: <inner ev>, **inner fields minus ev}` — the
child's whole event stream re-enveloped. Consumer: main.rs:2625 →
`handle_child` (main.rs:2493), which handles `kind` ∈ `thinking`, `speech`,
`post`, `complete`, `result`, `turn` and **drops** `error`, `done`, `stopped`,
`compacted`, `pending`, `resumed`, `guidance` (main.rs:2568 `_ => {}`) — a
child's error text reaches the human only via the terminal `subagent` event's
`error` field. Phase 3 adds `parent` + `depth` here (upgrade-paths, Phase 3).

### Transport-internal delta channel (not `ev` events)
`complete()`/OpenAI stream callbacks use `kind`, consumed only by
`loop.py:163-186 on_delta`:
- `thinking_delta` `{text}` — complete.py:408, openai.py:393/398/406
- `thinking` `{redacted, text}` — complete.py:396
- `text_delta` `{text}` — complete.py:413, openai.py:411/417
- `retry` `{attempt int, delay f64, reason str}` — complete.py:594, 683;
  openai.py:659-664 — **consumed by nobody**: `on_delta` matches only the
  three kinds above, so every retry wait is invisible to the user (dead event,
  and a real gap: the UI freezes for up to ~50s with no stated reason).

`acp.py` and `pending.py` emit no `ev` events of their own: ACP consumes
(acp.py:305-365) and re-speaks JSON-RPC `session/update`; pending's activity
is narrated by loop.py (`pending`/`resumed` above).

## Ops the bridge accepts — bridge.py:100-214 (`serve` match)

| op | fields | handled | sent by |
|---|---|---|---|
| `stop` | — | bridge.py:101-103 (reader thread, jumps the queue) | main.rs:2787, 3453 |
| `quit` | — | bridge.py:104-107 | main.rs:2776, 2782; check.py:2051 |
| `step` | `text` str | bridge.py:125-143 | main.rs:3490 |
| `snapshot` | — | bridge.py:144-145 | **nobody** (dead op) |
| `reset` | — | bridge.py:146-148 | main.rs:2812, 3594 |
| `reload` | — | bridge.py:149-152 | main.rs:3613 |
| `model` | `model` str, `effort` str (both optional, default current) | bridge.py:153-184 | main.rs:2844, 3580; check.py:2025 |
| `picker` | — | bridge.py:185-188 | check.py:2045 only (no TUI sender) |
| `login` | `method` str (default `"auto"`) | bridge.py:189-208 | main.rs:2839 |
| `thinking` | `level` str (default `"low"`) | bridge.py:209-212 | main.rs:3587 |
| anything else | — | bridge.py:214 → `error` event | — |

## Dead fields and dead events (findings, not formatting)

Produced and consumed by no UI (checks noted where they are the only reader):

- `ready`/`snapshot`.`ns`, `.tools`, `.cwd` — bridge.py:40-42; no Rust reader.
- `picker.providers[].source` — settings.py:161; picker.rs never reads it.
- `complete.residue` — loop.py:281; no reader.
- `post.origin` — loop.py:156; read only on `complete`.
- `result.delta` (delta phase) — loop.py:321; `phase` already decides.
- `turn.n` — loop.py:509; status-only consumers, subagent recounts from `w.log`.
- `error.n` — loop.py:361, 530; only `text` is read.
- `subagent` started.`structured`; progress/terminal `task`, `turns`, `usage`;
  terminal `result` — subagent.py:377-388, 531-547; handle_subagent never
  reads them (check.py:1729 reads phases only).
- `subagent` terminal phase `"stopped"` — matched at main.rs:2438, emitted by
  nobody (run.state is only `done`/`failed`).
- `pending` (loop.py:572), `resumed` (loop.py:577) — no UI/ACP consumer; only
  pending_check.py:58-67. `guidance` (loop.py:585) — no consumer at all.
- `retry` transport deltas (complete.py:594/683, openai.py:659) — dropped by
  `loop.py on_delta`; retry waits are invisible.
- `child` envelope kinds `error`/`done`/`stopped`/`compacted` — produced
  (every child event is forwarded, subagent.py:423-424), dropped by
  handle_child (main.rs:2568).
- op `snapshot` — accepted (bridge.py:144), sent by nobody.

The Phase 5.3 enum must still parse every produced field
(`deny_unknown_fields` cuts the other way: the enum lists them, the dead ones
above are candidates for deletion at the producer instead — decide per field
at Phase 3, do not silently widen the enum).
