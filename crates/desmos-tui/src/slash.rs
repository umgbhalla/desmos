//! Slash commands: what exists, what they take, and whether what you typed
//! will work — answered while you type rather than after you send.
//!
//! Every one of these was already implemented and reachable only by knowing
//! it existed. `/model` in particular had a bridge op from the start and no
//! way to reach it but the picker. A command surface that has to be
//! remembered is a command surface most people never use.
//!
//! Parameter values are not hardcoded. The bridge publishes its real catalog
//! in the ready/snapshot event and the picker already parses it, so a model
//! this build cannot actually run is never offered.

use crate::picker::Picker;

/// What a command takes after its name.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Param {
    None,
    /// A model id, from whichever providers have a usable credential.
    Model,
    /// An effort level, valid for the provider that owns the current model.
    Effort,
    Theme,
}

pub struct Command {
    pub name: &'static str,
    pub help: &'static str,
    pub param: Param,
    /// Whether typing a space should offer a completion list for the argument.
    /// Off for /model: the picker already shows provider, model, effort, live
    /// auth status and a tick on what is running, so a flat list of model names
    /// is a worse second surface for the same job -- and having both made
    /// /model two levels deep on the way to the surface that does the work.
    /// The param stays, so a typed-out `/model gpt-9` is still caught as a bad
    /// argument rather than sent.
    pub list: bool,
}

pub const THEMES: [&str; 6] = [
    "groknight",
    "tokyonight",
    "grokday",
    "rosepine",
    "oscura",
    "auto",
];

pub const COMMANDS: [Command; 9] = [
    Command { name: "/model", help: "model and thinking level", param: Param::Model, list: false },
    Command { name: "/thinking", help: "reasoning effort", param: Param::Effort, list: true },
    Command { name: "/theme", help: "colour scheme", param: Param::Theme, list: true },
    Command { name: "/dense", help: "tighter rows (spacing, not transcript folding)", param: Param::None, list: true },
    Command { name: "/timestamps", help: "show times on blocks", param: Param::None, list: true },
    Command { name: "/reload", help: "rediscover skills and extensions", param: Param::None, list: true },
    Command { name: "/reset", help: "clear the transcript, keep ns and notes", param: Param::None, list: true },
    Command { name: "/quit", help: "leave", param: Param::None, list: true },
    Command { name: "/exit", help: "leave", param: Param::None, list: true },
];

/// Live judgement on the line in the composer.
#[derive(Clone, PartialEq, Eq, Debug)]
pub enum Verdict {
    /// Not a slash line at all — ordinary prose.
    NotACommand,
    /// Complete and runnable.
    Ready,
    /// A real command still missing its argument.
    NeedsArg(&'static str),
    /// Typed far enough to be wrong.
    Unknown(String),
    BadArg { got: String, expected: String },
}

/// The completion list, recomputed from the composer on every keystroke.
#[derive(Default)]
pub struct Slash {
    pub open: bool,
    pub sel: usize,
    pub items: Vec<Item>,
    /// The part of the line the accepted item replaces.
    head: String,
}

#[derive(Clone, PartialEq, Eq, Debug)]
pub struct Item {
    pub text: String,
    pub help: String,
}

fn split(line: &str) -> Option<(&str, Option<&str>)> {
    let t = line.strip_prefix('/')?;
    if t.contains('\n') {
        return None;
    }
    match t.split_once(' ') {
        Some((cmd, rest)) => Some((cmd, Some(rest))),
        None => Some((t, None)),
    }
}

fn find(cmd: &str) -> Option<&'static Command> {
    COMMANDS.iter().find(|c| c.name == format!("/{cmd}"))
}

/// Values a parameter accepts right now, straight from the bridge's catalog.
pub fn values(param: Param, picker: &Picker) -> Vec<String> {
    match param {
        Param::None => Vec::new(),
        Param::Theme => THEMES.iter().map(|s| s.to_string()).collect(),
        // Only providers that answered "ok" — offering a model with no
        // credential behind it is offering a guaranteed error.
        Param::Model => picker
            .providers
            .iter()
            .filter(|p| p.ok)
            .flat_map(|p| p.models.iter().cloned())
            .collect(),
        Param::Effort => picker
            .providers
            .iter()
            .filter(|p| p.ok)
            .flat_map(|p| p.efforts.iter().cloned())
            .fold(Vec::new(), |mut acc, e| {
                if !acc.contains(&e) {
                    acc.push(e);
                }
                acc
            }),
    }
}

/// Is what is typed going to work? Answered without sending it.
pub fn verdict(line: &str, picker: &Picker) -> Verdict {
    let line = line.trim_end_matches(' ');
    let Some((cmd, arg)) = split(line) else {
        return Verdict::NotACommand;
    };
    let Some(found) = find(cmd) else {
        // Still typing a prefix of something real is not yet wrong.
        if COMMANDS.iter().any(|c| c.name.starts_with(&format!("/{cmd}"))) {
            return Verdict::NeedsArg("keep typing");
        }
        return Verdict::Unknown(format!("/{cmd}"));
    };
    match (found.param, arg.map(str::trim).filter(|a| !a.is_empty())) {
        (Param::None, _) => Verdict::Ready,
        // `/model` alone is meaningful: it opens the picker.
        (Param::Model, None) => Verdict::Ready,
        (_, None) => Verdict::NeedsArg(found.help),
        (p, Some(a)) => {
            let ok = values(p, picker);
            if ok.iter().any(|v| v == a) {
                Verdict::Ready
            } else {
                Verdict::BadArg { got: a.to_string(), expected: ok.join(" ") }
            }
        }
    }
}

impl Slash {
    /// Recompute from the composer. Closes itself when the line stops being a
    /// command, so nothing has to remember to dismiss it.
    pub fn update(&mut self, line: &str, picker: &Picker) {
        let Some((cmd, arg)) = split(line) else {
            self.close();
            return;
        };
        let (head, needle, items) = match arg {
            None => {
                let needle = cmd.to_ascii_lowercase();
                let items = COMMANDS
                    .iter()
                    .filter(|c| c.name[1..].starts_with(&needle))
                    .map(|c| Item { text: c.name.to_string(), help: c.help.to_string() })
                    .collect::<Vec<_>>();
                (String::new(), needle, items)
            }
            Some(rest) => {
                let Some(found) = find(cmd) else {
                    self.close();
                    return;
                };
                if found.param == Param::None || !found.list {
                    self.close();
                    return;
                }
                let needle = rest.trim_start().to_ascii_lowercase();
                let items = values(found.param, picker)
                    .into_iter()
                    .filter(|v| v.to_ascii_lowercase().starts_with(&needle))
                    .map(|v| Item { text: v, help: String::new() })
                    .collect::<Vec<_>>();
                (format!("{} ", found.name), needle, items)
            }
        };
        let _ = needle;
        self.head = head;
        // Keep the cursor on the same entry while it still exists, so typing a
        // narrowing character does not silently move the selection.
        let previous = self.items.get(self.sel).map(|i| i.text.clone());
        self.items = items;
        self.sel = previous
            .and_then(|p| self.items.iter().position(|i| i.text == p))
            .unwrap_or(0);
        self.open = !self.items.is_empty();
    }

    pub fn close(&mut self) {
        self.open = false;
        self.items.clear();
        self.sel = 0;
    }

    pub fn move_sel(&mut self, by: i32) {
        if self.items.is_empty() {
            return;
        }
        let n = self.items.len() as i32;
        self.sel = (((self.sel as i32 + by) % n + n) % n) as usize;
    }

    /// True only for the theme argument list, not the `/theme` command row.
    pub fn is_theme_values(&self) -> bool {
        self.open && self.head == "/theme "
    }

    pub fn selected_text(&self) -> Option<&str> {
        self.items.get(self.sel).map(|item| item.text.as_str())
    }

    /// The whole composer line after accepting the highlighted entry. A
    /// command that takes an argument keeps the trailing space, so the next
    /// keystroke lands in the argument and the list refills with its values.
    pub fn accept(&self) -> Option<String> {
        let item = self.items.get(self.sel)?;
        if self.head.is_empty() {
            let takes_arg = COMMANDS
                .iter()
                .find(|c| c.name == item.text)
                .is_some_and(|c| c.param != Param::None && c.list);
            return Some(if takes_arg { format!("{} ", item.text) } else { item.text.clone() });
        }
        Some(format!("{}{}", self.head, item.text))
    }
}
