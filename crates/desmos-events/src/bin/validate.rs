//! NDJSON conformance filter: parse every stdin line as a typed Event,
//! exit non-zero on the first line that does not, printing line number,
//! serde's error, and the offending line. Driven by desmos/checks/conformance.py
//! today; the socket transport runs through the same bin when it lands.

use std::io::BufRead;

fn main() {
    let stdin = std::io::stdin();
    for (i, line) in stdin.lock().lines().enumerate() {
        let line = line.expect("read stdin");
        if line.trim().is_empty() {
            continue;
        }
        if let Err(err) = serde_json::from_str::<desmos_events::Event>(&line) {
            eprintln!("line {}: {err}\n{line}", i + 1);
            std::process::exit(1);
        }
    }
}
