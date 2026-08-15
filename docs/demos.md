# Demos

Recording the TUI for docs and issues.

Recorded with [termctrl](https://github.com/zeu5/termctrl) driving the offline
`--demo` TUI, so a capture needs no API key.

    ./scripts/record-demo.sh panes     # story vs wire, folding, POST tree
    ./scripts/record-demo.sh wire      # tail stays open, overflow counted
    ./scripts/record-demo.sh zoom      # zoom a block into the pager, search

All three export **2600x1448 at 60fps**, verified with ffprobe. Resolution is
cell geometry, not a flag:

    width  = (cols * cell_width  + 2 * padding) * pixel_ratio
    height = (rows * cell_height + 2 * padding) * pixel_ratio

Defaults are `140x38` cells at `9x18`, padding `20`, ratio `2`. Override per run:

    COLS=160 ROWS=45 RATIO=3 ./scripts/record-demo.sh panes   # 4320x2532

Raise `RATIO` instead of upscaling afterwards: termctrl rasterises at the target
size, so glyph edges stay sharp. Exact 16:9 is not reachable with integer cells
-- 2600x1448 is 1.795.

`--record` keeps the raw `.termctrl` (original timing, bytes, input, markers), so
re-cutting a clip never re-runs the session:

    termctrl markers captures/panes.termctrl
    termctrl video captures/panes.termctrl --edit plan.json --footer

Two things that bite: input atoms need `text:<char>` (a bare `j` is rejected),
and `termctrl start` is not idempotent, so the script stops and prunes the
session name first.

## What is worth filming

One idea per clip. The offline demo is static content, so navigation scenes
yield only 3-5 unique screens. Streaming, queueing, subagents and the diff card
need a live session (`--live`, which does spend tokens).

| scene | shows | offline |
|---|---|---|
| `panes` | prose left, the syscalls that produced it right | yes |
| `wire` | newest cards open, older folded, `N more up` | yes |
| `zoom` | block viewer: search, wrap, raw | yes |
| stream | a syscall card opening, stdout arriving into it | no |
| queue | Enter stacking follow-ups mid-step, `[` `]` reorder | no |
| spawn | SubagentBlock, Enter into the child session | no |
| diff | an edit card rendering hunks | no |
| lag | `POST out #5  waiting #6` mid-step | no |
