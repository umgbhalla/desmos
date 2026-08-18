//! The App: parent session state, child sessions, focus, and pane layout.
//! Moved verbatim out of main.rs.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::time::Instant;

use ratatui::layout::Rect;
use serde_json::{Value, json};
use xai_grok_pager::appearance::{self, cache as appearance_cache};
use xai_grok_pager::theme::ThemeKind;
use crate::input::Media;
use xai_grok_pager::scrollback::text_selection::{
    ActiveTextDrag, PendingTextDrag, PersistentTextSelection, RangeHit, ResolvedSelectionModel,
};
use xai_grok_pager::scrollback::{
    EntryId, RenderBlock, ScratchBuffer, ScrollbackState,
};
use xai_grok_pager::theme::cache as theme_cache;
use xai_grok_pager::views::block_viewer::BlockViewerPane;

use crate::json_tree::JsonTree;
use crate::prompt::PromptBuf;
use crate::queue::QueryQueue;
use crate::{
    CacheMeter, ExecStream, NOTICE_TTL, PostInspect, PostRows, StreamCursor, ViewerSrc,
    fuzzy, grok_appearance, initial_theme, picker, session, side, slash, viewer_for_entry,
    wire_push,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Focus {
    Rail,
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

impl Focus {
    /// Tab walks the frame clockwise, Shift-Tab anti-clockwise. The old order
    /// walked down the right column, jumped back to the middle of the left one,
    /// and then walked *down* that too -- half a ring in one direction and half
    /// in the other, so neither key had a direction you could point at.
    ///
    /// The frame is two columns. Left, top to bottom: Story, the POST split,
    /// Queue, Input. Right, top to bottom: Activity, Git, Files, Meta. So
    /// clockwise is: across the top, down the right edge, back across the
    /// bottom, up the left edge.
    pub(crate) fn next(self) -> Self {
        match self {
            // Across the top and down the right column.
            Self::Rail => Self::Story,
            Self::Story => Self::Calls,
            Self::Calls => Self::Git,
            Self::Git => Self::Files,
            Self::Files => Self::Meter,
            // Bottom-right to bottom-left, then back up the left column.
            Self::Meter => Self::Input,
            Self::Input => Self::Queue,
            Self::Queue => Self::PostOut,
            // The POST split is one row of two panes; going left inside it is
            // still going anti-clockwise around the ring.
            Self::PostOut => Self::PostIn,
            Self::PostIn => Self::Rail,
        }
    }

    pub(crate) fn prev(self) -> Self {
        match self {
            Self::Rail => Self::PostIn,
            Self::Story => Self::Rail,
            Self::PostIn => Self::PostOut,
            Self::PostOut => Self::Queue,
            Self::Queue => Self::Input,
            Self::Input => Self::Meter,
            Self::Meter => Self::Files,
            Self::Files => Self::Git,
            Self::Git => Self::Calls,
            Self::Calls => Self::Story,
        }
    }

    /// Tab cycle. A pane collapsed to zero rows is not a pane.
    pub(crate) fn next_open(self, open: &dyn Fn(Focus) -> bool) -> Self {
        let mut f = self.next();
        for _ in 0..9 {
            if open(f) {
                break;
            }
            f = f.next();
        }
        f
    }

    pub(crate) fn prev_open(self, open: &dyn Fn(Focus) -> bool) -> Self {
        let mut f = self.prev();
        for _ in 0..9 {
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
pub(crate) struct PaneLayout {
    /// Width of the wire column (calls + meter) as a percent of the top row.
    pub(crate) wire_pct: u16,
    /// Rows for the POST in/out split; 0 hides it.
    pub(crate) post_h: u16,
    /// Rows for the cache meter; 0 hides it.
    pub(crate) meter_h: u16,
    /// Width of POST in as a percent of the POST row; the rest is POST out.
    pub(crate) post_split: u16,
    /// Rows for the git pane; 0 keeps it closed, which is how it starts.
    pub(crate) git_h: u16,
    /// Rows for the file view under it; 0 keeps it closed.
    pub(crate) files_h: u16,
}

/// Which way a resize key pushes. `+`/`-` drive each pane's main axis;
/// ctrl+arrows drive whichever axis the arrow points along.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum Axis {
    Horizontal,
    Vertical,
}

impl Default for PaneLayout {
    fn default() -> Self {
        Self {
            wire_pct: 38,
            post_h: 12,
            // Five inner rows hold context, cache, cost, agent, and theme;
            // +2 for the border. Runtime activity lives on the composer.
            meter_h: 7,
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
    pub(crate) const MIN_WIRE: u16 = 15;
    pub(crate) const MAX_WIRE: u16 = 75;
    pub(crate) const MAX_POST: u16 = 28;
    pub(crate) const MAX_METER: u16 = 12;
    pub(crate) const MIN_SPLIT: u16 = 20;
    pub(crate) const MAX_SPLIT: u16 = 80;
    pub(crate) const MAX_SIDE: u16 = 30;
    /// What a closed side pane opens to — enough for a tab strip and a few rows.
    pub(crate) const OPEN_SIDE: u16 = 10;

    /// The axis `+` / `-` drives for a pane: the one it can actually give away
    /// space along. Story and calls share a width; meter and POST own rows.
    pub(crate) fn main_axis(focus: Focus) -> Axis {
        match focus {
            Focus::Story | Focus::Calls => Axis::Horizontal,
            _ => Axis::Vertical,
        }
    }

    pub(crate) fn grow(&mut self, focus: Focus, by: i16) {
        self.grow_axis(focus, Self::main_axis(focus), by);
    }

    pub(crate) fn grow_axis(&mut self, focus: Focus, axis: Axis, by: i16) {
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
            (Focus::Rail | Focus::Queue | Focus::Input, _) => {}
        }
    }

    pub(crate) fn path() -> Option<PathBuf> {
        // The pane tests paint an App::new() and assert which panes got rows,
        // so they have to run against the default layout. Without this, a
        // developer (or CI) whose cwd holds a saved .desmos/tui.json with
        // post_h/git_h/files_h at 0 fails ten of them: the panes the test
        // expects to be painted are the ones that human collapsed.
        if cfg!(test) {
            return None;
        }
        let cwd = std::env::current_dir().ok()?;
        Some(cwd.join(".desmos").join("tui.json"))
    }

    pub(crate) fn load() -> Self {
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

    pub(crate) fn save(&self) {
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

/// One session's panes and stream state — the parent's or a child's.
///
/// App used to carry these twelve fields itself and ChildSess carried the
/// same twelve again, with six near-identical resolvers each re-doing the
/// parent-or-child lookup. Four parent/child-bleed bugs traced back to that
/// duplication. One struct, and the lookup lives in exactly two places:
/// [`App::sess`] and [`App::sess_mut`].
pub(crate) struct Sess {
    pub(crate) story: ScrollbackState,
    pub(crate) calls: ScrollbackState,
    pub(crate) story_scratch: ScratchBuffer,
    pub(crate) calls_scratch: ScratchBuffer,
    pub(crate) story_sel: ResolvedSelectionModel,
    pub(crate) calls_sel: ResolvedSelectionModel,
    pub(crate) stream: StreamCursor,
    pub(crate) exec: ExecStream,
    pub(crate) story_text: TextSel,
    pub(crate) calls_text: TextSel,
    /// This session's POSTs. A child runs its own, so it needs its own
    /// index — sharing the parent's would step the cursor to entries that
    /// are not in this pane.
    pub(crate) posts: PostRows,
    /// Hand-folded cards in *this* pane. EntryIds are handed out per
    /// ScrollbackState from 1, so a set shared with the parent pinned card #1
    /// in every session at once.
    pub(crate) wire_manual: HashSet<EntryId>,
}

impl Sess {
    pub(crate) fn new() -> Self {
        Self {
            story: ScrollbackState::new(),
            calls: ScrollbackState::new(),
            story_scratch: ScratchBuffer::new(),
            calls_scratch: ScratchBuffer::new(),
            story_sel: ResolvedSelectionModel::default(),
            calls_sel: ResolvedSelectionModel::default(),
            stream: StreamCursor::default(),
            exec: ExecStream::default(),
            story_text: TextSel::default(),
            calls_text: TextSel::default(),
            posts: PostRows::default(),
            wire_manual: HashSet::new(),
        }
    }

    /// This session's story or wire scrollback, by the caller's pane bool.
    pub(crate) fn scroll(&mut self, calls: bool) -> &mut ScrollbackState {
        if calls { &mut self.calls } else { &mut self.story }
    }

    pub(crate) fn text(&mut self, calls: bool) -> &mut TextSel {
        if calls {
            &mut self.calls_text
        } else {
            &mut self.story_text
        }
    }

    pub(crate) fn sel(&self, calls: bool) -> &ResolvedSelectionModel {
        if calls { &self.calls_sel } else { &self.story_sel }
    }
}

/// Child spawn session — grok's per-child AgentView, split into desmos panes.
pub(crate) struct ChildSess {
    pub(crate) sess: Sess,
    pub(crate) parent_entry: Option<EntryId>,
    /// Tree coordinates off the wire (every `subagent`/`child` event carries
    /// them, Phase 3): the spawning run's id — `None` when the root world
    /// spawned this child — and its nesting depth (root spawns are 0). The
    /// tree view (`tree.rs`) nests rows by them.
    pub(crate) parent: Option<String>,
    pub(crate) depth: u64,
    /// Arrival order, so siblings paint in spawn order. In-memory, monotonic:
    /// children are never removed.
    pub(crate) seq: u64,
    /// The rest of the tree row, all off the `subagent` events: agent name
    /// (started), stage/turns/usage (progress + terminal), the terminal phase
    /// as `state` (`running` from started until then), the judge's verdict.
    pub(crate) agent: String,
    pub(crate) stage: String,
    pub(crate) turns: u64,
    pub(crate) tok_in: u64,
    pub(crate) tok_out: u64,
    pub(crate) state: String,
    pub(crate) accepted: Option<bool>,
    /// An intervention this TUI sent that the kernel has not answered yet.
    /// The row says so; the terminal `subagent` event is the confirmation.
    pub(crate) op_sent: Option<&'static str>,
}

/// Grok text selection for one scrollback (drag, persist, double-click word).
#[derive(Default)]
pub(crate) struct TextSel {
    pub(crate) pending: Option<PendingTextDrag>,
    pub(crate) active: Option<ActiveTextDrag>,
    pub(crate) persist: Option<PersistentTextSelection>,
    pub(crate) last_hit: Option<(Instant, RangeHit)>,
    pub(crate) clicks: u8,
}

impl TextSel {
    pub(crate) fn clear(&mut self) {
        self.pending = None;
        self.active = None;
        self.persist = None;
    }

    pub(crate) fn note_click(&mut self, now: Instant, hit: RangeHit) -> u8 {
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum DecisionStatus {
    Open,
    Answered,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Decision {
    pub(crate) id: String,
    pub(crate) prompt: String,
    pub(crate) options: Vec<String>,
    pub(crate) status: DecisionStatus,
}

pub(crate) struct App {
    pub(crate) prompt: PromptBuf,
    pub(crate) model: String,
    pub(crate) thinking: String,
    /// A switch the bridge has not applied yet. `op: model` queues behind a
    /// running step, so writing app.model on the picker's say-so made the
    /// header claim a model the harness was not using — for up to max_turns.
    /// Hold it here and let the bridge's snapshot be the thing that promotes it.
    pub(crate) model_pending: Option<(String, String)>,
    /// Background work the kernel is still holding: a monitored shell, a
    /// sleeper, any submitted task. Non-empty means the session will resume
    /// itself when one lands, which is the one thing polling gets wrong.
    pub(crate) background: Vec<String>,
    /// Bridge-owned decisions waiting for a one-key human answer, oldest first.
    pub(crate) decisions: Vec<Decision>,
    /// Slash completion for the composer. Recomputed on every keystroke that
    /// changes the line, so it never has to be dismissed explicitly.
    pub(crate) slash: slash::Slash,
    /// A paste is data, not a local command. While set, leading slash text is
    /// sent as prose and completion stays closed until a normal key edits it.
    pub(crate) slash_paste_guard: bool,
    /// Theme active before the completion list began previewing. `None` means
    /// there is no preview transaction to roll back.
    pub(crate) theme_preview_origin: Option<ThemeKind>,
    pub(crate) generation: String,
    pub(crate) running: bool,
    pub(crate) turn_started: Option<Instant>,
    /// First chrome paint waits for the bridge `ready` snapshot so the
    /// status line never flashes `effort:— gen —`.
    pub(crate) ready: bool,
    /// Click on grok `[stop]` — applied in the event loop with the bridge.
    pub(crate) want_stop: bool,
    pub(crate) turn_cancel: Option<Rect>,
    pub(crate) status: String,
    /// The last thing worth telling the user, and when. `status` alone was
    /// written in twenty places and rendered in none of them; a notice is that
    /// same string with a clock on it, so it can lapse instead of lingering.
    pub(crate) notice: Option<(Instant, String)>,
    /// The parent session's panes. A child's live in its [`ChildSess`];
    /// [`App::sess`] / [`App::sess_mut`] resolve whichever is on screen.
    pub(crate) sess: Sess,
    pub(crate) post_in: JsonTree,
    pub(crate) post_out: JsonTree,
    pub(crate) post_req: Value,
    pub(crate) post_resp: Value,
    pub(crate) post_inspect: Option<PostInspect>,
    /// New/resume choice shown only when a saved transcript exists.
    pub(crate) session_picker: session::SessionPicker,
    /// Onboarding / settings overlay. Modal when open.
    pub(crate) picker: picker::Picker,
    /// The fuzzy file picker (ctrl-t), distinct from `picker` above (the
    /// provider/model onboarding picker). A worker thread runs fff-search and
    /// ranks by the frecency the kernel's <edit>s feed; a modal overlay when open.
    pub(crate) file_picker: fuzzy::Picker,
    /// Inline-image state: what has been uploaded to the terminal, and where
    /// this frame wants it drawn. Populated during `draw`, consumed by
    /// `flush_media` right after the frame lands.
    pub(crate) media: Media,
    pub(crate) post_n: u64,
    /// Sequence number of the response currently in `post_out`. Lags `post_n`
    /// while a step is in flight: the out pane is still holding the previous
    /// turn's reply, and saying so beats blanking it under a new number.
    pub(crate) post_out_n: u64,
    pub(crate) queue: QueryQueue,
    pub(crate) send_now: bool,
    /// Cheatsheet for the focused pane's keys, open until the next key.
    pub(crate) help: bool,
    /// Slot a queued row was lifted from for editing, so Enter puts it back
    /// where it was instead of at the end of the queue.
    pub(crate) queue_edit: Option<usize>,
    pub(crate) drain_after: bool,
    pub(crate) children: HashMap<String, ChildSess>,
    pub(crate) rail_sel: usize,
    pub(crate) rail_seen: HashSet<String>,
    pub(crate) rail_area: Rect,
    /// Tree-of-runs mode on the Activity column (`t` toggles it): one row per
    /// subagent run, nested by the kernel's parent/depth. Selection is an
    /// index into `tree::order`.
    pub(crate) tree_open: bool,
    pub(crate) tree_sel: usize,
    pub(crate) viewing: Option<String>,
    pub(crate) viewer: Option<BlockViewerPane>,
    pub(crate) viewer_src: ViewerSrc,
    pub(crate) focus: Focus,
    pub(crate) traj_area: Rect,
    pub(crate) call_area: Rect,
    /// Whether the `YOU POST` / `MODEL POST` cards are on the wire.
    ///
    /// Off by default: they are the turn's accounting, not its content, and
    /// one per turn between the syscalls is most of the pane. The chip in the
    /// calls title puts them back.
    pub(crate) show_posts: bool,
    /// Where that chip is, so a click on the border can hit it.
    pub(crate) calls_chip: Option<Rect>,
    pub(crate) post_in_area: Rect,
    pub(crate) post_out_area: Rect,
    pub(crate) queue_area: Rect,
    pub(crate) input_area: Rect,
    pub(crate) input_inner: Rect,
    /// First wrapped prompt row visible in the bounded composer viewport.
    pub(crate) input_scroll: u16,
    pub(crate) mouse: Option<(u16, u16)>,
    pub(crate) last_click: Option<(Instant, usize, u8)>, // time, entry, pane: 0 story 1 calls 2 in 3 out
    pub(crate) last_chip_click: Option<(Instant, u64)>,
    pub(crate) cache: CacheMeter,
    pub(crate) git: side::GitPane,
    pub(crate) files: side::FilePane,
    pub(crate) git_area: Rect,
    pub(crate) files_area: Rect,
    pub(crate) layout: PaneLayout,
    /// `--demo`: canned events, no harness behind the composer. Without it a
    /// missing bridge means the harness died, and the two must not paint the
    /// same, or a dead session answers a prompt with a POST card labelled
    /// "demo" and then sits there.
    pub(crate) demo: bool,
    /// The bridge was attached and then died. Distinct from having no
    /// bridge at all: --demo and the pane tests run without one on purpose,
    /// and telling them the harness is gone is a lie they cannot act on.
    pub(crate) bridge_gone: bool,
}

impl App {
    pub(crate) fn new() -> Self {
        theme_cache::set(initial_theme());
        let mut app = Self {
            prompt: PromptBuf::new(),
            model: String::new(),
            thinking: String::new(),
            model_pending: None,
            background: Vec::new(),
            decisions: Vec::new(),
            slash: slash::Slash::default(),
            slash_paste_guard: false,
            theme_preview_origin: None,
            generation: String::new(),
            running: false,
            turn_started: None,
            ready: false,
            want_stop: false,
            turn_cancel: None,
            status: "idle".into(),
            notice: None,
            sess: Sess::new(),
            post_in: JsonTree::default(),
            post_out: JsonTree::default(),
            post_req: json!({}),
            post_resp: json!({}),
            post_inspect: None,
            session_picker: session::SessionPicker::default(),
            picker: picker::Picker::default(),
            file_picker: fuzzy::Picker::default(),
            media: Media::default(),
            post_n: 0,
            post_out_n: 0,
            queue: QueryQueue::default(),
            send_now: false,
            help: false,
            queue_edit: None,
            drain_after: false,
            children: HashMap::new(),
            rail_sel: 0,
            rail_seen: HashSet::new(),
            rail_area: Rect::default(),
            tree_open: false,
            tree_sel: 0,
            viewing: None,
            viewer: None,
            viewer_src: ViewerSrc::Story,
            focus: Focus::Input,
            traj_area: Rect::default(),
            call_area: Rect::default(),
            show_posts: false,
            calls_chip: None,
            post_in_area: Rect::default(),
            post_out_area: Rect::default(),
            queue_area: Rect::default(),
            input_area: Rect::default(),
            input_inner: Rect::default(),
            input_scroll: 0,
            mouse: None,
            last_click: None,
            last_chip_click: None,
            cache: CacheMeter::default(),
            git: side::GitPane::new(&std::env::current_dir().unwrap_or_default()),
            files: side::FilePane::new(&std::env::current_dir().unwrap_or_default()),
            git_area: Rect::default(),
            files_area: Rect::default(),
            layout: PaneLayout::load(),
            demo: false,
            bridge_gone: false,
        };
        app.apply_grok_settings();
        app
    }

    /// Load ~/.grok/pager.toml + grok UI cache (timestamps, compact, …)
    /// and push it onto both scrollbacks. Same stack the pager uses.
    pub(crate) fn apply_grok_settings(&mut self) {
        let cfg = grok_appearance();
        appearance::set_tab_width(cfg.scrollback.display.tab_width);
        // Verb grouping folds consecutive tools into "Ran 3 tools". Fine in
        // grok chat; fatal on the wire pane — every POST and <python> stays
        // its own row.
        appearance_cache::set_group_tool_verbs(false);
        // One blank row after every entry. Grok's chat column is one block per
        // paragraph, so it costs ~10% there; the story is many one-line blocks
        // -- a thought, a system row, a tool title -- in a third of the screen,
        // and a third of the pane was going to gap rows. Every block already
        // carries an accent column and a bullet, which is what the gap was
        // saying. `/dense` keeps the row for anyone who wants the air.
        appearance_cache::set_entry_gap(0);
        // ...except the row above a user prompt. Dense packing loses the turn
        // boundary: with no blank row anywhere, a new prompt reads as one more
        // block in the same run. One row there is the only spacing the story
        // spends, and it is the one that says "this is where you spoke".
        appearance_cache::set_turn_gap(1);
        self.sess.story.set_appearance(cfg.clone());
        self.sess.calls.set_appearance(cfg.clone());
        for child in self.children.values_mut() {
            child.sess.story.set_appearance(cfg.clone());
            child.sess.calls.set_appearance(cfg.clone());
        }
    }

    pub(crate) fn ensure_child(&mut self, id: &str, task: &str) -> &mut ChildSess {
        if !self.children.contains_key(id) {
            let look = grok_appearance();
            let mut sess = Sess::new();
            sess.story.set_appearance(look.clone());
            sess.calls.set_appearance(look);
            if !task.is_empty() {
                sess.story.push_block(RenderBlock::user_prompt(task));
            }
            let seq = self.children.len() as u64;
            self.children.insert(
                id.to_string(),
                ChildSess {
                    sess,
                    parent_entry: None,
                    parent: None,
                    depth: 0,
                    seq,
                    agent: String::new(),
                    stage: String::new(),
                    turns: 0,
                    tok_in: 0,
                    tok_out: 0,
                    state: String::new(),
                    accepted: None,
                    op_sent: None,
                },
            );
        }
        self.children.get_mut(id).expect("just inserted")
    }

    /// The session on screen: the child being viewed, else the parent.
    pub(crate) fn sess(&self) -> &Sess {
        if let Some(id) = self.viewing.as_deref() {
            if let Some(c) = self.children.get(id) {
                return &c.sess;
            }
        }
        &self.sess
    }

    /// The session on screen, mutably. The parent-or-child lookup exists
    /// here and in [`App::sess`] and nowhere else.
    pub(crate) fn sess_mut(&mut self) -> &mut Sess {
        if let Some(id) = self.viewing.clone() {
            if let Some(c) = self.children.get_mut(&id) {
                return &mut c.sess;
            }
        }
        &mut self.sess
    }

    pub(crate) fn viewer_scroll(&mut self) -> &mut ScrollbackState {
        let calls = self.viewer_src == ViewerSrc::Calls;
        self.sess_mut().scroll(calls)
    }

    pub(crate) fn open_block_viewer(&mut self) -> bool {
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
        let entry = {
            let pane = self.sess_mut().scroll(src == ViewerSrc::Calls);
            let Some(idx) = pane.selected() else {
                return false;
            };
            let Some(entry) = pane.entry(idx).cloned() else {
                return false;
            };
            entry
        };
        let detail = (src == ViewerSrc::Story)
            .then(|| self.sess().stream.run.detail(entry.id))
            .flatten()
            .map(str::to_owned);
        let viewer = match detail {
            Some(body) => BlockViewerPane::for_plain_text("activity sequence", &body),
            None => {
                let Some(viewer) = viewer_for_entry(&entry) else {
                    return false;
                };
                viewer
            }
        };
        self.viewer_src = src;
        self.viewer = Some(viewer);
        true
    }

    pub(crate) fn open_selected_session(&mut self) -> bool {
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
        let Some(idx) = self.sess.story.selected() else {
            return false;
        };
        let id = match self.sess.story.entry(idx).map(|e| &e.block) {
            Some(RenderBlock::Subagent(sb)) => sb.child_session_id.clone(),
            _ => return false,
        };
        let task = match self.sess.story.entry(idx).map(|e| &e.block) {
            Some(RenderBlock::Subagent(sb)) => sb.description.clone(),
            _ => String::new(),
        };
        self.ensure_child(&id, &task);
        self.viewing = Some(id);
        self.focus = Focus::Story;
        true
    }

    /// Put the `POST #n` cards on the wire, or take them off. Both panes on
    /// screen follow the one flag: a child's wire is the same pane.
    pub(crate) fn toggle_posts(&mut self) {
        self.show_posts = !self.show_posts;
        let shown = self.show_posts;
        if shown {
            self.sess.posts.show(&mut self.sess.calls);
        } else {
            self.sess.posts.hide(&mut self.sess.calls);
        }
        for c in self.children.values_mut() {
            if shown {
                c.sess.posts.show(&mut c.sess.calls);
            } else {
                c.sess.posts.hide(&mut c.sess.calls);
            }
        }
    }

    pub(crate) fn story_push(&mut self, block: RenderBlock) {
        // follow_mode is already true on a fresh state; prepare_layout pins
        // the viewport. goto_bottom() before the first layout has
        // viewport_height=0, so max_offset == total_height and the whole
        // transcript scrolls off-screen.
        self.sess.story.push_block(block);
    }

    pub(crate) fn call_push(&mut self, block: RenderBlock) {
        wire_push(&mut self.sess.calls, block);
    }

    /// `(current, total)` group position, 1-based, for the wire pane title.
    ///
    /// "Current" follows the selection when there is one so `[`/`]` can be
    /// watched moving, and otherwise reports the newest group, which is what
    /// the tail the reader is staring at actually belongs to.
    pub(crate) fn call_group_pos(&self) -> Option<(usize, usize)> {
        let s = self.sess();
        let starts = s.posts.starts(&s.calls);
        let total = starts.len();
        if total == 0 {
            return None;
        }
        // Groups are pushed in entry order, so the group a card belongs to is
        // the last boundary at or above it.
        let cur = match s.calls.selected() {
            Some(sel) => starts.iter().filter(|start| **start <= sel).count().max(1),
            None => total,
        };
        Some((cur, total))
    }

    /// Move the wire selection to the first card of the previous/next group.
    ///
    /// Returns false when there is nowhere to go, so the caller can leave the
    /// selection alone rather than snapping to an end.
    pub(crate) fn select_call_group(&mut self, forward: bool) -> bool {
        let s = self.sess_mut();
        let starts = s.posts.starts(&s.calls);
        if starts.is_empty() {
            return false;
        }
        let target = match s.calls.selected() {
            None if forward => starts.first().copied(),
            None => starts.last().copied(),
            Some(cur) if forward => starts.iter().copied().find(|s| *s > cur),
            Some(cur) => starts.iter().rev().copied().find(|s| *s < cur),
        };
        let Some(target) = target else {
            return false;
        };
        s.calls.set_selected(Some(target));
        s.calls.scroll_to_entry_top(target);
        true
    }

    /// First tree row on screen: enough skipped that the selection stays
    /// visible. Deterministic from state, so the draw pass and the mouse
    /// hit-test compute the same number instead of sharing a cached one.
    pub(crate) fn tree_skip(&self) -> usize {
        let h = self.call_area.height.saturating_sub(2) as usize;
        if h == 0 { 0 } else { self.tree_sel.saturating_sub(h - 1) }
    }

    pub(crate) fn focused_scroll(&mut self) -> &mut ScrollbackState {
        let calls = self.focus == Focus::Calls;
        self.sess_mut().scroll(calls)
    }

    pub(crate) fn focused_tree(&mut self) -> Option<&mut JsonTree> {
        match self.focus {
            Focus::PostIn => Some(&mut self.post_in),
            Focus::PostOut => Some(&mut self.post_out),
            _ => None,
        }
    }

    pub(crate) fn set_last_post(&mut self, n: u64, request: &Value, response: &Value) {
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

    pub(crate) fn open_post_inspect(&mut self) {
        self.viewer = None;
        if self.post_inspect.is_some() {
            self.post_inspect = None;
            return;
        }
        let tab = if self.focus == Focus::PostOut { 1 } else { 0 };
        self.post_inspect = Some(PostInspect::open(tab));
    }

    /// Say something to the user. Sets `status` too, so the handful of places
    /// that read it keep working, but stamps it so the composer can show it and
    /// then drop it. Lifecycle words -- idle, running, stopping -- assign
    /// `status` directly and raise no notice: the composer's animated frame
    /// carries runtime state, and "idle" landing would wipe "copied".
    pub(crate) fn notify(&mut self, msg: impl Into<String>) {
        let msg = msg.into();
        self.notice = Some((Instant::now(), msg.clone()));
        self.status = msg;
    }

    /// A notice that has had its few seconds. Checked on the idle tick, since
    /// nothing else forces a repaint once the text stops being true.
    pub(crate) fn notice_stale(&self) -> bool {
        self.notice
            .as_ref()
            .is_some_and(|(t, _)| t.elapsed() > NOTICE_TTL)
    }

    pub(crate) fn set_focus(&mut self, focus: Focus) {
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
            Focus::Story => self.sess_mut().story.on_activate(),
            Focus::Calls => self.sess_mut().calls.on_activate(),
            Focus::Rail
            | Focus::Meter
            | Focus::Git
            | Focus::Files
            | Focus::PostIn
            | Focus::PostOut
            | Focus::Queue
            | Focus::Input => {}
        }
    }
}
