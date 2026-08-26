/** ACP session/update → story / activity. Same pane split as desk and Comet. */

export function emptyTurn() {
  return { story: [], activity: [], running: false, error: "", title: "New session" };
}

export function paneOf(msg) {
  const meta = msg.params && msg.params._meta;
  if (meta && meta.desmos && meta.desmos.pane) return meta.desmos.pane;
  const kind = (((msg.params || {}).update) || {}).sessionUpdate;
  if (kind === "agent_thought_chunk" || kind === "agent_message_chunk") return "story";
  return "activity";
}

export function familyOf(update) {
  const meta = update && update._meta;
  if (meta && meta.desmos && meta.desmos.family) return meta.desmos.family;
  if (update.title === "complete") return "complete";
  if ((update.title || "").startsWith("edit")) return "edit";
  const kind = update.sessionUpdate;
  if (kind === "agent_thought_chunk") return "thinking";
  if (kind === "agent_message_chunk") return "speech";
  return "syscall";
}

export function labelOf(update) {
  const meta = update && update._meta;
  if (meta && meta.desmos && meta.desmos.label) return meta.desmos.label;
  return update.title || "tool";
}

export function applyUpdate(turn, msg) {
  const update = (msg.params && msg.params.update) || {};
  const pane = paneOf(msg);
  const family = familyOf(update);
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
    }
    return turn;
  }
  if (kind === "tool_call") {
    turn.activity.push({
      id: update.toolCallId,
      family,
      title: labelOf(update),
      status: update.status || "pending",
      kind: update.kind || "",
      raw: update.rawInput || {},
      body: "",
      diff: null,
    });
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
    for (const part of update.content || []) {
      if (part.type === "diff") {
        card.diff = { path: part.path, oldText: part.oldText, newText: part.newText };
      } else if (part.type === "content" && part.content && part.content.text) {
        card.body = (card.body || "") + part.content.text;
      } else if (part.text) {
        card.body = (card.body || "") + part.text;
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
