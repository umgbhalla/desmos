import React from "react";
import { unifiedPatch } from "./patch.js";

const BG = "#1a1b26";
const STORY_BG = "#16161e";
const WIRE_BG = "#1f2335";
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
};

const THINK_THEME = {
  ...STORY_THEME,
  text: MUTED,
  textMuted: MUTED,
};

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
        style: { padding: 12, borderRadius: 8, backgroundColor: "#24283b" },
      },
      React.createElement("text", { style: { color: ACCENT, fontSize: 13 } }, item.text),
    );
  }
  if (item.kind === "thinking") {
    return React.createElement(
      "div",
      { key: `th${i}`, style: { paddingLeft: 8, borderLeftWidth: 2, borderColor: MUTED } },
      React.createElement("text", { style: { color: MUTED, fontSize: 11 } }, "thinking"),
      React.createElement("markdown", { source: item.text || "", theme: THINK_THEME }),
    );
  }
  return React.createElement(
    "div",
    { key: `a${i}`, style: { paddingTop: 8 } },
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
      React.createElement("text", { key: "b", style: { color: MUTED, fontSize: 12 } }, String(card.body).slice(0, 400)),
    );
  }
  return React.createElement(
    "div",
    {
      key: card.id || `c${i}`,
      style: { padding: 8, marginBottom: 8, backgroundColor: "#24283b", borderRadius: 6 },
    },
    ...kids,
  );
}

export function App({ turn, draft, onDraft, onSubmit, status }) {
  const story = (turn && turn.story) || [];
  const activity = (turn && turn.activity) || [];
  return React.createElement(
    "div",
    {
      style: {
        display: "flex",
        flexDirection: "column",
        width: "100%",
        height: "100%",
        backgroundColor: BG,
      },
    },
    React.createElement(
      "div",
      { style: { display: "flex", flexGrow: 1, minHeight: 0 } },
      React.createElement(
        "div",
        {
          style: {
            display: "flex",
            flexDirection: "column",
            flexGrow: 1,
            padding: 16,
            gap: 12,
            overflowY: "scroll",
            backgroundColor: STORY_BG,
          },
        },
        React.createElement("text", { style: { color: MUTED, fontSize: 11 } }, "story"),
        ...story.map(storyItem),
      ),
      React.createElement(
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
        ...activity.map(activityCard),
      ),
    ),
    React.createElement(
      "div",
      { style: { padding: 12, backgroundColor: "#16161e" } },
      React.createElement("textarea", {
        value: draft || "",
        placeholder: "prompt — Enter sends, same ACP session/prompt as desk",
        theme: STORY_THEME,
        minRows: 2,
        maxRows: 6,
        onChange: onDraft
          ? (ev) => onDraft(ev && ev.value != null ? ev.value : "")
          : undefined,
        onSubmit: onSubmit ? () => onSubmit() : undefined,
      }),
      React.createElement("text", { style: { color: MUTED, fontSize: 11 } }, status || ""),
    ),
  );
}
