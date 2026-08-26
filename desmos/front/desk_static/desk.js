/* Desmos desk — ACP client. Story is speech/thinking. Activity is the wire. */
(function () {
  "use strict";

  const $ = (sel, el = document) => el.querySelector(sel);
  const md = (src) => window.DesmosMd.render(src);
  const esc = (s) => window.DesmosMd.esc(s);

  const ACT_TABS = ["wire", "post", "edits", "git", "files", "channel", "term"];

  const state = {
    connected: false,
    error: "",
    cwd: "",
    sessions: [],
    persist: [],
    persistId: "",
    peers: [],
    channels: [],
    agents: [],
    channel: "general",
    channelStory: { messages: [], participants: [], activity: [], unread: 0 },
    git: { branch: "", status: [], branches: [], log: [], dirty: 0, error: null, read: false },
    gitTab: "status",
    files: { dir: ".", entries: [], path: null, lines: [], note: null, binary: false },
    bridge: { socket: null, attached: false, reason: "" },
    term: { name: "main", text: "", raw: "", shells: [], draft: "", line: "", seq: 0 },
    help: false,
    active: null,
    model: "",
    effort: "",
    models: [],
    efforts: [],
    turns: {},
    draft: "",
    channelDraft: "",
    nextId: 1,
    filter: "",
    showSidebar: true,
    showActivity: true,
    railTab: "sessions",
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
        persistId: "",
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

  function labelOf(update) {
    const meta = update && update._meta;
    if (meta && meta.desmos && meta.desmos.label) return meta.desmos.label;
    return update.title || "tool";
  }

  function persistMeta(result) {
    const meta = (result && result._meta && result._meta.desmos) || {};
    if (meta.persistSessionId) state.persistId = meta.persistSessionId;
    return meta;
  }

  function applyConfig(result) {
    persistMeta(result);
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
    const init = await acp.call("initialize", { protocolVersion: 1, clientInfo: { name: "desmos-desk", version: "1" } });
    const meta = (init && init._meta) || {};
    const models = meta.modelState || {};
    if (models.currentModelId) state.model = models.currentModelId;
    if (models.availableModels && models.availableModels.length) {
      state.models = models.availableModels.map((row) => row.modelId || row.name).filter(Boolean);
    }
    await acp.call("authenticate", { methodId: "none" });
    await newSession();
    await refreshEnv();
  }

  async function newSession() {
    const result = await acp.call("session/new", { cwd: state.cwd || undefined });
    const id = result.sessionId;
    applyConfig(result);
    const t = turn(id);
    t.persistId = ((result._meta || {}).desmos || {}).persistSessionId || "";
    state.sessions.unshift({ id, created: Date.now(), persistId: t.persistId });
    state.active = id;
    paint();
    refreshEnv();
    return id;
  }

  function applyLoadedStory(id, result) {
    const t = turn(id);
    t.story = [];
    const story = result.story || [];
    if (story.length) {
      for (const item of story) t.story.push({ kind: item.kind, text: item.text || "", open: false });
    } else {
      for (const row of result.turns || []) {
        if (row.prompt) t.story.push({ kind: "user", text: row.prompt });
        if (row.speech) t.story.push({ kind: "assistant", text: row.speech });
      }
    }
    const first = t.story.find((s) => s.kind === "user");
    t.title = first ? first.text.split("\n")[0].slice(0, 72) : "Resumed session";
    t.persistId = ((result._meta || {}).desmos || {}).persistSessionId || id;
  }

  async function loadPersist(persistId) {
    const existing = state.sessions.find((s) => s.id === persistId || s.persistId === persistId);
    if (existing && turn(existing.id).story.length) {
      state.active = existing.id;
      paint();
      return;
    }
    const result = await acp.call("session/load", { cwd: state.cwd || undefined, sessionId: persistId });
    applyConfig(result);
    const id = result.sessionId;
    applyLoadedStory(id, result);
    if (!state.sessions.some((s) => s.id === id)) {
      state.sessions.unshift({ id, created: Date.now(), persistId: persistId, resumed: true });
    }
    state.active = id;
    paint();
    refreshEnv();
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
    refreshEnv();
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

  async function refreshEnv() {
    const id = state.active;
    if (!id || !state.connected) return;
    const jobs = [
      acp.call("_session/sessions", { sessionId: id }).then((r) => {
        state.persist = r.sessions || [];
        if (r.persistSessionId) state.persistId = r.persistSessionId;
      }),
      acp.call("_session/roster", { sessionId: id }).then((r) => {
        state.agents = r.agents || [];
        state.channels = r.channels || [];
      }),
      acp.call("_session/peers", { sessionId: id }).then((r) => {
        state.peers = r.peers || [];
      }),
      acp.call("_session/bridge", { sessionId: id }).then((r) => {
        state.bridge = r;
      }),
    ];
    if (state.actTab === "git") {
      jobs.push(
        acp.call("_session/git", { sessionId: id }).then((r) => {
          state.git = r;
        })
      );
    }
    if (state.actTab === "files") {
      jobs.push(refreshFiles());
    }
    if (state.actTab === "channel") {
      jobs.push(refreshChannel());
    }
    if (state.actTab === "term") {
      jobs.push(refreshTerm());
    }
    try {
      await Promise.all(jobs);
    } catch (err) {
      state.error = err.message || String(err);
    }
    const typing =
      document.activeElement &&
      (document.activeElement.id === "draft" ||
        document.activeElement.id === "chan-draft" ||
        document.activeElement.id === "term-in" ||
        document.activeElement.id === "filter");
    if (!typing) paint();
    else paintStatus(state.active ? turn(state.active) : null);
  }

  async function refreshTerm() {
    const id = state.active;
    if (!id) return;
    const listed = await acp.call("_session/term", { sessionId: id, op: "list" });
    state.term.shells = listed.shells || [];
    const peek = await acp.call("_session/term", {
      sessionId: id,
      op: "peek",
      name: state.term.name || "main",
    });
    if (peek.text && !String(peek.text).startsWith("no shell")) state.term.text = peek.text;
    else if (peek.text && String(peek.text).startsWith("no shell")) state.term.text = "";
    try {
      const raw = await acp.call("_session/term", {
        sessionId: id,
        op: "bytes",
        name: state.term.name || "main",
      });
      if (raw.data) {
        state.term.raw = b64utf8(raw.data);
        state.term.seq = Number(raw.seq) || state.term.raw.length;
      }
    } catch {
      /* peek text still stands */
    }
  }

  function b64utf8(b64) {
    try {
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
      return new TextDecoder("utf-8").decode(bytes);
    } catch {
      return "";
    }
  }

  async function refreshFiles() {
    const id = state.active;
    if (!id) return;
    if (state.files.path) {
      const r = await acp.call("_session/fs", { sessionId: id, op: "read", path: state.files.path });
      state.files.lines = r.lines || [];
      state.files.note = r.note || null;
      state.files.binary = !!r.binary;
      return;
    }
    const r = await acp.call("_session/fs", { sessionId: id, op: "list", path: state.files.dir || "." });
    state.files.dir = r.dir || ".";
    state.files.entries = r.entries || [];
    state.files.note = r.note || null;
  }

  async function refreshChannel() {
    const id = state.active;
    if (!id) return;
    const r = await acp.call("_session/channel_read", { sessionId: id, channel: state.channel || "general" });
    state.channelStory = r;
    state.channel = r.channel || state.channel;
  }

  async function openFile(rel) {
    state.actTab = "files";
    state.files.path = rel;
    await refreshFiles();
    paint();
  }

  async function enterDir(name) {
    const dir = state.files.dir || ".";
    const next = name === ".." ? parentDir(dir) : dir === "." ? name : dir + "/" + name;
    state.files.path = null;
    state.files.dir = next;
    await refreshFiles();
    paint();
  }

  function parentDir(dir) {
    if (!dir || dir === ".") return ".";
    const parts = dir.split("/").filter(Boolean);
    parts.pop();
    return parts.length ? parts.join("/") : ".";
  }

  async function postChannel() {
    const id = state.active;
    const text = state.channelDraft.trim();
    if (!id || !text) return;
    await acp.call("_session/post", { sessionId: id, channel: state.channel, body: text });
    state.channelDraft = "";
    await refreshChannel();
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
          title: labelOf(update),
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
            title: labelOf(update),
            status: "pending",
            raw: {},
            body: "",
            diff: null,
            open: true,
          };
          t.activity.push(card);
        }
        if (update.status) card.status = update.status;
        if (update.title) card.title = labelOf(update);
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

  function copyText(text) {
    const value = String(text || "");
    if (!value) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).catch(() => {});
      return;
    }
    const box = document.createElement("textarea");
    box.value = value;
    document.body.appendChild(box);
    box.select();
    try {
      document.execCommand("copy");
    } catch {
      /* ignore */
    }
    box.remove();
  }

  function bindCopies(root) {
    if (!root) return;
    root.querySelectorAll("button.copy").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const host = btn.closest(".fence") || btn.parentElement;
        const code = host && host.querySelector("code");
        copyText((code && code.textContent) || "");
        const prev = btn.textContent;
        btn.textContent = "copied";
        setTimeout(() => {
          btn.textContent = prev || "copy";
        }, 1100);
      });
    });
  }

  function helpHtml() {
    const rows = [
      ["Enter", "send · Shift+Enter newline"],
      ["Esc", "cancel turn · close this"],
      ["N", "new session"],
      ["?", "keys"],
      ["Ctrl/⌘ K", "filter sessions"],
      ["Ctrl/⌘ `", "terminal"],
      ["1–7", "activity tabs"],
      ["dblclick", "copy a user prompt"],
    ];
    return `<div class="help-scrim" id="help-scrim"><div class="help" role="dialog" aria-label="Keys">
      <header>Keys</header>
      <dl>${rows
        .map(([k, v]) => `<div><dt><kbd>${esc(k)}</kbd></dt><dd>${esc(v)}</dd></div>`)
        .join("")}</dl>
      <p class="hint">Story is speech and thinking. Activity is the wire — complete() POSTs, syscalls, git, files, channels, and the kernel PTY.</p>
    </div></div>`;
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

  function matchesFilter(text) {
    const q = (state.filter || "").trim().toLowerCase();
    if (!q) return true;
    return String(text || "").toLowerCase().includes(q);
  }

  function paint() {
    const app = $("#app");
    app.classList.toggle("no-sidebar", !state.showSidebar);
    app.classList.toggle("no-activity", !state.showActivity);

    const focused = document.activeElement;
    const focusId = focused && focused.id;
    const selStart = focused && typeof focused.selectionStart === "number" ? focused.selectionStart : null;
    const selEnd = focused && typeof focused.selectionEnd === "number" ? focused.selectionEnd : null;

    const storyEl = $("#story");
    const actBody = $("#act-body");
    const stickStory =
      !storyEl || storyEl.scrollHeight - storyEl.scrollTop - storyEl.clientHeight < 140;
    const stickAct =
      !actBody || actBody.scrollHeight - actBody.scrollTop - actBody.clientHeight < 140;

    const t = state.active ? turn(state.active) : null;
    paintSidebar();
    paintTitle(t);
    paintStory(t);
    paintComposer(t);
    paintActivity(t);
    paintStatus(t);

    const storyNow = $("#story");
    if (storyNow && (stickStory || (t && t.running))) storyNow.scrollTop = storyNow.scrollHeight;
    const actNow = $("#act-body");
    if (actNow && (stickAct || (t && t.running))) actNow.scrollTop = actNow.scrollHeight;
    bindCopies($("app"));
    const scrim = $("#help-scrim");
    if (scrim) {
      scrim.onclick = (e) => {
        if (e.target === scrim) {
          state.help = false;
          paint();
        }
      };
    }
    if (focusId) {
      const el = document.getElementById(focusId);
      if (el) {
        el.focus();
        if (selStart != null && typeof el.setSelectionRange === "function") {
          try {
            el.setSelectionRange(selStart, selEnd);
          } catch {
            /* not a text field */
          }
        }
      }
    }
  }

  function paintSidebar() {
    $("#sidebar").innerHTML = `
      <div class="brand"><div class="mark"></div><div><div class="name">Desmos</div><div class="sub">kernel over ACP</div></div></div>
      <div class="side-actions">
        <button class="side-btn" data-act="new">${icon("plus")} New session <kbd>N</kbd></button>
        <input class="side-search" id="filter" placeholder="Filter" value="${esc(state.filter)}" />
      </div>
      <div class="rail-tabs">
        <button data-rail="sessions" class="${state.railTab === "sessions" ? "on" : ""}">sessions</button>
        <button data-rail="peers" class="${state.railTab === "peers" ? "on" : ""}">peers</button>
        <button data-rail="channels" class="${state.railTab === "channels" ? "on" : ""}">channels</button>
      </div>
      <div class="convs" id="convs"></div>
    `;
    $("[data-act=new]").onclick = () => newSession().catch((e) => ((state.error = e.message), paint()));
    $("#filter").oninput = (e) => {
      state.filter = e.target.value;
      paint();
    };
    $("#sidebar").querySelectorAll("[data-rail]").forEach((btn) => {
      btn.onclick = () => {
        state.railTab = btn.dataset.rail;
        if (state.railTab === "channels") state.actTab = "channel";
        paint();
        refreshEnv();
      };
    });
    const convs = $("#convs");
    if (state.railTab === "peers") paintPeers(convs);
    else if (state.railTab === "channels") paintChannelRail(convs);
    else paintSessionRail(convs);
  }

  function paintSessionRail(convs) {
    if (!state.sessions.length) {
      convs.innerHTML = `<div class="act-empty">${
        state.connected ? "Starting session…" : "Connecting…"
      }</div>`;
    } else {
      const liveLabel = document.createElement("div");
      liveLabel.className = "conv-label";
      liveLabel.textContent = "Live";
      convs.appendChild(liveLabel);
      for (const row of state.sessions) {
        const meta = sessionMeta(row.id);
        if (!matchesFilter(meta.title)) continue;
        const el = document.createElement("div");
        el.className = "conv" + (row.id === state.active ? " on" : "") + (meta.busy ? " busy" : "");
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
    const resumed = state.persist.filter((row) => matchesFilter(row.preview + " " + row.id));
    if (resumed.length) {
      const lab = document.createElement("div");
      lab.className = "conv-label";
      lab.textContent = "Resume";
      convs.appendChild(lab);
      for (const row of resumed) {
        const el = document.createElement("div");
        el.className = "conv" + (row.id === state.active || row.id === state.persistId ? " on" : "");
        const when = (row.started_at || "").slice(0, 16).replace("T", " ");
        el.innerHTML = `<div class="t">${esc(row.preview || row.id.slice(0, 8))}</div><div class="m"><span>${esc(
          when
        )}</span><span>${row.messages} msg</span></div>`;
        el.onclick = () => loadPersist(row.id).catch((e) => ((state.error = e.message), paint()));
        convs.appendChild(el);
      }
    }
  }

  function paintPeers(convs) {
    const rows = state.peers.filter((p) => matchesFilter((p.model || "") + " " + (p.run_id || "") + " " + (p.host || "")));
    if (!rows.length) {
      convs.innerHTML = `<div class="act-empty">${
        state.bridge && state.bridge.socket
          ? "A bridge socket is present. Desk does not attach as a second writer; this list is persist.peers()."
          : "No other live fronts in this workspace."
      }</div>`;
      return;
    }
    for (const peer of rows) {
      const el = document.createElement("div");
      el.className = "conv" + (peer.self ? " on" : "");
      const who = peer.self ? "this desk" : peer.remote ? peer.host || "remote" : `pid ${peer.pid || "?"}`;
      el.innerHTML = `<div class="t">${esc(who)}</div><div class="m"><span>${esc(
        peer.model || ""
      )}</span><span>${esc((peer.run_id || "").slice(0, 8))}</span></div>`;
      convs.appendChild(el);
    }
  }

  function paintChannelRail(convs) {
    const rows = state.channels.filter((c) => matchesFilter(c.channel || c.name || ""));
    if (!rows.length) {
      convs.innerHTML = `<div class="act-empty">No channels yet. Post from the Activity channel tab; it writes persist.channel_post.</div>`;
      return;
    }
    for (const row of rows) {
      const name = row.channel || row.name;
      const el = document.createElement("div");
      el.className = "conv" + (name === state.channel ? " on" : "");
      const unread = row.unread ? ` · ${row.unread}` : "";
      el.innerHTML = `<div class="t">${esc(name)}</div><div class="m"><span>${esc(
        row.preview || row.kind || ""
      )}${esc(unread)}</span></div>`;
      el.onclick = () => {
        state.channel = name;
        state.actTab = "channel";
        state.showActivity = true;
        paint();
        refreshChannel().then(paint);
      };
      convs.appendChild(el);
    }
  }

  function paintTitle(t) {
    const title = t ? t.title : "Desmos";
    $("#titlebar").innerHTML = `
      <button class="icon-btn" data-act="toggle-side" title="Sessions">${icon("panel")}</button>
      <div class="title">${esc(title)}</div>
      <button class="icon-btn" data-act="help" title="Keys">?</button>
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
    $("[data-act=help]").onclick = () => {
      state.help = !state.help;
      paint();
    };
  }

  function paintStory(t) {
    const story = $("#story");
    if (!t || (!t.story.length && !t.running)) {
      story.innerHTML = `
        <div class="empty">
          <h2>Do anything.</h2>
          <p>Story is speech and thinking. The wire — complete() POSTs, syscalls, git, files, channels, the PTY — stays on Activity.</p>
          <div class="keys">
            <span><kbd>Enter</kbd> send</span>
            <span><kbd>⇧ Enter</kbd> newline</span>
            <span><kbd>N</kbd> new session</span>
            <span><kbd>Esc</kbd> cancel</span>
            <span><kbd>?</kbd> keys</span>
            <span><kbd>1–7</kbd> activity</span>
          </div>
        </div>`;
      return;
    }
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
        wrap.querySelector("pre").hidden = item.open === false;
        wrap.querySelector(".lab").onclick = () => {
          item.open = item.open === false;
          paint();
        };
      } else if (item.kind === "assistant") {
        wrap.innerHTML = `<div class="assistant"><div class="md">${md(item.text)}</div></div>`;
      }
      if (item.kind === "user") {
        wrap.querySelector(".user").ondblclick = () => copyText(item.text);
      }
      inner.appendChild(wrap);
    }
    story.innerHTML = "";
    story.appendChild(inner);
  }

  function paintComposer(t) {
    const running = !!(t && t.running);
    const canSend = state.connected && !!state.draft.trim();
    const drafting = document.activeElement && document.activeElement.id === "draft";
    const selStart = drafting ? document.activeElement.selectionStart : state.draft.length;
    const selEnd = drafting ? document.activeElement.selectionEnd : state.draft.length;
    $("#composer-wrap").innerHTML = `
      ${state.help ? helpHtml() : ""}
      ${
        state.error || (t && t.error)
          ? `<div class="banner" data-dismiss>${esc(state.error || t.error)}<button type="button" class="banner-x" aria-label="Dismiss">×</button></div>`
          : ""
      }
      <div class="composer">
        <textarea id="draft" placeholder="Do anything…" rows="1"></textarea>
        <div class="composer-bar">
          <select class="chip" id="model">${state.models
            .map((m) => `<option value="${esc(m)}"${m === state.model ? " selected" : ""}>${esc(m)}</option>`)
            .join("")}</select>
          <select class="chip" id="effort">${state.efforts
            .map((e) => `<option value="${esc(e)}"${e === state.effort ? " selected" : ""}>${esc(e)}</option>`)
            .join("")}</select>
          <div class="grow">${running ? "running — Enter steers" : ""}</div>
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
    const model = $("#model");
    const effort = $("#effort");
    if (model) model.onchange = (e) => setConfig("model", e.target.value);
    if (effort) effort.onchange = (e) => setConfig("thought_level", e.target.value);
    const banner = $("[data-dismiss]");
    if (banner)
      banner.onclick = () => {
        state.error = "";
        if (t) t.error = "";
        paint();
      };
    if (drafting) {
      draft.focus();
      draft.setSelectionRange(selStart, selEnd);
    }
    draft.style.height = "44px";
    draft.style.height = Math.min(180, draft.scrollHeight) + "px";
  }

  function paintActivity(t) {
    let salvage = null;
    const existing = document.getElementById("term-host");
    if (state.actTab === "term" && existing && termLive.term) {
      salvage = existing;
      existing.remove();
    } else if (state.actTab !== "term") {
      disposeTerm();
    }
    $("#activity").innerHTML = `
      <div class="act-head">Activity
        <div class="act-tabs">
          ${ACT_TABS.map((tab) => {
            const labels = {
              wire: "wire",
              post: "post",
              edits: "edits",
              git: "git",
              files: "files",
              channel: "chan",
              term: "$",
            };
            return `<button data-tab="${tab}" class="${state.actTab === tab ? "on" : ""}">${
              labels[tab] || tab
            }</button>`;
          }).join("")}
        </div>
      </div>
      <div class="act-body" id="act-body"></div>
    `;
    $("#activity").querySelectorAll("[data-tab]").forEach((btn) => {
      btn.onclick = () => {
        state.actTab = btn.dataset.tab;
        paint();
        refreshEnv();
      };
    });
    const body = $("#act-body");
    if (state.actTab === "git") paintGit(body);
    else if (state.actTab === "files") paintFiles(body);
    else if (state.actTab === "channel") paintChannel(body);
    else if (state.actTab === "term") paintTerm(body, salvage);
    else paintWire(body, t);
  }

  function paintWire(body, t) {
    const cards = t ? t.activity : [];
    const shown =
      state.actTab === "post"
        ? cards.filter((c) => c.family === "complete")
        : state.actTab === "edits"
          ? cards.filter((c) => c.family === "edit")
          : cards;
    if (!shown.length) {
      body.innerHTML = `<div class="act-empty">${
        state.actTab === "post"
          ? "complete() POSTs land here — model, usage, span count. The request body is redacted."
          : state.actTab === "edits"
            ? "workspace edits stream as diffs, not as story."
            : "Syscalls, complete cards, and edits. Nothing here is mirrored into the story."
      }</div>`;
      return;
    }
    for (const card of shown) {
      const el = document.createElement("div");
      el.className = `card ${card.family}${card.open ? "" : " folded"}`;
      const st =
        card.status === "completed" ? "done" : card.status === "pending" || card.status === "in_progress" ? "run" : "fail";
      let inner = "";
      if (card.diff) {
        inner += `<div class="path">${esc(card.diff.path || "")}</div>`;
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

  function paintGit(body) {
    const g = state.git || {};
    if (!g.read && !g.error) {
      body.innerHTML = `<div class="act-empty">Reading git…</div>`;
      return;
    }
    if (g.error) {
      body.innerHTML = `<div class="act-empty">${esc(g.error)}</div>`;
      return;
    }
    const tabs = ["status", "branches", "log"];
    const head = document.createElement("div");
    head.className = "git-head";
    head.innerHTML = `<span class="git-branch">${esc(g.branch || "detached")}</span><span class="git-dirty">${
      g.dirty ? g.dirty + " dirty" : "clean"
    }</span><div class="act-tabs">${tabs
      .map((tab) => `<button data-gtab="${tab}" class="${state.gitTab === tab ? "on" : ""}">${tab}</button>`)
      .join("")}</div>`;
    head.querySelectorAll("[data-gtab]").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        state.gitTab = btn.dataset.gtab;
        paint();
      };
    });
    body.appendChild(head);
    const rows = g[state.gitTab] || [];
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "act-empty";
      empty.textContent = "nothing here";
      body.appendChild(empty);
      return;
    }
    for (const row of rows) {
      const el = document.createElement("div");
      el.className = "git-row";
      el.innerHTML = `<span class="mark">${esc(row.mark || "")}</span><span class="t">${esc(row.text || "")}</span>`;
      if (row.path) {
        el.classList.add("link");
        el.onclick = () => openFile(row.path);
      }
      body.appendChild(el);
    }
  }

  function paintFiles(body) {
    const f = state.files;
    if (f.path) {
      const head = document.createElement("div");
      head.className = "git-head";
      head.innerHTML = `<button class="text-btn" data-back>← ${esc(f.dir || ".")}</button><span class="path">${esc(
        f.path
      )}</span>`;
      head.querySelector("[data-back]").onclick = () => {
        state.files.path = null;
        refreshFiles().then(paint);
      };
      body.appendChild(head);
      const pre = document.createElement("pre");
      pre.className = "file-view";
      if (f.note) pre.textContent = f.note;
      else pre.innerHTML = `<code>${window.DesmosMd.highlight(f.lines.join("\n"), extLang(f.path))}</code>`;
      body.appendChild(pre);
      return;
    }
    const head = document.createElement("div");
    head.className = "git-head";
    head.innerHTML = `<span class="path">${esc(f.dir || ".")}</span>`;
    body.appendChild(head);
    if (f.note) {
      const note = document.createElement("div");
      note.className = "act-empty";
      note.textContent = f.note;
      body.appendChild(note);
    }
    for (const row of f.entries || []) {
      const el = document.createElement("div");
      el.className = "git-row link";
      el.innerHTML = `<span class="mark">${row.is_dir ? "▸" : " "}</span><span class="t">${esc(row.name)}</span>`;
      el.onclick = () => (row.is_dir ? enterDir(row.name) : openFile(joinPath(f.dir, row.name)));
      body.appendChild(el);
    }
  }

  function joinPath(dir, name) {
    if (!dir || dir === ".") return name;
    return dir + "/" + name;
  }

  function extLang(path) {
    const m = String(path || "").match(/\.([A-Za-z0-9]+)$/);
    return m ? m[1] : "";
  }

  function paintChannel(body) {
    const ch = state.channelStory || {};
    const head = document.createElement("div");
    head.className = "git-head";
    head.innerHTML = `<span class="path">${esc(state.channel || "general")}</span><span class="git-dirty">${
      (ch.participants || []).length
    } here</span>`;
    body.appendChild(head);
    const msgs = ch.messages || [];
    if (!msgs.length) {
      const empty = document.createElement("div");
      empty.className = "act-empty";
      empty.textContent = "No messages in this channel. Posts go through persist.channel_post.";
      body.appendChild(empty);
    }
    for (const row of msgs) {
      const el = document.createElement("div");
      el.className = "chan-msg";
      el.innerHTML = `<div class="who">${esc(row.author || "")} <span>${esc(
        String(row.created_at || "").slice(11, 16)
      )}</span></div><div class="body">${esc(row.body || "")}</div>`;
      body.appendChild(el);
    }
    const composer = document.createElement("div");
    composer.className = "chan-composer";
    composer.innerHTML = `<textarea id="chan-draft" rows="2" placeholder="Post to #${esc(
      state.channel || "general"
    )}"></textarea><button class="side-btn" id="chan-go">Post</button>`;
    body.appendChild(composer);
    const box = $("#chan-draft");
    box.value = state.channelDraft;
    box.oninput = () => {
      state.channelDraft = box.value;
    };
    box.onkeydown = (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        postChannel().catch((err) => ((state.error = err.message), paint()));
      }
    };
    $("#chan-go").onclick = () => postChannel().catch((err) => ((state.error = err.message), paint()));
  }

  const termLive = {
    term: null,
    fit: null,
    written: "",
    name: "",
    session: "",
  };

  function disposeTerm() {
    if (termLive.term) {
      try {
        termLive.term.dispose();
      } catch {
        /* already gone */
      }
    }
    termLive.term = null;
    termLive.fit = null;
    termLive.written = "";
    termLive.name = "";
    termLive.session = "";
  }

  function toCrlf(text) {
    return String(text || "")
      .replace(/\r\n/g, "\n")
      .replace(/\n/g, "\r\n");
  }

  function writeTermBlob(blob) {
    if (!termLive.term) return;
    const next = String(blob || "");
    if (next === termLive.written) return;
    if (termLive.written && next.startsWith(termLive.written)) {
      termLive.term.write(toCrlf(next.slice(termLive.written.length)));
    } else {
      termLive.term.reset();
      if (next) termLive.term.write(toCrlf(next));
    }
    termLive.written = next;
  }

  async function runTerm(cmd) {
    const id = state.active;
    const text = String(cmd || "").replace(/\n$/, "");
    if (!id || !text.trim()) return;
    try {
      const r = await acp.call("_session/term", {
        sessionId: id,
        op: "run",
        name: state.term.name || "main",
        body: text,
      });
      if (r && r.text) state.term.text = r.text;
      await refreshTerm();
    } catch (err) {
      state.error = err.message || String(err);
    }
    paint();
    const again = document.getElementById("term-in");
    if (again) again.focus();
  }

  function paintTerm(body, salvage) {
    body.classList.add("term-body");
    const name = state.term.name || "main";
    const session = state.active || "";
    if (termLive.term && (termLive.name !== name || termLive.session !== session)) {
      disposeTerm();
      salvage = null;
    }
    const shells = state.term.shells.length ? state.term.shells : [];
    const names = shells.length
      ? shells
          .map((s) => {
            const n = s.name || s;
            const on = n === name ? " on" : "";
            return `<button type="button" class="term-name${on}" data-term-name="${esc(n)}">${esc(n)}</button>`;
          })
          .join("")
      : `<span class="term-idle">no pty yet</span>`;
    const blob = state.term.raw || state.term.text || "";
    const empty = !String(blob).trim();
    body.innerHTML = `
      <div class="term-bar">
        <div class="term-names">${names}</div>
        <span class="grow"></span>
        <button type="button" data-term-int title="Interrupt the foreground job">int</button>
        <button type="button" data-term-close title="Close this PTY">close</button>
      </div>
      <div class="term-host" id="term-host"></div>
      <div class="term-in-wrap">
        <span class="term-prompt">$</span>
        <input id="term-in" class="term-in" spellcheck="false" autocomplete="off" placeholder="command" />
      </div>`;
    let host = body.querySelector("#term-host");
    if (salvage && termLive.term && host) {
      host.replaceWith(salvage);
      salvage.id = "term-host";
      host = salvage;
      writeTermBlob(blob);
      if (termLive.fit) {
        try {
          termLive.fit.fit();
        } catch {
          /* host may still be 0×0 */
        }
      }
    } else if (window.Terminal && host) {
      disposeTerm();
      const term = new window.Terminal({
        convertEol: true,
        cursorBlink: true,
        fontSize: 12,
        fontFamily: '"IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace',
        theme: {
          background: "#1a1b26",
          foreground: "#a9b1d6",
          cursor: "#c0caf5",
          selectionBackground: "#515c7e4d",
          black: "#15161e",
          red: "#f7768e",
          green: "#9ece6a",
          yellow: "#e0af68",
          blue: "#7aa2f7",
          magenta: "#bb9af7",
          cyan: "#7dcfff",
          white: "#a9b1d6",
        },
      });
      term.open(host);
      const Fit = window.FitAddon && (window.FitAddon.FitAddon || window.FitAddon);
      if (typeof Fit === "function") {
        const fit = new Fit();
        term.loadAddon(fit);
        termLive.fit = fit;
        try {
          fit.fit();
        } catch {
          /* host may still be 0×0 on first paint */
        }
      }
      termLive.term = term;
      termLive.name = name;
      termLive.session = session;
      termLive.written = "";
      writeTermBlob(blob);
      term.onData((data) => {
        if (data === "\x03") {
          acp
            .call("_session/term", {
              sessionId: state.active,
              op: "interrupt",
              name: state.term.name || "main",
            })
            .then(refreshTerm)
            .then(paint);
          return;
        }
        if (data === "\r" || data === "\n") {
          const cmd = state.term.line || "";
          state.term.line = "";
          term.write("\r\n");
          runTerm(cmd);
          return;
        }
        if (data === "\u007f") {
          state.term.line = (state.term.line || "").slice(0, -1);
          term.write("\b \b");
          return;
        }
        if (data.length === 1 && data >= " ") {
          state.term.line = (state.term.line || "") + data;
          term.write(data);
        }
      });
    } else if (host) {
      host.innerHTML = `<pre class="term-out" id="term-out">${
        empty
          ? `<span class="term-hint">Kernel PTY — world.shells. Type a command.</span>`
          : esc(blob)
      }</pre>`;
      const out = host.querySelector("#term-out");
      if (out) out.scrollTop = out.scrollHeight;
    }
    body.querySelectorAll("[data-term-name]").forEach((btn) => {
      btn.onclick = () => {
        state.term.name = btn.dataset.termName;
        refreshTerm().then(paint);
      };
    });
    const input = body.querySelector("#term-in");
    if (input) {
      input.value = state.term.draft;
      input.addEventListener("input", () => {
        state.term.draft = input.value;
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          const cmd = input.value;
          state.term.draft = "";
          input.value = "";
          runTerm(cmd);
        }
      });
    }
    body.querySelector("[data-term-int]").onclick = async () => {
      await acp.call("_session/term", {
        sessionId: state.active,
        op: "interrupt",
        name: state.term.name || "main",
      });
      await refreshTerm();
      paint();
    };
    body.querySelector("[data-term-close]").onclick = async () => {
      await acp.call("_session/term", {
        sessionId: state.active,
        op: "close",
        name: state.term.name || "main",
      });
      state.term.text = "";
      state.term.raw = "";
      disposeTerm();
      await refreshTerm();
      paint();
    };
  }

  function paintStatus(t) {
    const br = state.bridge || {};
    $("#status").innerHTML = `
      <span class="dot-live${state.connected ? "" : " off"}"></span>
      <span>${state.connected ? "acp" : "offline"}</span>
      <span>${esc(state.model || "—")}</span>
      <span>${esc(state.effort || "—")}</span>
      <span>${state.git && state.git.branch ? esc(state.git.branch) : ""}</span>
      <span>${state.term.shells && state.term.shells.length ? "pty " + (state.term.name || "main") : ""}</span>
      <span>${br.socket ? "bridge.sock" : ""}</span>
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
    if (e.key === "Escape" && state.help) {
      e.preventDefault();
      state.help = false;
      paint();
      return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      state.showSidebar = true;
      paint();
      const f = $("#filter");
      if (f) f.focus();
      return;
    }
    if ((e.metaKey || e.ctrlKey) && e.key === "`") {
      e.preventDefault();
      state.actTab = "term";
      state.showActivity = true;
      paint();
      refreshEnv().then(() => {
        const box = $("#term-in");
        if (box) box.focus();
      });
      return;
    }
    const typing =
      e.target &&
      (e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT" || e.target.tagName === "INPUT");
    if (typing) {
      if (e.key === "Escape") {
        e.preventDefault();
        if (e.target.id === "term-in") {
          state.term.draft = "";
          e.target.value = "";
          return;
        }
        cancel();
      }
      return;
    }
    if (e.key === "?" || (e.key === "/" && e.shiftKey)) {
      e.preventDefault();
      state.help = !state.help;
      paint();
      return;
    }
    if (e.key === "n" || e.key === "N") {
      e.preventDefault();
      newSession();
    }
    if (e.key === "Escape") cancel();
    const num = "1234567".indexOf(e.key);
    if (num >= 0) {
      state.actTab = ACT_TABS[num];
      state.showActivity = true;
      paint();
      refreshEnv();
    }
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
  setInterval(() => {
    if (state.connected && state.active) refreshEnv();
  }, 4000);
})();
