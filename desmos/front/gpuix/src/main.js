import React from "react";
import { GpuixRenderer } from "@gpuix/native";
import { createRoot, flushSync } from "@gpuix/react";
import { AcpClient } from "./acp.js";
import { App } from "./app.js";
import { applyUpdate, emptyTurn } from "./normalize.js";

const cwd = process.env.DESMOS_CWD || process.cwd();

const acp = new AcpClient({ cwd });
const state = {
  turn: emptyTurn(),
  sessionId: "",
  draft: "",
  status: "connecting",
};

const renderer = new GpuixRenderer();
renderer.init({ title: "Desmos", width: 1280, height: 800 });
const root = createRoot(renderer);

function paint() {
  flushSync(() => {
    root.render(
      React.createElement(App, {
        turn: state.turn,
        draft: state.draft,
        status: state.status,
        onDraft: (value) => {
          state.draft = value;
          paint();
        },
        onSubmit: () => {
          void send();
        },
      }),
    );
  });
}

async function send() {
  const text = String(state.draft || "").trim();
  if (!text || !state.sessionId) return;
  state.draft = "";
  state.turn.story.push({ kind: "user", text });
  state.turn.running = true;
  state.status = "prompting";
  paint();
  try {
    const result = await acp.call(
      "session/prompt",
      { sessionId: state.sessionId, prompt: [{ type: "text", text }] },
      600000,
    );
    state.status = (result && result.stopReason) || "idle";
  } catch (err) {
    state.status = String(err.message || err);
    state.turn.error = state.status;
  }
  state.turn.running = false;
  paint();
}

acp.onUpdate = (msg) => {
  const sid = msg.params && msg.params.sessionId;
  if (sid && state.sessionId && sid !== state.sessionId) return;
  applyUpdate(state.turn, msg);
  paint();
};

paint();

try {
  const init = await acp.call("initialize", {
    protocolVersion: 1,
    clientInfo: { name: "desmos-gpuix", version: "1" },
  });
  await acp.call("authenticate", { methodId: "none" });
  const created = await acp.call("session/new", { cwd });
  state.sessionId = created.sessionId;
  state.status = `session ${state.sessionId.slice(0, 8)}  protocol ${init.protocolVersion}`;
  paint();
} catch (err) {
  state.status = String(err.message || err);
  paint();
}
