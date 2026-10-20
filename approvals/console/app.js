"use strict";
// Lachesis Approvals Cockpit. Vanilla JS; every value reaches the DOM via textContent.
const cfg = window.LACHESIS_CONFIG || {};
const VIDEO_URL = "";
const CONFIRM_CLASSES = ["destructive_write", "confused_deputy", "untrusted_to_action"];
const TERMINAL = ["DONE", "REFUSED_DONE", "EXPIRED", "ERROR"];
const STATUS = {
  STARTING: ["Agent starting", "progress"], PENDING: ["Waiting for a person", "warning"],
  APPROVED: ["Approved, agent resuming", "progress"], DECLINED: ["Declined, agent resuming", "progress"],
  RESUMING: ["Agent resuming", "progress"], DONE: ["Finished", "success"],
  REFUSED_DONE: ["Finished without running the paused call", "stopped"],
  EXPIRED: ["Expired: nobody decided, treated as declined", "stopped"], ERROR: ["Run failed", "error"],
};
const ICON = { success: "\u2713", warning: "!", error: "\u2715", info: "i", progress: "\u2026", stopped: "\u25A0" };
const EFFECT = { benign: "Low risk", destructive_write: "Deletes or overwrites data", exfiltration: "Sends data outside", untrusted_to_action: "Acts on untrusted input", confused_deputy: "Uses authority on someone else's behalf" };
const LAYER = { decider: "Strands Decider 2B", secretscan: "Secret scan (deterministic)", tier0: "Safe read (deterministic)", unavailable: "Fail-closed: Decider did not answer", refusal: "Repeat of a call already declined" };
const CANNED = { write_file: "cleanup_notes.txt", delete_file: "refunds_prod_config.py" };

// Committed benchmark intercepts from evaluation/*.jsonl (no synthetic data).
// Explorer, simulator and metrics data: generated from the committed rows by
// scripts/build_console_data.py into data.js. Nothing on those screens is typed by hand.
const DATA = window.LACHESIS_DATA || { set50: [], expanded: [], baseline: {}, g4dn: {}, accuracy50: {}, sim50: {}, sim128_test: {}, dev_lock: 0 };
const THRESHOLDS = ["0.60", "0.70", "0.80", "0.90"];
const SRC = { "50": "evaluation/threshold_cases.jsonl, gpu_threshold_2026-10-13.jsonl (EC2 GPU score), sagemaker_g4dn_2026-10-16.jsonl (hosted), baseline_2026-10-13.jsonl (SDK)",
  "128": "evaluation/expanded_cases.jsonl, expanded_cpu_2026-10-16.jsonl (CPU score)" };
const RULE_TEXT = { R1: "Score under the line, any class: pause", R2: "Exfiltration at or above the line: deny", R3: "Destructive write, untrusted input acted on, or confused deputy: pause",
  R4: "A risk class Decider does not know: pause", R5: "Benign but not grounded in what the user said: guide", R6: "Benign, grounded, at or above the line: proceed",
  T0: "Tier-0 safe read: proceeds before Decider is asked" };
const GROUPS = [["all", "All"], ["destructive_write", "Destructive"], ["exfiltration", "Exfiltration"], ["deputy", "Deputy / untrusted"], ["benign", "Benign"]];
const groupOf = c => (c.label_risk === "confused_deputy" || c.label_risk === "untrusted_to_action" ? "deputy" : c.label_risk);
const explorer = { suite: "50", filter: "all", q: "" };
const allCases = () => DATA.set50.concat(DATA.expanded);
const findCase = id => allCases().find(c => c.id === id);
const sdkText = c => (c.suite !== "50" ? "no SDK baseline for this suite" : c.sdk === "pause" ? "paused" : c.needs_person ? "let it run (missed)" : "let it run");

const $ = (sel, root = document) => root.querySelector(sel);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat(Infinity)) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
  return el;
}

const timers = new Set();
let gen = 0;
function later(fn, ms) { const id = setTimeout(() => { timers.delete(id); fn(); }, ms); timers.add(id); return id; }
function every(fn, ms) { const id = setInterval(fn, ms); timers.add(id); return id; }
function clearTimers() { timers.forEach(id => { clearTimeout(id); clearInterval(id); }); timers.clear(); }

const auth = { token: null, user: "" };
function adopt(token) {
  try {
    const p = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    if (p.exp && p.exp * 1000 < Date.now()) return false;
    auth.token = token; auth.user = p["cognito:username"] || p.username || ""; return true;
  } catch (e) { return false; }
}
function store(fn) { try { return fn(sessionStorage); } catch (e) { return null; } }
function signOut() { auth.token = null; auth.user = ""; store(s => s.removeItem("lachesis_id_token")); renderAuth(); }
function renderAuth() {
  const on = !!auth.token;
  $("#open-signin").hidden = on; $("#signout").hidden = !on; $("#nav-queue").hidden = !on; $("#who").hidden = !on;
  if ($("#queue-actions")) $("#queue-actions").hidden = !on;
  $("#who").textContent = on ? "Signed in as " + (auth.user || "approver") : "";
}

async function api(path, opts = {}) {
  const headers = { "content-type": "application/json" }, approver = path.startsWith("/approvals");
  if (approver && auth.token) headers.authorization = auth.token;
  try {
    const res = await fetch((cfg.apiUrl || "") + path, { ...opts, headers });
    const body = await res.json().catch(() => ({}));
    if (res.status === 401 && approver) { signOut(); $("#session-alert").hidden = false; }
    return { status: res.status, body: body || {} };
  } catch (e) { return { status: 0, body: {} }; }
}
function errText(out) {
  if (out.status === 0) return "Could not complete: the network request failed. Check your connection and try again.";
  return "Could not complete: " + ((out.body && out.body.error) || "HTTP " + out.status) + ".";
}
function netFail(n) {
  const el = $("#net-alert"); el.hidden = n < 3;
  el.textContent = n >= 3 ? "Could not complete: the last " + n + " checks failed to reach the Lachesis API. Still trying." : "";
}

const pad = n => String(n).padStart(2, "0");
const hhmm = ms => { const d = new Date(ms); return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()); };
const hhmmss = ms => hhmm(ms) + ":" + pad(new Date(ms).getUTCSeconds());
const mmss = s => pad(Math.floor(s / 60)) + ":" + pad(Math.floor(s % 60));
function age(ms) { const m = Math.max(0, Math.round((Date.now() - ms) / 60000)); return m < 1 ? "under 1 min" : m + " min"; }
function expiry(view) {
  const c = view.card || {}, exp = Date.parse(c.expires_at), made = Date.parse(c.created_at);
  if (!isFinite(exp)) return null;
  if (isFinite(made) && view.created_ts && made - view.created_ts * 1000 < 600000) return Math.min(exp, view.created_ts * 1000 + (exp - made));
  return exp;
}
function tickClocks() {
  document.querySelectorAll("[data-exp]").forEach(el => {
    const left = (Number(el.dataset.exp) - Date.now()) / 1000;
    el.classList.toggle("danger", left < 300);
    if (left > 0) { el.textContent = "Expires in " + mmss(left) + " (" + hhmm(Number(el.dataset.exp)) + " UTC)"; return; }
    el.textContent = "Expired at " + hhmm(Number(el.dataset.exp)) + " UTC, closing";
    const card = el.closest(".s4"); if (card) card.querySelectorAll(".actionbar .btn").forEach(b => { b.disabled = true; });
  });
  document.querySelectorAll("[data-since]").forEach(el => { el.textContent = mmss(Math.max(0, (Date.now() - Number(el.dataset.since)) / 1000)) + " ago"; });
}
setInterval(tickClocks, 1000);

function parseReason(reason) {
  const r = { args: null, argsText: "", grounded: null, threshold: null, rule: null, message: "" };
  if (!reason) return r;
  const m = reason.match(/Decider: (\S+) at ([0-9.]+), threshold ([0-9.]+); grounded: (yes|no)/);
  if (m) { r.threshold = parseFloat(m[3]); r.grounded = m[4] === "yes"; }
  const i = reason.indexOf("\narguments:\n");
  if (i >= 0) {
    const rest = reason.slice(i + 12), j = rest.lastIndexOf("\n\n");
    r.argsText = j >= 0 ? rest.slice(0, j) : rest; r.message = j >= 0 ? rest.slice(j + 2) : "";
    try { r.args = JSON.parse(r.argsText); } catch (e) { r.args = null; }
  }
  const msg = r.message || reason;
  r.rule = /Deterministic scan/.test(msg) ? "secretscan" : /is unsure/.test(msg) ? "R1"
    : /pauses this/.test(msg) ? "R3" : /does not know the risk class/.test(msg) ? "R4" : /cannot use this/.test(msg) ? "R0" : null;
  return r;
}
function targetOf(args) {
  if (!args || typeof args !== "object") return "";
  for (const k of ["path", "file_path", "file"]) if (typeof args[k] === "string") return args[k];
  return "";
}
function rulePill(card, p) {
  if (card.layer === "unavailable") return h("span", { class: "pill pill--warn", text: "Fail-closed \u00B7 no score" });
  const label = { R1: "R1 \u00B7 score under the line", R3: "R3 \u00B7 " + String(card.risk || "").replace(/_/g, " "), R4: "R4 \u00B7 unknown class", secretscan: "Secret scan", R0: "Unusable score" }[p.rule] || "Paused";
  return h("span", { class: "pill " + (p.rule === "R3" ? "pill--danger" : "pill--warn"), text: label });
}
const scored = c => c && c.layer === "decider" && typeof c.confidence === "number" && isFinite(c.confidence);
function meter(conf, thr, mini) {
  const bar = h("div", { class: "meter " + (thr !== null && conf < thr ? "meter--below" : "") + (mini ? " mini" : ""), role: "meter",
    "aria-label": "Decider confidence", "aria-valuemin": "0", "aria-valuemax": "1", "aria-valuenow": conf.toFixed(2),
    "aria-valuetext": conf.toFixed(2) + (thr !== null ? ", line " + thr.toFixed(2) : "") });
  const fill = h("div", { class: "fill" }); fill.style.width = Math.round(conf * 100) + "%"; bar.append(fill);
  if (thr !== null) { const t = h("div", { class: "tick" }); t.style.left = Math.round(thr * 100) + "%"; bar.append(t); }
  if (mini) return h("span", {}, bar, conf.toFixed(2));
  return h("div", { class: "meter-wrap" }, bar, h("p", { class: "meter-label" }, h("strong", { text: conf.toFixed(2) }), " confidence" + (thr !== null ? " \u00B7 line " + thr.toFixed(2) : "")));
}
function kv(pairs) { return h("dl", { class: "kv" }, pairs.filter(p => p[1] !== "" && p[1] !== null && p[1] !== undefined).map(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)])); }
function codeView(text, limit) {
  const lines = String(text).split("\n"), pre = h("pre", { class: "code", tabindex: "0", "aria-label": "Arguments, " + lines.length + " lines" });
  const fill = n => { pre.textContent = ""; lines.slice(0, n).forEach(l => pre.append(h("span", { text: l }))); };
  fill(limit); if (lines.length <= limit) return pre;
  const more = h("button", { class: "btn btn--small", type: "button", text: "Show all " + lines.length + " lines", onclick: () => { fill(lines.length); more.remove(); pre.focus(); } });
  return h("div", {}, pre, more);
}
function copyBtn(text) {
  const b = h("button", { class: "btn btn--small copy", type: "button", text: "Copy", onclick: async () => { try { await navigator.clipboard.writeText(text); b.textContent = "Copied"; } catch (e) { b.textContent = "Copy failed"; } } });
  return b;
}
function statusLine(el, st) {
  const [label, kind] = STATUS[st] || [st || "Unknown", "info"];
  el.className = "status status--" + kind; el.textContent = "";
  el.append(h("span", { class: "ico", "aria-hidden": "true", text: ICON[kind] }), label);
}
function stepText(s) {
  if (s.layer === "decider" && typeof s.confidence === "number") {
    return "Decider: " + (s.risk || "benign") + " at " + s.confidence.toFixed(2) + ", grounded " + (s.grounded ? "yes" : "no") + (s.latency_ms ? ", " + Math.round(s.latency_ms) + " ms" : "");
  }
  return (LAYER[s.layer] || s.layer || "Deterministic") + ", no Decider score";
}
function priorList(card) {
  const steps = Array.isArray(card.prior_steps) ? card.prior_steps : [], files = Array.isArray(card.changed_files) ? card.changed_files : [];
  if (!steps.length && !files.length) return null;
  return h("div", {}, h("h4", { text: "Run so far" }), h("ul", {}, steps.map(s => h("li", { text: (s.tool || "tool") + ": proceeded. " + stepText(s) }))),
    files.length ? h("p", { class: "muted", text: "Workspace changed: " + files.join(", ") }) : null);
}

// ---- Slide-Over Inspector Drawer & Cockpit Explorer ----
const vChip = v => h("em", { class: "chip chip--" + (v === "Proceed" ? "ok" : v === "Deny" ? "danger" : v === "Guide" ? "info" : "warn"), text: v });
function openDrawer(title, ...nodes) { $("#drawer-h").textContent = title; const b = $("#drawer-body"); b.textContent = ""; b.append(...nodes.flat(Infinity).filter(n => n !== null && n !== undefined && n !== false)); $("#drawer").hidden = false; }
function closeDrawer() { $("#drawer").hidden = true; }
$("#drawer-close").addEventListener("click", closeDrawer);
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#drawer").hidden) closeDrawer(); });

function caseBtn(id) { const c = findCase(id); return h("button", { class: "linkbtn mono", type: "button", text: id, onclick: () => c && inspectCase(c) }); }
function inspectCase(c, thr = "0.70") {
  const [rule, verdict] = c.at[thr];
  openDrawer("Case " + c.id + " · " + c.tool,
    h("p", { class: "quote", text: "User: “" + c.user + "”" }),
    h("div", { class: "filter-bar", role: "toolbar", "aria-label": "Line" }, THRESHOLDS.map(t => h("button", { class: "chip" + (t === thr ? " chip--active" : ""),
      type: "button", "aria-pressed": String(t === thr), text: "line " + t, onclick: () => inspectCase(c, t) }))),
    c.layer === "tier0" ? h("p", { class: "muted small", text: "Tier-0 safe read: the handler lets it proceed before Decider is asked; the score below was measured, not used." }) : null,
    meter(c.conf, parseFloat(thr), false),
    kv([["Verdict at " + thr, h("span", {}, h("strong", { text: rule + " " }), vChip(verdict))], ["Rule", RULE_TEXT[rule]],
      ["Decider class", c.risk.replace(/_/g, " ")], ["Labelled class", c.label_risk.replace(/_/g, " ")],
      ["Grounded", (c.grounded ? "yes" : "no") + " (labelled " + (c.label_grounded ? "yes" : "no") + ")"],
      ["Needs a person", c.needs_person ? "yes, by label" : "no, benign and grounded"],
      ["Score from", c.suite === "50" ? "EC2 GPU run, 2026-10-13" : "CPU run, 2026-10-16"],
      c.suite === "50" ? ["Hosted g4dn", c.hosted_conf.toFixed(2) + " · " + Math.round(c.hosted_ms) + " ms · " + c.hosted_verdict] : ["CPU latency", Math.round(c.cpu_ms) + " ms"],
      ["SDK classifier", sdkText(c)], ["Sources", h("code", { text: SRC[c.suite] })]]),
    h("h4", { text: "Tool arguments" }), codeView(JSON.stringify(c.args, null, 2), 16));
}
function inspectRule(id) {
  const hits = allCases().filter(c => c.at["0.70"][0] === id), in50 = hits.filter(c => c.suite === "50").length;
  openDrawer("Rule " + id, h("p", {}, h("strong", { text: RULE_TEXT[id] })),
    h("p", { class: "muted small", text: hits.length ? hits.length + " of " + allCases().length + " cases end at " + id + " at the 0.70 line: " + in50 +
      " in the 50-case set, " + (hits.length - in50) + " in the 128-case suite." : "No case in either suite reaches " + id + " at the 0.70 line." }),
    kv([["Source", h("code", { text: "src/lachesis/policy.py" })], ["Order", "First match wins, R1 to R6"]]),
    hits.length ? h("p", { class: "small" }, "Cases: ", hits.slice(0, 12).map(c => [caseBtn(c.id), " "])) : null);
}
function renderCases(filter) {
  if (filter) explorer.filter = filter;
  const list = explorer.suite === "50" ? DATA.set50 : DATA.expanded, q = explorer.q.toLowerCase(), bar = $("#case-filters");
  const chip = (on, text, onclick) => h("button", { class: "chip" + (on ? " chip--active" : ""), type: "button", "aria-pressed": String(on), text, onclick });
  bar.textContent = "";
  bar.append(chip(explorer.suite === "50", "50-case set", () => { explorer.suite = "50"; renderCases(); }),
    chip(explorer.suite === "128", "128-case suite", () => { explorer.suite = "128"; renderCases(); }), h("span", { class: "sep", "aria-hidden": "true" }),
    ...GROUPS.map(([g, label]) => chip(explorer.filter === g, label + " (" + list.filter(c => g === "all" || groupOf(c) === g).length + ")", () => renderCases(g))));
  const shown = list.filter(c => (explorer.filter === "all" || groupOf(c) === explorer.filter) &&
    (!q || (c.id + " " + c.tool + " " + c.target + " " + c.user).toLowerCase().includes(q)));
  $("#cases-count").textContent = shown.length + " of " + list.length + " cases · " + SRC[explorer.suite];
  const tbody = $("#cases-table tbody"); tbody.textContent = "";
  shown.forEach(c => { const [rule, verdict] = c.at["0.70"]; tbody.append(
    h("tr", { tabindex: "0", onclick: () => inspectCase(c), onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); inspectCase(c); } } },
      h("td", { "data-label": "Case" }, h("code", { text: c.id })),
      h("td", { "data-label": "Tool · target" }, h("strong", { text: c.tool }), " ", h("span", { class: "muted", text: c.target })),
      h("td", { "data-label": "Decider class", text: c.risk.replace(/_/g, " ") }),
      h("td", { "data-label": "Confidence" }, meter(c.conf, 0.70, true)),
      h("td", { "data-label": "Grounded", text: c.grounded ? "yes" : "no" }),
      h("td", { "data-label": "Rule & verdict" }, h("strong", { text: rule + " " }), vChip(verdict)),
      h("td", { "data-label": "SDK classifier" }, h("span", { class: /missed/.test(sdkText(c)) ? "err small" : "muted small", text: sdkText(c) })))); });
}
function renderThrSim(thr = "0.70") {
  document.querySelectorAll("#thr-tabs [data-thr]").forEach(b => { b.classList.toggle("chip--active", b.dataset.thr === thr); b.setAttribute("aria-pressed", String(b.dataset.thr === thr)); });
  const a = DATA.sim50[thr], b = DATA.sim128_test[thr], box = $("#thr-sim-body"); box.textContent = "";
  if (!a || !b) return;
  box.append(h("h3", { text: "50-case set, EC2 GPU rows, tier-0 reads included" }),
    kv([["Governed calls that proceed", a.misses.length + " of " + a.governed], ["Clean calls paused", a.false_pauses.length + " of " + a.clean]]),
    a.misses.length ? h("p", { class: "small" }, "Proceeding: ", a.misses.map(id => [caseBtn(id), findCase(id) && findCase(id).layer === "tier0" ? " (tier-0 read) " : " (Decider) "])) : null,
    h("h3", { text: "128-case suite, test split, CPU rows" }),
    kv([["Governed proceed, Decider only", b.misses_raw + " of " + b.governed], ["Governed proceed, with tier-0 reads", b.misses_tier0 + " of " + b.governed],
      ["Clean calls paused", b.false_pauses + " of " + b.clean]]),
    h("p", { class: "muted small", text: "On the dev split the pre-registered rule picks " + DATA.dev_lock.toFixed(2) + "; the default stays 0.70. Figures from " + DATA.generated_by + "." }));
}
function renderMetrics() {
  const box = $("#metrics-body"); if (!box || box.children.length || !DATA.baseline.calls) return;
  const B = DATA.baseline, G = DATA.g4dn, A = DATA.accuracy50, T = DATA.sim128_test["0.70"];
  const tile = (k, v, sub) => h("div", { class: "tile tile--static" }, h("span", { class: "tile-k", text: k }), h("strong", { class: "tile-v", text: v }), h("span", { class: "tile-sub", text: sub }));
  const bar = (label, n, of, cls) => { const fill = h("div", { class: "fill" }); fill.style.width = Math.round(100 * n / of) + "%"; fill.style.background = "var(--" + cls + ")";
    return h("div", { class: "bar-row" }, h("span", { text: label }), h("div", { class: "meter" }, fill), h("strong", { class: "num", text: n + " / " + of })); };
  box.append(h("div", { class: "tiles" },
    tile("Destructive writes that ran", B.lachesis_ran.length + " / " + B.destructive, "SDK classifier: " + B.sdk_ran.length + " / " + B.destructive + " (" + B.sdk_ran.join(", ") + ")"),
    tile("Escalations caught", B.lachesis_caught + " / " + B.gold, "SDK classifier: " + B.sdk_caught + " / " + B.gold + " on the " + B.calls + "-call baseline"),
    tile("Hosted triage latency", Math.round(G.median_ms) + " ms", "median · p95 " + Math.round(G.p95_ms) + " ms · n=" + G.n + " · parity " + G.parity + "/" + G.of),
    tile("Risk answers right", A.risk_ok + " / " + A.scored, "grounding " + A.grounded_ok + " / " + A.scored + " · " + A.mistakes + " mistakes, highest " + A.worst_mistake.toFixed(2) + ", none at 0.70 or above")),
    h("div", { class: "grid-2" },
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "Against the SDK's built-in classifier" })),
        bar("Escalations caught, Lachesis", B.lachesis_caught, B.gold, "ok"), bar("Escalations caught, SDK", B.sdk_caught, B.gold, "warn"),
        bar("Clean calls paused, Lachesis (the cost)", B.lachesis_paused_clean, B.clean, "warn"), bar("Clean calls paused, SDK", B.sdk_paused_clean, B.clean, "accent"),
        h("p", { class: "muted small", text: "evaluation/baseline_2026-10-13.jsonl; SDK median " + B.sdk_median_ms + " ms per call. The SDK let " + B.sdk_ran.join(", ") + " run." })),
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", { text: "128-case suite, test split, line 0.70" })),
        kv([["Governed calls that proceed", T.misses_raw + " of " + T.governed + " (Decider only: " + T.miss_ids.join(", ") + ")"],
          ["With tier-0 reads", T.misses_tier0 + " of " + T.governed + " (ungrounded read_file calls proceed by design)"],
          ["Clean calls paused", T.false_pauses + " of " + T.clean], ["Dev-split rule", "picks " + DATA.dev_lock.toFixed(2) + "; the default stays 0.70"]]),
        h("p", { class: "muted small", text: "evaluation/expanded_cpu_2026-10-16.jsonl · every figure on this page: " + DATA.generated_by }))));
}

// ---- S4: the decision card ----
function decisionCard(view, lane, onDecided) {
  const card = view.card || {}, p = parseReason(card.reason || ""), target = targetOf(p.args) || "";
  const tool = card.tool || "tool", exp = expiry(view), cold = card.layer === "unavailable";
  const canDecide = view.status === "PENDING" && !(lane === "try" && cold);
  const head = h("div", { class: "s4-head" }, h("h3", { text: tool + (target ? " on " + target : "") }),
    exp && view.status === "PENDING" ? h("span", { class: "countdown", "data-exp": exp }) : null);
  let alert = null;
  if (cold && lane === "try") {
    alert = h("div", { class: "alert alert--warning", role: "alert" }, h("h3", { text: "The Decider GPU is asleep" }),
      h("p", { text: "It scales to zero when idle. This call paused fail-closed: no score, and the public demo cannot decide it. " +
        "Your click has asked the GPU to wake; a run started after it is awake will be scored. This pause expires at " +
        (exp ? hhmm(exp) : "the end of its window") + " UTC and is then treated as declined; nothing runs." }));
  } else if (cold) {
    alert = h("div", { class: "alert alert--warning" }, h("p", { text: "Decider did not answer, so this call paused fail-closed with no score. You decide it." }));
  }
  const thr = p.threshold;
  const why = h("div", { class: "s4-why" }, h("h4", { text: "Why it paused" }), h("p", {}, rulePill(card, p)),
    scored(card) ? [meter(card.confidence, thr, false), thr !== null ? h("p", { class: "caption",
      text: "At or above " + thr.toFixed(2) + " Decider's class is trusted; a destructive class still asks a person." }) : null]
      : h("p", { class: "muted", text: "No Decider score for this call." }),
    kv([["Risk class", card.risk ? card.risk.replace(/_/g, " ") : "not scored"], ["Grounded", p.grounded === null ? "" : p.grounded ? "yes" : "no"],
      ["Paused by", LAYER[card.layer] || card.layer || ""], ["Decided by", view.decided_by_name && view.status !== "PENDING" ? (view.decided_by_name === "try-visitor" ? "a visitor on this page" : view.decided_by_name) : ""],
      ["Latency", card.latency_ms ? Math.round(card.latency_ms) + " ms" : ""], ["Paused at", isFinite(Date.parse(card.created_at)) ? hhmmss(Date.parse(card.created_at)) + " UTC" : ""],
      ["Ref", h("span", {}, h("code", { text: view.approval_id || "" }), " ", copyBtn(view.approval_id || ""))]]));
  const limit = window.matchMedia("(max-width: 719px)").matches ? 12 : 24;
  const entries = p.args && typeof p.args === "object" && !Array.isArray(p.args) ? Object.entries(p.args) : [];
  const multi = entries.filter(([, v]) => typeof v === "string" && v.includes("\n"));
  const argPairs = entries.filter(e => !multi.includes(e)).map(([k, v]) => { const s = typeof v === "string" ? v : JSON.stringify(v); return [k, s.length > 120 ? s.slice(0, 120) + "\u2026" : s]; });
  const what = h("div", { class: "s4-what" }, h("h4", { text: "What will run" }),
    kv([["Tool", tool], ["Target", target], ["Effect", card.risk ? (EFFECT[card.risk] || card.risk) + " (derived from class)" : ""]]),
    argPairs.length ? [h("h4", { text: "Arguments" }), kv(argPairs)] : null,
    multi.map(([k, v]) => [h("h4", { text: k }), codeView(v, limit)]),
    multi.length ? null : p.argsText ? codeView(p.argsText, limit) : (card.reason ? h("pre", { class: "code code--plain", text: card.reason }) : null),
    lane === "approval" ? priorList(card) : null);
  const box = h("section", { class: "card s4", "aria-label": "Paused call: " + tool }, head, alert, h("div", { class: "s4-grid" }, why, what));
  if (canDecide) box.append(actionBar(view, lane, tool, target, card.risk, exp, onDecided));
  return box;
}
function actionBar(view, lane, tool, target, risk, exp, onDecided) {
  const id = view.approval_id;
  const path = lane === "try" ? "/try/" + encodeURIComponent(id) + "/decision" : "/approvals/" + encodeURIComponent(id) + "/decision";
  const msg = h("p", { class: "act-msg", role: "status", "aria-live": "polite" }), bar = h("div", { class: "actionbar" });
  async function send(decision) {
    bar.querySelectorAll(".btn").forEach(b => { b.disabled = true; });
    msg.className = "act-msg"; msg.textContent = "Recording decision...";
    const out = await api(path, { method: "POST", body: JSON.stringify({ decision }) });
    if (out.status === 202) { msg.textContent = "Recorded. Agent resuming..."; onDecided(); return; }
    msg.className = "act-msg err";
    const e = String((out.body && out.body.error) || "");
    if (out.status === 409 && /expired/.test(e)) msg.textContent = "This call expired at " + (exp ? hhmm(exp) : "its deadline") + " UTC and was treated as declined.";
    else if (out.status === 409 && /cold/.test(e)) msg.textContent = "Could not complete: the Decider GPU was asleep, so the public demo cannot decide this call.";
    else if (out.status === 409) msg.textContent = "Someone already decided this call.";
    else { msg.textContent = errText(out); bar.querySelectorAll(".btn").forEach(b => { b.disabled = false; }); }
    if (out.status === 409) onDecided();
  }
  const decline = h("button", { class: "btn btn--danger", type: "button", text: "Decline", onclick: () => send("decline") });
  const approve = h("button", { class: "btn btn--primary", type: "button", text: "Approve and run", onclick: () => {
    if (!CONFIRM_CLASSES.includes(risk)) { send("approve"); return; }
    const yes = h("button", { class: "btn btn--solid-danger", type: "button", text: "Yes, approve and run", onclick: () => { box.remove(); send("approve"); } });
    const box = h("div", { class: "confirm alert alert--warning" }, h("p", { text: "Approve " + tool + (target ? " on " + target : "") + "? The agent runs it immediately." }),
      h("div", { class: "row" }, h("button", { class: "btn btn--ghost", type: "button", text: "Cancel", onclick: () => { box.remove(); approve.focus(); } }), yes));
    if (!bar.querySelector(".confirm")) { bar.prepend(box); yes.focus(); }
  } });
  bar.append(decline, approve, msg);
  return bar;
}

function poll(path, onView, on404) {
  const g = gen; let fails = 0, handle = null, last = "";
  async function tick() {
    if (g !== gen) return;
    const out = await api(path); if (g !== gen) return;
    let wait = 4000;
    if (out.status === 200) {
      fails = 0; netFail(0);
      const v = out.body, key = v.status + "|" + ((v.card && v.card.created_at) || "") + "|" + (v.result_text || "").length;
      if (key !== last) { last = key; onView(v); }
      if (TERMINAL.includes(v.status)) return;
      if (v.status === "PENDING") wait = 15000;
    } else if (out.status === 404) { on404(); return; }
    else if (out.status === 401 || out.status === 403) { on404(out); return; }
    else { fails += 1; netFail(fails); wait = 15000; }
    handle = later(tick, wait);
  }
  tick();
  return { kick() { if (handle) { clearTimeout(handle); timers.delete(handle); } handle = later(tick, 2000); } };
}
const AFTER = { APPROVED: "Approved. The agent is resuming.", DECLINED: "Declined. The agent is resuming.", RESUMING: "Decided. The agent is resuming.", DONE: "Approved, then it ran.", REFUSED_DONE: "Declined.", EXPIRED: "Nobody decided before the deadline.", ERROR: "The run failed." };
function outcomeRow(v) {
  if (v.status === "DONE") return h("li", { class: "done" }, h("div", { class: "step-h", text: "Finished" }), v.result_text ? codeView(v.result_text, 40) : null);
  if (v.status === "REFUSED_DONE" || v.status === "EXPIRED") return h("li", { class: "stopped" }, h("div", { class: "step-h", text: "Agent stood down" }), h("p", { class: "step-d", text: "The paused call did not run." }), v.result_text ? codeView(v.result_text, 40) : null);
  if (v.status === "ERROR") return h("li", { class: "stopped" }, h("div", { class: "step-h", text: "Run failed" }), h("p", { class: "step-d", text: String(v.result_text || "").split(":")[0] || "The runner reported an error." }));
  return null;
}
function renderTry(v, ctl) {
  statusLine($("#run-status"), v.status);
  const card = v.card || {}, list = $("#steps"), p = parseReason(card.reason || ""), had = list.contains(document.activeElement);
  list.textContent = "";
  list.append(h("li", { class: "done" }, h("div", { class: "step-h" }, "Agent started", v.created_ts ? h("span", { class: "muted", "data-since": v.created_ts * 1000 }) : null)));
  (Array.isArray(card.prior_steps) ? card.prior_steps : []).forEach(s => list.append(h("li", { class: "done" },
    h("div", { class: "step-h" }, (s.tool || "tool") + (CANNED[s.tool] ? " \u00B7 " + CANNED[s.tool] : ""), h("span", { class: "pill pill--info", text: "Proceeded" })), h("p", { class: "step-d", text: stepText(s) }))));
  if (Array.isArray(card.changed_files) && card.changed_files.length) {
    const changed = h("p", { class: "step-d", text: "Workspace changed: " + card.changed_files.join(", ") });
    if (list.lastChild && list.children.length > 1) list.lastChild.append(changed); else list.append(h("li", { class: "done" }, changed));
  }
  if (card.tool) {
    const target = targetOf(p.args), item = h("li", { class: v.status === "PENDING" ? "paused" : "stopped" });
    if (v.status !== "PENDING") item.append(h("div", { class: "step-h" }, card.tool + (target ? " \u00B7 " + target : ""), h("span", { class: "pill pill--warn", text: "Paused" })));
    if (v.status === "PENDING") item.append(decisionCard(v, "try", () => ctl.kick()));
    else item.append(h("p", { class: "step-d", text: (AFTER[v.status] || v.status) + (v.decided_by_name === "try-visitor" ? " Decided on this page." : "") }));
    list.append(item);
  } else if (!TERMINAL.includes(v.status)) {
    list.append(h("li", {}, h("div", { class: "skeleton" }), h("p", { class: "step-d", text: "Waiting for the agent's first pause or its finish." })));
  }
  const end = outcomeRow(v); if (end) list.append(end);
  tickClocks(); if (had) { $("#run-status").setAttribute("tabindex", "-1"); $("#run-status").focus(); }
}

function viewTry(token) {
  const run = $("#run"), msg = $("#try-msg");
  msg.hidden = true; $("#try-start").disabled = false; $("#try-intro").hidden = !!token; $("#try-start").textContent = token ? "Start another run" : "Start the demo run";
  if (!token) { run.hidden = true; return; }
  run.hidden = false; $("#run-ref").textContent = token; $("#steps").textContent = ""; $("#run-status").textContent = "";
  $("[data-copy]").onclick = async e => { try { await navigator.clipboard.writeText(location.href); e.target.textContent = "Copied"; } catch (err) { e.target.textContent = "Copy failed"; } };
  const g = gen;
  later(() => { if (g === gen && !$("#steps").children.length) $("#steps").append(h("li", {}, h("div", { class: "skeleton" }), h("div", { class: "skeleton" }))); }, 200);
  const ctl = poll("/try/" + encodeURIComponent(token), v => renderTry(v, ctl), denied => {
    const mine = store(s => JSON.parse(s.getItem("lachesis_try_started") || "null")) || {}; // the 202 races the runner's first row write
    if (!denied && mine.token === token && Date.now() - mine.t < 20000) { if (!$("#run-status").textContent) renderTry({ status: "STARTING", card: {} }, ctl); ctl.kick(); return; }
    $("#steps").textContent = ""; $("#run-status").textContent = ""; msg.hidden = false; msg.textContent = "No demo run matches this link. Runs are removed about a day after they end.";
  });
}
$("#try-start").addEventListener("click", async () => {
  const btn = $("#try-start"), msg = $("#try-msg"); btn.disabled = true; msg.hidden = true;
  const out = await api("/try", { method: "POST", body: "{}" });
  if (out.status === 202 && out.body.token) { store(s => s.setItem("lachesis_try_started", JSON.stringify({ token: out.body.token, t: Date.now() }))); location.hash = "#/try/" + encodeURIComponent(out.body.token); return; }
  msg.hidden = false; msg.textContent = errText(out); btn.disabled = false;
});

function viewQueue() {
  const on = !!auth.token;
  $("#queue-out").hidden = on; $("#queue-in").hidden = !on; if ($("#queue-actions")) $("#queue-actions").hidden = !on;
  if (!on) return;
  const g = gen; let fails = 0;
  async function load() {
    if (g !== gen || !auth.token || document.visibilityState !== "visible") return;
    const out = await api("/approvals"); if (g !== gen) return;
    if (out.status === 0 || out.status >= 500) netFail(++fails); else { fails = 0; netFail(0); }
    if (out.status !== 200) {
      $("#queue-msg").textContent = out.status === 403 ? "This account is not in the approvers group." : out.status === 401 ? "" : errText(out);
      if (out.status === 401) viewQueue();
      return;
    }
    $("#queue-msg").textContent = ""; $("#queue-checked").textContent = "Last checked " + hhmmss(Date.now()) + " UTC";
    const rows = (out.body.approvals || []).slice().sort((a, b) => (a.created_ts || 0) - (b.created_ts || 0));
    const badge = $("#queue-badge"); if (badge) { badge.hidden = rows.length === 0; badge.textContent = String(rows.length); }
    $("#queue-empty").hidden = rows.length > 0; $("#queue-table").hidden = rows.length === 0;
    const body = $("#queue-table tbody"); body.textContent = "";
    rows.forEach(v => {
      const c = v.card || {}, p = parseReason(c.reason || ""), t = targetOf(p.args), exp = expiry(v), made = Date.parse(c.created_at) || (v.created_ts || 0) * 1000;
      body.append(h("tr", {}, h("td", { "data-label": "Waiting", text: made ? age(made) : "" }),
        h("td", { "data-label": "Tool \u00B7 target" }, h("a", { href: "#/a/" + encodeURIComponent(v.approval_id), text: (c.tool || "tool") + (t ? " \u00B7 " + t : "") })),
        h("td", { "data-label": "Why paused" }, rulePill(c, p)),
        h("td", { "data-label": "Score" }, scored(c) ? meter(c.confidence, p.threshold, true) : h("span", { class: "muted", text: "no score" })),
        h("td", { "data-label": "Layer", text: LAYER[c.layer] || c.layer || "" }),
        h("td", { "data-label": "Expires" }, exp ? h("span", { class: "countdown", "data-exp": exp }) : "")));
    });
    tickClocks();
  }
  refreshQueue = load; load(); every(load, 10000);
}
let refreshQueue = null;
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && current() === "queue" && refreshQueue) refreshQueue(); });
$("#queue-start").addEventListener("click", async () => {
  const btn = $("#queue-start"); btn.disabled = true;
  const out = await api("/approvals/start", { method: "POST", body: "{}" });
  btn.disabled = false; $("#queue-msg").textContent = out.status === 202 ? "Started run " + out.body.approval_id + ". Paused calls appear in the queue." : errText(out);
});

function viewDetail(id) {
  const body = $("#detail-body"), st = $("#detail-status"); body.textContent = ""; st.textContent = "";
  if (!auth.token) {
    body.append(h("p", { text: "Approvers only. Sign in to decide this call." }), h("button", { class: "btn btn--primary", type: "button", text: "Approver sign in", onclick: openSignin })); return;
  }
  const g = gen;
  later(() => { if (g === gen && !body.children.length) body.append(h("div", { class: "skeleton" }), h("div", { class: "skeleton" })); }, 200);
  const ctl = poll("/approvals/" + encodeURIComponent(id), v => {
    statusLine(st, v.status); body.textContent = ""; const card = v.card || {};
    if (card.tool || card.reason) body.append(decisionCard(v, "approval", () => ctl.kick()));
    const end = outcomeRow(v); if (end) body.append(h("ol", { class: "steps" }, end)); tickClocks();
  }, out => {
    body.textContent = ""; body.append(h("p", { class: "err", role: "alert", text: out && out.status === 403 ? "This account is not in the approvers group." : out ? "" : "No paused call matches this link." }));
  });
}

let challenge = null;
function openSignin() { $("#si-err").textContent = ""; $("#signin").showModal(); $("#si-user").focus(); }
async function cognito(target, payload) {
  try {
    const res = await fetch("https://cognito-idp." + cfg.region + ".amazonaws.com/", { method: "POST",
      headers: { "content-type": "application/x-amz-json-1.1", "x-amz-target": "AWSCognitoIdentityProviderService." + target }, body: JSON.stringify(payload) });
    return { status: res.status, body: await res.json().catch(() => ({})) };
  } catch (e) { return { status: 0, body: {} }; }
}
$("#signin-form").addEventListener("submit", async e => {
  e.preventDefault();
  const err = $("#si-err"), user = $("#si-user").value.trim(), pass = $("#si-pass").value, fresh = $("#si-new").value;
  if (!user || !pass) { err.textContent = "Enter your username and password."; return; }
  $("#si-submit").disabled = true; err.textContent = "";
  const out = challenge && fresh
    ? await cognito("RespondToAuthChallenge", { ChallengeName: "NEW_PASSWORD_REQUIRED", ClientId: cfg.clientId, Session: challenge, ChallengeResponses: { USERNAME: user, NEW_PASSWORD: fresh } })
    : await cognito("InitiateAuth", { AuthFlow: "USER_PASSWORD_AUTH", ClientId: cfg.clientId, AuthParameters: { USERNAME: user, PASSWORD: pass } });
  $("#si-submit").disabled = false; const b = out.body || {};
  if (b.ChallengeName === "NEW_PASSWORD_REQUIRED") {
    challenge = b.Session; $("#si-new-wrap").hidden = false; $("#si-new").focus(); err.textContent = "First sign in: choose a new password, then select Sign in again."; return;
  }
  const token = b.AuthenticationResult && b.AuthenticationResult.IdToken;
  if (token && adopt(token)) {
    store(s => s.setItem("lachesis_id_token", token)); challenge = null; $("#si-new-wrap").hidden = true; $("#signin-form").reset(); $("#signin").close();
    $("#session-alert").hidden = true; renderAuth(); if (current() === "home") location.hash = "#/queue"; else route(); return;
  }
  const type = String(b.__type || "");
  if (out.status === 0) err.textContent = "Could not complete: the network request failed.";
  else if (/NotAuthorized|UserNotFound/.test(type)) err.textContent = "Username or password is wrong.";
  else err.textContent = "Could not complete: " + (b.message || "HTTP " + out.status) + ".";
  if (challenge && !/InvalidPassword/.test(type)) { challenge = null; $("#si-new-wrap").hidden = true; }
});
$("#si-cancel").addEventListener("click", () => $("#signin").close());
$("#open-signin").addEventListener("click", openSignin);
document.querySelectorAll("[data-signin]").forEach(b => b.addEventListener("click", openSignin));
$("#signout").addEventListener("click", () => { signOut(); route(); });
$(".skip").addEventListener("click", e => { e.preventDefault(); $("#main").focus(); });

const caseSearch = $("#case-search"); if (caseSearch) caseSearch.addEventListener("input", () => { explorer.q = caseSearch.value; renderCases(); });
document.querySelectorAll("[data-drill-filter]").forEach(b => b.addEventListener("click", () => { explorer.suite = "50"; renderCases(b.dataset.drillFilter); $("#cases-table").scrollIntoView({ block: "start" }); }));
document.querySelectorAll("[data-goto]").forEach(b => b.addEventListener("click", () => { location.hash = b.dataset.goto; }));
document.querySelectorAll("#policy-rules [data-rule]").forEach(li => li.addEventListener("click", () => inspectRule(li.dataset.rule)));
document.querySelectorAll("#thr-tabs [data-thr]").forEach(b => b.addEventListener("click", () => renderThrSim(b.dataset.thr)));

function parts() { return (location.hash.replace(/^#\/?/, "") || "").split("/").filter(Boolean).map(decodeURIComponent); }
function current() {
  const p = parts();
  return p[0] === "try" ? "try" : p[0] === "queue" ? "queue" : p[0] === "policy" ? "policy" : p[0] === "metrics" ? "metrics" : p[0] === "a" && p[1] ? "detail" : "home";
}
// ---- motion layer: parallax, card spotlight, staggered reveal. Numbers never animate: every frame shows the measured value. Inert under prefers-reduced-motion. ----
const MOTION = window.matchMedia("(prefers-reduced-motion: no-preference)"), FINE = window.matchMedia("(pointer: fine) and (min-width: 900px)");
const fxState = { frame: 0, x: 0, y: 0 };
function fxApply() { fxState.frame = 0; const r = document.documentElement.style; r.setProperty("--px", fxState.x.toFixed(3)); r.setProperty("--py", fxState.y.toFixed(3)); r.setProperty("--sy", String(Math.round(window.scrollY))); } const fxQueue = () => { if (!fxState.frame) fxState.frame = requestAnimationFrame(fxApply); };
window.addEventListener("pointermove", e => {
  const card = e.target.closest && e.target.closest(".tile, .panel");
  if (card) { const b = card.getBoundingClientRect(); card.style.setProperty("--mx", Math.round(e.clientX - b.left) + "px"); card.style.setProperty("--my", Math.round(e.clientY - b.top) + "px"); }
  if (MOTION.matches && FINE.matches) { fxState.x = e.clientX / innerWidth - 0.5; fxState.y = e.clientY / innerHeight - 0.5; fxQueue(); }
}, { passive: true }); window.addEventListener("scroll", () => { if (MOTION.matches && FINE.matches) fxQueue(); }, { passive: true });
function fx(view) {
  const root = $("[data-view=" + view + "]"); if (!root || !MOTION.matches) return;
  root.querySelectorAll(".tile, .panel, .flow > li").forEach((el, i) => { el.classList.remove("fx-in"); void el.offsetWidth; el.style.setProperty("--d", Math.min(i, 8) * 60 + "ms"); el.classList.add("fx-in"); });
}

let first = true;
function route() {
  clearTimers(); gen += 1; netFail(0);
  const view = current(), p = parts();
  document.querySelectorAll("[data-view]").forEach(el => { el.hidden = el.dataset.view !== view; });
  document.querySelectorAll(".nav-item[data-nav]").forEach(a => {
    const active = a.dataset.nav === (view === "detail" ? "queue" : view);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  if (view === "try") viewTry(p[1]);
  else if (view === "queue") viewQueue();
  else if (view === "detail") viewDetail(p[1]);
  else if (view === "policy") renderThrSim("0.70");
  else if (view === "metrics") renderMetrics();
  if (!first) { const hd = $("[data-view=" + view + "] h1"); if (hd) hd.focus(); }
  first = false; fx(view);
}
window.addEventListener("hashchange", route);

if (VIDEO_URL) document.querySelectorAll(".video-link").forEach(a => { a.href = VIDEO_URL; a.hidden = false; });
if (cfg.build) $("#build").textContent = " \u00B7 console build " + cfg.build;
const saved = store(s => s.getItem("lachesis_id_token"));
if (saved && !adopt(saved)) store(s => s.removeItem("lachesis_id_token"));
renderCases("all"); renderThrSim("0.70"); renderMetrics(); renderAuth(); route();
