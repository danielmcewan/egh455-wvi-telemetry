/* WVI dashboard front end.
 *
 * EventSource is the browser's built-in Server-Sent Events client. You open it
 * once and the server pushes messages down whenever it likes. If the connection
 * drops the browser reconnects by itself - that behaviour is why SSE beats
 * hand-rolled polling here.
 *
 * Two different jobs live in this file and it is worth keeping them apart:
 *   - the LIVE half (tiles, gallery, alert) is fed by the SSE stream and never
 *     asks the server for anything;
 *   - the LOG half (search, table, verification) queries the server, because
 *     the flight record is far bigger than what a browser should hold.
 * The search box drives both, which is why it sits above them rather than
 * inside either panel.
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

const LATENCY_BUDGET_MS = 4000;          // REQ-M-19
const REQUIRED_LOG_MINUTES = 10;         // REQ-M-15
const GAUGE_DRILL_THRESHOLD_BAR = 2.0;   // REQ-F-09
const SPARK_POINTS = 40;
const GALLERY_MAX = 60;

const $ = id => document.getElementById(id);
const spark = Object.fromEntries(FIELDS.map(f => [f.key, []]));

/* Everything the search box and the filters currently say. Held in one object
   so the table, the row count and the CSV link cannot drift apart. */
const state = { q: "", offset: 0, total: 0 };

/* ---------------------------------------------------------------- tiles */
const tilesEl = $("tiles");
FIELDS.forEach(f => {
  const el = document.createElement("div");
  el.className = "tile";
  el.id = `tile-${f.key}`;
  el.innerHTML =
    `<div class="label">${f.label}</div>
     <div class="value"><span data-v>—</span><span class="unit">${f.unit}</span></div>
     <svg viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
       <polyline fill="none" stroke="var(--accent)" stroke-width="1.5" points=""/>
     </svg>`;
  tilesEl.appendChild(el);
});

function sparkline(key) {
  const pts = spark[key];
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
    const tile = $(`tile-${f.key}`);
    tile.querySelector("[data-v]").textContent = Number(raw).toFixed(f.dp);
    const buf = spark[f.key];
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
  const el = $("latency");
  const server = ev.ingest_latency_ms;
  const browser = Date.now() - Date.parse(ev.t_capture);
  const worst = Math.max(server, browser);
  $("latency-val").textContent = `${server.toFixed(0)} / ${browser.toFixed(0)} ms`;
  el.title =
    `server ingest: ${server.toFixed(1)} ms\n` +
    `capture → browser: ${browser.toFixed(0)} ms (assumes synced clocks)\n` +
    `REQ-M-19 budget: ${LATENCY_BUDGET_MS} ms`;
  el.dataset.state = worst <= LATENCY_BUDGET_MS ? "good" : "bad";
}

/* ------------------------------------------------------------- targets */
/* The gallery is held as data rather than as DOM nodes. Re-rendering from an
   array is what lets the type filter and the search box change what is shown
   without the newest detection being lost, which is the bug you get if you
   filter by hiding elements. */
const targetEvents = [];
const targetsEl = $("targets");

function targetClass(ev) {
  const d = ev.data || {};
  return d.class ?? d.cls ?? "?";
}

function imageUrl(ref) {
  return ref ? `/api/targets/image/${encodeURIComponent(ref)}` : null;
}

/* Search the VALUES, not the raw JSON.
   Searching the serialised event looked equivalent and was not: every
   detection carries an "aruco_id" key, so a search for "aruco" matched all
   four target types. Field names are structure, not content. */
function haystack(ev) {
  const d = ev.data || {};
  return [
    targetClass(ev), ev.source, ev.seq,
    d.aruco_id, d.confidence, d.gauge_value_bar, d.image_ref,
    d.pose_x_m, d.pose_y_m, d.pose_z_m, poseText(d, { units: false }),
    ev.t_capture, new Date(ev.t_capture).toLocaleTimeString(),
  ].filter(v => v !== null && v !== undefined).join(" ").toLowerCase();
}

function matchesSearch(ev, q) {
  if (!q) return true;
  return haystack(ev).includes(q.toLowerCase());
}

function visibleTargets() {
  const cls = $("tgt-class").value;
  return targetEvents.filter(ev =>
    (!cls || targetClass(ev) === cls) && matchesSearch(ev, state.q));
}

/* HLO-M-3 wants the localisation coordinates shown with the target, not only
   stored. Formatted as one string because three separate fields on a card that
   is 150 px wide is unreadable, and because two of the four target classes
   never carry a pose at all. */
function poseText(d, { units = true } = {}) {
  const n = v => typeof v === "number";
  if (!n(d.pose_x_m) || !n(d.pose_y_m) || !n(d.pose_z_m)) return null;
  const t = `x ${d.pose_x_m.toFixed(2)}, y ${d.pose_y_m.toFixed(2)}, z ${d.pose_z_m.toFixed(2)}`;
  return units ? `${t} m` : t;
}

function targetCard(ev) {
  const d = ev.data || {};
  const cls = targetClass(ev);
  const low = typeof d.gauge_value_bar === "number"
              && d.gauge_value_bar < GAUGE_DRILL_THRESHOLD_BAR;

  const bits = [`conf ${(d.confidence * 100).toFixed(0)}%`];
  if (d.aruco_id !== null && d.aruco_id !== undefined) bits.push(`id ${d.aruco_id}`);
  if (typeof d.gauge_value_bar === "number") bits.push(`${d.gauge_value_bar.toFixed(2)} bar`);
  bits.push(ev.source);

  const pose = poseText(d);

  const url = imageUrl(d.image_ref);
  const when = new Date(ev.t_capture).toLocaleTimeString();

  const el = document.createElement("article");
  el.className = "target" + (low ? " gauge-low" : "");
  el.innerHTML =
    (url
      /* REQ-F-07 wants the images of the targets, not only a list of what was
         found. Lazy loading keeps sixty thumbnails off the critical path. */
      ? `<button type="button" class="thumb" data-full="${url}" data-cap="${cls} · ${when}">
           <img src="${url}" alt="Snapshot of detected ${cls}" loading="lazy" decoding="async">
         </button>`
      : `<div class="thumb thumb-none" aria-hidden="true">no<br>snapshot</div>`) +
    `<div class="target-body">
       <div class="cls">${cls}</div>
       <div class="time">${when}</div>
       <div class="meta">${bits.join(" · ")}</div>
       ${pose ? `<div class="pose" title="UAV position relative to the ArUco marker (HLO-M-3)">${pose}</div>` : ""}
       ${low ? `<div class="flag">below ${GAUGE_DRILL_THRESHOLD_BAR} bar — drill condition met</div>` : ""}
     </div>`;
  return el;
}

function renderTargets() {
  const shown = visibleTargets();
  targetsEl.replaceChildren();
  if (!shown.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = targetEvents.length
      ? "No detections match the current filter."
      : "No targets detected.";
    targetsEl.appendChild(p);
  } else {
    shown.forEach(ev => targetsEl.appendChild(targetCard(ev)));
  }
  $("targets-foot").textContent =
    `${shown.length} shown · ${targetEvents.length} held in the live gallery` +
    (state.q || $("tgt-class").value ? " · filtered" : "");
}

function addTarget(ev, { announce = true } = {}) {
  targetEvents.unshift(ev);
  if (targetEvents.length > GALLERY_MAX) targetEvents.length = GALLERY_MAX;
  if (announce) raiseAlert(ev);
  renderTargets();
}

/* --------------------------------------------------------------- alert */
/* REQ-F-05: "the target identification system shall be capable of alerting the
   GCS of a target's type". The gallery alone is a passive list - an operator
   watching the video pane would miss it - so the newest type is also stated
   once, loudly, in a live region. */
let alertTimer = null;

function raiseAlert(ev) {
  const d = ev.data || {};
  const cls = targetClass(ev);
  const low = typeof d.gauge_value_bar === "number"
              && d.gauge_value_bar < GAUGE_DRILL_THRESHOLD_BAR;

  let text = `${cls.replace("_", " ")} detected`;
  if (d.aruco_id !== null && d.aruco_id !== undefined) text += ` — ArUco id ${d.aruco_id}`;
  if (typeof d.gauge_value_bar === "number") text += ` — reading ${d.gauge_value_bar.toFixed(2)} bar`;
  const pose = poseText(d);
  if (pose) text += ` — UAV at ${pose}`;
  text += ` · ${(d.confidence * 100).toFixed(0)}% confidence · ${new Date(ev.t_capture).toLocaleTimeString()}`;
  if (low) text += " · DRILL CONDITION MET (REQ-F-09)";

  const el = $("alert");
  $("alert-body").textContent = text;
  el.dataset.state = low ? "drill" : "new";
  el.classList.remove("pulse");
  void el.offsetWidth;
  el.classList.add("pulse");

  speak(cls, low);

  // Settle to a quieter state so a stale alert does not look like a live one.
  clearTimeout(alertTimer);
  alertTimer = setTimeout(() => { el.dataset.state = "seen"; }, 15000);
}

/* HLO-M-2: "Once a target is autonomously identified, the (GCS) will notify the
   operator by vocalising the detected target." A live region is read by screen
   readers only; the customer asked for the target to be spoken aloud, so it is.

   Off by default and remembered per browser: an operator debugging at a desk
   does not want a voice every six seconds, and browsers block speech until the
   page has been interacted with anyway. Turn it on before the demo. */
const SPEAK_KEY = "wvi.speakAlerts";
let speakOn = false;
try { speakOn = localStorage.getItem(SPEAK_KEY) === "1"; } catch (_) { /* private mode */ }

const speakToggle = $("speak-toggle");
if (speakToggle) {
  speakToggle.checked = speakOn;
  speakToggle.disabled = !("speechSynthesis" in window);
  speakToggle.addEventListener("change", () => {
    speakOn = speakToggle.checked;
    try { localStorage.setItem(SPEAK_KEY, speakOn ? "1" : "0"); } catch (_) { /* ignore */ }
    if (speakOn) say("Target announcements enabled");
  });
}

function say(text) {
  if (!("speechSynthesis" in window)) return;
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.05;
  // Cancel anything still queued: during a burst of detections the operator
  // needs the newest target, not a backlog read out in order.
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(u);
}

function speak(cls, drill) {
  if (!speakOn) return;
  const spoken = { valve_open: "valve, open", valve_closed: "valve, closed",
                   gauge: "pressure gauge", aruco: "ArUco marker" }[cls] || cls;
  say(drill ? `${spoken}. Drill condition met.` : `${spoken} detected`);
}

$("alert-dismiss").addEventListener("click", () => {
  $("alert").dataset.state = "idle";
  $("alert-body").textContent = "Alert dismissed — watching for the next detection";
});

/* -------------------------------------------------------------- health */
function renderHealth(h) {
  if (!h || !h.sources) return;
  const names = Object.keys(h.sources);
  const el = $("sources-val");
  el.textContent = names.length
    ? names.map(n => `${n}${h.sources[n].stale ? "⚠" : ""}`).join(" ")
    : "none";
  el.title = names.map(n =>
    `${n}: last seen ${h.sources[n].age_s}s ago, ${h.sources[n].dropped_messages} dropped`
  ).join("\n") || "no producer has sent anything yet";
}
setInterval(() => fetch("/healthz").then(r => r.json()).then(renderHealth).catch(() => {}), 5000);

/* ---------------------------------------------------------------- feed */
const conn = $("conn");
const es = new EventSource("/api/stream");

es.onopen = () => { conn.dataset.state = "live"; $("conn-text").textContent = "live"; };
es.onerror = () => { conn.dataset.state = "down"; $("conn-text").textContent = "reconnecting"; };
es.onmessage = e => {
  const ev = JSON.parse(e.data);
  if (ev.type === "snapshot") {
    if (ev.latest_air) renderAir(ev.latest_air);
    // Oldest first so the newest ends up on top, and silently: replaying the
    // backlog on page load must not fire an alert for a target from ten
    // minutes ago.
    (ev.targets || []).slice().reverse().forEach(t => addTarget(t, { announce: false }));
    renderHealth(ev.health);
  } else if (ev.type === "air_reading") {
    renderAir(ev);
  } else if (ev.type === "detection") {
    addTarget(ev);
    // A new detection changes the log, the tally and the drill record.
    scheduleLogRefresh();
  }
};

/* -------------------------------------------------------------- search */
/* One search box drives the gallery (client side, over what is already here)
   and the log table (server side, over the whole flight). Debounced so typing
   does not fire a query per keystroke.

   Free text alone is a poor interface for this data: an operator has to
   already know that "aruco" is a class name and "AQ" is a source before the
   box is any use. Typing "/" therefore turns the box into a command palette
   that lists what is actually searchable, so the vocabulary is discovered
   rather than remembered. Every entry drives a control that already exists
   in the Logged Data panel - the menu is a faster route to those filters,
   never a second, separate filtering path that could disagree with them. */
let searchTimer = null;
const qInput = $("q");
const qHint = $("q-hint");

/* Detection-only filters are meaningless over the air readings table. One
   function so the palette and the Record type dropdown cannot disagree. */
function syncDetOnly() {
  const detections = $("hist-kind").value === "detections";
  document.querySelectorAll(".det-only").forEach(el => { el.hidden = !detections; });
}

/* Set a <select> even when the wanted value is not in it yet. hist-class and
   hist-source are built from what the log actually contains, so "/target
   aruco" before the first ArUco detection would otherwise silently land on
   "Any" - a filter that says one thing and does another. */
function setSelect(id, value) {
  const sel = $(id);
  if (!sel) return;
  if (value && !Array.from(sel.options).some(o => o.value === value)) {
    sel.appendChild(new Option(value, value));
  }
  sel.value = value;
}

/* Apply several controls at once, then refresh once. Dispatching change on
   each select instead would fire a query per control. */
function applyFilters(fn) {
  fn();
  syncDetOnly();
  renderTargets();
  loadHistory({ resetPage: true });
}

/* UTC, because the datetime-local inputs are labelled UTC and boundFrom()
   appends the offset rather than converting. */
function utcMinutesAgo(minutes) {
  return new Date(Date.now() - minutes * 60000).toISOString().slice(0, 19);
}

/* ------------------------------------------------- the slash commands */
/* `name` is what gets typed; `aliases` are the words an operator is likely to
   reach for instead. `values` drives the second stage of the menu. */
const SLASH_COMMANDS = [
  {
    name: "target", aliases: ["class", "type", "valve", "gauge", "aruco", "marker"],
    hint: "Filter to one target type",
    values: [
      { value: "valve_open",   hint: "Open valve" },
      { value: "valve_closed", hint: "Closed valve" },
      { value: "gauge",        hint: "Pressure gauge — carries the bar reading" },
      { value: "aruco",        hint: "ArUco marker — carries the x/y/z coordinates" },
      { value: "", label: "any", hint: "Clear the target type filter" },
    ],
    run: v => applyFilters(() => {
      setSelect("hist-kind", "detections");
      setSelect("hist-class", v);
      setSelect("tgt-class", v);
    }),
  },
  {
    name: "source", aliases: ["subsystem", "from", "aq", "ip", "tai", "sim"],
    hint: "Filter to one producing subsystem",
    values: [
      { value: "AQ",  hint: "Air quality subsystem" },
      { value: "IP",  hint: "Image processing subsystem" },
      { value: "TAI", hint: "Target identification subsystem" },
      { value: "SIM", hint: "Built-in simulator" },
      { value: "", label: "any", hint: "Clear the source filter" },
    ],
    run: v => applyFilters(() => setSelect("hist-source", v)),
  },
  {
    name: "records", aliases: ["kind", "table", "readings", "detections", "air"],
    hint: "Which table the Logged Data panel shows",
    values: [
      { value: "detections", hint: "Target detections (REQ-F-05, F-07)" },
      { value: "readings",   hint: "Air quality readings (REQ-F-06)" },
    ],
    run: v => applyFilters(() => setSelect("hist-kind", v)),
  },
  {
    name: "confidence", aliases: ["conf", "min", "certainty"],
    hint: "Minimum detection confidence",
    values: [
      { value: "0.9", label: "0.90", hint: "Only very confident detections" },
      { value: "0.8", label: "0.80" },
      { value: "0.7", label: "0.70" },
      { value: "0.5", label: "0.50" },
      { value: "", label: "any", hint: "Clear the confidence filter" },
    ],
    run: v => applyFilters(() => {
      setSelect("hist-kind", "detections");
      setSelect("hist-conf", v);
    }),
  },
  {
    name: "since", aliases: ["last", "recent", "time", "window", "when"],
    hint: "Only records captured within a time window",
    values: [
      { value: "1",  label: "1m",  hint: "Last minute" },
      { value: "5",  label: "5m",  hint: "Last five minutes" },
      { value: "10", label: "10m", hint: "Last ten minutes — the REQ-M-15 window" },
      { value: "30", label: "30m", hint: "Last half hour" },
      { value: "",   label: "all", hint: "Clear the time window" },
    ],
    run: v => applyFilters(() => {
      $("hist-since").value = v ? utcMinutesAgo(Number(v)) : "";
      $("hist-until").value = "";
    }),
  },
  {
    name: "rows", aliases: ["page", "limit", "size"],
    hint: "Rows per page in the log table",
    values: [{ value: "50" }, { value: "100" }, { value: "250" }, { value: "500" }],
    run: v => applyFilters(() => setSelect("hist-limit", v)),
  },
  {
    name: "reset", aliases: ["clear", "everything", "all"],
    hint: "Clear every filter and the search text",
    run: () => {
      setSelect("tgt-class", "");
      $("hist-reset").click();
      return "all filters cleared";
    },
  },
  {
    name: "export", aliases: ["csv", "download", "save"],
    hint: "Download the filtered log as CSV",
    run: () => {
      $("hist-csv").click();
      return "CSV download started";
    },
  },
];

/* ------------------------------------------------------ command palette */
const menuEl = $("q-menu");
const menuHeadEl = $("q-menu-head");
const menuListEl = $("q-menu-list");
let menuItems = [];
let menuIndex = -1;

const commandMode = () => qInput.value.startsWith("/");

/* "/tar" -> still choosing a command. "/target ar" -> choosing a value for a
   known command. The space is what separates the two stages. */
function parseCommand(raw) {
  const rest = raw.slice(1);
  const sp = rest.indexOf(" ");
  if (sp === -1) return { word: rest, cmd: null, arg: "" };
  const word = rest.slice(0, sp).toLowerCase();
  const cmd = SLASH_COMMANDS.find(c => c.name === word);
  return { word, cmd, arg: rest.slice(sp + 1).trim() };
}

function commandMatches(c, needle) {
  return !needle
      || c.name.includes(needle)
      || c.hint.toLowerCase().includes(needle)
      || c.aliases.some(a => a.includes(needle));
}

function valueLabel(v) {
  return v.label ?? (v.value || "any");
}

/* Build the list for whatever is currently typed. Stage one offers commands;
   choosing one that takes a value rewrites the box and reopens at stage two
   rather than applying anything, so nothing happens until a value is picked. */
function buildMenuItems(raw) {
  const { word, cmd, arg } = parseCommand(raw);

  if (cmd && cmd.values) {
    const needle = arg.toLowerCase();
    menuHeadEl.textContent = `/${cmd.name} — ${cmd.hint}`;
    return cmd.values
      .filter(v => !needle
                || valueLabel(v).toLowerCase().includes(needle)
                || (v.hint || "").toLowerCase().includes(needle))
      .map(v => ({
        name: `/${cmd.name} ${valueLabel(v)}`,
        hint: v.hint || "",
        choose: () => { cmd.run(v.value); return `${cmd.name}: ${valueLabel(v)}`; },
      }));
  }

  const needle = word.toLowerCase();
  menuHeadEl.textContent = "Filters — type to narrow, ↑↓ to choose, Enter to select";
  return SLASH_COMMANDS
    .filter(c => commandMatches(c, needle))
    .map(c => ({
      name: `/${c.name}`,
      hint: c.hint,
      choose: () => {
        if (!c.values) return c.run() ?? c.name;
        // Takes a value: move to stage two instead of applying.
        qInput.value = `/${c.name} `;
        openMenu();
        return null;
      },
    }));
}

function openMenu() {
  menuItems = buildMenuItems(qInput.value);
  menuListEl.replaceChildren();

  if (!menuItems.length) {
    const li = document.createElement("li");
    li.className = "qmenu-empty";
    // Escape only closes the menu; text starting with "/" is still read as a
    // half-typed command, so the way out is to delete the slash.
    li.textContent = "No filter matches that. Delete the / to search for it as text.";
    menuListEl.appendChild(li);
  } else {
    menuItems.forEach((item, i) => {
      const li = document.createElement("li");
      li.className = "qopt";
      li.id = `q-opt-${i}`;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", "false");
      li.innerHTML = `<span class="qopt-name"></span><span class="qopt-hint"></span>`;
      li.querySelector(".qopt-name").textContent = item.name;
      li.querySelector(".qopt-hint").textContent = item.hint;
      menuListEl.appendChild(li);
    });
  }

  menuEl.hidden = false;
  qInput.setAttribute("aria-expanded", "true");
  setMenuIndex(menuItems.length ? 0 : -1);
}

function closeMenu() {
  menuEl.hidden = true;
  menuItems = [];
  menuIndex = -1;
  qInput.setAttribute("aria-expanded", "false");
  qInput.removeAttribute("aria-activedescendant");
}

function setMenuIndex(i) {
  menuIndex = i;
  Array.from(menuListEl.children).forEach((li, n) => {
    if (li.classList.contains("qmenu-empty")) return;
    li.setAttribute("aria-selected", String(n === i));
  });
  const active = menuListEl.children[i];
  if (active) {
    qInput.setAttribute("aria-activedescendant", active.id);
    active.scrollIntoView({ block: "nearest" });
  } else {
    qInput.removeAttribute("aria-activedescendant");
  }
}

function moveMenu(delta) {
  if (!menuItems.length) return;
  setMenuIndex((menuIndex + delta + menuItems.length) % menuItems.length);
}

/* The hint doubles as the confirmation that a command did something. Without
   it a command applied from the top of the page changes a dropdown two
   screens down and looks like it was ignored. */
function reportApplied(message) {
  if (!qHint) return;
  if (message) {
    qHint.textContent = `Filter applied — ${message}`;
    qHint.dataset.applied = "true";
  } else {
    qHint.innerHTML = 'Press <kbd>/</kbd> to search, then <kbd>/</kbd> again for filters';
    delete qHint.dataset.applied;
  }
}

function chooseMenuItem(i) {
  const item = menuItems[i];
  if (!item) return;
  const applied = item.choose();
  if (applied === null) return;       // stage two just opened
  qInput.value = "";
  runSearch();
  closeMenu();
  reportApplied(applied);
  qInput.focus();
}

/* ------------------------------------------------- free-text searching */
/* Command text is never sent as a search term: "/tar" is half a command, not
   something an operator wants matched against the log. */
function runSearch({ immediate = false } = {}) {
  const wasQ = state.q;
  state.q = commandMode() ? "" : qInput.value.trim();
  $("q-clear").hidden = !qInput.value;
  renderTargets();                       // instant, no round trip
  clearTimeout(searchTimer);
  if (state.q === wasQ && !immediate) return;
  searchTimer = setTimeout(() => loadHistory({ resetPage: true }), immediate ? 0 : 250);
}

qInput.addEventListener("input", () => {
  // A confirmation of the last command goes stale the moment the operator
  // starts typing something else. chooseMenuItem() re-sets it afterwards, so
  // clearing it here does not wipe the message it is about to show.
  if (qHint && qHint.dataset.applied) reportApplied(null);
  if (commandMode()) openMenu(); else closeMenu();
  runSearch();
});

qInput.addEventListener("keydown", e => {
  if (menuEl.hidden) return;
  switch (e.key) {
    case "ArrowDown": e.preventDefault(); moveMenu(1); break;
    case "ArrowUp":   e.preventDefault(); moveMenu(-1); break;
    case "Enter":
    case "Tab":       if (menuIndex < 0) return;
                      e.preventDefault(); chooseMenuItem(menuIndex); break;
    // Escape closes the menu without touching the box, so it does not also
    // wipe what was typed - the document-level handler would do that.
    case "Escape":    e.preventDefault(); e.stopPropagation(); closeMenu(); break;
    default: return;
  }
});

// mousedown rather than click, so the input never loses focus mid-selection.
menuListEl.addEventListener("mousedown", e => {
  const li = e.target.closest(".qopt");
  if (!li) return;
  e.preventDefault();
  chooseMenuItem(Array.from(menuListEl.children).indexOf(li));
});

menuListEl.addEventListener("mousemove", e => {
  const li = e.target.closest(".qopt");
  if (li) setMenuIndex(Array.from(menuListEl.children).indexOf(li));
});

document.addEventListener("mousedown", e => {
  if (!menuEl.hidden && !menuEl.contains(e.target) && e.target !== qInput) closeMenu();
});

$("q-clear").addEventListener("click", () => {
  qInput.value = "";
  closeMenu();
  runSearch({ immediate: true });
  reportApplied(null);
  qInput.focus();
});

document.addEventListener("keydown", e => {
  if (e.key === "/" && document.activeElement !== qInput
      && !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) {
    e.preventDefault();
    qInput.focus();
    qInput.select();
  }
  if (e.key === "Escape") {
    if (!$("lightbox").hidden) closeLightbox();
    else if (document.activeElement === qInput && qInput.value) $("q-clear").click();
  }
});

$("tgt-class").addEventListener("change", renderTargets);

/* ------------------------------------------------------------- history */
const HIST_COLS = {
  readings: ["t_capture", "source", "seq", "temperature_c", "humidity_pct", "pressure_hpa",
             "light_lux", "gas_oxidising_ohm", "gas_reducing_ohm", "gas_nh3_ohm",
             "ingest_latency_ms"],
  detections: ["t_capture", "source", "seq", "class", "confidence", "gauge_value_bar",
               "aruco_id", "pose_x_m", "pose_y_m", "pose_z_m", "image_ref",
               "ingest_latency_ms"],
};

/* The date inputs are labelled UTC and the log is stored in UTC, so the value
   is used as-is rather than being put through the browser's local timezone.
   `until` gets a microsecond tail so that "to 10:30:00" includes a reading
   captured at 10:30:00.4 - stored timestamps compare as text. */
function boundFrom(inputId, end = false) {
  const v = $(inputId).value;
  if (!v) return null;
  const withSeconds = v.length === 16 ? `${v}:00` : v;
  return end ? `${withSeconds}.999999+00:00` : `${withSeconds}+00:00`;
}

function historyParams({ forExport = false } = {}) {
  const kind = $("hist-kind").value;
  const p = new URLSearchParams({ kind });
  if (state.q) p.set("q", state.q);
  const source = $("hist-source").value;
  if (source) p.set("source", source);
  if (kind === "detections") {
    const cls = $("hist-class").value;
    if (cls) p.set("class", cls);
    const conf = $("hist-conf").value;
    if (conf) p.set("min_confidence", conf);
  }
  const since = boundFrom("hist-since");
  const until = boundFrom("hist-until", true);
  if (since) p.set("since", since);
  if (until) p.set("until", until);
  if (!forExport) {
    p.set("limit", $("hist-limit").value);
    p.set("offset", String(state.offset));
  }
  return p;
}

function cellText(col, value) {
  if (value === null || value === undefined || value === "") return "";
  if (col === "confidence") return Number(value).toFixed(2);
  if (col === "ingest_latency_ms") return Number(value).toFixed(1);
  return String(value);
}

async function loadHistory({ resetPage = false } = {}) {
  if (resetPage) state.offset = 0;
  const kind = $("hist-kind").value;
  const params = historyParams();
  const table = $("hist");

  // Keep the CSV pointed at exactly what the table is showing.
  $("hist-csv").href = `/api/export.csv?${historyParams({ forExport: true })}`;

  let data;
  try {
    data = await (await fetch(`/api/history?${params}`)).json();
  } catch {
    $("hist-count").textContent = "could not reach the server";
    return;
  }

  const cols = HIST_COLS[kind];
  state.total = data.total ?? 0;
  table.querySelector("thead").innerHTML =
    `<tr>${cols.map(c => `<th>${c}</th>`).join("")}</tr>`;
  table.querySelector("tbody").innerHTML = data.rows.length
    ? data.rows.map(r => `<tr>${cols.map(c => {
        if (c === "image_ref" && r[c]) {
          const url = imageUrl(r[c]);
          return `<td><a href="${url}" target="_blank" rel="noopener">${r[c]}</a></td>`;
        }
        return `<td>${cellText(c, r[c])}</td>`;
      }).join("")}</tr>`).join("")
    : `<tr><td colspan="${cols.length}">${
        state.q || params.has("class") || params.has("since")
          ? "No rows match the current search or filter."
          : "No rows logged yet."}</td></tr>`;

  const first = state.total ? state.offset + 1 : 0;
  const last = state.offset + data.rows.length;
  const label = kind === "readings" ? "air readings" : "target detections";
  $("hist-count").textContent =
    `${first}–${last} of ${state.total.toLocaleString()} ${label}` +
    (state.q ? ` matching “${state.q}”` : "");
  $("hist-prev").disabled = state.offset <= 0;
  $("hist-next").disabled = last >= state.total;

  // One search box covers two tables. Searching "aruco" while the air
  // readings are showing is a reasonable thing to do and used to look like
  // "no results" - so say where the matches actually are, and offer the switch.
  if (state.q && state.total === 0) await offerOtherKind(kind);
}

async function offerOtherKind(kind) {
  const other = kind === "readings" ? "detections" : "readings";
  const p = historyParams({ forExport: true });
  p.set("kind", other);
  p.set("limit", "1");
  let total = 0;
  try {
    total = (await (await fetch(`/api/history?${p}`)).json()).total ?? 0;
  } catch { return; }
  if (!total) return;
  const label = other === "readings" ? "air readings" : "target detections";
  const el = $("hist-count");
  el.append(" — ");
  const link = document.createElement("button");
  link.type = "button";
  link.className = "linkish";
  link.textContent = `${total.toLocaleString()} ${label} match. Show those instead`;
  link.addEventListener("click", () => {
    $("hist-kind").value = other;
    $("hist-kind").dispatchEvent(new Event("change"));
  });
  el.appendChild(link);
}

/* A detection arriving should update the table, but a burst of them must not
   fire a query each. Coalesce into one refresh a second at most. */
let logTimer = null;
function scheduleLogRefresh() {
  if (logTimer) return;
  logTimer = setTimeout(() => { logTimer = null; loadHistory(); loadVerification(); }, 1000);
}

function pageStep(delta) {
  const size = Number($("hist-limit").value);
  state.offset = Math.max(0, state.offset + delta * size);
  loadHistory();
}

$("hist-prev").addEventListener("click", () => pageStep(-1));
$("hist-next").addEventListener("click", () => pageStep(1));
$("hist-refresh").addEventListener("click", () => loadHistory());
$("hist-limit").addEventListener("change", () => loadHistory({ resetPage: true }));
["hist-source", "hist-class", "hist-conf", "hist-since", "hist-until"].forEach(id =>
  $(id).addEventListener("change", () => loadHistory({ resetPage: true })));

$("hist-kind").addEventListener("change", () => {
  syncDetOnly();
  loadHistory({ resetPage: true });
});

$("hist-reset").addEventListener("click", () => {
  ["hist-source", "hist-class", "hist-conf"].forEach(id => { $(id).value = ""; });
  ["hist-since", "hist-until"].forEach(id => { $(id).value = ""; });
  qInput.value = "";
  qInput.dispatchEvent(new Event("input"));
  loadHistory({ resetPage: true });
});

// Auto-refresh is a checkbox because it fights you otherwise: the table
// jumping under the cursor while you read a row is worse than a stale table.
setInterval(() => { if ($("hist-auto").checked) loadHistory(); }, 20000);

/* ------------------------------------------------------ filter options */
async function loadFilterOptions() {
  let opts;
  try {
    opts = await (await fetch("/api/filters")).json();
  } catch { return; }
  fillSelect($("hist-source"), opts.sources, "Any");
  fillSelect($("hist-class"), opts.classes, "Any");
}

function fillSelect(sel, values, anyLabel) {
  const keep = sel.value;
  sel.replaceChildren();
  sel.appendChild(new Option(anyLabel, ""));
  values.forEach(v => sel.appendChild(new Option(v, v)));
  /* These lists are rebuilt every minute from what the log actually holds.
     A filter chosen from the slash menu can name a class or source that has
     not been logged yet, and dropping it here would silently reset the filter
     to "Any" a minute after the operator set it. */
  if (keep && !values.includes(keep)) sel.appendChild(new Option(keep, keep));
  sel.value = keep;
}

/* -------------------------------------------------------- verification */
/* The requirements this subsystem is graded against are numeric ceilings, and
   the honest way to show one is met is to state the measurement next to the
   limit. This panel is the dashboard's own test evidence. */
function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60), s = Math.floor(seconds % 60);
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

function verifyCard(req, title, verdict, rows, note) {
  return `<div class="vcard" data-verdict="${verdict}">
    <div class="vhead"><span class="tag">${req}</span><h3>${title}</h3>
      <span class="verdict">${verdict}</span></div>
    <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
    ${note ? `<p class="vnote">${note}</p>` : ""}
  </div>`;
}

async function loadVerification() {
  let m, l;
  try {
    [m, l] = await Promise.all([
      fetch("/api/mission").then(r => r.json()),
      fetch("/api/latency").then(r => r.json()),
    ]);
  } catch { return; }

  // Status bar: how long the log actually covers (REQ-M-15).
  const loggedEl = $("logged");
  $("logged-val").textContent = fmtDuration(m.logged_seconds);
  loggedEl.dataset.state = m.pass ? "good" : "wait";
  loggedEl.title = `REQ-M-15 requires ${REQUIRED_LOG_MINUTES} minutes of continuous logged operation.\n`
                 + `Longest unbroken run: ${fmtDuration(m.logged_seconds)}`
                 + ` (${(m.longest_session?.readings ?? 0).toLocaleString()} readings).\n`
                 + `Whole log spans ${fmtDuration(m.record_span_seconds)} across all sessions.`;

  // Per-class tally beside the gallery (REQ-F-05: the operator wants types).
  $("tally").innerHTML = (m.per_class || []).length
    ? m.per_class.map(c =>
        `<button type="button" class="chip" data-class="${c.class}">
           <span class="chip-n">${c.n}</span> ${c.class}</button>`).join("")
    : `<span class="chip chip-empty">no detections logged yet</span>`;

  const lat = l.n
    ? [["samples", l.n.toLocaleString()],
       ["median (p50)", `${l.p50_ms} ms`],
       ["p95", `${l.p95_ms} ms`],
       ["p99", `${l.p99_ms} ms`],
       ["worst", `${l.max_ms} ms`],
       ["budget", `${l.budget_ms} ms`],
       ["over budget", `${l.over_budget}`]]
    : [["samples", "0"]];

  const drill = m.drill_condition || {};
  const html = [
    verifyCard("REQ-M-15", "Continuous logged operation", m.pass ? "PASS" : "PENDING",
      [["longest unbroken run", fmtDuration(m.logged_seconds)],
       ["required", `${REQUIRED_LOG_MINUTES}m 00s`],
       ["readings in that run", (m.longest_session?.readings ?? 0).toLocaleString()],
       ["run started", m.longest_session?.first
          ? m.longest_session.first.slice(0, 19).replace("T", " ") : "—"],
       ["whole log spans", fmtDuration(m.record_span_seconds)],
       ["total readings", (m.readings.count || 0).toLocaleString()]],
      `Measured from stored rows, so a mid-flight restart does not reset it. A silence `
      + `longer than ${m.longest_session?.gap_threshold_s ?? 30}s ends a run — first-to-last `
      + `record would otherwise count the gaps between test sessions as operation.`),

    verifyCard("REQ-M-19", "Capture-to-available latency",
      l.pass === true ? "PASS" : l.pass === false ? "FAIL" : "PENDING", lat,
      "Server-side ingest timing. Producer and server share a clock on the Pi, "
      + "so this figure carries no skew — but it is not the same as an "
      + "end-to-end measurement taken on the Pi under flight load."),

    verifyCard("REQ-F-07", "Target imagery", (m.images_stored || 0) > 0 ? "PASS" : "PENDING",
      [["snapshots stored", m.images_stored ?? 0],
       ["detections logged", (m.detections.count || 0).toLocaleString()],
       ["types seen", (m.per_class || []).length]],
      "Each detection carries a snapshot; the gallery refreshes as they arrive."),

    verifyCard("REQ-F-09", "Drill threshold record",
      drill.readings_below > 0 ? "TRIPPED" : "NOT MET",
      [["threshold", `${drill.threshold_bar ?? GAUGE_DRILL_THRESHOLD_BAR} bar`],
       ["readings below", drill.readings_below ?? 0],
       ["lowest reading", drill.lowest_bar != null ? `${drill.lowest_bar} bar` : "—"]],
      "WVI reports the gauge condition; actuating the drill belongs to EDM."),
  ].join("");

  $("verify").innerHTML = html;
}

// Clicking a tally chip filters the gallery to that type - the count and the
// filter should be the same control, not two that happen to agree.
$("tally").addEventListener("click", e => {
  const chip = e.target.closest(".chip[data-class]");
  if (!chip) return;
  const cls = chip.dataset.class;
  $("tgt-class").value = $("tgt-class").value === cls ? "" : cls;
  renderTargets();
});

/* ------------------------------------------------------------ lightbox */
const lightbox = $("lightbox");

targetsEl.addEventListener("click", e => {
  const btn = e.target.closest(".thumb[data-full]");
  if (!btn) return;
  $("lightbox-img").src = btn.dataset.full;
  $("lightbox-img").alt = btn.dataset.cap;
  $("lightbox-cap").textContent = btn.dataset.cap;
  lightbox.hidden = false;
  $("lightbox-close").focus();
});

function closeLightbox() {
  lightbox.hidden = true;
  $("lightbox-img").removeAttribute("src");
}
$("lightbox-close").addEventListener("click", closeLightbox);
lightbox.addEventListener("click", e => { if (e.target === lightbox) closeLightbox(); });


/* ------------------------------------------------------- navigation menu */
/* HLO-M-3's first bullet. The menu is anchor links, so it works with
   JavaScript disabled and the browser handles the scrolling; all this adds is
   which entry is highlighted, which is what makes it read as a menu rather
   than a row of links. */
(function nav() {
  const links = Array.from(document.querySelectorAll(".mainnav a[data-sec]"));
  if (!links.length) return;

  const byId = new Map(links.map(a => [a.dataset.sec, a]));
  const visible = new Set();

  function mark(link) {
    links.forEach(a => a.removeAttribute("aria-current"));
    if (link) link.setAttribute("aria-current", "true");
  }

  // Highlight on click regardless of whether the observer below is available,
  // so the menu still responds if IntersectionObserver is missing or inert.
  // Move focus as well as the viewport - a menu that only scrolls is not
  // usable from the keyboard.
  links.forEach(a => a.addEventListener("click", () => {
    mark(a);
    const el = document.getElementById(a.dataset.sec);
    if (el) setTimeout(() => el.focus({ preventScroll: true }), 300);
  }));

  if (!("IntersectionObserver" in window)) return;

  const io = new IntersectionObserver(entries => {
    for (const e of entries) {
      if (e.isIntersecting) visible.add(e.target.id);
      else visible.delete(e.target.id);
    }
    // Highlight the topmost section currently on screen, so scrolling past a
    // short panel does not leave two entries lit at once.
    const current = links.map(a => a.dataset.sec).find(id => visible.has(id));
    if (current) mark(byId.get(current));
  }, { rootMargin: "-72px 0px -55% 0px", threshold: 0 });

  byId.forEach((_, id) => {
    const el = document.getElementById(id);
    if (el) io.observe(el);
  });
})();

/* ------------------------------------------------------------- LCD mode */
/* HLO-M-5 requires the LCD display to be selectable from the web interface.
   WVI records the operator's choice and publishes it at GET /api/lcd; the
   enclosure subsystem polls that endpoint and drives the panel. Keeping the
   panel itself out of this subsystem is deliberate - WVI owns no hardware. */
const LCD_LABELS = { ip: "IP address", detection: "live target detection",
                     temperature: "temperature" };

async function loadLcd() {
  const el = $("lcd-state");
  if (!el) return;
  try {
    const r = await fetch("/api/lcd");
    const j = await r.json();
    $("lcd-mode").value = j.mode;
    el.textContent = j.set_at
      ? `showing ${LCD_LABELS[j.mode] || j.mode} — set ${new Date(j.set_at).toLocaleTimeString()}`
      : `showing ${LCD_LABELS[j.mode] || j.mode} (default)`;
  } catch (_) {
    el.textContent = "unavailable";
  }
}

if ($("lcd-set")) {
  $("lcd-set").addEventListener("click", async () => {
    const mode = $("lcd-mode").value;
    $("lcd-state").textContent = "setting…";
    try {
      await fetch(`/api/lcd?mode=${encodeURIComponent(mode)}`, { method: "POST" });
    } catch (_) { /* loadLcd reports the failure */ }
    loadLcd();
  });
}

/* ---------------------------------------------------------------- boot */
syncDetOnly();
loadFilterOptions();
loadHistory();
loadVerification();
setInterval(loadFilterOptions, 60000);
setInterval(loadVerification, 10000);
loadLcd();
