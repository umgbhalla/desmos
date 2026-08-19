//! Keyboard and mouse handling: `handle_key`, `handle_mouse`, the
//! scrollback click/drag/selection path, viewer and POST-inspect input, and
//! focus cycling. Moved verbatim out of main.rs; the order of checks here is
//! load-bearing, so nothing was cleaned up on the way over.
//!
//! The glob import is deliberate: this module is the crate root's input half
//! and reads the same names the root does.

use crate::*;
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

pub(crate) fn handle_key(
    mut bridge: Option<&mut Bridge>,
    app: &mut App,
    key: KeyEvent,
) -> io::Result<bool> {
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
        return on_ctrl_c(bridge.as_deref_mut(), app);
    }
    // A saved transcript must be accepted or replaced before either the model
    // picker or the panes receive input.
    if app.session_picker.open {
        if let Some(choice) = app.session_picker.key(key.code) {
            apply_session_choice(bridge.as_deref_mut(), app, choice)?;
        }
        return Ok(false);
    }
    // The picker is modal on purpose. On a fresh machine there is no session
    // behind it to type into, so it has to win before any pane sees the key.
    if app.picker.open {
        let action = app.picker.key(key.code);
        return apply_picker(bridge.as_deref_mut(), app, action);
    }
    // ctrl-t opens the fuzzy file picker from anywhere (ctrl-c/p/g/b are taken,
    // ctrl-f is PostIn-local). While open it is modal: it wins every key before
    // any pane sees it, same contract as the model picker above.
    if key.modifiers.contains(KeyModifiers::CONTROL)
        && key.code == KeyCode::Char('t')
        && app.viewer.is_none()
        && app.post_inspect.is_none()
    {
        if app.file_picker.is_open() {
            app.file_picker.close();
        } else {
            // Same cwd source the git/file panes use.
            app.file_picker.open(&std::env::current_dir().unwrap_or_default());
        }
        return Ok(false);
    }
    if app.file_picker.is_open() {
        match key.code {
            KeyCode::Esc => app.file_picker.close(),
            KeyCode::Enter => {
                app.file_picker.enter();
                if let Some(path) = app.file_picker.take_chosen() {
                    app.files.open(&path);
                    app.set_focus(Focus::Files);
                }
            }
            KeyCode::Up => app.file_picker.select(-1),
            KeyCode::Down => app.file_picker.select(1),
            KeyCode::Backspace => app.file_picker.backspace(),
            KeyCode::Char(c) => app.file_picker.push_char(c),
            _ => {}
        }
        return Ok(false);
    }
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('p') {
        let (m, e) = (app.model.clone(), app.thinking.clone());
        app.picker.open_for_change(&m, &e);
        return Ok(false);
    }
    // An open completion list is modal for the four keys it owns, and has to
    // say so up here: Tab and Esc are claimed by the global pane-cycle further
    // down, so a handler in the input branch never sees them.
    if app.slash.open && app.focus == Focus::Input {
        match key.code {
            KeyCode::Up => {
                app.slash.move_sel(-1);
                sync_theme_preview(app);
                return Ok(false);
            }
            KeyCode::Down => {
                app.slash.move_sel(1);
                sync_theme_preview(app);
                return Ok(false);
            }
            // Tab always completes. Enter only completes when there is
            // something left to complete -- otherwise it sends.
            //
            // Enter used to accept unconditionally, which made a command with
            // no argument unrunnable: typing /reset left one suggestion,
            // accepting it produced the line already typed, the list matched
            // it again, and Enter looped there forever. The only escape was a
            // space, because "/reset " has an empty argument and closes the
            // list. Accepting is only a move if it changes the line.
            KeyCode::Tab => {
                if let Some(line) = app.slash.accept() {
                    app.prompt.clear();
                    app.prompt.insert_str(&line);
                    update_slash(app);
                }
                return Ok(false);
            }
            KeyCode::Enter => {
                // A theme row is already a complete choice: Enter commits the
                // preview and runs the local command in one step. Other lists
                // keep their ordinary completion semantics.
                if app.slash.is_theme_values()
                    && let Some(line) = app.slash.accept()
                {
                    app.prompt.clear();
                    app.prompt.insert_str(&line);
                    app.slash.close();
                    app.theme_preview_origin = None;
                    return submit_prompt(bridge.as_deref_mut(), app);
                }
                // Send anything that already runs. "Would accepting change the
                // line" was the wrong question: /model takes an argument, so
                // accept() appended a space, so Enter completed instead of
                // sending -- and bare /model, which is how the picker opens,
                // could never be submitted at all. verdict already knows which
                // lines are runnable, including the ones whose argument is
                // optional, so ask it.
                let typed = app.prompt.to_send();
                if slash::verdict(&typed, &app.picker) == slash::Verdict::Ready {
                    app.slash.close();
                } else if let Some(line) = app.slash.accept() {
                    if line != typed {
                        app.prompt.clear();
                        app.prompt.insert_str(&line);
                        update_slash(app);
                        return Ok(false);
                    }
                    app.slash.close();
                } else {
                    app.slash.close();
                }
            }
            KeyCode::Esc => {
                app.slash.close();
                sync_theme_preview(app);
                return Ok(false);
            }
            _ => {}
        }
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

    // The cheatsheet is a modal over the focused pane, so it eats the next key
    // whatever it is. Anything else means guessing which keys are "dismiss" and
    // which fall through, and a sheet you have to dismiss twice is worse than
    // no sheet.
    if app.help {
        app.help = false;
        return Ok(false);
    }
    // `?` in any pane but the composer. Every pane has its own verbs and none
    // of them were written down anywhere you could read while looking at the
    // pane; the legend that used to live on the composer border was one line
    // for the whole app and went away with it. In the composer `?` is a
    // question mark.
    if key.code == KeyCode::Char('?')
        && app.focus != Focus::Input
        && app.viewer.is_none()
        && app.post_inspect.is_none()
    {
        app.help = true;
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
                None => app.notify("clipboard empty"),
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
                None => app.notify("clipboard empty"),
            }
            return Ok(false);
        }
        handle_post_inspect_key(app, key);
        return Ok(false);
    }
    if is_inline_paste_key(&key) {
        match clipboard_text() {
            Some(text) => apply_paste(app, &text, true),
            None => app.notify("clipboard empty"),
        }
        return Ok(false);
    }
    if is_paste_key(&key) {
        // grok's order: probe the pasteboard for a raster or file URLs before
        // falling back to text. A screenshot copied with Cmd+Shift+Ctrl+4 has
        // no text representation at all, so text-first sees an empty clipboard
        // and the picture is lost.
        if let Some(path) = prompt::clipboard_image_path() {
            if app.focus != Focus::Input {
                app.set_focus(Focus::Input);
            }
            guard_paste_from_slash(app);
            app.prompt.insert_image(&path);
            app.notify("attached 1 image(s)");
            return Ok(false);
        }
        match clipboard_text() {
            Some(text) => apply_paste(app, &text, false),
            None => app.notify("clipboard empty"),
        }
        return Ok(false);
    }
    // Match Codex: Enter steers the running turn; Tab keeps this draft as a
    // visible follow-up for the next turn. Empty Tab still cycles panes.
    if key.code == KeyCode::Tab
        && key.modifiers == KeyModifiers::NONE
        && app.focus == Focus::Input
        && app.running
        && (!app.prompt.to_send().trim().is_empty() || !app.prompt.images().is_empty())
    {
        return queue_prompt(bridge.as_deref_mut(), app);
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
            // Accumulate, never short-circuit: `||` and `any` stopped at the
            // first pane that had a selection, so with a highlight on both the
            // story and the wire, Esc cleared one per press.
            let mut cleared = app.sess.story_text.persist.take().is_some();
            cleared |= app.sess.calls_text.persist.take().is_some();
            for c in app.children.values_mut() {
                cleared |= c.sess.story_text.persist.take().is_some();
                cleared |= c.sess.calls_text.persist.take().is_some();
            }
            if cleared {
                return Ok(false);
            }
            if app.viewing.take().is_some() {
                // Back where you came from: a child opened off the run tree
                // returns to the tree, one opened off a story row to the story.
                app.focus = if app.tree_open {
                    Focus::Calls
                } else {
                    Focus::Story
                };
                return Ok(false);
            }
            if app.focus == Focus::Calls && app.tree_open {
                app.tree_open = false;
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

    if app.focus == Focus::Rail {
        let last = rail::rows(app).len().saturating_sub(1);
        match key.code {
            KeyCode::Char('j') | KeyCode::Down => app.rail_sel = (app.rail_sel + 1).min(last),
            KeyCode::Char('k') | KeyCode::Up => app.rail_sel = app.rail_sel.saturating_sub(1),
            KeyCode::Enter | KeyCode::Right => rail::activate(app),
            KeyCode::Char('i') => app.set_focus(Focus::Input),
            _ => {}
        }
        return Ok(false);
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
                app.queue_edit = None;
                if app.queue.is_empty() {
                    app.set_focus(Focus::Input);
                }
            }
            // Drop was the only thing you could do to a queued row, so fixing a
            // typo in one meant deleting it and typing the whole thing again.
            // `e` lifts it into the composer instead; the slot is remembered so
            // Enter puts it back where it was.
            KeyCode::Char('e') => {
                if let Some(idx) = app.queue.selected
                    && let Some(item) = app.queue.remove_selected()
                {
                    app.prompt.clear();
                    app.prompt.insert_str(&item.text);
                    app.queue_edit = Some(idx);
                    app.set_focus(Focus::Input);
                    app.notify(format!("editing #{} — enter puts it back", idx + 1));
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
            KeyCode::Char('r') => {
                app.git.poll(true);
            }
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
    // The run tree owns the Activity keys while it is up (upgrade-paths 3.2 /
    // 3.3): walk rows, open a child, intervene. `x`/`r` put the contract-C3 op
    // on the bridge; the row says "sent (unconfirmed)" until the kernel's own
    // terminal `subagent` event (kill) or a fresh `started` (rerun) answers.
    if app.focus == Focus::Calls && app.tree_open {
        let last = tree::order(&app.children).len().saturating_sub(1);
        match key.code {
            KeyCode::Char('j') | KeyCode::Down => app.tree_sel = (app.tree_sel + 1).min(last),
            KeyCode::Char('k') | KeyCode::Up => app.tree_sel = app.tree_sel.saturating_sub(1),
            KeyCode::Char('t') | KeyCode::Char('q') => app.tree_open = false,
            KeyCode::Enter | KeyCode::Char('l') | KeyCode::Right => {
                if let Some(id) = tree::order(&app.children).get(app.tree_sel).cloned() {
                    app.ensure_child(&id, "");
                    app.viewing = Some(id);
                }
            }
            KeyCode::Char('x') | KeyCode::Char('r') => {
                let kill = key.code == KeyCode::Char('x');
                match bridge.as_deref_mut() {
                    Some(b) => {
                        if let Some(op) = tree::intervene(app, kill) {
                            b.send(&op)?;
                            app.notify(if kill {
                                "kill_run sent — the run's terminal event confirms it"
                            } else {
                                "rerun sent — the new run appears as its own row"
                            });
                        }
                    }
                    // No bridge, no wire: nothing was sent, so nothing is
                    // marked pending and the composer says why.
                    None => app.notify("no bridge — intervention not sent"),
                }
            }
            KeyCode::Char('i') => app.set_focus(Focus::Input),
            _ => {}
        }
        return Ok(false);
    }
    // Choice blocks own vertical movement while the story pane is focused.
    // Enter turns the highlighted row into ordinary composer text and follows
    // the exact same submit path as a typed prompt.
    if app.focus == Focus::Story {
        let choice_key = match key.code {
            KeyCode::Char('j') | KeyCode::Down => {
                app.focused_scroll().move_latest_choice_selection(1)
            }
            KeyCode::Char('k') | KeyCode::Up => {
                app.focused_scroll().move_latest_choice_selection(-1)
            }
            KeyCode::Enter => {
                if let Some(prompt) = app.focused_scroll().take_latest_choice_prompt() {
                    app.prompt.clear();
                    app.prompt.insert_str(&prompt);
                    app.set_focus(Focus::Input);
                    return submit_prompt(bridge.as_deref_mut(), app);
                }
                false
            }
            _ => false,
        };
        if choice_key {
            return Ok(false);
        }
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
            // Group step. Arrows already mean fold in this pane, so walking
            // whole POST groups gets its own pair rather than overloading them.
            // The POST rows are the turn's accounting, not its content, so
            // they stay off until asked for — by this key or the title chip.
            KeyCode::Char('p') if app.focus == Focus::Calls => {
                app.toggle_posts();
                let on = if app.show_posts { "on" } else { "off" };
                app.notify(format!("POST rows {on}"));
            }
            KeyCode::Char('[') if app.focus == Focus::Calls => {
                app.select_call_group(false);
            }
            KeyCode::Char(']') if app.focus == Focus::Calls => {
                app.select_call_group(true);
            }
            // The run tree (3.2): one row per subagent run, nested by the
            // kernel's parent/depth, over this column until t/Esc.
            KeyCode::Char('t') if app.focus == Focus::Calls => {
                app.tree_open = true;
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
    // The newest open decision owns bare digits only while the composer is
    // empty. Once typing has begun, digits remain ordinary text.
    if app.prompt.to_send().is_empty()
        && app.prompt.images().is_empty()
        && let KeyCode::Char(digit @ '1'..='9') = key.code
        && is_text_key(&key)
        && let Some(decision) = app.decisions.last()
        && let Some(option) = decision.options.get(digit as usize - '1' as usize)
    {
        let line = format!(
            "decide:{} — {}: {}",
            decision.id, decision.prompt, option
        );
        app.prompt.insert_str(&line);
        return submit_prompt(bridge, app);
    }
    let edited = match key.code {
        KeyCode::Char('a') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.prompt.move_line_home(width);
            false
        }
        KeyCode::Char('e') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            app.prompt.move_line_end(width);
            false
        }
        KeyCode::Char(c) if is_text_key(&key) => {
            app.prompt.insert_char(c);
            true
        }
        KeyCode::Backspace => {
            app.prompt.backspace();
            true
        }
        KeyCode::Delete => {
            app.prompt.delete();
            true
        }
        KeyCode::Left => {
            app.prompt.move_left();
            false
        }
        KeyCode::Right => {
            app.prompt.move_right();
            false
        }
        KeyCode::Up => {
            app.prompt.move_up(width);
            false
        }
        KeyCode::Down => {
            app.prompt.move_down(width);
            false
        }
        KeyCode::Home => {
            app.prompt.move_line_home(width);
            false
        }
        KeyCode::End => {
            app.prompt.move_line_end(width);
            false
        }
        KeyCode::Enter if key.modifiers.contains(KeyModifiers::ALT) => {
            return submit_prompt_forced_step(bridge, app);
        }
        KeyCode::Enter if is_mod_enter(&key) => {
            app.prompt.insert_char('\n');
            true
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
        _ => false,
    };
    // Editing a paste makes it intentional composer text again. Movement alone
    // must not arm a pasted `/reset` as a local command.
    if edited {
        app.slash_paste_guard = false;
    }
    // One recompute after any edit, rather than a call at each of the dozen
    // sites that can change the line. A line that stopped being a command
    // closes the list on its own.
    update_slash(app);
    Ok(false)
}

/// Tab skips panes the layout has collapsed to nothing.
pub(crate) fn pane_open(app: &App) -> impl Fn(Focus) -> bool + use<> {
    // The rects draw actually assigned, not the heights the layout asked for.
    // A short terminal clamps a requested pane to zero rows, and Tab used to
    // land on it anyway — j/k then went to a pane with nothing on screen. This
    // is the same ground truth the mouse hit-tests use, and one frame is
    // always painted before the first key is read.
    let rail = app.rail_area.width > 0;
    let queue = !app.queue.is_empty();
    let post = app.post_in_area.height > 0;
    let meter = app.cache.area.height > 0;
    let git = app.git_area.height > 0;
    let files = app.files_area.height > 0;
    move |f| match f {
        Focus::Rail => rail,
        Focus::Queue => queue,
        Focus::PostIn | Focus::PostOut => post,
        Focus::Meter => meter,
        Focus::Git => git,
        Focus::Files => files,
        _ => true,
    }
}

/// The prompt text for an attachment-only send: the file names, nothing more.
pub(crate) fn image_prompt_text(images: &[String]) -> String {
    let names: Vec<&str> = images
        .iter()
        .map(|p| p.rsplit('/').next().unwrap_or(p.as_str()))
        .collect();
    names.join(", ")
}

/// Inline-image bookkeeping for the Kitty graphics protocol.
///
/// The scrollback renderer reserves the rows and hands back an
/// [`InlineMediaPlacement`] per visible image; the pixels are never part of
/// the ratatui buffer. They are escape sequences written straight to the
/// terminal after the frame is flushed, which is why this state lives outside
/// the draw pass: an image is transmitted once per path, then only *placed*
/// (a ~80 byte escape) on every later frame.
#[derive(Default)]
pub(crate) struct Media {
    /// Kitty image id per file, allocated on first successful transmit.
    pub(crate) ids: HashMap<PathBuf, u32>,
    /// Encoded bytes per file, kept so a re-place never re-reads the disk.
    pub(crate) bytes: HashMap<PathBuf, Vec<u8>>,
    pub(crate) next_id: u32,
    /// Ids holding a live placement from the previous frame. Anything that
    /// scrolls out of view has to be deleted explicitly -- a Kitty placement
    /// outlives the cells it was drawn over.
    pub(crate) placed: HashSet<u32>,
    /// Placements collected during the current draw pass.
    pub(crate) frame: Vec<InlineMediaPlacement>,
}

/// A story row for one attached image: the file name, its path, and the
/// picture underneath. `None` when the path is not a decodable image, in
/// which case nothing is pushed and the prompt row stands alone.
pub(crate) fn media_block(path: &str) -> Option<RenderBlock> {
    use xai_grok_pager::prompt_images::ScrollbackImageRef;
    ScrollbackImageRef::from_path(path)?;
    let name = std::path::Path::new(path)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.to_string());
    Some(RenderBlock::ToolCall(ToolCallBlock::Other(
        OtherToolCallBlock::new("image", name).with_media_ref(path, false),
    )))
}

/// Draw the frame's inline images.
///
/// Runs after `terminal.draw`: the placement escapes address screen cells the
/// frame just painted, and Kitty draws them under (z = -1) the text already
/// there. The cursor is saved and restored around the batch, because placing
/// an image moves it and the composer's caret was set by the frame.
pub(crate) fn flush_media(app: &mut App, out: &mut impl Write) -> io::Result<()> {
    let frame = std::mem::take(&mut app.media.frame);
    if !gfx::scrollback_inline_overlay_active() {
        return Ok(());
    }
    let mut esc = String::new();
    let mut now: HashSet<u32> = HashSet::new();
    let mut seen: HashSet<PathBuf> = HashSet::new();
    for p in frame {
        // `full_rows == 0` is the text-affordance placement the renderer emits
        // for terminals without graphics: no pixels, just a clickable row.
        if p.full_rows == 0 || p.info.is_video || !seen.insert(p.info.path.clone()) {
            continue;
        }
        let path = p.info.path.clone();
        if !app.media.bytes.contains_key(&path) {
            let Ok(raw) = std::fs::read(&path) else { continue };
            let Some(ready) = gfx::prepare_overlay_image_bytes(&raw) else {
                continue;
            };
            app.media.bytes.insert(path.clone(), ready);
        }
        let known = app.media.ids.get(&path).copied();
        let id = known.unwrap_or(app.media.next_id + 1);
        let bytes = &app.media.bytes[&path];
        if known.is_none() {
            let Some(t) = gfx::transmit_inline_image(bytes, id) else {
                continue;
            };
            esc.push_str(&t);
        }
        let Some(place) = gfx::place_inline_image(
            bytes,
            p.info.width,
            p.info.height,
            p.screen_rect,
            p.full_rows,
            p.top_crop_rows,
            id,
            known.is_none(),
        ) else {
            continue;
        };
        esc.push_str(&place);
        now.insert(id);
        if known.is_none() {
            app.media.next_id = id;
            app.media.ids.insert(path, id);
        }
    }
    for id in &app.media.placed {
        if !now.contains(id) {
            esc.push_str(&gfx::clear_kitty_image(*id));
        }
    }
    app.media.placed = now;
    if esc.is_empty() {
        return Ok(());
    }
    out.write_all(b"\x1b7")?;
    out.write_all(esc.as_bytes())?;
    out.write_all(b"\x1b8")?;
    out.flush()
}

fn guard_paste_from_slash(app: &mut App) {
    app.slash_paste_guard = true;
    app.slash.close();
    sync_theme_preview(app);
}

pub(crate) fn apply_paste(app: &mut App, text: &str, inline: bool) {
    guard_paste_from_slash(app);
    if app.focus != Focus::Input {
        app.set_focus(Focus::Input);
    }
    // A paste that is nothing but paths to images on disk is an attachment,
    // not prose -- dragging a screenshot onto the terminal is how most of them
    // arrive. Mixed or unresolvable text stays text; guessing the other way
    // would drop what the user actually typed.
    if !inline {
        let paths = prompt::image_paste_paths(text);
        if !paths.is_empty() {
            let n = paths.len();
            for path in paths {
                app.prompt.insert_image(&path);
            }
            app.notify(format!("attached {n} image(s)"));
            return;
        }
    }
    if inline {
        app.prompt.handle_inline_paste(text);
    } else {
        app.prompt.handle_paste(text);
    }
}

/// The command alone, or the command followed by its argument — never a
/// longer word that merely starts the same way.
pub(crate) fn is_slash_word(line: &str, cmd: &str) -> bool {
    line == cmd || line.strip_prefix(cmd).is_some_and(|rest| rest.starts_with(' '))
}

pub(crate) fn is_local_slash(line: &str) -> bool {
    let t = line.trim();
    t == "/quit"
        || t == "/exit"
        || t == "/timestamps"
        || t == "/compact"
        || t == "/dense"
        || t == "/reset"
        || t == "/reload"
        // A bare prefix match eats real prose: "/modelling the data" is a
        // prompt, not a command. Require the word to end.
        || is_slash_word(t, "/theme")
        || is_slash_word(t, "/thinking")
        || is_slash_word(t, "/model")
}

pub(crate) fn hit(area: Rect, col: u16, row: u16) -> bool {
    col >= area.x
        && col < area.x.saturating_add(area.width)
        && row >= area.y
        && row < area.y.saturating_add(area.height)
}

/// The git tab strip is drawn in the border title, so a click on a pane's top
/// row is a click on a tab. Mirrors the span layout in `draw_git`.
pub(crate) fn git_tab_at(area: Rect, col: u16, row: u16) -> Option<side::GitTab> {
    if area.height < 3 || row != area.y {
        return None;
    }
    // Left border, then the leading `Span::raw(" ")`.
    let mut x = area.x.saturating_add(2);
    for tab in side::GitTab::ALL {
        let w = tab.label().chars().count() as u16 + 2;
        if col >= x && col < x.saturating_add(w) {
            return Some(tab);
        }
        x = x.saturating_add(w);
    }
    None
}

/// Where a chip drawn inside a pane's border title lands on screen, so a
/// click on the frame can hit it. Mirrors the heading in `draw_scrollback`:
/// left border, the leading space, then the title.
pub(crate) fn title_chip_rect(area: Rect, title: &str, chip: &str) -> Option<Rect> {
    if area.height < 3 {
        return None;
    }
    let at = title.find(chip)?;
    let off = title[..at].chars().count() as u16;
    let x = area.x.saturating_add(2).saturating_add(off);
    let w = chip.chars().count() as u16;
    (x.saturating_add(w) <= area.x.saturating_add(area.width)).then_some(Rect {
        x,
        y: area.y,
        width: w,
        height: 1,
    })
}

/// Which content row of a bordered pane a screen row lands on. `None` for the
/// two border rows, so a click on the frame never moves a cursor.
pub(crate) fn pane_row(area: Rect, row: u16) -> Option<usize> {
    let inner = area.height.checked_sub(2)?;
    if row <= area.y || row >= area.y + area.height - 1 {
        return None;
    }
    let r = (row - area.y - 1) as usize;
    (r < inner as usize).then_some(r)
}

pub(crate) fn handle_mouse(app: &mut App, m: MouseEvent) {
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
        if let Some(area) = app.calls_chip {
            if hit(area, m.column, m.row) {
                app.toggle_posts();
                return;
            }
        }
    }
    let on_calls = hit(app.call_area, m.column, m.row);
    let on_story = hit(app.traj_area, m.column, m.row);
    let on_post_in = hit(app.post_in_area, m.column, m.row);
    let on_post_out = hit(app.post_out_area, m.column, m.row);
    let on_rail = hit(app.rail_area, m.column, m.row);
    let on_queue = hit(app.queue_area, m.column, m.row) && !app.queue.is_empty();
    let on_input = hit(app.input_area, m.column, m.row);
    let on_git = hit(app.git_area, m.column, m.row);
    let on_files = hit(app.files_area, m.column, m.row);
    let on_meta = hit(app.cache.area, m.column, m.row);
    let on_slash = slash_popup_area(app.input_area, app)
        .is_some_and(|area| hit(area, m.column, m.row));

    match m.kind {
        MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
            let up = matches!(m.kind, MouseEventKind::ScrollUp);
            if on_slash && app.slash.open {
                app.slash.move_sel(if up { -1 } else { 1 });
                sync_theme_preview(app);
            } else if on_calls && app.tree_open {
                let last = tree::order(&app.children).len().saturating_sub(1);
                app.tree_sel = if up {
                    app.tree_sel.saturating_sub(3)
                } else {
                    (app.tree_sel + 3).min(last)
                };
            } else if on_calls || on_story {
                wheel_scroll(app.sess_mut().scroll(on_calls), up, 3);
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
            } else if on_git {
                app.git.select(if up { -3 } else { 3 });
                let path = app.git.selected().and_then(|r| r.path.clone());
                app.files.preview(path.as_deref());
            } else if on_files {
                let rows = app.files_area.height.saturating_sub(2) as usize;
                app.files.move_by(if up { -3 } else { 3 }, rows);
            }
        }
        MouseEventKind::Down(MouseButton::Left) => {
            if on_rail {
                app.set_focus(Focus::Rail);
                if let Some(row) = pane_row(app.rail_area, m.row) {
                    if row < rail::rows(app).len() {
                        app.rail_sel = row;
                        rail::activate(app);
                    }
                }
                return;
            }
            if on_queue {
                app.set_focus(Focus::Queue);
                if app.queue_area.height > 2 {
                    let row = m.row.saturating_sub(app.queue_area.y.saturating_add(1)) as usize;
                    let idx = app.queue.visible_skip() + row;
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
                    let row = m
                        .row
                        .saturating_sub(app.input_inner.y)
                        .saturating_add(app.input_scroll);
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
            if on_meta {
                app.set_focus(Focus::Meter);
                return;
            }
            if on_git {
                app.set_focus(Focus::Git);
                if let Some(tab) = git_tab_at(app.git_area, m.column, m.row) {
                    app.git.set_tab(tab);
                } else if let Some(row) = pane_row(app.git_area, m.row) {
                    let idx = app.git.scroll + row;
                    if idx < app.git.rows().len() {
                        app.git.sel = idx;
                    }
                }
                // Same rule as the keyboard: moving the git cursor previews.
                let path = app.git.selected().and_then(|r| r.path.clone());
                app.files.preview(path.as_deref());
                return;
            }
            if on_files {
                app.set_focus(Focus::Files);
                if let Some(row) = pane_row(app.files_area, m.row) {
                    if !app.files.in_file() {
                        let idx = app.files.scroll + row;
                        if idx < app.files.entries.len() {
                            let now = Instant::now();
                            let dbl = app
                                .last_click
                                .map(|(t, e, p)| {
                                    p == 4 && e == idx && now.duration_since(t).as_millis() < 350
                                })
                                .unwrap_or(false);
                            app.files.sel = idx;
                            if dbl {
                                app.last_click = None;
                                app.files.enter();
                            } else {
                                app.last_click = Some((now, idx, 4));
                            }
                        }
                    }
                }
                return;
            }
            if on_calls && app.tree_open {
                // The tree replaced the scrollback, so its hit-test replaces
                // the scrollback's: a click selects the row under it.
                app.set_focus(Focus::Calls);
                if let Some(row) = pane_row(app.call_area, m.row) {
                    let idx = app.tree_skip() + row;
                    if idx < tree::order(&app.children).len() {
                        app.tree_sel = idx;
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
            if on_calls || on_story || app.sess.story_text.active.is_some() || app.sess.calls_text.active.is_some()
            {
                handle_scrollback_up(app, on_calls, m.column, m.row);
            }
        }
        _ => {}
    }
}

pub(crate) fn handle_scrollback_down(app: &mut App, calls: bool, col: u16, row: u16) {
    let model = app.sess().sel(calls).clone();
    app.sess_mut().text(calls).clear();
    if let Some(hit) = model.hit_test_text_exact(col, row) {
        let width = model.visible_block_content_width(hit.entry_idx);
        {
            let sel = app.sess_mut().text(calls);
            sel.pending = Some(PendingTextDrag {
                anchor: hit,
                start_col: col,
                start_row: row,
                anchor_content_width: width,
            });
            sel.note_click(Instant::now(), hit);
        }
        app.sess_mut().scroll(calls).set_selected(Some(hit.entry_idx));
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
    app.sess_mut().scroll(calls).set_selected(Some(idx));
    if dbl {
        app.last_click = None;
        if !calls && app.open_selected_session() {
            return;
        }
        let work_summary = !calls
            && app
                .sess()
                .story
                .entry(idx)
                .is_some_and(|entry| app.sess().stream.run.detail(entry.id).is_some());
        if work_summary && app.open_block_viewer() {
            return;
        }
        if calls {
            pin_selected_wire(app);
        }
        app.sess_mut().scroll(calls).toggle_fold_selected();
    } else {
        app.last_click = Some((now, idx, pane));
    }
}

pub(crate) fn handle_scrollback_drag(app: &mut App, calls: bool, col: u16, row: u16) {
    let model = app.sess().sel(calls).clone();
    let sel = app.sess_mut().text(calls);
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

pub(crate) fn handle_scrollback_up(app: &mut App, calls: bool, _col: u16, _row: u16) {
    let model = app.sess().sel(calls).clone();
    let copied = {
        let sel = app.sess_mut().text(calls);
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
        app.notify("copied");
    }
}

pub(crate) fn handle_viewer_key(app: &mut App, key: KeyEvent) {
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
            app.notify("copied");
        }
    }
}

pub(crate) fn handle_viewer_mouse(app: &mut App, m: MouseEvent) {
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
        app.notify("copied");
    }
}

pub(crate) fn handle_post_inspect_key(app: &mut App, key: KeyEvent) {
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
            app.notify("copied");
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

pub(crate) fn handle_post_inspect_mouse(app: &mut App, m: MouseEvent) {
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

/// Wheel/page only after prepare_layout has a real viewport. scroll_down
/// with viewport_height=0 uses max_offset=total_height and walks off the end.
pub(crate) fn wheel_scroll(sb: &mut ScrollbackState, up: bool, rows: u16) {
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
