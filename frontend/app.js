const metricConfig = {
  approval_rate: { label: "Approval Rate", format: (v) => (v * 100).toFixed(1) + "%", mode: "high" },
  rolling_success_rate: { label: "Rolling Success", format: (v) => (v * 100).toFixed(1) + "%", mode: "high" },
  retry_amplification_factor: { label: "Retry Amplification", format: (v) => v.toFixed(2), mode: "low" },
  sla_breach_rate: { label: "SLA Breach", format: (v) => (v * 100).toFixed(1) + "%", mode: "low" },
  circuit_open_rate: { label: "Circuit Open", format: (v) => (v * 100).toFixed(1) + "%", mode: "low" },
  timeout_rate: { label: "Timeout", format: (v) => (v * 100).toFixed(1) + "%", mode: "low" },
  p95_latency_ms: { label: "P95 Latency", format: (v) => v.toFixed(1), mode: "low" },
  cost_per_successful_txn: { label: "Cost / Success", format: (v) => "$" + v.toFixed(2), mode: "low" },
  average_attempts_per_txn: { label: "Attempts / Txn", format: (v) => v.toFixed(2), mode: "low" },
  average_decision_latency: { label: "Decision Latency", format: (v) => v.toFixed(2), mode: "low" },
};
const thresholds = {
  approval_rate: [0.9, 0.75],
  rolling_success_rate: [0.9, 0.75],
  retry_amplification_factor: [1.5, 3.0],
  sla_breach_rate: [0.05, 0.15],
  circuit_open_rate: [0.1, 0.5],
  timeout_rate: [0.05, 0.15],
  p95_latency_ms: [200, 500],
  cost_per_successful_txn: [0.3, 0.5],
  average_attempts_per_txn: [1.5, 2.5],
  average_decision_latency: [5, 20],
};
let thresholds_from_backend = null;
const deltaMap = {
  approval_rate: "approval_rate_delta",
  rolling_success_rate: "rolling_success_rate_delta",
  retry_amplification_factor: "retry_amplification_delta",
  sla_breach_rate: "sla_breach_rate_delta",
  circuit_open_rate: "circuit_open_rate_delta",
};
const chartState = {
  points: [],
  outageMarkers: [],
};
let chart;
let tick = 0;
const TAIL_WINDOW = 30;
let previousMetrics = {};
let previousPolicy = {};
// Read theme colors from CSS variables so JS follows the current theme.
function hexToRgba(hex, alpha) {
  if (!hex) return `rgba(0,0,0,${alpha})`;
  let h = String(hex).trim();
  if (h.startsWith("var(")) {
    const name = h.slice(4, -1).trim();
    h = getComputedStyle(document.documentElement).getPropertyValue(name) || h;
  }
  h = h.replace(/^#/, "");
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const num = parseInt(h, 16);
  const r = (num >> 16) & 255;
  const g = (num >> 8) & 255;
  const b = num & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
const __css = window.getComputedStyle(document.documentElement);
const COLORS = {
  good: (__css.getPropertyValue("--good") || "#059669").trim(),
  warn: (__css.getPropertyValue("--warn") || "#d97706").trim(),
  bad: (__css.getPropertyValue("--bad") || "#dc2626").trim(),
  muted: (__css.getPropertyValue("--muted") || "#6b7280").trim(),
  text: (__css.getPropertyValue("--text") || "#0f172a").trim(),
  accent: (__css.getPropertyValue("--accent") || "#0ea5ff").trim(),
  border: (__css.getPropertyValue("--border") || "#e6edf3").trim(),
};
const outageMarkerPlugin = {
  id: "outageMarkerPlugin",
  afterDraw(chartInstance) {
    const { ctx, chartArea, scales } = chartInstance;
    if (!chartArea) return;
    ctx.save();
    chartState.outageMarkers.forEach((xVal) => {
      const x = scales.x.getPixelForValue(xVal);
      ctx.strokeStyle = hexToRgba(COLORS.bad, 0.8);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, chartArea.top);
      ctx.lineTo(x, chartArea.bottom);
      ctx.stroke();
    });
    ctx.restore();
  },
};
function gradeMetric(key, value) {
  const [good, warn] = thresholds[key] || [0, 0];
  if (metricConfig[key].mode === "high") {
    if (value >= good) return "metric-good";
    if (value >= warn) return "metric-warn";
    return "metric-bad";
  }
  if (value <= good) return "metric-good";
  if (value <= warn) return "metric-warn";
  return "metric-bad";
}
function formatDelta(v, key) {
  if (v === undefined || v === null || Number.isNaN(v)) return "—";
  const arrow = v > 0 ? "▲" : v < 0 ? "▼" : "•";
  const sign = v > 0 ? "+" : "";
  const asPct = ["approval_rate", "rolling_success_rate", "sla_breach_rate", "circuit_open_rate", "timeout_rate"].includes(key);
  const amount = asPct ? (v * 100).toFixed(2) + "%" : v.toFixed(3);
  return `${arrow} ${sign}${amount}`;
}
function setHtml(id, text, className) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  if (className) {
    el.classList.remove("metric-good", "metric-warn", "metric-bad");
    el.classList.add(className);
  }
}
function toLines(containerId, lines, classifier) {
  const box = document.getElementById(containerId);
  if (!box) return;
  // Only auto-scroll when the user is near the bottom; otherwise preserve
  // their manual scroll position so they can inspect past entries.
  const nearBottom = (box.scrollHeight - box.clientHeight - box.scrollTop) < 50;
  const frag = document.createDocumentFragment();
  lines.forEach((line) => {
    const div = document.createElement("div");
    div.className = "log-entry" + (classifier ? ` ${classifier(line)}` : "");
    // Prefer a pre-rendered `line` property when present (avoid dumping whole objects)
    let text;
    if (typeof line === "string") text = line;
    else if (line && typeof line === 'object' && 'line' in line) text = line.line;
    else if (line && typeof line === 'object') text = JSON.stringify(line);
    else text = String(line);
    div.textContent = text;
    frag.appendChild(div);
  });
  // Replace content in one operation for fewer reflows
  box.innerHTML = "";
  box.appendChild(frag);
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

function updateStatusPill(status) {
  const el = document.getElementById("systemStatus");
  if (!el) return;
  const s = (status || "IDLE").toUpperCase();
  el.textContent = s;
  el.className = "pill status " + s.toLowerCase();
}

function updateGateway(regimes) {
  ["G1", "G2"].forEach((g) => {
    const el = document.getElementById(g.toLowerCase() + "Status");
    if (!el) return;
    const state = (regimes?.[g] || "HEALTHY").toUpperCase();
    const cls = state === "OUTAGE" ? "outage" : state === "DEGRADED" ? "degraded" : "healthy";
    el.textContent = `● ${g} ${state}`;
    el.className = "pill regime " + cls;
  });
}

function updateEngine(payload) {
  const mode = (payload.engine_state || "monitoring").toLowerCase();
  const pill = document.getElementById("engineState");
  if (pill) {
    const label = mode === "adapting" ? "ADAPTING" : mode === "cooldown" ? "COOLDOWN" : "MONITORING";
    pill.textContent = `● ${label}`;
    pill.className = "pill engine " + mode;
  }
  document.getElementById("lastTrigger").textContent = payload.engine_meta?.last_trigger || "-";
  document.getElementById("lastStatus").textContent = payload.engine_meta?.last_status || "-";
  const cur = Number(payload.engine_meta?.cycles ?? 0);
  const mx = payload.engine_meta?.max_cycles;
  document.getElementById("cycles").textContent = mx ? `${cur} / ${mx}` : String(cur);
  // show explicit adaptation/recovery status when available
  const adaptEl = document.getElementById("adaptationStatus");
  if (adaptEl) {
    adaptEl.textContent = payload.adaptation_status || "-";
  }
  // show TLC checker status
  const tlc = payload.tlc || {};
  const tlcEl = document.getElementById("tlcStatus");
  if (tlcEl) {
    // Prefer explicit tlc.result if available; otherwise fall back to status
    const result = tlc.result ? String(tlc.result).toUpperCase() : (tlc.status ? String(tlc.status).toUpperCase() : "-");
    const ran = !!tlc.ran;
    let label = result;
    if (result === "-" && ran) label = "RAN";
    // If verification_status is present and differs, append indicator
    const ver = tlc.verification_status ? String(tlc.verification_status).toUpperCase() : null;
    tlcEl.textContent = ver && ver !== label ? `${label} (${ver})` : label;
    tlcEl.className = 'mono' + (result === "PASSED" || ver === "PASSED" ? ' tlc-passed' : result === "FAILED" || ver === "FAILED" ? ' tlc-failed' : '');
  }
  // optionally show violations in adaptation log for visibility
  if (Array.isArray(tlc.violations) && tlc.violations.length > 0) {
    const existing = document.getElementById('tlcViolations');
    if (!existing) {
      const box = document.getElementById('adaptationLog');
      if (box) {
        const header = document.createElement('div');
        header.id = 'tlcViolations';
        header.className = 'log-entry tlc-violation';
        header.textContent = 'TLC Violations: ' + tlc.violations.join('; ');
        box.insertBefore(header, box.firstChild);
      }
    } else {
      existing.textContent = 'TLC Violations: ' + tlc.violations.join('; ');
    }
  } else {
    const existing = document.getElementById('tlcViolations');
    if (existing) existing.remove();
  }

  const adaptation = (payload.adaptation_log || []).map((entry) => {
    const ts = entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString() : "--:--:--";
    return { ...entry, line: `[${ts}] ${entry.message || ""}` };
  });
  toLines("adaptationLog", adaptation, (entry) => entry.type || "reasoning");

  // show a short textual reason for the adaptation status when provided by backend
  const reasonEl = document.getElementById("adaptationReason");
  if (reasonEl) {
    const r = payload.adaptation_status_reason || "-";
    // show only the message part if it's a kernel log line with timestamp
    reasonEl.textContent = r;
  }
}

function updatePolicy(policy) {
  const g1 = Number(policy.provider_weights?.G1 ?? 0);
  const g2 = Number(policy.provider_weights?.G2 ?? 0);
  document.getElementById("g1WeightBar").style.width = `${Math.max(0, Math.min(1, g1)) * 100}%`;
  document.getElementById("g2WeightBar").style.width = `${Math.max(0, Math.min(1, g2)) * 100}%`;
  document.getElementById("g1WeightVal").textContent = g1.toFixed(2);
  document.getElementById("g2WeightVal").textContent = g2.toFixed(2);
  document.getElementById("maxRetry").textContent = String(policy.max_retry ?? "—");
  document.getElementById("baseBackoff").textContent = String(policy.base_backoff_ms ?? "—");
  document.getElementById("maxRetriesWindow").textContent = String(policy.max_retries_per_window ?? "—");

  ["g1WeightVal", "g2WeightVal", "maxRetry", "baseBackoff", "maxRetriesWindow"].forEach((id) => {
    const value = document.getElementById(id).textContent;
    if (previousPolicy[id] !== undefined && previousPolicy[id] !== value) {
      const node = document.getElementById(id);
      node.classList.remove("flash");
      void node.offsetWidth;
      node.classList.add("flash");
    }
    previousPolicy[id] = value;
  });
}

function updateChart(approvalRate, forcedColor) {
  tick += 1;
  chartState.points.push({ x: tick, y: approvalRate });
  if (chartState.points.length > TAIL_WINDOW) {
    chartState.points.shift();
  }
  const xMin = Math.max(1, tick - TAIL_WINDOW + 1);
  chartState.outageMarkers = chartState.outageMarkers.filter((v) => v >= xMin);

  chart.data.datasets[0].data = chartState.points;
  chart.data.datasets[0].borderColor = forcedColor;
  chart.data.datasets[0].backgroundColor = forcedColor;
  chart.options.scales.x.min = xMin;
  chart.options.scales.x.max = Math.max(TAIL_WINDOW, tick);
  chart.update("none");
}

function updateMetrics(payload) {
  const metrics = payload.metrics || {};
  Object.keys(metricConfig).forEach((key) => {
    const raw = Number(metrics[key] ?? 0);
    const cls = gradeMetric(key, raw);
    setHtml(key, metricConfig[key].format(raw), cls);

    const mapped = deltaMap[key];
    const backendDelta = mapped ? payload.deltas?.[mapped] : undefined;
    const fallbackDelta = previousMetrics[key] !== undefined ? raw - previousMetrics[key] : undefined;
    setHtml(`${key}_delta`, formatDelta(backendDelta ?? fallbackDelta, key));
    previousMetrics[key] = raw;
  });

  const lineColorClass = gradeMetric("approval_rate", Number(metrics.approval_rate ?? 0));
  const lineColor = lineColorClass === "metric-good" ? COLORS.good : lineColorClass === "metric-warn" ? COLORS.warn : COLORS.bad;
  updateChart(Number(metrics.approval_rate ?? 0), lineColor);
}

async function post(url) {
  const res = await fetch(url, { method: "POST" });
  if (!res.ok) throw new Error(`Failed ${url}: ${res.status}`);
}

function attachControls() {
  document.getElementById("startBtn").addEventListener("click", async () => {
    // Clear Agent Decisions UI immediately for a fresh run
    try { const box = document.getElementById('agentLog'); if (box) box.innerHTML = ''; } catch (e) {}
    await post("/start");
  });
  document.getElementById("stopBtn").addEventListener("click", async () => {
    try {
      await post("/stop");
    } finally {
      // Immediately clear adaptation UI fields and local state for clarity
      try { document.getElementById("lastTrigger").textContent = "-"; } catch (e) {}
      try { document.getElementById("lastStatus").textContent = "-"; } catch (e) {}
      try { document.getElementById("adaptationStatus").textContent = "-"; } catch (e) {}
      try { document.getElementById("cycles").textContent = "0"; } catch (e) {}
      // reset gateway pills to healthy
      updateGateway({ G1: "HEALTHY", G2: "HEALTHY" });
      // clear logs and event tail
      toLines("simulatorLog", []);
      toLines("kernelLog", []);
      toLines("eventTail", []);
      toLines("adaptationLog", []);
      // clear agent decisions panel as well when stopping
      try { renderAgentLog([]); } catch (e) {}
      // clear chart data and outage markers
      chartState.points = [];
      chartState.outageMarkers = [];
      tick = 0;
      if (chart) {
        chart.data.datasets[0].data = [];
        chart.update();
      }
    }
  });
  document.getElementById("injectG1Btn").addEventListener("click", async () => {
    chartState.outageMarkers.push(tick);
    await post("/gateway/G1/outage");
  });

  document.getElementById("outageG1Btn").addEventListener("click", async () => {
    chartState.outageMarkers.push(tick);
    await post("/gateway/G1/outage");
  });
  document.getElementById("outageG2Btn").addEventListener("click", async () => {
    chartState.outageMarkers.push(tick);
    await post("/gateway/G2/outage");
  });
  document.getElementById("recoverG1Btn").addEventListener("click", async () => post("/gateway/G1/recover"));
  document.getElementById("recoverG2Btn").addEventListener("click", async () => post("/gateway/G2/recover"));
}

function initChart() {
  const ctx = document.getElementById("approvalChart");
  chart = new Chart(ctx, {
    type: "line",
    plugins: [outageMarkerPlugin],
    data: {
      datasets: [{
        label: "Approval Rate",
        data: [],
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.25,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: "x",
      animation: false,
      scales: {
        y: {
          type: "linear",
          min: 0,
          max: 1,
          ticks: { color: COLORS.muted },
          grid: { color: hexToRgba(COLORS.muted, 0.15) },
        },
        x: {
          type: "linear",
          min: 1,
          max: TAIL_WINDOW,
          title: { display: true, text: "Time (ticks)", color: COLORS.muted },
          ticks: { color: COLORS.muted, maxTicksLimit: 6, stepSize: 5 },
          grid: { color: hexToRgba(COLORS.muted, 0.08) },
        },
      },
      plugins: {
        legend: { labels: { color: COLORS.text } },
      },
    },
  });
}

function classifyEventLine(line) {
  if (line.event_type === "RouteDecision") return "event-route";
  if (line.event_type === "NewTransaction") return "event-new";
  if (line.event_type === "AttemptResult" && line.status === "SUCCESS") return "event-success";
  if (line.event_type === "AttemptResult" && line.status === "TIMEOUT") return "event-timeout";
  if (line.event_type === "AttemptResult") return "event-failed";
  return "";
}

function renderAgentLog(entries) {
  const box = document.getElementById('agentLog');
  if (!box) return;
  // Preserve user scroll unless they're near the bottom
  const nearBottom = (box.scrollHeight - box.clientHeight - box.scrollTop) < 80;
  box.innerHTML = '';
  entries.forEach((e) => {
    const card = document.createElement('div');
    card.className = 'log-entry agent-entry';
    try {
      const ts = e && e.ts ? new Date(e.ts).toLocaleTimeString() : new Date().toLocaleTimeString();
      const header = document.createElement('div');
      header.className = 'agent-header';
      const badge = document.createElement('div');
      badge.className = 'agent-badge';
      badge.textContent = (e && e.stage) ? e.stage.toUpperCase() : 'AGENT';
      const title = document.createElement('div');
      title.className = 'agent-meta';
      title.textContent = `[${ts}] ${e && (e.schema || '')} ${e && e.proposal_id ? '(proposal=' + e.proposal_id + ')' : ''}`;
      header.appendChild(badge);
      header.appendChild(title);
      card.appendChild(header);

      const body = document.createElement('div');
      body.className = 'agent-body';

      if (e && e.system_prompt) {
        const sec = document.createElement('div'); sec.className = 'agent-section';
        const label = document.createElement('div'); label.textContent = 'SYSTEM'; label.className = 'agent-table-key';
        const pre = document.createElement('pre'); pre.textContent = e.system_prompt;
        sec.appendChild(label); sec.appendChild(pre); body.appendChild(sec);
      }
      if (e && e.prompt) {
        const sec = document.createElement('div'); sec.className = 'agent-section';
        const label = document.createElement('div'); label.textContent = 'PROMPT'; label.className = 'agent-table-key';
        const pre = document.createElement('pre'); pre.textContent = e.prompt;
        sec.appendChild(label); sec.appendChild(pre); body.appendChild(sec);
      }
      if (e && e.response) {
        const sec = document.createElement('div'); sec.className = 'agent-section';
        const label = document.createElement('div'); label.textContent = 'RESPONSE'; label.className = 'agent-table-key';
        const pre = document.createElement('pre'); pre.textContent = typeof e.response === 'string' ? e.response : JSON.stringify(e.response, null, 2);
        sec.appendChild(label); sec.appendChild(pre); body.appendChild(sec);
      }
      if (e && e.error) {
        const sec = document.createElement('div'); sec.className = 'agent-section agent-error';
        sec.textContent = 'ERROR: ' + e.error;
        body.appendChild(sec);
      }

      card.appendChild(body);
    } catch (err) {
      card.textContent = typeof e === 'string' ? e : JSON.stringify(e);
    }
    box.appendChild(card);
  });
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

function connectSse() {
  const source = new EventSource("/stream");
  const handle = (evt) => {
    const payload = JSON.parse(evt.data || "{}");
    // If backend provides authoritative thresholds or max cycles, adopt them
    if (payload.thresholds && !thresholds_from_backend) {
      thresholds_from_backend = payload.thresholds;
      try {
        const t = payload.thresholds;
        // Map backend HealthThresholds to the UI grading bands
        thresholds.approval_rate = [Math.min(1, (t.min_approval_rate || 0.85) + 0.05), Math.max(0, (t.min_approval_rate || 0.85) - 0.1)];
        thresholds.rolling_success_rate = thresholds.approval_rate.slice();
        thresholds.p95_latency_ms = [Math.max(0, (t.max_p95_latency_ms || 500) * 0.6), (t.max_p95_latency_ms || 500)];
        thresholds.timeout_rate = [Math.max(0, (t.max_timeout_rate || 0.05) * 0.6), (t.max_timeout_rate || 0.05)];
        thresholds.sla_breach_rate = [Math.max(0, (t.max_sla_breach_rate || 0.10) * 0.6), (t.max_sla_breach_rate || 0.10)];
        thresholds.retry_amplification_factor = [Math.max(0, (t.max_retry_amplification || 2.0) * 0.6), (t.max_retry_amplification || 2.0)];
        thresholds.circuit_open_rate = [Math.max(0, (t.max_circuit_open_rate || 0.2) * 0.6), (t.max_circuit_open_rate || 0.2)];
      } catch (e) {
        console.warn("Failed to map backend thresholds", e);
      }
    }
    updateStatusPill(payload.system_status);
    updateGateway(payload.gateway_regimes || {});
    updateMetrics(payload);
    updatePolicy(payload.policy || {});
    updateEngine(payload);
    toLines("simulatorLog", payload.simulator_log || []);
    toLines("kernelLog", payload.kernel_log || []);
    toLines("eventTail", payload.event_tail || [], classifyEventLine);
    // Render agent decision events if present
    try {
      renderAgentLog(payload.agent_log || []);
    } catch (e) {
      // ignore render errors
    }
  };

  source.addEventListener("state", handle);
  source.onmessage = handle;
  source.onerror = () => {
    updateStatusPill("IDLE");
  };
}

function init() {
  attachControls();
  initChart();
  connectSse();
}
window.addEventListener("DOMContentLoaded", init);
