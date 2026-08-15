//! The speech stream: what the reader sees of a turn while it is still
//! arriving. `StreamCursor` buffers thinking/speech deltas; `spoken_prefix`
//! and `strip_syscalls` decide, with `scan.py`'s eyes, which bytes are prose
//! and which are a syscall the calls pane already owns -- *mid-stream only*.
//! At turn end the kernel's own verdict arrives as `complete.spans` and
//! `finish_speech_spans` strips exactly those bytes; the local grammar port is
//! the hold and the fallback (stop/error turns), never the final word on a
//! completed turn. Moved verbatim out of main.rs.

use std::borrow::Cow;

use xai_grok_pager::scrollback::{DisplayMode, EntryId, RenderBlock, ScrollbackState};

use crate::{WorkRun, set_wire_mode};

/// In-flight thinking / speech. Deltas buffer here; grok markdown
/// (`push_chunk`) runs once per frame, not once per SSE token.
#[derive(Default)]
pub(crate) struct StreamCursor {
    pub(crate) think: Option<EntryId>,
    pub(crate) speech: Option<EntryId>,
    pub(crate) speech_raw: String,
    pub(crate) speech_shown: String,
    pub(crate) pending_think: String,
    /// The invisible stretch since the last prose.
    pub(crate) run: WorkRun,
}

impl StreamCursor {
    pub(crate) fn live(&self) -> bool {
        self.think.is_some() || self.speech.is_some()
    }

    pub(crate) fn flush(&mut self, story: &mut ScrollbackState, activity: &mut ScrollbackState) {
        if let Some(id) = self.think {
            if !self.pending_think.is_empty() {
                activity.push_chunk_to_thinking(id, &self.pending_think);
                self.pending_think.clear();
            }
        }
        self.flush_speech(story);
    }

    pub(crate) fn flush_speech(&mut self, story: &mut ScrollbackState) {
        let shown = spoken_prefix(&self.speech_raw);
        self.show_speech(story, shown);
    }

    pub(crate) fn show_speech(&mut self, story: &mut ScrollbackState, shown: String) {
        if shown.trim().is_empty() && self.speech.is_none() {
            self.speech_shown = shown;
            return;
        }
        if self.speech.is_none() && !shown.is_empty() {
            self.run.fold(story);
            self.speech = Some(story.start_streaming_agent());
        }
        if let Some(id) = self.speech {
            if shown.starts_with(&self.speech_shown) {
                let extra = &shown[self.speech_shown.len()..];
                if !extra.is_empty() {
                    story.push_chunk_to_agent(id, extra);
                }
            } else {
                story.finish_running(id);
                let nid = story.start_streaming_agent();
                self.speech = Some(nid);
                if !shown.is_empty() {
                    story.push_chunk_to_agent(nid, &shown);
                }
            }
        }
        self.speech_shown = shown;
    }

    pub(crate) fn finish_think(&mut self, story: &mut ScrollbackState) {
        if let Some(id) = self.think {
            if !self.pending_think.is_empty() {
                story.push_chunk_to_thinking(id, &self.pending_think);
                self.pending_think.clear();
            }
        }
        if let Some(id) = self.think.take() {
            let empty = story.get_by_id(id).is_some_and(|e| match &e.block {
                RenderBlock::Thinking(t) => t.text().trim().is_empty(),
                _ => false,
            });
            if empty {
                story.remove_entry(id);
            } else {
                story.finish_running(id);
                // A live thought streams Expanded, and grok keeps an Expanded
                // thinking block expanded on finish (Ctrl+E stickiness). The
                // record we want is one row, so say so explicitly.
                set_wire_mode(story, id, DisplayMode::Collapsed);
                let ms = story.get_by_id(id).and_then(|e| match &e.block {
                    RenderBlock::Thinking(t) => t.elapsed_time_ms(),
                    _ => None,
                });
                self.run.thought(id, ms);
                self.run.sync(story);
            }
        }
    }

    pub(crate) fn finish_speech(&mut self, story: &mut ScrollbackState) {
        // Nothing is in flight once the stream is over, so release what
        // `spoken_prefix` held back: an opener whose closer never arrived is a
        // mention, not a call, and the kernel is about to dispatch nothing for
        // it. Without this pass the hold is never lifted -- `speech_raw` is
        // cleared three lines down -- and the tail of the message is printed
        // in neither pane.
        //
        // This is the *fallback* final pass, for the turns that end without a
        // kernel verdict: a user stop or an error kills the step before the
        // complete event fires. A turn that completes normally is finalized by
        // `finish_speech_spans` instead, from `complete.spans` -- the local
        // grammar port never gets the last word there.
        let shown = strip_syscalls(&self.speech_raw).trim_start().to_string();
        self.show_speech(story, shown);
        if let Some(id) = self.speech.take() {
            story.finish_running(id);
        }
        self.speech_raw.clear();
        self.speech_shown.clear();
    }

    /// Turn-end reconcile (Phase 3). `complete.spans` is the kernel saying,
    /// byte for byte, which stretches of the final speech it dispatched --
    /// authoritative where `strip_syscalls` is a port of the same grammar that
    /// can drift. What disappears from the story is exactly those bytes.
    ///
    /// When the mid-stream hold already printed something the kernel
    /// dispatched (the hold is conservative, not clairvoyant), the reconciled
    /// text no longer extends what is on screen; appending through
    /// `show_speech` would finish the stale block and print the whole message
    /// again beside it. Remove the half-printed block and re-show from the
    /// kernel's verdict instead.
    pub(crate) fn finish_speech_spans(
        &mut self,
        story: &mut ScrollbackState,
        spans: &[(usize, usize)],
    ) {
        let shown = match without_spans(&self.speech_raw, spans) {
            Some(s) => s.trim_start().to_string(),
            // Spans that do not fit this speech (a bridge/TUI version skew is
            // the only way): the local grammar is the self-heal, as mid-stream.
            None => strip_syscalls(&self.speech_raw).trim_start().to_string(),
        };
        if let Some(id) = self.speech {
            if !shown.starts_with(&self.speech_shown) {
                story.remove_entry(id);
                self.speech = None;
                self.speech_shown.clear();
            }
        }
        self.show_speech(story, shown);
        if let Some(id) = self.speech.take() {
            story.finish_running(id);
        }
        self.speech_raw.clear();
        self.speech_shown.clear();
    }

    pub(crate) fn finish(&mut self, story: &mut ScrollbackState, activity: &mut ScrollbackState) {
        self.finish_think(activity);
        self.finish_speech(story);
    }

    /// `finish`, but the speech pass uses the kernel's spans (see
    /// `finish_speech_spans`). Called on the `complete` event, the one carrier
    /// of the authoritative spans.
    pub(crate) fn finish_reconciled(
        &mut self,
        story: &mut ScrollbackState,
        activity: &mut ScrollbackState,
        spans: &[(usize, usize)],
    ) {
        self.finish_think(activity);
        self.finish_speech_spans(story, spans);
    }
}

/// `text` minus the given byte ranges. None when the ranges do not fit the
/// text (out of order, out of bounds, or off a char boundary) -- the caller
/// falls back to the local grammar rather than panicking on wire data.
fn without_spans(text: &str, spans: &[(usize, usize)]) -> Option<String> {
    let mut out = String::with_capacity(text.len());
    let mut at = 0usize;
    for &(start, end) in spans {
        if start < at || end < start {
            return None;
        }
        out.push_str(text.get(at..start)?);
        at = end;
    }
    out.push_str(text.get(at..)?);
    Some(out)
}

pub(crate) fn spoken_prefix(text: &str) -> String {
    let (spans, open_fence) = code_spans(text);
    // Mid-stream an open fence is still a code block: its closer is one of the
    // things the stream has not delivered yet. Masking it keeps a half-written
    // `<div` in a fenced HTML sample from stalling the render, and the tail
    // released by `finish_speech` is the one that follows `scan.py`.
    let mut live = spans.clone();
    if let Some(f) = open_fence {
        live.push((f, text.len()));
    }
    // A call whose closer has not arrived yet is a syscall in flight. Hold
    // everything from its `<` back: the alternative is that the body streams
    // into the story as prose, and it stays there, because by the time the
    // closer lands the chunk has already been appended to a live block.
    //
    // The streaming path must strip bodies, not just markers: dropping the
    // markers alone is right for prose about markup and wrong for a command.
    let mut cut = text.len();
    let mut i = 0usize;
    while let Some(hit) = next_tag(text, i, &live) {
        match hit.end {
            Some(end) => i = end,
            None => {
                cut = hit.start; // opener still being typed, or body still arriving
                break;
            }
        }
    }
    // Under an open fence the two readings differ, and only for a tag the
    // kernel would run: if the fence closes it is code, and if it never closes
    // `scan.py` dispatches it. Nobody can tell which yet, so hold there rather
    // than print a call and retract it a frame later -- a retraction that
    // cannot be taken back once the chunk is in a live block. A tag with no
    // closer needs no hold *yet* -- but mid-stream "no closer" only means it
    // has not arrived. Streaming past it printed the raw call, and when the
    // closer landed the cut moved backwards, so `shown` no longer began with
    // what was already drawn: flush_speech finished the stale block and opened
    // a second one, leaving the truncated copy in the story for good. Holding
    // at the first tag start costs nothing -- a fenced sample stalls at the `<`
    // until the fence closes either way.
    if let Some(f) = open_fence {
        if let Some(hit) = next_tag(text, f, &spans) {
            cut = cut.min(hit.start);
        }
    }
    // A call that opens the turn leaves the prose behind it beginning with the
    // newlines that separated the two. Those survive as an empty first line,
    // and the timestamp overlay always lands on the first content line -- so
    // the stamp ends up alone on a blank row, one row above the sentence it
    // belongs to, and a one-line reply costs four rows instead of one.
    // Leading whitespace is never information here, and trimming it is stable
    // under streaming: once consumed it stays consumed, so the prefix check in
    // flush_speech still holds.
    strip_syscalls(&text[..cut]).trim_start().to_string()
}

/// A trailing `<` is only worth withholding if it could open a tag: `<`
/// followed by a letter or `/`. Without this, `if a < b` in streamed prose
/// stalls the render until some later `>` arrives.
fn looks_like_tag_start(rest: &str) -> bool {
    let mut it = rest.chars();
    it.next();
    match it.next() {
        Some('/') => it.next().is_some_and(|c| c.is_ascii_alphabetic()),
        Some(c) => c.is_ascii_alphabetic(),
        None => true,
    }
}

/// Byte ranges of `text` that are literal code -- fenced blocks (fence lines
/// included), inline backtick spans, indented blocks -- and, separately, the
/// offset of a fence that never closed. XML stripping must leave the ranges
/// alone, or `<div>` inside a fenced HTML sample silently vanishes from the
/// story.
///
/// An unterminated fence is deliberately *not* one of the ranges.
/// `scan.py::_fence_span` returns None for it and says why: masking to end of
/// text would drop every syscall written after one stray backtick run. So the
/// kernel really does dispatch `<bash>ls</bash>` sitting under an unclosed
/// fence, and if the story masked it the call would be run *and* printed raw
/// as prose. Its offset comes back separately because the streaming caller has
/// one more thing to decide with it: mid-stream, that fence may simply be
/// waiting for a closer that has not arrived yet.
fn code_spans(text: &str) -> (Vec<(usize, usize)>, Option<usize>) {
    fn run(s: &str, c: char) -> usize {
        s.chars().take_while(|&x| x == c).count()
    }

    let mut spans: Vec<(usize, usize)> = Vec::new();
    // fence char, opener length, start offset, and how many spans predate it.
    let mut fence: Option<(char, usize, usize, usize)> = None;
    let mut off = 0usize;

    for line in text.split_inclusive('\n') {
        let trimmed = line.trim_start();
        let indent = line.len() - trimmed.len();
        let first = trimmed.chars().next();
        let mut fenced_line = false;

        match fence {
            Some((fc, flen, start, mark)) => {
                let n = run(trimmed, fc);
                if first == Some(fc) && n >= flen && trimmed[n..].trim().is_empty() {
                    // Closed. The block is one span, and the backtick runs
                    // recorded inside it were never inline spans of their own.
                    spans.truncate(mark);
                    spans.push((start, off + line.len()));
                    fence = None;
                    fenced_line = true;
                }
            }
            None => {
                if indent <= 3 && (first == Some('`') || first == Some('~')) {
                    let fc = first.unwrap();
                    let n = run(trimmed, fc);
                    if n >= 3 {
                        fence = Some((fc, n, off, spans.len()));
                        fenced_line = true;
                    }
                }
            }
        }

        // Interior lines are measured for inline spans as they go, because a
        // fence that never closes leaves them exactly that: ordinary lines
        // whose backticks still open and close spans, the way `scan.py` reads
        // them once `_fence_span` has declined to mask anything.
        if !fenced_line {
            inline_code_spans(line, off, &mut spans);
        }
        off += line.len();
    }

    let open = fence.map(|(_, _, start, _)| start);
    spans.extend(indented_spans(text));
    (spans, open)
}

/// Leading-whitespace width with tabs expanded to the next multiple of four,
/// the way `scan.py::_indent_width` measures it.
fn indent_width(line: &str) -> usize {
    let mut w = 0usize;
    for ch in line.chars() {
        match ch {
            ' ' => w += 1,
            '\t' => w += 4 - w % 4,
            _ => break,
        }
    }
    w
}

fn expand_tabs(line: &str) -> Cow<'_, str> {
    if !line.contains('\t') {
        return Cow::Borrowed(line);
    }
    let mut out = String::with_capacity(line.len() + 8);
    for ch in line.chars() {
        if ch == '\t' {
            let col = out.chars().count();
            out.push_str(&" ".repeat(4 - col % 4));
        } else {
            out.push(ch);
        }
    }
    Cow::Owned(out)
}

/// A list marker at the head of a tab-expanded line: how far the marker runs
/// (including the space after it) and whether that space was there. `scan.py`
/// spells this `BULLET`; both need it to know where a list item's content
/// starts, because indented code is measured from that column and not from
/// zero.
fn bullet(line: &str) -> Option<(usize, bool)> {
    let b = line.as_bytes();
    let mut i = 0usize;
    while i < b.len() && b[i] == b' ' {
        i += 1;
    }
    if i < b.len() && matches!(b[i], b'-' | b'*' | b'+') {
        i += 1;
    } else {
        let digits = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if i == digits || i >= b.len() || !matches!(b[i], b'.' | b')') {
            return None;
        }
        i += 1;
    }
    let after = i;
    while i < b.len() && (b[i] == b' ' || b[i] == b'\t') {
        i += 1;
    }
    if i > after {
        Some((i, true))
    } else if after == b.len() {
        Some((after, false))
    } else {
        None
    }
}

/// Byte ranges of CommonMark *indented* code blocks: a line four columns past
/// the enclosing list item's content column, opening after a blank line, and
/// everything that stays that deep.
///
/// The port of `scan.py::_indent_span` + `_list_col`, and it has to stay one:
/// the dispatcher does not run a tag inside one of these, so if the story
/// stripped it the sample would appear in neither pane — not dispatched, not
/// printed. The list column is the fiddly half and cannot be dropped: under
/// `- ` content starts at column 2, so four spaces there is an ordinary
/// paragraph of that item and a real call.
fn indented_spans(text: &str) -> Vec<(usize, usize)> {
    let mut spans: Vec<(usize, usize)> = Vec::new();
    let mut cols: Vec<usize> = Vec::new();
    let mut blank = true;
    let mut block_end = 0usize;
    let mut off = 0usize;
    for line in text.split('\n') {
        let opens = line.starts_with("    ") || line.starts_with('\t');
        if off >= block_end && blank && opens {
            let floor = cols.last().copied().unwrap_or(0) + 4;
            if indent_width(line) >= floor {
                let mut end = off + line.len();
                for next in text[end.min(text.len())..].split('\n').skip(1) {
                    if !next.trim().is_empty() && indent_width(next) < floor {
                        break;
                    }
                    end += next.len() + 1;
                }
                let end = end.min(text.len());
                spans.push((off, end));
                block_end = end;
            }
        }
        if line.trim().is_empty() {
            blank = true;
        } else {
            let ind = indent_width(line);
            let expanded = expand_tabs(line);
            let mark = bullet(&expanded);
            if blank || mark.is_some() {
                while cols.last().is_some_and(|&c| ind < c) {
                    cols.pop();
                }
            }
            if let Some((len, spaced)) = mark {
                if ind <= cols.last().copied().unwrap_or(0) + 3 {
                    cols.push(len + usize::from(!spaced));
                }
            }
            blank = false;
        }
        off += line.len() + 1;
    }
    spans
}

/// Backtick-delimited inline spans on one line.
///
/// A run with no matching closer is not a span — it is literal text, which is
/// what CommonMark says, what the markdown renderer draws, and what
/// `scan.py::_in_code_span` decides. Treating it as code to end of line put
/// the two out of step: one stray backtick ahead of a real call meant the
/// dispatcher ran the call and pushed its card while the story printed the
/// raw tag as prose — the same call in both panes, in two different shapes.
/// Nothing is eaten by this: a half-streamed `` `<tag `` is withheld by
/// `spoken_prefix`'s unterminated-tag cut instead, and printed once it closes.
fn inline_code_spans(line: &str, base: usize, out: &mut Vec<(usize, usize)>) {
    let b = line.as_bytes();
    let mut i = 0usize;
    while i < b.len() {
        if b[i] != b'`' {
            i += 1;
            continue;
        }
        let mut n = 0usize;
        while i + n < b.len() && b[i + n] == b'`' {
            n += 1;
        }
        let start = i;
        let mut j = i + n;
        let mut close = None;
        while j < b.len() {
            if b[j] == b'`' {
                let mut m = 0usize;
                while j + m < b.len() && b[j + m] == b'`' {
                    m += 1;
                }
                if m == n {
                    close = Some(j + m);
                    break;
                }
                j += m;
            } else {
                j += 1;
            }
        }
        let Some(end) = close else {
            i = start + n;
            continue;
        };
        out.push((base + start, base + end));
        i = end;
    }
}

fn in_code(spans: &[(usize, usize)], i: usize) -> bool {
    spans.iter().any(|&(a, z)| i >= a && i < z)
}

/// Tags whose body is executed verbatim, and the only ones the closing-tag
/// quoting heuristic applies to. The port of `scan.py::_QUOTED_BODY`, and it
/// has to stay the same set: widen it and prose bodies vanish from the story
/// on an apostrophe, narrow it and a quoted `</bash>` truncates a command.
const QUOTED_BODY: [&str; 4] = ["python", "bash", "shell", "register"];

/// One tag the way `scan.py::scan_spans` sees it.
struct TagHit {
    /// Offset of the `<`.
    start: usize,
    /// Offset just past the `>` of the opener.
    open_end: usize,
    /// End of the whole call, or None when no closer ever arrived. The kernel
    /// does not half-dispatch a cut-off reply -- `scan_spans` skips such an
    /// opener entirely -- so None means "this is prose", not "this is a call".
    end: Option<usize>,
}

/// The next syscall in `text` at or after `from`, skipping `spans`.
///
/// This is the single place the story decides what ran, and it answers exactly
/// what `scan.py::scan_spans` answers, because the two disagreeing is visible:
/// strip something the kernel left inert and the text is in neither pane, keep
/// something the kernel ran and it is raw prose in the story *and* a card in
/// the calls pane.
///
/// Not a call, and therefore skipped as ordinary text: a lone `</bash>`
/// (`TAG_OPEN` cannot match one, so the kernel never sees a tag there) and a
/// `<>` with no name.
fn next_tag(text: &str, from: usize, spans: &[(usize, usize)]) -> Option<TagHit> {
    let mut i = from;
    while let Some(rel) = text[i..].find('<') {
        let start = i + rel;
        if in_code(spans, start) || !looks_like_tag_start(&text[start..]) {
            i = start + 1;
            continue;
        }
        let Some(gt) = text[start..].find('>') else {
            // Still being typed; there is no tag until the `>` lands.
            return Some(TagHit { start, open_end: text.len(), end: None });
        };
        let open_end = start + gt + 1;
        let inner = &text[start + 1..open_end - 1];
        // Name runs to the first space, and a self-closing marker has no body.
        let name = inner
            .trim_end_matches('/')
            .split_whitespace()
            .next()
            .unwrap_or("");
        if name.is_empty() || name.starts_with('/') {
            i = open_end;
            continue;
        }
        if inner.trim_end().ends_with('/') {
            return Some(TagHit { start, open_end, end: Some(open_end) });
        }
        // An explicit end token makes the body opaque, exactly as in
        // `scan.py`: the call runs to its `name:TOKEN` closer and every bare
        // closer inside it is ordinary text. Miss this and the stripper hunts
        // for a closer that is never written, calls the opener unterminated,
        // and paints the whole body -- plus the token closer -- into the story.
        if let Some(token) = end_token(inner) {
            let usable = !token.is_empty()
                && token
                    .chars()
                    .all(|c| c.is_alphanumeric() || c == '_' || c == '.' || c == '-');
            // An unusable token is dropped by the kernel rather than falling
            // back to a bare closer, so nothing ran and nothing is hidden.
            let end = if usable {
                find_custom_close(text, name, &token, open_end)
            } else {
                None
            };
            return Some(TagHit { start, open_end, end });
        }
        // The body ends at the first closer that is not inside a quoted
        // string, which is `scan_spans`'s rule and its reason: the only closer
        // a model writes early is one it quoted (`echo "</bash>"`), and
        // stopping there truncates the body and runs half the program.
        //
        // Only for `QUOTED_BODY` tags, exactly as in scan.py. A commit message
        // is prose, and prose has apostrophes: "the TUI's stripper" opens a
        // quote that never closes, so every closer after it reads as quoted,
        // the call reads as unterminated, and the whole message lands in the
        // story pane. scan.py learned this at the cost of three lost commits.
        let quoted_body = QUOTED_BODY.contains(&name);
        let mut at = open_end;
        loop {
            let Some((cs, ce)) = find_close(text, name, at) else {
                return Some(TagHit { start, open_end, end: None });
            };
            if quoted_body && in_string(&text[open_end..cs]) {
                at = ce;
                continue;
            }
            return Some(TagHit { start, open_end, end: Some(ce) });
        }
    }
    None
}

/// `</name>` at or after `from`, allowing the space `scan.py`'s closer regex
/// allows (`</name\s*>`). Returns the range the closer occupies.
fn find_close(text: &str, name: &str, from: usize) -> Option<(usize, usize)> {
    let pat = format!("</{name}");
    let mut i = from;
    while let Some(rel) = text[i..].find(&pat) {
        let s = i + rel;
        let rest = &text[s + pat.len()..];
        let ws = rest.len() - rest.trim_start().len();
        if rest[ws..].starts_with('>') {
            return Some((s, s + pat.len() + ws + 1));
        }
        i = s + pat.len();
    }
    None
}

/// The `name:TOKEN` closer at or after `from`, allowing the space `scan.py`'s
/// custom closer allows around the colon and before the `>`. Returns the offset
/// just past it.
fn find_custom_close(text: &str, name: &str, token: &str, from: usize) -> Option<usize> {
    let open = format!("</{name}");
    let mut i = from;
    while let Some(rel) = text[i..].find(&open) {
        let s = i + rel;
        let rest = &text[s + open.len()..];
        let a = rest.len() - rest.trim_start().len();
        if let Some(after) = rest[a..].strip_prefix(':') {
            let b = after.len() - after.trim_start().len();
            if let Some(tail) = after[b..].strip_prefix(token) {
                let c = tail.len() - tail.trim_start().len();
                if tail[c..].starts_with('>') {
                    return Some(s + open.len() + a + 1 + b + token.len() + c + 1);
                }
            }
        }
        i = s + open.len();
    }
    None
}

/// The `end="TOKEN"` attribute of an opener, if it declared one. Values may be
/// quoted or bare, which is what `scan.py::ATTR` accepts.
fn end_token(inner: &str) -> Option<String> {
    let b = inner.as_bytes();
    let mut i = 0usize;
    while i < b.len() {
        if !(b[i].is_ascii_alphabetic() || b[i] == b'_') {
            i += 1;
            continue;
        }
        let s = i;
        while i < b.len()
            && (b[i].is_ascii_alphanumeric() || b[i] == b'_' || b[i] == b'.' || b[i] == b'-')
        {
            i += 1;
        }
        let name = &inner[s..i];
        let mut j = i;
        while j < b.len() && (b[j] as char).is_whitespace() {
            j += 1;
        }
        if j >= b.len() || b[j] != b'=' {
            continue;
        }
        j += 1;
        while j < b.len() && (b[j] as char).is_whitespace() {
            j += 1;
        }
        if j >= b.len() {
            return None;
        }
        let (value, next) = if b[j] == b'"' || b[j] == b'\'' {
            let quote = b[j];
            let vs = j + 1;
            let mut k = vs;
            while k < b.len() && b[k] != quote {
                k += 1;
            }
            (&inner[vs..k], (k + 1).min(b.len()))
        } else {
            let vs = j;
            let mut k = j;
            while k < b.len()
                && !(b[k] as char).is_whitespace()
                && b[k] != b'"'
                && b[k] != b'\''
                && b[k] != b'>'
            {
                k += 1;
            }
            (&inner[vs..k], k)
        };
        i = next;
        if name == "end" {
            return Some(value.to_string());
        }
    }
    None
}

/// Does `body` end inside an unclosed quote? The port of `scan.py::_in_string`,
/// and it decides where a syscall body ends. Bash and Python agree on the part
/// that matters: a run of `'` or `"` opens, the same run closes, a backslash
/// escapes the next character inside a double quote, and a triple run swallows
/// the closing tag a docstring happens to mention.
fn in_string(body: &str) -> bool {
    let b = body.as_bytes();
    let n = b.len();
    let mut i = 0usize;
    while i < n {
        if b[i] == b'\\' {
            i += 2;
            continue;
        }
        if b[i] != b'"' && b[i] != b'\'' {
            i += 1;
            continue;
        }
        let run = if b[i..].starts_with(b"\"\"\"") || b[i..].starts_with(b"'''") {
            3
        } else {
            1
        };
        let quote = &b[i..i + run];
        let mut j = i + run;
        while j < n {
            if b[j] == b'\\' && run == 1 && quote[0] == b'"' {
                j += 2;
                continue;
            }
            if b[j..].starts_with(quote) {
                break;
            }
            j += 1;
        }
        if j >= n {
            return true; // opened and never closed: everything after is string
        }
        i = j + run;
    }
    false
}

/// Strip whole syscalls from prose -- markers *and* bodies.
///
/// Deleting the markers alone is not enough: it leaves the command behind as
/// if someone had said it. The command is not prose; it belongs to the calls
/// pane, which already renders it as a card.
///
/// Mid-stream and fallback only. On a completed turn the final story text
/// comes from the kernel's `complete.spans` (`finish_speech_spans`), not from
/// this port of the grammar -- its former final-state role ended with Phase 3.
///
/// Structure decides, not a list of names: an opener with a matching closer is
/// a syscall whatever it is called, which means a tag registered later needs no
/// change here. A bare mention with no closer is left alone -- marker and all,
/// because the kernel leaves it alone too -- so naming a tool mid-sentence
/// still reads.
pub(crate) fn strip_syscalls(text: &str) -> String {
    let (spans, _) = code_spans(text);
    let mut out = String::new();
    let mut i = 0usize;
    while let Some(hit) = next_tag(text, i, &spans) {
        out.push_str(&text[i..hit.start]);
        match hit.end {
            Some(end) => i = end,
            None => {
                // Inert to the dispatcher, so it stays visible here or it is
                // in neither pane.
                out.push_str(&text[hit.start..hit.open_end]);
                i = hit.open_end;
            }
        }
    }
    out.push_str(&text[i..]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Code spans are protected: markup inside a fence or backticks is the
    /// reader's subject matter, not a call. This was covered against a stripper
    /// that no longer exists, so it is re-pinned against the one that runs.
    #[test]
    fn markup_inside_code_is_not_a_call() {
        let fenced = "see\n```html\n<div class=\"x\">hi</div>\n```\ndone";
        let got = strip_syscalls(fenced);
        assert!(got.contains("<div class=\"x\">"), "fenced opener stripped: {got}");
        assert!(got.contains("</div>"), "fenced closer stripped: {got}");
        let inline = "use `<python>` not <python>x</python>";
        assert_eq!(strip_syscalls(inline), "use `<python>` not ");
    }

    #[test]
    fn a_syscall_leaves_nothing_behind() {
        let one = format!("{}bash{}cd /tmp && cargo test{}bash{}", '<', '>', "</", '>');
        assert_eq!(strip_syscalls(&one).trim(), "", "body survived: {:?}", strip_syscalls(&one));
    }

    #[test]
    fn prose_around_a_multiline_call_survives() {
        let src = format!(
            "before\n{}edit path=\"f\"{}a\n---\nb{}edit{}\nafter",
            '<', '>', "</", '>'
        );
        let got = strip_syscalls(&src);
        assert!(got.contains("before"), "{got:?}");
        assert!(got.contains("after"), "{got:?}");
        assert!(!got.contains("---"), "body survived: {got:?}");
        assert!(!got.contains("path="), "attrs survived: {got:?}");
    }

    #[test]
    fn naming_a_tool_in_a_sentence_still_reads() {
        // No closer, so nothing to drop: the sentence keeps its shape.
        let src = format!("use {}python{} for the kernel", '<', '>');
        let got = strip_syscalls(&src);
        assert!(got.contains("use "), "{got:?}");
        assert!(got.contains("for the kernel"), "{got:?}");
        // And a backticked mention is untouched, code spans being sacred.
        let fenced = format!("use `{}python{}` not raw", '<', '>');
        assert!(strip_syscalls(&fenced).contains("python"), "{:?}", strip_syscalls(&fenced));
    }

    #[test]
    fn two_calls_in_one_turn_both_go() {
        let src = format!(
            "one{}bash{}ls{}bash{} two {}python{}x=1{}python{} three",
            '<', '>', "</", '>', '<', '>', "</", '>'
        );
        let got = strip_syscalls(&src);
        assert!(got.contains("one"), "{got:?}");
        assert!(got.contains("two"), "{got:?}");
        assert!(got.contains("three"), "{got:?}");
        assert!(!got.contains("ls"), "{got:?}");
        assert!(!got.contains("x=1"), "{got:?}");
    }

    #[test]
    fn stripping_keeps_markdown_structure() {
        let got = strip_syscalls("## cache\n\n**87%**\n<python>x</python>\nmore");
        assert!(got.contains("## cache"), "{got}");
        assert!(got.contains('\n'), "{got}");
        assert!(!got.contains("<python>"), "{got}");
    }

    #[test]
    fn spoken_prefix_holds_an_unclosed_tag() {
        assert_eq!(spoken_prefix("hello <python"), "hello ");
        // The body goes with the call. It is already a card in the calls pane.
        assert_eq!(spoken_prefix("hello <python>x</python>!"), "hello !");
        // Held while the closer is still in flight, not shown then retracted.
        assert_eq!(spoken_prefix("hello <bash>rm -rf /"), "hello ");
    }

    #[test]
    fn a_less_than_in_prose_does_not_stall_the_stream() {
        assert_eq!(
            spoken_prefix("loop while a < b and keep going"),
            "loop while a < b and keep going"
        );
    }

    #[test]
    fn open_fence_is_never_treated_as_markup() {
        // A `<` that cannot open a tag never stalls anything, fence or no fence.
        let live = "here:\n```python\nif a < b:\n    total = a + b\n";
        assert_eq!(spoken_prefix(live), live);
        // A tag under a fence that has not closed yet is the one case where the
        // two readings differ: if the fence closes it is code, and if it never
        // closes the kernel dispatches it. Mid-stream nobody knows which, so it
        // is held -- printing it and retracting it a frame later is what left a
        // truncated copy of the block in the story for good.
        let held = "here:\n```python\nprint('<hi>')\n";
        assert_eq!(spoken_prefix(held), "here:\n```python\nprint('");
        // The hold lasts exactly as long as the fence is open.
        let closed = "here:\n```python\nprint('<hi>')\n```\ndone";
        assert_eq!(spoken_prefix(closed), closed);
    }

    /// While the fence is open nobody can tell a stray fence from one whose
    /// closer is still in flight, so a call under it is held rather than
    /// printed and retracted -- a retraction the story cannot make, the chunk
    /// having already been appended to a live block.
    #[test]
    fn a_call_under_a_live_fence_is_held_not_printed() {
        let live = "here:\n```bash\ngit status\n\n<bash>ls</bash>\n";
        let shown = spoken_prefix(live);
        assert!(!shown.contains("<bash>"), "leaked into the story: {shown:?}");
        assert_eq!(shown, "here:\n```bash\ngit status\n\n");
    }

    /// `scan_spans` on both of these is `[]`: an opener with no closer is
    /// skipped, and `TAG_OPEN` cannot match a lone closer at all. Nothing is
    /// dispatched, so nothing may be deleted -- text eaten here is text in
    /// neither pane.
    #[test]
    fn an_inert_mention_keeps_its_markers() {
        assert_eq!(strip_syscalls("use the <bash> tool for that"), "use the <bash> tool for that");
        assert_eq!(strip_syscalls("that ends with </bash> ok"), "that ends with </bash> ok");
        // A lone closer is not a call, so it does not stall the stream either.
        assert_eq!(spoken_prefix("that ends with </bash> ok"), "that ends with </bash> ok");
    }

    /// `scan_spans('<bash>echo "</bash>"</bash>')` is one call over the whole
    /// string: the body ends at the first closer that is not quoted. Stopping
    /// at the quoted one left `"</bash>` behind as prose while the kernel ran
    /// the whole command.
    #[test]
    fn a_quoted_closer_does_not_end_the_body() {
        assert_eq!(strip_syscalls("<bash>echo \"</bash>\"</bash>"), "");
        assert_eq!(strip_syscalls("<python>print(\"</python>\")\nx = 1</python>"), "");
        // `scan_spans('<bash>ls</bash >')` -> one call: the closer regex is
        // `</name\s*>`.
        assert_eq!(strip_syscalls("<bash>ls</bash >"), "");
    }

    /// An `end="TOKEN"` body runs to its token closer and nothing else, so the
    /// story must hide all of it. Before this, the stripper looked only for a
    /// bare closer, never found one, treated the opener as unterminated prose,
    /// and painted the body and the trailing token closer into the pane --
    /// which is exactly how `</python:R1>` reached a reader's screen.
    ///
    /// `desmos/scan.py::scan_spans` on each input, run for real:
    ///     token body        -> [('python', 0, 35)]  dispatched
    ///     bare closer inside-> [('python', 0, 38)]  dispatched, body opaque
    ///     spaced closer     -> [('python', 0, 29)]  dispatched
    ///     unclosed token    -> []                   inert
    ///     bad token         -> []                   inert
    ///     closer in prose   -> []                   inert
    #[test]
    fn an_end_token_body_is_stripped_whole() {
        assert_eq!(strip_syscalls("<python end=\"X\">print(1)</python:X>"), "");
        // The point of the token: bare closers inside are ordinary text.
        assert_eq!(
            strip_syscalls("<python end=\"X\">a</python>b</python:X>"),
            ""
        );
        // Spaces where scan.py's custom closer allows them.
        assert_eq!(strip_syscalls("<python end=X>a</python : X >"), "");
        assert_eq!(strip_syscalls("<edit path=\"a.rs\" end='E1'>x</edit:E1>"), "");
        // Never closed: the kernel drops the opener, so nothing ran and the
        // text stays visible rather than vanishing from both panes.
        let unclosed = "<python end=\"X\">print(1)</python>";
        assert!(
            strip_syscalls(unclosed).contains("print(1)"),
            "{:?}",
            strip_syscalls(unclosed)
        );
        // An unusable token is dropped too, not silently given a bare closer.
        let bad = "<python end=\"a b\">print(1)</python>";
        assert!(strip_syscalls(bad).contains("print(1)"), "{:?}", strip_syscalls(bad));
        // A token closer written in prose is prose.
        assert_eq!(
            strip_syscalls("it ends with </python:X> ok"),
            "it ends with </python:X> ok"
        );
    }

    /// The quoting heuristic is for bodies that get executed. A commit message
    /// is prose, and prose has apostrophes: one in "the TUI's stripper" opened
    /// a quote that never closed, so every closer after it read as quoted, the
    /// call read as unterminated, and the whole message was painted into the
    /// story. scan.py restricts the heuristic to `_QUOTED_BODY` for exactly
    /// this reason, at the cost of three lost commits; this is that set.
    ///
    /// `desmos/scan.py::scan_spans` on each input, run for real:
    ///     commit with apostrophes -> [('commit', 0, 101)]  dispatched
    ///     todo with an apostrophe -> [('todo', 0, 30)]     dispatched
    ///     edit with apostrophes   -> [('edit', 0, 43)]     dispatched
    ///     quoted closer in bash   -> [('bash', 0, 27)]     body not truncated
    #[test]
    fn prose_bodies_are_stripped_through_an_apostrophe() {
        let msg = "the TUI's stripper looked for a bare closer, and scan.py's rule disagreed";
        let src = format!("<commit add=\"a.rs\">{msg}</commit>");
        assert_eq!(strip_syscalls(&src), "", "commit body leaked");
        assert_eq!(strip_syscalls("<todo>x 1\ndon't drop it</todo>"), "");
        assert_eq!(
            strip_syscalls("<edit path=\"a\">it's old\n---\nit's new</edit>"),
            ""
        );
        // Executed bodies keep the heuristic: a quoted closer does not end one.
        assert_eq!(strip_syscalls("<bash>echo \"</bash>\"</bash>"), "");
    }

    /// The story must strip exactly what the dispatcher ran, or a call shows
    /// up in both panes in two shapes (raw tag as prose plus its card) or in
    /// neither (an inert sample eaten as if it had run).
    ///
    /// Each expectation below is `desmos/scan.py::scan_spans` on the same
    /// input, run for real:
    ///     stray backtick   -> [('bash', 6, 21)]  dispatched
    ///     4-space indent   -> []                 inert
    ///     list + 6 spaces  -> []                 inert
    ///     list + 4 spaces  -> [('bash', 12, 27)] dispatched
    ///     closed inline    -> []                 inert
    #[test]
    fn strip_syscalls_agrees_with_the_dispatcher_on_what_ran() {
        let ran = |src: &str| !strip_syscalls(src).contains("<bash>");
        assert!(ran("a ` b <bash>ls</bash> done"), "stray backtick is not a span");
        assert!(!ran("text:\n\n    <bash>ls</bash>\n\nmore"), "indented code");
        assert!(!ran("- item\n\n      <bash>ls</bash>\n\nmore"), "indented in a list");
        assert!(ran("- item\n\n    <bash>ls</bash>\n\nmore"), "list item's own paragraph");
        assert!(!ran("use `<bash>ls</bash>` here"), "closed inline span");
    }
}
