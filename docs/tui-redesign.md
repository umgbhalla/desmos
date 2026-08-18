# The TUI, redesigned

Written for the ARES track. The current frame is a nine-pane cockpit for one
session. ARES needs a front for many lines of work, most of them running
without it attached. This is the shape that follows from that.

## What the current frame cannot express

- **One subject.** Story, Calls, Meter, Git, Files, POST in, POST out, Queue
  and Input all describe *the* session. Topics, siblings and work items have
  nowhere to go.
- **Furniture.** Git and Files hold vertical space in every turn that is not
  about git or files; the meter holds a column to show three numbers.
- **Ownership.** The bridge owns stdin and EOF kills the loop, so a session
  with no front is never woken. Unattended work can be started, not watched.
- **The record outgrew the pane.** Nothing is ever deleted, but scrollback
  ends at the live database and cannot page into the cold store.
- **One 9443-line draw tree.** Every pane's geometry is arithmetic in main.rs.

## What is kept, deliberately

Disjoint routes. A `result` event never reaches the story, so the two panes
cannot disagree about what a syscall is. The redesign extends that discipline
instead of replacing it: routing gains a subject dimension, never a filter.
Focus chrome that does not move geometry. One implementation shared by the
streaming and finished paths.

## Three zones plus input

Rail, Story, Activity, and a status row. Three focus targets instead of nine.
The activity pane carries its own tab bar, so there is no separate inspector.

**Rail** -- one list with one switchable dimension: topics, siblings, work. A
row is a status glyph (running, waiting, blocked, parked), a title, an unread
badge, and for work rows the claim holder. Rows nest: a fork or a resume is
drawn under its parent in box-drawing characters, because parent_id and kind
are recorded already. Toggleable, and collapses to a glyph strip when narrow.

**Story** -- speech and thinking, always on screen beside the rail. It never
receives a result event, which is what keeps the two routes from disagreeing.

**Activity** -- one pane, tabs across its top: activity, post, meter, git,
files, json, work, history. The first tab is the syscall stream; the rest are
keyed to the current selection, so post shows the selected call's request and
response rather than the last one anywhere. Routing never depends on which tab
is visible.

**Status** -- one row: model and effort, spend against the ceiling, context
fill against the fold threshold, queue depth, attach state. This carries the
ambient awareness the deleted panes used to provide.

## A pane must earn permanent space

The criterion is whether it changes a decision every turn. The story and the
activity stream do. Budget does, as one line. Git, files, post and the meter
breakdown answer a question you ask, so they are tabs. Queue is a count until
it is not empty.

## The two architectural changes

**Events carry a subject.** Every event names the session it belongs to; the
front keeps a buffer per subject and paints only the selected one, while the
rest accumulate badges. Without this the rail is decoration.

**The front attaches, it does not own.** The loop becomes a daemon with
durable control input; a front connects, replays the tail from the database,
then follows. Detaching stops nothing. This is what makes siblings watchable.

## Order

R1 status row. R2 the activity tabs, and the fixed right column goes. R3 events
carry
a subject, proved by a test that two subjects never cross-paint. R4 rail over
topics. R5 rail over siblings. R6 rail over work. R7 split the draw tree per
zone. R1 and R2 stand alone; R4 needs the topics table, R5 the daemon, R6 the
work graph.

## What this trades

Density for depth. Nine panes showed a little of everything; four zones show
one thing well and keep the rest a keypress away. The badges and the status
row are the mitigation, and they are the parts to get right first.

## The glyph set

One cell, always. Nothing with emoji presentation: it draws two cells in some
terminals and one in others, and a tree column that tears is worse than no
tree. Shape carries the meaning and colour only the severity, so the set reads
on a monochrome terminal. Every glyph here is Narrow or East Asian Ambiguous,
the exposure the existing borders already carry and no more.

Hierarchy, in the rail:

    ├ └ │ ─   structure           U+251C U+2514 U+2502 U+2500
    ⋔         forked from parent  U+22D4
    ↳         resumed its parent  U+21B3
    ▾ ▸       expanded, collapsed U+25BE U+25B8
    ▌         the selected row    U+258C

Status, the first column:

    ●  running                          U+25CF
    ◐  waiting on a gate or a lease     U+25D0
    ○  idle, attachable                 U+25CB
    ◌  parked by hand                   U+25CC
    ⊘  blocked by a rejected dependency U+2298
    ✓  finished green                   U+2713
    ✗  finished red                     U+2717

Motion and fill:

    ⠋⠙⠹⠸⠼⠴⠦⠧  streaming spinner, braille is narrow  U+2800 block
    █ ░        context and budget bars              U+2588 U+2591
    ▲ ▼        more above, more below                U+25B2 U+25BC
    ⋯          a fold, and truncation in a card      U+22EF

The frame, drawn:

    ▌● ares                  ⠙
     ├◐ topics               2
     │└⋔ tui redesign
     ├○ phase B
     └◌ release v0.1.1b1     ✓

    ▸activity │ post │ meter │ git │ files │ json │ work │ history

    opus-5 high · $2.41/$10 · ctx ███████░░░ 62% · q0 · attached
