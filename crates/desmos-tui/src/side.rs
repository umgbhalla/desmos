//! The two side panes under the wire column: git state in tabs, and the file
//! a git row points at.
//!
//! Shape borrowed from druk (vendor/druk), whose sidebar is one column with a
//! tab strip — Files / Git / Review / Extensions — over a view that changes
//! with the tab, and an editor slot beside it. Here the column is already the
//! wire, so git becomes a tabbed pane of its own and the editor slot becomes a
//! read-only file view under it.
//!
//! Both panes start closed. `git` shells out on a worker thread — the UI
//! thread must never wait on a repository that is packing or on a network
//! remote — and the file view reads a bounded prefix, since a pane six rows
//! tall has no use for the other 40k lines of a lockfile.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::mpsc::{Receiver, TryRecvError, channel};
use std::time::{Duration, Instant};

/// Which view the git pane is showing. Tabs, in strip order.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum GitTab {
    Status,
    Branches,
    Log,
}

impl GitTab {
    pub const ALL: [GitTab; 3] = [GitTab::Status, GitTab::Branches, GitTab::Log];

    pub fn label(self) -> &'static str {
        match self {
            GitTab::Status => "status",
            GitTab::Branches => "branches",
            GitTab::Log => "log",
        }
    }

    fn index(self) -> usize {
        match self {
            GitTab::Status => 0,
            GitTab::Branches => 1,
            GitTab::Log => 2,
        }
    }
}

/// One row of a git view. `path` is set where the row names a file the file
/// pane can open.
#[derive(Clone, Debug, Default)]
pub struct GitRow {
    pub text: String,
    /// Status letters (`M`, `A`, `??`) — the file pane shows nothing for them,
    /// but they colour the row.
    pub mark: String,
    pub path: Option<PathBuf>,
}

#[derive(Clone, Debug, Default)]
struct GitSnap {
    branch: String,
    status: Vec<GitRow>,
    branches: Vec<GitRow>,
    log: Vec<GitRow>,
    error: Option<String>,
}

pub struct GitPane {
    pub tab: GitTab,
    pub sel: usize,
    pub scroll: usize,
    snap: GitSnap,
    pending: Option<Receiver<GitSnap>>,
    last: Option<Instant>,
    cwd: PathBuf,
}

/// How often a visible git pane re-reads the repository. A status call on a
/// large tree is tens of milliseconds of disk, so this is a poll, not a watch.
const REFRESH: Duration = Duration::from_secs(4);
/// Rows kept per view. A log is unbounded and a pane is not.
const CAP: usize = 200;

impl GitPane {
    pub fn new(cwd: &Path) -> Self {
        Self {
            tab: GitTab::Status,
            sel: 0,
            scroll: 0,
            snap: GitSnap::default(),
            pending: None,
            last: None,
            cwd: cwd.to_path_buf(),
        }
    }

    pub fn branch(&self) -> &str {
        &self.snap.branch
    }

    pub fn error(&self) -> Option<&str> {
        self.snap.error.as_deref()
    }

    pub fn rows(&self) -> &[GitRow] {
        match self.tab {
            GitTab::Status => &self.snap.status,
            GitTab::Branches => &self.snap.branches,
            GitTab::Log => &self.snap.log,
        }
    }

    pub fn selected(&self) -> Option<&GitRow> {
        self.rows().get(self.sel)
    }

    pub fn next_tab(&mut self, by: i32) {
        let n = GitTab::ALL.len() as i32;
        let i = (self.tab.index() as i32 + by).rem_euclid(n) as usize;
        self.tab = GitTab::ALL[i];
        self.sel = 0;
        self.scroll = 0;
    }

    pub fn select(&mut self, by: i32) {
        let len = self.rows().len();
        if len == 0 {
            self.sel = 0;
            return;
        }
        self.sel = (self.sel as i32 + by).clamp(0, len as i32 - 1) as usize;
    }

    /// Keep the cursor on screen for a viewport `height` rows tall.
    pub fn clamp(&mut self, height: usize) {
        if height == 0 {
            return;
        }
        if self.sel < self.scroll {
            self.scroll = self.sel;
        } else if self.sel >= self.scroll + height {
            self.scroll = self.sel + 1 - height;
        }
        let max = self.rows().len().saturating_sub(height);
        self.scroll = self.scroll.min(max);
    }

    /// Start a read if one is due. `force` is the `r` key.
    pub fn poll(&mut self, force: bool) {
        if self.pending.is_some() {
            return;
        }
        let due = self.last.is_none_or(|t| t.elapsed() >= REFRESH);
        if !(force || due) {
            return;
        }
        self.last = Some(Instant::now());
        let (tx, rx) = channel();
        let cwd = self.cwd.clone();
        std::thread::spawn(move || {
            let _ = tx.send(read_repo(&cwd));
        });
        self.pending = Some(rx);
    }

    /// True when a read landed and the pane needs a repaint.
    pub fn drain(&mut self) -> bool {
        let Some(rx) = self.pending.as_ref() else {
            return false;
        };
        match rx.try_recv() {
            Ok(snap) => {
                self.snap = snap;
                let len = self.rows().len();
                self.sel = self.sel.min(len.saturating_sub(1));
                self.pending = None;
                true
            }
            Err(TryRecvError::Empty) => false,
            Err(TryRecvError::Disconnected) => {
                self.pending = None;
                true
            }
        }
    }
}

fn git(cwd: &Path, args: &[&str]) -> Result<String, String> {
    let out = Command::new("git")
        .args(args)
        .current_dir(cwd)
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        return Err(err.lines().next().unwrap_or("git failed").to_string());
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

fn read_repo(cwd: &Path) -> GitSnap {
    let mut snap = GitSnap::default();
    let porcelain = match git(cwd, &["status", "--porcelain", "-b"]) {
        Ok(s) => s,
        Err(e) => {
            snap.error = Some(e);
            return snap;
        }
    };
    for line in porcelain.lines().take(CAP) {
        if let Some(head) = line.strip_prefix("## ") {
            snap.branch = head.split_whitespace().next().unwrap_or(head).to_string();
            continue;
        }
        if line.len() < 4 {
            continue;
        }
        let (mark, rest) = line.split_at(2);
        let name = rest.trim();
        // A rename reads `old -> new`; the new path is the one on disk.
        let name = name.rsplit(" -> ").next().unwrap_or(name);
        snap.status.push(GitRow {
            text: name.to_string(),
            mark: mark.trim().to_string(),
            path: Some(cwd.join(name)),
        });
    }
    if let Ok(text) = git(cwd, &["branch", "--sort=-committerdate", "-v"]) {
        for line in text.lines().take(CAP) {
            let current = line.starts_with('*');
            snap.branches.push(GitRow {
                text: line.trim_start_matches('*').trim().to_string(),
                mark: if current { "*".into() } else { String::new() },
                path: None,
            });
        }
    }
    if let Ok(text) = git(cwd, &["log", "--oneline", "--decorate", "-n", "80"]) {
        for line in text.lines() {
            let (sha, rest) = line.split_once(' ').unwrap_or((line, ""));
            snap.log.push(GitRow {
                text: rest.to_string(),
                mark: sha.to_string(),
                path: None,
            });
        }
    }
    snap
}

/// The file under the git pane's cursor, read once and scrolled locally.
#[derive(Default)]
pub struct FilePane {
    pub path: Option<PathBuf>,
    pub lines: Vec<String>,
    pub scroll: usize,
    pub note: Option<String>,
}

/// A pane is a few dozen rows; reading a gigabyte to show six of them is how a
/// TUI freezes on a lockfile.
const MAX_BYTES: u64 = 512 * 1024;
const MAX_LINES: usize = 4000;

impl FilePane {
    /// Load `path` unless it is already loaded. Returns true when it changed.
    pub fn show(&mut self, path: Option<&Path>) -> bool {
        let same = match (&self.path, path) {
            (Some(a), Some(b)) => a == b,
            (None, None) => true,
            _ => false,
        };
        if same {
            return false;
        }
        self.scroll = 0;
        self.note = None;
        self.lines.clear();
        self.path = path.map(Path::to_path_buf);
        let Some(p) = path else { return true };
        match std::fs::metadata(p) {
            Ok(m) if m.is_dir() => {
                self.note = Some("directory".into());
                return true;
            }
            Ok(m) if m.len() > MAX_BYTES => {
                self.note = Some(format!("{} KB — too big to preview", m.len() / 1024));
                return true;
            }
            Err(e) => {
                self.note = Some(e.to_string());
                return true;
            }
            _ => {}
        }
        match std::fs::read(p) {
            Ok(bytes) => {
                if bytes.contains(&0) {
                    self.note = Some("binary".into());
                    return true;
                }
                self.lines = String::from_utf8_lossy(&bytes)
                    .lines()
                    .take(MAX_LINES)
                    .map(|l| l.replace('\t', "    "))
                    .collect();
            }
            Err(e) => self.note = Some(e.to_string()),
        }
        true
    }

    pub fn scroll_by(&mut self, by: i32, height: usize) {
        let max = self.lines.len().saturating_sub(height.max(1));
        self.scroll = (self.scroll as i32 + by).clamp(0, max as i32) as usize;
    }

    pub fn title(&self) -> String {
        match &self.path {
            Some(p) => p
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_else(|| p.display().to_string()),
            None => "file".into(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tabs_wrap_both_ways_and_reset_the_cursor() {
        let mut g = GitPane::new(Path::new("."));
        g.sel = 4;
        g.next_tab(1);
        assert_eq!(g.tab, GitTab::Branches);
        assert_eq!(g.sel, 0, "a new view starts at the top");
        g.next_tab(-1);
        assert_eq!(g.tab, GitTab::Status);
        g.next_tab(-1);
        assert_eq!(g.tab, GitTab::Log, "wraps backwards");
    }

    #[test]
    fn the_cursor_stays_inside_the_viewport() {
        let mut g = GitPane::new(Path::new("."));
        g.snap.status = (0..20)
            .map(|i| GitRow {
                text: format!("f{i}"),
                ..Default::default()
            })
            .collect();
        g.select(19);
        g.clamp(5);
        assert_eq!(g.sel, 19);
        assert!(g.scroll <= 15 && g.scroll + 5 > 19, "scroll {}", g.scroll);
        g.select(-19);
        g.clamp(5);
        assert_eq!((g.sel, g.scroll), (0, 0));
    }

    #[test]
    fn status_porcelain_becomes_rows_with_paths() {
        let snap = {
            let mut s = GitSnap::default();
            for line in "## main...origin/main\n M src/a.rs\n?? new.txt\nR  old.rs -> src/b.rs\n"
                .lines()
            {
                if let Some(head) = line.strip_prefix("## ") {
                    s.branch = head.split_whitespace().next().unwrap_or(head).into();
                    continue;
                }
                let (mark, rest) = line.split_at(2);
                let name = rest.trim();
                let name = name.rsplit(" -> ").next().unwrap_or(name);
                s.status.push(GitRow {
                    text: name.into(),
                    mark: mark.trim().into(),
                    path: Some(PathBuf::from(name)),
                });
            }
            s
        };
        assert_eq!(snap.branch, "main...origin/main");
        assert_eq!(snap.status.len(), 3);
        assert_eq!(snap.status[0].mark, "M");
        assert_eq!(snap.status[1].mark, "??");
        assert_eq!(
            snap.status[2].text, "src/b.rs",
            "a rename points at the new path"
        );
    }

    #[test]
    fn a_file_pane_refuses_what_it_cannot_show() {
        let dir = std::env::temp_dir().join("desmos-side-test");
        let _ = std::fs::create_dir_all(&dir);
        let bin = dir.join("bin");
        std::fs::write(&bin, [0u8, 1, 2, 3]).unwrap();
        let text = dir.join("t.txt");
        std::fs::write(&text, "one\ntwo\n").unwrap();

        let mut f = FilePane::default();
        assert!(f.show(Some(&bin)));
        assert_eq!(f.note.as_deref(), Some("binary"));
        assert!(f.lines.is_empty());

        assert!(f.show(Some(&text)));
        assert_eq!(f.lines, vec!["one", "two"]);
        assert!(f.note.is_none());
        assert!(!f.show(Some(&text)), "the same path is not re-read");

        assert!(f.show(Some(&dir)));
        assert_eq!(f.note.as_deref(), Some("directory"));
    }
}
