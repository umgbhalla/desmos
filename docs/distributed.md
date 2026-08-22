# Desmos distributed design — the super-WAL harness

Status: pinned 2026-08-22. Inspiration: herdr (control-plane legibility), mosh (state sync over replay), Cloudflare DO/D1 (single sequencer + archive).

## Shape

- **Local plane (every machine)**: desmosd (grown from `desmos bridge --daemon`) owns the local
  SQLite (single writer, WAL mode). Executors, kernels, and the TUI attach locally.
  Writes never wait on the network. Humans attach via mosh (flaky links) or ssh (tailscale).
- **Coordination plane (ONE Cloudflare Durable Object)**: every desmosd holds an *outbound*
  WebSocket to the same DO. The DO is single-threaded: it assigns the global per-channel `seq`,
  appends to its own SQLite storage (hot tail), and fans out to every connection.
  No machine opens an inbound port. WS hibernation keeps idle cost ~zero.
- **Archive plane (D1)**: the DO drains sealed segments to D1 — durable, queryable,
  cross-machine history and disaster recovery for the semantic log.

## Rules

1. One writer per SQLite file, forever. Cross-machine is message passing of an append-only log.
2. The outbox table (fingerprint, attempts, last_error) is the only egress: at-least-once
   delivery + fingerprint dedup at the DO = effectively exactly-once. Offline-first falls out.
3. Reattach is mosh-style: snapshot first (channel list, unread, story tail), then live stream.
   Never replay a backlog to become current.
4. Tri-state everywhere: every executor and channel is working | blocked | idle (herdr's killer
   feature). Blocked is first-class and waitable.
5. Channels are the unit: a channel is a DO-sequenced stream. Sessions are ephemeral attachments
   to a channel. Seated memory rides the same stream stamped by seat_id.
6. Additive on shared machines: desmos creates its own namespace, prompts only what it created,
   observes everything else read-only.

## Wire

- Local: NDJSON over unix socket (existing bridge protocol), `attach(since)` + sid stamping.
- Cloud: WebSocket JSON frames: `append{channel, fingerprint, body, author, seat}` ->
  `ack{seq}`; `sub{channels}` -> `event{channel, seq, ...}`; `snapshot{}` -> full state.
- Schema is printable from the binary (`desmos api schema`), herdr-style.

## Phases

1. ~~Channel syscalls wired locally~~ (already live: session op=post/read/inbox/dismiss/peers).
2. TUI channel rail: bridge op=channels, ev:channels handler, channel-keyed story, unread badges.
3. Cloudflare spine: Worker + DO (seq, DO-SQLite, fanout, hibernation) + D1 drain + deploy.
4. desmosd outbox drainer + outbound WS client (offline-first).
5. hyperion deploy: two machines, one channel, live; snapshot-sync reattach; tri-state surfacing.

Transport for humans stays mosh/ssh over tailscale. Cloudflare is the machine spine and the
optional human edge (zones: quel.computer, umgbhalla.com, umgbhalla.xyz). Creds in .env
(git-ignored): CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID.
