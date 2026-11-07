"use strict";
/* Add device: connect to one address, detect what it is (standalone, the shared HA cluster address
   or one HA unit's own address) and prompt for the right details: for an FGCP cluster the cluster
   address plus each unit's own address, each checked against the unit it should reach. */

function addDevice() {
  const O = { step: 1, c: { host: "", port: 443, scheme: "https", verify_ssl: false, token: "", ssh_user: "", ssh_password: "", ssh_port: 22, name: "" }, det: null };
  obConnect(O);
}

function obConnect(O) {
  const c = O.c;
  const f = modal("Add device", `
    <div class="ob-steps"><span class="on">1 Connect</span><span>2 Addresses</span></div>
    <p style="margin-top:0">Enter any management address of the FortiGate and its REST API token. The app connects (read-only) and works out
      whether it is a standalone unit or an HA cluster, then asks for anything else it needs.</p>
    <div class="banner info" style="margin:0 0 12px"><b>HA cluster?</b> Enter the cluster's shared management address if you know it (the interface IP that
      always reaches the primary). A unit's own address (its <code>management-ip</code> or dedicated HA management interface) works too: the app
      recognises it and asks for the cluster address.</div>
    <div class="grid">
      <div class="field">IP / FQDN<input id="obHost" value="${esc(c.host)}" placeholder="10.8.8.21" autofocus></div>
      <div class="field">HTTPS port<input id="obPort" type="number" value="${esc(c.port)}"></div>
      <div class="field">Protocol<select id="obScheme"><option ${c.scheme !== "http" ? "selected" : ""}>https</option><option ${c.scheme === "http" ? "selected" : ""}>http</option></select></div>
      <div class="field" style="grid-column:span 2">REST API token<input id="obTok" type="password" autocomplete="off" value="${esc(c.token)}"></div>
      <div class="field"><span>&nbsp;</span><label class="cb"><input type="checkbox" id="obTls" ${c.verify_ssl ? "checked" : ""}> Verify TLS certificate</label></div>
    </div>
    <details class="sec" ${c.ssh_user ? "open" : ""}><summary><b>SSH login</b><span class="hint">optional: used for CLI checks, and for an HA cluster to read the other unit's own address</span></summary>
      <div class="grid" style="padding:8px">
        <div class="field">SSH user<input id="obSu" value="${esc(c.ssh_user)}"></div>
        <div class="field">SSH password<input id="obSp" type="password" autocomplete="off" value="${esc(c.ssh_password)}"></div>
        <div class="field">SSH port<input id="obSport" type="number" value="${esc(c.ssh_port)}"></div>
      </div></details>
    <p class="hint">Create the token on the FortiGate under System › Administrators › Create New › REST API Admin, with read/write on System and read on
      everything else, and add this computer to its trusted hosts. In an HA cluster the same token works on every unit.</p>
    <div id="obErr"></div>`,
    `<button type="button" class="link" id="obManual" style="margin-right:auto">Enter details manually</button><button value="cancel">Cancel</button><button type="button" class="primary" id="obGo">${icon("search")}Detect</button>`);
  const read = () => Object.assign(c, { host: $("#obHost", f).value.trim(), port: Number($("#obPort", f).value) || 443, scheme: $("#obScheme", f).value,
    token: $("#obTok", f).value.trim(), verify_ssl: $("#obTls", f).checked, ssh_user: $("#obSu", f).value.trim(), ssh_password: $("#obSp", f).value,
    ssh_port: Number($("#obSport", f).value) || 22 });
  $("#obManual", f).onclick = () => { read(); editTarget({ type: "standalone", members: [{ host: c.host, port: c.port, scheme: c.scheme, token: c.token, ssh_user: c.ssh_user, ssh_password: c.ssh_password, ssh_port: c.ssh_port, verify_ssl: c.verify_ssl }] }); };
  const go = $("#obGo", f);
  go.onclick = async () => {
    read();
    if (!c.host || !c.token) { $("#obErr", f).innerHTML = '<div class="banner warn">Enter the address and the REST API token.</div>'; return; }
    go.disabled = true; go.innerHTML = '<span class="spin"></span> Connecting…';
    try { O.det = await api("/onboard/detect", { body: c }); obAddresses(O); }
    catch (e) { $("#obErr", f).innerHTML = `<div class="banner crit">${esc(e.message)}</div>`; go.disabled = false; go.innerHTML = `${icon("search")}Detect`; }
  };
  $$("input", f).forEach((i) => i.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); go.click(); } });
}

function obSummary(d) {
  return `<div class="ob-dev"><div><b>${esc(d.hostname)}</b> <span class="hint mono">${esc(d.serial)}</span></div>
    <div>${esc(d.model)} · <span class="chip ver">v${esc(d.version)}</span> b${esc(d.build)} · ${d.type === "fgcp" ? badge(`FGCP ${d.ha_mode === "a-a" ? "active-active" : "active-passive"}`, "info") : d.type === "fgsp" ? badge("FGSP", "info") : badge("Standalone", "grey")}</div></div>`;
}

function obAddresses(O) {
  const d = O.det, c = O.c;
  if (d.type !== "fgcp") return obSimple(O);
  const units = O.units || (O.units = d.units.map((u) => ({ ...u, addr: u.this && d.address.role === "unit" ? c.host : (u.own[0]?.addr || ""), port: u.this ? c.port : (d.admin_sport || c.port), check: null })));
  if (O.cluster == null) O.cluster = { host: d.address.cluster_host || "", port: d.address.role === "cluster" ? c.port : (d.admin_sport || c.port), check: null };
  const a = d.address;
  const me = units.find((u) => u.this);
  const what = a.role === "cluster" ? `<div class="banner ok" style="margin:0"><b>${esc(c.host)} is the cluster's shared address</b> (${esc(a.kind)}). It always reaches the primary, so upgrades go through it.</div>`
    : a.role === "unit" ? `<div class="banner warn" style="margin:0"><b>${esc(c.host)} is ${esc(me?.hostname || d.hostname)}'s own address</b> (${esc(a.kind)}). It always reaches that one unit, even after it becomes the secondary, so upgrades can't go through it. It's been put under that unit below. Enter the cluster address${a.cluster_host ? "; the app suggests " + esc(a.cluster_host) + ", check it is right" : ""}.</div>`
    : `<div class="banner info" style="margin:0"><b>${esc(c.host)} isn't an interface address on the unit</b> (an FQDN or NAT address?). If it reaches whichever unit is primary, use it as the cluster address; otherwise use it as ${esc(me?.hostname || "this unit")}'s own address.</div>`;
  const rowState = (k) => !k ? "" : k.ok ? `<span class="ob-ok">${icon("check")} ${esc(k.msg)}</span>` : `<span class="ob-bad">✕ ${esc(k.msg)}</span>`;
  const f = modal(`Add HA cluster: ${d.hostname}`, `
    <div class="ob-steps"><span class="done">1 Connect</span><span class="on">2 Addresses</span></div>
    ${obSummary(d)}
    ${what}
    <h3>Cluster address <span class="hint">required</span></h3>
    <p class="hint" style="margin-top:0">The shared management address of the cluster: the IP of an interface such as port1, which only the primary answers on and
      which moves to the new primary on failover. The app upgrades the whole cluster through it.</p>
    <div class="ob-row"><input id="obCl" value="${esc(O.cluster.host)}" placeholder="e.g. 10.8.8.21"><input id="obClPort" type="number" value="${esc(O.cluster.port)}" title="HTTPS port" style="width:90px"><span id="obClState">${rowState(O.cluster.check)}</span></div>
    <h3>Each unit's own address <span class="hint">recommended</span></h3>
    <p class="hint" style="margin-top:0">The address that reaches one unit whichever unit is primary: its dedicated HA management interface (<code>ha-mgmt-interfaces</code>)
      or the <code>management-ip</code> on an interface. With these the app shows every unit's details, confirms each unit's firmware after the upgrade,
      reviews both units' logs, probes each unit, and can use the Local only and Secondary only upgrade modes. Don't enter a unit's ordinary
      interface IP: secondaries don't answer on the cluster's shared addresses.</p>
    <table class="ob-units"><thead><tr><th>Unit</th><th>Own address</th><th>Port</th><th></th></tr></thead><tbody>
      ${units.map((u, i) => `<tr><td><b>${esc(u.hostname || u.serial)}</b> ${u.primary ? badge("Primary", "up") : badge("Secondary", "info")}<div class="hint mono">${esc(u.serial)} · priority ${esc(u.priority ?? "—")}</div></td>
        <td><input data-ua="${i}" value="${esc(u.addr)}" list="obl${i}" placeholder="${u.own.length ? "" : "its management-ip or HA management address"}"><datalist id="obl${i}">${u.own.map((o) => `<option value="${esc(o.addr)}">${esc(o.kind)}</option>`).join("")}</datalist>
          <div class="hint">${u.own.length ? `found ${u.source === "this unit" ? "on this unit" : "over SSH"}: ${u.own.map((o) => `${esc(o.addr)} (${esc(o.kind)}${o.https ? "" : ", HTTPS not allowed"})`).join(", ")}` : "not known: type it, or add SSH credentials on step 1 so the app can read it"}</div></td>
        <td><input data-up="${i}" type="number" value="${esc(u.port)}" style="width:80px"></td><td data-us="${i}">${rowState(u.check)}</td></tr>`).join("")}
    </tbody></table>
    ${d.notes.map((n) => `<div class="hint" style="margin-top:6px">${esc(n)}</div>`).join("")}
    <div class="form" style="margin-top:12px"><label>Device name</label><input id="obName" value="${esc(O.c.name || d.ha?.group_name || (d.hostname.replace(/-[ab12]$/i, "") + " cluster"))}"></div>
    <div id="obErr"></div>`,
    `<button type="button" id="obBack" style="margin-right:auto">‹ Back</button><button value="cancel">Cancel</button><button type="button" id="obCheck">${icon("check")}Check addresses</button><button type="button" class="primary" id="obSave">Save device</button>`);
  const read = () => {
    O.cluster.host = $("#obCl", f).value.trim(); O.cluster.port = Number($("#obClPort", f).value) || 443;
    units.forEach((u, i) => { u.addr = $(`[data-ua="${i}"]`, f).value.trim(); u.port = Number($(`[data-up="${i}"]`, f).value) || 443; });
    O.c.name = $("#obName", f).value.trim();
  };
  $("#obBack", f).onclick = () => { read(); obConnect(O); };
  const probe = (host, port) => api("/onboard/detect", { body: { ...c, host, port, ssh_user: "" } });
  const primary = units.find((u) => u.primary);
  const check = async () => {
    read();
    const btn = $("#obCheck", f); btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Checking…';
    const jobs = [];
    if (O.cluster.host) jobs.push(probe(O.cluster.host, O.cluster.port).then((r) => {
      O.cluster.check = r.address.role === "unit" ? { ok: false, msg: `this is ${r.hostname}'s own address, not the cluster address` }
        : primary && r.serial !== primary.serial ? { ok: false, msg: `reached ${r.hostname} (${r.serial}), not the primary` }
        : { ok: true, msg: `reaches the primary, ${r.hostname}` };
    }).catch((e) => { O.cluster.check = { ok: false, msg: e.message }; }));
    else O.cluster.check = { ok: false, msg: "required" };
    units.forEach((u) => {
      if (!u.addr) { u.check = null; return; }
      if (u.addr === O.cluster.host && u.port === O.cluster.port) { u.check = { ok: false, msg: "same as the cluster address" }; return; }
      jobs.push(probe(u.addr, u.port).then((r) => {
        u.check = r.serial === u.serial ? { ok: true, msg: `reaches ${r.hostname}` } : { ok: false, msg: `reached ${r.hostname} (${r.serial}) instead` };
      }).catch((e) => { u.check = { ok: false, msg: e.message.replace(/^Could not connect to [^:]+:\d+: /, "") }; }));
    });
    await Promise.all(jobs);
    $("#obClState", f).innerHTML = rowState(O.cluster.check);
    units.forEach((u, i) => { $(`[data-us="${i}"]`, f).innerHTML = rowState(u.check); });
    btn.disabled = false; btn.innerHTML = `${icon("check")}Check addresses`;
  };
  $("#obCheck", f).onclick = check;
  $("#obSave", f).onclick = async () => {
    await check();
    if (!O.cluster.check?.ok) { $("#obErr", f).innerHTML = `<div class="banner crit" style="margin-top:10px">The cluster address must reach the primary before the device can be saved: ${esc(O.cluster.check?.msg || "")}</div>`; return; }
    const bad = units.filter((u) => u.addr && !u.check?.ok), none = units.filter((u) => !u.addr);
    if (bad.length && !confirm(`${bad.map((u) => u.hostname).join(", ")}: the own address didn't check out. Save it anyway?`)) return;
    if (none.length && !confirm(`No own address for ${none.map((u) => u.hostname).join(", ")}. The dashboard, per-unit checks and probes will only see what the primary reports about ${none.length > 1 ? "them" : "it"}. Save anyway?`)) return;
    const cred = { scheme: c.scheme, verify_ssl: c.verify_ssl, token: c.token, ssh_user: c.ssh_user, ssh_password: c.ssh_password || undefined, ssh_port: c.ssh_port };
    const members = [{ ...cred, name: "cluster", host: O.cluster.host, port: O.cluster.port },
      ...units.filter((u) => u.addr).map((u) => ({ ...cred, name: u.hostname || u.serial, host: u.addr, port: u.port }))];
    await obSave({ name: O.c.name || d.hostname, type: "fgcp", members }, f);
  };
}

function obSimple(O) {
  const d = O.det, c = O.c;
  const f = modal(`Add device: ${d.hostname}`, `
    <div class="ob-steps"><span class="done">1 Connect</span><span class="on">2 Save</span></div>
    ${obSummary(d)}
    ${d.type === "fgsp" ? `<div class="banner info" style="margin:0 0 10px"><b>This unit is an FGSP peer.</b> Each FGSP peer is a separate FortiGate with its own address and is upgraded on its own.
      Continue in the device form to add the other peers' addresses.</div>` : `<div class="banner ok" style="margin:0 0 10px">A standalone FortiGate, reached at ${esc(c.host)}:${esc(c.port)}.</div>`}
    <div class="form"><label>Device name</label><input id="obName" value="${esc(c.name || d.hostname)}"></div><div id="obErr"></div>`,
    `<button type="button" id="obBack" style="margin-right:auto">‹ Back</button><button value="cancel">Cancel</button><button type="button" class="primary" id="obSave">${d.type === "fgsp" ? "Continue" : "Save device"}</button>`);
  $("#obBack", f).onclick = () => { c.name = $("#obName", f).value.trim(); obConnect(O); };
  const member = { name: d.hostname, host: c.host, port: c.port, scheme: c.scheme, verify_ssl: c.verify_ssl, token: c.token, ssh_user: c.ssh_user, ssh_password: c.ssh_password || undefined, ssh_port: c.ssh_port };
  $("#obSave", f).onclick = () => d.type === "fgsp" ? editTarget({ name: $("#obName", f).value.trim() || d.hostname, type: "fgsp", members: [member, {}] })
    : obSave({ name: $("#obName", f).value.trim() || d.hostname, type: "standalone", members: [member] }, f);
}

async function obSave(t, f) {
  try {
    const saved = await api("/targets", { body: t });
    $("#modal").close(); await loadTargets(); await loadInv(true);
    toast(`${t.name} added.`);
    go("device", [saved.id, "overview"]);
  } catch (e) { $("#obErr", f).innerHTML = `<div class="banner crit" style="margin-top:10px">${esc(e.message)}</div>`; }
}
