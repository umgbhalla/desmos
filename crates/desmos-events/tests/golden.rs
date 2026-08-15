//! Every line of every committed golden fixture parses into the typed Event
//! enum; a doctored line does not. The fixtures are the Python side of the
//! Phase 5.3 conformance pair — recorded from the real loop by
//! scripts/record-golden.py, so this test fails when either side drifts.

use desmos_events::Event;
use std::path::PathBuf;

fn golden_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../golden")
}

fn parse(line: &str) -> Result<Event, serde_json::Error> {
    serde_json::from_str::<Event>(line)
}

#[test]
fn every_golden_line_parses() {
    let mut fixtures = 0;
    let mut lines = 0;
    for entry in std::fs::read_dir(golden_dir()).expect("golden/ exists") {
        let path = entry.unwrap().path();
        if path.extension().and_then(|e| e.to_str()) != Some("jsonl") {
            continue;
        }
        fixtures += 1;
        let text = std::fs::read_to_string(&path).unwrap();
        for (i, line) in text.lines().enumerate() {
            if line.trim().is_empty() {
                continue;
            }
            if let Err(err) = parse(line) {
                panic!("{}:{}: {err}\n{line}", path.display(), i + 1);
            }
            lines += 1;
        }
    }
    assert_eq!(fixtures, 11, "expected the 11 recorded scenarios");
    assert!(lines > 0, "golden fixtures were empty");
}

/// The enum can actually fail: a real fixture line with one bogus field
/// injected is rejected and the error names the field. Guards against
/// `deny_unknown_fields` being lost (serde silently ignores it on shapes it
/// does not support, e.g. anything flattened).
#[test]
fn bogus_field_on_a_real_line_is_rejected() {
    let text = std::fs::read_to_string(golden_dir().join("plain.jsonl")).unwrap();
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        assert!(parse(line).is_ok(), "fixture line must parse clean: {line}");
        let mut obj: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(line).unwrap();
        obj.insert("bogus_field".into(), serde_json::Value::Bool(true));
        let doctored = serde_json::to_string(&obj).unwrap();
        let err = parse(&doctored).expect_err(&format!("accepted bogus field: {doctored}"));
        assert!(
            err.to_string().contains("bogus_field"),
            "error did not name the field: {err}"
        );
    }
}

/// A new `ev` kind the vocabulary does not list is an error, not a shrug.
#[test]
fn unknown_ev_kind_is_rejected() {
    for line in [
        r#"{"ev": "telemetry", "text": "new kind"}"#,
        r#"{"ev": "child", "id": "abcd1234", "kind": "telemetry", "text": "nested"}"#,
        r#"{"ev": "subagent", "phase": "stopped", "id": "abcd1234"}"#, // no producer emits it
    ] {
        assert!(parse(line).is_err(), "accepted unknown kind: {line}");
    }
}

/// Phase 3 tree fields are required, not optional: every recorded `subagent`
/// and `child` line carries `parent` + `depth`, and the same line with either
/// field deleted is rejected. Fails if the producer stops stamping the
/// envelope or the enum quietly demotes the fields to defaulted options.
#[test]
fn subagent_and_child_lines_require_parent_and_depth() {
    let text = std::fs::read_to_string(golden_dir().join("spawn.jsonl")).unwrap();
    let mut checked = 0;
    for line in text.lines().filter(|l| !l.trim().is_empty()) {
        let obj: serde_json::Map<String, serde_json::Value> =
            serde_json::from_str(line).unwrap();
        if !matches!(
            obj.get("ev").and_then(|e| e.as_str()),
            Some("subagent") | Some("child")
        ) {
            continue;
        }
        assert!(parse(line).is_ok(), "fixture line must parse clean: {line}");
        for field in ["parent", "depth"] {
            let mut doctored = obj.clone();
            assert!(
                doctored.remove(field).is_some(),
                "recorded line stopped carrying {field}: {line}"
            );
            let doctored = serde_json::to_string(&doctored).unwrap();
            assert!(
                parse(&doctored).is_err(),
                "a line missing {field} was accepted: {doctored}"
            );
        }
        checked += 1;
    }
    assert!(checked > 0, "spawn.jsonl carried no subagent/child lines");
}

/// Per-phase field sets hold: a field from one phase smuggled onto another is
/// rejected even though both are legal result fields.
#[test]
fn phase_field_sets_are_disjoint() {
    // attrs/body belong to start/done, not delta (loop.py:319).
    let cross = r#"{"ev": "result", "phase": "delta", "tag": "bash", "delta": true, "text": "x", "attrs": {}}"#;
    assert!(parse(cross).is_err(), "delta phase accepted start-phase attrs");
    // delta belongs to the delta phase only.
    let cross = r#"{"ev": "result", "phase": "done", "tag": "bash", "attrs": {}, "body": "b", "text": "t", "delta": true}"#;
    assert!(parse(cross).is_err(), "done phase accepted delta flag");
    // terminal subagent fields do not belong on started.
    let cross = r#"{"ev": "subagent", "phase": "started", "id": "abcd1234", "agent": "worker", "persona": "", "task": "t", "structured": false, "model": "m", "secs": 1.0}"#;
    assert!(parse(cross).is_err(), "started phase accepted terminal secs");
}
