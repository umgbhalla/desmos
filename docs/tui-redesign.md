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

## Four zones

Rail, Stage, Inspector, Status plus Input. Four focus targets instead of nine.

**Rail** -- one list with one switchable dimension: topics, siblings, work. A
row is a status glyph (running, waiting, blocked, parked), a title, an unread
badge, and for work rows the claim holder. Toggleable, and collapses to a
glyph strip on a narrow terminal.

**Stage** -- the disjoint pair, and the only zone always on screen. Story
above, calls below; side by side past 160 columns.

**Inspector** -- modes, not panes: post, meter, git, files, json, work,
history. A mode is keyed to the current selection, so post shows the selected
call's request and response rather than the last one anywhere.

**Status** -- one row: model and effort, spend against the ceiling, context
fill against the fold threshold, queue depth, attach state. This carries the
ambient awareness the deleted panes used to provide.

## A pane must earn permanent space

The criterion is whether it changes a decision every turn. The story and the
calls do. Budget does, as one line. Git, files, post and the meter breakdown
answer a question you ask, so they are modes. Queue is a count until it is
not empty.

## The two architectural changes

**Events carry a subject.** Every event names the session it belongs to; the
front keeps a buffer per subject and paints only the selected one, while the
rest accumulate badges. Without this the rail is decoration.

**The front attaches, it does not own.** The loop becomes a daemon with
durable control input; a front connects, replays the tail from the database,
then follows. Detaching stops nothing. This is what makes siblings watchable.

## Order

R1 status line. R2 inspector, and the fixed right column goes. R3 events carry
a subject, proved by a test that two subjects never cross-paint. R4 rail over
topics. R5 rail over siblings. R6 rail over work. R7 split the draw tree per
zone. R1 and R2 stand alone; R4 needs the topics table, R5 the daemon, R6 the
work graph.

## What this trades

Density for depth. Nine panes showed a little of everything; four zones show
one thing well and keep the rest a keypress away. The badges and the status
row are the mitigation, and they are the parts to get right first.
