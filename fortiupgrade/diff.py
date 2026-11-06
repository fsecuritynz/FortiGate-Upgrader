"""Diffing of snapshots: structured records, CLI text and FortiOS configuration."""
from __future__ import annotations

import difflib
import re

from .checks import CHECKS_BY_ID, CRIT, INFO, WARN, Check, record_key

_SEV_ORDER = {CRIT: 0, WARN: 1, INFO: 2}


def diff_records(check: Check, before: list[dict], after: list[dict]) -> list[dict]:
    sev = check.severity or (lambda *a: WARN)
    b = {record_key(check, r): r for r in before}
    a = {record_key(check, r): r for r in after}
    changes = []
    for k in sorted(b.keys() - a.keys()):
        if check.id == "logs":
            continue  # older events roll out of the log buffer; only new events matter
        changes.append({"type": "removed", "key": k, "before": b[k], "severity": sev("removed", k, None, b[k], None)})
    for k in sorted(a.keys() - b.keys()):
        changes.append({"type": "added", "key": k, "after": a[k], "severity": sev("added", k, None, None, a[k])})
    for k in sorted(a.keys() & b.keys()):
        for f in check.fields:
            if b[k].get(f) != a[k].get(f):
                changes.append({"type": "changed", "key": k, "field": f, "before": b[k].get(f),
                                "after": a[k].get(f), "severity": sev("changed", k, f, b[k].get(f), a[k].get(f))})
    return changes


_VOLATILE = [
    (re.compile(r"\b\d+d(ays?)?[ ,]*\d+h(ours?)?[ ,]*\d+m(in(utes?)?)?([ ,]*\d+s(ec(onds?)?)?)?\b", re.I), "<uptime>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}\b"), "<time>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<date>"),
    (re.compile(r"(Up/Down\s+State/PfxRcd.*)", re.I), r"\1"),
]


def normalise_text(t: str) -> list[str]:
    lines = []
    for line in (t or "").splitlines():
        for rx, rep in _VOLATILE:
            line = rx.sub(rep, line)
        lines.append(line.rstrip())
    return lines


def diff_text(before: str, after: str, name: str = "") -> str:
    return "\n".join(difflib.unified_diff(normalise_text(before), normalise_text(after),
                                          f"pre/{name}", f"post/{name}", lineterm="", n=2))


# ------------------------------------------------------------ config diff
_CFG_NOISE = [
    re.compile(r"^#conf_file_ver="),
    re.compile(r"^#buildno="),
    re.compile(r"^#global_vdom="),
]
_ENC = re.compile(r"(ENC\s+)\S+")
_BLOB_START = re.compile(r'^\s*set (private-key|certificate|csr|ca|crl|public-key|key-file|password\d?)\s+"-----BEGIN')


def normalise_config(text: str) -> list[str]:
    out, in_blob = [], False
    for line in (text or "").splitlines():
        if in_blob:
            if "-----END" in line:
                in_blob = False
            continue
        if _BLOB_START.match(line):
            out.append(line.split('"', 1)[0] + '"<pem redacted>"')
            in_blob = "-----END" not in line
            continue
        if any(rx.match(line) for rx in _CFG_NOISE):
            continue
        out.append(_ENC.sub(r"\1<redacted>", line.rstrip()))
    return out


def mask_config(text: str) -> str:
    """The configuration with ENC secrets and PEM blocks redacted (for display)."""
    out, in_blob = [], False
    for line in (text or "").splitlines():
        if in_blob:
            in_blob = "-----END" not in line
            continue
        if _BLOB_START.match(line):
            out.append(line.split('"', 1)[0] + '"<pem redacted>"')
            in_blob = "-----END" not in line
            continue
        out.append(_ENC.sub(r"\1<redacted>", line))
    return "\n".join(out)


def side_by_side(before: str, after: str, context: int = 4) -> list[dict]:
    """Rows for a split diff view: {t: same|chg|del|add|gap, a: (line no, text), b: (line no, text)}."""
    a, b = normalise_config(before), normalise_config(after)
    rows = []
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context):
        if rows:
            rows.append({"t": "gap"})
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows += [{"t": "same", "a": [i + 1, a[i]], "b": [j + 1, b[j]]} for i, j in zip(range(i1, i2), range(j1, j2))]
            elif tag == "replace":
                n = max(i2 - i1, j2 - j1)
                for k in range(n):
                    ai, bj = i1 + k, j1 + k
                    rows.append({"t": "chg", "a": [ai + 1, a[ai]] if ai < i2 else None,
                                 "b": [bj + 1, b[bj]] if bj < j2 else None})
            elif tag == "delete":
                rows += [{"t": "del", "a": [i + 1, a[i]], "b": None} for i in range(i1, i2)]
            else:
                rows += [{"t": "add", "a": None, "b": [j + 1, b[j]]} for j in range(j1, j2)]
    return rows


def parse_config(text: str) -> dict[str, str]:
    """Flatten FortiOS config into {path: value} where path is e.g.
    'config system interface > edit "port1" > set ip'."""
    flat: dict[str, str] = {}
    stack: list[str] = []
    for raw in normalise_config(text):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("config "):
            stack.append(s)
        elif s.startswith("edit "):
            stack.append(s)
            flat[" > ".join(stack)] = "(exists)"
        elif s == "next":
            if stack and stack[-1].startswith("edit "):
                stack.pop()
        elif s == "end":
            if stack and stack[-1].startswith("edit "):
                stack.pop()
            if stack:
                stack.pop()
        elif s.startswith(("set ", "unset ", "append ")):
            parts = s.split(None, 2)
            if len(parts) >= 2:
                flat[" > ".join(stack + [f"set {parts[1]}"])] = parts[2] if len(parts) > 2 else ""
    return flat


def diff_config(before: str, after: str) -> dict:
    b, a = parse_config(before), parse_config(after)
    changes = []
    for k in sorted(b.keys() - a.keys()):
        changes.append({"type": "removed", "path": k, "before": b[k]})
    for k in sorted(a.keys() - b.keys()):
        changes.append({"type": "added", "path": k, "after": a[k]})
    for k in sorted(a.keys() & b.keys()):
        if a[k] != b[k]:
            changes.append({"type": "changed", "path": k, "before": b[k], "after": a[k]})
    unified = "\n".join(difflib.unified_diff(normalise_config(before), normalise_config(after),
                                             "pre/config", "post/config", lineterm="", n=3))
    return {"changes": changes, "unified": unified,
            "summary": {t: sum(1 for c in changes if c["type"] == t) for t in ("added", "removed", "changed")}}


# ---------------------------------------------------------- snapshot diff
def diff_snapshots(pre: dict, post: dict, only: set[str] | None = None, include_config: bool = True) -> dict:
    report = {"pre": pre["meta"], "post": post["meta"], "checks": [], "config": None,
              "totals": {CRIT: 0, WARN: 0, INFO: 0}}
    for cid, scopes in pre["results"].items():
        check = CHECKS_BY_ID.get(cid)
        if not check or cid not in post["results"] or (only and cid not in only):
            continue
        for scope, before in scopes.items():
            after = post["results"][cid].get(scope)
            if after is None:
                continue
            entry = {"check": cid, "name": check.name, "scope": scope, "kind": check.kind}
            if check.kind == "text":
                entry["diff"] = diff_text(before, after, cid)
                entry["changes"] = []
            else:
                if not check.fields and check.severity is None:
                    continue
                entry["changes"] = diff_records(check, before, after)
                entry["before_count"], entry["after_count"] = len(before), len(after)
                if check.id.startswith("routes") and before:
                    drop = (len(before) - len(after)) / len(before)
                    if drop > 0.1:
                        entry["changes"].insert(0, {"type": "count", "key": "route count",
                                                    "before": len(before), "after": len(after),
                                                    "severity": CRIT})
            entry["changes"].sort(key=lambda c: _SEV_ORDER.get(c["severity"], 3))
            for c in entry["changes"]:
                report["totals"][c["severity"]] += 1
            entry["worst"] = entry["changes"][0]["severity"] if entry["changes"] else None
            report["checks"].append(entry)
    from .client import looks_like_config
    if include_config and pre.get("config") is not None and post.get("config") is not None:
        bad = [n for n, s in (("before", pre), ("after", post)) if not looks_like_config(s["config"])]
        if bad:
            report["config_note"] = (f"Configuration not compared: the {' and '.join(bad)} copy is not a FortiOS "
                                     "configuration (the FortiGate refused the backup).")
    if include_config and not report.get("config_note") and pre.get("config") is not None and post.get("config") is not None:
        report["config"] = diff_config(pre["config"], post["config"])
    report["checks"].sort(key=lambda e: (_SEV_ORDER.get(e["worst"], 3), e["check"], e["scope"]))
    return report
