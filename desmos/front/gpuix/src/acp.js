import { spawn } from "node:child_process";
import readline from "node:readline";

/** NDJSON JSON-RPC 2.0 client for `python -m desmos acp`. Not Content-Length. */
export class AcpClient {
  constructor({ command, args, cwd, env } = {}) {
    this.pending = new Map();
    this.n = 1;
    this.onUpdate = () => {};
    const cmd = command || process.env.DESMOS_PYTHON || process.env.PYTHON || "python3";
    const argv = args && args.length ? args : ["-m", "desmos", "acp"];
    this.child = spawn(cmd, argv, {
      cwd: cwd || process.env.DESMOS_CWD || process.cwd(),
      env: { ...process.env, ...(env || {}) },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child.on("error", (err) => {
      for (const [, p] of this.pending) p.reject(err);
      this.pending.clear();
    });
    this.child.stderr.on("data", (buf) => process.stderr.write(buf));
    const rl = readline.createInterface({ input: this.child.stdout });
    rl.on("line", (line) => this._onLine(line));
  }

  _onLine(line) {
    if (!line.trim()) return;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      return;
    }
    if (msg.method === "session/update") {
      this.onUpdate(msg);
      return;
    }
    if (msg.id != null && this.pending.has(msg.id)) {
      const p = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) {
        p.reject(Object.assign(new Error(msg.error.message || "rpc"), { rpc: msg.error }));
      } else {
        p.resolve(msg.result);
      }
    }
  }

  call(method, params, timeoutMs = 15000) {
    const id = this.n++;
    const payload = JSON.stringify({ jsonrpc: "2.0", id, method, params: params || {} }) + "\n";
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`timeout ${method}`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(timer);
          reject(e);
        },
      });
      if (!this.child.stdin.writable) {
        this.pending.delete(id);
        clearTimeout(timer);
        reject(new Error("acp stdin closed"));
        return;
      }
      this.child.stdin.write(payload);
    });
  }

  close() {
    try {
      this.child.stdin.end();
    } catch {
      /* already gone */
    }
    this.child.kill();
  }
}
