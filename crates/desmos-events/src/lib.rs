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

/// `result` — kernel/loop.py emits three phase shapes with different field sets.
#[derive(Debug, Deserialize)]
#[serde(tag = "phase", rename_all = "lowercase", deny_unknown_fields)]
pub enum ResultEvent {
    Start {
        tag: String,
        attrs: BTreeMap<String, String>,
        body: String,
        text: String, // always ""
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
    },
}

/// `subagent` — agents/subagent.py emits started / progress / terminal, where the
/// terminal phase is `run.state`: only ever `done` or `failed`.
#[derive(Debug, Deserialize)]
#[serde(tag = "phase", rename_all = "lowercase", deny_unknown_fields)]
pub enum SubagentEvent {
    Started {
        id: String,
        agent: String,
        persona: String,
        task: String,
        structured: bool, // dead but produced
        model: String,
    },
    Progress {
        id: String,
        task: String,
        stage: String,
        progress: String,
        turns: i64,
        usage: Obj,
    },
    Done(SubagentTerminal),
    Failed(SubagentTerminal),
}

/// The terminal payload, identical for `done` and `failed` (agents/subagent.py:533).
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubagentTerminal {
    pub id: String,
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
/// `{ev:"child", id, kind: <inner ev>, **inner fields minus ev}`
/// (agents/subagent.py:423-424). Every loop kind is forwarded, so every loop kind
/// must parse here, including the ones handle_child drops.
#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase", deny_unknown_fields)]
pub enum ChildEvent {
    Turn {
        id: String,
        n: i64,
    },
    Post {
        id: String,
        n: i64,
        origin: Origin,
        model: String,
        request: Obj,
    },
    Complete {
        id: String,
        n: i64,
        origin: Origin,
        model: String,
        thinking: String,
        thoughts: i64,
        redacted: i64,
        usage: Obj,
        residue: String,
        request: Obj,
        response: Obj,
    },
    Thinking {
        id: String,
        redacted: bool,
        text: String,
        delta: Option<bool>,
    },
    Speech {
        id: String,
        text: String,
        delta: Option<bool>,
    },
    Result(ChildResult),
    Error {
        id: String,
        text: String,
        n: Option<i64>,
    },
    Done {
        id: String,
    },
    Stopped {
        id: String,
        text: String,
    },
    Compacted {
        id: String,
        n: i64,
        kept: i64,
        text: String,
    },
    Pending {
        id: String,
        n: i64,
    },
    Resumed {
        id: String,
        n: i64,
        text: String,
    },
    Guidance {
        id: String,
        n: i64,
        text: String,
    },
}

/// A child's `result` event: ResultEvent's shapes plus the envelope `id`.
#[derive(Debug, Deserialize)]
#[serde(tag = "phase", rename_all = "lowercase", deny_unknown_fields)]
pub enum ChildResult {
    Start {
        id: String,
        tag: String,
        attrs: BTreeMap<String, String>,
        body: String,
        text: String,
    },
    Delta {
        id: String,
        tag: String,
        delta: bool,
        text: String,
    },
    Done {
        id: String,
        tag: String,
        attrs: BTreeMap<String, String>,
        body: String,
        text: String,
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
    // --- loop kinds ---
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
