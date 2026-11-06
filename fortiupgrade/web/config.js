"use strict";
/* Device > Configuration tab: config history viewer and diff of any two versions. */

const CURRENT = "__current__";
function cfgLabel(c) {
  if (!c) return "";
  const when = String(c.taken_at || "").replace("T", " ");
  const src = c.kind === "live" ? "fetched" : (c.label || "collection");
  return `${when} · ${src} · v${String(c.version || "").replace(/^v/, "")}`;
}
function hlConf(line) {
  if (/^\s*#/.test(line)) return `<span class="c-cm">${esc(line)}</span>`;
  const m = line.match(/^(\s*)(config|edit|set|unset|append|select|unselect|next|end)\b(\s*)(.*)$/);
  if (!m) return esc(line);
  const [, sp, kw, sp2, rest] = m;
  let key = "", tail = rest;
  if (/^(set|unset|append|select|unselect)$/.test(kw)) { const k = rest.match(/^(\S+)(.*)$/); if (k) { key = `<span class="c-key">${esc(k[1])}</span>`; tail = k[2]; } }
  let t = esc(tail).replace(/(&quot;.*?&quot;)/g, '<span class="c-str">$1</span>')
    .replace(/ENC &lt;redacted&gt;|&lt;pem redacted&gt;|ENC [A-Za-z0-9+/=]{12,}/g, (x) => `<span class="c-secret">${x}</span>`);
  if (kw === "config") t = `<span class="c-sec">${t}</span>`;
  return `${sp}<span class="c-kw c-${kw}">${kw}</span>${sp2}${key}${t}`;
}

async function devConfig(t, c) {
  const st = S.cfgState && S.cfgState.tid === t.id ? S.cfgState : (S.cfgState = { tid: t.id, member: t.members[0].id, mode: "view", ref: null, a: null, b: null, dview: "split", raw: false });
  c.innerHTML = '<div class="empty"><span class="spin"></span> Loading configuration history…</div>';
  let all = await api(`/targets/${t.id}/configs`);
  const mine = () => all.filter((x) => !x.member_id || x.member_id === st.member);
  const fetchCurrent = async (btn) => {
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Reading the running configuration…'; }
    try {
      const r = await api(`/targets/${t.id}/configs/fetch`, { body: { member_id: st.member } });
      all = await api(`/targets/${t.id}/configs`);
      return r;
    } catch (e) { toast(e.message); return null; } finally { if (btn) { btn.disabled = false; btn.innerHTML = `${icon("refresh")}Fetch current config`; } }
  };
  const draw = () => {
    const list = mine();
    if (!st.ref || !list.some((x) => x.ref === st.ref)) st.ref = list[0]?.ref || null;
    const opts = (sel, extra = "") => extra + list.map((x) => `<option value="${esc(x.ref)}" ${x.ref === sel ? "selected" : ""}>${esc(cfgLabel(x))}</option>`).join("");
    c.innerHTML = `<div class="panel"><div class="toolbar">
        ${t.members.length > 1 ? `<select id="cfM">${t.members.map((m) => `<option value="${m.id}" ${m.id === st.member ? "selected" : ""}>${esc(m.name || m.host)}</option>`).join("")}</select>` : ""}
        <div class="seg" id="cfMode"><button data-m="view" class="${st.mode === "view" ? "on" : ""}">View</button><button data-m="diff" class="${st.mode === "diff" ? "on" : ""}">Compare</button></div>
        ${st.mode === "view" ? `<span class="hint">Version</span><select id="cfRef" style="max-width:420px">${list.length ? opts(st.ref) : "<option>No configuration stored yet</option>"}</select>`
          : `<span class="hint">From</span><select id="cfA" style="max-width:330px">${opts(st.a, `<option value="${CURRENT}" ${st.a === CURRENT ? "selected" : ""}>Current (read from the device now)</option>`)}</select>
             <button class="small" id="cfSwap" title="Swap">⇄</button>
             <span class="hint">To</span><select id="cfB" style="max-width:330px">${opts(st.b, `<option value="${CURRENT}" ${st.b === CURRENT ? "selected" : ""}>Current (read from the device now)</option>`)}</select>
             <button class="primary" id="cfGo">${icon("snapshots")}Compare</button>`}
        <span class="grow"></span><button id="cfFetch">${icon("refresh")}Fetch current config</button></div>
      <div id="cfBody"></div></div>`;
    const cm = $("#cfM"); if (cm) cm.onchange = () => { st.member = cm.value; st.ref = st.a = st.b = null; draw(); };
    $$("#cfMode button").forEach((b) => b.onclick = () => { st.mode = b.dataset.m; draw(); });
    $("#cfFetch").onclick = async (e) => { const r = await fetchCurrent(e.currentTarget); if (r) { st.ref = r.ref; if (r.unchanged) toast(`The running configuration is identical to the stored version from ${String(r.taken_at).replace("T", " ")}.`); draw(); } };
    if (st.mode === "view") {
      if (!list.length) { $("#cfBody").innerHTML = '<div class="empty">No configuration has been stored for this device yet. Click <b>Fetch current config</b> (read-only), or run a data collection.</div>'; return; }
      $("#cfRef").onchange = (e) => { st.ref = e.target.value; draw(); };
      viewConfig(t, st, list.find((x) => x.ref === st.ref), $("#cfBody"));
    } else {
      if (!st.b) st.b = list[0]?.ref || CURRENT;
      if (!st.a) st.a = list.find((x) => x.ref !== st.b)?.ref || CURRENT;
      $("#cfA").value = st.a; $("#cfB").value = st.b;
      $("#cfA").onchange = (e) => { st.a = e.target.value; };
      $("#cfB").onchange = (e) => { st.b = e.target.value; };
      $("#cfSwap").onclick = () => { [st.a, st.b] = [st.b, st.a]; draw(); };
      $("#cfGo").onclick = async (e) => {
        for (const k of ["a", "b"]) {
          if (st[k] === CURRENT) {
            const r = await fetchCurrent(e.currentTarget);
            if (!r) return;
            st[k] = r.ref;
          }
        }
        if (st.a === st.b) return toast("Pick two different versions.");
        draw(); runDiff(t, st, $("#cfBody"));
      };
      if (st.a !== CURRENT && st.b !== CURRENT && st.a !== st.b && list.length > 1) runDiff(t, st, $("#cfBody"));
      else $("#cfBody").innerHTML = `<div class="empty">${list.length < 2 ? "Only one stored version so far. Choose <b>Current</b> for one side to compare it with the running configuration." : "Choose two versions and click <b>Compare</b>."}</div>`;
    }
  };
  draw();
}

async function viewConfig(t, st, meta, el) {
  el.innerHTML = '<div class="empty"><span class="spin"></span></div>';
  const text = await api(`/targets/${t.id}/configs/view?ref=${encodeURIComponent(meta.ref)}${st.raw ? "&raw=true" : ""}`);
  const lines = text.split("\n");
  const secs = [];
  lines.forEach((l, i) => { const m = l.match(/^(\s{0,4})config (.+)$/); if (m) secs.push({ i, name: m[2], depth: m[1].length / 4 }); });
  el.innerHTML = `<div class="cfg">
    <nav class="cfg-nav"><input id="cfNav" placeholder="Filter sections"><div id="cfSecs">${secs.map((s) => `<a href="#" data-l="${s.i}" style="padding-left:${10 + s.depth * 12}px" title="config ${esc(s.name)}">${esc(s.name)}</a>`).join("")}</div></nav>
    <div class="cfg-main"><div class="toolbar"><b class="mono">${esc(meta.file || meta.id)}</b><span class="hint">${lines.length.toLocaleString()} lines · ${esc(cfgLabel(meta))}</span><span class="grow"></span>
      <input id="cfQ" placeholder="Search" style="width:180px"><span class="hint" id="cfQn"></span><button class="small" id="cfPrev">▲</button><button class="small" id="cfNext">▼</button>
      <label class="cb" title="Show ENC secrets and certificates"><input type="checkbox" id="cfRaw" ${st.raw ? "checked" : ""}> Show secrets</label>
      <button class="small" id="cfCopy">Copy</button><button class="small" id="cfDl">Download</button></div>
      <div class="code" id="cfCode">${lines.map((l, i) => `<div class="cl" id="cl${i}"><span class="ln">${i + 1}</span><span class="cx">${hlConf(l) || " "}</span></div>`).join("")}</div></div></div>`;
  const code = $("#cfCode", el);
  const jump = (i) => { const row = $(`#cl${i}`, code); if (row) { code.scrollTop = row.offsetTop - code.offsetTop - 40; row.classList.add("flash"); setTimeout(() => row.classList.remove("flash"), 1200); } };
  $$("#cfSecs a", el).forEach((a) => a.onclick = (e) => { e.preventDefault(); jump(Number(a.dataset.l)); });
  $("#cfNav", el).oninput = (e) => { const q = e.target.value.toLowerCase(); $$("#cfSecs a", el).forEach((a) => a.hidden = q && !a.textContent.toLowerCase().includes(q)); };
  let hits = [], cur = -1;
  const search = () => {
    $$(".cl.hit", code).forEach((r) => r.classList.remove("hit", "cur"));
    const q = $("#cfQ", el).value.toLowerCase();
    hits = q ? lines.map((l, i) => l.toLowerCase().includes(q) ? i : -1).filter((i) => i >= 0) : [];
    hits.forEach((i) => $(`#cl${i}`, code).classList.add("hit"));
    cur = hits.length ? 0 : -1; show();
  };
  const show = () => { $("#cfQn", el).textContent = $("#cfQ", el).value ? `${hits.length ? cur + 1 : 0}/${hits.length}` : ""; $$(".cl.cur", code).forEach((r) => r.classList.remove("cur")); if (cur >= 0) { $(`#cl${hits[cur]}`, code).classList.add("cur"); jump(hits[cur]); } };
  let tmr; $("#cfQ", el).oninput = () => { clearTimeout(tmr); tmr = setTimeout(search, 200); };
  $("#cfQ", el).onkeydown = (e) => { if (e.key === "Enter" && hits.length) { cur = (cur + (e.shiftKey ? -1 : 1) + hits.length) % hits.length; show(); } };
  $("#cfNext", el).onclick = () => { if (hits.length) { cur = (cur + 1) % hits.length; show(); } };
  $("#cfPrev", el).onclick = () => { if (hits.length) { cur = (cur - 1 + hits.length) % hits.length; show(); } };
  $("#cfRaw", el).onchange = (e) => { st.raw = e.target.checked; viewConfig(t, st, meta, el); };
  $("#cfCopy", el).onclick = () => navigator.clipboard?.writeText(text);
  $("#cfDl", el).onclick = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" })); a.download = (meta.file || meta.id + ".conf").replace(/\.log$/, ".conf"); a.click(); };
}

async function runDiff(t, st, el) {
  el.innerHTML = '<div class="empty"><span class="spin"></span> Comparing…</div>';
  let d;
  try { d = await api(`/targets/${t.id}/configs/diff?a=${encodeURIComponent(st.a)}&b=${encodeURIComponent(st.b)}`); } catch (e) { el.innerHTML = `<div class="banner crit" style="margin:12px">${esc(e.message)}</div>`; return; }
  const s = d.summary, total = s.added + s.removed + s.changed;
  el.innerHTML = `<div class="cmp-head"><div><span class="hint">From</span> <b>${esc(cfgLabel(d.a))}</b></div><span class="arrow">→</span><div><span class="hint">To</span> <b>${esc(cfgLabel(d.b))}</b></div></div>
    <div class="tiles" style="border-bottom:1px solid var(--rule)"><div class="tile ${s.changed ? "warn" : ""}"><div class="n">${s.changed}</div><div class="l">Settings changed</div></div><div class="tile ${s.added ? "good" : ""}"><div class="n">${s.added}</div><div class="l">Added</div></div><div class="tile ${s.removed ? "bad" : ""}"><div class="n">${s.removed}</div><div class="l">Removed</div></div></div>
    ${total ? `<div class="toolbar"><div class="seg" id="dv"><button data-v="split" class="${st.dview === "split" ? "on" : ""}">Side by side</button><button data-v="unified" class="${st.dview === "unified" ? "on" : ""}">Unified</button><button data-v="changes" class="${st.dview === "changes" ? "on" : ""}">Changed settings</button></div>
      <span class="grow"></span><span class="vtools"></span><input id="dq" placeholder="Filter" style="width:200px"><span class="hint">Secrets and certificates are masked; build-stamp lines are ignored.</span></div><div id="dbody"></div>`
      : '<div class="empty">The two configurations are identical (ignoring secrets and build stamps).</div>'}`;
  if (!total) return;
  const body = $("#dbody", el);
  viewTools(el.closest(".panel"), $(".vtools", el));
  const draw = () => {
    const q = ($("#dq", el).value || "").toLowerCase();
    const match = (r) => !q || JSON.stringify(r).toLowerCase().includes(q);
    if (st.dview === "changes") {
      const rows = d.changes.filter(match);
      body.innerHTML = rows.length ? `<div class="tablewrap" style="max-height:640px"><table><thead><tr><th style="width:90px">Change</th><th>Setting</th><th>Before</th><th>After</th></tr></thead><tbody>${rows.map((c) => {
        const parts = c.path.split(" > "); const leaf = parts.pop();
        return `<tr><td>${badge(c.type === "changed" ? "Changed" : c.type === "added" ? "Added" : "Removed", c.type === "changed" ? "warn" : c.type === "added" ? "up" : "down")}</td>
          <td><div class="hint mono">${esc(parts.join(" › "))}</div><b class="mono">${esc(leaf)}</b></td><td class="mono before">${esc(c.before ?? "")}</td><td class="mono after">${esc(c.after ?? "")}</td></tr>`; }).join("")}</tbody></table></div>` : '<div class="empty">No matching changes</div>';
      return;
    }
    const rows = d.rows.filter((r) => r.t === "gap" || match(r));
    if (st.dview === "split") {
      body.innerHTML = `<div class="code diff"><table class="split">${rows.map((r) => r.t === "gap" ? `<tr class="gap"><td colspan="4">⋯ unchanged lines ⋯</td></tr>` :
        `<tr class="${r.t}"><td class="ln">${r.a ? r.a[0] : ""}</td><td class="cx ${r.a && r.t !== "same" ? "old" : ""}">${r.a ? hlConf(r.a[1]) : ""}</td><td class="ln">${r.b ? r.b[0] : ""}</td><td class="cx ${r.b && r.t !== "same" ? "new" : ""}">${r.b ? hlConf(r.b[1]) : ""}</td></tr>`).join("")}</table></div>`;
    } else {
      const out = [];
      for (const r of rows) {
        if (r.t === "gap") { out.push('<div class="ul gap">⋯ unchanged lines ⋯</div>'); continue; }
        if (r.t === "same") { out.push(`<div class="ul"><span class="ln">${r.a[0]}</span><span class="ln">${r.b[0]}</span><span class="sg"> </span><span class="cx">${hlConf(r.a[1])}</span></div>`); continue; }
        if (r.a) out.push(`<div class="ul old"><span class="ln">${r.a[0]}</span><span class="ln"></span><span class="sg">-</span><span class="cx">${hlConf(r.a[1])}</span></div>`);
        if (r.b) out.push(`<div class="ul new"><span class="ln"></span><span class="ln">${r.b[0]}</span><span class="sg">+</span><span class="cx">${hlConf(r.b[1])}</span></div>`);
      }
      body.innerHTML = `<div class="code diff">${out.join("")}</div>`;
    }
  };
  $$("#dv button", el).forEach((b) => b.onclick = () => { st.dview = b.dataset.v; $$("#dv button", el).forEach((x) => x.classList.toggle("on", x === b)); draw(); });
  let tmr; $("#dq", el).oninput = () => { clearTimeout(tmr); tmr = setTimeout(draw, 200); };
  draw();
}

// Maximise and font-size controls for the diff views. The size is remembered per viewer.
const CODE_FS = { min: 9, max: 20, key: "fu.codeFs" };
function codeFs() { try { return Number(localStorage.getItem(CODE_FS.key)) || 12; } catch { return 12; } }
function viewTools(panel, slot) {
  if (!panel || !slot) return;
  const apply = (n) => { panel.style.setProperty("--code-fs", n + "px"); $(".fs-n", slot).textContent = n + "px"; try { localStorage.setItem(CODE_FS.key, n); } catch { /* ignore */ } };
  slot.innerHTML = `<span class="fs-ctl" title="Font size"><button class="small" data-fs="-1" title="Smaller text">A−</button><span class="fs-n hint"></span><button class="small" data-fs="1" title="Larger text">A+</button></span>
    <button class="small" data-max title="Maximise (Esc to restore)">${icon("maximize")}<span>Maximise</span></button>`;
  apply(codeFs());
  $$("[data-fs]", slot).forEach((b) => b.onclick = () => apply(Math.min(CODE_FS.max, Math.max(CODE_FS.min, codeFs() + Number(b.dataset.fs)))));
  const mb = $("[data-max]", slot);
  const setMax = (on) => {
    panel.classList.toggle("maxed", on); document.body.classList.toggle("has-maxed", on);
    mb.innerHTML = `${icon(on ? "minimize" : "maximize")}<span>${on ? "Restore" : "Maximise"}</span>`;
    mb.title = on ? "Restore (Esc)" : "Maximise (Esc to restore)";
    document.onkeydown = on ? (e) => { if (e.key === "Escape" && !$("#modal").open) setMax(false); } : null;
  };
  mb.onclick = () => setMax(!panel.classList.contains("maxed"));
  if (document.body.classList.contains("has-maxed")) setMax(true);   // stay maximised when the view redraws
}
