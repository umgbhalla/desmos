# Security policy

## Reporting a vulnerability

Report privately through GitHub Security Advisories:
**[Report a vulnerability](https://github.com/umgbhalla/desmos/security/advisories/new)**.
Do not open a public issue for anything exploitable.

Include: what you ran, what happened, and the smallest reproduction you have.
Expect a first response within a week.

## Scope

desmos executes model-authored code by design. `<python>`, `<bash>` and
`<shell>` run with the privileges of the user who started the process, and the
agent can rewrite its own tools, notes and skills. **That is the product, not a
vulnerability.** Run it in a directory and under an account you are willing to
lose.

In scope:

- credential handling — anything that writes an API key or OAuth token to a
  place it should not be (`~/.desmos/auth.json`, `.desmos/harness.sqlite3`,
  `runs/`, the TUI panes, a log file, a commit)
- the PKCE login flow in `desmos/auth.py` and its localhost redirect
- prompt or transcript content escaping into a channel it should not reach
- a syscall executing outside the tag it was written in — dispatcher parsing
  bugs that let untrusted text become a call
- privilege the harness grants that it did not intend: a subagent writing the
  parent's state, a scoped tool set that is not actually enforced

Out of scope:

- the agent choosing to run a destructive command you asked it to run
- vendored third-party code (`vendor/`) — report those upstream
- results the model fabricates in prose; treat model output as untrusted input

## Credentials

- `ANTHROPIC_API_KEY` is read from the environment only. Never written to disk
  by desmos.
- OpenAI credentials land in `~/.desmos/auth.json` (mode 0600), or are read
  from an existing `~/.codex/auth.json`.
- `.desmos/` and `runs/` are gitignored. CI fails the build on key-shaped
  strings anywhere in the tree, including `vendor/`.

If you believe a key leaked into a commit, rotate it first, then report.
