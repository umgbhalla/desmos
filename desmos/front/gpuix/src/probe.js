import { mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AcpClient } from "./acp.js";
import {
  applyUpdate,
  emptyTurn,
  familyOf,
  fixtureTurn,
  paneOf,
  parseConfig,
  titleOf,
} from "./normalize.js";

function walk(node, acc = []) {
  if (!node) return acc;
  acc.push(node);
  for (const child of node.children || []) walk(child, acc);
  return acc;
}

function parseArgs(argv) {
  const out = { tree: false, acp: false, chat: false, cwd: process.env.DESMOS_CWD || "" };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--tree") out.tree = true;
    else if (argv[i] === "--acp") out.acp = true;
    else if (argv[i] === "--chat") out.chat = true;
    else if (argv[i] === "--cwd" && argv[i + 1]) {
      out.cwd = argv[++i];
    }
  }
  if (!out.tree && !out.acp && !out.chat) {
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
    applyUpdate(turn, {
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
    });
    applyUpdate(turn, {
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
    });
    return {
      protocolVersion: init.protocolVersion,
      sessionId: created.sessionId,
      loadSession: init.agentCapabilities && init.agentCapabilities.loadSession,
      storyKinds: turn.story.map((s) => s.kind),
      config: parseConfig(created),
    };
  } finally {
    acp.close();
  }
}

async function mount(turn, extra = {}) {
  const React = (await import("react")).default;
  const { GpuixRenderer } = await import("@gpuix/native");
  const { createRoot, flushSync } = await import("@gpuix/react");
  const { App } = await import("./app.js");
  const renderer = new GpuixRenderer();
  renderer.init({ title: "desmos-gpuix-probe", width: 960, height: 720 });
  const root = createRoot(renderer);
  flushSync(() => {
    root.render(
      React.createElement(App, {
        turn,
        draft: extra.draft || "",
        status: extra.status || "probe",
        sessions: extra.sessions || [
          { id: "s1", title: titleOf(turn) },
        ],
        activeId: extra.activeId || "s1",
        models: extra.models || ["claude-opus-5"],
        model: extra.model || "claude-opus-5",
        efforts: extra.efforts || ["low", "high"],
        effort: extra.effort || "low",
        running: Boolean(extra.running),
        showActivity: extra.showActivity !== false,
      }),
    );
  });
  const tree = JSON.parse(renderer.getAutomationTree() || "{}");
  const nodes = walk(tree);
  return {
    window: renderer.getWindowSize(),
    types: [...new Set(nodes.map((n) => n.type).filter(Boolean))],
    markdown: nodes
      .filter((n) => n.type === "markdown")
      .map((n) => (n.customProps && n.customProps.source) || ""),
    diffs: nodes.filter((n) => n.type === "diff").map((n) => n.customProps || {}),
    text: nodes.map((n) => n.text).filter(Boolean),
  };
}

async function probeTree() {
  const turn = process.env.DESMOS_GPUIX_TURN
    ? JSON.parse(process.env.DESMOS_GPUIX_TURN)
    : fixtureTurn();
  return mount(turn);
}

async function probeChat(cwd) {
  const dir = cwd || mkdtempSync(join(tmpdir(), "desmos-gpuix-"));
  mkdirSync(dir, { recursive: true });
  const acp = new AcpClient({ cwd: dir });
  const turn = emptyTurn();
  const panes = [];
  acp.onUpdate = (msg) => {
    const update = (msg.params && msg.params.update) || {};
    panes.push({
      pane: paneOf(msg),
      family: familyOf(update),
      kind: update.sessionUpdate,
    });
    applyUpdate(turn, msg);
  };
  try {
    await acp.call("initialize", {
      protocolVersion: 1,
      clientInfo: { name: "desmos-gpuix", version: "1" },
    });
    await acp.call("authenticate", { methodId: "none" });
    const created = await acp.call("session/new", { cwd: dir });
    const cfg = parseConfig(created);
    const prompt = process.env.DESMOS_GPUIX_PROMPT || "say ping";
    turn.story.push({ kind: "user", text: prompt });
    turn.title = prompt;
    const prompted = await acp.call(
      "session/prompt",
      { sessionId: created.sessionId, prompt: [{ type: "text", text: prompt }] },
      60000,
    );
    const second = await acp.call("session/new", { cwd: dir });
    const mounted = await mount(turn, {
      sessions: [
        { id: created.sessionId, title: titleOf(turn) },
        { id: second.sessionId, title: "New session" },
      ],
      activeId: created.sessionId,
      models: cfg.models,
      model: cfg.model,
      efforts: cfg.efforts,
      effort: cfg.effort,
      status: prompted.stopReason || "idle",
    });
    const speech = turn.story
      .filter((s) => s.kind === "assistant")
      .map((s) => s.text)
      .join("");
    return {
      stopReason: prompted.stopReason,
      sessionId: created.sessionId,
      secondId: second.sessionId,
      storyKinds: turn.story.map((s) => s.kind),
      activityFamilies: turn.activity.map((c) => c.family),
      panes,
      speech,
      ...mounted,
    };
  } finally {
    acp.close();
  }
}

const args = parseArgs(process.argv.slice(2));

try {
  const out = {};
  if (args.acp) out.acp = await probeAcp(args.cwd);
  if (args.tree) out.tree = await probeTree();
  if (args.chat) out.chat = await probeChat(args.cwd);
  out.ok = true;
  emit(out);
  process.exit(0);
} catch (err) {
  emit({ ok: false, error: String(err && err.stack ? err.stack : err) });
  process.exit(1);
}
