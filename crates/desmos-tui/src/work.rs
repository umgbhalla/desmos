//! The work run: the stretch of invisible work -- thoughts and syscalls
//! between two pieces of prose -- folded into the one story row the reader
//! can follow, plus the repo tail that says what that work did to the tree.
//! Moved verbatim out of main.rs (and `call_target` out of events.rs).

use std::collections::HashMap;

use serde_json::Value;
use xai_grok_pager::scrollback::{EntryId, RenderBlock, ScrollbackState};

use crate::side;

/// The legacy execution tag represented by an event.
///
/// Canonical events keep their `exec` kernel tag; only the TUI interpretation
/// is normalized here.
pub(crate) fn exec_tag<'a>(tag: &'a str, attrs: &'a Value) -> Option<&'a str> {
    match tag {
        "python" | "bash" | "shell" => Some(tag),
        "exec" => attrs
            .get("op")
            .and_then(Value::as_str)
            .filter(|op| matches!(*op, "python" | "bash" | "shell")),
        _ => None,
    }
}

/// What a call was aimed at, when that is structural rather than payload.
///
/// A path from an attr is a target. For a shell command the *program* is the
/// semantic part -- `cargo`, `git`, `grep` -- while its flags and arguments are
/// the payload the calls pane already holds, so only the bare program name
/// comes across, and only after stepping past a leading `cd`.
pub(crate) struct CallTarget {
    tag: String,
    target: Option<String>,
}

impl CallTarget {
    fn new(tag: &str, target: Option<String>) -> Self {
        Self {
            tag: tag.to_string(),
            target,
        }
    }

    pub(crate) fn as_deref(&self) -> Option<&str> {
        self.target.as_deref()
    }
}

pub(crate) fn call_target(tag: &str, ev: &Value) -> CallTarget {
    let empty = Value::Null;
    let attrs = ev.get("attrs").unwrap_or(&empty);
    let tag = exec_tag(tag, attrs).unwrap_or(tag);
    if let Some(p) = attrs.get("path").and_then(Value::as_str) {
        let target = Some(
            p.rsplit('/')
                .next()
                .filter(|s| !s.is_empty())
                .unwrap_or(p)
                .to_string(),
        );
        return CallTarget::new(tag, target);
    }
    if !matches!(tag, "bash" | "shell") {
        return CallTarget::new(tag, None);
    }
    let body = ev.get("body").and_then(Value::as_str);
    let target = body.and_then(|body| {
        for step in body.split("&&").flat_map(|s| s.split(';')) {
            let Some(word) = step.split_whitespace().next() else {
                continue;
            };
            if matches!(word, "cd" | "export" | "set" | "source" | "") || word.contains('=') {
                continue;
            }
            return Some(word.rsplit('/').next().unwrap_or(word).to_string());
        }
        None
    });
    CallTarget::new(tag, target)
}

/// What the repo looks like at the seam, where prose starts.
///
/// A run of invisible work is worth reading precisely because it changed
/// something on disk, and the reader should not have to go look. A commit the
/// kernel reported (`result.repo.committed` — the syscall's own output named
/// the sha) is the headline; otherwise the dirty count is.
///
/// The dirty count is read from the git pane's background snapshot. This used
/// to fork `git rev-parse` plus `git status --porcelain` on the UI thread,
/// from a function called after every syscall result and every finished
/// thought — 0.28s warm per status call in this repo, seconds of frozen frame
/// in a burst. The snapshot is stale by as much as one read, so a syscall
/// result forces a fresh one and [`WorkRun::settle`] rewrites the row when it
/// lands. The commit claim never comes from that snapshot: comparing HEAD
/// before/after raced the pane's worker and could attribute another
/// terminal's commit to this run.
fn git_tail(committed: Option<&str>, git: &side::GitPane) -> Option<String> {
    if let Some(sha) = committed {
        return Some(format!("\u{00b7} committed {sha}"));
    }
    match git.dirty()? {
        0 => Some("\u{00b7} tree clean".into()),
        1 => Some("\u{00b7} 1 file dirty".into()),
        n => Some(format!("\u{00b7} {n} files dirty")),
    }
}

/// One thing that happened where the story could not see it.
#[derive(Debug, Clone, PartialEq)]
enum Seg {
    /// A finished thought, in milliseconds.
    Thought(u64),
    /// A syscall: the tag, and a target only where one is structural.
    Call { tag: String, target: Option<String> },
}

/// A stretch of invisible work — the thoughts and syscalls between two pieces
/// of prose — folded into one line the reader can actually follow.
///
/// The story is a whitelist of narrative kinds, so a run of tool work shows up
/// as nothing but a stack of collapsed thoughts: three "Thought for 9s" rows
/// and no hint that six files were read and two were rewritten. This is the
/// connective tissue. It never carries a body, because it is built from tags
/// and attrs and never sees one.
#[derive(Default)]
pub(crate) struct WorkRun {
    segs: Vec<Seg>,
    /// Thought blocks to fold away once the row replaces them.
    thoughts: Vec<EntryId>,
    /// Short sha of the last commit the kernel reported in this run
    /// (`result.repo.committed`). The row's claim, never a HEAD-snapshot diff.
    committed: Option<String>,
    /// The repo tail for this run, built from the git pane's background
    /// snapshot as events arrive. `sync` cannot reach the pane — it is called
    /// from deep inside the stream cursor — and it must not shell out itself.
    tail: Option<String>,
    /// The row itself, once the run has earned one. Rewritten in place as the
    /// run grows, so it never moves and never stacks.
    row: Option<EntryId>,
    /// Full ordered segment records for compact rows. Kept by entry id after a
    /// run folds so the existing Story viewer can inspect what the one-line
    /// sentence deliberately compressed or elided.
    details: HashMap<EntryId, String>,
    /// Generation of the first git read that will see what the run's last
    /// syscall did — [`side::GitPane::poll`]'s answer, taken at the result.
    pub(crate) fresh_gen: u64,
    /// The row a fold just closed: its id, its sentence, the commit the
    /// kernel reported for it, and the read generation its tail is owed. Held
    /// until a read that old lands, because the dirty count written at the
    /// fold came from a snapshot older than the run's last syscall.
    pub(crate) settled: Option<(EntryId, String, Option<String>, u64)>,
}

/// Below this a run is not worth a row: the collapsed thought already says
/// everything, and a line for one grep is worse than silence.
const RUN_MIN_CALLS: usize = 2;

impl WorkRun {
    /// Take the repo state the git pane read on its worker thread. Called as
    /// events arrive, which is where `app.git` is reachable.
    pub(crate) fn note_repo(&mut self, git: &side::GitPane) {
        self.tail = git_tail(self.committed.as_deref(), git);
    }

    /// The kernel reported a commit on a result event (`repo.committed`).
    /// This is the only way the row earns a "committed" claim; the tail is
    /// rewritten at once so the next sync paints it without waiting for a
    /// git-pane read.
    pub(crate) fn commit(&mut self, sha: &str) {
        self.committed = Some(sha.to_string());
        self.tail = Some(format!("\u{00b7} committed {sha}"));
    }

    pub(crate) fn call(&mut self, _tag: &str, target: CallTarget) {
        self.segs.push(Seg::Call {
            tag: target.tag,
            target: target.target,
        });
    }

    pub(crate) fn thought(&mut self, _id: EntryId, elapsed_ms: Option<i64>) {
        // Thinking lives in Activity now, so the Story work-run summary must
        // not retain or remove an entry id from a different scrollback.
        self.segs
            .push(Seg::Thought(elapsed_ms.unwrap_or(0).max(0) as u64));
    }

    pub(crate) fn detail(&self, id: EntryId) -> Option<&str> {
        self.details.get(&id).map(String::as_str)
    }

    pub(crate) fn calls(&self) -> usize {
        self.segs
            .iter()
            .filter(|s| matches!(s, Seg::Call { .. }))
            .count()
    }

    /// Rewrite the row for the run so far. Called after every segment, so the
    /// reader watches the work accumulate instead of staring at a stack of
    /// collapsed thoughts until the answer arrives.
    ///
    /// The row is written in place: one entry per run, updated, never a second
    /// row and never a jump to the bottom. It appears only once the run has
    /// earned it, which is also when the thoughts it summarises are removed.
    pub(crate) fn sync(&mut self, story: &mut ScrollbackState) {
        if self.calls() < RUN_MIN_CALLS {
            return;
        }
        let line = row_line(&work_sentence(&self.segs), self.tail.as_deref());
        for id in self.thoughts.drain(..) {
            story.remove_entry(id);
        }
        let live = self.row.filter(|id| story.get_by_id(*id).is_some());
        let id = match live {
            Some(id) => {
                if let Some(entry) = story.get_by_id_mut(id) {
                    entry.block = RenderBlock::selectable_system(line);
                }
                story.mark_structurally_dirty(id);
                story.mark_height_dirty(id);
                id
            }
            None => {
                let id = story.push_block(RenderBlock::selectable_system(line));
                self.row = Some(id);
                id
            }
        };
        self.details
            .insert(id, work_detail(&self.segs, self.tail.as_deref()));
    }

    /// Close the run at the seam, just before prose starts. The last sync
    /// catches the final call; what git says about it is usually still in
    /// flight, so the row is handed to `settle` for one more read.
    pub(crate) fn fold(&mut self, story: &mut ScrollbackState) {
        self.sync(story);
        if let Some(id) = self.row {
            self.settled = Some((
                id,
                work_sentence(&self.segs),
                self.committed.clone(),
                self.fresh_gen,
            ));
        }
        self.reset();
    }

    /// Rewrite the folded row's tail from a git read that saw the run's last
    /// syscall.
    ///
    /// The dirty count is the racy part: the run's last syscall touches the
    /// tree, prose starts a fraction of a second later and folds the row from
    /// the snapshot that preceded it. Waiting on the generation rather than on
    /// "the next read to land" matters because when the change landed behind a
    /// read already in flight, the next snapshot to arrive is the one that
    /// started too early. The commit claim needs no wait — it is the kernel's
    /// verdict, carried through the fold — but the row still deserves the
    /// fresh read when there is none.
    pub(crate) fn settle(&mut self, story: &mut ScrollbackState, git: &side::GitPane) {
        let Some((id, sentence, committed, need)) = self.settled.take() else {
            return;
        };
        if committed.is_none() && git.snap_gen() < need {
            self.settled = Some((id, sentence, committed, need));
            return;
        }
        let Some(entry) = story.get_by_id_mut(id) else {
            return;
        };
        let tail = git_tail(committed.as_deref(), git);
        entry.block = RenderBlock::selectable_system(row_line(&sentence, tail.as_deref()));
        if let Some(detail) = self.details.get_mut(&id) {
            while detail.lines().last().is_some_and(|line| line.starts_with("Repository:")) {
                let keep = detail.rfind('\n').unwrap_or(0);
                detail.truncate(keep);
            }
            if let Some(tail) = tail {
                detail.push_str("\nRepository: ");
                detail.push_str(tail.trim_start_matches('·').trim());
            }
        }
        story.mark_structurally_dirty(id);
        story.mark_height_dirty(id);
    }

    pub(crate) fn reset(&mut self) {
        self.fresh_gen = 0;
        self.segs.clear();
        self.thoughts.clear();
        self.committed = None;
        self.tail = None;
        self.row = None;
    }
}

/// The work row as it is written: the sentence, then the repo tail if there
/// is one. Two spaces, because the tail already opens with its own `·`.
fn row_line(sentence: &str, tail: Option<&str>) -> String {
    match tail {
        Some(t) => format!("{sentence}  {t}"),
        None => sentence.to_string(),
    }
}

/// The popup record: every segment in order, without grouping or elision.
fn work_detail(segs: &[Seg], tail: Option<&str>) -> String {
    let mut rows = vec!["Activity sequence".to_string()];
    for (index, seg) in segs.iter().enumerate() {
        let value = match seg {
            Seg::Thought(ms) => format!("thought {} ({ms} ms)", human_secs(*ms)),
            Seg::Call { tag, target: Some(target) } => format!("{tag} → {target}"),
            Seg::Call { tag, target: None } => tag.clone(),
        };
        rows.push(format!("{:>2}. {value}", index + 1));
    }
    if let Some(tail) = tail {
        rows.push(format!("Repository: {}", tail.trim_start_matches('·').trim()));
    }
    rows.join("\n")
}

/// Render a run as one line: thoughts as durations, calls compressed.
///
/// Consecutive calls with the same tag become `tag xN`, a lone call keeps its
/// target, and a run longer than [`SENTENCE_MAX`] groups elides its middle. One
/// line, always, so the row cannot push the transcript around as it grows.
fn work_sentence(segs: &[Seg]) -> String {
    // Group into alternating thought / work phases.
    let mut phases: Vec<String> = Vec::new();
    let mut work: Vec<(String, Option<String>, usize)> = Vec::new();
    let flush = |work: &mut Vec<(String, Option<String>, usize)>, out: &mut Vec<String>| {
        if work.is_empty() {
            return;
        }
        let parts: Vec<String> = work
            .drain(..)
            .map(|(tag, target, n)| match (n, target) {
                (1, Some(t)) => format!("{tag} {t}"),
                (1, None) => tag,
                (n, _) => format!("{tag} \u{00d7}{n}"),
            })
            .collect();
        out.push(parts.join(", "));
    };
    for seg in segs {
        match seg {
            Seg::Thought(ms) => {
                flush(&mut work, &mut phases);
                phases.push(format!("thought {}", human_secs(*ms)));
            }
            Seg::Call { tag, target } => match work.last_mut() {
                Some((t, _, n)) if t == tag => *n += 1,
                _ => work.push((tag.clone(), target.clone(), 1)),
            },
        }
    }
    flush(&mut work, &mut phases);

    const SENTENCE_MAX: usize = 5;
    if phases.len() > SENTENCE_MAX {
        let head = phases[..2].join(" \u{2192} ");
        let tail = phases[phases.len() - 2..].join(" \u{2192} ");
        return format!("{head} \u{2192} \u{2026} \u{2192} {tail}");
    }
    phases.join(" \u{2192} ")
}

/// Durations read as durations: 900ms is "0.9s", 95s is "1m35s".
fn human_secs(ms: u64) -> String {
    let secs = ms as f64 / 1000.0;
    if secs < 10.0 {
        format!("{secs:.1}s")
    } else if secs < 60.0 {
        format!("{}s", secs.round() as u64)
    } else {
        let s = secs.round() as u64;
        format!("{}m{:02}s", s / 60, s % 60)
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn call(tag: &str, target: Option<&str>) -> Seg {
        Seg::Call {
            tag: tag.into(),
            target: target.map(Into::into),
        }
    }

    /// The sentence is the whole point: it has to read like one.
    #[test]
    fn a_run_reads_as_one_sentence() {
        let segs = vec![
            Seg::Thought(12_000),
            call("edit", Some("main.rs")),
            call("python", None),
            call("python", None),
            call("bash", Some("cargo")),
            Seg::Thought(8_400),
            call("bash", Some("cargo")),
        ];
        assert_eq!(
            work_sentence(&segs),
            "thought 12s \u{2192} edit main.rs, python \u{00d7}2, bash cargo \u{2192} thought 8.4s \u{2192} bash cargo"
        );
    }

    #[test]
    fn a_long_run_elides_its_middle_instead_of_wrapping() {
        let mut segs = Vec::new();
        for i in 0..8 {
            segs.push(Seg::Thought(1_000 * (i + 1)));
            segs.push(call("bash", Some("git")));
        }
        let line = work_sentence(&segs);
        assert!(line.contains('\u{2026}'), "long run must elide: {line}");
        assert!(line.len() < 90, "still too long: {line}");
    }

    #[test]
    fn detail_keeps_every_segment_that_the_summary_elides() {
        let mut segs = Vec::new();
        for i in 0..8 {
            segs.push(Seg::Thought(1_000 * (i + 1)));
            segs.push(call("read", Some(&format!("file-{i}.rs"))));
        }
        let summary = work_sentence(&segs);
        let detail = work_detail(&segs, Some("· tree clean"));
        assert!(summary.contains('\u{2026}'), "fixture must exercise summary elision");
        assert!(!detail.contains('\u{2026}'), "detail must never elide: {detail}");
        assert_eq!(detail.lines().filter(|line| line.contains(". ")).count(), 16);
        for i in 0..8 {
            assert!(detail.contains(&format!("file-{i}.rs")), "missing segment {i}: {detail}");
        }
        assert!(detail.ends_with("Repository: tree clean"));
    }

    #[test]
    fn durations_read_as_durations() {
        assert_eq!(human_secs(900), "0.9s");
        assert_eq!(human_secs(9_400), "9.4s");
        assert_eq!(human_secs(12_000), "12s");
        assert_eq!(human_secs(95_000), "1m35s");
    }

    #[test]
    fn canonical_exec_ops_use_their_legacy_work_tags() {
        for op in ["python", "bash", "shell"] {
            let attrs = json!({"op": op});
            assert_eq!(exec_tag("exec", &attrs), Some(op));
            assert_eq!(exec_tag(op, &json!({})), Some(op));

            let ev = json!({"attrs": attrs});
            let mut run = WorkRun::default();
            run.call("exec", call_target("exec", &ev));
            assert!(matches!(
                run.segs.as_slice(),
                [Seg::Call { tag, target: None }] if tag == op
            ));
        }
        assert_eq!(exec_tag("exec", &json!({"op": "unknown"})), None);
        let ev = json!({"attrs": {"op": "unknown"}});
        let mut run = WorkRun::default();
        run.call("exec", call_target("exec", &ev));
        assert!(matches!(
            run.segs.as_slice(),
            [Seg::Call { tag, target: None }] if tag == "exec"
        ));
    }

    /// A shell command contributes its program and nothing else. This is the
    /// rule that keeps the row from becoming a second calls pane.
    #[test]
    fn a_shell_call_contributes_its_program_not_its_command() {
        let ev = json!({
            "tag": "bash",
            "body": "cd /Users/zeus/hub/desmos && cargo test --workspace 2>&1 | grep FAIL",
        });
        assert_eq!(call_target("bash", &ev).as_deref(), Some("cargo"));
        let canonical = json!({
            "tag": "exec",
            "attrs": {"op": "bash"},
            "body": "cd /Users/zeus/hub/desmos && cargo test --workspace",
        });
        assert_eq!(call_target("exec", &canonical).as_deref(), Some("cargo"));
        let legacy_shell = json!({"tag": "shell", "body": "/usr/bin/git status"});
        assert_eq!(call_target("shell", &legacy_shell).as_deref(), Some("git"));
        let canonical_shell =
            json!({"tag": "exec", "attrs": {"op": "shell"}, "body": "/usr/bin/git status"});
        assert_eq!(call_target("exec", &canonical_shell).as_deref(), Some("git"));
        let edit = json!({"tag": "edit", "attrs": {"path": "crates/desmos-tui/src/main.rs"}});
        assert_eq!(call_target("edit", &edit).as_deref(), Some("main.rs"));
    }
}
