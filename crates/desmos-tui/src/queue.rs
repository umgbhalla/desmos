//! Follow-up query stack — grok's pending_prompts, local to desmos.
//!
//! Enter while a step is running appends a row. After the step settles the
//! front runs. Empty Enter mid-step is send-now: stop the current step and
//! fire the front immediately.

use std::collections::VecDeque;

use ratatui::style::Style;
use ratatui::text::{Line, Span};
use unicode_width::UnicodeWidthStr;
use xai_grok_pager::theme::Theme;

const MAX_VISIBLE: usize = 6;

#[derive(Debug, Clone)]
pub struct QueuedQuery {
    #[allow(dead_code)]
    pub id: u64,
    pub text: String,
}

#[derive(Debug, Default)]
pub struct QueryQueue {
    items: VecDeque<QueuedQuery>,
    next_id: u64,
    pub selected: Option<usize>,
}

impl QueryQueue {
    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = &QueuedQuery> {
        self.items.iter()
    }

    pub fn push(&mut self, text: String) -> u64 {
        let id = self.next_id;
        self.next_id += 1;
        self.items.push_back(QueuedQuery { id, text });
        self.selected = Some(self.items.len() - 1);
        id
    }

    pub fn pop_front(&mut self) -> Option<QueuedQuery> {
        let item = self.items.pop_front()?;
        if self.items.is_empty() {
            self.selected = None;
        } else if let Some(s) = self.selected {
            self.selected = Some(s.min(self.items.len() - 1));
        }
        Some(item)
    }

    pub fn remove_selected(&mut self) -> Option<QueuedQuery> {
        let idx = self.selected?;
        let item = self.items.remove(idx)?;
        if self.items.is_empty() {
            self.selected = None;
        } else {
            self.selected = Some(idx.min(self.items.len() - 1));
        }
        Some(item)
    }

    pub fn select_next(&mut self) {
        if self.items.is_empty() {
            return;
        }
        let i = self.selected.unwrap_or(0);
        self.selected = Some((i + 1).min(self.items.len() - 1));
    }

    pub fn select_prev(&mut self) {
        if self.items.is_empty() {
            return;
        }
        let i = self.selected.unwrap_or(0);
        self.selected = Some(i.saturating_sub(1));
    }

    /// Move the selected row up (`dir < 0`) or down (`dir > 0`).
    pub fn move_selected(&mut self, dir: i32) {
        let Some(i) = self.selected else {
            return;
        };
        if dir < 0 && i > 0 {
            self.items.swap(i, i - 1);
            self.selected = Some(i - 1);
        } else if dir > 0 && i + 1 < self.items.len() {
            self.items.swap(i, i + 1);
            self.selected = Some(i + 1);
        }
    }

    /// Put a row back at `idx`, clamped to the end.
    ///
    /// The editor lifts a row out of the queue and into the composer. When it
    /// comes back it belongs in the slot it left — a row that jumps to the back
    /// because you fixed a typo in it is a reorder you did not ask for.
    pub fn insert_at(&mut self, idx: usize, text: String) -> u64 {
        let id = self.next_id;
        self.next_id += 1;
        let idx = idx.min(self.items.len());
        self.items.insert(idx, QueuedQuery { id, text });
        self.selected = Some(idx);
        id
    }

    /// Pull `idx` to the front so send-now fires that row.
    pub fn rotate_to_front(&mut self, idx: usize) {
        if idx == 0 || idx >= self.items.len() {
            return;
        }
        if let Some(item) = self.items.remove(idx) {
            self.items.push_front(item);
            self.selected = Some(0);
        }
    }

    pub fn clear(&mut self) {
        self.items.clear();
        self.selected = None;
    }

    pub fn display_height(&self) -> u16 {
        if self.items.is_empty() {
            0
        } else {
            (self.items.len().min(MAX_VISIBLE) as u16).saturating_add(2)
        }
    }

    pub fn lines(&self, width: u16, focused: bool) -> Vec<Line<'static>> {
        let theme = Theme::current();
        let w = width.max(8) as usize;
        let skip = self.items.len().saturating_sub(MAX_VISIBLE);
        self.items
            .iter()
            .enumerate()
            .skip(skip)
            .map(|(i, q)| {
                let pos = i + 1;
                let extra = q.text.lines().count().saturating_sub(1);
                let suffix = if extra == 0 {
                    String::new()
                } else if extra == 1 {
                    " (+1 line)".to_string()
                } else {
                    format!(" (+{extra} lines)")
                };
                let first = q
                    .text
                    .lines()
                    .map(str::trim)
                    .find(|l| !l.is_empty())
                    .unwrap_or("")
                    .to_string();
                let prefix = format!("#{pos} ");
                let room = w
                    .saturating_sub(UnicodeWidthStr::width(prefix.as_str()))
                    .saturating_sub(UnicodeWidthStr::width(suffix.as_str()));
                let shown = truncate_width(&first, room);
                let selected = focused && self.selected == Some(i);
                let body = Style::default().fg(theme.accent_user);
                let mut line = Line::from(vec![
                    Span::styled(prefix, Style::default().fg(theme.gray)),
                    Span::styled(shown, body),
                    Span::styled(suffix, Style::default().fg(theme.gray)),
                ]);
                if selected {
                    let band = Style::default().bg(theme.bg_highlight);
                    for span in &mut line.spans {
                        span.style = span.style.patch(band);
                    }
                    let used: usize = line
                        .spans
                        .iter()
                        .map(|s| UnicodeWidthStr::width(s.content.as_ref()))
                        .sum();
                    let pad = w.saturating_sub(used);
                    if pad > 0 {
                        line.spans.push(Span::styled(" ".repeat(pad), band));
                    }
                }
                line
            })
            .collect()
    }
}

fn truncate_width(s: &str, max: usize) -> String {
    if UnicodeWidthStr::width(s) <= max {
        return s.to_string();
    }
    let mut out = String::new();
    let mut used = 0usize;
    for c in s.chars() {
        let cw = unicode_width::UnicodeWidthChar::width(c).unwrap_or(0);
        if used + cw + 1 > max {
            out.push('…');
            break;
        }
        out.push(c);
        used += cw;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fifo_and_rotate() {
        let mut q = QueryQueue::default();
        q.push("a".into());
        q.push("b".into());
        q.push("c".into());
        q.rotate_to_front(2);
        assert_eq!(q.pop_front().unwrap().text, "c");
        assert_eq!(q.pop_front().unwrap().text, "a");
    }

    #[test]
    fn remove_selected_clamps() {
        let mut q = QueryQueue::default();
        q.push("a".into());
        q.push("b".into());
        q.selected = Some(1);
        assert_eq!(q.remove_selected().unwrap().text, "b");
        assert_eq!(q.selected, Some(0));
    }
}
