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

// Analyst / Wisdom Loop tabs register into tabHandlers further down (p2, p3);
// boot runs once the whole file has been evaluated.
window.addEventListener("DOMContentLoaded", () => boot().catch((e) => {
  document.body.insertAdjacentHTML("afterbegin", `<div class="banner">Agent not reachable: ${esc(e.message)}</div>`);
}));
