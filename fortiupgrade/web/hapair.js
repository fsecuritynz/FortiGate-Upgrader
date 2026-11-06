"use strict";
/* HA cluster view on the device dashboard: every unit drawn as its appliance, the heartbeat
 * link between them, each unit's role and HA priority, and the cluster's addresses. */

function haUnits(m, ms) {
  const hi = m.ha_info || {};
  const peers = hi.peers && hi.peers.length ? hi.peers : [{ serial: m.serial, hostname: m.hostname, priority: hi.priority, primary: true }];
  const basePorts = m.ports || portsFromInterfaces(m.interfaces);
  return peers.map((p) => {
    const om = ms.find((x) => x.ok && x.serial === p.serial);
    const ownIf = om && (om.mgmt?.interfaces || []).find((i) => i.management_ip);
    return {
      ...p,
      member: om,
      model: (om || m).model, code: (om || m).model_code,
      // a unit the app has no address for: same layout as the unit we can see, link state unknown
      ports: om ? (om.ports || portsFromInterfaces(om.interfaces)) : basePorts.map((x) => ({ name: x.name, status: "Unknown" })),
      own: ownIf ? `${ownIf.management_ip} (${ownIf.name})` : om ? `${om.host}` : "",
      known: !!om,
    };
  });
}

function haPairPanel(t, m, ms) {
  const hi = m.ha_info || {};
  if (!["a-p", "a-a"].includes(m.ha_mode) || !hi.peers || hi.peers.length < 2) return "";
  const units = haUnits(m, ms);
  const hb = hi.hbdev || [];
  const clusterIf = (m.mgmt?.interfaces || []).find((i) => i.management_ip && i.ip) || (m.mgmt?.interfaces || []).find((i) => i.admin && i.ip);
  const unitCard = (u) => `<div class="ha-unit">
      <div class="ha-head"><b>${esc(u.hostname || u.serial)}</b>${u.primary ? badge("Primary", "up") : badge("Secondary", "info")}
        <span class="ha-prio" title="HA priority (higher wins when override is enabled or on equal uptime)">Priority <b>${esc(u.priority ?? "—")}</b></span>
        <span class="hint mono">${esc(u.serial)}</span><span class="hint" data-own="${esc(u.serial)}">${u.own ? `own address <span class="mono">${esc(u.own)}</span>` : "own address: looking it up…"}</span></div>
      <div class="faceplate">${faceplate(u.model, u.code, u.ports, { hb, legend: false })}</div>
      ${u.known ? "" : '<div class="hint" style="margin-top:2px">Port link state is not shown for this unit: the app has no address that reaches it.</div>'}</div>`;
  const body = units.map((u, i) => unitCard(u) + (i < units.length - 1 ? `<div class="ha-gap"><span class="ha-label">${icon("pulse")} HA heartbeat ${esc(hb.join(", ") || "—")}${Object.keys(hi.hb_priorities || {}).length ? ` <span class="hint">(heartbeat priority ${esc(Object.keys(hi.hb_priorities).length === 1 ? Object.values(hi.hb_priorities)[0] : Object.entries(hi.hb_priorities).map(([k, v]) => `${k} ${v}`).join(", "))})</span>` : ""}</span></div>` : "")).join("");
  const facts = [
    ["Mode", `${m.ha_mode === "a-p" ? "Active-passive" : "Active-active"} · group "${hi.group_name || "—"}"${hi.group_id != null ? ` (id ${hi.group_id})` : ""}`],
    ["Override", hi.override === "enable" ? "Enabled: the highest priority unit always becomes primary" : "Disabled: uptime decides first, then priority"],
    ["Monitored interfaces", (hi.monitor || []).join(", ") || "none"],
    ["Session pickup", hi.session_pickup === "enable" ? "Enabled" : "Disabled"],
    ["Cluster address", clusterIf ? `<span class="mono">${esc(clusterIf.ip)}</span> on ${esc(clusterIf.name)}: shared, always reaches the primary` : "—"],
  ];
  return `<div class="panel spanall ha-panel"><div class="ph"><span class="ttl">${icon("devices")} HA cluster: ${units.length} × ${esc(m.model)}</span><span class="tools hint">heartbeat interfaces are outlined in amber</span></div>
    <div class="ha-grid"><div class="hapair" id="hapair">${body}<svg class="ha-links" aria-hidden="true"></svg></div>
      <div class="kv ha-facts">${facts.map(([k, v]) => `<div>${esc(k)}</div><div>${v}</div>`).join("")}</div></div>
    ${haAddressFix(t, m, units, clusterIf)}</div>`;
}

// When the device is saved with one unit's own address, or a unit has no address yet, offer to
// re-save it as cluster address + unit addresses (credentials are reused; HA syncs admins and API users).
function haAddressFix(t, m, units, clusterIf) {
  const role = m.address?.role;
  const missing = units.filter((u) => !u.known);
  if (!clusterIf || (role === "cluster" && !missing.length && t.type === "fgcp")) return "";
  const why = role === "unit"
    ? `<b>${esc(t.name)} is saved with ${esc(m.host)}, ${esc(m.hostname)}'s own address</b> (the <code>management-ip</code> on ${esc(m.address.interface)}). It always reaches that one unit, even after it becomes the secondary, so cluster changes made through it during an upgrade (setting and restoring the HA upgrade mode) would go to the wrong unit. Upgrade through the cluster address ${esc(clusterIf.ip)}, and keep each unit's own address as an extra address.`
    : missing.length ? `${missing.map((u) => esc(u.hostname || u.serial)).join(", ")} ${missing.length > 1 ? "have" : "has"} no address in the app, so ${missing.length > 1 ? "their" : "its"} ports, version and reboot can't be checked individually and it can't be probed.`
    : `${esc(t.name)} is saved as ${esc(typeName(t.type))}. Save it as an HA cluster.`;
  const ownOf = (u) => u.member ? ((u.member.mgmt?.interfaces || []).find((i) => i.management_ip)?.management_ip || u.member.host) : "";
  return `<div class="pb" style="border-top:1px solid var(--rule)"><div class="banner ${role === "unit" ? "warn" : "info"}" style="margin:0 0 10px">${why}</div>
    <div class="ha-fix" id="haFix"><div class="field">Cluster address<input id="hfCluster" value="${esc(clusterIf.ip)}"></div>
      ${units.map((u, i) => `<div class="field">${esc(u.hostname || u.serial)} own address<input data-hfu="${i}" data-serial="${esc(u.serial)}" data-name="${esc(u.hostname || u.serial)}" value="${esc(ownOf(u))}" placeholder="its port1 management-ip, e.g. 10.8.8.20"></div>`).join("")}
      <button class="primary" id="hfSave">Save addresses</button></div>
    <p class="hint" style="margin:6px 0 0">The API token and SSH login of ${esc(m.host)} are reused for the new addresses (HA keeps admin accounts and API users the same on every unit). The device is saved as an HA cluster.</p></div>`;
}

function bindHaPanel(t, c, reload) {
  const root = $("#hapair", c);
  if (!root) return;
  const draw = () => {
    const svg = $(".ha-links", root);
    const box = root.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
    const units = $$(".ha-unit", root);
    let out = "";
    for (let i = 0; i < units.length - 1; i++) {
      const a = $$(".fp-hb", units[i]), b = $$(".fp-hb", units[i + 1]);
      a.forEach((pa) => {
        const pb = b.find((x) => x.dataset.port === pa.dataset.port);
        if (!pb) return;
        const ra = pa.getBoundingClientRect(), rb = pb.getBoundingClientRect();
        const x1 = ra.left + ra.width / 2 - box.left, y1 = ra.bottom - box.top + 2;
        const x2 = rb.left + rb.width / 2 - box.left, y2 = rb.top - box.top - 2;
        const my = (y1 + y2) / 2;
        out += `<path d="M${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2}" class="ha-cable"/><circle cx="${x1}" cy="${y1}" r="3" class="ha-end"/><circle cx="${x2}" cy="${y2}" r="3" class="ha-end"/>`;
      });
    }
    svg.innerHTML = out;
  };
  requestAnimationFrame(draw);
  if (window.ResizeObserver) { const ro = new ResizeObserver(draw); ro.observe(root); }
  // each unit's own address: from its saved address, or read from the primary over SSH
  api(`/targets/${t.id}/units`).then((r) => {
    for (const u of r.units) {
      const own = (u.own || [])[0];
      const span = $(`[data-own="${CSS.escape(u.serial)}"]`, c);
      if (span) span.innerHTML = own ? `own address <span class="mono">${esc(own.addr)}</span> <span class="hint">(${esc(own.kind)}${u.source === "ssh" ? ", read over SSH from the primary" : ""})</span>` : "no own management address configured";
      const inp = $(`[data-hfu][data-serial="${CSS.escape(u.serial)}"]`, c);
      if (inp && !inp.value && own) inp.value = own.addr;
    }
    $$("[data-own]", c).forEach((x) => { if (/looking/.test(x.textContent)) x.textContent = "own address not known: add it below"; });
  }).catch(() => $$("[data-own]", c).forEach((x) => { if (/looking/.test(x.textContent)) x.textContent = "own address not known"; }));
  const save = $("#hfSave", c);
  if (save) save.onclick = async () => {
    const cluster = $("#hfCluster", c).value.trim();
    const units = $$("[data-hfu]", c).map((x) => ({ host: x.value.trim(), name: x.dataset.name })).filter((u) => u.host && u.host !== cluster);
    if (!cluster) return toast("Enter the cluster address.");
    save.disabled = true;
    try { await api(`/targets/${t.id}/ha-addresses`, { body: { cluster_host: cluster, units } }); await loadTargets(); await loadInv(true); toast("Saved: upgrades now go through the cluster address."); reload(); }
    catch (e) { toast(e.message); save.disabled = false; }
  };
}
