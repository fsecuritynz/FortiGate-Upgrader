"""Human-readable .log rendering of a snapshot and the <hostname>-<day>-<dd-mm-yyyy-hh-mm-ss> naming."""
from __future__ import annotations

import datetime as dt
import re

from .checks import CHECKS_BY_ID, summarise

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def snapshot_name(hostname: str, when: dt.datetime) -> str:
    host = re.sub(r"[^\w.\-]", "_", hostname or "fortigate").strip("_") or "fortigate"
    return f"{host}-{WEEKDAYS[when.weekday()]}-{when.strftime('%d-%m-%Y-%H-%M-%S')}"


def _table(rows: list[dict]) -> str:
    if not rows:
        return "  (none)\n"
    cols = list(rows[0].keys())
    data = [[str(r.get(c, "")) for c in cols] for r in rows]
    w = [min(48, max(len(c), *(len(d[i]) for d in data))) for i, c in enumerate(cols)]
    line = lambda vals: "  " + "  ".join(v[:48].ljust(w[i]) for i, v in enumerate(vals))  # noqa: E731
    out = [line(cols), "  " + "  ".join("-" * x for x in w)]
    out += [line(d) for d in data]
    return "\n".join(out) + "\n"


def render(snap: dict) -> str:
    m = snap["meta"]
    d = m["device"]
    when = dt.datetime.fromisoformat(m["taken_at"])
    bar = "=" * 100
    out = [
        bar,
        "FortiGate Upgrader - data collection",
        bar,
        f"Hostname       : {d.get('hostname')}",
        f"Model          : {d.get('model')}",
        f"Serial number  : {d.get('serial')}",
        f"FortiOS        : v{d.get('version')} build {d.get('build')}",
        f"HA mode        : {d.get('ha_mode')}",
        f"VDOM mode      : {'multi-vdom' if d.get('vdom_mode') else 'no-vdom'}  ({', '.join(m.get('vdoms', []))})",
        f"Management     : {(d.get('management') or {}).get('summary', 'local')}",
        f"Collection     : {m.get('label')}",
        f"Collected at   : {WEEKDAYS[when.weekday()]} {when.strftime('%d-%m-%Y %H:%M:%S')}",
        "",
        "SUMMARY",
    ]
    for cid, scopes in snap["results"].items():
        c = CHECKS_BY_ID.get(cid)
        for scope, data in scopes.items():
            out.append(f"  {(c.name if c else cid):<45} {('Global' if scope == 'global' else 'VDOM ' + scope):<16} "
                       f"{summarise(c, data) if c else ''}")
    for cid, scopes in snap["results"].items():
        c = CHECKS_BY_ID.get(cid)
        for scope, data in scopes.items():
            out += ["", bar, f"{c.name if c else cid}  [{'Global' if scope == 'global' else 'VDOM ' + scope}]", bar]
            out.append(data.rstrip() + "\n" if isinstance(data, str) else _table(data))
    if snap.get("errors"):
        out += ["", bar, "Checks skipped or failed", bar]
        for cid, e in snap["errors"].items():
            for scope, msg in e.items():
                out.append(f"  {cid} [{scope}]: {msg}")
    if snap.get("config"):
        out += ["", bar, "Configuration backup", bar, snap["config"]]
    return "\n".join(out) + "\n"
