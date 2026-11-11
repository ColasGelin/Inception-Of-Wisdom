// Inception-of-Wisdom dashboard: vanilla JS, one SSE stream, no build step.
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleTimeString() : "—");
const ago = (t) => {
  if (!t) return "—";
  const s = Math.max(0, Math.round(Date.now() / 1000 - t));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
};
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
  return body;
}

const S = { status: null, events: new Map(), features: ["observer"] };
const tabHandlers = {};   // name -> { init(), onStatus(st), onEvent(type, data) }

// ---------------------------------------------------------------- tabs
function showTab(name) {
  document.querySelectorAll("nav.tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  try { localStorage.setItem("iow-tab", name); } catch (_) {}
}
document.querySelectorAll("nav.tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------------------------------------------------------------- header + observer
function stateClass(state) {
  return state === "running" ? "ok" : state === "restarting" || state === "created" ? "warn" : "bad";
}
function renderStatus(st) {
  S.status = st;
  const t = st.target;
  const pt = $("#pill-target");
  pt.textContent = `target: ${t.state}${t.state === "exited" && t.exit_code != null ? ` (${t.exit_code})` : ""}`;
  pt.className = `pill ${stateClass(t.state)}`;
  const pd = $("#pill-docker");
  pd.textContent = t.docker_ok ? "docker: connected" : "docker: unreachable";
  pd.className = `pill ${t.docker_ok ? "ok" : "bad"}`;
  const err = $("#config-error");
  err.classList.toggle("hidden", !st.config_error);
  err.textContent = st.config_error ? `iow.config.yml: ${st.config_error}` : "";
  $("#health").innerHTML = `
    <dt>container</dt><dd>${esc(t.container)}</dd>
    <dt>state</dt><dd class="${stateClass(t.state)}">${esc(t.state)}${t.exit_code != null && t.state !== "running" ? ` (exit ${t.exit_code})` : ""}</dd>
    <dt>since</dt><dd>${ago(t.since)}</dd>
    <dt>started at</dt><dd>${esc(t.started_at || "—")}</dd>
    <dt>restart count</dt><dd>${esc(t.restart_count)}</dd>
    <dt>docker daemon</dt><dd class="${t.docker_ok ? "ok" : "bad"}">${t.docker_ok ? "connected" : esc(t.docker_error || "unreachable")}</dd>`;
  renderProbes(st.probes);
  for (const h of Object.values(tabHandlers)) h.onStatus && h.onStatus(st);
}
function renderProbes(probes) {
  $("#probes").innerHTML = (probes || []).map((p) => {
    const cls = p.class === "ok" ? "ok" : p.class === "4xx" ? "warn" : p.class === "pending" ? "" : "bad";
    const label = p.class === "pending" ? "pending" : p.status ? `${p.status} (${p.class})` : p.class;
    return `<tr><td class="mono">${esc(p.url)}</td><td class="${cls}">${esc(label)}</td><td>${p.latency_ms != null ? `${p.latency_ms} ms` : "—"}</td></tr>`;
  }).join("");
}
function renderEvents() {
  const list = [...S.events.values()].sort((a, b) => b.last_seen - a.last_seen);
  $("#events-count").textContent = `(${list.length})`;
  $("#events").innerHTML = list.length ? list.map((e) => `
    <div class="ev ${e.kind}">
      <div class="top">
        <span><span class="badge ${e.kind}">${e.kind}</span> <span class="badge">${esc(e.source)}</span>
          ${e.restart_loop ? '<span class="badge crash">restart loop</span>' : ""}</span>
        <span class="muted">${e.count > 1 ? `<b>${e.count}×</b> · ${e.count - 1} duplicates merged · ` : ""}last ${ago(e.last_seen)}</span>
      </div>
      <div class="sig">${esc(e.signature)}</div>
      <div class="muted">${esc(e.message)}${e.status ? ` · ${esc(e.status)}` : ""} · first ${fmtTime(e.first_seen)}</div>
      ${e.log_excerpt ? `<details><summary>log excerpt</summary><pre class="code">${esc(e.log_excerpt)}</pre></details>` : ""}
    </div>`).join("") : '<p class="muted">No events yet. Plant a bug: <code>make bug BUG=syntax</code></p>';
  for (const h of Object.values(tabHandlers)) h.onEvents && h.onEvents(list);
}

const MAX_LOG = 600;
function appendLine(pre, html) {
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  pre.insertAdjacentHTML("beforeend", html + "\n");
  while (pre.childNodes.length > MAX_LOG * 2) pre.removeChild(pre.firstChild);
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}
const isErr = (line) => /Traceback|Error|Exception|  File "/.test(line);
function addTargetLog(e) {
  appendLine($("#target-logs"), `<span class="${isErr(e.line) ? "err" : ""}">${esc(e.line)}</span>`);
}
function addAgentLog(e) {
  appendLine($("#agent-logs"), `<span class="muted">${fmtTime(e.ts)}</span> <span class="agent-c">[${esc(e.component)}]</span> ${esc(e.message)}`);
}

// ---------------------------------------------------------------- boot
async function boot() {
  const st = await api("/status");
  S.features = st.features;
  for (const f of ["analyst", "loop"]) {
    const on = S.features.includes(f);
    document.querySelector(`nav.tabs button[data-tab="${f}"]`).classList.toggle("hidden", !on);
    if (on && tabHandlers[f]) tabHandlers[f].init();
  }
  if (S.features.includes("bonus") && tabHandlers.bonus) tabHandlers.bonus.init();
  renderStatus(st);
  const [logs, evs] = await Promise.all([api("/logs?n=300"), api("/events/recent")]);
  logs.lines.forEach(addTargetLog);
  logs.agent.forEach(addAgentLog);
  evs.events.forEach((e) => S.events.set(e.id, e));
  renderEvents();
  let saved = null;
  try { saved = localStorage.getItem("iow-tab"); } catch (_) {}
  if (saved && S.features.includes(saved)) showTab(saved);
  connect();
}

function connect() {
  const es = new EventSource("/events");
  es.addEventListener("log", (m) => addTargetLog(JSON.parse(m.data)));
  es.addEventListener("agent_log", (m) => addAgentLog(JSON.parse(m.data)));
  es.addEventListener("event", (m) => { const e = JSON.parse(m.data); S.events.set(e.id, e); renderEvents(); });
  es.addEventListener("status", (m) => { if (S.status) { S.status.target = JSON.parse(m.data); renderStatus(S.status); } });
  es.addEventListener("probes", (m) => renderProbes(JSON.parse(m.data)));
  for (const type of ["index", "heal", "loop_status"]) {
    es.addEventListener(type, (m) => {
      const data = JSON.parse(m.data);
      for (const h of Object.values(tabHandlers)) h.onEvent && h.onEvent(type, data);
    });
  }
  es.onerror = () => { $("#pill-target").textContent = "agent: reconnecting…"; };
}

// Status also refreshes on a timer so config edits and "ago" labels stay current.
setInterval(() => api("/status").then(renderStatus).catch(() => {}), 5000);

// ---------------------------------------------------------------- Analyst (tab 2)
function chunkHtml(c, extra = "") {
  const score = c.score != null ? `<span class="score">${Number(c.score).toFixed(3)}</span><span class="bar" style="width:${Math.max(2, c.score * 80)}px"></span>` : "";
  return `<div class="chunk"><div class="head"><span class="mono">${esc(c.path)}:${c.start}-${c.end} · ${esc(c.kind)} <b>${esc(c.name)}</b></span>
    <span>${extra}${score}</span></div><pre class="code">${esc(c.text)}</pre></div>`;
}
function renderDiagnosis(d) {
  if (!d) return "";
  const cands = (d.candidates || []).map((c) => `<tr><td class="mono">${esc(c.path)}</td><td>${c.score.toFixed(2)}</td><td>${esc(c.reasons.join("; "))}</td></tr>`).join("");
  const head = d.ok
    ? `<div class="success"><b>Suspect file${d.files.length > 1 ? "s" : ""}:</b> ${d.files.map((f) => `<code>${esc(f)}</code>`).join(", ")}<p>${esc(d.summary)}</p>
       <span class="muted">${d.llm ? `${esc(d.llm.model)} · ${d.llm.elapsed_s}s · ${d.llm.output_tokens ?? "?"} tokens` : ""}</span></div>`
    : `<div class="fail"><b>Diagnosis failed (${esc(d.stage)}):</b> ${esc(d.reason)}<br><span class="muted">The pipeline does not guess: nothing would be patched.</span></div>`;
  const json = d.ok ? { summary: d.summary, files: d.files } : { ok: false, reason: d.reason };
  return `${head}
    <h3>Diagnosis JSON (input of the Wisdom Loop)</h3><pre class="code">${esc(JSON.stringify(json, null, 2))}</pre>
    ${cands ? `<h3>Candidates found by the index before asking the model</h3>
      <table><thead><tr><th>file</th><th>score</th><th>why</th></tr></thead><tbody>${cands}</tbody></table>` : ""}
    ${d.clues ? `<h3>Clues parsed from the log</h3><dl class="kv"><dt>exception</dt><dd>${esc(d.clues.exc_type)}: ${esc(d.clues.message)}</dd>
      <dt>identifiers</dt><dd>${esc((d.clues.identifiers || []).join(", ") || "—")}</dd>
      <dt>traceback files</dt><dd>${esc((d.clues.target_frames || []).map((f) => `${f.path}:${f.line}`).join(", ") || "—")}</dd></dl>` : ""}
    ${(d.chunks || []).length ? `<details><summary>${d.chunks.length} chunks given to the model</summary>${d.chunks.map((c) => chunkHtml(c, `${esc(c.why)} `)).join("")}</details>` : ""}
    ${d.answer ? `<details><summary>raw model answer</summary><pre class="code">${esc(JSON.stringify(d.answer, null, 2))}</pre></details>` : ""}`;
}
tabHandlers.analyst = {
  init() {
    $("#tab-analyst").innerHTML = `
      <div class="grid two">
        <div class="card"><h2>Index <span class="muted">ChromaDB PersistentClient</span></h2><dl class="kv" id="an-index"></dl></div>
        <div class="card"><h2>Local model</h2><dl class="kv" id="an-model"></dl></div>
      </div>
      <div class="card" style="margin-bottom:16px">
        <h2>Retrieval</h2>
        <p class="hint">Free text in, top-k logical chunks out, each with its cosine similarity.</p>
        <form class="row" id="an-search-form"><input type="search" id="an-q" placeholder="e.g. where is the greeting built?" required>
          <input type="text" id="an-k" value="" size="3" placeholder="k" aria-label="top k">
          <button class="btn" id="an-search-btn">Search</button></form>
        <div id="an-results" class="result-box"></div>
      </div>
      <div class="card">
        <h2>Diagnosis</h2>
        <p class="hint">Pick a crash event: the index finds candidate files, the local model chooses among them and explains.</p>
        <div class="row"><select id="an-event"></select><button class="btn" id="an-diag-btn">Diagnose</button></div>
        <div id="an-diag" class="result-box"></div>
      </div>`;
    $("#an-search-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("#an-search-btn"); btn.disabled = true;
      $("#an-results").innerHTML = '<p class="muted">embedding the query…</p>';
      try {
        const k = parseInt($("#an-k").value, 10);
        const r = await api(`/analyst/search?q=${encodeURIComponent($("#an-q").value)}${k ? `&k=${k}` : ""}`);
        $("#an-results").innerHTML = r.results.length ? r.results.map((c, i) => chunkHtml(c, `#${i + 1} `)).join("") : '<p class="muted">no chunks indexed yet</p>';
      } catch (err) { $("#an-results").innerHTML = `<div class="fail">${esc(err.message)}</div>`; }
      btn.disabled = false;
    });
    $("#an-diag-btn").addEventListener("click", async () => {
      const id = $("#an-event").value;
      if (!id) return;
      const btn = $("#an-diag-btn"); btn.disabled = true;
      $("#an-diag").innerHTML = '<p class="muted">asking the local model (CPU inference can take a while)…</p>';
      try { $("#an-diag").innerHTML = renderDiagnosis(await api("/analyst/diagnose", { method: "POST", body: JSON.stringify({ event_id: id }) })); }
      catch (err) { $("#an-diag").innerHTML = `<div class="fail">${esc(err.message)}</div>`; }
      btn.disabled = false;
    });
  },
  onStatus(st) { if (st.analyst) this.render(st.analyst); },
  onEvent(type, data) { if (type === "index") this.render(data); },
  onEvents(list) {
    const sel = $("#an-event"); if (!sel) return;
    const cur = sel.value;
    const crashes = list.filter((e) => e.kind === "crash");
    sel.innerHTML = crashes.length ? crashes.map((e) => `<option value="${e.id}">${esc(e.signature)} (${e.count}×, ${ago(e.last_seen)})</option>`).join("")
      : '<option value="">no crash events yet: make bug BUG=import</option>';
    if (crashes.some((e) => e.id === cur)) sel.value = cur;
  },
  render(a) {
    const m = a.model || {};
    const pm = $("#pill-model");
    pm.classList.remove("hidden");
    pm.textContent = m.model ? `model: ${m.model} · ${m.params_human} params` : `model: ${a.state}`;
    pm.className = `pill ${m.under_3b ? "ok" : a.state === "error" ? "bad" : "warn"}`;
    pm.title = m.parameter_count ? `${m.parameter_count.toLocaleString()} parameters (read from Ollama /api/show)` : "";
    const ix = a.index || {}, ls = ix.last_sync || {};
    $("#an-index").innerHTML = `<dt>state</dt><dd class="${a.state === "ready" ? "ok" : a.state === "error" ? "bad" : "warn"}">${esc(a.state)}${a.error ? ` — ${esc(a.error)}` : ""}</dd>
      <dt>files / chunks</dt><dd>${ix.files ?? "—"} / ${ix.chunks ?? "—"}</dd>
      <dt>embedding model</dt><dd>${esc(ix.embedding_model || "—")}</dd>
      <dt>last sync</dt><dd>${ls.at ? `${ago(ls.at)}: ${ls.changed.length} changed, ${ls.removed.length} removed, ${ls.chunks_embedded} chunks embedded` : "—"}</dd>
      ${ls.changed && ls.changed.length ? `<dt>re-embedded</dt><dd>${esc(ls.changed.join(", "))}</dd>` : ""}
      ${a.reindex_error ? `<dt>reindex</dt><dd class="bad">${esc(a.reindex_error)}</dd>` : ""}`;
    $("#an-model").innerHTML = m.model ? `<dt>chat model</dt><dd>${esc(m.model)}</dd>
      <dt>parameters</dt><dd class="${m.under_3b ? "ok" : "bad"}">${m.parameter_count ? m.parameter_count.toLocaleString() : "?"} (${esc(m.params_human)}) ${m.under_3b ? "&lt; 3B ✓" : "NOT under 3B"}</dd>
      <dt>quantization</dt><dd>${esc(m.quantization || "—")}</dd><dt>runtime</dt><dd>local Ollama, temperature 0, fixed seed, JSON schema output</dd>`
      : `<dt>state</dt><dd>${esc(a.state)}</dd>`;
  },
};

// ---------------------------------------------------------------- Wisdom Loop (tab 3)
const STATE_BADGE = { running: "info", healed: "ok", rolled_back: "crash", failed: "crash", interrupted: "suggestion" };
const STATE_LABEL = { running: "running", healed: "HEALED", rolled_back: "CLEAN FAILURE: rolled back", failed: "FAILED", interrupted: "interrupted" };
const S3 = { heals: new Map(), open: new Set() };
const short = (sha) => (sha ? sha.slice(0, 8) : "—");
function stepsHtml(steps) {
  return `<div class="steps">${steps.map((s) => `<span class="step ${s.ok === true ? "pass" : s.ok === false ? "fail" : "run"}" title="${esc(s.detail)}">${esc(s.name)}${s.llm ? " · llm" : ""}${s.ended_at ? ` · ${(s.ended_at - s.started_at).toFixed(1)}s` : ""}</span>`).join("")}</div>`;
}
function attemptHtml(a) {
  const d = a.diagnosis || {};
  const checks = (a.checks || []).map((c) => `<tr><td>${c.passed ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>'}</td><td>${esc(c.name)}</td><td class="muted">${esc(c.detail)}</td></tr>`).join("");
  const h = a.health;
  const extra = tabHandlers.bonus && tabHandlers.bonus.attemptExtra ? tabHandlers.bonus.attemptExtra(a) : "";
  return `<div class="attempt">
    <div class="row"><b>Attempt ${a.n}</b> <span class="badge ${a.outcome === "healed" ? "ok" : a.outcome ? "crash" : "info"}">${esc(a.outcome || "in progress")}</span>
      ${a.commit ? `<span class="mono">commit ${short(a.commit)}</span>` : ""}</div>
    ${stepsHtml(a.steps)}
    ${d.ok ? `<p><b>Diagnosis:</b> ${d.files.map((f) => `<code>${esc(f)}</code>`).join(", ")}: ${esc(d.summary)}</p>`
      : d.reason ? `<div class="fail">Diagnosis failed (${esc(d.stage)}): ${esc(d.reason)}</div>` : ""}
    ${a.feedback ? `<p class="muted">Input to this attempt included: ${esc(a.feedback)}</p>` : ""}
    ${a.patch ? `<details><summary>patch JSON (${a.patch.files.length} file${a.patch.files.length > 1 ? "s" : ""}): ${esc(a.patch.summary)}</summary><pre class="code">${esc(JSON.stringify(a.patch, null, 2))}</pre></details>` : ""}
    ${extra}
    ${checks ? `<details><summary>sanity checks (${a.checks.filter((c) => c.passed).length}/${a.checks.length} passed)</summary><table>${checks}</table></details>` : ""}
    ${a.commit_message ? `<details><summary>commit message</summary><pre class="code">${esc(a.commit_message)}</pre></details>` : ""}
    ${h ? `<p><b>Post-restart health:</b> <span class="${h.healed ? "ok" : "bad"}">${h.healed ? "healthy" : "not healed"}</span> — ${esc(h.reason)}</p>
      ${h.excerpt && !h.healed ? `<details><summary>new error log (fed to the next attempt)</summary><pre class="code">${esc(h.excerpt)}</pre></details>` : ""}` : ""}
  </div>`;
}
function healHtml(h) {
  const rb = h.rollback && h.rollback.verify;
  const open = S3.open.has(h.id) || h.state === "running";
  return `<div class="heal" data-id="${h.id}">
    <div class="head"><span><span class="badge ${STATE_BADGE[h.state] || ""}">${esc(STATE_LABEL[h.state] || h.state)}</span>
      <span class="mono">${esc(h.signature)}</span></span>
      <span class="muted">${fmtTime(h.started_at)} · ${h.attempts.length} attempt(s)${h.ended_at ? ` · ${(h.ended_at - h.started_at).toFixed(0)}s` : ` · ${esc(h.phase)}`}
        · original ${esc(h.original ? `${h.original.branch} @ ${short(h.original.sha)}` : "—")}</span></div>
    <details class="body" ${open ? "open" : ""} data-heal="${h.id}"><summary>details</summary>
      ${h.reason ? `<p class="${h.state === "healed" ? "" : "bad"}">${esc(h.reason)}</p>` : ""}
      ${h.attempts.map(attemptHtml).join("")}
      ${rb ? `<div class="attempt"><b>Rollback</b> ${stepsHtml(h.steps.filter((s) => s.name !== "snapshot"))}
        <table>${[["HEAD is the original commit", rb.head_matches], ["git status clean", rb.status_clean], ["git diff &lt;original&gt; empty", rb.diff_empty],
          ["every file hashes to the original blob", rb.blobs_match], ["agent commits unreachable from refs", rb.agent_commits_unreachable]]
          .filter(([, v]) => v !== undefined).map(([k, v]) => `<tr><td>${v ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>'}</td><td>${k}</td></tr>`).join("")}</table>
        <p class="muted">Target restarted on ${short(rb.expected)}: ${esc(h.rollback.health ? h.rollback.health.reason : "")}</p></div>` : ""}
      ${tabHandlers.bonus && tabHandlers.bonus.healExtra ? tabHandlers.bonus.healExtra(h) : ""}
      <details><summary>agent log of this heal (${(h.log || []).length} lines)</summary><pre class="code">${esc((h.log || []).map((e) => `${fmtTime(e.ts)} ${e.text}`).join("\n"))}</pre></details>
    </details></div>`;
}
function renderHeals() {
  const list = [...S3.heals.values()].sort((a, b) => b.started_at - a.started_at);
  $("#lp-heals").innerHTML = list.length ? list.map(healHtml).join("") : '<p class="muted">No heal yet. Plant a bug: <code>make bug BUG=syntax</code></p>';
  document.querySelectorAll("#lp-heals details[data-heal]").forEach((d) => d.addEventListener("toggle", () => {
    if (d.open) S3.open.add(d.dataset.heal); else S3.open.delete(d.dataset.heal);
  }));
  if (tabHandlers.bonus && tabHandlers.bonus.onHeals) tabHandlers.bonus.onHeals(list);
}
function renderLoopStatus(ls) {
  const g = ls.guards, c = ls.config;
  const ks = $("#kill-switch");
  ks.checked = g.enabled;
  $("#kill-switch-wrap").className = `pill switch ${g.enabled ? "ok" : "bad"}`;
  $("#kill-switch-label").textContent = g.enabled ? "auto-heal: on" : "auto-heal: OFF (kill switch)";
  const cur = ls.current;
  $("#lp-state").innerHTML = cur ? `<span class="info">HEALING</span> ${esc(cur.signature)} — attempt ${cur.attempt}, step <b>${esc(cur.phase)}</b>`
    : g.enabled ? '<span class="ok">IDLE</span>, watching for crash events' : '<span class="bad">KILL SWITCH ON</span>: every new heal is refused';
  const cds = Object.entries(g.cooldowns);
  $("#lp-guards").innerHTML = `<dt>single-flight</dt><dd>${g.busy ? "busy (one heal running)" : "free"}</dd>
    <dt>heals in the last hour</dt><dd>${g.heals_last_hour} / ${g.max_heals_per_hour}</dd>
    <dt>heals this process</dt><dd>${g.lifetime} / ${g.max_heals_lifetime}</dd>
    <dt>cooldowns</dt><dd>${cds.length ? cds.map(([s, left]) => `${esc(s)}: ${left}s`).join("<br>") : "none"}</dd>`;
  $("#lp-config").innerHTML = `<dt>grace_period_s</dt><dd>${c.grace_period_s}</dd><dt>max_attempts</dt><dd>${c.max_attempts}</dd>
    <dt>cooldown_per_signature_s</dt><dd>${c.cooldown_per_signature_s}</dd><dt>max_heals_per_hour</dt><dd>${c.max_heals_per_hour}</dd>
    <dt>max_heals_lifetime</dt><dd>${c.max_heals_lifetime}</dd><dt>branch</dt><dd>${esc(c.branch)}</dd>`;
  $("#lp-refusals").innerHTML = ls.refusals.length ? ls.refusals.slice().reverse().map((r) =>
    `<tr><td class="mono">${esc(r.signature)}</td><td>${esc(r.reason)}</td><td>${r.count}×</td><td>${ago(r.last_seen)}</td></tr>`).join("")
    : '<tr><td colspan="4" class="muted">none</td></tr>';
}
tabHandlers.loop = {
  init() {
    $("#kill-switch-wrap").classList.remove("hidden");
    $("#kill-switch").addEventListener("change", async (e) => {
      try { await api("/loop/kill-switch", { method: "POST", body: JSON.stringify({ enabled: e.target.checked }) }); }
      catch (err) { alert(err.message); }
      api("/loop/status").then(renderLoopStatus);
    });
    $("#tab-loop").innerHTML = `
      <div class="card" style="margin-bottom:16px"><h2>Loop</h2><p id="lp-state"></p>
        <p class="hint">observe → think → act → verify. On a crash: snapshot the commit, then up to 3 × (diagnose → patch → sanity checks → apply → commit on the heal branch → restart → verify). Not healed after the last attempt: git reset to the snapshot, restart, prove the tree is byte-for-byte the original.</p></div>
      <div class="grid two">
        <div class="card"><h2>Safety bounds</h2><dl class="kv" id="lp-guards"></dl></div>
        <div class="card"><h2>Active timing <span class="muted">iow.config.yml, hot-reloaded</span></h2><dl class="kv" id="lp-config"></dl></div>
      </div>
      <div id="lp-bonus"></div>
      <div class="card" style="margin-bottom:16px"><h2>Heals <span class="muted">every attempt, newest first</span></h2><div id="lp-heals"></div></div>
      <div class="card"><h2>Refused triggers</h2><p class="hint">Crash events the bounds did not let through (deduplicated).</p>
        <table><thead><tr><th>signature</th><th>reason</th><th>count</th><th>last</th></tr></thead><tbody id="lp-refusals"></tbody></table></div>`;
    api("/loop/heals").then((r) => { r.heals.forEach((h) => S3.heals.set(h.id, h)); renderHeals(); });
  },
  onStatus(st) { if (st.loop) renderLoopStatus(st.loop); },
  onEvent(type, data) {
    if (type === "heal") { S3.heals.set(data.id, data); renderHeals(); }
    if (type === "loop_status") renderLoopStatus(data);
  },
};

// Analyst / Wisdom Loop tabs register into tabHandlers further down (p2, p3);
// boot runs once the whole file has been evaluated.
window.addEventListener("DOMContentLoaded", () => boot().catch((e) => {
  document.body.insertAdjacentHTML("afterbegin", `<div class="banner">Agent not reachable: ${esc(e.message)}</div>`);
}));
