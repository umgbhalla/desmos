# Identity: what desmos is made of

The exo-style inventory. Every piece of state, where it lives, and what
survives which reset. Every row is anchored to the code that writes it; a row
without a writer is a contract row and says so. The 5-line summary lives in
the runtime block (`desmos/kernel/catalog.py`); this file is the full table.

Resets, from mildest to hardest:

- `<reload_sdk/>` — reimports `desmos.*` (`kernel/loop.py reload_sdk`).
- `<rollback n>` — restores notes/tools/prior from `generations/NNNN.json`
  (`state/generations.py rollback` → `apply_snapshot`).
- reset (TUI reset op) — `kernel/loop.py reset_transcript`: drops
  `messages` and `prior`, then saves. Notes, tools, memory, generations stay.
- process restart — everything in RAM dies; `state/persist.py load` rebuilds
  from the db.
- `rm -rf .desmos` — the repo-local store dies; `~/.desmos` does not.

## The inventory

Columns: survives `<reload_sdk/>` / process restart / `<rollback>` /
`rm -rf .desmos` / checked-in.

| state | lives in | writer | reload | restart | rollback | rm .desmos | git |
|---|---|---|---|---|---|---|---|
| code + prompts (ABI `kernel/const.py`, catalog `kernel/catalog.py`, docs/, packaged skills `desmos/skills/`) | git worktree | humans + the model via `<edit>` (`kernel/edit.py`) | yes — reload re-reads it | yes | yes | yes | **yes** |
| harness db: workspaces, attach sessions and lineage, per-session messages/calls/events, prior turns, notes, grown tools, generation, thinking, presence and channels | `world.state_path` or `<cwd>/.desmos/harness.sqlite3` (`state/persist.py state_file`) | `state/persist.py` — one `DESMOS_SESSION_ID` per attach; workspace rows own state that survives restart, session rows own what happened during one attach; retention keeps 24 sessions and cascades their dependent rows | yes | **24-session lineage** — the load tail is `turn_aligned` and keeps the newest compaction checkpoint; message ownership is never copied across sessions | transcript yes; notes/tools/prior **replaced** from the snapshot (that is the point); generation never rewinds (`max(world.generation, n)`) | no | no |
| memory records + derived handbook | `.desmos/memories/records.jsonl`, `MEMORY.md`, `memory_summary.md` beside the db (`state/memory.py memory_root`) | `state/memory.py _save` via `remember`/`forget`/`verify`/`consolidate` — atomic, secret-scrubbed (`_redact`); refuses on `persist=False` | yes | yes | **yes** — `apply_snapshot` touches notes/tools/prior only (driven in `checks/kernel.py`) | no | no |
| generation snapshots (notes/tools/docs/prior — not messages, not memory) | `.desmos/generations/NNNN.json` | `state/generations.py write_generation` (persist-gated; `ensure_gen1` at world birth) | yes | yes | rollback reads them, never deletes; ids only go up | no | no |
| trajectory (exact outgoing POST per `complete()`) | `.desmos/trajectory/*.json` (`DESMOS_TRAJECTORY`; **process**-cwd-relative) | `transport/complete.py log_payload` — unique name + `os.replace`; `prune_trajectory` strips payloads past the newest 12, deletes past 400 | yes | yes | yes | no | no |
| pending handoff records (Track 1.1, landing this phase) | `.desmos/pending/` | contract: when a child settles, a notice file carrying the **whole notice text** is written; renamed to delivered in the same step that appends it to the transcript; replayed at load — a crash between settle and delivery cannot lose it | yes | yes — its purpose | yes | no | no |
| event replay | `events` table in the harness db, keyed by attach session | `state/persist.py record_event`, called by `front/bridge.py`; bridge-writer seq/time stamps, provider ciphertext stripped, giant post/complete bodies represented by byte count + SHA-256, animation/timing events stay live-only | yes | yes, bounded by 24-session retention | yes | no | no |
| subagent run records (raw child result; only the ≤400-char brief enters the parent transcript) | `.desmos/subagents/<id>.json` (**process**-cwd-relative `DIR`) | `agents/subagent.py _persist` at spawn and settle — Run minus messages, plus cfg | yes — `reload_sdk` also carries live `RUNS` + emitter across (`kernel/loop.py`) | file yes; live `RUNS` no | yes | no | no |
| fff frecency LMDB (path-search ranking; `<find>` reads it, recently-`<edit>`ed paths rank higher) | `<cwd>/.desmos/fff` | `state/find.py touch()` at the `<edit>` dispatch choke point → `track_access` (every world, root and child, by construction); a live `FileFinder` writes it under `<find>` | yes — file on disk; the module-global `_ENGINES` survives `reload_sdk` via `globals().get` | yes — file persists; the in-memory engine dies and rebuilds lazily on the next `<find>` | no — snapshots carry notes/tools/prior only | no | no |
| settings: provider / model / effort | `~/.desmos/settings.json` (`DESMOS_SETTINGS`) — **machine-global: one file for every checkout; a model switch in one repo switches all** | `transport/settings.py save` (atomic) | yes | yes | yes | **yes** | no |
| auth | `~/.desmos/auth.json` (`DESMOS_AUTH`), borrowed `~/.codex/auth.json` (`CODEX_HOME`), `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` env | `transport/auth.py write_auth_file` — 0600, atomic; a refresh rotates the refresh token, so writes go back to the file that was read | yes | yes | yes | **yes** | never — nor in logs, trajectory, or results |
| session registry (resume hints — the cwds that ever hosted a persistent world) | `~/.desmos/registry` (`DESMOS_REGISTRY`) — **machine-global** | `state/persist.py _append_registry` via `save()` — root worlds only (children `persist=False` short-circuit), deduped + atomic, dead roots lazily pruned | yes | yes | no | **yes** — under `~/.desmos`, not the repo | no |
| skills | packaged `desmos/skills/`; shared `~/.agents/skills`; user `~/.desmos/skills`; project chain `<dir>/.agents/skills` + `<dir>/.desmos/skills` up to the git root (`skills/__init__.py skill_roots`, rediscovered every turn) | the model via `<edit>`/bash | yes | yes | yes | only the project `.desmos/skills` root dies | packaged root only |
| extensions | `~/.desmos/extensions` + project chain `.desmos/extensions` (`state/extensions.py extension_roots`, loaded per turn) | the model via `<edit>`/bash | yes | yes | yes | project root dies; user root survives | no |
| TUI pane layout | `.desmos/tui.json` | `crates/desmos-tui/src/app.rs` (~231) — pixels; the kernel never reads it | yes | yes | yes | no | no |
| in-memory World: `ns` values, `messages` beyond the persisted tail, `shells` ptys, pending monitors (`agents/pending._BY_WORLD`), subagent `RUNS`, `transport/complete.LAST` | the process | the running kernel | **yes, by design** — `_RELOAD_SKIP` protects live-state modules and `reload_sdk` rebinds `RUNS`/emitter (driven in `checks/kernel.py`) | no | `ns`/`messages` untouched; notes/tools swapped | unaffected | no |

Planned (Phase 6 — built but not yet wired on this branch; contract, not inventory):

| surface | lives in | pending wiring |
|---|---|---|
| TUI fuzzy file picker read of the fff LMDB (one-way, kernel writes → TUI reads at scan time) | `crates/desmos-tui/src/fuzzy.rs` | the module exists but the frame (ctrl-key open, overlay draw, worker `poll`) is not wired in `main.rs`/`app.rs`/`input.rs` yet (6.4). The LMDB writer and the `<find>` reader are already live above. |

## Operating rules that fall out

- **What a fork inherits.** A child is `new_world(persist=False,
  state_path=None)` (`agents/subagent.py _child_world`): it loads nothing
  from the db, writes nothing back, cannot `remember()` ("memory disabled"),
  cannot snapshot a generation. It does inherit code, settings, auth, and the
  skills/extensions roots. Its only durable traces are its run record, its
  trajectory files, its child-enveloped events, and the files it edits.
- **Speech is not memory, and neither is `ns`.** The transcript is a tail
  and `ns` dies with the process. If future-you needs it: a memory record
  (survives everything but `rm -rf .desmos`), a note (survives restart, is
  replaced by rollback), or a file/skill (survives everything, durable in git
  once committed).
- **Rollback is narrow.** It moves notes, grown tools, and prior turns —
  nothing else. Do not expect it to undo an edit, a memory, or the
  transcript; do not fear it eating them either.
- **Machine-global blast radius.** `settings.json` and `auth.json` are
  per-user, not per-repo. Any test or check pins `DESMOS_SETTINGS`,
  `DESMOS_AUTH`, and a temp `.desmos` before touching them.
- **Two process-cwd-relative writers.** `transport/complete.TRAJECTORY_DIR`
  and `agents/subagent.DIR` resolve against the process cwd, not
  `world.cwd`. The fronts chdir first (`front/cli.py`); an embedded
  `attach()` world with a different cwd writes them beside the process.
