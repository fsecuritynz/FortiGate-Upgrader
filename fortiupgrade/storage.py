"""Local persistence: targets (devices/clusters), secrets (OS keychain), snapshots, reports."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import uuid
from pathlib import Path

SERVICE = "FortiGate Upgrader"
APP_DIR = "FortiGate Upgrader"
LEGACY = "FortiUpgrade"  # name used by earlier builds; data and keychain entries are migrated
_lock = threading.Lock()


def data_dir() -> Path:
    if os.environ.get("FORTIUPGRADE_HOME"):
        p = Path(os.environ["FORTIUPGRADE_HOME"]).resolve()
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / APP_DIR
    elif sys.platform.startswith("win"):
        p = Path(os.environ.get("APPDATA", Path.home())) / APP_DIR
    else:
        p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_DIR
    legacy = p.with_name(LEGACY)
    if not os.environ.get("FORTIUPGRADE_HOME") and not p.exists() and legacy.is_dir():
        try:
            legacy.rename(p)
        except OSError:
            pass
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe(s: str) -> str:
    return re.sub(r"[^\w.\-]", "_", s)[:80]


# ------------------------------------------------------------------ secrets
# Kept in one encrypted vault (see vault.py) so the OS keychain is touched once per launch.
def set_secret(name: str, value: str | None):
    from . import vault
    vault.set(name, value)


def get_secret(name: str) -> str | None:
    from . import vault
    return vault.get(name)


# ------------------------------------------------------------------ targets
def _targets_file() -> Path:
    return data_dir() / "targets.json"


def list_targets() -> list[dict]:
    f = _targets_file()
    return json.loads(f.read_text()) if f.exists() else []


def get_target(tid: str) -> dict:
    for t in list_targets():
        if t["id"] == tid:
            return t
    raise KeyError(tid)


def save_target(t: dict, secrets: dict[str, dict] | None = None) -> dict:
    """secrets: {member_id: {"token": ..., "ssh_password": ...}}"""
    with _lock:
        ts = list_targets()
        t.setdefault("id", uuid.uuid4().hex[:10])
        for m in t.get("members", []):
            m.setdefault("id", uuid.uuid4().hex[:10])
        ts = [x for x in ts if x["id"] != t["id"]] + [t]
        _targets_file().write_text(json.dumps(ts, indent=2))
    for i, m in enumerate(t.get("members", [])):
        s = (secrets or {}).get(m["id"]) or (secrets or {}).get(str(i)) or {}
        if s.get("token"):
            set_secret(f"{m['id']}:token", s["token"])
        if s.get("ssh_password"):
            set_secret(f"{m['id']}:ssh", s["ssh_password"])
    return t


def delete_target(tid: str):
    t = get_target(tid)
    for m in t.get("members", []):
        set_secret(f"{m['id']}:token", None)
        set_secret(f"{m['id']}:ssh", None)
    with _lock:
        _targets_file().write_text(json.dumps([x for x in list_targets() if x["id"] != tid], indent=2))


# ---------------------------------------------------------------- snapshots
def _snap_dir(tid: str) -> Path:
    p = data_dir() / "snapshots" / _safe(tid)
    p.mkdir(parents=True, exist_ok=True)
    return p


def snapshot_log_path(tid: str, sid: str) -> Path:
    return _snap_dir(tid) / f"{_safe(sid)}.log"


def _snap_data(tid: str, sid: str) -> Path:
    d = _snap_dir(tid) / ".data"
    d.mkdir(exist_ok=True)
    return d / f"{_safe(sid)}.json"


def save_snapshot(tid: str, member_id: str, snap: dict) -> str:
    """Writes <hostname>-<Weekday>-<dd-mm-yyyy-hh-mm-ss>.log (human-readable) plus a JSON sidecar
    used for diffs. Returns the snapshot id (the log name without extension)."""
    import datetime as dt

    from . import logfile
    when = dt.datetime.fromisoformat(snap["meta"]["taken_at"])
    base = logfile.snapshot_name(snap["meta"]["device"].get("hostname", ""), when)
    sid, n = base, 2
    while snapshot_log_path(tid, sid).exists():
        sid, n = f"{base}-{n}", n + 1
    snap["meta"]["id"] = sid
    snap["meta"]["member_id"] = member_id
    snap["meta"]["file"] = f"{sid}.log"
    _snap_data(tid, sid).write_text(json.dumps(snap))
    snapshot_log_path(tid, sid).write_text(logfile.render(snap))
    return sid


def list_snapshots(tid: str) -> list[dict]:
    out = []
    for f in (_snap_dir(tid) / ".data").glob("*.json") if (_snap_dir(tid) / ".data").exists() else []:
        try:
            out.append(json.loads(f.read_text())["meta"])
        except (ValueError, KeyError):
            continue
    return sorted(out, key=lambda m: m.get("taken_at", ""), reverse=True)


def load_snapshot(tid: str, sid: str) -> dict:
    return json.loads(_snap_data(tid, sid).read_text())


def snapshots_dir(tid: str) -> Path:
    return _snap_dir(tid)


def save_report(tid: str, name: str, report: dict) -> Path:
    p = data_dir() / "reports" / _safe(tid)
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"{_safe(name)}.json"
    f.write_text(json.dumps(report, indent=1))
    return f


# ------------------------------------------------------------ release notes
def notes_dir() -> Path:
    p = data_dir() / "release_notes"
    p.mkdir(exist_ok=True)
    return p


def user_rules_file() -> Path:
    return data_dir() / "rules.yaml"


def user_paths_file() -> Path:
    return data_dir() / "upgrade_paths.yaml"


# ------------------------------------------------------------- job history
def jobs_dir() -> Path:
    p = data_dir() / "jobs"
    p.mkdir(exist_ok=True)
    return p


def save_job(d: dict):
    d = dict(d)
    d.pop("_upload_sem", None)
    (jobs_dir() / f"{d['id']}.json").write_text(json.dumps(d, default=str))


def load_job(jid: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{6,32}", jid or ""):
        return None
    f = jobs_dir() / f"{jid}.json"
    return json.loads(f.read_text()) if f.exists() else None


def list_jobs() -> list[dict]:
    out = []
    for f in jobs_dir().glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        d.pop("log", None)
        out.append(d)
    return sorted(out, key=lambda d: d.get("created") or "", reverse=True)


# ------------------------------------------------------- config history
def _cfg_dir(tid: str) -> Path:
    p = data_dir() / "configs" / _safe(tid)
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_live_config(tid: str, member_id: str, device: dict, text: str) -> dict:
    """Store a configuration fetched on demand as <hostname>-<Weekday>-<dd-mm-yyyy-hh-mm-ss>.conf."""
    import datetime as dt

    from . import logfile
    now = dt.datetime.now()
    base = logfile.snapshot_name(device.get("hostname", ""), now)
    cid, n = base, 2
    while (_cfg_dir(tid) / f"{cid}.conf").exists():
        cid, n = f"{base}-{n}", n + 1
    (_cfg_dir(tid) / f"{cid}.conf").write_text(text)
    meta = {"ref": f"live:{cid}", "id": cid, "kind": "live", "label": "fetched", "member_id": member_id,
            "taken_at": now.isoformat(timespec="seconds"), "version": str(device.get("version", "")),
            "build": device.get("build"), "hostname": device.get("hostname", ""),
            "lines": len(text.splitlines()), "file": f"{cid}.conf"}
    (_cfg_dir(tid) / f"{cid}.json").write_text(json.dumps(meta))
    return meta


def list_configs(tid: str) -> list[dict]:
    out = []
    d = _snap_dir(tid) / ".data"
    for f in d.glob("*.json") if d.exists() else []:
        try:
            snap = json.loads(f.read_text())
        except ValueError:
            continue
        cfg = snap.get("config")
        from .client import looks_like_config
        if not cfg or not looks_like_config(cfg):
            continue
        m = snap["meta"]
        out.append({"ref": f"snap:{m['id']}", "id": m["id"], "kind": "collection", "label": m.get("label", ""),
                    "member_id": m.get("member_id"), "taken_at": m.get("taken_at", ""),
                    "version": str(m["device"].get("version", "")), "build": m["device"].get("build"),
                    "hostname": m["device"].get("hostname", ""), "lines": len(cfg.splitlines()),
                    "file": m.get("file", "")})
    for f in _cfg_dir(tid).glob("*.json"):
        try:
            out.append(json.loads(f.read_text()))
        except ValueError:
            continue
    return sorted(out, key=lambda c: (c.get("taken_at", ""), len(c.get("ref", "")), c.get("ref", "")), reverse=True)


def load_config(tid: str, ref: str) -> str:
    kind, _, cid = ref.partition(":")
    if kind == "snap":
        return load_snapshot(tid, cid).get("config") or ""
    if kind == "live":
        f = _cfg_dir(tid) / f"{_safe(cid)}.conf"
        if f.exists():
            return f.read_text()
    raise KeyError(ref)
