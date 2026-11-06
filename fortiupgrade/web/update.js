"use strict";
/* Software updates: badge under the version (bottom left), the update prompt, and Settings > Software updates. */

const UPD = { st: null, prompted: null, timer: null };
const vlabel = (v, b) => `v${esc(v)}${b ? ` build${String(b).padStart(4, "0")}` : ""}`;

async function updPoll(refresh = false) {
  try { UPD.st = await api(`/update${refresh ? "?refresh=true" : ""}`); } catch { return UPD.st; }
  updBadge();
  const st = UPD.st;
  if (st.available && !st.skipped && UPD.prompted !== st.latest.version) { UPD.prompted = st.latest.version; updModal(); }
  return st;
}

function updStart() {
  updPoll();
  setTimeout(() => updPoll(), 12000);           // the background check runs a few seconds after start-up
  setInterval(() => updPoll(), 10 * 60 * 1000);  // and every 6 hours on the server; this only reads its result
}

function updBadge() {
  const nv = $("#navVer"); if (!nv) return;
  $(".nv-upd", nv)?.remove();
  const st = UPD.st;
  if (st?.available) nv.insertAdjacentHTML("beforeend", `<span class="nv-upd" title="Click to update">${icon("upgrade")} Update available: v${esc(st.latest.version)}</span>`);
  nv.onclick = (e) => { e.preventDefault(); if (UPD.st?.available) updModal(); else go("changelog"); };
}

function notesHtml(md) {
  // release notes are plain markdown from changelog.json: headings, bullets and **bold**
  return String(md || "").split("\n").map((l) => {
    const t = esc(l.trim()).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
    if (!t) return "";
    if (/^#{1,6} /.test(l.trim())) return `<div class="un-h">${t.replace(/^#+ /, "")}</div>`;
    if (/^[-*] /.test(l.trim())) return `<div class="un-li">• ${t.slice(2)}</div>`;
    return `<div>${t}</div>`;
  }).join("");
}

function updModal() {
  const st = UPD.st; if (!st?.latest) return;
  const lt = st.latest;
  const f = modal(`Update available: FortiGate Upgrader ${vlabel(lt.version, lt.build)}`, `
    <p style="margin-top:0">You have ${vlabel(st.current.version, st.current.build)}. ${lt.published ? `Released ${esc(lt.published.slice(0, 10))}.` : ""}</p>
    <div class="upd-notes">${notesHtml(lt.notes) || '<span class="hint">No release notes.</span>'}</div>
    <div id="updBody" style="margin-top:12px"></div>`,
    `<button value="cancel" id="updLater">Later</button><button type="button" id="updSkip">Skip this version</button><button type="button" class="primary" id="updGo">Download and install</button>`);
  const body = $("#updBody", f), go_ = $("#updGo", f);
  const drawDl = (s) => {
    const d = s.download;
    if (!d) { body.innerHTML = st.can_install ? `<div class="hint">${esc(lt.asset.name)} · ${fmtSize(lt.asset.size)} from GitHub. The download is checked against the release's SHA-256 checksum before anything is installed.</div>`
      : `<div class="banner warn">There's no download for this platform (${esc(st.platform || "unknown")}) in this release. <a href="#" id="updPage">Open the releases page</a></div>`; return; }
    if (d.state === "downloading") {
      const pct = d.total ? Math.round(d.done / d.total * 100) : 0;
      body.innerHTML = `<div class="row"><span class="spin"></span> Downloading ${esc(d.name)} · ${fmtSize(d.done)}${d.total ? ` of ${fmtSize(d.total)}` : ""}</div><div class="bar" style="margin-top:6px"><i style="width:${pct}%"></i></div>`;
      go_.disabled = true; go_.textContent = "Downloading…";
    } else if (d.state === "ready") {
      body.innerHTML = `<div class="banner ok">Downloaded and verified (SHA-256 ${esc(d.sha256.slice(0, 16))}…). ${st.packaged ? "Restart to install: the app closes, updates itself and reopens. Running upgrades are stopped, so wait until none are running." : "You're running from source, so the installer will just be opened."}</div>`;
      go_.disabled = false; go_.textContent = st.packaged ? "Restart and install" : "Open installer";
    } else if (d.state === "failed") {
      body.innerHTML = `<div class="banner crit">Download failed: ${esc(d.error)}</div>`;
      go_.disabled = false; go_.textContent = "Try again";
    }
  };
  drawDl(st);
  const pg = $("#updPage", f); if (pg) pg.onclick = (e) => { e.preventDefault(); api("/open-url", { body: { url: st.releases_url } }); };
  if (!st.can_install) go_.disabled = true;
  let poll = null;
  const follow = () => { clearInterval(poll); poll = setInterval(async () => {
    const s = await api("/update"); UPD.st = s; drawDl(s);
    if (!s.download || s.download.state !== "downloading") clearInterval(poll); }, 700); };
  go_.onclick = async () => {
    const d = UPD.st.download;
    try {
      if (d && d.state === "ready" && d.version === lt.version) {
        if (document.querySelector(".top-pill:not([hidden])") && !confirm("Jobs are running. Installing now closes the app and stops them. Install anyway?")) return;
        const r = await api("/update/install", { body: {} });
        body.innerHTML = `<div class="banner info"><span class="spin"></span> ${esc(r.message)}</div>`; go_.disabled = true;
      } else { UPD.st = await api("/update/download", { body: {} }); drawDl(UPD.st); follow(); }
    } catch (e) { toast(e.message); }
  };
  $("#updSkip", f).onclick = async () => { await api("/update/prefs", { method: "PUT", body: { skip: lt.version } }); $("#modal").close(); updPoll(); };
  if (st.download?.state === "downloading") follow();
}

// Settings > Software updates
async function updSettingsPanel(el) {
  const draw = (st) => {
    const lt = st.latest;
    el.innerHTML = `<div class="panel"><div class="ph"><span class="ttl">${icon("upgrade")} Software updates</span><span class="tools"><button class="small" id="updCheck">${icon("refresh")}Check now</button></span></div><div class="kv">
      <div>Installed</div><div>FortiGate Upgrader ${vlabel(st.current.version, st.current.build)}</div>
      <div>Latest release</div><div>${lt ? `${vlabel(lt.version, lt.build)} ${st.available ? badge("Update available", "warn") + ` <button class="small primary" id="updOpen">Update…</button>` : badge("Up to date", "up")}` : `<span class="hint">${st.error ? esc(st.error) : "not checked yet"}</span>`}</div>
      <div>Last checked</div><div>${st.checked_at ? esc(new Date(st.checked_at * 1000).toLocaleString()) : "—"}${st.error && lt ? ` <span class="hint">(last attempt failed: ${esc(st.error)})</span>` : ""}</div>
      <div>Automatic checks</div><div><label class="cb"><input type="checkbox" id="updAuto" ${st.auto ? "checked" : ""}> Check GitHub for new releases at start-up and every 6 hours</label></div>
      <div>Source</div><div><a href="#" id="updRel">github.com/fsecuritynz/FortiGate-Upgrader/releases</a> <span class="hint">· only the release list and the download are fetched; nothing is sent about you or your devices</span></div>
    </div></div>`;
    $("#updCheck", el).onclick = async () => { $("#updCheck", el).disabled = true; draw(await updPoll(true) || st); };
    $("#updAuto", el).onchange = async (e) => draw(await api("/update/prefs", { method: "PUT", body: { auto: e.target.checked } }));
    $("#updRel", el).onclick = (e) => { e.preventDefault(); api("/open-url", { body: { url: st.releases_url } }); };
    const o = $("#updOpen", el); if (o) o.onclick = () => updModal();
  };
  draw(UPD.st || await api("/update"));
}
