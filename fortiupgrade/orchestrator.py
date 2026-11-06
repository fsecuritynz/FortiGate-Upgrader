"""Upgrade orchestration for standalone, FGCP and FGSP targets, singly or many at once.

Target model (stored in targets.json):
  type: standalone | fgcp | fgsp
  members: [{id, name, host, port, scheme, verify_ssl, ssh_user, ssh_port}]
    standalone: exactly one member.
    fgcp:       members[0] is the cluster (primary) management address. Optional further
                members are per-unit HA management interfaces, used for local-only upgrades
                of a specific unit and per-unit verification.
    fgsp:       every peer is an independent member with its own address.

HA upgrade modes
  FGCP: simultaneous | uninterruptible | local-only | secondary-only
        ('config system ha / set upgrade-mode' on 7.4.1+; before that only simultaneous and
        uninterruptible exist, via 'set uninterruptible-upgrade').
  FGSP: uninterruptible (rolling, one peer at a time with checks between) |
        simultaneous (all peers at once) | local-only (only the selected peers).

Image sources: fortiguard (the FortiGate downloads it) | store (pushed from the local
firmware store, matched on Version + Hardware tags).
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from . import probes, fmg, images, jobs, notes_cache, release_notes, storage, upgrade_path
from .checks import Ctx, c_ha, ha_in_sync
from .client import FortiAPIError, FortiGateClient
from .diff import diff_snapshots
from .jobs import Job
from .snapshot import take_snapshot
from .ssh import FortiSSH
from .version import FosVersion

FGCP_MODES = ("simultaneous", "uninterruptible", "local-only", "secondary-only")
FGSP_MODES = ("uninterruptible", "simultaneous", "local-only")
UPGRADE_MODE_SINCE = FosVersion(7, 4, 1)
_IMG_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)", re.I)


# ------------------------------------------------------------------ helpers
def client_for(m: dict, timeout: float = 30) -> FortiGateClient:
    token = storage.get_secret(f"{m['id']}:token")
    if not token:
        raise RuntimeError(f"No API token stored for {m.get('name') or m['host']}")
    c = FortiGateClient(m["host"], token, port=int(m.get("port") or 443),
                        verify_ssl=bool(m.get("verify_ssl")), scheme=m.get("scheme") or "https",
                        timeout=timeout)
    j = jobs.current()
    if j is not None:
        who = m.get("name") or m["host"]
        c.trace = lambda line: j.detail(line if who == j.title else f"{who}: {line}")
    return c


def ssh_for(m: dict):
    if not m.get("ssh_user"):
        return None
    pw = storage.get_secret(f"{m['id']}:ssh")
    j = jobs.current()
    if j is not None:
        j.detail(f"{m.get('name') or m['host']}: opening SSH session to {m['ssh_user']}@"
                 f"{m.get('ssh_host') or m['host']}:{m.get('ssh_port') or 22}")
    t0 = time.time()
    s = FortiSSH(m.get("ssh_host") or m["host"], m["ssh_user"], pw, port=int(m.get("ssh_port") or 22))
    if j is not None:
        j.detail(f"{m.get('name') or m['host']}: SSH session ready in {time.time() - t0:.1f} s")
        s.trace = lambda line: j.detail(line if (m.get("name") or m["host"]) == j.title
                                        else f"{m.get('name') or m['host']}: {line}")
    return s


def image_version(filename: str) -> str | None:
    m = _IMG_RE.search(Path(filename).name)
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else None


def _label(m):
    return m.get("name") or m["host"]


def snapshot_member(job: Job, target: dict, m: dict, label: str, check_ids=None, vdoms=None,
                    use_ssh=True) -> str:
    ssh = None
    if use_ssh and m.get("ssh_user"):
        try:
            ssh = ssh_for(m)
        except Exception as e:  # noqa: BLE001
            job.say(f"SSH to {_label(m)} failed, CLI checks skipped: {e}", "warn")
    try:
        with client_for(m, timeout=60) as c:
            snap = take_snapshot(c, label, check_ids=check_ids, vdoms=vdoms, ssh=ssh,
                                 logger=lambda s: job.say(f"{_label(m)}: {s}"))
            if snap.get("config"):
                snap["meta"]["device"]["management"] = fmg.from_config(snap["config"])
    finally:
        if ssh:
            ssh.close()
    sid = storage.save_snapshot(target["id"], m["id"], snap)
    errs = sum(len(v) for v in snap["errors"].values())
    job.say(f"{_label(m)}: saved {sid}.log{f' ({errs} check(s) skipped or failed)' if errs else ''}", "ok")
    return sid


def _available(c: FortiGateClient) -> list[str]:
    try:
        return [a.get("version") for a in (c.firmware().get("available") or []) if a.get("version")]
    except FortiAPIError:
        return []


# ---------------------------------------------------------------- preflight
def preflight(job: Job, target: dict, target_version: str, opts_fetch_notes: bool = True) -> dict:
    out = {"members": [], "findings": [], "plan": None, "analysis": None, "management": None, "images": []}

    def find(sev, msg):
        out["findings"].append({"severity": sev, "message": msg})
        job.say(msg, {"critical": "error", "warning": "warn"}.get(sev, "info"))

    job.set_stage("Connecting")
    infos = {}
    for m in target["members"]:
        try:
            with client_for(m) as c:
                info = c.discover()
                infos[m["id"]] = info
                out["members"].append({"member": _label(m), **info.to_dict()})
                job.say(f"{_label(m)}: {info.model} {info.version.full} HA={info.ha_mode} "
                        f"VDOMs={'yes' if info.vdom_mode else 'no'} ({len(info.vdoms)})")
        except Exception as e:  # noqa: BLE001
            find("critical", f"{_label(m)}: cannot connect: {e}")
    if not infos:
        return out
    primary = target["members"][0]
    pinfo = infos.get(primary["id"]) or next(iter(infos.values()))
    versions = {str(i.version) for i in infos.values()}
    if len(versions) > 1:
        find("warning", f"Members run different versions: {', '.join(sorted(versions))}")
    if target["type"] == "fgcp" and pinfo.ha_mode == "standalone":
        find("critical", "Target is defined as an FGCP cluster but the device reports HA mode standalone.")
    if target["type"] == "standalone" and pinfo.ha_mode != "standalone":
        find("warning", f"Device is in HA mode {pinfo.ha_mode}; consider defining it as an FGCP cluster.")
    if target["type"] == "fgsp" and not any(i.fgsp for i in infos.values()):
        find("warning", "No FGSP (standalone-cluster / cluster-sync) configuration detected.")

    with client_for(primary) as c:
        ctx = Ctx(client=c, vdom_mode=pinfo.vdom_mode)
        job.set_stage("Checking HA and resources")
        if pinfo.ha_mode != "standalone":
            ha = c_ha(ctx, None)
            sync = ha_in_sync(ha)
            if sync is False:
                find("critical", "HA cluster configuration is OUT OF SYNC. Resolve before upgrading.")
            elif sync:
                find("info", f"HA cluster in sync ({len(ha)} members).")
            hacfg = c.cmdb("system/ha") or {}
            out["ha"] = {k: hacfg.get(k) for k in ("mode", "group-name", "upgrade-mode",
                                                    "uninterruptible-upgrade", "uninterruptible-primary-wait",
                                                    "override", "priority") if k in hacfg}
        try:
            res = c.monitor("system/resource/usage", params={"interval": "1-min"}) or {}
            mem = res.get("mem")
            mem = mem[0].get("current") if isinstance(mem, list) and mem else mem
            if isinstance(mem, (int, float)) and mem >= 80:
                find("warning", f"Memory usage {mem}%: risk of conserve mode during the upgrade.")
        except FortiAPIError:
            pass

        job.set_stage("Planning upgrade path")
        available = _available(c)
        out["available"] = sorted({str(FosVersion.parse(a)) for a in available}, key=FosVersion.parse, reverse=True)
        p = upgrade_path.plan(c, str(pinfo.version), target_version, pinfo.model, available,
                              storage.user_paths_file())
        out["plan"] = p
        for w in p["warnings"]:
            find("warning", w)
        job.say(f"Upgrade path ({p['source']}): {' → '.join(p['steps'])}")
        for v in p["steps"][1:]:
            st_img = images.find(v, pinfo.model, pinfo.model_code)
            out["images"].append({"version": v, "fortiguard": any(FosVersion.parse(a) == FosVersion.parse(v)
                                                                  for a in available),
                                  "store": st_img["filename"] if st_img else None})

        job.set_stage("Analysing configuration")
        try:
            cfg = c.backup_config("global")
            mgmt = fmg.from_config(cfg)
            mgmt["connection"] = fmg.live_status(c)
            out["management"] = mgmt
            for f in fmg.findings(mgmt):
                find(f["severity"], f["message"])
            cur_v = pinfo.version
            if opts_fetch_notes:
                for v in p["steps"][1:]:
                    job.say(f"Release notes {v}: " + ("available" if notes_cache.ensure_text(v)
                                                       else "could not be downloaded (offline?)"))
            notes = []
            for f in sorted(storage.notes_dir().glob("*.txt")):
                try:
                    nv = FosVersion.parse(f.stem)
                    if not (cur_v < nv <= FosVersion.parse(target_version)):
                        continue  # notes for versions outside this upgrade
                except ValueError:
                    pass  # user-named notes file: always scan
                notes.append((f.stem, f.read_text()))
            an = release_notes.analyse(cfg, str(pinfo.version), target_version, pinfo.model, notes,
                                       storage.user_rules_file())
            out["analysis"] = an
            for h in an["rule_hits"]:
                find(h["severity"], f"{h['title']} (from {h['introduced_in'] or 'any version'})")
            if notes:
                job.say(f"Release notes scanned: {len(an['notes_hits'])} relevant paragraph(s).")
            else:
                find("info", "No release notes loaded; only the built-in rules were evaluated.")
        except FortiAPIError as e:
            find("warning", f"Config backup failed, configuration analysis skipped: {e}")
    return out


# ------------------------------------------------------------------ pushing
def _push(job: Job, c: FortiGateClient, version: str, opts: dict, dry: bool, who: str):
    src = opts.get("source", "fortiguard")
    job.set_stage(f"Transferring {version} image")
    if src == "fortiguard":
        fw = c.firmware()
        img = next((a for a in fw.get("available", []) or []
                    if a.get("version") and FosVersion.parse(a["version"]) == FosVersion.parse(version)), None)
        if not img:
            raise RuntimeError(f"{who}: {version} is not offered by FortiGuard for this model")
        job.say(f"{who}: install method: FortiGuard download. Image FortiOS {img.get('version')} build "
                f"{img.get('build')} ({'Mature' if img.get('maturity') == 'M' else 'Feature' if img.get('maturity') == 'F' else 'n/a'}"
                f" release, image id {img.get('id')})")
        job.say(f"{who}: {'SIMULATION - would ask' if dry else 'asking'} the FortiGate to download the image from "
                "FortiGuard and install it. The request returns once the download has finished.")
        if not dry:
            t0 = time.time()
            r = c.upgrade_from_fortiguard(img["id"])
            job.detail(f"{who}: response {str(r.get('results') if isinstance(r, dict) else r)[:300]}")
            _check_upgrade_response(r, who)
            job.say(f"{who}: FortiGuard download accepted after {time.time() - t0:.0f} s")
    else:
        if src == "store":
            st = c.status()
            item = images.find(version, st.get("model", ""), st.get("model_code", ""))
            if not item:
                ex = images.explain(version, st.get("model", ""), st.get("model_code", ""))
                raise RuntimeError(f"{who}: no {ex['expected_hw'] or st.get('model_code')} image for {version} in the "
                                   f"Firmware Store" + (f" ({ex['near'][0]['reason']})" if ex["near"] else ""))
            path = Path(item["path"])
        else:  # legacy explicit path map
            path = Path((opts.get("images") or {}).get(str(FosVersion.parse(version)), ""))
        if not path.is_file():
            raise RuntimeError(f"{who}: image file not found: {path}")
        iv = image_version(path.name)
        if iv and FosVersion.parse(iv) != FosVersion.parse(version):
            raise RuntimeError(f"{path.name} looks like {iv}, expected {version}")
        sem = opts.get("_upload_sem")
        size = path.stat().st_size
        fname = item["filename"] if src == "store" else path.name
        job.say(f"{who}: install method: push local image {fname} ({size / 1e6:.1f} MB) from the "
                f"{'Firmware Store' if src == 'store' else path.parent}")
        if src == "store":
            job.detail(f"{who}: store tags: version {item.get('version')} build {item.get('build')} hardware "
                       f"{', '.join(item.get('hardware') or [])} sha256 {item.get('sha256', '')[:16]}…")
        if not dry:
            if sem:
                if not sem.acquire(blocking=False):
                    job.say(f"{who}: waiting for a free upload slot (parallel upload limit)")
                    sem.acquire()
            try:
                r = _upload(job, c, path, fname, size, who, opts)
                job.detail(f"{who}: response {str(r.get('results') if isinstance(r, dict) else r)[:300]}")
                _check_upgrade_response(r, who)
            finally:
                if sem:
                    sem.release()
        else:
            job.say(f"{who}: SIMULATION - would upload {fname} to {c.base}")
    if not dry:
        job.say(f"{who}: image accepted - the FortiGate is installing it and will reboot", "ok")


TOO_LARGE = ("The FortiGate refused the upload as too large (HTTP 413). Its web server limits how much "
             "data one REST API request may carry. Use 'Download from FortiGuard', or upload the image once "
             "through the FortiGate GUI (System > Firmware & Registration) and upgrade from there.")


def _upload(job, c: FortiGateClient, path: Path, fname: str, size: int, who: str, opts: dict):
    """Multipart file upload (as the GUI does); falls back to base64 JSON if the build rejects it."""
    mode = opts.get("upload_mode", "auto")
    if mode in ("auto", "multipart"):
        t0 = time.time()
        job.say(f"{who}: uploading {size / 1e6:.1f} MB to {c.base} as a file (multipart, streamed from disk) …")
        try:
            r = c.upgrade_from_file_multipart(path, fname)
            secs = max(time.time() - t0, 0.001)
            job.say(f"{who}: upload complete in {secs:.0f} s ({size / 1e6 / secs:.1f} MB/s)")
            return r
        except FortiAPIError as e:
            if e.status == 413:
                raise RuntimeError(f"{who}: {TOO_LARGE}") from e
            if mode == "multipart" or e.status not in (None, 400, 405, 415, 422, 424, 500):
                raise
            job.say(f"{who}: this FortiOS build did not accept a multipart upload (HTTP {e.status}); "
                    "retrying with the base64 JSON form", "warn")
            c.reconnect()
    t0 = time.time()
    data = path.read_bytes()
    job.detail(f"{who}: read {len(data) / 1e6:.1f} MB from disk; base64 encoding adds a third "
               f"(~{len(data) * 4 / 3 / 1e6:.1f} MB on the wire)")
    job.say(f"{who}: uploading to {c.base} as base64 JSON …")
    try:
        r = c.upgrade_from_file(data, fname)
    except FortiAPIError as e:
        if e.status == 413:
            raise RuntimeError(f"{who}: {TOO_LARGE}") from e
        raise
    secs = max(time.time() - t0, 0.001)
    job.say(f"{who}: upload complete in {secs:.0f} s ({len(data) / 1e6 / secs:.1f} MB/s)")
    return r


def _check_upgrade_response(r, who):
    res = r.get("results") if isinstance(r, dict) else None
    if isinstance(res, dict) and res.get("status") not in (None, "success"):
        raise RuntimeError(f"{who}: upgrade rejected by the FortiGate: {res}")


def _wait(job: Job, m: dict, version: str | None, opts: dict, dry: bool):
    who = _label(m)
    job.set_stage(f"Installing {version or 'firmware'} and rebooting")
    if dry:
        job.say(f"{who}: SIMULATION - would wait for the reboot and confirm {version}")
        return
    job.say(f"{who}: waiting for the device to return on {version} (timeout {opts.get('wait_timeout', 1800)}s)")
    with client_for(m, timeout=15) as c:
        st = c.wait_until_up(timeout=float(opts.get("wait_timeout", 1800)),
                             interval=float(opts.get("poll_interval", 15)),
                             initial_delay=float(opts.get("initial_delay", 90)),
                             expect_version=version,
                             on_tick=lambda s: (job.check_cancel(), job.say(f"{who}: {s}")))
    job.say(f"{who}: back online on {st['version']} build {st.get('build')}", "ok")


def _wait_ha_sync(job: Job, m: dict, opts: dict, dry: bool, timeout: float = 900):
    if dry:
        return
    job.set_stage("Verifying HA sync")
    end = time.time() + timeout
    while time.time() < end:
        job.check_cancel()
        try:
            with client_for(m) as c:
                ha = c_ha(Ctx(client=c, vdom_mode=False), None)
            if ha_in_sync(ha):
                job.say(f"HA cluster in sync ({len(ha)} members)", "ok")
                return
            job.say("HA not yet in sync, waiting...", "debug")
        except Exception as e:  # noqa: BLE001
            job.say(f"HA sync check: {e}", "debug")
        time.sleep(float(opts.get("poll_interval", 15)))
    job.say("HA cluster did not report in sync within the timeout. Check 'diagnose sys ha checksum cluster'.",
            "warn")


# ------------------------------------------------------------------ FGCP
def _fgcp_set_mode(job, c: FortiGateClient, running: FosVersion, mode: str, dry: bool):
    ha = c.cmdb("system/ha") or {}
    if running >= UPGRADE_MODE_SINCE:
        key, want = "upgrade-mode", mode
    else:
        if mode not in ("simultaneous", "uninterruptible"):
            raise RuntimeError(f"HA upgrade mode '{mode}' needs FortiOS 7.4.1 or later (running {running}).")
        key, want = "uninterruptible-upgrade", "enable" if mode == "uninterruptible" else "disable"
    prev = ha.get(key)
    if prev == want:
        job.say(f"HA {key} is already '{want}'")
        return None
    job.set_stage("Setting HA upgrade mode")
    job.say(f"{'SIMULATION - would set' if dry else 'Setting'} HA {key}: {prev} → {want}")
    if not dry:
        c.set_ha({key: want})
    return (key, prev)


def _fgcp_hop(job, target, version, opts, dry, running: FosVersion):
    mode = opts.get("ha_mode", "uninterruptible")
    primary = target["members"][0]
    with client_for(primary) as c:
        changed = _fgcp_set_mode(job, c, running, mode, dry)
        if changed:
            job.state.setdefault("ha_restore", changed)
    sel = ([m for m in target["members"] if m["id"] in (opts.get("member_ids") or [primary["id"]])]
           if mode == "local-only" else [primary])
    for m in sel:
        job.check_cancel()
        with client_for(m, timeout=900) as c:
            _push(job, c, version, opts, dry, _label(m))
    if mode in ("simultaneous", "uninterruptible"):
        if mode == "uninterruptible":
            job.say("Uninterruptible: secondaries upgrade first, then the primary fails over and upgrades. "
                    "The cluster address drops briefly during failover.")
        _wait(job, primary, version, opts, dry)
        _wait_ha_sync(job, primary, opts, dry)
    elif mode == "secondary-only":
        units = target["members"][1:]
        if units:
            for m in units:
                _wait(job, m, version, opts, dry)
        else:
            job.say("secondary-only: no per-unit HA management addresses defined, so the secondaries' "
                    "version cannot be confirmed. Waiting for the cluster to settle.", "warn")
            if not dry:
                time.sleep(float(opts.get("initial_delay", 90)))
                _wait(job, primary, None, opts, dry)
    else:  # local-only
        for m in sel:
            _wait(job, m, version, opts, dry)


def _restore_ha(job, target, dry):
    r = job.state.get("ha_restore")
    if not r or not job.state.get("restore_ha") or r[1] is None:
        return
    key, prev = r
    job.set_stage("Restoring HA setting")
    job.say(f"{'SIMULATION - would restore' if dry else 'Restoring'} HA {key} → {prev}")
    if not dry:
        try:
            with client_for(target["members"][0]) as c:
                c.set_ha({key: prev})
        except FortiAPIError as e:
            job.say(f"Could not restore HA {key}: {e}", "warn")


def effective_type(saved: str, info) -> str:
    """Upgrade a unit according to what it reports: a cluster saved as 'standalone' must still get
    the chosen HA upgrade mode, or FortiOS applies whatever mode happens to be configured."""
    ha = getattr(info, "ha_mode", None) or (info.get("ha_mode") if isinstance(info, dict) else None)
    fgsp = getattr(info, "fgsp", None) if not isinstance(info, dict) else info.get("fgsp")
    if saved == "standalone" and ha in ("a-p", "a-a"):
        return "fgcp"
    if saved == "standalone" and fgsp:
        return "fgsp"
    return saved


# -------------------------------------------------------- single target
def run_upgrade(job: Job, target: dict, opts: dict) -> dict:
    job.state = {"restore_ha": bool(opts.get("restore_ha_setting", True))}
    dry = bool(opts.get("dry_run", False))
    ttype = target["type"]
    members = target["members"]
    primary = members[0]
    tv = str(FosVersion.parse(opts["target_version"]))
    check_ids = opts.get("check_ids")
    vdoms = opts.get("vdoms") or None
    job.say(f"{'SIMULATION - no changes will be made. ' if dry else ''}Upgrading {target['name']} "
            f"({ttype}) to {tv}", "warn" if dry else "info")

    # 1. discovery, management, path
    job.set_stage("Connecting")
    with client_for(primary) as c:
        info = c.discover()
        job.say(f"{info.hostname}: {info.model} running {info.version.full}")
        mgmt = info.management or {}
        if mgmt.get("managed"):
            for f in fmg.findings(mgmt):
                job.say(f["message"], "warn")
            if not opts.get("fmg_acknowledged"):
                raise RuntimeError(f"{info.hostname} is managed by {mgmt['summary']}. Tick 'I understand this "
                                   "device is managed by FortiManager' to upgrade it outside FortiManager.")
        job.set_stage("Planning upgrade path")
        avail = _available(c) if opts.get("source") == "fortiguard" else \
            [i["version"] for i in images.list_images() if i.get("version")]
        if opts.get("hops"):
            p = {"steps": [str(info.version)] + opts["hops"], "verified": True, "source": "user"}
        elif opts.get("direct"):
            p = {"steps": [str(info.version), tv], "verified": True, "source": "direct (path check skipped)"}
        else:
            p = upgrade_path.plan(c, str(info.version), tv, info.model, avail, storage.user_paths_file())
    if FosVersion.parse(tv) <= info.version and not opts.get("allow_downgrade"):
        raise RuntimeError(f"Target {tv} is not newer than the running version {info.version}")
    if not p.get("verified") and not opts.get("allow_unverified_path"):
        raise RuntimeError("The upgrade path could not be verified (" + p.get("source", "") + "). Confirm it with "
                           "Fortinet's upgrade tool and add it to the override file, or tick 'allow unverified path'.")
    hops = p["steps"][1:]
    job.state["path"] = p["steps"]
    job.say(f"Upgrade path ({p.get('source')}): {' → '.join(p['steps'])}")

    detected = effective_type(ttype, info)
    if detected != ttype:
        job.say(f"{target['name']} is saved as {ttype}, but {info.hostname} reports "
                f"{'FGCP HA mode ' + info.ha_mode if detected == 'fgcp' else 'FGSP session sync'}: "
                f"upgrading it as {'an FGCP cluster' if detected == 'fgcp' else 'an FGSP peer'}", "warn")
        ttype = detected
        target = dict(target, type=detected)
        opts = dict(opts, ha_mode=(opts.get("ha_modes") or {}).get(detected) or opts.get("ha_mode"))
    mode = opts.get("ha_mode") or ("uninterruptible" if ttype != "standalone" else None)
    if ttype == "fgcp" and mode not in FGCP_MODES:
        raise RuntimeError(f"FGCP mode must be one of {FGCP_MODES}")
    if ttype == "fgsp" and mode not in FGSP_MODES:
        raise RuntimeError(f"FGSP mode must be one of {FGSP_MODES}")
    opts = dict(opts, ha_mode=mode)

    # 2. pre-checks
    if ttype == "fgsp":
        snap_members = [m for m in members if mode != "local-only" or m["id"] in (opts.get("member_ids") or [])]
    elif ttype == "fgcp" and mode == "local-only":
        snap_members = [m for m in members if m["id"] in (opts.get("member_ids") or [primary["id"]])]
    else:
        snap_members = [primary]
    pre = {}
    if opts.get("run_checks", True):
        job.set_stage("Pre-upgrade checks")
        for m in snap_members:
            job.check_cancel()
            pre[m["id"]] = snapshot_member(job, target, m, "pre-upgrade", check_ids, vdoms)
            if ttype == "fgcp":
                s = storage.load_snapshot(target["id"], pre[m["id"]])
                if ha_in_sync(s["results"].get("ha", {}).get("global", [])) is False \
                        and not opts.get("ignore_ha_sync"):
                    raise RuntimeError("HA cluster is out of sync before the upgrade. Aborting.")
    job.state["pre"] = pre
    if opts.get("_batch_sync") and not opts.get("pause_after_prechecks", True):
        job.wait_approval(f"Pre-checks complete for {target['name']}.", quiet=True,
                          waiting_msg="Pre-checks complete; waiting for the other devices to finish theirs")
    elif opts.get("approve_before_upgrade", True):
        job.wait_approval(f"Pre-checks complete for {target['name']}. Upgrade via {' → '.join(p['steps'])}?")

    reports = []

    def post_and_diff(m):
        if not opts.get("run_checks", True) or m["id"] not in pre:
            return None
        job.set_stage("Post-upgrade checks")
        sid = snapshot_member(job, target, m, "post-upgrade", check_ids, vdoms)
        job.set_stage("Comparing pre and post")
        rep = diff_snapshots(storage.load_snapshot(target["id"], pre[m["id"]]),
                             storage.load_snapshot(target["id"], sid))
        f = storage.save_report(target["id"], f"{pre[m['id']]}__vs__{sid}", rep)
        t = rep["totals"]
        job.say(f"{_label(m)}: {t['critical']} critical, {t['warning']} warning, {t['info']} informational "
                "differences", "error" if t["critical"] else "warn" if t["warning"] else "ok")
        reports.append({"member": _label(m), "member_id": m["id"], "pre": pre[m["id"]], "post": sid,
                        "totals": t, "report_file": str(f)})
        return rep

    # 3. upgrade
    running = info.version
    try:
        if ttype == "standalone":
            for v in hops:
                job.check_cancel()
                with client_for(primary, timeout=900) as c:
                    _push(job, c, v, opts, dry, _label(primary))
                _wait(job, primary, v, opts, dry)
                running = FosVersion.parse(v)
            post_and_diff(primary)

        elif ttype == "fgcp":
            for i, v in enumerate(hops):
                job.check_cancel()
                _fgcp_hop(job, target, v, opts, dry, running)
                running = FosVersion.parse(v)
                if i < len(hops) - 1 and opts.get("approve_between_hops", False):
                    job.wait_approval(f"{target['name']} is now on {v}. Continue to {hops[i + 1]}?")
            _restore_ha(job, target, dry)
            for m in snap_members:
                post_and_diff(m)

        elif ttype == "fgsp":
            sel = snap_members
            if mode == "simultaneous":
                for v in hops:
                    for m in sel:
                        with client_for(m, timeout=900) as c:
                            _push(job, c, v, opts, dry, _label(m))
                    for m in sel:
                        _wait(job, m, v, opts, dry)
                for m in sel:
                    post_and_diff(m)
            else:  # rolling / local-only: one peer at a time
                for idx, m in enumerate(sel):
                    for v in hops:
                        job.check_cancel()
                        with client_for(m, timeout=900) as c:
                            _push(job, c, v, opts, dry, _label(m))
                        _wait(job, m, v, opts, dry)
                    rep = post_and_diff(m)
                    if idx < len(sel) - 1:
                        crit = rep and rep["totals"]["critical"]
                        if crit and opts.get("stop_on_critical", True):
                            job.wait_approval(f"{_label(m)} shows {crit} critical post-check difference(s). "
                                              f"Continue with {_label(sel[idx + 1])} anyway?")
                        elif opts.get("approve_between_members", True):
                            job.wait_approval(f"{_label(m)} upgraded. Continue with {_label(sel[idx + 1])}?")
    except BaseException:
        if ttype == "fgcp" and job.state.get("ha_restore"):
            job.say("Upgrade interrupted. Check the HA upgrade-mode setting on the cluster.", "warn")
        raise
    return {"dry_run": dry, "target": target["name"], "target_id": target["id"], "path": p["steps"],
            "reports": reports, "pre_snapshots": pre}


# ------------------------------------------------------- many targets
def run_batch(job: Job, targets: list[dict], opts: dict) -> dict:
    """Upgrade several targets simultaneously. Each target runs as a child job; all of them do
    their pre-checks in parallel, then a single approval releases every target that passed."""
    dry = bool(opts.get("dry_run", False))
    job.label = "all devices"
    job.say(f"{'SIMULATION: ' if dry else ''}Installing FortiOS {opts.get('target_version')} on "
            f"{len(targets)} device(s) via {'FortiGuard download' if opts.get('source', 'fortiguard') == 'fortiguard' else 'local image push'}")
    job.set_stage("Pre-upgrade checks")
    sem = threading.Semaphore(int(opts.get("max_parallel_uploads", 4)))
    modes = opts.get("ha_modes") or {}
    children: list[Job] = []
    for t in targets:
        o = dict(opts, ha_mode=modes.get(t["type"]) or opts.get("ha_mode"), ha_modes=modes,
                 approve_before_upgrade=True, _batch_sync=True,
                 _upload_sem=sem, member_ids=(opts.get("member_ids") or {}).get(t["id"], []))
        children.append(jobs.start("upgrade", t["name"], run_upgrade, t, o, parent=job, target_id=t["id"],
                                   meta={"target_version": opts.get("target_version"), "dry_run": dry}))
    job.say(f"Started {len(children)} device upgrade(s){' (simulation)' if dry else ''}")

    def statuses():
        return [c.status for c in children]

    session = None
    if opts.get("probes"):
        session = probes.ProbeSession(opts["probes"], job, min_outage_ms=int(opts.get("probe_min_outage_ms", 1000)))
        job.probes = session
        session.start()

    try:
        while not all(s in ("waiting", "failed", "cancelled", "done") for s in statuses()):
            job.check_cancel()
            time.sleep(0.5)
        ready = [c for c in children if c.status == "waiting"]
        failed = [c for c in children if c.status in ("failed", "cancelled")]
        for c in failed:
            job.say(f"{c.title}: {c.error or c.status} - excluded", "error")
        if not ready:
            raise RuntimeError("No device passed its pre-checks.")
        names = ", ".join(c.title for c in ready)
        if opts.get("pause_after_prechecks", True):
            job.wait_approval(f"{len(ready)} device(s) passed pre-checks ({names})"
                              f"{f'; {len(failed)} failed' if failed else ''}. "
                              f"{'Run the simulated upgrade' if dry else 'Upgrade them now'}?")
        else:
            job.say(f"Pre-checks passed on {len(ready)} device(s) ({names}); installing now", "ok")
        if session:
            need = float(opts.get("probe_baseline_s", 15)) - (time.time() - session.started / 1000)
            if need > 0:
                job.say(f"Measuring a service baseline for {need:.0f} s more before installing")
                end = time.time() + need
                while time.time() < end:
                    job.check_cancel()
                    time.sleep(0.25)
            for line, lvl in session.summary_lines():
                if "not reachable before" in line:
                    job.say(line, lvl, src="probes")
            session.set_phase("upgrade")
        job.set_stage("Upgrading")
        for c in ready:
            c.answer(True)
        while any(s in ("running", "waiting") for s in statuses()):
            job.check_cancel()
            done = sum(1 for s in statuses() if s in ("done", "failed", "cancelled"))
            job.stage = f"Upgrading ({done}/{len(children)} finished)"
            time.sleep(1)
        if session:
            _settle_probes(job, session, opts)
    except jobs.Cancelled:
        for c in children:
            c.cancel()
        raise
    finally:
        if session:
            if session.phase == "baseline":
                job.say("Service probes stopped: the upgrade stopped before any firmware was installed", "warn",
                        src="probes")
            session.stop()
            job.probes = session.summary()
    if session:
        for line, lvl in session.summary_lines():
            job.say(line, lvl, src="probes")
    results = [{"job": c.id, "target": c.title, "status": c.status, "error": c.error,
                "reports": (c.result or {}).get("reports", []), "path": (c.result or {}).get("path")}
               for c in children]
    ok = sum(1 for r in results if r["status"] == "done")
    job.say(f"{ok}/{len(results)} device(s) upgraded successfully", "ok" if ok == len(results) else "warn")
    return {"dry_run": dry, "results": results, "probes": job.probes if session else None}


def _settle_probes(job: Job, session, opts: dict):
    """Keep probing after the last device finishes: a fixed settle period, extended while any
    service is still down (up to probe_recover_s) so the outage end is captured."""
    session.set_phase("settle")
    job.set_stage("Monitoring services after the upgrade")
    settle = float(opts.get("probe_settle_s", 30))
    extra = float(opts.get("probe_recover_s", 300))
    job.say(f"Upgrade finished; probing for another {settle:.0f} s to confirm the services are stable", src="probes")
    end = time.time() + settle
    hard = end + extra
    told = False
    while True:
        job.check_cancel()
        now = time.time()
        if now >= end and (session.all_up() or now >= hard):
            break
        if now >= end and not told:
            told = True
            job.say(f"A service is still down; waiting up to {extra:.0f} s more for it to recover", "warn", src="probes")
        time.sleep(0.25)
