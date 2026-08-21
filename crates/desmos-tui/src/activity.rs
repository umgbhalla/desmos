//! The activity tab's draw tree, moved verbatim out of `main.rs`
//! (tui-redesign R7 slice 1). Pure move: no logic edits.

#![allow(clippy::wildcard_imports)]

use crate::*;

pub(crate) fn draw_viewer(f: &mut Frame, app: &mut App) {
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

pub(crate) fn draw_post_inspect(f: &mut Frame, app: &mut App) {
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

pub(crate) fn draw_decisions(f: &mut Frame, area: Rect, app: &App) {
    if area.height == 0 || area.width == 0 {
        return;
    }
    let theme = Theme::current();
    let mut lines = app
        .decisions
        .iter()
        .take(3)
        .map(|decision| {
            let short_id = decision.id.chars().take(8).collect::<String>();
            let mut text = format!("◐ decide:{short_id} {}", decision.prompt);
            for (index, option) in decision.options.iter().take(9).enumerate() {
                text.push_str(&format!(" [{}] {}", index + 1, option));
            }
            Line::from(Span::styled(
                text,
                Style::default()
                    .fg(theme.warning)
                    .bg(theme.bg_base)
                    .add_modifier(Modifier::BOLD),
            ))
        })
        .collect::<Vec<_>>();
    if app.decisions.len() > 3 {
        lines.push(Line::from(Span::styled(
            format!("  +{} more", app.decisions.len() - 3),
            Style::default().fg(theme.accent_tool).bg(theme.bg_base),
        )));
    }
    f.render_widget(
        Paragraph::new(lines).style(Style::default().bg(theme.bg_base)),
        area,
    );
}

pub(crate) fn draw_json_tree(
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

/// Git state as a tab strip over rows — druk's sidebar, with the views it
/// makes sense to have beside a wire pane. The strip is drawn in the border
/// title so it costs no row of its own.
pub(crate) fn draw_git(f: &mut Frame, area: Rect, app: &mut App) {
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
pub(crate) fn draw_files(f: &mut Frame, area: Rect, app: &mut App) {
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

pub(crate) fn draw_meta(f: &mut Frame, area: Rect, meter: &CacheMeter, focused: bool, id: &MetaId) {
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

pub(crate) fn draw_queue(f: &mut Frame, area: Rect, app: &App) {
    if area.height == 0 || pending_input_rows(app) == 0 {
        return;
    }
    let theme = Theme::current();
    let focused = app.focus == Focus::Queue;
    let border = if focused {
        theme.accent_user
    } else {
        theme.bg_base
    };
    let title = match (app.pending_steers.len(), app.queue.len()) {
        (0, queued) => format!(" Queue  {queued} "),
        (steers, 0) => format!(" Steer pending  {steers} "),
        (steers, queued) => format!(" Steer {steers} · Queue {queued} "),
    };
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
    let steer_rows = pending_steer_rows(app);
    let mut lines: Vec<Line<'static>> = app
        .pending_steers
        .iter()
        .skip(app.pending_steers.len().saturating_sub(steer_rows))
        .map(|text| {
            let first = text
                .lines()
                .map(str::trim)
                .find(|line| !line.is_empty())
                .unwrap_or("");
            Line::from(vec![
                Span::styled("↪ ", Style::default().fg(theme.gray)),
                Span::styled(
                    queue::truncate_width(first, inner.width.saturating_sub(2) as usize),
                    Style::default().fg(theme.accent_assistant),
                ),
            ])
        })
        .collect();
    let room = PENDING_INPUT_MAX_ROWS.saturating_sub(lines.len());
    let mut queued = app.queue.lines(inner.width, focused);
    if queued.len() > room {
        queued.drain(..queued.len() - room);
    }
    lines.extend(queued);
    f.render_widget(Paragraph::new(lines), inner);
}

/// The run tree over the Activity column: one row per subagent run, nested by
/// the kernel's own parent/depth, fed purely from events. A list pane in the
/// queue's shape — the rows come from `tree::row_text`, not a second renderer.
pub(crate) fn draw_tree_pane(f: &mut Frame, area: Rect, app: &mut App) {
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
