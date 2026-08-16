//! Typed mirror of the bridge's NDJSON event vocabulary (Phase 5.3).
//!
//! Source of truth: docs/ownership.md Part 2, verified row-by-row against the
//! producers (front/bridge.py, kernel/loop.py, agents/subagent.py, transport/settings.py). This enum is a
//! conformance instrument, not a constructor: it accepts exactly what is
//! PRODUCED — dead fields the doc lists (`ready.ns`, `complete.residue`,
//! `result.delta`, `subagent.structured`, …) still ride the wire, so they are
//! required here; a field listed in neither producer nor doc is an error
//! (`deny_unknown_fields`), and so is a new `ev` kind. Delete a dead field at
//! its producer first, then here — never widen this enum silently.
//!
//! Optionality follows the doc's per-phase field sets: `result` and
//! `subagent` are keyed on `phase`, the `child` envelope on its nested
//! `kind`, each shape carrying only the fields its emit site writes.
//!
//! Two entry points: [`Event`] is the WIRE (stdout / socket live stream),
//! which never carries `seq`/`ts`; [`parse_log_line`] is the event-log FILE
//! and attach-replay form (contract C2), where the bridge-side writer stamps
//! every event with both and prefixes the file with one `session` header.

use serde::Deserialize;
use std::collections::BTreeMap;

type Obj = serde_json::Map<String, serde_json::Value>;

/// `post.origin` / `complete.origin` — kernel/loop.py writes only these two.
/// `Option<T>` in serde treats a missing key as `None`, which conflates
/// "producer wrote null" with "producer stopped writing the field". These
/// fields are always written (as null or an object), so absence is the
/// regression this crate exists to catch.
fn required_nullable<'de, D, T>(d: D) -> Result<Option<T>, D::Error>
where
    D: serde::Deserializer<'de>,
    T: serde::Deserialize<'de>,
{
    Option::<T>::deserialize(d)
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Origin {
    User,
    Llm,
}

/// `ready`/`snapshot` `.billing` — front/bridge.py `_billing` returns only these.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Billing {
    Plan,
    Usage,
}

/// `intervention.action` — contract C3 names exactly these two ops.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InterventionAction {
    KillRun,
    Rerun,
}

/// One `providers[]` entry — transport/settings.py `picker()` builds every field.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Provider {
    pub provider: String,
    pub ok: bool,
    pub detail: String,
    pub account: String,
    pub plan: String,
    pub source: String, // dead on the Rust side, live on the wire
    pub can_login: bool,
    pub models: Vec<String>,
    pub efforts: Vec<String>,
}

/// `ready.current` / `picker.current` — `asdict(Settings)` or null.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Current {
    pub provider: String,
    pub model: String,
    pub effort: String,
}

/// `result.repo` — tag=bash/shell only, and only when the command's own output
/// carried git's commit summary line (kernel/loop.py `committed_sha`). A failed
/// commit produces no field at all, never an empty object.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Repo {
    pub committed: String,
}

/// `result` — kernel/loop.py emits three phase shapes with different field sets.
/// `span_idx` (Phase 3) is the call's position in its turn's dispatch order:
/// `complete.spans[span_idx]` is the stretch of speech it came from when that
/// list is non-empty (Anthropic family); start/done always carry it, delta
/// stays minimal.
#[derive(Debug, Deserialize)]
#[serde(tag = "phase", rename_all = "lowercase", deny_unknown_fields)]
pub enum ResultEvent {
    Start {
        tag: String,
        attrs: BTreeMap<String, String>,
        body: String,
        text: String, // always ""
        span_idx: u64,
    },
    Delta {
        tag: String,
        delta: bool, // dead (phase decides) but always produced on this phase
        text: String,
    },
    Done {
        tag: String,
        attrs: BTreeMap<String, String>,
        body: String,
        text: String,
        span_idx: u64,
        /// tag=edit only, and only on success: 1-based line of the unique
        /// match, located by kernel/edit.py at write time.
        line: Option<u64>,
        /// tag=bash/shell only, and only when the output proved a commit.
        repo: Option<Repo>,
    },
}

/// `subagent` — agents/subagent.py emits started / progress / terminal, where the
/// terminal phase is `run.state`: `done`, `failed`, or — when a kill_run
/// intervention cancels the run (including before it ever started) —
/// `stopped`, carrying the same terminal payload with stage `stopped`,
/// stop_reason `killed`, and accepted null.
///
/// Phase 3 tree fields, on every phase: `parent` is the spawning run's id
/// (null when the root world spawned this run) and `depth` is the spawner's
/// depth + 1 (root spawns are 0). Both are always written, so absence is drift.
#[derive(Debug, Deserialize)]
#[serde(tag = "phase", rename_all = "lowercase", deny_unknown_fields)]
pub enum SubagentEvent {
    Started {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        agent: String,
        persona: String,
        task: String,
        structured: bool, // dead but produced
        model: String,
        /// Track 4.3 lineage: the parent world's generation at spawn time
        /// (a rerun records the generation at rerun time).
        generation: i64,
    },
    Progress {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        task: String,
        stage: String,
        progress: String,
        turns: i64,
        usage: Obj,
    },
    Done(SubagentTerminal),
    Failed(SubagentTerminal),
    Stopped(SubagentTerminal),
}

/// The terminal payload, identical for `done`, `failed`, and `stopped`
/// (agents/subagent.py emits `run.state` with one field set).
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubagentTerminal {
    pub id: String,
    #[serde(deserialize_with = "required_nullable")]
    pub parent: Option<String>,
    pub depth: u64,
    pub task: String,
    pub stage: String,
    pub progress: String,
    pub stop_reason: String,
    pub accepted: Option<bool>, // null when no judgment ran
    pub secs: f64,
    pub turns: i64,
    pub usage: Obj,
    pub result: String, // clipped :800
    pub error: Option<String>,
}

/// `child` — the child's whole event stream re-enveloped as
/// `{ev:"child", id, parent, depth, kind: <inner ev>, **inner fields minus ev}`
/// (agents/subagent.py). Every loop kind is forwarded, so every loop kind
/// must parse here, including the ones handle_child drops. The envelope stamps
/// the Phase 3 tree fields (`parent`, `depth`) on every kind.
#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase", deny_unknown_fields)]
pub enum ChildEvent {
    /// C1 at child level: the task text injected into the child's transcript,
    /// emitted by the child's own `_run_turns` at injection time.
    Prompt {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        text: String,
        n: i64,
    },
    Turn {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        n: i64,
    },
    Post {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        n: i64,
        origin: Origin,
        model: String,
        request: Obj,
    },
    Complete {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        n: i64,
        origin: Origin,
        model: String,
        thinking: String,
        thoughts: i64,
        redacted: i64,
        usage: Obj,
        residue: String,
        spans: Vec<(u64, u64)>,
        request: Obj,
        response: Obj,
    },
    Thinking {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        redacted: bool,
        text: String,
        delta: Option<bool>,
    },
    Speech {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        text: String,
        delta: Option<bool>,
    },
    Result(ChildResult),
    Error {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        text: String,
        n: Option<i64>,
    },
    Done {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
    },
    Stopped {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        text: String,
    },
    Compacted {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        n: i64,
        kept: i64,
        text: String,
    },
    Pending {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        n: i64,
    },
    Resumed {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        n: i64,
        text: String,
    },
    Guidance {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        n: i64,
        text: String,
    },
}

/// A child's `result` event: ResultEvent's shapes plus the envelope
/// `id`/`parent`/`depth`.
#[derive(Debug, Deserialize)]
#[serde(tag = "phase", rename_all = "lowercase", deny_unknown_fields)]
pub enum ChildResult {
    Start {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        tag: String,
        attrs: BTreeMap<String, String>,
        body: String,
        text: String,
        span_idx: u64,
    },
    Delta {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        tag: String,
        delta: bool,
        text: String,
    },
    Done {
        id: String,
        #[serde(deserialize_with = "required_nullable")]
        parent: Option<String>,
        depth: u64,
        tag: String,
        attrs: BTreeMap<String, String>,
        body: String,
        text: String,
        span_idx: u64,
        /// tag=edit only, on success (same field the parent shape carries).
        line: Option<u64>,
        /// tag=bash/shell only, on a proven commit (same as the parent shape).
        repo: Option<Repo>,
    },
}

/// Every `ev` kind the bridge speaks, one variant per kind.
#[derive(Debug, Deserialize)]
#[serde(tag = "ev", rename_all = "lowercase", deny_unknown_fields)]
pub enum Event {
    // --- bridge-only kinds ---
    Ready {
        model: String,
        provider: String,
        billing: Billing,
        thinking: String,
        generation: i64,
        cwd: String,     // dead but produced
        ns: Vec<String>, // dead but produced
        tools: Vec<String>, // dead but produced
        onboarding: bool,
        #[serde(deserialize_with = "required_nullable")]
        current: Option<Current>,
        providers: Vec<Provider>,
    },
    Snapshot {
        model: String,
        provider: String,
        billing: Billing,
        thinking: String,
        generation: i64,
        cwd: String,
        ns: Vec<String>,
        tools: Vec<String>,
    },
    Picker {
        onboarding: bool,
        #[serde(deserialize_with = "required_nullable")]
        current: Option<Current>,
        providers: Vec<Provider>,
    },
    Login {
        text: String,
        done: Option<bool>,   // front/bridge.py:203 only
        failed: Option<bool>, // front/bridge.py:205 only
    },
    Notice {
        text: String,
    },
    /// `intervention` — front/bridge.py `_intervene`: one per kill_run/rerun
    /// op arriving on any transport (stdio or socket); its prose twin rides
    /// the `notice` kind. `result` is subagent.py's answer — an unknown id is
    /// a refusal string here, never an error event.
    Intervention {
        action: InterventionAction,
        id: String,
        result: String,
    },
    // --- loop kinds ---
    /// C1: the user's message text at injection time (kernel/loop.py
    /// `_run_turns`, immediately before the message is appended), never
    /// re-derived from POST bodies. `n` is the step ordinal within the
    /// session, starting at 1.
    Prompt {
        text: String,
        n: i64,
    },
    Turn {
        n: i64,
    },
    Post {
        n: i64,
        origin: Origin,
        model: String,
        request: Obj,
    },
    Complete {
        n: i64,
        origin: Origin,
        model: String,
        thinking: String,
        thoughts: i64,
        redacted: i64,
        usage: Obj,
        residue: String, // dead but produced
        /// Phase 3: UTF-8 byte ranges of the final speech that were dispatched
        /// as calls; the story pane's turn-end reconcile strips exactly these.
        spans: Vec<(u64, u64)>,
        request: Obj,
        response: Obj,
    },
    Thinking {
        redacted: bool,
        text: String,
        delta: Option<bool>, // absent on the unstreamed replay (kernel/loop.py:244)
    },
    Speech {
        text: String,
        delta: Option<bool>, // true on stream deltas only (kernel/loop.py:187)
    },
    Result(ResultEvent),
    Error {
        text: String,
        n: Option<i64>, // kernel/loop.py:362/535 only; bridge sites omit it
    },
    Done {},
    Stopped {
        text: String,
    },
    Compacted {
        n: i64,
        kept: i64,
        text: String,
    },
    Pending {
        n: i64,
    },
    Resumed {
        n: i64,
        text: String,
    },
    Guidance {
        n: i64,
        text: String,
    },
    Subagent(SubagentEvent),
    Child(ChildEvent),
}

/// One line of the event-log file `.desmos/events/<session_id>.jsonl` or of
/// an attach replay (contract C2). The bridge-side writer stamps every wire
/// event with a monotonic `seq` and an int-ms `ts`; the file's first line is
/// the unstamped session header. The wire enum above stays seq-less — a
/// `seq`/`ts` on a live wire event is producer drift and must fail there.
#[derive(Debug)]
pub enum LogLine {
    Session {
        session_id: String,
        cwd: String,
        ts: i64,
    },
    Stamped {
        seq: i64,
        ts: i64,
        event: Event,
    },
}

/// Parse one event-log line. Not serde-derived on purpose: `deny_unknown_fields`
/// is silently dropped under `#[serde(flatten)]`, so the stamps are stripped by
/// hand and the remainder goes through the same strict [`Event`] parser as the
/// wire.
pub fn parse_log_line(line: &str) -> Result<LogLine, String> {
    let mut obj: serde_json::Map<String, serde_json::Value> =
        serde_json::from_str(line).map_err(|e| e.to_string())?;
    if obj.get("ev").and_then(serde_json::Value::as_str) == Some("session") {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Session {
            #[allow(dead_code)]
            ev: String,
            session_id: String,
            cwd: String,
            ts: i64,
        }
        let s: Session =
            serde_json::from_value(serde_json::Value::Object(obj)).map_err(|e| e.to_string())?;
        return Ok(LogLine::Session {
            session_id: s.session_id,
            cwd: s.cwd,
            ts: s.ts,
        });
    }
    let seq = obj
        .remove("seq")
        .and_then(|v| v.as_i64())
        .ok_or("log line missing int seq")?;
    let ts = obj
        .remove("ts")
        .and_then(|v| v.as_i64())
        .ok_or("log line missing int ts")?;
    let event: Event =
        serde_json::from_value(serde_json::Value::Object(obj)).map_err(|e| e.to_string())?;
    Ok(LogLine::Stamped { seq, ts, event })
}
