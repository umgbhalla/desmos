//! Foldable JSON tree for the last complete() POST in/out panes.
//!
//! Not a pretty-printed blob. Objects and arrays are nodes you open;
//! long strings stay collapsed until you expand that row.

use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use serde_json::Value;
use unicode_width::UnicodeWidthStr;
use xai_grok_pager::theme::Theme;

const STRING_COLLAPSE: usize = 72;

#[derive(Debug, Clone)]
enum Body {
    Null,
    Bool(bool),
    Number(String),
    String(String),
    Array(Vec<Node>),
    Object(Vec<Node>),
}

#[derive(Debug, Clone)]
struct Node {
    key: String,
    body: Body,
    collapsed: bool,
}

#[derive(Debug, Clone)]
pub struct JsonTree {
    children: Vec<Node>,
    selected: usize,
    scroll: usize,
    layout_w: u16,
    layout: TreeLayout,
    /// Rows scrolled off (above, below) at the last `lines()` call.
    hidden: (usize, usize),
}

impl Default for JsonTree {
    fn default() -> Self {
        Self {
            children: Vec::new(),
            hidden: (0, 0),
            selected: 0,
            scroll: 0,
            layout_w: 0,
            layout: TreeLayout::default(),
        }
    }
}

impl JsonTree {
    pub fn from_value(value: &Value) -> Self {
        if value.is_null() || value == &Value::Object(Default::default()) {
            return Self::default();
        }
        let children = match value {
            Value::Object(map) => map
                .iter()
                .map(|(k, v)| node(k.clone(), v, 0))
                .collect(),
            Value::Array(items) => items
                .iter()
                .enumerate()
                .map(|(i, v)| node(i.to_string(), v, 0))
                .collect(),
            other => vec![node(String::new(), other, 0)],
        };
        Self {
            children,
            hidden: (0, 0),
            selected: 0,
            scroll: 0,
            layout_w: 0,
            layout: TreeLayout::default(),
        }
    }

    fn invalidate(&mut self) {
        self.layout_w = 0;
        self.layout = TreeLayout::default();
    }

    pub fn clear(&mut self) {
        *self = Self::default();
    }

    pub fn is_empty(&self) -> bool {
        self.children.is_empty()
    }

    pub fn select_next(&mut self) {
        let n = self.flat().len();
        if n == 0 {
            return;
        }
        self.selected = (self.selected + 1).min(n - 1);
    }

    pub fn select_prev(&mut self) {
        self.selected = self.selected.saturating_sub(1);
    }

    pub fn collapse(&mut self) {
        let Some(path) = self.flat().get(self.selected).cloned() else {
            return;
        };
        if let Some(node) = self.node_mut(&path) {
            if node.expandable() && !node.collapsed {
                node.collapsed = true;
                self.invalidate();
                return;
            }
        }
        if path.len() > 1 {
            let parent: Vec<usize> = path[..path.len() - 1].to_vec();
            if let Some(node) = self.node_mut(&parent) {
                node.collapsed = true;
                self.invalidate();
            }
            if let Some(i) = self.flat().iter().position(|p| p == &parent) {
                self.selected = i;
            }
        }
    }

    pub fn toggle(&mut self) {
        let Some(path) = self.flat().get(self.selected).cloned() else {
            return;
        };
        if let Some(node) = self.node_mut(&path) {
            if node.expandable() {
                node.collapsed = !node.collapsed;
                self.invalidate();
            }
        }
    }

    pub fn scroll_up(&mut self, n: usize) {
        self.scroll = self.scroll.saturating_sub(n);
    }

    pub fn scroll_down(&mut self, n: usize, view_h: u16) {
        self.ensure_layout(80);
        let total = self.layout.lines.len();
        let max = total.saturating_sub(view_h.max(1) as usize);
        self.scroll = (self.scroll + n).min(max);
    }

    pub fn click(&mut self, row: u16, width: u16) {
        self.ensure_layout(width);
        let idx = self.scroll + row as usize;
        if let Some(Some(node_i)) = self.layout.node_at_line.get(idx) {
            self.selected = *node_i;
        }
    }

    /// (above, below) rows the viewport is hiding, from the last `lines()`.
    pub fn hidden(&self) -> (usize, usize) {
        self.hidden
    }

    pub fn lines(&mut self, width: u16, height: u16, focused: bool) -> Vec<Line<'static>> {
        self.ensure_layout(width);
        let lay = &self.layout;
        if let Some(&line) = lay.line_of_node.get(self.selected) {
            if line < self.scroll {
                self.scroll = line;
            } else if line >= self.scroll + height as usize {
                self.scroll = line.saturating_sub(height.saturating_sub(1) as usize);
            }
        }
        let max = lay.lines.len().saturating_sub(height.max(1) as usize);
        if self.scroll > max {
            self.scroll = max;
        }
        let theme = Theme::current();
        let start = self.scroll;
        let end = (start + height as usize).min(self.layout.lines.len());
        // What the viewport cut off, for the caller's overflow markers. The
        // returned vec is already truncated to `height`, so a caller cannot
        // work this out after the fact.
        self.hidden = (start, self.layout.lines.len().saturating_sub(end));
        (start..end)
            .map(|i| {
                let mut line = self.layout.lines[i].clone();
                let sel = focused
                    && self
                        .layout
                        .node_at_line
                        .get(i)
                        .and_then(|n| *n)
                        .is_some_and(|n| n == self.selected);
                if sel {
                    paint_cursor_row(&mut line, width, &theme);
                }
                line
            })
            .collect()
    }

    fn ensure_layout(&mut self, width: u16) {
        if self.layout_w == width && self.layout_w != 0 {
            return;
        }
        self.layout = self.build_layout(width);
        self.layout_w = width.max(1);
    }
}

/// Grok list cursor: `bg_highlight` band, keep semantic fg. Not reverse video.
fn paint_cursor_row(line: &mut Line<'static>, width: u16, theme: &Theme) {
    let band = Style::default().bg(theme.bg_highlight);
    for span in &mut line.spans {
        span.style = span.style.patch(band);
    }
    let used: usize = line
        .spans
        .iter()
        .map(|s| UnicodeWidthStr::width(s.content.as_ref()))
        .sum();
    let pad = (width as usize).saturating_sub(used);
    if pad > 0 {
        line.spans.push(Span::styled(" ".repeat(pad), band));
    } else if line.spans.is_empty() {
        line.spans.push(Span::styled(
            " ".repeat((width as usize).max(1)),
            band,
        ));
    }
}

impl Node {
    fn expandable(&self) -> bool {
        match &self.body {
            Body::Array(v) => !v.is_empty(),
            Body::Object(v) => !v.is_empty(),
            Body::String(s) => s.len() > STRING_COLLAPSE || s.contains('\n'),
            _ => false,
        }
    }
}

fn node(key: String, value: &Value, _depth: usize) -> Node {
    let body = match value {
        Value::Null => Body::Null,
        Value::Bool(b) => Body::Bool(*b),
        Value::Number(n) => Body::Number(n.to_string()),
        Value::String(s) => Body::String(s.clone()),
        Value::Array(items) => Body::Array(
            items
                .iter()
                .enumerate()
                .map(|(i, v)| node(i.to_string(), v, _depth + 1))
                .collect(),
        ),
        Value::Object(map) => Body::Object(
            map.iter()
                .map(|(k, v)| node(k.clone(), v, _depth + 1))
                .collect(),
        ),
    };
    let collapsed = match &body {
        Body::Array(v) => v.len() > 24,
        Body::String(s) => s.len() > STRING_COLLAPSE || s.contains('\n'),
        _ => false,
    };
    Node {
        key,
        body,
        collapsed,
    }
}

impl JsonTree {
    fn flat(&self) -> Vec<Vec<usize>> {
        let mut out = Vec::new();
        for (i, child) in self.children.iter().enumerate() {
            walk(child, vec![i], &mut out);
        }
        out
    }

    fn node_mut(&mut self, path: &[usize]) -> Option<&mut Node> {
        let first = *path.first()?;
        let mut cur = self.children.get_mut(first)?;
        for &i in &path[1..] {
            cur = match &mut cur.body {
                Body::Array(v) | Body::Object(v) => v.get_mut(i)?,
                _ => return None,
            };
        }
        Some(cur)
    }

    fn build_layout(&self, width: u16) -> TreeLayout {
        let theme = Theme::current();
        let mut lay = TreeLayout::default();
        let flat = self.flat();
        for (idx, path) in flat.iter().enumerate() {
            let Some(node) = self.node_at(path) else {
                continue;
            };
            let depth = path.len().saturating_sub(1);
            let start = lay.lines.len();
            emit_node(&mut lay, node, depth, width, &theme);
            for _ in start..lay.lines.len() {
                lay.node_at_line.push(Some(idx));
            }
            lay.line_of_node.push(start);
        }
        if lay.lines.is_empty() {
            lay.lines.push(Line::from(Span::styled(
                "no POST yet",
                Style::default().fg(theme.gray),
            )));
            lay.node_at_line.push(None);
        }
        lay
    }

    fn node_at(&self, path: &[usize]) -> Option<&Node> {
        let first = *path.first()?;
        let mut cur = self.children.get(first)?;
        for &i in &path[1..] {
            cur = match &cur.body {
                Body::Array(v) | Body::Object(v) => v.get(i)?,
                _ => return None,
            };
        }
        Some(cur)
    }
}

fn walk(node: &Node, path: Vec<usize>, out: &mut Vec<Vec<usize>>) {
    out.push(path.clone());
    if node.collapsed {
        return;
    }
    let kids = match &node.body {
        Body::Array(v) | Body::Object(v) => v.as_slice(),
        _ => return,
    };
    for (i, kid) in kids.iter().enumerate() {
        let mut p = path.clone();
        p.push(i);
        walk(kid, p, out);
    }
}

#[derive(Default, Debug, Clone)]
struct TreeLayout {
    lines: Vec<Line<'static>>,
    node_at_line: Vec<Option<usize>>,
    line_of_node: Vec<usize>,
}

fn emit_node(lay: &mut TreeLayout, node: &Node, depth: usize, width: u16, theme: &Theme) {
    let indent = "  ".repeat(depth);
    let twist = if !node.expandable() {
        "  "
    } else if node.collapsed {
        "▸ "
    } else {
        "▾ "
    };
    let key_style = Style::default()
        .fg(theme.text_secondary)
        .add_modifier(Modifier::BOLD);
    let mut spans = vec![
        Span::raw(indent.clone()),
        Span::styled(twist.to_string(), Style::default().fg(theme.gray_bright)),
    ];
    if !node.key.is_empty() {
        spans.push(Span::styled(node.key.clone(), key_style));
        spans.push(Span::styled(": ", Style::default().fg(theme.gray_dim)));
    }
    match &node.body {
        Body::Null => spans.push(Span::styled("null", Style::default().fg(theme.gray))),
        Body::Bool(b) => spans.push(Span::styled(
            if *b { "true" } else { "false" },
            Style::default().fg(theme.warning),
        )),
        Body::Number(n) => spans.push(Span::styled(n.clone(), Style::default().fg(theme.running))),
        Body::String(s) if node.collapsed && node.expandable() => {
            let preview = first_line_preview(s, 48);
            spans.push(Span::styled(
                format!("\"{preview}\""),
                Style::default().fg(theme.md_code),
            ));
            spans.push(Span::styled(
                format!("  {} chars", s.len()),
                Style::default().fg(theme.gray),
            ));
        }
        Body::String(s) if !node.expandable() => {
            spans.push(Span::styled(
                format!("\"{s}\""),
                Style::default().fg(theme.md_code),
            ));
        }
        Body::String(s) => {
            spans.push(Span::styled(
                format!("string  {} chars", s.len()),
                Style::default().fg(theme.gray),
            ));
            lay.lines.push(Line::from(spans));
            let pad = format!("{}    ", "  ".repeat(depth));
            let wrap_w = (width as usize).saturating_sub(UnicodeWidthStr::width(pad.as_str())).max(16);
            for chunk in wrap_str(s, wrap_w) {
                lay.lines.push(Line::from(vec![
                    Span::raw(pad.clone()),
                    Span::styled(chunk, Style::default().fg(theme.md_code)),
                ]));
            }
            return;
        }
        Body::Array(v) => spans.push(Span::styled(
            format!("[{}]", v.len()),
            Style::default().fg(theme.gray),
        )),
        Body::Object(v) => spans.push(Span::styled(
            format!("{{{}}}", v.len()),
            Style::default().fg(theme.gray),
        )),
    }
    lay.lines.push(Line::from(spans));
}

fn first_line_preview(s: &str, max: usize) -> String {
    let line = s.lines().next().unwrap_or("");
    if UnicodeWidthStr::width(line) <= max && !s.contains('\n') {
        return line.to_string();
    }
    let mut out = String::new();
    let mut w = 0usize;
    for c in line.chars() {
        let cw = unicode_width::UnicodeWidthChar::width(c).unwrap_or(0);
        if w + cw + 1 > max {
            out.push('…');
            break;
        }
        out.push(c);
        w += cw;
    }
    if s.contains('\n') && !out.ends_with('…') {
        out.push('…');
    }
    out
}

fn wrap_str(s: &str, width: usize) -> Vec<String> {
    let mut rows = Vec::new();
    for line in s.lines() {
        if line.is_empty() {
            rows.push(String::new());
            continue;
        }
        let mut cur = String::new();
        let mut w = 0usize;
        for c in line.chars() {
            let cw = unicode_width::UnicodeWidthChar::width(c).unwrap_or(0);
            if w + cw > width && !cur.is_empty() {
                rows.push(std::mem::take(&mut cur));
                w = 0;
            }
            cur.push(c);
            w += cw;
        }
        if !cur.is_empty() {
            rows.push(cur);
        }
    }
    if rows.is_empty() {
        rows.push(String::new());
    }
    rows
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn object_keys_are_rows() {
        let t = JsonTree::from_value(&json!({"model": "claude-opus-5", "max_tokens": 8192}));
        let flat = t.flat();
        assert_eq!(flat.len(), 2);
    }

    #[test]
    fn nested_object_is_open_long_string_is_not() {
        let t = JsonTree::from_value(&json!({
            "thinking": {"type": "adaptive", "display": "summarized"},
            "blob": "x".repeat(200)
        }));
        let keys: Vec<String> = t.flat().iter().filter_map(|p| t.node_at(p).map(|n| n.key.clone())).collect();
        assert!(keys.iter().any(|k| k == "type"), "{keys:?}");
        assert!(keys.iter().any(|k| k == "blob"), "{keys:?}");
        let blob = t.children.iter().find(|n| n.key == "blob").unwrap();
        assert!(blob.collapsed);
    }

    #[test]
    fn long_string_does_not_dump_until_expanded() {
        let long = "x".repeat(200);
        let mut t = JsonTree::from_value(&json!({"text": long}));
        let lay = t.build_layout(80);
        let shown: String = lay
            .lines
            .iter()
            .flat_map(|l| l.spans.iter())
            .map(|s| s.content.as_ref())
            .collect();
        assert!(shown.contains("200 chars"), "{shown}");
        assert!(!shown.contains(&"x".repeat(80)), "collapsed string dumped");
        t.toggle();
        let lay = t.build_layout(80);
        assert!(lay.lines.len() > 1);
    }

    #[test]
    fn selected_row_uses_grok_highlight_not_reverse() {
        // lines() reads the process-global theme; the slash-command test writes
        // it. Without the pin this reads one theme and renders under another.
        let _pin = crate::theme_lock();
        let mut t = JsonTree::from_value(&json!({"model": "claude-opus-5", "max_tokens": 8}));
        let theme = Theme::current();
        let lines = t.lines(48, 6, true);
        assert!(!lines.is_empty());
        let line = &lines[0];
        assert!(
            line.spans
                .iter()
                .all(|s| !s.style.add_modifier.contains(Modifier::REVERSED)),
            "cursor must not invert fg/bg"
        );
        assert!(
            line.spans.iter().any(|s| s.style.bg == Some(theme.bg_highlight)),
            "cursor must use theme.bg_highlight"
        );
        let key = line
            .spans
            .iter()
            .find(|s| s.content == "model")
            .expect("key");
        assert_eq!(key.style.fg, Some(theme.text_secondary));
    }
}
