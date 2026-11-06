"use strict";
/* Upgrade wizard step: configuration and log review. Looks for things that can make an HA upgrade
   fail or cause problems afterwards (unlicensed features in use, removed features, compatibility,
   HA settings, resources) and for anything bad already happening in the logs. */

const rvKey = (W) => `${[...W.sel].sort().join(",")}|${W.version}|${W.source}`;
const RV_SEV = { critical: ["Critical", "down"], warning: ["Warning", "warn"], info: ["Info", "info"] };
const rvSev = (s) => badge(...(RV_SEV[s] || [s, "grey"]));

function wHealth(W, body, foot, nav, go_, c) {
  if (W.rv && W.rvKey !== rvKey(W)) { W.rv = null; W.rvAck = false; }
  if (!W.rv) {
    body.innerHTML = `<h2>Configuration and log review</h2>
      <p class="hint">Read-only. Each unit's configuration, HA settings, licences, resources and recent event logs are checked for anything
      that could make the HA upgrade fail or cause problems after upgrading to v${esc(W.version)}: features in use without a licence,
      features removed or changed in the target, FortiAnalyzer/FortiManager/FortiSwitch/FortiAP compatibility, and errors, crashes,
      failovers or instability already in the logs.</p><div id="rvc"></div>`;
    nav(true, true, "Reviewing…");
    $("#wNext").disabled = true;
    const con = makeConsole($("#rvc"), { title: "Configuration and log review", file: "review" });
    api("/review", { body: { target_ids: [...W.sel], target_version: W.version, source: W.source, ha_modes: W.ha } }).then((j) => {
      W.rvJob = j.id;
      streamJob(j.id, con, (job) => {
        if (job.status !== "running" && job.result) { W.rv = job.result; W.rvKey = rvKey(W); W.rvAck = false; drawWizard(c); }
        else if (job.status === "failed") body.insertAdjacentHTML("beforeend", `<div class="banner crit">${esc(job.error)}</div>`);
      });
    }).catch((e) => body.insertAdjacentHTML("beforeend", `<div class="banner crit">${esc(e.message)}</div>`));
    return;
  }
  const res = W.rv.results || [];
  const all = res.flatMap((r) => (r.result?.findings || []).map((f) => ({ ...f, target: r.target })));
  const tot = (s) => all.filter((f) => f.severity === s).length;
  const crit = res.filter((r) => !r.result || r.result.findings.some((f) => f.severity === "critical"));
  body.innerHTML = `<h2>Configuration and log review</h2>
    <div class="panel"><div class="tiles">
      <div class="tile ${tot("critical") ? "bad" : "good"}"><div class="n">${tot("critical")}</div><div class="l">Critical</div></div>
      <div class="tile ${tot("warning") ? "warn" : ""}"><div class="n">${tot("warning")}</div><div class="l">Warning</div></div>
      <div class="tile"><div class="n">${tot("info")}</div><div class="l">Informational</div></div>
      <div class="tile"><div class="n">${res.reduce((a, r) => a + (r.result?.logs || []).reduce((b, l) => b + l.read, 0), 0).toLocaleString()}</div><div class="l">Log events reviewed</div></div>
    </div></div>
    ${res.map((r) => rvDevice(r)).join("")}
    ${crit.length ? `<div class="banner crit"><b>Critical findings on ${crit.map((r) => esc(r.target)).join(", ")}.</b> These are likely to make the upgrade fail or cause an outage afterwards.
      <label class="cb" style="margin-top:6px"><input type="checkbox" id="rvAck" ${W.rvAck ? "checked" : ""}> I have reviewed the critical findings and want to continue</label></div>`
      : `<div class="banner ok">No critical findings. Review the warnings, then continue to the pre-flight checks.</div>`}`;
  nav(true, true, "Next", `<button id="rvAgain">${icon("refresh")}Review again</button>`);
  const nb = $("#wNext");
  const gate = () => { nb.disabled = crit.length > 0 && !W.rvAck; };
  const ack = $("#rvAck"); if (ack) ack.onchange = () => { W.rvAck = ack.checked; gate(); };
  gate();
  $("#rvAgain").onclick = () => { W.rv = null; drawWizard(c); };
  nb.onclick = () => go_(5);
  $$("[data-rvf]", body).forEach((b) => b.onclick = () => {
    const box = b.closest(".rv-dev"); const k = b.dataset.rvf;
    $$("[data-rvf]", box).forEach((x) => x.classList.toggle("on", x === b));
    $$("tr[data-sev]", box).forEach((tr) => tr.hidden = k === "issues" && tr.dataset.sev === "info");
  });
}

function rvDevice(r) {
  if (!r.result) return `<div class="panel"><div class="ph"><span class="ttl">${esc(r.target)}</span>${badge("Review failed", "down")}</div><div class="pb">${esc(r.error || "")}</div></div>`;
  const x = r.result, f = x.findings;
  const cats = {};
  f.forEach((y) => { const c = (cats[y.category] ||= { n: 0, worst: "info" }); c.n++; if ((RV_ORDER[y.severity] ?? 3) < (RV_ORDER[c.worst] ?? 3)) c.worst = y.severity; });
  const members = x.members.map((m) => m.ok ? `${esc(m.member)} <span class="hint">${esc(m.model || "")} v${esc(m.version || "")}${m.ha_mode && m.ha_mode !== "standalone" ? " · HA " + esc(m.ha_mode) : ""}</span>` : `${esc(m.member)} ${badge("unreachable", "down")}`).join(" · ");
  const ev = (y) => y.evidence?.length ? `<details class="rv-ev"><summary>Evidence (${y.evidence.length})</summary><pre>${esc(y.evidence.join("\n"))}</pre></details>` : "";
  return `<div class="panel rv-dev"><div class="ph"><span class="ttl">${esc(r.target)}</span><span class="hint">${members}</span>
      <span class="tools">${x.totals.critical ? badge(x.totals.critical + " critical", "down") : ""} ${x.totals.warning ? badge(x.totals.warning + " warning", "warn") : ""} ${!x.totals.critical && !x.totals.warning ? badge("No issues", "up") : ""}</span></div>
    ${x.steps?.length > 1 ? `<div class="pb" style="padding-bottom:0"><span class="hint">Checked for the upgrade</span> <span class="path">${x.steps.map((s, k) => `${k ? '<span class="arrow">→</span>' : ""}<span class="v">${esc(s)}</span>`).join("")}</span></div>` : ""}
    <div class="rv-cats">${Object.entries(cats).map(([k, v]) => `<span class="rv-cat ${v.worst}">${esc(k)} <b>${v.n}</b></span>`).join("") || '<span class="hint">Nothing found</span>'}</div>
    ${f.length ? `<div class="toolbar"><div class="seg"><button data-rvf="issues" class="on">Critical and warnings</button><button data-rvf="all">All findings</button></div></div>
    <div class="tablewrap"><table class="rv-t"><thead><tr><th style="width:86px">Severity</th><th style="width:130px">Area</th><th>Finding</th><th>What to do</th></tr></thead><tbody>
      ${f.map((y) => `<tr data-sev="${y.severity}" ${y.severity === "info" ? "hidden" : ""}><td>${rvSev(y.severity)}</td><td>${esc(y.category)}${y.member && x.members.length > 1 ? `<div class="hint">${esc(y.member)}</div>` : ""}</td>
        <td><b>${esc(y.title)}</b>${y.detail ? `<div class="rv-d">${esc(y.detail)}</div>` : ""}${ev(y)}</td><td class="rv-a">${esc(y.advice || "")}</td></tr>`).join("")}
    </tbody></table></div>` : ""}
    ${(x.logs || []).map(rvLogs).join("")}
  </div>`;
}
const RV_ORDER = { critical: 0, warning: 1, info: 2 };

function rvLogs(l) {
  const lv = Object.entries(l.levels || {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${esc(k)} ${n}`).join(" · ");
  return `<details class="sec rv-logs" ${l.groups.some((g) => g.severity === "critical") ? "open" : ""}><summary><b>Logs: ${esc(l.member)}</b>
      <span class="hint">${l.read.toLocaleString()} event${l.read === 1 ? "" : "s"}${l.read ? ` from ${esc(l.oldest)} to ${esc(l.newest)}` : ""}${lv ? ` · ${lv}` : ""}</span>
      ${l.groups.filter((g) => g.severity !== "info").length ? badge(`${l.groups.filter((g) => g.severity !== "info").length} problem pattern(s)`, l.groups.some((g) => g.severity === "critical") ? "down" : "warn") : badge("Nothing bad found", "up")}</summary>
    <div>${l.groups.length ? `<table><thead><tr><th style="width:86px">Severity</th><th>Pattern</th><th style="width:60px">Events</th><th>When</th><th>Latest events</th></tr></thead><tbody>
      ${l.groups.map((g) => `<tr><td>${rvSev(g.severity)}</td><td><b>${esc(g.title)}</b><div class="hint">${esc(g.category)}</div></td><td>${g.count}</td><td class="nowrap hint">${g.first === g.last ? esc(g.last) : `${esc(g.first)}<br>to ${esc(g.last)}`}</td>
        <td><div class="rv-samples">${g.samples.map((s) => `<div><span class="hint">${esc(s.time)} · ${esc(s.level)}${s.subtype ? " · " + esc(s.subtype) : ""}</span> ${esc(s.msg || s.event)}</div>`).join("")}</div></td></tr>`).join("")}
      </tbody></table>` : '<div class="empty">No errors, crashes, failovers or instability in the recent event logs.</div>'}</div></details>`;
}
