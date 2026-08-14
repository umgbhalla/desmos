//! First-run onboarding: pick a provider, a model, an effort.
//!
//! Nothing here knows a model name. The bridge's ready event carries the whole
//! catalog -- providers, their auth status, their models and efforts -- so a
//! model added in Python shows up here without a Rust change.
//!
//! The picker is also the auth screen: a provider with no credential cannot be
//! chosen, and Enter on it starts a login instead of advancing.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Widget};
use serde_json::Value;

use xai_grok_pager::theme::Theme;
use xai_grok_pager::views::modal_window::{
    ModalSizing, ModalWindowConfig, ModalWindowState, Shortcut, render_modal_window,
};

#[derive(Clone, Debug, Default)]
pub struct ProviderRow {
    pub name: String,
    pub ok: bool,
    pub detail: String,
    pub account: String,
    pub plan: String,
    pub can_login: bool,
    pub models: Vec<String>,
    pub efforts: Vec<String>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Stage {
    Provider,
    Model,
    Effort,
}

/// What the caller must do on the wire. The picker never talks to the bridge.
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum PickerAction {
    None,
    Login { provider: String },
    Apply { model: String, effort: String },
    Close,
}

pub struct Picker {
    pub open: bool,
    pub stage: Stage,
    pub providers: Vec<ProviderRow>,
    pub provider_idx: usize,
    pub model_idx: usize,
    pub effort_idx: usize,
    pub sel: usize,
    /// Progress from a running login: the consent URL or the device code.
    pub login: Vec<String>,
    pub logging_in: bool,
    /// Set when a session is already configured, so Esc can mean "keep it".
    pub configured: bool,
    pub modal: ModalWindowState,
}

impl Default for Picker {
    fn default() -> Self {
        Self {
            open: false,
            stage: Stage::Provider,
            providers: Vec::new(),
            provider_idx: 0,
            model_idx: 0,
            effort_idx: 0,
            sel: 0,
            login: Vec::new(),
            logging_in: false,
            configured: false,
            modal: ModalWindowState::with_tabs(1),
        }
    }
}

impl Picker {
    /// Fold a `ready` or `picker` event. Opens itself on a machine with no
    /// saved choice; a later event never re-opens a picker the user closed.
    pub fn observe(&mut self, ev: &Value) {
        let Some(list) = ev.get("providers").and_then(Value::as_array) else {
            return;
        };
        let keep = self.current_provider().map(|p| p.name.clone());
        self.providers = list
            .iter()
            .map(|p| ProviderRow {
                name: p.get("provider").and_then(Value::as_str).unwrap_or("").to_string(),
                ok: p.get("ok").and_then(Value::as_bool).unwrap_or(false),
                detail: p.get("detail").and_then(Value::as_str).unwrap_or("").to_string(),
                account: p.get("account").and_then(Value::as_str).unwrap_or("").to_string(),
                plan: p.get("plan").and_then(Value::as_str).unwrap_or("").to_string(),
                can_login: p.get("can_login").and_then(Value::as_bool).unwrap_or(false),
                models: str_list(p.get("models")),
                efforts: str_list(p.get("efforts")),
            })
            .collect();
        if let Some(name) = keep {
            if let Some(i) = self.providers.iter().position(|p| p.name == name) {
                self.provider_idx = i;
            }
        }
        self.provider_idx = self.provider_idx.min(self.providers.len().saturating_sub(1));
        let configured = ev.get("current").map(|c| !c.is_null()).unwrap_or(self.configured);
        self.configured = configured;
        if let Some(cur) = ev.get("current").filter(|c| !c.is_null()) {
            let model = cur.get("model").and_then(Value::as_str).unwrap_or("");
            let effort = cur.get("effort").and_then(Value::as_str).unwrap_or("");
            self.point_at(model, effort);
        }
        if ev.get("ev").and_then(Value::as_str) == Some("ready")
            && ev.get("onboarding").and_then(Value::as_bool).unwrap_or(false)
        {
            self.open = true;
            self.stage = Stage::Provider;
            self.sel = self.provider_idx;
        }
    }

    /// Move the cursors onto a model that is already in use.
    pub fn point_at(&mut self, model: &str, effort: &str) {
        for (i, p) in self.providers.iter().enumerate() {
            if let Some(j) = p.models.iter().position(|m| m == model) {
                self.provider_idx = i;
                self.model_idx = j;
                if let Some(k) = p.efforts.iter().position(|e| e == effort) {
                    self.effort_idx = k;
                }
                return;
            }
        }
    }

    pub fn login_line(&mut self, text: &str, done: bool) {
        for line in text.lines() {
            if !line.trim().is_empty() {
                self.login.push(line.to_string());
            }
        }
        if self.login.len() > 6 {
            let cut = self.login.len() - 6;
            self.login.drain(..cut);
        }
        if done {
            self.logging_in = false;
        }
    }

    pub fn open_for_change(&mut self) {
        self.open = true;
        self.stage = Stage::Provider;
        self.sel = self.provider_idx;
        self.login.clear();
    }

    pub fn current_provider(&self) -> Option<&ProviderRow> {
        self.providers.get(self.provider_idx)
    }

    fn rows(&self) -> usize {
        match self.stage {
            Stage::Provider => self.providers.len(),
            Stage::Model => self.current_provider().map(|p| p.models.len()).unwrap_or(0),
            Stage::Effort => self.current_provider().map(|p| p.efforts.len()).unwrap_or(0),
        }
    }

    pub fn key(&mut self, code: crossterm::event::KeyCode) -> PickerAction {
        use crossterm::event::KeyCode as K;
        let n = self.rows();
        match code {
            K::Char('j') | K::Down => {
                if n > 0 {
                    self.sel = (self.sel + 1) % n;
                }
            }
            K::Char('k') | K::Up => {
                if n > 0 {
                    self.sel = (self.sel + n - 1) % n;
                }
            }
            K::Esc | K::Char('q') => {
                // A configured session can keep what it had. A fresh one cannot
                // close the picker, because there is nothing behind it yet.
                if self.configured {
                    self.open = false;
                    return PickerAction::Close;
                }
            }
            K::Left | K::Char('h') => match self.stage {
                Stage::Model => {
                    self.stage = Stage::Provider;
                    self.sel = self.provider_idx;
                }
                Stage::Effort => {
                    self.stage = Stage::Model;
                    self.sel = self.model_idx;
                }
                Stage::Provider => {}
            },
            K::Enter | K::Right | K::Char('l') => return self.advance(),
            _ => {}
        }
        PickerAction::None
    }

    fn advance(&mut self) -> PickerAction {
        match self.stage {
            Stage::Provider => {
                let Some(p) = self.providers.get(self.sel) else {
                    return PickerAction::None;
                };
                if !p.ok {
                    if p.can_login && !self.logging_in {
                        self.logging_in = true;
                        self.login.clear();
                        self.login.push("starting sign-in...".into());
                        return PickerAction::Login { provider: p.name.clone() };
                    }
                    return PickerAction::None;
                }
                self.provider_idx = self.sel;
                self.model_idx = self.model_idx.min(p.models.len().saturating_sub(1));
                self.stage = Stage::Model;
                self.sel = self.model_idx;
            }
            Stage::Model => {
                self.model_idx = self.sel;
                let n = self.current_provider().map(|p| p.efforts.len()).unwrap_or(0);
                self.effort_idx = self.effort_idx.min(n.saturating_sub(1));
                self.stage = Stage::Effort;
                self.sel = self.effort_idx;
            }
            Stage::Effort => {
                self.effort_idx = self.sel;
                let Some(p) = self.current_provider() else {
                    return PickerAction::None;
                };
                let model = p.models.get(self.model_idx).cloned().unwrap_or_default();
                let effort = p.efforts.get(self.effort_idx).cloned().unwrap_or_default();
                self.open = false;
                self.configured = true;
                return PickerAction::Apply { model, effort };
            }
        }
        PickerAction::None
    }

    /// The rows as painted, without a terminal. Tests read this.
    pub fn lines(&self) -> Vec<String> {
        let mut out = Vec::new();
        let dot = |on: bool| if on { "▸" } else { " " };
        out.push(match self.stage {
            Stage::Provider => "provider".to_string(),
            Stage::Model => "model".to_string(),
            Stage::Effort => "effort".to_string(),
        });
        match self.stage {
            Stage::Provider => {
                for (i, p) in self.providers.iter().enumerate() {
                    let state = if p.ok {
                        let who = if !p.plan.is_empty() { p.plan.clone() } else { "signed in".into() };
                        format!("ok   {who}")
                    } else if p.can_login {
                        "--   enter to sign in".to_string()
                    } else {
                        format!("--   {}", p.detail)
                    };
                    out.push(format!("{} {:<10} {}", dot(i == self.sel), p.name, state));
                }
            }
            Stage::Model => {
                if let Some(p) = self.current_provider() {
                    for (i, m) in p.models.iter().enumerate() {
                        out.push(format!("{} {}", dot(i == self.sel), m));
                    }
                }
            }
            Stage::Effort => {
                if let Some(p) = self.current_provider() {
                    for (i, e) in p.efforts.iter().enumerate() {
                        out.push(format!("{} {}", dot(i == self.sel), e));
                    }
                }
            }
        }
        for line in &self.login {
            out.push(format!("  {line}"));
        }
        out
    }

    pub fn render(&mut self, buf: &mut Buffer, area: Rect) {
        let theme = Theme::current();
        let footer = [
            Shortcut { label: "j/k move", clickable: false, id: 0 },
            Shortcut { label: "enter choose", clickable: false, id: 1 },
            Shortcut { label: "h back", clickable: false, id: 2 },
        ];
        let title = if self.configured { "settings" } else { "welcome" };
        let config = ModalWindowConfig {
            title,
            tabs: None,
            shortcuts: &footer,
            sizing: ModalSizing {
                width_pct: 0.5,
                max_width: 72,
                min_width: 40,
                v_margin: 4,
                h_pad: 2,
                v_pad: 1,
                footer_lines: 2,
            },
            fold_info: None,
        };
        let Some(content) = render_modal_window(buf, area, &mut self.modal, &config, &theme) else {
            return;
        };
        let dim = Style::default().fg(theme.text_secondary);
        let bright = Style::default().fg(theme.text_primary).add_modifier(Modifier::BOLD);
        let mut lines: Vec<Line> = Vec::new();
        let rendered = self.lines();
        for (i, row) in rendered.iter().enumerate() {
            let style = if i == 0 {
                dim
            } else if row.starts_with('▸') {
                bright
            } else {
                Style::default().fg(theme.text_primary)
            };
            lines.push(Line::from(Span::styled(row.clone(), style)));
        }
        Paragraph::new(lines).render(content.content, buf);
    }
}

fn str_list(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}
