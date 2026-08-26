import { mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AcpClient } from "./acp.js";
import { applyUpdate, emptyTurn, fixtureTurn } from "./normalize.js";

function walk(node, acc = []) {
  if (!node) return acc;
  acc.push(node);
  for (const child of node.children || []) walk(child, acc);
  return acc;
}

function parseArgs(argv) {
  const out = { tree: false, acp: false, cwd: process.env.DESMOS_CWD || "" };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--tree") out.tree = true;
    else if (argv[i] === "--acp") out.acp = true;
    else if (argv[i] === "--cwd" && argv[i + 1]) {
      out.cwd = argv[++i];
    }
  }
  if (!out.tree && !out.acp) {
    out.tree = true;
    out.acp = true;
  }
  return out;
}

function emit(payload) {
  process.stdout.write(JSON.stringify(payload) + "\n");
}

async function probeAcp(cwd) {
  const dir = cwd || mkdtempSync(join(tmpdir(), "desmos-gpuix-"));
  mkdirSync(dir, { recursive: true });
  const acp = new AcpClient({ cwd: dir });
  try {
    const init = await acp.call("initialize", {
      protocolVersion: 1,
      clientInfo: { name: "desmos-gpuix", version: "1" },
    });
    await acp.call("authenticate", { methodId: "none" });
    const created = await acp.call("session/new", { cwd: dir });
    const turn = emptyTurn();
    const thought = {
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: created.sessionId,
        _meta: { desmos: { pane: "story", family: "thinking" } },
        update: {
          sessionUpdate: "agent_thought_chunk",
          content: { type: "text", text: "plan" },
        },
      },
    };
    const speech = {
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId: created.sessionId,
        _meta: { desmos: { pane: "story", family: "speech" } },
        update: {
          sessionUpdate: "agent_message_chunk",
          content: { type: "text", text: "keep ~~this~~" },
        },
      },
    };
    applyUpdate(turn, thought);
    applyUpdate(turn, speech);
    return {
      protocolVersion: init.protocolVersion,
      sessionId: created.sessionId,
      loadSession: init.agentCapabilities && init.agentCapabilities.loadSession,
      storyKinds: turn.story.map((s) => s.kind),
    };
  } finally {
    acp.close();
  }
}

async function probeTree() {
  const React = (await import("react")).default;
  const { GpuixRenderer } = await import("@gpuix/native");
  const { createRoot, flushSync } = await import("@gpuix/react");
  const { App } = await import("./app.js");
  const renderer = new GpuixRenderer();
  renderer.init({ title: "desmos-gpuix-probe", width: 960, height: 720 });
  const root = createRoot(renderer);
  const turn = process.env.DESMOS_GPUIX_TURN
    ? JSON.parse(process.env.DESMOS_GPUIX_TURN)
    : fixtureTurn();
  flushSync(() => {
    root.render(React.createElement(App, { turn, draft: "", status: "probe" }));
  });
  const tree = JSON.parse(renderer.getAutomationTree() || "{}");
  const nodes = walk(tree);
  const markdown = nodes.filter((n) => n.type === "markdown");
  const diffs = nodes.filter((n) => n.type === "diff");
  return {
    window: renderer.getWindowSize(),
    markdown: markdown.map((n) => (n.customProps && n.customProps.source) || ""),
    diffs: diffs.map((n) => n.customProps || {}),
  };
}

const args = parseArgs(process.argv.slice(2));

try {
  const out = {};
  if (args.acp) out.acp = await probeAcp(args.cwd);
  if (args.tree) out.tree = await probeTree();
  out.ok = true;
  emit(out);
  process.exit(0);
} catch (err) {
  emit({ ok: false, error: String(err && err.stack ? err.stack : err) });
  process.exit(1);
}
