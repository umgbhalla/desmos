//! The run tree: one row per subagent run, nested by the kernel's own
//! `parent`/`depth` coordinates (Phase 3 fields on every `subagent`/`child`
//! event). Painted from events alone — no filesystem, no forks. Toggled onto
//! the Activity column with `t`; Enter opens the child session that already
//! exists for the row, `x`/`r` send the Track 3.3 intervention ops.

use std::collections::HashMap;

use serde_json::{Value, json};

use crate::app::{App, ChildSess};

/// Depth-first forest order over the children map: roots (no parent, or a
/// parent this session never saw) by spawn order, each followed by its own
/// subtree. Every id appears exactly once — a child hangs under at most one
/// parent — so the walk terminates without a visited set.
pub(crate) fn order(children: &HashMap<String, ChildSess>) -> Vec<String> {
    let mut kids: HashMap<Option<&str>, Vec<(&str, u64)>> = HashMap::new();
    for (id, c) in children {
        let p = c
            .parent
            .as_deref()
            .filter(|p| children.contains_key(*p) && *p != id.as_str());
        kids.entry(p).or_default().push((id, c.seq));
    }
    for v in kids.values_mut() {
        v.sort_by_key(|(_, seq)| *seq);
    }
    let mut out = Vec::with_capacity(children.len());
    let mut stack: Vec<&str> = kids
        .get(&None)
        .map(|v| v.iter().rev().map(|(id, _)| *id).collect())
        .unwrap_or_default();
    while let Some(id) = stack.pop() {
        out.push(id.to_string());
        if let Some(v) = kids.get(&Some(id)) {
            stack.extend(v.iter().rev().map(|(id, _)| *id));
        }
    }
    out
}

/// State glyph: what the wire has actually said about this run. `·` is a run
/// known only from re-enveloped child events (late attach, no `started` yet).
pub(crate) fn glyph(state: &str) -> &'static str {
    match state {
        "running" => "●",
        "done" => "✓",
        "failed" => "✗",
        "stopped" => "■",
        _ => "·",
    }
}

fn fmt_tok(n: u64) -> String {
    if n >= 10_000 {
        format!("{}k", n / 1_000)
    } else {
        n.to_string()
    }
}

/// One row: depth-indented glyph, agent, stage, turns, usage, verdict, and
/// the unconfirmed-intervention marker. Everything here arrived on the wire;
/// a field the wire never sent is simply absent from the row.
pub(crate) fn row_text(c: &ChildSess) -> String {
    let mut parts: Vec<String> = vec![format!(
        "{} {}",
        glyph(&c.state),
        if c.agent.is_empty() { "?" } else { &c.agent }
    )];
    if !c.stage.is_empty() {
        parts.push(c.stage.clone());
    }
    if c.turns > 0 {
        parts.push(format!("t{}", c.turns));
    }
    if c.tok_in + c.tok_out > 0 {
        parts.push(format!("{}↓ {}↑ tok", fmt_tok(c.tok_in), fmt_tok(c.tok_out)));
    }
    match c.accepted {
        Some(true) => parts.push("accepted".into()),
        Some(false) => parts.push("rejected".into()),
        None if matches!(c.state.as_str(), "done" | "failed" | "stopped") => {
            parts.push("unjudged".into())
        }
        None => {}
    }
    if let Some(op) = c.op_sent {
        parts.push(format!("{op} (unconfirmed)"));
    }
    format!("{}{}", "  ".repeat(c.depth as usize), parts.join(" · "))
}

/// Contract C3, exactly: `{"op":"kill_run","id":"<run id>"}`.
pub(crate) fn kill_op(id: &str) -> Value {
    json!({"op": "kill_run", "id": id})
}

/// Contract C3, exactly: `{"op":"rerun","id":"<run id>"}`.
pub(crate) fn rerun_op(id: &str) -> Value {
    json!({"op": "rerun", "id": id})
}

/// The op for the selected tree row, marking the row as sent-but-unconfirmed.
/// The caller puts it on the wire; confirmation is not this function's claim —
/// it is the terminal `subagent` event (kill) or a new `started` (rerun), and
/// the marker stays on the row until that event actually arrives.
pub(crate) fn intervene(app: &mut App, kill: bool) -> Option<Value> {
    let ids = order(&app.children);
    let id = ids.get(app.tree_sel)?.clone();
    let c = app.children.get_mut(&id)?;
    c.op_sent = Some(if kill { "kill sent" } else { "rerun sent" });
    Some(if kill { kill_op(&id) } else { rerun_op(&id) })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::events::handle_event;
    use serde_json::json;

    /// The op JSON is the contract, byte-for-byte field-for-field: lane C's
    /// bridge will match on exactly this shape.
    #[test]
    fn intervention_ops_are_exactly_the_contract_shape() {
        assert_eq!(
            kill_op("ab12cd34"),
            serde_json::from_str::<Value>(r#"{"op":"kill_run","id":"ab12cd34"}"#).unwrap()
        );
        assert_eq!(
            rerun_op("ab12cd34"),
            serde_json::from_str::<Value>(r#"{"op":"rerun","id":"ab12cd34"}"#).unwrap()
        );
    }

    /// A depth-2 tree fed purely from events (golden spawn shape, hand-nested)
    /// paints nested, ordered, and with the wire's own numbers — no fs, no
    /// forks, nothing the events did not carry.
    #[test]
    fn a_depth_two_tree_paints_nested_rows_from_events_alone() {
        let _guard = crate::theme_lock();
        let mut app = App::new();
        app.ready = true;
        for ev in [
            json!({"ev":"subagent","phase":"started","id":"aaaa0001","parent":null,"depth":0,
                   "agent":"explore","persona":"researcher","task":"map the repo","model":"m"}),
            json!({"ev":"subagent","phase":"started","id":"bbbb0002","parent":"aaaa0001","depth":1,
                   "agent":"general","task":"read the loop","model":"m"}),
            json!({"ev":"subagent","phase":"started","id":"cccc0003","parent":"bbbb0002","depth":2,
                   "agent":"general","task":"grep one file","model":"m"}),
            json!({"ev":"subagent","phase":"started","id":"dddd0004","parent":null,"depth":0,
                   "agent":"review","task":"judge the diff","model":"m"}),
            json!({"ev":"subagent","phase":"progress","id":"bbbb0002","parent":"aaaa0001","depth":1,
                   "task":"read the loop","stage":"executing","progress":"model turn 2","turns":2,
                   "usage":{"input_tokens":11,"output_tokens":7}}),
            json!({"ev":"subagent","phase":"done","id":"cccc0003","parent":"bbbb0002","depth":2,
                   "task":"grep one file","stage":"completed","progress":"child finished",
                   "stop_reason":"completed","accepted":true,"secs":0.5,"turns":1,
                   "usage":{"input_tokens":3,"output_tokens":2},"result":"found it","error":null}),
        ] {
            handle_event(&mut app, ev);
        }

        // Forest order: each root in spawn order, subtree before the next root.
        assert_eq!(
            order(&app.children),
            vec!["aaaa0001", "bbbb0002", "cccc0003", "dddd0004"]
        );

        app.tree_open = true;
        app.set_focus(crate::Focus::Calls);
        // Wide enough that the wire column (38% of the frame) holds a whole
        // depth-2 row without truncation.
        let text = crate::tests::paint(&mut app, 170, 40);
        let col = |needle: &str| -> (usize, usize) {
            for (row, line) in text.lines().enumerate() {
                if let Some(at) = line.find(needle) {
                    return (row, line[..at].chars().count());
                }
            }
            panic!("{needle:?} not painted:\n{text}");
        };
        let (r_a, x_a) = col("● explore");
        let (r_b, x_b) = col("● general · executing · t2 · 11↓ 7↑ tok");
        let (r_c, x_c) = col("✓ general · completed · t1 · 3↓ 2↑ tok · accepted");
        // `started` is the wire saying the run is live, so its glyph is ●.
        let (r_d, x_d) = col("● review");
        assert!(r_a < r_b && r_b < r_c && r_c < r_d, "rows out of order:\n{text}");
        assert_eq!(x_b, x_a + 2, "depth 1 indents one step:\n{text}");
        assert_eq!(x_c, x_a + 4, "depth 2 indents two steps:\n{text}");
        assert_eq!(x_d, x_a, "a second root sits back on the margin:\n{text}");
    }

    /// `x` marks the row sent-but-unconfirmed; the confirmation is the terminal
    /// subagent event and nothing else clears the marker.
    #[test]
    fn a_kill_is_unconfirmed_until_the_terminal_event() {
        let _guard = crate::theme_lock();
        let mut app = App::new();
        handle_event(
            &mut app,
            json!({"ev":"subagent","phase":"started","id":"aaaa0001","parent":null,"depth":0,
                   "agent":"general","task":"spin","model":"m"}),
        );
        app.tree_sel = 0;
        let op = intervene(&mut app, true).expect("a row is selected");
        assert_eq!(
            op,
            serde_json::from_str::<Value>(r#"{"op":"kill_run","id":"aaaa0001"}"#).unwrap()
        );
        let row = row_text(&app.children["aaaa0001"]);
        assert!(row.contains("kill sent (unconfirmed)"), "{row}");

        // The kernel's terminal event is the confirmation.
        handle_event(
            &mut app,
            json!({"ev":"subagent","phase":"stopped","id":"aaaa0001","parent":null,"depth":0,
                   "task":"spin","stage":"stopped","stop_reason":"killed","accepted":null,
                   "secs":1.0,"turns":1,"usage":{},"error":null}),
        );
        let row = row_text(&app.children["aaaa0001"]);
        assert!(!row.contains("unconfirmed"), "{row}");
        assert!(row.starts_with("■"), "terminal glyph: {row}");
        assert!(row.contains("unjudged"), "{row}");
    }
}
