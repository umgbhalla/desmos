//! The root draw tree: the top-level `draw` entry point that lays out the
//! frame, and `draw_scrollback` for the Story/Activity panes. Moved verbatim
//! out of `main.rs` (tui-redesign R7 slice 3). Pure move: no logic edits.

#![allow(clippy::wildcard_imports)]

use crate::*;

fn hovered_entry(model: &ResolvedSelectionModel, mouse: Option<(u16, u16)>) -> Option<usize> {
    let (col, row) = mouse?;
    model.hit_test_visible_block(col, row).map(|g| g.entry_idx)
}

/// Fold one message body to the pane width without a wrapping widget, so the
/// tail can be kept exactly: a channel scrolled to the bottom is the only
/// useful place to stand in it.
fn wrap_body(text: &str, width: usize) -> Vec<String> {
    let mut out = Vec::new();
    for raw in text.split('\n') {
        let mut line = String::new();
        for word in raw.split_whitespace() {
            if !line.is_empty() && line.chars().count() + 1 + word.chars().count() > width {
                out.push(std::mem::take(&mut line));
            }
            if !line.is_empty() {
                line.push(' ');
            }
            line.push_str(word);
        }
        out.push(line);
    }
    out
}

/// Draw the channel the rail selected, in place of the agent's own story.
/// One wrapped row split so that `@name` carries its own colour. A mention is
/// the only token in a channel that does something -- it dispatches work to
/// another machine -- so it should not read like the rest of the sentence.
fn mention_spans(row: &str, base: Style, accent: Style) -> Vec<Span<'static>> {
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut plain = String::new();
    for word in row.split_inclusive(' ') {
        let token = word.trim_end_matches(' ');
        let name = token.trim_end_matches(|c: char| !c.is_alphanumeric() && c != '_' && c != '-');
        if name.len() > 1 && name.starts_with('@') {
            if !plain.is_empty() {
                spans.push(Span::styled(std::mem::take(&mut plain), base));
            }
            spans.push(Span::styled(name.to_string(), accent));
            plain.push_str(&word[name.len()..]);
        } else {
            plain.push_str(word);
        }
    }
    if !plain.is_empty() {
        spans.push(Span::styled(plain, base));
    }
    spans
}

pub(crate) fn draw_channel(f: &mut Frame, area: Rect, view: &mut ChannelView, focused: bool) {
    let theme = Theme::current();
    let border = if focused {
        theme.accent_assistant
    } else {
        theme.bg_base
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border))
        .title(Span::styled(
            // Say when the pane is not showing the live tail. Without this a
            // channel read as silent while the new messages were below.
            if view.scroll > 0 {
                format!(" #{} ↑{} ", view.name, view.scroll)
            } else {
                format!(" #{} ", view.name)
            },
            Style::default()
                .fg(theme.accent_assistant)
                .add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    let inner = block.inner(area);
    f.render_widget(block, area);
    let width = (inner.width as usize).max(8);
    let mut lines: Vec<Line> = Vec::new();
    for message in &view.messages {
        if !lines.is_empty() {
            lines.push(Line::from(""));
        }
        let mut head = vec![Span::styled(
            message.author.clone(),
            Style::default()
                .fg(theme.accent_tool)
                .add_modifier(Modifier::BOLD),
        )];
        if !message.ts.is_empty() {
            head.push(Span::styled(
                format!("  {}", message.ts),
                Style::default()
                    .fg(theme.text_secondary)
                    .add_modifier(Modifier::DIM),
            ));
        }
        lines.push(Line::from(head));
        for row in wrap_body(&message.body, width) {
            lines.push(Line::from(mention_spans(
                &row,
                Style::default().fg(theme.text_primary),
                Style::default()
                    .fg(theme.accent_user)
                    .add_modifier(Modifier::BOLD),
            )));
        }
    }
    if lines.is_empty() {
        lines.push(Line::from(Span::styled(
            "no messages yet — type to post here",
            Style::default()
                .fg(theme.text_secondary)
                .add_modifier(Modifier::DIM),
        )));
    }
    let height = inner.height as usize;
    // Only the renderer knows how many wrapped rows the messages became, so
    // the clamp lives here rather than in the key handler.
    view.scroll = view.scroll.min(lines.len().saturating_sub(height));
    if lines.len() > height {
        let end = lines.len() - view.scroll;
        lines.truncate(end);
        lines.drain(..end.saturating_sub(height));
    }
    f.render_widget(Paragraph::new(lines), inner);
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

fn meta_id(app: &App) -> MetaId {
    let dash = |s: &str| {
        if s.is_empty() {
            "—".to_string()
        } else {
            s.to_string()
        }
    };
    if let Some(remote) = app.remote_workspace.as_ref() {
        return MetaId {
            model: dash(&remote.model),
            effort: dash(&remote.thinking),
            generation: if remote.generation == 0 {
                "—".into()
            } else {
                remote.generation.to_string()
            },
            pending: None,
            theme: Theme::current_kind().display_name().to_string(),
            session: Some(format!("{} @{}", if remote.loaded { "remote" } else { "loading" }, remote.seat)),
            background: Vec::new(),
        };
    }
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

pub(crate) fn draw(f: &mut Frame, app: &mut App) {
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

    // The navigator is a real outer pane, but narrow terminals keep every
    // column for the transcript and composer.
    let frame_area = f.area();
    let work_area = if frame_area.width >= 90 {
        let outer = Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Length(26), Constraint::Min(1)])
            .split(frame_area);
        app.rail_area = outer[0];
        rail::draw(f, outer[0], app);
        outer[1]
    } else {
        app.rail_area = Rect::default();
        if app.focus == Focus::Rail {
            app.focus = Focus::Story;
        }
        frame_area
    };

    // Columns first, because the composer wraps at the story column's width.
    let body = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage(100 - app.layout.wire_pct),
            Constraint::Percentage(app.layout.wire_pct),
        ])
        .split(work_area);

    // The composer frame shares the Story column edges; only its own border is
    // removed when measuring wrapped text.
    let inner_w = body[0].width.saturating_sub(2);
    let queue_h = pending_input_height(app);
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
    let decision_h =
        app.decisions.len().min(3) as u16 + u16::from(app.decisions.len() > 3);
    // Grow with what is typed, up to half the column. The old ceiling of ten
    // rows existed to leave a legend band matching it opposite; there is no
    // legend now, and a long prompt is worth more rows than a short story is.
    let cap = (body[0].height / 2).saturating_sub(3).max(2);
    let default_rows = COMPOSER_DEFAULT_ROWS.min(cap);
    let prompt_rows = app.prompt.display_rows(inner_w).clamp(default_rows, cap);
    let input_h = (2 + float_rows + prompt_rows)
        .min(work_area.height.saturating_sub(8 + queue_h))
        .max(2 + float_rows + default_rows);
    let bottom_h = queue_h + decision_h + input_h;
    // Standing in a channel, the POST cards describe a conversation you are
    // not looking at -- and they were taking a third of the column, leaving
    // the channel nine rows on a forty-row terminal. A channel is the whole
    // place while you are in it.
    let post_h = if app.channel_view.is_some() || app.remote_workspace.is_some() {
        0
    } else {
        app.layout
            .post_h
            .min(body[0].height.saturating_sub(bottom_h + 3))
    };
    let left = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(3),
            Constraint::Length(post_h),
            Constraint::Length(queue_h),
            Constraint::Length(decision_h),
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
    let decision_area = left[3];
    app.input_area = left[4];

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
    } else if let Some(remote) = app.remote_workspace.as_mut() {
        let story_title = format!("Story @{}", remote.seat);
        draw_scrollback(
            f,
            panes[0],
            &mut remote.sess.story,
            &mut remote.sess.story_scratch,
            &mut remote.sess.story_sel,
            &story_title,
            theme.accent_assistant,
            app.focus == Focus::Story,
            app.mouse,
            &remote.sess.story_text,
            &mut app.media.frame,
        );
        if !app.tree_open {
            draw_scrollback(
                f,
                panes[1],
                &mut remote.sess.calls,
                &mut remote.sess.calls_scratch,
                &mut remote.sess.calls_sel,
                &calls_title,
                theme.accent_tool,
                app.focus == Focus::Calls,
                app.mouse,
                &remote.sess.calls_text,
                &mut app.media.frame,
            );
        }
    } else if let Some(view) = app.channel_view.as_mut() {
        draw_channel(f, panes[0], view, app.focus == Focus::Story);
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
    let meter = app
        .remote_workspace
        .as_ref()
        .map(|remote| &remote.cache)
        .unwrap_or(&app.cache);
    draw_meta(
        f,
        app.cache.area,
        meter,
        app.focus == Focus::Meter,
        &ident,
    );
    draw_git(f, app.git_area, app);
    draw_files(f, app.files_area, app);
    draw_queue(f, app.queue_area, app);
    draw_decisions(f, decision_area, app);
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

fn pending_input_height(app: &App) -> u16 {
    let rows = pending_input_rows(app);
    if rows == 0 { 0 } else { rows as u16 + 2 }
}
