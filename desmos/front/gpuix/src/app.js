import React from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@gpuix/react";
import { unifiedPatch } from "./patch.js";
import { titleOf } from "./normalize.js";

const BG = "#1a1b26";
const SIDEBAR = "#16161e";
const STORY_BG = "#1a1b26";
const WIRE_BG = "#1f2335";
const RAISED = "#24283b";
const TEXT = "#c0caf5";
const MUTED = "#565f89";
const ACCENT = "#7aa2f7";

const STORY_THEME = {
  appearance: "dark",
  bg: STORY_BG,
  text: TEXT,
  textMuted: MUTED,
  fontSans: "IBM Plex Sans",
  fontMono: "IBM Plex Mono",
  metrics: { mdTextSize: 14, mdLineHeight: 22 },
};

const THINK_THEME = { ...STORY_THEME, text: MUTED, textMuted: MUTED };

const DIFF_THEME = {
  appearance: "dark",
  bg: WIRE_BG,
  text: TEXT,
  diffAdd: "#9ece6a",
  diffDel: "#f7768e",
  fontMono: "IBM Plex Mono",
};

function storyItem(item, i) {
  if (item.kind === "user") {
    return React.createElement(
      "div",
      {
        key: `u${i}`,
        style: { padding: 12, marginBottom: 8, borderRadius: 8, backgroundColor: RAISED },
      },
      React.createElement("text", { style: { color: ACCENT, fontSize: 13 } }, item.text),
    );
  }
  if (item.kind === "thinking") {
    return React.createElement(
      "div",
      {
        key: `th${i}`,
        style: {
          paddingLeft: 8,
          marginBottom: 8,
          borderLeftWidth: 2,
          borderColor: MUTED,
        },
      },
      React.createElement("text", { style: { color: MUTED, fontSize: 11 } }, "thinking"),
      React.createElement("markdown", { source: item.text || "", theme: THINK_THEME }),
    );
  }
  if (item.kind === "steer") {
    return React.createElement(
      "div",
      { key: `st${i}`, style: { padding: 8, marginBottom: 8 } },
      React.createElement("text", { style: { color: MUTED, fontSize: 11 } }, "steer queued"),
      React.createElement("text", { style: { color: TEXT, fontSize: 13 } }, item.text),
    );
  }
  if (item.kind === "system") {
    return React.createElement(
      "div",
      { key: `sy${i}`, style: { padding: 8, marginBottom: 8 } },
      React.createElement("text", { style: { color: MUTED, fontSize: 11 } }, item.family || "notice"),
      React.createElement("text", { style: { color: TEXT, fontSize: 13 } }, item.text),
    );
  }
  if (item.kind === "subagent") {
    return React.createElement(
      "div",
      { key: `sa${i}`, style: { padding: 8, marginBottom: 8, backgroundColor: RAISED, borderRadius: 6 } },
      React.createElement("text", { style: { color: MUTED, fontSize: 11 } }, `subagent ${item.status || ""}`),
      React.createElement("text", { style: { color: TEXT, fontSize: 13 } }, item.title || ""),
      React.createElement("text", { style: { color: MUTED, fontSize: 12 } }, item.text || ""),
    );
  }
  return React.createElement(
    "div",
    { key: `a${i}`, style: { paddingTop: 4, marginBottom: 8 } },
    React.createElement("markdown", { source: item.text || "", theme: STORY_THEME }),
  );
}

function activityCard(card, i) {
  const kids = [
    React.createElement(
      "text",
      { key: "h", style: { color: ACCENT, fontSize: 12 } },
      `${card.family || ""} ${card.title || ""}`.trim(),
    ),
  ];
  if (card.diff) {
    kids.push(
      React.createElement("diff", {
        key: "d",
        patch: unifiedPatch(card.diff.path, card.diff.oldText, card.diff.newText),
        wordDiff: true,
        theme: DIFF_THEME,
      }),
    );
  } else if (card.body) {
    kids.push(
      React.createElement(
        "text",
        { key: "b", style: { color: MUTED, fontSize: 12 } },
        String(card.body).slice(0, 400),
      ),
    );
  }
  return React.createElement(
    "div",
    {
      key: card.id || `c${i}`,
      style: { padding: 8, marginBottom: 8, backgroundColor: RAISED, borderRadius: 6 },
    },
    ...kids,
  );
}

function picker(id, value, values, onChange, placeholder) {
  if (!values || !values.length) {
    return React.createElement("text", { style: { color: MUTED, fontSize: 12 } }, value || "");
  }
  return React.createElement(
    Select,
    { key: id, value: value || values[0], onValueChange: onChange },
    React.createElement(
      SelectTrigger,
      { style: { padding: 6, backgroundColor: RAISED, borderRadius: 6, minWidth: 120 } },
      React.createElement(SelectValue, { placeholder: placeholder || id }),
    ),
    React.createElement(
      SelectContent,
      null,
      ...values.map((v) =>
        React.createElement(
          SelectItem,
          { key: v, value: v },
          React.createElement("text", { style: { color: TEXT, fontSize: 13 } }, v),
        ),
      ),
    ),
  );
}

export function App({
  sessions = [],
  activeId = "",
  turn,
  models = [],
  model = "",
  efforts = [],
  effort = "",
  draft = "",
  status = "",
  running = false,
  showActivity = true,
  onNew,
  onSelect,
  onDraft,
  onSubmit,
  onCancel,
  onModel,
  onEffort,
  onToggleActivity,
}) {
  const story = (turn && turn.story) || [];
  const activity = (turn && turn.activity) || [];
  const title = titleOf(turn);

  return React.createElement(
    "div",
    {
      style: {
        display: "flex",
        flexDirection: "row",
        width: "100%",
        height: "100%",
        backgroundColor: BG,
      },
      tabIndex: 0,
      onKeyDown: (ev) => {
        const key = ev && (ev.key || ev.value);
        if ((key === "escape" || key === "Escape") && onCancel) onCancel();
      },
    },
    React.createElement(
      "div",
      {
        style: {
          display: "flex",
          flexDirection: "column",
          width: 252,
          padding: 12,
          gap: 8,
          backgroundColor: SIDEBAR,
        },
      },
      React.createElement("text", { style: { color: TEXT, fontSize: 14, fontWeight: "600" } }, "Desmos"),
      React.createElement(
        "div",
        {
          onClick: onNew,
          style: { padding: 8, borderRadius: 8, backgroundColor: RAISED, cursor: "pointer" },
        },
        React.createElement("text", { style: { color: ACCENT, fontSize: 13 } }, "New session"),
      ),
      ...(sessions.length
        ? sessions.map((row) =>
            React.createElement(
              "div",
              {
                key: row.id,
                onClick: onSelect ? () => onSelect(row.id) : undefined,
                style: {
                  padding: 8,
                  borderRadius: 8,
                  backgroundColor: row.id === activeId ? RAISED : "transparent",
                  cursor: "pointer",
                },
              },
              React.createElement(
                "text",
                { style: { color: row.id === activeId ? TEXT : MUTED, fontSize: 12 } },
                row.title || row.id.slice(0, 8),
              ),
            ),
          )
        : [
            React.createElement(
              "text",
              { key: "empty", style: { color: MUTED, fontSize: 12 } },
              "no sessions yet",
            ),
          ]),
    ),
    React.createElement(
      "div",
      { style: { display: "flex", flexDirection: "column", flexGrow: 1, minWidth: 0 } },
      React.createElement(
        "div",
        {
          style: {
            display: "flex",
            height: 48,
            paddingLeft: 16,
            paddingRight: 16,
            alignItems: "center",
            gap: 12,
            backgroundColor: SIDEBAR,
          },
        },
        React.createElement("text", { style: { color: TEXT, fontSize: 13, flexGrow: 1 } }, title),
        picker("model", model, models, onModel, "model"),
        picker("thought_level", effort, efforts, onEffort, "effort"),
        React.createElement(
          "div",
          {
            onClick: onToggleActivity,
            style: { padding: 6, borderRadius: 6, backgroundColor: RAISED, cursor: "pointer" },
          },
          React.createElement(
            "text",
            { style: { color: ACCENT, fontSize: 12 } },
            showActivity ? "hide activity" : "activity",
          ),
        ),
      ),
      React.createElement(
        "div",
        { style: { display: "flex", flexGrow: 1, minHeight: 0 } },
        React.createElement(
          "virtual-list",
          {
            alignment: "bottom",
            followTail: true,
            estimatedItemHeight: 72,
            style: { flexGrow: 1, padding: 16, backgroundColor: STORY_BG },
          },
          story.length
            ? story.map(storyItem)
            : React.createElement(
                "text",
                { style: { color: MUTED, fontSize: 13 } },
                "story — speech and thinking. tools stay on activity.",
              ),
        ),
        showActivity
          ? React.createElement(
              "div",
              {
                style: {
                  display: "flex",
                  flexDirection: "column",
                  width: 380,
                  padding: 12,
                  gap: 8,
                  overflowY: "scroll",
                  backgroundColor: WIRE_BG,
                },
              },
              React.createElement("text", { style: { color: MUTED, fontSize: 11 } }, "activity"),
              ...(activity.length
                ? activity.map(activityCard)
                : [
                    React.createElement(
                      "text",
                      { key: "empty-act", style: { color: MUTED, fontSize: 12 } },
                      "complete() and syscalls land here",
                    ),
                  ]),
            )
          : null,
      ),
      React.createElement(
        "div",
        {
          style: {
            display: "flex",
            padding: 12,
            gap: 8,
            alignItems: "flex-end",
            backgroundColor: SIDEBAR,
          },
        },
        React.createElement("textarea", {
          value: draft || "",
          placeholder: "prompt — Enter sends, Esc cancels",
          theme: STORY_THEME,
          minRows: 2,
          maxRows: 6,
          style: { flexGrow: 1 },
          onChange: onDraft
            ? (ev) => onDraft(ev && ev.value != null ? ev.value : "")
            : undefined,
          onSubmit: onSubmit && !running ? () => onSubmit() : undefined,
        }),
        React.createElement(
          "div",
          {
            onClick: running ? onCancel : onSubmit,
            style: {
              padding: 10,
              borderRadius: 8,
              backgroundColor: RAISED,
              cursor: "pointer",
            },
          },
          React.createElement(
            "text",
            { style: { color: ACCENT, fontSize: 13 } },
            running ? "stop" : "send",
          ),
        ),
      ),
      React.createElement(
        "text",
        { style: { color: MUTED, fontSize: 11, paddingLeft: 12, paddingBottom: 8 } },
        status || "",
      ),
    ),
  );
}
