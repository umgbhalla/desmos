// The far side of the desmos outbox: accept a batch, insert what is new.
//
// The client (desmos/state/d1.py) posts {"rows": [...]} with a bearer token.
// Every row carries the fingerprint the local queue already deduped on, so
// this end can be blunt: INSERT OR IGNORE, batched, no read-modify-write and
// nothing that can overwrite an older fact with a newer copy of itself.
//
// A non-2xx from here leaves the whole batch pending on the client, which is
// the intended failure: it costs a retry, never a row.

const MAX_ROWS = 200;

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return json({ error: "post a batch" }, 405);
    }
    const expected = env.DESMOS_SYNC_TOKEN;
    if (expected) {
      const given = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/, "");
      if (given !== expected) return json({ error: "unauthorized" }, 401);
    }

    let body;
    try {
      body = await request.json();
    } catch (err) {
      return json({ error: "unparseable body" }, 400);
    }
    const rows = Array.isArray(body && body.rows) ? body.rows : null;
    if (!rows) return json({ error: "rows must be an array" }, 400);
    if (rows.length > MAX_ROWS) return json({ error: "batch too large" }, 413);

    const now = new Date().toISOString();
    const insert = env.DB.prepare(
      "INSERT OR IGNORE INTO cold_facts(fingerprint, workspace_id, kind," +
      " payload_json, created_at, received_at) VALUES (?, ?, ?, ?, ?, ?)"
    );
    const statements = [];
    for (const row of rows) {
      if (!row || typeof row.fingerprint !== "string" || row.fingerprint.length !== 64) {
        return json({ error: "every row needs a sha256 fingerprint" }, 400);
      }
      statements.push(insert.bind(
        row.fingerprint,
        String(row.workspace_id || ""),
        String(row.kind || ""),
        JSON.stringify(row.payload === undefined ? null : row.payload),
        String(row.created_at || now),
        now
      ));
    }

    try {
      await env.DB.batch(statements);
    } catch (err) {
      // The client keeps the batch. Say why, and let it try again.
      return json({ error: String(err && err.message ? err.message : err) }, 502);
    }
    return json({ ok: true, received: rows.length });
  },
};

function json(value, status) {
  return new Response(JSON.stringify(value), {
    status: status || 200,
    headers: { "Content-Type": "application/json" },
  });
}
