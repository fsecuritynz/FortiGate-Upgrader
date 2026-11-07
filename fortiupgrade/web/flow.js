"use strict";
/* Stage diagram for an upgrade: one row per phase (Prepare, each firmware hop, Finish), every
   stage a node on the row's line, coloured by status, with live notes (e.g. reboot progress) and
   how long each stage took. */

const FD_ICON = { done: "✓", failed: "✕", cancelled: "✕", skipped: "–", waiting: "❚❚" };
const FD_WORD = { pending: "Pending", running: "In progress", waiting: "Waiting", done: "Done", failed: "Failed", cancelled: "Cancelled", skipped: "Skipped" };

function fdDur(ms) {
  if (ms == null || ms < 0) return "";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s` : `${Math.floor(s / 3600)}h ${String(Math.floor(s / 60) % 60).padStart(2, "0")}m`;
}

// Prepare = stages before the first hop, one phase per hop, Finish = stages after the last hop
function fdPhases(flow) {
  const out = [];
  let seenHop = false;
  for (const s of flow) {
    const name = s.group ? `Upgrade to ${s.group}` : seenHop ? "Finish" : "Prepare";
    if (s.group) seenHop = true;
    const last = out[out.length - 1];
    if (last && last.name === name) last.steps.push(s); else out.push({ name, hop: !!s.group, steps: [s] });
  }
  return out;
}

function fdPhaseState(steps) {
  const st = steps.map((s) => s.status);
  if (st.some((x) => x === "failed" || x === "cancelled")) return "failed";
  if (st.some((x) => x === "running")) return "running";
  if (st.some((x) => x === "waiting")) return "waiting";
  if (st.every((x) => x === "done" || x === "skipped")) return "done";
  if (st.some((x) => x !== "pending")) return "running";
  return "pending";
}

function flowDiagram(flow) {
  const now = Date.now();
  let n = 0;
  return `<div class="fd">${fdPhases(flow).map((ph) => {
    const state = fdPhaseState(ph.steps);
    const t0 = ph.steps.find((s) => s.started)?.started, t1 = [...ph.steps].reverse().find((s) => s.ended)?.ended;
    const dur = t0 ? fdDur((state === "done" || state === "failed" ? t1 || now : now) - t0) : "";
    const row = ph.steps.map((s, i) => {
      n++;
      const prev = ph.steps[i - 1], next = ph.steps[i + 1];
      const inDone = prev && ["done", "skipped"].includes(prev.status) && s.status !== "pending";
      const outDone = ["done", "skipped"].includes(s.status) && next && next.status !== "pending";
      const d = s.started ? fdDur((s.ended || now) - s.started) : "";
      return `<div class="fd-step s-${s.status}${s.attention ? " attn" : ""}" data-sid="${esc(s.id)}" title="${esc(`${s.label}: ${FD_WORD[s.status] || s.status}${s.note ? " · " + s.note : ""}`)}">
        <div class="fd-node"><span class="fd-line ${!prev ? "none" : inDone ? "lit" : s.status === "running" || s.status === "waiting" ? "live" : ""}"></span>
          <span class="fd-dot">${s.status === "running" ? '<span class="fd-spin"></span>' : FD_ICON[s.status] || n}</span>
          <span class="fd-line ${!next ? "none" : outDone ? "lit" : ""}"></span></div>
        <div class="fd-label">${esc(s.label)}</div>
        ${s.sub ? `<div class="fd-sub">${esc(s.sub)}</div>` : ""}
        ${s.note ? `<div class="fd-note">${esc(s.note)}</div>` : ""}
        <div class="fd-time">${s.status === "pending" ? "" : s.status === "skipped" ? "skipped" : esc(d)}</div></div>`;
    }).join("");
    return `<div class="fd-phase p-${state}${ph.hop ? " hop" : ""}"><div class="fd-ph"><b>${esc(ph.name)}</b><span>${esc(FD_WORD[state] || state)}${dur ? ` · ${dur}` : ""}</span></div><div class="fd-track">${row}</div></div>`;
  }).join("")}</div>`;
}

// nothing to follow any more: every phase is visible at once
function flowFollow() {}
