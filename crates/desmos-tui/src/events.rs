//! Bridge events -> panes. One function per seam: `handle_event` for the
//! parent stream, `handle_child` for re-enveloped subagent events,
//! `apply_result` for syscall result phases. Moved verbatim out of main.rs;
//! the helpers here are the ones only these handlers use.

use std::time::Duration;

use serde_json::{Value, json};
use xai_grok_pager::scrollback::blocks::{
    OtherToolCallBlock, SessionEvent, SessionEventBlock, SubagentBlock, ToolCallBlock,
};
use xai_grok_pager::scrollback::{DisplayMode, RenderBlock, ScrollbackState};

use crate::{
    App, ExecStream, PostArgs, StreamCursor, call_target, format_result, looks_failed,
    result_block, set_wire_mode, syscall_operation, wire_push,
};

/// The kernel's syscall spans off a `complete` event: UTF-8 byte ranges of the
/// final speech that were dispatched (kernel/loop.py, Phase 3). None when the
/// field is absent or malformed -- the stream cursor then falls back to its
/// own grammar, same as on a turn that never completed.
pub(crate) fn kernel_spans(ev: &Value) -> Option<Vec<(usize, usize)>> {
    let arr = ev.get("spans")?.as_array()?;
    let mut out = Vec::with_capacity(arr.len());
    for pair in arr {
        let pair = pair.as_array()?;
        if pair.len() != 2 {
            return None;
        }
        out.push((pair[0].as_u64()? as usize, pair[1].as_u64()? as usize));
    }
    Some(out)
}

/// Compact one-line title for a spawn: first non-empty line, parenthesised
/// asides (usually an absolute path) dropped, first sentence only, capped so
/// the live status suffix still fits on a normal-width story pane.
pub(crate) fn task_title(task: &str) -> String {
    let first = task
        .lines()
        .find(|l| !l.trim().is_empty())
        .unwrap_or("")
        .trim();
    let mut flat = String::new();
    let mut depth = 0usize;
    for ch in first.chars() {
        match ch {
            '(' => depth += 1,
            ')' => depth = depth.saturating_sub(1),
            _ if depth == 0 => flat.push(ch),
            _ => {}
        }
    }
    let flat = flat.split_whitespace().collect::<Vec<_>>().join(" ");
    let stop = flat.find(". ").map(|i| i + 1).unwrap_or(flat.len());
    let mut title = flat[..stop].trim().trim_end_matches('.').trim().to_string();
    if title.chars().count() > TITLE_CHARS {
        title = title
            .chars()
            .take(TITLE_CHARS - 1)
            .collect::<String>()
            .trim_end()
            .to_string();
        title.push('\u{2026}');
    }
    title
}

/// Longest task title kept before eliding.
pub(crate) const TITLE_CHARS: usize = 52;

pub(crate) fn subagent_status(ev: &Value, head: Option<&str>) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(head) = head.map(str::trim).filter(|s| !s.is_empty()) {
        parts.push(head.to_string());
    }
    if let Some(progress) = ev
        .get("progress")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty() && !parts.iter().any(|part| part == s))
    {
        parts.push(progress.to_string());
    }
    parts.join(" \u{b7} ")
}

/// How a finished child is labelled: the judge's verdict when there is one,
/// otherwise the stop reason, otherwise the terminal stage.
pub(crate) fn subagent_verdict(ev: &Value) -> String {
    if let Some(accepted) = ev.get("accepted").and_then(Value::as_bool) {
        return if accepted { "accepted" } else { "rejected" }.to_string();
    }
    for key in ["stop_reason", "stage", "phase"] {
        if let Some(v) = ev
            .get(key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
        {
            return v.to_string();
        }
    }
    String::new()
}

/// The kernel's tree coordinates for one child (Phase 3): `parent` is the
/// spawning run's id (null for a root spawn), `depth` its nesting level.
fn set_tree(child: &mut crate::ChildSess, ev: &Value) {
    if let Some(p) = ev.get("parent").and_then(Value::as_str) {
        child.parent = Some(p.to_string());
    }
    if let Some(d) = ev.get("depth").and_then(Value::as_u64) {
        child.depth = d;
    }
}

/// The tree-row facts a `subagent` progress/terminal event carries: stage,
/// turn count, token usage. Only what the event actually names is written.
fn set_run_facts(child: &mut crate::ChildSess, ev: &Value) {
    if let Some(s) = ev
        .get("stage")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        child.stage = s.to_string();
    }
    if let Some(t) = ev.get("turns").and_then(Value::as_u64) {
        child.turns = t;
    }
    if let Some(u) = ev.get("usage") {
        if let Some(n) = u.get("input_tokens").and_then(Value::as_u64) {
            child.tok_in = n;
        }
        if let Some(n) = u.get("output_tokens").and_then(Value::as_u64) {
            child.tok_out = n;
        }
    }
}

pub(crate) fn handle_subagent(app: &mut App, ev: &Value) {
    let phase = ev.get("phase").and_then(Value::as_str).unwrap_or("");
    let id = ev.get("id").and_then(Value::as_str).unwrap_or("");
    if id.is_empty() {
        return;
    }
    match phase {
        "started" => {
            let task = ev.get("task").and_then(Value::as_str).unwrap_or("");
            let agent = ev.get("agent").and_then(Value::as_str).unwrap_or("general");
            let persona = ev
                .get("persona")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_string);
            let model = ev
                .get("model")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_string);
            let title = task_title(task);
            let block = SubagentBlock::started(&title, id, agent, persona, None, model, true);
            let eid = app.sess.story.push_block(RenderBlock::Subagent(block));
            app.sess.story.set_last_running(true);
            let child = app.ensure_child(id, &title);
            child.parent_entry = Some(eid);
            child.agent = agent.to_string();
            child.state = "running".into();
            set_tree(child, ev);
        }
        "progress" => {
            let stage = ev.get("stage").and_then(Value::as_str);
            let label = subagent_status(ev, stage);
            // Late attach still learns the tree row: every phase carries the
            // coordinates, and progress carries stage/turns/usage.
            let child = app.ensure_child(id, "");
            set_tree(child, ev);
            set_run_facts(child, ev);
            let eid = app.children.get(id).and_then(|c| c.parent_entry);
            if let Some(eid) = eid {
                if let Some(entry) = app.sess.story.get_by_id_mut(eid) {
                    if let RenderBlock::Subagent(ref mut sb) = entry.block {
                        if !label.is_empty() {
                            sb.activity_label = Some(label);
                            entry.invalidate_cache();
                        }
                    }
                }
            }
        }
        // Parent cancellation and runtime failure are terminal too; no terminal
        // child may leave a spinner behind on the parent story.
        "done" | "failed" | "stopped" => {
            // The terminal event is the run's last word and the confirmation
            // for any intervention this TUI sent: state, verdict, final
            // stage/turns/usage land on the tree row, the sent-marker drops.
            let child = app.ensure_child(id, "");
            set_tree(child, ev);
            set_run_facts(child, ev);
            child.state = phase.to_string();
            child.accepted = ev.get("accepted").and_then(Value::as_bool);
            child.op_sent = None;
            // A fresh terminal transition is unseen even if this worker was
            // inspected while it was still running.
            app.rail_seen.remove(id);
            let secs = ev.get("secs").and_then(Value::as_f64).unwrap_or(0.0);
            let elapsed = Duration::from_secs_f64(secs.max(0.0));
            let err = ev
                .get("error")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_string);
            let eid = app.children.get(id).and_then(|c| c.parent_entry);
            if let Some(eid) = eid {
                app.sess.story.finish_running(eid);
                // The spawn row keeps rendering its activity label after it
                // stops running, so retire it with the verdict and what the
                // child actually spent rather than a stale mid-flight turn.
                let verdict = subagent_verdict(ev);
                let label = subagent_status(ev, Some(&verdict));
                if let Some(entry) = app.sess.story.get_by_id_mut(eid) {
                    if let RenderBlock::Subagent(ref mut sb) = entry.block {
                        if !label.is_empty() {
                            sb.activity_label = Some(label);
                            entry.invalidate_cache();
                        }
                    }
                }
            }
            let desc = eid
                .and_then(|eid| app.sess.story.get_by_id(eid))
                .and_then(|e| match &e.block {
                    RenderBlock::Subagent(sb) => Some(sb.description.clone()),
                    _ => None,
                })
                .unwrap_or_default();
            let terminal = if phase == "done" && err.is_none() {
                RenderBlock::Subagent(SubagentBlock::completed(&desc, id, elapsed))
            } else {
                RenderBlock::Subagent(SubagentBlock::failed(&desc, id, elapsed, err.clone()))
            };
            app.sess.story.push_block(terminal);
            // Child speech already landed via `child` events. Only surface a
            // failure that never produced speech.
            if let Some(err) = err {
                app.ensure_child(id, "")
                    .sess
                    .story
                    .push_block(RenderBlock::system(err));
            }
        }
        _ => {}
    }
}

pub(crate) fn handle_child(app: &mut App, ev: &Value) {
    let id = ev.get("id").and_then(Value::as_str).unwrap_or("");
    if id.is_empty() {
        return;
    }
    let kind = ev.get("kind").and_then(Value::as_str).unwrap_or("");
    // Every child envelope carries the tree fields, so a session attached
    // after the `started` event still learns where this child hangs.
    set_tree(app.ensure_child(id, ""), ev);
    // A subagent's tokens are billed to the same key, so they belong in the
    // money row — they were spent and shown nowhere. Totals only: the context
    // and TTL bars describe the parent's transcript, not this child's.
    if kind == "complete" {
        let usage = ev.get("usage").cloned().unwrap_or(json!({}));
        let model = ev.get("model").and_then(Value::as_str).unwrap_or("?");
        app.cache.bill(&usage, model);
    }
    let mut last_post: Option<(u64, Value, Value)> = None;
    let shown = app.show_posts;
    let child = app.children.get_mut(id).expect("child");
    match kind {
        "thinking" => {
            let redacted = ev.get("redacted").and_then(Value::as_bool).unwrap_or(false);
            let delta = ev.get("delta").and_then(Value::as_bool).unwrap_or(false);
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            apply_thinking(
                &mut child.sess.story,
                &mut child.sess.calls,
                &mut child.sess.stream,
                redacted,
                text,
                delta,
            );
        }
        "speech" => {
            let delta = ev.get("delta").and_then(Value::as_bool).unwrap_or(false);
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            apply_speech(
                &mut child.sess.story,
                &mut child.sess.calls,
                &mut child.sess.stream,
                text,
                delta,
            );
        }
        "post" => {
            let n = ev.get("n").and_then(Value::as_u64).unwrap_or(0);
            if let Some(req) = ev.get("request") {
                last_post = Some((n, req.clone(), json!({})));
            }
        }
        "complete" => {
            match kernel_spans(ev) {
                Some(spans) => child.sess.stream.finish_reconciled(
                    &mut child.sess.story,
                    &mut child.sess.calls,
                    &spans,
                ),
                None => child
                    .sess
                    .stream
                    .finish(&mut child.sess.story, &mut child.sess.calls),
            }
            finish_exec(&mut child.sess.calls, &mut child.sess.exec);
            let n = ev.get("n").and_then(Value::as_u64).unwrap_or(0);
            let origin = ev.get("origin").and_then(Value::as_str).unwrap_or("llm");
            let model = ev.get("model").and_then(Value::as_str).unwrap_or("?");
            let thinking = ev.get("thinking").and_then(Value::as_str).unwrap_or("");
            let usage = ev.get("usage").cloned().unwrap_or(json!({}));
            let thoughts = ev.get("thoughts").and_then(Value::as_u64).unwrap_or(0);
            let redacted = ev.get("redacted").and_then(Value::as_u64).unwrap_or(0);
            child.sess.posts.push(
                &mut child.sess.calls,
                PostArgs::new(origin, n, model, thinking, &usage, thoughts, redacted),
                shown,
            );
            if let (Some(req), Some(resp)) = (ev.get("request"), ev.get("response")) {
                last_post = Some((n, req.clone(), resp.clone()));
            }
        }
        "result" => {
            child
                .sess
                .stream
                .finish(&mut child.sess.story, &mut child.sess.calls);
            apply_result(&mut child.sess.calls, &mut child.sess.exec, ev);
        }
        "turn" => {
            child
                .sess
                .stream
                .finish(&mut child.sess.story, &mut child.sess.calls);
        }
        _ => {}
    }
    // The POST split is the parent's wire. A child's request/response only
    // belongs there while the human is actually inside that child session;
    // otherwise a background subagent silently overwrites the parent's meters
    // and JSON panes with someone else's model and usage.
    if let Some((n, req, resp)) = last_post {
        if app.viewing.as_deref() == Some(id) {
            app.set_last_post(n, &req, &resp);
        }
    }
}

fn decision_from_value(value: &Value) -> Option<crate::Decision> {
    let status = match value.get("status").and_then(Value::as_str)? {
        "open" => crate::DecisionStatus::Open,
        "answered" => crate::DecisionStatus::Answered,
        _ => return None,
    };
    Some(crate::Decision {
        id: value.get("id")?.as_str()?.to_string(),
        prompt: value.get("prompt")?.as_str()?.to_string(),
        options: value
            .get("options")?
            .as_array()?
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect(),
        status,
    })
}

fn apply_decision(app: &mut App, value: &Value) {
    let Some(id) = value.get("id").and_then(Value::as_str) else {
        return;
    };
    // Any non-open update closes this id. That includes answered and
    // forward-compatible statuses this client does not yet understand.
    app.decisions.retain(|item| item.id != id);
    if let Some(decision) = decision_from_value(value)
        && decision.status == crate::DecisionStatus::Open
    {
        app.decisions.push(decision);
    }
}

pub(crate) fn handle_event(app: &mut App, ev: Value) {
    let kind = ev.get("ev").and_then(Value::as_str).unwrap_or("");
    // The work row's repo tail, taken here because this is where the git pane
    // is reachable. sync() is called from inside the stream cursor and used to
    // fork git itself, on this thread, once per result and per thought.
    app.sess.stream.run.note_repo(&app.git);
    match kind {
        "decision" => apply_decision(app, &ev),
        "picker" => app.picker.observe(&ev),
        "login" => {
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            let done = ev.get("done").and_then(Value::as_bool).unwrap_or(false)
                || ev.get("failed").and_then(Value::as_bool).unwrap_or(false);
            app.picker.login_line(text, done);
        }
        "ready" | "snapshot" => {
            app.picker.observe(&ev);
            app.decisions.clear();
            if let Some(decisions) = ev.get("decisions").and_then(Value::as_array) {
                for decision in decisions {
                    apply_decision(app, decision);
                }
            }
            if let Some(b) = ev.get("billing").and_then(Value::as_str) {
                app.cache.plan = b == "plan";
            }
            if let Some(p) = ev.get("provider").and_then(Value::as_str) {
                app.cache.ephemeral = p == "anthropic";
            }
            if let Some(s) = ev.get("model").and_then(Value::as_str) {
                app.model = s.into();
                // The bridge is the authority. Once it reports the model we
                // queued, the pending badge has nothing left to announce.
                if app.model_pending.as_ref().is_some_and(|(m, _)| m == s) {
                    app.model_pending = None;
                }
            }
            if let Some(s) = ev.get("thinking").and_then(Value::as_str) {
                app.thinking = s.into();
            }
            if let Some(n) = ev.get("generation").and_then(Value::as_u64) {
                app.generation = n.to_string();
            } else if let Some(s) = ev.get("generation").and_then(Value::as_str) {
                app.generation = s.into();
            }
            app.ready = true;
            if !app.running {
                app.status = "idle".into();
            }
        }
        "subagent" => handle_subagent(app, &ev),
        "child" => handle_child(app, &ev),
        "thinking" => {
            let redacted = ev.get("redacted").and_then(Value::as_bool).unwrap_or(false);
            let delta = ev.get("delta").and_then(Value::as_bool).unwrap_or(false);
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            apply_thinking(
                &mut app.sess.story,
                &mut app.sess.calls,
                &mut app.sess.stream,
                redacted,
                text,
                delta,
            );
        }
        "speech" => {
            let delta = ev.get("delta").and_then(Value::as_bool).unwrap_or(false);
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            apply_speech(
                &mut app.sess.story,
                &mut app.sess.calls,
                &mut app.sess.stream,
                text,
                delta,
            );
        }
        "result" => {
            app.sess
                .stream
                .finish(&mut app.sess.story, &mut app.sess.calls);
            let phase = ev.get("phase").and_then(Value::as_str).unwrap_or("done");
            if phase != "start" && phase != "delta" {
                let tag = ev.get("tag").and_then(Value::as_str).unwrap_or("?");
                // The commit claim is the kernel's (result.repo.committed, set
                // only when the command's own output named the sha). The row
                // never again attributes a commit from a HEAD-snapshot diff.
                if let Some(sha) = ev
                    .get("repo")
                    .and_then(|r| r.get("committed"))
                    .and_then(Value::as_str)
                {
                    app.sess.stream.run.commit(sha);
                }
                // Every edit detail has one home: Activity. Do not duplicate
                // either its diff card or an `edit xN` work row in Story.
                if tag != "edit" {
                    let target = call_target(tag, &ev);
                    app.sess.stream.run.call(tag, target);
                    app.sess.stream.run.sync(&mut app.sess.story);
                }
                // A syscall just ran against this checkout. Start the read now
                // rather than waiting out the pane's timer: the run folds as
                // soon as prose starts, and the tail it prints is whatever has
                // landed by then. One read at a time however fast the calls
                // arrive — `poll` answers with the generation that will see
                // this call's work, and `settle` holds the row for it.
                app.sess.stream.run.fresh_gen = app.git.poll(true);
            }
            apply_result(&mut app.sess.calls, &mut app.sess.exec, &ev);
        }
        "post" => {
            let n = ev.get("n").and_then(Value::as_u64).unwrap_or(0);
            let empty = json!({});
            let req = ev.get("request").unwrap_or(&empty);
            // The body about to go over the wire is the only unarguable answer
            // to "which model is this". A switch applied mid-step (or from the
            // kernel, which never sends a snapshot) used to leave the composer
            // naming the old model until the next user turn.
            if let Some(m) = req.get("model").and_then(Value::as_str) {
                if !m.is_empty() && app.model != m {
                    app.model = m.into();
                }
                if app.model_pending.as_ref().is_some_and(|(p, _)| p == m) {
                    app.model_pending = None;
                }
            }
            app.set_last_post(n, req, &empty);
        }
        // The wire pane exists so the human sees what the harness did. A fold
        // rewrites the transcript the model reads, so it belongs here and not
        // in the story — it is not something the model said.
        "compacted" => {
            let n = ev.get("n").and_then(Value::as_u64).unwrap_or(0);
            let kept = ev.get("kept").and_then(Value::as_u64).unwrap_or(0);
            let summary = ev.get("text").and_then(Value::as_str).unwrap_or("");
            app.call_push(wire_compacted(n, kept, summary));
            // The card carries the summary, but a fold is not a detail: the
            // model's memory of this session just changed shape. Say so where
            // the human is actually reading, and say what it means.
            app.story_push(RenderBlock::system(&fold_notice(n, kept)));
            app.notify("context folded");
        }
        "complete" => {
            // The event that closes a turn's speech. Its `spans` are the
            // kernel's dispatch verdict; reconcile the story against them
            // instead of re-deriving with the local grammar.
            match kernel_spans(&ev) {
                Some(spans) => app.sess.stream.finish_reconciled(
                    &mut app.sess.story,
                    &mut app.sess.calls,
                    &spans,
                ),
                None => app
                    .sess
                    .stream
                    .finish(&mut app.sess.story, &mut app.sess.calls),
            }
            finish_exec(&mut app.sess.calls, &mut app.sess.exec);
            let n = ev.get("n").and_then(Value::as_u64).unwrap_or(0);
            let origin = ev.get("origin").and_then(Value::as_str).unwrap_or("llm");
            let model = ev.get("model").and_then(Value::as_str).unwrap_or("?");
            let thinking = ev.get("thinking").and_then(Value::as_str).unwrap_or("");
            let usage = ev.get("usage").cloned().unwrap_or(json!({}));
            app.cache.observe(&usage, model);
            if let Some(req) = ev.get("request") {
                app.cache.observe_roles(req);
            }
            let thoughts = ev.get("thoughts").and_then(Value::as_u64).unwrap_or(0);
            let redacted = ev.get("redacted").and_then(Value::as_u64).unwrap_or(0);
            app.call_push_group(PostArgs::new(
                origin, n, model, thinking, &usage, thoughts, redacted,
            ));
            let empty = json!({});
            let req = ev.get("request").unwrap_or(&empty);
            let resp = ev.get("response").unwrap_or(&empty);
            if req != &empty || resp != &empty {
                app.set_last_post(n, req, resp);
            }
        }
        "turn" => {
            app.status = "running".into();
            app.sess
                .stream
                .finish(&mut app.sess.story, &mut app.sess.calls);
        }
        // Outstanding background work, re-sent whenever the set changes. Meta
        // is the only reader: this is state, not an event worth a card.
        "pending" => {
            app.background = ev
                .get("tasks")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(Value::as_str)
                        .map(str::to_owned)
                        .collect()
                })
                .unwrap_or_default();
        }
        "done" => {
            app.sess
                .stream
                .finish(&mut app.sess.story, &mut app.sess.calls);
            app.sess.stream.run.fold(&mut app.sess.story);
            finish_exec(&mut app.sess.calls, &mut app.sess.exec);
            app.running = false;
            app.turn_started = None;
            app.status = "idle".into();
            app.drain_after = !app.queue.is_empty();
        }
        "stopped" => {
            app.sess
                .stream
                .finish(&mut app.sess.story, &mut app.sess.calls);
            finish_exec(&mut app.sess.calls, &mut app.sess.exec);
            let t = ev
                .get("text")
                .and_then(Value::as_str)
                .unwrap_or("stopped, saved");
            app.story_push(RenderBlock::system(t));
            app.running = false;
            app.turn_started = None;
            app.status = "idle".into();
            // A stop ends only the active step. Follow-ups already queued are
            // still explicit user requests and must self-feed exactly as they
            // do after a normal `done`; deleting a queue row is how to cancel it.
            app.drain_after = !app.queue.is_empty();
        }
        // The harness explaining itself. Not speech (that is the model) and not
        // an error, so it must not touch running state.
        "notice" => {
            let t = ev.get("text").and_then(Value::as_str).unwrap_or("");
            if !t.is_empty() {
                app.story_push(RenderBlock::system(t));
            }
        }
        // Ordinary shared-channel activity is transient chrome. Directed peer
        // exchanges are different: no local composer submission exists, so
        // without a durable Story row the model receives a message the human
        // never sees.
        "channel" => {
            let channel = ev
                .get("channel")
                .and_then(Value::as_str)
                .unwrap_or("conflicts");
            let author = ev.get("author").and_then(Value::as_str).unwrap_or("peer");
            let preview = ev.get("preview").and_then(Value::as_str).unwrap_or("");
            let unread = ev.get("unread").and_then(Value::as_u64).unwrap_or(1);
            if let Some(kind) = ev.get("directed").and_then(Value::as_str) {
                let body = ev.get("body").and_then(Value::as_str).unwrap_or(preview);
                app.story_push(RenderBlock::SessionEvent(SessionEventBlock::new(
                    SessionEvent::PeerMessage {
                        author: author.to_string(),
                        body: body.to_string(),
                        reply: kind == "reply",
                    },
                )));
            } else {
                app.notify(format!(
                    "IRC #{channel} · {author}: {preview} · {unread} unread"
                ));
            }
        }
        // Not a terminator. loop.py fires this for a reply the endpoint cut
        // short and keeps looping, and the reader thread synthesises one for
        // any unparseable NDJSON line. Clearing running here read as idle while
        // run_turns was still going, so Enter sent a second op:step that fired
        // later, out of order. Only done/stopped end a step.
        "error" => {
            app.sess
                .stream
                .finish(&mut app.sess.story, &mut app.sess.calls);
            finish_exec(&mut app.sess.calls, &mut app.sess.exec);
            let t = ev.get("text").and_then(Value::as_str).unwrap_or("error");
            app.story_push(RenderBlock::system(t));
        }
        _ => {}
    }
}

pub(crate) fn start_thinking(story: &mut ScrollbackState, stream: &mut StreamCursor) {
    if stream.think.is_some() {
        return;
    }
    let id = story.push_block(RenderBlock::thinking_streaming());
    story.set_last_running(true);
    // Grok's truncated mode marks the clipped head with a bare "…" row. That
    // marker only reads as a marker under the header, and the header is the
    // status row's job -- so a live thought renders its whole body (grok
    // minimal does the same in its live tail) and the pane's bottom edge does
    // the clipping. finish_think folds it back to one collapsed row.
    set_wire_mode(story, id, DisplayMode::Expanded);
    stream.think = Some(id);
}

pub(crate) fn finish_exec(calls: &mut ScrollbackState, exec: &mut ExecStream) {
    exec.flush(calls);
    if let Some(id) = exec.id.take() {
        calls.finish_running(id);
        // Do not fold here. reflow_wire owns fold state and keeps the tail
        // open, so collapsing on finish only produces a one-frame flash
        // before it is reopened.
    }
    exec.pending.clear();
    exec.tag.clear();
}

pub(crate) fn apply_result(calls: &mut ScrollbackState, exec: &mut ExecStream, ev: &Value) {
    let phase = ev.get("phase").and_then(Value::as_str).unwrap_or("done");
    match phase {
        "start" => {
            finish_exec(calls, exec);
            let id = wire_push(calls, result_block(ev));
            // Open while it streams so stdout is visible as it arrives.
            set_wire_mode(calls, id, DisplayMode::Expanded);
            calls.set_last_running(true);
            exec.id = Some(id);
            let tag = ev.get("tag").and_then(Value::as_str).unwrap_or("");
            let empty = Value::Null;
            let attrs = ev.get("attrs").unwrap_or(&empty);
            exec.tag = syscall_operation(tag, attrs).to_string();
        }
        "delta" => {
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            if !text.is_empty() {
                exec.pending.push_str(text);
            }
        }
        _ => {
            exec.flush(calls);
            let text = ev.get("text").and_then(Value::as_str).unwrap_or("");
            let tag = ev.get("tag").and_then(Value::as_str).unwrap_or("?");
            let empty = Value::Null;
            let attrs = ev.get("attrs").unwrap_or(&empty);
            let operation = syscall_operation(tag, attrs);
            if let Some(id) = exec.id.take() {
                if let Some(entry) = calls.get_by_id_mut(id) {
                    match &mut entry.block {
                        RenderBlock::ToolCall(ToolCallBlock::Execute(block)) => {
                            if !text.is_empty() {
                                let formatted = format_result(text);
                                if formatted != text
                                    || block.output.as_ref().is_none_or(|s| s.is_empty())
                            {
                                    block.output = Some(formatted);
                            }
                            }
                            if looks_failed(operation, text) {
                                block.set_error(Some(
                                    text.lines().next().unwrap_or("failed").to_string(),
                                ));
                            }
                            block.finish();
                        }
                        // Non-execute cards have no shared streaming output
                        // slot. Rebuild from the done event so Activity shows
                        // the final diff, media, or structured result.
                        other => *other = result_block(ev),
                    }
                }
                calls.finish_running(id);
                // Fold state is reflow_wire's job; a finished call that is
                // still recent stays readable instead of blinking shut.
                calls.mark_height_dirty(id);
            } else {
                wire_push(calls, result_block(ev));
            }
        }
    }
}

pub(crate) fn apply_thinking(
    story: &mut ScrollbackState,
    activity: &mut ScrollbackState,
    stream: &mut StreamCursor,
    redacted: bool,
    text: &str,
    delta: bool,
) {
    if redacted {
        stream.finish_speech(story);
        stream.finish_think(activity);
        activity.push_block(RenderBlock::thinking(
            "redacted thinking — opaque block, replayed on the next complete(), not speech.",
        ));
        return;
    }
    if delta {
        stream.finish_speech(story);
        if text.is_empty() {
            return;
        }
        start_thinking(activity, stream);
        stream.pending_think.push_str(text);
        return;
    }
    stream.finish_speech(story);
    stream.finish_think(activity);
    if !text.trim().is_empty() {
        activity.push_block(RenderBlock::thinking(text));
    }
}

pub(crate) fn apply_speech(
    _story: &mut ScrollbackState,
    activity: &mut ScrollbackState,
    stream: &mut StreamCursor,
    text: &str,
    _delta: bool,
) {
    // Delta or whole, speech buffers until its turn closes: the unstreamed
    // replay (kernel/loop.py fires one whole `speech` when nothing streamed)
    // is followed by the same `complete` event as the delta path, and that
    // event's kernel spans -- not the local grammar -- decide what of this
    // text is prose. Finalizing here would decide one message early.
    stream.finish_think(activity);
    stream.speech_raw.push_str(text);
}

/// Plain-language story row for a fold. The wire card is evidence; this is the
/// explanation — what happened, what the model now reads, what did not change.
pub(crate) fn fold_notice(n: u64, kept: u64) -> String {
    let scope = if kept > 0 {
        format!("the {kept} most recent messages were kept verbatim")
    } else {
        "only the summary was kept".to_string()
    };
    format!(
        "context folded at POST #{n} — the provider replaced the earlier turns with a summary; \
         {scope}. Nothing above was deleted from this pane; the model just reads the summary \
         instead of the originals from here on."
    )
}

/// Wire card for a server-side fold. The model's memory just got rewritten,
/// which is the largest thing the harness does to itself in a run — without a
/// card the only symptom is the context bar dropping for no stated reason.
pub(crate) fn wire_compacted(n: u64, kept: u64, summary: &str) -> RenderBlock {
    let head = if kept > 0 {
        format!("FOLD:  POST #{n}  {kept} kept")
    } else {
        format!("FOLD:  POST #{n}")
    };
    let body = if summary.trim().is_empty() {
        "earlier turns folded by the server".to_string()
    } else {
        summary.to_string()
    };
    RenderBlock::ToolCall(ToolCallBlock::Other(
        OtherToolCallBlock::new(head, "context compacted".to_string()).with_output(body),
    ))
}
