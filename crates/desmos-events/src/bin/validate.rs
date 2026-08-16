//! NDJSON conformance filter: parse every stdin line as a typed Event,
//! exit non-zero on the first line that does not, printing line number,
//! serde's error, and the offending line. Driven by desmos/checks/conformance.py
//! for both the stdio stream and the socket transport.
//!
//! `--log`: parse the event-log FILE / attach-replay form instead (contract
//! C2): every event stamped with `seq` + `ts` by the bridge-side writer,
//! session header lines allowed unstamped, and stamped `seq` strictly
//! increasing across the input.

use std::io::BufRead;

fn main() {
    let log_mode = std::env::args().skip(1).any(|a| a == "--log");
    let stdin = std::io::stdin();
    let mut last_seq: Option<i64> = None;
    for (i, line) in stdin.lock().lines().enumerate() {
        let line = line.expect("read stdin");
        if line.trim().is_empty() {
            continue;
        }
        if log_mode {
            match desmos_events::parse_log_line(&line) {
                Ok(desmos_events::LogLine::Session { .. }) => {}
                Ok(desmos_events::LogLine::Stamped { seq, .. }) => {
                    if last_seq.is_some_and(|prev| seq <= prev) {
                        eprintln!(
                            "line {}: seq {seq} not after {}\n{line}",
                            i + 1,
                            last_seq.unwrap()
                        );
                        std::process::exit(1);
                    }
                    last_seq = Some(seq);
                }
                Err(err) => {
                    eprintln!("line {}: {err}\n{line}", i + 1);
                    std::process::exit(1);
                }
            }
        } else if let Err(err) = serde_json::from_str::<desmos_events::Event>(&line) {
            eprintln!("line {}: {err}\n{line}", i + 1);
            std::process::exit(1);
        }
    }
}
