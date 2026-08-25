import { DurableObject } from "cloudflare:workers";

export interface Env {
  SPINE: DurableObjectNamespace<Spine>;
  ARCHIVE: D1Database;
  SPINE_TOKEN: string;
}

const tokenOk = (given: string, want: string): boolean => {
  if (!want || given.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) diff |= given.charCodeAt(i) ^ want.charCodeAt(i);
  return diff === 0;
};
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { "content-type": "application/json" },
});
const credential = (request: Request): string =>
  (request.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
const hex = (bytes: ArrayBuffer | Uint8Array): string =>
  [...new Uint8Array(bytes)].map((x) => x.toString(16).padStart(2, "0")).join("");
const hash = async (value: string): Promise<string> =>
  hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));

type Attachment = { channels: string[]; seat: string };
type Append = {
  op: "append"; channel: string; fingerprint: string; author: string; body: string; seat?: string;
};
type EventRow = {
  channel: string; seq: number; author: string; seat: string; body: string; ts: string;
};

const MAX_FRAME_BYTES = 256 * 1024;
const MAX_CHANNEL_BYTES = 128;
const MAX_FINGERPRINT_BYTES = 256;
const MAX_AUTHOR_BYTES = 128;
const MAX_BODY_BYTES = 128 * 1024;
const MAX_SUBSCRIPTIONS = 128;
const byteLength = (value: string): number => new TextEncoder().encode(value).byteLength;
const bounded = (value: unknown, max: number): value is string =>
  typeof value === "string" && value.length > 0 && byteLength(value) <= max;

export default {
  fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const stub = env.SPINE.get(env.SPINE.idFromName("spine"));
    if (url.pathname === "/health") return stub.fetch("https://spine/health");
    if (url.pathname.startsWith("/seats") || url.pathname === "/drain"
        || url.pathname === "/purge") {
      const given = (request.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
      if (!tokenOk(given, env.SPINE_TOKEN ?? "")) {
        return Promise.resolve(json({ error: "unauthorized" }, 401));
      }
      return stub.fetch(request);
    }
    if (url.pathname === "/ws") return stub.fetch(request);
    return Promise.resolve(json({ error: "not found" }, 404));
  },
} satisfies ExportedHandler<Env>;

export class Spine extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      this.ctx.storage.sql.exec(`
        CREATE TABLE IF NOT EXISTS counters (
          channel TEXT PRIMARY KEY, seq INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS migrations (
          name TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS seats (
          seat TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL, retired_at TEXT
        );
      `);
      const migrated = [...this.ctx.storage.sql.exec(
        "SELECT 1 FROM migrations WHERE name = 'channel_fingerprint'",
      )][0];
      if (!migrated) {
        this.ctx.storage.sql.exec(`
          DROP TABLE IF EXISTS hot_log;
          CREATE TABLE hot_log (
            channel TEXT NOT NULL, seq INTEGER NOT NULL,
            fingerprint TEXT NOT NULL, author TEXT NOT NULL,
            seat TEXT NOT NULL, body TEXT NOT NULL, ts TEXT NOT NULL,
            PRIMARY KEY (channel, seq),
            UNIQUE (channel, fingerprint)
          );
          CREATE INDEX hot_channel_seq ON hot_log(channel, seq DESC);
          INSERT INTO migrations(name) VALUES ('channel_fingerprint');
        `);
      }
    });
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") return json({ ok: true, do: "alive" });
    if (url.pathname === "/seats" && request.method === "POST") {
      let body: { seat?: unknown };
      try { body = await request.json(); } catch { return json({ error: "invalid json" }, 400); }
      if (typeof body.seat !== "string" || !body.seat) return json({ error: "invalid seat" }, 400);
      const token = hex(crypto.getRandomValues(new Uint8Array(32)));
      const tokenHash = await hash(token);
      const created = new Date().toISOString();
      this.ctx.storage.sql.exec(`
        INSERT INTO seats(seat, token_hash, created_at, retired_at) VALUES (?, ?, ?, NULL)
        ON CONFLICT(seat) DO UPDATE SET token_hash = excluded.token_hash, retired_at = NULL
      `, body.seat, tokenHash, created);
      return json({ seat: body.seat, token });
    }
    if (url.pathname === "/seats" && request.method === "GET") {
      const seats = [...this.ctx.storage.sql.exec(
        "SELECT seat, created_at, retired_at FROM seats ORDER BY seat",
      )];
      return json({ seats });
    }
    if (url.pathname.startsWith("/seats/") && request.method === "DELETE") {
      const seat = decodeURIComponent(url.pathname.slice("/seats/".length));
      const existing = [...this.ctx.storage.sql.exec<{ seat: string }>(
        "SELECT seat FROM seats WHERE seat = ?", seat,
      )][0];
      if (!existing) return json({ error: "seat not found" }, 404);
      this.ctx.storage.sql.exec(
        "UPDATE seats SET retired_at = ? WHERE seat = ?", new Date().toISOString(), seat,
      );
      for (const socket of this.ctx.getWebSockets()) {
        const attachment = socket.deserializeAttachment() as Attachment | null;
        if (attachment?.seat === seat) try { socket.close(4001, "seat retired"); } catch {}
      }
      return json({ seat, retired: true });
    }
    if (url.pathname === "/drain" && request.method === "POST") {
      return json({ drained: await this.drain() });
    }
    if (url.pathname === "/purge" && request.method === "POST") {
      let body: { channel?: unknown };
      try { body = await request.json(); } catch { return json({ error: "invalid json" }, 400); }
      if (!bounded(body.channel, MAX_CHANNEL_BYTES) || body.channel === "*") {
        return json({ error: "invalid channel" }, 400);
      }
      await this.purgeChannel(body.channel);
      return json({ purged: body.channel });
    }
    if (url.pathname !== "/ws") return json({ error: "not found" }, 404);
    const tokenHash = await hash(credential(request));
    const active = [...this.ctx.storage.sql.exec<{ seat: string }>(
      "SELECT seat FROM seats WHERE token_hash = ? AND retired_at IS NULL", tokenHash,
    )][0];
    if (!active) return json({ error: "unauthorized" }, 401);
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return json({ error: "websocket upgrade required" }, 426);
    }
    const pair = new WebSocketPair();
    const client = pair[0], server = pair[1];
    this.ctx.acceptWebSocket(server);
    server.serializeAttachment({ channels: [], seat: active.seat } satisfies Attachment);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws: WebSocket, raw: string | ArrayBuffer): Promise<void> {
    const attachment = ws.deserializeAttachment() as Attachment | null;
    if (!attachment || !this.active(attachment.seat)) {
      try { ws.close(4001, "seat retired"); } catch {}
      return;
    }
    try {
      const text = typeof raw === "string" ? raw : new TextDecoder().decode(raw);
      if (byteLength(text) > MAX_FRAME_BYTES) throw new Error("frame too large");
      const msg = JSON.parse(text);
      if (msg.op === "sub") {
        const channels = Array.isArray(msg.channels)
          ? [...new Set<string>(msg.channels.filter(
            (x: unknown) => bounded(x, MAX_CHANNEL_BYTES),
          ))] : [];
        if (channels.length > MAX_SUBSCRIPTIONS) throw new Error("too many subscriptions");
        if (Array.isArray(msg.channels) && channels.length !== msg.channels.length) {
          throw new Error("invalid subscription");
        }
        ws.serializeAttachment({ channels, seat: attachment.seat } satisfies Attachment);
        ws.send(JSON.stringify({ op: "subbed" }));
      } else if (msg.op === "append") {
        await this.append(ws, msg as Append, attachment.seat);
      } else if (msg.op === "snapshot") {
        ws.send(JSON.stringify(this.snapshot()));
      } else if (msg.op === "replay") {
        await this.replay(ws, msg);
      } else if (msg.op === "purge") {
        ws.send(JSON.stringify({ op: "error", error: "admin operation" }));
      } else {
        ws.send(JSON.stringify({ op: "error", error: "unknown op" }));
      }
    } catch (error) {
      ws.send(JSON.stringify({
        op: "error", error: error instanceof Error ? error.message : "invalid frame",
      }));
    }
  }

  private active(seat: string): boolean {
    return !![...this.ctx.storage.sql.exec(
      "SELECT 1 FROM seats WHERE seat = ? AND retired_at IS NULL", seat,
    )][0];
  }

  private async append(ws: WebSocket, msg: Append, seat: string): Promise<void> {
    const limits = {
      channel: MAX_CHANNEL_BYTES,
      fingerprint: MAX_FINGERPRINT_BYTES,
      author: MAX_AUTHOR_BYTES,
      body: MAX_BODY_BYTES,
    } as const;
    for (const key of ["channel", "fingerprint", "author", "body"] as const) {
      if (!bounded(msg[key], limits[key])) throw new Error(`invalid ${key}`);
    }
    if (msg.channel === "*") throw new Error("invalid channel");
    const result = this.ctx.storage.transactionSync(() => {
      const sql = this.ctx.storage.sql;
      const prior = [...sql.exec<{ seq: number }>(
        "SELECT seq FROM hot_log WHERE channel = ? AND fingerprint = ?",
        msg.channel, msg.fingerprint,
      )][0];
      if (prior) return { seq: prior.seq, duplicate: true, ts: undefined };
      const counter = [...sql.exec<{ seq: number }>(`INSERT INTO counters(channel, seq)
        VALUES (?, 1) ON CONFLICT(channel) DO UPDATE SET seq = seq + 1 RETURNING seq`,
        msg.channel)][0];
      const ts = new Date().toISOString();
      sql.exec(`INSERT INTO hot_log(channel, seq, fingerprint, author, seat, body, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)`,
        msg.channel, counter.seq, msg.fingerprint, msg.author, seat, msg.body, ts);
      return { seq: counter.seq, duplicate: false, ts };
    });
    if (!result.duplicate) {
      const event = JSON.stringify({
        op: "event", channel: msg.channel, seq: result.seq, author: msg.author,
        seat, fingerprint: msg.fingerprint, body: msg.body, ts: result.ts,
      });
      for (const socket of this.ctx.getWebSockets()) {
        const target = socket.deserializeAttachment() as Attachment | null;
        if (target?.channels.includes(msg.channel) || target?.channels.includes("*")) {
          try { socket.send(event); } catch {}
        }
      }
    }
    ws.send(JSON.stringify({ op: "ack", seq: result.seq, fingerprint: msg.fingerprint }));
    if (await this.ctx.storage.getAlarm() === null) {
      await this.ctx.storage.setAlarm(Date.now() + 5_000);
    }
  }

  private async replay(ws: WebSocket, msg: Record<string, unknown>): Promise<void> {
    if (typeof msg.channel !== "string" || !msg.channel) throw new Error("invalid channel");
    const since = Number.isSafeInteger(msg.since) && (msg.since as number) >= 0
      ? msg.since as number : 0;
    const requested = Number.isSafeInteger(msg.limit) ? msg.limit as number : 100;
    const limit = Math.max(1, Math.min(500, requested));
    const archived = await this.env.ARCHIVE.prepare(`SELECT channel, seq, author, seat, body, ts
      FROM log WHERE channel = ? AND seq > ? ORDER BY seq LIMIT ?`)
      .bind(msg.channel, since, limit + 1).all<EventRow>();
    const hot = [...this.ctx.storage.sql.exec<EventRow>(`SELECT channel, seq, author, seat, body, ts
      FROM hot_log WHERE channel = ? AND seq > ? ORDER BY seq LIMIT ?`,
      msg.channel, since, limit + 1)];
    const merged = new Map<number, EventRow>();
    for (const row of [...(archived.results ?? []), ...hot]) merged.set(row.seq, row);
    const ordered = [...merged.values()].sort((a, b) => a.seq - b.seq);
    const events = ordered.slice(0, limit);
    const next = ordered.length > limit ? events.at(-1)!.seq : null;
    const counter = [...this.ctx.storage.sql.exec<{ seq: number }>(
      "SELECT seq FROM counters WHERE channel = ?", msg.channel,
    )][0];
    const highWatermark = counter?.seq ?? 0;
    const archivedMin = await this.env.ARCHIVE.prepare(
      "SELECT MIN(seq) AS seq FROM log WHERE channel = ?",
    ).bind(msg.channel).first<{ seq: number | null }>();
    const hotMin = [...this.ctx.storage.sql.exec<{ seq: number | null }>(
      "SELECT MIN(seq) AS seq FROM hot_log WHERE channel = ?", msg.channel,
    )][0];
    const minima = [archivedMin?.seq, hotMin?.seq]
      .filter((value): value is number => typeof value === "number");
    const firstAvailable = minima.length ? Math.min(...minima) : null;
    const gap = firstAvailable !== null && since + 1 < firstAvailable
      ? { from: since + 1, to: firstAvailable - 1 }
      : null;
    ws.send(JSON.stringify({
      op: "replay", channel: msg.channel, events, next,
      high_watermark: highWatermark, first_available: firstAvailable, gap,
    }));
  }

  private async purgeChannel(channel: string): Promise<void> {
    this.ctx.storage.transactionSync(() => {
      this.ctx.storage.sql.exec("DELETE FROM hot_log WHERE channel = ?", channel);
      this.ctx.storage.sql.exec("DELETE FROM counters WHERE channel = ?", channel);
    });
    await this.env.ARCHIVE.prepare("DELETE FROM log WHERE channel = ?").bind(channel).run();
  }

  private snapshot(): unknown {
    const channels = [...this.ctx.storage.sql.exec<{ channel: string; max_seq: number }>(
      "SELECT channel, seq AS max_seq FROM counters ORDER BY channel",
    )].map((row) => ({
      channel: row.channel, max_seq: row.max_seq,
      tail: [...this.ctx.storage.sql.exec(`SELECT channel, seq, author, seat, body, ts
        FROM hot_log WHERE channel = ? ORDER BY seq DESC LIMIT 20`, row.channel)].reverse(),
    }));
    return { op: "snapshot", channels };
  }

  async alarm(): Promise<void> { await this.drain(); }

  private async drain(): Promise<number> {
    const rows = [...this.ctx.storage.sql.exec<EventRow>(`SELECT h.channel, h.seq, h.author,
      h.seat, h.body, h.ts FROM hot_log h JOIN counters c ON c.channel = h.channel
      WHERE h.seq <= c.seq - 500 ORDER BY h.channel, h.seq LIMIT 100`)];
    if (rows.length) {
      await this.env.ARCHIVE.batch(rows.map((row) => this.env.ARCHIVE.prepare(`
        INSERT OR IGNORE INTO log(channel, seq, author, seat, body, ts)
        VALUES (?, ?, ?, ?, ?, ?)`).bind(
          row.channel, row.seq, row.author, row.seat, row.body, row.ts,
        )));
      this.ctx.storage.transactionSync(() => {
        for (const row of rows) this.ctx.storage.sql.exec(
          "DELETE FROM hot_log WHERE channel = ? AND seq = ?", row.channel, row.seq,
        );
      });
      if (rows.length === 100) await this.ctx.storage.setAlarm(Date.now() + 1_000);
    }
    return rows.length;
  }
}
