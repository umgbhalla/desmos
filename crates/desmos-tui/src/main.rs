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

mod json_tree;
mod picker;
mod prompt;
mod queue;
mod side;

use std::collections::{HashMap, HashSet};
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
use unicode_width::UnicodeWidthStr;
use ratatui::prelude::CrosstermBackend;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph};
use ratatui::{Frame, Terminal};
use serde_json::{Value, json};
use xai_grok_pager::appearance::{
    self, AppearanceConfig, RawAppearanceConfig, cache as appearance_cache,
};
use xai_grok_pager::scrollback::blocks::{
    EditToolCallBlock, ExecuteToolCallBlock, OtherToolCallBlock, SubagentBlock, ToolCallBlock,
};
use xai_grok_pager_diff::diff_hunks_from_strings;
#[cfg(test)]
use xai_grok_pager::scrollback::blocks::SubagentBlockKind;
use xai_grok_pager::clipboard::SystemClipboard;
use xai_grok_pager::scrollback::{
    DisplayMode, EntryId, RenderBlock, ScratchBuffer, ScrollbackEntry, ScrollbackPane,
    ScrollbackState,
    text_selection::{
        ActiveTextDrag, PendingTextDrag, PersistentTextSelection, RangeHit,
        ResolvedSelectionModel, SelectionEndpoint, SelectionKind, SelectionOrigin,
        configured_word_separators, drag_threshold_exceeded, reconstruct_selection_text,
        render_active_selection_overlay, render_persistent_selection_overlay,
        semantic_selection_at,
    },
};
use xai_grok_pager::acp::tracker::{TurnActivity, WaitingReason};
use xai_grok_pager::app::agent::AgentState;
use xai_grok_pager::input::is_mod_enter;
use xai_grok_pager::theme::{Theme, ThemeKind, cache as theme_cache};
use xai_grok_pager::util;
use xai_grok_pager::views::block_viewer::{BlockViewerPane, ViewerKind};
use xai_grok_pager::views::modal_window::{
    ModalSizing, ModalWindowConfig, ModalWindowOutcome, ModalWindowState, Shortcut,
    handle_modal_key, handle_modal_mouse, render_modal_window,
};
use xai_grok_pager::views::turn_status::{
    self, MouseButtons, TurnStatusArgs, Watchers,
};

use json_tree::JsonTree;
use prompt::{PromptBuf, clipboard_text, coalesce_events, is_inline_paste_key, is_paste_key, is_text_key};
use queue::QueryQueue;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Focus {
    Story,
    Calls,
    Meter,
    Git,
    Files,
    PostIn,
    PostOut,
    Queue,
    Input,
}

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

impl Focus {
    fn next(self) -> Self {
        match self {
            Self::Story => Self::Calls,
            Self::Calls => Self::Meter,
            Self::Meter => Self::Git,
            Self::Git => Self::Files,
            Self::Files => Self::PostIn,
            Self::PostIn => Self::PostOut,
            Self::PostOut => Self::Queue,
            Self::Queue => Self::Input,
            Self::Input => Self::Story,
        }
    }

    fn prev(self) -> Self {
        match self {
            Self::Story => Self::Input,
            Self::Calls => Self::Story,
            Self::Meter => Self::Calls,
            Self::Git => Self::Meter,
            Self::Files => Self::Git,
            Self::PostIn => Self::Files,
            Self::PostOut => Self::PostIn,
            Self::Queue => Self::PostOut,
            Self::Input => Self::Queue,
        }
    }

    /// Tab cycle. A pane collapsed to zero rows is not a pane.
    fn next_open(self, open: &dyn Fn(Focus) -> bool) -> Self {
        let mut f = self.next();
        for _ in 0..8 {
            if open(f) {
                break;
            }
            f = f.next();
        }
        f
    }

    fn prev_open(self, open: &dyn Fn(Focus) -> bool) -> Self {
        let mut f = self.prev();
        for _ in 0..8 {
            if open(f) {
                break;
            }
            f = f.prev();
        }
        f
    }
}

/// Pane sizes, adjusted live with `+` / `-` on the focused pane and kept in
/// `.desmos/tui.json` so a session does not start by re-doing the layout.
#[derive(Clone, Copy, PartialEq, Eq)]
struct PaneLayout {
    /// Width of the wire column (calls + meter) as a percent of the top row.
    wire_pct: u16,
    /// Rows for the POST in/out split; 0 hides it.
    post_h: u16,
    /// Rows for the cache meter; 0 hides it.
    meter_h: u16,
    /// Width of POST in as a percent of the POST row; the rest is POST out.
    post_split: u16,
    /// Rows for the git pane; 0 keeps it closed, which is how it starts.
    git_h: u16,
    /// Rows for the file view under it; 0 keeps it closed.
    files_h: u16,
}

/// Which way a resize key pushes. `+`/`-` drive each pane's main axis;
/// ctrl+arrows drive whichever axis the arrow points along.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Axis {
    Horizontal,
    Vertical,
}

impl Default for PaneLayout {
    fn default() -> Self {
        Self {
            wire_pct: 38,
            post_h: 12,
            // Three inner rows is everything the meter has to say now that the
            // sparkline is gone; +2 for the border. Anything taller is dead
            // space under the context bar.
            meter_h: 5,
            post_split: 50,
            // Both side panes start open. A pane you have to know about before
            // you can see it is a pane nobody sees; git state and the file it
            // points at are the two things a harness turn is about. Kept small
            // so the calls pane still owns most of the wire column — the
            // `spare` clamp in draw() squeezes these to nothing before it
            // takes a row off calls.
            git_h: 6,
            files_h: 8,
        }
    }
}

impl PaneLayout {
    const MIN_WIRE: u16 = 15;
    const MAX_WIRE: u16 = 75;
    const MAX_POST: u16 = 28;
    const MAX_METER: u16 = 12;
    const MIN_SPLIT: u16 = 20;
    const MAX_SPLIT: u16 = 80;
    const MAX_SIDE: u16 = 30;
    /// What a closed side pane opens to — enough for a tab strip and a few rows.
    const OPEN_SIDE: u16 = 10;

    /// The axis `+` / `-` drives for a pane: the one it can actually give away
    /// space along. Story and calls share a width; meter and POST own rows.
    fn main_axis(focus: Focus) -> Axis {
        match focus {
            Focus::Story | Focus::Calls => Axis::Horizontal,
            _ => Axis::Vertical,
        }
    }

    fn grow(&mut self, focus: Focus, by: i16) {
        self.grow_axis(focus, Self::main_axis(focus), by);
    }

    fn grow_axis(&mut self, focus: Focus, axis: Axis, by: i16) {
        let step = |v: u16, lo: u16, hi: u16| -> u16 {
            (v as i16 + by).clamp(lo as i16, hi as i16) as u16
        };
        match (focus, axis) {
            // Story widens by taking the wire column's width, and vice versa.
            (Focus::Story, Axis::Horizontal) => {
                self.wire_pct = (self.wire_pct as i16 - by)
                    .clamp(Self::MIN_WIRE as i16, Self::MAX_WIRE as i16)
                    as u16
            }
            (Focus::Calls | Focus::Meter, Axis::Horizontal) => {
                self.wire_pct = step(self.wire_pct, Self::MIN_WIRE, Self::MAX_WIRE)
            }
            // The top row is whatever the POST split leaves, so story and calls
            // grow taller by pushing POST down.
            (Focus::Story | Focus::Calls, Axis::Vertical) => {
                self.post_h = (self.post_h as i16 - by).clamp(0, Self::MAX_POST as i16) as u16
            }
            (Focus::Meter, Axis::Vertical) => self.meter_h = step(self.meter_h, 0, Self::MAX_METER),
            (Focus::Git, Axis::Vertical) => self.git_h = step(self.git_h, 0, Self::MAX_SIDE),
            (Focus::Files, Axis::Vertical) => self.files_h = step(self.files_h, 0, Self::MAX_SIDE),
            (Focus::Git | Focus::Files, Axis::Horizontal) => {
                self.wire_pct = step(self.wire_pct, Self::MIN_WIRE, Self::MAX_WIRE)
            }
            (Focus::PostIn | Focus::PostOut, Axis::Vertical) => {
                self.post_h = step(self.post_h, 0, Self::MAX_POST)
            }
            // POST in and out share a row: one grows out of the other.
            (Focus::PostIn, Axis::Horizontal) => {
                self.post_split = step(self.post_split, Self::MIN_SPLIT, Self::MAX_SPLIT)
            }
            (Focus::PostOut, Axis::Horizontal) => {
                self.post_split = (self.post_split as i16 - by)
                    .clamp(Self::MIN_SPLIT as i16, Self::MAX_SPLIT as i16)
                    as u16
            }
            (Focus::Queue | Focus::Input, _) => {}
        }
    }

    fn path() -> Option<PathBuf> {
        let cwd = std::env::current_dir().ok()?;
        Some(cwd.join(".desmos").join("tui.json"))
    }

    fn load() -> Self {
        let Some(p) = Self::path() else {
            return Self::default();
        };
        let Ok(raw) = std::fs::read_to_string(p) else {
            return Self::default();
        };
        let Ok(v) = serde_json::from_str::<Value>(&raw) else {
            return Self::default();
        };
        let d = Self::default();
        let n = |k: &str, fallback: u16| {
            v.get(k).and_then(Value::as_u64).unwrap_or(fallback as u64) as u16
        };
        Self {
            wire_pct: n("wire_pct", d.wire_pct).clamp(Self::MIN_WIRE, Self::MAX_WIRE),
            post_h: n("post_h", d.post_h).min(Self::MAX_POST),
            meter_h: n("meter_h", d.meter_h).min(Self::MAX_METER),
            post_split: n("post_split", d.post_split).clamp(Self::MIN_SPLIT, Self::MAX_SPLIT),
            git_h: n("git_h", d.git_h).min(Self::MAX_SIDE),
            files_h: n("files_h", d.files_h).min(Self::MAX_SIDE),
        }
    }

    fn save(&self) {
        let Some(p) = Self::path() else { return };
        if let Some(dir) = p.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let _ = std::fs::write(
            p,
            json!({
                "wire_pct": self.wire_pct,
                "post_h": self.post_h,
                "meter_h": self.meter_h,
                "post_split": self.post_split,
                "git_h": self.git_h,
                "files_h": self.files_h,
            })
            .to_string(),
        );
    }
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

/// In-flight thinking / speech. Deltas buffer here; grok markdown
/// (`push_chunk`) runs once per frame, not once per SSE token.
#[derive(Default)]
struct StreamCursor {
    think: Option<EntryId>,
    speech: Option<EntryId>,
    speech_raw: String,
    speech_shown: String,
    pending_think: String,
    /// The invisible stretch since the last prose.
    run: WorkRun,
}

impl StreamCursor {
    fn live(&self) -> bool {
        self.think.is_some() || self.speech.is_some()
    }

    fn flush(&mut self, story: &mut ScrollbackState) {
        if let Some(id) = self.think {
            if !self.pending_think.is_empty() {
                story.push_chunk_to_thinking(id, &self.pending_think);
                self.pending_think.clear();
            }
        }
        self.flush_speech(story);
    }

    fn flush_speech(&mut self, story: &mut ScrollbackState) {
        let shown = spoken_prefix(&self.speech_raw);
        if shown.trim().is_empty() && self.speech.is_none() {
            self.speech_shown = shown;
            return;
        }
        if self.speech.is_none() && !shown.is_empty() {
            self.run.fold(story);
            self.speech = Some(story.start_streaming_agent());
        }
        if let Some(id) = self.speech {
            if shown.starts_with(&self.speech_shown) {
                let extra = &shown[self.speech_shown.len()..];
                if !extra.is_empty() {
                    story.push_chunk_to_agent(id, extra);
                }
            } else {
                story.finish_running(id);
                let nid = story.start_streaming_agent();
                self.speech = Some(nid);
                if !shown.is_empty() {
                    story.push_chunk_to_agent(nid, &shown);
                }
            }
        }
        self.speech_shown = shown;
    }

    fn finish_think(&mut self, story: &mut ScrollbackState) {
        if let Some(id) = self.think {
            if !self.pending_think.is_empty() {
                story.push_chunk_to_thinking(id, &self.pending_think);
                self.pending_think.clear();
            }
        }
        if let Some(id) = self.think.take() {
            let empty = story.get_by_id(id).is_some_and(|e| match &e.block {
                RenderBlock::Thinking(t) => t.text().trim().is_empty(),
                _ => false,
            });
            if empty {
                story.remove_entry(id);
            } else {
                story.finish_running(id);
                // A live thought streams Expanded, and grok keeps an Expanded
                // thinking block expanded on finish (Ctrl+E stickiness). The
                // record we want is one row, so say so explicitly.
                set_wire_mode(story, id, DisplayMode::Collapsed);
                let ms = story.get_by_id(id).and_then(|e| match &e.block {
                    RenderBlock::Thinking(t) => t.elapsed_time_ms(),
                    _ => None,
                });
                self.run.thought(id, ms);
                self.run.sync(story);
            }
        }
    }

    fn finish_speech(&mut self, story: &mut ScrollbackState) {
        self.flush_speech(story);
        if let Some(id) = self.speech.take() {
            story.finish_running(id);
        }
        self.speech_raw.clear();
        self.speech_shown.clear();
    }

    fn finish(&mut self, story: &mut ScrollbackState) {
        self.finish_think(story);
        self.finish_speech(story);
    }
}




/// What a call was aimed at, when that is structural rather than payload.
///
/// A path from an attr is a target. For a shell command the *program* is the
/// semantic part -- `cargo`, `git`, `grep` -- while its flags and arguments are
/// the payload the calls pane already holds, so only the bare program name
/// comes across, and only after stepping past a leading `cd`.
fn call_target(tag: &str, ev: &Value) -> Option<String> {
    if let Some(p) = ev
        .get("attrs")
        .and_then(|a| a.get("path"))
        .and_then(Value::as_str)
    {
        return Some(
            p.rsplit('/')
                .next()
                .filter(|s| !s.is_empty())
                .unwrap_or(p)
                .to_string(),
        );
    }
    if tag != "bash" {
        return None;
    }
    let body = ev.get("body").and_then(Value::as_str)?;
    for step in body.split("&&").flat_map(|s| s.split(';')) {
        let Some(word) = step.split_whitespace().next() else {
            continue;
        };
        if matches!(word, "cd" | "export" | "set" | "source" | "") || word.contains('=') {
            continue;
        }
        let prog = word.rsplit('/').next().unwrap_or(word);
        return Some(prog.to_string());
    }
    None
}

/// `git rev-parse --short HEAD`, or None outside a repo.
///
/// Called once per run, at the first syscall, never from the render path.
fn git_head() -> Option<String> {
    let out = std::process::Command::new("git")
        .args(["rev-parse", "--short", "HEAD"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let s = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (!s.is_empty()).then_some(s)
}

/// What the repo looks like at the seam, where prose starts.
///
/// A run of invisible work is worth reading precisely because it changed
/// something on disk, and the reader should not have to go look. A moved HEAD
/// is the headline; otherwise the dirty count is.
fn git_tail(head_at_start: Option<&str>) -> Option<String> {
    let head = git_head();
    if let (Some(before), Some(now)) = (head_at_start, head.as_deref()) {
        if before != now {
            return Some(format!("\u{00b7} committed {now}"));
        }
    }
    let out = std::process::Command::new("git")
        .args(["status", "--porcelain"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let dirty = String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter(|l| !l.trim().is_empty())
        .count();
    match dirty {
        0 => Some("\u{00b7} tree clean".into()),
        1 => Some("\u{00b7} 1 file dirty".into()),
        n => Some(format!("\u{00b7} {n} files dirty")),
    }
}

/// One thing that happened where the story could not see it.
#[derive(Debug, Clone, PartialEq)]
enum Seg {
    /// A finished thought, in milliseconds.
    Thought(u64),
    /// A syscall: the tag, and a target only where one is structural.
    Call { tag: String, target: Option<String> },
}

/// A stretch of invisible work — the thoughts and syscalls between two pieces
/// of prose — folded into one line the reader can actually follow.
///
/// The story is a whitelist of narrative kinds, so a run of tool work shows up
/// as nothing but a stack of collapsed thoughts: three "Thought for 9s" rows
/// and no hint that six files were read and two were rewritten. This is the
/// connective tissue. It never carries a body, because it is built from tags
/// and attrs and never sees one.
#[derive(Default)]
struct WorkRun {
    segs: Vec<Seg>,
    /// Thought blocks to fold away once the row replaces them.
    thoughts: Vec<EntryId>,
    /// HEAD when the run's first call landed, to spot a commit at the seam.
    head_at_start: Option<String>,
    /// The row itself, once the run has earned one. Rewritten in place as the
    /// run grows, so it never moves and never stacks.
    row: Option<EntryId>,
}

/// Below this a run is not worth a row: the collapsed thought already says
/// everything, and a line for one grep is worse than silence.
const RUN_MIN_CALLS: usize = 2;

impl WorkRun {
    fn call(&mut self, tag: &str, target: Option<String>) {
        if self.head_at_start.is_none() {
            self.head_at_start = git_head();
        }
        self.segs.push(Seg::Call {
            tag: tag.to_string(),
            target,
        });
    }

    fn thought(&mut self, id: EntryId, elapsed_ms: Option<i64>) {
        self.thoughts.push(id);
        self.segs
            .push(Seg::Thought(elapsed_ms.unwrap_or(0).max(0) as u64));
    }

    fn calls(&self) -> usize {
        self.segs
            .iter()
            .filter(|s| matches!(s, Seg::Call { .. }))
            .count()
    }

    /// Rewrite the row for the run so far. Called after every segment, so the
    /// reader watches the work accumulate instead of staring at a stack of
    /// collapsed thoughts until the answer arrives.
    ///
    /// The row is written in place: one entry per run, updated, never a second
    /// row and never a jump to the bottom. It appears only once the run has
    /// earned it, which is also when the thoughts it summarises are removed.
    fn sync(&mut self, story: &mut ScrollbackState) {
        if self.calls() < RUN_MIN_CALLS {
            return;
        }
        let mut line = work_sentence(&self.segs);
        if let Some(tail) = git_tail(self.head_at_start.as_deref()) {
            line.push_str("  ");
            line.push_str(&tail);
        }
        for id in self.thoughts.drain(..) {
            story.remove_entry(id);
        }
        let live = self.row.filter(|id| story.get_by_id(*id).is_some());
        match live {
            Some(id) => {
                if let Some(entry) = story.get_by_id_mut(id) {
                    entry.block = RenderBlock::system(line);
                }
                story.mark_structurally_dirty(id);
                story.mark_height_dirty(id);
            }
            None => self.row = Some(story.push_block(RenderBlock::system(line))),
        }
    }

    /// Close the run at the seam, just before prose starts. The last sync
    /// catches the final call and whatever git says about it.
    fn fold(&mut self, story: &mut ScrollbackState) {
        self.sync(story);
        self.reset();
    }

    fn reset(&mut self) {
        self.segs.clear();
        self.thoughts.clear();
        self.head_at_start = None;
        self.row = None;
    }
}

/// Render a run as one line: thoughts as durations, calls compressed.
///
/// Consecutive calls with the same tag become `tag xN`, a lone call keeps its
/// target, and a run longer than [`SENTENCE_MAX`] groups elides its middle. One
/// line, always, so the row cannot push the transcript around as it grows.
fn work_sentence(segs: &[Seg]) -> String {
    // Group into alternating thought / work phases.
    let mut phases: Vec<String> = Vec::new();
    let mut work: Vec<(String, Option<String>, usize)> = Vec::new();
    let flush = |work: &mut Vec<(String, Option<String>, usize)>, out: &mut Vec<String>| {
        if work.is_empty() {
            return;
        }
        let parts: Vec<String> = work
            .drain(..)
            .map(|(tag, target, n)| match (n, target) {
                (1, Some(t)) => format!("{tag} {t}"),
                (1, None) => tag,
                (n, _) => format!("{tag} \u{00d7}{n}"),
            })
            .collect();
        out.push(parts.join(", "));
    };
    for seg in segs {
        match seg {
            Seg::Thought(ms) => {
                flush(&mut work, &mut phases);
                phases.push(format!("thought {}", human_secs(*ms)));
            }
            Seg::Call { tag, target } => match work.last_mut() {
                Some((t, _, n)) if t == tag => *n += 1,
                _ => work.push((tag.clone(), target.clone(), 1)),
            },
        }
    }
    flush(&mut work, &mut phases);

    const SENTENCE_MAX: usize = 5;
    if phases.len() > SENTENCE_MAX {
        let head = phases[..2].join(" \u{2192} ");
        let tail = phases[phases.len() - 2..].join(" \u{2192} ");
        return format!("{head} \u{2192} \u{2026} \u{2192} {tail}");
    }
    phases.join(" \u{2192} ")
}

/// Durations read as durations: 900ms is "0.9s", 95s is "1m35s".
fn human_secs(ms: u64) -> String {
    let secs = ms as f64 / 1000.0;
    if secs < 10.0 {
        format!("{secs:.1}s")
    } else if secs < 60.0 {
        format!("{}s", secs.round() as u64)
    } else {
        let s = secs.round() as u64;
        format!("{}m{:02}s", s / 60, s % 60)
    }
}

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

/// Child spawn session — grok's per-child AgentView, split into desmos panes.
struct ChildSess {
    story: ScrollbackState,
    calls: ScrollbackState,
    story_scratch: ScratchBuffer,
    calls_scratch: ScratchBuffer,
    story_sel: ResolvedSelectionModel,
    calls_sel: ResolvedSelectionModel,
    parent_entry: Option<EntryId>,
    stream: StreamCursor,
    exec: ExecStream,
    story_text: TextSel,
    calls_text: TextSel,
}

/// Grok text selection for one scrollback (drag, persist, double-click word).
#[derive(Default)]
struct TextSel {
    pending: Option<PendingTextDrag>,
    active: Option<ActiveTextDrag>,
    persist: Option<PersistentTextSelection>,
    last_hit: Option<(Instant, RangeHit)>,
    clicks: u8,
}

impl TextSel {
    fn clear(&mut self) {
        self.pending = None;
        self.active = None;
        self.persist = None;
    }

    fn note_click(&mut self, now: Instant, hit: RangeHit) -> u8 {
        if let Some((t, prev)) = self.last_hit {
            if prev.entry_idx == hit.entry_idx
                && prev.range_id == hit.range_id
                && prev.block_line_idx == hit.block_line_idx
                && now.duration_since(t).as_millis() < 400
            {
                self.clicks = (self.clicks + 1).min(3);
                self.last_hit = Some((now, hit));
                return self.clicks;
            }
        }
        self.clicks = 1;
        self.last_hit = Some((now, hit));
        1
    }
}

struct App {
    prompt: PromptBuf,
    model: String,
    thinking: String,
    generation: String,
    running: bool,
    turn_started: Option<Instant>,
    /// First chrome paint waits for the bridge `ready` snapshot so the
    /// status line never flashes `effort:— gen —`.
    ready: bool,
    /// Click on grok `[stop]` — applied in the event loop with the bridge.
    want_stop: bool,
    last_activity: Option<TurnActivity>,
    activity_started_at: Option<Instant>,
    turn_status_area: Rect,
    turn_cancel: Option<Rect>,
    status: String,
    story: ScrollbackState,
    calls: ScrollbackState,
    post_in: JsonTree,
    post_out: JsonTree,
    post_req: Value,
    post_resp: Value,
    post_inspect: Option<PostInspect>,
    /// Onboarding / settings overlay. Modal when open.
    picker: picker::Picker,
    story_scratch: ScratchBuffer,
    calls_scratch: ScratchBuffer,
    story_sel: ResolvedSelectionModel,
    calls_sel: ResolvedSelectionModel,
    post_n: u64,
    /// Sequence number of the response currently in `post_out`. Lags `post_n`
    /// while a step is in flight: the out pane is still holding the previous
    /// turn's reply, and saying so beats blanking it under a new number.
    post_out_n: u64,
    queue: QueryQueue,
    send_now: bool,
    drain_after: bool,
    children: HashMap<String, ChildSess>,
    viewing: Option<String>,
    stream: StreamCursor,
    exec: ExecStream,
    viewer: Option<BlockViewerPane>,
    viewer_src: ViewerSrc,
    focus: Focus,
    traj_area: Rect,
    call_area: Rect,
    /// Wire cards the reader folded or opened by hand. reflow_wire leaves
    /// these alone; without it the auto-open would fight every keystroke.
    wire_manual: HashSet<EntryId>,
    post_in_area: Rect,
    post_out_area: Rect,
    queue_area: Rect,
    input_area: Rect,
    input_inner: Rect,
    mouse: Option<(u16, u16)>,
    last_click: Option<(Instant, usize, u8)>, // time, entry, pane: 0 story 1 calls 2 in 3 out
    last_chip_click: Option<(Instant, u64)>,
    story_text: TextSel,
    calls_text: TextSel,
    cache: CacheMeter,
    git: side::GitPane,
    files: side::FilePane,
    git_area: Rect,
    files_area: Rect,
    keys_area: Rect,
    layout: PaneLayout,
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

fn model_price(model: &str) -> (f64, f64) {
    match model {
        m if m.starts_with("claude-fable") || m.starts_with("claude-mythos") => (10.0, 50.0),
        m if m.starts_with("claude-opus") => (5.0, 25.0),
        m if m.starts_with("claude-sonnet") => (3.0, 15.0),
        m if m.starts_with("claude-haiku") => (1.0, 5.0),
        m if m.starts_with("gpt-") => (1.25, 10.0),
        _ => (5.0, 25.0),
    }
}

impl CacheMeter {
    /// Split the last request by role. Counts serialized characters, not
    /// tokens: the wire never reports per-role tokens, and the bar only needs
    /// proportions. System blocks and anything unlabelled land in slot 0.
    /// Cache share of this one call's prompt.
    fn hit(&self) -> u64 {
        let total = self.read + self.write + self.fresh;
        if total == 0 { 0 } else { self.read * 100 / total }
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
                        "function_call" | "function_call_output"
                        | "custom_tool_call" | "custom_tool_call_output" => 2,
                        "message" => match item.get("role").and_then(Value::as_str) {
                            Some("user") => {
                                if item.to_string().contains("<result") { 2 } else { 1 }
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

    fn observe(&mut self, usage: &Value, model: &str) {
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
        let write_5m = self.write.saturating_sub(hour);

        let (in_rate, out_rate) = model_price(model);
        let m = 1_000_000.0;
        let cost = (self.fresh as f64 * in_rate
            + self.read as f64 * in_rate * 0.1
            + write_5m as f64 * in_rate * 1.25
            + hour as f64 * in_rate * 2.0
            + self.out as f64 * out_rate)
            / m;
        // Uncached, those read tokens would have been fresh input.
        let saved = self.read as f64 * in_rate * 0.9 / m;

        self.calls += 1;
        if self.read > 0 {
            self.warm += 1;
        }
        self.read_total += self.read;
        self.write_total += self.write;
        self.fresh_total += self.fresh;
        self.out_total += self.out;
        self.spent += cost;
        self.saved += saved;

        // Prompt size for this call -- everything the model had to read before
        // it could answer. Recorded before the cold-call early return below, so
        // the trend does not silently skip uncached turns.
        let ctx = self.read + self.write + self.fresh;

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

impl App {
    fn new() -> Self {
        theme_cache::set(theme_cache::resolve_initial_theme());
        let mut app = Self {
            prompt: PromptBuf::new(),
            model: String::new(),
            thinking: String::new(),
            generation: String::new(),
            running: false,
            turn_started: None,
            ready: false,
            want_stop: false,
            last_activity: None,
            activity_started_at: None,
            turn_status_area: Rect::default(),
            turn_cancel: None,
            status: "idle".into(),
            story: ScrollbackState::new(),
            calls: ScrollbackState::new(),
            post_in: JsonTree::default(),
            post_out: JsonTree::default(),
            post_req: json!({}),
            post_resp: json!({}),
            post_inspect: None,
            picker: picker::Picker::default(),
            story_scratch: ScratchBuffer::new(),
            calls_scratch: ScratchBuffer::new(),
            story_sel: ResolvedSelectionModel::default(),
            calls_sel: ResolvedSelectionModel::default(),
            post_n: 0,
            post_out_n: 0,
            queue: QueryQueue::default(),
            send_now: false,
            drain_after: false,
            children: HashMap::new(),
            viewing: None,
            stream: StreamCursor::default(),
            exec: ExecStream::default(),
            viewer: None,
            viewer_src: ViewerSrc::Story,
            focus: Focus::Input,
            traj_area: Rect::default(),
            call_area: Rect::default(),
            wire_manual: HashSet::new(),
            post_in_area: Rect::default(),
            post_out_area: Rect::default(),
            queue_area: Rect::default(),
            input_area: Rect::default(),
            input_inner: Rect::default(),
            mouse: None,
            last_click: None,
            last_chip_click: None,
            story_text: TextSel::default(),
            calls_text: TextSel::default(),
            cache: CacheMeter::default(),
            git: side::GitPane::new(&std::env::current_dir().unwrap_or_default()),
            files: side::FilePane::new(&std::env::current_dir().unwrap_or_default()),
            git_area: Rect::default(),
            files_area: Rect::default(),
            keys_area: Rect::default(),
            layout: PaneLayout::load(),
        };
        app.apply_grok_settings();
        app
    }

    /// Load ~/.grok/pager.toml + grok UI cache (timestamps, compact, …)
    /// and push it onto both scrollbacks. Same stack the pager uses.
    fn apply_grok_settings(&mut self) {
        let cfg = grok_appearance();
        appearance::set_tab_width(cfg.scrollback.display.tab_width);
        // Verb grouping folds consecutive tools into "Ran 3 tools". Fine in
        // grok chat; fatal on the wire pane — every POST and <python> stays
        // its own row.
        appearance_cache::set_group_tool_verbs(false);
        self.story.set_appearance(cfg.clone());
        self.calls.set_appearance(cfg.clone());
        for child in self.children.values_mut() {
            child.story.set_appearance(cfg.clone());
            child.calls.set_appearance(cfg.clone());
        }
    }

    fn ensure_child(&mut self, id: &str, task: &str) -> &mut ChildSess {
        if !self.children.contains_key(id) {
            let look = grok_appearance();
            let mut story = ScrollbackState::new();
            let mut calls = ScrollbackState::new();
            story.set_appearance(look.clone());
            calls.set_appearance(look);
            if !task.is_empty() {
                story.push_block(RenderBlock::user_prompt(task));
            }
            self.children.insert(
                id.to_string(),
                ChildSess {
                    story,
                    calls,
                    story_scratch: ScratchBuffer::new(),
                    calls_scratch: ScratchBuffer::new(),
                    story_sel: ResolvedSelectionModel::default(),
                    calls_sel: ResolvedSelectionModel::default(),
                    parent_entry: None,
                    stream: StreamCursor::default(),
                    exec: ExecStream::default(),
                    story_text: TextSel::default(),
                    calls_text: TextSel::default(),
                },
            );
        }
        self.children.get_mut(id).expect("just inserted")
    }

    fn text_sel(&mut self, calls: bool) -> &mut TextSel {
        if let Some(id) = self.viewing.clone() {
            if let Some(c) = self.children.get_mut(&id) {
                return if calls {
                    &mut c.calls_text
                } else {
                    &mut c.story_text
                };
            }
        }
        if calls {
            &mut self.calls_text
        } else {
            &mut self.story_text
        }
    }

    fn sel_model(&self, calls: bool) -> &ResolvedSelectionModel {
        if let Some(id) = self.viewing.as_deref() {
            if let Some(c) = self.children.get(id) {
                return if calls { &c.calls_sel } else { &c.story_sel };
            }
        }
        if calls {
            &self.calls_sel
        } else {
            &self.story_sel
        }
    }

    fn viewer_scroll(&mut self) -> &mut ScrollbackState {
        match self.viewer_src {
            ViewerSrc::Calls => self.calls_scroll(),
            ViewerSrc::Story => self.story_scroll(),
        }
    }

    fn open_block_viewer(&mut self) -> bool {
        self.post_inspect = None;
        if self.viewer.is_some() {
            self.viewer = None;
            return true;
        }
        if self.open_selected_session() {
            return true;
        }
        let src = if self.focus == Focus::Calls {
            ViewerSrc::Calls
        } else {
            ViewerSrc::Story
        };
        let pane = match src {
            ViewerSrc::Calls => self.calls_scroll(),
            ViewerSrc::Story => self.story_scroll(),
        };
        let Some(idx) = pane.selected() else {
            return false;
        };
        let Some(entry) = pane.entry(idx) else {
            return false;
        };
        let Some(viewer) = viewer_for_entry(entry) else {
            return false;
        };
        self.viewer_src = src;
        self.viewer = Some(viewer);
        true
    }

    fn open_selected_session(&mut self) -> bool {
        // grok: Enter / Ctrl-F / double-click on SubagentBlock replaces
        // the parent scrollback with the child AgentView. Already inside
        // a child: do not nest (desmos spawn depth is 1).
        if self.viewing.is_some()
            || matches!(
                self.focus,
                Focus::Calls | Focus::PostIn | Focus::PostOut | Focus::Queue
            )
        {
            return false;
        }
        let Some(idx) = self.story.selected() else {
            return false;
        };
        let id = match self.story.entry(idx).map(|e| &e.block) {
            Some(RenderBlock::Subagent(sb)) => sb.child_session_id.clone(),
            _ => return false,
        };
        let task = match self.story.entry(idx).map(|e| &e.block) {
            Some(RenderBlock::Subagent(sb)) => sb.description.clone(),
            _ => String::new(),
        };
        self.ensure_child(&id, &task);
        self.viewing = Some(id);
        self.focus = Focus::Story;
        true
    }

    fn story_scroll(&mut self) -> &mut ScrollbackState {
        if let Some(id) = self.viewing.clone() {
            if let Some(c) = self.children.get_mut(&id) {
                return &mut c.story;
            }
        }
        &mut self.story
    }

    fn calls_scroll(&mut self) -> &mut ScrollbackState {
        if let Some(id) = self.viewing.clone() {
            if let Some(c) = self.children.get_mut(&id) {
                return &mut c.calls;
            }
        }
        &mut self.calls
    }

    fn story_push(&mut self, block: RenderBlock) {
        // follow_mode is already true on a fresh state; prepare_layout pins
        // the viewport. goto_bottom() before the first layout has
        // viewport_height=0, so max_offset == total_height and the whole
        // transcript scrolls off-screen.
        self.story.push_block(block);
    }

    fn call_push(&mut self, block: RenderBlock) {
        wire_push(&mut self.calls, block);
    }

    fn focused_scroll(&mut self) -> &mut ScrollbackState {
        match self.focus {
            Focus::Calls => self.calls_scroll(),
            _ => self.story_scroll(),
        }
    }

    fn focused_tree(&mut self) -> Option<&mut JsonTree> {
        match self.focus {
            Focus::PostIn => Some(&mut self.post_in),
            Focus::PostOut => Some(&mut self.post_out),
            _ => None,
        }
    }

    fn set_last_post(&mut self, n: u64, request: &Value, response: &Value) {
        self.post_n = n;
        self.post_req = request.clone();
        self.post_in = JsonTree::from_value(request);
        // The request event carries an empty response. Keep the previous reply
        // and its number rather than clearing the pane, so a completed turn
        // stays readable until the next one actually answers.
        if !response.is_null() && response != &json!({}) {
            self.post_resp = response.clone();
            self.post_out = JsonTree::from_value(response);
            self.post_out_n = n;
        }
        if let Some(inspect) = self.post_inspect.as_mut() {
            inspect.sync_raw(n, request, response);
        }
    }

    fn open_post_inspect(&mut self) {
        self.viewer = None;
        if self.post_inspect.is_some() {
            self.post_inspect = None;
            return;
        }
        let tab = if self.focus == Focus::PostOut { 1 } else { 0 };
        self.post_inspect = Some(PostInspect::open(tab));
    }

    fn set_focus(&mut self, focus: Focus) {
        let focus = if focus == Focus::Queue && self.queue.is_empty() {
            return;
        } else {
            focus
        };
        if self.focus == focus {
            return;
        }
        self.focus = focus;
        match focus {
            Focus::Story => self.story_scroll().on_activate(),
            Focus::Calls => self.calls_scroll().on_activate(),
            Focus::Meter
            | Focus::Git
            | Focus::Files
            | Focus::PostIn
            | Focus::PostOut
            | Focus::Queue
            | Focus::Input => {}
        }
    }
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
    cfg.show_timestamps = appearance_cache::load_timestamps();
    cfg.show_timeline = appearance_cache::load_show_timeline();
    cfg.prompt.compact = appearance_cache::load();
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
    app.story_push(RenderBlock::thinking(
        "cwd is mine. peek ns, then fire the smallest probe.",
    ));
    app.story_push(RenderBlock::thinking(
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
    app.call_push(wire_complete(
        "user",
        1,
        "claude-opus-5",
        "low",
        &json!({"input_tokens": 1200, "output_tokens": 380}),
        1,
        1,
    ));
    app.call_push(wire_syscall(
        "python",
        "sorted(k for k in world.ns if not k.startswith('_'))",
        &json!({}),
        "['CWD']",
    ));
    app.call_push(wire_syscall(
        "bash",
        "ls .desmos/generations",
        &json!({}),
        "0001.json 0002.json 0003.json 0004.json",
    ));
    app.call_push(wire_complete(
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
    app.call_push(wire_complete(
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
    app.queue.push("then show cache hit\nacross two lines".into());
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
    let eid = app.story.push_block(RenderBlock::Subagent(block));
    app.story.set_last_running(true);
    {
        let child = app.ensure_child(id, task);
        child.parent_entry = Some(eid);
        child.story.push_block(RenderBlock::thinking(
            "read notes first, then say what cache actually is.",
        ));
        child.story.push_block(RenderBlock::agent_message(
            "cache is last-user only. ABI is frozen. Speech is not memory.",
        ));
        wire_push(
            &mut child.calls,
            wire_complete(
                "user",
                1,
                "claude-opus-5",
                "low",
                &json!({"input_tokens": 400, "output_tokens": 80}),
                1,
                0,
            ),
        );
        wire_push(
            &mut child.calls,
            wire_syscall("python", "list(world.notes)", &json!({}), "['cache']"),
        );
    }
    app.story.finish_running(eid);
    app.story.push_block(RenderBlock::Subagent(SubagentBlock::completed(
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
    if demo {
        seed_demo(&mut app);
    }

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
    wait_ready(bridge.as_deref_mut(), app);
    let mut dirty = true;
    let mut last_anim = Instant::now();
    let mut last_cache = Instant::now();
    loop {
        let mut more = false;
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
                        app.status = "bridge died".into();
                        app.running = false;
                        app.turn_started = None;
                        app.ready = true;
                        dirty = true;
                    }
                }
            }
        }

        if app.drain_after && !app.running {
            app.drain_after = false;
            try_drain(bridge.as_deref_mut(), app)?;
            dirty = true;
        }

        let live = streaming(app) || app.running || app.story.has_running_entries();
        if live && last_anim.elapsed() >= ANIM {
            if tick_scrollbacks(app) || app.running {
                dirty = true;
            }
            last_anim = Instant::now();
        }
        // Cache TTL burns down while nothing else is live; one repaint a
        // second is enough for the bar and keeps idle CPU at zero otherwise.
        if app.layout.git_h > 0 {
            app.git.poll(false);
        }
        if app.git.drain() {
            dirty = true;
        }
        let cache_live = app.cache.left().is_some();
        if cache_live && last_cache.elapsed() >= Duration::from_secs(1) {
            dirty = true;
            last_cache = Instant::now();
        }

        if dirty {
            terminal.draw(|f| draw(f, app))?;
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
fn wait_ready(bridge: Option<&mut Bridge>, app: &mut App) {
    if app.ready {
        return;
    }
    let Some(b) = bridge else {
        app.ready = true;
        return;
    };
    let deadline = Instant::now() + Duration::from_secs(2);
    while !app.ready {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            app.status = "bridge silent".into();
            app.ready = true;
            break;
        }
        match b.rx.recv_timeout(left) {
            Ok(ev) => handle_event(app, ev),
            Err(RecvTimeoutError::Timeout) => {
                app.status = "bridge silent".into();
                app.ready = true;
            }
            Err(RecvTimeoutError::Disconnected) => {
                app.status = "bridge died".into();
                app.ready = true;
            }
        }
    }
    while let Ok(ev) = b.rx.try_recv() {
        handle_event(app, ev);
    }
}

fn handle_subagent(app: &mut App, ev: &Value) {
    let phase = ev.get("phase").and_then(Value::as_str).unwrap_or("");
    let id = ev.get("id").and_then(Value::as_str).unwrap_or("");
    if id.is_empty() {
        return;
    }
    match phase {
        "started" => {
            let task = ev.get("task").and_then(Value::as_str).unwrap_or("");
            let agent = ev.get("agent").and_then(Value::as_str).unwrap_or("general");
            let persona = ev
                .get("persona")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_string);
            let model = ev
                .get("model")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_string);
            let block = SubagentBlock::started(task, id, agent, persona, None, model, true);
            let eid = app.story.push_block(RenderBlock::Subagent(block));
            app.story.set_last_running(true);
            app.ensure_child(id, task).parent_entry = Some(eid);
        }
        "progress" => {
            let turns = ev.get("turns").and_then(Value::as_u64).unwrap_or(0);
            let eid = app.children.get(id).and_then(|c| c.parent_entry);
            if let Some(eid) = eid {
                if let Some(entry) = app.story.get_by_id_mut(eid) {
                    if let RenderBlock::Subagent(ref mut sb) = entry.block {
                        sb.activity_label = Some(format!("turn {turns}"));
                        entry.invalidate_cache();
                    }
                }
            }
        }
        "done" | "failed" => {
            let secs = ev.get("secs").and_then(Value::as_f64).unwrap_or(0.0);
            let elapsed = Duration::from_secs_f64(secs.max(0.0));
            let err = ev
                .get("error")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_string);
            let eid = app.children.get(id).and_then(|c| c.parent_entry);
            if let Some(eid) = eid {
                app.story.finish_running(eid);
            }
            let desc = eid
                .and_then(|eid| app.story.get_by_id(eid))
                .and_then(|e| match &e.block {
                    RenderBlock::Subagent(sb) => Some(sb.description.clone()),
                    _ => None,
                })
                .unwrap_or_default();
            let terminal = if phase == "done" && err.is_none() {
                RenderBlock::Subagent(SubagentBlock::completed(&desc, id, elapsed))
            } else {
                RenderBlock::Subagent(SubagentBlock::failed(
                    &desc,
                    id,
                    elapsed,
                    err.clone(),
                ))
            };
            app.story.push_block(terminal);
            // Child speech already landed via `child` events. Only surface a
            // failure that never produced speech.
            if let Some(err) = err {
                app.ensure_child(id, "")
                    .story
                    .push_block(RenderBlock::system(err));
            }
        }
        _ => {}
    }
}

fn handle_child(app: &mut App, ev: &Value) {
    let id = ev.get("id").and_then(Value::as_str).unwrap_or("");
    if id.is_empty() {
        return;
    }
    let kind = ev.get("kind").and_then(Value::as_str).unwrap_or("");
    app.ensure_child(id, "");
    let mut last_post: Option<(u64, Value, Value)> = None;
    let child = app.children.get_mut(id).expect("child");
    match kind {
        "thinking" => {
            let redacted = ev.get("redacted").and_then(Value::as_bool).unwrap_or(false);
            let delta = ev.get("delta").and_then(Value::as_bool).unwrap_or(false);
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            apply_thinking(&mut child.story, &mut child.stream, redacted, text, delta);
        }
        "speech" => {
            let delta = ev.get("delta").and_then(Value::as_bool).unwrap_or(false);
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            apply_speech(&mut child.story, &mut child.stream, text, delta);
        }
        "post" => {
            let n = ev.get("n").and_then(Value::as_u64).unwrap_or(0);
            if let Some(req) = ev.get("request") {
                last_post = Some((n, req.clone(), json!({})));
            }
        }
        "complete" => {
            child.stream.finish(&mut child.story);
            finish_exec(&mut child.calls, &mut child.exec);
            let n = ev.get("n").and_then(Value::as_u64).unwrap_or(0);
            let origin = ev.get("origin").and_then(Value::as_str).unwrap_or("llm");
            let model = ev.get("model").and_then(Value::as_str).unwrap_or("?");
            let thinking = ev.get("thinking").and_then(Value::as_str).unwrap_or("");
            let usage = ev.get("usage").cloned().unwrap_or(json!({}));
            let thoughts = ev.get("thoughts").and_then(Value::as_u64).unwrap_or(0);
            let redacted = ev.get("redacted").and_then(Value::as_u64).unwrap_or(0);
            wire_push(
                &mut child.calls,
                wire_complete(origin, n, model, thinking, &usage, thoughts, redacted),
            );
            if let (Some(req), Some(resp)) = (ev.get("request"), ev.get("response")) {
                last_post = Some((n, req.clone(), resp.clone()));
            }
        }
        "result" => {
            child.stream.finish(&mut child.story);
            apply_result(&mut child.calls, &mut child.exec, ev);
        }
        "turn" => {
            child.stream.finish(&mut child.story);
            start_thinking(&mut child.story, &mut child.stream);
        }
        _ => {}
    }
    if let Some((n, req, resp)) = last_post {
        app.set_last_post(n, &req, &resp);
    }
}

fn handle_event(app: &mut App, ev: Value) {
    let kind = ev.get("ev").and_then(Value::as_str).unwrap_or("");
    match kind {
        "picker" => app.picker.observe(&ev),
        "login" => {
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            let done = ev.get("done").and_then(Value::as_bool).unwrap_or(false)
                || ev.get("failed").and_then(Value::as_bool).unwrap_or(false);
            app.picker.login_line(text, done);
        }
        "ready" | "snapshot" => {
            app.picker.observe(&ev);
            if let Some(b) = ev.get("billing").and_then(Value::as_str) {
                app.cache.plan = b == "plan";
            }
            if let Some(s) = ev.get("model").and_then(Value::as_str) {
                app.model = s.into();
            }
            if let Some(s) = ev.get("thinking").and_then(Value::as_str) {
                app.thinking = s.into();
            }
            if let Some(n) = ev.get("generation").and_then(Value::as_u64) {
                app.generation = n.to_string();
            } else if let Some(s) = ev.get("generation").and_then(Value::as_str) {
                app.generation = s.into();
            }
            app.ready = true;
            if !app.running {
                app.status = "idle".into();
            }
        }
        "subagent" => handle_subagent(app, &ev),
        "child" => handle_child(app, &ev),
        "thinking" => {
            let redacted = ev.get("redacted").and_then(Value::as_bool).unwrap_or(false);
            let delta = ev.get("delta").and_then(Value::as_bool).unwrap_or(false);
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            apply_thinking(&mut app.story, &mut app.stream, redacted, text, delta);
        }
        "speech" => {
            let delta = ev.get("delta").and_then(Value::as_bool).unwrap_or(false);
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            apply_speech(&mut app.story, &mut app.stream, text, delta);
        }
        "result" => {
            app.stream.finish(&mut app.story);
            let phase = ev.get("phase").and_then(Value::as_str).unwrap_or("done");
            if phase != "start" && phase != "delta" {
                let tag = ev.get("tag").and_then(Value::as_str).unwrap_or("?");
                let target = call_target(tag, &ev);
                app.stream.run.call(tag, target);
                app.stream.run.sync(&mut app.story);
            }
            apply_result(&mut app.calls, &mut app.exec, &ev);
        }
        "post" => {
            let n = ev.get("n").and_then(Value::as_u64).unwrap_or(0);
            let empty = json!({});
            let req = ev.get("request").unwrap_or(&empty);
            app.set_last_post(n, req, &empty);
        }
        // The wire pane exists so the human sees what the harness did. A fold
        // rewrites the transcript the model reads, so it belongs here and not
        // in the story — it is not something the model said.
        "compacted" => {
            let n = ev.get("n").and_then(Value::as_u64).unwrap_or(0);
            let kept = ev.get("kept").and_then(Value::as_u64).unwrap_or(0);
            let summary = ev.get("text").and_then(Value::as_str).unwrap_or("");
            app.call_push(wire_compacted(n, kept, summary));
        }
        "complete" => {
            app.stream.finish(&mut app.story);
            finish_exec(&mut app.calls, &mut app.exec);
            let n = ev.get("n").and_then(Value::as_u64).unwrap_or(0);
            let origin = ev.get("origin").and_then(Value::as_str).unwrap_or("llm");
            let model = ev.get("model").and_then(Value::as_str).unwrap_or("?");
            let thinking = ev.get("thinking").and_then(Value::as_str).unwrap_or("");
            let usage = ev.get("usage").cloned().unwrap_or(json!({}));
            app.cache.observe(&usage, model);
            if let Some(req) = ev.get("request") {
                app.cache.observe_roles(req);
            }
            let thoughts = ev.get("thoughts").and_then(Value::as_u64).unwrap_or(0);
            let redacted = ev.get("redacted").and_then(Value::as_u64).unwrap_or(0);
            app.call_push(wire_complete(
                origin, n, model, thinking, &usage, thoughts, redacted,
            ));
            let empty = json!({});
            let req = ev.get("request").unwrap_or(&empty);
            let resp = ev.get("response").unwrap_or(&empty);
            if req != &empty || resp != &empty {
                app.set_last_post(n, req, resp);
            }
        }
        "turn" => {
            app.status = "running".into();
            app.stream.finish(&mut app.story);
            start_thinking(&mut app.story, &mut app.stream);
        }
        "done" => {
            app.stream.finish(&mut app.story);
            app.stream.run.fold(&mut app.story);
            finish_exec(&mut app.calls, &mut app.exec);
            app.running = false;
            app.turn_started = None;
            app.status = "idle".into();
            app.drain_after = !app.queue.is_empty();
        }
        "stopped" => {
            app.stream.finish(&mut app.story);
            finish_exec(&mut app.calls, &mut app.exec);
            let t = ev.get("text").and_then(Value::as_str).unwrap_or("stopped, saved");
            app.story_push(RenderBlock::system(t));
            app.running = false;
            app.turn_started = None;
            app.status = "idle".into();
            app.drain_after = app.send_now && !app.queue.is_empty();
        }
        "error" => {
            app.stream.finish(&mut app.story);
            finish_exec(&mut app.calls, &mut app.exec);
            let t = ev.get("text").and_then(Value::as_str).unwrap_or("error");
            app.story_push(RenderBlock::system(t));
            app.running = false;
            app.turn_started = None;
            app.status = "error".into();
        }
        _ => {}
    }
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
            app.model = model;
            app.thinking = effort;
        }
    }
    Ok(false)
}

fn handle_key(
    mut bridge: Option<&mut Bridge>,
    app: &mut App,
    key: KeyEvent,
) -> io::Result<bool> {
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
        return on_ctrl_c(bridge.as_deref_mut(), app);
    }
    // The picker is modal on purpose. On a fresh machine there is no session
    // behind it to type into, so it has to win before any pane sees the key.
    if app.picker.open {
        let action = app.picker.key(key.code);
        return apply_picker(bridge.as_deref_mut(), app, action);
    }
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('p') {
        app.picker.open_for_change();
        return Ok(false);
    }
    // ctrl+g / ctrl+b open the side panes from anywhere, including the input
    // box: a pane you have to tab to before you can open is a pane nobody
    // opens. Pressing the key on an open pane closes it again.
    if key.modifiers.contains(KeyModifiers::CONTROL)
        && matches!(key.code, KeyCode::Char('g') | KeyCode::Char('b'))
        && app.viewer.is_none()
        && app.post_inspect.is_none()
    {
        let git = key.code == KeyCode::Char('g');
        let (h, focus) = if git {
            (&mut app.layout.git_h, Focus::Git)
        } else {
            (&mut app.layout.files_h, Focus::Files)
        };
        if *h == 0 {
            *h = PaneLayout::OPEN_SIDE;
            app.layout.save();
            app.set_focus(focus);
            if git {
                app.git.poll(true);
            }
        } else {
            *h = 0;
            app.layout.save();
            if app.focus == focus {
                app.set_focus(Focus::Input);
            }
        }
        return Ok(false);
    }

    // Pane resize runs before every pane-specific branch: the POST trees and
    // the queue consume their keys and return, so a resize handled later never
    // reaches them. `+` grows the focused pane, `-` shrinks it, `0` resets.
    if app.focus != Focus::Input && app.viewer.is_none() && app.post_inspect.is_none() {
        match key.code {
            KeyCode::Char('+') | KeyCode::Char('=') => {
                app.layout.grow(app.focus, 2);
                app.layout.save();
                return Ok(false);
            }
            KeyCode::Char('-') | KeyCode::Char('_') => {
                app.layout.grow(app.focus, -2);
                app.layout.save();
                return Ok(false);
            }
            KeyCode::Char('0') => {
                app.layout = PaneLayout::default();
                app.layout.save();
                return Ok(false);
            }
            // ctrl+arrows resize along the arrow: up/down changes rows even for
            // panes whose `+` key drives width, left/right changes width even
            // for the ones whose `+` drives rows.
            KeyCode::Up | KeyCode::Down | KeyCode::Left | KeyCode::Right
                if key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                let (axis, by) = match key.code {
                    KeyCode::Up => (Axis::Vertical, 2),
                    KeyCode::Down => (Axis::Vertical, -2),
                    KeyCode::Right => (Axis::Horizontal, 2),
                    _ => (Axis::Horizontal, -2),
                };
                app.layout.grow_axis(app.focus, axis, by);
                app.layout.save();
                return Ok(false);
            }
            _ => {}
        }
    }
    if app.viewer.is_some() {
        if is_inline_paste_key(&key) || is_paste_key(&key) {
            match clipboard_text() {
                Some(text) => {
                    if let Some(viewer) = app.viewer.as_mut() {
                        viewer.handle_paste(&text);
                    }
                }
                None => app.status = "clipboard empty".into(),
            }
            return Ok(false);
        }
        handle_viewer_key(app, key);
        return Ok(false);
    }
    if app.post_inspect.is_some() {
        if is_inline_paste_key(&key) || is_paste_key(&key) {
            match clipboard_text() {
                Some(text) => {
                    if let Some(v) = app
                        .post_inspect
                        .as_mut()
                        .and_then(|p| p.raw_viewer.as_mut())
                    {
                        v.handle_paste(&text);
                    }
                }
                None => app.status = "clipboard empty".into(),
            }
            return Ok(false);
        }
        handle_post_inspect_key(app, key);
        return Ok(false);
    }
    if is_inline_paste_key(&key) {
        match clipboard_text() {
            Some(text) => apply_paste(app, &text, true),
            None => app.status = "clipboard empty".into(),
        }
        return Ok(false);
    }
    if is_paste_key(&key) {
        match clipboard_text() {
            Some(text) => apply_paste(app, &text, false),
            None => app.status = "clipboard empty".into(),
        }
        return Ok(false);
    }
    match key.code {
        KeyCode::Tab if key.modifiers.contains(KeyModifiers::SHIFT) => {
            app.set_focus(app.focus.prev_open(&pane_open(app)));
            return Ok(false);
        }
        KeyCode::BackTab => {
            app.set_focus(app.focus.prev_open(&pane_open(app)));
            return Ok(false);
        }
        KeyCode::Tab => {
            app.set_focus(app.focus.next_open(&pane_open(app)));
            return Ok(false);
        }
        KeyCode::Esc => {
            if app.story_text.persist.take().is_some()
                || app.calls_text.persist.take().is_some()
                || app.children.values_mut().any(|c| {
                    c.story_text.persist.take().is_some() || c.calls_text.persist.take().is_some()
                })
            {
                return Ok(false);
            }
            if app.viewing.take().is_some() {
                app.focus = Focus::Story;
                return Ok(false);
            }
            if app.focus == Focus::Input {
                app.set_focus(Focus::Story);
                return Ok(false);
            }
            if app.focus == Focus::Queue {
                app.set_focus(Focus::Input);
                return Ok(false);
            }
            // Esc steps back one pane rather than reaching the quit below it.
            // These branches used to live in the per-pane handlers further
            // down, where this match had already returned — so Esc anywhere in
            // the side column fell through to `focused_scroll`, which maps
            // every non-Calls focus to the story, found no selection there,
            // and quit the harness.
            if app.focus == Focus::Files {
                app.set_focus(Focus::Git);
                return Ok(false);
            }
            if app.focus == Focus::Git || app.focus == Focus::Meter {
                app.set_focus(Focus::Input);
                return Ok(false);
            }
            let sb = app.focused_scroll();
            if sb.selected().is_some() {
                sb.clear_selection();
                return Ok(false);
            }
            return Ok(true);
        }
        _ => {}
    }

    if matches!(app.focus, Focus::PostIn | Focus::PostOut) {
        let view_h = if app.focus == Focus::PostIn {
            app.post_in_area.height.saturating_sub(2)
        } else {
            app.post_out_area.height.saturating_sub(2)
        };
        match key.code {
            KeyCode::Char('j') | KeyCode::Down => {
                if let Some(t) = app.focused_tree() {
                    t.select_next();
                }
            }
            KeyCode::Char('k') | KeyCode::Up => {
                if let Some(t) = app.focused_tree() {
                    t.select_prev();
                }
            }
            KeyCode::Char('h') | KeyCode::Left => {
                if let Some(t) = app.focused_tree() {
                    t.collapse();
                }
            }
            KeyCode::Char('l') | KeyCode::Right | KeyCode::Enter => {
                if let Some(t) = app.focused_tree() {
                    t.toggle();
                }
            }
            KeyCode::Char('f') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                app.open_post_inspect();
            }
            KeyCode::Char('e') => app.open_post_inspect(),
            KeyCode::PageUp => {
                if let Some(t) = app.focused_tree() {
                    t.scroll_up(8);
                }
            }
            KeyCode::PageDown => {
                if let Some(t) = app.focused_tree() {
                    t.scroll_down(8, view_h);
                }
            }
            KeyCode::Char('i') => app.set_focus(Focus::Input),
            _ => {}
        }
        return Ok(false);
    }

    if app.focus == Focus::Queue {
        match key.code {
            KeyCode::Char('j') | KeyCode::Down => app.queue.select_next(),
            KeyCode::Char('k') | KeyCode::Up => app.queue.select_prev(),
            // The queue's second axis is order, so that is what ←/→ drive.
            KeyCode::Char('[') | KeyCode::Char('h') | KeyCode::Left => {
                app.queue.move_selected(-1)
            }
            KeyCode::Char(']') | KeyCode::Char('l') | KeyCode::Right => {
                app.queue.move_selected(1)
            }
            KeyCode::Char('d') | KeyCode::Backspace | KeyCode::Delete => {
                app.queue.remove_selected();
                if app.queue.is_empty() {
                    app.set_focus(Focus::Input);
                }
            }
            KeyCode::Enter => return send_now(bridge, app),
            KeyCode::Char('i') => app.set_focus(Focus::Input),
            _ => {}
        }
        return Ok(false);
    }

    if app.focus == Focus::Git && !matches!(key.code, KeyCode::Tab | KeyCode::BackTab) {
        let rows = app.git_area.height.saturating_sub(2) as usize;
        match key.code {
            KeyCode::Char('j') | KeyCode::Down => app.git.select(1),
            KeyCode::Char('k') | KeyCode::Up => app.git.select(-1),
            KeyCode::PageDown => app.git.select(rows as i32),
            KeyCode::PageUp => app.git.select(-(rows as i32)),
            // Git's second axis is the tab strip in its own title bar, so ←/→
            // move along it. Going *in* is Enter, which lands in the file pane.
            KeyCode::Char(']') | KeyCode::Right => app.git.next_tab(1),
            KeyCode::Char('[') | KeyCode::Left => app.git.next_tab(-1),
            KeyCode::Char('r') => app.git.poll(true),
            KeyCode::Char('i') => app.set_focus(Focus::Input),
            KeyCode::Enter | KeyCode::Char('l') => {
                // Opening a row is what fills the file pane, so open that pane
                // too rather than loading into something invisible.
                let path = app.git.selected().and_then(|r| r.path.clone());
                if let Some(p) = path {
                    app.files.open(&p);
                    if app.layout.files_h == 0 {
                        app.layout.files_h = PaneLayout::OPEN_SIDE;
                        app.layout.save();
                    }
                    app.set_focus(Focus::Files);
                }
            }
            _ => {}
        }
        // Walking the list previews as it goes, the way druk's tree does. A row
        // that names no file (a branch, a commit) leaves the pane alone.
        let path = app.git.selected().and_then(|r| r.path.clone());
        app.files.preview(path.as_deref());
        return Ok(false);
    }
    if app.focus == Focus::Files && !matches!(key.code, KeyCode::Tab | KeyCode::BackTab) {
        let rows = app.files_area.height.saturating_sub(2) as usize;
        match key.code {
            KeyCode::Char('j') | KeyCode::Down => app.files.move_by(1, rows),
            KeyCode::Char('k') | KeyCode::Up => app.files.move_by(-1, rows),
            KeyCode::PageDown => app.files.move_by(rows as i32, rows),
            KeyCode::PageUp => app.files.move_by(-(rows as i32), rows),
            // Down the tree and back up it. `←` out of a file lands on that
            // file in its own directory, so `←` `→` is a round trip.
            KeyCode::Char('l') | KeyCode::Right | KeyCode::Enter => app.files.enter(),
            KeyCode::Char('h') | KeyCode::Left => app.files.back(),
            KeyCode::Char('i') => app.set_focus(Focus::Input),
            _ => {}
        }
        return Ok(false);
    }
    // The meter has no cursor and nothing to fold. Without this it fell into
    // the scrollback branch below, where `focused_scroll` maps every non-Calls
    // focus to the story — so j/k in the meter silently drove the story pane.
    if app.focus == Focus::Meter {
        if key.code == KeyCode::Char('i') {
            app.set_focus(Focus::Input);
        }
        return Ok(false);
    }
    if app.focus != Focus::Input {
        match key.code {
            KeyCode::Char('j') | KeyCode::Down => app.focused_scroll().select_next(),
            KeyCode::Char('k') | KeyCode::Up => app.focused_scroll().select_prev(),
            KeyCode::Char('h') | KeyCode::Left => {
                if app.focus == Focus::Calls {
                    pin_selected_wire(app);
                }
                app.focused_scroll().collapse_selected()
            }
            KeyCode::Char('f') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                let _ = app.open_block_viewer();
            }
            KeyCode::Char('l') | KeyCode::Right | KeyCode::Enter => {
                // grok: Enter on a SubagentBlock opens the child session.
                // Anything else with a viewer zooms into BlockViewerPane.
                if key.code == KeyCode::Enter && app.open_block_viewer() {
                } else {
                    if app.focus == Focus::Calls {
                        pin_selected_wire(app);
                    }
                    app.focused_scroll().toggle_fold_selected();
                }
            }
            KeyCode::Char('r') => app.focused_scroll().toggle_raw_selected(),
            KeyCode::PageUp => app.focused_scroll().page_up(),
            KeyCode::PageDown => app.focused_scroll().page_down(),
            KeyCode::Char('i') => app.set_focus(Focus::Input),
            _ => {}
        }
        return Ok(false);
    }

    let width = app.input_inner.width.max(20);
    match key.code {
        KeyCode::Char('a') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.prompt.move_line_home(width);
        }
        KeyCode::Char('e') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.prompt.move_line_end(width);
        }
        KeyCode::Char(c) if is_text_key(&key) => app.prompt.insert_char(c),
        KeyCode::Backspace => app.prompt.backspace(),
        KeyCode::Delete => app.prompt.delete(),
        KeyCode::Left => app.prompt.move_left(),
        KeyCode::Right => app.prompt.move_right(),
        KeyCode::Up => app.prompt.move_up(width),
        KeyCode::Down => app.prompt.move_down(width),
        KeyCode::Home => app.prompt.move_line_home(width),
        KeyCode::End => app.prompt.move_line_end(width),
        KeyCode::Enter if is_mod_enter(&key) => {
            app.prompt.insert_char('\n');
        }
        KeyCode::Enter => {
            if app.prompt.expand_at_cursor() {
                return Ok(false);
            }
            if app.prompt.apply_backslash_continuation() {
                return Ok(false);
            }
            return submit_prompt(bridge, app);
        }
        _ => {}
    }
    Ok(false)
}

/// Tab skips panes the layout has collapsed to nothing.
fn pane_open(app: &App) -> impl Fn(Focus) -> bool + use<> {
    let queue = !app.queue.is_empty();
    let post = app.layout.post_h > 0;
    let meter = app.layout.meter_h > 0;
    let git = app.layout.git_h > 0;
    let files = app.layout.files_h > 0;
    move |f| match f {
        Focus::Queue => queue,
        Focus::PostIn | Focus::PostOut => post,
        Focus::Meter => meter,
        Focus::Git => git,
        Focus::Files => files,
        _ => true,
    }
}

fn apply_paste(app: &mut App, text: &str, inline: bool) {
    if app.focus != Focus::Input {
        app.set_focus(Focus::Input);
    }
    if inline {
        app.prompt.handle_inline_paste(text);
    } else {
        app.prompt.handle_paste(text);
    }
}

fn is_local_slash(line: &str) -> bool {
    let t = line.trim();
    t == "/quit"
        || t == "/exit"
        || t == "/timestamps"
        || t == "/compact"
        || t == "/reset"
        || t == "/reload"
        || t.starts_with("/theme")
        || t.starts_with("/thinking")
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
    app.status = "send now".into();
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
    start_step(bridge, app, item.text)
}

fn start_step(
    mut bridge: Option<&mut Bridge>,
    app: &mut App,
    line: String,
) -> io::Result<()> {
    app.story_push(RenderBlock::user_prompt(&line));
    app.story.follow_new_turn(None, false);
    if app.viewing.is_some() {
        app.status = "esc to leave session".into();
        return Ok(());
    }
    if let Some(b) = bridge.as_mut() {
        app.running = true;
        app.turn_started = Some(Instant::now());
        app.status = "running".into();
        b.send(&json!({"op": "step", "text": line}))?;
    } else {
        app.call_push(wire_complete("user", 0, "demo", "", &json!({}), 0, 0));
    }
    Ok(())
}

fn submit_prompt(mut bridge: Option<&mut Bridge>, app: &mut App) -> io::Result<bool> {
    let line = app.prompt.to_send();
    if line.trim().is_empty() {
        if app.running || !app.queue.is_empty() {
            return send_now(bridge, app);
        }
        return Ok(false);
    }
    app.prompt.clear();
    if line == "/quit" || line == "/exit" {
        return Ok(true);
    }
    if is_local_slash(&line) {
        app.story_push(RenderBlock::user_prompt(&line));
        app.story.follow_new_turn(None, false);
    if let Some(name) = line.strip_prefix("/theme") {
        let name = name.trim();
        if name.is_empty() {
            app.status = format!("theme {}", Theme::current_kind().display_name());
        } else if let Some(kind) = ThemeKind::from_name(name) {
            let kind = if kind.is_auto() {
                theme_cache::resolve_initial_theme()
            } else {
                kind
            };
            theme_cache::set(kind);
            app.apply_grok_settings();
            app.status = format!("theme {}", kind.display_name());
        } else {
            app.status = "theme: groknight tokyonight grokday rosepine oscura auto".into();
        }
    } else if line == "/timestamps" {
        let on = !appearance_cache::load_timestamps();
        appearance_cache::set_timestamps(on);
        app.apply_grok_settings();
        app.status = if on {
            "timestamps on"
        } else {
            "timestamps off"
        }
        .into();
    } else if line == "/compact" {
        let on = !appearance_cache::load();
        appearance_cache::set(on);
        app.apply_grok_settings();
        app.status = if on { "compact on" } else { "compact off" }.into();
    } else if let Some(level) = line.strip_prefix("/thinking") {
        let level = level.trim();
        if !level.is_empty() {
            if let Some(b) = bridge.as_mut() {
                b.send(&json!({"op":"thinking","level": level}))?;
            }
            app.thinking = level.into();
        }
    } else if line == "/reset" {
        if let Some(b) = bridge.as_mut() {
            b.send(&json!({"op":"reset"}))?;
        }
        app.story.clear();
        app.calls.clear();
        app.post_in.clear();
        app.post_out.clear();
        app.post_req = json!({});
        app.post_resp = json!({});
        app.post_inspect = None;
        app.post_n = 0;
        app.post_out_n = 0;
        app.queue.clear();
        app.send_now = false;
    } else if line == "/reload" {
        if let Some(b) = bridge.as_mut() {
            b.send(&json!({"op":"reload"}))?;
        }
    }
        return Ok(false);
    }
    if app.running {
        app.queue.push(line);
        app.status = format!("queued #{}", app.queue.len());
        return Ok(false);
    }
    start_step(bridge, app, line)?;
    Ok(false)
}

fn hit(area: Rect, col: u16, row: u16) -> bool {
    col >= area.x
        && col < area.x.saturating_add(area.width)
        && row >= area.y
        && row < area.y.saturating_add(area.height)
}

fn handle_mouse(app: &mut App, m: MouseEvent) {
    if app.viewer.is_some() {
        handle_viewer_mouse(app, m);
        return;
    }
    if app.post_inspect.is_some() {
        handle_post_inspect_mouse(app, m);
        return;
    }
    app.mouse = Some((m.column, m.row));
    if matches!(m.kind, MouseEventKind::Down(MouseButton::Left)) {
        if let Some(area) = app.turn_cancel {
            if hit(area, m.column, m.row) {
                app.want_stop = true;
                return;
            }
        }
    }
    let on_calls = hit(app.call_area, m.column, m.row);
    let on_story = hit(app.traj_area, m.column, m.row);
    let on_post_in = hit(app.post_in_area, m.column, m.row);
    let on_post_out = hit(app.post_out_area, m.column, m.row);
    let on_queue = hit(app.queue_area, m.column, m.row) && !app.queue.is_empty();
    let on_input = hit(app.input_area, m.column, m.row);

    match m.kind {
        MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
            let up = matches!(m.kind, MouseEventKind::ScrollUp);
            if on_calls {
                wheel_scroll(app.calls_scroll(), up, 3);
            } else if on_story {
                wheel_scroll(app.story_scroll(), up, 3);
            } else if on_post_in {
                if up {
                    app.post_in.scroll_up(3);
                } else {
                    app.post_in
                        .scroll_down(3, app.post_in_area.height.saturating_sub(2));
                }
            } else if on_post_out {
                if up {
                    app.post_out.scroll_up(3);
                } else {
                    app.post_out
                        .scroll_down(3, app.post_out_area.height.saturating_sub(2));
                }
            }
        }
        MouseEventKind::Down(MouseButton::Left) => {
            if on_queue {
                app.set_focus(Focus::Queue);
                if app.queue_area.height > 2 {
                    let row = m.row.saturating_sub(app.queue_area.y.saturating_add(1)) as usize;
                    let skip = app.queue.len().saturating_sub(6);
                    let idx = skip + row;
                    if idx < app.queue.len() {
                        app.queue.selected = Some(idx);
                    }
                }
                return;
            }
            if on_input {
                app.set_focus(Focus::Input);
                if hit(app.input_inner, m.column, m.row) {
                    let col = m.column.saturating_sub(app.input_inner.x);
                    let row = m.row.saturating_sub(app.input_inner.y);
                    let hit_chip = app.prompt.click(col, row, app.input_inner.width);
                    if let Some(id) = hit_chip {
                        let now = Instant::now();
                        let dbl = app
                            .last_chip_click
                            .map(|(t, cid)| cid == id && now.duration_since(t).as_millis() < 350)
                            .unwrap_or(false);
                        if dbl {
                            app.prompt.expand_chip_id(id);
                            app.last_chip_click = None;
                        } else {
                            app.last_chip_click = Some((now, id));
                        }
                    }
                }
                return;
            }
            if on_post_in || on_post_out {
                let (tree, area) = if on_post_in {
                    app.set_focus(Focus::PostIn);
                    (&mut app.post_in, app.post_in_area)
                } else {
                    app.set_focus(Focus::PostOut);
                    (&mut app.post_out, app.post_out_area)
                };
                if area.height > 2 && area.width > 2 {
                    let row = m.row.saturating_sub(area.y.saturating_add(1));
                    tree.click(row, area.width.saturating_sub(2));
                    let now = Instant::now();
                    let pane = if on_post_in { 2u8 } else { 3 };
                    let dbl = app
                        .last_click
                        .map(|(t, _, p)| p == pane && now.duration_since(t).as_millis() < 350)
                        .unwrap_or(false);
                    if dbl {
                        app.last_click = None;
                        app.open_post_inspect();
                    } else {
                        app.last_click = Some((now, 0, pane));
                    }
                }
                return;
            }
            if on_calls {
                app.set_focus(Focus::Calls);
            } else if on_story {
                app.set_focus(Focus::Story);
            } else {
                return;
            }
            handle_scrollback_down(app, on_calls, m.column, m.row);
        }
        MouseEventKind::Drag(MouseButton::Left) => {
            if on_calls || on_story {
                handle_scrollback_drag(app, on_calls, m.column, m.row);
            }
        }
        MouseEventKind::Up(MouseButton::Left) => {
            if on_calls || on_story || app.story_text.active.is_some() || app.calls_text.active.is_some()
            {
                handle_scrollback_up(app, on_calls, m.column, m.row);
            }
        }
        _ => {}
    }
}

fn handle_scrollback_down(app: &mut App, calls: bool, col: u16, row: u16) {
    let model = app.sel_model(calls).clone();
    app.text_sel(calls).clear();
    if let Some(hit) = model.hit_test_text_exact(col, row) {
        let width = model.visible_block_content_width(hit.entry_idx);
        {
            let sel = app.text_sel(calls);
            sel.pending = Some(PendingTextDrag {
                anchor: hit,
                start_col: col,
                start_row: row,
                anchor_content_width: width,
            });
            sel.note_click(Instant::now(), hit);
        }
        if calls {
            app.calls_scroll().set_selected(Some(hit.entry_idx));
        } else {
            app.story_scroll().set_selected(Some(hit.entry_idx));
        }
        return;
    }
    let Some(geom) = model.hit_test_visible_block(col, row) else {
        return;
    };
    let idx = geom.entry_idx;
    let now = Instant::now();
    let pane: u8 = if calls { 1 } else { 0 };
    let dbl = app
        .last_click
        .map(|(t, e, p)| p == pane && e == idx && now.duration_since(t).as_millis() < 350)
        .unwrap_or(false);
    if calls {
        app.calls_scroll().set_selected(Some(idx));
    } else {
        app.story_scroll().set_selected(Some(idx));
    }
    if dbl {
        app.last_click = None;
        if !calls && app.open_selected_session() {
            return;
        }
        if calls {
            pin_selected_wire(app);
            app.calls_scroll().toggle_fold_selected();
        } else {
            app.story_scroll().toggle_fold_selected();
        }
    } else {
        app.last_click = Some((now, idx, pane));
    }
}

fn handle_scrollback_drag(app: &mut App, calls: bool, col: u16, row: u16) {
    let model = app.sel_model(calls).clone();
    let sel = app.text_sel(calls);
    if let Some(pending) = sel.pending {
        if !drag_threshold_exceeded(&pending, col, row) {
            return;
        }
        let head = model
            .hit_test_nearest_in_range(pending.anchor, col, row)
            .unwrap_or(pending.anchor);
        sel.active = Some(ActiveTextDrag {
            anchor: pending.anchor,
            head,
            kind: SelectionKind::Linear,
            anchor_content_width: pending.anchor_content_width,
        });
        return;
    }
    if let Some(mut drag) = sel.active {
        if let Some(head) = model.hit_test_nearest_in_range(drag.anchor, col, row) {
            drag.head = head;
            sel.active = Some(drag);
        }
    }
}

fn handle_scrollback_up(app: &mut App, calls: bool, _col: u16, _row: u16) {
    let model = app.sel_model(calls).clone();
    let copied = {
        let sel = app.text_sel(calls);
        if let Some(drag) = sel.active.take() {
            sel.pending = None;
            if let Some(text) = reconstruct_selection_text(&model, &drag) {
                if !text.is_empty() {
                    sel.persist = Some(PersistentTextSelection {
                        entry_idx: drag.anchor.entry_idx,
                        range_id: drag.anchor.range_id,
                        anchor: SelectionEndpoint {
                            block_line_idx: drag.anchor.block_line_idx,
                            col_within_range: drag.anchor.col_within_range,
                        },
                        head: SelectionEndpoint {
                            block_line_idx: drag.head.block_line_idx,
                            col_within_range: drag.head.col_within_range,
                        },
                        origin: SelectionOrigin::Drag,
                        kind: drag.kind,
                    });
                    let _ = SystemClipboard::try_set(&text);
                    Some(text)
                } else {
                    None
                }
            } else {
                None
            }
        } else if let Some(pending) = sel.pending.take() {
            let clicks = sel.clicks;
            if clicks >= 2 {
                if let Some(word) =
                    semantic_selection_at(&model, &pending.anchor, configured_word_separators())
                {
                    sel.persist = Some(PersistentTextSelection {
                        entry_idx: pending.anchor.entry_idx,
                        range_id: pending.anchor.range_id,
                        anchor: word.anchor,
                        head: word.head,
                        origin: if clicks >= 3 {
                            SelectionOrigin::TripleClick
                        } else {
                            SelectionOrigin::DoubleClick
                        },
                        kind: SelectionKind::Linear,
                    });
                    if word.text.is_empty() {
                        None
                    } else {
                        let _ = SystemClipboard::try_set(&word.text);
                        Some(word.text)
                    }
                } else {
                    None
                }
            } else {
                None
            }
        } else {
            None
        }
    };
    if copied.is_some() {
        app.status = "copied".into();
    }
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
        RenderBlock::UserPrompt(p) => {
            Some(BlockViewerPane::for_plain_text("you", &p.text.clone()))
        }
        RenderBlock::System(s) => Some(BlockViewerPane::for_plain_text("system", &s.text.clone())),
        RenderBlock::Subagent(_) => None,
        _ => None,
    }
}

fn handle_viewer_key(app: &mut App, key: KeyEvent) {
    let mut raw = false;
    let mut id = None;
    let mut selected = None;
    {
        let Some(viewer) = app.viewer.as_mut() else {
            return;
        };
        if viewer.is_close_key(&key) {
            app.viewer = None;
            return;
        }
        if !viewer.handle_key(&key) {
            return;
        }
        if viewer.raw_toggle_pending {
            viewer.raw_toggle_pending = false;
            viewer.list_state.set_scroll_anchor();
            raw = true;
            id = Some(viewer.entry_id);
            selected = viewer.list_state.selected_id();
        }
    }
    if raw {
        let id = id.expect("raw toggle has an entry");
        let old_source = app.viewer_scroll().get_by_id(id).and_then(|entry| {
            selected.and_then(|sid| BlockViewerPane::source_line_for_id(&entry.block, sid))
        });
        if let Some(entry) = app.viewer_scroll().get_by_id_mut(id) {
            entry.toggle_raw();
        }
        if let Some(entry) = app.viewer_scroll().get_by_id(id).cloned() {
            if let Some(viewer) = app.viewer.as_mut() {
                viewer.rebuild_items(&entry);
                viewer.jump_to_source_line(&entry, old_source);
            }
        }
    }
    let id = app.viewer.as_ref().map(|v| v.entry_id);
    let entry = id.and_then(|id| app.viewer_scroll().get_by_id(id).cloned());
    if let (Some(entry), Some(viewer)) = (entry, app.viewer.as_mut()) {
        if let Some(text) = viewer.process_pending_copy(&entry) {
            let _ = SystemClipboard::try_set(&text);
            app.status = "copied".into();
        }
    }
}

fn handle_viewer_mouse(app: &mut App, m: MouseEvent) {
    let mut close = false;
    let mut drag = None;
    let mut id = None;
    {
        let Some(viewer) = app.viewer.as_mut() else {
            return;
        };
        match handle_modal_mouse(&mut viewer.modal, m.kind, m.column, m.row) {
            ModalWindowOutcome::CloseRequested => close = true,
            ModalWindowOutcome::Handled => return,
            _ => {
                match m.kind {
                    MouseEventKind::ScrollDown => viewer.handle_scroll(3),
                    MouseEventKind::ScrollUp => viewer.handle_scroll(-3),
                    MouseEventKind::Down(MouseButton::Left)
                    | MouseEventKind::Drag(MouseButton::Left)
                    | MouseEventKind::Up(MouseButton::Left)
                    | MouseEventKind::Moved => {
                        viewer.handle_mouse(m.kind, m.column, m.row);
                    }
                    _ => {}
                }
                drag = viewer.drag_copy_text.take();
                id = Some(viewer.entry_id);
            }
        }
    }
    if close {
        app.viewer = None;
        return;
    }
    let key_text = if drag.is_none() {
        id.and_then(|id| app.viewer_scroll().get_by_id(id).cloned())
            .and_then(|entry| {
                app.viewer
                    .as_mut()
                    .and_then(|v| v.process_pending_copy(&entry))
            })
    } else {
        None
    };
    if let Some(text) = drag.or(key_text) {
        let _ = SystemClipboard::try_set(&text);
        app.status = "copied".into();
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
        let Some(content) = render_modal_window(buf, area, &mut viewer.modal, &config, &theme) else {
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

fn handle_post_inspect_key(app: &mut App, key: KeyEvent) {
    let n = app.post_n;
    let req = app.post_req.clone();
    let resp = app.post_resp.clone();
    let footer = post_inspect_footer();
    let (title, mut config) = post_inspect_chrome(n, &footer);
    let title_owned = title;
    config.title = &title_owned;
    let close = {
        let Some(inspect) = app.post_inspect.as_mut() else {
            return;
        };
        match handle_modal_key(&mut inspect.modal, &key, &config) {
            ModalWindowOutcome::CloseRequested => true,
            ModalWindowOutcome::TabChanged(tab) => {
                inspect.set_tab(tab, n, &req, &resp);
                return;
            }
            _ => false,
        }
    };
    if close {
        app.post_inspect = None;
        return;
    }
    let none = KeyModifiers::NONE;
    match key.code {
        KeyCode::Char('q') if key.modifiers == none => {
            app.post_inspect = None;
            return;
        }
        KeyCode::Char('f') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.post_inspect = None;
            return;
        }
        KeyCode::Tab | KeyCode::Char(']') => {
            let tab = app
                .post_inspect
                .as_ref()
                .map(|p| (p.modal.active_tab + 1) % 2)
                .unwrap_or(0);
            if let Some(inspect) = app.post_inspect.as_mut() {
                inspect.set_tab(tab, n, &req, &resp);
            }
            return;
        }
        KeyCode::BackTab | KeyCode::Char('[') => {
            let tab = app
                .post_inspect
                .as_ref()
                .map(|p| if p.modal.active_tab == 0 { 1 } else { 0 })
                .unwrap_or(0);
            if let Some(inspect) = app.post_inspect.as_mut() {
                inspect.set_tab(tab, n, &req, &resp);
            }
            return;
        }
        KeyCode::Char('r') if key.modifiers == none => {
            if let Some(inspect) = app.post_inspect.as_mut() {
                inspect.toggle_raw(n, &req, &resp);
            }
            return;
        }
        KeyCode::Char('y') if key.modifiers == none => {
            let tab = app
                .post_inspect
                .as_ref()
                .map(|p| p.modal.active_tab)
                .unwrap_or(0);
            let val = if tab == 0 { &req } else { &resp };
            let _ = SystemClipboard::try_set(&pretty_json(val));
            app.status = "copied".into();
            return;
        }
        _ => {}
    }
    let raw = app.post_inspect.as_ref().is_some_and(|p| p.raw);
    if raw {
        let close_raw = app
            .post_inspect
            .as_ref()
            .and_then(|p| p.raw_viewer.as_ref())
            .is_some_and(|v| v.is_close_key(&key));
        if close_raw {
            app.post_inspect = None;
            return;
        }
        if let Some(viewer) = app
            .post_inspect
            .as_mut()
            .and_then(|p| p.raw_viewer.as_mut())
        {
            let _ = viewer.handle_key(&key);
        }
        return;
    }
    let view_h = app
        .post_inspect
        .as_ref()
        .map(|p| p.content.height)
        .unwrap_or(8);
    let tab = app
        .post_inspect
        .as_ref()
        .map(|p| p.modal.active_tab)
        .unwrap_or(0);
    let tree = if tab == 0 {
        &mut app.post_in
    } else {
        &mut app.post_out
    };
    match key.code {
        KeyCode::Char('j') | KeyCode::Down => tree.select_next(),
        KeyCode::Char('k') | KeyCode::Up => tree.select_prev(),
        KeyCode::Char('h') | KeyCode::Left => tree.collapse(),
        KeyCode::Char('l') | KeyCode::Right | KeyCode::Enter => tree.toggle(),
        KeyCode::PageUp => tree.scroll_up(8),
        KeyCode::PageDown => tree.scroll_down(8, view_h),
        _ => {}
    }
}

fn handle_post_inspect_mouse(app: &mut App, m: MouseEvent) {
    let n = app.post_n;
    let req = app.post_req.clone();
    let resp = app.post_resp.clone();
    let outcome = {
        let Some(inspect) = app.post_inspect.as_mut() else {
            return;
        };
        handle_modal_mouse(&mut inspect.modal, m.kind, m.column, m.row)
    };
    match outcome {
        ModalWindowOutcome::CloseRequested => {
            app.post_inspect = None;
            return;
        }
        ModalWindowOutcome::TabChanged(tab) => {
            if let Some(inspect) = app.post_inspect.as_mut() {
                inspect.set_tab(tab, n, &req, &resp);
            }
            return;
        }
        ModalWindowOutcome::Handled => return,
        _ => {}
    }
    let raw = app.post_inspect.as_ref().is_some_and(|p| p.raw);
    if raw {
        if let Some(viewer) = app
            .post_inspect
            .as_mut()
            .and_then(|p| p.raw_viewer.as_mut())
        {
            match m.kind {
                MouseEventKind::ScrollDown => viewer.handle_scroll(3),
                MouseEventKind::ScrollUp => viewer.handle_scroll(-3),
                MouseEventKind::Down(MouseButton::Left)
                | MouseEventKind::Drag(MouseButton::Left)
                | MouseEventKind::Up(MouseButton::Left)
                | MouseEventKind::Moved => {
                    viewer.handle_mouse(m.kind, m.column, m.row);
                }
                _ => {}
            }
        }
        return;
    }
    let area = app
        .post_inspect
        .as_ref()
        .map(|p| p.content)
        .unwrap_or_default();
    if area.width == 0 || area.height == 0 {
        return;
    }
    let on = m.column >= area.x
        && m.column < area.x.saturating_add(area.width)
        && m.row >= area.y
        && m.row < area.y.saturating_add(area.height);
    if !on {
        return;
    }
    let tab = app
        .post_inspect
        .as_ref()
        .map(|p| p.modal.active_tab)
        .unwrap_or(0);
    let tree = if tab == 0 {
        &mut app.post_in
    } else {
        &mut app.post_out
    };
    match m.kind {
        MouseEventKind::ScrollUp => tree.scroll_up(3),
        MouseEventKind::ScrollDown => tree.scroll_down(3, area.height),
        MouseEventKind::Down(MouseButton::Left) => {
            tree.click(m.row.saturating_sub(area.y), area.width);
        }
        _ => {}
    }
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

/// Wheel/page only after prepare_layout has a real viewport. scroll_down
/// with viewport_height=0 uses max_offset=total_height and walks off the end.
fn wheel_scroll(sb: &mut ScrollbackState, up: bool, rows: u16) {
    let (_, vp, _) = sb.scroll_info();
    if vp == 0 {
        return;
    }
    if up {
        sb.scroll_up(rows);
    } else {
        sb.scroll_down(rows);
    }
    clamp_scroll(sb);
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
) {
    let theme = Theme::current();
    let border = if focused {
        accent
    } else {
        theme.bg_base
    };
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

fn tick_scrollbacks(app: &mut App) -> bool {
    let mut need = app.story.tick() || app.calls.tick();
    for child in app.children.values_mut() {
        need |= child.story.tick() || child.calls.tick();
    }
    need
}

fn current_agent_state(app: &App) -> AgentState {
    if app.status == "stopping" {
        AgentState::TurnCancelling
    } else if app.running {
        AgentState::TurnRunning
    } else {
        AgentState::Idle
    }
}

fn current_watchers(app: &App) -> Watchers {
    let subagents = app
        .children
        .values()
        .filter(|c| c.stream.live() || c.exec.live())
        .count();
    Watchers {
        subagents,
        ..Watchers::default()
    }
}

fn current_turn_activity(app: &App) -> Option<TurnActivity> {
    if !app.running {
        return None;
    }
    if app.exec.live() {
        let title = exec_activity_title(app);
        return Some(TurnActivity::ToolRunning {
            title,
            description: None,
        });
    }
    if app.stream.speech.is_some() || !app.stream.speech_raw.is_empty() {
        return Some(TurnActivity::Responding);
    }
    if app.stream.think.is_some() || !app.stream.pending_think.is_empty() {
        return Some(TurnActivity::Thinking);
    }
    Some(TurnActivity::Waiting(WaitingReason::Model))
}

fn exec_activity_title(app: &App) -> String {
    if let Some(id) = app.exec.id {
        if let Some(entry) = app.calls.get_by_id(id) {
            if let RenderBlock::ToolCall(ToolCallBlock::Execute(b)) = &entry.block {
                let cmd = b.command.lines().next().unwrap_or("").trim();
                if !cmd.is_empty() {
                    return cmd.to_string();
                }
            }
        }
    }
    if app.exec.tag.is_empty() {
        "syscall".into()
    } else {
        format!("<{}>", app.exec.tag)
    }
}

fn streaming(app: &App) -> bool {
    app.stream.live()
        || app.exec.live()
        || app
            .children
            .values()
            .any(|c| c.stream.live() || c.exec.live())
}

fn flush_streams(app: &mut App) {
    app.stream.flush(&mut app.story);
    app.exec.flush(&mut app.calls);
    for child in app.children.values_mut() {
        child.stream.flush(&mut child.story);
        child.exec.flush(&mut child.calls);
    }
}

fn draw(f: &mut Frame, app: &mut App) {
    flush_streams(app);
    let theme = Theme::current();
    f.render_widget(
        Block::default().style(Style::default().bg(theme.bg_base).fg(theme.text_primary)),
        f.area(),
    );

    reflow_wire(&mut app.calls, &app.wire_manual);
    let manual = app.wire_manual.clone();
    for child in app.children.values_mut() {
        reflow_wire(&mut child.calls, &manual);
    }

    let agent_st = current_agent_state(app);
    let activity = current_turn_activity(app);
    if activity.as_ref() != app.last_activity.as_ref() {
        app.last_activity = activity.clone();
        app.activity_started_at = Some(Instant::now());
    }
    let watch = current_watchers(app);
    let show_turn = turn_status::should_show(&agent_st, false, None, watch, false);
    let turn_h = if show_turn { 1 } else { 0 };

    let inner_w = f.area().width.saturating_sub(2);
    // Roomy by default. An empty composer still opens three rows: this is where
    // a prompt gets drafted, not a one-line readline.
    let prompt_rows = app.prompt.display_rows(inner_w).clamp(3, 10);
    let queue_h = app.queue.display_height();
    let input_h = (3 + prompt_rows)
        .min(f.area().height.saturating_sub(10 + turn_h + queue_h))
        .max(6);
    // Columns first, and both run the full height. The composer belongs to the
    // story column -- it is where you type *about* the story -- and the wire
    // column spends the same band on a key legend, so the two columns end on
    // the same row. A full-width bar cutting across both is what this replaced.
    let body = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage(100 - app.layout.wire_pct),
            Constraint::Percentage(app.layout.wire_pct),
        ])
        .split(f.area());
    let bottom_h = queue_h + turn_h + input_h;
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
            Constraint::Length(turn_h),
            Constraint::Length(input_h),
        ])
        .split(body[0]);
    // The wire column stacks: calls, the meta pane, the two side panes — git
    // and the file it points at — then the key legend, which takes exactly the
    // band the composer takes opposite it.
    let spare = body[1].height.saturating_sub(bottom_h + 3);
    let meter_h = app.layout.meter_h.min(spare);
    let git_h = app.layout.git_h.min(spare.saturating_sub(meter_h));
    let files_h = app
        .layout
        .files_h
        .min(spare.saturating_sub(meter_h + git_h));
    let wire = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(3),
            Constraint::Length(meter_h),
            Constraint::Length(git_h),
            Constraint::Length(files_h),
            Constraint::Length(bottom_h),
        ])
        .split(body[1]);
    let panes = [left[0], wire[0]];
    app.cache.area = wire[1];
    app.git_area = wire[2];
    app.files_area = wire[3];
    app.keys_area = wire[4];
    let posts = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
        .split(left[1]);

    app.traj_area = panes[0];
    app.call_area = panes[1];
    app.post_in_area = posts[0];
    app.post_out_area = posts[1];
    app.queue_area = left[2];
    app.turn_status_area = left[3];
    app.input_area = left[4];

    let viewing = app.viewing.clone();
    let child_ok = viewing
        .as_deref()
        .is_some_and(|id| app.children.contains_key(id));
    if let (Some(id), true) = (viewing.as_deref(), child_ok) {
        let child = app.children.get_mut(id).expect("checked");
        let title = format!("session {id}");
        draw_scrollback(
            f,
            panes[0],
            &mut child.story,
            &mut child.story_scratch,
            &mut child.story_sel,
            &title,
            theme.accent_skill,
            app.focus == Focus::Story,
            app.mouse,
            &child.story_text,
        );
        draw_scrollback(
            f,
            panes[1],
            &mut child.calls,
            &mut child.calls_scratch,
            &mut child.calls_sel,
            "calls",
            theme.accent_tool,
            app.focus == Focus::Calls,
            app.mouse,
            &child.calls_text,
        );
    } else {
        draw_scrollback(
            f,
            panes[0],
            &mut app.story,
            &mut app.story_scratch,
            &mut app.story_sel,
            "story",
            theme.accent_assistant,
            app.focus == Focus::Story,
            app.mouse,
            &app.story_text,
        );
        draw_scrollback(
            f,
            panes[1],
            &mut app.calls,
            &mut app.calls_scratch,
            &mut app.calls_sel,
            "calls",
            theme.accent_tool,
            app.focus == Focus::Calls,
            app.mouse,
            &app.calls_text,
        );
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
    draw_meta(f, app.cache.area, &app.cache, app.focus == Focus::Meter);
    draw_git(f, app.git_area, app);
    draw_files(f, app.files_area, app);
    draw_queue(f, app.queue_area, app);
    if show_turn {
        let cancel_hovered = app.mouse.is_some_and(|(c, r)| {
            app.turn_cancel.is_some_and(|a| hit(a, c, r))
        });
        let out = turn_status::render_turn_status(
            f.buffer_mut(),
            app.turn_status_area,
            TurnStatusArgs {
                state: &agent_st,
                activity: &activity,
                turn_elapsed: app.turn_started.map(|t| t.elapsed()),
                activity_started_at: app.activity_started_at,
                tick: app.story.animation_tick(),
                drain_blocked: false,
                buttons: Some(MouseButtons {
                    cancel_hovered,
                    bg_hovered: false,
                    watching_hovered: false,
                }),
                has_running_execute: false,
                total_tokens: None,
                mcp_init_progress: None,
                is_bash_turn: false,
                is_pending_user_input: false,
                goal_verifying: false,
                watchers: watch,
                parked: false,
                flat_background: false,
                held_queue: app.queue.len(),
                held_queue_top_sendable: !app.queue.is_empty(),
            },
        );
        app.turn_cancel = out.cancel_button;
    } else {
        app.turn_cancel = None;
    }
    draw_keys(f, app.keys_area, app);
    draw_input(f, app.input_area, app);
    if app.post_inspect.is_some() {
        draw_post_inspect(f, app);
    }
    if app.viewer.is_some() {
        draw_viewer(f, app);
    }
    // Last, so it covers everything: on a fresh machine there is no session
    // behind it, and when reopened it is the only thing being interacted with.
    if app.picker.open {
        let area = f.area();
        let buf = f.buffer_mut();
        app.picker.render(buf, area);
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
            // Three rows is the whole meter now, so Full starts there. Dense
            // is the one-row-short case that has to drop the cost line.
            2..=2 => Self::Dense,
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
            let fallback = segments
                .first()
                .map(|(_, c)| *c)
                .unwrap_or(ink_on_track);
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
        let ink = if filled_here { ink_on_fill } else { ink_on_track };
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
                spans.push(Span::styled(std::mem::take(&mut run), Style::default().fg(prev)));
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
                let name = if row.is_dir && row.name != ".." {
                    format!("{}/", row.name)
                } else {
                    row.name.clone()
                };
                let style = if row.is_dir {
                    Style::default().fg(theme.accent_skill)
                } else {
                    Style::default().fg(theme.text_primary)
                };
                let body: String = name.chars().take(width).collect();
                let mut line = Line::from(Span::styled(body, style));
                if focused && i == app.files.sel {
                    line = line.style(Style::default().bg(theme.bg_highlight));
                }
                line
            })
            .collect()
    };
    f.render_widget(Paragraph::new(lines), inner);
}

fn draw_meta(f: &mut Frame, area: Rect, meter: &CacheMeter, focused: bool) {
    if area.height == 0 || area.width == 0 {
        return;
    }
    let theme = Theme::current();
    let left = meter.left();
    let secs = left.map(|l| (l * meter.ttl.as_secs_f32()).round() as u64);
    let ttl_label = if meter.ttl.as_secs() >= 3600 { "1h" } else { "5m" };
    let title = match secs {
        Some(s) => format!(" meta  cache {ttl_label} {}:{:02} ", s / 60, s % 60),
        None => " meta  cache cold ".to_string(),
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
            title,
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
    let rw = meter.read + meter.write;
    // Read against write on the last call: a write-heavy bar is the call that
    // paid to fill the cache rather than riding it. The track is always full --
    // this is a proportion, not a level.
    let cache_row = || {
        meter_row(
            inner.width,
            "cache",
            &format!("{}% cached", meter.hit()),
            &[
                (meter.read, theme.accent_success),
                (meter.write, theme.accent_tool),
            ],
            if rw == 0 { 0.0 } else { 1.0 },
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
            Span::styled(money(meter.saved), Style::default().fg(theme.accent_success)),
            label(" saved"),
        ])
    };

    // Degrade by which question matters most, not by what happens to fit. The
    // title already carries the TTL, so row one is context, not hit rate.
    let mut lines = match Tier::of(inner.height) {
        Tier::Line => vec![ctx_row()],
        Tier::Dense => vec![ctx_row(), cache_row(), money_row()],
        // The sparkline was the one row nobody read: a hit-rate trend restates
        // what the cache row already says, in less precise form.
        Tier::Full => vec![ctx_row(), cache_row(), money_row()],
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
    let title = format!(" queue  {} ", app.queue.len());
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
    f.render_widget(
        Paragraph::new(app.queue.lines(inner.width, focused)),
        inner,
    );
}

/// A key and the verb it fires, for the pane that has focus.
///
/// Two columns rather than one sentence: a cap and a verb are different kinds
/// of thing, and `j/k move   h/l fold   enter/ctrl-f open` makes the eye do
/// the separating. Caps use the glyph the keyboard prints where one exists,
/// so a modifier reads as a modifier instead of a hyphenated word.
fn key_rows(app: &App) -> Vec<(&'static str, String)> {
    let mut rows: Vec<(&'static str, String)> = Vec::new();
    match app.focus {
        Focus::Input => {
            let composer = !app.prompt.to_send().trim().is_empty();
            let send = if app.running && composer {
                Some("queues")
            } else if app.running && !app.queue.is_empty() {
                Some("send now")
            } else if app.running {
                None
            } else {
                Some("send")
            };
            if let Some(verb) = send {
                rows.push(("⏎", verb.into()));
            }
            rows.push(("⇧⏎", "newline".into()));
        }
        // Arrows read the same everywhere: ↑↓ moves the cursor in this pane,
        // ←→ drives whatever that pane's second axis is. j/k/h/l alias them.
        Focus::Story | Focus::Calls => {
            rows.push(("↑ ↓", "move".into()));
            rows.push(("← →", "fold".into()));
            rows.push(("⏎", "open".into()));
            rows.push(("r", "raw".into()));
            rows.push(("i", "input".into()));
        }
        // No cursor and nothing to fold, so the meter's arrows are the resize
        // ones and nothing else.
        Focus::Meter => {
            rows.push(("+ -", "rows".into()));
            rows.push(("^← →", "width".into()));
            rows.push(("i", "input".into()));
        }
        Focus::Git => {
            rows.push(("↑ ↓", "move".into()));
            rows.push(("← →", "tab".into()));
            rows.push(("⏎", "open file".into()));
            rows.push(("r", "refresh".into()));
            rows.push(("+ -", "rows".into()));
            rows.push(("i", "input".into()));
        }
        Focus::Files => {
            rows.push(("↑ ↓", if app.files.in_file() { "scroll" } else { "move" }.into()));
            rows.push((
                "← →",
                if app.files.in_file() {
                    "back to dir"
                } else {
                    "up / into"
                }
                .into(),
            ));
            rows.push(("␛", "git".into()));
            rows.push(("+ -", "rows".into()));
            rows.push(("i", "input".into()));
        }
        Focus::PostIn | Focus::PostOut => {
            rows.push(("↑ ↓", "move".into()));
            rows.push(("← →", "fold".into()));
            rows.push(("e", "inspect".into()));
            rows.push(("+ -", "rows".into()));
            rows.push(("^← →", "split".into()));
            rows.push(("i", "input".into()));
        }
        Focus::Queue => {
            rows.push(("↑ ↓", "select".into()));
            rows.push(("← →", "reorder".into()));
            rows.push(("d", "drop".into()));
            rows.push(("⏎", "send now".into()));
            rows.push(("i", "input".into()));
        }
    }
    rows.push(("⇥", "panes".into()));
    if app.focus != Focus::Input && !rows.iter().any(|(c, _)| *c == "+ -") {
        rows.push(("+ -", "resize".into()));
    }
    if app.focus != Focus::Input {
        rows.push(("0", "reset".into()));
    }
    if app.viewing.is_some() {
        rows.push(("␛", "parent".into()));
    }
    rows
}

/// The key legend, in the band the composer occupies in the other column.
///
/// It titles itself with the focused pane, because the rows change with focus
/// and a legend that silently re-writes itself is a legend you stop trusting.
fn draw_keys(f: &mut Frame, area: Rect, app: &App) {
    if area.height < 4 || area.width < 10 {
        return;
    }
    // Inset exactly like the composer opposite it, so the two cards float on
    // the same rows instead of one sitting a row proud of the other.
    let area = Rect {
        x: area.x.saturating_add(1),
        y: area.y.saturating_add(1),
        width: area.width.saturating_sub(2),
        height: area.height.saturating_sub(1),
    };
    let theme = Theme::current();
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(theme.bg_base))
        .title(Span::styled(
            format!(" keys  {} ", focus_name(app.focus)),
            Style::default().fg(theme.text_secondary),
        ))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    let inner = block.inner(area);
    f.render_widget(block, area);
    if inner.width == 0 || inner.height == 0 {
        return;
    }
    let rows = key_rows(app);
    let cap_w = rows
        .iter()
        .map(|(c, _)| UnicodeWidthStr::width(*c))
        .max()
        .unwrap_or(0);
    let lines: Vec<Line> = rows
        .iter()
        .take(inner.height as usize)
        .map(|(cap, verb)| {
            let pad = cap_w.saturating_sub(UnicodeWidthStr::width(*cap));
            Line::from(vec![
                Span::raw(" ".repeat(pad + 1)),
                Span::styled(
                    *cap,
                    Style::default()
                        .fg(theme.accent_user)
                        .add_modifier(Modifier::BOLD),
                ),
                Span::raw("  "),
                Span::styled(verb.clone(), Style::default().fg(theme.text_secondary)),
            ])
        })
        .collect();
    f.render_widget(Paragraph::new(lines), inner);
}

/// What to call the focused pane in the legend title.
fn focus_name(focus: Focus) -> &'static str {
    match focus {
        Focus::Input => "input",
        Focus::Story => "story",
        Focus::Calls => "calls",
        Focus::Meter => "meta",
        Focus::Git => "git",
        Focus::Files => "files",
        Focus::PostIn => "POST in",
        Focus::PostOut => "POST out",
        Focus::Queue => "queue",
    }
}

fn draw_input(f: &mut Frame, area: Rect, app: &mut App) {
    let theme = Theme::current();
    // Float the box: a blank row above it and a cell of gutter each side, so it
    // reads as a card laid over the story column rather than one more pane
    // stacked in the frame.
    let card = Rect {
        x: area.x.saturating_add(1),
        y: area.y.saturating_add(1),
        width: area.width.saturating_sub(2),
        height: area.height.saturating_sub(1),
    };
    // Identity lives on the box's own bottom edge now. It used to be a line of
    // running text above the box, competing with the shortcuts for one row and
    // losing the tail of both.
    let identity = app
        .viewing
        .as_deref()
        .map(|id| format!(" session {id} "))
        .unwrap_or_else(|| {
            format!(
                " {}  effort:{}  gen {} ",
                if app.model.is_empty() {
                    "—"
                } else {
                    app.model.as_str()
                },
                if app.thinking.is_empty() {
                    "—"
                } else {
                    app.thinking.as_str()
                },
                if app.generation.is_empty() {
                    "—"
                } else {
                    app.generation.as_str()
                },
            )
        });
    let prefix = " ❯ ";
    let focused = app.focus == Focus::Input;
    let border = if focused {
        theme.prompt_border_active
    } else {
        theme.prompt_border
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border))
        .title(Span::styled(
            if app.prompt.is_multiline() {
                " input  multiline "
            } else {
                " input "
            },
            Style::default().fg(theme.accent_success),
        ));
    let block = block
        .title_bottom(
            Line::from(Span::styled(
                identity,
                Style::default().fg(theme.text_secondary),
            ))
            .left_aligned(),
        )
        .title_bottom(
            Line::from(Span::styled(
                format!(" {} ", Theme::current_kind().display_name()),
                Style::default().fg(theme.text_secondary),
            ))
            .right_aligned(),
        )
        .style(Style::default().bg(theme.bg_base));
    let inner = block.inner(card);
    app.input_inner = inner;
    let lay = app.prompt.layout(prefix, inner.width);
    f.render_widget(block, card);
    if inner.width > 0 && inner.height > 0 {
        f.render_widget(
            Paragraph::new(lay.lines.clone()).style(Style::default().fg(theme.text_primary)),
            inner,
        );
    }
    if focused && inner.width > 0 && inner.height > 0 {
        let x = inner.x + lay.cursor_col.min(inner.width.saturating_sub(1));
        let y = inner.y + lay.cursor_row.min(inner.height.saturating_sub(1));
        f.buffer_mut()[(x, y)].modifier.insert(Modifier::REVERSED);
        f.set_cursor_position(Position { x, y });
    }
    if focused {
        if let Some(body) = app.prompt.preview_body() {
            draw_paste_preview(f, app.input_area, body, app.prompt.preview_on_chip());
        }
    }
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
        .title(Span::styled(
            " paste ",
            Style::default().fg(theme.paste_fg),
        ))
        .style(Style::default().bg(theme.paste_bg).fg(theme.paste_fg));
    let inner = block.inner(area);
    f.render_widget(block, area);
    let mut lines: Vec<Line> = shown
        .into_iter()
        .map(|l| Line::from(Span::styled(l.to_string(), Style::default().fg(theme.paste_fg))))
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

fn spoken_prefix(text: &str) -> String {
    let spans = code_spans(text);
    // A call whose closer has not arrived yet is a syscall in flight. Hold
    // everything from its `<` back: the alternative is that the body streams
    // into the story as prose, and it stays there, because by the time the
    // closer lands the chunk has already been appended to a live block.
    //
    // The streaming path must strip bodies, not just markers: dropping the
    // markers alone is right for prose about markup and wrong for a command.
    let mut cut = text.len();
    let mut i = 0usize;
    while let Some(rel) = text[i..].find('<') {
        let start = i + rel;
        if in_code(&spans, start) || !looks_like_tag_start(&text[start..]) {
            i = start + 1;
            continue;
        }
        let Some(gt) = text[start..].find('>') else {
            cut = start; // opener still being typed
            break;
        };
        let open_end = start + gt + 1;
        let inner = &text[start + 1..open_end - 1];
        let name = inner
            .trim_end_matches('/')
            .split_whitespace()
            .next()
            .unwrap_or("");
        if name.is_empty() || inner.trim_end().ends_with('/') || name.starts_with('/') {
            i = open_end;
            continue;
        }
        match text[open_end..].find(&format!("</{name}>")) {
            Some(rel_end) => i = open_end + rel_end + name.len() + 3,
            None => {
                cut = start; // body still arriving
                break;
            }
        }
    }
    // A call that opens the turn leaves the prose behind it beginning with the
    // newlines that separated the two. Those survive as an empty first line,
    // and the timestamp overlay always lands on the first content line -- so
    // the stamp ends up alone on a blank row, one row above the sentence it
    // belongs to, and a one-line reply costs four rows instead of one.
    // Leading whitespace is never information here, and trimming it is stable
    // under streaming: once consumed it stays consumed, so the prefix check in
    // flush_speech still holds.
    strip_syscalls(&text[..cut]).trim_start().to_string()
}

/// A trailing `<` is only worth withholding if it could open a tag: `<`
/// followed by a letter or `/`. Without this, `if a < b` in streamed prose
/// stalls the render until some later `>` arrives.
fn looks_like_tag_start(rest: &str) -> bool {
    let mut it = rest.chars();
    it.next();
    match it.next() {
        Some('/') => it.next().is_some_and(|c| c.is_ascii_alphabetic()),
        Some(c) => c.is_ascii_alphabetic(),
        None => true,
    }
}

fn start_thinking(story: &mut ScrollbackState, stream: &mut StreamCursor) {
    if stream.think.is_some() {
        return;
    }
    let id = story.push_block(RenderBlock::thinking_streaming());
    story.set_last_running(true);
    // Grok's truncated mode marks the clipped head with a bare "…" row. That
    // marker only reads as a marker under the header, and the header is the
    // status row's job -- so a live thought renders its whole body (grok
    // minimal does the same in its live tail) and the pane's bottom edge does
    // the clipping. finish_think folds it back to one collapsed row.
    set_wire_mode(story, id, DisplayMode::Expanded);
    stream.think = Some(id);
}

fn finish_exec(calls: &mut ScrollbackState, exec: &mut ExecStream) {
    exec.flush(calls);
    if let Some(id) = exec.id.take() {
        calls.finish_running(id);
        // Do not fold here. reflow_wire owns fold state and keeps the tail
        // open, so collapsing on finish only produces a one-frame flash
        // before it is reopened.
    }
    exec.pending.clear();
    exec.tag.clear();
}

fn apply_result(calls: &mut ScrollbackState, exec: &mut ExecStream, ev: &Value) {
    let phase = ev.get("phase").and_then(Value::as_str).unwrap_or("done");
    match phase {
        "start" => {
            finish_exec(calls, exec);
            let id = wire_push(calls, result_block(ev));
            // Open while it streams so stdout is visible as it arrives.
            set_wire_mode(calls, id, DisplayMode::Expanded);
            calls.set_last_running(true);
            exec.id = Some(id);
            exec.tag = ev
                .get("tag")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
        }
        "delta" => {
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            if !text.is_empty() {
                exec.pending.push_str(text);
            }
        }
        _ => {
            exec.flush(calls);
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            let tag = ev.get("tag").and_then(Value::as_str).unwrap_or("?");
            if let Some(id) = exec.id.take() {
                if let Some(entry) = calls.get_by_id_mut(id) {
                    match &mut entry.block {
                        RenderBlock::ToolCall(ToolCallBlock::Execute(block)) => {
                            if block.output.as_ref().is_none_or(|s| s.is_empty())
                                && !text.is_empty()
                            {
                                block.output = Some(text.to_string());
                            }
                            if looks_failed(tag, text) {
                                block.set_error(Some(
                                    text.lines().next().unwrap_or("failed").to_string(),
                                ));
                            }
                            block.finish();
                        }
                        // Only python/bash are Execute cards. `edit`, `register`,
                        // `system`, `skill`, `evolve` and every tag grown with
                        // <register> render as Other, which has no streaming
                        // output slot — rebuild the card from the done event so
                        // the wire pane actually shows what the syscall returned.
                        other => *other = result_block(ev),
                    }
                }
                calls.finish_running(id);
                // Fold state is reflow_wire's job; a finished call that is
                // still recent stays readable instead of blinking shut.
                calls.mark_height_dirty(id);
            } else {
                wire_push(calls, result_block(ev));
            }
        }
    }
}

fn apply_thinking(
    story: &mut ScrollbackState,
    stream: &mut StreamCursor,
    redacted: bool,
    text: &str,
    delta: bool,
) {
    if redacted {
        stream.finish_speech(story);
        stream.finish_think(story);
        story.push_block(RenderBlock::thinking(
            "redacted thinking — opaque block, replayed on the next complete(), not speech.",
        ));
        return;
    }
    if delta {
        stream.finish_speech(story);
        if text.is_empty() {
            return;
        }
        start_thinking(story, stream);
        stream.pending_think.push_str(text);
        return;
    }
    stream.finish_speech(story);
    stream.finish_think(story);
    if !text.trim().is_empty() {
        story.push_block(RenderBlock::thinking(text));
    }
}

fn apply_speech(story: &mut ScrollbackState, stream: &mut StreamCursor, text: &str, delta: bool) {
    if delta {
        stream.finish_think(story);
        stream.speech_raw.push_str(text);
        return;
    }
    stream.finish_think(story);
    stream.finish_speech(story);
    // The story carries prose. A syscall goes to the calls pane, body and all.
    let spoken = strip_syscalls(text);
    if !spoken.trim().is_empty() {
        story.push_block(RenderBlock::agent_message(spoken));
    }
}

/// Byte ranges of `text` that are literal code: fenced blocks (fence lines
/// included) and inline backtick spans. XML stripping must leave these alone,
/// or `<div>` inside a fenced HTML sample silently vanishes from the story.
///
/// An unterminated fence runs to the end of `text`. That is the streaming
/// case and the whole point: while a code block is still open, everything
/// after the opening fence is code.
fn code_spans(text: &str) -> Vec<(usize, usize)> {
    fn run(s: &str, c: char) -> usize {
        s.chars().take_while(|&x| x == c).count()
    }

    let mut spans: Vec<(usize, usize)> = Vec::new();
    let mut fence: Option<(char, usize, usize)> = None;
    let mut off = 0usize;

    for line in text.split_inclusive('\n') {
        let trimmed = line.trim_start();
        let indent = line.len() - trimmed.len();
        let first = trimmed.chars().next();
        let mut fenced_line = false;

        match fence {
            Some((fc, flen, start)) => {
                fenced_line = true;
                let n = run(trimmed, fc);
                if first == Some(fc) && n >= flen && trimmed[n..].trim().is_empty() {
                    spans.push((start, off + line.len()));
                    fence = None;
                }
            }
            None => {
                if indent <= 3 && (first == Some('`') || first == Some('~')) {
                    let fc = first.unwrap();
                    let n = run(trimmed, fc);
                    if n >= 3 {
                        fence = Some((fc, n, off));
                        fenced_line = true;
                    }
                }
            }
        }

        if !fenced_line {
            inline_code_spans(line, off, &mut spans);
        }
        off += line.len();
    }

    if let Some((_, _, start)) = fence {
        spans.push((start, text.len()));
    }
    spans
}

/// Backtick-delimited inline spans on one line. An unclosed opener is treated
/// as code to the end of the line, so a half-streamed `` `<tag` `` is not eaten.
fn inline_code_spans(line: &str, base: usize, out: &mut Vec<(usize, usize)>) {
    let b = line.as_bytes();
    let mut i = 0usize;
    while i < b.len() {
        if b[i] != b'`' {
            i += 1;
            continue;
        }
        let mut n = 0usize;
        while i + n < b.len() && b[i + n] == b'`' {
            n += 1;
        }
        let start = i;
        let mut j = i + n;
        let mut close = None;
        while j < b.len() {
            if b[j] == b'`' {
                let mut m = 0usize;
                while j + m < b.len() && b[j + m] == b'`' {
                    m += 1;
                }
                if m == n {
                    close = Some(j + m);
                    break;
                }
                j += m;
            } else {
                j += 1;
            }
        }
        let end = close.unwrap_or(b.len());
        out.push((base + start, base + end));
        i = end;
    }
}

fn in_code(spans: &[(usize, usize)], i: usize) -> bool {
    spans.iter().any(|&(a, z)| i >= a && i < z)
}

/// Strip whole syscalls from prose -- markers *and* bodies.
///
/// Deleting the markers alone is not enough: it leaves the command behind as
/// if someone had said it. The command is not prose; it belongs to the calls
/// pane, which already renders it as a card.
///
/// Structure decides, not a list of names: an opener with a matching closer is
/// a syscall whatever it is called, which means a tag registered later needs no
/// change here. A bare mention with no closer is left alone, so naming a tool
/// mid-sentence still reads.
fn strip_syscalls(text: &str) -> String {
    let spans = code_spans(text);
    let mut out = String::new();
    let mut i = 0usize;
    while i < text.len() {
        let Some(rel) = text[i..].find('<') else { break };
        let start = i + rel;
        out.push_str(&text[i..start]);
        if in_code(&spans, start) {
            out.push('<');
            i = start + 1;
            continue;
        }
        let Some(gt) = text[start..].find('>') else {
            out.push_str(&text[start..]);
            return out;
        };
        let open_end = start + gt + 1;
        let inner = &text[start + 1..open_end - 1];
        // Name runs to the first space, and a self-closing marker has no body.
        let name = inner
            .trim_end_matches('/')
            .split_whitespace()
            .next()
            .unwrap_or("");
        let selfclose = inner.trim_end().ends_with('/');
        if name.is_empty() || selfclose || name.starts_with('/') {
            i = open_end;
            continue;
        }
        // A matching closer makes this a call: drop the body with it.
        let close = format!("</{name}>");
        match text[open_end..].find(&close) {
            Some(rel_end) => i = open_end + rel_end + close.len(),
            None => i = open_end,
        }
    }
    out.push_str(&text[i..]);
    out
}

/// Push a wire card Collapsed. It does not stay that way: `reflow_wire` runs
/// every frame and reopens the tail, so a fresh card is Expanded by the time
/// it is painted. Starting folded keeps grok's Other/Read/Edit defaults from
/// flashing their full payload for one frame before the reconcile.
///
/// `l` / Enter opens a card, `h` folds it; either marks it manual and
/// `reflow_wire` stops managing it.
fn wire_push(sb: &mut ScrollbackState, block: RenderBlock) -> EntryId {
    let eid = sb.push_block(block);
    set_wire_mode(sb, eid, DisplayMode::Collapsed);
    eid
}

/// How many trailing wire cards stay open. The tail is where the reader is
/// looking; older cards fold back to a header + preview.
const WIRE_OPEN: usize = 3;

/// Keep the tail of the wire pane open: the last `WIRE_OPEN` cards, plus any
/// card still running, are Expanded; everything above them collapses. Cards
/// in `manual` were folded or opened by hand and are never touched.
///
/// Runs every frame. It is a fold-state reconcile, not an event handler, so a
/// card that arrives while another is streaming still ends up in the right
/// state without every push site remembering to call it.
fn reflow_wire(sb: &mut ScrollbackState, manual: &HashSet<EntryId>) {
    let n = sb.len();
    let mut rows: Vec<(EntryId, bool)> = Vec::with_capacity(n);
    for i in 0..n {
        if let Some(e) = sb.entry(i) {
            rows.push((e.id, e.is_running));
        }
    }
    let cut = rows.len().saturating_sub(WIRE_OPEN);
    for (idx, (id, running)) in rows.into_iter().enumerate() {
        if manual.contains(&id) {
            continue;
        }
        let want = if running || idx >= cut {
            DisplayMode::Expanded
        } else {
            DisplayMode::Collapsed
        };
        set_wire_mode(sb, id, want);
    }
}

/// Record that the reader took manual control of the selected wire card.
fn pin_selected_wire(app: &mut App) {
    let Some(i) = app.calls.selected() else {
        return;
    };
    if let Some(e) = app.calls.entry(i) {
        let id = e.id;
        app.wire_manual.insert(id);
    }
}

fn set_wire_mode(sb: &mut ScrollbackState, id: EntryId, mode: DisplayMode) {
    if let Some(entry) = sb.get_by_id_mut(id) {
        if entry.display_mode == mode {
            return;
        }
        entry.set_display_mode(mode);
        entry.display_mode_pinned = true;
    }
    sb.mark_height_dirty(id);
}

fn result_block(ev: &Value) -> RenderBlock {
    let tag = ev.get("tag").and_then(Value::as_str).unwrap_or("?");
    let body = ev.get("body").and_then(Value::as_str).unwrap_or("");
    let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
    let empty = json!({});
    let attrs = ev.get("attrs").unwrap_or(&empty);
    wire_syscall(tag, body, attrs, text)
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

/// Wire card for a server-side fold. The model's memory just got rewritten,
/// which is the largest thing the harness does to itself in a run — without a
/// card the only symptom is the context bar dropping for no stated reason.
fn wire_compacted(n: u64, kept: u64, summary: &str) -> RenderBlock {
    let head = if kept > 0 {
        format!("FOLD:  POST #{n}  {kept} kept")
    } else {
        format!("FOLD:  POST #{n}")
    };
    let body = if summary.trim().is_empty() {
        "earlier turns folded by the server".to_string()
    } else {
        summary.to_string()
    };
    RenderBlock::ToolCall(ToolCallBlock::Other(
        OtherToolCallBlock::new(head, "context compacted".to_string()).with_output(body),
    ))
}

/// Wire card for one XML syscall: the body that ran, then the result.
fn wire_syscall(tag: &str, body: &str, attrs: &Value, result: &str) -> RenderBlock {
    match tag {
        "python" | "bash" => {
            let cmd = if body.trim().is_empty() {
                syscall_label(tag, attrs)
            } else {
                body.to_string()
            };
            // Folded rows only show the description, so carry the command
            // there — a bare `<bash>` is not a preview of anything.
            let preview = first_line(&cmd);
            let desc = if preview.is_empty() {
                tag.to_string()
            } else {
                format!("{tag}  {preview}")
            };
            let mut block = ExecuteToolCallBlock::new(cmd)
                .with_description(desc)
                .with_output(result);
            if looks_failed(tag, result) {
                block = block.with_error(
                    result
                        .lines()
                        .next()
                        .unwrap_or("failed")
                        .to_string(),
                );
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
            let start = edit_start_line(&path, &old, &new);
            let hunks = diff_hunks_from_strings(&old, &new, start);
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
            let summary = {
                let attrs_s = attr_summary(attrs);
                if !body.trim().is_empty() {
                    first_line(body)
                } else if !attrs_s.is_empty() {
                    attrs_s
                } else {
                    first_line(result)
                }
            };
            let payload = match (body.trim().is_empty(), result.trim().is_empty()) {
                (true, _) => result.to_string(),
                (_, true) => body.to_string(),
                _ => format!("{body}\n\n→ {result}"),
            };
            let target = attr_summary(attrs);
            let head = if target.is_empty() {
                format!("{tag}: {summary}")
            } else {
                format!("{tag}: {target}")
            };
            let sub = if target.is_empty() {
                String::new()
            } else {
                summary
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
        if seen { after.push(line) } else { before.push(line) }
    }
    if !seen {
        return (String::new(), body.to_string());
    }
    (before.join("\n"), after.join("\n"))
}

/// 1-based line where the edit lands, so the diff gutter shows real file line
/// numbers instead of counting from 1.
///
/// The card is built both before the write (file still holds `old`) and after
/// it (file holds `new`), so try both. An unreadable path or a match that is
/// not unique falls back to 1 — a wrong offset is worse than an honest one.
fn edit_start_line(path: &str, old: &str, new: &str) -> usize {
    if path.is_empty() {
        return 1;
    }
    let Ok(text) = std::fs::read_to_string(path) else {
        return 1;
    };
    for probe in [old, new] {
        if probe.is_empty() {
            continue;
        }
        if let Some(byte) = text.find(probe)
            && text.match_indices(probe).count() == 1
        {
            return text[..byte].matches('\n').count() + 1;
        }
    }
    1
}

fn syscall_label(tag: &str, attrs: &Value) -> String {
    let extra = attr_summary(attrs);
    if extra.is_empty() {
        format!("<{tag}/>")
    } else {
        format!("<{tag} {extra}>")
    }
}

fn attr_summary(attrs: &Value) -> String {
    match attrs {
        Value::Object(map) if !map.is_empty() => map
            .iter()
            .filter_map(|(k, v)| v.as_str().map(|s| format!("{k}=\"{s}\"")))
            .collect::<Vec<_>>()
            .join(" "),
        _ => String::new(),
    }
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
        || (tag == "bash" && t.starts_with("exit "))
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
    format!(
        "hit {hit:>3}%  in {fresh:>4}+{read:>6}  out {out:>5}  think {thoughts}/{redacted}"
    )
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
        assert!(app.cache.plan, "the bridge said plan, the meter must believe it");
        // and list price for a gpt model is not opus price
        assert_eq!(model_price("gpt-5.6-luna"), (1.25, 10.0));
        assert_eq!(model_window("gpt-5.6-luna"), 400_000);

        app.cache
            .observe(&serde_json::json!({"input_tokens": 1_000_000, "output_tokens": 0}), "gpt-5.6-luna");
        assert!((app.cache.spent - 1.25).abs() < 1e-9, "{}", app.cache.spent);

        let painted = paint(&mut app, 120, 36);
        assert!(painted.contains("plan"), "plan sessions say plan:\n{painted}");
        assert!(painted.contains("at list"), "and label the figure as list price:\n{painted}");
        assert!(!painted.contains("spent"), "never a bill on a subscription:\n{painted}");

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
        assert!(rows.contains("enter to sign in"), "unauthed provider must offer login:\n{rows}");

        // an unauthed provider cannot be chosen: enter starts a login instead
        app.picker.sel = 1;
        assert_eq!(
            app.picker.key(KeyCode::Enter),
            picker::PickerAction::Login { provider: "openai".into() }
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
            picker::PickerAction::Apply { model: "gpt-5.6-luna".into(), effort: "xhigh".into() }
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
        assert!(!app.picker.open, "a configured session boots straight into the chat");
        // ...and reopening points at what is already in use
        app.picker.open_for_change();
        assert_eq!(app.picker.current_provider().unwrap().name, "openai");
        assert_eq!(app.picker.effort_idx, 1);
        assert_eq!(app.picker.key(KeyCode::Esc), picker::PickerAction::Close);
        assert!(!app.picker.open);
    }

    use super::*;
    use ratatui::backend::TestBackend;
    use xai_grok_pager::glyphs;

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

    fn paint(app: &mut App, w: u16, h: u16) -> String {
        let backend = TestBackend::new(w, h);
        let mut term = Terminal::new(backend).unwrap();
        term.draw(|f| draw(f, app)).unwrap();
        buffer_text(&term)
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
            handle_event(&mut app, json!({"ev": "speech", "delta": true,
                "text": format!("<bash>ls</bash>\n\nWaiting on {i}.")}));
            handle_event(&mut app, json!({"ev": "result", "tag": "bash", "text": "ok"}));
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
    /// the clipped head. The turn-status row already says Thinking with a
    /// spinner and the elapsed time, so the header was a copy and its blank was
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
            handle_event(&mut app, json!({
                "ev": "thinking", "delta": true,
                "text": format!("reasoning line number {i} with enough words to fill a row\n"),
            }));
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
        // Truncated mode could never show more than three body rows.
        let rows = text.lines().filter(|l| l.contains("number")).count();
        assert!(
            rows > 3,
            "a live thought renders its body, not a 3-row window ({rows}):\n{text}"
        );

        // One row, labelled, the moment it stops.
        handle_event(&mut app, json!({"ev": "speech", "delta": true, "text": "Answer.\n"}));
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

    fn call(tag: &str, target: Option<&str>) -> Seg {
        Seg::Call {
            tag: tag.into(),
            target: target.map(Into::into),
        }
    }

    /// The sentence is the whole point: it has to read like one.
    #[test]
    fn a_run_reads_as_one_sentence() {
        let segs = vec![
            Seg::Thought(12_000),
            call("edit", Some("main.rs")),
            call("python", None),
            call("python", None),
            call("bash", Some("cargo")),
            Seg::Thought(8_400),
            call("bash", Some("cargo")),
        ];
        assert_eq!(
            work_sentence(&segs),
            "thought 12s \u{2192} edit main.rs, python \u{00d7}2, bash cargo \u{2192} thought 8.4s \u{2192} bash cargo"
        );
    }

    #[test]
    fn a_long_run_elides_its_middle_instead_of_wrapping() {
        let mut segs = Vec::new();
        for i in 0..8 {
            segs.push(Seg::Thought(1_000 * (i + 1)));
            segs.push(call("bash", Some("git")));
        }
        let line = work_sentence(&segs);
        assert!(line.contains('\u{2026}'), "long run must elide: {line}");
        assert!(line.len() < 90, "still too long: {line}");
    }

    #[test]
    fn durations_read_as_durations() {
        assert_eq!(human_secs(900), "0.9s");
        assert_eq!(human_secs(9_400), "9.4s");
        assert_eq!(human_secs(12_000), "12s");
        assert_eq!(human_secs(95_000), "1m35s");
    }

    /// A shell command contributes its program and nothing else. This is the
    /// rule that keeps the row from becoming a second calls pane.
    #[test]
    fn a_shell_call_contributes_its_program_not_its_command() {
        let ev = json!({
            "tag": "bash",
            "body": "cd /Users/zeus/hub/desmos && cargo test --workspace 2>&1 | grep FAIL",
        });
        assert_eq!(call_target("bash", &ev).as_deref(), Some("cargo"));
        let edit = json!({"tag": "edit", "attrs": {"path": "crates/desmos-tui/src/main.rs"}});
        assert_eq!(call_target("edit", &edit).as_deref(), Some("main.rs"));
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
            (0..app.story.len()).find_map(|i| match app.story.entry(i).map(|e| &e.block) {
                Some(RenderBlock::System(b)) => Some(b.text.clone()),
                _ => None,
            })
        };
        handle_event(&mut app, json!({"ev": "thinking", "delta": true, "text": "planning\n"}));
        handle_event(&mut app, json!({"ev": "result", "tag": "bash", "body": "cargo build", "text": "ok"}));
        assert!(row(&app).is_none(), "one call is not a run: {:?}", row(&app));

        handle_event(&mut app, json!({"ev": "thinking", "delta": true, "text": "reading\n"}));
        handle_event(&mut app, json!({"ev": "result", "tag": "bash", "body": "cargo test", "text": "ok"}));
        let mid = row(&app).expect("the row must exist mid-run, before any speech");
        assert!(mid.contains("bash"), "mid-run row says nothing about the work: {mid}");

        // A third call rewrites the same row rather than stacking a second.
        handle_event(&mut app, json!({"ev": "result", "tag": "read", "body": "main.rs", "text": "ok"}));
        let rows = (0..app.story.len())
            .filter(|i| matches!(app.story.entry(*i).map(|e| &e.block), Some(RenderBlock::System(_))))
            .count();
        assert_eq!(rows, 1, "the run must own exactly one row");
        let grown = row(&app).unwrap();
        assert!(grown.contains("read"), "the row did not grow with the run: {grown}");
        assert_ne!(mid, grown, "the row must be rewritten, not frozen at its first shape");
    }

    #[test]
    fn invisible_work_folds_into_one_row_above_the_prose() {
        let mut app = App::new();
        handle_event(&mut app, json!({"ev": "turn", "text": "go"}));
        handle_event(
            &mut app,
            json!({"ev": "thinking", "delta": true, "text": "planning the change\n"}),
        );
        for (tag, body) in [("bash", "cd /tmp && cargo build"), ("bash", "cd /tmp && cargo test")] {
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

        let kinds: Vec<&str> = (0..app.story.len())
            .filter_map(|i| app.story.entry(i).map(|e| match &e.block {
                RenderBlock::Thinking(_) => "Thinking",
                RenderBlock::System(_) => "System",
                RenderBlock::AgentMessage(_) => "AgentMessage",
                _ => "other",
            }))
            .collect();
        assert_eq!(
            kinds,
            vec!["System", "AgentMessage"],
            "the run should be one row, then the prose: {kinds:?}"
        );
        let row = (0..app.story.len())
            .find_map(|i| match app.story.entry(i).map(|e| &e.block) {
                Some(RenderBlock::System(b)) => Some(b.text.clone()),
                _ => None,
            })
            .expect("work row");
        assert!(row.contains("bash \u{00d7}2"), "calls not compressed: {row}");
        assert!(row.contains("thought"), "thinking not folded in: {row}");
        assert!(!row.contains("cargo build"), "a command body leaked: {row}");
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
        handle_event(&mut app, json!({"ev": "speech", "delta": true, "text": "Found it."}));
        let _ = paint(&mut app, 120, 34);
        let thoughts = (0..app.story.len())
            .filter(|i| matches!(
                app.story.entry(*i).map(|e| &e.block),
                Some(RenderBlock::Thinking(_))
            ))
            .count();
        assert_eq!(thoughts, 1, "a lone call must not fold the thought away");
    }

    /// The composer belongs to the story column, and the legend takes the same
    /// band opposite it. Asserted on the rects the layout hands out, because a
    /// full-width bar and a column-width card paint the same words -- only the
    /// geometry tells them apart.
    #[test]
    fn the_composer_sits_under_the_story_column_only() {
        let mut app = App::new();
        let text = paint(&mut app, 140, 34);
        assert_eq!(app.input_area.x, app.traj_area.x, "composer left edge");
        assert_eq!(app.input_area.width, app.traj_area.width, "composer width");
        assert!(
            app.input_area.width + 20 < 140,
            "composer still spans the frame: {:?}",
            app.input_area
        );
        assert_eq!(app.keys_area.x, app.call_area.x, "legend sits in the wire column");
        assert_eq!(
            app.keys_area.y + app.keys_area.height,
            app.input_area.y + app.input_area.height,
            "columns must end on the same row"
        );
        assert!(app.keys_area.height >= 5, "legend band too thin: {:?}", app.keys_area);
        // The bar this replaced ran the full width and lost its tail.
        assert!(
            !text.contains("shift-enter newline"),
            "the old chrome sentence is still being painted:\n{text}"
        );
        assert!(text.contains("keys"), "legend pane missing:\n{text}");
    }

    /// Code spans are protected: markup inside a fence or backticks is the
    /// reader's subject matter, not a call. This was covered against a stripper
    /// that no longer exists, so it is re-pinned against the one that runs.
    #[test]
    fn markup_inside_code_is_not_a_call() {
        let fenced = "see\n```html\n<div class=\"x\">hi</div>\n```\ndone";
        let got = strip_syscalls(fenced);
        assert!(got.contains("<div class=\"x\">"), "fenced opener stripped: {got}");
        assert!(got.contains("</div>"), "fenced closer stripped: {got}");
        let inline = "use `<python>` not <python>x</python>";
        assert_eq!(strip_syscalls(inline), "use `<python>` not ");
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
            let story: String = (0..app.story.len())
                .filter_map(|i| match app.story.entry(i).map(|e| &e.block) {
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
        let story: String = (0..app.story.len())
            .filter_map(|i| match app.story.entry(i).map(|e| &e.block) {
                Some(RenderBlock::AgentMessage(m)) => Some(m.text()),
                _ => None,
            })
            .collect();
        assert!(story.contains("Checking the repo."), "prose lost: {story:?}");
        assert!(story.contains("Done."), "prose after the call lost: {story:?}");
        assert!(!story.contains("bash"), "tag name leaked: {story:?}");
        // And still absent once the closer has landed: holding the body during
        // the stream is worthless if the finished block prints it anyway.
        for leak in ["cd /tmp", "git rev-parse", "wc -l", "HEAD="] {
            assert!(!story.contains(leak), "final story leaked {leak:?}: {story:?}");
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
            (format!("e\n{lt}python{gt}x=1{lt}/python{gt} tail"), "e\n tail"),
            (format!("f {lt}bash{gt}inline cmd{lt}/bash{gt} g"), "f  g"),
        ] {
            assert_eq!(strip_syscalls(&src), want, "leaked from {src:?}");
        }
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
        let calls_x = (110 * (100 - app.layout.wire_pct) / 100) as u16;
        let calls_corner = buf.cell((calls_x, 0)).unwrap().style().fg.unwrap();
        assert_ne!(
            story_corner, theme.bg_base,
            "the focused pane must show its frame"
        );
        assert_eq!(
            calls_corner, theme.bg_base,
            "an unfocused frame must vanish into the background, got {calls_corner:?}"
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
        let text = paint(&mut app, 100, 24);
        assert!(text.contains("Checking the meter"), "prose missing:\n{text}");
        assert!(text.contains("All green"), "prose missing:\n{text}");
        assert!(
            !text.contains("cargo test --workspace"),
            "the command leaked into the story:\n{text}"
        );
    }

    #[test]
    fn a_syscall_leaves_nothing_behind() {
        let one = format!("{}bash{}cd /tmp && cargo test{}bash{}", '<', '>', "</", '>');
        assert_eq!(strip_syscalls(&one).trim(), "", "body survived: {:?}", strip_syscalls(&one));
    }

    #[test]
    fn prose_around_a_multiline_call_survives() {
        let src = format!(
            "before\n{}edit path=\"f\"{}a\n---\nb{}edit{}\nafter",
            '<', '>', "</", '>'
        );
        let got = strip_syscalls(&src);
        assert!(got.contains("before"), "{got:?}");
        assert!(got.contains("after"), "{got:?}");
        assert!(!got.contains("---"), "body survived: {got:?}");
        assert!(!got.contains("path="), "attrs survived: {got:?}");
    }

    #[test]
    fn naming_a_tool_in_a_sentence_still_reads() {
        // No closer, so nothing to drop: the sentence keeps its shape.
        let src = format!("use {}python{} for the kernel", '<', '>');
        let got = strip_syscalls(&src);
        assert!(got.contains("use "), "{got:?}");
        assert!(got.contains("for the kernel"), "{got:?}");
        // And a backticked mention is untouched, code spans being sacred.
        let fenced = format!("use `{}python{}` not raw", '<', '>');
        assert!(strip_syscalls(&fenced).contains("python"), "{:?}", strip_syscalls(&fenced));
    }

    #[test]
    fn two_calls_in_one_turn_both_go() {
        let src = format!(
            "one{}bash{}ls{}bash{} two {}python{}x=1{}python{} three",
            '<', '>', "</", '>', '<', '>', "</", '>'
        );
        let got = strip_syscalls(&src);
        assert!(got.contains("one"), "{got:?}");
        assert!(got.contains("two"), "{got:?}");
        assert!(got.contains("three"), "{got:?}");
        assert!(!got.contains("ls"), "{got:?}");
        assert!(!got.contains("x=1"), "{got:?}");
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
        assert_eq!(m.chunks.len(), 5, "one chunk per message or block: {:?}", m.chunks);
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
        assert_eq!(m.chunks.len(), 5, "one chunk per Responses item: {:?}", m.chunks);
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
        assert!(!app.cache.chunks.is_empty(), "event path recorded no chunks");
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
                let painted: usize = line
                    .spans
                    .iter()
                    .map(|s| s.content.chars().count())
                    .sum();
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
        assert!(!text.contains("context"), "label should have yielded: {text:?}");
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
        handle_event(&mut app, json!({"ev":"post","n":6,"request":{"seq":"REQSIX"}}));
        assert_eq!(app.post_n, 6, "in pane must advance at once");
        assert_eq!(app.post_out_n, 5, "held reply keeps its own number");
        let text = paint(&mut app, 150, 34);
        assert!(text.contains("POST in #6"), "in title wrong:\n{text}");
        assert!(text.contains("POST out #5"), "out title must still say 5:\n{text}");
        assert!(text.contains("RESPFIVE"), "held reply body was cleared:\n{text}");
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
        let id = app.calls.entry(0).map(|e| e.id).expect("card pushed");
        assert_eq!(
            app.calls.get_by_id(id).map(|e| e.display_mode),
            Some(DisplayMode::Expanded),
            "a running call should be open"
        );
        handle_event(
            &mut app,
            json!({"ev":"result","phase":"done","tag":"bash","attrs":{},"body":"echo hi","text":"hi"}),
        );
        // Checked before the next paint: nothing may fold it in between.
        assert_ne!(
            app.calls.get_by_id(id).map(|e| e.display_mode),
            Some(DisplayMode::Collapsed),
            "completed call folded before the next frame -- that is the flash"
        );
        let text = paint(&mut app, 120, 30);
        assert_eq!(
            app.calls.get_by_id(id).map(|e| e.display_mode),
            Some(DisplayMode::Expanded),
            "recent completed call should stay open"
        );
        assert!(text.contains("hi"), "output not visible:\n{text}");
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
            }),
        );
        let text = paint(&mut app, 130, 34);
        assert!(text.contains("notes.md"), "path missing:\n{text}");
        assert!(text.contains("UNIQUEOLD"), "removed side missing:\n{text}");
        assert!(text.contains("UNIQUENEW"), "added side missing:\n{text}");
    }

    #[test]
    fn a_non_unique_match_falls_back_to_line_one() {
        // desmos/loop.py holds MAX_TOKENS twice, so the offset cannot be
        // pinned and edit_start_line must say 1 rather than guess.
        assert_eq!(edit_start_line("does/not/exist.rs", "a", "b"), 1);
        assert_eq!(edit_start_line("", "a", "b"), 1);
    }



    #[test]
    fn edit_tag_becomes_a_real_diff_block() {
        let attrs = json!({"path": "notes.txt"});
        let block = wire_syscall("edit", "alpha\nbeta\n---\nalpha\nGAMMA\n", &attrs, "Edited notes.txt");
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
        assert!(tags.iter().any(|t| t == "Delete"), "no removed line: {tags:?}");
        assert!(tags.iter().any(|t| t == "Insert"), "no added line: {tags:?}");
        let texts: Vec<&str> = e
            .hunks
            .iter()
            .flat_map(|h| h.iter().map(|l| l.text.as_str()))
            .collect();
        assert!(texts.iter().any(|t| t.contains("beta")), "old text missing: {texts:?}");
        assert!(texts.iter().any(|t| t.contains("GAMMA")), "new text missing: {texts:?}");
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
        let block = wire_syscall("edit", "a\n---\nb", &attrs, "error: no such file");
        let RenderBlock::ToolCall(ToolCallBlock::Edit(e)) = block else {
            panic!("expected an edit block");
        };
        assert!(!e.is_success(), "a failing edit must not look successful");
    }

    fn wire_modes(app: &App) -> Vec<DisplayMode> {
        (0..app.calls.len())
            .filter_map(|i| app.calls.entry(i).map(|e| e.display_mode))
            .collect()
    }

    #[test]
    fn last_three_wire_cards_stay_open() {
        let mut app = App::new();
        for i in 0..7 {
            wire_push(&mut app.calls, RenderBlock::agent_message(format!("CARD{i}")));
        }
        let _ = paint(&mut app, 140, 40);
        let modes = wire_modes(&app);
        assert_eq!(modes.len(), 7);
        for (i, m) in modes.iter().enumerate() {
            if i >= 4 {
                assert_eq!(*m, DisplayMode::Expanded, "card {i} should be open: {modes:?}");
            } else {
                assert_eq!(*m, DisplayMode::Collapsed, "card {i} should be folded: {modes:?}");
            }
        }
    }

    #[test]
    fn a_running_card_stays_open_however_old() {
        let mut app = App::new();
        let old = wire_push(&mut app.calls, RenderBlock::agent_message("OLDRUNNER"));
        app.calls.set_last_running(true);
        for i in 0..6 {
            wire_push(&mut app.calls, RenderBlock::agent_message(format!("CARD{i}")));
        }
        let _ = paint(&mut app, 140, 40);
        let mode = app.calls.get_by_id(old).map(|e| e.display_mode);
        assert_eq!(mode, Some(DisplayMode::Expanded), "a running card must not fold");
    }

    #[test]
    fn a_hand_folded_card_is_left_alone() {
        let mut app = App::new();
        let mut last = None;
        for i in 0..3 {
            last = Some(wire_push(&mut app.calls, RenderBlock::agent_message(format!("CARD{i}"))));
        }
        let _ = paint(&mut app, 140, 40);
        let id = last.unwrap();
        app.wire_manual.insert(id);
        set_wire_mode(&mut app.calls, id, DisplayMode::Collapsed);
        let _ = paint(&mut app, 140, 40);
        let mode = app.calls.get_by_id(id).map(|e| e.display_mode);
        assert_eq!(mode, Some(DisplayMode::Collapsed), "reflow overrode a manual fold");
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
            .filter(|(_, l)| l.contains("calls") || l.contains("cache"))
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
        assert!(first.contains("more up"), "tail view hides rows above:\n{first}");
        assert!(
            !first.contains("more down"),
            "follow mode is pinned to the tail, nothing is below it:\n{first}"
        );
        app.story.scroll_up(20);
        let text = paint(&mut app, 120, 30);
        assert!(text.contains("more up"), "no up-overflow marker:\n{text}");
        assert!(text.contains("more down"), "no down-overflow marker:\n{text}");
    }

    #[test]
    fn a_pane_that_fits_shows_no_overflow_marker() {
        let mut app = App::new();
        app.story_push(RenderBlock::agent_message("just one short line"));
        let text = paint(&mut app, 120, 30);
        assert!(!text.contains("more up"), "spurious overflow marker:\n{text}");
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
        assert!(text.contains("story"), "{text}");
        assert!(text.contains("calls"), "{text}");
        assert!(
            !text.contains("out   ") && !text.lines().any(|l| l.trim_start().starts_with("out ")),
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
        app.story.goto_top();
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
        let (_, vp, _) = app.story.scroll_info();
        assert!(vp > 0, "layout never set a viewport");
        app.story.scroll_down(10_000);
        clamp_scroll(&mut app.story);
        term.draw(|f| draw(f, &mut app)).unwrap();
        let (off, vp, total) = app.story.scroll_info();
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
        wheel_scroll(&mut app.calls, false, 200);
        let (off, vp, total) = app.calls.scroll_info();
        let max = total.saturating_sub(vp as usize);
        assert_eq!(off, max.min(off));
        assert!(off <= max, "calls offset {off} > max {max}");
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
        assert_eq!(app.story.len(), 0, "queued follow-up must not hit story yet");
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
        assert_eq!(app.story.len(), 1);
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
        assert_eq!(app.story.len(), 1);
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
        assert!(text.contains("WIREANSWER"), "response body missing:\n{text}");
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
        assert_eq!((l.meter_h, l.post_h), (0, 0), "both panes must reach hidden");
    }

    #[test]
    fn tier_boundaries_and_the_default_layout() {
        assert_eq!(Tier::of(0), Tier::Line);
        assert_eq!(Tier::of(1), Tier::Line);
        assert_eq!(Tier::of(2), Tier::Dense);
        assert_eq!(Tier::of(3), Tier::Full);
        assert_eq!(Tier::of(12), Tier::Full);
        // The default hugs its content: three rows plus two border rows, and
        // nothing below the context bar going to waste.
        assert_eq!(PaneLayout::default().meter_h, 5);
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
            (5, "saved", "trend"),
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
        app.calls.goto_top();
        for _ in 0..app.calls.len() {
            app.calls.expand_selected();
            app.calls.select_next();
        }
        app.calls.goto_top();
    }

    /// A fold rewrites what the model remembers. It is the harness acting on
    /// itself, not something the model said, so it belongs on the wire.
    #[test]
    fn a_fold_lands_on_the_wire_and_never_in_the_story() {
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

        // `paint` renders both columns, so assert on the scrollback itself
        // rather than the frame text: the story took no entry at all.
        assert_eq!(app.story.len(), 0, "a fold is not speech");
    }

    #[test]
    fn calls_pane_says_who_posted_and_which_syscall() {
        let mut app = App::new();
        seed_demo(&mut app);
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
        let idx = app.calls.len().saturating_sub(1);
        let mode = app.calls.entry(idx).map(|e| e.display_mode());
        assert_eq!(mode, Some(DisplayMode::Expanded));
    }

    #[test]
    fn running_keeps_prompt_and_advances_wave_tick() {
        let mut app = App::new();
        app.running = true;
        app.turn_started = Some(Instant::now());
        app.story_push(RenderBlock::thinking_streaming());
        app.story.set_last_running(true);
        let t0 = app.story.animation_tick();
        assert!(app.story.tick(), "visible running entry must request redraw");
        assert!(app.story.animation_tick() > t0);
        app.set_focus(Focus::Input);
        let text = paint(&mut app, 120, 30);
        assert!(text.contains('❯'), "running must keep grok prompt:\n{text}");
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
            app.story.appearance().show_timestamps,
            appearance_cache::load_timestamps()
        );
        assert_eq!(
            app.calls.appearance().prompt.compact,
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
    fn stripping_keeps_markdown_structure() {
        let got = strip_syscalls("## cache\n\n**87%**\n<python>x</python>\nmore");
        assert!(got.contains("## cache"), "{got}");
        assert!(got.contains('\n'), "{got}");
        assert!(!got.contains("<python>"), "{got}");
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
            + 2 // floating gutter, then the box's own border
            + UnicodeWidthStr::width(" ❯ ") as u16
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
        assert_eq!(app.story.len(), 0);
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
        assert_eq!(app.story.len(), 0);
    }

    #[test]
    fn paste_multiline_does_not_submit() {
        let mut app = App::new();
        apply_paste(&mut app, "a\nb\nc", false);
        assert_eq!(app.prompt.to_send(), "a\nb\nc");
        assert_eq!(app.story.len(), 0);
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
        assert_eq!(app.story.len(), 0);
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

    fn first_subagent(app: &App) -> Option<usize> {
        (0..app.story.len()).find(|&i| {
            matches!(
                app.story.entry(i).map(|e| &e.block),
                Some(RenderBlock::Subagent(_))
            )
        })
    }

    #[test]
    fn spawn_started_stays_on_parent_child_gets_the_transcript() {
        let mut app = App::new();
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
                "secs": 1.5,
                "turns": 3,
                "result": "CHILDONLY last-user cache",
                "error": "",
            }),
        );

        let idx = first_subagent(&app).expect("started block");
        let entry = app.story.entry(idx).expect("entry");
        match &entry.block {
            RenderBlock::Subagent(sb) => {
                assert!(matches!(sb.kind, SubagentBlockKind::Started));
                assert_eq!(sb.child_session_id, "deadbeef");
                assert_eq!(sb.activity_label.as_deref(), Some("turn 3"));
            }
            other => panic!("expected Subagent, got {other:?}"),
        }
        assert!(
            (0..app.story.len()).any(|i| {
                matches!(
                    app.story.entry(i).map(|e| &e.block),
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
        assert_eq!(app.calls.len(), 0, "child wire must not hit parent calls");

        let child = app.children.get("deadbeef").expect("child session");
        assert!(child.story.len() >= 3, "task + thought + speech");
        assert_eq!(child.calls.len(), 2, "complete + syscall stay on child calls");
    }

    #[test]
    fn enter_opens_spawn_session_esc_returns() {
        let mut app = App::new();
        seed_demo(&mut app);
        app.set_focus(Focus::Story);
        let idx = first_subagent(&app).expect("demo spawn");
        app.story.set_selected(Some(idx));
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        );
        assert_eq!(app.viewing.as_deref(), Some("a1b2c3d4"));
        let inside = paint(&mut app, 120, 30);
        assert!(inside.contains("session a1b2c3d4"), "{inside}");
        assert!(
            inside.contains("last-user") || inside.contains("CHILDONLY") || inside.contains("cache"),
            "child speech missing inside session:\n{inside}"
        );
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE),
        );
        assert!(app.viewing.is_none());
        let back = paint(&mut app, 120, 30);
        assert!(back.contains("story"), "{back}");
        assert!(!back.contains("session a1b2c3d4"), "{back}");
    }

    #[test]
    fn ctrl_f_opens_spawn_session() {
        let mut app = App::new();
        seed_demo(&mut app);
        app.set_focus(Focus::Story);
        let idx = first_subagent(&app).expect("demo spawn");
        app.story.set_selected(Some(idx));
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
        assert!(text.contains("LIVEPROBE"), "POST in missing request:\n{text}");
        assert!(app.post_out.is_empty());
        assert_eq!(app.post_n, 1);
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
        let thinks: Vec<String> = (0..app.story.len())
            .filter_map(|i| match app.story.entry(i).map(|e| &e.block) {
                Some(RenderBlock::Thinking(t)) => Some(t.text()),
                _ => None,
            })
            .collect();
        assert_eq!(thinks, vec!["hello world".to_string()]);
        assert!(!app.stream.live());
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
        let spoken: Vec<String> = (0..app.story.len())
            .filter_map(|i| match app.story.entry(i).map(|e| &e.block) {
                Some(RenderBlock::AgentMessage(m)) => Some(m.text()),
                _ => None,
            })
            .collect();
        // The `1` is the call's body: it belongs to the calls pane, and it must
        // never appear in the story even for the one frame before it closes.
        assert_eq!(spoken, vec!["see  more".to_string()]);
        assert!(!spoken.iter().any(|s| s.contains("<python>") || s.contains('1')));
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
        let outs: Vec<String> = (0..app.calls.len())
            .filter_map(|i| match app.calls.entry(i).map(|e| &e.block) {
                Some(RenderBlock::ToolCall(ToolCallBlock::Execute(b))) => {
                    Some(b.output.clone().unwrap_or_default())
                }
                _ => None,
            })
            .collect();
        assert_eq!(outs, vec!["hi".to_string()]);
        assert!(app.exec.id.is_none());
    }

    #[test]
    fn spoken_prefix_holds_an_unclosed_tag() {
        assert_eq!(spoken_prefix("hello <python"), "hello ");
        // The body goes with the call. It is already a card in the calls pane.
        assert_eq!(spoken_prefix("hello <python>x</python>!"), "hello !");
        // Held while the closer is still in flight, not shown then retracted.
        assert_eq!(spoken_prefix("hello <bash>rm -rf /"), "hello ");
    }
    #[test]
    fn a_less_than_in_prose_does_not_stall_the_stream() {
        assert_eq!(
            spoken_prefix("loop while a < b and keep going"),
            "loop while a < b and keep going"
        );
    }

    #[test]
    fn open_fence_is_never_treated_as_markup() {
        let live = "here:\n```python\nif a < b:\n    print('<hi>')\n";
        assert_eq!(spoken_prefix(live), live);
    }

    #[test]
    fn code_spans_covers_open_fence_to_end() {
        let src = "a\n```\nb\n";
        let spans = code_spans(src);
        assert_eq!(spans.len(), 1, "{spans:?}");
        assert_eq!(spans[0].1, src.len(), "open fence must run to EOF");
    }


    fn first_speech(app: &App) -> Option<usize> {
        (0..app.story.len()).find(|&i| {
            matches!(
                app.story.entry(i).map(|e| &e.block),
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
        app.story.set_selected(Some(idx));
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        );
        assert!(app.viewer.is_some(), "Enter on speech must open BlockViewerPane");
        assert!(app.viewing.is_none(), "speech must not open a spawn session");
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
        app.story.set_selected(Some(idx));
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
        app.story.set_selected(Some(idx));
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
        assert!(text.contains("WIREPROBE"), "tree in popup missing request:\n{text}");
        assert!(text.contains("in") && text.contains("out"), "tabs missing:\n{text}");
        let _ = handle_key(
            None,
            &mut app,
            KeyEvent::new(KeyCode::Char(']'), KeyModifiers::NONE),
        );
        let out = paint(&mut app, 120, 36);
        assert!(out.contains("WIREANSWER"), "out tab missing response:\n{out}");
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
        app.story.set_selected(Some(0));
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
        assert!(app.story_text.pending.is_some());
        handle_mouse(
            &mut app,
            click(MouseEventKind::Drag(MouseButton::Left), col + 3, row),
        );
        assert!(app.story_text.active.is_some());
        handle_mouse(
            &mut app,
            click(MouseEventKind::Up(MouseButton::Left), col + 3, row),
        );
        assert!(app.story_text.persist.is_some());
        assert_eq!(app.status, "copied");
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
        app.story.goto_top();
        handle_key(None, &mut app, press(KeyCode::Down)).unwrap();
        let moved = app.story.selected();
        assert!(moved.is_some(), "↓ selects in the story");

        app.set_focus(Focus::Meter);
        handle_key(None, &mut app, press(KeyCode::Down)).unwrap();
        handle_key(None, &mut app, press(KeyCode::Up)).unwrap();
        assert_eq!(
            app.story.selected(),
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

    #[test]
    fn tab_skips_empty_queue() {
        let mut app = App::new();
        app.set_focus(Focus::PostOut);
        handle_key(None, &mut app, tab()).unwrap();
        assert_eq!(app.focus, Focus::Input, "empty queue must not take Tab");
        handle_key(None, &mut app, backtab()).unwrap();
        assert_eq!(app.focus, Focus::PostOut, "Shift-Tab must skip empty queue");
        app.queue.push("later".into());
        handle_key(None, &mut app, tab()).unwrap();
        assert_eq!(app.focus, Focus::Queue);
        handle_key(None, &mut app, tab()).unwrap();
        assert_eq!(app.focus, Focus::Input);
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
            before.contains("effort:—") && before.contains("gen —"),
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
        assert!(after.contains("claude-opus-5"), "{after}");
        assert!(after.contains("effort:low"), "{after}");
        assert!(after.contains("gen 7"), "{after}");
        assert!(!after.contains("effort:—"), "{after}");
        assert!(!after.contains("gen —"), "{after}");
    }

    #[test]
    fn running_composer_hints_enter_queues() {
        let mut app = App::new();
        app.ready = true;
        app.running = true;
        app.turn_started = Some(Instant::now());
        app.status = "running".into();
        app.prompt.insert_str("follow up later");
        let text = paint(&mut app, 160, 30);
        // The legend prints the cap and the verb in separate columns, so the
        // old sentence "enter queues" no longer exists as a substring.
        assert!(
            text.contains("queues"),
            "legend must say a typed follow-up queues:\n{text}"
        );
        assert!(text.contains('⏎'), "legend must show the enter cap:\n{text}");
    }

    #[test]
    fn turn_status_hosts_grok_widget() {
        let mut app = App::new();
        app.running = true;
        app.turn_started = Some(Instant::now());
        app.status = "running".into();
        start_thinking(&mut app.story, &mut app.stream);
        let text = paint(&mut app, 140, 30);
        assert!(
            text.contains("Thinking"),
            "grok turn-status must name Thinking:\n{text}"
        );
        let frames = glyphs::braille_spinner_frames();
        assert!(
            frames.iter().any(|f| text.contains(*f)),
            "turn-status must spin a grok braille frame:\n{text}"
        );
        assert!(text.contains("[stop]"), "turn-status [stop] missing:\n{text}");
        assert!(text.contains('❯'), "{text}");
    }

    #[test]
    fn click_stop_requests_cancel() {
        let mut app = App::new();
        app.running = true;
        app.turn_started = Some(Instant::now());
        app.status = "running".into();
        start_thinking(&mut app.story, &mut app.stream);
        let _ = paint(&mut app, 140, 30);
        let area = app.turn_cancel.expect("cancel hit area");
        handle_mouse(
            &mut app,
            click(
                MouseEventKind::Down(MouseButton::Left),
                area.x,
                area.y,
            ),
        );
        assert!(app.want_stop);
    }
}
