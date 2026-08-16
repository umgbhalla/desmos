## What this changes

<!-- One or two sentences. The diff says what; say why. -->

## Verification

<!-- Paste the command and its result. "Should work" is not verification. -->

- [ ] `python -m desmos check`
- [ ] `python -m unittest discover -s tests -q`
- [ ] `cargo test -p desmos-tui` (TUI changes only)
- [ ] `cargo test -p xai-grok-markdown -p xai-grok-markdown-core` (markdown crates only)

## Checklist

- [ ] No new runtime dependency in `desmos/` — the harness stays stdlib-only.
- [ ] `CANONICAL` operation meanings are unchanged; any new compatibility alias remains hidden from advertisement.
- [ ] No key material, transcript, or `.desmos/` state in the diff.
- [ ] `vendor/` untouched, or the change is called out explicitly.
- [ ] Docs updated when behaviour changed: `README.md`, `docs/`, `AGENTS.md`.
