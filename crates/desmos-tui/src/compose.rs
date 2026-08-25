//! The composer input box and its overlays (help cheatsheet, fuzzy file
//! picker, slash completion, paste preview), moved verbatim out of `main.rs`
//! (tui-redesign R7 slice 2). Pure move: no logic edits.

#![allow(clippy::wildcard_imports)]

use crate::*;

pub(crate) fn draw_input(f: &mut Frame, area: Rect, app: &mut App) {
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
    let (mut signal_label, mut signal_color) = match signal {
        Some(InputSignal::Inference) => (
            Some("enter steer · tab queue".to_string()),
            theme.accent_assistant,
        ),
        // No count here. The queue pane above already titles itself "Queue N"
        // and lists every item; repeating the number on the composer border put
        // "Queue 1" and "Queued 1" one row apart.
        Some(InputSignal::Queued) => (Some("Queued".to_string()), theme.accent_user),
        Some(InputSignal::Tool) if app.running => (
            Some("enter steer · tab queue".to_string()),
            theme.accent_tool,
        ),
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
    // Standing in a channel, Enter posts there: it does not steer the agent
    // and it does not queue. "tab queue" was a lie about where the text lands.
    if let Some(view) = app.channel_view.as_ref() {
        signal_label = Some(format!("enter posts to #{} · esc leaves", view.name));
        signal_color = theme.accent_user;
    }
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
pub(crate) fn draw_help(f: &mut Frame, app: &App) {
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
pub(crate) fn draw_file_picker(f: &mut Frame, app: &mut App) {
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
            if app.slash.is_mention() {
                " @ people ".to_string()
            } else {
                format!(" {mark} commands ")
            },
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
