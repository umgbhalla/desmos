import assert from "node:assert/strict";
const base = process.argv[2], admin = (process.env.DESMOS_SPINE_ADMIN_TOKEN ?? process.env.DESMOS_SPINE_TOKEN);
if (!base || !admin) throw new Error("usage: DESMOS_SPINE_TOKEN=... node test/roundtrip.mjs https://worker-url");
const adminFetch = (path, init = {}) => fetch(new URL(path, base), {
  ...init, headers: { authorization: `Bearer ${admin}`, "content-type": "application/json", ...init.headers },
});
async function mint(seat) {
  const r = await adminFetch("/seats", { method: "POST", body: JSON.stringify({ seat }) });
  assert.equal(r.status, 200);
  const v = await r.json();
  assert.equal(v.seat, seat);
  assert.match(v.token, /^[0-9a-f]{64}$/);
  return v.token;
}
function client(token) {
  const url = new URL("/ws", base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("token", token);
  const ws = new WebSocket(url), queue = [], waiters = [];
  ws.addEventListener("message", ({ data }) => {
    const msg = JSON.parse(data), waiter = waiters.shift();
    if (waiter) waiter(msg); else queue.push(msg);
  });
  const open = new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  const closed = new Promise((resolve) => ws.addEventListener("close", resolve, { once: true }));
  const next = () => queue.length ? Promise.resolve(queue.shift()) : new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("message timeout")), 10_000);
    waiters.push((msg) => { clearTimeout(timer); resolve(msg); });
  });
  return { ws, open, closed, next };
}
const unauthorized = await fetch(new URL("/ws", base), {
  headers: { authorization: `Bearer ${admin}` },
});
assert.equal(unauthorized.status, 401);
console.log("PASS admin token on /ws returned 401");

const stamp = `${Date.now()}-${crypto.randomUUID()}`;
const seat = `roundtrip-${stamp}`, token = await mint(seat);
console.log("PASS minted fresh seat with 64-hex token");
const listed = await adminFetch("/seats"), listing = await listed.json();
assert.equal(listed.status, 200);
const row = listing.seats.find((x) => x.seat === seat);
assert.ok(row);
assert.deepEqual(Object.keys(row).sort(), ["created_at", "retired_at", "seat"]);
console.log("PASS seat listing excludes token hashes");

const channel = `#roundtrip-${stamp}`, a = client(token), b = client(token);
await Promise.all([a.open, b.open]);
a.ws.send(JSON.stringify({ op: "sub", channels: [channel] }));
assert.deepEqual(await a.next(), { op: "subbed" });
const frames = [
  { op: "append", channel, fingerprint: `${stamp}-1`, author: "b", seat: "spoofed", body: "one" },
  { op: "append", channel, fingerprint: `${stamp}-1`, author: "b", seat: "spoofed", body: "one" },
  { op: "append", channel, fingerprint: `${stamp}-2`, author: "b", seat: "spoofed", body: "two" },
];
const acks = [];
for (const frame of frames) { b.ws.send(JSON.stringify(frame)); acks.push(await b.next()); }
assert.deepEqual(acks.map((x) => x.seq), [1, 1, 2]);
assert.equal(acks[0].fingerprint, acks[1].fingerprint);
const events = [await a.next(), await a.next()];
assert.deepEqual(events.map((x) => x.seq), [1, 2]);
assert.deepEqual(events.map((x) => x.seat), [seat, seat]);
console.log("PASS subscribed client received seqs 1,2 with server-stamped seat");
console.log("PASS duplicate fingerprint acked seq 1 twice and emitted once");

b.ws.send(JSON.stringify({ op: "snapshot" }));
const snapshot = await b.next();
assert.equal(snapshot.channels.find((x) => x.channel === channel)?.max_seq, 2);
console.log("PASS snapshot channel max_seq=2");
b.ws.send(JSON.stringify({ op: "replay", channel, since: 0, limit: 500 }));
const replay = await b.next();
assert.deepEqual(replay, {
  op: "replay", channel,
  events: events.map(({ channel, seq, author, seat, body, ts }) => ({ channel, seq, author, seat, body, ts })),
  next: null,
});
console.log("PASS replay returned ordered seqs 1,2 with exact event shape");

const retiredSeat = `retire-${stamp}`, retiredToken = await mint(retiredSeat);
const doomed = client(retiredToken);
await doomed.open;
doomed.ws.send(JSON.stringify({ op: 'sub', channels: ['#retire'] }));
assert.deepEqual(await doomed.next(), { op: 'subbed' });
const rr = await adminFetch(`/seats/${encodeURIComponent(retiredSeat)}`, { method: "DELETE" });
assert.equal(rr.status, 200);
assert.deepEqual(await rr.json(), { seat: retiredSeat, retired: true });
const close = await Promise.race([
  doomed.closed,
  new Promise((_, reject) => setTimeout(() => reject(new Error("close timeout")), 10_000)),
]);
assert.equal(close.code, 4001);
console.log("PASS retiring seat closed its live socket with code 4001");
const dr = await adminFetch("/drain", { method: "POST" }), drain = await dr.json();
assert.equal(dr.status, 200);
assert.equal(typeof drain.drained, "number");
console.log(`PASS drain returned drained=${drain.drained}`);
a.ws.close();
b.ws.close();
