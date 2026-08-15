# Upgrade paths

The execution plan for the ownership split, the library split, and the four
upgrade tracks. This document is the constitution for the `upgrade/all`
branch: every phase below lands as its own commit series, gated on the
verification instruments in the last section. Read AGENTS.md first; nothing
here overrides it.

## Doctrine

**Python is truth, Rust is paint.** `desmos-tui` never computes a fact the
bridge could send. Every fact arrives as an event field; Rust decides only
where pixels go. (Precedent: exo shipped two materializers and two cost
tables across its Rust/TS boundary and the pair disagreed.)

**Testing discipline.** Provider auth (API keys in the environment) may be
used for live verification. All state is temporary: `mkdtemp` for `.desmos`,
pinned temp settings files, temp dirs for filesystem tests. No test or check
ever writes the repo's `.desmos/` or `~/.desmos/`.

## Phase 0 — ownership doc + event vocabulary

`docs/ownership.md`: the fact table (which side owns which fact) plus the
event vocabulary — one entry per `ev` kind the bridge emits, with every
field, where it is produced, and who consumes it. Facts that move from Rust
to events (executed in Phase 3, documented now):

- syscall span classification: kernel emits authoritative spans on the
  turn-end/result event; the TUI keeps only a conservative mid-stream hold
  and reconciles.
- `<edit>` card start line: the edit result event carries `line`.
- work-row commit attribution: the kernel knows which syscall was a
  `git commit` from its own result; the git *pane* stays a Rust-side
  environment viewer.
- cost: stays in Rust only while it remains the single implementation.

## Phase 5.1 — golden-stream recorder (before any move)

`scripts/record-golden.py` runs canned sessions (stubbed `complete_fn`, no
network) through the real loop and captures the NDJSON event stream to
`golden/*.jsonl`. Normalization (timestamps, uuids, durations) lives in one
function used by both record and compare. Deterministic: two consecutive
recordings diff empty. Scenarios: plain turn, multi-syscall turn, edit,
subagent spawn, error turn, user stop, openai-shaped turn.

Phases 1–2 are pure moves: streams must stay byte-identical. Phase 3 changes
them deliberately; the fixture diff is the review artifact per change.

## Phase 1 — Python layers + SDK facade

```
desmos/
├── kernel/      # const, types, scan, dispatch, exec, shell, edit, loop, catalog
├── transport/   # complete, openai, auth, dialect, settings
├── state/       # persist, memory, generations, skills, extensions
├── agents/      # subagent, subagent_contracts, subagent_prompt, pending
├── front/       # bridge, acp, cli
└── checks/      # check.py split per subsystem + runner (--only, --fast)
```

Import direction is law: kernel imports nothing above it; transport imports
kernel only; state imports kernel; agents import kernel+transport+state;
front imports everything; checks import anything.

**The SDK is the facade.** Grown tools in harness state, extensions and
skills import `desmos.loop`, `desmos.types`, … — stored state imports these
names, so they never break. The existing top-level modules become thin
re-export facades with explicit `__all__`; implementation lives under the
subpackages. The facades are the public API; subpackages are private.

`reload_sdk` derives its reload order from the package topology (import
graph), replacing the hand-maintained list that already went stale once
(dialect was missing).

`git mv` per layer, one commit per layer, imports updated in the same
commit. No half-moved transition state.

## Phase 5.3 — cross-language conformance

Rust gets a typed `Event` enum (`serde(deny_unknown_fields)`). A check runs
the real Python bridge over the check scenarios and asserts the enum parses
every emitted event; a Rust test parses the committed golden fixtures. When
the socket transport lands, the same suite runs over stdio and socket.

## Phase 2 — Rust module split

Pure moves, one module per commit, cargo test green each step. Order:
`events.rs` first (protocol seam), then `app.rs` (App + ChildSess; collapse
the duplicated fields and the six resolvers to two), `stream.rs`, `work.rs`,
`wire.rs`, `input.rs` last. Tests move with their module.

## Phase 3 — protocol moves

Spans-on-result, edit line, repo claim, and `parent`+`depth` on
`subagent`/`child` events. One commit per change: kernel/agents emitter +
`events.rs` consumer + updated golden fixture + conformance entry.

## Phase 4 — the tracks

**Track 1, durability spine**
- 1.1 Durable pending handoff: notice file written when a child settles,
  renamed to delivered in the same step that appends it to the transcript,
  replayed at load. The record carries the whole notice text, not a pointer.
- 1.2 Orphan-call repair at load: synthesize a failed result for any call
  with no output, in the persist load path. Never rebuild the prompt from
  events (cache breakpoint requires a byte-stable prefix).
- 1.3 Sequenced event log: every emitted event also appended to
  `.desmos/events/<session>.jsonl` with a monotonic seq. Replay substrate
  for remote attach; auditable history. No tamper-evidence claims.
  Named preconditions for Phase 6.5 (memex cannot build a Record without
  them): one header line per file `{"ev":"session","session_id","cwd","ts"}`;
  `ts` on every line; a `prompt` event carrying the user's message text at
  injection time (never re-derived from POST bodies). Enum + golden fixtures
  updated in the same change.

**Track 2, fork tree**
- 2.1 `parent` + `depth` on `Run`; delete the `_DEPTH` thread-local. Depth
  budget inherited and decremented; leaf scope has no spawn. A refused
  spawn is a result string the parent reads — one terminator per step stays.
- 2.2 `orchestrator` capability: spawn/wait/memory/system/skill + read-only
  probes; no bash/python/edit/shell. Read-only, not pure.
- 2.3 Briefs: the parent transcript receives a structured brief (~200
  bytes), raw output stays in the child record + trajectory. Over-limit
  handling is explicit in the result text.
- No transcript-copying fork primitive; `resume=` is the primitive.

**Track 3, remote bridge + debug TUI**
- 3.1 Unix socket fan-out: `_WIRE` becomes a list of writers under the
  existing lock; a socket reader feeds the same inbox queue (serialization
  = the queue). Socket file permissions; no TCP; stdlib only. Late attach =
  snapshot from `.desmos/subagents/` + replay from seq.
- 3.2 Tree view in the existing TUI: nest `ensure_child` by `parent`. No
  second renderer, no second binary.
- 3.3 Interventions: kill-subtree, re-run-child-with-edited-contract, via
  the same inbox; every intervention is also an event in the seq log.

**Track 4, self-improvement hardening**
- 4.1 `reload_sdk` gated on the fast check tier; refusal is a result
  string, old modules stay live.
- 4.2 `docs/identity.md`: every piece of state, where it lives, what
  survives which reset; summary taught in the runtime block.
- 4.3 Child generation lineage recorded on `Run` once 2.1 lands.

## Verification instruments

1. **Golden-stream replay** (5.1): byte-identical through pure-move phases;
   deliberate diffs reviewed per protocol change.
2. **Layering check**: walk imports with ast, assert the direction. Fails
   the moment a lower layer imports a higher one.
3. **Cross-language conformance** (5.3): the bridge's real output parses
   into the typed Rust enum; both sides checked from one fixture corpus.
4. **Paint-from-events-alone**: render a recorded stream in an empty temp
   dir; story/wire panes identical to fixture. Git pane exempt (declared
   environment viewer).
5. **The floor**: `python -m desmos check` + `cargo test -p desmos-tui`
   green at every phase boundary, plus one adversarial-verify pass per
   phase diff.

No check may assert that a string exists in prose. Every added check must
fail when its fix is reverted — prove it, then restore.

## Decision record: PyO3

Embedding Python in the TUI binary: no. Track 3 is detachability (the kernel
outlives its viewers) and the process boundary is fault isolation (a panic in
the vendored render stack must not kill the kernel, its ptys, or in-flight
subagents). The GIL is not the reason — the render loop never calls Python.

Rust extension modules into Python: held open with a tripwire. The one
candidate is a shared scanner crate (parity by construction for the last
duplicated rule, at the cost of the scanner leaving the live-editable RSI
surface). Promotion condition: if the paint-from-events or conformance suite
catches a story/wire mirroring violation AFTER Phase 3's reconcile design has
landed, the scanner moves to crates/desmos-scan (PyO3 to the kernel, path dep
to the TUI, scan.py the facade over it). Not before: the worst historical
scan bugs were scanner-wrong, not parity — one implementation would have been
consistently wrong, and the golden fixtures are the defense either way.
Events stay untyped on the Python side by design (the vocabulary is
model-growable); the typed Rust enum is a conformance instrument, not a
constructor.

## Phase 6 — search and recall (fff + memex)

Two engines. The doctrine applies unchanged: what the kernel learns arrives
as a syscall result; what the TUI shows is events or a declared environment
viewer. Neither engine gets a second implementation as a fallback — absent
engine means a refusal in prose and the model uses bash/rg (reuse, not
duplication). Dependency order: 6.1→6.2→6.3→6.4 (fff, independent);
Track 1.3 preconditions → 6.5→6.6→6.7 (memex); 6.8 last.

- 6.1 Vendor fff as its OWN workspace: `vendor/fff` committed restricted to
  fff-core, fff-query-parser, fff-python, packages/fff-python (all MIT;
  vendored libgit2 is GPLv2-with-linking-exception, link-safe; zlob feature
  never built). Root manifest gains `exclude = ["vendor/fff"]` plus
  `fff-search = { path = "vendor/fff/crates/fff-core" }` — no dep-table
  merge; maturin builds fff-python (abi3-py310) from its own tree via
  scripts/build-fff-python.sh, wired into setup, never into tui launch.
  Upstream-shaped vendor patch: `track_access(path)` pymethod (port of the
  fff-nvim pair, split so the frecency LMDB write needs no picker and no
  scan). Decision-record extension: a third-party engine as an extension
  module is the sqlite3 case, NOT the scanner tripwire (unchanged) — and the
  accepted risk is named: an abort in fff-core's unsafe SIMD/mmap kills the
  kernel; PanicException is handled, aborts are not.
- 6.2 `<find>` syscall: body = fff-query-parser query, `limit=` attr; path
  search only (grep already has an owner). Engine per world.cwd in a
  module-global dict (reload-survival pattern), ctor with
  `enable_content_indexing=False` explicit, frecency db at `.desmos/fff`,
  watch=True; first query waits for the scan and SAYS SO if still scanning.
  Absent module: loud refusal naming the build script. CAPS: read+edit.
- 6.3 Frecency fed by the kernel's own `<edit>` results at the dispatch
  choke point — one call site, children covered by construction. touch()
  opens the frecency DB alone when no engine is live; full hydration stays
  lazy on first `<find>`. Check: edit alpha_two, then `<find>alpha</find>`
  ranks it first — this is also the tripwire for the vendor patch.
- 6.4 fff in the TUI: the files pane KEEPS read_dir (correct owner of
  "list this directory"); the new surface is a ctrl-key fuzzy picker on a
  side-worker thread (git-pane pattern) sharing the frecency LMDB read-only
  in practice — the LMDB is kernel→TUI one-way (fuzzy_search reads scores
  cached at scan time), declared as ranking state in ownership.md. Check is
  TWO-process: kernel-side track_access, fresh TUI picker open, ranking
  reflects it.
- 6.5 memex fork (`memex-desmos`, pinned rev): memex has no adapter
  mechanism — SourceKind is a closed enum — so the honest path is a fork
  adding SourceKind::Desmos + src/sources/desmos.rs walking
  `<root>/.desmos/events/*.jsonl` (byte-offset incremental), parsing
  prompt/speech/complete.thinking/result-done/subagent/child. Parity
  fixture generated by the golden recorder, never handwritten. Distribution:
  external binary via scripts/memex-setup.sh (tantivy+usearch+ort must not
  enter our build); probe = `memex search --source desmos --limit 1` exit
  code (stock memex rejects the label) — silent when absent, loud when
  present and wrong. Never parse trajectory/*.json or harness.sqlite3.
- 6.6 Registry + resume: save() appends world.cwd to ~/.desmos/registry
  (deduped, atomic, children never write it; lazy-prune dead roots); fork
  resume template `cd {cwd_shell} && python -m desmos tui`, gains
  `--resume {session_id}` when Track 1.3 session ids become resumable.
- 6.7 `<recall>` syscall: shells `memex search <q> --json-array` per call,
  no kernel-owned daemon; warm calls are ms-scale BM25, freshness is
  memex's own TTL+flock lease. Results are spill-capped user-role results —
  on the record, no secret channel. SECURITY: recall output passes the same
  secret scrub as other results before spill; children are restricted to
  source=desmos (cross-agent history is the user's, not a prompt-injected
  child's). setup script ends with `memex index` so first use is warm; a
  timeout says "index may be cold", not bare failure. Lexical default;
  hybrid opt-in (ONNX init cost). CAPS: read+edit now, orchestrator at 2.2.
- 6.8 Closure: no new event kinds here (find/recall ride `result`); any
  event-log header work lands as Track 1.3 with its own enum+golden diff.
  ownership.md gains the picker row and the LMDB/registry inventory rows;
  the runtime block teaches both syscalls as live behaviour. Every check
  proven by reverting its fix.
