"""FortiGate Upgrader local HTTP API + static UI. Binds to 127.0.0.1 only and requires a per-launch token."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __build__, __version__, version_string
from . import hainfo, images, jobs, notes_cache, orchestrator, probes, release_notes, storage
from .client import FortiGateClient, FortiAPIError
from .checks import Ctx, c_ha, c_interfaces, c_license, c_resources, is_vm, physical_ports, vm_series
from .version import FosVersion
from .checks import CHECKS
from .diff import diff_snapshots

WEB = Path(__file__).parent / "web"
TOKEN = os.environ.get("FORTIUPGRADE_TOKEN") or pysecrets.token_urlsafe(24)
FILE_PICKER = None  # set by desktop.py when running inside pywebview

app = FastAPI(title="FortiGate Upgrader", docs_url=None, redoc_url=None)


def _setup_logging():
    """Errors go to <data>/logs/fortigate-upgrader.log (the packaged app has no console)."""
    import logging
    from logging.handlers import RotatingFileHandler
    d = storage.data_dir() / "logs"
    d.mkdir(exist_ok=True)
    h = RotatingFileHandler(d / "fortigate-upgrader.log", maxBytes=5_000_000, backupCount=3)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    lg = logging.getLogger("fortiupgrade")
    if not any(isinstance(x, RotatingFileHandler) for x in lg.handlers):
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    return lg


LOG = _setup_logging()


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Log the traceback and tell the UI what actually went wrong instead of 'Internal Server Error'."""
    LOG.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc} (details in logs/fortigate-upgrader.log)"},
                        status_code=500)


@app.middleware("http")
async def auth(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost", "testserver"):
        return JSONResponse({"detail": "bad host"}, 403)
    if request.url.path.startswith("/api/") and request.headers.get("x-auth") != TOKEN:
        return JSONResponse({"detail": "unauthorised"}, 401)
    return await call_next(request)


def _target(tid):
    try:
        return storage.get_target(tid)
    except KeyError:
        raise HTTPException(404, "target not found")


def _member(t, mid):
    for m in t["members"]:
        if m["id"] == mid:
            return m
    raise HTTPException(404, "member not found")


# ------------------------------------------------------------------ meta
@app.get("/api/meta")
def meta():
    return {"checks": [{"id": c.id, "name": c.name, "scope": c.scope, "needs_ssh": c.needs_ssh,
                        "default": c.default, "kind": c.kind, "description": c.description} for c in CHECKS],
            "fgcp_modes": orchestrator.FGCP_MODES, "fgsp_modes": orchestrator.FGSP_MODES,
            "data_dir": str(storage.data_dir()), "native_picker": FILE_PICKER is not None,
            "version": __version__, "build": __build__, "version_string": version_string()}


@app.get("/api/update")
def update_status(refresh: bool = False):
    from . import updater
    return updater.check(force=True) if refresh else updater.status()


@app.post("/api/update/download")
def update_download():
    from . import updater
    try:
        return updater.download()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/update/install")
def update_install():
    from . import updater
    try:
        return updater.install()
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


@app.put("/api/update/prefs")
def update_prefs(body: dict = Body(...)):
    from . import updater
    updater.set_prefs(auto=body.get("auto"), skip=body.get("skip"))
    return updater.status()


@app.get("/api/changelog")
def changelog():
    return json.loads((Path(__file__).parent / "changelog.json").read_text())


@app.get("/api/settings")
def get_settings():
    from . import vault
    return {**vault.settings(), "keychain_available": vault.keyring is not None,
            "keychain_name": "Credential Manager" if sys.platform.startswith("win") else "Keychain",
            "data_dir": str(storage.data_dir())}


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    from . import vault
    if "secret_store" in body:
        try:
            vault.set_store(body["secret_store"])
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e)) from e
    return get_settings()


@app.post("/api/pick-files")
def pick_files():
    if FILE_PICKER is None:
        raise HTTPException(400, "native file picker unavailable")
    return FILE_PICKER() or []


# --------------------------------------------------------------- targets
@app.get("/api/targets")
def targets():
    out = []
    for t in storage.list_targets():
        t = dict(t)
        t["members"] = [dict(m, has_token=bool(storage.get_secret(f"{m['id']}:token")),
                             has_ssh=bool(storage.get_secret(f"{m['id']}:ssh"))) for m in t["members"]]
        out.append(t)
    return out


@app.post("/api/targets")
def save_target(body: dict = Body(...)):
    if body.get("type") not in ("standalone", "fgcp", "fgsp"):
        raise HTTPException(400, "type must be standalone, fgcp or fgsp")
    members = body.get("members") or []
    if not members:
        raise HTTPException(400, "at least one member is required")
    if body["type"] == "standalone" and len(members) != 1:
        raise HTTPException(400, "standalone targets have exactly one member")
    secrets = {}
    clean = []
    for i, m in enumerate(members):
        if not m.get("host"):
            raise HTTPException(400, f"member {i + 1}: host is required")
        s = {"token": m.pop("token", None), "ssh_password": m.pop("ssh_password", None)}
        m.pop("has_token", None)
        m.pop("has_ssh", None)
        clean.append(m)
        secrets[m.get("id") or str(i)] = s
    t = {"id": body.get("id"), "name": body.get("name") or members[0]["host"], "type": body["type"],
         "members": clean, "notes": body.get("notes", "")}
    if not t["id"]:
        t.pop("id")
    # secrets for new members are keyed by index; storage assigns ids first
    saved = storage.save_target(t, secrets)
    return saved


@app.post("/api/targets/{tid}/members/{mid}/token")
def reveal_token(tid: str, mid: str):
    """The stored REST API token of one member, for the Show button in the device form. POST and
    no-store so it isn't cached; the value is never written to logs or job output."""
    t = _target(tid)
    if not any(m["id"] == mid for m in t["members"]):
        raise HTTPException(404, "member not found")
    tok = storage.get_secret(f"{mid}:token")
    if not tok:
        raise HTTPException(404, "no API token is stored for this member")
    return JSONResponse({"token": tok}, headers={"Cache-Control": "no-store"})


@app.delete("/api/targets/{tid}")
def delete_target(tid: str):
    storage.delete_target(tid)
    return {"ok": True}


@app.post("/api/targets/{tid}/test")
def test_target(tid: str):
    t = _target(tid)
    out = []
    for m in t["members"]:
        try:
            with orchestrator.client_for(m, timeout=15) as c:
                out.append({"member": m["id"], "ok": True, **c.discover().to_dict()})
        except Exception as e:  # noqa: BLE001
            out.append({"member": m["id"], "ok": False, "error": str(e)})
    return out


@app.get("/api/targets/{tid}/firmware")
def firmware(tid: str, member: str | None = None):
    t = _target(tid)
    m = _member(t, member) if member else t["members"][0]
    with orchestrator.client_for(m) as c:
        fw = c.firmware()
    return {"current": fw.get("current"),
            "available": [{k: a.get(k) for k in ("id", "version", "build", "maturity", "release-type", "notes")}
                          for a in fw.get("available", []) or []]}


# -------------------------------------------------------------- snapshots
@app.post("/api/targets/{tid}/snapshot")
def snapshot(tid: str, body: dict = Body(...)):
    t = _target(tid)
    ms = [m for m in t["members"] if m["id"] in (body.get("member_ids") or [t["members"][0]["id"]])]
    label = (body.get("label") or "manual").strip()[:40]

    def run(job):
        return [orchestrator.snapshot_member(job, t, m, label, body.get("check_ids"), body.get("vdoms"),
                                             body.get("use_ssh", True)) for m in ms]

    return jobs.start("snapshot", f"Data collection '{label}' of {t['name']}", run, target_id=tid,
                      meta={"label": label}).to_dict()


@app.get("/api/targets/{tid}/snapshots")
def snapshots(tid: str):
    return storage.list_snapshots(tid)


@app.get("/api/targets/{tid}/snapshots/{sid}")
def snapshot_get(tid: str, sid: str):
    s = storage.load_snapshot(tid, sid)
    s["config_lines"] = len((s.get("config") or "").splitlines())
    s.pop("config", None)
    return s


def _pair_snapshots(snaps: list[dict]) -> None:
    """Link each pre-upgrade collection to the post-upgrade one that followed it for the same
    unit (sets "pair" on both). snaps is newest first."""
    open_pre: dict[str, dict] = {}
    for s in reversed(snaps):
        key = s.get("member_id") or ""
        if s.get("label") == "pre-upgrade":
            open_pre[key] = s
        elif s.get("label") == "post-upgrade" and key in open_pre:
            pre = open_pre.pop(key)
            pre["pair"], s["pair"] = s["id"], pre["id"]


@app.get("/api/snapshots")
def all_snapshots():
    """Every data collection of every device, newest first, with its pre/post partner."""
    out = []
    names = {}
    for t in storage.list_targets():
        names[t["id"]] = t
        snaps = storage.list_snapshots(t["id"])
        _pair_snapshots(snaps)
        member = {m["id"]: (m.get("name") or m["host"]) for m in t.get("members", [])}
        for s in snaps:
            d = s.get("device") or {}
            out.append({"target_id": t["id"], "target": t["name"], "id": s["id"], "file": s.get("file") or s["id"] + ".log",
                        "label": s.get("label"), "taken_at": s.get("taken_at"), "member_id": s.get("member_id"),
                        "member": member.get(s.get("member_id"), ""), "hostname": d.get("hostname") or "",
                        "model": d.get("model") or "", "version": d.get("version") or "", "build": d.get("build"),
                        "vdoms": s.get("vdoms") or [], "pair": s.get("pair")})
    return sorted(out, key=lambda x: x["taken_at"] or "", reverse=True)


def _snap_log_text(tid: str, sid: str, snap: dict) -> str:
    from . import logfile
    from .diff import mask_config
    p = storage.snapshot_log_path(tid, sid)
    return mask_config(p.read_text() if p.exists() else logfile.render(snap))


@app.get("/api/targets/{tid}/compare")
def compare(tid: str, a: str, b: str, full: bool = False):
    """Before/after comparison of two collections: delta summary, structured diff and the two
    collection files side by side (secrets masked)."""
    from .diff import compare_summary, side_by_side_text
    _target(tid)
    try:
        pre, post = storage.load_snapshot(tid, a), storage.load_snapshot(tid, b)
    except (FileNotFoundError, KeyError) as e:
        raise HTTPException(404, "collection not found") from e
    d = diff_snapshots(pre, post)
    rows = side_by_side_text(_snap_log_text(tid, a, pre), _snap_log_text(tid, b, post), None if full else 3)
    cfg = d.pop("config", None)
    d["config_summary"] = cfg["summary"] if cfg else None
    d["config_changes"] = cfg["changes"][:2000] if cfg else []
    d["summary"] = compare_summary(pre, post, d)
    d["rows"] = rows
    d["full"] = full
    d["changed_lines"] = sum(1 for r in rows if r["t"] in ("chg", "add", "del"))
    return d


@app.get("/api/targets/{tid}/snapshots/{sid}/log", response_class=PlainTextResponse)
def snapshot_log(tid: str, sid: str):
    return storage.snapshot_log_path(tid, sid).read_text()


@app.get("/api/targets/{tid}/snapshots/{sid}/config", response_class=PlainTextResponse)
def snapshot_config(tid: str, sid: str):
    return storage.load_snapshot(tid, sid).get("config") or ""


@app.get("/api/targets/{tid}/configs")
def configs(tid: str):
    _target(tid)
    return storage.list_configs(tid)


@app.post("/api/targets/{tid}/configs/fetch")
def config_fetch(tid: str, body: dict = Body(default={})):
    """Read the running configuration now (read-only) and keep it in the device's config history.
    If it is identical to the newest stored version, that version is returned instead."""
    t = _target(tid)
    m = next((x for x in t["members"] if x["id"] == body.get("member_id")), t["members"][0])
    try:
        with orchestrator.client_for(m, timeout=60) as c:
            info = c.discover()
            try:
                text = c.backup_config("global")
            except FortiAPIError as e:
                if not m.get("ssh_user"):
                    raise RuntimeError(f"{e}. Add SSH credentials to the device to read it over SSH instead.") from e
                from .snapshot import config_via_ssh
                with orchestrator.ssh_for(m) as ssh:
                    text = config_via_ssh(ssh, info.vdom_mode)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not read the configuration from {m.get('name') or m['host']}: {e}") from e
    from .diff import normalise_config
    norm = normalise_config(text)
    for prev in storage.list_configs(tid):
        if prev.get("member_id") not in (None, m["id"]):
            continue
        try:
            if normalise_config(storage.load_config(tid, prev["ref"])) == norm:
                return {**prev, "unchanged": True}
        except KeyError:
            pass
        break
    dev = {"hostname": info.hostname, "version": str(info.version), "build": info.version.build}
    return {**storage.save_live_config(tid, m["id"], dev, text), "unchanged": False}


@app.get("/api/targets/{tid}/configs/view", response_class=PlainTextResponse)
def config_view(tid: str, ref: str, raw: bool = False):
    from .diff import mask_config
    try:
        text = storage.load_config(tid, ref)
    except (KeyError, FileNotFoundError) as e:
        raise HTTPException(404, "configuration not found") from e
    return text if raw else mask_config(text)


@app.get("/api/targets/{tid}/configs/diff")
def config_diff(tid: str, a: str, b: str, context: int = 4):
    from .diff import diff_config, side_by_side
    try:
        ta, tb = storage.load_config(tid, a), storage.load_config(tid, b)
    except (KeyError, FileNotFoundError) as e:
        raise HTTPException(404, "configuration not found") from e
    meta = {c["ref"]: c for c in storage.list_configs(tid)}
    d = diff_config(ta, tb)
    return {"a": meta.get(a, {"ref": a}), "b": meta.get(b, {"ref": b}), "summary": d["summary"],
            "changes": d["changes"], "rows": side_by_side(ta, tb, max(0, min(context, 50)))}


@app.get("/api/targets/{tid}/diff")
def diff(tid: str, pre: str, post: str, checks: str = "", config: bool = True):
    only = {c for c in checks.split(",") if c} or None
    return diff_snapshots(storage.load_snapshot(tid, pre), storage.load_snapshot(tid, post), only,
                          include_config=config and not only)


@app.get("/api/targets/{tid}/latest")
def latest(tid: str, member: str = ""):
    """Most recent data collection for the device (optionally one member)."""
    _target(tid)
    for s in storage.list_snapshots(tid):
        if member and s.get("member_id") not in (None, member):
            continue
        snap = storage.load_snapshot(tid, s["id"])
        snap.pop("config", None)
        snap["id"] = s["id"]
        return snap
    return None


def _member_overview(m):
    """Live dashboard data of one address: identity, licences, resources, interfaces, HA."""
    out = {"member": m["id"], "name": m.get("name") or m["host"], "host": m["host"], "port": m.get("port", 443)}
    try:
        with orchestrator.client_for(m, timeout=15) as c:
            info = c.discover()
            out.update(ok=True, **info.to_dict())
            ctx = Ctx(client=c, vdom_mode=info.vdom_mode)
            for key, fn in (("licences", c_license), ("resources", c_resources), ("ha", c_ha)):
                try:
                    out[key] = fn(ctx, None)
                except Exception as e:  # noqa: BLE001
                    out[key + "_error"] = str(e)
            if is_vm(info.model, info.serial):
                lic_vm, vmi = {}, {}
                try:
                    lic_vm = (c.monitor("license/status") or {}).get("vm") or {}
                except Exception:  # noqa: BLE001
                    pass
                try:
                    vmi = c.monitor("system/vm-information") or {}
                    vmi = vmi[0] if isinstance(vmi, list) and vmi else vmi
                    vmi = vmi if isinstance(vmi, dict) else {}
                except Exception:  # noqa: BLE001
                    pass
                out["vm"] = vm_series(info.model, info.serial, lic_vm, vmi)
                out["vm_raw"] = {"license": lic_vm, "vm_information": vmi}
            try:
                out["interfaces"] = c_interfaces(ctx, None if not info.vdom_mode else None)
            except Exception as e:  # noqa: BLE001
                out["interfaces_error"] = str(e)
            try:
                out["ports"] = physical_ports(ctx)
            except Exception as e:  # noqa: BLE001
                out["ports_error"] = str(e)
            try:
                out["mgmt"] = hainfo.mgmt_info(c)
                out["ha_info"] = hainfo.ha_info(c)
                out["address"] = hainfo.address_role(m["host"], out["mgmt"], info.ha_mode)
            except Exception as e:  # noqa: BLE001
                out["ha_error"] = str(e)
            try:
                hacfg = c.cmdb("system/ha") or {}
                out["ha_config"] = {k: hacfg.get(k) for k in ("mode", "group-name", "group-id", "priority",
                                                              "override", "upgrade-mode",
                                                              "uninterruptible-upgrade") if k in hacfg}
            except Exception:  # noqa: BLE001
                pass
            try:
                fw = c.monitor("system/firmware") or {}
                cur = fw.get("current") or {}
                out["release"] = {"maturity": cur.get("maturity"), "release_type": cur.get("release-type"),
                                  "notes": cur.get("notes")}
                newer = [a for a in fw.get("available") or []
                         if FosVersion.parse(a.get("version")) > info.version]
                out["newer"] = sorted({str(FosVersion.parse(a["version"])) for a in newer},
                                      key=FosVersion.parse, reverse=True)
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        out.update(ok=False, error=str(e))
    return out


@app.get("/api/targets/{tid}/overview")
def overview(tid: str):
    """Live dashboard data for every member: identity, licences, resources, interfaces, HA."""
    t = _target(tid)
    one = _member_overview

    with ThreadPoolExecutor(max_workers=8) as ex:
        members = list(ex.map(one, t["members"]))
    ok = next((m for m in members if m.get("ok")), None)
    if ok:
        _INV[tid] = (time.time(), {k: v for k, v in ok.items() if k in (
            "ok", "hostname", "serial", "model", "version", "build", "vdom_mode", "vdoms", "ha_mode", "fgsp",
            "model_code", "management")})
    units, unit_notes = _cluster_unit_overviews(t, members)
    return {"target": tid, "members": members, "units": units, "unit_notes": unit_notes}


def _own_addresses(x: dict) -> list[str]:
    ded = [i.get("interface") for i in ((x.get("ha_info") or {}).get("ha_mgmt_interfaces") or []) if i.get("interface")]
    return [a["addr"] for a in hainfo.unit_addresses(x.get("mgmt") or {}, True, ded)]


def _cluster_unit_overviews(t: dict, members: list[dict]) -> tuple[list[dict], list[str]]:
    """For an FGCP cluster: one entry per physical unit, each read through an address that reaches
    that unit itself (its management-ip or dedicated HA management interface) when there is one.

    Unit addresses saved on the device are used first. For a unit without one, its own address is
    found with cluster_units() (the primary's interfaces, or over SSH from the primary) and read
    with the device's API token, which HA keeps the same on every unit."""
    src = next((x for x in members if x.get("ok") and x.get("ha_mode") in ("a-p", "a-a")
                and len((x.get("ha_info") or {}).get("peers") or []) >= 2), None)
    if not src:
        return [], []
    peers = src["ha_info"]["peers"]
    notes: list[str] = []
    by_serial: dict[str, dict] = {}
    primary = next((p["serial"] for p in peers if p.get("primary")), None)
    first = t["members"][0]["id"]
    for x in members:
        if not x.get("ok"):
            continue
        # a unit's own address: listed as its management-ip / HA management address, any extra
        # (unit) address of a cluster, or anything that reaches a secondary (secondaries never
        # answer on the shared cluster address)
        own = x["host"] in _own_addresses(x) or (t["type"] == "fgcp" and x["member"] != first) or x["serial"] != primary
        cur = by_serial.get(x["serial"])
        if not cur or (own and not cur["_own"]):
            by_serial[x["serial"]] = dict(x, _own=own, reached=("its own address " if own else "the cluster address ") + x["host"])
    missing = [p for p in peers if p["serial"] not in by_serial]
    if missing:
        try:
            cu = cluster_units(t)
        except Exception as e:  # noqa: BLE001
            cu = {"units": []}
            notes.append(f"Could not look up the other units' addresses: {e}")
        base = next((m for m in t["members"] if any(x.get("ok") and x["member"] == m["id"] for x in members)), t["members"][0])
        jobs_ = []
        for p in missing:
            u = next((u for u in cu["units"] if u["serial"] == p["serial"]), None)
            own = (u or {}).get("own") or []
            if not own:
                notes.append(f"{p['hostname'] or p['serial']}: no address of its own is known, so only what the primary "
                             "reports about it is shown. Add its management-ip or dedicated HA management address on the "
                             "device, or SSH credentials so the app can read it from the primary.")
                continue
            a = own[0]
            port = ((u or {}).get("mgmt") or {}).get("admin_sport") or base.get("port") or 443
            jobs_.append((p, u, a, dict(base, host=a["addr"], port=port, name=p["hostname"] or p["serial"])))
        with ThreadPoolExecutor(max_workers=4) as ex:
            got = list(ex.map(lambda j: _member_overview(j[3]), jobs_))
        for (p, u, a, m), r in zip(jobs_, got):
            r["member"] = "unit:" + p["serial"]
            if not r.get("ok"):
                notes.append(f"{p['hostname'] or p['serial']}: its own address {a['addr']} did not answer: {r.get('error')}")
                continue
            if r.get("serial") != p["serial"]:
                notes.append(f"{p['hostname'] or p['serial']}: {a['addr']} answered as {r.get('serial')}, not this unit.")
                continue
            r.update(discovered=True, _own=True,
                     reached=f"its own address {a['addr']} ({a['kind']}"
                             f"{', found over SSH from the primary' if (u or {}).get('source') == 'ssh' else ''})")
            by_serial[p["serial"]] = r
    units = []
    for p in peers:
        x = by_serial.get(p["serial"])
        if x is None:
            x = {"ok": False, "member": "unit:" + p["serial"], "serial": p["serial"], "hostname": p.get("hostname") or "",
                 "name": p.get("hostname") or p["serial"], "host": "", "error": "no address reaches this unit",
                 "model": src.get("model"), "reached": None}
        x = {k: v for k, v in x.items() if k != "_own"}
        x.update(unit_role="primary" if p.get("primary") else "secondary", unit_priority=p.get("priority"),
                 unit_sessions=p.get("sessions"), unit_cpu=p.get("cpu"), unit_mem=p.get("mem"))
        units.append(x)
    return units, notes


@app.get("/api/targets/{tid}/known-issues")
def known_issues(tid: str, version: str = "", refresh: bool = False):
    """Known issues of the release the device runs, rated against the device configuration."""
    t = _target(tid)
    if not version:
        info = _info(t)
        if not info.get("ok"):
            snaps = storage.list_snapshots(tid)
            if not snaps:
                raise HTTPException(503, info.get("error") or "device unreachable")
            info = snaps[0]["device"]
        version, model = str(info["version"]), info.get("model", "")
    else:
        model = (_info(t) or {}).get("model", "")
    try:
        notes = notes_cache.get(version, refresh=refresh)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not download the FortiOS {version} release notes: {e}") from e
    cfg = ""
    for s in storage.list_snapshots(tid):
        snap = storage.load_snapshot(tid, s["id"])
        if snap.get("config") and str(snap["meta"]["device"]["version"]).lstrip("v") == str(version).lstrip("v"):
            cfg = snap["config"]
            break
    else:
        for s in storage.list_snapshots(tid)[:1]:
            cfg = storage.load_snapshot(tid, s["id"]).get("config") or ""
    issues = release_notes.rate_known_issues(notes["known_issues"], cfg, model)
    return {"version": notes["version"], "url": notes["url"], "fetched_at": notes["fetched_at"],
            "config_checked": bool(cfg), "issues": issues,
            "relevant": sum(1 for i in issues if i["relevant"])}


# ------------------------------------------------------- planning/upgrade
@app.post("/api/targets/{tid}/preflight")
def preflight(tid: str, body: dict = Body(...)):
    t = _target(tid)
    return jobs.start("preflight", f"Pre-flight {t['name']} -> {body['target_version']}",
                      orchestrator.preflight, t, body["target_version"], target_id=tid,
                      meta={"target_version": body["target_version"]}).to_dict()


@app.post("/api/preflight")
def preflight_many(body: dict = Body(...)):
    targets = [_target(i) for i in body.get("target_ids") or []]
    if not targets or not body.get("target_version"):
        raise HTTPException(400, "select devices and a target version")
    tv = body["target_version"]

    def run(job):
        job.label = "pre-flight"
        kids = [jobs.start("preflight", t["name"], orchestrator.preflight, t, tv, parent=job, target_id=t["id"])
                for t in targets]
        job.set_stage("Analysing")
        while any(k.status == "running" for k in kids):
            job.check_cancel()
            time.sleep(0.5)
        return {"results": [{"job": k.id, "target_id": k.target_id, "target": k.title, "status": k.status,
                             "error": k.error, "result": k.result} for k in kids]}

    return jobs.start("preflight", f"Pre-flight {len(targets)} device(s) → {tv}", run,
                      target_ids=[t["id"] for t in targets], meta={"target_version": tv}).to_dict()


@app.post("/api/review")
def review_many(body: dict = Body(...)):
    """Upgrade-readiness review of the configuration, HA, licences, resources and logs (read-only)."""
    from . import review
    targets = [_target(i) for i in body.get("target_ids") or []]
    if not targets or not body.get("target_version"):
        raise HTTPException(400, "select devices and a target version")
    tv, src = body["target_version"], body.get("source") or "fortiguard"

    def run(job):
        job.label = "review"
        kids = [jobs.start("review", t["name"], review.review_target, t, tv, src, body.get("ha_modes"), parent=job,
                           target_id=t["id"]) for t in targets]
        job.set_stage("Reviewing")
        while any(k.status == "running" for k in kids):
            job.check_cancel()
            time.sleep(0.5)
        return {"results": [{"job": k.id, "target_id": k.target_id, "target": k.title, "status": k.status,
                             "error": k.error, "result": k.result} for k in kids]}

    return jobs.start("review", f"Configuration and log review {len(targets)} device(s) → {tv}", run,
                      target_ids=[t["id"] for t in targets], meta={"target_version": tv}).to_dict()


@app.post("/api/upgrade")
def upgrade(body: dict = Body(...)):
    """Upgrade one or many targets simultaneously."""
    ids = body.get("target_ids") or []
    if not ids:
        raise HTTPException(400, "select at least one device")
    try:
        body["probes"] = probes.normalise_all(body.get("probes") or [])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not body.get("target_version"):
        raise HTTPException(400, "target_version required")
    targets = [_target(i) for i in ids]
    sim = "[Simulation] " if body.get("dry_run") else ""
    title = f"{sim}Upgrade {len(targets)} device(s) to {body['target_version']}" if len(targets) > 1 \
        else f"{sim}Upgrade {targets[0]['name']} to {body['target_version']}"
    return jobs.start("batch", title, orchestrator.run_batch, targets, body, target_ids=ids,
                      meta={"target_version": body["target_version"], "dry_run": bool(body.get("dry_run")),
                            "source": body.get("source", "fortiguard"), "direct": bool(body.get("direct")),
                            "ha_modes": body.get("ha_modes") or {}, "run_checks": body.get("run_checks", True),
                            "preflight_job": body.get("preflight_job"), "review_job": body.get("review_job"),
                            "probes": len(body.get("probes") or [])}).to_dict()


_UNITS: dict[str, tuple[float, dict]] = {}


def cluster_units(t: dict, refresh: bool = False) -> dict:
    """Every physical unit behind a device and the addresses that reach that one unit.

    Members are read over the REST API. HA units the device has no unit address for are read
    from the primary over SSH (`execute ha manage` + `show system interface`, read-only), so a
    secondary's own management-ip is found without the user typing it in. Cached for 10 minutes."""
    hit = _UNITS.get(t["id"])
    if hit and not refresh and time.time() - hit[0] < 600:
        return hit[1]
    units, notes = {}, []

    def one(m):
        try:
            with orchestrator.client_for(m, timeout=10) as c:
                info = c.discover()
                ha = hainfo.ha_info(c) if info.ha_mode in ("a-p", "a-a") else {"peers": [], "ha_mgmt_interfaces": []}
                return {"target": t["name"], "hostname": info.hostname, "serial": info.serial, "ha_mode": info.ha_mode,
                        "host": m["host"], "port": m.get("port", 443), "mgmt": hainfo.mgmt_info(c), "ha": ha,
                        "member": m, "source": "api"}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e), "member": m}

    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(one, t["members"]))
    for r in res:
        if r.get("error"):
            notes.append(f"{t['name']} ({r['member']['host']}): could not read its addresses: {r['error']}")
            continue
        r["dedicated"] = [x["interface"] for x in r["ha"].get("ha_mgmt_interfaces") or [] if x.get("interface")]
        cur = units.get(r["serial"])
        # several addresses can reach the same unit (cluster address and that unit's own address): keep one
        if not cur or (cur["host"] not in [a["addr"] for a in hainfo.unit_addresses(cur["mgmt"], True, cur["dedicated"])]):
            units[r["serial"]] = r
    ok = [r for r in res if not r.get("error")]
    peers = next((r["ha"]["peers"] for r in ok if r["ha"].get("peers")), [])
    missing = [p for p in peers if p["serial"] not in units]
    if missing:
        via = next((r for r in ok if r["member"].get("ssh_user")), None)
        if via:
            m = via["member"]
            try:
                from .ssh import FortiSSH
                with FortiSSH(m.get("ssh_host") or m["host"], m["ssh_user"], storage.get_secret(f"{m['id']}:ssh"),
                              port=int(m.get("ssh_port") or 22), timeout=15) as sh:
                    got = sh.ha_peer_show(m["ssh_user"], storage.get_secret(f"{m['id']}:ssh"), ["show system interface"])
                for p in missing:
                    if p["serial"] in got:
                        units[p["serial"]] = {"target": t["name"], "hostname": p["hostname"], "serial": p["serial"],
                                              "ha_mode": via["ha_mode"], "host": None, "port": None,
                                              "mgmt": {"admin_sport": via["mgmt"].get("admin_sport", 443),
                                                       "interfaces": hainfo.parse_interfaces(got[p["serial"]]["show system interface"])},
                                              "dedicated": via["dedicated"], "source": "ssh"}
            except Exception as e:  # noqa: BLE001
                notes.append(f"{t['name']}: could not read the other cluster unit over SSH ({e})")
        for p in missing:
            if p["serial"] not in units:
                notes.append(f"{t['name']}: {p['hostname'] or p['serial']} is in the cluster but the app can't reach it: "
                             "add its own management address (its management-ip) on the device"
                             + ("" if via else ", or add SSH credentials so the app can read it from the primary") + ".")
    out = {"units": list(units.values()), "notes": notes, "peers": peers}
    for u in out["units"]:
        u.pop("member", None)
        u["own"] = [{"addr": a["addr"], "kind": a["kind"], "access": a["access"]}
                    for a in hainfo.unit_addresses(u["mgmt"], u["ha_mode"] in ("a-p", "a-a"), u.get("dedicated"))]
    _UNITS[t["id"]] = (time.time(), out)
    return out


@app.post("/api/onboard/detect")
def onboard_detect(body: dict = Body(...)):
    """Add device: connect to one address with the token typed in (nothing is saved), and work out
    what it is and every address the device should be saved with. For an FGCP cluster: the shared
    cluster address and each unit's own address (this unit's from its interfaces; the others' read
    over SSH from this unit with `execute ha manage`, when SSH credentials are given). Read-only."""
    host, token = (body.get("host") or "").strip(), (body.get("token") or "").strip()
    if not host or not token:
        raise HTTPException(400, "enter the address and the REST API token")
    port, scheme = int(body.get("port") or 443), body.get("scheme") or "https"
    try:
        with FortiGateClient(host, token, port=port, verify_ssl=bool(body.get("verify_ssl")), scheme=scheme, timeout=15) as c:
            info = c.discover()
            mgmt = hainfo.mgmt_info(c)
            ha = hainfo.ha_info(c) if info.ha_mode in ("a-p", "a-a") else {"peers": [], "ha_mgmt_interfaces": []}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not connect to {host}:{port}: {e}") from e
    ded = [x["interface"] for x in ha.get("ha_mgmt_interfaces") or [] if x.get("interface")]
    out = {"host": host, "port": port, "hostname": info.hostname, "serial": info.serial, "model": info.model,
           "version": str(info.version), "build": info.version.build, "ha_mode": info.ha_mode, "fgsp": bool(info.fgsp),
           "admin_sport": mgmt.get("admin_sport", 443), "dedicated": ded,
           "address": hainfo.classify_address(host, mgmt, info.ha_mode, ded), "type": "standalone", "units": [], "notes": []}
    if info.ha_mode not in ("a-p", "a-a"):
        out["type"] = "fgsp" if info.fgsp else "standalone"
        return out
    out["type"] = "fgcp"
    out["ha"] = {k: ha.get(k) for k in ("group_name", "group_id", "override", "hbdev", "monitor", "session_pickup")}
    others: dict[str, list[dict]] = {}
    if len(ha.get("peers") or []) > 1 and body.get("ssh_user"):
        try:
            from .ssh import FortiSSH
            with FortiSSH(body.get("ssh_host") or host, body["ssh_user"], body.get("ssh_password"),
                          port=int(body.get("ssh_port") or 22), timeout=15) as sh:
                got = sh.ha_peer_show(body["ssh_user"], body.get("ssh_password"), ["show system interface"])
            for sn, cmds in got.items():
                m2 = {"admin_sport": mgmt.get("admin_sport", 443),
                      "interfaces": hainfo.parse_interfaces(cmds.get("show system interface", ""))}
                others[sn] = hainfo.unit_addresses(m2, True, ded)
        except Exception as e:  # noqa: BLE001
            out["notes"].append(f"Could not read the other unit over SSH: {e}")
    for p in ha.get("peers") or [{"serial": info.serial, "hostname": info.hostname, "priority": ha.get("priority"), "primary": True}]:
        if p["serial"] == info.serial:
            own, src = hainfo.unit_addresses(mgmt, True, ded), "this unit"
        elif p["serial"] in others:
            own, src = others[p["serial"]], "read over SSH"
        else:
            own, src = [], None
        out["units"].append({"serial": p["serial"], "hostname": p.get("hostname") or "", "priority": p.get("priority"),
                             "primary": bool(p.get("primary")), "this": p["serial"] == info.serial, "source": src,
                             "own": [{"addr": a["addr"], "kind": a["kind"], "https": "https" in (a.get("access") or [])} for a in own]})
    if len(out["units"]) < 2:
        out["notes"].append("The cluster reports only one unit. Check the heartbeat links before upgrading.")
    if any(not u["own"] for u in out["units"]) and not body.get("ssh_user") and len(out["units"]) > 1:
        out["notes"].append("Add SSH credentials and detect again to have the other unit's own address filled in, or type it.")
    return out


@app.get("/api/targets/{tid}/units")
def target_units(tid: str, refresh: bool = False):
    """Each physical unit of a device with the addresses that reach that unit (dashboard, HA panel)."""
    r = cluster_units(_target(tid), refresh)
    return {"units": [{k: u.get(k) for k in ("hostname", "serial", "own", "source", "host")} for u in r["units"]],
            "notes": r["notes"]}


@app.get("/api/probes/suggest")
def probes_suggest(target_ids: str = ""):
    """Default probes for the selected devices: each unit's own management address (for HA, each unit's
    management-ip or dedicated HA management interface, never the shared cluster address), with ping
    where ping is allowed and HTTPS on the admin port where HTTPS is."""
    targets = [_target(i) for i in target_ids.split(",") if i]
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(lambda t: cluster_units(t, refresh=True), targets))
    units = [u for r in res for u in r["units"]]
    out = hainfo.suggest_probes(units)
    out["notes"] = [n for r in res for n in r["notes"]] + out["notes"]
    return out


@app.post("/api/targets/{tid}/ha-addresses")
def set_ha_addresses(tid: str, body: dict = Body(...)):
    """Re-save an HA device with the cluster address first and each unit's own address after it.
    The API token and SSH credentials of the existing address are reused: HA synchronises admin
    accounts and API users, so they are valid on every unit."""
    t = _target(tid)
    src = t["members"][0]
    tok = storage.get_secret(f"{src['id']}:token")
    sshpw = storage.get_secret(f"{src['id']}:ssh")
    want = [{"host": body["cluster_host"].strip(), "name": body.get("cluster_name") or f"{t['name']} (cluster)"}]
    want += [{"host": u["host"].strip(), "name": u.get("name") or u["host"]} for u in body.get("units") or [] if u.get("host")]
    by_host = {m["host"]: m for m in t["members"]}
    members, secrets = [], {}
    for w in want:
        m = dict(by_host.get(w["host"]) or {k: src.get(k) for k in ("port", "scheme", "verify_ssl", "ssh_user", "ssh_port")})
        m.update(host=w["host"], name=m.get("name") if w["host"] in by_host else w["name"])
        if w["host"] not in by_host:
            m.pop("id", None)
            secrets[str(len(members))] = {"token": tok, "ssh_password": sshpw}
        members.append(m)
    hosts = [m["host"] for m in members]
    if len(set(hosts)) != len(hosts):
        raise HTTPException(400, "the same address is listed twice")
    for i, m in enumerate(members):
        if "id" not in m:
            m["id"] = __import__("uuid").uuid4().hex[:10]
            secrets[m["id"]] = secrets.pop(str(i))
    saved = storage.save_target({**t, "type": "fgcp", "members": members}, secrets)
    for old in t["members"]:
        if old["id"] not in {m["id"] for m in members}:
            storage.set_secret(f"{old['id']}:token", None)
            storage.set_secret(f"{old['id']}:ssh", None)
    _INV.pop(tid, None)
    _UNITS.pop(tid, None)
    return saved


@app.post("/api/probes/test")
def probes_test(body: dict = Body(...)):
    """Run each probe a few times now, so the user knows the targets answer before the upgrade."""
    try:
        return {"results": probes.test(body.get("probes") or [])}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# --------------------------------------------------------------- inventory
_INV: dict[str, tuple[float, dict]] = {}


def _info(t: dict, fresh: bool = False) -> dict:
    hit = _INV.get(t["id"])
    if hit and not fresh and time.time() - hit[0] < 120:
        return hit[1]
    try:
        with orchestrator.client_for(t["members"][0], timeout=10) as c:
            d = {"ok": True, **c.discover().to_dict()}
    except Exception as e:  # noqa: BLE001
        d = {"ok": False, "error": str(e)}
    _INV[t["id"]] = (time.time(), d)
    return d


@app.get("/api/inventory")
def inventory(fresh: bool = False):
    ts = storage.list_targets()
    with ThreadPoolExecutor(max_workers=16) as ex:
        infos = list(ex.map(lambda t: _info(t, fresh), ts))
    return {t["id"]: i for t, i in zip(ts, infos)}


@app.post("/api/targets/import")
def import_targets(body: dict = Body(...)):
    """CSV lines: name,host,port,api_token[,type][,ssh_user,ssh_password]. Header optional."""
    created, errors = [], []
    for n, row in enumerate(csv.reader(io.StringIO(body.get("csv", "")))):
        row = [c.strip() for c in row]
        if not row or not any(row) or row[0].lower() == "name" or row[0].startswith("#"):
            continue
        if len(row) < 4:
            errors.append(f"line {n + 1}: need name,host,port,token")
            continue
        name, host, port, token = row[:4]
        ttype = (row[4] if len(row) > 4 and row[4] else "standalone").lower()
        if ttype not in ("standalone", "fgcp"):
            errors.append(f"line {n + 1}: type must be standalone or fgcp (add FGSP groups in the editor)")
            continue
        m = {"name": name or host, "host": host, "port": int(port or 443), "scheme": "https"}
        if len(row) > 5 and row[5]:
            m["ssh_user"] = row[5]
        sec = {"0": {"token": token, "ssh_password": row[6] if len(row) > 6 else None}}
        created.append(storage.save_target({"name": name or host, "type": ttype, "members": [m]}, sec)["id"])
    return {"created": len(created), "errors": errors}


# --------------------------------------------------------------- firmware store
@app.get("/api/images")
def list_images():
    return images.list_images()


@app.get("/api/images/match")
def match_images(version: str, target_ids: str):
    """For each device: the store image that fits `version`, the expected file name, and why
    other images of that version do not fit (e.g. a FortiFirewall image for a FortiGate)."""
    out = {}
    for tid in [t for t in target_ids.split(",") if t]:
        t = _target(tid)
        info = _info(t)
        if not info.get("ok"):
            snaps = storage.list_snapshots(tid)
            info = snaps[0]["device"] if snaps else {}
        out[tid] = {"model": info.get("model", ""), "model_code": info.get("model_code", ""),
                    **images.explain(version, info.get("model", ""), info.get("model_code", ""))}
    return out


@app.post("/api/images")
async def upload_image(file: UploadFile = File(...)):
    tmp = Path(tempfile.mkdtemp()) / Path(file.filename).name
    with open(tmp, "wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
    try:
        return images.add(tmp, file.filename, move=True)
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


@app.post("/api/images/import")
def import_images(paths: list[str] = Body(..., embed=True)):
    out = []
    for p in paths:
        p = Path(p).expanduser()
        if not p.is_file():
            raise HTTPException(400, f"file not found: {p}")
        out.append(images.add(p))
    return out


@app.patch("/api/images/{iid}")
def update_image(iid: str, body: dict = Body(...)):
    try:
        return images.update(iid, body)
    except KeyError:
        raise HTTPException(404)


@app.delete("/api/images/{iid}")
def delete_image(iid: str):
    images.delete(iid)
    return {"ok": True}


@app.post("/api/open-url")
def open_url(url: str = Body(..., embed=True)):
    """Open the project's GitHub pages in the default browser (the only URLs allowed)."""
    from . import updater
    if not url.startswith(f"https://github.com/{updater.REPO}"):
        raise HTTPException(400, "only the FortiGate Upgrader GitHub pages can be opened")
    import webbrowser
    webbrowser.open(url)
    return {"ok": True}


@app.post("/api/open-folder")
def open_folder(kind: str = Body(..., embed=True), tid: str | None = Body(None, embed=True)):
    p = storage.snapshots_dir(tid) if kind == "snapshots" and tid else \
        storage.data_dir() / "firmware" if kind == "firmware" else storage.data_dir()
    p.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    elif sys.platform.startswith("win"):
        os.startfile(str(p))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(p)])
    return {"path": str(p)}


# ---------------------------------------------------------- release notes
@app.get("/api/notes")
def notes():
    return [{"name": f.stem, "chars": f.stat().st_size} for f in sorted(storage.notes_dir().glob("*.txt"))]


@app.post("/api/notes")
async def add_notes(name: str = Form(""), url: str = Form(""), file: UploadFile | None = File(None)):
    if file is not None and file.filename:
        text = release_notes.extract_text(await file.read(), file.filename)
        name = name or Path(file.filename).stem
    elif url:
        try:
            text = release_notes.fetch_url(url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"fetch failed: {e}")
        name = name or url.rstrip("/").split("/")[-1]
    else:
        raise HTTPException(400, "file or url required")
    name = re.sub(r"[^\w.\-]", "_", name)[:80]
    (storage.notes_dir() / f"{name}.txt").write_text(text)
    return {"name": name, "chars": len(text)}


@app.delete("/api/notes/{name}")
def del_notes(name: str):
    f = storage.notes_dir() / f"{re.sub(r'[^\w.-]', '_', name)}.txt"
    f.unlink(missing_ok=True)
    return {"ok": True}


# ------------------------------------------------------------------- jobs
def _job_dict(jid: str, since: int = 0) -> dict | None:
    j = jobs.JOBS.get(jid)
    if j:
        return j.to_dict(since)
    d = storage.load_job(jid)
    if d is None:
        return None
    if d.get("status") in ("running", "waiting"):
        d["status"], d["stage"] = "interrupted", "Interrupted (app was closed)"
    d["log"] = (d.get("log") or [])[since:]
    d["history"] = True
    return d


@app.get("/api/jobs")
def list_jobs(target_id: str = "", kind: str = "", children: bool = False, active: bool = False):
    """Jobs in this session plus the saved history. With target_id, every job that touched
    that device (including per-device children of batch upgrades)."""
    live = {j.id: {k: v for k, v in j.to_dict().items() if k not in ("log", "result")} for j in jobs.JOBS.values()}
    out = dict(live)
    for d in storage.list_jobs():
        if d["id"] not in out:
            if d.get("status") in ("running", "waiting"):
                d["status"], d["stage"] = "interrupted", "Interrupted (app was closed)"
            d.pop("result", None)
            out[d["id"]] = d
    rows = sorted(out.values(), key=lambda d: d.get("created") or "", reverse=True)
    if target_id:
        rows = [d for d in rows if target_id in (d.get("target_ids") or [d.get("target_id")])]
        if not children:
            # prefer the per-device child of a batch over the batch itself
            rows = [d for d in rows if not (d.get("kind") == "batch" and len(d.get("target_ids") or []) > 1)]
    elif not children:
        rows = [d for d in rows if not d.get("parent")]
    if kind:
        rows = [d for d in rows if d.get("kind") in kind.split(",")]
    if active:
        rows = [d for d in rows if d.get("status") in ("running", "waiting")]
    for d in rows:
        if d["id"] in jobs.JOBS:
            j = jobs.JOBS[d["id"]]
            d["last"] = j.log[-1]["msg"] if j.log else ""
    return rows


@app.get("/api/jobs/{jid}")
def get_job(jid: str, since: int = 0):
    d = _job_dict(jid, since)
    if d is None:
        raise HTTPException(404)
    kids = []
    for c in d.get("children") or []:
        k = _job_dict(c)
        if k:
            log = k.pop("log", [])
            k["last"] = log[-1]["msg"] if log else ""
            kids.append(k)
    d["children_detail"] = kids
    if not d.get("probes") and d.get("parent"):  # a device's upgrade shows its batch's service probes
        par = _job_dict(d["parent"])
        if par and par.get("probes"):
            d["parent_probes"] = par["probes"]
    return d


@app.post("/api/jobs/{jid}/report")
def job_report(jid: str, body: dict = Body(default={})):
    """Generate (or regenerate) the PDF report of an upgrade run."""
    from . import report
    try:
        path = report.build(jid, include_api_trace=body.get("api_trace", True))
    except KeyError as e:
        raise HTTPException(404, "job not found") from e
    except Exception:  # noqa: BLE001
        LOG.exception("report from the live job %s failed; retrying from the saved copy", jid)
        try:
            path = report.build(jid, include_api_trace=body.get("api_trace", True), from_disk=True)
        except Exception as e2:  # noqa: BLE001
            LOG.exception("report from the saved copy of %s failed", jid)
            raise HTTPException(500, f"Could not build the PDF report: {type(e2).__name__}: {e2} "
                                     "(details in logs/fortigate-upgrader.log)") from e2
    return {"path": str(path), "name": path.name, "size": path.stat().st_size}


@app.get("/api/jobs/{jid}/report.pdf")
def job_report_pdf(jid: str):
    r = job_report(jid, {})
    return FileResponse(r["path"], media_type="application/pdf", filename=r["name"])


@app.post("/api/open-file")
def open_file(path: str = Body(..., embed=True), reveal: bool = Body(False, embed=True)):
    """Open a file the app created (inside its data folder) with the system's default app."""
    p = Path(path).resolve()
    if storage.data_dir().resolve() not in p.parents or not p.is_file():
        raise HTTPException(400, "not an app file")
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)] if reveal else ["open", str(p)])
    elif sys.platform.startswith("win"):
        if reveal:
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(str(p))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(p.parent if reveal else p)])
    return {"ok": True}


@app.post("/api/jobs/{jid}/answer")
def answer(jid: str, approve: bool = Body(..., embed=True)):
    jobs.JOBS[jid].answer(approve)
    return {"ok": True}


@app.post("/api/jobs/{jid}/cancel")
def cancel(jid: str):
    jobs.JOBS[jid].cancel()
    return {"ok": True}


# ------------------------------------------------------------------ static
@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/static", StaticFiles(directory=WEB), name="static")
