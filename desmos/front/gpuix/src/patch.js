/** Unified git patch for gpuix `<diff patch={...} wordDiff>`.

The element parses `git diff` output. This emits that contract from the
ACP `oldText` / `newText` pair. It is not a second diff viewer.
*/

function lcsOps(a, b) {
  const n = a.length;
  const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push(["eq", a[i]]);
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      ops.push(["del", a[i]]);
      i += 1;
    } else {
      ops.push(["add", b[j]]);
      j += 1;
    }
  }
  while (i < n) {
    ops.push(["del", a[i]]);
    i += 1;
  }
  while (j < m) {
    ops.push(["add", b[j]]);
    j += 1;
  }
  return ops;
}

function splitLines(text) {
  const raw = String(text ?? "");
  if (raw === "") return [];
  const ends = raw.endsWith("\n");
  const body = ends ? raw.slice(0, -1) : raw;
  return body.split("\n");
}

export function unifiedPatch(path, oldText, newText) {
  const file = String(path || "edit").replace(/\\/g, "/");
  const a = splitLines(oldText);
  const b = splitLines(newText);
  const ops = lcsOps(a, b);
  const hunk = [];
  let oldCount = 0;
  let newCount = 0;
  for (const [kind, line] of ops) {
    if (kind === "eq") {
      hunk.push(` ${line}`);
      oldCount += 1;
      newCount += 1;
    } else if (kind === "del") {
      hunk.push(`-${line}`);
      oldCount += 1;
    } else {
      hunk.push(`+${line}`);
      newCount += 1;
    }
  }
  const oldStart = oldCount ? 1 : 0;
  const newStart = newCount ? 1 : 0;
  const header = `@@ -${oldStart},${oldCount} +${newStart},${newCount} @@`;
  return [
    `diff --git a/${file} b/${file}`,
    `--- a/${file}`,
    `+++ b/${file}`,
    header,
    ...hunk,
    "",
  ].join("\n");
}
