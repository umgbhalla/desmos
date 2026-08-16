//! The ctrl-key fuzzy file picker.
//!
//! The files pane already browses a directory with `read_dir` (`side.rs`), and
//! that stays the owner of "list this directory". This is the other move: type
//! a few characters, get the whole tree ranked by fuzzy match *and* by how
//! recently the kernel touched each file. The ranking is not ours to compute —
//! it is `fff-search`'s, reading the frecency LMDB the kernel writes at
//! `<cwd>/.desmos/fff` through its `<edit>` choke point (upgrade-paths 6.3).
//! That LMDB is one-way, kernel → TUI: `fuzzy_search` reads the access scores
//! cached at scan time, and a picker opened after a kernel edit sees the newer
//! ranking because it runs a fresh scan. The picker never writes it back; a
//! selection here is not an access the kernel should learn from.
//!
//! Threading follows the git pane (`side.rs GitPane`): the UI thread must never
//! block on a scan of a large tree, so a worker thread owns the `FilePicker`
//! (which is `!Sync` — all access goes through `SharedFilePicker`) and answers
//! queries over a channel. A panic in that worker unwinds into `catch_unwind`,
//! becomes a one-line pane notice, and the UI loop keeps running; only a hard
//! abort inside fff-core's unsafe SIMD/mmap can take the process down, the same
//! accepted risk named for the kernel side in upgrade-paths 6.1.
//!
//! When the crate is built without the `fuzzy` feature (the vendored engine
//! blocked), the whole worker is compiled out: `open` leaves a notice saying so
//! and the files pane's `read_dir` browser is the way to open a file. That is
//! the degrade the spec asks for — never a second, hand-rolled fuzzy matcher.

use std::path::{Path, PathBuf};

#[cfg(feature = "fuzzy")]
use std::sync::mpsc::{Receiver, Sender, TryRecvError, channel};
#[cfg(feature = "fuzzy")]
use std::time::Duration;

/// Rows a single search returns. A tree has more matches than a pane has lines;
/// past this the ranking is noise no one scrolls to.
#[cfg(feature = "fuzzy")]
const LIMIT: usize = 200;
/// How long the worker waits for the initial scan before serving queries. A
/// cold scan of a big tree is seconds; a timeout serves an empty index rather
/// than hanging the open forever.
#[cfg(feature = "fuzzy")]
const SCAN_TIMEOUT: Duration = Duration::from_secs(10);

/// A message from the worker thread back to the UI.
#[cfg(feature = "fuzzy")]
enum Msg {
    /// The initial scan finished; queries now return real rows.
    Ready,
    /// Results for the query the worker last processed. Absolute paths, ranked.
    Results(Vec<PathBuf>),
    /// The picker could not start (or the worker panicked). One-line notice;
    /// the pane degrades to it and the files pane stays the browser.
    Error(String),
}

/// The worker side of the channel pair. Dropping it disconnects the query
/// channel, which the worker loop reads as "closed" and exits, dropping the
/// `FilePicker` so its background scan/watch threads wind down.
#[cfg(feature = "fuzzy")]
struct Worker {
    queries: Sender<String>,
    results: Receiver<Msg>,
    _handle: std::thread::JoinHandle<()>,
}

/// The fuzzy picker overlay. Pure state plus (when built with `fuzzy`) a handle
/// to the search worker. Drawing lives in the frame layer, the same split the
/// git and files panes use — this struct only decides *what* is shown.
#[derive(Default)]
pub struct Picker {
    open: bool,
    query: String,
    results: Vec<PathBuf>,
    sel: usize,
    scroll: usize,
    /// A one-line status: "scanning…", or the degrade/error reason. Cleared
    /// once real results are in.
    notice: Option<String>,
    /// Set by `enter`; the frame drains it into `FilePane::open` and closes.
    chosen: Option<PathBuf>,
    #[cfg(feature = "fuzzy")]
    worker: Option<Worker>,
}

impl Picker {
    pub fn is_open(&self) -> bool {
        self.open
    }

    pub fn query(&self) -> &str {
        &self.query
    }

    pub fn results(&self) -> &[PathBuf] {
        &self.results
    }

    pub fn sel(&self) -> usize {
        self.sel
    }

    pub fn scroll(&self) -> usize {
        self.scroll
    }

    pub fn notice(&self) -> Option<&str> {
        self.notice.as_deref()
    }

    /// Open the picker over `cwd`, starting a fresh worker so the scan re-reads
    /// the frecency LMDB as the kernel left it. Re-opening after a kernel edit
    /// is what surfaces the new ranking.
    #[cfg(feature = "fuzzy")]
    pub fn open(&mut self, cwd: &Path) {
        self.open = true;
        self.query.clear();
        self.results.clear();
        self.sel = 0;
        self.scroll = 0;
        self.chosen = None;
        self.notice = Some("scanning…".into());
        self.worker = Some(spawn(cwd.to_path_buf()));
        // An empty query rides the frecency ordering (fff short-circuits to
        // `score_filtered_by_frecency`), so the list is useful before a keystroke.
        self.send_query();
    }

    /// Without the engine, opening states the degrade and stops. The files pane
    /// is the browser; this is a signpost, not a second implementation.
    #[cfg(not(feature = "fuzzy"))]
    pub fn open(&mut self, _cwd: &Path) {
        self.open = true;
        self.query.clear();
        self.results.clear();
        self.sel = 0;
        self.scroll = 0;
        self.chosen = None;
        self.notice = Some("fuzzy picker unavailable (built without fff-search)".into());
    }

    pub fn close(&mut self) {
        self.open = false;
        self.query.clear();
        self.results.clear();
        self.sel = 0;
        self.scroll = 0;
        self.notice = None;
        #[cfg(feature = "fuzzy")]
        {
            // Dropping the worker closes the query channel; the worker exits and
            // its FilePicker's background threads wind down.
            self.worker = None;
        }
    }

    pub fn push_char(&mut self, c: char) {
        if !self.open {
            return;
        }
        self.query.push(c);
        self.on_query_changed();
    }

    pub fn backspace(&mut self) {
        if !self.open {
            return;
        }
        self.query.pop();
        self.on_query_changed();
    }

    #[cfg(feature = "fuzzy")]
    fn on_query_changed(&mut self) {
        self.send_query();
    }

    #[cfg(not(feature = "fuzzy"))]
    fn on_query_changed(&mut self) {}

    #[cfg(feature = "fuzzy")]
    fn send_query(&mut self) {
        if let Some(w) = self.worker.as_ref() {
            // A dead channel means the worker exited; the next `poll` reports it.
            let _ = w.queries.send(self.query.clone());
        }
    }

    /// Move the cursor. Selection stays inside the result list; the frame keeps
    /// it inside the viewport with `clamp`.
    pub fn select(&mut self, by: i32) {
        let len = self.results.len();
        if len == 0 {
            self.sel = 0;
            return;
        }
        self.sel = (self.sel as i32 + by).clamp(0, len as i32 - 1) as usize;
    }

    /// Keep the cursor on screen for a viewport `height` rows tall. Same shape
    /// as `GitPane::clamp` — a picker and a git pane scroll the same way.
    pub fn clamp(&mut self, height: usize) {
        if height == 0 {
            return;
        }
        if self.sel < self.scroll {
            self.scroll = self.sel;
        } else if self.sel >= self.scroll + height {
            self.scroll = self.sel + 1 - height;
        }
        let max = self.results.len().saturating_sub(height);
        self.scroll = self.scroll.min(max);
    }

    /// Choose the highlighted row: stash its path for the frame to open, and
    /// close. Nothing highlighted (empty results) is a no-op that stays open.
    pub fn enter(&mut self) {
        if let Some(p) = self.results.get(self.sel).cloned() {
            self.chosen = Some(p);
            self.close(); // sets open = false; leaves `chosen` for the frame
        }
    }

    /// The frame drains this after a `poll`/key and hands it to `FilePane::open`.
    pub fn take_chosen(&mut self) -> Option<PathBuf> {
        self.chosen.take()
    }

    /// Drain whatever the worker has said. Returns true when something changed
    /// and the pane needs a repaint. No-op (false) without the engine.
    #[cfg(feature = "fuzzy")]
    pub fn poll(&mut self) -> bool {
        let Some(w) = self.worker.as_ref() else {
            return false;
        };
        let mut dirty = false;
        let mut latest: Option<Vec<PathBuf>> = None;
        loop {
            match w.results.try_recv() {
                Ok(Msg::Ready) => {
                    if self.notice.as_deref() == Some("scanning…") {
                        self.notice = None;
                        dirty = true;
                    }
                }
                Ok(Msg::Results(paths)) => latest = Some(paths),
                Ok(Msg::Error(text)) => {
                    self.notice = Some(text);
                    self.results.clear();
                    self.sel = 0;
                    self.scroll = 0;
                    // A crashed worker cannot answer again; let it go so a
                    // later reopen starts clean.
                    self.worker = None;
                    return true;
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => {
                    // The worker exited without an Error (e.g. its send races
                    // its own drop). Say so once rather than freezing on stale
                    // rows.
                    self.worker = None;
                    self.notice = Some("fuzzy picker worker stopped".into());
                    return true;
                }
            }
        }
        if let Some(paths) = latest {
            self.results = paths;
            self.sel = self.sel.min(self.results.len().saturating_sub(1));
            if !self.results.is_empty() {
                self.notice = None;
            }
            dirty = true;
        }
        dirty
    }

    #[cfg(not(feature = "fuzzy"))]
    pub fn poll(&mut self) -> bool {
        false
    }
}

/// Spawn the search worker. The closure owns the shared handles and the
/// `FilePicker`; `catch_unwind` turns a panic into a pane notice so the UI loop
/// survives a bug in the ranking code.
#[cfg(feature = "fuzzy")]
fn spawn(cwd: PathBuf) -> Worker {
    let (qtx, qrx) = channel::<String>();
    let (rtx, rrx) = channel::<Msg>();
    let rtx_panic = rtx.clone();
    let handle = std::thread::spawn(move || {
        let run = std::panic::AssertUnwindSafe(|| worker_main(&cwd, &qrx, &rtx));
        if std::panic::catch_unwind(run).is_err() {
            let _ = rtx_panic.send(Msg::Error("fuzzy picker worker crashed".into()));
        }
    });
    Worker {
        queries: qtx,
        results: rrx,
        _handle: handle,
    }
}

/// The worker body: build the picker sharing the kernel's frecency LMDB, wait
/// for the scan, then serve queries until the UI drops the channel.
#[cfg(feature = "fuzzy")]
fn worker_main(cwd: &Path, queries: &Receiver<String>, results: &Sender<Msg>) {
    use fff_search::file_picker::FilePicker;
    use fff_search::frecency::FrecencyTracker;
    use fff_search::{FFFMode, FilePickerOptions, SharedFilePicker, SharedFrecency};

    let shared_picker = SharedFilePicker::default();
    let shared_frecency = SharedFrecency::default();

    // The kernel's find lane writes frecency here (upgrade-paths 6.2/6.3). We
    // only read it: opening it read-side lets `fuzzy_search` fold the kernel's
    // access history into the ranking. Its absence is not an error — a repo the
    // kernel has never searched simply ranks by fuzzy match alone.
    let db = cwd.join(".desmos").join("fff");
    if db.exists() {
        match FrecencyTracker::open(&db) {
            Ok(t) => {
                let _ = shared_frecency.init(t);
            }
            Err(e) => {
                let _ = results.send(Msg::Error(format!("frecency db unreadable: {e}")));
            }
        }
    }

    let opts = FilePickerOptions {
        base_path: cwd.to_string_lossy().into_owned(),
        mode: FFFMode::Ai,
        // Path search only; grep already has an owner (upgrade-paths 6.2).
        enable_content_indexing: false,
        // One-shot picker: a fresh scan per open is exactly how a reopen picks
        // up the kernel's newer frecency, so there is nothing to watch for.
        watch: false,
        ..Default::default()
    };
    if let Err(e) =
        FilePicker::new_with_shared_state(shared_picker.clone(), shared_frecency, opts)
    {
        let _ = results.send(Msg::Error(format!("fuzzy picker unavailable: {e}")));
        return;
    }

    shared_picker.wait_for_scan(SCAN_TIMEOUT);
    if results.send(Msg::Ready).is_err() {
        return;
    }

    // ponytail: a fresh full scan on every open. It is what the two-process
    // "reopen reflects the kernel's ranking" contract needs; swap to a
    // long-lived worker with restart_index if open latency ever bites.
    while let Ok(mut q) = queries.recv() {
        // Coalesce a burst of keystrokes: only the latest query matters.
        while let Ok(next) = queries.try_recv() {
            q = next;
        }
        let paths = search(&shared_picker, &q);
        if results.send(Msg::Results(paths)).is_err() {
            break;
        }
    }
}

/// Run one fuzzy search and lift the borrowed results into owned absolute paths
/// before the picker guard drops.
#[cfg(feature = "fuzzy")]
fn search(shared: &fff_search::SharedFilePicker, query: &str) -> Vec<PathBuf> {
    use fff_search::{FuzzySearchOptions, PaginationArgs, QueryParser};

    let Ok(guard) = shared.read() else {
        return Vec::new();
    };
    let Some(picker) = guard.as_ref() else {
        return Vec::new();
    };
    let parser = QueryParser::default();
    let parsed = parser.parse(query);
    let result = picker.fuzzy_search(
        &parsed,
        None,
        FuzzySearchOptions {
            max_threads: 0,
            pagination: PaginationArgs {
                offset: 0,
                limit: LIMIT,
            },
            ..Default::default()
        },
    );
    result
        .items
        .iter()
        .map(|item| picker.base_path.join(item.relative_path(picker)))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Paint discipline: the cursor never leaves the result list and the
    /// viewport always contains it. Same guard the git pane carries — a pane
    /// that scrolls its selection off screen shows a highlight no one can see.
    #[test]
    fn selection_and_scroll_stay_inside() {
        let mut p = Picker::default();
        p.open = true;
        p.results = (0..20).map(|i| PathBuf::from(format!("f{i}"))).collect();

        // Down past the end clamps to the last row.
        p.select(100);
        assert_eq!(p.sel, 19);
        p.clamp(5);
        assert!(p.scroll <= 15 && p.scroll + 5 > p.sel, "scroll {}", p.scroll);

        // Up past the top clamps to zero.
        p.select(-100);
        p.clamp(5);
        assert_eq!((p.sel, p.scroll), (0, 0));

        // An empty list keeps the cursor at zero rather than underflowing.
        p.results.clear();
        p.select(-1);
        p.select(3);
        assert_eq!(p.sel, 0);
        p.clamp(5);
        assert_eq!(p.scroll, 0);
    }

    /// Enter on the highlighted row stashes its path and closes; the frame
    /// opens it in the files pane. Enter on an empty list changes nothing and
    /// leaves the picker up.
    #[test]
    fn enter_chooses_the_row_then_closes() {
        let mut p = Picker::default();
        p.open = true;
        p.results = vec![PathBuf::from("a"), PathBuf::from("b"), PathBuf::from("c")];
        p.sel = 2;
        p.enter();
        assert!(!p.is_open());
        assert_eq!(p.take_chosen(), Some(PathBuf::from("c")));
        assert_eq!(p.take_chosen(), None, "drained once");

        let mut empty = Picker::default();
        empty.open = true;
        empty.enter();
        assert!(empty.is_open(), "nothing to choose keeps the picker up");
        assert_eq!(empty.take_chosen(), None);
    }

    /// Without the `fuzzy` feature the picker opens to a one-line notice and no
    /// worker — the degrade path, not a second matcher. Only meaningful in a
    /// non-fuzzy build.
    #[cfg(not(feature = "fuzzy"))]
    #[test]
    fn degrades_to_a_notice_without_the_engine() {
        let mut p = Picker::default();
        p.open(Path::new("."));
        assert!(p.is_open());
        assert!(p.notice().is_some());
        assert!(!p.poll());
    }

    /// The worker, over a real temp tree, ranks a seeded file first for a
    /// typo'd query — the whole point of a fuzzy picker over `read_dir`.
    ///
    /// `#[ignore]`: needs fff-core linked. The orchestrator builds the vendored
    /// engine out of band (never a cold `cargo build` here); this runs once it
    /// links. Do not un-ignore by faking the engine.
    #[cfg(feature = "fuzzy")]
    #[test]
    fn worker_finds_a_typod_file_over_a_temp_tree() {
        let root = std::env::temp_dir().join("desmos-fuzzy-typo");
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("src")).unwrap();
        std::fs::write(root.join("src").join("dispatcher.rs"), "x").unwrap();
        std::fs::write(root.join("README.md"), "x").unwrap();

        let mut p = Picker::default();
        p.open(&root);
        // "dispatchr" is a transposition/drop typo of "dispatcher".
        for c in "dispatchr".chars() {
            p.push_char(c);
        }
        let deadline = std::time::Instant::now() + Duration::from_secs(15);
        while std::time::Instant::now() < deadline {
            p.poll();
            if !p.results().is_empty() {
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let top = p.results().first().expect("a fuzzy match for the typo");
        assert!(
            top.ends_with("src/dispatcher.rs"),
            "typo ranked {top:?} first"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    /// Two processes, one LMDB: the kernel writes an access (here via the same
    /// `FrecencyTracker::track_access` the vendor pymethod wraps), then a FRESH
    /// picker open reads that ranking. This is the direction the critic said
    /// actually works — scan-time frecency read, kernel → TUI one-way.
    ///
    /// `#[ignore]`: needs fff-core linked; see the note above.
    #[cfg(feature = "fuzzy")]
    #[test]
    fn a_fresh_open_reflects_a_kernel_side_access() {
        use fff_search::frecency::FrecencyTracker;

        let root = std::env::temp_dir().join("desmos-fuzzy-frecency");
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        // Two files that fuzzy-match "notes" equally well by name shape; only
        // frecency can break the tie.
        std::fs::write(root.join("notes_a.txt"), "x").unwrap();
        std::fs::write(root.join("notes_b.txt"), "x").unwrap();

        // Kernel side: record an access to notes_b at the shared LMDB path the
        // TUI worker reads (`<cwd>/.desmos/fff`).
        let db = root.join(".desmos").join("fff");
        std::fs::create_dir_all(&db).unwrap();
        {
            let tracker = FrecencyTracker::open(&db).unwrap();
            // One access registers the file but does not cross fff's frecency
            // boost threshold, so it would not break the name-shape tie. The
            // kernel feeds a touch per <edit>; a file edited repeatedly is the
            // one frecency is meant to float. Model that with a run of accesses.
            for _ in 0..6 {
                tracker.track_access(&root.join("notes_b.txt")).unwrap();
            }
        } // closed before the picker opens — a genuinely fresh reader.

        let mut p = Picker::default();
        p.open(&root);
        for c in "notes".chars() {
            p.push_char(c);
        }
        let deadline = std::time::Instant::now() + Duration::from_secs(15);
        let mut top = None;
        while std::time::Instant::now() < deadline {
            p.poll();
            if let Some(first) = p.results().first() {
                top = Some(first.clone());
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let top = top.expect("results for 'notes'");
        assert!(
            top.ends_with("notes_b.txt"),
            "the accessed file must rank first, got {top:?}"
        );
        let _ = std::fs::remove_dir_all(&root);
    }
}
