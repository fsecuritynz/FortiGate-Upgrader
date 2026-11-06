"use strict";
/* Front-panel drawings of FortiGate models.
 *
 * Layouts follow the port groups in Fortinet's datasheets (port names as FortiOS reports
 * them, labels as printed on the unit). Models without a layout get one generated from
 * the device's own physical ports: desktop models are drawn in one row, 1U models
 * with odd ports on top and even ports below, SFP/SFP+ cages to the right.
 *
 * Port types: rj45 | sfp | sfpp (SFP+) | shared (RJ45 + SFP/SFP+ pair, one interface)
 */
const P = (n, l, t = "rj45") => ({ n, l, t });
const range = (a, b, f) => Array.from({ length: b - a + 1 }, (_, i) => f(a + i));
const FACEPLATES = {
  "40F": { form: "desktop", groups: [
    { cap: "WAN", ports: [P("wan", "WAN")] },
    { cap: "LAN", ports: range(1, 3, (i) => P(`lan${i}`, String(i))) },
    { cap: "FortiLink", ports: [P("a", "A")] }] },
  "60F": { form: "desktop", groups: [
    { cap: "WAN", ports: [P("wan1", "WAN1"), P("wan2", "WAN2")] },
    { cap: "DMZ", ports: [P("dmz", "DMZ")] },
    { cap: "FortiLink", ports: [P("a", "A"), P("b", "B")] },
    { cap: "Internal", ports: range(1, 5, (i) => P(`internal${i}`, String(i))) }] },
  "90G": { form: "desktop", groups: [
    { cap: "10G WAN (RJ45 / SFP+)", ports: [P("x1", "X1", "shared"), P("x2", "X2", "shared")] },
    { cap: "GE", ports: range(1, 6, (i) => P(`port${i}`, String(i))) },
    { cap: "FortiLink", ports: [P("a", "A"), P("b", "B")] }] },
  "100F": { form: "1u", groups: [
    { cap: "MGMT / DMZ", rows: 2, ports: [P("mgmt", "MGMT"), P("dmz", "DMZ")] },
    { cap: "HA", rows: 2, ports: [P("ha1", "HA1"), P("ha2", "HA2")] },
    { cap: "WAN", rows: 2, ports: [P("wan1", "WAN1"), P("wan2", "WAN2")] },
    { cap: "GE RJ45", rows: 2, ports: range(1, 12, (i) => P(`port${i}`, String(i))) },
    { cap: "Shared RJ45 / SFP", ports: range(17, 20, (i) => P(`port${i}`, String(i), "shared")) },
    { cap: "GE SFP", rows: 2, ports: range(13, 16, (i) => P(`port${i}`, String(i), "sfp")) },
    { cap: "10G SFP+ FortiLink", rows: 2, ports: [P("x1", "X1", "sfpp"), P("x2", "X2", "sfpp")] }] },
  "200F": { form: "1u", groups: [
    { cap: "HA / MGMT", rows: 2, ports: [P("ha", "HA"), P("mgmt", "MGMT")] },
    { cap: "GE RJ45", rows: 2, ports: range(1, 16, (i) => P(`port${i}`, String(i))) },
    { cap: "GE SFP", rows: 2, ports: range(17, 24, (i) => P(`port${i}`, String(i), "sfp")) },
    { cap: "10G SFP+", rows: 2, ports: [P("x1", "X1", "sfpp"), P("x2", "X2", "sfpp")] },
    { cap: "10G SFP+ FortiLink", rows: 2, ports: [P("x3", "X3", "sfpp"), P("x4", "X4", "sfpp")] }] },
};
Object.assign(FACEPLATES, { "41F": FACEPLATES["40F"], "61F": FACEPLATES["60F"], "91G": FACEPLATES["90G"], "101F": FACEPLATES["100F"], "201F": FACEPLATES["201F"] || FACEPLATES["200F"] });

function modelKey(model, code) {
  const m = String(model || "").toUpperCase().replace(/^FORTI(GATE|WIFI)[- ]?/, "").replace(/-?(POE|DSL|3G4G|LTE)$/, "");
  if (/VM/.test(m) || /^FGVM/.test(code || "")) return "VM";
  return m.replace(/[^0-9A-Z]/g, "");
}

function genericLayout(model, ports) {
  const key = modelKey(model);
  const num = Number((key.match(/^\d+/) || [0])[0]);
  const form = key === "VM" ? "vm" : num && num < 100 ? "desktop" : "1u";
  const kind = (n) => /^x\d/.test(n) ? "sfpp" : /^(sfp|s)\d/.test(n) ? "sfp" : "rj45";
  const rank = (n) => /^mgmt/.test(n) ? 0 : /^ha/.test(n) ? 1 : /^wan/.test(n) ? 2 : /^dmz/.test(n) ? 3 : /^(internal|lan)/.test(n) ? 4 : /^port/.test(n) ? 5 : /^[ab]$/.test(n) ? 6 : /^(sfp|s)\d/.test(n) ? 7 : /^x\d/.test(n) ? 8 : 9;
  const caps = ["MGMT", "HA", "WAN", "DMZ", "Internal", "Ports", "FortiLink", "SFP", "SFP+", "Other"];
  const sorted = [...ports].sort((a, b) => rank(a.name) - rank(b.name) || Number((a.name.match(/\d+/) || [0])[0]) - Number((b.name.match(/\d+/) || [0])[0]) || a.name.localeCompare(b.name));
  const groups = [];
  for (const p of sorted) {
    const r = rank(p.name);
    if (!groups.length || groups[groups.length - 1].r !== r) groups.push({ r, cap: form === "vm" ? "Virtual NICs" : caps[r], ports: [] });
    const lbl = p.name.replace(/^(port|internal|lan)(\d+)$/, "$2").toUpperCase();
    groups[groups.length - 1].ports.push(P(p.name, lbl, form === "vm" ? "rj45" : kind(p.name)));
  }
  groups.forEach((g) => { g.rows = form === "1u" && g.ports.length > 1 ? 2 : 1; });
  return { form, groups, generic: true };
}

function faceplateLayout(model, code, ports) {
  const key = modelKey(model, code);
  const base = FACEPLATES[key];
  if (!base) return genericLayout(model, ports);
  const have = new Set(ports.map((p) => p.name));
  const placed = new Set(base.groups.flatMap((g) => g.ports.map((p) => p.n)));
  const extra = ports.filter((p) => !placed.has(p.name));
  const groups = base.groups.map((g) => ({ ...g, rows: g.rows || 1 }));
  if (extra.length) groups.push(...genericLayout(model, extra).groups.map((g) => ({ ...g, cap: g.cap + " (other)" })));
  return { form: base.form, groups, known: true, missing: [...placed].filter((n) => !have.has(n)) };
}

/* ---------------------------------------------------------------- drawing */
const PORT_SIZE = { rj45: [26, 22], sfp: [30, 15], sfpp: [30, 15], shared: [30, 40] };
function drawPort(x, y, spec, st, label, labelBelow, hb) {
  const [w, h] = PORT_SIZE[spec.t];
  const up = st && st.status === "Up", known = st && st.status !== "Unknown";
  const media = (st && st.media) || "";
  const edge = !st ? "#4a5056" : up ? "#5fd08a" : "#6a7178";
  const tip = !st ? `${spec.n}: not reported by the device` :
    `${spec.n}${st.alias ? ` (${st.alias})` : ""}${spec.t === "shared" && media ? ` [${/sfp|fib/.test(media) ? "SFP" : "RJ45"} in use]` : ""}: ${known ? (up ? "link up" : "link down") : "status unknown"}${st.status_from_switch ? ` (status of ${st.member_of})` : ""}` +
    `${st.member_of ? `\nMember of ${st.member_of}` : ""}${st.ip ? `\nIP ${st.ip}` : ""}${st.speed && st.speed !== "—" ? `\n${st.speed}` : ""}` +
    `${st.access && st.access !== "None" ? `\nAdmin access: ${st.access}` : ""}${st.admin_down ? "\nAdministratively down" : ""}`;
  const rj = (yy, on) => `<rect x="${x}" y="${yy}" width="26" height="22" rx="2" fill="var(--port)" stroke="${on ? "#5fd08a" : edge === "#5fd08a" ? "#6a7178" : edge}" stroke-width="${on ? 1.5 : 1}"/>
      <path d="M${x + 6} ${yy + 22}v-6h3v-3h8v3h3v6" fill="#262b30"/>
      <rect x="${x + 2}" y="${yy + 2}" width="6" height="3" fill="${on ? "#4be37f" : "#2f3439"}"/><rect x="${x + 18}" y="${yy + 2}" width="6" height="3" fill="${on ? "#f0b43c" : "#2f3439"}"/>`;
  const cage = (yy, ww, on) => `<rect x="${x}" y="${yy}" width="${ww}" height="15" rx="1.5" fill="#1d2125" stroke="${on ? "#5fd08a" : edge === "#5fd08a" ? "#6a7178" : edge}" stroke-width="${on ? 1.5 : 1}"/>
      <rect x="${x + 4}" y="${yy + 4}" width="${ww - 8}" height="7" fill="#0b0d0f"/><rect x="${x + ww - 8}" y="${yy - 4}" width="6" height="3" fill="${on ? "#4be37f" : "#2f3439"}"/>`;
  let body;
  if (spec.t === "rj45") body = rj(y, up);
  else if (spec.t === "shared") body = rj(y, up && !/sfp|fib/.test(media)) + cage(y + 25, 30, up && /sfp|fib/.test(media));
  else body = cage(y, w, up);
  const ly = labelBelow ? y + h + 11 : y - 4;
  const hbRing = hb ? `<rect x="${x - 3}" y="${y - 3}" width="${w + 6}" height="${h + 6}" rx="4" fill="none" stroke="#f0b43c" stroke-width="2"/>` : "";
  return `<g class="fp-port${hb ? " fp-hb" : ""}" data-port="${esc(spec.n)}" opacity="${st || hb ? 1 : 0.45}"><title>${esc(tip)}${hb ? "\nHA heartbeat interface" : ""}</title>${body}${hbRing}
    <text x="${x + w / 2}" y="${ly}" text-anchor="middle" font-size="9" font-weight="600" fill="#cfd5da">${esc(label)}</text></g>`;
}

function faceplate(model, code, ports, opts = {}) {
  const hbSet = new Set(opts.hb || []);
  ports = (ports || []).filter((p) => p && p.name);
  if (!ports.length) return '<div class="empty">The device did not report any physical ports.</div>';
  const lay = faceplateLayout(model, code, ports);
  const byName = Object.fromEntries(ports.map((p) => [p.name, p]));
  const short = String(model || "").replace(/^Forti(Gate|WiFi)[- ]?/i, "") || "FortiGate";
  const fs = Math.max(13, Math.min(22, Math.floor(120 / (short.length * 0.62))));
  const rack = lay.form === "1u", vm = lay.form === "vm";
  const ear = rack ? 26 : 0;
  const left = ear + Math.max(rack ? 170 : 150, Math.ceil(short.length * fs * 0.72) + 60);
  const H = rack ? 126 : 118;
  const anyShared = lay.groups.some((g) => g.ports.some((p) => p.t === "shared"));
  let x = left, svg = "";
  for (const g of lay.groups) {
    const rows = g.rows || 1;
    const cols = Math.ceil(g.ports.length / rows);
    const cw = Math.max(...g.ports.map((p) => PORT_SIZE[p.t][0])) + 8;
    const ph = Math.max(...g.ports.map((p) => PORT_SIZE[p.t][1]));
    const pw = cols * cw - 8;
    const gw = Math.max(pw, Math.ceil(String(g.cap).length * 5.4));
    const x0 = x + (gw - pw) / 2;
    const top = rows === 2 ? 34 : anyShared || ph > 30 ? 34 : 50;
    svg += `<text x="${x + gw / 2}" y="22" text-anchor="middle" font-size="8.5" letter-spacing=".5" fill="#9aa3ab">${esc(String(g.cap).toUpperCase())}</text>`;
    g.ports.forEach((p, i) => {
      const col = rows === 2 ? Math.floor(i / 2) : i, row = rows === 2 ? i % 2 : 0;
      const [, h] = PORT_SIZE[p.t];
      const y = rows === 2 ? (row === 0 ? top + 6 : top + 50) : top + 6;
      svg += drawPort(x0 + col * cw, y, p, byName[p.n], p.l, true, hbSet.has(p.n));
      void h;
    });
    x += gw + 18;
  }
  const W = x - 18 + 16 + ear;
  const ears = rack ? [0, W - ear].map((ex) => `<rect x="${ex}" y="4" width="${ear}" height="${H - 8}" rx="3" fill="var(--chassis-edge)"/><circle cx="${ex + ear / 2}" cy="24" r="5" fill="var(--bg)"/><circle cx="${ex + ear / 2}" cy="${H - 24}" r="5" fill="var(--bg)"/>`).join("") : "";
  const bx = rack ? ear : 1, bw = W - (rack ? ear * 2 : 2);
  const cons = `<g opacity=".8"><rect x="${ear + 18}" y="${H - 27}" width="20" height="15" rx="2" fill="#1b1f23" stroke="#5a6168"/><text x="${ear + 28}" y="${H - 31}" text-anchor="middle" font-size="7" fill="#9aa3ab">CONSOLE</text>
    <rect x="${ear + 58}" y="${H - 23}" width="18" height="8" rx="1" fill="#1b1f23" stroke="#5a6168"/><text x="${ear + 67}" y="${H - 31}" text-anchor="middle" font-size="7" fill="#9aa3ab">USB</text></g>`;
  const head = `${ears}<rect x="${bx}" y="1" width="${bw}" height="${H - 2}" rx="${rack ? 3 : 8}" fill="var(--chassis)" stroke="var(--chassis-edge)" stroke-width="2" ${vm ? 'stroke-dasharray="6 4"' : ""}/>
    ${rack ? "" : `<rect x="${bx + 6}" y="${H - 9}" width="${bw - 12}" height="3" rx="1.5" fill="#00000030"/>`}
    <text x="${ear + 18}" y="28" font-size="10" fill="#aeb6bd" letter-spacing="1.2">${vm ? "FORTIGATE VM" : "FORTIGATE"}</text>
    <text x="${ear + 18}" y="${28 + fs + 4}" font-size="${fs}" font-weight="700" fill="#fff">${esc(short)}</text>
    ${[["PWR", true], ["STATUS", true]].map(([n, on], k) => `<circle cx="${ear + 22 + k * 50}" cy="${28 + fs + 16}" r="3.5" fill="${on ? "#4be37f" : "#3a3f44"}"/><text x="${ear + 29 + k * 50}" y="${28 + fs + 19}" font-size="8.5" fill="#aeb6bd">${n}</text>`).join("")}
    ${vm ? "" : cons}`;
  const upN = ports.filter((p) => p.status === "Up").length;
  const note = lay.known ? `Layout of the ${esc(short)} front panel` : vm ? "Virtual NICs" : `No stored layout for ${esc(short)}: drawn from the device's physical ports`;
  if (opts.legend === false) return `<svg viewBox="0 0 ${W} ${H}" width="${Math.round(W * 1.4)}" role="img" aria-label="${esc(model)} front panel">${head}${svg}</svg>`;
  return `<svg viewBox="0 0 ${W} ${H}" width="${Math.round(W * 1.4)}" role="img" aria-label="${esc(model)} front panel">${head}${svg}</svg>
    <div class="legend"><span><span class="dot up"></span> Link up (${upN})</span><span><span class="dot grey"></span> Link down (${ports.length - upN})</span><span>${note} · hover a port for details</span></div>`;
}
