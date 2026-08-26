/** ACP session/update → story / activity. Same pane split as desk and Comet. */

export function emptyTurn() {
  return { story: [], activity: [], running: false, error: "", title: "New session" };
}

export function paneOf(msg) {
  const meta = msg.params && msg.params._meta;
  if (meta && meta.desmos && meta.desmos.pane) return meta.desmos.pane;
  const kind = (((msg.params || {}).update) || {}).sessionUpdate;
  if (
    kind === "agent_thought_chunk" ||
    kind === "agent_message_chunk" ||
    kind === "user_message_chunk"
  )
    return "story";
  return "activity";
}

export function familyOf(update) {
  const meta = update && update._meta;
  if (meta && meta.desmos && meta.desmos.family) return meta.desmos.family;
  if (update.title === "complete") return "complete";
  if (update.title === "error") return "error";
  if (update.title === "compacted") return "compacted";
  if (update.title === "decision") return "decision";
  if (update.title === "pending") return "pending";
  if (update.title === "attached") return "attached";
  if (update.title === "stopped") return "stopped";
  if (update.title === "notice") return "notice";
  if (update.title === "model_rejected") return "model_rejected";
  if (update.title === "resumed") return "resumed";
  if (update.title === "guidance") return "guidance";
  if ((update.title || "").startsWith("edit")) return "edit";
  const kind = update.sessionUpdate;
  if (kind === "agent_thought_chunk") return "thinking";
  if (kind === "agent_message_chunk") return "speech";
  if (kind === "user_message_chunk") return "prompt";
  return "syscall";
}

export function labelOf(update) {
  const meta = update && update._meta;
  if (meta && meta.desmos && meta.desmos.label) return meta.desmos.label;
  return update.title || "tool";
}

function desmosOf(msg, update) {
  const nested = update && update._meta && update._meta.desmos;
  if (nested) return nested;
  const outer = msg && msg.params && msg.params._meta && msg.params._meta.desmos;
  return outer || {};
}

function cardText(update) {
  let body = "";
  for (const part of update.content || []) {
    if (part.type === "content" && part.content && part.content.text) body += part.content.text;
    else if (part.text) body += part.text;
  }
  return body;
}

function applySpans(text, spans) {
  if (!text || !spans || !spans.length) return text;
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  const raw = enc.encode(text);
  const chunks = [];
  let cursor = 0;
  for (const pair of spans) {
    const a = pair[0];
    const z = pair[1];
    if (typeof a !== "number" || typeof z !== "number" || z < a) continue;
    chunks.push(raw.subarray(cursor, a));
    cursor = z;
  }
  chunks.push(raw.subarray(cursor));
  let total = 0;
  for (const chunk of chunks) total += chunk.length;
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return dec.decode(out);
}

export function applyUpdate(turn, msg) {
  const update = (msg.params && msg.params.update) || {};
  const pane = paneOf(msg);
  const family = familyOf(update);
  const meta = desmosOf(msg, update);
  const kind = update.sessionUpdate;
  if (pane === "story") {
    if (kind === "agent_thought_chunk") {
      const chunk = (update.content && update.content.text) || "";
      const last = turn.story[turn.story.length - 1];
      if (last && last.kind === "thinking") last.text += chunk;
      else turn.story.push({ kind: "thinking", text: chunk });
    } else if (kind === "agent_message_chunk") {
      const chunk = (update.content && update.content.text) || "";
      const last = turn.story[turn.story.length - 1];
      if (last && last.kind === "assistant") last.text += chunk;
      else turn.story.push({ kind: "assistant", text: chunk });
    } else if (kind === "user_message_chunk") {
      const raw = (update.content && update.content.text) || "";
      const steered = raw.startsWith("[steer] ") ? raw.slice(8) : raw;
      if (family === "steer" || raw.startsWith("[steer] ")) {
        turn.story.push({ kind: "steer", text: steered });
      } else if (raw) {
        turn.story.push({ kind: "user", text: raw });
      }
    } else if (kind === "tool_call" || kind === "tool_call_update") {
      let card = turn.story.find((c) => c.kind === "subagent" && c.id === update.toolCallId);
      const chunk = cardText(update);
      if (!card) {
        turn.story.push({
          kind: "subagent",
          id: update.toolCallId,
          family,
          title: labelOf(update),
          status: update.status || "pending",
          text: chunk,
        });
      } else {
        if (update.status) card.status = update.status;
        if (chunk) card.text = (card.text || "") + (card.text ? "\n" : "") + chunk;
      }
    }
    return turn;
  }
  if (kind === "tool_call") {
    const body = cardText(update);
    turn.activity.push({
      id: update.toolCallId,
      family,
      title: labelOf(update),
      status: update.status || "pending",
      kind: update.kind || "",
      raw: update.rawInput || {},
      body,
      diff: null,
      n: meta.n,
      decisionId: meta.decisionId || meta.id,
      options: Array.isArray(meta.options) ? meta.options : [],
    });
    if ((family === "error" || family === "compacted" || family === "stopped") && body) {
      turn.story.push({ kind: "system", text: body, family });
    }
    return turn;
  }
  if (kind === "tool_call_update") {
    let card = turn.activity.find((c) => c.id === update.toolCallId);
    if (!card) {
      card = {
        id: update.toolCallId,
        family,
        title: labelOf(update),
        status: "pending",
        raw: {},
        body: "",
        diff: null,
      };
      turn.activity.push(card);
    }
    if (update.status) card.status = update.status;
    if (update.title) card.title = labelOf(update);
    if (update.kind) card.kind = update.kind;
    if (meta.n != null) card.n = meta.n;
    if (meta.decisionId || meta.id) card.decisionId = meta.decisionId || meta.id;
    if (Array.isArray(meta.options)) card.options = meta.options;
    const replace = meta.replace === true;
    for (const part of update.content || []) {
      if (part.type === "diff") {
        card.diff = { path: part.path, oldText: part.oldText, newText: part.newText };
      } else if (part.type === "content" && part.content && part.content.text) {
        if (replace) card.body = part.content.text;
        else card.body = (card.body || "") + part.content.text;
      } else if (part.text) {
        if (replace) card.body = part.text;
        else card.body = (card.body || "") + part.text;
      }
    }
    if (Array.isArray(meta.spans) && meta.spans.length) {
      for (let i = turn.story.length - 1; i >= 0; i--) {
        if (turn.story[i].kind === "assistant") {
          turn.story[i].text = applySpans(turn.story[i].text, meta.spans);
          break;
        }
      }
    }
  }
  return turn;
}

export function parseConfig(result) {
  const models = [];
  const efforts = [];
  let model = "";
  let effort = "";
  for (const opt of result.configOptions || []) {
    const values = (opt.options || []).map((o) => o.value).filter(Boolean);
    if (opt.id === "model" || opt.category === "model") {
      models.push(...values);
      if (opt.currentValue) model = opt.currentValue;
    }
    if (opt.id === "thought_level" || opt.category === "thought_level") {
      efforts.push(...values);
      if (opt.currentValue) effort = opt.currentValue;
    }
  }
  const modelsBlock = result.models || {};
  if (modelsBlock.currentModelId) model = modelsBlock.currentModelId;
  return { models, efforts, model, effort };
}

export function titleOf(turn) {
  if (!turn) return "New session";
  if (turn.title && turn.title !== "New session") return turn.title;
  const user = (turn.story || []).find((s) => s.kind === "user");
  if (user && user.text) return String(user.text).split("\n")[0].slice(0, 72);
  return "New session";
}

/** One turn whose story/activity must paint through gpuix markdown / diff. */
export function fixtureTurn() {
  return {
    story: [
      { kind: "user", text: "show strike and a one-token edit" },
      { kind: "thinking", text: "will strike ~~scratch~~ then speak" },
      {
        kind: "assistant",
        text: "keep ~~this~~ but not ~that~\n\nonly: ~**10%** (~**300**)\n",
      },
    ],
    activity: [
      {
        id: "c1",
        family: "complete",
        title: "complete",
        status: "completed",
        body: '{"n":1}',
        diff: null,
      },
      {
        id: "t1",
        family: "edit",
        title: "edit n.txt",
        status: "completed",
        body: "",
        diff: { path: "n.txt", oldText: "keep 1\n", newText: "keep 2\n" },
      },
    ],
    running: false,
    error: "",
    title: "probe",
  };
}
