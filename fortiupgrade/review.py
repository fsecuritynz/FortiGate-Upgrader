"""Upgrade-readiness review: the running configuration, HA, licences, resources and recent logs
are checked for things that can make an HA upgrade fail or cause problems after the upgrade.

Read-only. Each finding: {id, severity (critical|warning|info), category, title, detail, advice,
evidence[], member}. Logs are grouped by pattern with counts and samples.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from collections import Counter

from . import hainfo, release_notes, storage
from .checks import LOG_LEVELS, LOG_SUBTYPES
from .client import FortiAPIError, looks_like_config
from .version import FosVersion

SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}
INACTIVE = {"no_license", "expired", "invalid", "unregistered", "cloud_na", "pending", "unlicensed"}
CATEGORIES = ["HA", "Licensing", "Removed or changed features", "Compatibility", "Resources", "Configuration", "Logs"]


# ------------------------------------------------------------- config parser
def entries(cfg: str, section: str) -> list[dict]:
    """Entries of every `config <section>` block (all VDOMs): [{"_name": edit name, key: value}].
    Nested config blocks inside an entry are kept as raw text under "_sub"."""
    out: list[dict] = []
    lines = cfg.splitlines()
    i = 0
    head = f"config {section}"
    while i < len(lines):
        s = lines[i].strip()
        if s == head:
            ind = len(lines[i]) - len(lines[i].lstrip())
            i += 1
            cur = None
            depth = 0
            while i < len(lines):
                ln = lines[i]
                st = ln.strip()
                li = len(ln) - len(ln.lstrip())
                if li == ind and st == "end":
                    break
                if depth == 0 and li == ind + 4 and st.startswith("edit "):
                    cur = {"_name": st[5:].strip().strip('"'), "_sub": ""}
                    out.append(cur)
                elif depth == 0 and li == ind + 4 and st == "next":
                    cur = None
                elif st.startswith("config "):
                    depth += 1
                    if cur is not None:
                        cur["_sub"] += st + "\n"
                elif st == "end" and depth:
                    depth -= 1
                elif depth and cur is not None:
                    cur["_sub"] += st + "\n"
                elif st.startswith(("set ", "unset ")) and cur is not None and li == ind + 8:
                    k, _, v = st[4:].partition(" ")
                    cur[k] = v.strip()
                elif st.startswith("set ") and cur is None and li == ind + 4:
                    k, _, v = st[4:].partition(" ")
                    out.append({"_name": "", k: v.strip(), "_setting": True})
                i += 1
        i += 1
    return out


def settings(cfg: str, section: str) -> dict:
    """`set` lines directly under `config <section>` (a settings block), merged across VDOMs."""
    d: dict = {}
    for e in entries(cfg, section):
        if e.get("_setting"):
            d.update({k: v for k, v in e.items() if not k.startswith("_")})
    return d


def _names(v: str) -> list[str]:
    return re.findall(r'"([^"]+)"', v or "") or (v or "").split()


# ------------------------------------------------------------------ helpers
def _find(out, sev, cat, title, detail="", advice="", evidence=None, member="", fid=""):
    out.append({"id": fid or re.sub(r"\W+", "-", title.lower()).strip("-")[:48], "severity": sev, "category": cat,
                "title": title, "detail": detail, "advice": advice, "evidence": [str(e)[:220] for e in (evidence or [])][:8],
                "member": member})


def _lic_state(lic: dict, key: str):
    d = lic.get(key)
    if not isinstance(d, dict):
        return None, None
    return str(d.get("status") or "").lower(), d.get("expires")


# Security profiles used by enabled policies -> the FortiGuard licence they need
PROFILE_LICENCE = [
    ("av-profile", "antivirus", "Antivirus", "antivirus signatures"),
    ("ips-sensor", "ips", "IPS", "IPS signatures"),
    ("webfilter-profile", "web_filtering", "Web filtering", "FortiGuard web filter ratings"),
    ("dnsfilter-profile", "web_filtering", "DNS filter", "FortiGuard DNS ratings (part of web filtering)"),
    ("application-list", "appctrl", "Application control", "application signatures"),
    ("emailfilter-profile", "antispam", "Email filter (antispam)", "FortiGuard antispam ratings"),
    ("videofilter-profile", "web_filtering", "Video filter", "FortiGuard video ratings"),
]


def review_config(cfg: str, lic: dict, ha: dict, model: str, current: str, target: str, steps: list[str],
                  ha_type: str, source: str, member: str = "") -> list[dict]:
    out: list[dict] = []
    tgt = FosVersion.parse(target)

    # ---------------------------------------------------------------- licences
    pols = [p for p in entries(cfg, "firewall policy") if p.get("status", "enable") != "disable"]
    now = time.time()
    for key, lkey, feature, what in PROFILE_LICENCE:
        users = [p["_name"] for p in pols if p.get(key)]
        if not users:
            continue
        st, exp = _lic_state(lic, lkey)
        if st is None:
            continue
        if st in INACTIVE:
            _find(out, "warning", "Licensing", f"{feature} is used but not licensed",
                  f"{len(users)} enabled polic{'y uses' if len(users) == 1 else 'ies use'} {'an' if feature[0] in 'AEIOU' else 'a'} {feature.lower()} profile "
                  f"({key}) but the licence status is '{st}'. Without a licence {what} are not updated, and "
                  "after the upgrade the new firmware's bundled database may be older than what the unit uses now.",
                  "Renew the licence, or remove the profile from the policies so the policies don't appear to protect traffic they don't.",
                  [f"policy {n}" for n in users[:6]], member, f"lic-{lkey}-{key}")
        elif isinstance(exp, (int, float)) and 0 < exp - now < 30 * 86400:
            _find(out, "info", "Licensing", f"{feature} licence expires in {int((exp - now) / 86400)} days",
                  f"Used by {len(users)} enabled polic{'y' if len(users) == 1 else 'ies'}.", "Plan the renewal.",
                  member=member, fid=f"lic-exp-{lkey}")
    fc, _ = _lic_state(lic, "forticare")
    fw_upd, _ = _lic_state(lic, "firmware_updates")
    if fc in INACTIVE or fw_upd in INACTIVE:
        sev = "critical" if source == "fortiguard" else "warning"
        _find(out, sev, "Licensing", "No valid FortiCare support contract",
              f"FortiCare status '{fc or '—'}'" + (f", firmware updates '{fw_upd}'" if fw_upd else "") +
              ". FortiGuard only offers firmware downloads to units with a valid support contract.",
              "Use 'Push local image' with images from the Firmware Store, or renew FortiCare." if source == "fortiguard"
              else "The image is pushed from this computer, so the upgrade itself works.", member=member, fid="lic-forticare")
    vm, _ = _lic_state(lic, "vm")
    if vm and vm not in ("vm_valid", "valid", "licensed"):
        _find(out, "critical", "Licensing", "FortiGate-VM licence is not valid",
              f"VM licence status '{vm}'. An invalid VM licence limits the unit and can stop it passing traffic after a reboot.",
              "Fix the VM licence before upgrading.", member=member, fid="lic-vm")

    # ---------------------------------------------------- removed / changed features
    for h in release_notes.evaluate_rules(cfg, current, target, model, storage.user_rules_file()):
        _find(out, h["severity"], "Removed or changed features", h["title"], h["message"],
              " ".join(x for x in ((f"Introduced in FortiOS {h['introduced_in']}." if h.get("introduced_in") else ""),
                                   (f"See {h['reference']}." if h.get("reference") else "")) if x),
              h.get("evidence"), member, "rule-" + h["id"])

    # ------------------------------------------------------------- compatibility
    faz = [e for e in entries(cfg, "log fortianalyzer setting") if e.get("status") == "enable"]
    cm = settings(cfg, "system central-management")
    if faz:
        srv = faz[0].get("server", "").strip('"')
        _find(out, "warning", "Compatibility", "Logs go to FortiAnalyzer",
              f"FortiAnalyzer {srv} must run the same FortiOS major/minor version as the target (v{tgt.major}.{tgt.minor}) or later, "
              "or logging and reports may stop working.", "Check the FortiAnalyzer version and upgrade it first if needed.",
              [f"set server {srv}"] if srv else [], member, "compat-faz")
    if cm.get("type") == "fortimanager":
        _find(out, "warning", "Compatibility", "Managed by FortiManager",
              f"FortiManager must support FortiOS v{tgt.major}.{tgt.minor} before the unit is upgraded, and its device "
              "database must be refreshed (retrieve config) afterwards.", "Upgrade FortiManager first; retrieve the configuration after the upgrade.",
              [f"set fmg {cm.get('fmg', '')}"], member, "compat-fmg")
    sw = entries(cfg, "switch-controller managed-switch")
    ap = entries(cfg, "wireless-controller wtp")
    ext = entries(cfg, "extension-controller extender")
    if sw or ap or ext:
        parts = [f"{len(sw)} FortiSwitch" if sw else "", f"{len(ap)} FortiAP" if ap else "", f"{len(ext)} FortiExtender" if ext else ""]
        _find(out, "warning", "Compatibility", "Managed FortiSwitch / FortiAP / FortiExtender devices",
              f"{', '.join(p for p in parts if p)} managed by this unit. Their firmware must be supported by FortiOS v{target} "
              "(see 'Product integration and support' in the release notes), or they may go offline after the upgrade.",
              "Check the compatibility matrix; upgrade the managed devices if required.",
              [f"switch {e['_name']}" for e in sw[:3]] + [f"AP {e['_name']}" for e in ap[:3]], member, "compat-fabric")
    ems = entries(cfg, "endpoint-control fctems")
    if any(e.get("status", "enable") == "enable" and e.get("server") for e in ems):
        _find(out, "info", "Compatibility", "FortiClient EMS connector",
              "Check the EMS version is supported by the target FortiOS.", member=member, fid="compat-ems")

    # --------------------------------------------------------------------- HA
    if ha_type == "fgcp" or (ha.get("mode") in ("a-p", "a-a")):
        if ha.get("session_pickup") in ("disable", None) and ha.get("mode") in ("a-p", "a-a"):
            _find(out, "warning", "HA", "Session pickup is disabled",
                  "Sessions are not synchronised to the other unit, so every failover during the upgrade drops existing "
                  "connections (VPNs, long-lived TCP sessions).", "Enable 'set session-pickup enable' before upgrading if the outage matters.",
                  ["set session-pickup disable"], member, "ha-session-pickup")
        if ha.get("override") == "enable":
            _find(out, "warning", "HA", "HA override is enabled",
                  "With override, the higher-priority unit takes the primary role back as soon as it has been upgraded, "
                  "adding an extra failover (and traffic interruption) to the upgrade.",
                  "Expect two failovers, or disable override for the upgrade.", ["set override enable"], member, "ha-override")
        hb = ha.get("hbdev") or []
        if len(hb) == 1:
            _find(out, "warning", "HA", "Only one heartbeat interface",
                  f"Heartbeat runs over {hb[0]} only. If it drops while a unit reboots, both units can become primary (split brain).",
                  "Add a second heartbeat interface.", [f"set hbdev {hb[0]}"], member, "ha-hbdev")
        if not ha.get("monitor"):
            _find(out, "info", "HA", "No monitored interfaces",
                  "The cluster doesn't fail over on link failure; a unit that comes back from the upgrade with a link down stays primary.",
                  member=member, fid="ha-monitor")
        if not ha.get("ha_mgmt_interfaces") and not re.search(r"^\s*set management-ip ", cfg, re.M):
            _find(out, "info", "HA", "No per-unit management address",
                  "Neither a dedicated HA management interface nor an interface management-ip is configured, so a secondary "
                  "unit can't be reached directly to check it during the upgrade.",
                  "Configure ha-mgmt-interfaces or 'set management-ip' so each unit can be monitored.", member=member, fid="ha-mgmt")
        hs = settings(cfg, "system ha")
        if hs.get("uninterruptible-primary-wait") and hs["uninterruptible-primary-wait"].isdigit() and int(hs["uninterruptible-primary-wait"]) < 15:
            _find(out, "info", "HA", "Short uninterruptible-primary-wait",
                  f"The primary waits only {hs['uninterruptible-primary-wait']} minutes for the secondary to come back.",
                  member=member, fid="ha-wait")

    # ---------------------------------------------------------- configuration
    pp = settings(cfg, "system password-policy")
    forced = [e["_name"] for e in entries(cfg, "system admin") if e.get("force-password-change") == "enable"]
    if pp.get("status") == "enable" and pp.get("expire-status") == "enable" or forced:
        _find(out, "warning", "Configuration", "Admin password expiry or forced password change",
              "An admin whose password has expired or must be changed is stopped at a password-change prompt at the next "
              "SSH login, which breaks CLI checks and SSH configuration reads during the upgrade.",
              "Log in once interactively with the account this app uses, or exempt it.",
              (["config system password-policy: set expire-status enable"] if pp.get("expire-status") == "enable" else []) +
              [f"admin {n}: set force-password-change enable" for n in forced], member, "cfg-password")
    disabled_ifs = [e["_name"] for e in entries(cfg, "system interface") if e.get("status") == "down"]
    mon_down = [i for i in (ha.get("monitor") or []) if i in disabled_ifs]
    if mon_down:
        _find(out, "critical", "HA", "A monitored interface is administratively down",
              f"HA monitors {', '.join(mon_down)}, which is set down. A unit with fewer working monitored interfaces "
              "is not chosen as primary, which can stop the upgrade's failover.", "Remove it from 'set monitor' or bring it up.",
              [f"edit {n} / set status down" for n in mon_down], member, "ha-monitor-down")
    return out


# ------------------------------------------------------------------- logs
LOG_PATTERNS = [
    ("conserve", "critical", "Resources", "Memory conserve mode", r"conserve mode|conserve-mode|memory (is )?(low|exhaust)",
     "The unit ran short of memory. An upgrade needs free memory to stage the image; fix the cause first."),
    ("crash", "critical", "Stability", "Process crashes or unexpected restarts", r"crash|segfault|segmentation|kernel panic|watchdog|unexpected(ly)? (reboot|restart)|application .* (exit|killed)|core dump",
     "Read 'diagnose debug crashlog read' and check the target release notes' resolved issues for the process."),
    ("ha-sync", "critical", "HA", "HA synchronisation problems", r"out of sync|not (in )?sync|checksum (mismatch|differ)|sync(hroni[sz]ation)? fail|config.*sync.*fail",
     "Get the cluster in sync (diagnose sys ha checksum cluster) before upgrading."),
    ("ha-hb", "warning", "HA", "HA heartbeat loss or failovers", r"heartbeat.*(lost|down|fail|timeout)|hb.*(lost|down)|(member|peer|device) (lost|left|timeout|leave)|ha failover|failover|state chang|became (primary|master)|split.?brain",
     "Unplanned failovers before the upgrade point to a heartbeat or monitored-link problem; the upgrade relies on clean failovers."),
    ("hardware", "warning", "Hardware", "Hardware alerts (power, fans, temperature)", r"power supply|\bpsu\b|\bfan\b|temperature|thermal|over.?heat|voltage|sensor alarm",
     "Check the hardware before rebooting it."),
    ("disk", "warning", "Hardware", "Disk or flash problems", r"(disk|flash|storage|file ?system|partition).*(error|fail|full|corrupt|bad|unmount)|need(s)? format|disk log full",
     "A failing or full disk can stop the image being written; check 'diagnose hardware deviceinfo disk' and log usage."),
    ("routing", "warning", "Network", "Routing neighbour instability", r"bgp.*(down|notification|hold timer|idle)|ospf.*(down|dead|lost)|neighbor .* down",
     "Note which neighbours were already unstable so post-upgrade differences aren't blamed on the upgrade."),
    ("vpn", "warning", "Network", "VPN failures", r"(ipsec|phase ?[12]|tunnel|ike|ssl.?vpn).*(down|fail|error|timeout|mismatch)|negotiat\w* fail",
     "Note tunnels that were already failing before the upgrade."),
    ("sdwan", "warning", "Network", "SD-WAN health-check failures", r"(health.?check|sla|performance sla).*(dead|fail|down)|sd-?wan member .* (dead|down)",
     "Note SD-WAN members already failing SLAs."),
    ("link", "warning", "Network", "Interface link changes", r"link (status|monitor|is)? ?.*\bdown\b|interface .* (down|flap)|link down",
     "Flapping links cause HA failovers and SD-WAN changes during the upgrade."),
    ("fortiguard", "warning", "Licensing", "FortiGuard or licence problems", r"(licen[cs]e|contract|subscription).*(expir|invalid|fail)|fortiguard.*(fail|unreach|unable|error)|(update|fds).*fail",
     "FortiGuard firmware downloads need a working FortiGuard connection and valid contract."),
    ("config", "warning", "Configuration", "Configuration errors", r"config(uration)?.*(error|fail|invalid|could not)|failed to (save|apply|commit)|cli.*error",
     "Configuration errors usually get worse across an upgrade; resolve them first."),
    ("auth", "warning", "Security", "Failed admin logins", r"(admin|administrator).*(login|log in).*(fail|disabled|lockout)|login fail|authentication fail",
     "Many failed logins can mean brute force against the management interface."),
]
_RX = [(pid, sev, cat, title, re.compile(rx, re.I), advice) for pid, sev, cat, title, rx, advice in LOG_PATTERNS]
_IFACE = re.compile(r'(?:interface|port|link)\s*[=:]?\s*"?([A-Za-z][\w.\-]*\d[\w.\-]*)"?', re.I)


def read_logs(c, vdom: str | None = "root", rows: int = 500) -> list[dict]:
    seen, out = set(), []
    for dev in ("memory", "disk"):
        for sub in LOG_SUBTYPES:
            try:
                r = c.get(f"log/{dev}/event/{sub}", vdom=vdom, params={"rows": rows})
            except FortiAPIError:
                continue
            for e in (r.get("results") if isinstance(r, dict) else None) or []:
                k = (e.get("eventtime"), e.get("logid"), e.get("msg"))
                if k in seen:
                    continue
                seen.add(k)
                out.append(e)
        if out:
            break
    out.sort(key=lambda e: e.get("eventtime") or 0, reverse=True)
    return out


def analyse_logs(events: list[dict]) -> dict:
    """Group recent events by known bad patterns; anything else at error level or worse goes in
    'Other errors'."""
    groups: dict[str, dict] = {}
    levels = Counter()
    for e in events:
        lvl = str(e.get("level") or "").lower()
        levels[lvl] += 1
        text = f"{e.get('logdesc') or ''} {e.get('msg') or ''} {e.get('action') or ''}"
        when = f"{e.get('date', '')} {e.get('time', '')}".strip()
        hit = None
        for pid, sev, cat, title, rx, advice in _RX:
            if rx.search(text) and lvl not in ("information", "debug") or (rx.search(text) and pid in ("conserve", "crash", "ha-sync")):
                hit = (pid, sev, cat, title, advice)
                break
        if not hit:
            if lvl in ("emergency", "alert", "critical", "error"):
                key = "other:" + (e.get("logdesc") or "event")
                hit = (key, "warning" if lvl in ("emergency", "alert", "critical") else "info", "Other",
                       f"{e.get('logdesc') or 'Event'} ({LOG_LEVELS.get(lvl, lvl)})", "")
            else:
                continue
        g = groups.setdefault(hit[0], {"id": hit[0], "severity": hit[1], "category": hit[2], "title": hit[3],
                                       "advice": hit[4], "count": 0, "first": when, "last": when, "samples": [],
                                       "levels": Counter(), "subjects": Counter()})
        g["count"] += 1
        g["first"] = when or g["first"]  # events are newest first, so the last one seen is the oldest
        g["levels"][lvl] += 1
        if len(g["samples"]) < 6:
            g["samples"].append({"time": when, "level": LOG_LEVELS.get(lvl, lvl), "subtype": e.get("subtype") or "",
                                 "event": e.get("logdesc") or "", "msg": (e.get("msg") or e.get("action") or "")[:300]})
        if hit[0] == "link":
            m = _IFACE.search(text)
            if m:
                g["subjects"][m.group(1)] += 1
    out = []
    for g in groups.values():
        # a single link-down or routing blip is normal; repeated ones are a problem
        if g["id"] == "link":
            flapping = {k: v for k, v in g["subjects"].items() if v >= 3}
            if flapping:
                g["title"] = "Interface flapping: " + ", ".join(f"{k} ({v}×)" for k, v in flapping.items())
            elif g["count"] < 3:
                g["severity"] = "info"
        if g["id"] in ("routing", "vpn", "sdwan") and g["count"] < 3:
            g["severity"] = "info"
        if g["id"] == "auth" and g["count"] < 10:
            g["severity"] = "info"
        if g["id"] == "ha-hb" and g["count"] < 2:
            g["severity"] = "info"
        g["levels"] = dict(g["levels"])
        g["subjects"] = dict(g["subjects"])
        out.append(g)
    out.sort(key=lambda g: (SEV_ORDER.get(g["severity"], 3), -g["count"]))
    times = [f"{e.get('date', '')} {e.get('time', '')}".strip() for e in events]
    return {"read": len(events), "oldest": times[-1] if times else None, "newest": times[0] if times else None,
            "levels": {LOG_LEVELS.get(k, k): v for k, v in levels.items()}, "groups": out}


def config_error_lines(text: str) -> list[str]:
    """Rejected lines from 'diagnose debug config-error-log read' (e.g. '>>> "set x" @ root.firewall.policy.3:command parse error')."""
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s or "config-error-log" in s or s.endswith(("#", "$")):
            continue
        if ">>>" in s or re.search(r"parse error|command fail|error", s, re.I):
            out.append(s[:220])
    return out


def parse_crashlog(text: str, days: int = 30) -> list[str]:
    """Recent entries from 'diagnose debug crashlog read' (lines starting with a date)."""
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    out = []
    for ln in (text or "").splitlines():
        m = re.match(r"\s*\d+:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)", ln)
        if not m:
            continue
        try:
            when = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if when >= cutoff and re.search(r"(application|signal|crash|killed|firmware|kernel)", m.group(2), re.I) \
                and not re.search(r"(firmware upgraded|boot|reboot by|restarted by user)", m.group(2), re.I):
            out.append(f"{m.group(1)} {m.group(2)}"[:220])
    return out


# ------------------------------------------------------------------- runner
def review_target(job, target: dict, target_version: str, source: str = "fortiguard", ha_modes: dict | None = None) -> dict:
    """Review every reachable unit of a target. Runs in a job (job.say logs progress)."""
    from .orchestrator import _label, client_for, effective_type, ssh_for  # local import: orchestrator imports a lot

    res = {"target_id": target["id"], "target": target["name"], "members": [], "findings": [], "logs": [],
           "licences": {}, "steps": [], "reviewed_at": dt.datetime.now().isoformat(timespec="seconds")}
    ha_type = None
    for idx, m in enumerate(target["members"]):
        who = _label(m)
        job.set_stage(f"Reviewing {who}")
        try:
            with client_for(m, timeout=60) as c:
                info = c.discover()
                if ha_type is None:
                    ha_type = effective_type(target["type"], info)
                res["members"].append({"member": who, "model": info.model, "version": str(info.version),
                                       "hostname": info.hostname, "ha_mode": info.ha_mode, "ok": True})
                job.say(f"{who}: {info.model} {info.version.full}, HA {info.ha_mode}")
                cur = str(info.version)
                steps = [cur, target_version]
                try:
                    from . import upgrade_path
                    steps = upgrade_path.plan(c, cur, target_version, info.model, [], storage.user_paths_file())["steps"] or steps
                except Exception:  # noqa: BLE001
                    pass
                res["steps"] = steps
                # configuration
                cfg = ""
                try:
                    cfg = c.backup_config("global")
                    if not looks_like_config(cfg):
                        raise FortiAPIError("backup refused")
                    job.say(f"{who}: read the configuration ({len(cfg.splitlines()):,} lines)")
                except (FortiAPIError, Exception) as e:  # noqa: BLE001
                    cfg = ""
                    if m.get("ssh_user"):
                        try:
                            from .snapshot import config_via_ssh
                            with ssh_for(m) as ssh:
                                cfg = config_via_ssh(ssh, info.vdom_mode)
                            job.say(f"{who}: read the configuration over SSH")
                        except Exception as e2:  # noqa: BLE001
                            job.say(f"{who}: configuration could not be read: {e2}", "warn")
                    else:
                        job.say(f"{who}: configuration could not be read ({e}); configuration checks skipped", "warn")
                if not cfg:
                    _find(res["findings"], "warning", "Configuration", "Configuration not reviewed",
                          "The configuration could not be read, so licence-vs-feature, removed-feature and compatibility checks were skipped.",
                          "Add SSH credentials so the configuration can be read over the CLI.", member=who, fid="cfg-unread")
                try:
                    lic = c.monitor("license/status") or {}
                except FortiAPIError:
                    lic = {}
                res["licences"][who] = {k: v.get("status") for k, v in lic.items() if isinstance(v, dict)}
                ha = hainfo.ha_info(c)
                if cfg and idx == 0 or (cfg and info.ha_mode == "standalone"):
                    res["findings"] += review_config(cfg, lic, ha, info.model, cur, target_version, steps, ha_type or target["type"],
                                                     source, who)
                elif cfg:
                    # secondaries share the cluster configuration; only licences differ per unit
                    res["findings"] += [f for f in review_config(cfg, lic, ha, info.model, cur, target_version, steps,
                                                                 ha_type or target["type"], source, who)
                                        if f["category"] == "Licensing"]
                # cluster health (primary only)
                if idx == 0 and info.ha_mode in ("a-p", "a-a"):
                    _cluster_checks(c, target, ha, res["findings"], who, info)
                # resources
                try:
                    r = c.monitor("system/resource/usage", params={"interval": "1-min"}) or {}

                    def cur_of(k):
                        v = r.get(k)
                        v = v[0].get("current") if isinstance(v, list) and v else v
                        return v if isinstance(v, (int, float)) else None
                    mem, cpu = cur_of("mem"), cur_of("cpu")
                    if mem is not None and mem >= 80:
                        _find(res["findings"], "critical" if mem >= 88 else "warning", "Resources", f"Memory usage {mem}%",
                              "The upgrade stages the new image in memory. Above about 88% FortiOS enters conserve mode and the upgrade can fail.",
                              "Reduce load or reboot the unit (in a maintenance window) before upgrading.", member=who, fid="res-mem")
                    if cpu is not None and cpu >= 85:
                        _find(res["findings"], "warning", "Resources", f"CPU usage {cpu}%",
                              "High CPU slows HA synchronisation and the upgrade.", member=who, fid="res-cpu")
                except FortiAPIError:
                    pass
                # logs
                ev = read_logs(c, "root" if info.vdom_mode else None)
                la = analyse_logs(ev)
                la["member"] = who
                res["logs"].append(la)
                job.say(f"{who}: reviewed {la['read']} log events" + (f" ({la['oldest']} to {la['newest']})" if la["read"] else ""))
                if not la["read"]:
                    _find(res["findings"], "warning", "Logs", "No event logs could be read",
                          "Neither memory nor disk event logs were returned. Check that event logging is enabled and the API admin profile can read logs.",
                          member=who, fid="log-none")
                for g in la["groups"]:
                    if g["severity"] in ("critical", "warning"):
                        _find(res["findings"], g["severity"], "Logs", f"{g['title']} ({g['count']} event{'s' if g['count'] != 1 else ''})",
                              f"{g['first']} to {g['last']}" if g["first"] != g["last"] else g["last"], g["advice"],
                              [f"{s['time']} {s['msg']}" for s in g["samples"][:4]], who, "log-" + g["id"])
                # crash log and boot-time config errors over SSH
                if m.get("ssh_user"):
                    _ssh_checks(m, who, res["findings"], job, ssh_for, info.vdom_mode)
        except Exception as e:  # noqa: BLE001
            res["members"].append({"member": who, "ok": False, "error": str(e)})
            job.say(f"{who}: cannot review: {e}", "error")
            _find(res["findings"], "critical" if idx == 0 else "warning", "Connectivity", f"{who} could not be reviewed", str(e),
                  member=who, fid="conn")
    if (ha_type or target["type"]) == "fgcp" and len(target["members"]) == 1:
        _find(res["findings"], "info", "HA", "Only the cluster address was reviewed",
              "Logs and licences of the other unit weren't read because its own address isn't saved.",
              "Use 'Save addresses' on the device's HA cluster panel.", fid="ha-one-member")
    seen = set()
    uniq = []
    for f in res["findings"]:
        k = (f["id"], f["member"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    res["findings"] = sorted(uniq, key=lambda f: (SEV_ORDER.get(f["severity"], 3), f["category"], f["title"]))
    res["totals"] = {s: sum(1 for f in res["findings"] if f["severity"] == s) for s in ("critical", "warning", "info")}
    for f in res["findings"]:
        if f["severity"] != "info":
            job.say(f"{f['member'] or target['name']}: [{f['severity']}] {f['title']}", "error" if f["severity"] == "critical" else "warn")
    return res


def _cluster_checks(c, target, ha, findings, who, info):
    peers = ha.get("peers") or []
    if len(peers) == 1:
        _find(findings, "critical", "HA", "Only one unit in the cluster",
              "The FGCP cluster reports a single member. Upgrading it reboots the only unit, so traffic stops for the whole upgrade.",
              "Check the heartbeat links and the other unit before upgrading.", member=who, fid="ha-one-peer")
    try:
        cs = [x for x in c.monitor("system/ha-checksums") or [] if isinstance(x, dict)]
    except FortiAPIError:
        cs = []
    if len(cs) > 1:
        keys = set().union(*(x.get("checksum", {}).keys() for x in cs))
        bad = sorted(k for k in keys if len({str(x.get("checksum", {}).get(k)) for x in cs}) > 1)
        if bad:
            _find(findings, "critical", "HA", "Cluster configuration out of sync",
                  f"Checksums differ for: {', '.join(bad)}. An out-of-sync cluster can fail over to a unit with a different "
                  "configuration, and the upgrade may stall waiting for the cluster to synchronise.",
                  "Compare with 'diagnose sys ha checksum cluster' and resynchronise before upgrading.",
                  [f"{x.get('serial_no')}: " + ", ".join(f"{k}={x.get('checksum', {}).get(k)}" for k in bad) for x in cs], who, "ha-sync")
    try:
        stats = [x for x in c.monitor("system/ha-statistics") or [] if isinstance(x, dict)]
    except FortiAPIError:
        stats = []
    models = {x.get("model") for x in stats if x.get("model")}
    if len(models) > 1:
        _find(findings, "critical", "HA", "Cluster units are different models", ", ".join(sorted(models)), member=who, fid="ha-models")


def _ssh_checks(m, who, findings, job, ssh_for, vdom_mode=False):
    try:
        with ssh_for(m) as ssh:
            try:
                crash = ssh.run("diagnose debug crashlog read", global_ctx=True, vdom_mode=vdom_mode, timeout=60)
                recent = parse_crashlog(crash)
                if recent:
                    _find(findings, "warning" if len(recent) < 5 else "critical", "Stability",
                          f"{len(recent)} crash-log entr{'y' if len(recent) == 1 else 'ies'} in the last 30 days",
                          "Processes crashed recently. Check whether the target version fixes them (resolved issues) and that "
                          "the unit is stable before rebooting it.", "Read the full crash log: diagnose debug crashlog read",
                          recent[-6:], who, "ssh-crashlog")
                job.say(f"{who}: crash log read ({len(recent)} recent entr{'y' if len(recent) == 1 else 'ies'})")
            except Exception as e:  # noqa: BLE001
                job.detail(f"{who}: crash log not read: {e}")
            try:
                err = ssh.run("diagnose debug config-error-log read", global_ctx=True, vdom_mode=vdom_mode, timeout=60)
                lines = config_error_lines(err)
                if lines:
                    _find(findings, "warning", "Configuration", "Configuration lines rejected at the last boot or upgrade",
                          "FortiOS couldn't apply these lines from the configuration file. They were silently dropped and are "
                          "likely to fail again on the next upgrade.", "Review and fix or remove them before upgrading.",
                          lines[:8], who, "ssh-config-errors")
            except Exception as e:  # noqa: BLE001
                job.detail(f"{who}: config error log not read: {e}")
    except Exception as e:  # noqa: BLE001
        job.say(f"{who}: SSH checks skipped: {e}", "warn")
