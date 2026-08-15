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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Choice {
    New,
    Resume,
}

#[derive(Debug, Default)]
pub(crate) struct SessionPicker {
    pub(crate) open: bool,
    turns: Vec<Turn>,
    selected: usize,
    source: Option<PathBuf>,
}

impl SessionPicker {
    pub(crate) fn discover(cwd: &Path) -> Self {
        let source = cwd.join(".desmos").join("harness.sqlite3");
        let turns = load_turns(&source).unwrap_or_default();
        Self {
            open: !turns.is_empty(),
            turns,
            selected: 0,
            source: (!source.as_os_str().is_empty()).then_some(source),
        }
    }

    #[cfg(test)]
    pub(crate) fn with_turns(turns: Vec<Turn>) -> Self {
        Self {
            open: !turns.is_empty(),
            turns,
            selected: 0,
            source: None,
        }
    }

    pub(crate) fn key(&mut self, code: KeyCode) -> Option<Choice> {
        if !self.open {
            return None;
        }
        match code {
            KeyCode::Up | KeyCode::Char('k') => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down | KeyCode::Char('j') => self.selected = (self.selected + 1).min(1),
            KeyCode::Char('n') => return self.close(Choice::New),
            KeyCode::Char('r') => return self.close(Choice::Resume),
            KeyCode::Enter => {
                return self.close(if self.selected == 0 {
                    Choice::Resume
                } else {
                    Choice::New
                });
            }
            _ => {}
        }
        None
    }

    fn close(&mut self, choice: Choice) -> Option<Choice> {
        self.open = false;
        Some(choice)
    }

    pub(crate) fn resumed_turns(&self) -> &[Turn] {
        &self.turns
    }

    pub(crate) fn render(&self, frame: &mut Frame) {
        let outer = centered(frame.area(), 58, 11);
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
        let count = self.turns.len();
        let path = self
            .source
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| "saved transcript".into());
        frame.render_widget(
            Paragraph::new(vec![
                Line::from(Span::styled(
                    "Continue where you left off?",
                    Style::default().add_modifier(Modifier::BOLD),
                )),
                Line::from(format!(
                    "{count} saved turn{} · {path}",
                    if count == 1 { "" } else { "s" }
                )),
            ])
            .alignment(Alignment::Center),
            rows[0],
        );
        let items = [
            ListItem::new(" Resume transcript"),
            ListItem::new(" New session"),
        ];
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

fn load_turns(path: &Path) -> Option<Vec<Turn>> {
    if !path.is_file() {
        return None;
    }
    let out = Command::new("sqlite3")
        .args([
            "-json",
            path.to_str()?,
            "SELECT prompt, speech FROM prior_turns WHERE session_id='default' ORDER BY seq",
        ])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let rows = serde_json::from_slice::<serde_json::Value>(&out.stdout).ok()?;
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

    #[test]
    fn existing_transcript_opens_picker_and_defaults_to_resume() {
        let mut picker = SessionPicker::with_turns(vec![Turn {
            prompt: "hello".into(),
            speech: "hi".into(),
        }]);
        assert!(picker.open);
        assert_eq!(picker.key(KeyCode::Enter), Some(Choice::Resume));
    }

    #[test]
    fn new_session_has_a_direct_choice() {
        let mut picker = SessionPicker::with_turns(vec![Turn {
            prompt: "old".into(),
            speech: "answer".into(),
        }]);
        assert_eq!(picker.key(KeyCode::Char('n')), Some(Choice::New));
        assert!(!picker.open);
    }

    #[test]
    fn no_transcript_preserves_direct_launch() {
        let picker = SessionPicker::discover(Path::new("/path/that/does/not/exist"));
        assert!(!picker.open);
    }

    #[test]
    fn discovers_saved_turns_from_the_harness_database() {
        let root = std::env::temp_dir().join(format!("desmos-tui-session-{}", std::process::id()));
        let dir = root.join(".desmos");
        std::fs::create_dir_all(&dir).unwrap();
        let db = dir.join("harness.sqlite3");
        let status = Command::new("sqlite3")
            .arg(&db)
            .arg("CREATE TABLE prior_turns(session_id TEXT, seq INTEGER, prompt TEXT, speech TEXT); INSERT INTO prior_turns VALUES('default',0,'stored prompt','stored answer');")
            .status()
            .unwrap();
        assert!(status.success());

        let picker = SessionPicker::discover(&root);

        assert!(picker.open);
        assert_eq!(
            picker.resumed_turns(),
            &[Turn {
                prompt: "stored prompt".into(),
                speech: "stored answer".into()
            }]
        );
        std::fs::remove_dir_all(root).unwrap();
    }
}
