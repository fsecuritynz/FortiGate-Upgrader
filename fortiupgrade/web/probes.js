"use strict";
/* Service availability probes: wizard editor and the live/finished timeline panel. */

const PROBE_KINDS = { ping: "Ping", tcp: "TCP", http: "HTTP(S)" };
const PROBE_DEF_IV = { ping: 200, tcp: 1000, http: 2000 };
const PROBE_MIN_IV = { ping: 100, tcp: 250, http: 1000 };
const probeKey = (p) => `${p.type}|${p.type === "http" ? p.url : p.target}|${p.port || ""}`;

function fmtMs(ms) {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return m < 60 ? `${m} min ${String(r).padStart(2, "0")} s` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}
const clockMs = (t) => { const d = new Date(t); return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0"); };

function probeStore(probes) {
  // only the probes the user added are remembered; management-address probes are re-read from the devices
  try { if (probes) localStorage.setItem("fgu-probes", JSON.stringify(probes.filter((p) => !p.auto))); else return JSON.parse(localStorage.getItem("fgu-probes") || "null"); } catch { /* storage blocked */ }
  return null;
}

// ------------------------------------------------------------ wizard editor
function probeEditor(W, el) {
  if (!W.probes) W.probes = (probeStore() || []).filter((p) => !p.auto);
  if (!W.probeOpts) W.probeOpts = { baseline: 15, settle: 60, minOutage: 1000 };
  if (!W.probeRemoved) W.probeRemoved = new Set();
  const P = W.probeOpts;
  // default probes: every management address of the selected devices (re-read when the selection changes)
  const selKey = [...W.sel].sort().join(",");
  if (W.probeAutoFor !== selKey) {
    W.probeAutoFor = selKey; W.probeAutoBusy = true; W.probeNotes = [];
    api(`/probes/suggest?target_ids=${encodeURIComponent(selKey)}`).then((r) => {
      const manual = W.probes.filter((p) => !p.auto), have = new Set(manual.map(probeKey));
      W.probes = [...r.probes.filter((p) => !have.has(probeKey(p)) && !W.probeRemoved.has(probeKey(p))), ...manual];
      W.probeNotes = r.notes || [];
    }).catch((e) => { W.probeNotes = [`Could not read the devices' management addresses: ${e.message}`]; })
      .finally(() => { W.probeAutoBusy = false; W.probeTest = null; if (el.isConnected) draw(); });
  }
  const row = (p, i) => {
    const t = W.probeTest?.[i];
    const tcell = !t ? "" : t.busy ? '<span class="spin"></span>' : t.ok === t.attempts ? badge(`${t.ok}/${t.attempts} ok${t.rtt_ms != null ? ` · ${t.rtt_ms} ms` : ""}`, "up")
      : t.ok ? badge(`${t.ok}/${t.attempts} ok`, "warn") : badge(t.error || "no answer", "down");
    return `<tr data-i="${i}" class="${p.auto ? "auto" : ""}">
      <td class="nw">${p.auto ? `<span class="badge b-info" title="Added automatically: ${esc(p.why || "management address")}">Auto</span> ` : ""}<select data-f="type">${Object.entries(PROBE_KINDS).map(([k, n]) => `<option value="${k}" ${p.type === k ? "selected" : ""}>${n}</option>`).join("")}</select></td>
      <td><input data-f="${p.type === "http" ? "url" : "target"}" value="${esc(p.type === "http" ? p.url || "" : p.target || "")}" placeholder="${p.type === "http" ? "https://intranet.example.com/health" : "10.0.0.10 or server.example.com"}" class="pt-in"></td>
      <td>${p.type === "tcp" ? `<input data-f="port" type="number" min="1" max="65535" value="${esc(p.port || "")}" placeholder="443" style="width:76px">` : p.type === "http" ? `<input data-f="expect" value="${esc(p.expect || "")}" placeholder="any < 500" title="Expected status, e.g. 200 or 2xx,3xx. Empty: any answer below 500 counts as up." style="width:90px">` : '<span class="hint">ICMP echo</span>'}</td>
      <td class="nw"><input data-f="interval_ms" type="number" min="${PROBE_MIN_IV[p.type]}" step="100" value="${esc(p.interval_ms || PROBE_DEF_IV[p.type])}" style="width:70px" title="${p.type === "http" ? "At least 1000 ms: every HTTP(S) probe is a full new TLS session, and faster rates load the FortiGate's admin web server" : ""}"> <span class="hint">ms</span></td>
      <td>${tcell}</td>
      <td><button class="small link" data-del="${i}" title="Remove">✕</button></td></tr>`;
  };
  const draw = () => {
    el.innerHTML = `<div class="panel"><div class="ph"><span class="ttl">${icon("pulse")} Service availability probes</span><span class="tools hint">${W.probes.length ? `${W.probes.length} probe${W.probes.length > 1 ? "s" : ""}` : "optional"}</span></div><div class="pb">
      <p class="hint" style="margin-top:0">Measure the real outage window. While the upgrade runs, this computer pings, opens TCP connections or makes HTTP requests at a fast, fixed rate and records every failure, so you can see exactly when services dropped, for how long, and which upgrade step caused it (reboot, HA failover). Choose targets whose traffic passes <b>through</b> the FortiGate, e.g. a server behind it, a VIP, or an internet address if this computer routes through it. Probing the FortiGate's own address shows when the unit or cluster address stops answering.</p>
      <p class="hint">${W.probeAutoBusy ? '<span class="spin"></span> Reading the management addresses of the selected FortiGates…' : `Rows marked <b>Auto</b> are added automatically: each unit's own management address, so the timeline shows every unit's availability separately. For an HA cluster that is each unit's <code>management-ip</code> or dedicated HA management interface, never the shared cluster address. Another unit's address is read from the primary over SSH if it isn't saved on the device. Ping is only added where the interface allows ping, and HTTPS only where it allows HTTPS, on the admin HTTPS port. Remove any you don't want.`}</p>
      ${(W.probeNotes || []).length ? `<ul class="hint" style="margin:0 0 8px 16px">${W.probeNotes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
      ${W.probes.length ? `<div class="probe-scroll"><table class="probe-ed"><thead><tr><th>Type</th><th class="pt">Target</th><th>Port / expect</th><th>Every</th><th>Test</th><th></th></tr></thead><tbody>${W.probes.map(row).join("")}</tbody></table></div>` : ""}
      <div class="row" style="margin-top:10px;flex-wrap:wrap">
        <button class="small" data-add="ping">+ Ping</button><button class="small" data-add="tcp">+ TCP port</button><button class="small" data-add="http">+ HTTP(S)</button>
        <button class="small" id="pAddFgt" title="Ping and TCP-connect to each selected FortiGate's management address. For FGCP that is the shared cluster address, which shows the failover outage users see; each unit's own HA management address is pinged too, if you have added it.">+ Selected FortiGates</button>
        <span class="grow"></span>${W.probes.length ? `<button class="small" id="pTest">${icon("play")}Test now</button><button class="small link" id="pClear">Remove all</button>` : ""}</div>
      ${W.probes.length ? `<details class="sec" style="margin-top:10px"><summary>Timing</summary><div class="grid" style="padding:10px 12px">
        <div class="row">Baseline before installing <input id="pBase" type="number" min="0" value="${P.baseline}" style="width:70px"> s</div>
        <div class="row">Keep probing afterwards <input id="pSettle" type="number" min="0" value="${P.settle}" style="width:70px"> s</div>
        <div class="row">Count as outage after <input id="pMin" type="number" min="100" step="100" value="${P.minOutage}" style="width:80px"> ms of failures</div></div>
        <p class="hint" style="margin:0 12px 10px">Probing starts with the pre-checks. If a service is still down when the settle time ends, probing continues for up to 5 more minutes to capture when it recovers.</p></details>` : ""}
    </div></div>`;
    $$("[data-add]", el).forEach((b) => b.onclick = () => { W.probes.push({ type: b.dataset.add, interval_ms: PROBE_DEF_IV[b.dataset.add] }); W.probeTest = null; save(); draw(); $(`tr[data-i="${W.probes.length - 1}"] input`, el)?.focus(); });
    $$("[data-del]", el).forEach((b) => b.onclick = () => { const [gone] = W.probes.splice(Number(b.dataset.del), 1); if (gone?.auto) W.probeRemoved.add(probeKey(gone)); W.probeTest = null; save(); draw(); });
    $$("tr[data-i]", el).forEach((tr) => $$("[data-f]", tr).forEach((inp) => inp.onchange = () => {
      const p = W.probes[Number(tr.dataset.i)], f = inp.dataset.f;
      if (f === "type") { p.type = inp.value; p.interval_ms = PROBE_DEF_IV[p.type]; if (p.type === "http" && !p.url && p.target) p.url = p.target; }
      else p[f] = inp.type === "number" ? Number(inp.value) : inp.value.trim();
      if (f === "interval_ms" && p[f] < PROBE_MIN_IV[p.type]) { p[f] = PROBE_MIN_IV[p.type]; inp.value = p[f]; toast(`${PROBE_KINDS[p.type]} probes run at most every ${PROBE_MIN_IV[p.type]} ms.`); }
      if (p.auto && f !== "interval_ms") delete p.auto;  // edited: now the user's own probe
      W.probeTest = null; save(); if (f === "type") draw();
    }));
    const addF = $("#pAddFgt", el);
    if (addF) addF.onclick = () => {
      for (const t of wSel()) {
        const m = t.members[0]; if (!m) continue;
        if (!W.probes.some((p) => p.type === "ping" && p.target === m.host)) W.probes.push({ type: "ping", target: m.host, interval_ms: 200 });
        const port = m.port || 443;
        if (!W.probes.some((p) => p.type === "tcp" && p.target === m.host && Number(p.port) === port)) W.probes.push({ type: "tcp", target: m.host, port, interval_ms: 1000 });
        // HA: each unit's dedicated management address shows that unit's own reboot
        t.members.slice(1).forEach((u, n) => { if (u.host && !W.probes.some((p) => p.type === "ping" && p.target === u.host)) W.probes.push({ type: "ping", target: u.host, interval_ms: 200, name: `Ping ${u.name || "unit " + (n + 2)} (${u.host})` }); });
      }
      W.probeTest = null; save(); draw();
    };
    const tb = $("#pTest", el);
    if (tb) tb.onclick = async () => {
      W.probeTest = W.probes.map(() => ({ busy: true })); draw();
      try {
        const r = await api("/probes/test", { body: { probes: W.probes } });
        const valid = W.probes.map((p, i) => i).filter((i) => W.probes[i].target || W.probes[i].url);
        W.probeTest = W.probes.map(() => null);
        r.results.forEach((x, k) => { W.probeTest[valid[k]] = x; });
      } catch (e) { W.probeTest = null; toast(e.message); }
      draw();
    };
    const cl = $("#pClear", el); if (cl) cl.onclick = () => { W.probes = []; W.probeTest = null; save(); draw(); };
    [["pBase", "baseline"], ["pSettle", "settle"], ["pMin", "minOutage"]].forEach(([id, k]) => { const x = $("#" + id, el); if (x) x.onchange = () => { P[k] = Number(x.value) || 0; }; });
  };
  const save = () => probeStore(W.probes);
  draw();
}

function probeSpecs(W) {
  return (W.probes || []).filter((p) => p.target || p.url).map((p) => ({ type: p.type, target: p.target, url: p.url, port: p.port, expect: p.expect, interval_ms: p.interval_ms, timeout_ms: p.timeout_ms, name: p.name }));
}
function probeValidate(W) {
  for (const [i, p] of (W.probes || []).entries()) {
    if (!(p.target || p.url)) return `Probe ${i + 1}: enter a ${p.type === "http" ? "URL" : "target address"}, or remove it.`;
    if (p.type === "tcp" && !(Number(p.port) > 0 && Number(p.port) < 65536)) return `Probe ${i + 1}: enter a TCP port.`;
  }
  return null;
}

// ------------------------------------------------------------ timeline panel
function probePanel(sum, opts = {}) {
  if (!sum || !sum.probes || !sum.probes.length) return "";
  const t0 = sum.started, t1 = sum.ended || sum.now || Date.now(), span = Math.max(t1 - t0, 1);
  const X = (t) => Math.max(0, Math.min(1000, (t - t0) / span * 1000));
  const bms = sum.bucket_ms || 5000;
  const live = !sum.ended;
  const marks = (sum.markers || []).map((m) => `<line x1="${X(m.t)}" x2="${X(m.t)}" y1="0" y2="16" class="pm"><title>${esc(clockMs(m.t))} ${esc(m.src)}: ${esc(m.label)}</title></line>`).join("");
  const PH = { baseline: "Baseline", upgrade: "Upgrade", settle: "After" };
  const phs = (sum.phases || []).filter((p, i, a) => !a[i + 1] || X(a[i + 1].t) - X(p.t) > 70);  // drop labels that would overlap
  const phases = phs.map((p) => `<span class="pph" style="left:${Math.min(X(p.t) / 10, 92)}%">${esc(PH[p.phase] || p.phase)}</span>`).join("");
  const rows = sum.probes.map((p) => {
    const bars = (p.buckets || []).map(([bt, sent, lost, avg, mx]) => `<rect x="${X(bt)}" width="${Math.max(X(bt + bms) - X(bt), 0.8) + 0.6}" y="3" height="10" class="${!lost ? "pb-ok" : lost >= sent ? "pb-down" : "pb-loss"}"><title>${esc(clockMs(bt))}: ${sent - lost}/${sent} ok${avg != null ? `, avg ${avg} ms, max ${mx} ms` : ""}</title></rect>`).join("");
    const outs = (p.windows || []).filter((w) => w.kind === "outage").map((w) => `<rect x="${X(w.start)}" width="${Math.max(X(w.end) - X(w.start), 1.5)}" y="1" height="14" class="pb-out"><title>Down ${esc(clockMs(w.start))} → ${w.ongoing ? "still down" : esc(clockMs(w.end))} (${fmtMs(w.duration_ms)})${w.during ? ` during ${esc(w.during_src ? w.during_src + ": " : "")}${esc(w.during)}` : ""}</title></rect>`).join("");
    const now = p.status === "down" ? badge("Down", "down") : p.status === "up" ? badge("Up", "up") : p.status === "degraded" ? badge("Lossy", "warn") : badge("Starting", "grey");
    return `<div class="prow"><div class="pname" title="${esc(p.name)} · ${esc(p.method || "")} every ${p.interval_ms} ms">${live ? now + " " : ""}<b>${esc(p.name)}</b></div>
      <svg class="ptrack" viewBox="0 0 1000 16" preserveAspectRatio="none">${bars}${outs}${marks}</svg></div>`;
  }).join("");
  const rtt = (ph) => ph && ph.rtt_avg != null ? `${ph.rtt_avg} ms` : "—";
  const stats = sum.probes.map((p) => ({
    Probe: p.name, Now: live ? (p.last?.ok ? (p.last.rtt != null ? `${p.last.rtt} ms` : "ok") : (p.last?.err || "—")) : undefined,
    Availability: p.availability_pct != null ? `${p.availability_pct.toFixed(p.availability_pct === 100 ? 0 : 2)} %` : "—",
    Outages: String(p.outages), "Total down": p.outages ? fmtMs(p.downtime_ms) : "—", Longest: p.outages ? fmtMs(p.longest_ms) : "—",
    "Lost / sent": `${p.total.lost.toLocaleString()} / ${p.total.sent.toLocaleString()}`,
    "Latency before → after": `${rtt(p.phases?.baseline)} → ${rtt(p.phases?.settle || p.phases?.upgrade)}`,
  }));
  if (!live) stats.forEach((s) => delete s.Now);
  const wins = sum.probes.flatMap((p) => (p.windows || []).filter((w) => w.kind === "outage").map((w) => ({ p, w }))).sort((a, b) => a.w.start - b.w.start);
  const worst = sum.worst_outage_ms;
  const early = sum.ended && sum.phase === "baseline";
  const head = early ? `<div class="banner info" style="margin:0 0 8px">Monitoring stopped during the baseline because the upgrade stopped before anything was installed.${sum.still_down?.length ? ` These probes were already failing: ${esc(sum.still_down.join(", "))}. The device was struggling before any upgrade step, so look at its management plane first.` : ""}</div>`
    : sum.still_down?.length ? `<div class="banner crit" style="margin:0 0 8px">Still down: ${esc(sum.still_down.join(", "))}</div>`
    : `<div class="ptiles"><div class="tile ${sum.total_outages ? "warn" : "good"}"><div class="n">${sum.total_outages}</div><div class="l">Outage${sum.total_outages === 1 ? "" : "s"}</div></div>
       <div class="tile ${worst ? "warn" : "good"}"><div class="n">${worst ? fmtMs(worst) : "0"}</div><div class="l">Longest outage</div></div>
       <div class="tile"><div class="n">${fmtMs(t1 - t0)}</div><div class="l">${live ? "Monitoring for" : "Monitored"}</div></div></div>`;
  return `<div class="panel probe-panel"><div class="ph"><span class="ttl">${icon("pulse")} Service availability</span><span class="tools hint">${live ? `<span class="spin"></span> probing · ${esc(sum.phase === "upgrade" ? "upgrade in progress" : sum.phase === "settle" ? "confirming services are stable" : "measuring baseline")}` : `${esc(clockMs(t0))} → ${esc(clockMs(t1))}`}</span></div>
    <div class="pb">${head}
      <div class="ptimeline"><div class="prow paxis"><div class="pname"></div><div class="pphases">${phases}</div></div>${rows}
        <div class="prow"><div class="pname"></div><div class="paxis-t"><span>${esc(clockMs(t0).slice(0, 8))}</span><span>${esc(clockMs(t1).slice(0, 8))}</span></div></div></div>
      <div class="hint plegend"><i class="pb-ok"></i>no loss <i class="pb-loss"></i>some loss <i class="pb-out"></i>outage (≥ ${fmtMs(sum.min_outage_ms)}) <i class="pm"></i>upgrade event (hover for details)</div></div>
    <div class="pb flush">${table(stats)}</div>
    ${wins.length ? `<details class="sec" style="margin:8px 12px 12px" ${opts.openWindows ? "open" : ""}><summary>Outage windows <span class="hint">${wins.length}</span></summary><div class="pb flush">${table(wins.map(({ p, w }) => ({
      Probe: p.name, "Down from": clockMs(w.start), "Back at": w.ongoing ? "still down" : clockMs(w.end), Duration: fmtMs(w.duration_ms), "Failed probes": String(w.fails),
      During: (w.during_src ? w.during_src + ": " : "") + (w.during || w.phase), Errors: Object.entries(w.errors || {}).map(([k, v]) => `${k || "no reply"} ×${v}`).join(", ") })))}</div></details>` : ""}
  </div>`;
}
