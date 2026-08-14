//! Grok-shaped prompt paste: bracketed paste, chips, Ctrl+V / Cmd+V.
//!
//! Short pastes land inline (newlines stay; they do not submit). Four or
//! more lines, or more than 10 KB, become a `[Pasted: …]` chip. Enter on
//! the chip expands it; sending the prompt expands every chip to the
//! original body. Rapid key bursts without bracketed paste are coalesced
//! so a mid-paste Enter cannot submit.

use crossterm::event::{Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use unicode_width::UnicodeWidthStr;
use xai_grok_pager::theme::Theme;

pub const PASTE_CHIP_DISPLAY_BYTES: usize = 10_000;
pub const PASTE_CHIP_MIN_LINES: usize = 4;
const PASTE_COALESCE_THRESHOLD: usize = 3;
const TAB: &str = "    ";

#[derive(Debug, Clone)]
enum Seg {
    Text(String),
    Chip { id: u64, body: String },
}

#[derive(Debug, Clone, Default)]
pub struct PromptBuf {
    segs: Vec<Seg>,
    /// Segment the cursor is in, or `segs.len()` for the empty tail.
    seg: usize,
    /// Byte offset in a Text seg. Chip segs are atomic (`off` is 0).
    off: usize,
    next_id: u64,
}

#[derive(Debug, Clone)]
pub struct ChipBox {
    pub id: u64,
    pub seg: usize,
    pub row: u16,
    pub col: u16,
    pub width: u16,
}

#[derive(Debug, Clone)]
pub struct PromptLayout {
    pub lines: Vec<Line<'static>>,
    pub cursor_col: u16,
    pub cursor_row: u16,
    pub chip_boxes: Vec<ChipBox>,
}

impl PromptBuf {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn clear(&mut self) {
        self.segs.clear();
        self.seg = 0;
        self.off = 0;
    }

    pub fn to_send(&self) -> String {
        let mut out = String::new();
        for s in &self.segs {
            match s {
                Seg::Text(t) => out.push_str(t),
                Seg::Chip { body, .. } => out.push_str(body),
            }
        }
        out
    }

    pub fn insert_char(&mut self, c: char) {
        let mut buf = [0u8; 4];
        self.insert_str(c.encode_utf8(&mut buf));
    }

    pub fn insert_str(&mut self, raw: &str) {
        if raw.is_empty() {
            return;
        }
        let text = expand_tabs(&normalize_cr(raw));
        self.insert_raw(&text);
    }

    /// Grok `handle_paste`. Small pastes inline; large ones chip.
    /// Repasting a chip's exact body expands it instead of duplicating.
    pub fn handle_paste(&mut self, raw: &str) -> bool {
        if raw.is_empty() {
            return false;
        }
        let text = expand_tabs(&normalize_cr(raw));
        if text.is_empty() {
            return false;
        }
        if let Some(idx) = self.chip_near_cursor() {
            if let Seg::Chip { body, .. } = &self.segs[idx] {
                if body == &text {
                    self.expand_seg(idx);
                    return true;
                }
            }
        }
        let lines = text.lines().count();
        if lines >= PASTE_CHIP_MIN_LINES || text.len() > PASTE_CHIP_DISPLAY_BYTES {
            self.insert_chip(text);
        } else {
            self.insert_raw(&text);
        }
        true
    }

    /// Ctrl+Shift+V / Cmd+Shift+V: always inline, never a chip.
    pub fn handle_inline_paste(&mut self, raw: &str) -> bool {
        if raw.is_empty() {
            return false;
        }
        let text = expand_tabs(&normalize_cr(raw));
        if text.is_empty() {
            return false;
        }
        self.insert_raw(&text);
        true
    }

    pub fn expand_at_cursor(&mut self) -> bool {
        let Some(idx) = self.chip_at_cursor() else {
            return false;
        };
        self.expand_seg(idx);
        true
    }

    pub fn expand_chip_id(&mut self, id: u64) -> bool {
        let Some(idx) = self
            .segs
            .iter()
            .position(|s| matches!(s, Seg::Chip { id: cid, .. } if *cid == id))
        else {
            return false;
        };
        self.expand_seg(idx);
        true
    }

    pub fn backspace(&mut self) {
        if self.chip_at_cursor().is_some() {
            self.delete_seg(self.seg);
            return;
        }
        if self.seg < self.segs.len() {
            if let Seg::Text(t) = &mut self.segs[self.seg] {
                if self.off > 0 {
                    let prev = prev_boundary(t, self.off);
                    t.replace_range(prev..self.off, "");
                    self.off = prev;
                    self.drop_empty_text(self.seg);
                    self.merge_texts();
                    return;
                }
            }
        }
        if self.seg == 0 {
            return;
        }
        let prev = self.seg - 1;
        match &self.segs[prev] {
            Seg::Chip { .. } => self.delete_seg(prev),
            Seg::Text(_) => {
                self.seg = prev;
                if let Seg::Text(t) = &self.segs[prev] {
                    self.off = t.len();
                }
                self.backspace();
            }
        }
    }

    pub fn move_left(&mut self) {
        if self.chip_at_cursor().is_some() {
            if self.seg == 0 {
                return;
            }
            self.seg -= 1;
            self.off = match &self.segs[self.seg] {
                Seg::Text(t) => t.len(),
                Seg::Chip { .. } => 0,
            };
            return;
        }
        if self.seg < self.segs.len() {
            if let Seg::Text(t) = &self.segs[self.seg] {
                if self.off > 0 {
                    self.off = prev_boundary(t, self.off);
                    return;
                }
            }
        }
        if self.seg == 0 {
            return;
        }
        self.seg -= 1;
        self.off = match &self.segs[self.seg] {
            Seg::Text(t) => t.len(),
            Seg::Chip { .. } => 0,
        };
    }

    pub fn move_right(&mut self) {
        if self.chip_at_cursor().is_some() {
            self.seg += 1;
            self.off = 0;
            return;
        }
        if self.seg >= self.segs.len() {
            return;
        }
        if let Seg::Text(t) = &self.segs[self.seg] {
            if self.off < t.len() {
                self.off = next_boundary(t, self.off);
                return;
            }
        }
        self.seg += 1;
        self.off = 0;
    }

    pub fn move_end(&mut self) {
        self.seg = self.segs.len();
        self.off = 0;
        if let Some(Seg::Text(t)) = self.segs.last() {
            self.seg = self.segs.len() - 1;
            self.off = t.len();
        }
    }

    pub fn move_up(&mut self, width: u16) {
        let lay = self.layout(" ", width);
        if lay.cursor_row == 0 {
            return;
        }
        self.click(lay.cursor_col, lay.cursor_row - 1, width);
    }

    pub fn move_down(&mut self, width: u16) {
        let lay = self.layout(" ", width);
        let last = lay.lines.len().saturating_sub(1) as u16;
        if lay.cursor_row >= last {
            return;
        }
        self.click(lay.cursor_col, lay.cursor_row + 1, width);
    }

    pub fn move_line_home(&mut self, width: u16) {
        let lay = self.layout(" ", width);
        let col = if lay.cursor_row == 0 {
            UnicodeWidthStr::width(" ") as u16
        } else {
            UnicodeWidthStr::width(" ") as u16
        };
        self.click(col, lay.cursor_row, width);
    }

    pub fn move_line_end(&mut self, width: u16) {
        let lay = self.layout(" ", width);
        self.click(width.saturating_sub(1), lay.cursor_row, width);
    }

    pub fn delete(&mut self) {
        if self.chip_at_cursor().is_some() {
            self.delete_seg(self.seg);
            return;
        }
        if self.seg >= self.segs.len() {
            return;
        }
        if let Seg::Text(t) = &self.segs[self.seg] {
            if self.off < t.len() {
                let next = next_boundary(t, self.off);
                if let Seg::Text(t) = &mut self.segs[self.seg] {
                    t.replace_range(self.off..next, "");
                }
                self.drop_empty_text(self.seg);
                self.merge_texts();
                return;
            }
        }
        let next = self.seg + 1;
        if next >= self.segs.len() {
            return;
        }
        match &self.segs[next] {
            Seg::Chip { .. } => self.delete_seg(next),
            Seg::Text(_) => {
                self.seg = next;
                self.off = 0;
                self.delete();
            }
        }
    }

    /// `\` immediately before the cursor becomes a newline (grok continuation).
    pub fn apply_backslash_continuation(&mut self) -> bool {
        if self.char_before() != Some('\\') {
            return false;
        }
        self.backspace();
        self.insert_char('\n');
        true
    }

    pub fn is_multiline(&self) -> bool {
        self.to_send().contains('\n')
    }

    fn char_before(&self) -> Option<char> {
        if self.seg < self.segs.len() {
            if let Seg::Text(t) = &self.segs[self.seg] {
                if self.off > 0 {
                    return t[..self.off].chars().next_back();
                }
            }
        }
        if self.seg == 0 {
            return None;
        }
        match &self.segs[self.seg - 1] {
            Seg::Text(t) => t.chars().next_back(),
            Seg::Chip { .. } => None,
        }
    }

    pub fn preview_body(&self) -> Option<&str> {
        let idx = self.chip_near_cursor()?;
        match &self.segs[idx] {
            Seg::Chip { body, .. } => Some(body.as_str()),
            _ => None,
        }
    }

    pub fn preview_on_chip(&self) -> bool {
        self.chip_at_cursor().is_some()
    }

    pub fn display_rows(&self, width: u16) -> u16 {
        self.layout(" ", width).lines.len().max(1) as u16
    }

    pub fn layout(&self, prefix: &str, width: u16) -> PromptLayout {
        let theme = Theme::current();
        let width = width.max(1) as usize;
        let prefix_w = UnicodeWidthStr::width(prefix);
        let hang = prefix_w;

        let mut lines: Vec<Vec<Span<'static>>> = vec![vec![Span::raw(prefix.to_string())]];
        let mut x = prefix_w;
        let mut cursor_col = prefix_w as u16;
        let mut cursor_row = 0u16;
        let mut chips = Vec::new();
        let mut marked = false;

        let mark = |x: usize, row: u16, marked: &mut bool, cc: &mut u16, cr: &mut u16| {
            if !*marked {
                *cc = x.min(width.saturating_sub(1)) as u16;
                *cr = row;
                *marked = true;
            }
        };

        let newline = |lines: &mut Vec<Vec<Span<'static>>>, x: &mut usize| {
            lines.push(vec![Span::raw(" ".repeat(hang))]);
            *x = hang;
        };

        for (i, seg) in self.segs.iter().enumerate() {
            let here = self.seg == i;
            match seg {
                Seg::Chip { id, body } => {
                    let label = format!("[{}]", chip_label(body));
                    let w = UnicodeWidthStr::width(label.as_str());
                    if x + w > width && x > hang {
                        newline(&mut lines, &mut x);
                    }
                    if here {
                        mark(x, (lines.len() - 1) as u16, &mut marked, &mut cursor_col, &mut cursor_row);
                    }
                    chips.push(ChipBox {
                        id: *id,
                        seg: i,
                        row: (lines.len() - 1) as u16,
                        col: x as u16,
                        width: w as u16,
                    });
                    let bracket = Style::default().fg(theme.paste_dim).bg(theme.paste_bg);
                    let fill = Style::default()
                        .fg(theme.paste_fg)
                        .bg(theme.paste_bg)
                        .add_modifier(if here { Modifier::REVERSED } else { Modifier::empty() });
                    let last = lines.last_mut().expect("row");
                    last.push(Span::styled("[", bracket));
                    last.push(Span::styled(chip_label(body), fill));
                    last.push(Span::styled("]", bracket));
                    x += w;
                }
                Seg::Text(t) => {
                    let mut byte = 0usize;
                    for ch in t.chars() {
                        if here && self.off == byte {
                            mark(x, (lines.len() - 1) as u16, &mut marked, &mut cursor_col, &mut cursor_row);
                        }
                        if ch == '\n' {
                            newline(&mut lines, &mut x);
                        } else {
                            let cw = unicode_width::UnicodeWidthChar::width(ch).unwrap_or(0);
                            if x + cw > width && x > hang {
                                newline(&mut lines, &mut x);
                            }
                            lines
                                .last_mut()
                                .expect("row")
                                .push(Span::styled(ch.to_string(), Style::default().fg(theme.text_primary)));
                            x += cw;
                        }
                        byte += ch.len_utf8();
                    }
                    if here && self.off == byte {
                        mark(x, (lines.len() - 1) as u16, &mut marked, &mut cursor_col, &mut cursor_row);
                    }
                }
            }
        }
        if self.seg >= self.segs.len() {
            mark(x, (lines.len() - 1) as u16, &mut marked, &mut cursor_col, &mut cursor_row);
        }

        PromptLayout {
            lines: lines.into_iter().map(Line::from).collect(),
            cursor_col,
            cursor_row,
            chip_boxes: chips,
        }
    }

    pub fn click(&mut self, col: u16, row: u16, width: u16) -> Option<u64> {
        let lay = self.layout(" ", width);
        for b in &lay.chip_boxes {
            if b.row == row && col >= b.col && col < b.col.saturating_add(b.width) {
                self.seg = b.seg;
                self.off = 0;
                return Some(b.id);
            }
        }
        // Fall back to end of the clicked row's content by walking display.
        self.move_end();
        if row < lay.cursor_row || (row == lay.cursor_row && col < lay.cursor_col) {
            // Step left until the layout cursor matches. Bounded by send len.
            let budget = self.to_send().chars().count() + self.segs.len() + 4;
            for _ in 0..budget {
                let now = self.layout(" ", width);
                if now.cursor_row < row {
                    break;
                }
                if now.cursor_row == row && now.cursor_col <= col {
                    break;
                }
                let before = (self.seg, self.off);
                self.move_left();
                if (self.seg, self.off) == before {
                    break;
                }
            }
        }
        None
    }

    fn insert_chip(&mut self, body: String) {
        let id = self.next_id;
        self.next_id += 1;
        let idx = self.split_at_cursor();
        self.segs.insert(idx, Seg::Chip { id, body });
        // Leave the cursor after the chip (grok: Enter submits, paste-again expands).
        self.seg = idx + 1;
        self.off = 0;
        self.merge_texts();
    }

    fn insert_raw(&mut self, text: &str) {
        if self.chip_at_cursor().is_some() {
            let idx = self.seg + 1;
            self.segs.insert(idx, Seg::Text(text.to_string()));
            self.seg = idx;
            self.off = text.len();
            self.merge_texts();
            return;
        }
        if self.seg >= self.segs.len() {
            if matches!(self.segs.last(), Some(Seg::Text(_))) {
                if let Some(Seg::Text(t)) = self.segs.last_mut() {
                    t.push_str(text);
                    self.off = t.len();
                }
                self.seg = self.segs.len() - 1;
            } else {
                self.segs.push(Seg::Text(text.to_string()));
                self.seg = self.segs.len() - 1;
                self.off = text.len();
            }
            return;
        }
        if let Seg::Text(t) = &mut self.segs[self.seg] {
            t.insert_str(self.off, text);
            self.off += text.len();
        }
        self.merge_texts();
    }

    fn split_at_cursor(&mut self) -> usize {
        if self.seg >= self.segs.len() {
            return self.segs.len();
        }
        if self.chip_at_cursor().is_some() {
            return self.seg;
        }
        if let Seg::Text(t) = &self.segs[self.seg] {
            if self.off == 0 {
                return self.seg;
            }
            if self.off >= t.len() {
                return self.seg + 1;
            }
        }
        if let Seg::Text(t) = &mut self.segs[self.seg] {
            let rest = t[self.off..].to_string();
            t.truncate(self.off);
            let idx = self.seg + 1;
            self.segs.insert(idx, Seg::Text(rest));
            return idx;
        }
        self.seg
    }

    fn expand_seg(&mut self, idx: usize) {
        let Seg::Chip { body, .. } = self.segs.remove(idx) else {
            return;
        };
        self.segs.insert(idx, Seg::Text(body.clone()));
        self.seg = idx;
        self.off = body.len();
        self.merge_texts();
    }

    fn delete_seg(&mut self, idx: usize) {
        if idx >= self.segs.len() {
            return;
        }
        self.segs.remove(idx);
        self.seg = idx.min(self.segs.len());
        self.off = 0;
        if self.seg < self.segs.len() {
            if let Seg::Text(_) = &self.segs[self.seg] {
                self.off = 0;
            }
        } else if let Some(Seg::Text(t)) = self.segs.last() {
            self.seg = self.segs.len() - 1;
            self.off = t.len();
        }
        self.merge_texts();
    }

    fn drop_empty_text(&mut self, idx: usize) {
        if idx >= self.segs.len() {
            return;
        }
        if matches!(&self.segs[idx], Seg::Text(t) if t.is_empty()) {
            self.segs.remove(idx);
            if self.seg > idx {
                self.seg -= 1;
            } else if self.seg == idx {
                self.off = 0;
            }
        }
    }

    fn merge_texts(&mut self) {
        let mut i = 0;
        while i + 1 < self.segs.len() {
            match (&self.segs[i], &self.segs[i + 1]) {
                (Seg::Text(_), Seg::Text(_)) => {
                    let Seg::Text(right) = self.segs.remove(i + 1) else {
                        unreachable!()
                    };
                    if self.seg == i + 1 {
                        self.seg = i;
                        if let Seg::Text(left) = &self.segs[i] {
                            self.off += left.len();
                        }
                    } else if self.seg > i + 1 {
                        self.seg -= 1;
                    }
                    if let Seg::Text(left) = &mut self.segs[i] {
                        left.push_str(&right);
                    }
                }
                _ => i += 1,
            }
        }
    }

    fn chip_at_cursor(&self) -> Option<usize> {
        match self.segs.get(self.seg) {
            Some(Seg::Chip { .. }) => Some(self.seg),
            _ => None,
        }
    }

    fn chip_near_cursor(&self) -> Option<usize> {
        if let Some(i) = self.chip_at_cursor() {
            return Some(i);
        }
        if self.off == 0 && self.seg > 0 {
            if matches!(self.segs.get(self.seg - 1), Some(Seg::Chip { .. })) {
                return Some(self.seg - 1);
            }
        }
        None
    }
}

pub fn is_paste_key(key: &KeyEvent) -> bool {
    if key.kind != KeyEventKind::Press && key.kind != KeyEventKind::Repeat {
        return false;
    }
    if key.code != KeyCode::Char('v') && key.code != KeyCode::Char('V') {
        return false;
    }
    let m = key.modifiers;
    m.contains(KeyModifiers::CONTROL) || m.contains(KeyModifiers::SUPER)
}

pub fn is_inline_paste_key(key: &KeyEvent) -> bool {
    is_paste_key(key) && key.modifiers.contains(KeyModifiers::SHIFT)
}

pub fn is_text_key(key: &KeyEvent) -> bool {
    matches!(key.code, KeyCode::Char(_))
        && !key.modifiers.contains(KeyModifiers::CONTROL)
        && !key.modifiers.contains(KeyModifiers::SUPER)
        && !key.modifiers.contains(KeyModifiers::ALT)
}

pub fn clipboard_text() -> Option<String> {
    xai_grok_pager::clipboard::system_clipboard_get().filter(|s| !s.is_empty())
}

/// Drain-side paste repair: merge `Event::Paste` fragments and turn a
/// burst of Char/Enter/Tab keys into one paste (no bracketed paste).
pub fn coalesce_events(events: Vec<Event>) -> Vec<Event> {
    if events.is_empty() {
        return events;
    }
    let has_paste = events.iter().any(|e| matches!(e, Event::Paste(_)));
    if has_paste {
        return merge_paste_fragments(events);
    }
    let events: Vec<Event> = events
        .into_iter()
        .filter(|e| !matches!(e, Event::Key(k) if k.kind == KeyEventKind::Release))
        .collect();
    if events.len() < PASTE_COALESCE_THRESHOLD {
        return events;
    }
    let mut out = Vec::new();
    let mut i = 0;
    while i < events.len() {
        if is_pasteable_key(&events[i]) {
            let start = i;
            let mut text = String::new();
            let mut seen_enter = false;
            let mut after_enter = false;
            while i < events.len() && is_pasteable_key(&events[i]) {
                if let Event::Key(ke) = &events[i] {
                    match ke.code {
                        KeyCode::Char(c) => {
                            text.push(c);
                            if seen_enter {
                                after_enter = true;
                            }
                        }
                        KeyCode::Enter => {
                            text.push('\n');
                            seen_enter = true;
                        }
                        KeyCode::Tab => {
                            text.push('\t');
                            if seen_enter {
                                after_enter = true;
                            }
                        }
                        _ => {}
                    }
                }
                i += 1;
            }
            if i - start >= PASTE_COALESCE_THRESHOLD && after_enter {
                out.push(Event::Paste(text));
            } else {
                out.extend(events[start..i].iter().cloned());
            }
        } else {
            out.push(events[i].clone());
            i += 1;
        }
    }
    out
}

fn merge_paste_fragments(events: Vec<Event>) -> Vec<Event> {
    let mut out = Vec::new();
    let mut buf = String::new();
    let flush = |out: &mut Vec<Event>, buf: &mut String| {
        if !buf.is_empty() {
            out.push(Event::Paste(std::mem::take(buf)));
        }
    };
    for e in events {
        match e {
            Event::Paste(t) => buf.push_str(&t),
            Event::Key(ke) if is_pasteable_key(&Event::Key(ke.clone())) => match ke.code {
                KeyCode::Char(c) => buf.push(c),
                KeyCode::Enter => buf.push('\n'),
                KeyCode::Tab => buf.push('\t'),
                _ => {}
            },
            other => {
                flush(&mut out, &mut buf);
                out.push(other);
            }
        }
    }
    flush(&mut out, &mut buf);
    out
}

fn is_pasteable_key(ev: &Event) -> bool {
    matches!(
        ev,
        Event::Key(ke)
            if (ke.kind == KeyEventKind::Press || ke.kind == KeyEventKind::Repeat)
                && ke.modifiers.is_empty()
                && matches!(ke.code, KeyCode::Char(_) | KeyCode::Enter | KeyCode::Tab)
    )
}

/// Bare `\r` → `\n`. Leave `\r\n` alone (grok `normalize_cr`).
pub fn normalize_cr(text: &str) -> String {
    let mut s = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\r' && chars.peek() != Some(&'\n') {
            s.push('\n');
        } else {
            s.push(c);
        }
    }
    s
}

fn expand_tabs(text: &str) -> String {
    text.replace('\t', TAB)
}

fn chip_label(body: &str) -> String {
    if body.len() > PASTE_CHIP_DISPLAY_BYTES {
        let n = body.len();
        let size = if n >= 1_000_000 {
            format!("{:.1} MB", n as f64 / 1_000_000.0)
        } else if n >= 1000 {
            format!("{} KB", n / 1000)
        } else {
            format!("{n} bytes")
        };
        format!("Pasted: {size}")
    } else {
        let n = body.lines().count();
        format!("Pasted: {n} line{}", if n != 1 { "s" } else { "" })
    }
}

fn prev_boundary(s: &str, off: usize) -> usize {
    s.get(..off)
        .and_then(|h| h.chars().next_back())
        .map(|c| off - c.len_utf8())
        .unwrap_or(0)
}

fn next_boundary(s: &str, off: usize) -> usize {
    s.get(off..)
        .and_then(|t| t.chars().next())
        .map(|c| off + c.len_utf8())
        .unwrap_or(off)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_cr_bare_and_crlf() {
        assert_eq!(normalize_cr("a\rb\rc"), "a\nb\nc");
        assert_eq!(normalize_cr("a\r\nb\r\nc"), "a\r\nb\r\nc");
        assert_eq!(normalize_cr("a\r\nb\rc"), "a\r\nb\nc");
    }

    #[test]
    fn small_paste_stays_inline_with_newlines() {
        let mut p = PromptBuf::new();
        p.handle_paste("line1\nline2\nline3");
        assert_eq!(p.to_send(), "line1\nline2\nline3");
        assert!(p.preview_body().is_none());
    }

    #[test]
    fn four_lines_become_a_chip() {
        let mut p = PromptBuf::new();
        p.handle_paste("a\nb\nc\nd");
        assert_eq!(p.to_send(), "a\nb\nc\nd");
        assert!(p.preview_body().is_some());
        assert!(!p.preview_on_chip(), "cursor sits after the chip");
        let lay = p.layout(" ", 80);
        let shown = lay
            .lines
            .iter()
            .flat_map(|l| l.spans.iter())
            .map(|s| s.content.as_ref())
            .collect::<String>();
        assert!(shown.contains("Pasted: 4 lines"), "{shown}");
        assert!(!shown.contains("a\nb"), "chip hid the body");
    }

    #[test]
    fn large_bytes_become_a_chip() {
        let mut p = PromptBuf::new();
        let body = "x".repeat(PASTE_CHIP_DISPLAY_BYTES + 1);
        p.handle_paste(&body);
        assert_eq!(p.to_send(), body);
        let lay = p.layout(" ", 80);
        let shown = lay
            .lines
            .iter()
            .flat_map(|l| l.spans.iter())
            .map(|s| s.content.as_ref())
            .collect::<String>();
        assert!(shown.contains("Pasted:"), "{shown}");
    }

    #[test]
    fn repaste_expands_chip() {
        let mut p = PromptBuf::new();
        let body = "a\nb\nc\nd";
        p.handle_paste(body);
        assert!(p.preview_body().is_some());
        p.handle_paste(body);
        assert!(p.preview_body().is_none());
        assert_eq!(p.to_send(), body);
    }

    #[test]
    fn enter_on_chip_expands() {
        let mut p = PromptBuf::new();
        p.handle_paste("a\nb\nc\nd");
        p.move_left();
        assert!(p.preview_on_chip());
        assert!(p.expand_at_cursor());
        assert_eq!(p.to_send(), "a\nb\nc\nd");
        assert!(p.preview_body().is_none());
    }

    #[test]
    fn inline_paste_key_skips_chip() {
        let mut p = PromptBuf::new();
        p.handle_inline_paste("a\nb\nc\nd");
        assert!(p.preview_body().is_none());
        assert_eq!(p.to_send(), "a\nb\nc\nd");
    }

    #[test]
    fn paste_does_not_submit() {
        let mut p = PromptBuf::new();
        p.insert_str("keep ");
        p.handle_paste("a\nb");
        assert_eq!(p.to_send(), "keep a\nb");
    }

    #[test]
    fn coalesce_multiline_key_burst() {
        let evs = vec![
            Event::Key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE)),
            Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
            Event::Key(KeyEvent::new(KeyCode::Char('b'), KeyModifiers::NONE)),
        ];
        let out = coalesce_events(evs);
        assert!(matches!(&out[..], [Event::Paste(t)] if t == "a\nb"), "{out:?}");
    }

    #[test]
    fn coalesce_merges_paste_fragments() {
        let evs = vec![
            Event::Paste("hel".into()),
            Event::Key(KeyEvent::new(KeyCode::Char('l'), KeyModifiers::NONE)),
            Event::Paste("o".into()),
        ];
        let out = coalesce_events(evs);
        assert!(matches!(&out[..], [Event::Paste(t)] if t == "hello"), "{out:?}");
    }

    #[test]
    fn newline_renders_as_two_rows() {
        let mut p = PromptBuf::new();
        p.insert_str("aaa\nbbb");
        assert!(p.is_multiline());
        let lay = p.layout(" ", 80);
        assert!(
            lay.lines.len() >= 2,
            "expected two visual rows, got {}",
            lay.lines.len()
        );
        assert_eq!(lay.cursor_row, 1);
        let shown = lay
            .lines
            .iter()
            .flat_map(|l| l.spans.iter())
            .map(|s| s.content.as_ref())
            .collect::<String>();
        assert!(shown.contains("aaa") && shown.contains("bbb"), "{shown}");
    }

    #[test]
    fn backslash_enter_is_continuation() {
        let mut p = PromptBuf::new();
        p.insert_str("hello\\");
        assert!(p.apply_backslash_continuation());
        assert_eq!(p.to_send(), "hello\n");
        assert!(p.is_multiline());
    }

    #[test]
    fn up_down_crosses_hard_newlines() {
        let mut p = PromptBuf::new();
        p.insert_str("aaa\nbbb");
        let down = p.layout(" ", 80);
        assert_eq!(down.cursor_row, 1);
        p.move_up(80);
        let up = p.layout(" ", 80);
        assert_eq!(up.cursor_row, 0);
        p.move_down(80);
        let back = p.layout(" ", 80);
        assert_eq!(back.cursor_row, 1);
    }
}
