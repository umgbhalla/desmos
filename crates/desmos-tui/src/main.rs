//! Fullscreen desmos TUI.
//!
//! Middle — turn story (you / think / speech). No syscall bodies.
//! Right  — wire calls only: complete() and XML syscalls, USER vs LLM.
//! Bottom — input.

use std::io::{self, BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::time::Duration;

use crossterm::event::{
    self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyEvent, KeyModifiers,
    MouseEvent, MouseEventKind,
};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::prelude::CrosstermBackend;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::{Frame, Terminal};
use serde_json::{Value, json};

mod pal {
    use ratatui::style::Color;
    pub const BG: Color = Color::Rgb(10, 10, 10);
    pub const BG2: Color = Color::Rgb(20, 20, 20);
    pub const FG: Color = Color::Rgb(225, 225, 225);
    pub const DIM: Color = Color::Rgb(108, 108, 108);
    pub const BLUE: Color = Color::Rgb(122, 162, 247);
    pub const CYAN: Color = Color::Rgb(125, 207, 255);
    pub const GREEN: Color = Color::Rgb(158, 206, 106);
    pub const ORANGE: Color = Color::Rgb(255, 158, 100);
    pub const MAGENTA: Color = Color::Rgb(187, 154, 247);
    pub const YELLOW: Color = Color::Rgb(224, 175, 104);
    pub const RED: Color = Color::Rgb(247, 118, 142);
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Origin {
    User,
    Llm,
}

struct TrajItem {
    kind: &'static str,
    text: String,
}

struct CallItem {
    origin: Origin,
    kind: &'static str, // "complete" | "syscall"
    title: String,
    detail: String,
}

struct Bridge {
    child: Child,
    stdin: ChildStdin,
    rx: Receiver<Value>,
}

impl Bridge {
    fn spawn(python: &str, cwd: &str) -> io::Result<Self> {
        let mut child = Command::new(python)
            .args(["-m", "desmos", "bridge", "--cwd", cwd])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "bridge stdin"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "bridge stdout"))?;
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if line.trim().is_empty() {
                    continue;
                }
                let v = serde_json::from_str(&line)
                    .unwrap_or_else(|e| json!({"ev":"error","text": e.to_string()}));
                if tx.send(v).is_err() {
                    break;
                }
            }
        });
        Ok(Self { child, stdin, rx })
    }

    fn send(&mut self, msg: &Value) -> io::Result<()> {
        writeln!(self.stdin, "{msg}")?;
        self.stdin.flush()
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

struct App {
    prompt: String,
    model: String,
    thinking: String,
    generation: String,
    running: bool,
    status: String,
    traj: Vec<TrajItem>,
    calls: Vec<CallItem>,
    traj_scroll: u16,
    call_scroll: u16,
    follow_traj: bool,
    follow_calls: bool,
    traj_area: Rect,
    call_area: Rect,
}

impl App {
    fn push_traj(&mut self, kind: &'static str, text: impl Into<String>) {
        let text = text.into();
        if text.trim().is_empty() {
            return;
        }
        self.traj.push(TrajItem { kind, text });
        self.follow_traj = true;
    }

    fn push_call(
        &mut self,
        origin: Origin,
        kind: &'static str,
        title: impl Into<String>,
        detail: impl Into<String>,
    ) {
        self.calls.push(CallItem {
            origin,
            kind,
            title: title.into(),
            detail: clip(&detail.into(), 280),
        });
        self.follow_calls = true;
    }
}

fn clip(s: &str, n: usize) -> String {
    if s.len() <= n {
        s.to_string()
    } else {
        format!("{}…", &s[..n])
    }
}

fn parse_args() -> (String, String, bool) {
    let python = std::env::var("DESMOS_PYTHON").unwrap_or_else(|_| "python3".into());
    let cwd = std::env::current_dir()
        .map(|p| p.display().to_string())
        .unwrap_or_else(|_| ".".into());
    let mut python = python;
    let mut cwd = cwd;
    let mut demo = false;
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--python" if i + 1 < args.len() => {
                python = args[i + 1].clone();
                i += 2;
            }
            "--cwd" if i + 1 < args.len() => {
                cwd = args[i + 1].clone();
                i += 2;
            }
            "--demo" => {
                demo = true;
                i += 1;
            }
            _ => i += 1,
        }
    }
    (python, cwd, demo)
}

fn seed_demo(app: &mut App) {
    app.model = "claude-opus-5".into();
    app.thinking = "low".into();
    app.generation = "4".into();
    app.status = "demo".into();
    // middle = story only
    app.push_traj("you", "look around the kernel and list what you can grow");
    app.push_traj(
        "think",
        "cwd is mine. peek ns, then fire the smallest probe.",
    );
    app.push_traj(
        "speech",
        "ns has **CWD**. Frozen: `python` `bash` `edit` `register`.\n\n- grown: usage\n- grown: traj\n- grown: agents",
    );
    app.push_traj("you", "ok check cache");
    app.push_traj(
        "speech",
        "## cache\n\nhit rate **87%** on the last `complete()`. 3 calls this step.",
    );
    // right = wire only. not the same sentences.
    app.push_call(
        Origin::User,
        "complete",
        "complete #1",
        "POST /v1/messages  opus-5  adaptive/low\nin 1.2k  cache_read 0  out 380",
    );
    app.push_call(
        Origin::Llm,
        "syscall",
        "<python>",
        "sorted(k for k in world.ns if not k.startswith('_'))\n→ ['CWD']",
    );
    app.push_call(
        Origin::Llm,
        "syscall",
        "<bash>",
        "ls .desmos/generations\n→ 0001.json 0002.json 0003.json 0004.json",
    );
    app.push_call(
        Origin::Llm,
        "complete",
        "complete #2",
        "POST /v1/messages  (after <result>)\nin 80  cache_read 11.4k  out 120  hit 99%",
    );
    app.push_call(
        Origin::User,
        "complete",
        "complete #3",
        "POST /v1/messages  user: ok check cache\nin 90  cache_read 12.1k  out 60  hit 99%",
    );
    app.push_call(
        Origin::Llm,
        "syscall",
        "<usage>",
        "3 calls  cache hit 87%  est $0.04",
    );
}

fn main() -> io::Result<()> {
    let (python, cwd, demo) = parse_args();
    let mut bridge = if demo {
        None
    } else {
        Some(Bridge::spawn(&python, &cwd)?)
    };
    let mut app = App {
        prompt: String::new(),
        model: String::new(),
        thinking: String::new(),
        generation: String::new(),
        running: false,
        status: "idle".into(),
        traj: Vec::new(),
        calls: Vec::new(),
        traj_scroll: 0,
        call_scroll: 0,
        follow_traj: true,
        follow_calls: true,
        traj_area: Rect::default(),
        call_area: Rect::default(),
    };
    if demo {
        seed_demo(&mut app);
    }

    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let mut terminal = Terminal::new(CrosstermBackend::new(stdout))?;
    let result = run(&mut terminal, bridge.as_mut(), &mut app);
    disable_raw_mode()?;
    execute!(io::stdout(), DisableMouseCapture, LeaveAlternateScreen)?;
    result
}

fn run(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    mut bridge: Option<&mut Bridge>,
    app: &mut App,
) -> io::Result<()> {
    loop {
        if let Some(b) = bridge.as_mut() {
            loop {
                match b.rx.try_recv() {
                    Ok(ev) => handle_event(app, ev),
                    Err(TryRecvError::Empty) => break,
                    Err(TryRecvError::Disconnected) => {
                        app.status = "bridge died".into();
                        app.running = false;
                    }
                }
            }
        }

        terminal.draw(|f| draw(f, app))?;

        if event::poll(Duration::from_millis(80))? {
            match event::read()? {
                Event::Key(key) => {
                    if handle_key(bridge.as_deref_mut(), app, key)? {
                        return Ok(());
                    }
                }
                Event::Mouse(m) => handle_mouse(app, m),
                Event::Resize(_, _) => {}
                _ => {}
            }
        }
    }
}

fn handle_event(app: &mut App, ev: Value) {
    let kind = ev.get("ev").and_then(Value::as_str).unwrap_or("");
    match kind {
        "ready" | "snapshot" => {
            if let Some(s) = ev.get("model").and_then(Value::as_str) {
                app.model = s.into();
            }
            if let Some(s) = ev.get("thinking").and_then(Value::as_str) {
                app.thinking = s.into();
            }
            if let Some(n) = ev.get("generation").and_then(Value::as_u64) {
                app.generation = n.to_string();
            } else if let Some(s) = ev.get("generation").and_then(Value::as_str) {
                app.generation = s.into();
            }
            if !app.running {
                app.status = "idle".into();
            }
        }
        "thinking" => {
            if let Some(t) = ev.get("text").and_then(Value::as_str) {
                app.push_traj("think", t);
            }
        }
        "speech" => {
            if let Some(t) = ev.get("text").and_then(Value::as_str) {
                let spoken = strip_tags(t);
                if !spoken.is_empty() {
                    app.push_traj("speech", spoken);
                }
            }
        }
        "result" => {
            let tag = ev.get("tag").and_then(Value::as_str).unwrap_or("?");
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            app.push_call(Origin::Llm, "syscall", format!("<{tag}>"), text);
        }
        "complete" => {
            let n = ev.get("n").and_then(Value::as_u64).unwrap_or(0);
            let origin = match ev.get("origin").and_then(Value::as_str) {
                Some("user") => Origin::User,
                _ => Origin::Llm,
            };
            let model = ev.get("model").and_then(Value::as_str).unwrap_or("?");
            let thinking = ev.get("thinking").and_then(Value::as_str).unwrap_or("");
            let usage = ev.get("usage").cloned().unwrap_or(json!({}));
            let detail = format_usage(model, thinking, &usage);
            app.push_call(origin, "complete", format!("complete #{n}"), detail);
        }
        "turn" => {
            app.status = "running".into();
        }
        "done" => {
            app.running = false;
            app.status = "idle".into();
        }
        "error" => {
            let t = ev.get("text").and_then(Value::as_str).unwrap_or("error");
            app.push_traj("err", t);
            app.running = false;
            app.status = "error".into();
        }
        _ => {}
    }
}

fn handle_key(
    mut bridge: Option<&mut Bridge>,
    app: &mut App,
    key: KeyEvent,
) -> io::Result<bool> {
    if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
        return Ok(true);
    }
    match key.code {
        KeyCode::Esc => return Ok(true),
        KeyCode::PageUp => {
            app.follow_traj = false;
            app.traj_scroll = app.traj_scroll.saturating_sub(4);
        }
        KeyCode::PageDown => {
            app.traj_scroll = app.traj_scroll.saturating_add(4);
            app.follow_traj = true;
        }
        KeyCode::Char(c) if key.modifiers.contains(KeyModifiers::CONTROL) && c == 'k' => {
            app.follow_calls = false;
            app.call_scroll = app.call_scroll.saturating_sub(4);
        }
        KeyCode::Char(c) if key.modifiers.contains(KeyModifiers::CONTROL) && c == 'j' => {
            app.call_scroll = app.call_scroll.saturating_add(4);
            app.follow_calls = true;
        }
        KeyCode::Char(c) if !app.running => app.prompt.push(c),
        KeyCode::Backspace if !app.running => {
            app.prompt.pop();
        }
        KeyCode::Enter if !app.running => {
            let line = app.prompt.trim().to_string();
            app.prompt.clear();
            if line.is_empty() {
                return Ok(false);
            }
            if line == "/quit" || line == "/exit" {
                return Ok(true);
            }
            app.push_traj("you", &line);
            if let Some(level) = line.strip_prefix("/thinking") {
                let level = level.trim();
                if !level.is_empty() {
                    if let Some(b) = bridge.as_mut() {
                        b.send(&json!({"op":"thinking","level": level}))?;
                    }
                    app.thinking = level.into();
                }
            } else if line == "/reset" {
                if let Some(b) = bridge.as_mut() {
                    b.send(&json!({"op":"reset"}))?;
                }
                app.traj.clear();
                app.calls.clear();
            } else if line == "/reload" {
                if let Some(b) = bridge.as_mut() {
                    b.send(&json!({"op":"reload"}))?;
                }
            } else if let Some(b) = bridge.as_mut() {
                app.running = true;
                app.status = "running".into();
                b.send(&json!({"op":"step","text": line}))?;
            } else {
                app.push_call(
                    Origin::User,
                    "complete",
                    "complete (demo)",
                    "no kernel attached — drop --demo to POST for real",
                );
            }
        }
        _ => {}
    }
    Ok(false)
}

fn hit(area: Rect, col: u16, row: u16) -> bool {
    col >= area.x
        && col < area.x.saturating_add(area.width)
        && row >= area.y
        && row < area.y.saturating_add(area.height)
}

fn scroll_pane(follow: &mut bool, offset: &mut u16, up: bool) {
    *follow = false;
    if up {
        *offset = offset.saturating_sub(3);
    } else {
        *offset = offset.saturating_add(3);
    }
}

fn handle_mouse(app: &mut App, m: MouseEvent) {
    let up = matches!(m.kind, MouseEventKind::ScrollUp);
    let down = matches!(m.kind, MouseEventKind::ScrollDown);
    if !up && !down {
        return;
    }
    if hit(app.call_area, m.column, m.row) {
        scroll_pane(&mut app.follow_calls, &mut app.call_scroll, up);
    } else if hit(app.traj_area, m.column, m.row) {
        scroll_pane(&mut app.follow_traj, &mut app.traj_scroll, up);
    } else {
        // default: right pane if the cursor is on that half, else trajectory
        scroll_pane(&mut app.follow_calls, &mut app.call_scroll, up);
    }
}

fn draw(f: &mut Frame, app: &mut App) {
    let bg = Style::default().bg(pal::BG).fg(pal::FG);
    f.render_widget(Block::default().style(bg), f.area());

    let cols = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Min(6), Constraint::Length(4)])
        .split(f.area());
    let panes = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(62), Constraint::Percentage(38)])
        .split(cols[0]);

    app.traj_area = panes[0];
    app.call_area = panes[1];
    draw_traj(f, panes[0], app);
    draw_calls(f, panes[1], app);
    draw_input(f, cols[1], app);
}

fn draw_traj(f: &mut Frame, area: Rect, app: &mut App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(pal::BLUE))
        .title(Span::styled(
            " trajectory ",
            Style::default().fg(pal::BLUE).add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(pal::BG2).fg(pal::FG));
    let mut lines = Vec::new();
    for item in &app.traj {
        let (label, color) = match item.kind {
            "you" => ("you", pal::YELLOW),
            "think" => ("think", pal::DIM),
            "speech" => ("out", pal::GREEN),
            "turn" => ("turn", pal::CYAN),
            "err" => ("err", pal::RED),
            _ => (item.kind, pal::FG),
        };
        lines.push(Line::from(vec![
            Span::styled(format!("{label:<6}"), Style::default().fg(color)),
            Span::raw(" "),
        ]));
        let width = area.width.saturating_sub(8) as usize;
        if item.kind == "speech" {
            lines.extend(md_lines(&item.text, width));
        } else {
            for row in wrap(&item.text, width) {
                lines.push(Line::from(Span::styled(row, Style::default().fg(pal::FG))));
            }
        }
        lines.push(Line::from(""));
    }
    if lines.is_empty() {
        lines.push(Line::from(Span::styled(
            "story only — what you said, what it thought, what it answered.",
            Style::default().fg(pal::DIM),
        )));
    }
    let inner = area.height.saturating_sub(2);
    if app.follow_traj {
        app.traj_scroll = lines.len().saturating_sub(inner as usize) as u16;
    }
    f.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false })
            .scroll((app.traj_scroll, 0)),
        area,
    );
}

fn draw_calls(f: &mut Frame, area: Rect, app: &mut App) {
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(pal::ORANGE))
        .title(Span::styled(
            " calls ",
            Style::default().fg(pal::ORANGE).add_modifier(Modifier::BOLD),
        ))
        .style(Style::default().bg(pal::BG2).fg(pal::FG));
    let mut lines = Vec::new();
    for c in &app.calls {
        let (who, color) = match c.origin {
            Origin::User => ("USER", pal::YELLOW),
            Origin::Llm => ("LLM", pal::MAGENTA),
        };
        let title_color = if c.kind == "complete" {
            pal::ORANGE
        } else {
            pal::CYAN
        };
        lines.push(Line::from(vec![
            Span::styled(
                format!("{who} "),
                Style::default().fg(color).add_modifier(Modifier::BOLD),
            ),
            Span::styled(c.title.clone(), Style::default().fg(title_color)),
        ]));
        for row in wrap(&c.detail, area.width.saturating_sub(4) as usize) {
            lines.push(Line::from(Span::styled(
                format!("  {row}"),
                Style::default().fg(pal::DIM),
            )));
        }
        lines.push(Line::from(""));
    }
    if lines.is_empty() {
        lines.push(Line::from(Span::styled(
            "wire only — complete() and syscalls.",
            Style::default().fg(pal::DIM),
        )));
        lines.push(Line::from(Span::styled(
            "USER = your enter started the POST. LLM = the model called again.",
            Style::default().fg(pal::DIM),
        )));
    }
    let inner = area.height.saturating_sub(2);
    if app.follow_calls {
        app.call_scroll = lines.len().saturating_sub(inner as usize) as u16;
    }
    f.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false })
            .scroll((app.call_scroll, 0)),
        area,
    );
}

fn draw_input(f: &mut Frame, area: Rect, app: &App) {
    let status = format!(
        " desmos  {}  think:{}  gen {}  {}   pgup/pgdn traj  ^j/^k calls  esc quit",
        if app.model.is_empty() {
            "—"
        } else {
            &app.model
        },
        if app.thinking.is_empty() {
            "—"
        } else {
            &app.thinking
        },
        if app.generation.is_empty() {
            "—"
        } else {
            &app.generation
        },
        app.status,
    );
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Min(2)])
        .split(area);
    f.render_widget(
        Paragraph::new(Line::from(Span::styled(status, Style::default().fg(pal::DIM).bg(pal::BG)))),
        chunks[0],
    );
    let prefix = if app.running { " … " } else { " ❯ " };
    let prompt = format!("{prefix}{}", app.prompt);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(if app.running { pal::DIM } else { pal::GREEN }))
        .title(Span::styled(" input ", Style::default().fg(pal::GREEN)));
    f.render_widget(
        Paragraph::new(Span::styled(prompt, Style::default().fg(pal::FG)))
            .block(block)
            .style(Style::default().bg(pal::BG2)),
        chunks[1],
    );
}

fn md_lines(text: &str, width: usize) -> Vec<Line<'static>> {
    use pulldown_cmark::{CodeBlockKind, Event, Options, Parser, Tag, TagEnd};
    let mut opts = Options::empty();
    opts.insert(Options::ENABLE_STRIKETHROUGH);
    let mut lines = Vec::new();
    let mut spans: Vec<Span<'static>> = Vec::new();
    let mut style = Style::default().fg(pal::FG);
    let mut prefix = String::new();
    let flush = |spans: &mut Vec<Span<'static>>, lines: &mut Vec<Line<'static>>| {
        if spans.is_empty() {
            lines.push(Line::from(""));
        } else {
            lines.push(Line::from(std::mem::take(spans)));
        }
    };
    for ev in Parser::new_ext(text, opts) {
        match ev {
            Event::Start(Tag::Heading { .. }) => {
                if !spans.is_empty() {
                    flush(&mut spans, &mut lines);
                }
                style = Style::default().fg(pal::CYAN).add_modifier(Modifier::BOLD);
            }
            Event::End(TagEnd::Heading(_)) => {
                flush(&mut spans, &mut lines);
                style = Style::default().fg(pal::FG);
            }
            Event::Start(Tag::Emphasis) => {
                style = style.add_modifier(Modifier::ITALIC).fg(pal::FG);
            }
            Event::End(TagEnd::Emphasis) => {
                style = Style::default().fg(pal::FG);
            }
            Event::Start(Tag::Strong) => {
                style = style.add_modifier(Modifier::BOLD).fg(pal::YELLOW);
            }
            Event::End(TagEnd::Strong) => {
                style = Style::default().fg(pal::FG);
            }
            Event::Start(Tag::Item) => {
                if !spans.is_empty() {
                    flush(&mut spans, &mut lines);
                }
                prefix = "• ".into();
            }
            Event::End(TagEnd::Item) => {
                flush(&mut spans, &mut lines);
                prefix.clear();
            }
            Event::Start(Tag::CodeBlock(CodeBlockKind::Fenced(_)) | Tag::CodeBlock(CodeBlockKind::Indented)) => {
                if !spans.is_empty() {
                    flush(&mut spans, &mut lines);
                }
                style = Style::default().fg(pal::CYAN).bg(pal::BG);
            }
            Event::End(TagEnd::CodeBlock) => {
                flush(&mut spans, &mut lines);
                style = Style::default().fg(pal::FG);
            }
            Event::Code(s) => {
                let body = if prefix.is_empty() {
                    s.to_string()
                } else {
                    format!("{prefix}{s}")
                };
                prefix.clear();
                spans.push(Span::styled(
                    body,
                    Style::default().fg(pal::CYAN).bg(pal::BG),
                ));
            }
            Event::Text(s) => {
                let body = if prefix.is_empty() {
                    s.to_string()
                } else {
                    let p = std::mem::take(&mut prefix);
                    format!("{p}{s}")
                };
                for (i, row) in wrap(&body, width.max(8)).into_iter().enumerate() {
                    if i > 0 {
                        flush(&mut spans, &mut lines);
                    }
                    spans.push(Span::styled(row, style));
                }
            }
            Event::SoftBreak | Event::HardBreak => flush(&mut spans, &mut lines),
            Event::End(TagEnd::Paragraph) => {
                flush(&mut spans, &mut lines);
                lines.push(Line::from(""));
            }
            _ => {}
        }
    }
    if !spans.is_empty() {
        flush(&mut spans, &mut lines);
    }
    if lines.is_empty() {
        for row in wrap(text, width.max(8)) {
            lines.push(Line::from(Span::styled(row, Style::default().fg(pal::FG))));
        }
    }
    lines
}

fn strip_tags(text: &str) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(start) = rest.find('<') {
        out.push_str(&rest[..start]);
        match rest[start..].find('>') {
            Some(end) => {
                let tag = &rest[start + 1..start + end];
                if tag.starts_with('/') {
                    out.push(' ');
                }
                rest = &rest[start + end + 1..];
            }
            None => {
                out.push_str(rest);
                rest = "";
                break;
            }
        }
    }
    out.push_str(rest);
    out.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn format_usage(model: &str, thinking: &str, usage: &Value) -> String {
    let get = |k: &str| usage.get(k).and_then(Value::as_u64).unwrap_or(0);
    let fresh = get("input_tokens");
    let read = get("cache_read_input_tokens");
    let write = get("cache_creation_input_tokens");
    let out = get("output_tokens");
    let total = fresh + read + write;
    let hit = if total == 0 {
        0
    } else {
        100 * read / total
    };
    format!("POST /v1/messages  {model}  think={thinking}\nin {fresh}  cache_r {read}  cache_w {write}  out {out}  hit {hit}%")
}

fn wrap(text: &str, width: usize) -> Vec<String> {
    if width < 8 {
        return vec![text.to_string()];
    }
    let mut out = Vec::new();
    for para in text.lines() {
        let mut rest = para;
        if rest.is_empty() {
            out.push(String::new());
            continue;
        }
        while rest.len() > width {
            let mut cut = width;
            while cut > 0 && !rest.is_char_boundary(cut) {
                cut -= 1;
            }
            if cut == 0 {
                break;
            }
            out.push(rest[..cut].to_string());
            rest = &rest[cut..];
        }
        if !rest.is_empty() {
            out.push(rest.to_string());
        }
    }
    out
}
