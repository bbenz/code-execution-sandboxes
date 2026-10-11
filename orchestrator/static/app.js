"use strict";

const PHASES = ["creating", "health", "uploading", "policy", "compile", "test", "lint", "auditing", "cleanup"];
const PHASE_LABEL = {
  creating: "Booting sandbox", health: "Health probe", uploading: "Uploading candidate",
  policy: "Static scan", compile: "Compiling", test: "Running tests", lint: "Linting",
  auditing: "Egress audit", cleanup: "Deleting sandbox",
};

const el = (id) => document.getElementById(id);
const state = { config: null, lanes: new Map(), source: null, runId: null };

async function loadConfig() {
  const config = await (await fetch("/api/config")).json();
  state.config = config;
  el("backendName").textContent = config.backend.title || config.backend.name;
  const tele = el("telemetryPill");
  tele.textContent = config.telemetry ? "Azure Monitor on" : "telemetry off";
  tele.classList.toggle("on", config.telemetry);
  el("tenantPill").textContent = "tenant: " + config.tenant;

  const targetSel = el("target");
  targetSel.innerHTML = "";
  for (const t of config.targets) {
    const opt = document.createElement("option");
    opt.value = t.name;
    opt.textContent = `${t.title} (${t.language})`;
    targetSel.appendChild(opt);
  }
  if (!config.llm_configured) {
    el("generator").value = "recorded";
    for (const opt of el("generator").options) if (opt.value === "llm") opt.disabled = true;
  }
  renderIssue();
}

async function renderIssue() {
  const name = el("target").value;
  if (!name) return;
  const target = await (await fetch(`/api/targets/${name}`)).json();
  el("issue").innerHTML = `<h2>${escapeHtml(target.title)}</h2><p>${escapeHtml(target.issue)}</p>
    <p style="margin-top:6px">Editable: ${target.editable.map((f) => `<code>${escapeHtml(f.split("/").pop())}</code>`).join(" ")}</p>`;
}

function log(message) {
  const box = el("log");
  const line = document.createElement("div");
  line.textContent = message;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function laneEl(id) {
  return state.lanes.get(id)?.node;
}

function ensureWave(wave) {
  let container = document.querySelector(`.wave[data-wave="${wave}"]`);
  if (!container) {
    container = document.createElement("div");
    container.className = "wave";
    container.dataset.wave = wave;
    container.innerHTML = `<div class="wave-title">Wave ${wave} <span class="wstatus"></span></div><div class="lanes"></div>`;
    el("waves").appendChild(container);
  }
  return container.querySelector(".lanes");
}

function addLane(candidate) {
  const lanes = ensureWave(candidate.wave);
  const node = document.createElement("div");
  node.className = "lane queued";
  node.dataset.id = candidate.id;
  node.innerHTML = `
    <div class="strategy">${escapeHtml(candidate.strategy)}</div>
    <div class="author">${escapeHtml(candidate.author)} · ${candidate.files.length} file(s)</div>
    <div class="steps">${PHASES.map((p) => `<div class="step" data-step="${p}" title="${PHASE_LABEL[p]}"></div>`).join("")}</div>
    <div class="phase"><span class="dot"></span><span class="phase-text">queued</span></div>
    <div class="verdict" style="display:none"></div>`;
  lanes.appendChild(node);
  state.lanes.set(candidate.id, { node, candidate });
}

function setPhase(id, phase, detail) {
  const node = laneEl(id);
  if (!node) return;
  node.classList.add("running");
  node.querySelector(".phase-text").textContent = (PHASE_LABEL[phase] || phase) + (detail ? ` — ${detail}` : "");
  const index = PHASES.indexOf(phase);
  node.querySelectorAll(".step").forEach((step, i) => {
    if (i < index) step.classList.add("done");
    if (i === index) step.classList.add("done");
  });
}

function finishLane(outcome) {
  const entry = state.lanes.get(outcome.candidate.id);
  if (!entry) return;
  const node = entry.node;
  node.classList.remove("running", "queued");
  const v = outcome;
  const steps = node.querySelectorAll(".step");
  const good = v.status === "passed";
  steps.forEach((step) => {
    step.classList.remove("done");
    step.classList.add(good ? "ok" : "bad");
  });
  node.querySelector(".phase").style.display = "none";
  const verdict = node.querySelector(".verdict");
  verdict.style.display = "block";
  const tests = v.tests ? `<span class="tests">${v.tests.passed}/${v.tests.total} tests</span>` : "";
  const diff = v.diff ? ` · +${v.diff.lines_added}/-${v.diff.lines_removed}` : "";
  let audit = "";
  if (v.audit && v.audit.status === "violation") {
    const a = v.audit.denied[0] || {};
    audit = `<div class="audit block">⛔ egress blocked: ${escapeHtml((a.host || "") + (a.path || ""))}</div>`;
  } else if (v.audit && v.audit.status === "clean") {
    audit = `<div class="audit">✓ no outbound attempts</div>`;
  }
  verdict.innerHTML = `<span class="badge ${v.status}">${v.status}</span> ${tests}${diff}
    <div class="reason">${escapeHtml(v.reason || "")}</div>${audit}`;
}

function markWinner(id) {
  const node = laneEl(id);
  if (!node) return;
  node.classList.add("win");
  if (!node.querySelector(".crown")) {
    const crown = document.createElement("div");
    crown.className = "crown";
    crown.textContent = "👑";
    node.appendChild(crown);
  }
}

function handle(event) {
  switch (event.type) {
    case "run.started":
      el("backendName").textContent = event.backend.title || event.backend.name;
      log(`Run ${event.run_id} started (${event.kind}) on ${event.backend.name}`);
      break;
    case "wave.generating":
      log(`Wave ${event.wave}: asking ${event.generator} for candidates…`);
      break;
    case "wave.started":
      for (const c of event.candidates) addLane(c);
      break;
    case "capacity": {
      el("capPill").textContent = `${event.active} sandbox${event.active === 1 ? "" : "es"} active · ${event.cpu} / ${event.memory} each`;
      break;
    }
    case "candidate.phase":
      setPhase(event.id, event.phase, event.detail);
      break;
    case "candidate.health":
      if (!event.ok) log(`✗ ${event.id} failed its health probe`);
      break;
    case "candidate.sandbox":
      setPhase(event.id, "health", `sandbox ${event.sandbox_id?.slice(0, 18) || ""} in ${event.create_ms} ms`);
      break;
    case "candidate.finished":
      finishLane(event.outcome);
      break;
    case "wave.finished": {
      const c = event.counts || {};
      const summary = Object.entries(c).map(([k, v]) => `${v} ${k}`).join(", ");
      const title = document.querySelector(`.wave[data-wave="${event.wave}"] .wstatus`);
      if (title) title.textContent = `— ${summary}`;
      log(`Wave ${event.wave}: ${summary}` + (event.winner ? ` · winner ${event.winner}` : " · no winner, looping"));
      break;
    }
    case "run.finished":
      finishRun(event);
      break;
    case "log":
      log(`[${event.level}] ${event.message}`);
      break;
  }
}

function finishRun(event) {
  setButtonsBusy(false);
  const banner = el("banner");
  banner.classList.add("show");
  if (event.kind === "benchmark") {
    const rows = (event.lanes || []).map((l) => `<strong>${l.label}</strong>: build ${Math.round(l.build_ms)} ms (${l.status})`).join(" &nbsp;·&nbsp; ");
    banner.innerHTML = `<strong>Benchmark complete.</strong> ${rows}`;
    return;
  }
  if (event.winner) {
    markWinner(event.winner.candidate.id);
    banner.classList.add("win");
    const w = event.winner;
    banner.innerHTML = `<strong>👑 Winner after ${event.waves} wave(s):</strong> “${escapeHtml(w.candidate.strategy)}” —
      ${w.summary || ""} · +${w.diff?.lines_added}/-${w.diff?.lines_removed} across ${event.sandboxes} sandboxes.`;
  } else {
    banner.classList.remove("win");
    banner.innerHTML = `<strong>No candidate passed</strong> after ${event.waves} wave(s) and ${event.sandboxes} sandboxes.` +
      (event.error ? ` <span style="color:var(--amber)">${escapeHtml(event.error)}</span>` : "");
  }
}

function reset() {
  el("waves").innerHTML = "";
  el("banner").className = "result-banner";
  el("banner").innerHTML = "";
  el("log").innerHTML = "";
  state.lanes.clear();
  if (state.source) { state.source.close(); state.source = null; }
}

function stream(runId) {
  state.runId = runId;
  const source = new EventSource(`/api/runs/${runId}/events`);
  state.source = source;
  source.onmessage = (message) => {
    try { handle(JSON.parse(message.data)); } catch (e) { /* keep-alive */ }
  };
  source.onerror = () => { /* EventSource retries automatically */ };
}

function setButtonsBusy(busy) {
  el("startBtn").disabled = busy;
  el("benchBtn").disabled = busy;
  el("startBtn").textContent = busy ? "Racing…" : "Start the race";
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || response.statusText);
  }
  return response.json();
}

el("startBtn").addEventListener("click", async () => {
  reset();
  setButtonsBusy(true);
  try {
    const { run_id } = await post("/api/race", {
      target: el("target").value,
      generator: el("generator").value,
      waves: Number(el("waves").value),
      candidates_per_wave: Number(el("cpw").value),
      inject_rogue: el("rogue").checked,
    });
    stream(run_id);
  } catch (e) {
    setButtonsBusy(false);
    log("Error: " + e.message);
    alert(e.message);
  }
});

el("benchBtn").addEventListener("click", async () => {
  reset();
  setButtonsBusy(true);
  try {
    const { run_id } = await post("/api/benchmark", { target: el("target").value });
    stream(run_id);
  } catch (e) {
    setButtonsBusy(false);
    alert(e.message);
  }
});

el("sweepBtn").addEventListener("click", async () => {
  try {
    const { deleted } = await post("/api/sweep", {});
    log(`Swept ${deleted} leftover sandbox(es).`);
  } catch (e) { alert(e.message); }
});

el("target").addEventListener("change", renderIssue);

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

loadConfig().catch((e) => log("Failed to load config: " + e.message));
