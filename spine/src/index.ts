import { DurableObject } from "cloudflare:workers";

export interface Env {
  SPINE: DurableObjectNamespace<Spine>;
  ARCHIVE: D1Database;
  SPINE_TOKEN: string;
}

/** Constant-time compare; length leak is fine, content leak is not. */
const tokenOk = (given: string, want: string): boolean => {
  if (!want || given.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) {
    diff |= given.charCodeAt(i) ^ want.charCodeAt(i);
  }
  return diff === 0;
};

type Attachment = { channels: string[] };
type Append = {
  op: "append";
  channel: string;
  fingerprint: string;
  author: string;
  seat: string;
  body: string;
};

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });

export default {
  fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const stub = env.SPINE.get(env.SPINE.idFromName("spine"));
    if (url.pathname === "/health") return stub.fetch("https://spine/health");
    if (url.pathname === "/ws") {
      const bearer = (request.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "");
      const given = bearer || url.searchParams.get("token") || "";
      if (!tokenOk(given, env.SPINE_TOKEN ?? "")) {
        return Promise.resolve(json({ error: "unauthorized" }, 401));
      }
      return stub.fetch(request);
    }
    return Promise.resolve(json({ error: "not found" }, 404));
  },
} satisfies ExportedHandler<Env>;

export class Spine extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      this.ctx.storage.sql.exec(`
        CREATE TABLE IF NOT EXISTS counters (
          channel TEXT PRIMARY KEY,
          seq INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hot_log (
          channel TEXT NOT NULL,
          seq INTEGER NOT NULL,
          fingerprint TEXT NOT NULL UNIQUE,
          author TEXT NOT NULL,
          seat TEXT NOT NULL,
          body TEXT NOT NULL,
          ts TEXT NOT NULL,
          PRIMARY KEY (channel, seq)
        );
        CREATE INDEX IF NOT EXISTS hot_channel_seq
          ON hot_log(channel, seq DESC);
      `);
    });
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/health") return json({ ok: true, do: "alive" });
    if (
      url.pathname !== "/ws" ||
      request.headers.get("Upgrade")?.toLowerCase() !== "websocket"
    ) return json({ error: "websocket upgrade required" }, 426);

    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    this.ctx.acceptWebSocket(server);
    server.serializeAttachment({ channels: [] } satisfies Attachment);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws: WebSocket, raw: string | ArrayBuffer): Promise<void> {
    try {
      const text = typeof raw === "string" ? raw : new TextDecoder().decode(raw);
      const msg = JSON.parse(text);
      if (msg.op === "sub") {
        const channels = Array.isArray(msg.channels)
          ? [...new Set(msg.channels.filter((x: unknown) => typeof x === "string"))]
          : [];
        ws.serializeAttachment({ channels } satisfies Attachment);
        ws.send(JSON.stringify({ op: "subbed" }));
      } else if (msg.op === "append") {
        await this.append(ws, msg as Append);
      } else if (msg.op === "snapshot") {
        ws.send(JSON.stringify(this.snapshot()));
      } else {
        ws.send(JSON.stringify({ op: "error", error: "unknown op" }));
      }
    } catch (error) {
      ws.send(JSON.stringify({
        op: "error",
        error: error instanceof Error ? error.message : "invalid frame",
      }));
    }
  }

  private async append(ws: WebSocket, msg: Append): Promise<void> {
    for (const key of ["channel", "fingerprint", "author", "seat", "body"] as const) {
      if (typeof msg[key] !== "string" || !msg[key]) throw new Error(`invalid ${key}`);
    }

    const result = this.ctx.storage.transactionSync(() => {
      const sql = this.ctx.storage.sql;
      const prior = [...sql.exec<{ seq: number }>(
        "SELECT seq FROM hot_log WHERE fingerprint = ?",
        msg.fingerprint,
      )][0];
      if (prior) return { seq: prior.seq, duplicate: true };

      const counter = [...sql.exec<{ seq: number }>(`
        INSERT INTO counters(channel, seq) VALUES (?, 1)
        ON CONFLICT(channel) DO UPDATE SET seq = seq + 1
        RETURNING seq
      `, msg.channel)][0];
      const ts = new Date().toISOString();
      sql.exec(
        `INSERT INTO hot_log(channel, seq, fingerprint, author, seat, body, ts)
         VALUES (?, ?, ?, ?, ?, ?, ?)`,
        msg.channel, counter.seq, msg.fingerprint, msg.author, msg.seat, msg.body, ts,
      );
      return { seq: counter.seq, duplicate: false, ts };
    });

    if (!result.duplicate) {
      const event = JSON.stringify({
        op: "event",
        channel: msg.channel,
        seq: result.seq,
        author: msg.author,
        seat: msg.seat,
        fingerprint: msg.fingerprint,
        body: msg.body,
        ts: result.ts,
      });
      for (const socket of this.ctx.getWebSockets()) {
        const attachment = socket.deserializeAttachment() as Attachment | null;
        if (attachment?.channels.includes(msg.channel) || attachment?.channels.includes("*")) {
          try { socket.send(event); } catch { /* stale socket */ }
        }
      }
    }
    ws.send(JSON.stringify({
      op: "ack",
      seq: result.seq,
      fingerprint: msg.fingerprint,
    }));
    if (await this.ctx.storage.getAlarm() === null) {
      await this.ctx.storage.setAlarm(Date.now() + 5_000);
    }
  }

  private snapshot(): unknown {
    const channels = [...this.ctx.storage.sql.exec<{
      channel: string;
      max_seq: number;
    }>("SELECT channel, seq AS max_seq FROM counters ORDER BY channel")].map((row) => ({
      channel: row.channel,
      max_seq: row.max_seq,
      tail: [...this.ctx.storage.sql.exec(
        `SELECT channel, seq, author, seat, body, ts
         FROM hot_log WHERE channel = ? ORDER BY seq DESC LIMIT 20`,
        row.channel,
      )].reverse(),
    }));
    return { op: "snapshot", channels };
  }

  async alarm(): Promise<void> {
    const rows = [...this.ctx.storage.sql.exec<{
      channel: string; seq: number; author: string; seat: string; body: string; ts: string;
    }>(`
      SELECT h.channel, h.seq, h.author, h.seat, h.body, h.ts
      FROM hot_log h JOIN counters c ON c.channel = h.channel
      WHERE h.seq <= c.seq - 500
      ORDER BY h.channel, h.seq LIMIT 100
    `)];

    if (rows.length) {
      await this.env.ARCHIVE.batch(rows.map((row) =>
        this.env.ARCHIVE.prepare(
          `INSERT OR IGNORE INTO log(channel, seq, author, seat, body, ts)
           VALUES (?, ?, ?, ?, ?, ?)`,
        ).bind(row.channel, row.seq, row.author, row.seat, row.body, row.ts)
      ));
      this.ctx.storage.transactionSync(() => {
        for (const row of rows) {
          this.ctx.storage.sql.exec("DELETE FROM hot_log WHERE channel = ? AND seq = ?", row.channel, row.seq);
        }
      });
      if (rows.length === 100) await this.ctx.storage.setAlarm(Date.now() + 1_000);
    }
  }
}
