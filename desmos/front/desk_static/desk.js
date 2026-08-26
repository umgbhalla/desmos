/* Desmos desk — ACP client. Story is speech/thinking. Activity is the wire. */
(function () {
  "use strict";

  const $ = (sel, el = document) => el.querySelector(sel);
  const md = (src) => window.DesmosMd.render(src);
  const esc = (s) => window.DesmosMd.esc(s);

  const state = {
    connected: false,
    error: "",
    cwd: "",
    sessions: [],
    active: null,
    model: "",
    effort: "",
    models: [],
    efforts: [],
    turns: {},
    draft: "",
    nextId: 1,
    filter: "",
    showSidebar: true,
    showActivity: true,
    actTab: "wire",
  };

  function turn(id) {
    if (!state.turns[id]) {
      state.turns[id] = {
        story: [],
        activity: [],
        running: false,
        error: "",
        title: "New session",
      };
    }
    return state.turns[id];
  }

  class Acp {
    constructor() {
      this.ws = null;
      this.pending = new Map();
      this.n = 1;
      this.onUpdate = () => {};
      this.onStatus = () => {};
    }
    url() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      return `${proto}://${location.host}/acp`;
    }
    connect() {
      if (this.ws && (this.ws.readyState === 0 || this.ws.readyState === 1)) return;
      const ws = new WebSocket(this.url());
      this.ws = ws;
      ws.onopen = () => this.onStatus(true, "");
      ws.onclose = () => {
        this.onStatus(false, "disconnected");
        for (const [, p] of this.pending) p.reject(new Error("disconnected"));
        this.pending.clear();
        setTimeout(() => this.connect(), 800);
      };
      ws.onerror = () => this.onStatus(false, "socket error");
      ws.onmessage = (ev) => {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.method === "desk/ping") return;
        if (msg.method === "session/update") {
          this.onUpdate(msg);
          return;
        }
        if (msg.id != null && this.pending.has(msg.id)) {
          const p = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          if (msg.error) p.reject(Object.assign(new Error(msg.error.message || "rpc"), { rpc: msg.error }));
          else p.resolve(msg.result);
        }
      };
    }
    call(method, params) {
      const id = this.n++;
      const payload = { jsonrpc: "2.0", id, method, params: params || {} };
      return new Promise((resolve, reject) => {
        this.pending.set(id, { resolve, reject });
        if (!this.ws || this.ws.readyState !== 1) {
          this.pending.delete(id);
          reject(new Error("not connected"));
          return;
        }
        this.ws.send(JSON.stringify(payload));
      });
    }
  }

  const acp = new Acp();

  function paneOf(msg) {
    const meta = msg.params && msg.params._meta;
    if (meta && meta.desmos && meta.desmos.pane) return meta.desmos.pane;
    const kind = (((msg.params || {}).update) || {}).sessionUpdate;
    if (kind === "agent_thought_chunk" || kind === "agent_message_chunk") return "story";
    return "activity";
  }

  function familyOf(update) {
    const meta = update && update._meta;
    if (meta && meta.desmos && meta.desmos.family) return meta.desmos.family;
    if (update.title === "complete") return "complete";
    if ((update.title || "").startsWith("edit")) return "edit";
    const kind = update.sessionUpdate;
    if (kind === "agent_thought_chunk") return "thinking";
    if (kind === "agent_message_chunk") return "speech";
    return "syscall";
  }

  function applyConfig(result) {
    const opts = (result && result.configOptions) || [];
    for (const opt of opts) {
      const values = (opt.options || []).map((o) => o.value);
      if (opt.category === "model" || opt.id === "model") {
        state.models = values;
        if (opt.currentValue) state.model = opt.currentValue;
      }
      if (opt.category === "thought_level" || opt.id === "thought_level") {
        state.efforts = values;
        if (opt.currentValue) state.effort = opt.currentValue;
      }
    }
    const models = result && result.models;
    if (models && models.currentModelId) state.model = models.currentModelId;
  }

  async function boot() {
    await acp.call("initialize", { protocolVersion: 1, clientInfo: { name: "desmos-desk", version: "1" } });
    await acp.call("authenticate", { methodId: "none" });
    await newSession();
  }

  async function newSession() {
    const result = await acp.call("session/new", { cwd: state.cwd || undefined });
    const id = result.sessionId;
    applyConfig(result);
    turn(id);
    state.sessions.unshift({ id, created: Date.now() });
    state.active = id;
    paint();
    return id;
  }

  async function send() {
    const id = state.active;
    if (!id) return;
    const text = state.draft.trim();
    if (!text) return;
    const t = turn(id);
    if (t.running) {
      await acp.call("_session/steer", { sessionId: id, text });
      t.story.push({ kind: "steer", text });
      state.draft = "";
      paint();
      return;
    }
    t.story.push({ kind: "user", text });
    if (t.title === "New session") t.title = text.split("\n")[0].slice(0, 72);
    t.running = true;
    t.error = "";
    state.draft = "";
    paint();
    try {
      const result = await acp.call("session/prompt", {
        sessionId: id,
        prompt: [{ type: "text", text }],
      });
      t.running = false;
      if (result && result.stopReason === "cancelled") t.error = "";
    } catch (err) {
      t.running = false;
      t.error = err.message || String(err);
    }
    paint();
    scrollStory(true);
  }

  async function cancel() {
    const id = state.active;
    if (!id) return;
    await acp.call("session/cancel", { sessionId: id });
  }

  async function setConfig(configId, value) {
    const id = state.active;
    if (!id) return;
    try {
      const result = await acp.call("session/set_config_option", {
        sessionId: id,
        configId,
        value,
      });
      applyConfig(result);
      state.error = "";
    } catch (err) {
      state.error = err.message || String(err);
    }
    paint();
  }

  function onUpdate(msg) {
    const sid = (msg.params || {}).sessionId;
    if (!sid) return;
    const update = msg.params.update || {};
    const t = turn(sid);
    const pane = paneOf(msg);
    const family = familyOf(update);
    const kind = update.sessionUpdate;
    if (pane === "story") {
      if (kind === "agent_thought_chunk") {
        const chunk = (update.content && update.content.text) || "";
        const last = t.story[t.story.length - 1];
        if (last && last.kind === "thinking") last.text += chunk;
        else t.story.push({ kind: "thinking", text: chunk, open: true });
      } else if (kind === "agent_message_chunk") {
        const chunk = (update.content && update.content.text) || "";
        const last = t.story[t.story.length - 1];
        if (last && last.kind === "assistant") last.text += chunk;
        else t.story.push({ kind: "assistant", text: chunk });
      }
    } else {
      if (kind === "tool_call") {
        t.activity.push({
          id: update.toolCallId,
          family,
          title: update.title || "tool",
          status: update.status || "pending",
          kind: update.kind || "",
          raw: update.rawInput || {},
          locations: update.locations || [],
          body: "",
          diff: null,
          open: family === "complete" || family === "edit" ? true : false,
        });
      } else if (kind === "tool_call_update") {
        let card = t.activity.find((c) => c.id === update.toolCallId);
        if (!card) {
          card = {
            id: update.toolCallId,
            family,
            title: update.title || "tool",
            status: "pending",
            raw: {},
            body: "",
            diff: null,
            open: true,
          };
          t.activity.push(card);
        }
        if (update.status) card.status = update.status;
        if (update.title) card.title = update.title;
        if (update.kind) card.kind = update.kind;
        const parts = update.content || [];
        for (const part of parts) {
          if (part.type === "diff") {
            card.diff = { path: part.path, oldText: part.oldText, newText: part.newText };
            card.open = true;
          } else if (part.type === "content" && part.content && part.content.text) {
            card.body = (card.body || "") + part.content.text;
          } else if (part.text) {
            card.body = (card.body || "") + part.text;
          }
        }
      }
    }
    paint();
    scrollStory(true);
  }

  function scrollStory(ifNear) {
    const el = $("#story");
    if (!el) return;
    const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (!ifNear || gap < 80) el.scrollTop = el.scrollHeight;
  }

  function icon(name) {
    const paths = {
      plus: '<path d="M12 5v14M5 12h14"/>',
      panel: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M9 5v14"/>',
      wire: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M15 5v14"/>',
      send: '<path d="M12 19V5M6 11l6-6 6 6"/>',
      stop: '<rect x="7" y="7" width="10" height="10" rx="1"/>',
    };
    return `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">${paths[name] || ""}</svg>`;
  }

  function sessionMeta(id) {
    const row = state.sessions.find((s) => s.id === id);
    const t = turn(id);
    return {
      title: t.title,
      preview: t.running ? "running" : t.story.length ? "idle" : "empty",
      busy: t.running,
      created: row ? row.created : 0,
    };
  }

  function paint() {
    const app = $("#app");
    app.classList.toggle("no-sidebar", !state.showSidebar);
    app.classList.toggle("no-activity", !state.showActivity);

    const t = state.active ? turn(state.active) : null;
    $("#sidebar").innerHTML = `
      <div class="brand"><div class="mark"></div><div><div class="name">Desmos</div><div class="sub">kernel over ACP</div></div></div>
      <div class="side-actions">
        <button class="side-btn" data-act="new">${icon("plus")} New session <kbd>N</kbd></button>
      </div>
      <div class="conv-label">Sessions</div>
      <div class="convs" id="convs"></div>
    `;
    const convs = $("#convs");
    if (!state.sessions.length) {
      convs.innerHTML = `<div class="act-empty">No sessions yet.</div>`;
    } else {
      for (const row of state.sessions) {
        const meta = sessionMeta(row.id);
        const el = document.createElement("div");
        el.className = "conv" + (row.id === state.active ? " on" : "") + (meta.busy ? " busy" : "");
        el.dataset.id = row.id;
        el.innerHTML = `<div style="display:flex;gap:8px"><span class="dot"></span><div class="t">${esc(
          meta.title
        )}</div></div><div class="m"><span>${esc(meta.preview)}</span></div>`;
        el.onclick = () => {
          state.active = row.id;
          paint();
        };
        convs.appendChild(el);
      }
    }
    $("[data-act=new]").onclick = () => newSession().catch((e) => ((state.error = e.message), paint()));

    const title = t ? t.title : "Desmos";
    $("#titlebar").innerHTML = `
      <button class="icon-btn" data-act="toggle-side" title="Sessions">${icon("panel")}</button>
      <div class="title">${esc(title)}</div>
      <div class="pickers">
        <select class="chip" id="model">${state.models
          .map((m) => `<option value="${esc(m)}"${m === state.model ? " selected" : ""}>${esc(m)}</option>`)
          .join("")}</select>
        <select class="chip" id="effort">${state.efforts
          .map((e) => `<option value="${esc(e)}"${e === state.effort ? " selected" : ""}>${esc(e)}</option>`)
          .join("")}</select>
      </div>
      <button class="icon-btn" data-act="toggle-act" title="Activity">${icon("wire")}</button>
    `;
    $("[data-act=toggle-side]").onclick = () => {
      state.showSidebar = !state.showSidebar;
      paint();
    };
    $("[data-act=toggle-act]").onclick = () => {
      state.showActivity = !state.showActivity;
      paint();
    };
    $("#model").onchange = (e) => setConfig("model", e.target.value);
    $("#effort").onchange = (e) => setConfig("thought_level", e.target.value);

    const story = $("#story");
    if (!t || (!t.story.length && !t.running)) {
      story.innerHTML = `
        <div class="empty">
          <h2>Story stays speech.</h2>
          <p>Prompts, thinking, and markdown land here. Syscalls, complete() POSTs, and edit diffs stay on Activity — the wire is visible, never restated.</p>
          <p class="hint">Enter to send · Shift+Enter for a newline · N for a new session · Esc to cancel</p>
        </div>`;
    } else {
      const inner = document.createElement("div");
      inner.className = "story-inner";
      for (const item of t.story) {
        const wrap = document.createElement("div");
        wrap.className = "turn " + item.kind;
        if (item.kind === "user") wrap.innerHTML = `<div class="user">${esc(item.text)}</div>`;
        else if (item.kind === "steer")
          wrap.innerHTML = `<div class="thinking"><div class="lab">steer queued</div><pre>${esc(item.text)}</pre></div>`;
        else if (item.kind === "thinking") {
          wrap.innerHTML = `<div class="thinking"><div class="lab">thinking</div><pre></pre></div>`;
          wrap.querySelector("pre").textContent = item.text;
        } else if (item.kind === "assistant") {
          wrap.innerHTML = `<div class="assistant"><div class="md">${md(item.text)}</div></div>`;
        }
        inner.appendChild(wrap);
      }
      story.innerHTML = "";
      story.appendChild(inner);
    }

    const running = !!(t && t.running);
    const canSend = state.connected && !!state.draft.trim();
    const drafting = document.activeElement && document.activeElement.id === "draft";
    const selStart = drafting ? document.activeElement.selectionStart : state.draft.length;
    const selEnd = drafting ? document.activeElement.selectionEnd : state.draft.length;
    $("#composer-wrap").innerHTML = `
      ${state.error || (t && t.error) ? `<div class="banner">${esc(state.error || t.error)}</div>` : ""}
      <div class="composer">
        <textarea id="draft" placeholder="Ask Desmos…" rows="2"></textarea>
        <div class="composer-bar">
          <div class="grow">${running ? "running — Enter steers the next turn" : "markdown in · syscalls on the right"}</div>
          <button class="send${running ? " stop" : ""}" id="go" ${!running && !canSend ? "disabled" : ""} title="${
      running ? "Stop" : "Send"
    }">${running ? icon("stop") : icon("send")}</button>
        </div>
      </div>
    `;
    const draft = $("#draft");
    draft.value = state.draft;
    draft.oninput = () => {
      state.draft = draft.value;
      const go = $("#go");
      if (!turn(state.active).running) go.disabled = !state.draft.trim();
      draft.style.height = "44px";
      draft.style.height = Math.min(180, draft.scrollHeight) + "px";
    };
    draft.onkeydown = (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    };
    $("#go").onclick = () => (running ? cancel() : send());
    if (drafting) {
      draft.focus();
      draft.setSelectionRange(selStart, selEnd);
    }
    draft.style.height = "44px";
    draft.style.height = Math.min(180, draft.scrollHeight) + "px";

    const cards = t ? t.activity : [];
    const shown =
      state.actTab === "post"
        ? cards.filter((c) => c.family === "complete")
        : state.actTab === "edits"
          ? cards.filter((c) => c.family === "edit")
          : cards;
    $("#activity").innerHTML = `
      <div class="act-head">Activity
        <div class="act-tabs">
          <button data-tab="wire" class="${state.actTab === "wire" ? "on" : ""}">wire</button>
          <button data-tab="post" class="${state.actTab === "post" ? "on" : ""}">complete</button>
          <button data-tab="edits" class="${state.actTab === "edits" ? "on" : ""}">edits</button>
        </div>
      </div>
      <div class="act-body" id="act-body"></div>
    `;
    $("#activity").querySelectorAll("[data-tab]").forEach((btn) => {
      btn.onclick = () => {
        state.actTab = btn.dataset.tab;
        paint();
      };
    });
    const body = $("#act-body");
    if (!shown.length) {
      body.innerHTML = `<div class="act-empty">${
        state.actTab === "post"
          ? "complete() POSTs land here — model, usage, span count. The request body is redacted."
          : state.actTab === "edits"
            ? "workspace edits stream as diffs, not as story."
            : "Syscalls, complete cards, and edits. Nothing here is mirrored into the story."
      }</div>`;
    } else {
      for (const card of shown) {
        const el = document.createElement("div");
        el.className = `card ${card.family}${card.open ? "" : " folded"}`;
        const st =
          card.status === "completed" ? "done" : card.status === "pending" || card.status === "in_progress" ? "run" : "fail";
        let inner = "";
        if (card.diff) {
          inner += `<div class="path" style="color:var(--tertiary);font-size:11px;margin-bottom:6px">${esc(
            card.diff.path || ""
          )}</div>`;
          inner += window.DesmosMd.diffHtml(card.diff.oldText, card.diff.newText);
        }
        if (card.body) inner += `<div class="txt">${esc(card.body)}</div>`;
        if (!inner && card.raw && Object.keys(card.raw).length)
          inner = `<div class="txt">${esc(JSON.stringify(card.raw, null, 2))}</div>`;
        el.innerHTML = `<div class="hd"><span class="fam">${esc(card.family)}</span><span class="name">${esc(
          card.title
        )}</span><span class="st ${st}">${esc(card.status || "")}</span></div><div class="bd">${inner || ""}</div>`;
        el.querySelector(".hd").onclick = () => {
          card.open = !card.open;
          paint();
        };
        body.appendChild(el);
      }
    }

    $("#status").innerHTML = `
      <span class="dot-live${state.connected ? "" : " off"}"></span>
      <span>${state.connected ? "acp" : "offline"}</span>
      <span>${esc(state.model || "—")}</span>
      <span>${esc(state.effort || "—")}</span>
      <span class="spin${t && t.running ? " on" : ""}"></span>
      <span style="margin-left:auto">${esc(state.cwd || "")}</span>
    `;
  }

  let booted = false;
  acp.onUpdate = onUpdate;
  acp.onStatus = (ok, err) => {
    state.connected = ok;
    if (!ok) state.error = err;
    else if (state.error === "disconnected" || state.error === "socket error") state.error = "";
    paint();
    if (ok && !booted) {
      booted = true;
      boot().catch((e) => ((state.error = e.message), paint()));
    }
  };

  document.addEventListener("keydown", (e) => {
    if (e.target && (e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT" || e.target.tagName === "INPUT")) {
      if (e.key === "Escape") {
        e.preventDefault();
        cancel();
      }
      return;
    }
    if (e.key === "n" || e.key === "N") {
      e.preventDefault();
      newSession();
    }
    if (e.key === "Escape") cancel();
  });

  fetch("/health")
    .then((r) => r.json())
    .then((h) => {
      state.cwd = h.cwd || "";
      paint();
    })
    .catch(() => {});

  paint();
  acp.connect();
})();
