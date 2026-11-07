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

FGCP_MODES = ("uninterruptible", "service-gated", "simultaneous", "local-only", "secondary-only")
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
            r = _send_and_watch(job, c, who, version, opts, lambda: c.upgrade_from_fortiguard(img["id"]),
                                lambda: True, what="the download request")
            if r is not None:
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
                r = _upload(job, c, path, fname, size, who, opts, version)
                if r is not None:
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


def _upload(job, c: FortiGateClient, path: Path, fname: str, size: int, who: str, opts: dict, version: str = ""):
    """Multipart file upload (as the GUI does); falls back to base64 JSON if the build rejects it."""
    mode = opts.get("upload_mode", "auto")
    if mode in ("auto", "multipart"):
        t0 = time.time()
        job.say(f"{who}: uploading {size / 1e6:.1f} MB to {c.base} as a file (multipart, streamed from disk) …")
        sent = [0]

        def progress(n):
            sent[0] = n

        try:
            r = _send_and_watch(job, c, who, version, opts,
                                lambda: c.upgrade_from_file_multipart(path, fname, progress=progress),
                                lambda: sent[0] >= size, progress=lambda: (sent[0], size), what="the upload")
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
        r = _send_and_watch(job, c, who, version, opts, lambda: c.upgrade_from_file(data, fname),
                            lambda: time.time() - t0 > max(60, len(data) * 4 / 3 / 2e6), what="the upload")
    except FortiAPIError as e:
        if e.status == 413:
            raise RuntimeError(f"{who}: {TOO_LARGE}") from e
        raise
    secs = max(time.time() - t0, 0.001)
    job.say(f"{who}: upload complete in {secs:.0f} s ({len(data) / 1e6 / secs:.1f} MB/s)")
    return r


def _install_evidence(job, c: FortiGateClient, version: str, opts: dict) -> str | None:
    """Signs that a FortiGate has started installing although its HTTP reply hasn't arrived:
    the address it was sent to (or another unit of the device) stops answering because it is
    rebooting, or reports the new version, or a unit has left the cluster."""
    signs = []
    try:
        with c.clone(timeout=8) as w:
            st = w.status()
            if version and FosVersion.parse(st["version"]) == FosVersion.parse(version):
                return f"{c.host} now runs {st['version']}"
            try:
                rows = c_ha(Ctx(client=w, vdom_mode=False), None)
                expect = int(opts.get("_ha_units") or 0)
                if expect > 1 and 0 < len(rows) < expect:
                    signs.append(f"only {len(rows)} of {expect} units are in the cluster (a unit is rebooting)")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        signs.append(f"{c.host} stopped answering (rebooting)")
    for m in opts.get("_members") or []:
        if m.get("host") == c.host and int(m.get("port") or 443) == int(c.port):
            continue
        try:
            with client_for(m, timeout=8) as w:
                w.retries = 0
                st = w.status()
            if version and FosVersion.parse(st["version"]) == FosVersion.parse(version):
                return f"{_label(m)} now runs {st['version']}"
        except Exception:  # noqa: BLE001
            signs.append(f"{_label(m)} stopped answering (rebooting)")
    return "; ".join(signs) or None


def _send_and_watch(job, c: FortiGateClient, who: str, version: str, opts: dict, call, body_sent,
                    progress=None, what="the request"):
    """Run the install request in the background and watch the device. FortiOS doesn't always
    answer it (an HA primary can go straight on to upgrading the cluster), so once the request
    has been sent, the install having visibly started counts as accepted. Returns the reply, or
    None when the install was confirmed another way."""
    box: dict = {}

    def run():
        try:
            box["r"] = call()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=run, daemon=True, name=f"install-{job.id}")
    th.start()
    grace = float(opts.get("install_reply_grace_s", 45))
    give_up = float(opts.get("install_start_timeout_s", 900))
    sent_at = None
    next_check = 0.0
    seen = 0
    try:
        while True:
            th.join(1.0)
            if not th.is_alive():
                break
            job.check_cancel()
            now = time.time()
            if sent_at is None:
                if progress:
                    n, size = progress()
                    job.flow_note(f"{n / max(size, 1):.0%} sent · {n / 1e6:.0f} of {size / 1e6:.0f} MB")
                if body_sent():
                    sent_at = now
                    next_check = now + grace
                    job.begin_wait("the FortiGate to accept the image")
                    job.say(f"{who}: {what} has been sent; waiting for the FortiGate to accept it")
                continue
            waited = now - sent_at
            if job.skip_wait.is_set():
                job.say(f"{who}: continuing without a reply at your request; the reboot wait confirms the install", "warn")
                job.flow_note("you continued without a reply")
                job._flow_attention()
                return None
            job.flow_note(f"sent · waiting {int(waited)}s for the FortiGate to accept it")
            if now >= next_check:
                next_check = now + 10
                ev = _install_evidence(job, c, version, opts)
                seen = seen + 1 if ev else 0
                if ev and (seen >= 2 or "now runs" in ev):
                    job.say(f"{who}: no reply to {what} after {int(waited)} s, but the install has started: {ev}", "ok")
                    job.flow_note(f"install started · {ev}"[:90])
                    return None
                if ev:
                    job.detail(f"{who}: possible sign the install started ({ev}); checking again")
            if waited > give_up:
                raise RuntimeError(f"{who}: {what} was sent {int(waited)} s ago, but the FortiGate neither answered nor "
                                   "started installing. Check System > Firmware on the device.")
    finally:
        if sent_at is not None:
            job.end_wait()
    if "e" in box:
        raise box["e"]
    return box.get("r")


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
                             on_tick=lambda s: (job.check_cancel(), job.say(f"{who}: {s}"), job.flow_note(_tick_note(s))))
    job.say(f"{who}: back online on {st['version']} build {st.get('build')}", "ok")
    job.flow_note(f"back on v{FosVersion.parse(st['version'])} build {st.get('build')}")


def _tick_note(s: str) -> str:
    """Short form of a reboot-wait tick for the stage diagram."""
    m = re.match(r"\[(\w+)\] (.*?)(; next check.*)?$", s)
    if not m:
        return s[:80]
    el, what = m.group(1), m.group(2)
    what = re.sub(r"^check #\d+: ", "", what)
    return f"{el} · {what}"[:90]


def _wait_ha_sync(job: Job, m: dict, opts: dict, dry: bool, timeout: float | None = None):
    """Wait until every unit is back in the cluster with matching configuration checksums."""
    if dry:
        return
    job.set_stage("Verifying HA sync")
    expect = int(job.state.get("ha_units") or 1)
    timeout = float(timeout if timeout is not None else opts.get("ha_sync_timeout", 900))
    t0 = time.time()
    end = t0 + timeout
    job.begin_wait("HA sync")
    try:
        _ha_sync_loop(job, m, opts, expect, t0, end, timeout)
    finally:
        job.end_wait()


def _ha_sync_loop(job, m, opts, expect, t0, end, timeout):
    last = ""
    while time.time() < end:
        job.check_cancel()
        if job.skip_wait.is_set():
            job.say(f"Stopped waiting for HA sync at your request ({last or 'no answer yet'})", "warn")
            job.flow_note(f"stopped waiting · {last}")
            job._flow_attention()
            return
        try:
            with client_for(m) as c:
                ha = c_ha(Ctx(client=c, vdom_mode=False), None)
            n_sync = sum(1 for r in ha if r.get("Config in sync") == "Yes")
            if len(ha) >= expect and ha_in_sync(ha):
                job.say(f"HA cluster in sync ({len(ha)} units)", "ok")
                job.flow_note(f"{len(ha)} units in sync")
                return
            last = (f"{len(ha)} of {expect} units in the cluster" if len(ha) < expect
                    else f"{n_sync} of {len(ha)} units in sync")
            job.flow_note(f"{int(time.time() - t0)}s · {last}")
            job.say(f"HA: {last}, waiting...", "debug")
        except Exception as e:  # noqa: BLE001
            last = f"no answer ({str(e)[:60]})"
            job.flow_note(f"{int(time.time() - t0)}s · {last}")
            job.say(f"HA sync check: {e}", "debug")
        job.skip_wait.wait(float(opts.get("poll_interval", 15)))  # wakes at once on Stop waiting
    job.say(f"HA cluster not in sync after {int(timeout)} s ({last}). Check 'diagnose sys ha checksum cluster'.",
            "warn")
    job.flow_note(f"timed out · {last}")
    job._flow_attention()


def _wait_units(job: Job, target: dict, version: str, opts: dict, dry: bool):
    """The upgrade is only complete when every unit runs the new firmware. The cluster address
    answers on the new version as soon as an upgraded unit takes over, while the old primary is
    still installing, so each unit is checked through its own address."""
    units = target["members"][1:]
    if dry or not units:
        return
    job.set_stage("Confirming every unit")
    t0 = time.time()
    end = t0 + float(opts.get("wait_timeout", 1800))
    todo = {m["id"]: m for m in units}
    done: list[str] = []
    job.begin_wait("every unit on the new firmware")
    try:
        _units_loop(job, units, todo, done, version, opts, t0, end)
    finally:
        job.end_wait()


def _units_loop(job, units, todo, done, version, opts, t0, end):
    while todo and time.time() < end:
        job.check_cancel()
        if job.skip_wait.is_set():
            job.say(f"Stopped waiting for {', '.join(_label(m) for m in todo.values())} at your request", "warn")
            job.flow_note(f"stopped waiting · {len(done)} of {len(units)} confirmed")
            job._flow_attention()
            return
        for mid, m in list(todo.items()):
            try:
                with client_for(m, timeout=10) as c:
                    c.retries = 0
                    st = c.status()
                if FosVersion.parse(st["version"]) == FosVersion.parse(version):
                    job.say(f"{_label(m)}: running {st['version']} build {st.get('build')}", "ok")
                    done.append(_label(m))
                    todo.pop(mid)
            except Exception:  # noqa: BLE001
                pass
        waiting = ", ".join(_label(m) for m in todo.values())
        job.flow_note(f"{int(time.time() - t0)}s · {len(done)} of {len(units)} on v{version}" + (f" · waiting for {waiting}" if waiting else ""))
        if todo:
            job.skip_wait.wait(float(opts.get("poll_interval", 15)))
    if todo:
        job.say(f"Not confirmed on {version} within the timeout: {', '.join(_label(m) for m in todo.values())}", "warn")
        job._flow_attention()
    else:
        job.flow_note(f"all {len(units)} units on v{version}")


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
    first = job._flow("hamode") is not None and job._flow("hamode")["status"] == "pending"
    if first:
        job.flow_go("hamode", mode)
    fos_mode = "local-only" if mode == "service-gated" else mode
    with client_for(primary) as c:
        changed = _fgcp_set_mode(job, c, running, fos_mode, dry)
    if mode == "service-gated":
        if changed:
            job.state.setdefault("ha_restore", changed)
        if first:
            job.flow_set("hamode", "done" if changed else "skipped",
                         f"upgrade-mode: {changed[1]} → local-only" if changed else "already local-only")
        return _gated_hop(job, target, version, opts, dry)
    if changed:
        job.state.setdefault("ha_restore", changed)
    if first:
        job.flow_set("hamode", "done" if changed else "skipped",
                     f"{changed[0]}: {changed[1]} → {mode if changed[0] == 'upgrade-mode' else ('enable' if mode == 'uninterruptible' else 'disable')}"
                     if changed else f"already {mode}")
    job.flow_go(f"xfer:{version}")
    sel = ([m for m in target["members"] if m["id"] in (opts.get("member_ids") or [primary["id"]])]
           if mode == "local-only" else [primary])
    for m in sel:
        job.check_cancel()
        with client_for(m, timeout=900) as c:
            _push(job, c, version, opts, dry, _label(m))
    job.flow_go(f"inst:{version}")
    if mode in ("simultaneous", "uninterruptible"):
        if mode == "uninterruptible":
            job.say("Uninterruptible: secondaries upgrade first, then the primary fails over and upgrades. "
                    "The cluster address drops briefly during failover.")
        _wait(job, primary, version, opts, dry)
        if job._flow(f"units:{version}"):
            job.flow_go(f"units:{version}")
            _wait_units(job, target, version, opts, dry)
        job.flow_go(f"sync:{version}")
        _wait_ha_sync(job, primary, opts, dry)
        if dry:
            job.flow_note("simulation")
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


# ------------------------------------------------------- service-gated FGCP
def _unit_map(job, target) -> dict:
    """serial -> the saved address that reaches that one unit (the device's extra addresses)."""
    if "unit_map" in job.state:
        return job.state["unit_map"]
    out = {}
    for m in target["members"][1:]:
        try:
            with client_for(m, timeout=15) as c:
                out[c.status()["serial"]] = m
        except Exception as e:  # noqa: BLE001
            job.say(f"{_label(m)}: not reachable ({e})", "warn")
    job.state["unit_map"] = out
    return out


def _cluster_roles(target) -> tuple[str, list[dict]]:
    """(primary serial, HA peers) as the cluster address reports them now."""
    from .hainfo import ha_info
    with client_for(target["members"][0]) as c:
        st = c.status()
        peers = ha_info(c).get("peers") or []
    return st["serial"], peers


_HA_STATUS_RX = re.compile(r"^\s*(?:Primary|Secondary|Master|Slave)\s*:\s*([^,\n]*?)\s*,\s*([A-Z0-9]{10,20})\b", re.M)


def parse_ha_status_members(text: str) -> dict[str, str]:
    """{serial: hostname} of the units in 'get system ha status' (Primary/Secondary lines)."""
    return {sn: name.strip() for name, sn in _HA_STATUS_RX.findall(text or "")}


def _serials_via_api(m: dict) -> tuple[set[str], str]:
    """Serials of the cluster members as one address reports them. ha-checksums can fail (HTTP 500)
    while the units run different firmware, so the HA peer list and statistics are tried first."""
    errs = []
    with client_for(m, timeout=10) as c:
        c.retries = 0
        for ep in ("system/ha-peer", "system/ha-statistics", "system/ha-checksums"):
            try:
                rows = c.monitor(ep) or []
                rows = rows if isinstance(rows, list) else [rows]
                sns = {r.get("serial_no") or r.get("serial") for r in rows if isinstance(r, dict)} - {None, ""}
                if sns:
                    return sns, ep.split("/")[-1]
                errs.append(f"{ep.split('/')[-1]} empty")
            except FortiAPIError as e:
                errs.append(f"{ep.split('/')[-1]} HTTP {e.status}")
    return set(), ", ".join(errs)


def _cluster_serials(job, target: dict, unit: dict | None, serial: str = "", expect: int = 0) -> tuple[set[str], str]:
    """Who is in the cluster right now, and how we know. Every source is asked until one shows the
    unit back with all units: the cluster address, the unit's own address, then 'get system ha
    status' over SSH. A primary on older firmware can leave a peer on newer firmware out of its
    HA peer list, so one source not seeing the unit isn't taken as the answer.
    Returns the confirming view, or the most complete view seen."""
    views: list[tuple[set[str], str]] = []
    tried = []

    def done(sns):
        return serial and serial in sns and len(sns) >= expect

    for who, m in (("cluster address", target["members"][0]), (_label(unit) if unit else "", unit)):
        if not m:
            continue
        try:
            sns, how = _serials_via_api(m)
            if sns:
                views.append((sns, f"{who} ({how})"))
                if done(sns) or not serial:
                    return views[-1]
            else:
                tried.append(f"{who}: {how}")
        except Exception as e:  # noqa: BLE001
            tried.append(f"{who}: {str(e)[:60]}")
    m = target["members"][0]
    if m.get("ssh_user"):
        try:
            with ssh_for(m) as sh:
                got = parse_ha_status_members(sh.run("get system ha status", timeout=30))
            if got:
                views.append((set(got), "get system ha status over SSH"))
                if done(set(got)):
                    return views[-1]
            else:
                tried.append("SSH: no Primary/Secondary lines")
        except Exception as e:  # noqa: BLE001
            tried.append(f"SSH: {str(e)[:60]}")
    if views:
        sns, how = max(views, key=lambda v: (serial in v[0], len(v[0])))
        others = [h for s_, h in views if h != how]
        return sns, how + (f"; also {', '.join(others)}" if others else "")
    return set(), "; ".join(tried)


def _log_ha_status(job, members: list):
    """Record each unit's own view of the cluster ('get system ha status' over SSH) in the job log,
    so a rejoin that can't be confirmed can be diagnosed afterwards."""
    for m in members:
        if not m:
            continue
        if not m.get("ssh_user"):
            job.say(f"{_label(m)}: no SSH login saved, so its 'get system ha status' can't be recorded", "warn")
            continue
        try:
            with ssh_for(m) as sh:
                out = sh.run("get system ha status", timeout=30)
            lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()][:40]
            job.say(f"{_label(m)} ({m['host']}): get system ha status\n" + "\n".join("    " + ln for ln in lines))
        except Exception as e:  # noqa: BLE001
            job.say(f"{_label(m)}: could not read 'get system ha status' over SSH: {e}", "warn")


def _wait_rejoin(job, target, serial: str, expect: int, opts: dict, dry: bool):
    """Wait until a unit that rebooted is back in the cluster. Works while the units run different
    firmware (where ha-checksums fails); after rejoin_timeout_s it asks rather than waiting on."""
    if dry:
        job.flow_note("simulation")
        return
    unit = (job.state.get("unit_map") or {}).get(serial)
    who = _label(unit) if unit else serial
    t0 = time.time()
    limit = float(opts.get("rejoin_timeout_s", 900))
    last = None
    dumped = False
    job.begin_wait(f"{who} to rejoin the cluster")
    try:
        while time.time() - t0 < limit:
            job.check_cancel()
            if job.skip_wait.is_set():
                job.say(f"Stopped waiting for {who} to rejoin at your request", "warn")
                job.flow_note("stopped waiting")
                job._flow_attention()
                return
            sns, how = _cluster_serials(job, target, unit, serial, expect)
            n = len(sns)
            if serial in sns and n >= expect:
                job.say(f"{who} is back in the cluster: {n} units seen by the {how}", "ok")
                job.flow_note(f"back in the cluster · {n} units ({how.split(' (')[0]})")
                return
            seen = (f"{n} of {expect} units in the cluster ({how})" if sns else f"no answer ({how})")
            if seen != last:
                job.say(f"Waiting for {who} to rejoin: {seen}")
                last = seen
            if not dumped and time.time() - t0 >= float(opts.get("rejoin_dump_after_s", 120)):
                dumped = True
                _log_ha_status(job, [target["members"][0], unit])
            job.flow_note(f"{int(time.time() - t0)}s · {seen}"[:110])
            job.skip_wait.wait(float(opts.get("poll_interval", 15)))
    finally:
        job.end_wait()
    job._flow_attention()
    job.flow_note(f"not seen back after {int(limit)}s · {last}"[:110])
    job.wait_approval(f"{who} runs the new firmware but hasn't been seen back in the cluster after {int(limit // 60)} min "
                      f"({last}). Check 'get system ha status' on the primary: if it isn't listed as a secondary, "
                      "upgrading the primary next takes the whole cluster down while it reboots. Carry on anyway?")


def _service_gate(job, opts: dict, sid: str, before: str, dry: bool):
    """Hold the upgrade until every service probe has had no failures for gate_stable_s.
    Without probes, wait gate_stable_s so the cluster settles. If services don't settle within
    gate_timeout_s, ask before carrying on."""
    job.flow_go(sid)
    stable = float(opts.get("gate_stable_s", 60))
    limit = float(opts.get("gate_timeout_s", 900))
    if dry:
        job.flow_note("simulation")
        return
    session = (opts.get("_probe_holder") or {}).get("s")
    t0 = time.time()
    job.begin_wait(f"services to be stable before {before}")
    try:
        if session is None:
            job.say(f"No service probes: waiting {stable:.0f} s for the cluster to settle before {before}")
            while time.time() - t0 < stable and not job.skip_wait.is_set():
                job.check_cancel()
                job.flow_note(f"no probes · settling {int(stable - (time.time() - t0))}s")
                job.skip_wait.wait(1)
            job.flow_note(f"no probes · settled {int(time.time() - t0)}s")
            return
        bad: list[str] = []
        while time.time() - t0 < limit:
            job.check_cancel()
            bad = session.unstable(int(stable * 1000))
            if not bad and time.time() - t0 >= min(stable, 5):
                job.say(f"Every service has been stable for {stable:.0f} s; continuing with {before}", "ok")
                job.flow_note(f"stable for {stable:.0f}s after {int(time.time() - t0)}s")
                return
            if job.skip_wait.is_set():
                job.say(f"Continuing with {before} at your request; still unstable: {', '.join(bad)}", "warn")
                job.flow_note(f"you continued · unstable: {', '.join(bad)}")
                job._flow_attention()
                return
            job.flow_note(f"{int(time.time() - t0)}s · waiting for {', '.join(bad) or 'a clean period'}")
            job.skip_wait.wait(1)
    finally:
        job.end_wait()
    job._flow_attention()
    job.flow_note(f"not stable after {int(limit)}s: {', '.join(bad)}")
    job.wait_approval(f"{', '.join(bad)} still not stable after {int(limit)} s. Continue with {before} anyway?")


def _gated_hop(job, target, version, opts, dry):
    """Service-gated FGCP upgrade (FortiOS upgrade-mode local-only): each secondary is upgraded
    through its own address and must rejoin and the services must be stable before the next
    unit. The primary goes last; its reboot is the cutover, so the app decides when it happens."""
    units = _unit_map(job, target)
    p_sn, peers = (job.state.get("primary_sn"), []) if dry else _cluster_roles(target)
    if dry:
        peers = [{"serial": s} for s in units] or [{"serial": p_sn}]
    expect = max(int(job.state.get("ha_units") or 1), len(peers))
    order = [p["serial"] for p in peers if p["serial"] != p_sn] + [p_sn]
    missing = [s for s in order if s not in units]
    if missing:
        raise RuntimeError("Service-gated upgrades go unit by unit, so every unit needs its own saved address "
                           f"(its management-ip or HA management interface). Missing for: {', '.join(missing)}.")
    v = version
    # the primary changes with every hop (the upgraded unit takes over): re-plan this hop's stages
    opts.setdefault("_gated_orders", {})[v] = [(s, _label(units[s])) for s in order]
    if job.state.get("plan_args"):
        job.flow_plan(_flow_steps(*job.state["plan_args"]))
    for sn in order:
        m = units[sn]
        who = _label(m)
        is_primary = sn == p_sn
        if is_primary:
            _service_gate(job, opts, f"gate:{v}", "the cutover", dry)
            if opts.get("approve_cutover"):
                job.flow_go(f"approvecut:{v}")
                job.wait_approval(f"The upgraded unit is back and services are stable. Upgrade {who} now? Its reboot "
                                  "moves traffic to the upgraded unit.")
        job.flow_go(f"xfer:{sn}:{v}")
        with client_for(m, timeout=900) as c:
            _push(job, c, v, opts, dry, who)
        job.flow_go(f"inst:{sn}:{v}")
        if is_primary:
            job.say(f"{who} is the primary: its reboot is the cutover to the upgraded unit", "warn")
        _wait(job, m, v, opts, dry)
        job.flow_go(f"rejoin:{sn}:{v}")
        _wait_rejoin(job, target, sn, expect, opts, dry)
    job.flow_go(f"sync:{v}")
    if not dry:
        job.say("Both units run the new firmware; waiting for the cluster to synchronise")
    _wait_ha_sync(job, target["members"][0], opts, dry)
    _service_gate(job, opts, f"after:{v}", "the next step", dry)


def _restore_ha(job, target, dry):
    r = job.state.get("ha_restore")
    if not r or not job.state.get("restore_ha") or r[1] is None:
        job.flow_set("harestore", "skipped", "nothing to restore")
        return
    key, prev = r
    job.set_stage("Restoring HA setting")
    job.flow_go("harestore", f"{key} → {prev}")
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


# ------------------------------------------------------------ stage plan
def _flow_steps(ttype, mode, hops, opts, members, path) -> list[dict]:
    """The stages an upgrade goes through, in order, for the install view's diagram."""
    src = "FortiGuard download" if opts.get("source", "fortiguard") == "fortiguard" else "push from this computer"
    st = [{"id": "connect", "label": "Connect and plan", "sub": "read the device, plan the path"}]
    if opts.get("run_checks", True):
        st.append({"id": "pre", "label": "Pre-upgrade checks", "sub": "collect state to compare"})
    if opts.get("_batch_sync") or opts.get("approve_before_upgrade", True):
        st.append({"id": "approve", "label": "Go-ahead", "sub": "start the install"})
    inst_sub = {"uninterruptible": "secondaries first, then failover and the primary",
                "simultaneous": "all units reboot together", "local-only": "selected units only",
                "secondary-only": "secondaries only"}
    if ttype == "fgcp":
        st.append({"id": "hamode", "label": "Set HA upgrade mode", "sub": "local-only (app-controlled)" if mode == "service-gated" else mode})
    for k, v in enumerate(hops):
        g = f"v{v}"
        # planned order: secondaries, then the primary; after each hop the upgraded unit is primary
        order = (opts.get("_gated_orders") or {}).get(v) or list(opts.get("_gated_order") or [])
        if order and v not in (opts.get("_gated_orders") or {}) and k % 2 == 1 and len(order) == 2:
            order = order[::-1]
        if ttype == "fgcp" and mode == "service-gated":
            for i, (sn, name) in enumerate(order):
                last = i == len(order) - 1
                if last:
                    st.append({"id": f"gate:{v}", "label": "Services stable", "sub": "before the cutover", "group": g})
                    if opts.get("approve_cutover"):
                        st.append({"id": f"approvecut:{v}", "label": "Approve cutover", "sub": "your go-ahead", "group": g})
                role = "primary · reboot is the cutover" if last else "secondary"
                st += [{"id": f"xfer:{sn}:{v}", "label": "Transfer image", "sub": f"{name} · {src}", "group": g},
                       {"id": f"inst:{sn}:{v}", "label": "Install and reboot", "sub": f"{name} ({role})", "group": g},
                       {"id": f"rejoin:{sn}:{v}", "label": "Rejoin cluster", "sub": name, "group": g}]
            st += [{"id": f"sync:{v}", "label": "HA in sync", "sub": "configuration checksums match", "group": g},
                   {"id": f"after:{v}", "label": "Services stable", "sub": "after the upgrade", "group": g}]
            continue
        if ttype == "fgsp" and mode != "simultaneous":
            for m in members if mode != "local-only" else [x for x in members if x["id"] in (opts.get("member_ids") or [])]:
                who = m.get("name") or m["host"]
                st += [{"id": f"xfer:{m['id']}:{v}", "label": "Transfer image", "sub": f"{who} · {src}", "group": g},
                       {"id": f"inst:{m['id']}:{v}", "label": "Install and reboot", "sub": who, "group": g}]
            continue
        st += [{"id": f"xfer:{v}", "label": "Transfer image", "sub": src, "group": g},
               {"id": f"inst:{v}", "label": "Install and reboot", "sub": inst_sub.get(mode, "") if ttype == "fgcp" else "", "group": g}]
        if ttype == "fgcp" and mode in ("simultaneous", "uninterruptible"):
            if len(members) > 1:
                st.append({"id": f"units:{v}", "label": "Every unit upgraded", "sub": "each unit's own address reports the new firmware", "group": g})
            st.append({"id": f"sync:{v}", "label": "HA in sync", "sub": "every unit back, configuration checksums match", "group": g})
    if ttype == "fgcp" and opts.get("restore_ha_setting", True):
        st.append({"id": "harestore", "label": "Restore HA setting", "sub": "put the upgrade mode back"})
    if opts.get("run_checks", True):
        st += [{"id": "post", "label": "Post-upgrade checks", "sub": "collect state again"},
               {"id": "cmp", "label": "Compare", "sub": "before and after"}]
    return st


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
    job.flow_plan([{"id": "connect", "label": "Connect and plan", "sub": "read the device, plan the path"}])
    job.flow_go("connect")
    with client_for(primary) as c:
        info = c.discover()
        job.say(f"{info.hostname}: {info.model} running {info.version.full}")
        job.flow_note(f"{info.hostname} · {info.model} · v{info.version}")
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

    if info.ha_mode in ("a-p", "a-a"):
        try:
            from .hainfo import ha_info
            with client_for(primary) as c:
                job.state["ha_units"] = max(1, len(ha_info(c).get("peers") or []))
        except Exception:  # noqa: BLE001
            job.state["ha_units"] = max(1, len(members))
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
    opts = dict(opts, ha_mode=mode, _members=members, _ha_units=job.state.get("ha_units"))
    if ttype == "fgcp" and mode == "service-gated":
        if info.version < UPGRADE_MODE_SINCE:
            raise RuntimeError(f"Service-gated upgrades use FortiOS upgrade-mode local-only, which needs FortiOS 7.4.1 or "
                               f"later (running {info.version}). Upgrade to 7.4.1+ with Uninterruptible first.")
        units = _unit_map(job, target)
        job.state["primary_sn"] = info.serial
        try:
            from .hainfo import ha_info as _hai
            with client_for(primary) as c:
                peers = _hai(c).get("peers") or []
        except Exception:  # noqa: BLE001
            peers = []
        sns = [x["serial"] for x in peers if x["serial"] != info.serial] + [info.serial]
        missing = [s for s in sns if s not in units]
        if missing:
            raise RuntimeError("Service-gated upgrades go unit by unit, so every unit needs its own saved address "
                               "(its management-ip or HA management interface). No address reaches "
                               f"{', '.join(missing)}. Add them with Edit, or use Add device to detect them.")
        opts = dict(opts, _gated_order=[(s, _label(units[s])) for s in sns])
    job.state["plan_args"] = (ttype, mode, hops, opts, members, p["steps"])
    job.flow_plan(_flow_steps(ttype, mode, hops, opts, members, p["steps"]))
    job.flow_set("connect", "done", " → ".join(f"v{s}" for s in p["steps"]))

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
        job.flow_go("pre")
        for m in snap_members:
            job.check_cancel()
            pre[m["id"]] = snapshot_member(job, target, m, "pre-upgrade", check_ids, vdoms)
            if ttype == "fgcp":
                s = storage.load_snapshot(target["id"], pre[m["id"]])
                if ha_in_sync(s["results"].get("ha", {}).get("global", [])) is False \
                        and not opts.get("ignore_ha_sync"):
                    raise RuntimeError("HA cluster is out of sync before the upgrade. Aborting.")
    job.state["pre"] = pre
    if pre:
        job.flow_set("pre", "done", f"{len(pre)} unit{'s' if len(pre) > 1 else ''} collected")
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
        job.flow_go("post", _label(m))
        sid = snapshot_member(job, target, m, "post-upgrade", check_ids, vdoms)
        job.set_stage("Comparing pre and post")
        job.flow_go("cmp")
        rep = diff_snapshots(storage.load_snapshot(target["id"], pre[m["id"]]),
                             storage.load_snapshot(target["id"], sid))
        f = storage.save_report(target["id"], f"{pre[m['id']]}__vs__{sid}", rep)
        t = rep["totals"]
        job.say(f"{_label(m)}: {t['critical']} critical, {t['warning']} warning, {t['info']} informational "
                "differences", "error" if t["critical"] else "warn" if t["warning"] else "ok")
        job.flow_set("cmp", "done", f"{t['critical']} critical · {t['warning']} warning · {t['info']} info")
        if t["critical"]:
            job._flow("cmp")["attention"] = True
        reports.append({"member": _label(m), "member_id": m["id"], "pre": pre[m["id"]], "post": sid,
                        "totals": t, "report_file": str(f)})
        return rep

    # 3. upgrade
    running = info.version
    try:
        if ttype == "standalone":
            for v in hops:
                job.check_cancel()
                job.flow_go(f"xfer:{v}")
                with client_for(primary, timeout=900) as c:
                    _push(job, c, v, opts, dry, _label(primary))
                job.flow_go(f"inst:{v}")
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
                    job.flow_go(f"xfer:{v}")
                    for m in sel:
                        with client_for(m, timeout=900) as c:
                            _push(job, c, v, opts, dry, _label(m))
                    job.flow_go(f"inst:{v}")
                    for m in sel:
                        _wait(job, m, v, opts, dry)
                for m in sel:
                    post_and_diff(m)
            else:  # rolling / local-only: one peer at a time
                for idx, m in enumerate(sel):
                    for v in hops:
                        job.check_cancel()
                        job.flow_go(f"xfer:{m['id']}:{v}")
                        with client_for(m, timeout=900) as c:
                            _push(job, c, v, opts, dry, _label(m))
                        job.flow_go(f"inst:{m['id']}:{v}")
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
    holder: dict = {}  # the probe session, once started, for service-gated HA upgrades
    for t in targets:
        o = dict(opts, ha_mode=modes.get(t["type"]) or opts.get("ha_mode"), ha_modes=modes,
                 approve_before_upgrade=True, _batch_sync=True, _probe_holder=holder,
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
        holder["s"] = session

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
    """Keep probing after the last device finishes: a fixed settle period, then until every
    service has been stable (no failed probe) for probe_stable_s, at most probe_recover_s more.
    The user can stop waiting at any time."""
    session.set_phase("settle")
    settle = float(opts.get("probe_settle_s", 30))
    extra = float(opts.get("probe_recover_s", 300))
    stable_ms = int(float(opts.get("probe_stable_s", 20)) * 1000)
    job.say(f"Upgrade finished; probing for another {settle:.0f} s, then until every service has been stable for "
            f"{stable_ms // 1000} s (at most {extra:.0f} s more)", src="probes")
    end = time.time() + settle
    hard = end + extra
    told = 0.0
    job.begin_wait("services to be stable")
    try:
        _settle_loop(job, session, end, hard, extra, stable_ms, told)
    finally:
        job.end_wait()


def _settle_loop(job, session, end, hard, extra, stable_ms, told):
    while True:
        job.check_cancel()
        now = time.time()
        bad = session.unstable(stable_ms)
        if job.skip_wait.is_set():
            job.say("Stopped monitoring at your request" + (f"; still unstable: {', '.join(bad)}" if bad else ""),
                    "warn", src="probes")
            break
        if now >= end and (not bad or now >= hard):
            if bad:
                job.say(f"Still unstable after {extra:.0f} s: {', '.join(bad)}. Finishing; check these services.",
                        "warn", src="probes")
            break
        if now < end:
            job.stage = f"Monitoring services after the upgrade ({int(end - now)} s)"
        else:
            job.stage = (f"Waiting for {', '.join(bad)} to be stable for {stable_ms // 1000} s "
                         f"(up to {int(hard - now)} s more)")
            if now - told > 30:
                told = now
                job.say(f"Waiting for {', '.join(bad)} to be stable for {stable_ms // 1000} s; "
                        f"up to {int(hard - now)} s more", "warn", src="probes")
        time.sleep(0.25)
