//! Startup choice for a persisted harness transcript.

use std::path::{Path, PathBuf};
use std::process::Command;

use crossterm::event::KeyCode;
use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, List, ListItem, ListState, Paragraph};

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Turn {
    pub(crate) prompt: String,
    pub(crate) speech: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Choice {
    New,
    Resume(String),
}

/// One resumable line of descent.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct SessionRow {
    pub(crate) id: String,
    pub(crate) started_at: String,
    pub(crate) messages: usize,
    pub(crate) preview: String,
}

#[derive(Debug, Default)]
pub(crate) struct SessionPicker {
    pub(crate) open: bool,
    sessions: Vec<SessionRow>,
    turns: Vec<Turn>,
    selected: usize,
    source: Option<PathBuf>,
}

impl SessionPicker {
    pub(crate) fn discover(cwd: &Path) -> Self {
        let source = cwd.join(".desmos").join("harness.sqlite3");
        let sessions = load_sessions(&source).unwrap_or_default();
        Self {
            open: !sessions.is_empty(),
            sessions,
            turns: Vec::new(),
            selected: 0,
            source: Some(source),
        }
    }

    #[cfg(test)]
    pub(crate) fn with_sessions(sessions: Vec<SessionRow>, turns: Vec<Turn>) -> Self {
        Self {
            open: !sessions.is_empty(),
            sessions,
            turns,
            selected: 0,
            source: None,
        }
    }

    pub(crate) fn key(&mut self, code: KeyCode) -> Option<Choice> {
        if !self.open {
            return None;
        }
        let last = self.sessions.len();
        match code {
            KeyCode::Up | KeyCode::Char('k') => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down | KeyCode::Char('j') => self.selected = (self.selected + 1).min(last),
            KeyCode::Char('n') => return self.close(Choice::New),
            KeyCode::Char('r') => {
                let choice = self.resume(self.selected.min(last.saturating_sub(1)))?;
                return self.close(choice);
            }
            KeyCode::Enter => {
                let choice = self.resume(self.selected).unwrap_or(Choice::New);
                return self.close(choice);
            }
            _ => {}
        }
        None
    }

    /// Name the chosen line and load its turns. The row past the last session
    /// is "new session", which has nothing to resume.
    fn resume(&mut self, index: usize) -> Option<Choice> {
        let row = self.sessions.get(index)?.clone();
        if let Some(path) = self.source.clone() {
            self.turns = load_turns(&path, &row.id).unwrap_or_default();
        }
        Some(Choice::Resume(row.id))
    }

    fn close(&mut self, choice: Choice) -> Option<Choice> {
        self.open = false;
        Some(choice)
    }

    /// Roster UX: never ask which transcript. The newest session is main;
    /// open on it, or report None on a fresh workspace so the caller starts
    /// a new session.
    pub(crate) fn auto_resume(&mut self) -> Option<Choice> {
        if !self.open {
            return None;
        }
        let choice = self.resume(0)?;
        self.close(choice)
    }

    pub(crate) fn resumed_turns(&self) -> &[Turn] {
        &self.turns
    }

    pub(crate) fn render(&self, frame: &mut Frame) {
        let outer = centered(frame.area(), 76, 18);
        frame.render_widget(Clear, outer);
        let block = Block::default().title(" session ").borders(Borders::ALL);
        let inner = block.inner(outer);
        frame.render_widget(block, outer);
        let rows = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(3),
                Constraint::Min(2),
                Constraint::Length(2),
            ])
            .split(inner);
        let count = self.sessions.len();
        let path = self
            .source
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| "saved transcript".into());
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(Span::styled(
                    "Which session?",
                    Style::default().add_modifier(Modifier::BOLD),
                )),
                Line::from(format!(
                    "{count} resumable session{} · {path}",
                    if count == 1 { "" } else { "s" }
                )),
            ])
            .alignment(Alignment::Center),
            rows[0],
        );
        let mut items: Vec<ListItem> = self
            .sessions
            .iter()
            .map(|row| {
                let when = row.started_at.get(..16).unwrap_or(&row.started_at);
                let preview: String = row.preview.chars().take(30).collect();
                ListItem::new(format!(" {when}  {:>4} msg  {preview}", row.messages))
            })
            .collect();
        items.push(ListItem::new(" New session"));
        let mut state = ListState::default().with_selected(Some(self.selected));
        frame.render_stateful_widget(
            List::new(items)
                .highlight_symbol("›")
                .highlight_style(Style::default().add_modifier(Modifier::BOLD)),
            rows[1],
            &mut state,
        );
        frame.render_widget(
            Paragraph::new("↑/↓ choose  enter select  r resume  n new")
                .alignment(Alignment::Center),
            rows[2],
        );
    }
}

fn centered(area: Rect, width: u16, height: u16) -> Rect {
    let width = width.min(area.width);
    let height = height.min(area.height);
    Rect::new(
        area.x + area.width.saturating_sub(width) / 2,
        area.y + area.height.saturating_sub(height) / 2,
        width,
        height,
    )
}

const SESSIONS_SQL: &str = "SELECT id, started_at, messages, preview FROM (\
SELECT s.id AS id, s.started_at AS started_at, \
(SELECT count(*) FROM messages m WHERE m.session_id = s.id) AS messages, \
coalesce((SELECT p.prompt FROM prior_turns p WHERE p.session_id = s.id \
ORDER BY p.seq DESC LIMIT 1), '') AS preview FROM sessions s) \
WHERE messages > 0 ORDER BY started_at DESC LIMIT 12";

/// An id reaches SQL as a literal, so refuse anything that is not one of ours.
fn safe_id(id: &str) -> bool {
    !id.is_empty() && id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
}

fn query(path: &Path, sql: &str) -> Option<serde_json::Value> {
    if !path.is_file() {
        return None;
    }
    let out = Command::new("sqlite3")
        .args(["-json", path.to_str()?, sql])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    serde_json::from_slice::<serde_json::Value>(&out.stdout).ok()
}

fn load_sessions(path: &Path) -> Option<Vec<SessionRow>> {
    let rows = query(path, SESSIONS_SQL)?;
    Some(
        rows.as_array()?
            .iter()
            .filter_map(|row| {
                Some(SessionRow {
                    id: row.get("id")?.as_str()?.to_owned(),
                    started_at: row.get("started_at")?.as_str().unwrap_or("").to_owned(),
                    messages: row.get("messages")?.as_u64().unwrap_or(0) as usize,
                    preview: row
                        .get("preview")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_owned(),
                })
            })
            .collect(),
    )
}

fn load_turns(path: &Path, session: &str) -> Option<Vec<Turn>> {
    if !safe_id(session) {
        return None;
    }
    let sql =
        format!("SELECT prompt, speech FROM prior_turns WHERE session_id='{session}' ORDER BY seq");
    let rows = query(path, &sql)?;
    Some(
        rows.as_array()?
            .iter()
            .filter_map(|row| {
                Some(Turn {
                    prompt: row.get("prompt")?.as_str()?.to_owned(),
                    speech: row.get("speech")?.as_str()?.to_owned(),
                })
            })
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(id: &str) -> SessionRow {
        SessionRow {
            id: id.into(),
            started_at: "2026-08-17T04:00:00".into(),
            messages: 3,
            preview: "hello".into(),
        }
    }

    #[test]
    fn enter_resumes_the_selected_session_by_id() {
        let mut picker = SessionPicker::with_sessions(vec![row("0191aaaa")], Vec::new());
        assert!(picker.open);
        assert_eq!(
            picker.key(KeyCode::Enter),
            Some(Choice::Resume("0191aaaa".into()))
        );
    }

    #[test]
    fn new_session_has_a_direct_choice() {
        let mut picker = SessionPicker::with_sessions(vec![row("0191aaaa")], Vec::new());
        assert_eq!(picker.key(KeyCode::Char('n')), Some(Choice::New));
        assert!(!picker.open);
    }

    #[test]
    fn the_row_past_the_last_session_is_new() {
        let mut picker = SessionPicker::with_sessions(vec![row("0191aaaa")], Vec::new());
        picker.key(KeyCode::Down);
        assert_eq!(picker.key(KeyCode::Enter), Some(Choice::New));
    }

    #[test]
    fn no_database_preserves_direct_launch() {
        let picker = SessionPicker::discover(Path::new("/path/that/does/not/exist"));
        assert!(!picker.open);
    }
}
