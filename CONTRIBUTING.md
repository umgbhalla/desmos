# Contributing

Thanks for looking. desmos is a small harness with an unusual constraint: the
agent it runs can rewrite parts of itself at runtime, so the code that ships in
this repo is only the frozen half. Read [docs/design.md](docs/design.md) before
changing anything under `desmos/`.

## Ground rules

- **The harness is stdlib-only.** `pyproject.toml` has `dependencies = []` and it
  stays that way. IPython lives behind the optional `kernel` extra; anything else
  belongs in a skill or an extension, not in `desmos/`.
- **The frozen tag set is an ABI.** `FROZEN` in `desmos/const.py` is what every
  prompt promises. Adding, renaming or repurposing one of those tags breaks every
  persisted generation on every machine. New capability goes in as a grown tool,
  a skill, or an extension.
- **Never commit key material.** CI greps the whole tree, `vendor/` included, for
  key-shaped strings. `.env.example` shows the shape; real keys live in the
  environment or `~/.desmos/auth.json`.
- **Do not build the workspace.** Every vendored grok crate is a workspace
  member, so `cargo build --workspace` compiles ~89 packages. Always target a
  package: `-p desmos-tui`, `-p xai-grok-markdown`.

## Setup

```bash
uv venv && uv pip install -e ".[kernel]"
source .venv/bin/activate
python -m desmos check                     # harness self-check, no API key needed
python -m unittest discover -s tests -q    # unit tests
```

The TUI additionally needs Rust (pinned by `rust-toolchain.toml`) and a protobuf
compiler:

```bash
brew install protobuf          # or: apt-get install protobuf-compiler
cargo test -p desmos-tui
python -m desmos tui --demo    # offline, no API key
```

## Before you open a pull request

Run what CI runs:

| change                | command                                              |
|-----------------------|------------------------------------------------------|
| anything in `desmos/` | `python -m desmos check` + `python -m unittest discover -s tests -q` |
| TUI (`crates/desmos-tui`) | `cargo test -p desmos-tui`                       |
| markdown crates       | `cargo test -p xai-grok-markdown -p xai-grok-markdown-core` |
| docs / README         | links resolve, code blocks are runnable as written    |

`python -m desmos check` is the subcommand. `python -m desmos.check` imports the
module, runs nothing, and exits 0 — it is not a test run.

## Style

- One change, one commit. Commit messages are prose, present tense, and explain
  *why*; the diff already says what.
- Comments earn their place by recording a decision or a trap, not by narrating
  the line below them.
- Prefer deleting a code path to adding a flag that selects between two.
- If a fix has both a streaming path and a finished path, fix both or delete
  one. Two implementations of the same transform is the bug.

## Working with an agent

`AGENTS.md` (symlinked as `CLAUDE.md`) is the instruction file for coding agents
run against this repo. If you change a workflow an agent depends on — a command
name, a state path, a pane's meaning — update `AGENTS.md` in the same commit.

## Reporting things

Bugs and feature requests go through
[the issue templates](https://github.com/umgbhalla/desmos/issues/new/choose).
Security issues do not — see [SECURITY.md](SECURITY.md).
