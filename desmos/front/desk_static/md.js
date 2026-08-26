/* Desmos desk — GFM subset for streamed assistant markdown.
   Headings, lists, fences, tables, quotes, emphasis, links, inline code.
   Fences are lexed before inline, so a ``` block cannot leak. Highlight
   tokens follow Tokyo Night (the grok-build markdown crate's Syntect theme). */

(function (root) {
  "use strict";

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
  function esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ESC[c]);
  }

  const KW = {
    py: new Set(
      "and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield self True False None match case type".split(
        /\s+/
      )
    ),
    rs: new Set(
      "as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while async await".split(
        /\s+/
      )
    ),
    js: new Set(
      "async await break case catch class const continue debugger default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof var void while with yield of as".split(
        /\s+/
      )
    ),
    go: new Set(
      "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var true false nil".split(
        /\s+/
      )
    ),
  };
  KW.ts = KW.js;
  KW.tsx = KW.js;
  KW.jsx = KW.js;
  KW.python = KW.py;
  KW.rust = KW.rs;
  KW.javascript = KW.js;
  KW.typescript = KW.js;

  function langKey(lang) {
    return String(lang || "").toLowerCase().replace(/^\./, "");
  }

  function highlight(code, lang) {
    const src = String(code);
    const key = langKey(lang);
    if (!key) return esc(src);
    const keywords = KW[key];
    const out = [];
    const re =
      /(\/\/[^\n]*|#(?![\{!])[^\n]*|\/\*[\s\S]*?\*\/)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(\b\d+(?:\.\d+)?\b)|(\b[A-Za-z_][A-Za-z0-9_]*\b)|(\s+)|(.)/g;
    let m;
    while ((m = re.exec(src))) {
      if (m[1]) out.push(`<span class="tok-cmt">${esc(m[1])}</span>`);
      else if (m[2]) out.push(`<span class="tok-str">${esc(m[2])}</span>`);
      else if (m[3]) out.push(`<span class="tok-num">${esc(m[3])}</span>`);
      else if (m[4]) {
        const w = m[4];
        const rest = src.slice(m.index + w.length);
        if (keywords && keywords.has(w)) out.push(`<span class="tok-kw">${esc(w)}</span>`);
        else if (/^\s*\(/.test(rest)) out.push(`<span class="tok-fn">${esc(w)}</span>`);
        else if (/^[A-Z]/.test(w)) out.push(`<span class="tok-ty">${esc(w)}</span>`);
        else out.push(esc(w));
      } else out.push(esc(m[5] || m[6] || ""));
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
    s = s.replace(/(^|[\s>(])((https?:\/\/[^\s<]+))/g, (_, pre, url) => {
      const href = url.replace(/[),.;:!?]+$/, "");
      const tail = url.slice(href.length);
      return `${pre}<a href="${esc(href)}" target="_blank" rel="noreferrer">${esc(href)}</a>${tail}`;
    });
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
      const fence = line.match(/^\s{0,3}```([\w.+-]*)\s*$/);
      if (fence) {
        flushPara();
        const lang = fence[1] || "";
        const body = [];
        i += 1;
        while (i < lines.length && !/^\s{0,3}```\s*$/.test(lines[i])) {
          body.push(lines[i]);
          i += 1;
        }
        if (i < lines.length) i += 1;
        const cls = lang ? ` class="lang-${esc(lang)}"` : "";
        html.push(
          `<div class="fence"><header><span>${esc(
            lang || "code"
          )}</span><button type="button" class="copy">copy</button></header><pre class="code"><code${cls}>${highlight(
            body.join("\n"),
            lang
          )}</code></pre></div>`
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

  function indexDiff(a, b) {
    const rows = [];
    let i = 0;
    let j = 0;
    while (i < a.length || j < b.length) {
      if (i < a.length && j < b.length && a[i] === b[j]) {
        rows.push(["eq", a[i]]);
        i += 1;
        j += 1;
        continue;
      }
      if (i < a.length && (j >= b.length || !b.includes(a[i], j))) {
        rows.push(["del", a[i]]);
        i += 1;
        continue;
      }
      if (j < b.length && (i >= a.length || !a.includes(b[j], i))) {
        rows.push(["add", b[j]]);
        j += 1;
        continue;
      }
      rows.push(["del", a[i]]);
      rows.push(["add", b[j]]);
      i += 1;
      j += 1;
    }
    return rows;
  }

  function lcsOps(a, b) {
    const n = a.length;
    const m = b.length;
    if (n * m > 250000) return indexDiff(a, b);
    const dp = new Array(n + 1);
    for (let i = 0; i <= n; i += 1) dp[i] = new Uint16Array(m + 1);
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const rows = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (a[i] === b[j]) {
        rows.push(["eq", a[i]]);
        i += 1;
        j += 1;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) {
        rows.push(["del", a[i]]);
        i += 1;
      } else {
        rows.push(["add", b[j]]);
        j += 1;
      }
    }
    while (i < n) {
      rows.push(["del", a[i]]);
      i += 1;
    }
    while (j < m) {
      rows.push(["add", b[j]]);
      j += 1;
    }
    return rows;
  }

  function diffHtml(oldText, newText) {
    const a = String(oldText || "").split("\n");
    const b = String(newText || "").split("\n");
    const ops = lcsOps(a, b);
    if (!ops.length) {
      for (const line of b) ops.push(["add", line]);
    }
    const html = ops.map(([kind, line]) => {
      const cls = kind === "eq" ? "d-eq" : kind === "del" ? "d-del" : "d-add";
      const g = kind === "eq" ? "" : kind === "del" ? "−" : "+";
      return `<div class="${cls}"><span class="g">${g}</span><span class="t">${esc(line)}</span></div>`;
    });
    return `<div class="diff">${html.join("")}</div>`;
  }

  root.DesmosMd = { render, highlight, diffHtml, esc };
})(typeof window !== "undefined" ? window : globalThis);
