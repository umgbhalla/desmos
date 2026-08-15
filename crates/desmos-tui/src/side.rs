//! The two side panes under the wire column: git state in tabs, and the file
//! a git row points at.
//!
//! Shape borrowed from druk (vendor/druk), whose sidebar is one column with a
//! tab strip — Files / Git / Review / Extensions — over a view that changes
//! with the tab, and an editor slot beside it. Here the column is already the
//! wire, so git becomes a tabbed pane of its own and the editor slot becomes a
//! read-only file view under it.
//!
//! Both panes start open — a pane you have to know about before you can see
//! it is a pane nobody sees. `git` shells out on a worker thread — the UI
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

    /// Jump straight to a tab. The keyboard walks the strip with `next_tab`;
    /// a click lands on one label and needs to say which.
    pub fn set_tab(&mut self, tab: GitTab) {
        if tab == self.tab {
            return;
        }
        self.tab = tab;
        self.sel = 0;
        self.scroll = 0;
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

/// One row of a directory listing.
pub struct DirRow {
    pub name: String,
    pub is_dir: bool,
}

/// The file view, which is also the filesystem view — the two are one pane with
/// two states, and `←` / `→` move between them. Opening a file from git jumps
/// straight to the content state; `←` drops back to the listing for that file's
/// own directory, with the cursor on the file you just left. `←` again walks up
/// a level. That is the whole navigation model: down is `→`, up is `←`.
///
/// The listing is not a tree. A tree pane six rows tall shows you a spine and
/// no leaves; one directory at a time shows you the directory.
#[derive(Default)]
pub struct FilePane {
    /// The directory being listed, and the one `←` returns to from a file.
    pub dir: PathBuf,
    pub entries: Vec<DirRow>,
    /// Cursor into `entries`. Only meaningful while `path` is None.
    pub sel: usize,
    /// The open file, or None while listing `dir`.
    pub path: Option<PathBuf>,
    pub lines: Vec<String>,
    pub scroll: usize,
    pub note: Option<String>,
}

/// A pane is a few dozen rows; reading a gigabyte to show six of them is how a
/// TUI freezes on a lockfile.
const MAX_BYTES: u64 = 512 * 1024;
const MAX_LINES: usize = 4000;
/// Directories with more entries than this exist (node_modules, a build dir).
/// The pane still opens; it just stops reading past what anyone will scroll.
const MAX_ENTRIES: usize = 2000;
/// The row that walks up a level. Kept as a real row so `→` and `←` agree, and
/// so the way out is visible rather than something you have to already know.
const PARENT: &str = "..";

impl FilePane {
    pub fn new(cwd: &Path) -> Self {
        let mut pane = Self {
            dir: cwd.to_path_buf(),
            ..Default::default()
        };
        pane.read_dir();
        pane
    }

    /// True while the pane is showing a file rather than a directory.
    pub fn in_file(&self) -> bool {
        self.path.is_some()
    }

    fn read_dir(&mut self) {
        self.entries.clear();
        self.note = None;
        self.sel = 0;
        self.scroll = 0;
        let mut rows: Vec<DirRow> = match std::fs::read_dir(&self.dir) {
            Ok(it) => it
                .filter_map(Result::ok)
                .take(MAX_ENTRIES)
                .map(|e| DirRow {
                    is_dir: e.file_type().is_ok_and(|t| t.is_dir()),
                    name: e.file_name().to_string_lossy().into_owned(),
                })
                .collect(),
            Err(e) => {
                self.note = Some(e.to_string());
                return;
            }
        };
        rows.sort_by(|a, b| b.is_dir.cmp(&a.is_dir).then_with(|| a.name.cmp(&b.name)));
        if self.dir.parent().is_some() {
            rows.insert(
                0,
                DirRow {
                    name: PARENT.into(),
                    is_dir: true,
                },
            );
        }
        self.entries = rows;
    }

    fn select_name(&mut self, name: Option<&str>) {
        let Some(name) = name else { return };
        if let Some(i) = self.entries.iter().position(|r| r.name == name) {
            self.sel = i;
        }
    }

    /// `→`: descend. Into a directory, or into a file's contents. Already
    /// inside a file, there is nowhere further down to go.
    pub fn enter(&mut self) {
        if self.in_file() {
            return;
        }
        let Some(row) = self.entries.get(self.sel) else {
            return;
        };
        if row.name == PARENT {
            self.back();
            return;
        }
        let target = self.dir.join(&row.name);
        if row.is_dir {
            self.dir = target;
            self.read_dir();
        } else {
            self.open(&target);
        }
    }

    /// `←`: back out one level. From a file, to the directory holding it, with
    /// the cursor on that file. From a directory, to its parent, with the
    /// cursor on the directory you left — so `←` `→` is a round trip, not a
    /// reset to the top of the list.
    pub fn back(&mut self) {
        if let Some(p) = self.path.take() {
            self.lines.clear();
            if let Some(parent) = p.parent() {
                self.dir = parent.to_path_buf();
            }
            self.read_dir();
            self.select_name(p.file_name().map(|n| n.to_string_lossy()).as_deref());
            return;
        }
        let Some(parent) = self.dir.parent().map(Path::to_path_buf) else {
            return;
        };
        let leaving = self
            .dir
            .file_name()
            .map(|n| n.to_string_lossy().into_owned());
        self.dir = parent;
        self.read_dir();
        self.select_name(leaving.as_deref());
    }

    /// Read `path` into the content state. Public so the git pane can hand a
    /// row's file straight over without walking there.
    pub fn open(&mut self, path: &Path) {
        self.scroll = 0;
        self.note = None;
        self.lines.clear();
        self.path = Some(path.to_path_buf());
        match std::fs::metadata(path) {
            Ok(m) if m.is_dir() => {
                // A directory reached through `open` is a listing, not a note
                // saying the word "directory" at you.
                self.path = None;
                self.dir = path.to_path_buf();
                self.read_dir();
                return;
            }
            Ok(m) if m.len() > MAX_BYTES => {
                self.note = Some(format!("{} KB — too big to preview", m.len() / 1024));
                return;
            }
            Err(e) => {
                self.note = Some(e.to_string());
                return;
            }
            _ => {}
        }
        match std::fs::read(path) {
            Ok(bytes) => {
                if bytes.contains(&0) {
                    self.note = Some("binary".into());
                    return;
                }
                self.lines = String::from_utf8_lossy(&bytes)
                    .lines()
                    .take(MAX_LINES)
                    .map(|l| l.replace('\t', "    "))
                    .collect();
            }
            Err(e) => self.note = Some(e.to_string()),
        }
    }

    /// The git cursor moved. A row naming a file opens it; a row naming none —
    /// a branch, a commit — leaves the pane alone, so walking the branches tab
    /// does not blank the file you were reading.
    pub fn preview(&mut self, path: Option<&Path>) -> bool {
        let Some(p) = path else { return false };
        if self.path.as_deref() == Some(p) {
            return false;
        }
        self.open(p);
        true
    }

    /// `↑` / `↓`, whichever state the pane is in: the cursor in a listing, the
    /// viewport in a file.
    pub fn move_by(&mut self, by: i32, height: usize) {
        if self.in_file() {
            self.scroll_by(by, height);
            return;
        }
        let len = self.entries.len();
        if len == 0 {
            return;
        }
        self.sel = (self.sel as i32 + by).clamp(0, len as i32 - 1) as usize;
        if self.sel < self.scroll {
            self.scroll = self.sel;
        } else if height > 0 && self.sel >= self.scroll + height {
            self.scroll = self.sel + 1 - height;
        }
        self.scroll = self.scroll.min(len.saturating_sub(height.max(1)));
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
            None => self
                .dir
                .file_name()
                .map(|n| format!("{}/", n.to_string_lossy()))
                .unwrap_or_else(|| self.dir.display().to_string()),
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

        let mut f = FilePane::new(&dir);
        assert!(f.preview(Some(&bin)));
        assert_eq!(f.note.as_deref(), Some("binary"));
        assert!(f.lines.is_empty());

        assert!(f.preview(Some(&text)));
        assert_eq!(f.lines, vec!["one", "two"]);
        assert!(f.note.is_none());
        assert!(!f.preview(Some(&text)), "the same path is not re-read");

        // A row that names no file leaves the open file alone rather than
        // blanking the pane.
        assert!(!f.preview(None));
        assert_eq!(f.lines, vec!["one", "two"]);

        // A directory handed to `open` becomes a listing, not a note.
        f.open(&dir);
        assert!(!f.in_file());
        assert_eq!(f.dir, dir);
    }

    #[test]
    fn arrows_walk_down_into_a_file_and_back_out_to_where_it_sat() {
        let root = std::env::temp_dir().join("desmos-side-walk");
        let _ = std::fs::remove_dir_all(&root);
        let sub = root.join("sub");
        std::fs::create_dir_all(&sub).unwrap();
        std::fs::write(sub.join("a.txt"), "hello\n").unwrap();

        let mut f = FilePane::new(&root);
        // `..` sorts first, then directories, then files.
        assert_eq!(f.entries[0].name, "..");
        assert_eq!(f.entries[1].name, "sub");

        f.move_by(1, 10);
        f.enter();
        assert_eq!(f.dir, sub, "→ on a directory descends");
        assert!(!f.in_file());

        f.select_name(Some("a.txt"));
        f.enter();
        assert!(f.in_file(), "→ on a file opens it");
        assert_eq!(f.lines, vec!["hello"]);

        f.back();
        assert!(!f.in_file(), "← leaves the file");
        assert_eq!(f.dir, sub);
        assert_eq!(
            f.entries[f.sel].name, "a.txt",
            "← lands on the file you left, not the top of the list"
        );

        f.back();
        assert_eq!(f.dir, root, "← again walks up a level");
        assert_eq!(
            f.entries[f.sel].name, "sub",
            "← lands on the directory you left"
        );

        // `→` on the `..` row is the same move as `←`.
        f.sel = 0;
        let up = f.dir.parent().unwrap().to_path_buf();
        f.enter();
        assert_eq!(f.dir, up);
    }
}
