/* WVI dashboard front end.
 *
 * EventSource is the browser's built-in Server-Sent Events client. You open it
 * once and the server pushes messages down whenever it likes. If the connection
 * drops the browser reconnects by itself - that behaviour is why SSE beats
 * hand-rolled polling here.
 */

const FIELDS = [
  { key: "temperature_c",     label: "Temperature",  unit: "°C",  dp: 2 },
  { key: "humidity_pct",      label: "Humidity",     unit: "%",   dp: 1 },
  { key: "pressure_hpa",      label: "Pressure",     unit: "hPa", dp: 1 },
  { key: "light_lux",         label: "Light",        unit: "lux", dp: 0 },
  { key: "gas_oxidising_ohm", label: "Oxidising",    unit: "Ω",   dp: 0 },
  { key: "gas_reducing_ohm",  label: "Reducing",     unit: "Ω",   dp: 0 },
  { key: "gas_nh3_ohm",       label: "NH₃",          unit: "Ω",   dp: 0 },
];

const LATENCY_BUDGET_MS = 4000;   // REQ-M-19
const SPARK_POINTS = 40;
const GAUGE_DRILL_THRESHOLD_BAR = 2.0;   // REQ-F-09

const history = Object.fromEntries(FIELDS.map(f => [f.key, []]));

/* ---------------------------------------------------------------- tiles */
const tilesEl = document.getElementById("tiles");
FIELDS.forEach(f => {
  const el = document.createElement("div");
  el.className = "tile";
  el.id = `tile-${f.key}`;
  el.innerHTML =
    `<div class="label">${f.label}</div>
     <div class="value"><span data-v>—</span><span class="unit">${f.unit}</span></div>
     <svg viewBox="0 0 100 30" preserveAspectRatio="none">
       <polyline fill="none" stroke="var(--accent)" stroke-width="1.5" points=""/>
     </svg>`;
  tilesEl.appendChild(el);
});

function sparkline(key) {
  const pts = history[key];
  if (pts.length < 2) return "";
  const min = Math.min(...pts), max = Math.max(...pts);
  const span = (max - min) || 1;
  return pts.map((v, i) => {
    const x = (i / (pts.length - 1)) * 100;
    const y = 28 - ((v - min) / span) * 26;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

function renderAir(ev) {
  const d = ev.data;
  FIELDS.forEach(f => {
    const raw = d[f.key];
    if (raw === undefined || raw === null) return;
    const tile = document.getElementById(`tile-${f.key}`);
    tile.querySelector("[data-v]").textContent = Number(raw).toFixed(f.dp);
    const buf = history[f.key];
    buf.push(Number(raw));
    if (buf.length > SPARK_POINTS) buf.shift();
    tile.querySelector("polyline").setAttribute("points", sparkline(f.key));
    tile.classList.remove("bump");
    void tile.offsetWidth;            // restart the CSS animation
    tile.classList.add("bump");
  });
  renderLatency(ev);
}

/* ------------------------------------------------------------- latency */
function renderLatency(ev) {
  /* Two numbers matter and they are not the same thing:
     - ingest_latency_ms : sensor capture -> server. Measured server-side, so it
       is immune to your laptop's clock being wrong.
     - browser delta     : capture -> painted on screen. The full REQ-M-19 path,
       but only trustworthy if this machine's clock agrees with the producer's. */
  const el = document.getElementById("latency");
  const val = document.getElementById("latency-val");
  const server = ev.ingest_latency_ms;
  const browser = Date.now() - Date.parse(ev.t_capture);
  const worst = Math.max(server, browser);
  val.textContent = `${server.toFixed(0)} / ${browser.toFixed(0)} ms`;
  el.title =
    `server ingest: ${server.toFixed(1)} ms\n` +
    `capture → browser: ${browser.toFixed(0)} ms (assumes synced clocks)\n` +
    `REQ-M-19 budget: ${LATENCY_BUDGET_MS} ms`;
  el.dataset.state = worst <= LATENCY_BUDGET_MS ? "good" : "bad";
}

/* ------------------------------------------------------------- targets */
const targetsEl = document.getElementById("targets");
let targetCount = 0;

function renderTarget(ev, prepend = true) {
  if (targetCount === 0) targetsEl.innerHTML = "";
  targetCount++;
  const d = ev.data;
  const cls = d.class ?? d.cls ?? "?";
  const el = document.createElement("div");
  el.className = "target";
  const bits = [`conf ${(d.confidence * 100).toFixed(0)}%`];
  if (d.aruco_id !== null && d.aruco_id !== undefined) bits.push(`id ${d.aruco_id}`);
  if (d.gauge_value_bar !== null && d.gauge_value_bar !== undefined) {
    bits.push(`${d.gauge_value_bar.toFixed(2)} bar`);
    if (d.gauge_value_bar < GAUGE_DRILL_THRESHOLD_BAR) el.classList.add("gauge-low");
  }
  const t = new Date(ev.t_capture).toLocaleTimeString();
  el.innerHTML =
    `<div class="cls">${cls}</div>
     <div class="time">${t}</div>
     <div class="meta">${bits.join(" · ")}</div>` +
    (el.classList.contains("gauge-low")
      ? `<div class="flag">below 2 bar — drill condition met</div>` : "");
  if (prepend) targetsEl.prepend(el); else targetsEl.appendChild(el);
  while (targetsEl.children.length > 60) targetsEl.lastChild.remove();
}

/* -------------------------------------------------------------- health */
function renderHealth(h) {
  if (!h || !h.sources) return;
  const names = Object.keys(h.sources);
  const el = document.getElementById("sources-val");
  el.textContent = names.length
    ? names.map(n => `${n}${h.sources[n].stale ? "⚠" : ""}`).join(" ")
    : "none";
}
setInterval(() => fetch("/healthz").then(r => r.json()).then(renderHealth).catch(() => {}), 5000);

/* ---------------------------------------------------------------- feed */
const conn = document.getElementById("conn");
const connText = document.getElementById("conn-text");
const es = new EventSource("/api/stream");

es.onopen = () => { conn.dataset.state = "live"; connText.textContent = "live"; };
es.onerror = () => { conn.dataset.state = "down"; connText.textContent = "reconnecting"; };
es.onmessage = e => {
  const ev = JSON.parse(e.data);
  if (ev.type === "snapshot") {
    if (ev.latest_air) renderAir(ev.latest_air);
    (ev.targets || []).slice().reverse().forEach(t => renderTarget(t));
    renderHealth(ev.health);
  } else if (ev.type === "air_reading") {
    renderAir(ev);
  } else if (ev.type === "detection") {
    renderTarget(ev);
  }
};

/* ------------------------------------------------------------- history */
const HIST_COLS = {
  readings: ["t_capture", "source", "temperature_c", "humidity_pct", "pressure_hpa",
             "light_lux", "gas_oxidising_ohm", "gas_reducing_ohm", "gas_nh3_ohm",
             "ingest_latency_ms"],
  detections: ["t_capture", "source", "class", "confidence", "gauge_value_bar",
               "aruco_id", "image_ref", "ingest_latency_ms"],
};

async function loadHistory() {
  const kind = document.getElementById("hist-kind").value;
  const res = await fetch(`/api/history?kind=${kind}&limit=200`);
  const { rows } = await res.json();
  const cols = HIST_COLS[kind];
  const table = document.getElementById("hist");
  table.querySelector("thead").innerHTML =
    `<tr>${cols.map(c => `<th>${c}</th>`).join("")}</tr>`;
  table.querySelector("tbody").innerHTML = rows.length
    ? rows.map(r => `<tr>${cols.map(c => `<td>${r[c] ?? ""}</td>`).join("")}</tr>`).join("")
    : `<tr><td colspan="${cols.length}">No rows logged yet.</td></tr>`;
}

document.getElementById("hist-refresh").addEventListener("click", loadHistory);
document.getElementById("hist-kind").addEventListener("change", loadHistory);
loadHistory();
setInterval(loadHistory, 20000);
