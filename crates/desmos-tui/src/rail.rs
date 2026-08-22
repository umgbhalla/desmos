//! Compact session/worker navigator shown to the left of the workspace.

use ratatui::{
    Frame,
    layout::Rect,
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Paragraph},
};

use crate::{App, Focus, Theme};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Presence {
    Running,
    NeedsMe,
    FinishedUnseen,
    FinishedSeen,
}

impl Presence {
    fn glyph(self) -> &'static str {
        match self {
            Self::Running => "●",
            Self::NeedsMe => "◐",
            Self::FinishedUnseen | Self::FinishedSeen => "○",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Target {
    Root,
    Child(String),
    Background,
    Channel(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Row {
    pub(crate) target: Target,
    pub(crate) label: String,
    pub(crate) presence: Presence,
}

pub(crate) fn rows(app: &App) -> Vec<Row> {
    let mut rows = vec![Row {
        target: Target::Root,
        label: "root session".into(),
        presence: if app.running {
            Presence::Running
        } else {
            Presence::NeedsMe
        },
    }];
    let mut children: Vec<_> = app.children.iter().collect();
    children.sort_by_key(|(_, child)| child.seq);
    rows.extend(children.into_iter().map(|(id, child)| {
        let pending = child.op_sent.is_some()
            || child.stage.contains("prompt")
            || child.stage.contains("intervention");
        let presence = if pending {
            Presence::NeedsMe
        } else if child.state == "running" {
            Presence::Running
        } else if app.rail_seen.contains(id) {
            Presence::FinishedSeen
        } else {
            Presence::FinishedUnseen
        };
        let mut label = if child.agent.is_empty() {
            id.clone()
        } else {
            child.agent.clone()
        };
        if !child.stage.is_empty() {
            label.push_str(&format!(" · {}", child.stage));
        }
        if child.turns > 0 {
            label.push_str(&format!(" · {}", child.turns));
        }
        if child.state != "running" {
            let verdict = match child.accepted {
                Some(true) => "accepted",
                Some(false) => "rejected",
                None => child.state.as_str(),
            };
            if !verdict.is_empty() {
                label.push_str(&format!(" · {verdict}"));
            }
        }
        Row {
            target: Target::Child(id.clone()),
            label,
            presence,
        }
    }));
    rows.extend(app.background.iter().cloned().map(|label| Row {
        target: Target::Background,
        label,
        presence: Presence::Running,
    }));
    rows.extend(app.channels.iter().map(|channel| Row {
        target: Target::Channel(channel.channel.clone()),
        label: if channel.unread > 0 {
            format!("#{}  {}", channel.channel, channel.unread)
        } else {
            format!("#{}", channel.channel)
        },
        presence: if channel.unread > 0 {
            Presence::FinishedUnseen
        } else {
            Presence::FinishedSeen
        },
    }));
    rows
}

pub(crate) fn activate(app: &mut App) {
    let Some(row) = rows(app).get(app.rail_sel).cloned() else {
        return;
    };
    match row.target {
        Target::Root => {
            app.viewing = None;
            app.focus = Focus::Story;
        }
        Target::Child(id) => {
            app.ensure_child(&id, "");
            app.rail_seen.insert(id.clone());
            app.viewing = Some(id);
            app.focus = Focus::Story;
        }
        Target::Background => {}
        Target::Channel(channel) => {
            app.pending_channel_read = Some(channel);
        }
    }
}

pub(crate) fn draw(f: &mut Frame, area: Rect, app: &mut App) {
    let theme = Theme::current();
    let focused = app.focus == Focus::Rail;
    let rows = rows(app);
    app.rail_sel = app.rail_sel.min(rows.len().saturating_sub(1));
    let border = if focused {
        theme.accent_tool
    } else {
        theme.bg_base
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(border))
        .title(Span::styled(
            " Sessions ",
            Style::default()
                .fg(theme.accent_tool)
                .add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(theme.bg_base).fg(theme.text_primary));
    let inner = block.inner(area);
    f.render_widget(block, area);
    let lines = rows
        .iter()
        .enumerate()
        .take(inner.height as usize)
        .map(|(i, row)| {
            let selected = focused && i == app.rail_sel;
            let mut style = Style::default().fg(match row.presence {
                Presence::Running => theme.accent_success,
                Presence::NeedsMe => theme.warning,
                Presence::FinishedUnseen => theme.text_primary,
                Presence::FinishedSeen => theme.text_secondary,
            });
            if row.presence == Presence::FinishedSeen {
                style = style.add_modifier(Modifier::DIM);
            }
            if row.presence == Presence::FinishedUnseen {
                style = style.bg(theme.bg_highlight);
            }
            if selected {
                style = style.bg(theme.bg_highlight).add_modifier(Modifier::BOLD);
            }
            Line::from(vec![
                Span::styled(format!("{} ", row.presence.glyph()), style),
                Span::styled(row.label.clone(), style),
            ])
        });
    f.render_widget(Paragraph::new(lines.collect::<Vec<_>>()), inner);
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;
    use crate::events::handle_event;

    #[test]
    fn real_subagent_events_drive_presence_rows_in_spawn_order() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({
                "ev": "subagent", "phase": "started", "id": "first",
                "agent": "explore", "task": "first task"
            }),
        );
        handle_event(
            &mut app,
            json!({
                "ev": "subagent", "phase": "started", "id": "second",
                "agent": "edit", "task": "second task"
            }),
        );
        handle_event(
            &mut app,
            json!({
                "ev": "subagent", "phase": "done", "id": "first",
                "stage": "terminal", "turns": 3, "accepted": true
            }),
        );
        app.background.push("index repository".into());

        let got: Vec<_> = rows(&app)
            .into_iter()
            .map(|row| (row.target, row.presence))
            .collect();
        assert_eq!(
            got,
            vec![
                (Target::Root, Presence::NeedsMe),
                (Target::Child("first".into()), Presence::FinishedUnseen),
                (Target::Child("second".into()), Presence::Running),
                (Target::Background, Presence::Running),
            ]
        );

        app.rail_sel = 1;
        activate(&mut app);
        assert_eq!(app.viewing.as_deref(), Some("first"));
        assert_eq!(rows(&app)[1].presence, Presence::FinishedSeen);
    }
}
