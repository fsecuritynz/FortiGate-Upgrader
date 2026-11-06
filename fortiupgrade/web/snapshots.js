"use strict";
/* Snapshots & Diff: every data collection, sortable by date and hostname. Opening one shows it
   side by side with its before/after partner, deltas highlighted, under a delta summary tile. */

const SNAP_COLS = [
  ["taken_at", "Date"], ["hostname", "Hostname"], ["target", "Device"], ["label", "Collection"], ["version", "Firmware"],
];
const snapWhen = (s) => String(s || "").replace("T", " ");
const snapLabel = (l) => l === "pre-upgrade" ? badge("Before upgrade", "info") : l === "post-upgrade" ? badge("After upgrade", "up") : `<span>${esc(l || "")}</span>`;

async function pSnapshots(arg) {
  if (arg && arg.a && arg.b) return snapCompare(arg);
  const c = page("Snapshots & Diff", "Pre/post upgrade data collection", `<button id="fold">${icon("folder")}Open folder</button>`);
  const st = S.snapList || (S.snapList = { dev: "", kind: "", q: "", sort: "taken_at", dir: -1 });
  c.innerHTML = `<div class="panel"><div class="toolbar">
      <select id="sDev"><option value="">All devices</option>${S.targets.map((t) => `<option value="${t.id}" ${t.id === st.dev ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select>
      <select id="sKind"><option value="">All collections</option><option value="upgrade" ${st.kind === "upgrade" ? "selected" : ""}>Upgrades (before and after)</option><option value="manual" ${st.kind === "manual" ? "selected" : ""}>Manual</option></select>
      <input id="sQ" placeholder="Search" value="${esc(st.q)}" style="width:180px">
      <span class="grow"></span><input id="lbl" value="manual" style="width:110px" title="Label for a new collection"><button class="primary" id="take">${icon("play")}Collect now</button></div>
    <div id="sj"></div><div id="sList"><div class="empty"><span class="spin"></span></div></div></div>
    <div class="hint">Open a collection to see it side by side with the one it pairs with: an upgrade's before and after collections, or otherwise the next collection of the same unit.</div>`;
  $("#fold").onclick = () => api("/open-folder", st.dev ? { body: { kind: "snapshots", tid: st.dev } } : { body: { kind: "data" } }).catch((e) => toast(e.message));
  let all = [];
  const draw = () => {
    const q = st.q.toLowerCase();
    let rows = all.filter((s) => (!st.dev || s.target_id === st.dev)
      && (!st.kind || (st.kind === "upgrade" ? /^(pre|post)-upgrade$/.test(s.label) : !/^(pre|post)-upgrade$/.test(s.label)))
      && (!q || [s.hostname, s.target, s.file, s.label, s.version, s.model].join(" ").toLowerCase().includes(q)));
    const k = st.sort;
    rows = rows.slice().sort((a, b) => {
      const r = k === "version" ? vcmp(a.version, b.version) : String(a[k] || "").localeCompare(String(b[k] || ""), undefined, { numeric: true, sensitivity: "base" });
      return (r || String(a.taken_at).localeCompare(String(b.taken_at))) * st.dir;
    });
    $("#take").disabled = !st.dev;
    $("#take").title = st.dev ? "" : "Choose a device first";
    if (!rows.length) { $("#sList").innerHTML = `<div class="empty">${all.length ? "No collections match." : "No data collected yet. Choose a device and click <b>Collect now</b>, or run an upgrade with pre/post checks."}</div>`; return; }
    const byId = Object.fromEntries(all.map((s) => [s.target_id + "/" + s.id, s]));
    $("#sList").innerHTML = `<div class="tablewrap"><table class="snaps"><thead><tr>${SNAP_COLS.map(([key, name]) => `<th class="sortable ${st.sort === key ? "sorted" : ""}" data-k="${key}">${name}<span class="sarr">${st.sort === key ? (st.dir > 0 ? "▲" : "▼") : ""}</span></th>`).join("")}<th>Paired with</th><th></th></tr></thead>
      <tbody>${rows.map((s) => { const p = s.pair && byId[s.target_id + "/" + s.pair];
        return `<tr class="click" data-o="${esc(s.target_id)}|${esc(s.id)}"><td class="nowrap">${esc(snapWhen(s.taken_at))}</td><td><b>${esc(s.hostname)}</b>${s.member && s.member !== s.hostname ? ` <span class="hint">${esc(s.member)}</span>` : ""}<div class="hint mono fname">${esc(s.file)}</div></td>
        <td>${esc(s.target)}</td><td>${snapLabel(s.label)}</td><td class="nowrap"><span class="chip ver">v${esc(s.version)}</span> <span class="hint">b${esc(s.build ?? "")}</span></td>
        <td class="hint nowrap">${p ? `${p.label === "post-upgrade" ? "After" : "Before"} · ${esc(snapWhen(p.taken_at))}` : "—"}</td>
        <td class="nowrap"><button class="small primary" data-open="${esc(s.target_id)}|${esc(s.id)}">Open</button> <button class="small" data-l="${esc(s.target_id)}|${esc(s.id)}">.log</button></td></tr>`; }).join("")}</tbody></table></div>`;
    $$("th.sortable", $("#sList")).forEach((th) => th.onclick = () => { const key = th.dataset.k; st.dir = st.sort === key ? -st.dir : (key === "taken_at" ? -1 : 1); st.sort = key; draw(); });
    $$("[data-l]", $("#sList")).forEach((b) => b.onclick = async (e) => { e.stopPropagation(); const [tid, sid] = b.dataset.l.split("|"); const txt = await api(`/targets/${tid}/snapshots/${encodeURIComponent(sid)}/log`); modal(sid + ".log", `<pre style="max-height:70vh">${esc(txt)}</pre>`); });
    $$("tr[data-o]", $("#sList")).forEach((tr) => tr.onclick = () => { const [tid, sid] = tr.dataset.o.split("|"); snapOpen(all, tid, sid); });
  };
  const load = async () => { all = await api("/snapshots"); draw(); };
  $("#sDev").onchange = (e) => { st.dev = e.target.value; draw(); };
  $("#sKind").onchange = (e) => { st.kind = e.target.value; draw(); };
  let tmr; $("#sQ").oninput = (e) => { clearTimeout(tmr); tmr = setTimeout(() => { st.q = e.target.value; draw(); }, 150); };
  $("#take").onclick = async () => {
    const t = S.targets.find((x) => x.id === st.dev); if (!t) return;
    const j = await api(`/targets/${t.id}/snapshot`, { body: { label: $("#lbl").value, member_ids: t.members.map((m) => m.id) } });
    watchJob(j.id, $("#sj"), { onDone: load });
  };
  await load();
}

// Before = the older collection, after = the newer one. An upgrade's collections open as their pair.
function snapOpen(all, tid, sid) {
  const s = all.find((x) => x.target_id === tid && x.id === sid);
  const same = all.filter((x) => x.target_id === tid && x.id !== sid && (x.member_id || "") === (s.member_id || ""))
    .sort((a, b) => String(a.taken_at).localeCompare(String(b.taken_at)));
  let other = s.pair && all.find((x) => x.target_id === tid && x.id === s.pair);
  if (!other) {
    const newer = same.filter((x) => x.taken_at > s.taken_at), older = same.filter((x) => x.taken_at <= s.taken_at);
    other = s.label === "post-upgrade" ? (older.pop() || newer[0]) : (newer[0] || older.pop());
  }
  if (!other) return snapModal(tid, sid);
  const [a, b] = other.taken_at < s.taken_at ? [other, s] : [s, other];
  go("snapshots", { tid, a: a.id, b: b.id });
}

// intra-line highlight: the part between the common prefix and suffix of a changed line pair
function lineDelta(x, y) {
  if (x == null || y == null) return [x == null ? "" : esc(x), y == null ? "" : esc(y)];
  let p = 0; while (p < x.length && p < y.length && x[p] === y[p]) p++;
  let s = 0; while (s < x.length - p && s < y.length - p && x[x.length - 1 - s] === y[y.length - 1 - s]) s++;
  const mk = (t) => { const mid = t.slice(p, t.length - s); return esc(t.slice(0, p)) + (mid ? `<mark class="dl">${esc(mid)}</mark>` : "") + esc(t.slice(t.length - s)); };
  return [mk(x), mk(y)];
}

async function snapCompare(arg) {
  const st = S.snapCmp && S.snapCmp.tid === arg.tid ? Object.assign(S.snapCmp, arg) : (S.snapCmp = { full: false, ...arg });
  const t = S.targets.find((x) => x.id === st.tid);
  const c = page("Before and after", `Snapshots & Diff › ${t ? t.name : st.tid}`,
    `<button id="back">${icon("snapshots")}All collections</button><button id="swap" title="Swap before and after">⇄ Swap</button>`);
  $("#back").onclick = () => go("snapshots");
  $("#swap").onclick = () => go("snapshots", { tid: st.tid, a: st.b, b: st.a });
  c.innerHTML = '<div class="empty"><span class="spin"></span> Comparing…</div>';
  let d, list;
  try {
    [d, list] = await Promise.all([api(`/targets/${st.tid}/compare?a=${encodeURIComponent(st.a)}&b=${encodeURIComponent(st.b)}${st.full ? "&full=true" : ""}`), api(`/targets/${st.tid}/snapshots`)]);
  } catch (e) { c.innerHTML = `<div class="banner crit">${esc(e.message)}</div>`; return; }
  const pre = d.pre, post = d.post, cs = d.config_summary;
  const opts = (sel) => list.map((s) => `<option value="${esc(s.id)}" ${s.id === sel ? "selected" : ""}>${esc(snapWhen(s.taken_at))} · ${esc(s.label)} · v${esc(s.device.version)}</option>`).join("");
  const sumRows = d.summary;
  const changedChecks = sumRows.filter((r) => r.status !== "same").length;
  const deltas = (r) => [r.critical ? badge(`${r.critical} critical`, "down") : "", r.warning ? badge(`${r.warning} warning`, "warn") : "", r.info ? badge(`${r.info} info`, "info") : "",
    r.lines ? badge(`${r.lines} line${r.lines > 1 ? "s" : ""}`, "info") : ""].filter(Boolean).join(" ") || (r.status === "missing" ? badge("Only in one", "warn") : r.status === "changed" ? badge("Changed", "warn") : '<span class="hint">No change</span>');
  const head = (s, side) => `<div class="sbs-h ${side}"><div class="sbs-t">${side === "a" ? "Before" : "After"}</div>
    <select data-side="${side}">${opts(side === "a" ? st.a : st.b)}</select>
    <div class="hint">${esc(s.device.hostname)} · ${esc(s.device.model)} · <span class="chip ver">v${esc(s.device.version)}</span> b${esc(s.device.build)} · ${esc(s.file || s.id)}</div></div>`;
  c.innerHTML = `
    <div class="panel"><div class="ph"><span class="ttl">${icon("snapshots")} Delta summary</span><span class="hint">${esc(pre.device.hostname)} · ${esc(snapWhen(pre.taken_at))} → ${esc(snapWhen(post.taken_at))}</span></div>
      <div class="tiles" style="border-bottom:1px solid var(--rule)">
        <div class="tile fw"><div class="n"><span class="chip ver">v${esc(pre.device.version)}</span> → <span class="chip ver">v${esc(post.device.version)}</span></div><div class="l">Firmware</div></div>
        <div class="tile ${d.totals.critical ? "bad" : ""}"><div class="n">${d.totals.critical}</div><div class="l">Critical</div></div>
        <div class="tile ${d.totals.warning ? "warn" : ""}"><div class="n">${d.totals.warning}</div><div class="l">Warning</div></div>
        <div class="tile"><div class="n">${d.totals.info}</div><div class="l">Informational</div></div>
        <div class="tile ${changedChecks ? "warn" : "good"}"><div class="n">${changedChecks}/${sumRows.length}</div><div class="l">Checks changed</div></div>
        ${cs ? `<div class="tile ${cs.removed || cs.changed ? "warn" : ""}"><div class="n">${cs.changed + cs.removed + cs.added}</div><div class="l">Config settings changed</div></div>` : ""}
        <div class="tile"><div class="n">${d.changed_lines}</div><div class="l">Lines differing</div></div>
      </div>
      <div class="tablewrap" style="max-height:340px"><table class="dsum"><thead><tr><th>Check</th><th>Scope</th><th>Before</th><th>After</th><th>Deltas</th></tr></thead><tbody>
        ${sumRows.map((r, i) => `<tr class="click ${r.status === "same" ? "dim" : ""}" data-sec="${i}"><td><b>${esc(r.name)}</b></td><td class="nowrap">${esc(scopeName(r.scope))}</td>
          <td class="${r.status !== "same" ? "before" : ""}">${esc(r.before ?? "—")}</td><td class="${r.status !== "same" ? "after" : ""}">${esc(r.after ?? "—")}</td><td class="nowrap">${deltas(r)}</td></tr>`).join("")}
        ${cs ? `<tr class="click ${cs.added + cs.removed + cs.changed ? "" : "dim"}" data-sec="cfg"><td><b>Configuration</b></td><td>Global</td><td colspan="2">${cs.changed} changed · ${cs.added} added · ${cs.removed} removed</td>
          <td class="nowrap">${cs.removed ? badge(`${cs.removed} removed`, "down") : ""} ${cs.changed ? badge(`${cs.changed} changed`, "warn") : ""} ${cs.added ? badge(`${cs.added} added`, "up") : ""}${cs.added + cs.removed + cs.changed ? "" : '<span class="hint">No change</span>'}</td></tr>` : ""}
      </tbody></table></div>
      ${d.config_note ? `<div class="pb"><div class="banner warn" style="margin:0">${esc(d.config_note)}</div></div>` : ""}
    </div>
    <div class="panel sbs"><div class="sbs-heads">${head(pre, "a")}${head(post, "b")}</div>
      <div class="toolbar"><div class="seg" id="sbsMode"><button data-f="0" class="${st.full ? "" : "on"}">Changes only</button><button data-f="1" class="${st.full ? "on" : ""}">Full file</button></div>
        <button class="small" id="dPrev">▲ Previous change</button><button class="small" id="dNext">▼ Next change</button><span class="hint" id="dPos"></span>
        <span class="grow"></span><span class="hint">Matched ignoring clocks, dates and uptimes · secrets masked</span></div>
      <div class="code diff sbs-body" id="sbsBody">${d.rows.length ? `<table class="split">${d.rows.map((r, i) => {
        if (r.t === "gap") return `<tr class="gap"><td colspan="4">⋯ unchanged lines ⋯</td></tr>`;
        const [ax, bx] = r.t === "chg" ? lineDelta(r.a?.[1], r.b?.[1]) : [r.a ? esc(r.a[1]) : "", r.b ? esc(r.b[1]) : ""];
        return `<tr class="${r.t}" data-i="${i}"><td class="ln">${r.a ? r.a[0] : ""}</td><td class="cx ${r.a && r.t !== "same" ? "old" : ""}">${ax}</td><td class="ln">${r.b ? r.b[0] : ""}</td><td class="cx ${r.b && r.t !== "same" ? "new" : ""}">${bx}</td></tr>`; }).join("")}</table>`
        : '<div class="empty">The two files are identical.</div>'}</div></div>`;
  $$(".sbs-h select", c).forEach((sel) => sel.onchange = () => go("snapshots", { tid: st.tid, a: sel.dataset.side === "a" ? sel.value : st.a, b: sel.dataset.side === "b" ? sel.value : st.b }));
  $$("#sbsMode button", c).forEach((b) => b.onclick = () => { st.full = b.dataset.f === "1"; snapCompare({ tid: st.tid, a: st.a, b: st.b }); });
  const body = $("#sbsBody", c);
  // start of each run of changed rows
  const trs = $$("tr[data-i]", body);
  const starts = trs.filter((tr, i) => tr.className !== "same" && (i === 0 || trs[i - 1].className === "same" || tr.previousElementSibling?.classList.contains("gap")));
  let cur = -1;
  const show = (el) => { body.scrollTop += el.getBoundingClientRect().top - body.getBoundingClientRect().top - 60; el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1200); };
  const pos = () => { $("#dPos", c).textContent = starts.length ? `${cur + 1 > 0 ? cur + 1 : "–"} of ${starts.length} change${starts.length > 1 ? "s" : ""}` : "no changes"; };
  $("#dNext", c).onclick = () => { if (!starts.length) return; body.scrollIntoView({ block: "start" }); cur = (cur + 1) % starts.length; show(starts[cur]); pos(); };
  $("#dPrev", c).onclick = () => { if (!starts.length) return; body.scrollIntoView({ block: "start" }); cur = (cur - 1 + starts.length) % starts.length; show(starts[cur]); pos(); };
  pos();
  // summary row -> that section in the files
  $$("tr[data-sec]", c).forEach((tr) => tr.onclick = () => {
    const r = tr.dataset.sec === "cfg" ? null : sumRows[Number(tr.dataset.sec)];
    const title = r ? `${r.name}  [${r.scope === "global" ? "Global" : "VDOM " + r.scope}]` : "Configuration backup";
    const hit = trs.find((x) => { const i = Number(x.dataset.i); const row = d.rows[i]; return (row.a && row.a[1] === title) || (row.b && row.b[1] === title); });
    if (hit) { body.scrollIntoView({ behavior: "smooth", block: "start" }); show(hit); return; }
    // the section has no changed lines, so it isn't in the "changes only" view
    if (!st.full) { st.full = true; st.jump = title; snapCompare({ tid: st.tid, a: st.a, b: st.b }); }
  });
  if (st.jump) {
    const title = st.jump; st.jump = null;
    const hit = trs.find((x) => { const row = d.rows[Number(x.dataset.i)]; return (row.a && row.a[1] === title) || (row.b && row.b[1] === title); });
    if (hit) { body.scrollIntoView({ block: "start" }); show(hit); }
  }
}
