"""PDF report of an upgrade run: settings, pre-flight, per-device timeline, pre/post checks,
check and configuration diffs, warnings/errors and the full activity log."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

from . import jobs, storage, version_string
from .diff import diff_snapshots

RED, AMBER, GREEN, BLUE = colors.HexColor("#d43a2f"), colors.HexColor("#d9822b"), colors.HexColor("#2f9e5b"), colors.HexColor("#2c7cc4")
GREY, LIGHT, DARK = colors.HexColor("#6c7680"), colors.HexColor("#f1f3f4"), colors.HexColor("#24292e")
SEV_COL = {"critical": RED, "warning": AMBER, "info": BLUE}

_ss = getSampleStyleSheet()
ST = {
    "title": ParagraphStyle("t", parent=_ss["Title"], fontSize=22, leading=26, alignment=TA_LEFT, spaceAfter=4),
    "h1": ParagraphStyle("h1", parent=_ss["Heading1"], fontSize=15, leading=18, spaceBefore=6, spaceAfter=6),
    "h2": ParagraphStyle("h2", parent=_ss["Heading2"], fontSize=12, leading=15, spaceBefore=10, spaceAfter=4),
    "h3": ParagraphStyle("h3", parent=_ss["Heading3"], fontSize=10, leading=13, spaceBefore=6, spaceAfter=3),
    "p": ParagraphStyle("p", parent=_ss["BodyText"], fontSize=9, leading=12),
    "small": ParagraphStyle("s", parent=_ss["BodyText"], fontSize=7.5, leading=9.5),
    "cell": ParagraphStyle("c", parent=_ss["BodyText"], fontSize=7.5, leading=9.2),
    "cellb": ParagraphStyle("cb", parent=_ss["BodyText"], fontSize=7.5, leading=9.2, fontName="Helvetica-Bold"),
    "mono": ParagraphStyle("m", fontName="Courier", fontSize=6.6, leading=8.0),
}
MAX_ROWS = 400


def _p(text, style="cell"):
    return Paragraph(escape(str(text if text is not None else "—")).replace("\n", "<br/>"), ST[style])


def _fmt(v):
    if isinstance(v, dict):
        return " · ".join(f"{k}: {x}" for k, x in v.items() if x not in ("—", None, ""))
    return "—" if v in (None, "") else str(v)


def _sev_cell(v):
    col = SEV_COL.get(str(v).lower()) or {"failed": RED, "complete": GREEN, "error": RED}.get(str(v).lower())
    if not col:
        return _p(v)
    return Paragraph(f'<font color="{col.hexval()}"><b>{escape(str(v))}</b></font>', ST["cell"])


def _table(header, rows, widths, sev_col=None, zebra=True):
    data = [[_p(h, "cellb") for h in header]] + [[c if hasattr(c, "wrap") else (_sev_cell(c) if j == sev_col else _p(c))
                                                  for j, c in enumerate(r)] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), LIGHT), ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d8dde1")),
             ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
             ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
    if zebra:
        style += [("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafbfc")])]
    t.setStyle(TableStyle(style))
    return t


def _kv(pairs, width):
    return _table(["Item", "Value"], [[k, v] for k, v in pairs], [width * 0.28, width * 0.72], zebra=False)


def _status_word(s):
    return {"done": "Complete", "failed": "Failed", "cancelled": "Cancelled", "interrupted": "Interrupted",
            "running": "Running", "waiting": "Waiting"}.get(s, s)


_FROM_DISK = False


def _job(jid):
    j = None if _FROM_DISK else jobs.JOBS.get(jid)
    if j is not None:
        import json
        return json.loads(json.dumps(j.to_dict(), default=str))  # a stable, plain copy of the live job
    return storage.load_job(jid)


def _dur(a, b):
    try:
        s = (dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds()
    except (TypeError, ValueError):
        return "—"
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.0f} min" if s < 5400 else f"{s / 3600:.1f} h"


def build(job_id: str, out: Path | None = None, include_api_trace: bool = True, from_disk: bool = False) -> Path:
    global _FROM_DISK
    _FROM_DISK = from_disk
    try:
        return _build(job_id, out, include_api_trace)
    finally:
        _FROM_DISK = False


def _build(job_id: str, out: Path | None, include_api_trace: bool) -> Path:
    batch = _job(job_id)
    if not batch:
        raise KeyError(job_id)
    if batch.get("kind") != "batch" and batch.get("parent"):
        batch = _job(batch["parent"]) or batch
    meta = batch.get("meta") or {}
    kids = [k for k in (_job(c) for c in batch.get("children") or []) if k]
    if not kids and batch.get("kind") == "upgrade":
        kids = [batch]
    pf = _job(meta["preflight_job"]) if meta.get("preflight_job") else None
    targets = {t["id"]: t for t in storage.list_targets()}
    when = dt.datetime.fromisoformat(batch.get("created") or dt.datetime.now().isoformat())
    names = [k.get("title", "") for k in kids]
    if out is None:
        d = storage.data_dir() / "reports" / "upgrades"
        d.mkdir(parents=True, exist_ok=True)
        tag = names[0] if len(names) == 1 else f"{len(names)}-devices"
        base = f"upgrade-report-{tag}-{when.strftime('%A')}-{when.strftime('%d-%m-%Y-%H-%M-%S')}"
        out = d / (storage._safe(base) + ".pdf")  # noqa: SLF001
    doc = SimpleDocTemplate(str(out), pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=f"FortiGate upgrade report {when:%d-%m-%Y %H:%M}", author="FortiGate Upgrader")
    W = doc.width
    sim = bool(meta.get("dry_run"))
    method = "Download from FortiGuard" if meta.get("source", "fortiguard") == "fortiguard" else "Push local image (Firmware Store)"
    story = []

    # ------------------------------------------------------------ cover / summary
    story += [Paragraph("FortiGate Upgrade Report" + (" (simulation)" if sim else ""), ST["title"]),
              Paragraph(f"{escape(batch.get('title', ''))}", ST["p"]), Spacer(1, 6)]
    ok = sum(1 for k in kids if k.get("status") == "done")
    story.append(_kv([
        ("Result", f"{_status_word(batch.get('status'))}: {ok} of {len(kids)} device(s) completed"),
        ("Target image", f"FortiOS {meta.get('target_version', '?')}"),
        ("Install method", method),
        ("Upgrade path", "Direct to the target version" if meta.get("direct") else "Fortinet supported upgrade path"),
        ("HA modes", ", ".join(f"{k.upper()}: {v}" for k, v in (meta.get("ha_modes") or {}).items()
                               if v and any(targets.get(t, {}).get("type") == k for t in batch.get("target_ids") or []))
         or "Not applicable (standalone devices)"),
        ("Pre/post checks", "Yes" if meta.get("run_checks", True) else "No"),
        ("Started", (batch.get("created") or "").replace("T", " ")),
        ("Finished", (batch.get("finished") or "—").replace("T", " ")),
        ("Duration", _dur(batch.get("created"), batch.get("finished"))),
        ("Generated", f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} by FortiGate Upgrader {version_string()}"),
    ], W * 0.6))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Devices", ST["h2"]))
    rows = []
    for k in kids:
        r = k.get("result") or {}
        pre = (r.get("pre_snapshots") or {})
        dev = {}
        if pre:
            try:
                dev = storage.load_snapshot(k.get("target_id"), next(iter(pre.values())))["meta"]["device"]
            except Exception:  # noqa: BLE001
                dev = {}
        tot = {"critical": 0, "warning": 0, "info": 0}
        for rep in r.get("reports") or []:
            for kk in tot:
                tot[kk] += rep["totals"].get(kk, 0)
        rows.append([k.get("title"), dev.get("model", "—"), dev.get("serial", "—"),
                     " → ".join(r.get("path") or []) or "—", _status_word(k.get("status")),
                     _dur(k.get("created"), k.get("finished")),
                     f"{tot['critical']} critical · {tot['warning']} warning · {tot['info']} info" if r.get("reports") else "—",
                     k.get("error") or ""])
    story.append(_table(["Device", "Model", "Serial", "Path", "Result", "Duration", "Post-check differences", "Error"], rows,
                        [W * x for x in (0.1, 0.12, 0.12, 0.13, 0.07, 0.06, 0.16, 0.24)], sev_col=4))
    pr = batch.get("probes") or (batch.get("result") or {}).get("probes")
    if pr and pr.get("probes"):
        story += _probes_section(pr, W)

    # ------------------------------------------------------------ pre-flight
    if pf and pf.get("result"):
        story += [PageBreak(), Paragraph("Pre-flight", ST["h1"]),
                  Paragraph("Read-only checks run before the upgrade was started.", ST["p"])]
        for it in pf["result"].get("results") or []:
            res = it.get("result") or {}
            story.append(Paragraph(escape(it.get("target", "")), ST["h2"]))
            if not res:
                story.append(Paragraph(f"Pre-flight failed: {escape(str(it.get('error')))}", ST["p"]))
                continue
            plan = res.get("plan") or {}
            story.append(_kv([("Upgrade path", " → ".join(plan.get("steps") or []) or "—"),
                              ("Path source", f"{plan.get('source', '—')} ({'verified' if plan.get('verified') else 'unverified'})"),
                              ("Images", "; ".join(f"{x['version']}: FortiGuard {'yes' if x['fortiguard'] else 'no'}, store "
                                                   f"{x['store'] or 'no'}" for x in res.get("images") or []) or "—")], W * 0.7))
            f = res.get("findings") or []
            if f:
                story.append(Paragraph("Findings", ST["h3"]))
                story.append(_table(["Severity", "Finding"], [[x["severity"].title(), x["message"]] for x in f],
                                    [W * 0.1, W * 0.9], sev_col=0))
            an = res.get("analysis") or {}
            hits = an.get("notes_hits") or []
            if an.get("rule_hits") or hits:
                story.append(Paragraph("Configuration vs release notes", ST["h3"]))
                rr = [[h["severity"].title(), h["title"], h.get("message", "")] for h in an.get("rule_hits") or []]
                rr += [[h["severity"].title(), f"{h['source']} · {h['section']} · {h['term']}", h["text"]] for h in hits[:60]]
                story.append(_table(["Severity", "Topic", "Detail"], rr, [W * 0.08, W * 0.22, W * 0.7], sev_col=0))

    # ------------------------------------------------------------ per device
    for k in kids:
        r = k.get("result") or {}
        tid = k.get("target_id")
        t = targets.get(tid, {})
        story += [PageBreak(), Paragraph(f"{escape(k.get('title', ''))}: {_status_word(k.get('status'))}", ST["h1"])]
        klog = k.get("log") or []
        img_lines = [l["msg"].split("install method: ", 1)[1] for l in klog if "install method: " in l.get("msg", "")]
        sha = next((l["msg"].split("sha256 ", 1)[1] for l in klog if "sha256 " in l.get("msg", "")), "")
        story.append(_kv([("Device type", {"standalone": "Standalone", "fgcp": "FGCP cluster", "fgsp": "FGSP peers"}.get(t.get("type"), "—")),
                          ("Image", "; ".join(dict.fromkeys(img_lines)) + (f" (sha256 {sha})" if sha else "") if img_lines else "—"),
                          ("Members", ", ".join(f"{m.get('name') or m['host']} ({m['host']})" for m in t.get("members", [])) or "—"),
                          ("Path", " → ".join(r.get("path") or []) or "—"),
                          ("Stage reached", k.get("stage", "—")),
                          ("Error", k.get("error") or "None"),
                          ("Started / finished", f"{(k.get('created') or '').replace('T', ' ')} → {(k.get('finished') or '—').replace('T', ' ')}")],
                         W * 0.75))
        log = k.get("log") or []
        timeline = [[l["t"], l["msg"].strip("— ")] for l in log if l.get("level") == "stage"]
        if timeline:
            story.append(Paragraph("Timeline", ST["h2"]))
            story.append(_table(["Time", "Stage"], timeline, [W * 0.12, W * 0.88]))
        issues = [[l["t"], {"error": "Error", "warn": "Warning", "gate": "Approval"}.get(l["level"]), l["msg"]]
                  for l in log if l.get("level") in ("error", "warn", "gate")]
        if issues:
            story.append(Paragraph("Warnings, errors and approvals", ST["h2"]))
            story.append(_table(["Time", "Type", "Message"], issues[:MAX_ROWS], [W * 0.1, W * 0.08, W * 0.82]))
        for rep in r.get("reports") or []:
            try:
                pre_s, post_s = storage.load_snapshot(tid, rep["pre"]), storage.load_snapshot(tid, rep["post"])
            except Exception as e:  # noqa: BLE001
                story.append(Paragraph(f"Snapshots unavailable: {escape(str(e))}", ST["p"]))
                continue
            story.append(Paragraph(f"Pre / post checks: {escape(rep['member'])}", ST["h2"]))
            story.append(Paragraph(f"Before: {escape(rep['pre'])}.log  ·  After: {escape(rep['post'])}.log", ST["small"]))
            d = diff_snapshots(pre_s, post_s)
            per = {(c["check"], c["scope"]): c for c in d["checks"]}
            sums_pre = {(x["check"], x["scope"]): x for x in pre_s.get("summary") or []}
            sums_post = {(x["check"], x["scope"]): x for x in post_s.get("summary") or []}
            rows = []
            for key in list(dict.fromkeys(list(sums_pre) + list(sums_post))):
                c = per.get(key) or {}
                ch = c.get("changes") or []
                worst = c.get("worst")
                rows.append([(sums_pre.get(key) or sums_post.get(key))["name"], "Global" if key[1] == "global" else f"VDOM {key[1]}",
                             (sums_pre.get(key) or {}).get("text", "—"), (sums_post.get(key) or {}).get("text", "—"),
                             (worst or "no change").title() if ch or worst else "No change", str(len(ch))])
            story.append(_table(["Check", "Scope", "Before", "After", "Result", "Changes"], rows,
                                [W * 0.18, W * 0.08, W * 0.29, W * 0.29, W * 0.09, W * 0.07], sev_col=4))
            t = d["totals"]
            story.append(Paragraph(f"Differences: <b>{t['critical']}</b> critical · <b>{t['warning']}</b> warning · "
                                   f"<b>{t['info']}</b> informational", ST["p"]))
            det = []
            for c in d["checks"]:
                for x in c.get("changes") or []:
                    if x["type"] == "changed":
                        what = f"{x['field']}: {_fmt(x['before'])} → {_fmt(x['after'])}"
                    elif x["type"] == "count":
                        what = f"Route count {x['before']} → {x['after']}"
                    elif x["type"] == "removed":
                        what = f"No longer present ({_fmt(x['before'])})"
                    else:
                        what = f"New ({_fmt(x['after'])})"
                    det.append([x["severity"].title(), c["name"], x["key"], what])
            if det:
                story.append(Paragraph("Check differences", ST["h3"]))
                story.append(_table(["Severity", "Check", "Item", "Change"], det[:MAX_ROWS],
                                    [W * 0.08, W * 0.15, W * 0.27, W * 0.5], sev_col=0))
                if len(det) > MAX_ROWS:
                    story.append(Paragraph(f"{len(det) - MAX_ROWS} more differences not shown.", ST["small"]))
            cfg = d.get("config")
            if d.get("config_note"):
                story.append(Paragraph("Configuration differences", ST["h3"]))
                story.append(Paragraph(escape(d["config_note"]), ST["p"]))
            if cfg:
                s = cfg["summary"]
                story.append(Paragraph("Configuration differences", ST["h3"]))
                story.append(Paragraph(f"{s['changed']} changed · {s['added']} added · {s['removed']} removed "
                                       "(secrets masked; new default settings commonly appear after upgrades).", ST["p"]))
                rows = [[{"changed": "Changed", "added": "Added", "removed": "Removed"}[x["type"]],
                         x["path"].replace(" > ", " › "), x.get("before", ""), x.get("after", "")] for x in cfg["changes"]]
                if rows:
                    story.append(_table(["Change", "Setting", "Before", "After"], rows[:MAX_ROWS],
                                        [W * 0.07, W * 0.53, W * 0.2, W * 0.2]))
                    if len(rows) > MAX_ROWS:
                        story.append(Paragraph(f"{len(rows) - MAX_ROWS} more configuration differences not shown.", ST["small"]))

    # ------------------------------------------------------------ full log
    story += [PageBreak(), Paragraph("Activity log", ST["h1"]),
              Paragraph("Every step of the run in order" + (", including each REST API and SSH call" if include_api_trace else "")
                        + ".", ST["p"]), Spacer(1, 4)]
    lines = []
    for l in batch.get("log") or []:
        if l.get("level") == "debug" or (l.get("level") == "detail" and not include_api_trace):
            continue
        src = (l.get("src") or "")[:14]
        lines.append(f"{l.get('t', '')}  {src:<14}  {l.get('msg', '')}")
    for i in range(0, len(lines), 90):
        story.append(Preformatted("\n".join(_wrap(x, 190) for x in lines[i:i + 90]), ST["mono"]))

    def page(canvas, doc_):
        canvas.saveState()
        canvas.setFillColor(DARK)
        canvas.rect(0, doc_.pagesize[1] - 8 * mm, doc_.pagesize[0], 8 * mm, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(12 * mm, doc_.pagesize[1] - 5.5 * mm, "FortiGate Upgrader · Upgrade report" + (" · SIMULATION" if sim else ""))
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(doc_.pagesize[0] - 12 * mm, doc_.pagesize[1] - 5.5 * mm, f"FortiOS {meta.get('target_version', '')} · {method}")
        canvas.setFillColor(GREY)
        canvas.drawString(12 * mm, 7 * mm, f"{when:%Y-%m-%d %H:%M:%S}")
        canvas.drawRightString(doc_.pagesize[0] - 12 * mm, 7 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=page, onLaterPages=page)
    return out


def _probes_section(pr: dict, W: float) -> list:
    """Service availability: a timeline of every probe plus outage statistics and windows."""
    from reportlab.graphics.shapes import Drawing, Line, Rect, String

    from .probes import fmt_ms
    out = [Spacer(1, 8), Paragraph("Service availability during the upgrade", ST["h2"]),
           Paragraph(f"Synthetic probes ran from the computer running FortiGate Upgrader. A run of failed probes "
                     f"lasting {fmt_ms(pr.get('min_outage_ms', 1000))} or more counts as an outage; shorter runs are "
                     "packet loss. Outage times are accurate to about one probe interval.", ST["small"])]
    t0, t1 = pr.get("started") or 0, pr.get("ended") or pr.get("now") or 0
    span = max(t1 - t0, 1)
    probes_ = pr["probes"]
    lw, row, top = 150, 16, 18
    h = top + row * len(probes_) + 14
    d = Drawing(W, h)
    x0, x1 = lw, W - 4
    X = lambda t: x0 + (x1 - x0) * (min(max(t, t0), t1) - t0) / span  # noqa: E731
    bms = pr.get("bucket_ms", 5000)
    for ph in pr.get("phases") or []:
        d.add(Line(X(ph["t"]), 4, X(ph["t"]), h - 4, strokeColor=GREY, strokeWidth=0.4, strokeDashArray=[1, 2]))
        d.add(String(X(ph["t"]) + 2, h - 10, ph["phase"].title(), fontSize=6.5, fillColor=GREY))
    for m in pr.get("markers") or []:
        d.add(Line(X(m["t"]), 10, X(m["t"]), h - 14, strokeColor=BLUE, strokeWidth=0.35))
    for i, p in enumerate(probes_):
        y = h - top - row * (i + 1)
        d.add(String(2, y + 4, (p["name"][:34]), fontSize=7, fillColor=DARK))
        d.add(Rect(x0, y + 2, x1 - x0, row - 6, fillColor=LIGHT, strokeColor=None))
        for b in p.get("buckets") or []:
            bt, sent, lost = b[0], b[1], b[2]
            col = GREEN if not lost else (RED if lost >= sent else AMBER)
            d.add(Rect(X(bt), y + 2, max(X(bt + bms) - X(bt), 0.6), row - 6, fillColor=col, strokeColor=None))
        for w in p.get("windows") or []:
            if w["kind"] == "outage":
                d.add(Rect(X(w["start"]), y + 1, max(X(w["end"]) - X(w["start"]), 1.2), row - 4, fillColor=RED,
                           strokeColor=None))
    d.add(String(x0, 0, dt.datetime.fromtimestamp(t0 / 1000).strftime("%H:%M:%S"), fontSize=6.5, fillColor=GREY))
    d.add(String(x1 - 26, 0, dt.datetime.fromtimestamp(t1 / 1000).strftime("%H:%M:%S"), fontSize=6.5, fillColor=GREY))
    out += [Spacer(1, 4), d, Paragraph('<font color="#2f9e5b">■</font> no loss  <font color="#d9822b">■</font> some loss  '
                                       '<font color="#d43a2f">■</font> outage  <font color="#2c7cc4">|</font> upgrade event',
                                       ST["small"]), Spacer(1, 6)]

    def rtt(ph):
        return f"{ph['rtt_avg']} ms (p95 {ph['rtt_p95']})" if ph and ph.get("rtt_avg") is not None else "—"
    rows = []
    for p in probes_:
        ph = p.get("phases") or {}
        rows.append([p["name"], p.get("method") or p["type"], f"{p['interval_ms']} ms",
                     f"{p['total']['sent']:,}", f"{p['total']['lost']:,}",
                     f"{p['availability_pct']:.2f} %" if p.get("availability_pct") is not None else "—",
                     str(p["outages"]), fmt_ms(p["downtime_ms"]) if p["outages"] else "—",
                     fmt_ms(p["longest_ms"]) if p["outages"] else "—",
                     rtt(ph.get("baseline")), rtt(ph.get("settle") or ph.get("upgrade"))])
    out.append(_table(["Probe", "Method", "Interval", "Sent", "Lost", "Availability", "Outages", "Total down",
                       "Longest", "Latency before", "Latency after"], rows,
                      [W * x for x in (0.2, 0.07, 0.06, 0.06, 0.05, 0.08, 0.06, 0.08, 0.08, 0.13, 0.13)]))
    wins = [(p, w) for p in probes_ for w in p.get("windows") or [] if w["kind"] == "outage"]
    if wins:
        fmt = lambda t: dt.datetime.fromtimestamp(t / 1000).strftime("%H:%M:%S.%f")[:-3]  # noqa: E731
        out.append(KeepTogether([Paragraph("Outage windows", ST["h3"]), _table(["Probe", "Down from", "Back at", "Duration", "Failed probes", "During", "Errors"],
                          [[p["name"], fmt(w["start"]), "still down" if w.get("ongoing") else fmt(w["end"]),
                            fmt_ms(w["duration_ms"]), str(w["fails"]),
                            (f"{w.get('during_src')}: " if w.get("during_src") else "") + (w.get("during") or w.get("phase", "")),
                            ", ".join(f"{k or 'no reply'} ×{v}" for k, v in (w.get("errors") or {}).items())]
                           for p, w in sorted(wins, key=lambda x: x[1]["start"])[:MAX_ROWS]],
                          [W * x for x in (0.2, 0.1, 0.1, 0.08, 0.08, 0.26, 0.18)])]))
    if pr.get("still_down"):
        out.append(Paragraph(f'<font color="#d43a2f"><b>Still down when monitoring stopped: '
                             f'{escape(", ".join(pr["still_down"]))}</b></font>', ST["p"]))
    return out


def _wrap(line: str, width: int) -> str:
    if len(line) <= width:
        return line
    parts = [line[i:i + width] for i in range(0, len(line), width)]
    return ("\n" + " " * 32).join(parts)

