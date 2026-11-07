"use strict";
/* FortiGate Upgrader UI (vanilla JS, no build step). */
const TOKEN = (() => {
  let t = location.hash.slice(1);
  try { if (t) sessionStorage.setItem("fgu-token", t); else t = sessionStorage.getItem("fgu-token") || ""; } catch { /* storage blocked */ }
  return t;
})();
const S = { meta: null, targets: [], inv: {}, images: [], page: "dashboard", dev: null, devTab: "overview", pollers: {}, upSel: new Set() };
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const headers = { "X-Auth": TOKEN, ...(opts.headers || {}) };
  let body = opts.body;
  if (body && !(body instanceof FormData)) { headers["Content-Type"] = "application/json"; body = JSON.stringify(body); }
  const r = await fetch("/api" + path, { method: opts.method || (body ? "POST" : "GET"), headers, body });
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}
const toast = (m) => alert(m);

// ------------------------------------------------------------------ icons
const ICON = {
  changelog: '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4M9 11h7M9 15h7M9 19h4"/>',
  pulse: '<path d="M2 12h4l3-8 4 16 3-8h6"/>',
  dashboard: '<path d="M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z"/>',
  devices: '<rect x="2" y="4" width="20" height="6" rx="1"/><rect x="2" y="14" width="20" height="6" rx="1"/><path d="M6 7h.01M6 17h.01"/>',
  upgrade: '<circle cx="12" cy="12" r="9"/><path d="M12 16V8M8.5 11.5 12 8l3.5 3.5"/>',
  snapshots: '<path d="M4 4h16v16H4z"/><path d="M8 9h8M8 13h8M8 17h5"/>',
  firmware: '<rect x="6" y="6" width="12" height="12" rx="1"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  notes: '<path d="M6 2h9l5 5v15H6z"/><path d="M14 2v6h6M9 13h7M9 17h7"/>',
  jobs: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M15 8l2 2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>', trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  maximize: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>', minimize: '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>', upload: '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>', folder: '<path d="M3 6h7l2 2h9v11H3z"/>',
  import: '<path d="M12 4v12M7 11l5 5 5-5M4 20h16"/>',
  cloud: '<path d="M7 18h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.5 1.5A3.3 3.3 0 0 0 7 18z"/><path d="M12 11v6M9.5 14.5 12 17l2.5-2.5"/>', play: '<path d="M7 4l12 8-12 8z"/>', check: '<path d="M5 12l5 5L20 7"/>',
};
const icon = (n) => `<svg viewBox="0 0 24 24">${ICON[n] || ""}</svg>`;
const NAV = ["Monitor", ["dashboard", "Dashboard"], ["devices", "Devices"], ["jobs", "Jobs"], "Operations", ["upgrade", "Upgrade"], ["snapshots", "Snapshots & Diff"],
  "Library", ["firmware", "Firmware Store"], ["notes", "Release Notes"], "System", ["settings", "Settings"], ["changelog", "Change log"]];

// ------------------------------------------------------- value rendering
const GOOD = new Set(["Up", "Alive", "Established", "Full", "Yes", "Licensed", "Registered", "Valid", "Enabled", "Primary", "Connected"]);
const BAD = new Set(["Down", "Dead", "Idle", "Active", "Connect", "Opensent", "Openconfirm", "No", "Expired", "Unlicensed", "Shut down", "Invalid"]);
function val(v) {
  if (v === null || v === undefined || v === "" || v === "—") return '<span class="muted">—</span>';
  if (typeof v === "number") return v.toLocaleString();
  if (typeof v === "boolean") return v ? badge("Yes", "up") : badge("No", "down");
  const s = String(v);
  if (s === "Up") return '<span class="st up"><i>▲</i>Up</span>';
  if (s === "Down") return '<span class="st down"><i>▼</i>Down</span>';
  if (GOOD.has(s)) return badge(s, "up");
  if (BAD.has(s)) return badge(s, "down");
  if (s === "Not licensed" || s === "Not available" || s === "Disabled") return badge(s, "grey");
  if (s === "Free tier") return badge(s, "info");
  if (s === "Secondary") return badge(s, "grey");
  return esc(s);
}
const badge = (t, k) => `<span class="badge b-${k}">${esc(t)}</span>`;
const sevBadge = (s) => s === "critical" ? badge("Critical", "down") : s === "warning" ? badge("Warning", "warn") : s === "info" ? badge("Info", "info") : badge("No change", "up");
function table(rows, opts = {}) {
  if (typeof rows === "string") return `<pre>${esc(rows || "(no output)")}</pre>`;
  if (!rows || !rows.length) return `<div class="empty">${opts.empty || "No entries"}</div>`;
  const cols = opts.cols || Object.keys(rows[0]);
  const lim = opts.limit || 3000;
  return `<table><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${rows.slice(0, lim).map((r) =>
    `<tr>${cols.map((c) => `<td class="${typeof r[c] === "number" ? "num" : ""}">${opts.fmt && opts.fmt[c] ? opts.fmt[c](r[c], r) : val(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table>
    ${rows.length > lim ? `<div class="hint" style="padding:6px 10px">${rows.length - lim} more rows not shown (see the .log file)</div>` : ""}`;
}
const fmtSize = (b) => b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : (b / 1e3).toFixed(0) + " KB";
const ver = (s) => (String(s || "").replace(/^v/, "").match(/(\d+)\.(\d+)\.(\d+)/) || []).slice(1).map(Number);
const vcmp = (a, b) => { const x = ver(a), y = ver(b); for (let i = 0; i < 3; i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) - (y[i] || 0); return 0; };
const hwKey = (s) => (s || "").toUpperCase().replace("FORTIGATE-", "FGT").replace("FORTIWIFI-", "FWF").replace(/[^A-Z0-9]/g, "").replace("FGVM", "FGTVM");
const typeName = (t) => ({ standalone: "Standalone", fgcp: "HA cluster (FGCP)", fgsp: "HA peers (FGSP)" }[t] || t);
// what the device actually is: a unit saved as standalone that reports HA is treated as a cluster
function effType(t) {
  const i = S.inv[t.id] || {};
  if (t.type === "standalone" && ["a-p", "a-a"].includes(i.ha_mode)) return "fgcp";
  if (t.type === "standalone" && i.fgsp) return "fgsp";
  return t.type;
}
const typeMismatch = (t) => effType(t) !== t.type;
const scopeName = (s) => s === "global" ? "Global" : "VDOM " + s;

// ------------------------------------------------------------------ shell
async function boot() {
  S.meta = await api("/meta");
  const nv = $("#navVer");
  if (nv && S.meta) { nv.innerHTML = `<span class="nv-name">FortiGate Upgrader</span><span class="nv-ver">v${esc(S.meta.version)} <span class="nv-build">build${String(S.meta.build).padStart(4, "0")}</span></span>`; nv.onclick = (e) => { e.preventDefault(); go("changelog"); }; }
  $("#nav").innerHTML = NAV.map((n) => typeof n === "string" ? `<div class="grp">${esc(n)}</div>` : `<a href="#" data-page="${n[0]}">${icon(n[0])}<span>${n[1]}</span></a>`).join("");
  $$("#nav a").forEach((a) => a.onclick = (e) => { e.preventDefault(); go(a.dataset.page); });
  $("#jobBadge").onclick = () => go("jobs");
  await loadTargets();
  go("dashboard");
  setInterval(jobBadge, 4000);
  updStart();
}
window.addEventListener("hashchange", () => location.reload());
async function loadTargets() { S.targets = await api("/targets"); }
async function loadInv(fresh = false) { S.inv = await api("/inventory" + (fresh ? "?fresh=true" : "")); return S.inv; }
async function jobBadge() {
  try {
    const js = await api("/jobs");
    const act = js.filter((j) => j.status === "running" || j.status === "waiting").length;
    $("#jobBadge").hidden = !act; $("#jobBadge").textContent = `${act} job${act > 1 ? "s" : ""} running`;
  } catch { /* ignore */ }
}
function go(page, arg) {
  S.page = page;
  document.body.classList.remove("has-maxed"); document.onkeydown = null;
  Object.values(S.pollers).forEach(clearInterval); S.pollers = {};
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page || (page === "device" && a.dataset.page === "devices")));
  ({ dashboard: pDashboard, devices: pDevices, device: pDevice, upgrade: pUpgrade, snapshots: pSnapshots, firmware: pFirmware, notes: pNotes, jobs: pJobs, settings: pSettings, changelog: pChangelog }[page])(arg);
}
function page(title, crumb, actions = "", tabs = "", tags = "") {
  $("#main").innerHTML = `<div class="page-head"><div class="ttl"><div><div class="crumb">${esc(crumb)}</div><h1>${esc(title)}${tags}</h1></div><div class="row">${actions}</div></div>${tabs}</div><div class="content" id="pc"></div>`;
  return $("#pc");
}
function modal(title, body, foot) {
  $("#modalBody").innerHTML = `<div class="mh">${esc(title)}</div><div class="mb">${body}</div><div class="mf">${foot || '<button value="cancel">Close</button>'}</div>`;
  $("#modal").showModal();
  return $("#modalBody");
}

// -------------------------------------------------------------- dashboard
async function pDashboard() {
  const c = page("Dashboard", "Status");
  c.innerHTML = `<div class="widgets" id="w"><div class="panel"><div class="ph">Devices</div><div class="pb"><span class="spin"></span> Polling devices…</div></div></div>`;
  const [inv, imgs, js] = await Promise.all([loadInv(), api("/images"), api("/jobs")]);
  S.images = imgs;
  const ts = S.targets, infos = ts.map((t) => inv[t.id] || {});
  const up = infos.filter((i) => i.ok).length;
  const byVer = {}; infos.filter((i) => i.ok).forEach((i) => byVer[i.version] = (byVer[i.version] || 0) + 1);
  const vers = Object.entries(byVer).sort((a, b) => vcmp(b[0], a[0]));
  const max = Math.max(1, ...vers.map((v) => v[1]));
  const fmgs = ts.filter((t) => inv[t.id]?.management?.managed);
  const active = js.filter((j) => ["running", "waiting"].includes(j.status));
  $("#w").innerHTML = `
    <div class="panel"><div class="ph">Devices</div><div class="pb"><div class="big">${ts.length}</div>
      <div class="row" style="margin-top:6px"><span><span class="dot up"></span> ${up} reachable</span><span><span class="dot down"></span> ${ts.length - up} unreachable</span></div>
      <div class="hint" style="margin-top:6px">${["standalone", "fgcp", "fgsp"].map((k) => `${ts.filter((t) => effType(t) === k).length} ${typeName(k)}`).join(" · ")}</div></div></div>
    <div class="panel"><div class="ph">Firmware versions</div><div class="pb">${vers.length ? vers.map(([v, n]) => `<div class="row" style="margin:4px 0"><span style="width:60px"><b>v${esc(v)}</b></span><div class="bar" style="flex:1"><i style="width:${(n / max) * 100}%"></i></div><span>${n}</span></div>`).join("") : '<span class="hint">No reachable devices</span>'}</div></div>
    <div class="panel"><div class="ph">Central management</div><div class="pb"><div class="big">${fmgs.length}</div><div class="hint">managed by FortiManager</div>
      ${fmgs.map((t) => `<div style="margin-top:4px">${badge("FMG", "warn")} ${esc(t.name)} <span class="hint">${esc(inv[t.id].management.fmg)}</span></div>`).join("")}</div></div>
    <div class="panel"><div class="ph">Firmware store</div><div class="pb"><div class="big">${imgs.length}</div><div class="hint">images · ${fmtSize(imgs.reduce((a, i) => a + i.size, 0))}</div>
      <div style="margin-top:6px">${[...new Set(imgs.map((i) => i.version))].slice(0, 8).map((v) => `<span class="chip ver">v${esc(v)}</span>`).join("")}</div></div></div>
    <div class="panel"><div class="ph">Data folder <button class="small" id="dfOpen">${icon("folder")}Open</button></div><div class="pb"><div class="mono path">${esc(S.meta.data_dir)}</div>
      <div class="hint" style="margin-top:6px">Devices, secrets vault, firmware images, collections, configurations, jobs and reports</div></div></div>
    <div class="panel"><div class="ph">Active jobs <button class="small" onclick="go('jobs')">All jobs</button></div><div class="pb flush">${active.length ? table(active.map((j) => ({ Job: j.title, Stage: j.stage, Status: j.status, Started: j.created }))) : '<div class="empty">No jobs running</div>'}</div></div>`;
  $("#dfOpen").onclick = () => api("/open-folder", { body: { kind: "data" } }).catch((err) => toast(err.message));
}

// ---------------------------------------------------------------- devices
async function pDevices() {
  const c = page("Devices", "Device inventory");
  c.innerHTML = `<div class="panel"><div class="toolbar">
      <button class="primary" id="new">${icon("plus")}Create New</button><button id="edit" disabled>${icon("edit")}Edit</button><button id="del" disabled>${icon("trash")}Delete</button>
      <button id="imp">${icon("import")}Import CSV</button><button id="upg" disabled>${icon("upgrade")}Upgrade selected</button>
      <span class="grow"></span><input id="q" placeholder="Search"><button id="ref">${icon("refresh")}Refresh</button></div>
    <div id="tbl"><div class="empty"><span class="spin"></span> Polling devices…</div></div></div>`;
  const sel = new Set();
  const draw = () => {
    const q = $("#q").value.toLowerCase();
    const rows = S.targets.filter((t) => !q || JSON.stringify([t, S.inv[t.id]]).toLowerCase().includes(q));
    $("#tbl").innerHTML = rows.length ? `<table><thead><tr><th><input type="checkbox" id="all"></th><th>Status</th><th>Name</th><th>Type</th><th>Address</th><th>Model</th><th>Firmware</th><th>HA</th><th>VDOMs</th><th>Management</th></tr></thead><tbody>
      ${rows.map((t) => { const i = S.inv[t.id] || {}; const m = t.members[0];
        return `<tr class="click ${sel.has(t.id) ? "sel" : ""}" data-id="${t.id}"><td><input type="checkbox" data-cb="${t.id}" ${sel.has(t.id) ? "checked" : ""}></td>
        <td>${i.ok ? '<span class="dot up" title="Reachable"></span>' : i.error ? `<span class="dot down" title="${esc(i.error)}"></span>` : '<span class="dot grey"></span>'}</td>
        <td><a href="#" data-open="${t.id}"><b>${esc(t.name)}</b></a>${i.hostname && i.hostname !== t.name ? `<div class="hint">${esc(i.hostname)}</div>` : ""}</td><td>${typeName(effType(t))}${t.members.length > 1 ? ` <span class="hint">(${t.members.length})</span>` : ""}${typeMismatch(t) ? ` <span class="hint" title="Saved as Standalone; the device reports HA">· detected</span>` : ""}</td>
        <td>${esc(m.host)}${m.port && m.port !== 443 ? ":" + m.port : ""}</td><td>${esc(i.model || "")}</td><td>${i.version ? `<span class="chip ver">v${esc(i.version)}</span> <span class="hint">b${esc(i.build)}</span>` : ""}</td>
        <td>${esc(i.ha_mode && i.ha_mode !== "standalone" ? i.ha_mode.toUpperCase() : i.ok ? "Standalone" : "")}${i.fgsp ? " + FGSP" : ""}</td><td>${i.ok ? (i.vdom_mode ? i.vdoms.length : "Disabled") : ""}</td>
        <td>${i.management?.managed ? badge("FortiManager " + i.management.fmg, "warn") : i.management?.cloud ? badge("FortiGate Cloud", "info") : i.ok ? '<span class="hint">Local</span>' : ""}</td></tr>`; }).join("")}</tbody></table>`
      : `<div class="empty">No devices yet. Click <b>Create New</b> or <b>Import CSV</b>.</div>`;
    $$("[data-cb]").forEach((cb) => cb.onclick = (e) => { e.stopPropagation(); cb.checked ? sel.add(cb.dataset.cb) : sel.delete(cb.dataset.cb); upd(); draw(); });
    $$("[data-open]").forEach((a) => a.onclick = (e) => { e.preventDefault(); e.stopPropagation(); go("device", a.dataset.open); });
    $$("tr[data-id]").forEach((tr) => { tr.onclick = () => { sel.clear(); sel.add(tr.dataset.id); upd(); draw(); }; tr.ondblclick = () => go("device", tr.dataset.id); });
    const all = $("#all"); if (all) all.onclick = () => { all.checked ? rows.forEach((t) => sel.add(t.id)) : sel.clear(); upd(); draw(); };
  };
  const upd = () => { $("#edit").disabled = sel.size !== 1; $("#del").disabled = !sel.size; $("#upg").disabled = !sel.size; };
  $("#q").oninput = draw;
  $("#new").onclick = () => editTarget();
  $("#edit").onclick = () => editTarget(JSON.parse(JSON.stringify(S.targets.find((t) => sel.has(t.id)))));
  $("#del").onclick = async () => { if (!confirm(`Delete ${sel.size} device(s)? Snapshots are kept.`)) return; for (const id of sel) await api(`/targets/${id}`, { method: "DELETE" }); await loadTargets(); pDevices(); };
  $("#upg").onclick = () => { S.upSel = new Set(sel); go("upgrade"); };
  $("#imp").onclick = importCsv;
  $("#ref").onclick = async () => { $("#tbl").innerHTML = '<div class="empty"><span class="spin"></span> Polling devices…</div>'; await loadInv(true); draw(); };
  draw(); await loadInv(); draw();
}

function importCsv() {
  const f = modal("Import devices", `<p>One device per line: <code>name,host,port,api_token[,type][,ssh_user,ssh_password]</code>. Type is <code>standalone</code> (default) or <code>fgcp</code> for a cluster address. Add FGSP groups with Create New.</p>
    <textarea id="csv" rows="10" placeholder="branch-01,10.1.0.1,443,xxxxxxxxxxxxxxxx&#10;dc-cluster,10.0.0.1,443,yyyyyyyyyyyyyyyy,fgcp,admin,secret"></textarea>
    <p class="hint">Tokens and passwords are stored in the OS keychain.</p>`,
  '<button value="cancel">Cancel</button><button type="button" class="primary" id="go">Import</button>');
  $("#go", f).onclick = async () => {
    try { const r = await api("/targets/import", { body: { csv: $("#csv", f).value } }); $("#modal").close(); await loadTargets(); await loadInv(true); pDevices();
      toast(`Imported ${r.created} device(s)${r.errors.length ? "\n\n" + r.errors.join("\n") : ""}`); } catch (e) { toast(e.message); }
  };
}

function memberRow(m = {}, i = 0, type = "standalone", tid = "") {
  const hint = type === "fgcp" ? (i === 0 ? "Cluster address (shared, follows the primary)" : `Unit ${i + 1}: dedicated HA management address (optional)`) : type === "fgsp" ? `FGSP peer ${i + 1}: its own management address` : "Device";
  return `<div class="member"><div class="row" style="justify-content:space-between"><b>${hint}</b>${i > 0 ? '<button type="button" class="small" data-rm>Remove</button>' : ""}</div>
    <div class="grid" style="margin-top:6px">
      <div class="field">Name<input name="name" value="${esc(m.name || "")}" placeholder="fw01"></div>
      <div class="field">IP / FQDN<input name="host" value="${esc(m.host || "")}" required></div>
      <div class="field">HTTPS port<input name="port" type="number" value="${esc(m.port || 443)}"></div>
      <div class="field">REST API token ${m.has_token ? "(stored — blank keeps it)" : ""}<div class="tok-in"><input name="token" type="password" autocomplete="off">${m.has_token && tid && m.id ? `<button type="button" class="small" data-showtok="${esc(tid)}|${esc(m.id)}" title="Show the stored token">${icon("eye")}Show</button>` : ""}</div><div class="tok-show" hidden></div></div>
      <div class="field">SSH user (optional)<input name="ssh_user" value="${esc(m.ssh_user || "")}"></div>
      <div class="field">SSH password ${m.has_ssh ? "(stored)" : ""}<input name="ssh_password" type="password" autocomplete="off"></div>
      <div class="field">SSH port<input name="ssh_port" type="number" value="${esc(m.ssh_port || 22)}"></div>
      <div class="field">Protocol<select name="scheme"><option ${m.scheme !== "http" ? "selected" : ""}>https</option><option ${m.scheme === "http" ? "selected" : ""}>http</option></select></div>
    </div><label class="cb"><input type="checkbox" name="verify_ssl" ${m.verify_ssl ? "checked" : ""}> Verify TLS certificate</label></div>`;
}
// Show the stored API token under the field for 60 s, with Copy and Hide.
async function showToken(btn) {
  const box = btn.closest(".field").querySelector(".tok-show");
  if (!box.hidden) { box.hidden = true; box.innerHTML = ""; btn.innerHTML = `${icon("eye")}Show`; return; }
  const [tid, mid] = btn.dataset.showtok.split("|");
  let tok;
  try { tok = (await api(`/targets/${tid}/members/${mid}/token`, { body: {} })).token; } catch (e) { return toast(e.message); }
  box.innerHTML = `<code class="tok-val">${esc(tok)}</code><button type="button" class="small" data-cp>Copy</button>`;
  box.hidden = false; btn.innerHTML = `${icon("eye")}Hide`;
  $("[data-cp]", box).onclick = async (e) => { try { await navigator.clipboard.writeText(tok); e.target.textContent = "Copied"; } catch { toast("Copy failed; select the token and copy it."); } };
  clearTimeout(btn._t); btn._t = setTimeout(() => { if (!box.hidden) btn.click(); }, 60000);
}
function editTarget(t = { type: "standalone", members: [{}] }) {
  const draw = () => {
    const f = modal(t.id ? `Edit ${t.name}` : "New device", `
      <div class="form"><label>Name</label><input id="tName" value="${esc(t.name || "")}">
      <label>Type</label><select id="tType"><option value="standalone">Standalone FortiGate</option><option value="fgcp">HA cluster (FGCP)</option><option value="fgsp">HA peers (FGSP)</option></select></div>
      <p class="hint">${{ standalone: "A single FortiGate.", fgcp: "<b>First address: the cluster's shared management IP</b>, an interface address that only the primary answers on and that moves with it on failover. All upgrades go through it. <b>Extra addresses (optional): each unit's dedicated HA management interface</b> (<code>ha-mgmt-interfaces</code>), which reaches that unit whichever unit is primary. Add them to use Local only or Secondary only, confirm each unit's version, and run checks on secondaries. Don't enter a secondary's ordinary interface IP: secondaries don't answer on the cluster's shared addresses.", fgsp: "Each peer is a separate FortiGate and is upgraded on its own. Add every peer's own management address." }[t.type]}</p>
      <div id="members">${t.members.map((m, i) => memberRow(m, i, t.type, t.id)).join("")}</div>
      ${t.type !== "standalone" ? '<button type="button" id="addMember">' + icon("plus") + 'Add member</button>' : ""}
      <p class="hint">Create the token on the FortiGate under System › Administrators › Create New › REST API Admin, with read/write on System and read on everything else. Tokens and SSH passwords are kept in the app's encrypted vault (Settings › Credential storage).</p>`,
    '<button value="cancel">Cancel</button><button type="button" class="primary" id="saveT">OK</button>');
    $("#tType", f).value = t.type;
    $("#tType", f).onchange = (e) => { collect(f); t.type = e.target.value; if (t.type === "standalone") t.members = t.members.slice(0, 1); draw(); };
    const am = $("#addMember", f); if (am) am.onclick = () => { collect(f); t.members.push({}); draw(); };
    $$("[data-rm]", f).forEach((b, i) => b.onclick = () => { collect(f); t.members.splice(i + 1, 1); draw(); });
    $$("[data-showtok]", f).forEach((b) => b.onclick = () => showToken(b));
    $("#saveT", f).onclick = async () => { collect(f);
      const hosts = t.members.map((m) => `${(m.host || "").trim()}:${m.port || 443}`);
      if (new Set(hosts).size !== hosts.length) return toast("Two addresses are the same. For an FGCP cluster, extra addresses must be each unit's own HA management address, not the cluster address again.");
      try { await api("/targets", { body: t }); $("#modal").close(); await loadTargets(); await loadInv(true); go(S.page === "device" ? "device" : "devices", S.dev); } catch (e) { toast(e.message); } };
  };
  const collect = (f) => {
    t.name = $("#tName", f).value;
    t.members = $$(".member", f).map((el, i) => {
      const o = { ...(t.members[i] || {}) };
      $$("input,select", el).forEach((inp) => {
        if (inp.type === "checkbox") o[inp.name] = inp.checked;
        else if (inp.type === "number") o[inp.name] = Number(inp.value);
        else if (inp.type === "password") { if (inp.value) o[inp.name] = inp.value; else delete o[inp.name]; }
        else o[inp.name] = inp.value;
      });
      return o;
    });
  };
  draw();
}

// ----------------------------------------------------------- device page
const DEV_TABS = [["overview", "Dashboard", "dashboard"], ["status", "Current status", "snapshots"], ["config", "Configuration", "notes"], ["upgrades", "Upgrades", "upgrade"], ["jobs", "Jobs", "jobs"]];
async function pDevice(arg) {
  const [id, tab] = Array.isArray(arg) ? arg : [arg, S.dev === arg ? S.devTab : "overview"];
  S.dev = id; S.devTab = tab || "overview";
  const t = S.targets.find((x) => x.id === id);
  if (!t) return go("devices");
  const i = S.inv[id] || {};
  const c = page(t.name, `Devices › ${typeName(t.type)}`,
    `<button id="ed">${icon("edit")}Edit</button><button class="primary" id="up">${icon("upgrade")}Upgrade</button>`,
    `<div class="tabs" id="dtabs">${DEV_TABS.map(([k, n, ic]) => `<button data-t="${k}" class="${k === S.devTab ? "on" : ""}">${icon(ic)}${n}${k === "jobs" ? ' <span class="count" id="jcount" hidden></span>' : ""}</button>`).join("")}</div>`,
    i.ok ? `<span class="chip ver">v${esc(i.version)}</span><span class="chip hw">${esc(i.model)}</span>` : "");
  $("#ed").onclick = () => editTarget(JSON.parse(JSON.stringify(t)));
  $("#up").onclick = () => { S.upSel = new Set([id]); go("upgrade"); };
  $$("#dtabs button").forEach((b) => b.onclick = () => go("device", [id, b.dataset.t]));
  const countJobs = async () => { try { const a = await api(`/jobs?target_id=${id}&active=true&children=true`); const el = $("#jcount"); if (el) { el.hidden = !a.length; el.textContent = a.length; } } catch { /* ignore */ } };
  countJobs(); S.pollers.jc = setInterval(countJobs, 5000);
  try {
    await ({ overview: devOverview, status: devStatus, config: devConfig, upgrades: devUpgrades, jobs: devJobs }[S.devTab])(t, c);
  } catch (e) { c.insertAdjacentHTML("afterbegin", `<div class="banner crit">${esc(e.message)}</div>`); }
}

// ---- dashboard tab
const st = (cls, txt, sym) => `<span class="st ${cls}"><i>${sym || { up: "▲", down: "▼", warn: "!", grey: "–", info: "i" }[cls]}</i>${esc(txt)}</span>`;
function gauge(label, pctv, sub) {
  const p = Math.max(0, Math.min(100, Number(pctv) || 0)), r = 38, C = 2 * Math.PI * r;
  const col = p >= 85 ? "var(--down)" : p >= 70 ? "var(--warn)" : "var(--up)";
  return `<div class="gauge"><svg viewBox="0 0 92 92"><circle cx="46" cy="46" r="${r}" fill="none" stroke="var(--rule)" stroke-width="9"/>
    <circle cx="46" cy="46" r="${r}" fill="none" stroke="${col}" stroke-width="9" stroke-dasharray="${(p / 100) * C} ${C}" transform="rotate(-90 46 46)" stroke-linecap="round"/>
    <text x="46" y="51" text-anchor="middle" font-size="18" font-weight="600" fill="var(--text)">${Math.round(p)}%</text></svg><b>${esc(label)}</b>${sub ? `<div>${esc(sub)}</div>` : ""}</div>`;
}
const metric = (rows, name) => { const r = (rows || []).find((x) => x.Metric === name); return r ? r.Value : null; };
const num = (v) => parseFloat(String(v ?? "").replace(/[^0-9.]/g, "")) || 0;
const accessHtml = (v) => !v || v === "—" ? '<span class="muted">—</span>' : v === "None" ? '<span class="muted">None</span>' :
  v.split(", ").map((a) => `<span class="chip ${["HTTP", "TELNET", "SNMP"].includes(a) ? "warn" : "grey"}" title="${["HTTP", "TELNET"].includes(a) ? "Unencrypted administrative access" : ""}">${esc(a)}</span>`).join("");
const daysTo = (d) => { const m = String(d || "").match(/^(\d\d)-(\d\d)-(\d{4})$/); return m ? Math.round((new Date(`${m[3]}-${m[2]}-${m[1]}`) - new Date()) / 864e5) : null; };

const NOT_A_PORT = /^(npu\d+_vlink\d+|.*vlink.*|modem|wwan|lte\d*|ssl\..*|l2t\..*|naf\..*|wqt\..*|fortilink|_default)$/i;
function portsFromInterfaces(ifs) {
  return (ifs || []).filter((x) => x.Type === "Physical" && !NOT_A_PORT.test(x.Interface)).map((x) => ({
    name: x.Interface, alias: x.Alias !== "—" ? x.Alias : "", status: x.Status, ip: x["IP address"] !== "—" ? x["IP address"] : "",
    speed: x.Speed, access: x["Admin access"], member_of: "", media: "" }));
}

async function devOverview(t, c) {
  c.innerHTML = '<div class="empty"><span class="spin"></span> Reading the device…</div>';
  const [ov, snaps, ups] = await Promise.all([api(`/targets/${t.id}/overview`), api(`/targets/${t.id}/snapshots`).catch(() => []), api(`/jobs?target_id=${t.id}&kind=upgrade`).catch(() => [])]);
  const ms = ov.members;
  let cur = ms.find((m) => m.member === S.ovMember) || ms.find((m) => m.ok) || ms[0];
  const draw = () => {
    const m = cur;
    const pick = ms.length > 1 ? `<div class="row" style="margin-bottom:12px"><span class="hint">Cluster member</span><div class="seg">${ms.map((x) => `<button data-m="${x.member}" class="${x.member === m.member ? "on" : ""}">${x.ok ? "" : "⚠ "}${esc(x.hostname || x.name)}</button>`).join("")}</div></div>` : "";
    if (!m.ok) {
      c.innerHTML = pick + `<div class="banner crit"><b>${esc(m.name)} (${esc(m.host)}) is unreachable.</b><br>${esc(m.error)}</div>` + (snaps[0] ? `<div class="hint">Last data collected ${esc(snaps[0].taken_at.replace("T", " "))}: see the Current status tab.</div>` : "");
      bindPick(); return;
    }
    const lic = m.licences || [];
    const active = lic.filter((l) => ["Licensed", "Registered", "Valid", "Free tier"].includes(l.Status));
    const expired = lic.filter((l) => ["Expired", "Invalid"].includes(l.Status));
    const soon = lic.filter((l) => { const d = daysTo(l.Expires); return d !== null && d >= 0 && d <= 30; });
    const key = ["FortiCare support", "Firmware updates", "Antivirus", "IPS", "Web filtering", "Application control", "Antispam", "Outbreak prevention", "VM licence", "FortiCloud logging"];
    const licRows = [...lic].sort((a, b) => (key.indexOf(a.Service) + 1 || 99) - (key.indexOf(b.Service) + 1 || 99));
    const ha = m.ha || [], hc = m.ha_config || {};
    const haText = m.ha_mode === "standalone" ? "Standalone" : `FGCP ${String(m.ha_mode).replace("a-p", "active-passive").replace("a-a", "active-active")}${hc["group-name"] ? ` · group "${hc["group-name"]}"` : ""}`;
    const weak = (m.interfaces || []).filter((x) => /HTTP(,|$)|TELNET/.test(x["Admin access"] || ""));
    const cpu = metric(m.resources, "CPU usage"), mem = metric(m.resources, "Memory usage"), disk = metric(m.resources, "Disk usage");
    const notices = [];
    if (typeMismatch(t)) notices.push(["info", `This unit reports ${m.ha_mode === "a-p" ? "FGCP active-passive" : m.ha_mode === "a-a" ? "FGCP active-active" : "FGSP"} HA but is saved as Standalone. Upgrades treat it as a cluster and let you choose the HA upgrade mode. Use "Save addresses" in the HA cluster panel to save it as a cluster with its cluster and unit addresses.`]);
    if (m.management?.managed) notices.push(["warn", `Managed by FortiManager ${m.management.fmg}. Upgrade from FortiManager, or follow the FortiManager checklist on the Upgrade page.`]);
    if (ha.length && ha.some((r) => r["Config in sync"] !== "Yes")) notices.push(["crit", "HA cluster configuration is out of sync."]);
    if (expired.length) notices.push(["crit", `${expired.length} licence(s) expired: ${expired.map((l) => l.Service).join(", ")}.`]);
    if (soon.length) notices.push(["warn", `${soon.length} licence(s) expire within 30 days: ${soon.map((l) => `${l.Service} (${l.Expires})`).join(", ")}.`]);
    if (weak.length) notices.push(["warn", `Unencrypted admin access (HTTP/TELNET) is enabled on ${weak.map((x) => x.Interface).join(", ")}.`]);
    if ((m.newer || []).length) notices.push(["info", `Newer firmware available from FortiGuard: ${m.newer.slice(0, 4).map((v) => "v" + v).join(", ")}${m.newer.length > 4 ? "…" : ""}.`]);
    const lastSnap = snaps.find((s) => s.member_id === m.member) || snaps[0];
    const lastUp = ups[0];
    c.innerHTML = pick + `<div class="widgets">
      <div class="panel fit"><div class="ph"><span class="ttl">System information</span><span class="tools"><button class="small" id="rf">${icon("refresh")}Refresh</button></span></div><div class="kv">
        <div>Hostname</div><div><b>${esc(m.hostname)}</b></div>
        <div>IP address</div><div class="mono">${esc(m.host)}${m.port && m.port !== 443 ? ":" + m.port : ""}</div>
        <div>Model</div><div>${esc(m.model)} <span class="hint">(${esc(m.model_code)})</span></div>
        <div>Serial number</div><div class="mono">${esc(m.serial)}</div>
        ${m.vm ? `<div>VM series</div><div>${m.vm.series ? `<span class="chip ver">${esc(m.vm.series)}</span>` : `<span class="hint">not reported</span>`}${m.vm.licence ? ` ${esc(m.vm.licence)}` : ""}${m.vm.basis ? ` <span class="hint">(from ${esc(m.vm.basis)})</span>` : ""}</div>
        ${m.vm.cpu_allowed != null || m.vm.cpu_used != null ? `<div>VM resources</div><div>${esc(m.vm.cpu_used ?? "?")} vCPU / ${esc(m.vm.cpu_allowed ?? "?")} allowed${m.vm.mem_used != null ? ` · ${esc(m.vm.mem_used)} MB RAM / ${esc(m.vm.mem_allowed ?? "?")}${m.vm.mem_allowed != null && m.vm.mem_allowed !== "unlimited" ? " MB" : ""}` : ""}</div>` : ""}` : ""}
        <div>Firmware</div><div><span class="chip ver">v${esc(m.version)}</span> build ${esc(m.build)}${m.release?.maturity ? ` <span class="chip ${m.release.maturity === "M" ? "ver" : "grey"}" title="Fortinet maturity tag">${m.release.maturity === "M" ? "Mature" : "Feature"}</span>` : ""}</div>
        <div>HA mode</div><div>${m.ha_mode === "standalone" ? st("grey", "Standalone", "1") : st("up", haText, "⇄")}${m.fgsp ? " " + badge("FGSP", "info") : ""}</div>
        <div>VDOMs</div><div>${m.vdom_mode ? `Multi-VDOM (${m.vdoms.length}): ${esc(m.vdoms.join(", "))}` : "Disabled"}</div>
        <div>Management</div><div>${m.management?.managed ? badge("FortiManager " + m.management.fmg, "warn") : esc(m.management?.summary || "Local")}</div>
        ${m.ha_mode !== "standalone" && (hc["upgrade-mode"] || hc["uninterruptible-upgrade"]) ? `<div>HA upgrade mode</div><div>${esc(hc["upgrade-mode"] || ("uninterruptible-upgrade " + hc["uninterruptible-upgrade"]))}</div>` : ""}
      </div></div>
      ${haPairPanel(t, m, ms) || `<div class="panel span2"><div class="ph"><span class="ttl">${esc(m.model)}</span><span class="tools hint">${(m.ports || portsFromInterfaces(m.interfaces)).length} physical ports</span></div><div class="faceplate">${faceplate(m.model, m.model_code, m.ports || portsFromInterfaces(m.interfaces))}</div></div>`}
      <div class="panel"><div class="ph"><span class="ttl">Licences</span><span class="tools"><button class="small" id="licAll">Show all ${lic.length}</button></span></div>
        <div class="tiles"><div class="tile good"><div class="n">${active.length}</div><div class="l">Active</div></div><div class="tile ${soon.length ? "warn" : ""}"><div class="n">${soon.length}</div><div class="l">Expire ≤ 30 days</div></div><div class="tile ${expired.length ? "bad" : ""}"><div class="n">${expired.length}</div><div class="l">Expired</div></div></div>
        <div class="pb flush" style="border-top:1px solid var(--rule)" id="licT"></div></div>
      <div class="panel"><div class="ph"><span class="ttl">Resources</span></div><div class="gauges">${gauge("CPU", num(cpu))}${gauge("Memory", num(mem))}${disk !== null ? gauge("Disk", num(disk)) : ""}</div>
        <div class="kv" style="border-top:1px solid var(--rule)"><div>IPv4 sessions</div><div>${esc(metric(m.resources, "IPv4 sessions") ?? "—")}</div><div>Session rate</div><div>${esc(metric(m.resources, "Session setup rate") ?? "—")}</div></div></div>
      <div class="panel"><div class="ph"><span class="ttl">Notices</span></div><div class="pb">${notices.length ? notices.map(([k, txt]) => `<div class="banner ${k}" style="margin-bottom:8px">${esc(txt)}</div>`).join("") : `<div class="row">${st("up", "No issues detected", "✓")}</div>`}</div></div>
      ${ha.length ? `<div class="panel"><div class="ph"><span class="ttl">HA cluster</span></div><div class="pb flush">${table(ha.map((r) => ({ "Serial number": r["Serial number"], Role: r.Role, "Config in sync": r["Config in sync"] })))}</div></div>` : ""}
      <div class="panel"><div class="ph"><span class="ttl">Activity</span></div><div class="kv">
        <div>Last data collection</div><div>${lastSnap ? `${esc(lastSnap.taken_at.replace("T", " "))} <span class="hint">(${esc(lastSnap.label)})</span> <button class="small link" id="toStatus">View</button>` : '<span class="muted">Never</span>'}</div>
        <div>Last upgrade</div><div>${lastUp ? `${esc(lastUp.created.replace("T", " "))} ${statusBadge(lastUp.status)}${lastUp.meta?.dry_run ? " " + badge("Simulation", "grey") : ""} <button class="small link" id="toUp">View</button>` : '<span class="muted">None recorded</span>'}</div>
      </div></div>
    </div>`;
    let all = false;
    const drawLic = () => { $("#licT").innerHTML = table((all ? licRows : licRows.slice(0, 8)).map((l) => ({ Service: l.Service, Status: l.Status, Expires: l.Expires })), { fmt: { Expires: (v) => { const d = daysTo(v); return `<span class="nw ${d !== null && d <= 30 ? "b-warn badge" : ""}">${esc(v)}</span>`; } } }); $("#licAll").textContent = all ? "Show fewer" : `Show all ${lic.length}`; };
    $("#licAll").onclick = () => { all = !all; drawLic(); }; drawLic();
    $("#rf").onclick = () => devOverview(t, c);
    bindHaPanel(t, c, () => go("device", [t.id, "overview"]));
    const ts = $("#toStatus"); if (ts) ts.onclick = () => go("device", [t.id, "status"]);
    const tu = $("#toUp"); if (tu) tu.onclick = () => go("device", [t.id, "upgrades"]);
    bindPick();
  };
  const bindPick = () => $$("[data-m]", c).forEach((b) => b.onclick = () => { S.ovMember = b.dataset.m; cur = ms.find((x) => x.member === b.dataset.m); draw(); });
  draw();
}

// ---- current status tab
const STATUS_SECTIONS = [
  { id: "interfaces", title: "Interfaces", checks: ["interfaces"], cols: ["Interface", "Alias", "Type", "Role", "Status", "IP address", "Admin access", "Speed"], filter: true },
  { id: "sdwan", title: "SD-WAN zones", checks: ["sdwan_zones"] },
  { id: "arp", title: "ARP table", checks: ["arp"], filter: true },
  { id: "neigh", title: "BGP / OSPF neighbours", checks: ["bgp", "bgp_tx", "ospf"], cmp: true },
  { id: "routes", title: "Routing table", checks: ["routes4", "routes6"], cmp: true, filter: true },
  { id: "ipsec", title: "IPsec tunnels", checks: ["ipsec"], cmp: true },
  { id: "bugs", title: "Known issues in the running release", custom: true },
  { id: "logs", title: "Event logs", checks: ["logs"], cmp: true, filter: true, levels: true },
];
const CHECK_NAMES = { bgp: "BGP neighbours", bgp_tx: "BGP advertised prefixes", ospf: "OSPF neighbours", routes4: "IPv4", routes6: "IPv6" };
const LEVEL_KIND = { Emergency: "down", Alert: "down", Critical: "down", Error: "down", Warning: "warn", Notice: "info", Information: "grey", Debug: "grey" };
const FMT = { Time: (v) => `<span class="nw mono">${esc(v)}</span>`, "Admin access": accessHtml, Level: (v) => badge(v, LEVEL_KIND[v] || "grey"), Message: (v) => `<span title="${esc(v)}">${esc(v)}</span>` };

async function devStatus(t, c) {
  const mid = S.stMember && t.members.some((m) => m.id === S.stMember) ? S.stMember : t.members[0].id;
  c.innerHTML = '<div class="empty"><span class="spin"></span> Loading the latest collection…</div>';
  const [snaps, latest] = await Promise.all([api(`/targets/${t.id}/snapshots`), api(`/targets/${t.id}/latest?member=${mid}`)]);
  const mine = snaps.filter((s) => !s.member_id || s.member_id === mid);
  const vdoms = latest?.meta?.vdoms || ["root"];
  const vd = vdoms.includes(S.stVdom) ? S.stVdom : vdoms[0];
  const memberSel = t.members.length > 1 ? `<select id="stm">${t.members.map((m) => `<option value="${m.id}" ${m.id === mid ? "selected" : ""}>${esc(m.name || m.host)}</option>`).join("")}</select>` : "";
  const vdomSel = vdoms.length > 1 ? `<span class="hint">VDOM</span><select id="stv">${vdoms.map((v) => `<option ${v === vd ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>` : "";
  c.innerHTML = `<div class="panel"><div class="toolbar ${latest ? "" : "info"}">${memberSel}${vdomSel}
      <span>${latest ? `Showing data collected <b>${esc(latest.meta.taken_at.replace("T", " "))}</b> <span class="hint">(${esc(latest.meta.label)} · v${esc(latest.meta.device.version)} · ${esc(latest.meta.file || latest.id + ".log")})</span>` : "No data has been collected from this device yet."}</span>
      <span class="grow"></span>${latest ? `<button id="olog">${icon("snapshots")}Open .log</button>` : ""}<button class="primary" id="collect">${icon("refresh")}Collect now</button></div><div id="colj"></div></div>
    <div id="secs"></div>`;
  if ($("#stm")) $("#stm").onchange = (e) => { S.stMember = e.target.value; devStatus(t, c); };
  if ($("#stv")) $("#stv").onchange = (e) => { S.stVdom = e.target.value; devStatus(t, c); };
  $("#collect").onclick = async () => {
    $("#collect").disabled = true;
    const j = await api(`/targets/${t.id}/snapshot`, { body: { label: "status", member_ids: [mid] } });
    watchJob(j.id, $("#colj"), { compact: true, onDone: () => setTimeout(() => devStatus(t, c), 600) });
  };
  if (latest) $("#olog").onclick = async () => { const txt = await api(`/targets/${t.id}/snapshots/${encodeURIComponent(latest.id)}/log`); modal(latest.id + ".log", `<pre style="max-height:70vh">${esc(txt)}</pre>`); };
  if (!latest) { $("#secs").innerHTML = '<div class="empty">Click <b>Collect now</b> to read interfaces, routing, neighbours, VPNs and logs from the device. It is read-only.</div>'; return; }
  const scopeOf = (cid) => (CHECKS_META()[cid]?.scope === "global" ? "global" : vd);
  const sum = (cid) => (latest.summary || []).find((x) => x.check === cid && x.scope === scopeOf(cid));
  $("#secs").innerHTML = STATUS_SECTIONS.map((s) => `<div class="panel" id="sec-${s.id}"><div class="ph"><span class="ttl">${esc(s.title)}${s.custom ? "" : s.checks.map((k) => sum(k) ? ` <span class="chip ${cardState(sum(k).text) === "ok" ? "grey" : "warn"}" title="${esc(CHECK_NAMES[k] || "")}">${esc((s.checks.length > 1 ? (CHECK_NAMES[k] || k) + ": " : "") + sum(k).text)}</span>` : "").join("")}</span>
    <span class="tools">${s.levels ? `<div class="seg" data-lv><button class="on" data-l="all">All</button><button data-l="warn">Warning+</button><button data-l="err">Error+</button></div>` : ""}${s.filter ? `<input placeholder="Filter" data-f style="width:150px">` : ""}${s.cmp ? `<button class="small" data-cmp>${icon("snapshots")}Compare</button>` : ""}</span></div>
    <div data-cmpbox></div><div class="pb flush tablewrap" data-body></div></div>`).join("");
  for (const s of STATUS_SECTIONS) {
    const el = $(`#sec-${s.id}`), body = $("[data-body]", el);
    if (s.custom) { knownIssues(t, body); continue; }
    let q = "", lvl = "all";
    const draw = () => {
      body.innerHTML = s.checks.map((cid) => {
        const sc = scopeOf(cid);
        const err = latest.errors?.[cid]?.[sc] || latest.errors?.[cid]?.["*"];
        let rows = latest.results?.[cid]?.[sc];
        const head = s.checks.length > 1 ? `<div class="toolbar" style="background:var(--panel-head)"><b>${esc(CHECK_NAMES[cid] || cid)}</b></div>` : "";
        if (rows === undefined) return head + `<div class="empty">${err ? esc(err) : "Not included in this collection. Click <b>Collect now</b> to gather it."}</div>`;
        if (typeof rows === "string") return head + `<pre>${esc(rows)}</pre>`;
        if (q) rows = rows.filter((r) => JSON.stringify(r).toLowerCase().includes(q));
        if (lvl !== "all") rows = rows.filter((r) => lvl === "err" ? LEVEL_KIND[r.Level] === "down" : ["down", "warn"].includes(LEVEL_KIND[r.Level]));
        return head + table(rows, { cols: s.cols && rows[0] ? s.cols.filter((k) => k in rows[0]) : undefined, fmt: FMT, limit: 500, empty: q || lvl !== "all" ? "No matching entries" : "None" });
      }).join("");
    };
    draw();
    const f = $("[data-f]", el); if (f) f.oninput = () => { q = f.value.toLowerCase(); draw(); };
    $$("[data-l]", el).forEach((b) => b.onclick = () => { lvl = b.dataset.l; $$("[data-l]", el).forEach((x) => x.classList.toggle("on", x === b)); draw(); });
    const cb = $("[data-cmp]", el);
    if (cb) cb.onclick = () => compareBar(t, latest, mine, s, $("[data-cmpbox]", el), scopeOf);
  }
}
function CHECKS_META() { const o = {}; (S.meta.checks || []).forEach((k) => o[k.id] = k); return o; }

function compareBar(t, latest, snaps, sec, box, scopeOf) {
  const older = snaps.filter((s) => s.id !== latest.id && s.taken_at <= latest.meta.taken_at);
  if (box.innerHTML) { box.innerHTML = ""; return; }
  if (!older.length) { box.innerHTML = '<div class="cmpbar">No earlier collection from this device to compare with. <button class="small" data-x>Close</button></div>'; $("[data-x]", box).onclick = () => box.innerHTML = ""; return; }
  box.innerHTML = `<div class="cmpbar"><b>Compare with</b><select data-pre>${older.map((s) => `<option value="${esc(s.id)}">${esc(s.taken_at.replace("T", " "))} · ${esc(s.label)} · v${esc(s.device.version)}</option>`).join("")}</select>
    <span class="hint">→ current (${esc(latest.meta.taken_at.replace("T", " "))})</span><button class="small primary" data-go>Compare</button><button class="small" data-x>Close</button></div><div data-res></div>`;
  $("[data-x]", box).onclick = () => box.innerHTML = "";
  const run = async () => {
    const res = $("[data-res]", box); res.innerHTML = '<div class="empty"><span class="spin"></span></div>';
    const d = await api(`/targets/${t.id}/diff?pre=${encodeURIComponent($("[data-pre]", box).value)}&post=${encodeURIComponent(latest.id)}&checks=${sec.checks.join(",")}`);
    const rows = d.checks.filter((k) => k.scope === scopeOf(k.check)).flatMap((k) => k.changes.map((x) => ({ ...x, check: k.name })));
    res.innerHTML = `<div class="toolbar"><span>${sevBadge(d.totals.critical ? "critical" : d.totals.warning ? "warning" : d.totals.info ? "info" : null)}</span>
      <span><b>${d.totals.critical}</b> critical · <b>${d.totals.warning}</b> warning · <b>${d.totals.info}</b> informational</span>${sec.id === "logs" ? '<span class="hint">New events since the earlier collection</span>' : ""}</div>
      ${rows.length ? `<table><thead><tr><th style="width:90px">Severity</th>${sec.checks.length > 1 ? "<th>Table</th>" : ""}<th>Item</th><th>Change</th></tr></thead><tbody>${rows.slice(0, 800).map((x) => `<tr><td>${sevBadge(x.severity)}</td>${sec.checks.length > 1 ? `<td>${esc(x.check)}</td>` : ""}<td><b>${esc(x.key)}</b></td><td>${changeDesc(x)}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No differences</div>'}`;
  };
  $("[data-go]", box).onclick = run; run();
}

async function knownIssues(t, body, refresh = false) {
  body.innerHTML = '<div class="empty"><span class="spin"></span> Reading the FortiOS release notes from docs.fortinet.com (only downloaded once per version)…</div>';
  let k;
  try { k = await api(`/targets/${t.id}/known-issues${refresh ? "?refresh=true" : ""}`); } catch (e) {
    body.innerHTML = `<div class="banner warn" style="margin:12px">${esc(e.message)} <button class="small" data-r>Retry</button></div>`; $("[data-r]", body).onclick = () => knownIssues(t, body, true); return;
  }
  let all = !k.relevant;
  const isNew = (i) => /new/i.test(i.group);
  const draw = () => {
    const rows = k.issues.filter((i) => all || i.relevant);
    body.innerHTML = `<div class="tiles" style="border-bottom:1px solid var(--rule)"><div class="tile"><div class="n">${k.issues.length}</div><div class="l">Known issues in v${esc(k.version)}</div></div>
      <div class="tile ${k.relevant ? "warn" : "good"}"><div class="n">${k.relevant}</div><div class="l">May affect this configuration</div></div><div class="tile"><div class="n">${k.issues.filter(isNew).length}</div><div class="l">New in this release</div></div></div>
      <div class="toolbar"><div class="seg"><button data-a="0" class="${all ? "" : "on"}">May affect this device</button><button data-a="1" class="${all ? "on" : ""}">All ${k.issues.length}</button></div>
      <span class="grow"></span><span class="hint">${k.config_checked ? "Rated against the configuration from the latest collection." : "No configuration collected yet: rating is by category only."} Downloaded ${esc(k.fetched_at.replace("T", " "))}</span>
      <a href="${esc(k.url)}" target="_blank" rel="noopener">Release notes ↗</a><button class="small" data-r>${icon("refresh")}Re-download</button></div>
      ${rows.length ? table(rows.map((i) => ({ "Bug ID": i.bug_id, Category: i.category, Description: i.description, "Why flagged": i.reason || "—", " ": isNew(i) ? "New" : "" })), { fmt: { "Bug ID": (v) => `<span class="mono">${esc(v)}</span>`, " ": (v) => v ? badge(v, "info") : "" } }) : '<div class="empty">No known issue matches the features this device uses.</div>'}`;
    $$("[data-a]", body).forEach((b) => b.onclick = () => { all = b.dataset.a === "1"; draw(); });
    $("[data-r]", body).onclick = () => knownIssues(t, body, true);
  };
  draw();
}

// ---- upgrades tab
const dur = (a, b) => { if (!a || !b) return "—"; const s = Math.max(0, (new Date(b) - new Date(a)) / 1000); return s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`; };
async function devUpgrades(t, c) {
  const js = await api(`/jobs?target_id=${t.id}&kind=upgrade`);
  c.innerHTML = `<div class="panel"><div class="ph"><span class="ttl">Upgrade history</span><span class="tools hint">${js.length} job(s) · click a row for details</span></div>
    <div class="pb flush">${js.length ? `<table><thead><tr><th>Started</th><th>Target version</th><th>Mode</th><th>Status</th><th>Stage</th><th>Duration</th></tr></thead><tbody>${js.map((j) => `<tr class="click" data-j="${j.id}">
      <td>${esc(j.created.replace("T", " "))}</td><td>${j.meta?.target_version ? `<span class="chip ver">v${esc(j.meta.target_version)}</span>` : "—"}</td><td>${j.meta?.dry_run ? badge("Simulation", "grey") : badge("Upgrade", "info")}</td>
      <td>${statusBadge(j.status)}</td><td>${esc(j.stage)}</td><td>${dur(j.created, j.finished)}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No upgrades have been run on this device from this app.</div>'}</div></div>
    <div id="jd"></div>`;
  $$("[data-j]", c).forEach((r) => r.onclick = () => { $$("[data-j]", c).forEach((x) => x.classList.toggle("sel", x === r)); jobDetail(r.dataset.j, $("#jd")); });
  if (js[0]) $(`[data-j="${js[0].id}"]`, c).click();
}

async function jobDetail(id, el) {
  const j = await api(`/jobs/${id}`);
  if (["running", "waiting"].includes(j.status)) { el.innerHTML = '<div class="panel" id="jdp"></div>'; watchJob(id, $("#jdp", el), {}); return; }
  const r = j.result || {};
  if (/^(Failed|Cancelled)$/.test(j.stage || "")) {   // older records: recover the stage from the log
    const last = [...(j.log || [])].reverse().find((l) => l.level === "stage");
    if (last) j.stage = `${j.stage} during: ${last.msg.replace(/^— | —$/g, "")}`;
  }
  if (/HTTP 413/.test(j.error || "") && !/too large/.test(j.error)) j.error += " · The FortiGate refused the upload as too large. This version of the app uploads images as a file (like the FortiOS GUI); run the upgrade again, or use 'Download from FortiGuard'.";
  el.innerHTML = `<div class="panel"><div class="ph"><span class="ttl">${esc(j.title)} ${statusBadge(j.status)}${j.meta?.dry_run ? " " + badge("Simulation", "grey") : ""}</span><span class="tools"><span class="hint">${esc(j.created.replace("T", " "))} → ${esc((j.finished || "").replace("T", " ") || "—")} · ${dur(j.created, j.finished)}</span>${j.kind === "upgrade" ? ` <button class="small" id="jdRep">${icon("notes")}PDF report</button>` : ""}</span></div>
    <div class="pb">${j.kind === "upgrade" ? stepper(j.stage, j.status) + `<div class="hint" style="margin-bottom:8px">${esc(j.stage)}</div>` : ""}
      ${r.path ? `<div class="row" style="margin:8px 0"><span class="hint">Path</span><div class="path">${r.path.map((v, k) => `${k ? '<span class="arrow">→</span>' : ""}<span class="v">${esc(v)}</span>`).join("")}</div></div>` : ""}
      ${j.error ? `<div class="banner crit">${esc(j.error)}</div>` : ""}
      ${probePanel(j.probes || j.parent_probes, { openWindows: true })}
      ${(r.reports || []).map((x) => `<div class="row" style="margin:6px 0">${sevBadge(x.totals.critical ? "critical" : x.totals.warning ? "warning" : null)} <b>${esc(x.member)}</b> ${x.totals.critical} critical · ${x.totals.warning} warning · ${x.totals.info} info
        <button class="small" data-diff="${esc(j.target_id)}|${esc(x.pre)}|${esc(x.post)}">View pre/post diff</button><span class="hint">${esc(x.pre)}.log → ${esc(x.post)}.log</span></div>`).join("")}
    </div><details class="sec" style="margin:0 12px 12px" open><summary>Log <span class="hint">${(j.log || []).length} lines</span></summary><div class="log" style="height:320px">${(j.log || []).filter((l) => l.level !== "debug").map((l) => `<div class="${l.level}">${esc(l.t)}  ${esc(l.msg)}</div>`).join("")}</div></details></div>`;
  $$("[data-diff]", el).forEach((b) => b.onclick = () => { const [tt, pre, post] = b.dataset.diff.split("|"); diffModal(tt, pre, post); });
  const rb = $("#jdRep", el);
  if (rb) rb.onclick = () => { rb.remove(); el.insertAdjacentHTML("afterbegin", '<div id="jdRepP"></div>'); reportPanel(id, $("#jdRepP", el)); };
}

// ---- jobs tab
async function devJobs(t, c) {
  const [act, all] = await Promise.all([api(`/jobs?target_id=${t.id}&active=true&children=true`), api(`/jobs?target_id=${t.id}&children=true`)]);
  const recent = all.filter((j) => !["running", "waiting"].includes(j.status)).slice(0, 25);
  c.innerHTML = `<div class="panel"><div class="ph"><span class="ttl">In progress</span><span class="tools hint">${act.length} running</span></div><div id="act">${act.length ? "" : '<div class="empty">Nothing is running on this device.</div>'}</div></div>
    <div class="panel"><div class="ph"><span class="ttl">Recent activity</span></div><div class="pb flush">${recent.length ? `<table><thead><tr><th>Started</th><th>Job</th><th>Type</th><th>Status</th><th>Duration</th></tr></thead><tbody>${recent.map((j) => `<tr class="click" data-j="${j.id}"><td>${esc(j.created.replace("T", " "))}</td><td><b>${esc(j.title)}</b></td><td>${esc(KIND_NAME[j.kind] || j.kind)}${j.meta?.dry_run ? " " + badge("Simulation", "grey") : ""}</td><td>${statusBadge(j.status)}</td><td>${dur(j.created, j.finished)}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No jobs yet</div>'}</div></div><div id="jd"></div>`;
  act.forEach((j, k) => { $("#act").insertAdjacentHTML("beforeend", `<div id="aj${k}" style="border-top:${k ? "1px solid var(--rule)" : "none"}"></div>`); watchJob(j.id, $(`#aj${k}`), { compact: true }); });
  $$("[data-j]", c).forEach((r) => r.onclick = () => { $$("[data-j]", c).forEach((x) => x.classList.toggle("sel", x === r)); jobDetail(r.dataset.j, $("#jd")); $("#jd").scrollIntoView({ behavior: "smooth" }); });
}
const KIND_NAME = { upgrade: "Upgrade", batch: "Upgrade (batch)", snapshot: "Data collection", preflight: "Pre-flight" };

async function versionsFor(ids) {
  const out = new Set();
  await Promise.all(ids.map(async (id) => { try { (await api(`/targets/${id}/firmware`)).available.forEach((a) => out.add(String(a.version).replace(/^v/, ""))); } catch { /* offline */ } }));
  (S.images.length ? S.images : await api("/images")).forEach((i) => i.version && out.add(i.version));
  return [...out].sort((a, b) => vcmp(b, a));
}

function fmgBanner(ts) {
  return `<div class="banner warn"><b>Managed by FortiManager:</b> ${ts.map((t) => `${esc(t.name)} (${esc(S.inv[t.id]?.management?.fmg || "")})`).join(", ")}
    <ul><li>Make sure FortiManager and the ADOM support the target version; upgrade FortiManager / the ADOM first if needed.</li>
    <li>Install or discard pending policy-package and device-settings changes in FortiManager first.</li>
    <li>Disable or align FortiManager firmware templates and scheduled upgrades for these devices; they can conflict with or revert this upgrade.</li>
    <li>Afterwards, run <b>Retrieve Config</b> / refresh the device in FortiManager before the next install.</li>
    <li>Preferably perform the upgrade from FortiManager (Device Manager › Firmware).</li></ul></div>`;
}

// ---------------------------------------------------------- pre-flight
function renderPreflights(res, el) {
  const items = res?.results || [];
  el.insertAdjacentHTML("beforeend", items.map((it) => {
    const r = it.result; if (!r) return `<div class="banner crit"><b>${esc(it.target)}</b>: ${esc(it.error || it.status)}</div>`;
    const p = r.plan || { steps: [] }; const a = r.analysis || { rule_hits: [], notes_hits: [], features: [] };
    const worst = r.findings.some((f) => f.severity === "critical") ? "critical" : r.findings.some((f) => f.severity === "warning") ? "warning" : null;
    return `<details class="sec" open><summary>${sevBadge(worst)}<b>${esc(it.target)}</b><span class="hint">${esc(r.members[0]?.model || "")} · v${esc(r.members[0]?.version || "")} → ${esc(p.target || "")}</span></summary><div style="padding:10px">
      <div class="row"><b>Upgrade path</b><div class="path">${p.steps.map((s) => `<span class="v">${esc(s)}</span>`).join("<span>→</span>")}</div>${p.verified ? badge("Verified · " + p.source, "up") : badge("Unverified · " + p.source, "warn")}</div>
      ${(p.notes || []).map((n) => `<div class="hint">${esc(n)}</div>`).join("")}
      <h4>Image availability</h4>${table((r.images || []).map((x) => ({ Version: x.version, FortiGuard: x.fortiguard ? "Yes" : "No", "Firmware store": x.store || "No" })), { empty: "No upgrade needed" })}
      <h4>Findings</h4><table><tbody>${r.findings.map((f) => `<tr><td style="width:90px">${sevBadge(f.severity)}</td><td>${esc(f.message)}</td></tr>`).join("")}</tbody></table>
      ${r.ha ? `<h4>HA settings</h4>${table([Object.fromEntries(Object.entries(r.ha).map(([k, v]) => [k.replace(/-/g, " "), v]))])}` : ""}
      <h4>Configuration vs release notes</h4><div class="hint">Features in use: ${esc(a.features.map((f) => f.replace("config ", "")).join(", ") || "none detected")}</div>
      ${a.rule_hits.map((h) => `<div class="banner ${h.severity === "critical" ? "crit" : h.severity === "warning" ? "warn" : "info"}" style="margin-top:8px"><b>${esc(h.title)}</b> <span class="hint">from ${esc(h.introduced_in || "any version")}</span><div>${esc(h.message)}</div><div class="hint">Evidence: ${h.evidence.map((e) => `<code>${esc(e)}</code>`).join(" ")} · ${esc(h.reference)}</div></div>`).join("")}
      ${a.notes_sources?.length ? "" : '<div class="banner info" style="margin-top:8px">No release notes loaded. Add the release notes of every version on the path under <b>Release Notes</b> for a thorough check.</div>'}
      ${a.notes_hits.length ? `<h4>Release-notes paragraphs mentioning features you use (${a.notes_hits.length})</h4>${table(a.notes_hits.map((h) => ({ Severity: h.severity === "warning" ? "Warning" : "Info", Source: h.source, Section: h.section, Feature: h.term + (h.bug_id ? ` (#${h.bug_id})` : ""), Text: h.text })))}` : ""}
    </div></details>`; }).join(""));
}

// ------------------------------------------------------------ PDF report
async function reportPanel(jobId, el, auto = true) {
  const draw = (r, busy, err) => {
    el.innerHTML = `<div class="panel report-card"><div class="ph"><span class="ttl">${icon("notes")} Upgrade report (PDF)</span></div><div class="pb">
      ${busy ? '<div class="row"><span class="spin"></span> Building the report: settings, pre-flight, timeline, pre/post checks, check and configuration diffs, warnings and errors, full activity log…</div>'
        : err ? `<div class="banner crit">${esc(err)}</div><button id="rpRetry">Try again</button>`
        : `<div class="row"><b class="mono">${esc(r.name)}</b><span class="hint">${fmtSize(r.size)} · saved in the app's Reports folder</span></div>
           <div class="row" style="margin-top:10px"><button class="primary" id="rpOpen">${icon("notes")}Open PDF</button><button id="rpShow">${icon("folder")}Show in folder</button>
           <button id="rpDl">${icon("import")}Download</button><button id="rpRe">${icon("refresh")}Regenerate</button></div>`}</div></div>`;
    if (busy) return;
    if (err) { $("#rpRetry", el).onclick = gen; return; }
    $("#rpOpen", el).onclick = () => api("/open-file", { body: { path: r.path } }).catch((e) => toast(e.message));
    $("#rpShow", el).onclick = () => api("/open-file", { body: { path: r.path, reveal: true } }).catch((e) => toast(e.message));
    $("#rpRe", el).onclick = gen;
    $("#rpDl", el).onclick = async () => {
      const res = await fetch(`/api/jobs/${jobId}/report.pdf`, { headers: { "X-Auth": TOKEN } });
      if (!res.ok) return toast("Could not download the report");
      const a = document.createElement("a"); a.href = URL.createObjectURL(await res.blob()); a.download = r.name; a.click();
    };
  };
  const gen = async () => { draw(null, true); try { draw(await api(`/jobs/${jobId}/report`, { body: {} })); } catch (e) { draw(null, false, e.message); } };
  if (auto) await gen(); else draw(null, false, null);
}

// The upgrade wizard lives in wizard.js

// ---------------------------------------------------------- job viewing
const STEPS = ["Pre-checks", "Transfer", "Install & reboot", "Post-checks", "Done"];
function stepIdx(stage = "", status) {
  if (status === "done") return 5;
  if (/Post-upgrade|Comparing/.test(stage)) return 3;
  if (/Installing|Verifying HA|Restoring/.test(stage)) return 2;
  if (/Transferring|Setting HA/.test(stage)) return 1;
  return 0;
}
function stepper(stage, status) {
  const k = stepIdx(stage, status);
  return `<div class="steps">${STEPS.map((s, i) => `<div class="${i < k || status === "done" ? "done" : i === k ? (status === "failed" || status === "cancelled" ? "fail" : "cur") : ""}">${s}</div>`).join("")}</div>`;
}
const statusBadge = (s) => ({ done: badge("Complete", "up"), failed: badge("Failed", "down"), cancelled: badge("Cancelled", "warn"), waiting: badge("Awaiting approval", "info"), running: badge("Running", "info"), interrupted: badge("Interrupted", "warn") }[s] || esc(s));

function watchJob(id, el, opts = {}) {
  el.innerHTML = `<div class="toolbar"><b id="jt"></b><span id="js"></span><span id="jstage" class="hint"></span><span class="grow"></span><button class="small" id="jc">Cancel</button></div>
    <div id="jg"></div><div id="kids"></div><details class="sec" style="margin:8px 12px" ${opts.batch || opts.compact ? "" : "open"}><summary>Log</summary><div class="log" id="jl"></div></details><div id="jres"></div>`;
  let since = 0, shown = null;
  const log = $("#jl", el);
  $("#jc", el).onclick = () => { if (confirm("Cancel this job? Devices already rebooting will finish their upgrade.")) api(`/jobs/${id}/cancel`, { method: "POST" }); };
  const tick = async () => {
    let j; try { j = await api(`/jobs/${id}?since=${since}`); } catch { return; }
    $("#jt", el).textContent = j.title; $("#js", el).innerHTML = statusBadge(j.status); $("#jstage", el).textContent = j.stage || "";
    for (const l of j.log) log.insertAdjacentHTML("beforeend", `<div class="${l.level}">${esc(l.t)}  ${esc(l.msg)}</div>`);
    if (j.log.length) log.scrollTop = log.scrollHeight;
    since = j.log_len;
    const g = $("#jg", el);
    if (j.prompt !== shown) {
      shown = j.prompt;
      g.innerHTML = j.prompt ? `<div class="gate"><b>${esc(j.prompt)}</b><div class="row" style="margin-top:8px"><button class="primary" id="ja">${icon("check")}Approve</button><button id="jr">Stop</button></div></div>` : "";
      if (j.prompt) { $("#ja", g).onclick = () => api(`/jobs/${id}/answer`, { body: { approve: true } }); $("#jr", g).onclick = () => api(`/jobs/${id}/answer`, { body: { approve: false } }); }
    }
    if (j.children_detail?.length && j.kind === "batch") {
      $("#kids", el).innerHTML = `<div class="devcards">${j.children_detail.map((k) => `<div class="devcard"><div class="dh"><b>${esc(k.title)}</b>${statusBadge(k.status)}</div><div class="db">
        ${stepper(k.stage, k.status)}<div><b>${esc(k.stage || "")}</b></div><div class="lastline" title="${esc(k.last)}">${esc(k.error || k.last)}</div>
        ${k.prompt && j.status !== "waiting" ? `<div class="gate" style="margin:8px 0"><b>${esc(k.prompt)}</b><div class="row" style="margin-top:6px"><button class="small primary" data-ok="${k.id}">Approve</button><button class="small" data-no="${k.id}">Stop</button></div></div>` : ""}
        ${(k.result?.reports || []).map((r) => `<div class="row" style="margin-top:6px">${sevBadge(r.totals.critical ? "critical" : r.totals.warning ? "warning" : null)} ${esc(r.member)}: ${r.totals.critical} critical · ${r.totals.warning} warning <button class="small link" data-diff="${k.target_id}|${r.pre}|${r.post}">View diff</button></div>`).join("")}
        <button class="small link" data-log="${k.id}">Device log</button></div></div>`).join("")}</div>`;
      $$("[data-ok]", el).forEach((b) => b.onclick = () => api(`/jobs/${b.dataset.ok}/answer`, { body: { approve: true } }));
      $$("[data-no]", el).forEach((b) => b.onclick = () => api(`/jobs/${b.dataset.no}/answer`, { body: { approve: false } }));
      $$("[data-diff]", el).forEach((b) => b.onclick = () => { const [t, pre, post] = b.dataset.diff.split("|"); diffModal(t, pre, post); });
      $$("[data-log]", el).forEach((b) => b.onclick = async () => { const k = await api(`/jobs/${b.dataset.log}`); modal(`Log: ${k.title}`, `<div class="log" style="height:60vh">${k.log.map((l) => `<div class="${l.level}">${esc(l.t)}  ${esc(l.msg)}</div>`).join("")}</div>`); });
    }
    if (j.kind === "upgrade" && !$("#jstep", el)) $("#jg", el).insertAdjacentHTML("beforebegin", '<div id="jstep" style="padding:0 12px"></div>');
    if ($("#jstep", el)) $("#jstep", el).innerHTML = stepper(j.stage, j.status);
    if (["done", "failed", "cancelled", "interrupted"].includes(j.status) && !j.children_detail?.some((k) => ["running", "waiting"].includes(k.status))) {
      clearInterval(S.pollers[id]); delete S.pollers[id]; $("#jc", el)?.remove();
      if (j.status === "failed") $("#jres", el).innerHTML = `<div class="banner crit" style="margin:8px 12px">${esc(j.error)}</div>`;
      if (opts.onDone) opts.onDone(await api(`/jobs/${id}?since=${since}`));
    }
  };
  S.pollers[id] = setInterval(tick, 1200);
  tick();
}

// ----------------------------------------------------- snapshots & diff (list and compare view: snapshots.js)
async function snapModal(tid, sid) {
  const s = await api(`/targets/${tid}/snapshots/${encodeURIComponent(sid)}`);
  const d = s.meta.device;
  const sum = s.summary || [];
  const f = modal(s.meta.file || sid, `<div class="row" style="margin-bottom:10px"><b>${esc(d.hostname)}</b><span>${esc(d.model)}</span><span class="chip ver">v${esc(d.version)} b${esc(d.build)}</span><span class="hint">${esc(s.meta.label)} · ${esc(s.meta.taken_at.replace("T", " "))}</span>${d.management?.managed ? badge("FortiManager " + d.management.fmg, "warn") : ""}</div>
    <div class="summary-cards">${sum.map((x, i) => `<div class="scard ${cardState(x.text)}" data-i="${i}"><b>${esc(x.name)}</b><span>${esc(scopeName(x.scope))} · ${esc(x.text)}</span></div>`).join("")}</div>
    ${Object.entries(s.results).map(([cid, scopes]) => Object.entries(scopes).map(([sc, rows]) => { const k = S.meta.checks.find((c) => c.id === cid); return `<details class="sec" id="sec-${cid}-${sc}"><summary><b>${esc(k ? k.name : cid)}</b><span class="hint">${esc(scopeName(sc))}</span></summary><div>${table(rows)}</div></details>`; }).join("")).join("")}
    ${Object.keys(s.errors).length ? `<details class="sec"><summary><b>Skipped / failed</b></summary><div>${table(Object.entries(s.errors).flatMap(([c, e]) => Object.entries(e).map(([sc, m]) => ({ Check: S.meta.checks.find((k) => k.id === c)?.name || c, Scope: sc === "*" ? "All" : scopeName(sc), Reason: m }))))}</div></details>` : ""}`);
  $$(".scard", f).forEach((card) => card.onclick = () => { const x = sum[card.dataset.i]; const d2 = $(`#sec-${x.check}-${x.scope}`, f); if (d2) { d2.open = true; d2.scrollIntoView({ behavior: "smooth" }); } });
}
function cardState(text) {
  if (/OUT OF SYNC/.test(text)) return "crit";
  const down = text.match(/(\d+) down/);
  if (down && Number(down[1]) > 0) return "warn";
  const frac = text.match(/^(\d+)\/(\d+) (established|full|valid)/);
  if (frac && Number(frac[1]) < Number(frac[2])) return "warn";
  return "ok";
}
function fmtVal(v) { return v && typeof v === "object" ? Object.entries(v).filter(([, x]) => x !== "—").map(([k, x]) => `${k}: ${x}`).join(" · ") : v; }
const changeDesc = (c) => c.type === "changed" ? `<b>${esc(c.field)}</b>: <span class="before">${val(c.before)}</span><span class="arrow">→</span><span class="after">${val(c.after)}</span>`
  : c.type === "count" ? `Route count <span class="before">${c.before}</span><span class="arrow">→</span><span class="after">${c.after}</span>`
  : c.type === "removed" ? `<span class="before">No longer present</span> <span class="hint">${esc(fmtVal(c.before))}</span>` : `<span class="after">New</span> <span class="hint">${esc(fmtVal(c.after))}</span>`;
async function diffModal(tid, pre, post) {
  const d = await api(`/targets/${tid}/diff?pre=${encodeURIComponent(pre)}&post=${encodeURIComponent(post)}`);
  const cfg = d.config;
  const desc = changeDesc;
  const f = modal(`Comparison: ${pre} → ${post}`, `
    <div class="row" style="justify-content:flex-end;margin-bottom:6px"><button class="small primary" id="dmSbs">${icon("snapshots")}Open side by side</button></div>
    <div class="row" style="margin-bottom:10px"><span class="chip ver">v${esc(d.pre.device.version)}</span><span>→</span><span class="chip ver">v${esc(d.post.device.version)}</span><span class="hint">${esc(d.pre.device.hostname)}</span></div>
    <div class="totals"><div><b style="color:var(--down)">${d.totals.critical}</b>Critical</div><div><b style="color:var(--warn)">${d.totals.warning}</b>Warning</div><div><b style="color:var(--info)">${d.totals.info}</b>Informational</div>${cfg ? `<div><b>${cfg.summary.removed + cfg.summary.changed}</b>Config removed/changed</div>` : ""}</div>
    ${d.checks.map((c) => `<details class="sec" ${c.worst === "critical" ? "open" : ""}><summary>${sevBadge(c.worst)}<b>${esc(c.name)}</b><span class="hint">${esc(scopeName(c.scope))}${c.before_count !== undefined ? ` · ${c.before_count} → ${c.after_count} entries` : ""}</span></summary><div>
      ${c.kind === "text" ? (c.diff ? `<pre>${esc(c.diff)}</pre>` : '<div class="empty">No differences</div>') : c.changes.length ? `<table><thead><tr><th style="width:90px">Severity</th><th>Item</th><th>Change</th></tr></thead><tbody>${c.changes.slice(0, 1500).map((x) => `<tr><td>${sevBadge(x.severity)}</td><td><b>${esc(x.key)}</b></td><td>${desc(x)}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No differences</div>'}</div></details>`).join("")}
    ${d.config_note ? `<div class="banner warn" style="margin-top:10px">${esc(d.config_note)}</div>` : ""}
    ${cfg ? `<h3>Configuration</h3><div class="hint">New default settings appear after most upgrades; review removals and changes. Secrets and certificates are masked.</div>
      <details class="sec" open><summary><b>Removed or changed</b><span class="hint">${cfg.summary.removed} removed · ${cfg.summary.changed} changed</span></summary><div>${table(cfg.changes.filter((x) => x.type !== "added").map((x) => ({ Change: x.type === "removed" ? "Removed" : "Changed", Setting: x.path.replace(/ > /g, " › "), Before: x.before, After: x.after ?? "—" })), { empty: "Nothing removed or changed" })}</div></details>
      <details class="sec"><summary><b>Added</b><span class="hint">${cfg.summary.added}</span></summary><div>${table(cfg.changes.filter((x) => x.type === "added").map((x) => ({ Setting: x.path.replace(/ > /g, " › "), Value: x.after })), { empty: "Nothing added" })}</div></details>
      <details class="sec"><summary><b>Unified diff</b></summary><div><pre>${esc(cfg.unified || "(identical)")}</pre></div></details>` : ""}`);
  $("#dmSbs", f).onclick = () => { $("#modal").close(); go("snapshots", { tid, a: pre, b: post }); };
}

// ---------------------------------------------------------- firmware store
async function pFirmware() {
  const c = page("Firmware Store", "Local FortiOS images, tagged by version and hardware", `<button id="fold">${icon("folder")}Open folder</button>`);
  c.innerHTML = `<div class="panel"><div class="toolbar">
      <label class="btn primary" style="cursor:pointer">${icon("upload")}Upload<input type="file" id="up" multiple accept=".out" hidden></label>
      ${S.meta.native_picker ? `<button id="pick">${icon("import")}Import from disk</button>` : ""}
      <button id="edit" disabled>${icon("edit")}Edit tags</button><button id="del" disabled>${icon("trash")}Delete</button>
      <span class="grow"></span><select id="fv"><option value="">All versions</option></select><select id="fh"><option value="">All hardware</option></select><input id="q" placeholder="Search"></div>
    <div id="upj"></div><div id="tbl"></div></div>
    <div class="hint">Version and Hardware tags are read from Fortinet file names such as <code>FGT_60F-v7.4.8.F-build2795-FORTINET.out</code>. Upgrades from the store pick the image whose tags match the device's model and the version of each path step.</div>`;
  $("#fold").onclick = () => api("/open-folder", { body: { kind: "firmware" } });
  let sel = null;
  const load = async () => { S.images = await api("/images");
    const vs = [...new Set(S.images.map((i) => i.version).filter(Boolean))].sort((a, b) => vcmp(b, a)), hs = [...new Set(S.images.flatMap((i) => i.hardware))].sort();
    const fv = $("#fv").value, fh = $("#fh").value;
    $("#fv").innerHTML = '<option value="">All versions</option>' + vs.map((v) => `<option ${v === fv ? "selected" : ""}>${esc(v)}</option>`).join("");
    $("#fh").innerHTML = '<option value="">All hardware</option>' + hs.map((h) => `<option ${h === fh ? "selected" : ""}>${esc(h)}</option>`).join(""); draw(); };
  const draw = () => {
    const q = $("#q").value.toLowerCase(), fv = $("#fv").value, fh = $("#fh").value;
    const rows = S.images.filter((i) => (!fv || i.version === fv) && (!fh || i.hardware.includes(fh)) && (!q || JSON.stringify(i).toLowerCase().includes(q)));
    $("#tbl").innerHTML = rows.length ? `<table><thead><tr><th>File name</th><th>Version</th><th>Build</th><th>Hardware</th><th>Tags</th><th>Size</th><th>Added</th><th>SHA-256</th></tr></thead><tbody>${rows.map((i) => `<tr class="click ${sel === i.id ? "sel" : ""}" data-id="${i.id}">
      <td><b>${esc(i.filename)}</b>${i.notes ? `<div class="hint">${esc(i.notes)}</div>` : ""}</td><td>${i.version ? `<span class="chip ver">v${esc(i.version)}</span>` : badge("untagged", "warn")}</td><td>${esc(i.build ?? "—")}</td>
      <td>${i.hardware.length ? i.hardware.map((h) => `<span class="chip hw">${esc(h)}</span>`).join("") : badge("untagged", "warn")}${i.product && !/^(FortiGate|FortiWiFi)/.test(i.product) ? ` <span class="badge b-warn" title="This image is for a different Fortinet product and cannot be installed on a FortiGate">${esc(i.product)}</span>` : ""}</td><td>${i.tags.map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</td>
      <td class="num">${fmtSize(i.size)}</td><td>${esc(i.added.replace("T", " "))}</td><td><code title="${esc(i.sha256)}">${esc(i.sha256.slice(0, 12))}…</code></td></tr>`).join("")}</tbody></table>`
      : '<div class="empty">No images. Upload FortiOS <code>.out</code> files from support.fortinet.com.</div>';
    $$("#tbl tr[data-id]").forEach((tr) => { tr.onclick = () => { sel = tr.dataset.id; $("#edit").disabled = $("#del").disabled = false; draw(); }; tr.ondblclick = () => editImg(sel); });
  };
  const editImg = (id) => { const i = S.images.find((x) => x.id === id);
    const f = modal(`Edit ${i.filename}`, `<div class="form"><label>Version</label><input id="v" value="${esc(i.version)}"><label>Build</label><input id="b" value="${esc(i.build ?? "")}">
      <label>Hardware</label><input id="h" value="${esc(i.hardware.join(", "))}" placeholder="FGT_60F, FGT_61F"><label>Tags</label><input id="t" value="${esc(i.tags.join(", "))}" placeholder="approved, lab"><label>Notes</label><input id="n" value="${esc(i.notes || "")}"></div>
      <p class="hint">Hardware tags are compared with the device model ignoring underscores and dashes (FGT_60F matches FGT60F). Add several for images shared across models.</p>`,
    '<button value="cancel">Cancel</button><button type="button" class="primary" id="ok">OK</button>');
    $("#ok", f).onclick = async () => { try { await api(`/images/${id}`, { method: "PATCH", body: { version: $("#v", f).value, build: $("#b", f).value, hardware: $("#h", f).value.split(","), tags: $("#t", f).value.split(","), notes: $("#n", f).value } }); $("#modal").close(); load(); } catch (e) { toast(e.message); } };
  };
  $("#edit").onclick = () => sel && editImg(sel);
  $("#del").onclick = async () => { if (sel && confirm("Delete this image from the store?")) { await api(`/images/${sel}`, { method: "DELETE" }); sel = null; load(); } };
  ["fv", "fh"].forEach((x) => $("#" + x).onchange = draw); $("#q").oninput = draw;
  $("#up").onchange = async (e) => { for (const file of e.target.files) { $("#upj").innerHTML = `<div class="banner info" style="margin:8px"><span class="spin"></span> Uploading ${esc(file.name)} (${fmtSize(file.size)})…</div>`;
    const fd = new FormData(); fd.append("file", file); try { await api("/images", { body: fd, method: "POST" }); } catch (err) { toast(err.message); } } $("#upj").innerHTML = ""; e.target.value = ""; load(); };
  if ($("#pick")) $("#pick").onclick = async () => { try { const paths = await api("/pick-files", { method: "POST" }); if (paths.length) { $("#upj").innerHTML = '<div class="banner info" style="margin:8px"><span class="spin"></span> Importing…</div>'; await api("/images/import", { body: { paths } }); $("#upj").innerHTML = ""; load(); } } catch (err) { toast(err.message); } };
  load();
}

// --------------------------------------------------------- release notes
async function pNotes() {
  const c = page("Release Notes", "Used by pre-flight to flag issues for features in your configuration");
  c.innerHTML = `<div class="panel"><div class="ph">Add release notes</div><div class="pb"><div class="row"><input type="file" id="nf" accept=".pdf,.txt,.html,.htm"><input id="nu" placeholder="…or a URL to a release notes page / PDF" style="flex:1"><input id="nn" placeholder="Name, e.g. 7.4.8" style="width:140px"><button class="primary" id="na">${icon("plus")}Add</button></div>
    <p class="hint">Load the release notes for every version on your upgrade path (docs.fortinet.com › FortiGate › FortiOS › version › Release Notes › PDF). Pre-flight scans Special notices, Changes in default behaviour and CLI, Upgrade information and Known issues.</p></div></div>
    <div class="panel"><div class="ph">Loaded</div><div class="pb flush" id="nl"></div></div>`;
  const draw = async () => { const ns = await api("/notes");
    $("#nl").innerHTML = ns.length ? `<table><tbody>${ns.map((n) => `<tr><td><b>${esc(n.name)}</b></td><td class="num">${(n.chars / 1000).toFixed(0)}k characters</td><td style="width:90px"><button class="small" data-n="${esc(n.name)}">${icon("trash")}Remove</button></td></tr>`).join("")}</tbody></table>` : '<div class="empty">None loaded</div>';
    $$("[data-n]").forEach((b) => b.onclick = async () => { await api(`/notes/${encodeURIComponent(b.dataset.n)}`, { method: "DELETE" }); draw(); }); };
  $("#na").onclick = async () => { const fd = new FormData(); fd.append("name", $("#nn").value); fd.append("url", $("#nu").value); if ($("#nf").files[0]) fd.append("file", $("#nf").files[0]);
    try { await api("/notes", { body: fd, method: "POST" }); $("#nf").value = $("#nu").value = $("#nn").value = ""; draw(); } catch (e) { toast(e.message); } };
  draw();
}

// ------------------------------------------------------------------- jobs
async function pJobs() {
  const c = page("Jobs", "Upgrades, pre-flights and data collection", `<button id="jr">${icon("refresh")}Refresh</button>`);
  $("#jr").onclick = () => pJobs();
  c.innerHTML = `<div class="panel"><div class="ph"><span class="ttl">Devices in progress</span><span class="tools hint" id="ipc"></span></div><div id="inprog"><div class="empty"><span class="spin"></span></div></div></div>
    <div class="panel"><div class="ph"><span class="ttl">History</span><span class="tools"><div class="seg" id="kf"><button class="on" data-k="">All</button><button data-k="batch">Upgrades</button><button data-k="preflight">Pre-flights</button><button data-k="snapshot">Data collections</button></div></span></div><div class="pb flush" id="hist"></div></div>
    <div id="jd"></div>`;
  const devName = (id) => S.targets.find((t) => t.id === id)?.name || id;
  const prog = async () => {
    let act; try { act = await api("/jobs?active=true&children=true"); } catch { return; }
    const ups = act.filter((j) => j.kind === "upgrade" || (j.kind !== "batch" && j.target_id));
    const gates = act.filter((j) => j.kind === "batch" && j.status === "waiting");
    $("#ipc").textContent = `${ups.length} device job(s) running`;
    $("#inprog").innerHTML = (gates.length ? gates.map((g) => `<div class="gate"><b>${esc(g.title)}</b><div class="hint">Waiting for approval</div><div class="row" style="margin-top:6px"><button class="small primary" data-open="${g.id}">Open to approve</button></div></div>`).join("") : "") +
      (ups.length ? `<div class="devcards">${ups.map((k) => `<div class="devcard"><div class="dh"><b class="click" data-dev="${esc(k.target_id)}" style="cursor:pointer">${esc(devName(k.target_id))}</b><span>${k.meta?.dry_run ? badge("Simulation", "grey") + " " : ""}${statusBadge(k.status)}</span></div><div class="db">
        <div class="row" style="justify-content:space-between"><span>${esc(KIND_NAME[k.kind] || k.kind)}${k.meta?.target_version ? ` → <span class="chip ver">v${esc(k.meta.target_version)}</span>` : ""}</span><span class="hint">started ${esc(k.created.slice(11))}</span></div>
        ${k.kind === "upgrade" ? stepper(k.stage, k.status) : ""}<div><b>${esc(k.stage || "")}</b></div><div class="lastline" title="${esc(k.last || "")}">${esc(k.last || "")}</div>
        <button class="small link" data-open="${k.parent || k.id}">Open job</button></div></div>`).join("")}</div>` : '<div class="empty">No device is being upgraded or collected right now.</div>');
    $$("[data-open]", $("#inprog")).forEach((b) => b.onclick = () => openJob(b.dataset.open));
    $$("[data-dev]", $("#inprog")).forEach((b) => b.onclick = () => go("device", [b.dataset.dev, "jobs"]));
  };
  let kind = "", selId = null, lastSig = "";
  const hist = async (force) => {
    const js = await api(`/jobs${kind ? "?kind=" + kind : ""}`);
    const sig = JSON.stringify(js.map((j) => [j.id, j.status, j.stage]));
    if (!force && sig === lastSig) return;
    lastSig = sig;
    $("#hist").innerHTML = js.length ? `<table><thead><tr><th>Started</th><th>Job</th><th>Devices</th><th>Type</th><th>Status</th><th>Duration</th></tr></thead><tbody>${js.slice(0, 200).map((j) => `<tr class="click" data-j="${j.id}" data-k="${j.kind}">
      <td>${esc(j.created.replace("T", " "))}</td><td><b>${esc(j.title)}</b></td><td>${esc((j.target_ids || []).map(devName).join(", "))}</td><td>${esc(KIND_NAME[j.kind] || j.kind)}${j.meta?.dry_run ? " " + badge("Simulation", "grey") : ""}</td><td>${statusBadge(j.status)}</td><td>${dur(j.created, j.finished)}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No jobs yet</div>';
    $$("[data-j]", $("#hist")).forEach((r) => { r.classList.toggle("sel", r.dataset.j === selId); r.onclick = () => { selId = r.dataset.j; $$("[data-j]", $("#hist")).forEach((x) => x.classList.toggle("sel", x === r)); openJob(r.dataset.j, r.dataset.k); }; });
  };
  const openJob = async (id, k) => {
    const el = $("#jd");
    k = k || (await api(`/jobs/${id}`)).kind;
    el.innerHTML = '<div class="panel" id="jdp"></div>';
    watchJob(id, $("#jdp"), { batch: k === "batch", onDone: k === "preflight" ? (job) => job.result && renderPreflights(job.result, $("#jdp")) : null });
    el.scrollIntoView({ behavior: "smooth" });
  };
  $$("#kf button").forEach((b) => b.onclick = () => { kind = b.dataset.k; $$("#kf button").forEach((x) => x.classList.toggle("on", x === b)); hist(true); });
  prog(); hist(true);
  S.pollers.prog = setInterval(() => { prog(); hist(); }, 2500);
}

async function pSettings() {
  const c = page("Settings", "System");
  const draw = (st) => {
    const kc = st.keychain_name;
    c.innerHTML = `<div class="panel"><div class="ph"><span class="ttl">${icon("key")} Credential storage</span></div><div class="pb">
      <p style="margin-top:0">API tokens and SSH passwords are kept in one encrypted file in the data folder. Choose where its key is kept:</p>
      <label class="cb" style="display:flex;align-items:flex-start;margin:10px 0"><input type="radio" name="ss" value="keychain" ${st.secret_store === "keychain" ? "checked" : ""} ${st.keychain_available ? "" : "disabled"}>
        <span><b>${kc}</b> (recommended)<br><span class="hint">One ${kc} entry, "FortiGate Upgrader / vault-key", read once each time the app starts. macOS asks for access once per app build; click <b>Always Allow</b>.</span></span></label>
      <label class="cb" style="display:flex;align-items:flex-start;margin:10px 0"><input type="radio" name="ss" value="file" ${st.secret_store === "file" ? "checked" : ""}>
        <span><b>Local key file</b><br><span class="hint">No ${kc} prompts at all. The key is saved as <code>vault.key</code> next to the vault, readable only by your user account, so anyone who can read your user's files can read the credentials.</span></span></label>
      <div class="row" style="margin-top:12px"><button class="primary" id="ssSave">Apply</button><span class="hint" id="ssMsg"></span></div></div></div>
      <div id="updSet"></div>
      <div class="panel"><div class="ph"><span class="ttl">Data folder</span><span class="tools"><button class="small" id="fold">${icon("folder")}Open</button></span></div><div class="pb mono">${esc(st.data_dir)}</div></div>`;
    $("#ssSave").onclick = async () => {
      const v = $("[name=ss]:checked")?.value;
      try { const r = await api("/settings", { method: "PUT", body: { secret_store: v } }); draw(r); $("#ssMsg").textContent = "Saved."; } catch (e) { $("#ssMsg").textContent = e.message; }
    };
    $("#fold").onclick = () => api("/open-folder", { body: { kind: "data" } }).catch((e) => toast(e.message));
    updSettingsPanel($("#updSet"));
  };
  draw(await api("/settings"));
}

boot().catch((e) => { $("#main").innerHTML = `<div class="content"><div class="banner crit">Failed to start: ${esc(e.message)}. Open the app from its launcher (the address needs its access token).</div></div>`; });


// ------------------------------------------------------------ System > Change log
const CHANGE_KIND = { new: ["New", "up"], improved: ["Improved", "info"], changed: ["Changed", "warn"], fixed: ["Fixed", "grey"] };
async function pChangelog() {
  const c = page("Change log", `System · installed: FortiGate Upgrader v${S.meta.version} build${String(S.meta.build).padStart(4, "0")}`);
  c.innerHTML = '<div class="empty"><span class="spin"></span> Loading…</div>';
  const log = await api("/changelog");
  c.innerHTML = log.map((r, i) => `<div class="panel cl-rel"><div class="ph"><span class="ttl"><span class="chip ver">v${esc(r.version)}</span> <span class="hint mono">build${String(r.build).padStart(4, "0")}</span> ${esc(r.title)}${i === 0 ? " " + badge("Installed", "up") : ""}</span><span class="tools hint">${esc(r.date)}</span></div>
    <div class="pb"><ul class="cl-list">${r.changes.map((x) => { const [n, k] = CHANGE_KIND[x.type] || [x.type, "grey"]; return `<li>${badge(n, k)}<span>${esc(x.text)}</span></li>`; }).join("")}</ul></div></div>`).join("");
}
