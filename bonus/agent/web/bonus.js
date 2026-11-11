// Bonus dashboard: patch diffs computed here from full contents, per-chunk relevance, heal timeline, PR links.
"use strict";

// Line diff (LCS). Files are small, O(n·m) is fine, and it never trusts a model-written diff.
function lineDiff(a, b) {
  const x = a.split("\n"), y = b.split("\n");
  const n = x.length, m = y.length;
  const L = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    L[i][j] = x[i] === y[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push({ t: " ", s: x[i], a: i + 1, b: j + 1 }); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) { out.push({ t: "-", s: x[i], a: i + 1 }); i++; }
    else { out.push({ t: "+", s: y[j], b: j + 1 }); j++; }
  }
  while (i < n) out.push({ t: "-", s: x[i], a: ++i });
  while (j < m) out.push({ t: "+", s: y[j], b: ++j });
  return out;
}
function diffHtml(path, before, after) {
  const ops = lineDiff(before ?? "", after ?? "");
  const keep = new Set();
  ops.forEach((o, k) => { if (o.t !== " ") for (let d = -3; d <= 3; d++) keep.add(k + d); });
  let html = "", last = -2;
  ops.forEach((o, k) => {
    if (!keep.has(k)) return;
    if (k !== last + 1) html += `<span class="hunk">@@ ${before == null ? "new file" : `line ${o.a ?? o.b}`} @@</span>\n`;
    last = k;
    html += `<span class="${o.t === "+" ? "add" : o.t === "-" ? "del" : ""}">${o.t} ${esc(o.s)}</span>\n`;
  });
  const added = ops.filter((o) => o.t === "+").length, removed = ops.filter((o) => o.t === "-").length;
  return `<div class="chunk"><div class="head"><span class="mono">${esc(path)}</span><span><span class="ok">+${added}</span> <span class="bad">−${removed}</span></span></div>
    <pre class="code diff">${html || '<span class="muted">no change</span>'}</pre></div>`;
}

tabHandlers.bonus = {
  repoUrl: null,
  init() {
    api("/bonus/gitea").then((g) => { this.repoUrl = g.repo_url; this.gitea = g; renderHeals(); }).catch(() => {});
  },
  attemptExtra(a) {
    let html = "";
    if (a.patch && a.originals) {
      html += `<h3>Patch as a diff <span class="muted">computed by the dashboard from the full before/after files</span></h3>` +
        a.patch.files.map((f) => diffHtml(f.path, a.originals[f.path], f.op === "delete" ? "" : f.content)).join("");
    }
    const d = a.diagnosis || {};
    if ((d.chunks || []).length) {
      const max = Math.max(...d.chunks.map((c) => c.score || 0), 0.0001);
      html += `<details><summary>per-chunk relevance (${d.chunks.length} chunks shown to the model)</summary><table>
        <thead><tr><th>chunk</th><th>why</th><th>cosine</th></tr></thead><tbody>${d.chunks.map((c) => `<tr><td class="mono">${esc(c.id)} <b>${esc(c.name || "")}</b></td>
        <td>${esc(c.why)}</td><td>${c.score != null ? `<span class="score">${c.score.toFixed(3)}</span><span class="bar" style="width:${Math.max(2, (c.score / max) * 90)}px"></span>` : '<span class="muted">from the traceback</span>'}</td></tr>`).join("")}</tbody></table></details>`;
    }
    if ((d.candidates || []).length) {
      html += `<details><summary>candidate ranking (index, before the model)</summary><table>${d.candidates.map((c) =>
        `<tr><td class="mono">${esc(c.path)}</td><td>${c.score.toFixed(2)}</td><td class="muted">${esc(c.reasons.join("; "))}</td></tr>`).join("")}</table></details>`;
    }
    return html;
  },
  healExtra(h) {
    if (h.pr) return `<p>Pull request: <a href="${esc(h.pr.url)}" target="_blank" rel="noopener">#${h.pr.number}</a> ${esc(h.pr.head)} → ${esc(h.pr.base)} on the local Gitea${h.pr.updated ? " (updated)" : ""}. Never merged by the agent.</p>`;
    if (h.pr_error) return `<p class="bad">Pull request not opened: ${esc(h.pr_error)}</p>`;
    return "";
  },
  onHeals(list) {
    const box = $("#lp-bonus");
    if (!box) return;
    const done = list.filter((h) => h.state !== "running");
    const healed = done.filter((h) => h.state === "healed");
    const mean = healed.length ? healed.reduce((s, h) => s + (h.ended_at - h.started_at), 0) / healed.length : null;
    const dots = list.slice().reverse().map((h) =>
      `<span class="dot ${h.state}" title="${esc(`${fmtTime(h.started_at)} · ${STATE_LABEL[h.state] || h.state} · ${h.signature} · ${h.attempts.length} attempt(s)`)}"></span>`).join("");
    box.innerHTML = `<div class="card" style="margin-bottom:16px"><h2>Timeline <span class="muted">oldest → newest</span></h2>
      <div class="timeline">${dots || '<span class="muted">no heal yet</span>'}</div>
      <p class="muted" style="margin-top:8px">${done.length} finished · <span class="ok">${healed.length} healed</span> ·
        <span class="bad">${done.filter((h) => h.state === "rolled_back").length} rolled back</span>${mean != null ? ` · mean time to heal ${mean.toFixed(0)}s` : ""}
        ${this.repoUrl ? ` · <a href="${esc(this.repoUrl)}" target="_blank" rel="noopener">Gitea repository</a>${this.gitea && !this.gitea.bootstrapped ? " (bootstrapping…)" : ""}` : ""}</p></div>`;
  },
};
