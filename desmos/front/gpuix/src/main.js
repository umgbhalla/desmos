import React from "react";
import { GpuixRenderer } from "@gpuix/native";
import { createRoot, flushSync } from "@gpuix/react";
import { AcpClient } from "./acp.js";
import { App } from "./app.js";
import { applyUpdate, emptyTurn, parseConfig, titleOf } from "./normalize.js";

const cwd = process.env.DESMOS_CWD || process.cwd();

const acp = new AcpClient({ cwd });
const state = {
  turns: {},
  sessions: [],
  activeId: "",
  draft: "",
  status: "connecting",
  model: "",
  models: [],
  effort: "",
  efforts: [],
  showActivity: true,
};

function turn(id) {
  if (!id) return emptyTurn();
  if (!state.turns[id]) state.turns[id] = emptyTurn();
  return state.turns[id];
}

const renderer = new GpuixRenderer();
renderer.init({ title: "Desmos", width: 1280, height: 800 });
const root = createRoot(renderer);

function paint() {
  const id = state.activeId;
  const t = turn(id);
  flushSync(() => {
    root.render(
      React.createElement(App, {
        sessions: state.sessions,
        activeId: id,
        turn: t,
        models: state.models,
        model: state.model,
        efforts: state.efforts,
        effort: state.effort,
        draft: state.draft,
        status: state.status,
        running: Boolean(t.running),
        showActivity: state.showActivity,
        onNew: () => {
          void newSession();
        },
        onSelect: (sid) => {
          state.activeId = sid;
          paint();
        },
        onDraft: (value) => {
          state.draft = value;
          paint();
        },
        onSubmit: () => {
          void send();
        },
        onCancel: () => {
          void cancel();
        },
        onModel: (value) => {
          void setConfig("model", value);
        },
        onEffort: (value) => {
          void setConfig("thought_level", value);
        },
        onToggleActivity: () => {
          state.showActivity = !state.showActivity;
          paint();
        },
        onDecide: (decisionId, option) => {
          void sendDecide(decisionId, option);
        },
      }),
    );
  });
}

function applyCfg(result) {
  const cfg = parseConfig(result || {});
  if (cfg.models.length) state.models = cfg.models;
  if (cfg.efforts.length) state.efforts = cfg.efforts;
  if (cfg.model) state.model = cfg.model;
  if (cfg.effort) state.effort = cfg.effort;
}

async function newSession() {
  const result = await acp.call("session/new", { cwd });
  applyCfg(result);
  const id = result.sessionId;
  const t = turn(id);
  t.persistId = ((result._meta || {}).desmos || {}).persistSessionId || "";
  state.sessions = [
    { id, title: "New session", persistId: t.persistId },
    ...state.sessions.filter((s) => s.id !== id),
  ];
  state.activeId = id;
  state.status = `session ${id.slice(0, 8)}`;
  paint();
  return id;
}

async function send() {
  const id = state.activeId;
  if (!id) return;
  const text = String(state.draft || "").trim();
  if (!text) return;
  const t = turn(id);
  if (t.running) {
    await acp.call("_session/steering", {
      sessionId: id,
      prompt: [{ type: "text", text }],
      _meta: { steering: { idleBehavior: "promptRequired" } },
    });
    t.story.push({ kind: "steer", text });
    state.draft = "";
    paint();
    return;
  }
  t.story.push({ kind: "user", text });
  t.title = titleOf(t);
  const row = state.sessions.find((s) => s.id === id);
  if (row) row.title = t.title;
  t.running = true;
  t.error = "";
  state.draft = "";
  state.status = "prompting";
  paint();
  try {
    const result = await acp.call(
      "session/prompt",
      { sessionId: id, prompt: [{ type: "text", text }] },
      600000,
    );
    t.running = false;
    state.status = (result && result.stopReason) || "idle";
  } catch (err) {
    t.running = false;
    t.error = String(err.message || err);
    state.status = t.error;
  }
  paint();
}

async function sendDecide(decisionId, option) {
  const id = state.activeId;
  if (!id || !decisionId) return;
  const text = `decide:${decisionId}: ${option}`;
  const t = turn(id);
  t.story.push({ kind: "user", text });
  t.running = true;
  t.error = "";
  state.status = "prompting";
  paint();
  try {
    const result = await acp.call(
      "session/prompt",
      { sessionId: id, prompt: [{ type: "text", text }] },
      600000,
    );
    t.running = false;
    state.status = (result && result.stopReason) || "idle";
  } catch (err) {
    t.running = false;
    t.error = String(err.message || err);
    state.status = t.error;
  }
  paint();
}

async function cancel() {
  const id = state.activeId;
  if (!id) return;
  await acp.call("session/cancel", { sessionId: id });
}

async function setConfig(configId, value) {
  const id = state.activeId;
  if (!id || !value) return;
  try {
    const result = await acp.call("session/set_config_option", {
      sessionId: id,
      configId,
      value,
    });
    applyCfg(result);
    if (configId === "model") state.model = value;
    if (configId === "thought_level") state.effort = value;
  } catch (err) {
    state.status = String(err.message || err);
  }
  paint();
}

acp.onUpdate = (msg) => {
  const sid = msg.params && msg.params.sessionId;
  if (!sid) return;
  applyUpdate(turn(sid), msg);
  if (sid === state.activeId) paint();
};

paint();

try {
  const init = await acp.call("initialize", {
    protocolVersion: 1,
    clientInfo: { name: "desmos-gpuix", version: "1" },
  });
  await acp.call("authenticate", { methodId: "none" });
  await newSession();
  state.status = `session ${state.activeId.slice(0, 8)}  protocol ${init.protocolVersion}`;
  paint();
} catch (err) {
  state.status = String(err.message || err);
  paint();
}
