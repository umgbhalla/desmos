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
    Header,
    Root,
    Child(String),
    Background,
    Agent(String),
    Channel(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Row {
    pub(crate) target: Target,
    pub(crate) label: String,
    pub(crate) presence: Presence,
}

fn header(label: &str) -> Row {
    Row {
        target: Target::Header,
        label: label.into(),
        presence: Presence::FinishedSeen,
    }
}

pub(crate) fn rows(app: &App) -> Vec<Row> {
    let mut rows = vec![
        header("agents"),
        Row {
            target: Target::Root,
            label: "main".into(),
            presence: if app.running {
                Presence::Running
            } else {
                Presence::NeedsMe
            },
        },
    ];
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
    // Bots are roster rows, not spawn rows: hyperion is in the rail because
    // the database lists it, whether or not this session has ever talked to
    // it. Liveness comes from presence, so a dark daemon reads as dark.
    rows.extend(app.agents.iter().filter(|a| a.kind == "bot").map(|bot| Row {
        target: Target::Agent(bot.name.clone()),
        label: format!("@{}", bot.name),
        presence: if bot.live {
            Presence::Running
        } else {
            Presence::FinishedSeen
        },
    }));
    rows.extend(app.background.iter().cloned().map(|label| Row {
        target: Target::Background,
        label,
        presence: Presence::Running,
    }));
    rows.push(header("channels"));
    rows.extend(app.channels.iter().filter(|c| c.kind != "sys").map(|channel| Row {
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

fn enter_channel(app: &mut App, name: String) {
    // Paint the destination immediately instead of leaving the old story on
    // screen during the bridge round trip. Keep an already-open view intact;
    // the channel_story response replaces it with the fresh durable tail.
    if app.channel_view.as_ref().is_none_or(|view| view.name != name) {
        app.channel_view = Some(crate::ChannelView {
            name: name.clone(),
            messages: Vec::new(),
            scroll: 0,
        });
    }
    app.pending_channel_read = Some(name);
    app.focus = Focus::Input;
}

pub(crate) fn activate(app: &mut App) {
    let Some(row) = rows(app).get(app.rail_sel).cloned() else {
        return;
    };
    match row.target {
        Target::Header => {}
        Target::Root => {
            app.viewing = None;
            app.channel_view = None;
            app.focus = Focus::Story;
        }
        Target::Child(id) => {
            app.ensure_child(&id, "");
            app.rail_seen.insert(id.clone());
            app.viewing = Some(id);
            app.focus = Focus::Story;
        }
        Target::Background => {}
        // A rail is navigation, not text input. Selecting a person opens their
        // DM; selecting a channel opens that channel. Neither is allowed to
        // mutate a draft -- Slack does not type @alice because you clicked
        // Alice, and repeated clicks must be idempotent.
        Target::Agent(name) => enter_channel(app, name),
        Target::Channel(channel) => enter_channel(app, channel),
    }
}

/// Move the rail cursor one selectable row, skipping section headers.
pub(crate) fn step(app: &mut App, down: bool) {
    let rows = rows(app);
    if rows.is_empty() {
        return;
    }
    let mut i = app.rail_sel.min(rows.len() - 1);
    loop {
        if down {
            if i + 1 >= rows.len() {
                return;
            }
            i += 1;
        } else {
            if i == 0 {
                return;
            }
            i -= 1;
        }
        if !matches!(rows[i].target, Target::Header) {
            app.rail_sel = i;
            return;
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
            " Roster ",
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
            if row.target == Target::Header {
                return Line::from(Span::styled(
                    format!(" {} ", row.label.to_uppercase()),
                    Style::default()
                        .fg(theme.text_secondary)
                        .add_modifier(Modifier::DIM | Modifier::BOLD),
                ));
            }
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
    fn roster_agents_paint_bots_and_selecting_one_opens_its_dm() {
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({
                "ev": "agents",
                "agents": [
                    {"name": "main", "kind": "chief", "host": "", "live": true},
                    {"name": "hyperion", "kind": "bot", "host": "hyperion", "live": true},
                    {"name": "darkbot", "kind": "bot", "host": "dark", "live": false}
                ]
            }),
        );
        let got = rows(&app);
        // The chief is the local session row; the roster must not double it.
        assert_eq!(got.iter().filter(|r| r.label == "main").count(), 1);
        let bots: Vec<_> = got
            .iter()
            .filter(|r| matches!(r.target, Target::Agent(_)))
            .map(|r| (r.label.clone(), r.presence))
            .collect();
        assert_eq!(
            bots,
            vec![
                ("@hyperion".to_string(), Presence::Running),
                ("@darkbot".to_string(), Presence::FinishedSeen),
            ]
        );
        app.rail_sel = got.iter().position(|r| r.label == "@hyperion").unwrap();
        app.prompt.insert_str("draft stays mine");
        activate(&mut app);
        assert_eq!(app.prompt.to_send(), "draft stays mine");
        assert_eq!(app.focus, Focus::Input);
        assert_eq!(app.channel_view.as_ref().map(|v| v.name.as_str()), Some("hyperion"));
        assert_eq!(app.pending_channel_read.as_deref(), Some("hyperion"));

        // Re-entering the same DM is navigation, not another edit or an empty
        // repaint while the fresh tail is in flight.
        app.channel_view.as_mut().unwrap().messages.push(crate::ChannelMsg {
            author: "peer".into(), body: "still visible".into(), ts: String::new(),
        });
        activate(&mut app);
        assert_eq!(app.prompt.to_send(), "draft stays mine");
        assert_eq!(app.channel_view.as_ref().unwrap().messages.len(), 1);
    }

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
                (Target::Header, Presence::FinishedSeen),
                (Target::Root, Presence::NeedsMe),
                (Target::Child("first".into()), Presence::FinishedUnseen),
                (Target::Child("second".into()), Presence::Running),
                (Target::Background, Presence::Running),
                (Target::Header, Presence::FinishedSeen),
            ]
        );

        app.rail_sel = 2;
        activate(&mut app);
        assert_eq!(app.viewing.as_deref(), Some("first"));
        assert_eq!(rows(&app)[2].presence, Presence::FinishedSeen);

        // The stepper never lands on a header: from main it walks to the
        // first child, and upward from main it stays put.
        app.rail_sel = 1;
        step(&mut app, true);
        assert_eq!(app.rail_sel, 2);
        app.rail_sel = 1;
        step(&mut app, false);
        assert_eq!(app.rail_sel, 1);
    }
}
