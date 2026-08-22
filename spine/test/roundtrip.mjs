import assert from "node:assert/strict";

const base = process.argv[2];
if (!base) throw new Error("usage: node test/roundtrip.mjs https://worker-url");
const wsUrl = new URL("/ws", base);
wsUrl.protocol = wsUrl.protocol === "https:" ? "wss:" : "ws:";
if (process.env.DESMOS_SPINE_TOKEN) {
  wsUrl.searchParams.set("token", process.env.DESMOS_SPINE_TOKEN);
}

function client() {
  const ws = new WebSocket(wsUrl);
  const queue = [];
  const waiters = [];
  ws.addEventListener("message", ({ data }) => {
    const msg = JSON.parse(data);
    const waiter = waiters.shift();
    if (waiter) waiter(msg); else queue.push(msg);
  });
  const open = new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  const next = () => queue.length
    ? Promise.resolve(queue.shift())
    : new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("message timeout")), 10_000);
        waiters.push((msg) => { clearTimeout(timer); resolve(msg); });
      });
  return { ws, open, next };
}

const a = client();
const b = client();
await Promise.all([a.open, b.open]);

a.ws.send(JSON.stringify({ op: "sub", channels: ["#test"] }));
assert.deepEqual(await a.next(), { op: "subbed" });

const stamp = `${Date.now()}-${Math.random()}`;
const frames = [
  { op: "append", channel: "#test", fingerprint: `${stamp}-1`, author: "b", seat: "test", body: "one" },
  { op: "append", channel: "#test", fingerprint: `${stamp}-1`, author: "b", seat: "test", body: "one" },
  { op: "append", channel: "#test", fingerprint: `${stamp}-2`, author: "b", seat: "test", body: "two" },
];
const acks = [];
for (const frame of frames) {
  b.ws.send(JSON.stringify(frame));
  acks.push(await b.next());
}
assert.deepEqual(acks.map((x) => x.seq), [1, 1, 2]);
assert.equal(acks[0].fingerprint, acks[1].fingerprint);

const events = [await a.next(), await a.next()];
assert.deepEqual(events.map((x) => x.seq), [1, 2]);

b.ws.send(JSON.stringify({ op: "snapshot" }));
const snapshot = await b.next();
assert.equal(snapshot.op, "snapshot");
assert.equal(snapshot.channels.find((x) => x.channel === "#test")?.max_seq, 2);

a.ws.close();
b.ws.close();
console.log("PASS subscribed client received seqs 1,2");
console.log("PASS duplicate fingerprint acked seq 1 twice and emitted once");
console.log("PASS snapshot #test max_seq=2");
