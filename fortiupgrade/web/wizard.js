"use strict";
/* Upgrade wizard (installer style) and the live terminal console. */

// ------------------------------------------------------------ terminal console
function makeConsole(el, opts = {}) {
  const st = { lines: [], verbose: opts.verbose ?? true, follow: true, dev: "" };
  el.innerHTML = `<div class="term">
    <div class="term-bar"><span class="term-dots"><i></i><i></i><i></i></span><b>${esc(opts.title || "Activity")}</b><span class="grow"></span>
      <select data-dev><option value="">All devices</option></select>
      <label><input type="checkbox" data-verb ${st.verbose ? "checked" : ""}> API &amp; SSH calls</label>
      <label><input type="checkbox" data-follow checked> Follow</label>
      <button class="small" data-copy>Copy</button><button class="small" data-dl>Download</button></div>
    <div class="term-body" data-body></div></div>`;
  const body = $("[data-body]", el), sel = $("[data-dev]", el);
  const devs = new Set();
  const cls = (l) => ({ ok: "t-ok", warn: "t-warn", error: "t-err", stage: "t-stage", gate: "t-gate", detail: "t-detail", debug: "t-debug" }[l.level] || "");
  const visible = (l) => (st.verbose || (l.level !== "detail" && l.level !== "debug")) && (!st.dev || l.src === st.dev);
  const line = (l) => {
    if (l.level === "stage") return `<div class="t-stage"><span class="t-time">${esc(l.t)}</span> <span class="t-src">${esc(l.src || "")}</span> ━━ ${esc(l.msg.replace(/^— | —$/g, ""))} ━━</div>`;
    const sym = { ok: "✔", warn: "▲", error: "✖", gate: "◆", detail: "·" }[l.level] || "›";
    const msg = l.src && l.msg.startsWith(l.src + ": ") ? l.msg.slice(l.src.length + 2) : l.msg;
    return `<div class="${cls(l)}"><span class="t-time">${esc(l.t)}</span> <span class="t-src">${esc((l.src || "").padEnd(12).slice(0, 18))}</span> <span class="t-sym">${sym}</span> ${esc(msg)}</div>`;
  };
  const redraw = () => { body.innerHTML = st.lines.filter(visible).map(line).join(""); if (st.follow) body.scrollTop = body.scrollHeight; };
  const api_ = {
    add(entries) {
      if (!entries || !entries.length) return;
      const html = [];
      for (const l of entries) {
        st.lines.push(l);
        if (l.src && !devs.has(l.src)) { devs.add(l.src); sel.insertAdjacentHTML("beforeend", `<option>${esc(l.src)}</option>`); }
        if (visible(l)) html.push(line(l));
      }
      if (st.lines.length > 20000) { st.lines = st.lines.slice(-15000); return redraw(); }
      body.insertAdjacentHTML("beforeend", html.join(""));
      if (st.follow) body.scrollTop = body.scrollHeight;
    },
    text() { return st.lines.map((l) => `${l.t} [${l.level}] ${l.src || ""}: ${l.msg}`).join("\n"); },
  };
  $("[data-verb]", el).onchange = (e) => { st.verbose = e.target.checked; redraw(); };
  $("[data-follow]", el).onchange = (e) => { st.follow = e.target.checked; if (st.follow) body.scrollTop = body.scrollHeight; };
  sel.onchange = () => { st.dev = sel.value; redraw(); };
  body.addEventListener("wheel", () => { if (body.scrollTop + body.clientHeight < body.scrollHeight - 30) { st.follow = false; $("[data-follow]", el).checked = false; } });
  $("[data-copy]", el).onclick = () => navigator.clipboard?.writeText(api_.text());
  $("[data-dl]", el).onclick = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([api_.text()], { type: "text/plain" })); a.download = `${(opts.file || "upgrade").replace(/[^\w.-]+/g, "_")}.log`; a.click(); };
  return api_;
}

// poll a job, stream its log into a console, call onTick(job) each time
function streamJob(id, con, onTick) {
  let since = 0, stopped = false;
  const key = "stream-" + id;
  const tick = async () => {
    if (stopped) return;
    let j; try { j = await api(`/jobs/${id}?since=${since}`); } catch { return; }
    con.add(j.log); since = j.log_len;
    onTick && onTick(j);
    if (["done", "failed", "cancelled", "interrupted"].includes(j.status) && !(j.children_detail || []).some((k) => ["running", "waiting"].includes(k.status))) {
      stopped = true; clearInterval(S.pollers[key]); delete S.pollers[key];
    }
  };
  S.pollers[key] = setInterval(tick, 1000);
  tick();
  return { stop() { stopped = true; clearInterval(S.pollers[key]); } };
}

// ------------------------------------------------------------------- wizard
const WSTEPS = [["devices", "Devices"], ["image", "Target image"], ["method", "Install method"], ["options", "Options"], ["health", "Config & log review"], ["review", "Pre-flight & review"], ["install", "Install"]];
function newWizard() {
  return { step: 0, sel: new Set(S.upSel || []), version: "", build: null, maturity: null, source: "fortiguard", path: "fortinet",
    fw: {}, ha: { fgcp: "uninterruptible", fgsp: "uninterruptible" }, units: {}, checks: true, pause: false, restore: true,
    unverified: false, wait: 1800, par: 4, checkIds: null, pf: null, pfJob: null, fmgAck: false, critAck: false, job: null, dry: false, rv: null, rvJob: null, rvAck: false };
}
async function pUpgrade() {
  if (!S.wiz || S.wiz.done) S.wiz = newWizard();
  if (S.upSel?.size) { S.wiz.sel = new Set(S.upSel); S.upSel = new Set(); }
  const W = S.wiz;
  if (W.checkIds === null) W.checkIds = S.meta.checks.filter((k) => k.default).map((k) => k.id);
  const c = page("Firmware Upgrade", "Install FortiOS on one or more FortiGates");
  c.innerHTML = '<div class="empty"><span class="spin"></span> Reading devices…</div>';
  [S.images] = await Promise.all([api("/images"), loadInv()]);
  drawWizard(c);
}
const wSel = () => S.targets.filter((t) => S.wiz.sel.has(t.id));
const methodName = (src) => src === "fortiguard" ? "FortiGuard download" : "Push local image";
function wImageFor(t) {
  const m = S.wiz.match && S.wiz.match.version === S.wiz.version ? S.wiz.match.res[t.id] : null;
  return m ? m.image : null;
}
async function wLoadMatch(ts) {
  const W = S.wiz;
  W.match = { version: W.version, res: await api(`/images/match?version=${encodeURIComponent(W.version)}&target_ids=${ts.map((t) => t.id).join(",")}`) };
}
function drawWizard(c) {
  const W = S.wiz;
  const running = W.job && W.step === 6;
  const target = W.version ? `FortiOS v${esc(W.version)}${W.build ? ` <span class="hint">build ${esc(W.build)}</span>` : ""}${W.maturity ? ` ${W.maturity === "M" ? badge("Mature", "up") : badge("Feature", "grey")}` : ""}` : '<span class="muted">not chosen</span>';
  const method = W.source === "fortiguard" ? `${icon("cloud")} FortiGuard download` : `${icon("upload")} Push local image`;
  c.innerHTML = `<div class="wiz">
    <aside class="wiz-steps">${WSTEPS.map(([k, n], i) => `<div class="ws ${i < W.step ? "done" : i === W.step ? "cur" : ""}"><span class="wn">${i < W.step ? "✓" : i + 1}</span>${esc(n)}</div>`).join("")}</aside>
    <section class="wiz-main">
      <div class="wiz-summary">
        <div><span class="l">Target image</span><span class="v">${target}</span></div>
        <div><span class="l">Install method</span><span class="v">${W.step >= 2 || W.source !== "fortiguard" ? method : '<span class="muted">not chosen</span>'}</span></div>
        <div><span class="l">Devices</span><span class="v">${W.sel.size ? `${W.sel.size} selected` : '<span class="muted">none</span>'}</span></div>
        ${(W.probes || []).length && W.step >= 3 ? `<div><span class="l">Service probes</span><span class="v">${icon("pulse")} ${W.probes.length} probe${W.probes.length > 1 ? "s" : ""}</span></div>` : ""}
        ${W.dry ? `<div><span class="l">Mode</span><span class="v">${badge("Simulation", "grey")}</span></div>` : ""}
      </div>
      <div class="wiz-body" id="wb"></div>
      <div class="wiz-foot" id="wf"></div>
    </section></div>`;
  const body = $("#wb"), foot = $("#wf");
  const nav = (back, next, nextLabel = "Next", extra = "") => {
    foot.innerHTML = `${back ? `<button id="wBack">‹ Back</button>` : ""}<span class="grow"></span>${extra}${next ? `<button class="primary" id="wNext">${nextLabel} ›</button>` : ""}`;
    if (back) $("#wBack").onclick = () => { W.step--; drawWizard(c); };
  };
  const go_ = (n) => { W.step = n; drawWizard(c); };
  ({ 0: wDevices, 1: wImage, 2: wMethod, 3: wOptions, 4: wHealth, 5: wReview, 6: wInstall }[W.step])(W, body, foot, nav, go_, c);
  void running;
}

function wDevices(W, body, foot, nav, go_) {
  body.innerHTML = `<h2>Which FortiGates do you want to upgrade?</h2><p class="hint">Select one or more devices. They are upgraded at the same time; HA clusters follow the HA mode you choose later.</p>
    <div class="panel"><div class="pb flush">${S.targets.length ? `<table><thead><tr><th style="width:30px"></th><th>Device</th><th>Type</th><th>Model</th><th>Running</th><th>Management</th><th>Status</th></tr></thead><tbody>${S.targets.map((t) => { const i = S.inv[t.id] || {};
      return `<tr class="click ${W.sel.has(t.id) ? "sel" : ""} ${i.ok ? "" : "dim"}" data-id="${t.id}"><td><input type="checkbox" ${W.sel.has(t.id) ? "checked" : ""} ${i.ok ? "" : "disabled"}></td><td><b>${esc(t.name)}</b>${i.hostname && i.hostname !== t.name ? ` <span class="hint">${esc(i.hostname)}</span>` : ""}</td><td>${typeName(effType(t))}${typeMismatch(t) ? ` <span class="hint">· detected</span>` : ""}</td><td>${esc(i.model || "")}</td>
      <td>${i.version ? `<span class="chip ver">v${esc(i.version)}</span> <span class="hint">b${esc(i.build)}</span>` : ""}</td><td>${i.management?.managed ? badge("FortiManager", "warn") : i.ok ? '<span class="hint">Local</span>' : ""}</td><td>${i.ok ? val("Up") : badge("Unreachable", "down")}</td></tr>`; }).join("")}</tbody></table>` : '<div class="empty">No devices yet. Add them under Devices.</div>'}</div></div>`;
  nav(false, true);
  $$("tr[data-id]", body).forEach((tr) => tr.onclick = () => { if (!S.inv[tr.dataset.id]?.ok) return; W.sel.has(tr.dataset.id) ? W.sel.delete(tr.dataset.id) : W.sel.add(tr.dataset.id); W.pf = null; drawWizard($("#pc")); });
  $("#wNext").disabled = !W.sel.size;
  $("#wNext").onclick = () => go_(1);
}

async function wImage(W, body, foot, nav, go_) {
  nav(true, true);
  $("#wNext").disabled = true;
  body.innerHTML = '<h2>Which FortiOS version should be installed?</h2><div class="empty"><span class="spin"></span> Asking FortiGuard which images each device can install…</div>';
  const ts = wSel();
  await Promise.all(ts.filter((t) => !W.fw[t.id]).map(async (t) => { try { W.fw[t.id] = await api(`/targets/${t.id}/firmware`); } catch (e) { W.fw[t.id] = { error: e.message, available: [] }; } }));
  const vmap = {};
  ts.forEach((t) => (W.fw[t.id].available || []).forEach((a) => { const v = String(a.version).replace(/^v/, ""); (vmap[v] = vmap[v] || { v, build: a.build, maturity: a.maturity, notes: a.notes, ts: new Set() }).ts.add(t.id); }));
  const lowest = ts.map((t) => S.inv[t.id]?.version).filter(Boolean).sort(vcmp)[0];
  const vers = Object.values(vmap).filter((x) => !lowest || vcmp(x.v, lowest) > 0).sort((a, b) => vcmp(b.v, a.v));
  const latestMature = vers.find((x) => x.maturity === "M");
  const offline = ts.filter((t) => W.fw[t.id].error);
  body.innerHTML = `<h2>Which FortiOS version should be installed?</h2>
    <p class="hint">Versions FortiGuard offers for the selected devices, newest first. Each device follows the supported upgrade path to the version you pick.</p>
    ${offline.length ? `<div class="banner warn">Could not read the image list from ${offline.map((t) => esc(t.name)).join(", ")}: ${esc(offline[0].error)}</div>` : ""}
    <div class="vlist">${vers.slice(0, 24).map((x) => `<label class="vcard ${W.version === x.v ? "on" : ""}"><input type="radio" name="ver" value="${esc(x.v)}" ${W.version === x.v ? "checked" : ""}>
      <div class="vt"><b>FortiOS ${esc(x.v)}</b> <span class="hint">build ${esc(x.build)}</span></div>
      <div class="vm">${x.maturity === "M" ? badge("Mature", "up") : badge("Feature", "grey")}${x === latestMature ? " " + badge("Recommended", "info") : ""}${x === vers[0] ? " " + badge("Latest", "info") : ""}
        ${x.ts.size < ts.length ? ` <span class="hint">offered to ${x.ts.size}/${ts.length} devices</span>` : ""}${x.notes ? ` <a href="${esc(x.notes)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Release notes ↗</a>` : ""}</div></label>`).join("") || '<div class="empty">FortiGuard offers no newer version for these devices. Enter one below to install from a local image.</div>'}</div>
    <div class="row" style="margin:12px 0"><span class="hint">Other version</span><input id="wOther" placeholder="e.g. 7.4.8" style="width:120px" value="${vmap[W.version] ? "" : esc(W.version)}"></div>
    <div class="panel"><div class="ph"><span class="ttl">Upgrade path</span></div><div class="pb">
      <label class="cb" style="display:flex"><input type="radio" name="wPath" value="fortinet" ${W.path === "fortinet" ? "checked" : ""}> <span><b>Follow Fortinet's supported upgrade path</b> (recommended) <span class="hint">· intermediate versions are installed automatically when required</span></span></label>
      <label class="cb" style="display:flex"><input type="radio" name="wPath" value="direct" ${W.path === "direct" ? "checked" : ""}> <span><b>Install the target version directly</b> <span class="hint">· skips the path check</span></span></label></div></div>
    <div id="wPer"></div>`;
  const per = () => {
    $("#wPer").innerHTML = W.version ? `<div class="panel"><div class="ph"><span class="ttl">What happens on each device</span></div><div class="pb flush">${table(ts.map((t) => { const i = S.inv[t.id] || {}; const cmp = vcmp(W.version, i.version);
      return { Device: t.name, Model: i.model, Running: "v" + i.version, "→": "v" + W.version, Note: cmp <= 0 ? "Already on or above this version (will be skipped)" : (W.fw[t.id].available || []).some((a) => vcmp(a.version, W.version) === 0) ? "Offered by FortiGuard" : "Not offered by FortiGuard: needs a local image" }; }))}</div></div>` : "";
    $("#wNext").disabled = !ver(W.version).length || !ts.some((t) => vcmp(W.version, S.inv[t.id]?.version) > 0);
  };
  const pick = (v) => { W.version = v; const x = vmap[v]; W.build = x?.build || null; W.maturity = x?.maturity || null; W.pf = null; };
  $$("input[name=ver]", body).forEach((r) => r.onchange = () => { pick(r.value); $("#wOther").value = ""; $$(".vcard", body).forEach((l) => l.classList.toggle("on", l.contains(r))); per(); refreshSummary(); });
  $("#wOther").oninput = (e) => { pick(e.target.value.trim().replace(/^v/, "")); $$("input[name=ver]", body).forEach((r) => { r.checked = false; r.closest(".vcard").classList.remove("on"); }); per(); refreshSummary(); };
  $$("input[name=wPath]", body).forEach((r) => r.onchange = () => { W.path = r.value; W.pf = null; });
  if (!W.version && latestMature) pick(latestMature.v);
  $$("input[name=ver]", body).forEach((r) => { if (r.value === W.version) { r.checked = true; r.closest(".vcard").classList.add("on"); } });
  per(); refreshSummary();
  $("#wNext").onclick = () => go_(2);
}
function refreshSummary() {
  const W = S.wiz, v = $(".wiz-summary .v");
  if (v) v.innerHTML = W.version ? `FortiOS v${esc(W.version)}${W.build ? ` <span class="hint">build ${esc(W.build)}</span>` : ""}${W.maturity ? ` ${W.maturity === "M" ? badge("Mature", "up") : badge("Feature", "grey")}` : ""}` : '<span class="muted">not chosen</span>';
}

function wMethod(W, body, foot, nav, go_, c) {
  const ts = wSel().filter((t) => vcmp(W.version, S.inv[t.id]?.version) > 0);
  const fgOk = ts.every((t) => (W.fw[t.id]?.available || []).some((a) => vcmp(a.version, W.version) === 0));
  const draw = async () => {
    if (W.source === "store" && (!W.match || W.match.version !== W.version || W.matchStale)) {
      body.innerHTML = '<div class="empty"><span class="spin"></span> Matching images to devices…</div>';
      try { await wLoadMatch(ts); } catch (e) { toast(e.message); }
      W.matchStale = false;
    }
    body.innerHTML = `<h2>How should FortiOS v${esc(W.version)} get onto the devices?</h2>
      <div class="mcards">
        <label class="mcard ${W.source === "fortiguard" ? "on" : ""}"><input type="radio" name="src" value="fortiguard" ${W.source === "fortiguard" ? "checked" : ""}>
          <div class="mi">${icon("cloud")}</div><div><b>Download from FortiGuard</b><p>Each FortiGate downloads the signed image directly from Fortinet's FortiGuard servers. Nothing is transferred from this computer.</p>
          <p class="hint">Needs internet access from the FortiGate and a valid FortiCare contract.</p>${fgOk ? badge("Available for all selected devices", "up") : badge("Not offered for every device", "warn")}</div></label>
        <label class="mcard ${W.source === "store" ? "on" : ""}"><input type="radio" name="src" value="store" ${W.source === "store" ? "checked" : ""}>
          <div class="mi">${icon("upload")}</div><div><b>Push a local image</b><p>This computer uploads the <code>.out</code> image file from the Firmware Store to each FortiGate over HTTPS.</p>
          <p class="hint">Use for devices without internet access, or for a specific build you have tested.</p></div></label>
      </div>
      ${W.source === "store" ? `<div class="panel"><div class="ph"><span class="ttl">Image for each device</span><span class="tools"><button class="small" id="addImg">${icon("upload")}Add image file…</button><input type="file" id="imgFile" accept=".out,.zip,.bin" hidden></span></div><div class="pb flush">
        ${table(ts.map((t) => { const m = W.match?.res?.[t.id] || {}; const im = m.image;
          return { Device: t.name, Model: m.model || S.inv[t.id]?.model, "Image file": im ? im.filename : { m }, Build: im?.build ? String(im.build) : "—", Size: im ? fmtSize(im.size) : "—" }; }),
          { fmt: { "Image file": (v) => typeof v === "string" ? `<span class="mono">${esc(v)}</span>` :
            `${badge(`No ${v.m.expected_hw || "matching"} image for v${W.version}`, "down")}
             ${v.m.expected_hw ? `<div class="hint">Needed: <span class="mono">${esc(v.m.expected_hw)}-v${esc(W.version)}.…-build…-FORTINET.out</span></div>` : ""}
             ${(v.m.near || []).map((n) => `<div class="hint" style="color:var(--warn)">▲ ${esc(n.reason)}</div>`).join("")}` } })}
        </div></div><p class="hint">If the upgrade path has intermediate versions, the Firmware Store needs an image for each of them too; pre-flight checks this.</p>` : ""}`;
    $$("input[name=src]", body).forEach((r) => r.onchange = () => { W.source = r.value; W.pf = null; drawWizard(c); });
    const add = $("#addImg");
    if (add) {
      add.onclick = () => $("#imgFile").click();
      $("#imgFile").onchange = async (e) => {
        const f = e.target.files[0]; if (!f) return;
        add.disabled = true; add.innerHTML = '<span class="spin"></span> Uploading to the store…';
        const fd = new FormData(); fd.append("file", f);
        try { await api("/images", { method: "POST", body: fd }); S.images = await api("/images"); } catch (er) { toast(er.message); }
        W.matchStale = true; draw();
      };
    }
    nav(true, true);
    const missing = W.source === "store" && ts.some((t) => !wImageFor(t));
    $("#wNext").disabled = missing;
    $("#wNext").onclick = () => go_(3);
  };
  draw();
}

function wOptions(W, body, foot, nav, go_) {
  const ts = wSel(), fgcp = ts.filter((t) => effType(t) === "fgcp"), fgsp = ts.filter((t) => effType(t) === "fgsp");
  const FG = { uninterruptible: ["Uninterruptible", "Secondaries upgrade first, then the primary fails over and upgrades. Short failover, no full outage."], simultaneous: ["Simultaneous", "All units upgrade and reboot together. Fastest, causes an outage."], "local-only": ["Local only", "Only the units you select (FortiOS 7.4.1+)."], "secondary-only": ["Secondary only", "Upgrade the secondaries; the primary stays (FortiOS 7.4.1+)."] };
  const FS = { uninterruptible: ["Rolling", "One peer at a time, with post-checks between peers."], simultaneous: ["Simultaneous", "All peers at once."], "local-only": ["Local only", "Only the peers you select."] };
  const cards = (kind, modes, names) => `<div class="mcards small">${modes.map((m) => `<label class="mcard ${W.ha[kind] === m ? "on" : ""}"><input type="radio" name="ha-${kind}" value="${m}" ${W.ha[kind] === m ? "checked" : ""}><div><b>${names[m][0]}</b><p>${names[m][1]}</p></div></label>`).join("")}</div>`;
  const haInfo = (t) => { const i = S.inv[t.id] || {}; const n = t.members.length;
    return `<tr><td><b>${esc(t.name)}</b>${typeMismatch(t) ? ` ${badge("Saved as standalone", "warn")}` : ""}</td><td>${esc(i.ha_mode === "a-a" ? "Active-active" : i.ha_mode === "a-p" ? "Active-passive" : i.fgsp ? "FGSP" : "—")}</td>
      <td><span class="mono">${esc(t.members[0]?.host || "")}</span> <span class="hint">${effType(t) === "fgcp" ? "cluster address" : "peer address"}</span></td><td>${n > 1 ? `${n - 1} unit address${n > 2 ? "es" : ""}` : '<span class="hint">none</span>'}</td>
      <td>${typeMismatch(t) ? `<button class="small" data-fixtype="${t.id}">Save as ${effType(t) === "fgcp" ? "HA cluster" : "FGSP peer"}</button>` : ""}</td></tr>`; };
  const addrHelp = `<details class="sec" style="margin-top:10px"><summary><b>Which management address does the app use?</b></summary><div class="pb" style="font-size:12.5px;line-height:1.55">
      <p style="margin-top:0"><b>The cluster address (required).</b> This is the device's first address, an interface IP that only the current primary answers on and that moves with it on failover. Every FGCP upgrade goes through it: the primary takes the image, sends it to the secondaries and runs the upgrade in the HA mode chosen above. Use the shared address, not one unit's own IP.</p>
      <p><b>Per-unit HA management addresses (optional).</b> These are each unit's dedicated management interface (<code>config system ha</code> › <code>set ha-mgmt-status enable</code> › <code>ha-mgmt-interfaces</code>). They always reach that specific unit, whichever unit is primary. You need them for <b>Local only</b> and <b>Secondary only</b>, to confirm each unit's version after its reboot, and to run checks on a secondary. Add them as extra addresses under Devices › Edit.</p>
      <p style="margin-bottom:0"><b>FGSP:</b> every peer is a separate FortiGate, so add each peer's own management address.</p>
      <p class="hint" style="margin-bottom:0">For service probes, the cluster address shows the outage users see during failover; a unit's own address shows when that unit reboots.</p></div></details>`;
  body.innerHTML = `<h2>Upgrade options</h2>
    ${fgcp.length || fgsp.length ? `<div class="panel"><div class="ph"><span class="ttl">HA devices in this upgrade</span></div><div class="pb flush"><table><thead><tr><th>Device</th><th>HA mode on the device</th><th>Upgrade via</th><th>Per-unit addresses</th><th></th></tr></thead><tbody>${[...fgcp, ...fgsp].map(haInfo).join("")}</tbody></table></div>
      ${[...fgcp, ...fgsp].some(typeMismatch) ? '<div class="pb" style="padding-top:0"><div class="banner info" style="margin:8px 0 0">A device saved as <b>Standalone</b> reports that it is in an HA cluster. It is upgraded as a cluster with the HA mode you choose below, so FortiOS does not fall back to whatever upgrade mode happens to be configured on it.</div></div>' : ""}</div>` : ""}
    ${fgcp.length ? `<div class="panel"><div class="ph"><span class="ttl">How should the FGCP cluster${fgcp.length > 1 ? "s" : ""} upgrade and fail over? ${fgcp.map((t) => esc(t.name)).join(", ")}</span></div><div class="pb">${cards("fgcp", S.meta.fgcp_modes, FG)}<div data-units="fgcp"></div>${addrHelp}</div></div>` : ""}
    ${fgsp.length ? `<div class="panel"><div class="ph"><span class="ttl">How should the FGSP peers upgrade? ${fgsp.map((t) => esc(t.name)).join(", ")}</span></div><div class="pb">${cards("fgsp", S.meta.fgsp_modes, FS)}<div data-units="fgsp"></div>${fgcp.length ? "" : addrHelp}</div></div>` : ""}
    <div class="panel"><div class="ph"><span class="ttl">Checks</span></div><div class="pb">
      <label class="cb" style="display:flex"><input type="checkbox" id="oChecks" ${W.checks ? "checked" : ""}> <span><b>Collect state before and after, then compare</b> <span class="hint">· interfaces, routing, neighbours, VPNs, logs and the configuration</span></span></label>
      <label class="cb" style="display:flex"><input type="checkbox" id="oPause" ${W.pause ? "checked" : ""}> <span><b>Pause for my approval after the pre-upgrade checks</b> <span class="hint">· otherwise installation starts as soon as the checks pass</span></span></label>
      <details class="sec"><summary>Advanced</summary><div style="padding:10px 12px"><div class="grid">
        <label class="cb"><input type="checkbox" id="oRestore" ${W.restore ? "checked" : ""}> Restore the HA upgrade setting afterwards</label>
        <label class="cb"><input type="checkbox" id="oUnver" ${W.unverified ? "checked" : ""}> Allow an unverified upgrade path</label>
        <div class="row">Reboot timeout <input id="oWait" type="number" value="${W.wait}" style="width:80px"> s</div>
        <div class="row">Parallel uploads <input id="oPar" type="number" min="1" value="${W.par}" style="width:60px"></div></div>
        <div class="hint" style="margin:10px 0 4px">Checks to run</div><div class="grid">${S.meta.checks.map((k) => `<label class="cb" title="${esc(k.description)}"><input type="checkbox" data-chk="${k.id}" ${W.checkIds.includes(k.id) ? "checked" : ""}> ${esc(k.name)}</label>`).join("")}</div></div></details>
    </div></div><div id="wProbes"></div>`;
  probeEditor(W, $("#wProbes", body));
  const units = () => ["fgcp", "fgsp"].forEach((k) => { const el = $(`[data-units=${k}]`, body); if (!el) return;
    const mode = W.ha[k], list = k === "fgcp" ? fgcp : fgsp;
    const needUnits = k === "fgcp" && ["local-only", "secondary-only"].includes(mode) && list.filter((t) => t.members.length < 2);
    el.innerHTML = (needUnits && needUnits.length ? `<div class="banner warn" style="margin-top:10px">${esc(needUnits.map((t) => t.name).join(", "))}: ${mode === "local-only" ? "Local only upgrades the units you pick, which needs each unit's own HA management address." : "Secondary only can't confirm the secondaries' version without their HA management addresses; the app will only wait for the cluster to settle."} Add them under Devices › Edit (see below).</div>` : "") +
      (mode === "local-only" ? list.map((t) => `<div style="margin-top:8px"><b>${esc(t.name)}:</b> ${t.members.map((m, n) => `<label class="cb"><input type="checkbox" data-unit="${t.id}:${m.id}" ${(W.units[t.id] || []).includes(m.id) ? "checked" : ""}> ${esc(m.name || m.host)} <span class="hint">${n === 0 ? "(cluster address)" : "(unit address)"}</span></label>`).join("")}</div>`).join("") : "");
    $$("[data-unit]", el).forEach((x) => x.onchange = () => { const [t, m] = x.dataset.unit.split(":"); const a = new Set(W.units[t] || []); x.checked ? a.add(m) : a.delete(m); W.units[t] = [...a]; }); });
  ["fgcp", "fgsp"].forEach((k) => $$(`input[name=ha-${k}]`, body).forEach((r) => r.onchange = () => { W.ha[k] = r.value; $$(`input[name=ha-${k}]`, body).forEach((x) => x.closest(".mcard").classList.toggle("on", x.checked)); units(); W.pf = null; }));
  units();
  $$("[data-fixtype]", body).forEach((b) => b.onclick = async () => {
    const t = S.targets.find((x) => x.id === b.dataset.fixtype);
    try { await api("/targets", { body: { ...t, type: effType(t) } }); await loadTargets(); toast(`${t.name} is now saved as ${typeName(effType(S.targets.find((x) => x.id === t.id)) || "fgcp")}.`); drawWizard($("#pc")); } catch (e) { toast(e.message); }
  });
  nav(true, true);
  $("#wNext").onclick = () => {
    W.checks = $("#oChecks").checked; W.pause = $("#oPause").checked; W.restore = $("#oRestore").checked; W.unverified = $("#oUnver").checked;
    W.wait = Number($("#oWait").value) || 1800; W.par = Number($("#oPar").value) || 4; W.checkIds = $$("[data-chk]", body).filter((x) => x.checked).map((x) => x.dataset.chk);
    const lo = [...(W.ha.fgcp === "local-only" ? fgcp : []), ...(W.ha.fgsp === "local-only" ? fgsp : [])];
    if (lo.some((t) => !(W.units[t.id] || []).length)) return toast("Select the units to upgrade for local-only mode.");
    const pe = probeValidate(W); if (pe) return toast(pe);
    if (W.rv && W.rvKey !== rvKey(W)) { W.rv = null; W.rvAck = false; }
    go_(4);
  };
}

function wBody(W, dry) {
  return { target_ids: [...W.sel], target_version: W.version, source: W.source, direct: W.path === "direct", ha_modes: { fgcp: W.ha.fgcp, fgsp: W.ha.fgsp },
    member_ids: W.units, dry_run: dry, run_checks: W.checks, pause_after_prechecks: W.pause, stop_on_critical: true, approve_between_hops: false,
    approve_between_members: W.pause, restore_ha_setting: W.restore, allow_unverified_path: W.unverified, wait_timeout: W.wait, max_parallel_uploads: W.par,
    check_ids: W.checkIds, fmg_acknowledged: W.fmgAck, preflight_job: W.pfJob, review_job: W.rvJob,
    probes: probeSpecs(W), probe_baseline_s: W.probeOpts?.baseline ?? 15, probe_settle_s: W.probeOpts?.settle ?? 60,
    probe_min_outage_ms: W.probeOpts?.minOutage ?? 1000 };
}

function wReview(W, body, foot, nav, go_, c) {
  const ts = wSel();
  if (W.source === "store" && (!W.match || W.match.version !== W.version)) { wLoadMatch(ts).then(() => drawWizard(c)); return; }
  if (!W.pf) {
    body.innerHTML = `<h2>Pre-flight checks</h2><p class="hint">Read-only: connectivity, HA state, resources, upgrade path, image availability, FortiManager and the configuration against the release notes.</p><div id="pfc"></div>`;
    nav(true, true, "Running…");
    $("#wNext").disabled = true;
    const con = makeConsole($("#pfc"), { title: "Pre-flight", file: "preflight" });
    api("/preflight", { body: { target_ids: [...W.sel], target_version: W.version } }).then((j) => {
      W.pfJob = j.id;
      streamJob(j.id, con, (job) => { if (job.status !== "running" && job.result) { W.pf = job.result; W.pfLog = con.text(); drawWizard(c); } else if (job.status === "failed") { body.insertAdjacentHTML("beforeend", `<div class="banner crit">${esc(job.error)}</div>`); } });
    }).catch((e) => body.insertAdjacentHTML("beforeend", `<div class="banner crit">${esc(e.message)}</div>`));
    return;
  }
  const res = W.pf.results || [];
  const byT = Object.fromEntries(res.map((r) => [r.target_id, r]));
  const crit = res.filter((r) => !r.result || r.result.findings.some((f) => f.severity === "critical"));
  const fmgT = ts.filter((t) => S.inv[t.id]?.management?.managed);
  const rows = ts.map((t) => { const r = byT[t.id]?.result; const p = r?.plan || { steps: [] }; const f = r?.findings || [];
    const n = (s) => f.filter((x) => x.severity === s).length;
    const img = W.source === "store" ? (wImageFor(t)?.filename || "missing") : `FortiGuard${W.build ? " build " + W.build : ""}`;
    return `<tr><td><b>${esc(t.name)}</b><div class="hint">${esc(r?.members?.[0]?.model || S.inv[t.id]?.model || "")}</div></td>
      <td><div class="path">${p.steps.map((s, k) => `${k ? '<span class="arrow">→</span>' : ""}<span class="v">${esc(s)}</span>`).join("") || "—"}</div>${p.steps.length ? (p.verified ? " " + badge("Supported path", "up") : " " + badge("Unverified path", "warn")) : ""}</td>
      <td><span class="mono">${esc(img)}</span></td><td>${effType(t) === "fgcp" ? esc(W.ha.fgcp) : effType(t) === "fgsp" ? esc(W.ha.fgsp) : "—"}</td>
      <td>${!r ? badge(byT[t.id]?.error || "Failed", "down") : `${n("critical") ? badge(n("critical") + " critical", "down") + " " : ""}${n("warning") ? badge(n("warning") + " warning", "warn") + " " : ""}${!n("critical") && !n("warning") ? badge("Ready", "up") : ""}`}</td></tr>`; }).join("");
  body.innerHTML = `<h2>Review and start</h2>
    <div class="panel"><div class="pb flush"><table><thead><tr><th>Device</th><th>Upgrade path</th><th>Image</th><th>HA mode</th><th>Pre-flight</th></tr></thead><tbody>${rows}</tbody></table></div></div>
    <details class="sec"><summary><b>Pre-flight details</b><span class="hint">findings, image availability and release-note analysis</span></summary><div id="pfd" style="padding:0 8px 8px"></div></details>
    ${fmgT.length ? fmgBanner(fmgT).replace("</ul></div>", `</ul><label class="cb"><input type="checkbox" id="fmgAck" ${W.fmgAck ? "checked" : ""}> <b>Upgrade these FortiManager-managed devices from this app anyway</b></label></div>`) : ""}
    ${crit.length ? `<div class="banner crit"><b>Pre-flight found critical issues on ${crit.map((r) => esc(r.target)).join(", ")}:</b>
      <ul>${crit.flatMap((r) => r.result ? r.result.findings.filter((f) => f.severity === "critical").map((f) => `<li><b>${esc(r.target)}:</b> ${esc(f.message)}</li>`) : [`<li><b>${esc(r.target)}:</b> ${esc(r.error || "pre-flight failed")}</li>`]).join("")}</ul> <label class="cb"><input type="checkbox" id="critAck" ${W.critAck ? "checked" : ""}> Continue anyway</label></div>` : ""}
    <div class="banner warn"><b>Ready to install FortiOS v${esc(W.version)} on ${ts.length} device${ts.length > 1 ? "s" : ""} ${W.source === "fortiguard" ? "downloaded by each device from FortiGuard" : "pushed from this computer"}.</b>
      Each device will reboot, interrupting traffic it handles${ts.some((t) => effType(t) !== "standalone") ? " (HA clusters follow the selected HA mode)" : ""}. ${W.checks ? "State is collected before and after so you can compare." : ""}
      ${probeSpecs(W).length ? `${probeSpecs(W).length} service probe(s) will measure the outage window from this computer.` : ""}
      <b>Simulate</b> runs every step without changing the devices.</div>`;
  renderPreflights(W.pf, $("#pfd"));
  nav(true, true, "Start upgrade", `<button id="wSim">${icon("play")}Simulate</button>`);
  const nb = $("#wNext"); nb.classList.remove("primary"); nb.classList.add("danger");
  const gate = () => { const ok = (!fmgT.length || W.fmgAck) && (!crit.length || W.critAck); nb.disabled = !ok; $("#wSim").disabled = !((!fmgT.length || W.fmgAck)); };
  const fa = $("#fmgAck"); if (fa) fa.onchange = () => { W.fmgAck = fa.checked; gate(); };
  const ca = $("#critAck"); if (ca) ca.onchange = () => { W.critAck = ca.checked; gate(); };
  gate();
  const start = async (dry) => { W.dry = dry; try { const j = await api("/upgrade", { body: wBody(W, dry) }); W.job = j.id; go_(6); } catch (e) { toast(e.message); } };
  nb.onclick = () => start(false);
  $("#wSim").onclick = () => start(true);
  $("#wBack").onclick = () => { W.pf = null; W.step = 4; drawWizard(c); };
}

function wInstall(W, body, foot, nav, go_, c) {
  const ts = wSel();
  body.innerHTML = `<div class="inst-head" id="ih"></div><div id="igate"></div><div id="ireport"></div><div class="devcards" id="icards" style="padding:0;margin:12px 0"></div><div id="iprobes"></div><div id="icon"></div><div id="ifinish"></div>`;
  foot.classList.add("static");
  foot.innerHTML = `<button id="iCancel">Cancel</button><span class="grow"></span><button class="primary" id="iDone" hidden>Finish</button>`;
  const con = makeConsole($("#icon"), { title: `${W.dry ? "Simulation" : "Installation"} log`, file: `upgrade-${W.version}` });
  $("#iCancel").onclick = () => { if (confirm("Cancel the upgrade? Devices that are already installing will finish rebooting.")) api(`/jobs/${W.job}/cancel`, { method: "POST" }); };
  $("#iDone").onclick = () => { W.done = true; S.wiz = null; pUpgrade(); };
  let shownPrompt = null, finished = false;
  streamJob(W.job, con, (j) => {
    const kids = j.children_detail || [];
    const doneN = kids.filter((k) => ["done", "failed", "cancelled"].includes(k.status)).length;
    const pct = kids.length ? Math.round(kids.reduce((a, k) => a + Math.min(stepIdx(k.stage, k.status), 5) / 5, 0) / kids.length * 100) : 0;
    $("#ih").innerHTML = `<div class="row" style="justify-content:space-between"><h2 style="margin:0">${j.status === "done" ? (W.dry ? "Simulation complete" : "Upgrade complete") : j.status === "failed" ? "Upgrade stopped" : j.status === "cancelled" ? "Upgrade cancelled" : (W.dry ? "Simulating upgrade…" : "Installing FortiOS v" + esc(W.version) + "…")}</h2>${statusBadge(j.status)}</div>
      <div class="hint">${esc(j.stage || "")} · ${doneN}/${kids.length || ts.length} device(s) finished</div><div class="bar big"><i style="width:${j.status === "done" ? 100 : pct}%"></i></div>`;
    if (j.prompt !== shownPrompt) {
      shownPrompt = j.prompt;
      $("#igate").innerHTML = j.prompt ? `<div class="gate"><b>${esc(j.prompt)}</b><div class="row" style="margin-top:8px"><button class="primary" id="gOk">${icon("check")}Continue</button><button id="gNo">Stop</button></div></div>` : "";
      if (j.prompt) { $("#gOk").onclick = () => api(`/jobs/${W.job}/answer`, { body: { approve: true } }); $("#gNo").onclick = () => api(`/jobs/${W.job}/answer`, { body: { approve: false } }); }
    }
    if (j.probes) {
      const op = $("#iprobes details")?.open;
      $("#iprobes").innerHTML = probePanel(j.probes);
      if (op) $("#iprobes details").open = true;
    }
    const keep = {};
    $$(".devcard[data-k]", $("#icards")).forEach((d) => { const tr = $(".fd-track", d); if (tr && tr.dataset.user === "1") keep[d.dataset.k] = tr.scrollLeft; });
    $("#icards").classList.toggle("flowcards", kids.some((k) => (k.flow || []).length));
    $("#icards").innerHTML = kids.map((k) => `<div class="devcard" data-k="${k.id}"><div class="dh"><b>${esc(k.title)}</b><span class="hint">${esc(k.stage || "")}</span>${statusBadge(k.status)}</div><div class="db">${(k.flow || []).length ? flowDiagram(k.flow) : stepper(k.stage, k.status) + `<div><b>${esc(k.stage || "")}</b></div>`}<div class="lastline" title="${esc(k.last)}">${esc(k.error || k.last || "")}</div>
      ${k.prompt && j.status !== "waiting" ? `<div class="gate" style="margin:8px 0"><b>${esc(k.prompt)}</b><div class="row" style="margin-top:6px"><button class="small primary" data-ok="${k.id}">Continue</button><button class="small" data-no="${k.id}">Stop</button></div></div>` : ""}</div></div>`).join("");
    flowFollow($("#icards"));
    Object.entries(keep).forEach(([id, x]) => { const tr = $(`.devcard[data-k="${id}"] .fd-track`, $("#icards")); if (tr) { tr.scrollLeft = x; tr.dataset.user = "1"; } });
    $$("[data-ok]", $("#icards")).forEach((b) => b.onclick = () => api(`/jobs/${b.dataset.ok}/answer`, { body: { approve: true } }));
    $$("[data-no]", $("#icards")).forEach((b) => b.onclick = () => api(`/jobs/${b.dataset.no}/answer`, { body: { approve: false } }));
    const over = ["done", "failed", "cancelled", "interrupted"].includes(j.status) && !kids.some((k) => ["running", "waiting"].includes(k.status));
    if (over && !finished) {
      finished = true;
      $("#iCancel").hidden = true; $("#iDone").hidden = false;
      setTimeout(() => reportPanel(W.job, $("#ireport")), 800);
      api(`/jobs/${W.job}`).then((jj) => {
        const res = jj.result?.results || [];
        $("#ifinish").innerHTML = `<div class="panel" style="margin-top:12px"><div class="ph"><span class="ttl">Results</span></div><div class="pb flush"><table><thead><tr><th>Device</th><th>Result</th><th>Path</th><th>Post-checks</th><th></th></tr></thead><tbody>
          ${(res.length ? res : kids.map((k) => ({ target: k.title, status: k.status, error: k.error, reports: [], path: null }))).map((r) => { const t = S.targets.find((x) => x.name === r.target);
            return `<tr><td><b>${esc(r.target)}</b></td><td>${statusBadge(r.status)} ${r.error ? `<span class="hint">${esc(r.error)}</span>` : ""}</td><td>${r.path ? `<div class="path">${r.path.map((s, k) => `${k ? '<span class="arrow">→</span>' : ""}<span class="v">${esc(s)}</span>`).join("")}</div>` : "—"}</td>
            <td>${(r.reports || []).map((x) => `${sevBadge(x.totals.critical ? "critical" : x.totals.warning ? "warning" : null)} ${x.totals.critical} critical · ${x.totals.warning} warning <button class="small link" data-diff="${t?.id}|${x.pre}|${x.post}">View diff</button>`).join("<br>") || "—"}</td>
            <td>${t ? `<button class="small" data-dev="${t.id}">Open device</button>` : ""}</td></tr>`; }).join("")}</tbody></table></div></div>`;
        $$("[data-diff]", $("#ifinish")).forEach((b) => b.onclick = () => { const [tt, pre, post] = b.dataset.diff.split("|"); diffModal(tt, pre, post); });
        $$("[data-dev]", $("#ifinish")).forEach((b) => b.onclick = () => go("device", [b.dataset.dev, "upgrades"]));
      });
    }
  });
}
