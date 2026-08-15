//! The wire pane's card mechanics: POST call groups (`PostArgs` /
//! `PostRows`, one group per `complete()` POST) and fold state (`wire_push`,
//! `reflow_wire`, `set_wire_mode`, manual pins). Moved verbatim out of
//! main.rs (and `App::call_push_group` out of app.rs).

use std::collections::HashSet;

use serde_json::Value;
use xai_grok_pager::scrollback::{
    DisplayMode, EntryId, RenderBlock, ScrollbackState,
};

use crate::{App, wire_complete};

impl App {
    /// Push a card that opens a new call group. Every `complete()` POST starts
    /// one; the syscalls it produced land after it and belong to it.
    pub(crate) fn call_push_group(&mut self, args: PostArgs) {
        let shown = self.show_posts;
        self.sess.posts.push(&mut self.sess.calls, args, shown);
    }
}

/// Push a wire card Collapsed. It does not stay that way: `reflow_wire` runs
/// every frame and reopens the tail, so a fresh card is Expanded by the time
/// it is painted. Starting folded keeps grok's Other/Read/Edit defaults from
/// flashing their full payload for one frame before the reconcile.
///
/// `l` / Enter opens a card, `h` folds it; either marks it manual and
/// `reflow_wire` stops managing it.
/// The arguments one `POST #n` card is built from.
///
/// Held next to the pane rather than only inside it: POST rows are off by
/// default now, and a row that is not on screen still has to be rebuildable
/// the moment the reader asks for it back.
#[derive(Clone)]
pub(crate) struct PostArgs {
    origin: String,
    n: u64,
    model: String,
    thinking: String,
    usage: Value,
    thoughts: u64,
    redacted: u64,
}

impl PostArgs {
    pub(crate) fn new(
        origin: &str,
        n: u64,
        model: &str,
        thinking: &str,
        usage: &Value,
        thoughts: u64,
        redacted: u64,
    ) -> Self {
        Self {
            origin: origin.to_string(),
            n,
            model: model.to_string(),
            thinking: thinking.to_string(),
            usage: usage.clone(),
            thoughts,
            redacted,
        }
    }

    pub(crate) fn block(&self) -> RenderBlock {
        wire_complete(
            &self.origin,
            self.n,
            &self.model,
            &self.thinking,
            &self.usage,
            self.thoughts,
            self.redacted,
        )
    }
}

pub(crate) struct PostRow {
    args: PostArgs,
    /// The live card, while the row is on screen.
    id: Option<EntryId>,
    /// The card this POST was pushed after — `None` when it opened the pane.
    /// Recorded once, at push time, and never moved: it is what a hidden row
    /// goes back in front of. The card *after* it cannot be used for that,
    /// because streaming execute output and results append to the pane
    /// without passing through here.
    prev: Option<EntryId>,
}

/// Every `complete()` POST of one wire pane, on screen or held back.
///
/// This is also the pane's group index: a group starts at its POST card, or —
/// when the POST rows are hidden — at the first card that followed it.
#[derive(Default)]
pub(crate) struct PostRows(Vec<PostRow>);

impl PostRows {
    pub(crate) fn len(&self) -> usize {
        self.0.len()
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub(crate) fn clear(&mut self) {
        self.0.clear();
    }

    pub(crate) fn push(&mut self, calls: &mut ScrollbackState, args: PostArgs, shown: bool) {
        let prev = calls
            .len()
            .checked_sub(1)
            .and_then(|i| calls.entry(i))
            .map(|e| e.id);
        let id = if shown {
            Some(wire_push(calls, args.block()))
        } else {
            None
        };
        self.0.push(PostRow { args, id, prev });
    }

    /// Put every held-back card back where it was pushed.
    pub(crate) fn show(&mut self, calls: &mut ScrollbackState) {
        let mut after: Option<EntryId> = None;
        for row in &mut self.0 {
            if let Some(id) = row.id {
                after = Some(id);
                continue;
            }
            // Two POSTs in a row share a `prev` — the model answered without
            // calling anything — so the second belongs after the first.
            let prev = match (after, row.prev) {
                (Some(a), None) => Some(a),
                (Some(a), Some(p))
                    if calls.index_of_id(a) >= calls.index_of_id(p) && a != p =>
                {
                    Some(a)
                }
                _ => row.prev,
            };
            let at = match prev {
                Some(p) => calls.index_of_id(p).map(|i| i + 1),
                None => Some(0),
            };
            let anchor = at.and_then(|i| calls.entry(i)).map(|e| e.id);
            let id = match anchor {
                Some(a) => calls.insert_block_before(a, row.args.block()),
                None => calls.push_block(row.args.block()),
            };
            set_wire_mode(calls, id, DisplayMode::Collapsed);
            row.id = Some(id);
            after = Some(id);
        }
    }

    /// Take every POST card off the pane, keeping the data to rebuild it.
    pub(crate) fn hide(&mut self, calls: &mut ScrollbackState) {
        // A POST pushed straight after another POST has that card as its
        // `prev`; dropping the first would leave the second pointing at an id
        // the pane no longer has, so the link is repaired as we go.
        let mut dropped: Option<(EntryId, Option<EntryId>)> = None;
        for row in &mut self.0 {
            if let (Some((gone, gone_prev)), Some(p)) = (dropped, row.prev) {
                if p == gone {
                    row.prev = gone_prev;
                }
            }
            if let Some(id) = row.id.take() {
                calls.remove_entry(id);
                dropped = Some((id, row.prev));
            }
        }
    }

    /// Index of the first card of each group, in pane order.
    pub(crate) fn starts(&self, calls: &ScrollbackState) -> Vec<usize> {
        let mut out: Vec<usize> = self
            .0
            .iter()
            .filter_map(|row| match row.id {
                Some(id) => calls.index_of_id(id),
                None => {
                    let at = match row.prev {
                        Some(p) => calls.index_of_id(p)? + 1,
                        None => 0,
                    };
                    (at < calls.len()).then_some(at)
                }
            })
            .collect();
        out.sort_unstable();
        out.dedup();
        out
    }
}

pub(crate) fn wire_push(sb: &mut ScrollbackState, block: RenderBlock) -> EntryId {
    let eid = sb.push_block(block);
    set_wire_mode(sb, eid, DisplayMode::Collapsed);
    eid
}

/// How many trailing wire cards stay open. The tail is where the reader is
/// looking; older cards fold back to a header + preview.
const WIRE_OPEN: usize = 3;

/// Keep the tail of the wire pane open: the last `WIRE_OPEN` cards, plus any
/// card still running, are Expanded; everything above them collapses. Finished
/// thoughts start collapsed even at the tail. Cards in `manual` were folded or
/// opened by hand and are never touched.
///
/// Runs every frame. It is a fold-state reconcile, not an event handler, so a
/// card that arrives while another is streaming still ends up in the right
/// state without every push site remembering to call it.
pub(crate) fn reflow_wire(sb: &mut ScrollbackState, manual: &HashSet<EntryId>) {
    let n = sb.len();
    let mut rows: Vec<(EntryId, bool, bool)> = Vec::with_capacity(n);
    for i in 0..n {
        if let Some(e) = sb.entry(i) {
            rows.push((
                e.id,
                e.is_running,
                matches!(e.block, RenderBlock::Thinking(_)),
            ));
        }
    }
    let cut = rows.len().saturating_sub(WIRE_OPEN);
    for (idx, (id, running, thinking)) in rows.into_iter().enumerate() {
        if manual.contains(&id) {
            continue;
        }
        let want = if thinking && !running {
            DisplayMode::Collapsed
        } else if running || idx >= cut {
            DisplayMode::Expanded
        } else {
            DisplayMode::Collapsed
        };
        set_wire_mode(sb, id, want);
    }
}

/// Record that the reader took manual control of the selected wire card.
///
/// Resolves the pane on screen: the fold this protects is applied to
/// `focused_scroll()`, so inside a child session this used to pin a parent id
/// and reflow re-expanded the child's card on the next frame.
pub(crate) fn pin_selected_wire(app: &mut App) {
    let s = app.sess_mut();
    let Some(i) = s.calls.selected() else {
        return;
    };
    if let Some(e) = s.calls.entry(i) {
        let id = e.id;
        s.wire_manual.insert(id);
    }
}

pub(crate) fn set_wire_mode(sb: &mut ScrollbackState, id: EntryId, mode: DisplayMode) {
    if let Some(entry) = sb.get_by_id_mut(id) {
        if entry.display_mode == mode {
            return;
        }
        entry.set_display_mode(mode);
        entry.display_mode_pinned = true;
    }
    sb.mark_height_dirty(id);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tests::paint;

    fn wire_modes(app: &App) -> Vec<DisplayMode> {
        (0..app.sess.calls.len())
            .filter_map(|i| app.sess.calls.entry(i).map(|e| e.display_mode))
            .collect()
    }

    #[test]
    fn last_three_wire_cards_stay_open() {
        let mut app = App::new();
        for i in 0..7 {
            wire_push(&mut app.sess.calls, RenderBlock::agent_message(format!("CARD{i}")));
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
        let old = wire_push(&mut app.sess.calls, RenderBlock::agent_message("OLDRUNNER"));
        app.sess.calls.set_last_running(true);
        for i in 0..6 {
            wire_push(&mut app.sess.calls, RenderBlock::agent_message(format!("CARD{i}")));
        }
        let _ = paint(&mut app, 140, 40);
        let mode = app.sess.calls.get_by_id(old).map(|e| e.display_mode);
        assert_eq!(mode, Some(DisplayMode::Expanded), "a running card must not fold");
    }

    #[test]
    fn a_hand_folded_card_is_left_alone() {
        let mut app = App::new();
        let mut last = None;
        for i in 0..3 {
            last = Some(wire_push(&mut app.sess.calls, RenderBlock::agent_message(format!("CARD{i}"))));
        }
        let _ = paint(&mut app, 140, 40);
        let id = last.unwrap();
        app.sess.wire_manual.insert(id);
        set_wire_mode(&mut app.sess.calls, id, DisplayMode::Collapsed);
        let _ = paint(&mut app, 140, 40);
        let mode = app.sess.calls.get_by_id(id).map(|e| e.display_mode);
        assert_eq!(mode, Some(DisplayMode::Collapsed), "reflow overrode a manual fold");
    }
}
