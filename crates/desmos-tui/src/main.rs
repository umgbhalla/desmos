//! Three-pane desmos TUI.
//!
//! Middle and right host grok-build `ScrollbackState` + `ScrollbackPane`:
//! UserPrompt / Thinking / AgentMessage / ToolCall with Collapsed /
//! Truncated / Expanded, selection box, hover, click-to-select,
//! double-click fold, j/k and Tab.
//!
//! Spawn is grok's async subagent: parent story keeps a `SubagentBlock`
//! (started stays, completed/failed is a second row). Enter / Ctrl-F /
//! double-click opens the child session (its own story + calls). Esc
//! returns. Child thinking/speech never land on the parent story.
//!
//! Paste is grok's path: bracketed paste, Ctrl+V / Cmd+V, chips for
//! long blobs, Shift+Enter newline, Enter on a chip expands it.
//!
//! Zoom is grok's BlockViewerPane. Enter / Ctrl-F on a block opens it
//! (markdown / execute / other as plain text) and ListPane re-wraps at
//! the popup width. Esc / q / Ctrl-F close. Spawn still wins on a
//! SubagentBlock. Terminal font zoom is a resize: prepare_layout + the
//! viewer re-wrap, not a second renderer.
//!
//! POST in/out expand with Ctrl-F / e / double-click into a mid grok
//! modal (in/out tabs). Same JSON tree at popup width; r is raw pretty
//! JSON in BlockViewerPane.
//!
//! Bottom split under story+calls is the last complete() POST as a
//! foldable JSON tree (not a pretty-printed blob). Redacted ciphertext
//! is stripped. POST in paints when the request is queued; thinking and
//! speech stream in as deltas; each syscall card lands when that tag
//! finishes. A turn is not a single paint at the end.
//!
//! Query stack: Enter mid-step queues a follow-up. After the step ends the
//! front runs. Empty Enter is send-now (stop + fire the front).

mod app;
mod events;
mod fuzzy;
mod input;
mod json_tree;
mod picker;
mod prompt;
mod queue;
mod session;
mod side;
mod slash;
mod stream;
mod tree;
mod wire;
mod work;

/// The theme cache is process-global and cargo runs tests on threads. Any test
/// that reads or writes it takes this first, in any module of this binary.
///
/// Taking it also installs the theme, once. `theme_cache::current_kind` seeds
/// itself lazily from `~/.grok/config.toml` and falls back to grok's GrokNight
/// when that names no theme, while `App::new` installs desmos's own default —
/// so whichever ran first decided the answer. A test could read `GrokNight`
/// from an untouched cache, have another thread's `App::new` install
/// `OscuraMidnight` a microsecond later, and then render its rows under a
/// palette that no longer matched the one it had asserted against. Seeding
/// here settles `LOADED` before the guard is handed out, which kills the lazy
/// path for the rest of the process; every later write is the same value.
#[cfg(test)]
fn theme_lock() -> std::sync::MutexGuard<'static, ()> {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    static SEED: std::sync::Once = std::sync::Once::new();
    let guard = LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    SEED.call_once(|| theme_cache::set(initial_theme()));
    guard
}

use std::io::{self, BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, TryRecvError};
use std::time::{Duration, Instant};

use crossterm::cursor::{EnableBlinking, SetCursorStyle};
use crossterm::event::{
    self, DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
    Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::layout::{Constraint, Direction, Layout, Position, Rect};
use ratatui::prelude::CrosstermBackend;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph};
use ratatui::{Frame, Terminal};
use serde_json::{Value, json};
use unicode_width::UnicodeWidthStr;
use xai_grok_pager::acp::tracker::{TurnActivity, WaitingReason};
use xai_grok_pager::appearance::{
    self, AppearanceConfig, RawAppearanceConfig, cache as appearance_cache,
};
use xai_grok_pager::clipboard::SystemClipboard;
use xai_grok_pager::glyphs;
use xai_grok_pager::input::is_mod_enter;
#[cfg(test)]
use xai_grok_pager::scrollback::blocks::SubagentBlockKind;
use xai_grok_pager::scrollback::blocks::{
    EditToolCallBlock, ExecuteToolCallBlock, OtherToolCallBlock, SubagentBlock, ToolCallBlock,
};
use xai_grok_pager::scrollback::render::InlineMediaPlacement;
use xai_grok_pager::scrollback::{
    EntryId, RenderBlock, ScratchBuffer, ScrollbackEntry, ScrollbackPane, ScrollbackState,
    text_selection::{
        ActiveTextDrag, PendingTextDrag, PersistentTextSelection, ResolvedSelectionModel,
        SelectionEndpoint, SelectionKind, SelectionOrigin, configured_word_separators,
        drag_threshold_exceeded, reconstruct_selection_text, render_active_selection_overlay,
        render_persistent_selection_overlay, semantic_selection_at,
    },
};
use xai_grok_pager::terminal::image as gfx;
use xai_grok_pager::theme::{Theme, ThemeKind, cache as theme_cache};
use xai_grok_pager::util;
use xai_grok_pager::views::block_viewer::{BlockViewerPane, ViewerKind};
use xai_grok_pager::views::modal_window::{
    ModalSizing, ModalWindowConfig, ModalWindowOutcome, ModalWindowState, Shortcut,
    handle_modal_key, handle_modal_mouse, render_modal_window,
};
use xai_grok_pager_diff::diff_hunks_from_strings;

use app::*;
use events::*;
use input::*;
use json_tree::JsonTree;
use prompt::{clipboard_text, coalesce_events, is_inline_paste_key, is_paste_key, is_text_key};
use stream::*;
use wire::*;
use work::*;

#[derive(Clone, Copy, PartialEq, Eq)]
enum ViewerSrc {
    Story,
    Calls,
}

/// Mid popup for the last complete() POST. Grok modal chrome, in/out tabs.
/// Tree is the same JsonTree as the bottom split; `r` swaps in grok
/// BlockViewerPane over pretty-printed JSON so search/wrap/copy work.
struct PostInspect {
    modal: ModalWindowState,
    raw: bool,
    raw_viewer: Option<BlockViewerPane>,
    content: Rect,
}

impl PostInspect {
    fn open(tab: usize) -> Self {
        let mut modal = ModalWindowState::with_tabs(2);
        modal.active_tab = tab.min(1);
        Self {
            modal,
            raw: false,
            raw_viewer: None,
            content: Rect::default(),
        }
    }

    fn set_tab(&mut self, tab: usize, n: u64, req: &Value, resp: &Value) {
        self.modal.active_tab = tab.min(1);
        self.sync_raw(n, req, resp);
    }

    fn toggle_raw(&mut self, n: u64, req: &Value, resp: &Value) {
        self.raw = !self.raw;
        self.sync_raw(n, req, resp);
    }

    fn sync_raw(&mut self, n: u64, req: &Value, resp: &Value) {
        if !self.raw {
            self.raw_viewer = None;
            return;
        }
        let (title, val) = if self.modal.active_tab == 0 {
            (format!("POST in #{n}"), req)
        } else {
            (format!("POST out #{n}"), resp)
        };
        self.raw_viewer = Some(BlockViewerPane::for_plain_text(&title, &pretty_json(val)));
    }
}

fn pretty_json(v: &Value) -> String {
    if v.is_null() || v == &Value::Object(Default::default()) {
        return "no POST yet".into();
    }
    serde_json::to_string_pretty(v).unwrap_or_else(|_| v.to_string())
}

struct Bridge {
    child: Child,
    stdin: ChildStdin,
    rx: Receiver<Value>,
}

impl Bridge {
    fn spawn(python: &str, cwd: &str) -> io::Result<Self> {
        let mut child = Command::new(python)
            .args(["-m", "desmos", "bridge", "--cwd", cwd])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "bridge stdin"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "bridge stdout"))?;
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if line.trim().is_empty() {
                    continue;
                }
                let v = serde_json::from_str(&line)
                    .unwrap_or_else(|e| json!({"ev":"error","text": e.to_string()}));
                if tx.send(v).is_err() {
                    break;
                }
            }
        });
        Ok(Self { child, stdin, rx })
    }

    /// A bridge whose child echoes whatever it is sent, so a test can read
    /// back the ops the TUI emitted. `cat` round-trips one JSON object per
    /// line, which is exactly the wire format.
    #[cfg(test)]
    fn loopback() -> io::Result<Self> {
        let mut child = Command::new("cat")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "loopback stdin"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "loopback stdout"))?;
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if line.trim().is_empty() {
                    continue;
                }
                let v = serde_json::from_str(&line)
                    .unwrap_or_else(|e| json!({"ev":"error","text": e.to_string()}));
                if tx.send(v).is_err() {
                    break;
                }
            }
        });
        Ok(Self { child, stdin, rx })
    }

    fn send(&mut self, msg: &Value) -> io::Result<()> {
        writeln!(self.stdin, "{msg}")?;
        self.stdin.flush()
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// How long a notice stays on the composer's edge. Long enough to read after
/// the keystroke that caused it, short enough that it is never stale chrome.
const NOTICE_TTL: Duration = Duration::from_secs(4);

/// An empty composer should invite a real prompt rather than collapse to a
/// two-line text slot. It can still shrink on short terminals and grows up to
/// half the Story column once the text needs more room.
const COMPOSER_DEFAULT_ROWS: u16 = 8;
/// What the story says once the harness process is gone: once where it died,
/// and again under any prompt typed afterwards. Nothing in this session can
/// run again — the transcript on disk is the harness's, and it stopped where
/// the kernel stopped.
const BRIDGE_GONE: &str = "the harness process is gone. nothing further runs in this session; \
                           quit and start it again to continue from the saved transcript.";

/// Live execute card. Stdout deltas join here; one `push_chunk_to_execute` per frame.
#[derive(Default)]
struct ExecStream {
    id: Option<EntryId>,
    tag: String,
    pending: String,
}

impl ExecStream {
    fn live(&self) -> bool {
        self.id.is_some()
    }

    fn flush(&mut self, calls: &mut ScrollbackState) {
        let Some(id) = self.id else {
            self.pending.clear();
            return;
        };
        if self.pending.is_empty() {
            return;
        }
        calls.push_chunk_to_execute(id, &self.pending);
        self.pending.clear();
    }
}

/// Prompt-cache window for the meter under the calls pane.
///
/// Anthropic `cache_control: {"type":"ephemeral"}` is a 5m TTL (`ttl:"1h"`
/// opts into the hour), and every request that reads the entry refreshes it,
/// so the deadline is `last complete() + ttl`, reset on each POST that touched
/// the cache. `usage.cache_creation.ephemeral_1h_input_tokens` is how the wire
/// says which bucket was written.
#[derive(Default)]
struct CacheMeter {
    at: Option<Instant>,
    ttl: Duration,
    read: u64,
    write: u64,
    fresh: u64,
    out: u64,
    /// Session totals — the wire only ever reports one call at a time, so the
    /// running cost and the real hit count have to be accumulated here.
    calls: u64,
    warm: u64,
    read_total: u64,
    write_total: u64,
    fresh_total: u64,
    out_total: u64,
    spent: f64,
    /// Billing is a subscription, not per token: show list price, not a bill.
    plan: bool,
    /// True when the provider hands out a client-declared ephemeral cache with
    /// a TTL we can count down. Anthropic does; OpenAI's Responses cache is the
    /// endpoint's own, with no window we are told about — so counting one down
    /// invented a deadline, and calling it "cold" five minutes later claimed a
    /// cache had expired when nothing had said so.
    ephemeral: bool,
    /// What the cached reads would have cost at full input price, minus what
    /// they did cost — the money the cache actually saved this session.
    saved: f64,
    area: Rect,
    /// Characters of the last request, split by what actually produced them.
    /// The wire only has three roles, which is too coarse to act on: syscall
    /// output rides the user role, and replayed thinking rides the assistant
    /// role, so both hide inside a slice named for someone else.
    /// Order: system, prompt, tool, thinking, speech.
    roles: [u64; 5],
    /// The same request as an ordered run of chunks -- one per message, or per
    /// block for an assistant turn -- each carrying its length and its kind.
    /// Totals say how much; this says where. A single huge tool result and a
    /// hundred small ones weigh the same in a percentage and look nothing
    /// alike here.
    chunks: Vec<(u64, u8)>,
    /// Context ceiling for the model that answered, so "how full" has a
    /// denominator. Zero until the first call lands.
    window: u64,
}

/// List price per million tokens (input, output). Cache reads bill at 0.1x
/// input, 5m writes at 1.25x, 1h writes at 2x.
/// Context window per model, in tokens. The wire never reports the ceiling,
/// so the bar needs one to divide by.
fn model_window(model: &str) -> u64 {
    match model {
        m if m.starts_with("gpt-") || m.starts_with("o3") || m.starts_with("o4") => 400_000,
        m if m.starts_with("claude-haiku") => 200_000,
        m if m.starts_with("claude-") => 200_000,
        _ => 200_000,
    }
}

/// The rate card, shared with the kernel rather than copied from it. Two
/// hardcoded price lists are two answers to "what did this session spend", and
/// they had already drifted: this table said opus, `desmos/kernel/prices.py`
/// said sonnet for every model. Compiled in, so a malformed table is a build
/// failure and not a wrong invoice.
const PRICE_TABLE_JSON: &str = include_str!("../../../desmos/kernel/prices.json");

fn price_table() -> &'static Value {
    static TABLE: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    TABLE.get_or_init(|| {
        serde_json::from_str(PRICE_TABLE_JSON).expect("desmos/kernel/prices.json is malformed")
    })
}

/// A cache tier's multiplier over list input price.
fn price_multiplier(key: &str) -> f64 {
    price_table()
        .get("multipliers")
        .and_then(|m| m.get(key))
        .and_then(Value::as_f64)
        .unwrap_or(1.0)
}

fn model_price(model: &str) -> (f64, f64) {
    let table = price_table();
    let rate = |entry: &Value| {
        (
            entry.get("input").and_then(Value::as_f64).unwrap_or(0.0),
            entry.get("output").and_then(Value::as_f64).unwrap_or(0.0),
        )
    };
    if let Some(models) = table.get("models").and_then(Value::as_array) {
        for entry in models {
            match entry.get("prefix").and_then(Value::as_str) {
                Some(prefix) if model.starts_with(prefix) => return rate(entry),
                _ => {}
            }
        }
    }
    table.get("default").map(rate).unwrap_or((5.0, 25.0))
}

#[cfg(test)]
mod price_table_tests {
    use super::*;

    /// The shared fixture. `desmos/checks/state.py` prices the same usage
    /// through `desmos.kernel.prices` and asserts this same number, so a rate
    /// edited on one side and not the other fails on the other side.
    const FIXTURE_COST_OPUS: f64 = 0.0023125;

    #[test]
    fn table_drives_the_rates() {
        assert_eq!(model_price("claude-opus-5"), (5.0, 25.0));
        assert_eq!(model_price("gpt-5.6-sol"), (1.25, 10.0));
        // Unknown models bill at the default, never free.
        assert_eq!(model_price("mystery-9"), (5.0, 25.0));
        assert_eq!(price_multiplier("cache_read"), 0.1);
        assert_eq!(price_multiplier("cache_write_5m"), 1.25);
    }

    #[test]
    fn bill_agrees_with_the_kernel_on_the_fixture() {
        let mut meter = CacheMeter::default();
        meter.bill(
            &json!({
                "input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 10,
                "output_tokens": 50
            }),
            "claude-opus-5",
        );
        assert!(
            (meter.spent - FIXTURE_COST_OPUS).abs() < 1e-9,
            "rust billed {} for the shared fixture, kernel says {}",
            meter.spent,
            FIXTURE_COST_OPUS
        );
    }
}

impl CacheMeter {
    /// Split the last request by role. Counts serialized characters, not
    /// tokens: the wire never reports per-role tokens, and the bar only needs
    /// proportions. System blocks and anything unlabelled land in slot 0.
    /// Cache share of this one call's prompt.
    fn hit(&self) -> u64 {
        let total = self.read + self.write + self.fresh;
        if total == 0 {
            0
        } else {
            self.read * 100 / total
        }
    }

    fn observe_roles(&mut self, request: &Value) {
        // Record the run in order; the per-kind totals are summed from it, so
        // the sequence and the percentages can never describe different
        // requests.
        let mut chunks: Vec<(u64, u8)> = Vec::new();

        // OpenAI Responses calls use `instructions` and `input`, not the
        // Anthropic-shaped `system` and `messages`. Without this branch the
        // OpenAI context bar had no role chunks at all.
        if request.get("instructions").is_some() || request.get("input").is_some() {
            if let Some(instructions) = request.get("instructions") {
                chunks.push((instructions.to_string().len() as u64, 0));
            }
            if let Some(items) = request.get("input").and_then(Value::as_array) {
                for item in items {
                    let item_type = item.get("type").and_then(Value::as_str).unwrap_or("");
                    let item_len = item.to_string().len() as u64;
                    let kind = match item_type {
                        "reasoning" => 3,
                        "function_call"
                        | "function_call_output"
                        | "custom_tool_call"
                        | "custom_tool_call_output" => 2,
                        "message" => match item.get("role").and_then(Value::as_str) {
                            Some("user") => {
                                if item.to_string().contains("<result") {
                                    2
                                } else {
                                    1
                                }
                            }
                            Some("assistant") => 4,
                            _ => 0,
                        },
                        _ => 0,
                    };
                    chunks.push((item_len, kind));
                }
            }
            let mut split = [0u64; 5];
            for (len, kind) in &chunks {
                split[*kind as usize] += *len;
            }
            self.roles = split;
            self.chunks = chunks;
            return;
        }

        if let Some(sys) = request.get("system") {
            chunks.push((sys.to_string().len() as u64, 0));
        }
        if let Some(msgs) = request.get("messages").and_then(Value::as_array) {
            for m in msgs {
                let len = m.to_string().len() as u64;
                match m.get("role").and_then(Value::as_str) {
                    // Syscall output is sent as a user turn, but nobody typed
                    // it, and it is the first thing worth trimming.
                    Some("user") => {
                        let tool = m
                            .get("content")
                            .map(|c| match c.as_str() {
                                Some(t) => t.contains("<result"),
                                None => c.to_string().contains("<result"),
                            })
                            .unwrap_or(false);
                        chunks.push((len, u8::from(tool) + 1));
                    }
                    // One chunk per block, not per turn: thinking is replayed
                    // on every call and outweighs the speech beside it.
                    Some("assistant") => match m.get("content").and_then(Value::as_array) {
                        Some(blocks) => {
                            for b in blocks {
                                let bl = b.to_string().len() as u64;
                                let kind = match b.get("type").and_then(Value::as_str) {
                                    Some("thinking" | "redacted_thinking") => 3,
                                    // A syscall call is tool traffic, the same
                                    // as its custom_tool_call twin on the
                                    // Responses branch above -- not speech.
                                    Some("tool_use") => 2,
                                    _ => 4,
                                };
                                chunks.push((bl, kind));
                            }
                        }
                        None => chunks.push((len, 4)),
                    },
                    _ => chunks.push((len, 0)),
                }
            }
        }
        let mut split = [0u64; 5];
        for (len, kind) in &chunks {
            split[*kind as usize] += *len;
        }
        self.roles = split;
        self.chunks = chunks;
    }

    /// Session totals only. A subagent's POST spends real money against a
    /// different transcript, so it bills here without touching the last-call
    /// meters -- feeding those would paint someone else's context and TTL over
    /// the bars that describe this conversation.
    fn bill(&mut self, usage: &Value, model: &str) {
        let n = |k: &str| usage.get(k).and_then(Value::as_u64).unwrap_or(0);
        let read = n("cache_read_input_tokens");
        let write = n("cache_creation_input_tokens");
        let fresh = n("input_tokens");
        let out = n("output_tokens");

        let hour = usage
            .get("cache_creation")
            .and_then(|c| c.get("ephemeral_1h_input_tokens"))
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let write_5m = write.saturating_sub(hour);

        let (in_rate, out_rate) = model_price(model);
        let m = 1_000_000.0;
        let cost = (fresh as f64 * in_rate
            + read as f64 * in_rate * price_multiplier("cache_read")
            + write_5m as f64 * in_rate * price_multiplier("cache_write_5m")
            + hour as f64 * in_rate * price_multiplier("cache_write_1h")
            + out as f64 * out_rate)
            / m;
        // Uncached, those read tokens would have been fresh input.
        let saved = read as f64 * in_rate * (1.0 - price_multiplier("cache_read")) / m;

        self.calls += 1;
        if read > 0 {
            self.warm += 1;
        }
        self.read_total += read;
        self.write_total += write;
        self.fresh_total += fresh;
        self.out_total += out;
        self.spent += cost;
        self.saved += saved;
    }

    fn observe(&mut self, usage: &Value, model: &str) {
        self.bill(usage, model);
        self.window = model_window(model);
        let n = |k: &str| usage.get(k).and_then(Value::as_u64).unwrap_or(0);
        self.read = n("cache_read_input_tokens");
        self.write = n("cache_creation_input_tokens");
        self.fresh = n("input_tokens");
        self.out = n("output_tokens");

        let hour = usage
            .get("cache_creation")
            .and_then(|c| c.get("ephemeral_1h_input_tokens"))
            .and_then(Value::as_u64)
            .unwrap_or(0);

        if self.read == 0 && self.write == 0 {
            return;
        }
        self.ttl = if hour > 0 {
            Duration::from_secs(3600)
        } else {
            Duration::from_secs(300)
        };
        self.at = Some(Instant::now());
    }

    /// Fraction of the TTL still on the clock; `None` once it has expired.
    fn left(&self) -> Option<f32> {
        let at = self.at?;
        let ttl = self.ttl.as_secs_f32();
        if ttl <= 0.0 {
            return None;
        }
        let left = 1.0 - at.elapsed().as_secs_f32() / ttl;
        (left > 0.0).then_some(left)
    }
}

/// desmos launches on Oscura Midnight.
///
/// grok's `resolve_initial_theme` walks `GROK_THEME`, then the grok config's
/// `[ui].theme`, and falls back to GrokNight -- which is grok's default, not
/// ours. This keeps that whole precedence chain and replaces only the tail:
/// anything the environment or the config names still wins, including an
/// explicit `groknight`, and `auto` still resolves by system appearance. The
/// only case we decide is the one nobody decided.
fn initial_theme() -> ThemeKind {
    let env = ["GROK_THEME", "LC_GROK_THEME"]
        .into_iter()
        .filter_map(|key| std::env::var(key).ok())
        .find(|raw| ThemeKind::from_name(raw).is_some());
    initial_theme_from(env.as_deref(), config_theme())
}

fn initial_theme_from(env: Option<&str>, config: Option<ThemeKind>) -> ThemeKind {
    match env.and_then(ThemeKind::from_name).or(config) {
        // Auto is a question, not an answer; grok knows how to ask the terminal.
        Some(kind) if kind.is_auto() => theme_cache::resolve_initial_theme(),
        Some(kind) => kind,
        None => ThemeKind::OscuraMidnight,
    }
}

fn config_theme() -> Option<ThemeKind> {
    let root = xai_grok_config::load_effective_config_disk_only().ok()?;
    let table = root.as_table()?;
    table
        .get("ui")
        .and_then(|ui| ui.get("theme"))
        .or_else(|| table.get("theme"))
        .and_then(toml::Value::as_str)
        .and_then(ThemeKind::from_name)
}

fn grok_appearance() -> AppearanceConfig {
    let mut cfg = std::fs::read_to_string(util::pager_toml_path())
        .ok()
        .and_then(|s| toml::from_str::<RawAppearanceConfig>(&s).ok())
        .map(AppearanceConfig::from)
        .unwrap_or_default();
    // The pager's Label style writes `Run <desc>` above a `$ <command>` line,
    // so the command is announced twice and the tool name arrives third. Shell
    // style drops the verb: the description stands as the title, the command
    // follows on its own line, the output after that.
    cfg.scrollback.blocks.execute.header_style = appearance::ExecuteHeaderStyle::Shell;
    // A streaming thought spent three rows on chrome before a word of reasoning:
    // the header, a blank separator the header always drags with it, and the
    // ellipsis row. The header is the one I can drop, and it is the one that was
    // never earning its rows here -- the turn-status row already says Thinking
    // with a spinner and the elapsed time. Collapsed mode keeps its header
    // regardless of this flag, so a finished thought still reads "Thought for
    // 12s".
    cfg.scrollback.blocks.thinking.header = false;
    // Two card kinds, one column grid. An execute card draws the `┃` rail and
    // puts its body at column 2; an edit card defaulted to no rail at all and a
    // diff pushed two further columns right by its own indent. Side by side in
    // the same pane that read as half the cards being tabbed. Give the edit the
    // finished-command rail and drop its extra indent so both bodies start in
    // the same place.
    cfg.scrollback.blocks.edit.accent = Some(Theme::current().accent_success);
    cfg.scrollback.blocks.edit.indent = false;
    cfg.show_timestamps = appearance_cache::load_timestamps();
    cfg.show_timeline = appearance_cache::load_show_timeline();
    // Density. Grok's defaults are tuned for one full-width chat column; this
    // is two columns plus four stacked side panes, so every default pad is
    // paid several times over. Compact is the default here, not a small-screen
    // fallback: it zeroes the outer vertical pad and clamps the horizontal pad
    // to one cell. A pager.toml that asks for compact cannot un-ask.
    cfg.prompt.compact = true;
    // `/dense` used to write a setting nothing read: every pad below was a
    // constant, so the toggle changed a stored bool and not one cell of the
    // screen. These are the knobs that actually cost rows and columns.
    let dense = appearance_cache::load();
    // Two cells of pad on each side of every block, inside a pane that is a
    // third of the screen. One cell still separates content from the accent
    // column, which is all the pad was doing -- and dense drops even that.
    let pad = if dense { 0 } else { 1 };
    cfg.scrollback.layout.block_pad_left = pad;
    cfg.scrollback.layout.block_pad_right = pad;
    cfg.scrollback.layout.outer_vpad = 0;
    cfg.scrollback.layout.outer_hpad_left = pad;
    cfg.scrollback.layout.outer_hpad_right = pad;
    // A blank row above and below every user prompt. The prompt already has an
    // accent column and a background tint; it does not also need two rows of
    // air, and it is the most frequent block in the story.
    cfg.scrollback.blocks.prompt.vpad = false;
    // An edit card's blank rows are the other per-block pair, and they were
    // never turned off at all.
    cfg.scrollback.blocks.edit.vpad = !dense;
    // A timeline gutter is a column on every row. Dense gives it back.
    if dense {
        cfg.show_timeline = false;
    }
    // The turn-status row lives in a one-row band of its own, immediately above
    // the composer border. Its gap row would come out of the story.
    cfg.turn_status.gap = false;
    cfg
}

fn parse_args() -> (String, String, bool) {
    let python = std::env::var("DESMOS_PYTHON").unwrap_or_else(|_| "python3".into());
    let cwd = std::env::current_dir()
        .map(|p| p.display().to_string())
        .unwrap_or_else(|_| ".".into());
    let mut python = python;
    let mut cwd = cwd;
    let mut demo = false;
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--python" if i + 1 < args.len() => {
                python = args[i + 1].clone();
                i += 2;
            }
            "--cwd" if i + 1 < args.len() => {
                cwd = args[i + 1].clone();
                i += 2;
            }
            "--demo" => {
                demo = true;
                i += 1;
            }
            "--version" | "-V" => {
                println!(
                    "desmos-tui {} ({})",
                    env!("CARGO_PKG_VERSION"),
                    env!("DESMOS_PROFILE")
                );
                std::process::exit(0);
            }
            _ => i += 1,
        }
    }
    (python, cwd, demo)
}

fn seed_demo(app: &mut App) {
    app.model = "claude-opus-5".into();
    app.thinking = "low".into();
    app.generation = "4".into();
    app.status = "demo".into();
    app.ready = true;
    app.story_push(RenderBlock::user_prompt(
        "look around the kernel\nand list what you can grow",
    ));
    app.call_push(RenderBlock::thinking(
        "cwd is mine. peek ns, then fire the smallest probe.",
    ));
    app.call_push(RenderBlock::thinking(
        "redacted thinking — opaque block, replayed on the next complete(), not speech.",
    ));
    app.story_push(RenderBlock::agent_message(
        "ns has **CWD**. Frozen: `python` `bash` `edit` `register`.\n\n\
         - grown: usage\n- grown: traj\n- grown: agents\n\n\
         ```python\nprint(sorted(k for k in world.ns if not k.startswith('_')))\n```\n\n\
         $E = mc^2$ is not a syscall.",
    ));
    app.story_push(RenderBlock::user_prompt("ok check cache"));
    app.story_push(RenderBlock::agent_message(
        "## cache\n\nhit rate **87%** on the last `complete()`. 3 calls this step.\n\n\
         | call | hit |\n| ---: | ---: |\n| 1 | 0% |\n| 2 | 99% |\n| 3 | 99% |\n\n\
         PARAONE first paragraph stays its own block.\n\n\
         PARATWO second paragraph after a blank line.\n\n\
         WRAPSTART The kernel keeps names under ns and never dumps the heap \
         into chat. Speech is not memory. If future-you needs a fact it has \
         to live in a note, a skill, or a named object the index still lists. \
         A paragraph this long must wrap across more than one row. WRAPEND",
    ));
    // One edit in Activity; Story carries only the reader-facing conversation.
    let demo_edit = json!({
        "ev": "result",
        "tag": "edit",
        "attrs": {"path": "desmos/loop.py"},
        "body": "    if n < max_turns:\n---\n    if n <= max_turns:",
        "text": "ok",
        "line": 212,
    });
    app.call_push_group(PostArgs::new(
        "user",
        1,
        "claude-opus-5",
        "low",
        &json!({"input_tokens": 1200, "output_tokens": 380}),
        1,
        1,
    ));
    app.call_push(result_block(&demo_edit));
    app.call_push(wire_syscall(
        "python",
        "sorted(k for k in world.ns if not k.startswith('_'))",
        &json!({}),
        "['CWD']",
        None,
    ));
    app.call_push(wire_syscall(
        "bash",
        "ls .desmos/generations",
        &json!({}),
        "0001.json 0002.json 0003.json 0004.json",
        None,
    ));
    app.call_push_group(PostArgs::new(
        "llm",
        2,
        "claude-opus-5",
        "low",
        &json!({
            "input_tokens": 80,
            "cache_read_input_tokens": 11400,
            "output_tokens": 120
        }),
        1,
        0,
    ));
    app.call_push_group(PostArgs::new(
        "user",
        3,
        "claude-opus-5",
        "low",
        &json!({
            "input_tokens": 90,
            "cache_read_input_tokens": 12100,
            "output_tokens": 60
        }),
        0,
        0,
    ));
    app.call_push(wire_syscall(
        "usage",
        "",
        &json!({}),
        "3 calls  cache hit 87%  est $0.04",
        None,
    ));
    app.set_last_post(
        3,
        &json!({
            "model": "claude-opus-5",
            "max_tokens": 8192,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "system": [{"type": "text", "text": "ABI frozen. Speak markdown."}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "ok check cache"}]}]
        }),
        &json!({
            "content": [
                {"type": "thinking", "thinking": "cache is last-user"},
                {"type": "redacted_thinking", "data": "[redacted]"},
                {"type": "text", "text": "hit rate 87%"}
            ],
            "usage": {"input_tokens": 90, "cache_read_input_tokens": 12100, "output_tokens": 60}
        }),
    );
    seed_spawn(app);
    app.queue.push("then list what grew".into());
    app.queue
        .push("then show cache hit\nacross two lines".into());
}

fn seed_spawn(app: &mut App) {
    // grok background spawn: Started stays on the parent, a Completed row
    // is appended when the child settles. Enter opens the child session.
    let id = "a1b2c3d4";
    let task = "scan notes for cache doctrine";
    let block = SubagentBlock::started(
        task,
        id,
        "explore",
        Some("researcher".into()),
        None,
        Some("claude-opus-5".into()),
        true,
    );
    let eid = app.sess.story.push_block(RenderBlock::Subagent(block));
    app.sess.story.set_last_running(true);
    {
        let shown = app.show_posts;
        let child = app.ensure_child(id, task);
        child.parent_entry = Some(eid);
        child.sess.story.push_block(RenderBlock::thinking(
            "read notes first, then say what cache actually is.",
        ));
        child.sess.story.push_block(RenderBlock::agent_message(
            "cache is last-user only. ABI is frozen. Speech is not memory.",
        ));
        let args = PostArgs::new(
            "user",
            1,
            "claude-opus-5",
            "low",
            &json!({"input_tokens": 400, "output_tokens": 80}),
            1,
            0,
        );
        child.sess.posts.push(&mut child.sess.calls, args, shown);
        wire_push(
            &mut child.sess.calls,
            wire_syscall("python", "list(world.notes)", &json!({}), "['cache']", None),
        );
    }
    app.sess.story.finish_running(eid);
    app.sess
        .story
        .push_block(RenderBlock::Subagent(SubagentBlock::completed(
        task,
        id,
        Duration::from_secs(4),
    )));
}

fn main() -> io::Result<()> {
    let (python, cwd, demo) = parse_args();
    let mut bridge = if demo {
        None
    } else {
        Some(Bridge::spawn(&python, &cwd)?)
    };
    let mut app = App::new();
    if !demo {
        app.session_picker = session::SessionPicker::discover(&PathBuf::from(&cwd));
    }
    if demo {
        app.demo = true;
        seed_demo(&mut app);
    }
    wait_ready(bridge.as_mut(), &mut app)?;

    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(
        stdout,
        EnterAlternateScreen,
        EnableMouseCapture,
        EnableBracketedPaste,
        EnableBlinking,
        SetCursorStyle::BlinkingBar
    )?;
    let mut terminal = Terminal::new(CrosstermBackend::new(stdout))?;
    let result = run(&mut terminal, bridge.as_mut(), &mut app);
    disable_raw_mode()?;
    execute!(
        io::stdout(),
        SetCursorStyle::DefaultUserShape,
        DisableBracketedPaste,
        DisableMouseCapture,
        LeaveAlternateScreen
    )?;
    result
}

fn resolved_theme(name: &str) -> Option<ThemeKind> {
    ThemeKind::from_name(name).map(|kind| {
        if kind.is_auto() {
            theme_cache::resolve_initial_theme()
        } else {
            kind
        }
    })
}

/// Keep theme completion transactional: selection paints immediately, while
/// leaving the list without accepting restores the theme active on entry.
fn sync_theme_preview(app: &mut App) {
    let selected = app
        .slash
        .is_theme_values()
        .then(|| app.slash.selected_text())
        .flatten()
        .and_then(resolved_theme);
    if let Some(kind) = selected {
        if app.theme_preview_origin.is_none() {
            app.theme_preview_origin = Some(Theme::current_kind());
        }
        if Theme::current_kind() != kind {
            theme_cache::set(kind);
            app.apply_grok_settings();
        }
    } else if let Some(origin) = app.theme_preview_origin.take() {
        theme_cache::set(origin);
        app.apply_grok_settings();
    }
}

fn update_slash(app: &mut App) {
    if app.slash_paste_guard {
        app.slash.close();
    } else {
        app.slash.update(&app.prompt.to_send(), &app.picker);
    }
    sync_theme_preview(app);
}

fn run(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    mut bridge: Option<&mut Bridge>,
    app: &mut App,
) -> io::Result<()> {
    // Redraw only when something changed. Ratatui's draw() sends Show+MoveTo
    // every frame, which resets the terminal blink timer so the caret looks
    // solid. Grok's draw_frame skips cursor commands when idle for the same
    // reason.
    //
    // Never drain the whole SSE burst before painting. Each thinking/speech
    // token used to call grok push_chunk (syntect) on the UI thread. Cap the
    // drain, flush buffers once, then draw.
    //
    // Grok's wave / braille spinner is `ScrollbackState::tick()` at ~30fps
    // when a running entry is in view. `tick_running()` only invalidates
    // caches — it does not advance the frame.
    const DRAIN: usize = 32;
    const ANIM: Duration = Duration::from_millis(33);
    let mut dirty = true;
    let mut last_anim = Instant::now();
    let mut last_cache = Instant::now();
    loop {
        let mut more = false;
        let mut died = false;
        if let Some(b) = bridge.as_mut() {
            let mut n = 0;
            loop {
                match b.rx.try_recv() {
                    Ok(ev) => {
                        handle_event(app, ev);
                        dirty = true;
                        n += 1;
                        if n >= DRAIN {
                            more = true;
                            break;
                        }
                    }
                    Err(TryRecvError::Empty) => break,
                    Err(TryRecvError::Disconnected) => {
                        // try_recv on a closed, drained channel returns
                        // Disconnected forever. Without this break the drain
                        // loop spun at 100% and never reached draw or
                        // event::poll, so a crashed child froze the frame with
                        // Ctrl+C unreadable.
                        // A notice lapses after four seconds; the harness
                        // being gone does not. The story keeps the row.
                        app.story_push(RenderBlock::system(BRIDGE_GONE));
                        app.notify("bridge died");
                        app.bridge_gone = true;
                        app.running = false;
                        app.turn_started = None;
                        app.ready = true;
                        dirty = true;
                        died = true;
                        break;
                    }
                }
            }
        }
        if died {
            // Drop the dead handle, or the next pass re-notifies every 80ms.
            bridge = None;
        }

        if app.drain_after && !app.running {
            app.drain_after = false;
            try_drain(bridge.as_deref_mut(), app)?;
            dirty = true;
        }

        let live = streaming(app) || app.running || app.sess.story.has_running_entries();
        if live && last_anim.elapsed() >= ANIM {
            if tick_scrollbacks(app) || app.running {
                dirty = true;
            }
            last_anim = Instant::now();
        }
        // Polled whether or not the pane is on screen, because the work row's
        // git tail reads the same snapshot — but only while there is a reader
        // for it. A collapsed pane over an idle session wants no timer at all:
        // three `git` subprocesses every four seconds, forever, on a tree
        // where a status call is a quarter second of disk.
        if app.layout.git_h > 0 || app.running || app.sess.stream.run.settled.is_some() {
            app.git.poll(false);
        }
        if app.file_picker.poll() {
            dirty = true;
        }
        if app.git.drain() {
            // The row a fold left owing takes its tail from this read if this
            // is the read it was waiting on; the live run gets its next sync
            // from it either way.
            app.sess.stream.run.settle(&mut app.sess.story, &app.git);
            app.sess.stream.run.note_repo(&app.git);
            dirty = true;
        }
        if expire_notice(app) {
            dirty = true;
        }
        // Cache TTL burns down while nothing else is live; one repaint a
        // second is enough for the bar, and there is nothing else asking for a
        // frame once the git timer above has gone quiet.
        let cache_live = app.cache.left().is_some();
        if cache_live && last_cache.elapsed() >= Duration::from_secs(1) {
            dirty = true;
            last_cache = Instant::now();
        }

        if dirty {
            app.media.frame.clear();
            terminal.draw(|f| draw(f, app))?;
            flush_media(app, &mut io::stdout())?;
            dirty = false;
        }

        let wait = if more {
            Duration::ZERO
        } else if live {
            ANIM
        } else {
            Duration::from_millis(80)
        };
        if event::poll(wait)? {
            let mut evs = vec![event::read()?];
            while event::poll(Duration::ZERO)? {
                evs.push(event::read()?);
            }
            for ev in coalesce_events(evs) {
                match ev {
                    Event::Paste(text) => {
                        if let Some(viewer) = app.viewer.as_mut() {
                            viewer.handle_paste(&text);
                        } else {
                            apply_paste(app, &text, false);
                        }
                        dirty = true;
                    }
                    Event::Key(key)
                        if key.kind == KeyEventKind::Press || key.kind == KeyEventKind::Repeat =>
                    {
                        if handle_key(bridge.as_deref_mut(), app, key)? {
                            return Ok(());
                        }
                        dirty = true;
                    }
                    Event::Mouse(m) => {
                        handle_mouse(app, m);
                        if app.want_stop {
                            app.want_stop = false;
                            if on_ctrl_c(bridge.as_deref_mut(), app)? {
                                return Ok(());
                            }
                        }
                        dirty = true;
                    }
                    Event::Resize(_, _) => dirty = true,
                    _ => {}
                }
            }
        }
    }
}

/// Apply the bridge `ready` snapshot before the first chrome paint.
/// Demo (no bridge) already has model/gen; a silent bridge still paints
/// after 2s so the user is not stuck on a blank alternate screen.
fn wait_ready(bridge: Option<&mut Bridge>, app: &mut App) -> io::Result<()> {
    if app.ready {
        return Ok(());
    }
    let Some(b) = bridge else {
        app.ready = true;
        return Ok(());
    };
    let deadline = Instant::now() + Duration::from_secs(2);
    while !app.ready {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            app.notify("bridge silent");
            app.ready = true;
            break;
        }
        match b.rx.recv_timeout(left) {
            Ok(ev) => handle_event(app, ev),
            Err(RecvTimeoutError::Timeout) => {
                app.notify("bridge silent");
                app.ready = true;
            }
            Err(RecvTimeoutError::Disconnected) => {
                return Err(io::Error::new(
                    io::ErrorKind::BrokenPipe,
                    "harness process exited before ready",
                ));
            }
        }
    }
    while let Ok(ev) = b.rx.try_recv() {
        handle_event(app, ev);
    }
    Ok(())
}

/// Ctrl+C: stop the in-flight step and persist; quit only when idle.
/// A second Ctrl+C while stopping force-quits.
fn on_ctrl_c(bridge: Option<&mut Bridge>, app: &mut App) -> io::Result<bool> {
    if !app.running {
        if let Some(b) = bridge {
            let _ = b.send(&json!({"op": "quit"}));
        }
        return Ok(true);
    }
    if app.status == "stopping" {
        if let Some(b) = bridge {
            let _ = b.send(&json!({"op": "quit"}));
        }
        return Ok(true);
    }
    if let Some(b) = bridge {
        b.send(&json!({"op": "stop"}))?;
        app.status = "stopping".into();
        return Ok(false);
    }
    app.running = false;
    app.turn_started = None;
    app.status = "idle".into();
    Ok(true)
}

/// Turn a picker decision into a bridge op. The picker never sends anything
/// itself; this is the only place a choice becomes a request.
fn apply_session_choice(
    mut bridge: Option<&mut Bridge>,
    app: &mut App,
    choice: session::Choice,
) -> io::Result<()> {
    app.sess.story = ScrollbackState::new();
    app.sess.calls = ScrollbackState::new();
    app.apply_grok_settings();
    match choice {
        session::Choice::New => {
            // The bridge owns persistence; reset through its public operation
            // instead of moving the SQLite database out from under it.
            if let Some(b) = bridge.as_mut() {
                b.send(&json!({"op": "session"}))?;
            }
            app.notify("new session");
        }
        session::Choice::Resume(id) => {
            // The bridge is older than the choice, so the session is named to
            // it now rather than read from the environment it launched with.
            if let Some(b) = bridge.as_mut() {
                b.send(&json!({"op": "session", "id": id}))?;
            }
            let turns = app.session_picker.resumed_turns().to_vec();
            for turn in turns {
                app.story_push(RenderBlock::user_prompt(turn.prompt));
                if !turn.speech.trim().is_empty() {
                    app.story_push(RenderBlock::agent_message(turn.speech));
                }
            }
            app.notify("transcript resumed");
        }
    }
    Ok(())
}

fn apply_picker(
    mut bridge: Option<&mut Bridge>,
    app: &mut App,
    action: picker::PickerAction,
) -> io::Result<bool> {
    match action {
        picker::PickerAction::None | picker::PickerAction::Close => {}
        picker::PickerAction::Login { .. } => {
            if let Some(b) = bridge.as_mut() {
                b.send(&json!({"op": "login", "method": "auto"}))?;
            }
        }
        picker::PickerAction::Apply { model, effort } => {
            if let Some(b) = bridge.as_mut() {
                b.send(&json!({"op": "model", "model": model, "effort": effort}))?;
            }
            // Idle, the bridge picks this up immediately, so showing it now is
            // honest. Mid-step the op waits in the inbox behind run_turns —
            // claiming the new model there would be a lie for the rest of the
            // step, so it stays pending until the bridge says otherwise.
            if app.running {
                app.notify(format!("{model} after this step"));
                app.model_pending = Some((model, effort));
            } else {
                app.model = model;
                app.thinking = effort;
            }
        }
    }
    Ok(false)
}

fn send_now(mut bridge: Option<&mut Bridge>, app: &mut App) -> io::Result<bool> {
    if app.queue.is_empty() {
        return Ok(false);
    }
    if let Some(idx) = app.queue.selected {
        app.queue.rotate_to_front(idx);
    }
    if !app.running {
        return try_drain(bridge, app).map(|_| false);
    }
    app.send_now = true;
    app.notify("send now");
    if let Some(b) = bridge.as_mut() {
        b.send(&json!({"op": "stop"}))?;
        app.status = "stopping".into();
    } else {
        app.running = false;
        try_drain(None, app)?;
    }
    Ok(false)
}

fn try_drain(bridge: Option<&mut Bridge>, app: &mut App) -> io::Result<()> {
    if app.running || app.queue.is_empty() {
        app.send_now = false;
        return Ok(());
    }
    let Some(item) = app.queue.pop_front() else {
        return Ok(());
    };
    app.send_now = false;
    start_step(bridge, app, item.text, item.images)
}

fn start_step(
    mut bridge: Option<&mut Bridge>,
    app: &mut App,
    line: String,
    images: Vec<String>,
) -> io::Result<()> {
    app.story_push(RenderBlock::user_prompt(&line));
    app.sess.story.follow_new_turn(None, false);
    // The attachments ride under the prompt that carries them: one row per
    // image, with the picture itself where the terminal can draw one.
    for path in &images {
        if let Some(block) = media_block(path) {
            app.story_push(block);
        }
    }
    app.sess.story.follow_new_turn(None, false);
    if app.viewing.is_some() {
        app.notify("esc to leave session");
        return Ok(());
    }
    match bridge.as_mut() {
        Some(b) => {
            app.running = true;
            app.turn_started = Some(Instant::now());
            app.status = "running".into();
            b.send(&json!({"op": "step", "text": line, "images": images}))?;
        }
        // --demo drives the same pane with canned events; its POST card is
        // what a step looks like there.
        None if app.demo => {
            app.call_push_group(PostArgs::new("user", 0, "demo", "", &json!({}), 0, 0));
        }
        // A bridge that died and a prompt typed after it: say so where the
        // prompt is, durably — a POST card here would be a lookalike for a
        // step that cannot happen, and a notice lapses in four seconds.
        None if app.bridge_gone => {
            app.story_push(RenderBlock::system(BRIDGE_GONE));
            app.notify("bridge is gone");
        }
        // Never attached one. Nothing died, so nothing is announced.
        None => {}
    }
    Ok(())
}

fn submit_prompt(mut bridge: Option<&mut Bridge>, app: &mut App) -> io::Result<bool> {
    let images = app.prompt.images();
    // An image with no words is a real prompt -- "look at this" is the whole
    // message. The bridge rejects an empty one, so name what was attached
    // rather than inventing a sentence the user did not write.
    let line = match (app.prompt.to_send(), images.is_empty()) {
        (t, _) if !t.trim().is_empty() => t,
        (_, false) => image_prompt_text(&images),
        (t, true) => t,
    };
    if line.trim().is_empty() {
        // Emptying a row you lifted out of the queue is how you delete it. The
        // row is already gone; say so, and do not fall through to send-now,
        // which would fire some other row you never selected.
        if let Some(idx) = app.queue_edit.take() {
            app.notify(format!("dropped #{}", idx + 1));
            return Ok(false);
        }
        if app.running || !app.queue.is_empty() {
            return send_now(bridge, app);
        }
        return Ok(false);
    }
    app.prompt.clear();
    let slash_allowed = !app.slash_paste_guard;
    app.slash_paste_guard = false;
    let slot = app.queue_edit.take();
    if slash_allowed && (line == "/quit" || line == "/exit") {
        return Ok(true);
    }
    if slash_allowed && is_local_slash(&line) {
        // No story row. A local command never reaches the model -- it is not
        // in world.messages and no turn runs -- so pushing a UserPrompt block
        // put a turn in the transcript that never happened. It was the only
        // acknowledgement back when nothing drew app.status; the notice on the
        // composer's edge is that acknowledgement now, and it lapses, which is
        // the right lifetime for "theme changed".
    if let Some(name) = line.strip_prefix("/theme") {
        let name = name.trim();
        if name.is_empty() {
            app.notify(format!("theme {}", Theme::current_kind().display_name()));
        } else if let Some(kind) = ThemeKind::from_name(name) {
            let kind = if kind.is_auto() {
                theme_cache::resolve_initial_theme()
            } else {
                kind
            };
            theme_cache::set(kind);
            app.apply_grok_settings();
            app.notify(format!("theme {}", kind.display_name()));
        } else {
            app.notify("theme: groknight tokyonight grokday rosepine oscura auto");
        }
    } else if line == "/timestamps" {
        let on = !appearance_cache::load_timestamps();
        appearance_cache::set_timestamps(on);
        app.apply_grok_settings();
            app.notify(if on {
                "timestamps on"
            } else {
                "timestamps off"
            });
    } else if line == "/compact" || line == "/dense" {
        // `/compact` reads like "fold the transcript" and does not — folding is
        // the server's, on its own trigger, and there is no client verb for it.
        // This only changes row spacing. `/dense` says that; the old name stays
        // so muscle memory does not hit an unknown command.
        let on = !appearance_cache::load();
        appearance_cache::set(on);
        app.apply_grok_settings();
        app.notify(if on {
            "dense rows on (this is spacing, not transcript folding)"
        } else {
            "dense rows off"
        });
    } else if let Some(rest) = line.strip_prefix("/model") {
        // The bridge op has always existed; nothing typed to it. The picker is
        // still the discoverable path -- this is for people who already know
        // what they want.
        let want = rest.trim();
        if want.is_empty() {
            let (m, e) = (app.model.clone(), app.thinking.clone());
            app.picker.open_for_change(&m, &e);
        } else if let Some(b) = bridge.as_mut() {
            b.send(&json!({"op": "model", "model": want, "effort": app.thinking}))?;
            app.notify(format!("model {want}"));
        }
    } else if let Some(level) = line.strip_prefix("/thinking") {
        let level = level.trim();
        if !level.is_empty() {
            if let Some(b) = bridge.as_mut() {
                b.send(&json!({"op":"thinking","level": level}))?;
            }
            app.thinking = level.into();
            app.notify(format!("thinking {level}"));
        }
    } else if line == "/reset" {
        if let Some(b) = bridge.as_mut() {
            b.send(&json!({"op":"reset"}))?;
        }
        app.sess.story.clear();
        app.sess.calls.clear();
        app.sess.posts.clear();
        app.sess.wire_manual.clear();
        app.post_in.clear();
        app.post_out.clear();
        app.post_req = json!({});
        app.post_resp = json!({});
        app.post_inspect = None;
        app.post_n = 0;
        app.post_out_n = 0;
        app.queue.clear();
        app.queue_edit = None;
        app.send_now = false;
        app.notify("transcript cleared");
    } else if line == "/reload" {
        if let Some(b) = bridge.as_mut() {
            b.send(&json!({"op":"reload"}))?;
        }
        app.notify("reloading skills and extensions");
    }
        return Ok(false);
    }
    if app.running {
        let pos = match slot {
            Some(idx) => {
                app.queue.insert_at_with(idx, line, images);
                idx + 1
            }
            None => {
                app.queue.push_with(line, images);
                app.queue.len()
            }
        };
        // Tell the loop something was typed. A queued follow-up outranks
        // background work, but run_turns can only see it through the bridge's
        // inbox -- the queue lives here, not there. With nothing sent,
        // pending.wait_next blocked until every task landed, so any turn that
        // left a shell monitor running parked the composer in "queued" and the
        // follow-up never fired. The op itself does nothing; being in the
        // inbox is the whole signal.
        if let Some(b) = bridge.as_mut() {
            b.send(&json!({"op": "typed"}))?;
        }
        app.notify(format!("queued #{pos}"));
        return Ok(false);
    }
    start_step(bridge, app, line, images)?;
    Ok(false)
}

fn viewer_for_entry(entry: &ScrollbackEntry) -> Option<BlockViewerPane> {
    match &entry.block {
        RenderBlock::Thinking(_) | RenderBlock::AgentMessage(_) => {
            BlockViewerPane::for_markdown(entry.id, entry)
        }
        RenderBlock::ToolCall(ToolCallBlock::Execute(_)) => {
            BlockViewerPane::for_execute(entry.id, entry)
        }
        RenderBlock::ToolCall(ToolCallBlock::Edit(_)) => BlockViewerPane::for_edit(entry.id, entry),
        RenderBlock::ToolCall(ToolCallBlock::Other(other)) => {
            let title = other.name.clone();
            let body = other
                .output
                .clone()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| other.summary.clone());
            Some(BlockViewerPane::for_plain_text(&title, &body))
        }
        RenderBlock::UserPrompt(p) => Some(BlockViewerPane::for_plain_text("you", &p.text.clone())),
        RenderBlock::System(s) => Some(BlockViewerPane::for_plain_text("system", &s.text.clone())),
        RenderBlock::Subagent(_) => None,
        _ => None,
    }
}

fn draw_viewer(f: &mut Frame, app: &mut App) {
    let theme = Theme::current();
    let kind = app.viewer.as_ref().map(|v| v.kind);
    let id = app.viewer.as_ref().map(|v| v.entry_id);
    let dummy = ScrollbackEntry::new(RenderBlock::system(String::new()));
    let owned = if kind == Some(ViewerKind::PlainText) {
        None
    } else if let Some(id) = id {
        match app.viewer_scroll().get_by_id(id).cloned() {
            Some(entry) => Some(entry),
            None => {
                app.viewer = None;
                return;
            }
        }
    } else {
        app.viewer = None;
        return;
    };
    let entry = owned.as_ref().unwrap_or(&dummy);
    if let Some(viewer) = app.viewer.as_mut() {
        if kind != Some(ViewerKind::PlainText) {
            let _ = viewer.tick(entry);
        }
        let footer = [
            Shortcut {
                label: "esc close",
                clickable: false,
                id: 0,
            },
            Shortcut {
                label: "/ search",
                clickable: false,
                id: 1,
            },
            Shortcut {
                label: "w wrap",
                clickable: false,
                id: 2,
            },
            Shortcut {
                label: "r raw",
                clickable: false,
                id: 3,
            },
        ];
        let title = match viewer.kind {
            ViewerKind::Markdown => match &entry.block {
                RenderBlock::Thinking(_) => "thought",
                _ => "speech",
            },
            ViewerKind::Execute => "execute",
            ViewerKind::Edit => "edit",
            ViewerKind::PlainText => "view",
            _ => "view",
        };
        let config = ModalWindowConfig {
            title,
            tabs: None,
            shortcuts: &footer,
            sizing: ModalSizing {
                width_pct: 0.95,
                max_width: 400,
                min_width: 40,
                v_margin: 1,
                h_pad: 2,
                v_pad: 1,
                footer_lines: 2,
            },
            fold_info: None,
        };
        let area = f.area();
        let buf = f.buffer_mut();
        let Some(content) = render_modal_window(buf, area, &mut viewer.modal, &config, &theme)
        else {
            return;
        };
        viewer.render_content(content.content, buf, entry, true, &[]);
        viewer.render_text_drag_overlay(buf);
    }
}

fn post_inspect_chrome<'a>(n: u64, footer: &'a [Shortcut<'a>]) -> (String, ModalWindowConfig<'a>) {
    let title = if n == 0 {
        "POST".to_string()
    } else {
        format!("POST #{n}")
    };
    let config = ModalWindowConfig {
        title: "",
        tabs: Some(&["in", "out"]),
        shortcuts: footer,
        sizing: ModalSizing {
            width_pct: 0.72,
            max_width: 140,
            min_width: 56,
            v_margin: 4,
            h_pad: 2,
            v_pad: 1,
            footer_lines: 2,
        },
        fold_info: None,
    };
    (title, config)
}

fn post_inspect_footer() -> [Shortcut<'static>; 4] {
    [
        Shortcut {
            label: "esc close",
            clickable: false,
            id: 0,
        },
        Shortcut {
            label: "r raw",
            clickable: false,
            id: 1,
        },
        Shortcut {
            label: "[ ] tab",
            clickable: false,
            id: 2,
        },
        Shortcut {
            label: "y copy",
            clickable: false,
            id: 3,
        },
    ]
}

fn draw_post_inspect(f: &mut Frame, app: &mut App) {
    let theme = Theme::current();
    let n = app.post_n;
    let footer = post_inspect_footer();
    let (title, mut config) = post_inspect_chrome(n, &footer);
    let title_owned = title;
    config.title = &title_owned;
    let area = f.area();
    let (raw, tab, inner) = {
        let Some(inspect) = app.post_inspect.as_mut() else {
            return;
        };
        let buf = f.buffer_mut();
        let Some(content) = render_modal_window(buf, area, &mut inspect.modal, &config, &theme)
        else {
            return;
        };
        inspect.content = content.content;
        let raw = inspect.raw;
        let tab = inspect.modal.active_tab;
        let inner = content.content;
        if raw {
            let dummy = ScrollbackEntry::new(RenderBlock::system(String::new()));
            if let Some(viewer) = inspect.raw_viewer.as_mut() {
                viewer.render_content(inner, buf, &dummy, true, &[]);
                viewer.render_text_drag_overlay(buf);
            }
            return;
        }
        (raw, tab, inner)
    };
    let _ = raw;
    let tree = if tab == 0 {
        &mut app.post_in
    } else {
        &mut app.post_out
    };
    let lines = tree.lines(inner.width, inner.height, true);
    f.render_widget(Paragraph::new(lines), inner);
}

fn clamp_scroll(sb: &mut ScrollbackState) {
    if sb.is_follow_mode() {
        return;
    }
    let (off, vp, total) = sb.scroll_info();
    if vp == 0 {
        return;
    }
    let max = total.saturating_sub(vp as usize);
    if off > max {
        sb.set_scroll_offset(max);
    }
}

fn hovered_entry(model: &ResolvedSelectionModel, mouse: Option<(u16, u16)>) -> Option<usize> {
    let (col, row) = mouse?;
    model.hit_test_visible_block(col, row).map(|g| g.entry_idx)
}

fn draw_scrollback(
    f: &mut Frame,
    area: Rect,
    state: &mut ScrollbackState,
    scratch: &mut ScratchBuffer,
    sel_model: &mut ResolvedSelectionModel,
    title: &str,
    accent: ratatui::style::Color,
    focused: bool,
    mouse: Option<(u16, u16)>,
    text: &TextSel,
    media: &mut Vec<InlineMediaPlacement>,
) {
    let theme = Theme::current();
    let border = if focused { accent } else { theme.bg_base };
    // Lay out before drawing the frame: the border title carries the count of
    // rows scrolled off the top, so it has to be known before the block is
    // rendered. Overflow below is stamped on the bottom border afterwards.
    let inner = Block::default().borders(Borders::ALL).inner(area);
    if inner.width == 0 || inner.height == 0 {
        return;
    }
    state.begin_frame();
    state.prepare_layout(inner.width, inner.height);
    clamp_scroll(state);
    let (above, below) = hidden_rows(state);
    let heading = if above > 0 {
        format!(" {title}  {above} more up ")
    } else {
        format!(" {title} ")
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border))
        .title(Span::styled(
            heading,
            Style::default().fg(accent).add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    f.render_widget(block, area);
    let hover = hovered_entry(sel_model, mouse);
    let output = ScrollbackPane::new()
        .active(focused)
        .with_hovered_entry(hover)
        .render_with_scratch(inner, f.buffer_mut(), state, scratch);
    *sel_model = output.selection_model;
    media.extend(output.inline_media);
    if let Some(sel) = output.selection_box {
        sel.render(f.buffer_mut());
    }
    if let Some(drag) = text.active {
        render_active_selection_overlay(sel_model, &drag, None, f.buffer_mut());
    }
    if let Some(persist) = text.persist {
        render_persistent_selection_overlay(sel_model, &persist, None, f.buffer_mut());
    }
    if below > 0 {
        stamp_footer(f, area, &format!(" {below} more down "), theme.gray_bright);
    }
}

/// Rows of this scrollback that are outside the viewport: (above, below).
/// Follow mode pins the view to the tail, so nothing is ever hidden below it.
fn hidden_rows(sb: &ScrollbackState) -> (usize, usize) {
    let (off, vp, total) = sb.scroll_info();
    if vp == 0 {
        return (0, 0);
    }
    let vp = vp as usize;
    let above = off.min(total);
    let below = total.saturating_sub(off + vp);
    (above, below)
}

/// Write a short label into the bottom border of `area`, right-aligned one
/// cell in from the corner. Purely decorative: it overwrites border glyphs
/// that the block already painted.
fn stamp_footer(f: &mut Frame, area: Rect, label: &str, color: ratatui::style::Color) {
    if area.height < 2 || area.width < 4 {
        return;
    }
    let w = label.chars().count() as u16;
    if w + 2 > area.width {
        return;
    }
    let x = area.x + area.width - 1 - w;
    let y = area.y + area.height - 1;
    let spot = Rect {
        x,
        y,
        width: w,
        height: 1,
    };
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(
            label.to_string(),
            Style::default().fg(color),
        ))),
        spot,
    );
}

/// Drop a notice that has had its seconds. Returns whether the frame needs a
/// repaint to erase it -- nothing else is live when a notice lapses.
fn expire_notice(app: &mut App) -> bool {
    if app.notice_stale() {
        app.notice = None;
        return true;
    }
    false
}

fn tick_scrollbacks(app: &mut App) -> bool {
    let mut need = app.sess.story.tick() || app.sess.calls.tick();
    for child in app.children.values_mut() {
        need |= child.sess.story.tick() || child.sess.calls.tick();
    }
    need
}

fn current_turn_activity(app: &App) -> Option<TurnActivity> {
    if !app.running {
        return None;
    }
    if app.sess.exec.live() {
        let title = exec_activity_title(app);
        return Some(TurnActivity::ToolRunning {
            title,
            description: None,
        });
    }
    if app.sess.stream.speech.is_some() || !app.sess.stream.speech_raw.is_empty() {
        return Some(TurnActivity::Responding);
    }
    if app.sess.stream.think.is_some() || !app.sess.stream.pending_think.is_empty() {
        return Some(TurnActivity::Thinking);
    }
    Some(TurnActivity::Waiting(WaitingReason::Model))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum InputSignal {
    Inference,
    Queued,
    Tool,
}

fn input_signal(app: &App) -> Option<InputSignal> {
    if matches!(
        current_turn_activity(app),
        Some(TurnActivity::ToolRunning { .. })
    ) {
        Some(InputSignal::Tool)
    } else if app.running {
        Some(InputSignal::Inference)
    } else if !app.queue.is_empty() {
        Some(InputSignal::Queued)
    } else {
        None
    }
}

/// What the harness is configured as, for the meta pane's lower rows.
///
/// This used to be a strip of text on the composer's bottom border, which is
/// the wrong place twice over: it is not something you type, and the composer
/// grows and shrinks under it. The meta pane is where "what am I running"
/// questions already get answered.
struct MetaId {
    model: String,
    effort: String,
    generation: String,
    /// A switch that takes effect next turn, named as queued rather than as
    /// current -- printing it as current is the pane claiming a model the wire
    /// is not using.
    pending: Option<(String, String)>,
    theme: String,
    session: Option<String>,
    /// Background tasks that will resume this session on their own.
    background: Vec<String>,
}

fn meta_id(app: &App) -> MetaId {
    let dash = |s: &str| {
        if s.is_empty() {
            "—".to_string()
        } else {
            s.to_string()
        }
    };
    MetaId {
        model: dash(&app.model),
        effort: dash(&app.thinking),
        generation: dash(&app.generation),
        pending: app.model_pending.clone(),
        theme: Theme::current_kind().display_name().to_string(),
        session: app.viewing.clone(),
        background: app.background.clone(),
    }
}

/// The meta pane names the syscall in flight, and only names it.
///
/// It used to reach into the Execute block and take the first line of the
/// command, so a `bash` call painted its argv into the status row: paths,
/// flags, whatever the body happened to start with. Meta is a meter a few
/// columns wide, so that lands as a truncated fragment of a command — no use
/// to read, and content that already has exactly one home. The calls pane
/// carries every body and every result. Disjoint routes, not a filter: meta
/// gets the tag, calls gets the payload.
fn exec_activity_title(app: &App) -> String {
    if app.sess.exec.tag.is_empty() {
        "syscall".into()
    } else {
        format!("<{}>", app.sess.exec.tag)
    }
}

fn streaming(app: &App) -> bool {
    app.sess.stream.live()
        || app.sess.exec.live()
        || app
            .children
            .values()
            .any(|c| c.sess.stream.live() || c.sess.exec.live())
}

fn flush_streams(app: &mut App) {
    app.sess
        .stream
        .flush(&mut app.sess.story, &mut app.sess.calls);
    app.sess.exec.flush(&mut app.sess.calls);
    for child in app.children.values_mut() {
        child
            .sess
            .stream
            .flush(&mut child.sess.story, &mut child.sess.calls);
        child.sess.exec.flush(&mut child.sess.calls);
    }
}

fn draw(f: &mut Frame, app: &mut App) {
    flush_streams(app);
    let theme = Theme::current();
    f.render_widget(
        Block::default().style(Style::default().bg(theme.bg_base).fg(theme.text_primary)),
        f.area(),
    );

    reflow_wire(&mut app.sess.calls, &app.sess.wire_manual);
    // Only the pane on screen: a fold reconcile is about what is drawn, and
    // children are never pruned, so walking all of them grew per-frame work
    // for panes nobody is looking at. `viewing` is set before the frame that
    // first shows a child.
    if let Some(c) = app.viewing.clone().and_then(|id| app.children.get_mut(&id)) {
        reflow_wire(&mut c.sess.calls, &c.sess.wire_manual);
    }

    // Columns first, because the composer wraps at the story column's width and
    // not the terminal's. Measuring against the whole frame under-counted rows
    // by the width of the wire column, so a paragraph overflowed a box that had
    // decided three rows were enough.
    let body = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage(100 - app.layout.wire_pct),
            Constraint::Percentage(app.layout.wire_pct),
        ])
        .split(f.area());

    // The composer frame shares the Story column edges; only its own border is
    // removed when measuring wrapped text.
    let inner_w = body[0].width.saturating_sub(2);
    let queue_h = app.queue.display_height();
    // The composer floats over the column while POST is open: a blank row above
    // it, so it reads as a card rather than one more stacked pane. Once POST is
    // fully collapsed that row is only dead
    // space between story and input, so the composer gives it back. An open
    // queue is the top card of the same group and owns the spacer itself --
    // including the giving back, which it did not do: with POST collapsed the
    // queue kept its float and left two blank rows under the story, its own
    // border and then the spacer.
    let queue_float = u16::from(queue_h > 0 && app.layout.post_h > 0);
    let queue_h = if queue_h > 0 {
        queue_h + queue_float
    } else {
        0
    };
    let float_rows = input_float_rows(app);
    // Grow with what is typed, up to half the column. The old ceiling of ten
    // rows existed to leave a legend band matching it opposite; there is no
    // legend now, and a long prompt is worth more rows than a short story is.
    let cap = (body[0].height / 2).saturating_sub(3).max(2);
    let default_rows = COMPOSER_DEFAULT_ROWS.min(cap);
    let prompt_rows = app.prompt.display_rows(inner_w).clamp(default_rows, cap);
    let input_h = (2 + float_rows + prompt_rows)
        .min(f.area().height.saturating_sub(8 + queue_h))
        .max(2 + float_rows + default_rows);
    let bottom_h = queue_h + input_h;
    let post_h = app
        .layout
        .post_h
        .min(body[0].height.saturating_sub(bottom_h + 3));
    let left = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(3),
            Constraint::Length(post_h),
            Constraint::Length(queue_h),
            Constraint::Length(input_h),
        ])
        .split(body[0]);
    // The wire column stacks: Activity, cache/session Meta, then git and the file it points
    // at. It runs to the bottom of the frame: the band it used to spend on a
    // key legend opposite the composer is the calls pane's now.
    let spare = body[1].height.saturating_sub(3);
    let meter_h = app.layout.meter_h.min(spare);
    let git_h = app.layout.git_h.min(spare.saturating_sub(meter_h));
    let files_h = app
        .layout
        .files_h
        .min(spare.saturating_sub(meter_h + git_h));
    // Meta sits last, in the bottom-right corner: it is the pane you glance at
    // between turns rather than read during one, and it is where every piece
    // of "what am I running" now lives -- model, effort, generation, theme.
    let wire = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(3),
            Constraint::Length(git_h),
            Constraint::Length(files_h),
            Constraint::Length(meter_h),
        ])
        .split(body[1]);
    let panes = [left[0], wire[0]];
    app.git_area = wire[1];
    app.files_area = wire[2];
    app.cache.area = wire[3];
    let posts = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage(app.layout.post_split),
            Constraint::Percentage(100 - app.layout.post_split),
        ])
        .split(left[1]);

    app.traj_area = panes[0];
    app.call_area = panes[1];
    app.post_in_area = posts[0];
    app.post_out_area = posts[1];
    // The card, not the slot: the float row above it is not part of the queue,
    // so a click there does nothing and the row arithmetic below stays honest.
    app.queue_area = if queue_float > 0 && left[2].height > 1 {
        Rect {
            x: left[2].x,
            y: left[2].y.saturating_add(queue_float),
            width: left[2].width,
            height: left[2].height.saturating_sub(queue_float),
        }
    } else {
        left[2]
    };
    app.input_area = left[3];

    let viewing = app.viewing.clone();
    let child_ok = viewing
        .as_deref()
        .is_some_and(|id| app.children.contains_key(id));
    // Computed before the child borrow: call_group_pos already resolves to
    // whichever session is on screen, so both branches want the same string.
    // The POST cards are off by default, so the pane says so where the switch
    // is: a chip in its own border title, clickable, `p` from the keyboard.
    let chip = if app.show_posts {
        "[-posts]"
    } else {
        "[+posts]"
    };
    let calls_title = match app.call_group_pos() {
        Some((cur, total)) => format!("Activity  #{cur}/{total}  {chip}"),
        None => format!("Activity  {chip}"),
    };
    app.calls_chip = if app.tree_open {
        // The tree replaces the Activity pane, chip included: a click where
        // the chip was must not flip a setting on a pane that is not there.
        None
    } else {
        title_chip_rect(app.call_area, &calls_title, chip)
    };
    if let (Some(id), true) = (viewing.as_deref(), child_ok) {
        let child = app.children.get_mut(id).expect("checked");
        let title = format!("Session {id}");
        draw_scrollback(
            f,
            panes[0],
            &mut child.sess.story,
            &mut child.sess.story_scratch,
            &mut child.sess.story_sel,
            &title,
            theme.accent_skill,
            app.focus == Focus::Story,
            app.mouse,
            &child.sess.story_text,
            &mut app.media.frame,
        );
        if !app.tree_open {
            draw_scrollback(
                f,
                panes[1],
                &mut child.sess.calls,
                &mut child.sess.calls_scratch,
                &mut child.sess.calls_sel,
                &calls_title,
                theme.accent_tool,
                app.focus == Focus::Calls,
                app.mouse,
                &child.sess.calls_text,
                &mut app.media.frame,
            );
        }
    } else {
        draw_scrollback(
            f,
            panes[0],
            &mut app.sess.story,
            &mut app.sess.story_scratch,
            &mut app.sess.story_sel,
            "Story",
            theme.accent_assistant,
            app.focus == Focus::Story,
            app.mouse,
            &app.sess.story_text,
            &mut app.media.frame,
        );
        if !app.tree_open {
            draw_scrollback(
                f,
                panes[1],
                &mut app.sess.calls,
                &mut app.sess.calls_scratch,
                &mut app.sess.calls_sel,
                &calls_title,
                theme.accent_tool,
                app.focus == Focus::Calls,
                app.mouse,
                &app.sess.calls_text,
                &mut app.media.frame,
            );
        }
    }
    // `t` on the Activity pane: the run tree takes the column until Esc/t.
    if app.tree_open {
        draw_tree_pane(f, panes[1], app);
    }
    let n = app.post_n;
    let in_title = if n == 0 {
        "POST in".to_string()
    } else {
        format!("POST in #{n}")
    };
    let on = app.post_out_n;
    let out_title = if on == 0 {
        "POST out".to_string()
    } else if on == n {
        format!("POST out #{on}")
    } else {
        format!("POST out #{on}  waiting #{n}")
    };
    draw_json_tree(
        f,
        posts[0],
        &mut app.post_in,
        &in_title,
        theme.accent_user,
        app.focus == Focus::PostIn,
    );
    draw_json_tree(
        f,
        posts[1],
        &mut app.post_out,
        &out_title,
        theme.accent_assistant,
        app.focus == Focus::PostOut,
    );
    let ident = meta_id(app);
    draw_meta(
        f,
        app.cache.area,
        &app.cache,
        app.focus == Focus::Meter,
        &ident,
    );
    draw_git(f, app.git_area, app);
    draw_files(f, app.files_area, app);
    draw_queue(f, app.queue_area, app);
    draw_input(f, app.input_area, app);
    if app.file_picker.is_open() {
        draw_file_picker(f, app);
    }
    if app.post_inspect.is_some() {
        draw_post_inspect(f, app);
    }
    if app.viewer.is_some() {
        draw_viewer(f, app);
    }
    if app.help {
        draw_help(f, app);
    }
    // Last, so startup cannot accidentally expose an interactive transcript.
    if app.session_picker.open {
        app.session_picker.render(f);
    } else if app.picker.open {
        let area = f.area();
        let buf = f.buffer_mut();
        app.picker.render(buf, area);
    }
    // Everything above paints over the panes, but a Kitty placement sits
    // above the cell background: an image behind an open modal would show
    // through it. Dropping this frame's placements makes the flush delete
    // them, and the next frame without a modal puts them back.
    if app.post_inspect.is_some()
        || app.viewer.is_some()
        || app.help
        || app.session_picker.open
        || app.picker.open
    {
        app.media.frame.clear();
    }
}

fn draw_json_tree(
    f: &mut Frame,
    area: Rect,
    tree: &mut JsonTree,
    title: &str,
    accent: ratatui::style::Color,
    focused: bool,
) {
    let theme = Theme::current();
    let border = if focused { accent } else { theme.bg_base };
    // Lay the rows out first: the title reports what scrolled off the top, so
    // the count has to exist before the block is built. Same contract the
    // scrollback panes already follow.
    let inner = Block::default().borders(Borders::ALL).inner(area);
    if inner.width == 0 || inner.height == 0 {
        f.render_widget(
            Block::default()
                .borders(Borders::ALL)
                .border_style(Style::default().fg(border))
                .title(Span::styled(
                    format!(" {title} "),
                    Style::default().fg(accent).add_modifier(Modifier::BOLD),
                ))
                .style(Style::default().bg(theme.bg_base).fg(theme.text_primary)),
            area,
        );
        return;
    }
    let lines = tree.lines(inner.width, inner.height, focused);
    let (above, below) = tree.hidden();
    let heading = if above > 0 {
        format!(" {title}  {above} more up ")
    } else {
        format!(" {title} ")
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border))
        .title(Span::styled(
            heading,
            Style::default().fg(accent).add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    f.render_widget(block, area);
    f.render_widget(Paragraph::new(lines), inner);
    if below > 0 {
        stamp_footer(f, area, &format!("{below} more down"), theme.gray_bright);
    }
}

/// The meta pane: prompt-cache TTL burning down, the token split of the last
/// `complete()`, and what the session has spent. Named for what it holds now,
/// not for the one number it started as.
/// How much a pane can say in the rows it was given. A pane picks its own
/// rendering from this instead of drawing one layout and letting ratatui clip
/// the tail — a clipped meter and a cold meter look identical.
///
/// Rows only: width is handled by the panes themselves (`fit_status`, the
/// scrollback's own wrapping, `truncate_width` in the queue). Derived fresh
/// every frame and never stored, so there is no stale tier to oscillate on.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Tier {
    Line,
    Dense,
    Full,
}

impl Tier {
    fn of(rows: u16) -> Self {
        match rows {
            0..=1 => Self::Line,
            // Dense keeps only context and cache. Full adds cost and identity.
            2..=3 => Self::Dense,
            _ => Self::Full,
        }
    }
}

/// One meter row: a filled track with its label and value written *inside* it.
///
/// The old layout spent two rows per bar -- the bar, then a legend underneath --
/// which is why the pane sprawled. Painting the text over the track costs one
/// row and reads the same, so long as the ink flips to the background colour
/// where it crosses a filled cell.
#[allow(clippy::too_many_arguments)]
fn meter_row(
    width: u16,
    label: &str,
    value: &str,
    segments: &[(u64, ratatui::style::Color)],
    ratio: f64,
    track: ratatui::style::Color,
    ink_on_fill: ratatui::style::Color,
    ink_on_track: ratatui::style::Color,
) -> Line<'static> {
    let w = width as usize;
    if w == 0 {
        return Line::from("");
    }
    let filled = ((w as f64) * ratio.clamp(0.0, 1.0)).round() as usize;

    // Paint the track, then hand the filled prefix to the segment allocator so
    // a role split and a plain fill share one code path.
    let mut bg: Vec<ratatui::style::Color> = vec![track; w];
    let total: u64 = segments.iter().map(|(v, _)| *v).sum();
    if filled > 0 {
        if total == 0 {
            let fallback = segments.first().map(|(_, c)| *c).unwrap_or(ink_on_track);
            for cell in bg.iter_mut().take(filled) {
                *cell = fallback;
            }
        } else {
            let mut at = 0usize;
            for span in sequence_bar_spans(filled as u16, segments, track) {
                let n = span.content.chars().count();
                let color = span.style.fg.unwrap_or(track);
                for cell in bg.iter_mut().skip(at).take(n) {
                    *cell = color;
                }
                at += n;
            }
        }
    }

    // Label hugs the left edge, value the right. If they would collide the
    // value wins -- a number you cannot read is worse than a missing word.
    let mut text: Vec<Option<char>> = vec![None; w];
    let vchars: Vec<char> = value.chars().collect();
    // One cell of gutter on the right so the value never touches the border.
    let vstart = w.saturating_sub(vchars.len() + 1);
    for (i, ch) in vchars.iter().enumerate() {
        if vstart + i < w {
            text[vstart + i] = Some(*ch);
        }
    }
    let lchars: Vec<char> = label.chars().collect();
    if 1 + lchars.len() < vstart {
        for (i, ch) in lchars.iter().enumerate() {
            text[1 + i] = Some(*ch);
        }
    }

    // Group runs of identical style so a 60-cell row is a handful of spans.
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut run = String::new();
    let mut run_style: Option<Style> = None;
    for i in 0..w {
        let filled_here = bg[i] != track;
        let ink = if filled_here {
            ink_on_fill
        } else {
            ink_on_track
        };
        let style = Style::default().bg(bg[i]).fg(ink);
        let ch = text[i].unwrap_or(' ');
        if run_style == Some(style) {
            run.push(ch);
        } else {
            if let Some(st) = run_style {
                spans.push(Span::styled(std::mem::take(&mut run), st));
            }
            run.push(ch);
            run_style = Some(style);
        }
    }
    if let Some(st) = run_style {
        spans.push(Span::styled(run, st));
    }
    Line::from(spans)
}

/// Draw chunks in the order they occur.
///
/// Sorting chunks into buckets and giving each one contiguous block answers
/// how much and destroys where. This keeps the order, sampling the run at the
/// midpoint of every cell, so a fat chunk late in the trajectory shows up late
/// in the bar. That is what replaced the bucketed bar, and why.
fn sequence_bar_spans(
    width: u16,
    chunks: &[(u64, ratatui::style::Color)],
    empty: ratatui::style::Color,
) -> Vec<Span<'static>> {
    let w = width as usize;
    if w == 0 || chunks.is_empty() {
        return Vec::new();
    }
    let total: u64 = chunks.iter().map(|(n, _)| *n).sum();
    if total == 0 {
        return vec![Span::styled("█".repeat(w), Style::default().fg(empty))];
    }
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut run = String::new();
    let mut cur: Option<ratatui::style::Color> = None;
    for i in 0..w {
        let mid = total * (2 * i as u64 + 1) / (2 * w as u64);
        let mut acc = 0u64;
        let mut color = chunks[chunks.len() - 1].1;
        for (n, c) in chunks {
            acc += *n;
            if mid < acc {
                color = *c;
                break;
            }
        }
        if cur == Some(color) {
            run.push('█');
        } else {
            if let Some(prev) = cur {
                spans.push(Span::styled(
                    std::mem::take(&mut run),
                    Style::default().fg(prev),
                ));
            }
            run.push('█');
            cur = Some(color);
        }
    }
    if let Some(prev) = cur {
        spans.push(Span::styled(run, Style::default().fg(prev)));
    }
    spans
}

/// Git state as a tab strip over rows — druk's sidebar, with the views it
/// makes sense to have beside a wire pane. The strip is drawn in the border
/// title so it costs no row of its own.
fn draw_git(f: &mut Frame, area: Rect, app: &mut App) {
    if area.height < 3 || area.width == 0 {
        return;
    }
    let theme = Theme::current();
    let focused = app.focus == Focus::Git;
    let border = if focused {
        theme.accent_skill
    } else {
        theme.bg_base
    };
    let mut title: Vec<Span> = vec![Span::raw(" ")];
    for tab in side::GitTab::ALL {
        let on = tab == app.git.tab;
        title.push(Span::styled(
            format!(" {} ", tab.label()),
            if on {
                Style::default()
                    .fg(theme.accent_skill)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(theme.text_secondary)
            },
        ));
    }
    let branch = app.git.branch().to_string();
    if !branch.is_empty() {
        title.push(Span::styled(
            format!(" {branch} "),
            Style::default().fg(theme.text_secondary),
        ));
    }
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border))
        .title(Line::from(title))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    let inner = block.inner(area);
    f.render_widget(block, area);
    if inner.width == 0 || inner.height == 0 {
        return;
    }
    app.git.clamp(inner.height as usize);
    let rows = app.git.rows();
    if let Some(err) = app.git.error() {
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                err.to_string(),
                Style::default().fg(theme.accent_user),
            ))),
            inner,
        );
        return;
    }
    if rows.is_empty() {
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                "clean",
                Style::default().fg(theme.text_secondary),
            ))),
            inner,
        );
        return;
    }
    let lines: Vec<Line> = rows
        .iter()
        .enumerate()
        .skip(app.git.scroll)
        .take(inner.height as usize)
        .map(|(i, row)| {
            let mark_style = match row.mark.as_str() {
                "??" => Style::default().fg(theme.text_secondary),
                "*" => Style::default().fg(theme.accent_success),
                m if m.starts_with('D') => Style::default().fg(theme.accent_user),
                _ => Style::default().fg(theme.accent_tool),
            };
            let mut line = Line::from(vec![
                Span::styled(format!("{:<3}", row.mark), mark_style),
                Span::styled(row.text.clone(), Style::default().fg(theme.text_primary)),
            ]);
            if focused && i == app.git.sel {
                line = line.style(Style::default().bg(theme.bg_highlight));
            }
            line
        })
        .collect();
    f.render_widget(Paragraph::new(lines), inner);
}

/// The file the git cursor points at, read-only. druk puts an editor here;
/// this is the part of it that belongs next to a harness — see what changed
/// without leaving the pane.
fn draw_files(f: &mut Frame, area: Rect, app: &mut App) {
    if area.height < 3 || area.width == 0 {
        return;
    }
    let theme = Theme::current();
    let focused = app.focus == Focus::Files;
    let border = if focused {
        theme.accent_assistant
    } else {
        theme.bg_base
    };
    let title = format!(" {} ", app.files.title());
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border))
        .title(Span::styled(
            title,
            Style::default()
                .fg(theme.accent_assistant)
                .add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    let inner = block.inner(area);
    f.render_widget(block, area);
    if inner.width == 0 || inner.height == 0 {
        return;
    }
    if let Some(note) = &app.files.note {
        f.render_widget(
            Paragraph::new(Line::from(Span::styled(
                note.clone(),
                Style::default().fg(theme.text_secondary),
            ))),
            inner,
        );
        return;
    }
    let width = inner.width as usize;
    // Keep the existing flat FilePane model, but borrow druk's tree grammar:
    // a guide and one-column icon lead each name, while git marks own the right edge.
    let git_marks: Vec<(String, String)> = app
        .git
        .status_rows()
        .iter()
        .map(|row| (row.text.clone(), row.mark.clone()))
        .collect();
    // Two states, one pane: a directory listing, or the file opened out of it.
    // The title says which — `src/` against `side.rs`.
    let lines: Vec<Line> = if app.files.in_file() {
        app.files
            .lines
            .iter()
            .enumerate()
            .skip(app.files.scroll)
            .take(inner.height as usize)
            .map(|(i, text)| {
                let n = format!("{:>4} ", i + 1);
                let room = width.saturating_sub(n.len());
                let body: String = text.chars().take(room).collect();
                Line::from(vec![
                    Span::styled(n, Style::default().fg(theme.gray_bright)),
                    Span::styled(body, Style::default().fg(theme.text_primary)),
                ])
            })
            .collect()
    } else {
        app.files
            .entries
            .iter()
            .enumerate()
            .skip(app.files.scroll)
            .take(inner.height as usize)
            .map(|(i, row)| {
                let status = git_marks.iter().find_map(|(path, mark)| {
                    (path == &row.name
                        || path
                            .rsplit_once('/')
                            .is_some_and(|(_, name)| name == row.name))
                    .then_some(mark.as_str())
                });
                let (mark, mark_style) = match status {
                    Some("??") => ("U".to_string(), Style::default().fg(theme.accent_success)),
                    Some(mark) if mark.starts_with('D') => {
                        ("D".to_string(), Style::default().fg(theme.accent_user))
                    }
                    Some("*") => ("M".to_string(), Style::default().fg(theme.accent_tool)),
                    Some(mark) => (
                        mark.trim()
                            .chars()
                            .next()
                            .map_or("M".to_string(), |c| c.to_string()),
                        Style::default().fg(theme.accent_tool),
                    ),
                    None => (String::new(), Style::default().fg(theme.text_secondary)),
                };
                let guide = if row.name == ".." { "  " } else { "│ " };
                let icon = if row.name == ".." {
                    "▴ "
                } else if row.is_dir {
                    "▸ "
                } else {
                    "· "
                };
                let mark_width = usize::from(!mark.is_empty()) * 2;
                let room = width.saturating_sub(guide.chars().count() + 2 + mark_width);
                let name_chars: Vec<char> = row.name.chars().collect();
                let name = if name_chars.len() > room {
                    let mut clipped: String = name_chars
                        .iter()
                        .take(room.saturating_sub(1))
                        .copied()
                        .collect();
                    if room > 0 {
                        clipped.push('…');
                    }
                    clipped
                } else {
                    row.name.clone()
                };
                let padding = " ".repeat(room.saturating_sub(name.chars().count()));
                let name_style = if row.is_dir {
                    Style::default()
                        .fg(theme.accent_skill)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(theme.text_primary)
                };
                let icon_style = if row.is_dir {
                    Style::default().fg(theme.accent_skill)
                } else {
                    Style::default().fg(theme.gray_bright)
                };
                let mut line = Line::from(vec![
                    Span::styled(guide, Style::default().fg(theme.gray_bright)),
                    Span::styled(icon, icon_style),
                    Span::styled(name, name_style),
                    Span::raw(padding),
                    Span::styled(
                        if mark.is_empty() {
                            String::new()
                        } else {
                            format!("{mark} ")
                        },
                        mark_style,
                    ),
                ]);
                if i == app.files.sel {
                    let bg = if focused {
                        theme.bg_hover
                    } else {
                        theme.bg_highlight
                    };
                    // Background and weight only: flattening the foregrounds
                    // too would cost the selected row its directory accent and
                    // the colour of its git mark, which is the information the
                    // row is there to carry.
                    let selected = Style::default().bg(bg).add_modifier(Modifier::BOLD);
                    for span in &mut line.spans {
                        span.style = span.style.patch(selected);
                    }
                    line = line.style(selected);
                }
                line
            })
            .collect()
    };
    f.render_widget(Paragraph::new(lines), inner);
}

/// Where the cache sits in its window, as (fill, segments) for the bar.
///
/// The countdown is what a glance needs: the fill is the time still on the
/// clock and the colour says which stage of the window that is. Blue while
/// nothing is cached and nothing is counting down, green while the entry is
/// fresh, yellow once it is past halfway, amber in the last fifth -- the
/// stretch where the next call is the one that pays to write it again. A
/// provider that declares no TTL has no window to stage, so it keeps the
/// read-against-write proportion, which is the only honest thing its bar can
/// say.
fn cache_stage(meter: &CacheMeter, theme: &Theme) -> (f64, Vec<(u64, ratatui::style::Color)>) {
    if !meter.ephemeral {
        let rw = meter.read + meter.write;
        return (
            if rw == 0 { 0.0 } else { 1.0 },
            vec![
                (meter.read, theme.accent_success),
                (meter.write, theme.accent_tool),
            ],
        );
    }
    match meter.left() {
        // Never written, or already expired. Both are cold, and a cold window
        // is not running out of anything -- so it reads as a full calm track
        // rather than an empty one, which would look like a cache about to die.
        None => (1.0, vec![(1, theme.accent_system)]),
        Some(l) => {
            let colour = if l > 0.5 {
                theme.accent_success
            } else if l > 0.2 {
                theme.warning
            } else {
                theme.path
            };
            (f64::from(l), vec![(1, colour)])
        }
    }
}

fn draw_meta(f: &mut Frame, area: Rect, meter: &CacheMeter, focused: bool, id: &MetaId) {
    if area.height == 0 || area.width == 0 {
        return;
    }
    let theme = Theme::current();
    let left = meter.left();
    let secs = left.map(|l| (l * meter.ttl.as_secs_f32()).round() as u64);
    let ttl_label = if meter.ttl.as_secs() >= 3600 {
        "1h"
    } else {
        "5m"
    };
    // Cache status belongs on the cache row, not jammed into the pane title.
    // That keeps Meta's chrome aligned with every other pane.
    let cache_value = match secs {
        _ if !meter.ephemeral && meter.read + meter.write == 0 => "cold".to_string(),
        _ if !meter.ephemeral => format!("{}% cached", meter.hit()),
        Some(s) => format!("{}% · {ttl_label} {}:{:02}", meter.hit(), s / 60, s % 60),
        None => "cold".to_string(),
    };
    let border = if focused {
        theme.accent_tool
    } else {
        theme.bg_base
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border))
        .title(Span::styled(
            " Meta ",
            Style::default()
                .fg(theme.accent_tool)
                .add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    let inner = block.inner(area);
    f.render_widget(block, area);
    if inner.width == 0 || inner.height == 0 {
        return;
    }

    let label = |s: &str| Span::styled(s.to_string(), Style::default().fg(theme.text_secondary));

    // One row, two zones: how long the cache entry has left, and what the
    // last call did with it. They answer the same question — is the window
    // working for me — so they share a row instead of stacking.

    // Priority order: what a glance needs first, then detail. Whatever the
    // pane cannot fit is dropped from the tail, not clipped mid-thought.
    let ctx_used = meter.read + meter.write + meter.fresh;
    let window = if meter.window == 0 {
        200_000
    } else {
        meter.window
    };
    let kind_color = |k: u8| match k {
        0 => theme.accent_skill,
        1 => theme.accent_user,
        2 => theme.accent_success,
        3 => theme.accent_tool,
        _ => theme.accent_assistant,
    };
    // The run in order, not five buckets. Where the weight sits in the
    // trajectory is the part a percentage cannot say.
    let roles: Vec<(u64, ratatui::style::Color)> = if meter.chunks.is_empty() {
        meter
            .roles
            .iter()
            .enumerate()
            .map(|(i, n)| (*n, kind_color(i as u8)))
            .collect()
    } else {
        meter
            .chunks
            .iter()
            .map(|(n, k)| (*n, kind_color(*k)))
            .collect()
    };
    // How full, and full of what -- one bar answers both. A role split
    // normalised to 100% looks identical at 8k and at 180k, which is the one
    // thing worth knowing.
    let ctx_row = || {
        meter_row(
            inner.width,
            "ctx",
            &format!("{} / {}", tokens(ctx_used), tokens(window)),
            &roles,
            ctx_used as f64 / window as f64,
            theme.bg_highlight,
            theme.bg_base,
            theme.text_primary,
        )
    };
    let (cache_fill, cache_segments) = cache_stage(meter, &theme);
    let cache_row = || {
        meter_row(
            inner.width,
            "cache",
            &cache_value,
            &cache_segments,
            cache_fill,
            theme.bg_highlight,
            theme.bg_base,
            theme.text_primary,
        )
    };
    let money_row = || {
        // A ChatGPT plan does not bill per token. Printing a dollar figure
        // there is not an estimate, it is a number that will never appear on
        // any invoice -- so say what it would have cost at list price instead.
        if meter.plan {
            return Line::from(vec![
                Span::styled(
                    "plan",
                    Style::default()
                        .fg(theme.accent_success)
                        .add_modifier(Modifier::BOLD),
                ),
                label("   "),
                Span::styled(money(meter.spent), Style::default().fg(theme.gray)),
                label(" at list"),
            ]);
        }
        Line::from(vec![
            Span::styled(
                money(meter.spent),
                Style::default()
                    .fg(theme.accent_user)
                    .add_modifier(Modifier::BOLD),
            ),
            label(" spent   "),
            Span::styled(
                money(meter.saved),
                Style::default().fg(theme.accent_success),
            ),
            label(" saved"),
        ])
    };

    // What is running, and under what settings. A queued switch is named as
    // queued.
    let agent_row = || {
        let mut spans = vec![Span::styled(
            id.model.clone(),
            Style::default()
                .fg(theme.accent_assistant)
                .add_modifier(Modifier::BOLD),
        )];
        spans.push(label("  effort "));
        spans.push(Span::styled(
            id.effort.clone(),
            Style::default().fg(theme.text_primary),
        ));
        spans.push(label("  gen "));
        spans.push(Span::styled(
            id.generation.clone(),
            Style::default().fg(theme.text_primary),
        ));
        if let Some(s) = &id.session {
            spans.push(label("   session "));
            spans.push(Span::styled(
                s.clone(),
                Style::default().fg(theme.accent_skill),
            ));
        }
        Line::from(spans)
    };

    // A switch that lands next turn gets the row the theme swatches were
    // using. It is transient and it changes what the next request costs; a
    // palette is neither.
    let pending_row = || {
        let (m, e) = id.pending.clone().unwrap_or_default();
        Line::from(vec![
            Span::styled("\u{2192} ", Style::default().fg(theme.accent_user)),
            Span::styled(
                format!("{m}/{e}"),
                Style::default()
                    .fg(theme.accent_user)
                    .add_modifier(Modifier::BOLD),
            ),
            label(" queued"),
        ])
    };

    // Background work the kernel is still holding. It earns a row because it
    // changes what to do next: something is going to come back and resume the
    // session, so waiting is correct and polling is not. Named, because "1
    // task" does not say whether it is a build or a sleep.
    let background_row = || {
        let mut spans = vec![
            Span::styled("\u{21bb} ", Style::default().fg(theme.accent_tool)),
            Span::styled(
                format!("{}", id.background.len()),
                Style::default()
                    .fg(theme.accent_tool)
                    .add_modifier(Modifier::BOLD),
            ),
            label(" waiting  "),
        ];
        spans.push(Span::styled(
            id.background.join(", "),
            Style::default().fg(theme.text_secondary),
        ));
        Line::from(spans)
    };

    // The theme, shown rather than named: the palette a block will actually be
    // painted in, in the order the panes use it.
    let theme_row = || {
        let mut spans = vec![Span::styled(
            id.theme.clone(),
            Style::default().fg(theme.text_secondary),
        )];
        spans.push(Span::raw("  "));
        for c in [
            theme.accent_user,
            theme.accent_assistant,
            theme.accent_tool,
            theme.accent_skill,
            theme.accent_success,
            theme.gray,
        ] {
            spans.push(Span::styled("\u{2588}\u{2588}", Style::default().fg(c)));
        }
        Line::from(spans)
    };

    // Degrade by which question matters most, not by what happens to fit. The
    // title already carries the TTL, so row one is context, not hit rate.
    let mut lines = match Tier::of(inner.height) {
        // One row is not enough to say both; a squeezed meter is still a meter.
        Tier::Line => vec![ctx_row()],
        Tier::Dense => vec![ctx_row(), cache_row()],
        // The sparkline was the one row nobody read: a hit-rate trend restates
        // what the cache row already says, in less precise form.
        Tier::Full => vec![
            ctx_row(),
            cache_row(),
            money_row(),
            agent_row(),
            // Last row, three claimants, in order of how fast the answer
            // goes stale: work that will resume the session, then a switch
            // that lands next turn, then the palette.
            if !id.background.is_empty() {
                background_row()
            } else if id.pending.is_some() {
                pending_row()
            } else {
                theme_row()
            },
        ],
    };
    lines.truncate(inner.height as usize);
    f.render_widget(Paragraph::new(lines), inner);
}

/// 26258 → `26.3k`; keeps the meter columns from jumping as a session grows.
fn tokens(n: u64) -> String {
    match n {
        n if n >= 1_000_000 => format!("{:.1}M", n as f64 / 1e6),
        n if n >= 10_000 => format!("{:.1}k", n as f64 / 1e3),
        n => n.to_string(),
    }
}

fn money(v: f64) -> String {
    if v >= 1.0 {
        format!("${v:.2}")
    } else {
        format!("${v:.4}")
    }
}

fn draw_queue(f: &mut Frame, area: Rect, app: &App) {
    if area.height == 0 || app.queue.is_empty() {
        return;
    }
    let theme = Theme::current();
    let focused = app.focus == Focus::Queue;
    let border = if focused {
        theme.accent_user
    } else {
        theme.bg_base
    };
    let title = format!(" Queue  {} ", app.queue.len());
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border))
        .title(Span::styled(
            title,
            Style::default()
                .fg(theme.accent_user)
                .add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    let inner = block.inner(area);
    f.render_widget(block, area);
    if inner.width == 0 || inner.height == 0 {
        return;
    }
    // The band is drawn where `selected` and the scroll offset are both in
    // scope. Reapplying it here indexed the visible slice with an absolute
    // row number, which lands on the wrong row the moment the queue scrolls.
    f.render_widget(Paragraph::new(app.queue.lines(inner.width, focused)), inner);
}

fn less_saturated(color: Color) -> Color {
    let Color::Rgb(r, g, b) = color else {
        return color;
    };
    // Preserve the selected hue while pulling 40% of its chroma toward its
    // luminance. At rest it reads as the same state colour, not generic gray.
    let gray = ((r as u16 * 30 + g as u16 * 59 + b as u16 * 11) / 100) as u8;
    let mix = |channel: u8| ((channel as u16 * 3 + gray as u16 * 2) / 5) as u8;
    Color::Rgb(mix(r), mix(g), mix(b))
}

/// The run tree over the Activity column: one row per subagent run, nested by
/// the kernel's own parent/depth, fed purely from events. A list pane in the
/// queue's shape — the rows come from `tree::row_text`, not a second renderer.
fn draw_tree_pane(f: &mut Frame, area: Rect, app: &mut App) {
    if area.height < 3 || area.width == 0 {
        return;
    }
    let theme = Theme::current();
    let focused = app.focus == Focus::Calls;
    let border = if focused {
        theme.accent_tool
    } else {
        theme.bg_base
    };
    let ids = tree::order(&app.children);
    app.tree_sel = app.tree_sel.min(ids.len().saturating_sub(1));
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border))
        .title(Span::styled(
            format!(" Runs  {} ", ids.len()),
            Style::default()
                .fg(theme.accent_tool)
                .add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    let inner = block.inner(area);
    f.render_widget(block, area);
    if inner.width == 0 || inner.height == 0 {
        return;
    }
    if ids.is_empty() {
        f.render_widget(
            Paragraph::new(Span::styled(
                "no runs this session",
                Style::default().fg(theme.text_secondary),
            )),
            inner,
        );
        return;
    }
    let w = inner.width as usize;
    let lines: Vec<Line> = ids
        .iter()
        .enumerate()
        .skip(app.tree_skip())
        .take(inner.height as usize)
        .map(|(i, id)| {
            let text = tree::row_text(&app.children[id]);
            let selected = focused && i == app.tree_sel;
            let tone = if selected {
                theme.accent_tool
            } else {
                less_saturated(theme.accent_tool)
            };
            let mut line = Line::from(Span::styled(text, Style::default().fg(tone)));
            if selected {
                let band = Style::default()
                    .bg(theme.bg_highlight)
                    .add_modifier(Modifier::BOLD);
                for span in &mut line.spans {
                    span.style = span.style.patch(band);
                }
                let used: usize = line
                    .spans
                    .iter()
                    .map(|s| UnicodeWidthStr::width(s.content.as_ref()))
                    .sum();
                if w > used {
                    line.spans.push(Span::styled(" ".repeat(w - used), band));
                }
            }
            line
        })
        .collect();
    f.render_widget(Paragraph::new(lines), inner);
}

/// What to call the focused pane in the legend title.
fn focus_name(focus: Focus) -> &'static str {
    match focus {
        Focus::Input => "input",
        Focus::Story => "story",
        Focus::Calls => "Activity",
        Focus::Meter => "meta",
        Focus::Git => "git",
        Focus::Files => "files",
        Focus::PostIn => "POST in",
        Focus::PostOut => "POST out",
        Focus::Queue => "queue",
    }
}

/// The blank spacer row the composer floats on: one while POST is open, none
/// when it is collapsed or when an open queue is carrying that row instead.
fn input_float_rows(app: &App) -> u16 {
    u16::from(app.queue.is_empty() && app.layout.post_h > 0)
}

fn draw_input(f: &mut Frame, area: Rect, app: &mut App) {
    let theme = Theme::current();
    // Float the box while POST is visible. The blank row is the group's, not
    // this box's: with the queue open it sits above the queue and the two cards
    // touch; with POST collapsed it disappears entirely.
    let float = input_float_rows(app);
    let card = Rect {
        x: area.x,
        y: area.y.saturating_add(float),
        width: area.width,
        height: area.height.saturating_sub(float),
    };
    let prefix = " ";
    let focused = app.focus == Focus::Input;
    let signal = input_signal(app);
    let (signal_label, signal_color) = match signal {
        Some(InputSignal::Inference) => (Some("Inference".to_string()), theme.accent_assistant),
        // No count here. The queue pane above already titles itself "Queue N"
        // and lists every item; repeating the number on the composer border put
        // "Queue 1" and "Queued 1" one row apart.
        Some(InputSignal::Queued) => (Some("Queued".to_string()), theme.accent_user),
        Some(InputSignal::Tool) => (Some("Tool".to_string()), theme.accent_tool),
        None => (
            None,
            if focused {
                theme.prompt_border_active
            } else {
                theme.prompt_border
            },
        ),
    };
    // The whole composer frame is the activity indicator. Runtime states keep
    // their own hue while the bold pulse makes progress visible without adding
    // another status row.
    let pulse = (app.sess.story.animation_tick() / 6) % 2 == 0;
    let mut border_style = Style::default().fg(signal_color);
    if signal.is_some() && pulse {
        border_style = border_style.add_modifier(Modifier::BOLD);
    }

    let stop = " [stop] ";
    let stop_w = UnicodeWidthStr::width(stop) as u16;
    let stop_area = Rect {
        x: card.x + card.width.saturating_sub(1 + stop_w),
        y: card.y,
        width: stop_w,
        height: 1,
    };
    app.turn_cancel = if app.running { Some(stop_area) } else { None };
    let mut block = Block::default()
        .borders(Borders::ALL)
        .border_style(border_style);

    let mut left_title: Vec<Span> = Vec::new();
    if let Some(label) = signal_label.as_ref() {
        let frames = glyphs::braille_spinner_frames();
        let frame = frames
            .get(app.sess.story.animation_tick() as usize % frames.len().max(1))
            .copied()
            .unwrap_or(" ");
        left_title.push(Span::styled(
            format!(" {frame} {label} "),
            Style::default()
                .fg(signal_color)
                .add_modifier(Modifier::BOLD),
        ));
    }
    // Notices share the left title instead of replacing the runtime state.
    if let Some((_, msg)) = app.notice.as_ref() {
        let used = left_title
            .iter()
            .map(|span| UnicodeWidthStr::width(span.content.as_ref()) as u16)
            .sum::<u16>();
        let room = card
            .width
            .saturating_sub(if app.running { stop_w + 3 } else { 2 })
            .saturating_sub(used + 2) as usize;
        let mut text = msg.clone();
        if UnicodeWidthStr::width(text.as_str()) > room {
            text = text
                .chars()
                .take(room.saturating_sub(1))
                .collect::<String>()
                + "\u{2026}";
        }
        left_title.push(Span::styled(
            format!(" {text} "),
            Style::default().fg(theme.text_secondary),
        ));
    }
    // No "multiline" chip. Wrapping to a second row is visible in the box
    // itself, so labelling it spent a title slot on something the user can
    // already see -- and it painted a success-green accent on a state that is
    // neither a success nor an event.
    if !left_title.is_empty() {
        block = block.title(Line::from(left_title));
    }
    if app.running {
        let hovered = app.mouse.is_some_and(|(c, r)| hit(stop_area, c, r));
        block = block.title(
            Line::from(Span::styled(
                stop,
                Style::default()
                    .fg(if hovered {
                        theme.accent_user
                    } else {
                        theme.text_secondary
                    })
                    .add_modifier(Modifier::BOLD),
            ))
            .right_aligned(),
        );
    }
    let block = block.style(Style::default().bg(theme.bg_base));
    let inner = block.inner(card);
    app.input_inner = inner;
    let lay = app.prompt.layout(prefix, inner.width);
    // Past the growth cap this becomes a viewport over the wrapped prompt.
    app.input_scroll = lay
        .cursor_row
        .saturating_sub(inner.height.saturating_sub(1));
    f.render_widget(block, card);
    if inner.width > 0 && inner.height > 0 {
        f.render_widget(
            Paragraph::new(lay.lines.clone())
                .scroll((app.input_scroll, 0))
                .style(Style::default().fg(theme.text_primary)),
            inner,
        );
    }
    if focused && inner.width > 0 && inner.height > 0 {
        let x = inner.x + lay.cursor_col.min(inner.width.saturating_sub(1));
        let y = inner.y
            + lay
                .cursor_row
                .saturating_sub(app.input_scroll)
                .min(inner.height.saturating_sub(1));
        f.buffer_mut()[(x, y)].modifier.insert(Modifier::REVERSED);
        f.set_cursor_position(Position { x, y });
    }
    if focused {
        // A paste preview and a command list want the same strip of screen and
        // never apply at once — a pasted body is not a slash line.
        draw_slash(f, app.input_area, app);
        if let Some(body) = app.prompt.preview_body() {
            draw_paste_preview(f, app.input_area, body, app.prompt.preview_on_chip());
        }
    }
}

/// The keys a pane answers to. One table, read by the cheatsheet and by the
/// test that checks the table against the handler — a legend nobody can verify
/// is a legend that goes stale, which is how the old composer-border strip
/// ended up describing keys that had moved.
fn pane_keys(focus: Focus) -> (&'static str, &'static [(&'static str, &'static str)]) {
    match focus {
        Focus::Story | Focus::Calls => (
            "story / Activity",
            &[
                ("j k", "select a block"),
                ("h", "collapse it"),
                ("l", "fold: collapsed / truncated / expanded"),
                (
                    "enter",
                    "zoom into the viewer (a spawn row opens the child)",
                ),
                ("ctrl-f", "zoom, without moving the fold"),
                ("[ ]", "previous / next POST group (Activity)"),
                ("p", "show / hide the POST rows (Activity)"),
                (
                    "t",
                    "run tree (Activity): enter opens, x kill, r rerun, t/esc back",
                ),
                ("r", "raw text of the selected block"),
                ("pgup pgdn", "scroll a page"),
                ("i", "back to the composer"),
            ],
        ),
        Focus::PostIn | Focus::PostOut => (
            "POST in / out",
            &[
                ("j k", "walk the JSON tree"),
                ("h", "fold this node"),
                ("l enter", "open this node"),
                ("e ctrl-f", "expand the whole side into the popup"),
                ("pgup pgdn", "scroll a page"),
                ("i", "back to the composer"),
            ],
        ),
        Focus::Queue => (
            "queue",
            &[
                ("j k", "select a queued row"),
                ("[ ] h l", "move it earlier / later (arrows too)"),
                (
                    "e",
                    "edit it in the composer (enter returns it to its slot)",
                ),
                ("d del", "drop it"),
                ("enter", "send now: stop this step and run the front row"),
                ("i", "back to the composer"),
            ],
        ),
        Focus::Git => (
            "git",
            &[
                ("j k", "select a row (it previews as you go)"),
                ("[ ]", "along the tab strip in the title"),
                ("enter l", "open the row in the files pane"),
                ("r", "refresh"),
                ("pgup pgdn", "scroll a page"),
                ("i", "back to the composer"),
            ],
        ),
        Focus::Files => (
            "files",
            &[
                ("j k", "select"),
                ("l enter", "into the directory / open the file"),
                ("h", "back up (lands on the file you came from)"),
                ("pgup pgdn", "scroll a page"),
                ("i", "back to the composer"),
            ],
        ),
        Focus::Meter => (
            "meta",
            &[
                ("", "a meter: no cursor, nothing to fold"),
                ("i", "back to the composer"),
            ],
        ),
        Focus::Input => ("composer", &[]),
    }
}

/// Keys that mean the same thing in every pane. Listed once, under the
/// pane's own verbs.
const SHARED_KEYS: &[(&str, &str)] = &[
    ("tab", "next pane (shift-tab back)"),
    ("+ -", "grow / shrink this pane, 0 resets"),
    ("ctrl-g ctrl-b", "open or close git / files"),
    ("?", "this sheet — any key closes it"),
];

/// Floating cheatsheet over the focused pane.
///
/// Over that pane, not centred on the frame: the sheet is about the pane you
/// are standing in, and the keys differ per pane, so it has to say which one
/// it is describing by where it lands as well as by its title.
fn draw_help(f: &mut Frame, app: &App) {
    let theme = Theme::current();
    let (title, keys) = pane_keys(app.focus);
    let rows: Vec<(&str, &str)> = keys
        .iter()
        .copied()
        .chain(std::iter::once(("", "")))
        .chain(SHARED_KEYS.iter().copied())
        .collect();
    let key_w = rows.iter().map(|(k, _)| k.len()).max().unwrap_or(0);
    let w = rows
        .iter()
        .map(|(k, d)| key_w.max(k.len()) + 2 + d.len())
        .max()
        .unwrap_or(20) as u16
        + 2;
    let h = rows.len() as u16 + 2;
    let pane = match app.focus {
        Focus::Calls => app.call_area,
        Focus::PostIn => app.post_in_area,
        Focus::PostOut => app.post_out_area,
        Focus::Queue => app.queue_area,
        Focus::Git => app.git_area,
        Focus::Files => app.files_area,
        Focus::Meter => app.cache.area,
        _ => app.traj_area,
    };
    let full = f.area();
    let w = w.min(full.width);
    let h = h.min(full.height);
    // Centre on the pane, then push back inside the frame: a narrow side pane
    // holds a sheet wider than itself, and half of it would be off-screen.
    let x = pane
        .x
        .saturating_add(pane.width / 2)
        .saturating_sub(w / 2)
        .min(full.width.saturating_sub(w));
    let y = pane
        .y
        .saturating_add(pane.height / 2)
        .saturating_sub(h / 2)
        .min(full.height.saturating_sub(h));
    let area = Rect {
        x,
        y,
        width: w,
        height: h,
    };
    f.render_widget(Clear, area);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(theme.accent_user))
        .title(Span::styled(
            format!(" {title} keys "),
            Style::default()
                .fg(theme.accent_user)
                .add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    let inner = block.inner(area);
    f.render_widget(block, area);
    if inner.width == 0 || inner.height == 0 {
        return;
    }
    let lines: Vec<Line> = rows
        .iter()
        .map(|(k, d)| {
            Line::from(vec![
                Span::styled(
                    format!("{k:key_w$}  "),
                    Style::default().fg(theme.accent_success),
                ),
                Span::styled((*d).to_string(), Style::default().fg(theme.text_primary)),
            ])
        })
        .collect();
    f.render_widget(Paragraph::new(lines), inner);
}

/// The fuzzy file picker overlay (ctrl-t). A centered modal: query line, a
/// ranked result list, and a notice when the engine is absent or still
/// scanning. Reads only from the Picker's own worker state — paint, no IO.
fn draw_file_picker(f: &mut Frame, app: &mut App) {
    let theme = Theme::current();
    let full = f.area();
    let w = (full.width * 3 / 5).clamp(30, full.width.saturating_sub(2));
    let h = (full.height * 3 / 5).clamp(6, full.height.saturating_sub(2));
    let x = (full.width.saturating_sub(w)) / 2;
    let y = (full.height.saturating_sub(h)) / 2;
    let area = Rect {
        x,
        y,
        width: w,
        height: h,
    };
    f.render_widget(Clear, area);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(theme.accent_tool))
        .title(Span::styled(
            " find file (ctrl-t) ",
            Style::default()
                .fg(theme.accent_tool)
                .add_modifier(Modifier::BOLD),
        ));
    let inner = block.inner(area);
    f.render_widget(block, area);
    if inner.height < 2 {
        return;
    }
    // The result list gets the rows below the query line; clamp the scroll to it.
    app.file_picker
        .clamp(inner.height.saturating_sub(1) as usize);
    let mut lines: Vec<Line> = Vec::new();
    lines.push(Line::from(vec![
        Span::styled("> ", Style::default().fg(theme.accent_tool)),
        Span::styled(
            app.file_picker.query().to_string(),
            Style::default().fg(theme.text_primary),
        ),
    ]));
    if let Some(notice) = app.file_picker.notice() {
        lines.push(Line::from(Span::styled(
            notice.to_string(),
            Style::default().fg(theme.text_secondary),
        )));
    }
    let rows = inner.height.saturating_sub(1) as usize;
    let sel = app.file_picker.sel();
    let scroll = app.file_picker.scroll();
    for (i, path) in app
        .file_picker
        .results()
        .iter()
        .enumerate()
        .skip(scroll)
        .take(rows)
    {
        let shown = path.to_string_lossy().to_string();
        let style = if i == sel {
            Style::default()
                .fg(theme.accent_success)
                .add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(theme.text_primary)
        };
        let mark = if i == sel { "> " } else { "  " };
        lines.push(Line::from(vec![
            Span::styled(mark, Style::default().fg(theme.accent_success)),
            Span::styled(shown, style),
        ]));
    }
    f.render_widget(Paragraph::new(lines), inner);
}

fn slash_verdict(app: &App) -> slash::Verdict {
    if app.slash_paste_guard {
        slash::Verdict::NotACommand
    } else {
        slash::verdict(&app.prompt.to_send(), &app.picker)
    }
}

fn slash_popup_area(input: Rect, app: &App) -> Option<Rect> {
    let verdict = slash_verdict(app);
    if !app.slash.open && matches!(verdict, slash::Verdict::NotACommand) {
        return None;
    }
    let rows = app.slash.items.len().min(8);
    let note = !matches!(verdict, slash::Verdict::Ready | slash::Verdict::NotACommand);
    let h = rows as u16 + if note { 3 } else { 2 };
    if input.width < 12 || input.y < h {
        return None;
    }
    Some(Rect {
        x: input.x,
        y: input.y.saturating_sub(h),
        width: input.width.min(88),
        height: h,
    })
}

/// The completion list, above the composer, plus the verdict on what is
/// typed. The verdict is the point: a bad model id used to be discoverable
/// only by sending it and reading an error a step later.
fn draw_slash(f: &mut Frame, input: Rect, app: &App) {
    let theme = Theme::current();
    let verdict = slash_verdict(app);
    if !app.slash.open && matches!(verdict, slash::Verdict::NotACommand) {
        return;
    }
    let (mark, note, tone) = match &verdict {
        slash::Verdict::Ready => ("✓", String::new(), theme.accent_success),
        slash::Verdict::NeedsArg(help) => ("·", (*help).to_string(), theme.text_secondary),
        slash::Verdict::Unknown(what) => {
            ("✗", format!("no such command {what}"), theme.accent_user)
        }
        slash::Verdict::BadArg { got, expected } => (
            "✗",
            format!("{got} is not one of: {expected}"),
            theme.accent_user,
        ),
        slash::Verdict::NotACommand => ("", String::new(), theme.text_secondary),
    };
    let rows = app.slash.items.len().min(8);
    let Some(area) = slash_popup_area(input, app) else {
        return;
    };
    // The popup floats over the story pane. Without wiping the cells first the
    // text underneath shows through wherever a suggestion is shorter than the
    // box, which rendered model names with story prose spliced onto them.
    f.render_widget(Clear, area);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(tone))
        .title(Span::styled(
            format!(" {mark} commands "),
            Style::default().fg(tone).add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    let inner = block.inner(area);
    f.render_widget(block, area);
    if inner.width == 0 || inner.height == 0 {
        return;
    }
    let width = inner.width as usize;
    let mut lines: Vec<Line> = app
        .slash
        .items
        .iter()
        .take(rows)
        .enumerate()
        .map(|(i, item)| {
            let on = i == app.slash.sel;
            let body = if item.help.is_empty() {
                item.text.clone()
            } else {
                format!("{:<14} {}", item.text, item.help)
            };
            let mut body: String = body.chars().take(width).collect();
            // Pad to the full inner width: a short row otherwise leaves the
            // cells to its right holding whatever was painted before.
            let pad = width.saturating_sub(body.chars().count());
            body.push_str(&" ".repeat(pad));
            let mut line = Line::from(Span::styled(
                body,
                Style::default().fg(if on { tone } else { theme.text_primary }),
            ));
            if on {
                line = line.style(Style::default().bg(theme.bg_highlight));
            }
            line
        })
        .collect();
    if !note.is_empty() {
        lines.push(Line::from(Span::styled(
            note.chars().take(width).collect::<String>(),
            Style::default().fg(tone),
        )));
    }
    f.render_widget(Paragraph::new(lines), inner);
}

fn draw_paste_preview(f: &mut Frame, input: Rect, body: &str, on_chip: bool) {
    let theme = Theme::current();
    let shown: Vec<&str> = body.lines().take(8).collect();
    let extra = body.lines().count().saturating_sub(shown.len());
    let hint = if on_chip {
        "enter or double-click to expand"
    } else {
        "paste again or double-click to expand"
    };
    let h = (shown.len() as u16 + 3).min(input.y.max(3));
    if h < 3 || input.width < 8 {
        return;
    }
    let area = Rect {
        x: input.x,
        y: input.y.saturating_sub(h),
        width: input.width.min(88),
        height: h,
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(theme.paste_dim))
        .title(Span::styled(" paste ", Style::default().fg(theme.paste_fg)))
        .style(Style::default().bg(theme.paste_bg).fg(theme.paste_fg));
    let inner = block.inner(area);
    f.render_widget(block, area);
    let mut lines: Vec<Line> = shown
        .into_iter()
        .map(|l| {
            Line::from(Span::styled(
                l.to_string(),
                Style::default().fg(theme.paste_fg),
            ))
        })
        .collect();
    if extra > 0 {
        lines.push(Line::from(Span::styled(
            format!("… {extra} more"),
            Style::default().fg(theme.paste_dim),
        )));
    }
    lines.push(Line::from(Span::styled(
        hint,
        Style::default()
            .fg(theme.fuzzy_accent)
            .add_modifier(Modifier::BOLD),
    )));
    f.render_widget(Paragraph::new(lines), inner);
}

fn result_block(ev: &Value) -> RenderBlock {
    let tag = ev.get("tag").and_then(Value::as_str).unwrap_or("?");
    let body = ev.get("body").and_then(Value::as_str).unwrap_or("");
    let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
    let empty = json!({});
    let attrs = ev.get("attrs").unwrap_or(&empty);
    // The kernel located the edit at write time and says so on the done
    // event; that is the only source of the diff's line numbers.
    let line = ev.get("line").and_then(Value::as_u64);
    wire_syscall(tag, body, attrs, text, line)
}

/// Wire card for one `complete()` POST. Grok's Other header splits on `: `
/// so this paints **YOU** `POST #1`, not `complete  complete #1`.
fn wire_complete(
    origin: &str,
    n: u64,
    model: &str,
    thinking: &str,
    usage: &Value,
    thoughts: u64,
    redacted: u64,
) -> RenderBlock {
    let who = match origin {
        "user" => "YOU",
        _ => "MODEL",
    };
    // The header renders as bold <who> + the rest; pad the short label so
    // POST #n and the usage columns start at the same column on every row.
    let pad = " ".repeat("MODEL".len() - who.len());
    RenderBlock::ToolCall(ToolCallBlock::Other(
        OtherToolCallBlock::new(
            format!("{who}: {pad}POST #{n}"),
            format_usage_line(usage, thoughts, redacted),
        )
        .with_output(format_usage(model, thinking, usage, thoughts, redacted)),
    ))
}

/// The story's copy of an `<edit>` result, or `None` for any other syscall.
///
/// The story is otherwise a whitelist of narrative kinds and syscalls are the
/// wire pane's job — but an edit is the one call whose result *is* the
/// narrative. "It changed these four lines" is the sentence the prose is
/// about, and a work-run row reading `edit x3` cannot carry it. The card is
/// pushed collapsed, so a turn that rewrites twenty files costs twenty header
/// rows and not twenty diffs; `l` opens one, Enter zooms it into the viewer.
///
/// Wire card for one XML syscall: the body that ran, then the result.
///
/// `line` is the kernel's verdict from the edit `result` event: the 1-based
/// line where the unique match sat when the file was written. It is never
/// derived here — reading the file back would race the next edit. Absent
/// (failed edit, start phase, non-edit tags) the card carries no hunks and
/// therefore claims no line, honestly.
fn wire_syscall(
    tag: &str,
    body: &str,
    attrs: &Value,
    result: &str,
    line: Option<u64>,
) -> RenderBlock {
    let operation = syscall_operation(tag, attrs);
    match operation {
        exec @ ("python" | "bash" | "shell") => {
            let cmd = if body.trim().is_empty() {
                syscall_label(exec, attrs, operation != tag)
            } else {
                body.to_string()
            };
            // Folded rows only show the description, so carry the command
            // there — a bare `<bash>` is not a preview of anything.
            let preview = card_summary(&cmd);
            let desc = if preview.is_empty() {
                exec.to_string()
            } else {
                format!("{exec}  {preview}")
            };
            let mut block = ExecuteToolCallBlock::new(cmd)
                .with_description(desc)
                .with_output(format_result(result));
            if looks_failed(exec, result) {
                block = block.with_error(result.lines().next().unwrap_or("failed").to_string());
            }
            RenderBlock::ToolCall(ToolCallBlock::Execute(block))
        }
        "edit" => {
            let path = attrs
                .get("path")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let (old, new) = split_edit_body(body);
            let hunks = match line {
                Some(l) => diff_hunks_from_strings(&old, &new, l as usize),
                None => Vec::new(),
            };
            let label = if path.is_empty() {
                "edit".to_string()
            } else {
                path.clone()
            };
            let mut block = EditToolCallBlock::new(label, hunks);
            // looks_failed only knows bash/python exit conventions, so edit
            // needs its own read: the handler answers with a confirmation
            // line, and anything that opens like an error is a failure.
            if edit_failed(result) {
                block = block.with_error(first_line(result));
            }
            RenderBlock::ToolCall(ToolCallBlock::Edit(block))
        }
        _ => {
            let routed = operation != tag;
            let attrs_s = attr_summary(attrs, routed);
            let summary = card_summary(&{
                if !body.trim().is_empty() {
                    first_line(body)
                } else if !attrs_s.is_empty() {
                    attrs_s.clone()
                } else if let Some(summary) = structured_result_summary(result) {
                    summary
                } else {
                    first_line(result)
                }
            });
            let shown_result = format_result(result);
            let payload = match (body.trim().is_empty(), result.trim().is_empty()) {
                (true, _) => shown_result,
                (_, true) => body.to_string(),
                _ => format!("{body}\n\n→ {shown_result}"),
            };
            let target = card_summary(&attrs_s);
            let head = if routed {
                format!("{tag}: {operation}")
            } else if target.is_empty() {
                format!("{tag}: {summary}")
            } else {
                format!("{tag}: {target}")
            };
            let sub = if routed || !target.is_empty() {
                summary
            } else {
                String::new()
            };
            RenderBlock::ToolCall(ToolCallBlock::Other(
                OtherToolCallBlock::new(head, sub).with_output(payload),
            ))
        }
    }
}

/// An edit result that opens like an error message. The handler answers
/// "Edited <path>" (or similar) when it worked, so only the known failure
/// shapes count -- a confirmation must never be painted as a red card.
fn edit_failed(result: &str) -> bool {
    let t = result.trim_start();
    if t.is_empty() {
        return false;
    }
    let low = t.to_ascii_lowercase();
    low.starts_with("error")
        || low.starts_with("unknown tag")
        || low.starts_with("not a file")
        || low.starts_with("no match")
        || low.starts_with("traceback")
}

/// The edit tag body is `old\n---\nnew`, split on the first `---` that sits
/// alone on a line. A body with no separator is treated as a pure insertion so
/// the card still renders something truthful instead of an empty diff.
fn split_edit_body(body: &str) -> (String, String) {
    let mut before: Vec<&str> = Vec::new();
    let mut after: Vec<&str> = Vec::new();
    let mut seen = false;
    for line in body.split('\n') {
        if !seen && line.trim_end() == "---" {
            seen = true;
            continue;
        }
        if seen {
            after.push(line)
        } else {
            before.push(line)
        }
    }
    if !seen {
        return (String::new(), body.to_string());
    }
    (before.join("\n"), after.join("\n"))
}

fn syscall_label(tag: &str, attrs: &Value, routed: bool) -> String {
    let extra = attr_summary(attrs, routed);
    if extra.is_empty() {
        format!("<{tag}/>")
    } else {
        format!("<{tag} {extra}>")
    }
}

fn attr_summary(attrs: &Value, without_route: bool) -> String {
    match attrs {
        Value::Object(map) if !map.is_empty() => map
            .iter()
            .filter(|(k, _)| !without_route || !matches!(k.as_str(), "op" | "action"))
            .filter_map(|(k, v)| v.as_str().map(|s| format!("{k}=\"{s}\"")))
            .collect::<Vec<_>>()
            .join(" "),
        _ => String::new(),
    }
}

/// Pretty-print structured syscall output for Activity only. The transcript
/// keeps the exact result text the kernel produced.
pub(crate) fn format_result(result: &str) -> String {
    let Ok(value @ (Value::Object(_) | Value::Array(_))) = serde_json::from_str(result.trim())
    else {
        return result.to_string();
    };
    serde_json::to_string_pretty(&value).unwrap_or_else(|_| result.to_string())
}

/// A stable one-line sidebar summary for structured results. Detailed fields
/// remain in the expanded, pretty-printed body.
fn structured_result_summary(result: &str) -> Option<String> {
    match serde_json::from_str::<Value>(result.trim()).ok()? {
        Value::Array(items) => Some(format!("{} items", items.len())),
        Value::Object(fields) => {
            let kind = fields.get("type").and_then(Value::as_str);
            let n = fields.get("n").and_then(|v| match v {
                Value::String(s) => Some(s.clone()),
                Value::Number(n) => Some(n.to_string()),
                _ => None,
            });
            match (kind, n) {
                (Some(kind), Some(n)) => Some(format!("{kind} #{n}")),
                (Some(kind), None) => Some(kind.to_string()),
                (None, _) => Some(format!("{} fields", fields.len())),
            }
        }
        _ => None,
    }
}

/// One row of preview, never more.
///
/// A wrapped card header is what made the Activity pane look ragged. The
/// wrapped rows land back at the header's own column, so continuation text
/// reads as three new top-level entries rather than as more of the same row --
/// and for an execute card the command was then printed a *second* time
/// underneath as `$ cmd`, indented properly, which is what made the
/// inconsistency obvious. A header is a label. The body is where the full text
/// already lives.
const CARD_SUMMARY_W: usize = 64;

fn card_summary(text: &str) -> String {
    let flat = first_line(text)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    if flat.chars().count() <= CARD_SUMMARY_W {
        return flat;
    }
    flat.chars()
        .take(CARD_SUMMARY_W - 1)
        .collect::<String>()
        .trim_end()
        .to_string()
        + "\u{2026}"
}

fn first_line(text: &str) -> String {
    text.lines()
        .find(|l| !l.trim().is_empty())
        .unwrap_or("")
        .to_string()
}

fn looks_failed(tag: &str, result: &str) -> bool {
    let t = result.trim_start();
    t.contains("Traceback (most recent call last)")
        || t.starts_with("SyntaxError")
        || t.starts_with("NameError")
        || t.starts_with("TypeError")
        || (matches!(tag, "bash" | "shell")
            && (t.starts_with("exit ")
                || result.lines().any(|line| {
                    line.trim()
                        .strip_prefix("[exit ")
                        .and_then(|code| code.strip_suffix(']'))
                        .is_some_and(|code| code != "0")
                })))
}

fn format_usage_line(usage: &Value, thoughts: u64, redacted: u64) -> String {
    let get = |k: &str| usage.get(k).and_then(Value::as_u64).unwrap_or(0);
    let fresh = get("input_tokens");
    let read = get("cache_read_input_tokens");
    let out = get("output_tokens");
    let total = fresh + read + get("cache_creation_input_tokens");
    let hit = if total == 0 { 0 } else { 100 * read / total };
    // Labels left, counts right — the columns have to line up down the pane,
    // and ragged numbers were unreadable at a glance.
    format!("hit {hit:>3}%  in {fresh:>4}+{read:>6}  out {out:>5}  think {thoughts}/{redacted}")
}

fn format_usage(
    model: &str,
    thinking: &str,
    usage: &Value,
    thoughts: u64,
    redacted: u64,
) -> String {
    let get = |k: &str| usage.get(k).and_then(Value::as_u64).unwrap_or(0);
    let fresh = get("input_tokens");
    let read = get("cache_read_input_tokens");
    let write = get("cache_creation_input_tokens");
    let out = get("output_tokens");
    let total = fresh + read + write;
    let hit = if total == 0 { 0 } else { 100 * read / total };
    // OpenAI bills reasoning separately and often returns no summary to show
    // for it, so the block counts above can read 0 on a turn that spent most of
    // its output budget thinking. Print the tokens when the provider sends them.
    let reasoning = get("reasoning_tokens");
    let thought_line = if reasoning > 0 {
        format!("thinking {thoughts}  redacted {redacted}  reasoning {reasoning} tok")
    } else {
        format!("thinking {thoughts}  redacted {redacted}")
    };
    format!(
        "model {model}  effort {thinking}\n{thought_line}\nfresh in {fresh}  cache read {read}  cache write {write}  out {out}\ncache hit {hit}%"
    )
}

#[cfg(test)]
mod tests {

    #[test]
    fn a_chatgpt_plan_does_not_print_a_bill() {
        let mut app = App::new();
        handle_event(
            &mut app,
            serde_json::json!({"ev": "snapshot", "model": "gpt-5.6-luna", "billing": "plan"}),
        );
        assert!(
            app.cache.plan,
            "the bridge said plan, the meter must believe it"
        );
        // and list price for a gpt model is not opus price
        assert_eq!(model_price("gpt-5.6-luna"), (1.25, 10.0));
        assert_eq!(model_window("gpt-5.6-luna"), 400_000);

        app.cache.observe(
            &serde_json::json!({"input_tokens": 1_000_000, "output_tokens": 0}),
            "gpt-5.6-luna",
        );
        assert!((app.cache.spent - 1.25).abs() < 1e-9, "{}", app.cache.spent);

        let painted = paint(&mut app, 120, 36);
        assert!(
            painted.contains("plan"),
            "plan sessions say plan:\n{painted}"
        );
        assert!(
            painted.contains("at list"),
            "and label the figure as list price:\n{painted}"
        );
        assert!(
            !painted.contains("spent"),
            "never a bill on a subscription:\n{painted}"
        );

        handle_event(
            &mut app,
            serde_json::json!({"ev": "snapshot", "model": "claude-opus-5", "billing": "usage"}),
        );
        assert!(!app.cache.plan);
        assert!(paint(&mut app, 120, 36).contains("spent"));
    }

    #[test]
    fn the_picker_opens_from_a_real_ready_event() {
        let mut app = App::new();
        handle_event(
            &mut app,
            serde_json::json!({
                "ev": "ready",
                "model": "claude-opus-5",
                "thinking": "low",
                "onboarding": true,
                "current": serde_json::Value::Null,
                "providers": [
                    {"provider": "anthropic", "ok": true, "plan": "", "can_login": false,
                     "models": ["claude-opus-5"], "efforts": ["low", "high", "xhigh"]},
                    {"provider": "openai", "ok": false, "detail": "no credential", "can_login": true,
                     "models": ["gpt-5.6-sol", "gpt-5.6-luna"], "efforts": ["low", "high", "xhigh"]}
                ]
            }),
        );
        assert!(app.picker.open, "a fresh machine must land on the picker");
        let rows = app.picker.lines().join("\n");
        assert!(rows.contains("anthropic"), "{rows}");
        assert!(
            rows.contains("enter to sign in"),
            "unauthed provider must offer login:\n{rows}"
        );

        // an unauthed provider cannot be chosen: enter starts a login instead
        app.picker.sel = 1;
        assert_eq!(
            app.picker.key(KeyCode::Enter),
            picker::PickerAction::Login {
                provider: "openai".into()
            }
        );
        assert!(app.picker.open, "login must not close the picker");

        // signing in arrives as a picker event, and then the provider is usable
        handle_event(
            &mut app,
            serde_json::json!({
                "ev": "picker",
                "onboarding": true,
                "current": serde_json::Value::Null,
                "providers": [
                    {"provider": "anthropic", "ok": true, "can_login": false,
                     "models": ["claude-opus-5"], "efforts": ["low", "high", "xhigh"]},
                    {"provider": "openai", "ok": true, "plan": "pro", "can_login": true,
                     "models": ["gpt-5.6-sol", "gpt-5.6-luna"], "efforts": ["low", "high", "xhigh"]}
                ]
            }),
        );
        app.picker.sel = 1;
        assert_eq!(app.picker.key(KeyCode::Enter), picker::PickerAction::None);
        assert_eq!(app.picker.stage, picker::Stage::Model);
        app.picker.key(KeyCode::Char('j')); // luna
        app.picker.key(KeyCode::Enter);
        assert_eq!(app.picker.stage, picker::Stage::Effort);
        app.picker.key(KeyCode::Char('k')); // wrap to xhigh
        let done = app.picker.key(KeyCode::Enter);
        assert_eq!(
            done,
            picker::PickerAction::Apply {
                model: "gpt-5.6-luna".into(),
                effort: "xhigh".into()
            }
        );
        assert!(!app.picker.open, "choosing an effort closes the picker");
    }

    #[test]
    fn a_configured_session_does_not_reopen_the_picker() {
        let mut app = App::new();
        handle_event(
            &mut app,
            serde_json::json!({
                "ev": "ready",
                "model": "gpt-5.6-sol",
                "onboarding": false,
                "current": {"provider": "openai", "model": "gpt-5.6-sol", "effort": "high"},
                "providers": [
                    {"provider": "anthropic", "ok": true, "can_login": false,
                     "models": ["claude-opus-5"], "efforts": ["low", "high", "xhigh"]},
                    {"provider": "openai", "ok": true, "can_login": true,
                     "models": ["gpt-5.6-sol", "gpt-5.6-luna"], "efforts": ["low", "high", "xhigh"]}
                ]
            }),
        );
        assert!(
            !app.picker.open,
            "a configured session boots straight into the chat"
        );
        // ...and reopening points at what is already in use. This is the whole
        // contract: point_at only fires for an event carrying `current`, and a
        // model switch emits a snapshot, which has none — so opening the picker
        // used to highlight whatever was last chosen in it rather than what was
        // running.
        app.picker.open_for_change("gpt-5.6-sol", "high");
        assert_eq!(app.picker.current_provider().unwrap().name, "openai");
        assert_eq!(app.picker.effort_idx, 1);
        assert_eq!(app.picker.key(KeyCode::Esc), picker::PickerAction::Close);
        assert!(!app.picker.open);
    }

    use super::*;
    use ratatui::backend::TestBackend;
    use xai_grok_pager::glyphs;
    use xai_grok_pager::scrollback::DisplayMode;

    fn buffer_text(term: &Terminal<TestBackend>) -> String {
        let buf = term.backend().buffer();
        let area = buf.area();
        let mut out = String::new();
        for y in 0..area.height {
            for x in 0..area.width {
                out.push_str(buf[(x, y)].symbol());
            }
            out.push('\n');
        }
        out
    }

    fn row_of(text: &str, needle: &str) -> Option<usize> {
        text.lines().position(|l| l.contains(needle))
    }

    /// The POST cards are accounting, not content: one per turn, between the
    /// syscalls, and most of the pane. They stay off until the chip in the
    /// calls title is clicked, and clicking it again puts them away.
    #[test]
    fn the_post_rows_are_off_until_the_chip_is_clicked() {
        let mut app = App::new();
        seed_demo(&mut app);
        let text = paint(&mut app, 100, 40);
        let calls = rows_of(&text, app.call_area);
        assert!(
            !calls.contains("POST #"),
            "POST rows are on by default:\n{calls}"
        );
        assert!(
            calls.contains("[+posts]"),
            "no switch in the title:\n{calls}"
        );

        let chip = app.calls_chip.expect("the chip has no hit box");
        handle_mouse(
            &mut app,
            click(MouseEventKind::Down(MouseButton::Left), chip.x, chip.y),
        );
        let text = paint(&mut app, 100, 40);
        let calls = rows_of(&text, app.call_area);
        assert!(
            calls.contains("POST #"),
            "the chip did not put them back:\n{calls}"
        );
        assert!(calls.contains("[-posts]"), "{calls}");

        handle_mouse(
            &mut app,
            click(MouseEventKind::Down(MouseButton::Left), chip.x, chip.y),
        );
        let text = paint(&mut app, 100, 40);
        let calls = rows_of(&text, app.call_area);
        assert!(
            !calls.contains("POST #"),
            "the chip is not a toggle:\n{calls}"
        );
    }

    /// A hidden POST card is held, not dropped: it goes back in front of the
    /// same syscall it opened. Two POSTs in a row share the card they were
    /// pushed after, which is the case that reversed them.
    #[test]
    fn showing_the_posts_again_restores_the_pane_exactly() {
        let mut app = App::new();
        app.show_posts = true;
        seed_demo(&mut app);
        let before = rows_of(&paint(&mut app, 100, 40), app.call_area);
        assert!(before.contains("POST #"), "{before}");

        app.toggle_posts();
        let hidden = rows_of(&paint(&mut app, 100, 40), app.call_area);
        assert!(!hidden.contains("POST #"), "{hidden}");

        app.toggle_posts();
        let after = rows_of(&paint(&mut app, 100, 40), app.call_area);
        assert_eq!(before, after, "a POST card came back in the wrong place");
    }

    /// A fully collapsed POST leaves no spacer between story and composer.
    #[test]
    fn collapsed_post_gives_its_gap_row_back_to_story() {
        let mut app = App::new();
        app.layout.post_h = 0;
        let text = paint(&mut app, 100, 30);
        let rows: Vec<&str> = text.lines().collect();

        assert_eq!(app.post_in_area.height, 0);
        assert_eq!(
            app.traj_area.y + app.traj_area.height,
            app.input_area.y,
            "a layout row survived between story and composer",
        );
        let top = app.input_area.y as usize;
        let left: String = rows[top]
            .chars()
            .take(app.input_area.width as usize)
            .collect();
        assert!(
            left.trim_start().starts_with('\u{250c}'),
            "the collapsed POST left a blank row above the composer: {left:?}",
        );
    }

    /// The rule the composer follows for a collapsed POST is the group's, not
    /// the composer's: an open queue is the top card, so it is the one that has
    /// to give the float row back. It did not, and a collapsed POST left the
    /// story's bottom border and then a blank spacer above the queue.
    #[test]
    fn a_collapsed_post_leaves_no_spacer_above_the_queue() {
        let mut app = App::new();
        app.layout.post_h = 0;
        app.queue.push("a follow-up".into());
        let text = paint(&mut app, 100, 30);
        let rows: Vec<&str> = text.lines().collect();

        assert_eq!(
            app.traj_area.y + app.traj_area.height,
            app.queue_area.y,
            "a layout row survived between story and queue",
        );
        let top = app.queue_area.y as usize;
        let left: String = rows[top]
            .chars()
            .take(app.queue_area.width as usize)
            .collect();
        assert!(
            left.trim_start().starts_with('\u{250c}'),
            "blank row above the queue with POST collapsed: {left:?}",
        );
    }

    /// The queue and the composer are one stack of cards, not two panes with
    /// a gap. The composer floats on a blank row above it, and when the queue
    /// is open that float belongs to the
    /// queue, so the two boxes touch and share a left edge. It used to be the
    /// composer's own, which put the blank row *between* them and left the
    /// queue running a column wider on both sides.
    #[test]
    fn the_queue_and_the_composer_are_one_stack() {
        let mut app = App::new();
        app.queue.push("first follow-up".into());
        app.queue.push("second follow-up".into());
        let text = paint(&mut app, 100, 30);
        let rows: Vec<&str> = text.lines().collect();

        let last = app.queue_area.y as usize + app.queue_area.height as usize - 1;
        assert!(
            rows[last].trim_start().starts_with('\u{2514}'),
            "queue_area does not end on the box's bottom edge: {:?}",
            rows[last]
        );
        let below = rows[last + 1];
        assert!(
            below.trim_start().starts_with('\u{250c}'),
            "a blank row sits between the queue and the composer: {below:?}"
        );
        assert_eq!(
            rows[last].find('\u{2514}'),
            below.find('\u{250c}'),
            "the queue and the composer do not share a left edge",
        );
        // The slot lost the float row, so it must lose the height too, or the
        // composer draws one blank row of its own instead.
        assert_eq!(
            app.input_area.height, 10,
            "the composer kept a float row it no longer draws: {:?}",
            app.input_area
        );

        // With no queue the float is still there -- the composer is the top
        // card of the group then, and it is what separates it from the story.
        let mut bare = App::new();
        let text = paint(&mut bare, 100, 30);
        let rows: Vec<&str> = text.lines().collect();
        let top = bare.input_area.y as usize;
        // Only the story column: the wire column paints its own panes there.
        let left: String = rows[top]
            .chars()
            .take(bare.input_area.width as usize)
            .collect();
        assert!(
            left.trim().is_empty(),
            "the composer lost its float row: {left:?}"
        );
        assert!(
            rows[top + 1].trim_start().starts_with('\u{250c}'),
            "{:?}",
            rows[top + 1]
        );
    }

    pub(crate) fn paint(app: &mut App, w: u16, h: u16) -> String {
        let backend = TestBackend::new(w, h);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, app)).unwrap();
        buffer_text(&term)
    }

    fn paint_input_state(
        app: &mut App,
        w: u16,
        h: u16,
    ) -> (String, ratatui::style::Color, Modifier) {
        let backend = TestBackend::new(w, h);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, app)).unwrap();
        let x = app.input_inner.x.saturating_sub(1);
        let y = app.input_inner.y.saturating_sub(1);
        let cell = &term.backend().buffer()[(x, y)];
        (buffer_text(&term), cell.fg, cell.modifier)
    }
    /// Prose after a syscall must not push its own timestamp onto a blank row.
    ///
    /// The stream is `<bash>…</bash>\n\nI'll wait.`, and stripping the call
    /// leaves the newlines. grok overlays the stamp on a block's *first*
    /// content line, so an empty first line orphans the stamp one row above
    /// the sentence -- four rows for one line of prose, which is what the
    /// story actually looked like.
    #[test]
    fn a_reply_after_a_call_starts_on_its_first_row() {
        assert_eq!(spoken_prefix("<bash>ls</bash>\n\nI'll wait."), "I'll wait.");

        let mut app = App::new();
        app.ready = true;
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        for i in 0..3 {
            handle_event(
                &mut app,
                json!({"ev": "speech", "delta": true,
                "text": format!("<bash>ls</bash>\n\nWaiting on {i}.")}),
            );
            handle_event(
                &mut app,
                json!({"ev": "result", "tag": "bash", "text": "ok"}),
            );
        }
        let text = paint(&mut app, 90, 40);
        let first = row_of(&text, "Waiting on 0.").expect("prose on screen");
        let stamp = text
            .lines()
            .position(|l| l.contains("AM") || l.contains("PM"))
            .unwrap_or(first);
        assert_eq!(
            stamp, first,
            "the stamp belongs on the sentence's own row:\n{text}"
        );
        // Three one-line replies, three rows of prose, and the gaps between.
        let last = row_of(&text, "Waiting on 2.").expect("last reply on screen");
        assert!(
            last - first <= 4,
            "three one-line replies must not span {} rows:\n{text}",
            last - first + 1
        );
    }

    /// A streaming thought is body, and nothing but body.
    ///
    /// It used to open with three rows of chrome: a "Thinking…" header, the
    /// blank separator the header always drags with it, and grok's "…" marking
    /// the clipped head. The animated composer already signals inference, so
    /// a second live header was a copy and its blank was
    /// waste; the marker only reads as a marker under that header. So a live
    /// thought streams Expanded -- whole body, no chrome, the pane's bottom edge
    /// doing the clipping, which is how grok minimal draws its live tail -- and
    /// collapses to a single "Thought for Xs" row the moment it ends.
    #[test]
    fn a_streaming_thought_does_not_repeat_the_status_row() {
        let mut app = App::new();
        app.ready = true;
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        for i in 0..8 {
            handle_event(
                &mut app,
                json!({
                "ev": "thinking", "delta": true,
                "text": format!("reasoning line number {i} with enough words to fill a row\n"),
                }),
            );
        }
        let text = paint(&mut app, 70, 30);
        assert!(
            !text.contains("Thinking"),
            "the header is the status row's job:\n{text}"
        );
        assert!(
            !text.contains('\u{2026}'),
            "no marker: the body is all there is:\n{text}"
        );
        assert!(
            text.contains("number 7"),
            "the newest reasoning must be on screen:\n{text}"
        );
        // Truncated mode could never show more than three body rows. Count the
        // thought's accent-column rows rather than source newlines: wrapping
        // can place two source lines on one painted row.
        let rows = text.lines().filter(|l| l.contains('\u{2503}')).count();
        assert!(
            rows > 3,
            "a live thought renders its body, not a 3-row window ({rows}):\n{text}"
        );

        // One row, labelled, the moment it stops.
        handle_event(
            &mut app,
            json!({"ev": "speech", "delta": true, "text": "Answer.\n"}),
        );
        let text = paint(&mut app, 70, 30);
        assert!(
            text.contains("Thought for"),
            "a finished thought must still say what it was:\n{text}"
        );
        assert!(
            !text.contains("number 3"),
            "a finished thought is one collapsed row:\n{text}"
        );
    }

    /// Driven through handle_event, because the row is only worth anything if
    /// the real event path builds it. The thoughts must be gone: two rows
    /// saying the same thing is what this replaced.
    /// The row is live: it appears while the run is happening, not when the
    /// answer starts. Watching a stack of collapsed thoughts and nothing else
    /// for a minute is exactly the silence the row exists to fill.
    #[test]
    fn the_work_row_appears_before_the_answer_does() {
        let mut app = App::new();
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        let row = |app: &App| {
            (0..app.sess.story.len()).find_map(|i| {
                match app.sess.story.entry(i).map(|e| &e.block) {
                Some(RenderBlock::System(b)) => Some(b.text.clone()),
                _ => None,
                }
            })
        };
        handle_event(
            &mut app,
            json!({"ev": "thinking", "delta": true, "text": "planning\n"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "result", "tag": "bash", "body": "cargo build", "text": "ok"}),
        );
        assert!(
            row(&app).is_none(),
            "one call is not a run: {:?}",
            row(&app)
        );

        handle_event(
            &mut app,
            json!({"ev": "thinking", "delta": true, "text": "reading\n"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "result", "tag": "bash", "body": "cargo test", "text": "ok"}),
        );
        let mid = row(&app).expect("the row must exist mid-run, before any speech");
        assert!(
            mid.contains("bash"),
            "mid-run row says nothing about the work: {mid}"
        );

        // A third call rewrites the same row rather than stacking a second.
        handle_event(
            &mut app,
            json!({"ev": "result", "tag": "read", "body": "main.rs", "text": "ok"}),
        );
        let rows = (0..app.sess.story.len())
            .filter(|i| {
                matches!(
                    app.sess.story.entry(*i).map(|e| &e.block),
                    Some(RenderBlock::System(_))
                )
            })
            .count();
        assert_eq!(rows, 1, "the run must own exactly one row");
        let grown = row(&app).unwrap();
        assert!(
            grown.contains("read"),
            "the row did not grow with the run: {grown}"
        );
        assert_ne!(
            mid, grown,
            "the row must be rewritten, not frozen at its first shape"
        );
    }

    #[test]
    fn work_summary_navigation_click_enter_and_viewer_controls_are_wired() {
        let mut app = App::new();
        app.ready = true;
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        app.story_push(RenderBlock::system("ordinary status"));
        handle_event(
            &mut app,
            json!({"ev": "result", "tag": "python", "body": "first()", "text": "ok"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "result", "tag": "read", "attrs": {"path": "main.rs"}, "text": "ok"}),
        );
        let idx = (0..app.sess.story.len())
            .find(|i| {
                app.sess
                    .story
                    .entry(*i)
                    .is_some_and(|entry| app.sess.stream.run.detail(entry.id).is_some())
            })
            .expect("work summary row");
        let ordinary = (0..app.sess.story.len())
            .find(|i| {
                matches!(
                    app.sess.story.entry(*i).map(|entry| &entry.block),
                    Some(RenderBlock::System(system)) if system.text == "ordinary status"
                )
            })
            .expect("ordinary system row");
        let id = app.sess.story.entry(idx).unwrap().id;
        let detail = app.sess.stream.run.detail(id).expect("archived detail");
        assert!(detail.contains("1. python"), "{detail}");
        assert!(detail.contains("2. read → main.rs"), "{detail}");

        app.set_focus(Focus::Story);
        let _ = paint(&mut app, 100, 40);
        let area = app.traj_area;
        let point = (area.y..area.bottom())
            .find_map(|row| {
                (area.x..area.right()).find_map(|col| {
                    let block = app.sess.story_sel.hit_test_visible_block(col, row)?;
                    (block.entry_idx == idx).then_some((col, row))
                })
            })
            .expect("summary block has no clickable cell");
        handle_scrollback_down(&mut app, false, point.0, point.1);
        assert_eq!(
            app.sess.story.selected(),
            Some(idx),
            "single click must select the work summary"
        );

        handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char('k'), KeyModifiers::NONE),
        )
        .unwrap();
        assert_ne!(
            app.sess.story.selected(),
            Some(ordinary),
            "k must skip an ordinary system row"
        );
        handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char('j'), KeyModifiers::NONE),
        )
        .unwrap();
        assert_eq!(
            app.sess.story.selected(),
            Some(idx),
            "j must navigate back onto the selectable work summary"
        );

        handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        )
        .unwrap();
        assert_eq!(
            app.viewer.as_ref().map(|viewer| viewer.kind),
            Some(ViewerKind::PlainText)
        );
        let popup = paint(&mut app, 100, 40);
        assert!(popup.contains("search"), "{popup}");
        assert!(popup.contains("wrap"), "{popup}");
        {
            let viewer = app.viewer.as_mut().expect("plain text viewer");
            assert!(viewer.handle_key(&KeyEvent::new(KeyCode::Char('/'), KeyModifiers::NONE,)));
            assert!(viewer.handle_key(&KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE,)));
            assert!(viewer.handle_key(&KeyEvent::new(KeyCode::Char('w'), KeyModifiers::NONE,)));
            assert!(viewer.handle_key(&KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE,)));
        }

        handle_viewer_key(
            &mut app,
            KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE),
        );
        assert!(app.viewer.is_some(), "scrolling closed the detail popup");
        handle_viewer_key(&mut app, KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(
            app.viewer.is_none(),
            "escape did not close the detail popup"
        );
        assert_eq!(app.focus, Focus::Story, "popup close changed pane focus");
    }

    #[test]
    fn work_summary_double_click_opens_the_detail_popup() {
        let mut app = App::new();
        app.ready = true;
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        handle_event(
            &mut app,
            json!({"ev": "result", "tag": "python", "text": "ok"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "result", "tag": "read", "attrs": {"path": "main.rs"}, "text": "ok"}),
        );
        let idx = (0..app.sess.story.len())
            .find(|i| {
                matches!(
                app.sess.story.entry(*i).map(|entry| &entry.block),
                Some(RenderBlock::System(_))
                )
            })
            .expect("work summary row");
        let _ = paint(&mut app, 100, 40);
        let area = app.traj_area;
        let point = (area.y..area.bottom())
            .find_map(|row| {
            (area.x..area.right()).find_map(|col| {
                let block = app.sess.story_sel.hit_test_visible_block(col, row)?;
                (block.entry_idx == idx
                    && app.sess.story_sel.hit_test_text_exact(col, row).is_none())
                    .then_some((col, row))
            })
            })
            .expect("summary block has no clickable non-text cell");

        app.focus = Focus::Story;
        handle_scrollback_down(&mut app, false, point.0, point.1);
        handle_scrollback_down(&mut app, false, point.0, point.1);
        assert_eq!(
            app.viewer.as_ref().map(|viewer| viewer.kind),
            Some(ViewerKind::PlainText),
            "double-click did not open the archived activity detail",
        );
    }

    #[test]
    fn invisible_work_folds_into_one_row_above_the_prose() {
        let mut app = App::new();
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        handle_event(
            &mut app,
            json!({"ev": "thinking", "delta": true, "text": "planning the change\n"}),
        );
        for (tag, body) in [
            ("bash", "cd /tmp && cargo build"),
            ("bash", "cd /tmp && cargo test"),
        ] {
            handle_event(
                &mut app,
                json!({"ev": "result", "tag": tag, "body": body, "text": "ok"}),
            );
        }
        handle_event(
            &mut app,
            json!({"ev": "speech", "delta": true, "text": "Done, 111 green."}),
        );
        let _ = paint(&mut app, 120, 34);

        let kinds: Vec<&str> = (0..app.sess.story.len())
            .filter_map(|i| {
                app.sess.story.entry(i).map(|e| match &e.block {
                RenderBlock::Thinking(_) => "Thinking",
                RenderBlock::System(_) => "System",
                RenderBlock::AgentMessage(_) => "AgentMessage",
                _ => "other",
                })
            })
            .collect();
        assert_eq!(
            kinds,
            vec!["System", "AgentMessage"],
            "the run should be one row, then the prose: {kinds:?}"
        );
        let row = (0..app.sess.story.len())
            .find_map(|i| match app.sess.story.entry(i).map(|e| &e.block) {
                Some(RenderBlock::System(b)) => Some(b.text.clone()),
                _ => None,
            })
            .expect("work row");
        assert!(
            row.contains("bash \u{00d7}2"),
            "calls not compressed: {row}"
        );
        assert!(row.contains("thought"), "thinking not folded in: {row}");
        assert!(!row.contains("cargo build"), "a command body leaked: {row}");
    }

    /// The demo keeps its edit in Activity, where it opens into readable
    /// before/after rows beside the wire group counter.
    #[test]
    fn the_demo_paints_an_activity_edit_and_a_group_counter() {
        let mut app = App::new();
        seed_demo(&mut app);
        // The POST rows are off by default; this test is about them, so put
        // them back the way the chip does.
        app.toggle_posts();

        assert_eq!(
            activity_edits(&app).len(),
            1,
            "demo edit missing from Activity"
        );
        assert!(
            !(0..app.sess.story.len()).any(|i| matches!(
                app.sess.story.entry(i).map(|e| &e.block),
                Some(RenderBlock::ToolCall(ToolCallBlock::Edit(_)))
            )),
            "demo edit leaked into Story"
        );
        let painted = paint(&mut app, 120, 40);
        assert!(
            painted.contains("Activity  #3/3"),
            "the wire title lost its group counter: {painted}"
        );
    }

    /// Two POSTs, each with syscalls under it. `]` walks forward to the next
    /// group head, `[` back, and neither wraps past the ends.
    #[test]
    fn brackets_step_the_wire_through_post_groups() {
        let mut app = App::new();
        app.show_posts = true;
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        for n in 1..=3u64 {
            handle_event(&mut app, json!({"ev": "complete", "n": n, "origin": "llm"}));
            handle_event(
                &mut app,
                json!({"ev": "result", "tag": "bash", "body": "cargo test", "text": "ok"}),
            );
        }
        let _ = paint(&mut app, 120, 34);
        assert_eq!(app.sess.posts.len(), 3, "one group per POST");

        let starts: Vec<usize> = app.sess.posts.starts(&app.sess.calls);

        // A painted pane already has a cursor, so clear it to reach the
        // no-selection path: forward from nowhere lands on the first group.
        app.sess.calls.set_selected(None);
        assert!(app.select_call_group(true));
        assert_eq!(app.sess.calls.selected(), Some(starts[0]));
        assert!(app.select_call_group(true));
        assert_eq!(app.sess.calls.selected(), Some(starts[1]));
        assert!(app.select_call_group(true));
        assert_eq!(app.sess.calls.selected(), Some(starts[2]));
        // Past the last group there is nowhere to go, and the selection holds.
        assert!(!app.select_call_group(true), "forward wrapped off the end");
        assert_eq!(app.sess.calls.selected(), Some(starts[2]));

        assert!(app.select_call_group(false));
        assert_eq!(app.sess.calls.selected(), Some(starts[1]));
        assert!(app.select_call_group(false));
        assert_eq!(app.sess.calls.selected(), Some(starts[0]));
        assert!(!app.select_call_group(false), "back wrapped off the start");
        assert_eq!(app.sess.calls.selected(), Some(starts[0]));
    }

    /// A child session runs its own POSTs, so it needs its own group index.
    /// Stepping inside the child must not reach into the parent's wire, and
    /// the counter has to follow whichever session is on screen.
    #[test]
    fn a_child_session_walks_its_own_groups() {
        let mut app = App::new();
        app.show_posts = true;
        // Parent: two POSTs of its own, to prove the child does not see them.
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        for n in 1..=2u64 {
            handle_event(&mut app, json!({"ev": "complete", "n": n, "origin": "llm"}));
        }
        handle_event(
            &mut app,
            json!({
                "ev": "subagent", "phase": "started", "id": "deadbeef",
                "agent": "explore", "persona": "researcher",
                "task": "find cache notes", "model": "claude-opus-5",
            }),
        );
        for n in 1..=3u64 {
            handle_event(
                &mut app,
                json!({
                    "ev": "child", "id": "deadbeef", "kind": "complete",
                    "n": n, "origin": "llm", "model": "claude-opus-5",
                    "thinking": "low", "usage": {}, "thoughts": 0, "redacted": 0,
                }),
            );
        }

        assert_eq!(app.sess.posts.len(), 2, "parent groups");
        assert_eq!(
            app.children["deadbeef"].sess.posts.len(),
            3,
            "the child's POSTs did not open groups of their own",
        );

        // Looking at the parent, the counter is the parent's.
        let _ = paint(&mut app, 120, 34);
        assert_eq!(app.call_group_pos(), Some((2, 2)));

        // Enter the child: the counter and the step both switch with it.
        app.viewing = Some("deadbeef".to_string());
        let _ = paint(&mut app, 120, 34);
        assert_eq!(
            app.call_group_pos(),
            Some((3, 3)),
            "the counter stayed on the parent inside a child session",
        );

        let child_starts: Vec<usize> = app.children["deadbeef"]
            .sess
            .posts
            .starts(&app.children["deadbeef"].sess.calls);
        // The parent was painted, so it already carries a cursor. Whatever it
        // is, a step taken inside the child must leave it exactly there.
        let parent_sel = app.sess.calls.selected();
        app.children
            .get_mut("deadbeef")
            .unwrap()
            .sess
            .calls
            .set_selected(None);
        assert!(app.select_call_group(true));
        assert_eq!(
            app.children["deadbeef"].sess.calls.selected(),
            Some(child_starts[0]),
            "the step moved something other than the child's wire",
        );
        assert_eq!(
            app.sess.calls.selected(),
            parent_sel,
            "stepping inside a child moved the parent's wire cursor",
        );
    }

    /// `[`/`]` step from a card in the middle of a group to that group's
    /// neighbours, not to the card's own neighbours.
    #[test]
    fn a_group_step_jumps_from_mid_group_to_the_next_head() {
        let mut app = App::new();
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        for n in 1..=2u64 {
            handle_event(&mut app, json!({"ev": "complete", "n": n, "origin": "llm"}));
            for _ in 0..3 {
                handle_event(
                    &mut app,
                    json!({"ev": "result", "tag": "bash", "body": "x", "text": "ok"}),
                );
            }
        }
        let _ = paint(&mut app, 120, 34);
        let starts: Vec<usize> = app.sess.posts.starts(&app.sess.calls);

        // Land inside group 1, two cards past its head.
        app.sess.calls.set_selected(Some(starts[0] + 2));
        assert!(app.select_call_group(true));
        assert_eq!(
            app.sess.calls.selected(),
            Some(starts[1]),
            "a group step behaved like a plain cursor move",
        );
    }

    /// The title reports which group the cursor is in, so the step is visible.
    #[test]
    fn the_wire_title_counts_groups() {
        let mut app = App::new();
        app.show_posts = true;
        assert_eq!(app.call_group_pos(), None, "no POSTs, no counter");

        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        for n in 1..=3u64 {
            handle_event(&mut app, json!({"ev": "complete", "n": n, "origin": "llm"}));
        }
        let _ = paint(&mut app, 120, 34);
        // Nothing selected: the counter names the newest group, which is the
        // one the tail belongs to.
        assert_eq!(app.call_group_pos(), Some((3, 3)));

        app.select_call_group(false);
        app.select_call_group(false);
        assert_eq!(app.call_group_pos(), Some((1, 3)), "counter did not follow");
    }

    /// `/reset` clears the wire, so the group index has to go with it or the
    /// stale ids leave the counter claiming groups that are gone.
    #[test]
    fn reset_drops_the_group_index() {
        let mut app = App::new();
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        handle_event(&mut app, json!({"ev": "complete", "n": 1, "origin": "llm"}));
        assert_eq!(app.sess.posts.len(), 1);

        app.prompt = prompt::PromptBuf::new();
        for c in "/reset".chars() {
            app.prompt.insert_char(c);
        }
        let _ = submit_prompt(None, &mut app);
        assert!(app.sess.posts.is_empty(), "group index survived /reset");
        assert_eq!(app.call_group_pos(), None);
    }

    fn edit_ev(path: &str, old: &str, new: &str, result: &str) -> Value {
        json!({
            "ev": "result",
            "tag": "edit",
            "attrs": {"path": path},
            "body": format!("{old}\n---\n{new}"),
            "text": result,
        })
    }

    fn activity_edits(app: &App) -> Vec<usize> {
        (0..app.sess.calls.len())
            .filter(|i| {
                matches!(
                    app.sess.calls.entry(*i).map(|e| &e.block),
                    Some(RenderBlock::ToolCall(ToolCallBlock::Edit(_)))
                )
            })
            .collect()
    }

    #[test]
    fn edits_render_only_in_activity() {
        let mut app = App::new();
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        handle_event(
            &mut app,
            edit_ev("desmos/loop.py", "if old:", "if new:", "ok"),
        );

        assert!(
            !(0..app.sess.story.len()).any(|i| matches!(
                app.sess.story.entry(i).map(|e| &e.block),
                Some(RenderBlock::ToolCall(ToolCallBlock::Edit(_)))
            )),
            "edit diff leaked into Story"
        );
        assert_eq!(activity_edits(&app).len(), 1, "edit missing from Activity");
    }

    #[test]
    fn edits_do_not_create_story_work_rows() {
        let mut app = App::new();
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        for f in ["a.rs", "b.rs", "c.rs"] {
            handle_event(&mut app, edit_ev(f, "old", "new", "ok"));
        }
        let row = (0..app.sess.story.len()).find_map(|i| {
            match app.sess.story.entry(i).map(|e| &e.block) {
            Some(RenderBlock::System(b)) => Some(b.text.clone()),
            _ => None,
            }
        });
        assert!(row.is_none(), "edits created a Story work row: {row:?}");
        assert_eq!(activity_edits(&app).len(), 3, "one Activity card per edit");
    }

    #[test]
    fn failed_edits_stay_in_activity_too() {
        let mut app = App::new();
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        handle_event(
            &mut app,
            edit_ev("f.rs", "missing", "new", "no match for old_string"),
        );
        assert_eq!(activity_edits(&app).len(), 1, "failed edit vanished");
        assert!(
            !(0..app.sess.story.len()).any(|i| matches!(
                app.sess.story.entry(i).map(|e| &e.block),
                Some(RenderBlock::ToolCall(ToolCallBlock::Edit(_)))
            )),
            "failed edit leaked into Story"
        );
    }

    /// One call is not a run. A row for a lone grep is worse than silence.
    #[test]
    fn a_single_call_leaves_the_story_alone() {
        let mut app = App::new();
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        handle_event(
            &mut app,
            json!({"ev": "thinking", "delta": true, "text": "one look\n"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "result", "tag": "grep", "body": "pattern", "text": "hit"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "speech", "delta": true, "text": "Found it."}),
        );
        let _ = paint(&mut app, 120, 34);
        let story_thoughts = (0..app.sess.story.len())
            .filter(|i| {
                matches!(
                    app.sess.story.entry(*i).map(|e| &e.block),
                    Some(RenderBlock::Thinking(_))
                )
            })
            .count();
        let activity_thoughts = (0..app.sess.calls.len())
            .filter(|i| {
                matches!(
                    app.sess.calls.entry(*i).map(|e| &e.block),
                    Some(RenderBlock::Thinking(_))
                )
            })
            .count();
        assert_eq!(story_thoughts, 0, "thinking leaked into Story");
        assert_eq!(
            activity_thoughts, 1,
            "a lone call must not fold the Activity thought away"
        );
    }

    /// The composer belongs to the story column. The wire column used to stop
    /// short of the bottom to hold a key legend opposite it; that band is the
    /// calls pane's now, so the wire column runs to the last row.
    #[test]
    fn the_composer_sits_under_the_story_column_only() {
        let mut app = App::new();
        let text = paint(&mut app, 140, 34);
        assert_eq!(app.input_area.x, app.traj_area.x, "composer left edge");
        assert_eq!(app.input_area.width, app.traj_area.width, "composer width");
        let top = app.input_area.y + input_float_rows(&app);
        let row = text.lines().nth(top as usize).unwrap_or_default();
        assert_eq!(
            row.find('\u{250c}'),
            Some(app.traj_area.x as usize),
            "painted composer frame is inset from Story: {row:?}"
        );
        assert!(
            app.input_area.width + 20 < 140,
            "composer still spans the frame: {:?}",
            app.input_area
        );
        assert_eq!(
            app.cache.area.y + app.cache.area.height,
            34,
            "meta must be the bottom-right pane: {:?}",
            app.cache.area
        );
        assert!(!text.contains("keys "), "the key legend is gone:\n{text}");
    }

    /// The failure this guards: a command body streamed into the story one
    /// delta at a time, was appended to a live block, and stayed there when
    /// the closer finally arrived. Checking only the final story misses it --
    /// the leak is committed long before the turn ends. So assert after every
    /// delta, the way the user actually watches it.
    #[test]
    fn a_streaming_call_never_flashes_its_body_into_the_story() {
        let mut app = App::new();
        let lt = '<';
        let gt = '>';
        let deltas = [
            "Checking the repo.\n\n".to_string(),
            format!("{lt}ba"),
            "sh".to_string(),
            format!("{gt}cd /tmp && echo "),
            "\"HEAD=$(git rev-parse HEAD)\"".to_string(),
            "; wc -l *.rs".to_string(),
            format!("{lt}/ba"),
            format!("sh{gt}"),
            "\n\nDone.".to_string(),
        ];
        for (n, d) in deltas.iter().enumerate() {
            handle_event(&mut app, json!({"ev": "speech", "delta": true, "text": d}));
            let story: String = (0..app.sess.story.len())
                .filter_map(|i| match app.sess.story.entry(i).map(|e| &e.block) {
                    Some(RenderBlock::AgentMessage(m)) => Some(m.text()),
                    _ => None,
                })
                .collect();
            for leak in ["cd /tmp", "git rev-parse", "wc -l", "HEAD="] {
                assert!(
                    !story.contains(leak),
                    "delta {n} leaked {leak:?} into the story: {story:?}"
                );
            }
        }
        handle_event(&mut app, json!({"ev": "complete", "n": 1}));
        let story: String = (0..app.sess.story.len())
            .filter_map(|i| match app.sess.story.entry(i).map(|e| &e.block) {
                Some(RenderBlock::AgentMessage(m)) => Some(m.text()),
                _ => None,
            })
            .collect();
        assert!(
            story.contains("Checking the repo."),
            "prose lost: {story:?}"
        );
        assert!(
            story.contains("Done."),
            "prose after the call lost: {story:?}"
        );
        assert!(!story.contains("bash"), "tag name leaked: {story:?}");
        // And still absent once the closer has landed: holding the body during
        // the stream is worthless if the finished block prints it anyway.
        for leak in ["cd /tmp", "git rev-parse", "wc -l", "HEAD="] {
            assert!(
                !story.contains(leak),
                "final story leaked {leak:?}: {story:?}"
            );
        }
    }

    /// Every syscall shape the model actually emits, gone from the story --
    /// attributes, self-closing markers and mid-sentence calls included. The
    /// calls pane already renders each as a card; a second copy as prose is
    /// noise the reader did not ask for.
    #[test]
    fn no_syscall_shape_reaches_the_story() {
        let lt = '<';
        let gt = '>';
        for (src, want) in [
            (
                format!("prose one\n{lt}bash{gt}rm -rf /tmp/x{lt}/bash{gt}\nprose two"),
                "prose one\n\nprose two",
            ),
            (
                format!("a\n{lt}edit path=\"f.rs\"{gt}OLDLINE\n---\nNEWLINE{lt}/edit{gt}\nb"),
                "a\n\nb",
            ),
            (format!("c\n{lt}skill name=\"edit\"/{gt}\nd"), "c\n\nd"),
            (
                format!("e\n{lt}python{gt}x=1{lt}/python{gt} tail"),
                "e\n tail",
            ),
            (format!("f {lt}bash{gt}inline cmd{lt}/bash{gt} g"), "f  g"),
        ] {
            assert_eq!(strip_syscalls(&src), want, "leaked from {src:?}");
        }
    }

    /// The cache bar is a countdown, staged by colour. Cold is blue and does
    /// not look like an emergency; the window turns yellow at halfway and
    /// amber in the last fifth, which is the warning worth having.
    #[test]
    fn cache_bar_stages_by_time_left_in_the_window() {
        let theme = Theme::current();
        let mut m = CacheMeter::default();
        m.ephemeral = true;
        m.ttl = Duration::from_secs(300);

        let colour = |m: &CacheMeter| cache_stage(m, &theme).1[0].1;
        assert_eq!(colour(&m), theme.accent_system, "cold must be blue");

        m.read = 900;
        m.write = 100;
        for (elapsed, want, stage) in [
            (10u64, theme.accent_success, "fresh"),
            (200, theme.warning, "past halfway"),
            (280, theme.path, "nearly gone"),
        ] {
            m.at = Some(Instant::now() - Duration::from_secs(elapsed));
            let (fill, seg) = cache_stage(&m, &theme);
            assert_eq!(seg[0].1, want, "{stage}");
            assert!(fill > 0.0 && fill <= 1.0, "{stage} fill {fill}");
        }

        // Expired is cold again, not "almost gone" forever.
        m.at = Some(Instant::now() - Duration::from_secs(400));
        assert_eq!(colour(&m), theme.accent_system, "expired must read as cold");

        // A provider with no declared window keeps the read/write proportion.
        m.ephemeral = false;
        let (fill, seg) = cache_stage(&m, &theme);
        assert_eq!(fill, 1.0);
        assert_eq!(seg.len(), 2, "read and write must stay separate spans");
    }

    /// And the stage must reach the pane. Drives the real frame and reads the
    /// cache row's cells back, so a staged colour nothing paints fails here.
    #[test]
    fn the_meta_cache_row_paints_its_stage() {
        let mut app = App::new();
        app.cache.ephemeral = true;
        app.cache.ttl = Duration::from_secs(300);
        app.cache.read = 900;
        app.cache.write = 100;
        app.cache.window = 200_000;
        app.cache.at = Some(Instant::now() - Duration::from_secs(200));

        let backend = TestBackend::new(120, 44);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let buf = term.backend().buffer();
        let want = Theme::current().warning;

        let mut seen = false;
        for y in 0..buf.area.height {
            let row: String = (0..buf.area.width)
                .map(|x| buf[(x, y)].symbol().to_string())
                .collect();
            if !row.contains("cache") {
                continue;
            }
            seen = (0..buf.area.width).any(|x| buf[(x, y)].bg == want);
            if seen {
                break;
            }
        }
        assert!(seen, "the cache row never painted the past-halfway stage");
    }

    /// A TUI without a ready bridge is not interactive. Keep it out of the
    /// alternate screen instead of accepting prompts that cannot run.
    #[test]
    fn launch_fails_if_the_bridge_exits_before_ready() {
        let mut app = App::new();
        let mut bridge = match Bridge::loopback() {
            Ok(b) => b,
            Err(_) => return,
        };
        bridge.child.kill().unwrap();
        bridge.child.wait().unwrap();

        let err = wait_ready(Some(&mut bridge), &mut app).expect_err("dead bridge launched TUI");
        assert_eq!(err.kind(), io::ErrorKind::BrokenPipe);
    }

    /// A follow-up typed while a step runs must reach the bridge, not just the
    /// local queue. `run_turns` parks on `pending.wait_next` while background
    /// tasks are outstanding and only releases when its inbox is non-empty, so
    /// a queue-only push left the composer stuck in "queued" until every
    /// monitor landed. Drives the real key path and reads the wire back.
    #[test]
    fn queued_followup_pokes_the_bridge() {
        let mut app = App::new();
        let mut bridge = match Bridge::loopback() {
            Ok(b) => b,
            Err(_) => return, // no `cat` on this box; nothing to assert
        };
        app.running = true;
        app.prompt.insert_str("and then push it");
        submit_prompt(Some(&mut bridge), &mut app).unwrap();

        assert_eq!(app.queue.len(), 1, "follow-up did not queue");
        let sent = bridge
            .rx
            .recv_timeout(std::time::Duration::from_secs(5))
            .expect("nothing reached the bridge -- the loop cannot see the queue");
        assert_eq!(sent["op"], "typed", "wrong op on the wire: {sent}");
    }

    /// User prompts go through the real Story renderer with their own semantic
    /// color and stronger weight. Assistant prose remains markdown text; pane
    /// focus and block selection must not collapse the prompt back onto it.
    #[test]
    fn user_prompt_has_its_own_story_color_in_every_state() {
        let _guard = theme_lock();
        let mut app = App::new();
        app.prompt.insert_str("distinct user color");
        submit_prompt(None, &mut app).unwrap();
        app.story_push(RenderBlock::agent_message("assistant color control"));

        let theme = Theme::current();
        let user_fg = match theme.accent_user {
            Color::Reset => Color::Cyan,
            color => color,
        };
        let paint_cell = |app: &mut App, needle: &str| {
            let backend = TestBackend::new(100, 28);
            let mut term = Terminal::new(backend).unwrap();
            term.draw(|frame| draw(frame, app)).unwrap();
            let text = buffer_text(&term);
            let y = row_of(&text, needle).expect("story text was not rendered") as u16;
            let row = text.lines().nth(y as usize).unwrap();
            let x = row.find(needle).unwrap() as u16;
            term.backend().buffer()[(x, y)].clone()
        };

        let focused = paint_cell(&mut app, "distinct user color");
        assert_eq!(focused.fg, user_fg);
        assert!(focused.modifier.contains(Modifier::BOLD));
        let assistant = paint_cell(&mut app, "assistant color control");
        assert_eq!(assistant.fg, theme.md_text);

        app.focus = Focus::Calls;
        let unfocused = paint_cell(&mut app, "distinct user color");
        assert_eq!(unfocused.fg, user_fg, "pane focus changed prompt semantics");

        let prompt_idx = (0..app.sess.story.len())
            .find(|index| {
                matches!(
                app.sess.story.entry(*index).map(|entry| &entry.block),
                Some(RenderBlock::UserPrompt(_))
                )
            })
            .unwrap();
        app.focus = Focus::Story;
        app.sess.story.set_selected(Some(prompt_idx));
        let selected = paint_cell(&mut app, "distinct user color");
        assert_eq!(selected.fg, user_fg, "selection erased the prompt color");
        assert_ne!(
            selected.fg, selected.bg,
            "selected prompt lost text contrast"
        );
    }

    /// Only the focused pane draws a visible frame. The border cells stay put
    /// so the geometry never jumps on Tab -- they are painted in the pane
    /// background instead, which is the difference this asserts.
    #[test]
    fn an_unfocused_pane_has_no_visible_border() {
        let theme = Theme::current();
        let mut app = App::new();
        app.ready = true;
        app.focus = Focus::Story;
        let backend = TestBackend::new(110, 26);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let buf = term.backend().buffer();
        // Top-left corner of the story pane (focused) and of the calls pane.
        let story_corner = buf.cell((0, 0)).unwrap().style().fg.unwrap();
        let calls_x = app.call_area.x;
        let calls_corner = buf
            .cell((calls_x, app.call_area.y))
            .unwrap()
            .style()
            .fg
            .unwrap();
        let calls_bg = buf
            .cell((calls_x + 1, app.call_area.y + 1))
            .unwrap()
            .style()
            .bg
            .unwrap();
        assert_ne!(
            story_corner, theme.bg_base,
            "the focused pane must show its frame"
        );
        assert_eq!(
            calls_corner, calls_bg,
            "an unfocused frame must vanish into its pane background, got {calls_corner:?}"
        );
    }

    #[test]
    fn the_story_pane_shows_prose_and_not_commands() {
        // Drives the real event path. Asserts on the painted story, so a
        // renderer that exists but is never called cannot pass this.
        let mut app = App::new();
        app.ready = true;
        let speech = format!(
            "Checking the meter now.\n{}bash{}cargo test --workspace{}bash{}\nAll green.",
            '<', '>', "</", '>'
        );
        handle_event(&mut app, json!({"ev": "speech", "text": speech}));
        let text = paint(&mut app, 100, 40);
        assert!(
            text.contains("Checking the meter"),
            "prose missing:\n{text}"
        );
        assert!(text.contains("All green"), "prose missing:\n{text}");
        assert!(
            !text.contains("cargo test --workspace"),
            "the command leaked into the story:\n{text}"
        );
    }

    /// Turn-end reconcile follows the kernel, not the local grammar port.
    /// scan.py's TAG_OPEN accepts `[A-Za-z_]`, so `<_probe>…</_probe>`
    /// DISPATCHES — while the TUI's mid-stream hold (`looks_like_tag_start`)
    /// reads `<_` as prose and prints the whole call into the live block.
    /// The `complete` event's `spans` are the kernel's verdict; after it the
    /// story must contain exactly the bytes outside those spans. Revert the
    /// reconcile (strip_syscalls as the final pass) and this fails: the local
    /// grammar keeps the call as prose.
    #[test]
    fn turn_end_reconcile_follows_the_kernel_spans() {
        let mut app = App::new();
        app.ready = true;
        // Streamed as SSE would deliver it, split inside the tag.
        handle_event(
            &mut app,
            json!({"ev": "speech", "delta": true, "text": "hold on <_probe>rm -rf"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "speech", "delta": true, "text": " /tmp/x</_probe> done"}),
        );
        // A frame paints mid-stream: the conservative hold shows the call as
        // prose. This is the disagreement the reconcile exists to repair.
        flush_streams(&mut app);
        // desmos/kernel/scan.py::scan_spans on this speech, run for real:
        //   [('_probe', 8, 38)]  (ascii, so bytes == chars)
        handle_event(&mut app, json!({"ev": "complete", "spans": [[8, 38]]}));
        let text = paint(&mut app, 100, 40);
        assert!(
            text.contains("hold on"),
            "prose before the call missing:\n{text}"
        );
        assert!(
            text.contains("done"),
            "prose after the call missing:\n{text}"
        );
        assert!(
            !text.contains("<_probe>"),
            "the kernel dispatched this call; the story kept it as prose:\n{text}"
        );
        assert!(
            !text.contains("rm -rf"),
            "a dispatched body survived reconcile:\n{text}"
        );
    }

    /// The spans are UTF-8 *byte* offsets — the kernel converts scan.py's char
    /// offsets before emitting. A multibyte char ahead of the call catches a
    /// consumer that slices by chars.
    #[test]
    fn kernel_spans_are_byte_offsets() {
        let mut app = App::new();
        app.ready = true;
        handle_event(
            &mut app,
            json!({"ev": "speech", "delta": true, "text": "héllo <bash>ls</bash> done"}),
        );
        // scan_spans chars: (6, 21); as UTF-8 bytes (é is two): (7, 22).
        handle_event(&mut app, json!({"ev": "complete", "spans": [[7, 22]]}));
        let text = paint(&mut app, 100, 40);
        assert!(text.contains("héllo"), "prose missing:\n{text}");
        assert!(text.contains("done"), "prose missing:\n{text}");
        assert!(
            !text.contains("<bash>"),
            "call survived byte-span reconcile:\n{text}"
        );
    }

    #[test]
    fn a_sequence_bar_keeps_the_order_it_was_given() {
        use ratatui::style::Color;
        let chunks: Vec<(u64, Color)> = (0..40)
            .map(|i| (100u64, if i % 2 == 0 { Color::Red } else { Color::Blue }))
            .collect();
        let spans = sequence_bar_spans(60, &chunks, Color::Black);
        let painted: usize = spans.iter().map(|s| s.content.chars().count()).sum();
        assert_eq!(painted, 60, "bar did not fill its width");
        assert!(
            spans.len() > 10,
            "order collapsed into {} runs, so this is a bucket chart",
            spans.len()
        );
    }

    #[test]
    fn a_late_chunk_lands_late_in_the_bar() {
        use ratatui::style::Color;
        let mut chunks: Vec<(u64, Color)> = vec![(10, Color::Red); 20];
        chunks.push((5000, Color::Blue));
        let spans = sequence_bar_spans(60, &chunks, Color::Black);
        assert_eq!(spans.first().unwrap().style.fg.unwrap(), Color::Red);
        assert_eq!(spans.last().unwrap().style.fg.unwrap(), Color::Blue);
    }

    #[test]
    fn totals_are_derived_from_the_sequence() {
        let mut m = CacheMeter::default();
        m.observe_roles(&json!({
            "system": [{"type": "text", "text": "SSSS"}],
            "messages": [
                {"role": "user", "content": "typed"},
                {"role": "user", "content": "<result>dump</result>"},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "TTTT"},
                    {"type": "text", "text": "said"}
                ]}
            ]
        }));
        assert_eq!(
            m.chunks.len(),
            5,
            "one chunk per message or block: {:?}",
            m.chunks
        );
        let mut from_chunks = [0u64; 5];
        for (len, kind) in &m.chunks {
            from_chunks[*kind as usize] += *len;
        }
        assert_eq!(from_chunks, m.roles, "totals disagree with the sequence");
    }

    #[test]
    fn openai_responses_roles_are_not_empty() {
        let mut m = CacheMeter::default();
        m.observe_roles(&json!({
            "instructions": "system and catalog",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "typed"}]},
                {"type": "function_call_output", "call_id": "c", "output": "<result>tool</result>"},
                {"type": "reasoning", "summary": [], "encrypted_content": "opaque"},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "answer"}]}
            ]
        }));
        assert!(m.roles[0] > 0, "instructions slot empty: {:?}", m.roles);
        assert!(m.roles[1] > 0, "prompt slot empty: {:?}", m.roles);
        assert!(m.roles[2] > 0, "tool slot empty: {:?}", m.roles);
        assert!(m.roles[3] > 0, "reasoning slot empty: {:?}", m.roles);
        assert!(m.roles[4] > 0, "speech slot empty: {:?}", m.roles);
        assert_eq!(
            m.chunks.len(),
            5,
            "one chunk per Responses item: {:?}",
            m.chunks
        );
    }

    #[test]
    fn complete_event_records_openai_response_roles() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({
                "ev": "complete",
                "n": 1,
                "model": "gpt-5.6-luna",
                "usage": {"input_tokens": 100},
                "request": {
                    "instructions": "system",
                    "input": [
                        {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "prompt"}]},
                        {"type": "reasoning", "encrypted_content": "opaque"},
                        {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "answer"}]}
                    ]
                },
                "response": {}
            }),
        );
        assert!(app.cache.roles[0] > 0, "system was not recorded");
        assert!(app.cache.roles[1] > 0, "prompt was not recorded");
        assert!(app.cache.roles[3] > 0, "reasoning was not recorded");
        assert!(app.cache.roles[4] > 0, "speech was not recorded");
        assert!(
            !app.cache.chunks.is_empty(),
            "event path recorded no chunks"
        );
    }

    #[test]
    fn syscall_output_is_not_counted_as_something_you_typed() {
        // Tool results ride the user role on the wire. They are the first
        // thing worth trimming, so they must not hide in the prompt slice.
        let mut m = CacheMeter::default();
        m.observe_roles(&json!({
            "messages": [
                {"role": "user", "content": "please look at the meter"},
                {"role": "user", "content": "<result tag=\"bash\">a very long dump</result>"}
            ]
        }));
        assert!(m.roles[1] > 0, "prompt slot empty: {:?}", m.roles);
        assert!(m.roles[2] > 0, "tool slot empty: {:?}", m.roles);
        assert!(
            m.roles[2] > m.roles[1],
            "the longer result should outweigh the prompt: {:?}",
            m.roles
        );
    }

    #[test]
    fn thinking_is_counted_apart_from_speech() {
        // Replayed thinking outweighs the speech; together they only say
        // "the assistant is large", which is not something you can act on.
        let mut m = CacheMeter::default();
        m.observe_roles(&json!({
            "messages": [{
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "TTTTTTTTTTTTTTTTTTTTTTTTTTTTTT"},
                    {"type": "text", "text": "ok"}
                ]
            }]
        }));
        assert!(m.roles[3] > 0, "thinking slot empty: {:?}", m.roles);
        assert!(m.roles[4] > 0, "speech slot empty: {:?}", m.roles);
        assert!(
            m.roles[3] > m.roles[4],
            "thinking should outweigh the short reply: {:?}",
            m.roles
        );
    }

    #[test]
    fn a_meter_row_paints_exactly_its_width() {
        let c = ratatui::style::Color::Red;
        let t = ratatui::style::Color::Black;
        for w in [1u16, 12, 40, 77] {
            for ratio in [0.0f64, 0.13, 0.5, 0.999, 1.0] {
                let line = meter_row(w, "ctx", "62k / 200k", &[(3, c), (7, c)], ratio, t, t, c);
                let painted: usize = line.spans.iter().map(|s| s.content.chars().count()).sum();
                assert_eq!(painted, w as usize, "w={w} ratio={ratio}");
            }
        }
    }

    #[test]
    fn a_meter_row_keeps_the_value_when_the_label_will_not_fit() {
        let c = ratatui::style::Color::Red;
        let t = ratatui::style::Color::Black;
        // Narrow row: the number has to survive, the word can go.
        let line = meter_row(14, "context", "62k / 200k", &[(1, c)], 0.5, t, t, c);
        let text: String = line.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(text.contains("62k / 200k"), "value dropped: {text:?}");
        assert!(
            !text.contains("context"),
            "label should have yielded: {text:?}"
        );
        // One cell of gutter on the right, so the value never touches the border.
        assert!(
            text.ends_with(' '),
            "value is flush against the edge: {text:?}"
        );
    }

    #[test]
    fn role_split_attributes_system_user_and_assistant() {
        let mut m = CacheMeter::default();
        m.observe_roles(&json!({
            "system": [{"type":"text","text":"SYSTEMTEXT"}],
            "messages": [
                {"role":"user","content":"UUUU"},
                {"role":"assistant","content":"AAAAAAAA"},
                {"role":"user","content":"UU"}
            ]
        }));
        assert!(m.roles[0] > 0, "system slot empty: {:?}", m.roles);
        assert!(m.roles[1] > 0, "prompt slot empty: {:?}", m.roles);
        assert!(m.roles[4] > 0, "speech slot empty: {:?}", m.roles);
    }

    #[test]
    fn an_unlabelled_message_counts_as_system() {
        let mut m = CacheMeter::default();
        m.observe_roles(&json!({"messages":[{"content":"NOROLE"}]}));
        assert!(m.roles[0] > 0, "unlabelled went nowhere: {:?}", m.roles);
        assert_eq!(m.roles[1..], [0, 0, 0, 0]);
    }

    /// A request event carries an empty response. It must not clear the out
    /// pane nor relabel the held reply: the pane showed #5's body, went blank
    /// under "#6", then jumped when the real reply landed.
    #[test]
    fn post_out_holds_the_previous_reply_until_the_next_one_lands() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({"ev":"complete","n":5,"model":"m",
                   "request":{"seq":"REQFIVE"},"response":{"seq":"RESPFIVE"}}),
        );
        assert_eq!(app.post_n, 5);
        assert_eq!(app.post_out_n, 5);
        handle_event(
            &mut app,
            json!({"ev":"post","n":6,"request":{"seq":"REQSIX"}}),
        );
        assert_eq!(app.post_n, 6, "in pane must advance at once");
        assert_eq!(app.post_out_n, 5, "held reply keeps its own number");
        let text = paint(&mut app, 150, 34);
        assert!(text.contains("POST in #6"), "in title wrong:\n{text}");
        assert!(
            text.contains("POST out #5"),
            "out title must still say 5:\n{text}"
        );
        assert!(
            text.contains("RESPFIVE"),
            "held reply body was cleared:\n{text}"
        );
        handle_event(
            &mut app,
            json!({"ev":"complete","n":6,"model":"m",
                   "request":{"seq":"REQSIX"},"response":{"seq":"RESPSIX"}}),
        );
        assert_eq!(app.post_out_n, 6, "reply 6 should take over");
    }

    /// A call that just finished must not blink shut. finish_exec used to fold
    /// it, and reflow_wire reopened it on the next frame -- one frame of
    /// collapsed card for every completed syscall.
    #[test]
    fn a_just_finished_call_does_not_flash_shut() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({"ev":"result","phase":"start","tag":"bash","attrs":{},"body":"echo hi"}),
        );
        let _ = paint(&mut app, 120, 30);
        let id = app.sess.calls.entry(0).map(|e| e.id).expect("card pushed");
        assert_eq!(
            app.sess.calls.get_by_id(id).map(|e| e.display_mode),
            Some(DisplayMode::Expanded),
            "a running call should be open"
        );
        handle_event(
            &mut app,
            json!({"ev":"result","phase":"done","tag":"bash","attrs":{},"body":"echo hi","text":"hi"}),
        );
        // Checked before the next paint: nothing may fold it in between.
        assert_ne!(
            app.sess.calls.get_by_id(id).map(|e| e.display_mode),
            Some(DisplayMode::Collapsed),
            "completed call folded before the next frame -- that is the flash"
        );
        let text = paint(&mut app, 120, 30);
        assert_eq!(
            app.sess.calls.get_by_id(id).map(|e| e.display_mode),
            Some(DisplayMode::Expanded),
            "recent completed call should stay open"
        );
        assert!(text.contains("hi"), "output not visible:\n{text}");
    }

    #[test]
    fn canonical_and_legacy_exec_events_make_equivalent_execute_cards() {
        for (legacy, failure) in [
            ("python", "Traceback (most recent call last): boom"),
            ("bash", "exit 1"),
            ("shell", "exit 1"),
        ] {
            for body in ["echo audited", ""] {
                let card = |tag, attrs| {
                    let ev = json!({
                        "ev": "result",
                        "phase": "done",
                        "tag": tag,
                        "attrs": attrs,
                        "body": body,
                        "text": failure,
                    });
                    let RenderBlock::ToolCall(ToolCallBlock::Execute(block)) = result_block(&ev)
                    else {
                        panic!("{tag}/{legacy} did not produce an execute card");
                    };
                    block
                };
                let legacy_card = card(legacy, json!({}));
                let canonical_card = card("exec", json!({"op": legacy}));
                assert_eq!(canonical_card.command, legacy_card.command);
                assert_eq!(canonical_card.description, legacy_card.description);
                assert_eq!(canonical_card.output, legacy_card.output);
                assert_eq!(canonical_card.error, legacy_card.error);
                if body.is_empty() {
                    assert_eq!(canonical_card.command, format!("<{legacy}/>"));
                }
            }
        }

        let RenderBlock::ToolCall(ToolCallBlock::Execute(shell)) =
            wire_syscall("exec", "", &json!({"op": "shell", "id": "build"}), "", None)
        else {
            panic!("canonical shell did not produce an execute card");
        };
        assert_eq!(shell.command, "<shell id=\"build\">");
    }

    #[test]
    fn canonical_workspace_edit_keeps_the_real_diff_card() {
        let event = json!({
            "ev": "result",
            "phase": "done",
            "tag": "workspace",
            "attrs": {"op": "edit", "path": "notes.txt"},
            "body": "old\n---\nnew",
            "text": "Edited notes.txt",
            "line": 9,
        });
        let RenderBlock::ToolCall(ToolCallBlock::Edit(edit)) = result_block(&event) else {
            panic!("workspace op=edit fell back to a generic card");
        };
        assert_eq!(edit.path, "notes.txt");
        assert_eq!(edit.hunks[0][0].lo, 9);
        assert!(edit.hunks[0].iter().any(|line| line.text.contains("old")));
        assert!(edit.hunks[0].iter().any(|line| line.text.contains("new")));
    }

    #[test]
    fn structured_results_get_a_typed_summary_and_pretty_body() {
        let card = wire_syscall(
            "agents",
            "",
            &json!({"op": "status"}),
            r#"{"type":"worker","n":3,"state":"done"}"#,
            None,
        );
        let RenderBlock::ToolCall(ToolCallBlock::Other(other)) = card else {
            panic!("agents status should use the generic structured card");
        };
        assert_eq!(other.name, "agents: status");
        assert_eq!(other.summary, "worker #3");
        assert_eq!(
            other.output.as_deref(),
            Some("{\n  \"type\": \"worker\",\n  \"n\": 3,\n  \"state\": \"done\"\n}")
        );
    }

    #[test]
    fn canonical_streaming_shell_marks_its_real_exit_failure() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({"ev":"result","phase":"start","tag":"exec",
                   "attrs":{"op":"shell","id":"main"},"body":"false"}),
        );
        assert_eq!(app.sess.exec.tag, "shell");
        handle_event(
            &mut app,
            json!({"ev":"result","phase":"done","tag":"exec",
                   "attrs":{"op":"shell","id":"main"},"body":"false",
                   "text":"command failed\n[exit 1]"}),
        );
        let RenderBlock::ToolCall(ToolCallBlock::Execute(exec)) =
            &app.sess.calls.entry(0).expect("shell card").block
        else {
            panic!("canonical shell lost its execute card");
        };
        assert!(!exec.is_success(), "[exit 1] was painted as success");
    }

    #[test]
    fn streamed_json_finishes_as_a_formatted_result() {
        let mut app = App::new();
        let compact = r#"{"type":"worker","n":3}"#;
        handle_event(
            &mut app,
            json!({"ev":"result","phase":"start","tag":"exec",
                   "attrs":{"op":"python"},"body":"print(status)"}),
        );
        handle_event(
            &mut app,
            json!({"ev":"result","phase":"delta","tag":"exec","text":compact}),
        );
        handle_event(
            &mut app,
            json!({"ev":"result","phase":"done","tag":"exec",
                   "attrs":{"op":"python"},"body":"print(status)","text":compact}),
        );
        let RenderBlock::ToolCall(ToolCallBlock::Execute(exec)) =
            &app.sess.calls.entry(0).expect("python card").block
        else {
            panic!("canonical python lost its execute card");
        };
        assert_eq!(
            exec.output.as_deref(),
            Some("{\n  \"type\": \"worker\",\n  \"n\": 3\n}")
        );
    }

    /// Row colouring is NOT verified. The hunks carry the right Delete/Insert
    /// tags (see edit_tag_becomes_a_real_diff_block), and grok picks per-row
    /// styles out of ctx.appearance.scrollback.blocks.edit -- which desmos-tui
    /// builds itself and may not populate. A buffer probe showed both rows
    /// painted the same foreground. Unresolved: whether that is a test-harness
    /// artefact or a real gap in build_appearance().
    #[test]
    fn edit_card_shows_both_sides_of_the_change() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({
                "ev": "result",
                "tag": "edit",
                "attrs": {"path": "notes.md"},
                "body": "UNIQUEOLD\n---\nUNIQUENEW",
                "text": "Edited notes.md",
                "line": 3,
            }),
        );
        let text = paint(&mut app, 130, 34);
        assert!(text.contains("notes.md"), "path missing:\n{text}");
        assert!(text.contains("UNIQUEOLD"), "removed side missing:\n{text}");
        assert!(text.contains("UNIQUENEW"), "added side missing:\n{text}");
    }

    /// Paint-from-events discipline: the diff's line numbers come from the
    /// event's `line` field alone. The path does not exist on disk, so any
    /// surviving filesystem re-derivation would place the hunk at line 1.
    #[test]
    fn edit_card_lands_at_the_kernels_line_without_touching_disk() {
        let ev = json!({
            "ev": "result",
            "phase": "done",
            "tag": "edit",
            "attrs": {"path": "does/not/exist.rs"},
            "body": "old text\n---\nnew text",
            "text": "Edited does/not/exist.rs",
            "span_idx": 0,
            "line": 41,
        });
        let RenderBlock::ToolCall(ToolCallBlock::Edit(e)) = result_block(&ev) else {
            panic!("expected an edit block");
        };
        let first = e
            .hunks
            .first()
            .and_then(|h| h.first())
            .expect("hunks present");
        assert_eq!(first.lo, 41, "diff must anchor at the kernel's line");
        // No field, no line: an event without `line` must not invent one.
        let mut bare = ev.clone();
        bare.as_object_mut().unwrap().remove("line");
        let RenderBlock::ToolCall(ToolCallBlock::Edit(e2)) = result_block(&bare) else {
            panic!("expected an edit block");
        };
        assert!(
            e2.hunks.is_empty(),
            "an absent line field must not fabricate an anchor"
        );
    }

    #[test]
    fn edit_tag_becomes_a_real_diff_block() {
        let attrs = json!({"path": "notes.txt"});
        let block = wire_syscall(
            "edit",
            "alpha\nbeta\n---\nalpha\nGAMMA\n",
            &attrs,
            "Edited notes.txt",
            Some(1),
        );
        let RenderBlock::ToolCall(ToolCallBlock::Edit(e)) = block else {
            panic!("edit must render as a diff, not a generic Other card");
        };
        assert_eq!(e.path, "notes.txt");
        // DiffHunk is a flat Vec<DiffLine>; tag is similar::ChangeTag.
        let tags: Vec<String> = e
            .hunks
            .iter()
            .flat_map(|h| h.iter().map(|l| format!("{:?}", l.tag)))
            .collect();
        assert!(
            tags.iter().any(|t| t == "Delete"),
            "no removed line: {tags:?}"
        );
        assert!(
            tags.iter().any(|t| t == "Insert"),
            "no added line: {tags:?}"
        );
        let texts: Vec<&str> = e
            .hunks
            .iter()
            .flat_map(|h| h.iter().map(|l| l.text.as_str()))
            .collect();
        assert!(
            texts.iter().any(|t| t.contains("beta")),
            "old text missing: {texts:?}"
        );
        assert!(
            texts.iter().any(|t| t.contains("GAMMA")),
            "new text missing: {texts:?}"
        );
    }

    #[test]
    fn edit_body_splits_on_a_lone_separator() {
        let (o, n) = split_edit_body("a\nb\n---\nc\nd");
        assert_eq!(o, "a\nb");
        assert_eq!(n, "c\nd");
        // A --- inside the payload must not split again.
        let (o2, n2) = split_edit_body("x\n---\ny\n---\nz");
        assert_eq!(o2, "x");
        assert_eq!(n2, "y\n---\nz");
    }

    #[test]
    fn edit_body_without_a_separator_is_an_insertion() {
        let (o, n) = split_edit_body("just new text");
        assert!(o.is_empty(), "old side should be empty, got {o:?}");
        assert_eq!(n, "just new text");
    }

    #[test]
    fn a_failed_edit_carries_its_error() {
        let attrs = json!({"path": "gone.txt"});
        // A refused edit has no edit site, so the kernel sends no line.
        let block = wire_syscall("edit", "a\n---\nb", &attrs, "error: no such file", None);
        let RenderBlock::ToolCall(ToolCallBlock::Edit(e)) = block else {
            panic!("expected an edit block");
        };
        assert!(!e.is_success(), "a failing edit must not look successful");
    }

    #[test]
    fn wire_column_reaches_the_queue_not_just_the_top_third() {
        // POST in/out sit under the story column, so the calls border must
        // extend below the row where the POST panes start.
        let mut app = App::new();
        seed_demo(&mut app);
        let text = paint(&mut app, 140, 40);
        let calls_rows: Vec<usize> = text
            .lines()
            .enumerate()
            .filter(|(_, l)| l.contains("Activity") || l.contains("cache"))
            .map(|(i, _)| i)
            .collect();
        let post_row = row_of(&text, "POST in").expect(&text);
        let lowest = *calls_rows.iter().max().expect(&text);
        assert!(
            lowest > post_row,
            "wire column stops above the POST split (wire {lowest} <= post {post_row}):\n{text}"
        );
    }

    #[test]
    fn overflowing_pane_reports_how_much_is_hidden() {
        let mut app = App::new();
        for i in 0..60 {
            app.story_push(RenderBlock::agent_message(format!("ROW{i} filler line")));
        }
        // Paint once so prepare_layout establishes a viewport; scroll_up is a
        // no-op while viewport_height is 0.
        let first = paint(&mut app, 120, 30);
        assert!(
            first.contains("more up"),
            "tail view hides rows above:\n{first}"
        );
        assert!(
            !first.contains("more down"),
            "follow mode is pinned to the tail, nothing is below it:\n{first}"
        );
        app.sess.story.scroll_up(20);
        let text = paint(&mut app, 120, 30);
        assert!(text.contains("more up"), "no up-overflow marker:\n{text}");
        assert!(
            text.contains("more down"),
            "no down-overflow marker:\n{text}"
        );
    }

    #[test]
    fn a_pane_that_fits_shows_no_overflow_marker() {
        let mut app = App::new();
        app.story_push(RenderBlock::agent_message("just one short line"));
        let text = paint(&mut app, 120, 30);
        assert!(
            !text.contains("more up"),
            "spurious overflow marker:\n{text}"
        );
    }

    #[test]
    fn demo_uses_grok_blocks_not_out_labels() {
        let mut app = App::new();
        seed_demo(&mut app);
        app.set_focus(Focus::Story);
        let backend = TestBackend::new(140, 40);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let text = buffer_text(&term);
        assert!(text.contains("Story"), "{text}");
        assert!(text.contains("Activity"), "{text}");
        // A block *stamped* `out`, not the POST card's `out 100` usage column
        // — which is a real number and was only ever absent here because the
        // calls pane used to be clipped a row above it.
        assert!(
            !text.lines().any(|l| l.trim_start().starts_with("out ")),
            "legacy 'out' stamp still present:\n{text}"
        );
        assert!(
            text.contains("Thought") || text.contains("think") || text.contains("Thinking"),
            "thinking block missing:\n{text}"
        );
        assert!(
            text.contains("look around") || text.contains("kernel"),
            "user prompt missing:\n{text}"
        );
        assert!(
            text.contains('│') || text.contains('─') || text.contains("cache"),
            "agent markdown missing:\n{text}"
        );
        assert!(
            text.contains("POST")
                || text.contains("syscall")
                || text.contains("<python>")
                || text.contains("sorted"),
            "calls pane empty:\n{text}"
        );
        assert!(
            !text.contains("complete  complete"),
            "redundant grok tool title still in calls:\n{text}"
        );
    }

    #[test]
    fn hard_newlines_render_as_separate_rows() {
        let mut app = App::new();
        app.story_push(RenderBlock::user_prompt(
            "LINEA first line of the prompt\nLINEB second line still the same prompt",
        ));
        app.story_push(RenderBlock::agent_message(
            "PARAONE first paragraph stays its own block.\n\nPARATWO second paragraph after a blank line.",
        ));
        // 34 rows, not 24: the composer opens roomy and the legend band is
        // reserved opposite it, so a short terminal scrolls the story.
        let text = paint(&mut app, 80, 34);
        let a = row_of(&text, "LINEA").expect(&text);
        let b = row_of(&text, "LINEB").expect(&text);
        assert!(b > a, "user hard-newline stayed on one row:\n{text}");
        let p1 = row_of(&text, "PARAONE").expect(&text);
        let p2 = row_of(&text, "PARATWO").expect(&text);
        assert!(p2 > p1, "markdown paragraphs collapsed:\n{text}");
    }

    #[test]
    fn long_paragraph_wraps_across_rows() {
        let mut app = App::new();
        app.story_push(RenderBlock::agent_message(
            "WRAPSTART The kernel keeps names under ns and never dumps the heap \
             into chat. Speech is not memory. If future-you needs a fact it has \
             to live in a note, a skill, or a named object the index still lists. \
             A paragraph this long must wrap across more than one row. WRAPEND",
        ));
        let backend = TestBackend::new(52, 42);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        app.sess.story.goto_top();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let text = buffer_text(&term);
        let start = row_of(&text, "WRAPSTART").expect(&text);
        let end = row_of(&text, "WRAPEND").expect(&text);
        assert!(
            end > start,
            "paragraph did not wrap (start={start} end={end}):\n{text}"
        );
    }

    #[test]
    fn cannot_scroll_past_bottom() {
        let mut app = App::new();
        seed_demo(&mut app);
        let backend = TestBackend::new(140, 40);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let (_, vp, _) = app.sess.story.scroll_info();
        assert!(vp > 0, "layout never set a viewport");
        app.sess.story.scroll_down(10_000);
        clamp_scroll(&mut app.sess.story);
        term.draw(|f| draw(f, &mut app)).unwrap();
        let (off, vp, total) = app.sess.story.scroll_info();
        let max = total.saturating_sub(vp as usize);
        assert!(
            off <= max,
            "scrolled past end: offset={off} max={max} total={total} vp={vp}"
        );
        let text = buffer_text(&term);
        assert!(
            text.contains("cache") || text.contains("hit") || text.contains('─'),
            "bottom content left the pane:\n{text}"
        );
    }

    #[test]
    fn short_pane_stays_at_zero() {
        let mut app = App::new();
        seed_demo(&mut app);
        let backend = TestBackend::new(140, 40);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        wheel_scroll(&mut app.sess.calls, false, 200);
        let (off, vp, total) = app.sess.calls.scroll_info();
        let max = total.saturating_sub(vp as usize);
        assert_eq!(off, max.min(off));
        assert!(off <= max, "calls offset {off} > max {max}");
    }

    /// `?` opens the sheet for whichever pane you are standing in, and the
    /// next key — any key — closes it.
    #[test]
    fn question_mark_opens_the_pane_cheatsheet() {
        let mut app = App::new();
        seed_demo(&mut app);
        app.set_focus(Focus::Calls);
        let q = KeyEvent::new(KeyCode::Char('?'), KeyModifiers::NONE);
        handle_key(None, &mut app, q).unwrap();
        assert!(app.help, "? must open the sheet outside the composer");
        let text = paint(&mut app, 140, 60);
        assert!(
            text.contains("story / Activity keys"),
            "the sheet has to name the pane it describes"
        );
        assert!(
            text.contains("previous / next POST group"),
            "the sheet has to carry that pane's own verbs"
        );
        handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char('j'), KeyModifiers::NONE),
        )
        .unwrap();
        assert!(!app.help, "any key closes it");
    }

    /// In the composer `?` is a question mark. It was the only pane where the
    /// key already meant something.
    #[test]
    fn question_mark_in_the_composer_is_just_text() {
        let mut app = App::new();
        app.set_focus(Focus::Input);
        handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char('?'), KeyModifiers::NONE),
        )
        .unwrap();
        assert!(!app.help);
        assert_eq!(app.prompt.to_send(), "?");
    }

    /// A legend nobody checks is a legend that lies. Every `Char` key a pane's
    /// handler answers to has to appear in that pane's table — read out of
    /// `input.rs`, where `handle_key` lives, so adding a key without
    /// documenting it fails here.
    #[test]
    fn the_cheatsheet_lists_every_key_its_pane_answers_to() {
        let src = include_str!("input.rs");
        let slice = |from: &str, to: &str| -> String {
            let a = src
                .find(from)
                .unwrap_or_else(|| panic!("anchor gone: {from}"));
            let b = src[a..]
                .find(to)
                .unwrap_or_else(|| panic!("end anchor gone: {to}"));
            src[a..a + b].to_string()
        };
        let blocks = [
            (
                Focus::Queue,
                slice(
                    "KeyCode::Char('j') | KeyCode::Down => app.queue.select_next(),",
                    "if app.focus == Focus::Git &&",
                ),
            ),
            (
                Focus::Git,
                slice(
                    "if app.focus == Focus::Git &&",
                    "if app.focus == Focus::Files",
                ),
            ),
            (
                Focus::Files,
                slice(
                    "if app.focus == Focus::Files &&",
                    "// The meter has no cursor",
                ),
            ),
            (
                Focus::PostIn,
                slice(
                    "if matches!(app.focus, Focus::PostIn | Focus::PostOut) {",
                    "KeyCode::Char('j') | KeyCode::Down => app.queue.select_next(),",
                ),
            ),
            (
                Focus::Calls,
                slice(
                    "KeyCode::Char('j') | KeyCode::Down => app.focused_scroll().select_next(),",
                    "let width = app.input_inner.width.max(20);",
                ),
            ),
        ];
        for (focus, body) in blocks {
            let (name, keys) = pane_keys(focus);
            let listed: Vec<String> = keys
                .iter()
                .flat_map(|(k, _)| k.split_whitespace())
                .map(str::to_string)
                .collect();
            // Every KeyCode::Char('x') the handler matches, in source order.
            let mut wanted: Vec<char> = Vec::new();
            let mut rest = body.as_str();
            while let Some(i) = rest.find("KeyCode::Char('") {
                rest = &rest[i + "KeyCode::Char('".len()..];
                if let Some(c) = rest.chars().next()
                    && !wanted.contains(&c)
                {
                    wanted.push(c);
                }
            }
            assert!(
                wanted.len() >= 4,
                "{name}: found only {wanted:?} — the source anchors have moved"
            );
            for c in wanted {
                let plain = c.to_string();
                let ctrl = format!("ctrl-{c}");
                assert!(
                    listed.contains(&plain) || listed.contains(&ctrl),
                    "{name} answers to {c:?} and the cheatsheet does not say so: {listed:?}"
                );
            }
        }
    }

    /// Drop used to be the only thing you could do to a queued row, so a typo
    /// meant deleting it and retyping the whole thing. `e` lifts it into the
    /// composer and Enter puts it back in the slot it left -- not at the end,
    /// which would be a reorder nobody asked for.
    #[test]
    fn e_edits_a_queued_row_and_enter_returns_it_to_its_slot() {
        let mut app = App::new();
        app.running = true;
        for q in ["first", "middel one", "third"] {
            app.queue.push(q.into());
        }
        app.queue.selected = Some(1);
        app.set_focus(Focus::Queue);
        let key = |c| KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE);
        handle_key(None, &mut app, key('e')).unwrap();
        assert_eq!(
            app.queue.len(),
            2,
            "the row is lifted out while you edit it"
        );
        assert_eq!(app.prompt.to_send(), "middel one");
        assert_eq!(app.focus, Focus::Input, "editing happens in the composer");
        // Fix the typo and send it back.
        app.prompt.clear();
        app.prompt.insert_str("middle one");
        handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        )
        .unwrap();
        let rows: Vec<String> = app.queue.iter().map(|q| q.text.clone()).collect();
        assert_eq!(rows, vec!["first", "middle one", "third"]);
        assert!(
            app.queue_edit.is_none(),
            "the slot is spent once it is used"
        );
        assert_eq!(app.sess.story.len(), 0, "editing a queued row runs no turn");
    }

    /// Emptying the composer while editing is the delete. It must not fall
    /// through to send-now, which fires whichever row happens to be selected.
    #[test]
    fn emptying_an_edited_row_drops_it_and_fires_nothing() {
        let mut app = App::new();
        app.running = true;
        app.queue.push("keep me".into());
        app.queue.push("kill me".into());
        app.queue.selected = Some(1);
        app.set_focus(Focus::Queue);
        handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char('e'), KeyModifiers::NONE),
        )
        .unwrap();
        app.prompt.clear();
        handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        )
        .unwrap();
        let rows: Vec<String> = app.queue.iter().map(|q| q.text.clone()).collect();
        assert_eq!(rows, vec!["keep me"]);
        assert!(!app.send_now, "an emptied edit is a delete, not a send-now");
        assert!(app.queue_edit.is_none());
    }

    #[test]
    fn enter_while_running_stacks_follow_up() {
        let mut app = App::new();
        app.running = true;
        app.prompt.insert_str("do this next");
        let quit = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        )
        .unwrap();
        assert!(!quit);
        assert_eq!(app.queue.len(), 1);
        assert!(app.prompt.to_send().is_empty());
        assert_eq!(
            app.sess.story.len(),
            0,
            "queued follow-up must not hit story yet"
        );
    }

    #[test]
    fn done_drains_front_of_queue() {
        let mut app = App::new();
        app.queue.push("first queued".into());
        app.queue.push("second queued".into());
        handle_event(&mut app, json!({"ev": "done"}));
        assert!(app.drain_after);
        try_drain(None, &mut app).unwrap();
        assert_eq!(app.queue.len(), 1);
        assert_eq!(app.queue.iter().next().unwrap().text, "second queued");
        assert!(
            matches!(
                app.sess.story.entry(0).map(|e| &e.block),
                Some(RenderBlock::UserPrompt(_))
            ),
            "the drained row is the turn that starts"
        );
    }

    #[test]
    fn stopped_returns_idle_and_drains_existing_queue() {
        let mut app = App::new();
        app.running = true;
        app.status = "stopping".into();
        app.queue.push("run after the stop".into());

        handle_event(&mut app, json!({"ev": "stopped", "text": "stopped, saved"}));
        assert!(!app.running);
        assert_eq!(app.status, "idle");
        assert!(app.drain_after, "a queued request was stranded by stopped");

        app.drain_after = false;
        try_drain(None, &mut app).unwrap();
        assert!(app.queue.is_empty());
        assert!(matches!(
            app.sess
                .story
                .entry(app.sess.story.len() - 1)
                .map(|entry| &entry.block),
            Some(RenderBlock::UserPrompt(_))
        ));
    }

    #[test]
    fn empty_enter_send_now_stops_and_fires_front() {
        let mut app = App::new();
        app.running = true;
        app.queue.push("send this now".into());
        let quit = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        )
        .unwrap();
        assert!(!quit);
        assert!(!app.running);
        assert!(app.queue.is_empty());
        assert!(
            matches!(
                app.sess.story.entry(0).map(|e| &e.block),
                Some(RenderBlock::UserPrompt(_))
            ),
            "send-now puts the row it fired in the story"
        );
    }

    #[test]
    fn last_post_panes_show_raw_body_without_redacted_ciphertext() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({
                "ev": "complete",
                "n": 4,
                "origin": "llm",
                "model": "claude-opus-5",
                "thinking": "low",
                "thoughts": 1,
                "redacted": 1,
                "usage": {},
                "request": {
                    "model": "claude-opus-5",
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "WIREPROBE"}]}]
                },
                "response": {
                    "content": [
                        {"type": "text", "text": "WIREANSWER"},
                        {"type": "redacted_thinking", "data": "[redacted]"}
                    ]
                }
            }),
        );
        app.set_focus(Focus::PostIn);
        let text = paint(&mut app, 120, 40);
        assert!(text.contains("POST in"), "{text}");
        assert!(text.contains("POST out"), "{text}");
        assert!(text.contains("WIREPROBE"), "request body missing:\n{text}");
        assert!(
            text.contains("WIREANSWER"),
            "response body missing:\n{text}"
        );
        assert!(!text.contains("opaque-secret"), "{text}");
        assert_eq!(app.post_n, 4);
    }

    #[test]
    fn plus_grows_the_focused_pane_on_its_own_axis() {
        let mut l = PaneLayout::default();
        let wide = l.wire_pct;
        l.grow(Focus::Calls, 2);
        assert_eq!(l.wire_pct, wide + 2, "calls + must widen the wire column");
        l.grow(Focus::Story, 2);
        assert_eq!(l.wire_pct, wide, "story + must take that width back");
        let rows = l.post_h;
        l.grow(Focus::PostIn, -2);
        assert_eq!(l.post_h, rows - 2, "post - must shorten the POST row");
    }

    #[test]
    fn ctrl_arrows_resize_along_the_arrow() {
        let mut l = PaneLayout::default();
        let rows = l.post_h;
        // Story has no height of its own: it grows by pushing POST down.
        l.grow_axis(Focus::Story, Axis::Vertical, 2);
        assert_eq!(l.post_h, rows - 2);
        l.grow_axis(Focus::Calls, Axis::Vertical, -2);
        assert_eq!(l.post_h, rows);
        // POST in and out share one row and trade width with each other.
        let split = l.post_split;
        l.grow_axis(Focus::PostIn, Axis::Horizontal, 5);
        assert_eq!(l.post_split, split + 5);
        l.grow_axis(Focus::PostOut, Axis::Horizontal, 5);
        assert_eq!(l.post_split, split);
        // The meter takes rows from the calls pane above it.
        let meter = l.meter_h;
        l.grow_axis(Focus::Meter, Axis::Vertical, 2);
        assert_eq!(l.meter_h, meter + 2);
    }

    #[test]
    fn layout_sizes_stay_inside_their_clamps() {
        let mut l = PaneLayout::default();
        for _ in 0..50 {
            l.grow_axis(Focus::Calls, Axis::Horizontal, 5);
            l.grow_axis(Focus::PostIn, Axis::Horizontal, 5);
            l.grow_axis(Focus::Meter, Axis::Vertical, 5);
            l.grow_axis(Focus::PostIn, Axis::Vertical, 5);
        }
        assert_eq!(l.wire_pct, PaneLayout::MAX_WIRE);
        assert_eq!(l.post_split, PaneLayout::MAX_SPLIT);
        assert_eq!(l.meter_h, PaneLayout::MAX_METER);
        assert_eq!(l.post_h, PaneLayout::MAX_POST);
        for _ in 0..50 {
            l.grow_axis(Focus::Calls, Axis::Horizontal, -5);
            l.grow_axis(Focus::PostIn, Axis::Horizontal, -5);
            l.grow_axis(Focus::Meter, Axis::Vertical, -5);
            l.grow_axis(Focus::PostIn, Axis::Vertical, -5);
        }
        assert_eq!(l.wire_pct, PaneLayout::MIN_WIRE);
        assert_eq!(l.post_split, PaneLayout::MIN_SPLIT);
        assert_eq!(
            (l.meter_h, l.post_h),
            (0, 0),
            "both panes must reach hidden"
        );
    }

    #[test]
    fn tier_boundaries_and_the_default_layout() {
        assert_eq!(Tier::of(0), Tier::Line);
        assert_eq!(Tier::of(1), Tier::Line);
        assert_eq!(Tier::of(2), Tier::Dense);
        assert_eq!(Tier::of(3), Tier::Dense);
        assert_eq!(Tier::of(4), Tier::Full);
        assert_eq!(Tier::of(12), Tier::Full);
        // The default hugs its content: context, cache, cost, agent config,
        // and theme, plus two border rows. Activity lives on the composer.
        assert_eq!(PaneLayout::default().meter_h, 7);
        let inner = PaneLayout::default().meter_h - 2;
        assert_eq!(Tier::of(inner), Tier::Full);
        // Both side panes are open out of the box. A saved `.desmos/tui.json`
        // still wins — `0` on any pane is what puts these back.
        let d = PaneLayout::default();
        assert!(d.git_h > 0 && d.files_h > 0, "side panes start open");
        assert!(
            d.meter_h + d.git_h + d.files_h < 24,
            "the wire column still belongs to the calls pane"
        );
    }

    #[test]
    fn a_short_meter_keeps_the_numbers_that_decide_something() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({
                "ev": "complete",
                "n": 1,
                "origin": "user",
                "model": "claude-opus-5",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 40000,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 100,
                },
            }),
        );
        // Rows the meter can be dragged to, and what has to survive each.
        for (rows, wants, drops) in [
            // One inner row answers "how full am I", not "what did it cost".
            (3u16, "ctx", "trend"),
            // Three rows buy the cache split and the running cost. The trend
            // sparkline is gone at every height: it restated the cache row.
            (6, "saved", "trend"),
            (9, "saved", "trend"),
        ] {
            app.layout.meter_h = rows;
            let text = paint(&mut app, 150, 40);
            assert!(
                text.contains(wants),
                "meter_h {rows} must still show {wants:?}:\n{text}"
            );
            if !drops.is_empty() {
                assert!(
                    !text.contains(drops),
                    "meter_h {rows} must not try to draw {drops:?}:\n{text}"
                );
            }
        }
    }

    #[test]
    fn cache_meter_bills_reads_at_a_tenth_and_counts_warm_calls() {
        let mut m = CacheMeter::default();
        // Cold: 1000 fresh in, 1000 written to the 5m cache, 100 out.
        m.observe(
            &json!({
                "input_tokens": 1000,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 0,
                "output_tokens": 100,
                "cache_creation": {"ephemeral_5m_input_tokens": 1000},
            }),
            "claude-opus-5",
        );
        // opus: $5/MTok in, $25 out. 1000*5 + 1000*6.25 + 100*25 = 13_750e-6
        assert!((m.spent - 0.013_75).abs() < 1e-9, "{}", m.spent);
        assert_eq!((m.calls, m.warm), (1, 0));
        assert_eq!(m.saved, 0.0);

        // Warm: the same 1000 tokens come back as a cache read.
        m.observe(
            &json!({
                "input_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 1000,
                "output_tokens": 100,
            }),
            "claude-opus-5",
        );
        // + 10*5 + 1000*0.5 + 100*25 = 3_050e-6
        assert!((m.spent - 0.016_80).abs() < 1e-9, "{}", m.spent);
        // Uncached those reads would have cost 1000*5; they cost 1000*0.5.
        assert!((m.saved - 0.004_5).abs() < 1e-9, "{}", m.saved);
        assert_eq!((m.calls, m.warm), (2, 1));
        assert_eq!(m.read_total, 1000);
        assert_eq!(m.ttl, Duration::from_secs(300));
    }

    #[test]
    fn cache_meter_reads_the_1h_bucket_off_the_wire() {
        let mut m = CacheMeter::default();
        m.observe(
            &json!({
                "input_tokens": 0,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 0,
                "output_tokens": 0,
                "cache_creation": {"ephemeral_1h_input_tokens": 1000},
            }),
            "claude-opus-5",
        );
        assert_eq!(m.ttl, Duration::from_secs(3600));
        // 1h writes bill at 2x input, not 1.25x.
        assert!((m.spent - 0.010).abs() < 1e-9, "{}", m.spent);
    }

    /// Wire cards ship folded; open every one so a test can assert on the
    /// body and result the fold hides.
    fn expand_calls(app: &mut App) {
        app.sess.calls.goto_top();
        for _ in 0..app.sess.calls.len() {
            app.sess.calls.expand_selected();
            app.sess.calls.select_next();
        }
        app.sess.calls.goto_top();
    }

    /// A fold rewrites what the model remembers. It is the harness acting on
    /// itself, not something the model said, so it belongs on the wire.
    /// The bridge op for switching models existed from the start; nothing
    /// typed to it, so the picker was the only way in. And `/compact` reads
    /// like "fold the transcript" while only changing row spacing.
    /// A picker holding the catalog a live bridge would have published.
    fn stocked_picker() -> picker::Picker {
        let mut p = picker::Picker::default();
        p.observe(&json!({
            "ev": "ready",
            "providers": [
                {"provider": "anthropic", "ok": true, "models": ["claude-opus-5", "claude-sonnet-4-6"],
                 "efforts": ["low", "high", "xhigh"], "can_login": false},
                {"provider": "openai", "ok": true, "models": ["gpt-5.6-sol", "gpt-5.6-luna"],
                 "efforts": ["low", "medium", "high", "xhigh", "max"], "can_login": true},
                {"provider": "ghost", "ok": false, "models": ["never-offer-me"],
                 "efforts": ["low"], "can_login": false},
            ],
        }));
        p
    }

    /// Every command was reachable only by knowing it existed, and a bad
    /// argument was discoverable only by sending it.
    #[test]
    fn slash_completes_commands_then_their_arguments() {
        let pick = stocked_picker();
        let mut s = slash::Slash::default();

        s.update("/mod", &pick);
        assert!(s.open);
        assert_eq!(s.items[0].text, "/model");
        // /model is complete on its own -- no trailing space, because the
        // picker is the argument surface. Appending one sent you to a second,
        // worse list on the way to the screen that does the work.
        assert_eq!(s.accept().as_deref(), Some("/model"));
        s.update("/model ", &pick);
        assert!(!s.open, "no argument list stands in front of the picker");

        // A command whose argument has no better surface still lists it.
        s.update("/the", &pick);
        assert_eq!(s.accept().as_deref(), Some("/theme "));
        s.update("/theme rose", &pick);
        assert_eq!(s.accept().as_deref(), Some("/theme rosepine"));

        s.update("/thinking ", &pick);
        let offered: Vec<&str> = s.items.iter().map(|i| i.text.as_str()).collect();
        assert!(offered.contains(&"xhigh"), "{offered:?}");

        // Tab and Esc belong to the global pane-cycle, so an open list has to
        // claim them before that runs — this was a real miss, caught live.
        let mut app = App::new();
        app.picker = stocked_picker();
        app.set_focus(Focus::Input);
        for c in "/mod".chars() {
            handle_key(None, &mut app, press(KeyCode::Char(c))).unwrap();
        }
        assert!(app.slash.open, "typing a slash opens the list");
        handle_key(None, &mut app, tab()).unwrap();
        assert_eq!(
            app.focus,
            Focus::Input,
            "Tab must complete, not cycle panes"
        );
        assert_eq!(app.prompt.to_send(), "/model");
        handle_key(None, &mut app, press(KeyCode::Esc)).unwrap();
        assert!(!app.slash.open);
        assert_eq!(
            app.focus,
            Focus::Input,
            "Esc dismissed the list, not the pane"
        );

        // A command taking nothing has nothing to complete.
        s.update("/reset ", &pick);
        assert!(!s.open);
        // Prose is not a command.
        s.update("what about /model", &pick);
        assert!(!s.open);
    }

    #[test]
    fn theme_completion_previews_then_rolls_back_or_commits() {
        let _pin = theme_lock();
        let saved = Theme::current_kind();
        let mut app = App::new();
        theme_cache::set(ThemeKind::OscuraMidnight);
        app.apply_grok_settings();
        app.set_focus(Focus::Input);

        for c in "/theme ".chars() {
            handle_key(None, &mut app, press(KeyCode::Char(c))).unwrap();
        }
        assert_eq!(
            Theme::current_kind(),
            ThemeKind::GrokNight,
            "opening the values must preview the highlighted row"
        );
        let _ = paint(&mut app, 100, 30);
        let popup = slash_popup_area(app.input_area, &app).expect("theme popup");
        handle_mouse(
            &mut app,
            MouseEvent {
                kind: MouseEventKind::ScrollDown,
                column: popup.x + 1,
                row: popup.y + 1,
                modifiers: KeyModifiers::NONE,
            },
        );
        assert_eq!(
            Theme::current_kind(),
            ThemeKind::TokyoNight,
            "wheel navigation must preview the row it selects"
        );
        handle_key(None, &mut app, press(KeyCode::Esc)).unwrap();
        assert_eq!(
            Theme::current_kind(),
            ThemeKind::OscuraMidnight,
            "Escape must restore the theme active before preview"
        );

        app.prompt.clear();
        update_slash(&mut app);
        for c in "/theme ".chars() {
            handle_key(None, &mut app, press(KeyCode::Char(c))).unwrap();
        }
        handle_key(None, &mut app, press(KeyCode::Down)).unwrap();
        handle_key(None, &mut app, press(KeyCode::Enter)).unwrap();
        assert_eq!(
            Theme::current_kind(),
            ThemeKind::TokyoNight,
            "Enter must keep the highlighted preview"
        );
        assert!(!app.slash.open);
        assert!(app.prompt.to_send().is_empty());
        assert!(app.theme_preview_origin.is_none());

        theme_cache::set(saved);
        app.apply_grok_settings();
    }

    /// Enter accepted unconditionally, so a command taking no argument could
    /// never be sent: /reset left one suggestion, accepting it produced the
    /// line already typed, the list matched again, and Enter looped there.
    /// The only way out was to type a space.
    #[test]
    fn enter_sends_a_command_that_has_nothing_left_to_complete() {
        let mut app = App::new();
        app.picker = stocked_picker();
        app.set_focus(Focus::Input);
        for c in "/reset".chars() {
            handle_key(None, &mut app, press(KeyCode::Char(c))).unwrap();
        }
        assert!(app.slash.open, "the list is up");
        handle_key(None, &mut app, press(KeyCode::Enter)).unwrap();
        // Sent: the composer is empty and the list is gone. Looping would have
        // left "/reset" sitting in the composer with the list still open.
        assert!(!app.slash.open, "the list must not survive the send");
        assert_eq!(
            app.prompt.to_send(),
            "",
            "Enter sent it instead of re-completing"
        );

        // A prefix still has something to add, so Enter completes there.
        for c in "/the".chars() {
            handle_key(None, &mut app, press(KeyCode::Char(c))).unwrap();
        }
        handle_key(None, &mut app, press(KeyCode::Enter)).unwrap();
        assert_eq!(app.prompt.to_send(), "/theme ", "a prefix completes");
        assert!(app.slash.open, "and the argument list opens");
        for c in "rosepine".chars() {
            handle_key(None, &mut app, press(KeyCode::Char(c))).unwrap();
        }
        handle_key(None, &mut app, press(KeyCode::Enter)).unwrap();
        assert_eq!(app.prompt.to_send(), "", "a chosen argument sends");

        // /model is one keystroke from the picker: complete the name, send it,
        // and the picker is up. It used to append a space, open a list of bare
        // model names, and only reach the picker if you deleted the space.
        for c in "/mod".chars() {
            handle_key(None, &mut app, press(KeyCode::Char(c))).unwrap();
        }
        handle_key(None, &mut app, press(KeyCode::Enter)).unwrap();
        assert_eq!(app.prompt.to_send(), "/model", "no space, no second list");
        handle_key(None, &mut app, press(KeyCode::Enter)).unwrap();
        assert_eq!(app.prompt.to_send(), "", "the next Enter sends it");
        assert!(app.picker.open, "straight into the picker");
        app.picker.open = false;
    }

    #[test]
    fn slash_says_whether_it_will_work_before_you_send_it() {
        let pick = stocked_picker();
        use slash::Verdict;
        assert_eq!(slash::verdict("hello there", &pick), Verdict::NotACommand);
        assert_eq!(slash::verdict("/reset", &pick), Verdict::Ready);
        assert_eq!(
            slash::verdict("/model", &pick),
            Verdict::Ready,
            "bare /model opens the picker"
        );
        assert_eq!(
            slash::verdict("/model claude-opus-5", &pick),
            Verdict::Ready
        );
        assert_eq!(slash::verdict("/theme rosepine", &pick), Verdict::Ready);
        assert!(matches!(
            slash::verdict("/thinking", &pick),
            Verdict::NeedsArg(_)
        ));
        assert!(matches!(
            slash::verdict("/nonsense", &pick),
            Verdict::Unknown(_)
        ));
        match slash::verdict("/model gpt-9", &pick) {
            Verdict::BadArg { got, expected } => {
                assert_eq!(got, "gpt-9");
                assert!(expected.contains("gpt-5.6-sol"), "{expected}");
            }
            other => panic!("{other:?}"),
        }
        // An effort the current build cannot serve is caught the same way.
        assert!(matches!(
            slash::verdict("/thinking ludicrous", &pick),
            Verdict::BadArg { .. }
        ));
        assert_eq!(slash::verdict("/thinking xhigh", &pick), Verdict::Ready);
    }

    /// The meter counted down a 5-minute clock for OpenAI too, so a session
    /// that had cached fine read "cache cold" once the Anthropic-shaped TTL
    /// ran out — a deadline that provider never gave us.
    #[test]
    fn only_a_provider_that_declares_a_ttl_gets_a_countdown() {
        let usage = json!({
            "input_tokens": 638,
            "cache_read_input_tokens": 2816,
            "cache_creation_input_tokens": 0,
            "output_tokens": 57,
        });
        let mut app = App::new();
        handle_event(&mut app, json!({"ev": "snapshot", "provider": "openai"}));
        app.cache.observe(&usage, "gpt-5.6-luna");
        assert!(!app.cache.ephemeral);
        let painted = paint(&mut app, 130, 40);
        assert!(painted.contains("81% cached"), "{painted}");
        assert!(!painted.contains("cold"), "{painted}");

        handle_event(&mut app, json!({"ev": "snapshot", "provider": "anthropic"}));
        assert!(app.cache.ephemeral, "anthropic does declare one");
        app.cache.observe(&usage, "claude-opus-5");
        let painted = paint(&mut app, 130, 40);
        assert!(
            painted.contains("81%") && painted.contains("5m"),
            "{painted}"
        );
    }

    #[test]
    fn a_queued_switch_is_labelled_queued_not_current() {
        let mut app = App::new();
        app.model = "claude-opus-5".into();
        app.running = true;
        let _ = apply_picker(
            None,
            &mut app,
            picker::PickerAction::Apply {
                model: "gpt-5.6-sol".into(),
                effort: "low".into(),
            },
        );
        assert_eq!(
            app.model, "claude-opus-5",
            "the wire is still on the old model"
        );
        assert!(app.model_pending.is_some());
        // The badge has to survive a composer that is not the whole screen —
        // the right column takes roughly a third of it.
        let painted = paint(&mut app, 120, 40);
        let meta = rows_of(&painted, app.cache.area);
        assert!(
            meta.contains("→ gpt-5.6-sol/low queued"),
            "the queued badge belongs to the meta pane:\n{meta}"
        );

        // The bridge's snapshot is what promotes it.
        handle_event(
            &mut app,
            json!({"ev": "snapshot", "model": "gpt-5.6-sol", "thinking": "low"}),
        );
        assert_eq!(app.model, "gpt-5.6-sol");
        assert!(app.model_pending.is_none());
    }

    #[test]
    fn density_is_the_default_and_the_story_gets_the_rows() {
        // The single policy point: every pane's appearance comes from here.
        // /dense used to write a setting nothing read: every pad here was a
        // constant, so toggling it changed a stored bool and not one cell.
        let dense0 = appearance_cache::load();
        appearance_cache::set(false);
        let roomy = grok_appearance();
        appearance_cache::set(true);
        let cfg = grok_appearance();
        assert!(cfg.prompt.compact, "compact is the default, not a fallback");
        assert!(
            cfg.scrollback.layout.block_pad_left < roomy.scrollback.layout.block_pad_left,
            "dense has to actually take a column back"
        );
        assert!(cfg.scrollback.layout.block_pad_right < roomy.scrollback.layout.block_pad_right);
        assert!(
            !cfg.scrollback.blocks.edit.vpad && roomy.scrollback.blocks.edit.vpad,
            "and an edit card's blank rows, which were never toggled at all"
        );
        assert!(
            !cfg.scrollback.blocks.prompt.vpad,
            "a prompt does not need two blank rows"
        );
        assert!(!cfg.turn_status.gap);

        // And it reaches the frame: an idle composer exposes eight text rows,
        // plus its border and the floating spacer above the card.
        let mut app = App::new();
        let _ = paint(&mut app, 140, 34);
        assert_eq!(
            app.input_area.height, 11,
            "idle composer: {:?}",
            app.input_area
        );
        assert_eq!(
            app.traj_area.y + app.traj_area.height + app.layout.post_h,
            app.input_area.y,
            "the story column has to absorb the reclaimed row",
        );
        appearance_cache::set(dense0);
    }

    #[test]
    fn the_post_body_promotes_the_model_the_composer_names() {
        let mut app = App::new();
        app.model = "claude-opus-5".into();
        app.running = true;
        let _ = apply_picker(
            None,
            &mut app,
            picker::PickerAction::Apply {
                model: "gpt-5.6-sol".into(),
                effort: "low".into(),
            },
        );
        assert!(app.model_pending.is_some());
        // No snapshot arrives mid-step, but the request does — and the request
        // is what the model is actually being asked as.
        handle_event(
            &mut app,
            json!({"ev": "post", "n": 3, "request": {"model": "gpt-5.6-sol"}}),
        );
        assert_eq!(
            app.model, "gpt-5.6-sol",
            "the body on the wire is the authority"
        );
        assert!(
            app.model_pending.is_none(),
            "nothing left to queue once it is in a request"
        );
    }

    #[test]
    fn a_fold_is_explained_in_the_story_not_only_on_the_wire() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({"ev": "compacted", "n": 7, "kept": 24, "text": "summary of earlier turns"}),
        );
        let painted = paint(&mut app, 120, 40);
        assert!(painted.contains("context folded"), "{painted}");
        assert!(
            painted.contains("24"),
            "kept count is the fact that matters: {painted}"
        );
        assert_eq!(app.status, "context folded");
    }

    #[test]
    fn a_notice_explains_itself_without_ending_the_turn() {
        let mut app = App::new();
        app.running = true;
        handle_event(
            &mut app,
            json!({"ev": "notice", "text": "provider switched anthropic to openai."}),
        );
        assert!(app.running, "a notice is not a terminator");
        let painted = paint(&mut app, 120, 40);
        assert!(painted.contains("provider switched"), "{painted}");
    }

    #[test]
    fn channel_activity_pops_up_without_becoming_story_narration() {
        let mut app = App::new();
        app.running = true;
        handle_event(
            &mut app,
            json!({
                "ev": "channel",
                "channel": "conflicts",
                "author": "worker-b",
                "preview": "persist.py conflict",
                "unread": 2,
                "message_id": 9
            }),
        );
        assert!(app.running, "a channel popup is not a turn terminator");
        assert_eq!(
            app.status,
            "IRC #conflicts · worker-b: persist.py conflict · 2 unread"
        );
        assert_eq!(
            app.sess.story.len(),
            0,
            "transient IRC activity leaked into Story"
        );
        let painted = paint(&mut app, 120, 40);
        assert!(painted.contains("persist.py conflict"), "{painted}");
    }

    #[test]
    fn directed_peer_activity_is_persistent_story_content() {
        let mut app = App::new();
        app.running = true;
        handle_event(
            &mut app,
            json!({
                "ev": "channel",
                "channel": "peer:self:reply",
                "author": "peer-123",
                "preview": "hello from the other side",
                "unread": 1,
                "message_id": 10,
                "directed": "reply",
                "body": "hello from the other side"
            }),
        );
        assert!(app.running, "a peer message is not a turn terminator");
        assert_eq!(
            app.sess.story.len(),
            1,
            "directed peer message was not routed to Story"
        );
        let painted = paint(&mut app, 120, 40);
        assert!(painted.contains("Peer reply · peer-123"), "{painted}");
        assert!(painted.contains("hello from the other side"), "{painted}");
    }

    #[test]
    fn model_and_dense_are_local_slashes_and_compact_still_answers() {
        for line in ["/model", "/model gpt-5.6-sol", "/dense", "/compact"] {
            assert!(is_local_slash(line), "{line} must not reach the model");
        }
        // A prefix match ate real prose. These are prompts, not commands.
        assert!(!is_local_slash("/modelling the data"));
        assert!(!is_local_slash("/themes worth trying"));
        assert!(!is_local_slash("/thinkingcap"));
        assert!(!is_local_slash("model gpt-5.6-sol"));
        assert!(is_local_slash("/theme rosepine"));
        assert!(is_local_slash("/thinking high"));
    }

    #[test]
    fn a_fold_lands_on_the_wire_and_is_explained_in_the_story() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({"ev": "compacted", "n": 7, "kept": 12, "text": "folded 40 turns"}),
        );
        app.set_focus(Focus::Calls);
        let _ = paint(&mut app, 100, 40);
        expand_calls(&mut app);
        let wire = paint(&mut app, 100, 60);
        assert!(wire.contains("FOLD"), "{wire}");
        assert!(wire.contains("#7") && wire.contains("12 kept"), "{wire}");
        assert!(wire.contains("folded 40 turns"), "{wire}");

        // The evidence is the card; the story gets exactly one harness row that
        // says what happened. It must be a System row — a fold is not speech.
        assert_eq!(
            app.sess.story.len(),
            1,
            "the fold went unexplained where it is read"
        );
        assert!(
            matches!(
                app.sess.story.entry(0).map(|e| &e.block),
                Some(RenderBlock::System(_))
            ),
            "a fold is not speech, but it must be said",
        );
    }

    #[test]
    fn edit_and_execute_cards_share_a_rail_and_a_body_column() {
        // Two card kinds in one pane. The execute card always drew the accent
        // rail and set its body at column 2; the edit card shipped with no
        // accent and a diff indented two further columns. Read together that
        // looked like half the pane was tabbed and half was not.
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({"ev":"result","tag":"bash","attrs":{},
                   "body":"echo hi","text":"hi"}),
        );
        handle_event(
            &mut app,
            json!({"ev":"result","tag":"edit","attrs":{"path":"a.rs"},
                   "body":"old\n---\nnew","text":"Edited a.rs","line":1}),
        );
        app.set_focus(Focus::Calls);
        let text = paint(&mut app, 90, 60);
        // Rail on both headers.
        assert!(text.contains("\u{2503}\u{25c6} bash"), "{text}");
        assert!(text.contains("\u{2503}\u{25c6} Edit"), "{text}");
        // Bodies start in the same column, one gap past the rail.
        assert!(text.contains("\u{2503}  $ echo hi"), "{text}");
        assert!(text.contains("\u{2503}  1  old"), "{text}");
    }

    #[test]
    fn a_long_command_does_not_wrap_the_card_header() {
        // A wrapped header lands its continuation rows back at the header's own
        // column, so they read as new top-level entries -- and the execute card
        // then prints the whole command a second time underneath, correctly
        // indented. That mismatch is what made the pane look half-tabbed.
        let mut app = App::new();
        let cmd = "cd /Users/zeus/hub/desmos/crates/desmos-tui && cargo build 2>&1 \
                   | grep -E '^(error|warning)' | head -10; echo SENTINEL_TAIL";
        handle_event(
            &mut app,
            json!({
                "ev": "result",
                "tag": "bash",
                "attrs": {},
                "body": cmd,
                "text": "ok",
            }),
        );
        app.set_focus(Focus::Calls);
        let text = paint(&mut app, 100, 60);
        assert!(text.contains("bash"), "{text}");
        // The header is a label: clipped, with the tail living only in the body.
        assert!(text.contains('\u{2026}'), "header was not clipped:\n{text}");
        assert!(
            text.matches("SENTINEL_TAIL").count() <= 1,
            "command echoed twice:\n{text}"
        );
        assert!(card_summary(cmd).chars().count() <= CARD_SUMMARY_W);
        assert_eq!(card_summary("short one"), "short one");
        // Flattened: a header is one row, so embedded newlines cannot make two.
        assert!(!card_summary("a\nb").contains('\n'));
    }

    #[test]
    fn calls_pane_says_who_posted_and_which_syscall() {
        let mut app = App::new();
        seed_demo(&mut app);
        app.toggle_posts();
        app.set_focus(Focus::Calls);
        let _ = paint(&mut app, 100, 36);
        expand_calls(&mut app);
        // Opened cards are tall — give the pane room or the result scrolls off.
        let text = paint(&mut app, 100, 80);
        assert!(text.contains("YOU") && text.contains("POST"), "{text}");
        assert!(
            text.contains("<python>") || text.contains("sorted"),
            "{text}"
        );
        assert!(text.contains("CWD") || text.contains("['CWD']"), "{text}");
        assert!(!text.contains("complete  complete"), "{text}");
    }

    #[test]
    fn wire_shows_syscall_body_and_result_not_just_a_header() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({
                "ev": "result",
                "tag": "python",
                "attrs": {},
                "body": "print(1+1)",
                "text": "2",
            }),
        );
        handle_event(
            &mut app,
            json!({
                "ev": "result",
                "tag": "edit",
                "attrs": {"path": "notes/cache.md"},
                "body": "old cache line\n---\nlast-user only",
                "text": "wrote notes/cache.md",
                "line": 1,
            }),
        );
        app.set_focus(Focus::Calls);
        let folded = paint(&mut app, 100, 36);
        assert!(
            folded.contains("print(1+1)"),
            "folded python row must preview the command:\n{folded}"
        );
        expand_calls(&mut app);
        let text = paint(&mut app, 100, 36);
        assert!(
            text.contains("print(1+1)"),
            "python body missing (collapsed or never sent):\n{text}"
        );
        assert!(text.contains('2'), "python result missing:\n{text}");
        assert!(
            text.contains("notes/cache.md") && text.contains("last-user only"),
            "edit body/path missing:\n{text}"
        );
        // The confirmation line is gone on purpose: an edit card renders the
        // diff, and the -/+ rows above are the evidence the write landed.
        assert!(
            text.contains("old cache line"),
            "removed line missing from the diff:\n{text}"
        );
        let idx = app.sess.calls.len().saturating_sub(1);
        let mode = app.sess.calls.entry(idx).map(|e| e.display_mode());
        assert_eq!(mode, Some(DisplayMode::Expanded));
    }

    #[test]
    fn running_keeps_prompt_and_advances_wave_tick() {
        let mut app = App::new();
        app.running = true;
        app.turn_started = Some(Instant::now());
        app.story_push(RenderBlock::thinking_streaming());
        app.sess.story.set_last_running(true);
        let t0 = app.sess.story.animation_tick();
        assert!(
            app.sess.story.tick(),
            "visible running entry must request redraw"
        );
        assert!(app.sess.story.animation_tick() > t0);
        app.set_focus(Focus::Input);
        let text = paint(&mut app, 120, 30);
        assert!(
            !text.contains('❯'),
            "input must not show a chevron:\n{text}"
        );
        assert!(
            !text.contains(" … "),
            "running must not replace the prompt with ellipsis:\n{text}"
        );
        let frames = glyphs::braille_spinner_frames();
        assert!(
            frames.iter().any(|f| text.contains(f)),
            "status must show a grok braille spinner frame:\n{text}"
        );
    }

    #[test]
    fn grok_settings_land_on_scrollback() {
        let app = App::new();
        assert_eq!(
            app.sess.story.appearance().show_timestamps,
            appearance_cache::load_timestamps()
        );
        assert_eq!(
            app.sess.calls.appearance().prompt.compact,
            appearance_cache::load()
        );
    }

    #[test]
    fn ctrl_c_quits_when_idle() {
        let mut app = App::new();
        assert!(!app.running);
        assert!(on_ctrl_c(None, &mut app).unwrap());
    }

    #[test]
    fn ctrl_c_stops_when_running_without_bridge() {
        let mut app = App::new();
        app.running = true;
        app.status = "running".into();
        assert!(on_ctrl_c(None, &mut app).unwrap());
        assert!(!app.running);
    }

    #[test]
    fn input_shows_caret_when_focused() {
        let mut app = App::new();
        app.set_focus(Focus::Input);
        app.prompt.insert_str("hi");
        let backend = TestBackend::new(80, 24);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let pos = term.get_cursor_position().expect("hardware cursor");
        assert!(
            pos.y >= app.input_area.y && pos.y < app.input_area.y + app.input_area.height,
            "cursor y {} outside input {:?}",
            pos.y,
            app.input_area
        );
        let expected_x = app.input_area.x
            + 1 // the box's own border; the old outer gutter is gone
            + UnicodeWidthStr::width(" ") as u16
            + UnicodeWidthStr::width("hi") as u16;
        assert_eq!(pos.x, expected_x, "caret not at end of prompt");
        assert!(
            term.backend().buffer()[(pos.x, pos.y)]
                .modifier
                .contains(Modifier::REVERSED),
            "input caret cell is not reversed"
        );
    }

    #[test]
    fn shift_enter_inserts_newline_without_submit() {
        let mut app = App::new();
        app.prompt.insert_str("hello");
        let quit = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT),
        )
        .unwrap();
        assert!(!quit);
        assert_eq!(app.prompt.to_send(), "hello\n");
        assert_eq!(app.sess.story.len(), 0);
        assert!(app.prompt.is_multiline());
    }

    #[test]
    fn backslash_enter_does_not_submit() {
        let mut app = App::new();
        app.prompt.insert_str("hello\\");
        let quit = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        )
        .unwrap();
        assert!(!quit);
        assert_eq!(app.prompt.to_send(), "hello\n");
        assert_eq!(app.sess.story.len(), 0);
    }

    #[test]
    fn paste_multiline_does_not_submit() {
        let mut app = App::new();
        apply_paste(&mut app, "a\nb\nc", false);
        assert_eq!(app.prompt.to_send(), "a\nb\nc");
        assert_eq!(app.sess.story.len(), 0);
    }

    #[test]
    fn pasted_slashes_are_data_and_image_paths_never_open_commands() {
        let mut pasted_command = App::new();
        apply_paste(&mut pasted_command, "/reset", false);
        assert!(pasted_command.slash_paste_guard);
        assert!(!pasted_command.slash.open);
        assert!(matches!(
            slash_verdict(&pasted_command),
            slash::Verdict::NotACommand
        ));
        handle_key(
            None,
            &mut pasted_command,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        )
            .unwrap();
        assert_ne!(
            pasted_command.status, "transcript cleared",
            "a pasted /reset executed as a local command"
        );

        let mut typed_command = App::new();
        for c in "/reset".chars() {
            handle_key(None, &mut typed_command, press(KeyCode::Char(c))).unwrap();
        }
        handle_key(
            None,
            &mut typed_command,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        )
            .unwrap();
        assert_eq!(typed_command.status, "transcript cleared");

        let png = png_file("paste-slash");
        let mut image_path = App::new();
        apply_paste(&mut image_path, png.to_str().unwrap(), false);
        assert_eq!(image_path.prompt.images().len(), 1);
        assert!(image_path.prompt.to_send().is_empty());
        assert!(image_path.slash_paste_guard);
        assert!(matches!(
            slash_verdict(&image_path),
            slash::Verdict::NotACommand
        ));
    }

    #[test]
    fn paste_chip_enter_expands_instead_of_send() {
        let mut app = App::new();
        apply_paste(&mut app, "a\nb\nc\nd", false);
        app.prompt.move_left();
        let quit = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        )
        .unwrap();
        assert!(!quit);
        assert_eq!(app.prompt.to_send(), "a\nb\nc\nd");
        assert_eq!(app.sess.story.len(), 0);
        assert!(app.prompt.preview_body().is_none());
    }

    #[test]
    fn input_hides_caret_when_unfocused() {
        let mut app = App::new();
        app.set_focus(Focus::Story);
        let backend = TestBackend::new(80, 24);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let y = app.input_area.y + 2;
        let buf = term.backend().buffer();
        let reversed = (app.input_area.x..app.input_area.x + app.input_area.width)
            .any(|x| buf[(x, y)].modifier.contains(Modifier::REVERSED));
        assert!(!reversed, "caret still painted while story is focused");
    }

    #[test]
    fn a_spawn_row_carries_a_title_and_live_progress_not_the_raw_task() {
        let mut app = App::new();
        let task = "Audit the desmos repo (/Users/zeus/hub/desmos) for whether these \
                    PYTHON-side todo items are already implemented. For each, answer \
                    DONE / PARTIAL / NOT DONE with file:line evidence.";
        handle_event(
            &mut app,
            json!({
                "ev": "subagent",
                "phase": "started",
                "id": "cafe",
                "agent": "explore",
                "task": task,
            }),
        );
        let idx = first_subagent(&app).expect("spawn row");
        let RenderBlock::Subagent(sb) = &app.sess.story.entry(idx).expect("entry").block else {
            panic!("expected a spawn row");
        };
        assert_eq!(
            sb.description,
            "Audit the desmos repo for whether these PYTHON-side\u{2026}"
        );

        handle_event(
            &mut app,
            json!({
                "ev": "subagent",
                "phase": "progress",
                "id": "cafe",
                "stage": "executing",
                "progress": "collected bash evidence",
                "turns": 12,
            }),
        );
        let RenderBlock::Subagent(sb) = &app.sess.story.entry(idx).expect("entry").block else {
            panic!("expected a spawn row");
        };
        assert_eq!(
            sb.activity_label.as_deref(),
            Some("executing \u{b7} collected bash evidence")
        );
    }

    fn first_subagent(app: &App) -> Option<usize> {
        (0..app.sess.story.len()).find(|&i| {
            matches!(
                app.sess.story.entry(i).map(|e| &e.block),
                Some(RenderBlock::Subagent(_))
            )
        })
    }

    #[test]
    fn a_background_child_post_never_touches_the_parent_wire() {
        let mut app = App::new();
        // Parent turn: this is what the POST split must keep showing.
        handle_event(
            &mut app,
            json!({
                "ev": "post",
                "n": 7,
                "request": {"model": "claude-opus-5", "input": []},
            }),
        );
        assert_eq!(app.post_n, 7);
        let parent_model = app
            .post_req
            .get("model")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();

        // A subagent runs in the background. The human is not inside it.
        assert!(app.viewing.is_none());
        handle_event(
            &mut app,
            json!({
                "ev": "child",
                "id": "deadbeef",
                "kind": "post",
                "n": 99,
                "request": {"model": "gpt-5.6-luna", "input": []},
            }),
        );
        assert_eq!(app.post_n, 7, "child post overwrote the parent POST number");
        assert_eq!(
            app.post_req.get("model").and_then(Value::as_str),
            Some(parent_model.as_str()),
            "child request leaked into the parent POST pane"
        );

        // Inside that session, the child's wire is exactly what should show.
        app.viewing = Some("deadbeef".to_string());
        handle_event(
            &mut app,
            json!({
                "ev": "child",
                "id": "deadbeef",
                "kind": "post",
                "n": 99,
                "request": {"model": "gpt-5.6-luna", "input": []},
            }),
        );
        assert_eq!(app.post_n, 99);
        assert_eq!(
            app.post_req.get("model").and_then(Value::as_str),
            Some("gpt-5.6-luna")
        );
    }

    #[test]
    fn spawn_started_stays_on_parent_child_gets_the_transcript() {
        let mut app = App::new();
        app.show_posts = true;
        handle_event(
            &mut app,
            json!({
                "ev": "subagent",
                "phase": "started",
                "id": "deadbeef",
                "agent": "explore",
                "persona": "researcher",
                "task": "find cache notes",
                "model": "claude-opus-5",
            }),
        );
        handle_event(
            &mut app,
            json!({
                "ev": "subagent",
                "phase": "progress",
                "id": "deadbeef",
                "stage": "executing",
                "progress": "collected python evidence",
                "turns": 3,
            }),
        );
        handle_event(
            &mut app,
            json!({
                "ev": "child",
                "id": "deadbeef",
                "kind": "thinking",
                "redacted": false,
                "text": "look in notes",
            }),
        );
        handle_event(
            &mut app,
            json!({
                "ev": "child",
                "id": "deadbeef",
                "kind": "speech",
                "text": "CHILDONLY last-user cache",
            }),
        );
        handle_event(
            &mut app,
            json!({
                "ev": "child",
                "id": "deadbeef",
                "kind": "complete",
                "n": 1,
                "origin": "user",
                "model": "claude-opus-5",
                "thinking": "low",
                "usage": {},
                "thoughts": 1,
                "redacted": 0,
            }),
        );
        handle_event(
            &mut app,
            json!({
                "ev": "child",
                "id": "deadbeef",
                "kind": "result",
                "tag": "python",
                "text": "notes()",
            }),
        );
        handle_event(
            &mut app,
            json!({
                "ev": "subagent",
                "phase": "done",
                "id": "deadbeef",
                "stage": "accepted",
                "accepted": true,
                "secs": 1.5,
                "turns": 3,
                "result": "CHILDONLY last-user cache",
                "error": "",
            }),
        );

        let idx = first_subagent(&app).expect("started block");
        let entry = app.sess.story.entry(idx).expect("entry");
        match &entry.block {
            RenderBlock::Subagent(sb) => {
                assert!(matches!(sb.kind, SubagentBlockKind::Started));
                assert_eq!(sb.child_session_id, "deadbeef");
                assert_eq!(sb.activity_label.as_deref(), Some("accepted"));
            }
            other => panic!("expected Subagent, got {other:?}"),
        }
        assert!(
            (0..app.sess.story.len()).any(|i| {
                matches!(
                    app.sess.story.entry(i).map(|e| &e.block),
                    Some(RenderBlock::Subagent(sb))
                        if matches!(sb.kind, SubagentBlockKind::Completed { .. })
                )
            }),
            "async spawn must keep started and add a completed row"
        );

        let parent = paint(&mut app, 120, 28);
        assert!(parent.contains("Subagent"), "{parent}");
        assert!(
            !parent.contains("CHILDONLY"),
            "child speech leaked onto parent story:\n{parent}"
        );
        assert_eq!(
            app.sess.calls.len(),
            0,
            "child wire must not hit parent calls"
        );

        let child = app.children.get("deadbeef").expect("child session");
        assert!(
            child.sess.story.len() >= 2,
            "task + speech stay in child Story"
        );
        assert_eq!(
            child.sess.calls.len(),
            3,
            "thought + complete + syscall stay in child Activity"
        );
    }

    #[test]
    fn enter_opens_spawn_session_esc_returns() {
        let mut app = App::new();
        seed_demo(&mut app);
        app.set_focus(Focus::Story);
        let idx = first_subagent(&app).expect("demo spawn");
        app.sess.story.set_selected(Some(idx));
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        );
        assert_eq!(app.viewing.as_deref(), Some("a1b2c3d4"));
        let inside = paint(&mut app, 120, 30);
        assert!(inside.contains("Session a1b2c3d4"), "{inside}");
        assert!(
            inside.contains("last-user")
                || inside.contains("CHILDONLY")
                || inside.contains("cache"),
            "child speech missing inside session:\n{inside}"
        );
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE),
        );
        assert!(app.viewing.is_none());
        let back = paint(&mut app, 120, 30);
        assert!(back.contains("Story"), "{back}");
        assert!(!back.contains("Session a1b2c3d4"), "{back}");
    }

    #[test]
    fn ctrl_f_opens_spawn_session() {
        let mut app = App::new();
        seed_demo(&mut app);
        app.set_focus(Focus::Story);
        let idx = first_subagent(&app).expect("demo spawn");
        app.sess.story.set_selected(Some(idx));
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char('f'), KeyModifiers::CONTROL),
        );
        assert_eq!(app.viewing.as_deref(), Some("a1b2c3d4"));
    }

    #[test]
    fn post_in_paints_before_the_response() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({
                "ev": "post",
                "n": 1,
                "request": {
                    "model": "claude-opus-5",
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "LIVEPROBE"}]}]
                }
            }),
        );
        app.set_focus(Focus::PostIn);
        let text = paint(&mut app, 120, 40);
        assert!(
            text.contains("LIVEPROBE"),
            "POST in missing request:\n{text}"
        );
        assert!(app.post_out.is_empty());
        assert_eq!(app.post_n, 1);
    }

    #[test]
    fn live_events_route_conversation_to_story_and_work_to_activity() {
        let mut app = App::new();
        start_step(None, &mut app, "inspect routing".into(), Vec::new()).unwrap();
        handle_event(
            &mut app,
            json!({"ev": "thinking", "text": "private plan", "delta": false}),
        );
        handle_event(
            &mut app,
            json!({"ev": "result", "tag": "bash", "body": "pwd", "text": "/tmp"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "speech", "text": "final answer", "delta": false}),
        );
        // Speech buffers until its turn closes; the complete event's kernel
        // spans (none here — the reply is pure prose) finalize the story text.
        handle_event(&mut app, json!({"ev": "complete", "spans": []}));

        let story_kinds: Vec<&str> = (0..app.sess.story.len())
            .filter_map(|i| app.sess.story.entry(i))
            .map(|entry| match &entry.block {
                RenderBlock::UserPrompt(_) => "prompt",
                RenderBlock::AgentMessage(_) => "speech",
                RenderBlock::Thinking(_) => "thinking",
                _ => "other",
            })
            .collect();
        let activity_kinds: Vec<&str> = (0..app.sess.calls.len())
            .filter_map(|i| app.sess.calls.entry(i))
            .map(|entry| match &entry.block {
                RenderBlock::Thinking(_) => "thinking",
                RenderBlock::ToolCall(_) => "tool",
                _ => "other",
            })
            .collect();

        assert_eq!(story_kinds, vec!["prompt", "speech"]);
        assert!(activity_kinds.contains(&"thinking"), "{activity_kinds:?}");
        assert!(activity_kinds.contains(&"tool"), "{activity_kinds:?}");
    }

    #[test]
    fn activity_is_the_rendered_and_focused_pane_name() {
        let mut app = App::new();
        app.set_focus(Focus::Calls);
        let painted = paint(&mut app, 120, 34);
        assert!(painted.contains("Activity  [+posts]"), "{painted}");
        assert!(!painted.contains("calls  [+posts]"), "{painted}");
        assert_eq!(focus_name(Focus::Calls), "Activity");

        app.help = true;
        let help = paint(&mut app, 120, 34);
        assert!(help.contains("story / Activity"), "{help}");
        assert!(help.contains("POST group (Activity)"), "{help}");
        assert!(!help.contains("story / calls"), "{help}");
    }

    #[test]
    fn a_turn_waits_without_an_empty_thinking_card() {
        let mut app = App::new();
        app.running = true;
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        assert!(
            app.sess.calls.is_empty(),
            "turn eagerly created Activity chrome"
        );

        let waiting = paint(&mut app, 120, 34);
        assert!(rows_of(&waiting, app.input_area).contains("Inference"));
        assert!(!rows_of(&waiting, app.call_area).contains("Thinking"));

        handle_event(
            &mut app,
            json!({"ev": "thinking", "delta": true, "redacted": false, "text": "real plan"}),
        );
        let active = paint(&mut app, 120, 34);
        assert_eq!(app.sess.calls.len(), 1);
        assert!(rows_of(&active, app.call_area).contains("real plan"));
    }

    #[test]
    fn thinking_deltas_append_one_block() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({"ev": "thinking", "delta": true, "redacted": false, "text": "hel"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "thinking", "delta": true, "redacted": false, "text": "lo"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "thinking", "delta": true, "redacted": false, "text": " world"}),
        );
        handle_event(&mut app, json!({"ev": "complete", "n": 1}));
        let thinks: Vec<String> = (0..app.sess.calls.len())
            .filter_map(|i| match app.sess.calls.entry(i).map(|e| &e.block) {
                Some(RenderBlock::Thinking(t)) => Some(t.text()),
                _ => None,
            })
            .collect();
        assert_eq!(thinks, vec!["hello world".to_string()]);
        assert!(!app.sess.stream.live());
    }

    #[test]
    fn speech_deltas_append_and_hold_split_tags() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({"ev": "speech", "delta": true, "text": "see "}),
        );
        handle_event(
            &mut app,
            json!({"ev": "speech", "delta": true, "text": "<python>1"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "speech", "delta": true, "text": "</python> more"}),
        );
        handle_event(&mut app, json!({"ev": "complete", "n": 1}));
        let spoken: Vec<String> = (0..app.sess.story.len())
            .filter_map(|i| match app.sess.story.entry(i).map(|e| &e.block) {
                Some(RenderBlock::AgentMessage(m)) => Some(m.text()),
                _ => None,
            })
            .collect();
        // The `1` is the call's body: it belongs to the calls pane, and it must
        // never appear in the story even for the one frame before it closes.
        assert_eq!(spoken, vec!["see  more".to_string()]);
        assert!(
            !spoken
                .iter()
                .any(|s| s.contains("<python>") || s.contains('1'))
        );
    }

    #[test]
    fn execute_stdout_streams_one_card() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({
                "ev": "result",
                "phase": "start",
                "tag": "bash",
                "body": "printf hi",
                "text": ""
            }),
        );
        handle_event(
            &mut app,
            json!({"ev": "result", "phase": "delta", "tag": "bash", "text": "h"}),
        );
        handle_event(
            &mut app,
            json!({"ev": "result", "phase": "delta", "tag": "bash", "text": "i"}),
        );
        handle_event(
            &mut app,
            json!({
                "ev": "result",
                "phase": "done",
                "tag": "bash",
                "body": "printf hi",
                "text": "hi"
            }),
        );
        let outs: Vec<String> = (0..app.sess.calls.len())
            .filter_map(|i| match app.sess.calls.entry(i).map(|e| &e.block) {
                Some(RenderBlock::ToolCall(ToolCallBlock::Execute(b))) => {
                    Some(b.output.clone().unwrap_or_default())
                }
                _ => None,
            })
            .collect();
        assert_eq!(outs, vec!["hi".to_string()]);
        assert!(app.sess.exec.id.is_none());
    }

    /// A folded work row is owed one git read: the one that saw its last
    /// syscall. Rewriting it from whichever snapshot arrives first prints a
    /// second pre-commit answer, because the read already in flight when the
    /// commit landed started before it.
    #[test]
    fn a_folded_row_waits_for_the_read_that_saw_its_last_call() {
        let mut app = App::new();
        app.sess
            .stream
            .run
            .call("bash", call_target("bash", &json!({})));
        app.sess
            .stream
            .run
            .call("bash", call_target("bash", &json!({})));
        // No read has landed at all, so the pane's generation is 0.
        assert_eq!(app.git.snap_gen(), 0);
        app.sess.stream.run.fresh_gen = 7;
        app.sess.stream.run.fold(&mut app.sess.story);
        assert!(app.sess.stream.run.settled.is_some(), "fold owes a row");
        app.sess.stream.run.settle(&mut app.sess.story, &app.git);
        assert!(
            app.sess.stream.run.settled.is_some(),
            "settled on a snapshot that predates the call it is reporting"
        );
        // A run whose calls forced no read is owed nothing and closes at once.
        app.sess
            .stream
            .run
            .call("read", call_target("read", &json!({})));
        app.sess
            .stream
            .run
            .call("read", call_target("read", &json!({})));
        app.sess.stream.run.fold(&mut app.sess.story);
        app.sess.stream.run.settle(&mut app.sess.story, &app.git);
        assert!(app.sess.stream.run.settled.is_none());
    }

    /// The work row's "committed" claim is the kernel's fact
    /// (`result.repo.committed`), not a HEAD-snapshot diff: a result event
    /// carrying the field paints the claim, and results without it never do —
    /// even when the command and its output look exactly like a commit.
    #[test]
    fn the_work_row_claims_only_the_commit_the_kernel_reported() {
        let rows = |app: &App| -> Vec<String> {
            (0..app.sess.story.len())
                .filter_map(|i| match app.sess.story.entry(i).map(|e| &e.block) {
                    Some(RenderBlock::System(b)) => Some(b.text.clone()),
                    _ => None,
                })
                .collect()
        };
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({
                "ev": "result", "phase": "done", "tag": "bash",
                "body": "git add -A && git commit -m x",
                "text": "[main abc1234] x",
            }),
        );
        handle_event(
            &mut app,
            json!({
                "ev": "result", "phase": "done", "tag": "bash",
                "body": "echo hi", "text": "hi",
            }),
        );
        let before = rows(&app);
        assert!(!before.is_empty(), "two calls earn a work row");
        assert!(
            !before.iter().any(|r| r.contains("committed")),
            "output that merely looks like a commit must not claim one: {before:?}"
        );
        handle_event(
            &mut app,
            json!({
                "ev": "result", "phase": "done", "tag": "bash",
                "body": "git commit -m y",
                "text": "[main beef123] y",
                "repo": {"committed": "beef123"},
            }),
        );
        let after = rows(&app);
        assert!(
            after.iter().any(|r| r.contains("committed beef123")),
            "the kernel's repo claim must paint the row: {after:?}"
        );
        // The claim survives the fold: settle rewrites the tail from the
        // carried verdict, never from a git snapshot.
        handle_event(&mut app, json!({"ev": "done"}));
        app.sess.stream.run.settle(&mut app.sess.story, &app.git);
        assert!(
            app.sess.stream.run.settled.is_none(),
            "a kernel claim needs no git read"
        );
        let folded = rows(&app);
        assert!(
            folded.iter().any(|r| r.contains("committed beef123")),
            "the claim must survive settle: {folded:?}"
        );
    }

    /// A dead harness is not a step. The old no-bridge branch pushed a POST
    /// card labelled "demo" under the prompt, so the reader saw a request go
    /// out and no answer ever come back.
    #[test]
    fn a_prompt_after_the_bridge_dies_says_so_instead_of_posting() {
        let mut app = App::new();
        assert!(!app.demo);
        app.bridge_gone = true;
        start_step(None, &mut app, "carry on".into(), Vec::new()).unwrap();
        assert!(!app.running, "nothing is running");
        assert_eq!(
            app.sess.posts.len(),
            0,
            "no POST group for a step that cannot run"
        );
        let last = app
            .sess
            .story
            .entry(app.sess.story.len() - 1)
            .map(|e| e.block.clone());
        assert!(
            matches!(last, Some(RenderBlock::System(ref b)) if b.text == BRIDGE_GONE),
            "the story must carry the death under the prompt, not a 4s notice: {last:?}"
        );
    }

    /// A fence with no closer masks nothing, because `scan.py::_fence_span`
    /// masks nothing: the call after a stray backtick run is dispatched for
    /// real. Masking it here printed it raw in the story *and* ran it.
    ///
    /// Each expectation is `desmos/scan.py::scan_spans` on the same input, run
    /// for real:
    ///     open ```bash then a call -> [('bash', 26, 41)] dispatched
    ///     bare ``` then a call     -> [('bash',  6, 21)] dispatched
    ///     bare ~~~ then a call     -> [('bash',  6, 21)] dispatched
    ///     closed fence round it    -> []                 inert
    ///     open fence, inline span  -> []                 inert

    #[test]
    fn an_unclosed_fence_hides_nothing_from_the_story() {
        let ran = |src: &str| !strip_syscalls(src).contains("<bash>");
        assert!(
            ran("here:\n```bash\ngit status\n\n<bash>ls</bash>\n"),
            "stray info fence"
        );
        assert!(ran("a\n```\n<bash>ls</bash>\n"), "stray backtick fence");
        assert!(ran("a\n~~~\n<bash>ls</bash>\n"), "stray tilde fence");
        assert!(
            !ran("here:\n```bash\n<bash>ls</bash>\n```\ndone\n"),
            "closed fence"
        );
        // The fence never closes, so its lines are ordinary lines again --
        // including their backticks, which is the only reason this one is
        // inert to the kernel.
        assert!(
            !ran("a\n```\nuse `<bash>ls</bash>` here\n"),
            "inline span under a stray fence"
        );
    }

    /// The stream holds a bare `<bash>` back in case its closer is still
    /// coming. When the message ends without one the kernel dispatches
    /// nothing, so the hold has to be released or the tail of the sentence is
    /// printed nowhere.
    #[test]
    fn a_held_mention_is_released_when_the_stream_ends() {
        let mut app = App::new();
        apply_speech(
            &mut app.sess.story,
            &mut app.sess.calls,
            &mut app.sess.stream,
            "use the <bash> tool for that",
            true,
        );
        app.sess
            .stream
            .flush(&mut app.sess.story, &mut app.sess.calls);
        let held = (0..app.sess.story.len())
            .filter_map(|i| match app.sess.story.entry(i).map(|e| &e.block) {
                Some(RenderBlock::AgentMessage(m)) => Some(m.text()),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("");
        assert!(
            !held.contains("tool for that"),
            "printed before the closer could arrive"
        );
        app.sess
            .stream
            .finish(&mut app.sess.story, &mut app.sess.calls);
        let text = (0..app.sess.story.len())
            .filter_map(|i| match app.sess.story.entry(i).map(|e| &e.block) {
                Some(RenderBlock::AgentMessage(m)) => Some(m.text()),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("");
        assert_eq!(text.trim(), "use the <bash> tool for that");
    }

    fn first_speech(app: &App) -> Option<usize> {
        (0..app.sess.story.len()).find(|&i| {
            matches!(
                app.sess.story.entry(i).map(|e| &e.block),
                Some(RenderBlock::AgentMessage(_))
            )
        })
    }

    #[test]
    fn enter_zooms_speech_into_grok_block_viewer() {
        let mut app = App::new();
        seed_demo(&mut app);
        app.set_focus(Focus::Story);
        let idx = first_speech(&app).expect("speech");
        app.sess.story.set_selected(Some(idx));
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        );
        assert!(
            app.viewer.is_some(),
            "Enter on speech must open BlockViewerPane"
        );
        assert!(
            app.viewing.is_none(),
            "speech must not open a spawn session"
        );
        let text = paint(&mut app, 120, 36);
        assert!(
            text.contains("esc close") || text.contains("wrap") || text.contains("speech"),
            "viewer chrome missing:\n{text}"
        );
    }

    #[test]
    fn esc_closes_block_viewer() {
        let mut app = App::new();
        seed_demo(&mut app);
        app.set_focus(Focus::Story);
        let idx = first_speech(&app).expect("speech");
        app.sess.story.set_selected(Some(idx));
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        );
        assert!(app.viewer.is_some());
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE),
        );
        assert!(app.viewer.is_none());
    }

    #[test]
    fn ctrl_f_toggles_block_viewer() {
        let mut app = App::new();
        seed_demo(&mut app);
        app.set_focus(Focus::Story);
        let idx = first_speech(&app).expect("speech");
        app.sess.story.set_selected(Some(idx));
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char('f'), KeyModifiers::CONTROL),
        );
        assert!(app.viewer.is_some());
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char('f'), KeyModifiers::CONTROL),
        );
        assert!(app.viewer.is_none());
    }

    #[test]
    fn ctrl_f_on_post_opens_mid_inspect() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({
                "ev": "complete",
                "n": 4,
                "request": {
                    "model": "claude-opus-5",
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "WIREPROBE"}]}]
                },
                "response": {
                    "content": [{"type": "text", "text": "WIREANSWER"}]
                }
            }),
        );
        app.set_focus(Focus::PostIn);
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char('f'), KeyModifiers::CONTROL),
        );
        assert!(app.post_inspect.is_some());
        assert!(app.viewer.is_none());
        let text = paint(&mut app, 120, 36);
        assert!(
            text.contains("WIREPROBE"),
            "tree in popup missing request:\n{text}"
        );
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char(']'), KeyModifiers::NONE),
        );
        let out = paint(&mut app, 120, 36);
        assert!(
            out.contains("WIREANSWER"),
            "out tab missing response:\n{out}"
        );
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE),
        );
        assert!(app.post_inspect.as_ref().is_some_and(|p| p.raw));
        let raw = paint(&mut app, 120, 36);
        assert!(
            raw.contains("WIREANSWER"),
            "raw pretty POST out missing:\n{raw}"
        );
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE),
        );
        assert!(app.post_inspect.is_none());
    }

    #[test]
    fn viewer_rewraps_when_the_terminal_zooms() {
        let mut app = App::new();
        app.story_push(RenderBlock::agent_message("ZOOMWRAP ".repeat(80)));
        app.set_focus(Focus::Story);
        app.sess.story.set_selected(Some(0));
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        );
        assert!(app.viewer.is_some());
        let _ = paint(&mut app, 50, 24);
        let narrow = app
            .viewer
            .as_ref()
            .expect("viewer")
            .list_state
            .total_height();
        let _ = paint(&mut app, 140, 40);
        let wide = app
            .viewer
            .as_ref()
            .expect("viewer")
            .list_state
            .total_height();
        assert!(
            narrow > wide,
            "viewer must re-wrap at the new width: narrow={narrow} wide={wide}"
        );
    }

    /// Every card in the calls pane has one left edge. The bullet sits in the
    /// gutter and the header text starts two columns in; the body has to start
    /// there too, or a stack of cards has header text at one column and bodies
    /// at another and the pane reads as ragged.
    #[test]
    fn every_card_body_hangs_under_its_header() {
        let mut app = App::new();
        for ev in [
            json!({"ev":"result","phase":"done","tag":"bash","body":"ls -la crates",
                   "text":"total 8\ndrwxr-xr-x  desmos-tui\na line long enough that it has to wrap across more than one painted row"}),
            json!({"ev":"result","phase":"done","tag":"read","attrs":{"path":"a.py"},
                   "body":"","text":"one\ntwo\nthree"}),
        ] {
            handle_event(&mut app, ev);
        }
        // Twice: the first paint builds the layout the second one measures.
        let _ = paint(&mut app, 160, 120);
        let text = paint(&mut app, 160, 120);
        // Drop the pane border and the accent gutter; what is left is the
        // content column, where the bullet and the body both have to line up.
        let rows: Vec<String> = rows_of(&text, app.call_area)
            .lines()
            .skip(1)
            .map(|l| {
                l.trim_end()
                    .trim_end_matches('\u{2502}')
                    .chars()
                    .skip(2)
                    .collect::<String>()
                    .trim_end()
                    .to_string()
            })
            .take_while(|l| !l.starts_with('\u{2500}'))
            .filter(|l| !l.is_empty())
            .collect();
        let headers = rows.iter().filter(|l| l.starts_with("\u{25c6} ")).count();
        let bodies = rows.len() - headers;
        assert!(
            headers >= 2 && bodies >= 4,
            "need real cards with real bodies to prove anything: {rows:#?}"
        );
        for line in rows.iter().filter(|l| !l.starts_with("\u{25c6} ")) {
            assert!(
                line.starts_with("  ") && !line.starts_with("   "),
                "body row is not hung under the header text: {line:?}\nall rows: {rows:#?}"
            );
        }
    }

    /// Runtime state belongs on the composer, but a tool body still has one
    /// home: its Activity card. Neither the composer nor Meta may repeat it.
    #[test]
    fn the_tool_signal_never_repeats_the_syscall_body() {
        let mut app = App::new();
        app.running = true;
        handle_event(
            &mut app,
            json!({
                "ev": "result",
                "phase": "start",
                "tag": "bash",
                "body": "cat secret.env && curl -H 'Authorization: Bearer sk-live-42' https://example.com",
                "text": ""
            }),
        );
        assert!(
            app.sess.exec.live(),
            "exec must be live or meta has nothing to say about a syscall"
        );
        assert_eq!(input_signal(&app), Some(InputSignal::Tool));
        let painted = paint(&mut app, 120, 28);
        let input = rows_of(&painted, app.input_area);
        let meta = rows_of(&painted, app.cache.area);
        assert!(input.contains("Tool"), "{input}");
        for leak in ["secret", "sk-live", "curl", "Authorization", "example.com"] {
            assert!(!input.contains(leak), "composer leaked {leak}: {input}");
            assert!(!meta.contains(leak), "Meta leaked {leak}: {meta}");
        }
    }

    /// Dense packing left no blank rows at all, so a new user prompt read as
    /// one more block in the same run and the turn boundary vanished. The gap
    /// above a prompt is the only spacing the story spends -- and it is a gap
    /// ABOVE, so the reply below the prompt still packs tight against it.
    #[test]
    fn the_row_above_a_user_prompt_marks_the_turn() {
        let mut app = App::new();
        seed_demo(&mut app);
        // Twice: the first paint builds the layout the second one measures.
        let _ = paint(&mut app, 140, 120);
        let text = paint(&mut app, 140, 120);
        let rows = rows_of(&text, app.traj_area);
        let body: Vec<String> = rows
            .lines()
            .map(|l| l.trim_matches(|c| c == '\u{2502}' || c == ' ').to_string())
            .collect();
        let prompt = body
            .iter()
            .position(|l| l.starts_with("ok check cache"))
            .expect("seed_demo's second prompt is in the story");
        assert!(
            prompt > 0 && body[prompt - 1].is_empty(),
            "no blank row above the prompt at row {prompt}: {:?}",
            &body[prompt.saturating_sub(2)..=prompt]
        );
        assert!(
            !body[prompt + 1].is_empty(),
            "blank row BELOW the prompt too -- the turn gap became a global entry gap"
        );
    }

    /// Thinking now streams in Activity, so Story no longer reserves two blank
    /// rows beneath its tail. Both scrollbacks may use their full inner height.
    #[test]
    fn story_and_activity_use_their_full_viewports() {
        let mut app = App::new();
        seed_demo(&mut app);
        let _ = paint(&mut app, 140, 44);
        let _ = paint(&mut app, 140, 44);

        let (_, story_vp, _) = app.sess.story.scroll_info();
        let (_, activity_vp, _) = app.sess.calls.scroll_info();
        assert_eq!(story_vp, app.traj_area.height - 2);
        assert_eq!(activity_vp, app.call_area.height - 2);
    }

    /// Blank rows are the story's largest single expense: grok gaps every
    /// entry, which is ~10% of a chat column and was a third of this pane.
    #[test]
    fn the_story_does_not_spend_a_third_of_itself_on_blank_rows() {
        let mut app = App::new();
        seed_demo(&mut app);
        // Twice: the first paint builds the layout the second one measures.
        let _ = paint(&mut app, 140, 120);
        let text = paint(&mut app, 140, 120);
        let rows = rows_of(&text, app.traj_area);
        let all: Vec<&str> = rows.lines().collect();
        // Drop the pane's own two border rows; a border is not a blank row.
        let body: Vec<&str> = all[1..all.len() - 1]
            .iter()
            .map(|l| l.trim_matches(|c| c == '\u{2502}' || c == ' '))
            .collect();
        let last = body
            .iter()
            .rposition(|l| !l.is_empty())
            .expect("story has content");
        let body = &body[..=last];
        let blank = body.iter().filter(|l| l.is_empty()).count();
        assert!(
            blank * 4 < body.len(),
            "story is {blank}/{} blank rows; the gap knob is not reaching the layout",
            body.len()
        );
    }

    /// The launch theme is Oscura Midnight, and it is the tail of the
    /// precedence chain, not a clobber of it.
    #[test]
    fn nothing_named_a_theme_so_we_land_on_oscura_midnight() {
        assert_eq!(initial_theme_from(None, None), ThemeKind::OscuraMidnight);
        // An explicit groknight, from either source, still means groknight.
        assert_eq!(
            initial_theme_from(None, Some(ThemeKind::GrokNight)),
            ThemeKind::GrokNight
        );
        assert_eq!(
            initial_theme_from(Some("groknight"), None),
            ThemeKind::GrokNight
        );
        // Env outranks config; an unparseable env name falls through to it.
        assert_eq!(
            initial_theme_from(Some("tokyonight"), Some(ThemeKind::GrokDay)),
            ThemeKind::TokyoNight
        );
        assert_eq!(
            initial_theme_from(Some("not-a-theme"), Some(ThemeKind::GrokDay)),
            ThemeKind::GrokDay
        );
    }

    /// Wiring: the resolver is what App::new actually installs. Testing the
    /// function alone would pass just as well against the old call site.
    #[test]
    fn the_app_starts_on_the_theme_the_resolver_picked() {
        let _pin = theme_lock();
        let want = initial_theme();
        let _app = App::new();
        assert_eq!(Theme::current_kind(), want);
        // On a machine that names no theme -- the default install -- that is
        // Oscura Midnight, and grok's GrokNight fallback would fail here.
        if config_theme().is_none() && std::env::var("GROK_THEME").is_err() {
            assert_eq!(Theme::current_kind(), ThemeKind::OscuraMidnight);
        }
    }

    /// A local slash command never reaches the model: no turn runs, nothing
    /// lands in world.messages. It used to push a UserPrompt block anyway, so
    /// the story showed a turn that never happened.
    #[test]
    fn a_local_slash_command_leaves_no_turn_in_the_story() {
        let mut app = App::new();
        seed_demo(&mut app);
        let before = app.sess.story.len();
        // These commands write process-global appearance state. Put it back, or
        // this test silently re-themes whichever test runs next on this thread.
        let (theme0, ts0, dense0) = (
            Theme::current_kind(),
            appearance_cache::load_timestamps(),
            appearance_cache::load(),
        );
        let _pin = theme_lock();
        for cmd in [
            "/timestamps",
            "/dense",
            "/theme tokyonight",
            "/thinking high",
        ] {
            app.prompt.clear();
            for ch in cmd.chars() {
                app.prompt.insert_char(ch);
            }
            assert!(is_local_slash(cmd), "{cmd} must be handled locally");
            let quit = submit_prompt(None, &mut app).expect("submit");
            assert!(!quit);
            assert_eq!(
                app.sess.story.len(),
                before,
                "{cmd} put a turn in the story that never ran"
            );
            // Silent is worse than a stray row: every local command still says
            // it did something, on the composer's edge, for a few seconds.
            assert!(app.notice.is_some(), "{cmd} acknowledged nothing");
            app.notice = None;
        }
        theme_cache::set(theme0);
        appearance_cache::set_timestamps(ts0);
        appearance_cache::set(dense0);
        app.apply_grok_settings();
    }

    fn click(kind: MouseEventKind, col: u16, row: u16) -> MouseEvent {
        MouseEvent {
            kind,
            column: col,
            row,
            modifiers: KeyModifiers::NONE,
        }
    }

    #[test]
    fn mouse_drag_selects_and_persists_story_text() {
        let mut app = App::new();
        seed_demo(&mut app);
        app.set_focus(Focus::Story);
        let _ = paint(&mut app, 120, 36);
        let line = app
            .sess
            .story_sel
            .ranges
            .iter()
            .flat_map(|r| r.lines.iter())
            .find(|l| l.selectable_cols.end > l.selectable_cols.start + 3)
            .expect("selectable story text");
        let col = line.screen_x + line.selectable_cols.start;
        let row = line.screen_y;
        handle_mouse(
            &mut app,
            click(MouseEventKind::Down(MouseButton::Left), col, row),
        );
        assert!(app.sess.story_text.pending.is_some());
        handle_mouse(
            &mut app,
            click(MouseEventKind::Drag(MouseButton::Left), col + 3, row),
        );
        assert!(app.sess.story_text.active.is_some());
        handle_mouse(
            &mut app,
            click(MouseEventKind::Up(MouseButton::Left), col + 3, row),
        );
        assert!(app.sess.story_text.persist.is_some());
        assert_eq!(app.status, "copied");

        // The whole point of a notice: it is drawn, on the one piece of chrome
        // that is always on screen, and then it lapses.
        let text = paint(&mut app, 120, 36);
        let top = rows_of(
            &text,
            Rect {
                height: 2,
                ..app.input_area
            },
        );
        assert!(
            top.contains("copied"),
            "a notice must land on the composer's top edge:\n{top}"
        );
        let (t, msg) = app.notice.clone().expect("notice");
        app.notice = Some((t - NOTICE_TTL - Duration::from_secs(1), msg));
        assert!(
            expire_notice(&mut app),
            "a stale notice must ask for a repaint"
        );
        assert!(app.notice.is_none());
        let text = paint(&mut app, 120, 36);
        let top = rows_of(
            &text,
            Rect {
                height: 2,
                ..app.input_area
            },
        );
        assert!(
            !top.contains("copied"),
            "a lapsed notice must be gone:\n{top}"
        );
    }

    fn tab() -> KeyEvent {
        KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE)
    }

    fn backtab() -> KeyEvent {
        KeyEvent::new(KeyCode::BackTab, KeyModifiers::NONE)
    }

    fn press(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    /// Esc used to fall past every side pane into the quit at the bottom of the
    /// global branch, because `focused_scroll` maps each of them to the story
    /// and an unselected story means "nothing left to dismiss, so leave".
    #[test]
    fn esc_in_the_side_column_steps_back_instead_of_quitting() {
        let mut app = App::new();
        for (from, to) in [
            (Focus::Files, Focus::Git),
            (Focus::Git, Focus::Input),
            (Focus::Meter, Focus::Input),
        ] {
            app.set_focus(from);
            let quit = handle_key(None, &mut app, press(KeyCode::Esc)).unwrap();
            assert!(!quit, "Esc in {} must not quit", focus_name(from));
            assert_eq!(app.focus, to, "Esc in {}", focus_name(from));
        }
    }

    /// Every pane answers the arrows, and answers them about itself. The meter
    /// has no cursor, so its arrows must do nothing at all — they used to drive
    /// the story pane's selection from three panes away.
    #[test]
    fn arrows_stay_inside_the_pane_that_has_focus() {
        let mut app = App::new();
        seed_demo(&mut app);
        // A scrollback has no rows to select until it has been laid out once.
        paint(&mut app, 100, 40);
        app.set_focus(Focus::Story);
        app.sess.story.goto_top();
        handle_key(None, &mut app, press(KeyCode::Down)).unwrap();
        let moved = app.sess.story.selected();
        assert!(moved.is_some(), "↓ selects in the story");

        app.set_focus(Focus::Meter);
        handle_key(None, &mut app, press(KeyCode::Down)).unwrap();
        handle_key(None, &mut app, press(KeyCode::Up)).unwrap();
        assert_eq!(
            app.sess.story.selected(),
            moved,
            "the meter has no cursor, so its arrows must not move the story's"
        );

        app.queue.push("one".into());
        app.queue.push("two".into());
        app.set_focus(Focus::Queue);
        app.queue.selected = Some(1);
        handle_key(None, &mut app, press(KeyCode::Left)).unwrap();
        assert_eq!(app.queue.selected, Some(0), "← reorders the queue");

        app.set_focus(Focus::Git);
        let tab_before = app.git.tab;
        handle_key(None, &mut app, press(KeyCode::Right)).unwrap();
        assert_ne!(app.git.tab, tab_before, "→ walks the git tab strip");
    }

    /// Tab is a ring, and the ring has a direction: clockwise around the frame.
    /// Asserted against the rects draw actually assigned, not against the match
    /// arms -- an order that reads clockwise in source can still zig-zag on
    /// screen once the layout moves a pane.
    #[test]
    fn tab_walks_the_frame_clockwise() {
        let mut app = App::new();
        // Side panes are collapsed until they are opened, and a pane with no
        // rows is not on the ring at all.
        app.layout.git_h = 6;
        app.layout.files_h = 6;
        app.queue.push("later".into());
        let _ = paint(&mut app, 160, 60);
        let rect = |app: &App, f: Focus| match f {
            Focus::Story => app.traj_area,
            Focus::Calls => app.call_area,
            Focus::PostIn => app.post_in_area,
            Focus::PostOut => app.post_out_area,
            Focus::Queue => app.queue_area,
            Focus::Git => app.git_area,
            Focus::Files => app.files_area,
            Focus::Meter => app.cache.area,
            Focus::Input => app.input_area,
        };
        let ring = [
            Focus::Story,
            Focus::Calls,
            Focus::Git,
            Focus::Files,
            Focus::Meter,
            Focus::Input,
            Focus::Queue,
            Focus::PostOut,
            Focus::PostIn,
        ];
        // The cycle is exactly this ring, and Shift-Tab is exactly its inverse.
        let open = pane_open(&app);
        for (i, f) in ring.iter().enumerate() {
            let want = ring[(i + 1) % ring.len()];
            assert!(open(*f), "{} is not on screen", focus_name(*f));
            assert_eq!(f.next_open(&open), want, "Tab from {}", focus_name(*f));
            assert_eq!(
                want.prev_open(&open),
                *f,
                "Shift-Tab from {}",
                focus_name(want)
            );
        }
        // Down the right edge: x stays on the right column, y only grows.
        for pair in [
            (Focus::Calls, Focus::Git),
            (Focus::Git, Focus::Files),
            (Focus::Files, Focus::Meter),
        ] {
            let (a, b) = (rect(&app, pair.0), rect(&app, pair.1));
            assert_eq!(a.x, b.x, "{} left the right column", focus_name(pair.1));
            assert!(a.y < b.y, "{} went up, not down", focus_name(pair.1));
        }
        // Up the left edge: x stays left, y only shrinks.
        for pair in [
            (Focus::Input, Focus::Queue),
            (Focus::Queue, Focus::PostOut),
            (Focus::PostOut, Focus::PostIn),
        ] {
            let (a, b) = (rect(&app, pair.0), rect(&app, pair.1));
            assert!(a.y >= b.y, "{} went down, not up", focus_name(pair.1));
        }
        // The POST split is one row of two panes, so climbing the left edge
        // enters it on the right and leaves on the left.
        assert!(rect(&app, Focus::PostIn).x < rect(&app, Focus::PostOut).x);
        // The two crossings: top-left to top-right, bottom-right to bottom-left.
        assert!(rect(&app, Focus::Story).x < rect(&app, Focus::Calls).x);
        assert!(rect(&app, Focus::Input).x < rect(&app, Focus::Meter).x);
    }

    #[test]
    fn tab_skips_empty_queue() {
        let mut app = App::new();
        // Focus follows the rects draw assigned, so a pane has to have been
        // painted before Tab can land on it.
        let _ = paint(&mut app, 140, 40);
        app.set_focus(Focus::Input);
        handle_key(None, &mut app, tab()).unwrap();
        assert_eq!(app.focus, Focus::PostOut, "empty queue must not take Tab");
        handle_key(None, &mut app, backtab()).unwrap();
        assert_eq!(app.focus, Focus::Input, "Shift-Tab must skip empty queue");
        app.queue.push("later".into());
        handle_key(None, &mut app, tab()).unwrap();
        assert_eq!(app.focus, Focus::Queue);
        handle_key(None, &mut app, tab()).unwrap();
        assert_eq!(app.focus, Focus::PostOut);
        handle_key(None, &mut app, backtab()).unwrap();
        assert_eq!(app.focus, Focus::Queue);
    }

    #[test]
    fn set_focus_refuses_empty_queue() {
        let mut app = App::new();
        app.set_focus(Focus::Queue);
        assert_eq!(app.focus, Focus::Input);
        app.queue.push("x".into());
        app.set_focus(Focus::Queue);
        assert_eq!(app.focus, Focus::Queue);
    }

    #[test]
    fn ready_snapshot_fills_chrome() {
        let mut app = App::new();
        assert!(!app.ready);
        let before = paint(&mut app, 120, 24);
        assert!(
            before.contains("effort —") && before.contains("gen —"),
            "empty chrome before ready:\n{before}"
        );
        handle_event(
            &mut app,
            json!({
                "ev": "ready",
                "model": "claude-opus-5",
                "thinking": "low",
                "generation": 7
            }),
        );
        assert!(app.ready);
        assert_eq!(app.model, "claude-opus-5");
        assert_eq!(app.thinking, "low");
        assert_eq!(app.generation, "7");
        let after = paint(&mut app, 120, 24);
        // In the meta pane, not on the composer's bottom border where it used
        // to run — a strip of text under a box that grows as you type.
        let meta = rows_of(&after, app.cache.area);
        assert!(meta.contains("claude-opus-5"), "{meta}");
        assert!(meta.contains("effort low"), "{meta}");
        assert!(meta.contains("gen 7"), "{meta}");
        assert!(!after.contains("effort —"), "{after}");
        assert!(!after.contains("gen —"), "{after}");
        let card = rows_of(&after, app.input_area);
        assert!(
            !card.contains("gen 7"),
            "the composer still carries identity:\n{card}"
        );
    }

    #[test]
    fn a_multiline_composer_is_not_labelled() {
        // Wrapping to a second row is visible in the box itself. Labelling it
        // spent a title slot on something already on screen, and painted a
        // success-green accent on a state that is neither a success nor an
        // event.
        let mut app = App::new();
        app.prompt.insert_str("first line\nsecond line");
        assert!(app.prompt.is_multiline());
        let painted = paint(&mut app, 120, 24);
        let card = rows_of(&painted, app.input_area);
        assert!(
            card.contains("second line"),
            "composer lost its text:\n{card}"
        );
        assert!(
            !card.contains("multiline"),
            "composer still labels itself:\n{card}"
        );
    }

    #[test]
    fn long_composer_is_a_caret_following_internal_viewport() {
        let mut app = App::new();
        app.set_focus(Focus::Input);
        let body = (0..30)
            .map(|n| format!("line-{n:02}"))
            .collect::<Vec<_>>()
            .join("\n");
        app.prompt.insert_str(&body);

        let tail = paint(&mut app, 80, 24);
        let tail_card = rows_of(&tail, app.input_area);
        assert!(
            app.input_scroll > 0,
            "long prompt never entered viewport mode"
        );
        assert!(
            tail_card.contains("line-29"),
            "caret tail is not visible:\n{tail_card}"
        );
        assert!(
            !tail_card.contains("line-00"),
            "viewport did not scroll:\n{tail_card}"
        );

        for _ in 0..40 {
            handle_key(None, &mut app, press(KeyCode::Up)).unwrap();
        }
        let head = paint(&mut app, 80, 24);
        let head_card = rows_of(&head, app.input_area);
        assert_eq!(
            app.input_scroll, 0,
            "Up did not scroll back to the first row"
        );
        assert!(
            head_card.contains("line-00"),
            "prompt head is not visible:\n{head_card}"
        );
        let backend = TestBackend::new(80, 24);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        let pos = term.get_cursor_position().expect("composer cursor");
        assert!(
            hit(app.input_inner, pos.x, pos.y),
            "caret escaped its viewport"
        );
    }

    #[test]
    fn runtime_state_animates_the_composer_in_three_distinct_colors() {
        let mut inference = App::new();
        inference.running = true;
        inference.turn_started = Some(Instant::now());
        start_thinking(&mut inference.sess.calls, &mut inference.sess.stream);
        let (first, inference_color, first_mod) = paint_input_state(&mut inference, 140, 30);
        assert!(rows_of(&first, inference.input_area).contains("Inference"));
        let meta = rows_of(&first, inference.cache.area);
        assert!(
            !meta.contains("idle") && !meta.contains("thinking"),
            "{meta}"
        );
        assert!(rows_of(&first, inference.input_area).contains("[stop]"));
        assert!(
            inference.turn_cancel.is_some(),
            "running composer lost [stop] hit box"
        );

        for _ in 0..6 {
            inference.sess.story.tick();
        }
        let (_, _, second_mod) = paint_input_state(&mut inference, 140, 30);
        assert_ne!(
            first_mod.contains(Modifier::BOLD),
            second_mod.contains(Modifier::BOLD),
            "the live composer border did not pulse"
        );

        // Queued is what the composer says when nothing is running -- and it
        // carries no count, because the queue pane owns that number.
        let mut queued = App::new();
        queued.queue.push("next prompt".into());
        let (queued_text, queued_color, _) = paint_input_state(&mut queued, 140, 30);
        let queued_rows = rows_of(&queued_text, queued.input_area);
        assert!(queued_rows.contains("Queued"), "{queued_rows}");
        assert!(
            !queued_rows.contains("Queued 1"),
            "composer duplicates the queue count:\n{queued_rows}"
        );

        // A queued follow-up must not hide what the turn is actually doing.
        let mut busy = App::new();
        busy.running = true;
        busy.turn_started = Some(Instant::now());
        start_thinking(&mut busy.sess.calls, &mut busy.sess.stream);
        busy.queue.push("next prompt".into());
        let (busy_text, _, _) = paint_input_state(&mut busy, 140, 30);
        let busy_rows = rows_of(&busy_text, busy.input_area);
        assert!(
            busy_rows.contains("Inference") && !busy_rows.contains("Queued"),
            "{busy_rows}"
        );

        let mut tool = App::new();
        tool.running = true;
        tool.queue.push("queued behind tool".into());
        handle_event(
            &mut tool,
            json!({"ev":"result","phase":"start","tag":"bash","attrs":{},"body":"echo hi"}),
        );
        let (tool_text, tool_color, _) = paint_input_state(&mut tool, 140, 30);
        assert!(rows_of(&tool_text, tool.input_area).contains("Tool"));
        assert_ne!(inference_color, queued_color);
        assert_ne!(queued_color, tool_color);
        assert_ne!(inference_color, tool_color);
    }

    #[test]
    fn activity_stack_keeps_a_muted_tool_hue_off_selection() {
        let _theme = theme_lock();
        let mut app = App::new();
        app.focus = Focus::Calls;
        app.tree_open = true;
        {
            let first = app.ensure_child("first", "first task");
            first.agent = "explore".into();
            first.state = "running".into();
        }
        {
            let second = app.ensure_child("second", "second task");
            second.agent = "review".into();
            second.state = "done".into();
        }

        let backend = TestBackend::new(70, 8);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw_tree_pane(f, Rect::new(0, 0, 70, 8), &mut app))
            .unwrap();
        let theme = Theme::current();
        let buf = term.backend().buffer();
        assert_eq!(
            buf[(1, 1)].fg,
            theme.accent_tool,
            "selected run lost its tool hue"
        );
        assert_eq!(
            buf[(1, 2)].fg,
            less_saturated(theme.accent_tool),
            "resting run fell back to generic gray"
        );
        assert_eq!(
            less_saturated(Color::Rgb(0, 180, 255)),
            Color::Rgb(53, 161, 206),
            "desaturation no longer preserves a recognizable source hue"
        );
    }

    #[test]
    fn pane_titles_share_one_chrome_style_and_meta_owns_no_runtime_state() {
        let mut app = App::new();
        app.running = true;
        let text = paint(&mut app, 140, 34);
        assert!(text.contains(" Story "), "{text}");
        assert!(text.contains(" Activity "), "{text}");
        assert!(text.contains(" Meta "), "{text}");
        let meta = rows_of(&text, app.cache.area);
        assert!(
            !meta.contains("idle") && !meta.contains("waiting"),
            "{meta}"
        );
        assert!(
            !meta.lines().next().unwrap_or_default().contains("cache"),
            "{meta}"
        );
    }

    /// The composer grows with what is typed, and it measures against the
    /// column it lives in. Rows used to be counted at the full frame width
    /// while the box was painted at the story column's — roughly half of it —
    /// so a paragraph overflowed a box that had decided it needed three rows.
    #[test]
    fn the_composer_grows_to_fit_at_its_own_width() {
        let mut app = App::new();
        let idle = {
            let _ = paint(&mut app, 140, 40);
            app.input_area.height
        };
        let body = "the quick brown fox jumps over the lazy dog and keeps \
                    on running until the sentence is long enough to need \
                    several rows of a composer that is only half the frame. \
                    This second passage keeps going through another collection \
                    of deliberately ordinary words so the prompt exceeds the \
                    generous eight-row default and proves the composer still \
                    grows when a genuinely long request needs the space."
            .repeat(3);
        app.prompt.handle_paste(&body);
        let text = paint(&mut app, 140, 40);
        assert!(
            app.input_area.height > idle,
            "composer did not grow: {} -> {}",
            idle,
            app.input_area.height
        );
        // Every row it claims to need must be a row it actually got.
        let inner_w = app.input_area.width.saturating_sub(2);
        let want = app.prompt.display_rows(inner_w);
        assert!(
            app.input_area.height >= want + 3,
            "needs {want} text rows, box is {}",
            app.input_area.height
        );
        // And the tail is on screen rather than clipped off the bottom.
        let card = rows_of(&text, app.input_area);
        assert!(card.contains("frame"), "composer clipped its tail:\n{card}");
    }

    /// The painted rows a rect covers, joined — for asserting *where* a string
    /// landed rather than that it landed at all.
    /// Background work is the one thing Meta must say out loud: while a monitor
    /// holds a shell, waiting is correct and polling is not. Driven through the
    /// real event handler, because a row nothing sets is a row nothing shows.
    #[test]
    fn meta_names_background_work_that_will_resume_the_session() {
        let mut app = App::new();
        let _ = paint(&mut app, 160, 48);
        let idle = rows_of(&paint(&mut app, 160, 48), app.cache.area);
        assert!(
            !idle.contains("waiting"),
            "idle Meta claims background work:\n{idle}"
        );

        handle_event(
            &mut app,
            json!({"ev": "pending", "n": 1, "tasks": ["shell main [t7-abc]"]}),
        );
        assert_eq!(
            app.background.len(),
            1,
            "the pending event never reached App"
        );
        let busy = rows_of(&paint(&mut app, 160, 48), app.cache.area);
        assert!(
            busy.contains("1 waiting") && busy.contains("shell main"),
            "Meta does not name the work holding the session:\n{busy}"
        );

        handle_event(&mut app, json!({"ev": "pending", "n": 0, "tasks": []}));
        let cleared = rows_of(&paint(&mut app, 160, 48), app.cache.area);
        assert!(
            !cleared.contains("waiting"),
            "the row outlived the task:\n{cleared}"
        );
    }

    fn rows_of(text: &str, area: Rect) -> String {
        text.lines()
            .skip(area.y as usize)
            .take(area.height.max(1) as usize)
            .map(|l| {
                l.chars()
                    .skip(area.x as usize)
                    .take(area.width as usize)
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn click_stop_requests_cancel() {
        let mut app = App::new();
        app.running = true;
        app.turn_started = Some(Instant::now());
        app.status = "running".into();
        start_thinking(&mut app.sess.calls, &mut app.sess.stream);
        let _ = paint(&mut app, 140, 30);
        let area = app.turn_cancel.expect("cancel hit area");
        handle_mouse(
            &mut app,
            click(MouseEventKind::Down(MouseButton::Left), area.x, area.y),
        );
        assert!(app.want_stop);
    }

    #[test]
    fn mouse_reaches_git_files_and_meta() {
        let mut app = App::new();
        app.layout.git_h = 6;
        app.layout.files_h = 8;
        app.layout.meter_h = 8;
        app.set_focus(Focus::Story);
        let _ = paint(&mut app, 140, 50);
        assert!(app.git_area.height >= 3 && app.files_area.height >= 3);

        // Meta: click anywhere inside and it takes focus. It has no cursor, so
        // that is the whole contract.
        let meta = app.cache.area;
        handle_mouse(
            &mut app,
            click(
                MouseEventKind::Down(MouseButton::Left),
                meta.x + 2,
                meta.y + 1,
            ),
        );
        assert_eq!(app.focus, Focus::Meter);

        // Git: the tab strip is in the border title, so the top row is clickable.
        let git = app.git_area;
        let log = side::GitTab::Log;
        let label_x = git_tab_x(git, log);
        handle_mouse(
            &mut app,
            click(MouseEventKind::Down(MouseButton::Left), label_x, git.y),
        );
        assert_eq!(app.focus, Focus::Git);
        assert_eq!(app.git.tab, log, "clicking a tab label must select it");

        // Files: the wheel moves the cursor, a click lands on a row, and a
        // click on the frame moves nothing.
        let _ = paint(&mut app, 140, 50);
        let files = app.files_area;
        app.files.sel = 0;
        handle_mouse(
            &mut app,
            click(MouseEventKind::ScrollDown, files.x + 2, files.y + 2),
        );
        assert!(
            app.files.sel > 0,
            "wheel over the file pane must move its cursor"
        );
        let row = 2u16;
        handle_mouse(
            &mut app,
            click(
                MouseEventKind::Down(MouseButton::Left),
                files.x + 2,
                files.y + 1 + row,
            ),
        );
        assert_eq!(app.focus, Focus::Files);
        assert_eq!(app.files.sel, app.files.scroll + row as usize);

        let fixed = app.files.sel;
        handle_mouse(
            &mut app,
            click(
                MouseEventKind::Down(MouseButton::Left),
                files.x + 2,
                files.y,
            ),
        );
        assert_eq!(app.files.sel, fixed, "border clicks must not move a cursor");
    }

    /// `error` is not a terminator. loop.py fires it for a reply the endpoint
    /// cut short and keeps looping, and the reader thread synthesises one for
    /// any unparseable NDJSON line — run_turns still emits exactly one
    /// done/stopped afterwards. Clearing `running` here read as idle mid-step,
    /// so Enter sent a second op:step that the bridge fired out of order.
    #[test]
    fn an_error_event_does_not_end_the_step() {
        let mut app = App::new();
        app.ready = true;
        app.running = true;
        app.turn_started = Some(Instant::now());
        handle_event(&mut app, json!({"ev": "turn", "n": 1}));
        handle_event(
            &mut app,
            json!({"ev": "error", "n": 1, "text": "[reply was cut short: max_tokens]"}),
        );
        assert!(
            app.running,
            "error ended a step that run_turns is still running"
        );
        assert!(
            app.turn_started.is_some(),
            "the turn clock was reset mid-step"
        );
        handle_event(&mut app, json!({"ev": "turn", "n": 2}));
        handle_event(&mut app, json!({"ev": "done"}));
        assert!(!app.running, "done is the terminator");
    }

    /// Phase 3 tree fields: every subagent/child event carries the kernel's
    /// `parent` + `depth`, and the ChildSess keeps them so the Phase 4 tree
    /// view (upgrade-paths 3.2) can nest children under their spawner.
    #[test]
    fn child_sessions_store_the_kernels_parent_and_depth() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({"ev": "subagent", "phase": "started", "id": "aaaa1111",
                   "parent": null, "depth": 0, "agent": "explore",
                   "persona": "", "task": "root child", "structured": false,
                   "model": "m"}),
        );
        let c = &app.children["aaaa1111"];
        assert_eq!(c.parent, None, "a root spawn has no parent");
        assert_eq!(c.depth, 0, "a root spawn sits at depth 0");
        // A grandchild first seen through its child envelope (late attach:
        // no `started` event observed) still lands at its spot in the tree.
        handle_event(
            &mut app,
            json!({"ev": "child", "id": "cccc3333", "parent": "bbbb2222",
                   "depth": 2, "kind": "speech", "text": "hi", "delta": false}),
        );
        let c = &app.children["cccc3333"];
        assert_eq!(c.parent.as_deref(), Some("bbbb2222"));
        assert_eq!(c.depth, 2);
    }

    /// EntryIds are handed out per ScrollbackState starting at 1, so one shared
    /// pin set made the parent's first wire card and every child's first wire
    /// card the same id: folding one pinned the other, in a pane nobody
    /// touched.
    #[test]
    fn a_wire_fold_pins_the_pane_it_was_made_in() {
        let mut app = App::new();
        let mut parent = Vec::new();
        for i in 0..8 {
            parent.push(wire_push(
                &mut app.sess.calls,
                RenderBlock::agent_message(format!("P{i}")),
            ));
        }
        app.sess.calls.set_selected(Some(0));
        pin_selected_wire(&mut app);
        set_wire_mode(&mut app.sess.calls, parent[0], DisplayMode::Collapsed);

        let mut kid = Vec::new();
        {
            let child = app.ensure_child("kid", "task");
            for i in 0..8 {
                kid.push(wire_push(
                    &mut child.sess.calls,
                    RenderBlock::agent_message(format!("C{i}")),
                ));
            }
        }
        assert_eq!(parent[0], kid[0], "ids are per pane, so they collide");

        app.viewing = Some("kid".into());
        set_wire_mode(
            &mut app.children.get_mut("kid").unwrap().sess.calls,
            kid[0],
            DisplayMode::Expanded,
        );
        let _ = paint(&mut app, 140, 40);
        let mode = app.children["kid"]
            .sess
            .calls
            .get_by_id(kid[0])
            .map(|e| e.display_mode);
        assert_eq!(
            mode,
            Some(DisplayMode::Collapsed),
            "the parent's pin held a stale child card open"
        );

        // And a fold made inside the child survives the next frame.
        let last = *kid.last().unwrap();
        app.children
            .get_mut("kid")
            .unwrap()
            .sess
            .calls
            .set_selected(Some(7));
        pin_selected_wire(&mut app);
        set_wire_mode(
            &mut app.children.get_mut("kid").unwrap().sess.calls,
            last,
            DisplayMode::Collapsed,
        );
        let _ = paint(&mut app, 140, 40);
        let mode = app.children["kid"]
            .sess
            .calls
            .get_by_id(last)
            .map(|e| e.display_mode);
        assert_eq!(
            mode,
            Some(DisplayMode::Collapsed),
            "reflow re-expanded a card the reader folded in the child pane"
        );
        assert_eq!(
            app.sess.wire_manual.len(),
            1,
            "a child fold landed in the parent's set"
        );
    }

    /// A subagent's POST is billed to the same key. It used to be spent and
    /// shown nowhere; it still must not repaint the bars that describe the
    /// parent's transcript.
    #[test]
    fn a_subagent_bills_the_meter_without_taking_the_context_bar() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({"ev": "complete", "n": 1, "model": "claude-opus-5",
                   "usage": {"input_tokens": 1000, "output_tokens": 100,
                             "cache_read_input_tokens": 2000}}),
        );
        let parent_spent = app.cache.spent;
        let parent_fresh = app.cache.fresh;
        handle_event(
            &mut app,
            json!({"ev": "child", "id": "kid", "kind": "complete", "n": 1,
                   "model": "claude-opus-5",
                   "usage": {"input_tokens": 500_000, "output_tokens": 1000}}),
        );
        assert!(
            app.cache.spent > parent_spent,
            "a subagent's tokens never reached the money row"
        );
        assert_eq!(
            app.cache.calls, 2,
            "the child's POST is a call that happened"
        );
        assert_eq!(
            app.cache.fresh, parent_fresh,
            "the child overwrote the parent's context bar"
        );
    }

    /// Esc clears every selection on screen. `||` and `any` short-circuited at
    /// the first pane that had one, so with a highlight on both panes it took
    /// two presses and the second one also left the child session.
    #[test]
    fn esc_clears_both_panes_in_one_press() {
        let mut app = App::new();
        let sel = PersistentTextSelection {
            entry_idx: 0,
            range_id: 0,
            anchor: SelectionEndpoint {
                block_line_idx: 0,
                col_within_range: 0,
            },
            head: SelectionEndpoint {
                block_line_idx: 0,
                col_within_range: 3,
            },
            origin: SelectionOrigin::Drag,
            kind: SelectionKind::Linear,
        };
        app.sess.story_text.persist = Some(sel);
        app.sess.calls_text.persist = Some(sel);
        handle_key(None, &mut app, press(KeyCode::Esc)).unwrap();
        assert!(app.sess.story_text.persist.is_none());
        assert!(
            app.sess.calls_text.persist.is_none(),
            "the wire pane kept its highlight"
        );
    }

    /// ctrl+←/→ on the POST panes moves a divider that draw ignored: the row
    /// was split 50/50 whatever the layout said, saved and reloaded.
    #[test]
    fn the_post_divider_is_where_the_layout_says() {
        let mut app = App::new();
        let _ = paint(&mut app, 140, 40);
        let even = app.post_in_area.width;
        app.layout.grow_axis(Focus::PostIn, Axis::Horizontal, 20);
        let _ = paint(&mut app, 140, 40);
        assert!(
            app.post_in_area.width > even,
            "the POST divider ignored the layout: {} vs {even}",
            app.post_in_area.width
        );
        assert_eq!(
            app.post_in_area.width + app.post_out_area.width,
            app.traj_area.width,
            "the two halves must still fill the row"
        );
    }

    /// Focus follows the rects draw assigned. On a short terminal the frame
    /// clamps a requested pane to nothing, and Tab used to land on it anyway —
    /// j/k then went to a pane with no rows on screen.
    #[test]
    fn focus_skips_a_pane_the_frame_clamped_away() {
        let mut app = App::new();
        let _ = paint(&mut app, 100, 12);
        let open = pane_open(&app);
        for (focus, area) in [
            (Focus::Files, app.files_area),
            (Focus::Git, app.git_area),
            (Focus::PostIn, app.post_in_area),
            (Focus::Meter, app.cache.area),
        ] {
            assert_eq!(
                open(focus),
                area.height > 0,
                "{focus:?} claims to be open with {} rows",
                area.height
            );
        }
        assert!(
            app.files_area.height == 0 || app.git_area.height == 0,
            "12 rows should not fit both side panes; the test needs a smaller frame"
        );
        app.set_focus(Focus::Story);
        for _ in 0..12 {
            handle_key(None, &mut app, tab()).unwrap();
            let rows = match app.focus {
                Focus::Files => app.files_area.height,
                Focus::Git => app.git_area.height,
                Focus::PostIn => app.post_in_area.height,
                Focus::PostOut => app.post_out_area.height,
                Focus::Meter => app.cache.area.height,
                _ => 1,
            };
            assert!(rows > 0, "Tab landed on {:?}, which has no rows", app.focus);
        }
    }

    /// The work row's git tail comes from the git pane's background snapshot.
    /// `sync` used to fork `git rev-parse` plus `git status --porcelain`
    /// itself, and it runs after every syscall result and every finished
    /// thought — on the thread that draws. `git status` measures 0.28s warm in
    /// this repo, so a burst of results stalled the frame for seconds.
    #[test]
    fn a_burst_of_results_does_not_shell_out_on_the_ui_thread() {
        let mut app = App::new();
        app.ready = true;
        let started = Instant::now();
        for i in 0..16 {
            handle_event(
                &mut app,
                json!({"ev": "result", "phase": "done", "tag": "bash",
                       "attrs": {}, "body": format!("ls {i}"), "text": "ok"}),
            );
        }
        let took = started.elapsed();
        assert!(
            took < Duration::from_millis(500),
            "16 results took {took:?} — the event path is forking a process again"
        );
    }

    #[test]
    fn choosing_new_creates_an_empty_visual_session() {
        let mut app = App::new();
        app.ready = true;
        app.story_push(RenderBlock::user_prompt("old prompt"));
        app.session_picker = session::SessionPicker::with_sessions(
            vec![session::SessionRow {
                id: "0191aaaa".into(),
                started_at: "2026-08-17T04:00:00".into(),
                messages: 2,
                preview: "earlier line".into(),
            }],
            vec![session::Turn {
                prompt: "old prompt".into(),
                speech: "old answer".into(),
            }],
        );

        apply_session_choice(None, &mut app, session::Choice::New).unwrap();

        let screen = paint(&mut app, 100, 30);
        assert!(
            !screen.contains("old prompt"),
            "new session retained history:\n{screen}"
        );
    }

    #[test]
    fn choosing_resume_rebuilds_saved_turns_in_the_story() {
        let mut app = App::new();
        app.ready = true;
        app.session_picker = session::SessionPicker::with_sessions(
            vec![session::SessionRow {
                id: "0191aaaa".into(),
                started_at: "2026-08-17T04:00:00".into(),
                messages: 2,
                preview: "saved question".into(),
            }],
            vec![session::Turn {
                prompt: "saved question".into(),
                speech: "saved answer".into(),
            }],
        );

        apply_session_choice(None, &mut app, session::Choice::Resume("0191aaaa".into())).unwrap();

        let screen = paint(&mut app, 100, 30);
        assert!(
            screen.contains("saved question"),
            "prompt was not resumed:\n{screen}"
        );
        assert!(
            screen.contains("saved answer"),
            "answer was not resumed:\n{screen}"
        );
    }

    #[test]
    fn rendered_entry_point_wires_picker_enter_to_resume() {
        let mut app = App::new();
        app.ready = true;
        app.session_picker = session::SessionPicker::with_sessions(
            vec![session::SessionRow {
                id: "0191aaaa".into(),
                started_at: "2026-08-17T04:00:00".into(),
                messages: 2,
                preview: "entry point question".into(),
            }],
            vec![session::Turn {
                prompt: "entry point question".into(),
                speech: "entry point answer".into(),
            }],
        );

        let picker = paint(&mut app, 100, 30);
        assert!(
            picker.contains("Which session?"),
            "startup picker not rendered:\n{picker}"
        );
        handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        )
        .unwrap();
        let resumed = paint(&mut app, 100, 30);
        assert!(
            resumed.contains("entry point question"),
            "Enter was not wired to resume:\n{resumed}"
        );
        assert!(!resumed.contains("Which session?"));
    }

    /// The x of a tab label in the git title strip, laid out as `draw_git` does.
    fn git_tab_x(area: Rect, want: side::GitTab) -> u16 {
        let mut x = area.x + 2;
        for tab in side::GitTab::ALL {
            if tab == want {
                return x + 1;
            }
            x += tab.label().chars().count() as u16 + 2;
        }
        x
    }

    // ── inline images ────────────────────────────────────────────────

    /// A 1x1 transparent PNG: small enough to inline, real enough that
    /// `ScrollbackImageRef::from_path` decodes dimensions off it and the
    /// Kitty path recognises the magic and transmits it unconverted.
    const PNG_1X1: &[u8] = &[
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44,
        0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1F,
        0x15, 0xC4, 0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x63, 0x00,
        0x01, 0x00, 0x00, 0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00, 0x00, 0x00, 0x00, 0x49,
        0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
    ];

    fn png_file(name: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("desmos-tui-{name}.png"));
        std::fs::write(&p, PNG_1X1).unwrap();
        p
    }

    #[test]
    fn media_block_only_for_decodable_images() {
        let txt = std::env::temp_dir().join("desmos-tui-not-an-image.txt");
        std::fs::write(&txt, b"words").unwrap();
        assert!(media_block(txt.to_str().unwrap()).is_none());
        assert!(media_block("/no/such/file.png").is_none());
        let png = png_file("block");
        assert!(media_block(png.to_str().unwrap()).is_some());
    }

    /// The wiring, not the renderer. An attachment on a prompt has to reach
    /// the story as a media block, survive a real `draw`, and come back out
    /// as a placement the flush turns into Kitty escapes -- then be deleted
    /// once it stops being drawn, or the picture outlives its cells.
    #[test]
    fn attached_image_places_then_clears() {
        assert_eq!(
            gfx::protocol_for_brand(xai_grok_pager::terminal::TerminalName::Ghostty, false),
            gfx::GraphicsProtocol::Kitty,
            "Ghostty must select the real Kitty graphics protocol",
        );
        let _kitty = gfx::set_protocol_for_test(gfx::GraphicsProtocol::Kitty);
        let png = png_file("attach");
        let mut app = App::new();
        start_step(
            None,
            &mut app,
            "look at this".into(),
            vec![png.to_string_lossy().into_owned()],
        )
        .unwrap();

        app.media.frame.clear();
        let backend = TestBackend::new(140, 40);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        assert!(
            !app.media.frame.is_empty(),
            "draw produced no inline-media placement"
        );

        let mut out: Vec<u8> = Vec::new();
        flush_media(&mut app, &mut out).unwrap();
        let esc = String::from_utf8_lossy(&out).into_owned();
        assert!(esc.contains("\x1b_Ga=t"), "image was never transmitted");
        assert!(esc.contains("a=p,i="), "image was never placed");
        assert_eq!(app.media.placed.len(), 1, "placement not recorded");

        let mut gone: Vec<u8> = Vec::new();
        flush_media(&mut app, &mut gone).unwrap();
        let esc = String::from_utf8_lossy(&gone).into_owned();
        assert!(esc.contains("a=d,d=i,i="), "stale placement never cleared");
        assert!(app.media.placed.is_empty());
    }

    /// A `see` card carries the paths it attached, so the Activity pane draws
    /// the picture the model just looked at. No separate plumbing: the block
    /// scrapes image paths out of its own output text.
    #[test]
    fn a_see_card_renders_its_image() {
        let _kitty = gfx::set_protocol_for_test(gfx::GraphicsProtocol::Kitty);
        let png = png_file("see");
        let p = png.to_string_lossy().into_owned();
        let card = wire_syscall(
            "workspace",
            &p,
            &json!({"op": "see"}),
            &format!("attached 1 image(s): {p} [1KB]"),
            None,
        );
        let mut app = App::new();
        app.call_push(card);
        app.media.frame.clear();
        let backend = TestBackend::new(140, 40);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, &mut app)).unwrap();
        assert!(
            !app.media.frame.is_empty(),
            "see card produced no image placement"
        );
    }

    /// An open modal paints over the story, but a placement is drawn above
    /// the cell background -- the image would show through. The frame has to
    /// come back empty so the flush deletes it.
    #[test]
    fn a_modal_takes_the_images_down() {
        let _kitty = gfx::set_protocol_for_test(gfx::GraphicsProtocol::Kitty);
        let png = png_file("modal");
        let mut app = App::new();
        start_step(
            None,
            &mut app,
            "look".into(),
            vec![png.to_string_lossy().into_owned()],
        )
        .unwrap();
        let backend = TestBackend::new(140, 40);
        let mut term = Terminal::new(backend).unwrap();

        app.media.frame.clear();
        term.draw(|f| draw(f, &mut app)).unwrap();
        assert!(!app.media.frame.is_empty(), "no image to hide");

        app.help = true;
        app.media.frame.clear();
        term.draw(|f| draw(f, &mut app)).unwrap();
        assert!(app.media.frame.is_empty(), "image left under the modal");
    }

    /// Without inline graphics nothing is written to the terminal at all --
    /// the renderer falls back to a text `[Open]` row on its own.
    #[test]
    fn no_graphics_writes_no_escapes() {
        let _none = gfx::set_protocol_for_test(gfx::GraphicsProtocol::None);
        let mut app = App::new();
        app.media.frame.clear();
        let mut out: Vec<u8> = Vec::new();
        flush_media(&mut app, &mut out).unwrap();
        assert!(out.is_empty());
    }

    #[test]
    fn files_tree_row_paints_icons_guides_and_strong_selection() {
        let _theme = theme_lock();
        let root = std::env::temp_dir().join(format!(
            "desmos-files-render-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(root.join("folder")).unwrap();
        std::fs::write(root.join("a_very_long_file_name.rs"), "fn main() {}\n").unwrap();

        let mut app = App::new();
        app.files = side::FilePane::new(&root);
        app.focus = Focus::Files;
        app.files.sel = app
            .files
            .entries
            .iter()
            .position(|row| row.name == "a_very_long_file_name.rs")
            .unwrap();

        let backend = TestBackend::new(18, 8);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw_files(f, Rect::new(0, 0, 18, 8), &mut app))
            .unwrap();
        let buf = term.backend().buffer();
        let selected_y = 1 + app.files.sel as u16;
        let selected = &buf[(5, selected_y)];
        assert_eq!(
            buf[(1, selected_y)].symbol(),
            "│",
            "tree guide was not painted"
        );
        assert_eq!(
            buf[(3, selected_y)].symbol(),
            "·",
            "file icon was not painted"
        );
        assert!(
            (1..17).any(|x| buf[(x, selected_y)].symbol() == "…"),
            "long filename was not visibly truncated"
        );
        assert_eq!(selected.bg, Theme::current().bg_hover);
        assert!(
            selected.modifier.contains(Modifier::BOLD),
            "selected file text is not bold"
        );

        let folder_y = (1..7)
            .find(|&y| {
                (0..18)
                    .map(|x| buf[(x, y)].symbol())
                    .collect::<String>()
                    .contains("folder")
            })
            .expect("directory row was not painted");
        assert_eq!(
            buf[(3, folder_y)].symbol(),
            "▸",
            "directory icon was not painted"
        );
        assert!(
            buf[(5, folder_y)].modifier.contains(Modifier::BOLD),
            "directory name is not heavier than file names"
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    /// The band belongs to an item, not to a row number. Reapplying it in the
    /// pane indexed the visible slice with an absolute index, so once the
    /// queue scrolled past six rows it landed on a different follow-up.
    #[test]
    fn queue_selection_follows_the_item_when_the_queue_scrolls() {
        let _theme = theme_lock();
        let mut app = App::new();
        for i in 1..=9 {
            app.queue.push(format!("follow-up {i}"));
        }
        app.queue.selected = Some(8);
        app.focus = Focus::Queue;

        let backend = TestBackend::new(40, 10);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw_queue(f, Rect::new(0, 0, 40, 10), &app))
            .unwrap();
        let buf = term.backend().buffer();
        let want = Theme::current().bg_hover;
        let banded: Vec<String> = (1..9)
            .filter(|&y| buf[(2, y)].bg == want)
            .map(|y| (0..40).map(|x| buf[(x, y)].symbol()).collect::<String>())
            .collect();
        assert_eq!(
            banded.len(),
            1,
            "exactly one row carries the band: {banded:?}"
        );
        assert!(
            banded[0].contains("follow-up 9"),
            "band landed on the wrong row: {:?}",
            banded[0]
        );
    }

    #[test]
    fn queue_selected_row_paints_strong_theme_highlight() {
        let _theme = theme_lock();
        let mut app = App::new();
        app.queue.push("selected follow-up".into());
        app.queue.push("ordinary follow-up".into());
        app.queue.selected = Some(0);
        app.focus = Focus::Queue;

        let backend = TestBackend::new(40, 6);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw_queue(f, Rect::new(0, 0, 40, 6), &app))
            .unwrap();
        let buf = term.backend().buffer();
        let (x, y) = (1..5)
            .find_map(|y| {
                let row = (0..40).map(|x| buf[(x, y)].symbol()).collect::<String>();
                row.find("selected follow-up").map(|x| (x as u16, y))
            })
            .expect("selected queue row was not painted");
        let selected = &buf[(x, y)];
        assert_eq!(selected.bg, Theme::current().bg_hover);
        assert!(
            selected.modifier.contains(Modifier::BOLD),
            "selected queue text is not bold"
        );
    }
}
