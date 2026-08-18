# Topics: a long-running, multi-channel UX

## 1. What a topic is

A topic is a long-running line of discussion with its own transcript, its own
fold history and its own name. Sessions are days; a topic is the conversation
those days belong to. It is created by the user, or forked by the seat agent
when a thread turns into its own discussion.

A topic is a **session lineage**, not a new store. Root session plus its resume
chain. `sessions.parent_id` and `kind` already carry the shape, and `kind='fork'`
— which the schema has always allowed and nothing has ever written
(persist.py:450-455, 740-749) — finally gets an owner: a forked topic records
the session it left and the sequence it left at.

## 2. What already exists

- `switch_session()` rebinds the session environment (loop.py:1131-1157).
- The picker lists real sessions and the choice carries an id (bea417d).
- The bridge exports `DESMOS_SESSION_ID`, so the chosen session is the loaded
  one (QUERY 5).
- `_read_data` loads one session, not the workspace (139753a).
- `channel_messages` / `channel_cursors` make a session addressable.

Switching and per-session isolation are done. Missing: topic identity distinct
from a session row, a persistent sidebar instead of a modal picker,
topic-scoped notes, and agent-side topic ops.

## 3. Data model

Additive only, and therefore lands after Phase 0 of the ARES plan.

- `topics(id, workspace_id, title, status, created_at, updated_at,
  parent_topic_id, forked_from_session, forked_at_seq)`.
- `sessions.topic_id`, a nullable column an older reader can ignore.
- status is `live | parked | done`. Nothing is deleted (decision 7).
- unread comes from `channel_messages` timestamps against `channel_cursors`.

## 4. Notes and todos become topic-scoped

This is the finding that makes topics more than a UI change. `notes` is keyed
`(workspace_id, name)` (persist.py:477-483), so every topic would share one
handoff note and one todo list — a fold in one topic would rewrite another's.

Additive fix: a `topic_notes(topic_id, name, body, updated_at)` table and a
lookup order of topic note first, workspace note second. Workspace notes stay
what they are: shared doctrine. The todo becomes per topic.

## 5. The TUI

The panes stay exactly as they are. The addition is a left rail.

- Rows are topics ordered by last activity: status glyph, title, unread badge,
  current one highlighted.
- Keys: new topic, fork the current one at the cursor, rename, park, and move
  between rows. The rail toggles; hidden is the current look, unchanged.
- Only the focused pane draws a frame — existing chrome doctrine, so geometry
  never jumps.
- Switching rebuilds the world from that session row. The bridge already does
  this for the picker; the rail only makes it non-modal.

## 6. Bridge protocol

- event `topics`: the list, emitted on ready, after every turn, and when a
  channel message lands.
- requests: `topic.switch {id}`, `topic.new {title}`,
  `topic.fork {session, seq, title}`, `topic.rename`, `topic.park`.
- Reuse the existing snapshot and replay path; do not add a second transport.

## 7. The agent side

`session op=topic`, body forms: empty lists, `new TITLE`, `fork TITLE`, `park`,
`done`. Topics are not gated — seats are people and cost a decision, topics are
cheap and visible.

## 8. Multi-channel

Every topic is an address. A sibling session (ARES 2) posts into a topic over
the existing peer rail; the rail shows an unread badge; opening the topic
delivers those messages into that transcript. No second messaging system.

## 9. Build order

1. Phase 0 tolerance (ARES 10). Prerequisite for any column.
2. `topics` + `sessions.topic_id`, backfilled — every existing lineage becomes
   a topic titled from its first prompt. *Gate:* restart, the list is unchanged.
3. `topic_notes` with fallback lookup. *Gate:* two topics, two handoffs, no
   bleed between them.
4. bridge `topics` event and switch request. *Gate:* switch by id and the
   transcript that loads is that topic's.
5. the Rust rail. *Gate:* a test through the real event handler asserting rail
   state after a `topics` event — not a renderer test.
6. `session op=topic`. *Gate:* a fork writes `kind='fork'` with the sequence.
7. unread badges from the channel cursors.

## 10. Risks

- **Switching mid-turn.** Refuse while a turn is running, the same guard
  `reset_transcript` already uses (loop.py:1118-1120).
- **Folds.** Each topic folds independently once notes are topic-scoped; before
  that, a fold in one topic rewrites another's handoff.
- **Still one live front.** Topics are lines inside one front, not parallel
  fronts. The workspace lock is unchanged, and siblings remain separate
  headless processes.
