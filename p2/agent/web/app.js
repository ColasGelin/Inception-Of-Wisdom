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
    pm.dataset.tip = m.parameter_count ? `${m.parameter_count.toLocaleString()} parameters (read from Ollama /api/show)` : "";
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

// Analyst / Wisdom Loop tabs register into tabHandlers further down (p2, p3);
// boot runs once the whole file has been evaluated.
window.addEventListener("DOMContentLoaded", () => boot().catch((e) => {
  document.body.insertAdjacentHTML("afterbegin", `<div class="banner">Agent not reachable: ${esc(e.message)}</div>`);
}));
