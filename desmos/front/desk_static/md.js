/* Desmos desk — GFM subset for streamed assistant markdown.
   Headings, lists, fences, tables, quotes, emphasis, links, inline code.
   Not a toy: fences are lexed before inline, so a ``` block cannot leak. */

(function (root) {
  "use strict";

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
  function esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ESC[c]);
  }

  const LANG = {
    keyword:
      "and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield self True False None const let var function return typeof instanceof new this class extends",
  };

  function highlight(code, lang) {
    const src = String(code);
    if (!lang) return esc(src);
    const keywords = new Set(LANG.keyword.split(/\s+/));
    const out = [];
    const re =
      /(```)|(\/\/[^\n]*|#(?!\{)[^\n]*|\/\*[\s\S]*?\*\/)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(\b\d+(?:\.\d+)?\b)|(\b[A-Za-z_][A-Za-z0-9_]*\b)|(\s+)|(.)/g;
    let m;
    while ((m = re.exec(src))) {
      if (m[2]) out.push(`<span class="tok-cmt">${esc(m[2])}</span>`);
      else if (m[3]) out.push(`<span class="tok-str">${esc(m[3])}</span>`);
      else if (m[4]) out.push(`<span class="tok-num">${esc(m[4])}</span>`);
      else if (m[5]) {
        const w = m[5];
        out.push(keywords.has(w) ? `<span class="tok-kw">${esc(w)}</span>` : esc(w));
      } else out.push(esc(m[6] || m[7] || ""));
    }
    return out.join("");
  }

  function inline(text) {
    let s = esc(text);
    s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^\w])\*([^*]+)\*(?!\*)/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    s = s.replace(
      /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1</a>'
    );
    return s;
  }

  function render(src) {
    const lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
    const html = [];
    let i = 0;
    let para = [];
    function flushPara() {
      if (!para.length) return;
      html.push(`<p>${inline(para.join(" "))}</p>`);
      para = [];
    }
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^```([\w.+-]*)\s*$/);
      if (fence) {
        flushPara();
        const lang = fence[1] || "";
        const body = [];
        i += 1;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) {
          body.push(lines[i]);
          i += 1;
        }
        if (i < lines.length) i += 1;
        html.push(
          `<pre class="code"><header>${esc(lang || "code")}</header><code>${highlight(
            body.join("\n"),
            lang
          )}</code></pre>`
        );
        continue;
      }
      if (/^\s*\|.+\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*[-:| ]+\|\s*$/.test(lines[i + 1])) {
        flushPara();
        const rows = [];
        while (i < lines.length && /^\s*\|/.test(lines[i])) {
          const cells = lines[i]
            .replace(/^\s*\|/, "")
            .replace(/\|\s*$/, "")
            .split("|")
            .map((c) => c.trim());
          rows.push(cells);
          i += 1;
        }
        if (rows.length >= 2) {
          const head = rows[0];
          const body = rows.slice(2);
          html.push("<table><thead><tr>");
          head.forEach((c) => html.push(`<th>${inline(c)}</th>`));
          html.push("</tr></thead><tbody>");
          body.forEach((r) => {
            html.push("<tr>");
            r.forEach((c) => html.push(`<td>${inline(c)}</td>`));
            html.push("</tr>");
          });
          html.push("</tbody></table>");
        }
        continue;
      }
      if (/^#{1,6}\s+\S/.test(line)) {
        flushPara();
        const n = line.match(/^(#{1,6})/)[1].length;
        html.push(`<h${n}>${inline(line.replace(/^#{1,6}\s+/, ""))}</h${n}>`);
        i += 1;
        continue;
      }
      if (/^>\s?/.test(line)) {
        flushPara();
        const q = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) {
          q.push(lines[i].replace(/^>\s?/, ""));
          i += 1;
        }
        html.push(`<blockquote>${inline(q.join(" "))}</blockquote>`);
        continue;
      }
      if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
        flushPara();
        const ordered = /^\s*\d+\./.test(line);
        html.push(ordered ? "<ol>" : "<ul>");
        while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
          const item = lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, "");
          const task = item.match(/^\[([ xX])\]\s+(.*)$/);
          if (task) {
            html.push(
              `<li class="task"><span class="box">${
                task[1] !== " " ? "✓" : ""
              }</span>${inline(task[2])}</li>`
            );
          } else html.push(`<li>${inline(item)}</li>`);
          i += 1;
        }
        html.push(ordered ? "</ol>" : "</ul>");
        continue;
      }
      if (/^---+\s*$/.test(line) || /^\*\*\*+\s*$/.test(line)) {
        flushPara();
        html.push("<hr/>");
        i += 1;
        continue;
      }
      if (!line.trim()) {
        flushPara();
        i += 1;
        continue;
      }
      para.push(line.trim());
      i += 1;
    }
    flushPara();
    return html.join("") || "<p></p>";
  }

  function diffHtml(oldText, newText) {
    const a = String(oldText || "").split("\n");
    const b = String(newText || "").split("\n");
    const rows = [];
    const n = Math.max(a.length, b.length);
    // Line-oriented LCS is overkill for streamed cards; pair by index then
    // mark leftovers. A real unified diff from the kernel still wins when
    // present — this is the fallback for old/new bodies.
    let i = 0;
    let j = 0;
    while (i < a.length || j < b.length) {
      if (i < a.length && j < b.length && a[i] === b[j]) {
        rows.push(`<div class="d-eq"><span class="g"></span><span class="t">${esc(a[i])}</span></div>`);
        i += 1;
        j += 1;
        continue;
      }
      if (i < a.length && (j >= b.length || !b.includes(a[i], j))) {
        rows.push(`<div class="d-del"><span class="g">−</span><span class="t">${esc(a[i])}</span></div>`);
        i += 1;
        continue;
      }
      if (j < b.length && (i >= a.length || !a.includes(b[j], i))) {
        rows.push(`<div class="d-add"><span class="g">+</span><span class="t">${esc(b[j])}</span></div>`);
        j += 1;
        continue;
      }
      rows.push(`<div class="d-del"><span class="g">−</span><span class="t">${esc(a[i])}</span></div>`);
      rows.push(`<div class="d-add"><span class="g">+</span><span class="t">${esc(b[j])}</span></div>`);
      i += 1;
      j += 1;
    }
    if (!rows.length) {
      for (const line of b) rows.push(`<div class="d-add"><span class="g">+</span><span class="t">${esc(line)}</span></div>`);
    }
    void n;
    return `<div class="diff">${rows.join("")}</div>`;
  }

  root.DesmosMd = { render, highlight, diffHtml, esc };
})(typeof window !== "undefined" ? window : globalThis);
