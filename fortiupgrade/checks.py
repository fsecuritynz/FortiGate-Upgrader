"""Pre/post-upgrade state collectors.

Each check returns a list of records with human-readable field names and values
(e.g. "Status": "Up", "Speed": "1 Gbps"), so the same data reads well in the UI, in
the .log file and in diffs. Records carry stable key fields so pre/post snapshots can
be diffed per VDOM. Volatile counters are deliberately not collected.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .client import FortiAPIError, FortiGateClient

CRIT, WARN, INFO = "critical", "warning", "info"
DASH = "—"


@dataclass
class Ctx:
    client: FortiGateClient
    vdom_mode: bool
    ssh: Any = None  # FortiSSH | None
    bgp_neighbors: dict = field(default_factory=dict)  # vdom -> [ip]
    cache: dict = field(default_factory=dict)


@dataclass
class Check:
    id: str
    name: str
    scope: str  # "vdom" | "global"
    collect: Callable[[Ctx, str | None], list[dict] | str]
    key: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    kind: str = "records"  # records | text
    needs_ssh: bool = False
    default: bool = True
    severity: Callable[..., str] | None = None
    summary: Callable[[list[dict]], str] | None = None
    description: str = ""


# ------------------------------------------------------------- formatting
def _list(x) -> list:
    if x is None:
        return []
    if isinstance(x, dict):
        return list(x.values())
    return list(x)


def updown(x) -> str:
    if isinstance(x, str):
        return {"up": "Up", "down": "Down", "1": "Up", "0": "Down"}.get(x.lower(), x.title())
    return "Up" if x else "Down"


def yesno(x) -> str:
    return "Yes" if x in (True, 1, "1", "yes", "enable", "enabled") else "No"


def speed(x) -> str:
    try:
        n = int(float(x))
    except (TypeError, ValueError):
        return DASH
    if n <= 0:
        return DASH
    return f"{n // 1000} Gbps" if n >= 1000 and n % 1000 == 0 else f"{n} Mbps"


def duplex(x) -> str:
    return {1: "Full", "1": "Full", "full": "Full", 0: "Half", "0": "Half", "half": "Half"}.get(x, DASH)


def title(s: str) -> str:
    s = str(s or "")
    special = {"bgp": "BGP", "ospf": "OSPF", "rip": "RIP", "isis": "IS-IS", "connect": "Connected",
               "static": "Static", "ips": "IPS", "av": "AV", "sdwan": "SD-WAN", "dns": "DNS",
               "forticare": "FortiCare", "fortiguard": "FortiGuard", "vpn": "VPN", "ha": "HA",
               "db": "DB", "ai": "AI", "genai": "GenAI", "iot": "IoT", "ot": "OT", "vm": "VM", "sms": "SMS",
               "casb": "CASB", "psirt": "PSIRT", "sase": "SASE", "url": "URL", "urls": "URLs", "geoip": "GeoIP",
               "ip": "IP", "os": "OS", "id": "ID", "na": "N/A", "fortianalyzer": "FortiAnalyzer",
               "fortimanager": "FortiManager", "fortisandbox": "FortiSandbox", "forticloud": "FortiCloud",
               "fortisase": "FortiSASE", "fortiems": "FortiClient EMS", "forticonverter": "FortiConverter",
               "aas": "as a Service", "alci": "(account)", "fds": "FDS"}
    return " ".join(special.get(w.lower(), w.capitalize()) for w in re.split(r"[_\-\s]+", s) if w)


def pct(x) -> str:
    try:
        return f"{float(x):.0f} %"
    except (TypeError, ValueError):
        return DASH


def count_by(rows, f):
    return Counter(r.get(f) for r in rows)


# --------------------------------------------------------------- collectors
def c_system(ctx: Ctx, _):
    st = ctx.client.status()
    labels = {"hostname": "Hostname", "model": "Model", "model_code": "Model code", "serial": "Serial number",
              "version": "FortiOS version", "build": "Build"}
    return [{"Item": labels.get(k, title(k)), "Value": str(v)} for k, v in st.items()]


ACCESS = {"ping": "PING", "https": "HTTPS", "http": "HTTP", "ssh": "SSH", "telnet": "TELNET", "snmp": "SNMP",
          "fgfm": "FMG-Access", "radius-acct": "RADIUS accounting", "probe-response": "Probe response",
          "fabric": "Security Fabric", "ftm": "FTM", "speed-test": "Speed test", "scim": "SCIM", "capwap": "CAPWAP"}
IF_TYPES = {"physical": "Physical", "vlan": "VLAN", "aggregate": "Aggregate", "redundant": "Redundant",
            "tunnel": "Tunnel", "loopback": "Loopback", "hard-switch": "Hardware switch", "switch": "Software switch",
            "vap-switch": "WiFi SSID", "vdom-link": "VDOM link", "emac-vlan": "EMAC VLAN", "wl-mesh": "WiFi mesh",
            "fortilink": "FortiLink", "switch-vlan": "FortiSwitch VLAN", "geneve": "GENEVE", "vxlan": "VXLAN",
            "ssl": "SSL VPN tunnel", "lan-extension": "LAN extension"}
IF_ROLES = {"lan": "LAN", "wan": "WAN", "dmz": "DMZ", "undefined": "Undefined"}


def admin_access(v) -> str:
    items = [ACCESS.get(x, x) for x in str(v or "").split()]
    return ", ".join(items) if items else "None"


def _if_config(ctx: Ctx) -> dict:
    if "ifcfg" not in ctx.cache:
        try:
            rows = _list(ctx.client.cmdb("system/interface", params={
                "format": "name|allowaccess|type|role|vdom|ip|status|alias|description|member"}))
        except FortiAPIError:
            rows = []
        ctx.cache["ifcfg"] = {r.get("name"): r for r in rows}
    return ctx.cache["ifcfg"]


def c_interfaces(ctx: Ctx, vdom):
    mon = ctx.client.monitor("system/interface", vdom=vdom,
                             params={"include_vlan": "true", "include_aggregate": "true"}) or {}
    cfg = _if_config(ctx)
    out = []
    for i in _list(mon):
        ip = i.get("ip") or "0.0.0.0"
        c = cfg.get(i.get("name"), {})
        out.append({
            "Interface": i.get("name"),
            "Alias": i.get("alias") or c.get("alias") or DASH,
            "Type": IF_TYPES.get(c.get("type"), title(c.get("type")) or DASH),
            "Role": IF_ROLES.get(c.get("role"), title(c.get("role")) or DASH),
            "Status": updown(i.get("link")),
            "Admin status": ("Disabled" if c.get("status") == "down" else "Enabled") if c else DASH,
            "IP address": f"{ip}/{i.get('mask', 0)}" if ip != "0.0.0.0" else DASH,
            "Admin access": admin_access(c.get("allowaccess")) if c else DASH,
            "Speed": speed(i.get("speed")),
            "Duplex": duplex(i.get("duplex")) if speed(i.get("speed")) != DASH else DASH,
            "MAC": i.get("mac") if (i.get("mac") or "").strip("0:") else DASH,
        })
    return out


# Interfaces FortiOS reports as type "physical" that are not front-panel ports.
NOT_A_PORT = re.compile(r"^(npu\d+_vlink\d+|.*vlink.*|modem|wwan|lte\d*|ssl\..*|l2t\..*|naf\..*|wqt\..*|"
                        r"fortilink|_default|vsw\..*|sit\d+|ipsec.*)$", re.I)


def physical_ports(ctx: Ctx) -> list[dict]:
    """Front-panel ports only: physical interfaces plus hardware/virtual-switch member ports,
    without NPU VDOM links, modems and other logical interfaces."""
    cfg = _if_config(ctx)
    try:
        mon = {i.get("name"): i for i in _list(ctx.client.monitor(
            "system/interface", params={"include_vlan": "false", "include_aggregate": "true"}))}
    except FortiAPIError:
        mon = {}
    # optional: per-port data incl. switch members (not on every build)
    avail = {}
    try:
        for a in _list(ctx.client.monitor("system/available-interfaces")):
            if a.get("name"):
                avail[a["name"]] = a
    except FortiAPIError:
        pass
    members: dict[str, str] = {}
    for path in ("system/virtual-switch", "system/physical-switch"):
        try:
            for sw in _list(ctx.client.cmdb(path)):
                for prt in sw.get("port") or []:
                    if prt.get("name"):
                        members[prt["name"]] = sw.get("name")
        except FortiAPIError:
            pass
    for c in cfg.values():  # legacy hardware switches list their members on the interface
        for prt in c.get("member") or []:
            if isinstance(prt, dict) and prt.get("interface-name") and c.get("type") == "hard-switch":
                members[prt["interface-name"]] = c.get("name")
    names = []
    for n, c in cfg.items():
        if c.get("type") == "physical" and not NOT_A_PORT.match(n or ""):
            names.append(n)
    if not cfg:  # no config access: fall back to the monitor list
        names = [n for n in mon if not NOT_A_PORT.match(n or "")]
    names += [m for m in members if m not in names]
    agg = {}
    for c in cfg.values():
        if c.get("type") in ("aggregate", "redundant"):
            for prt in c.get("member") or []:
                if isinstance(prt, dict) and prt.get("interface-name"):
                    agg[prt["interface-name"]] = c.get("name")
    out = []
    for n in names:
        m = mon.get(n) or {}
        a = avail.get(n) or {}
        sw = members.get(n)
        link = m.get("link") if "link" in m else a.get("link") if "link" in a else None
        inherited = False
        if link is None and sw and sw in mon:
            link, inherited = mon[sw].get("link"), True
        c = cfg.get(n) or cfg.get(sw or "", {})
        ip = m.get("ip") or ""
        out.append({
            "name": n,
            "alias": (cfg.get(n) or {}).get("alias") or "",
            "status": "Up" if link else "Down" if link is not None else "Unknown",
            "status_from_switch": inherited,
            "member_of": sw or agg.get(n) or "",
            "ip": f"{ip}/{m.get('mask', 0)}" if ip and ip != "0.0.0.0" else "",
            "speed": speed(m.get("speed") or a.get("speed")),
            "access": admin_access(c.get("allowaccess")) if c else "",
            "media": str(a.get("media") or a.get("port_media") or m.get("media") or "").lower(),
            "admin_down": (cfg.get(n) or {}).get("status") == "down",
        })
    return out


def c_sdwan_zones(ctx: Ctx, vdom):
    cfg = ctx.client.cmdb("system/sdwan", vdom=vdom) or {}
    if isinstance(cfg, list):
        cfg = cfg[0] if cfg else {}
    try:
        mon = ctx.client.monitor("virtual-wan/members", vdom=vdom) or {}
    except FortiAPIError:
        mon = {}
    mon_by_if = {}
    for k, v in (mon.items() if isinstance(mon, dict) else enumerate(mon)):
        if isinstance(v, dict):
            mon_by_if[v.get("interface") or k] = v
    enabled = cfg.get("status") == "enable"
    members = cfg.get("members") or []
    out = []
    for z in cfg.get("zone") or []:
        zm = [m for m in members if m.get("zone") == z.get("name")]
        if not zm:
            out.append({"Zone": z.get("name"), "Member": DASH, "Gateway": DASH, "Cost": DASH, "Priority": DASH,
                        "Member state": DASH, "Link": DASH, "SD-WAN": "Enabled" if enabled else "Disabled"})
        for m in zm:
            st = mon_by_if.get(m.get("interface"), {})
            link = st.get("link_status") or st.get("status")
            out.append({"Zone": z.get("name"), "Member": m.get("interface"),
                        "Gateway": m.get("gateway") if m.get("gateway") not in (None, "", "0.0.0.0") else DASH,
                        "Cost": m.get("cost", DASH), "Priority": m.get("priority", DASH),
                        "Member state": "Disabled" if m.get("status") == "disable" else "Enabled",
                        "Link": updown(link) if link is not None else DASH,
                        "SD-WAN": "Enabled" if enabled else "Disabled"})
    return out


LOG_LEVELS = {"emergency": "Emergency", "alert": "Alert", "critical": "Critical", "error": "Error",
              "warning": "Warning", "notice": "Notice", "information": "Information", "debug": "Debug"}
LOG_SUBTYPES = ("system", "vpn", "router", "ha", "sdwan", "user")


def c_logs(ctx: Ctx, vdom, rows: int = 300):
    """Recent event logs (system, VPN, routing, HA, SD-WAN, user) from memory or disk."""
    seen, out = set(), []
    errors = []
    for dev in ("memory", "disk"):
        for sub in LOG_SUBTYPES:
            try:
                r = ctx.client.get(f"log/{dev}/event/{sub}", vdom=vdom, params={"rows": rows})
            except FortiAPIError as e:
                errors.append(str(e))
                continue
            for e in (r.get("results") if isinstance(r, dict) else None) or []:
                k = (e.get("eventtime"), e.get("logid"))
                if k in seen:
                    continue
                seen.add(k)
                out.append({"_t": e.get("eventtime") or 0,
                            "Time": f"{e.get('date', '')} {e.get('time', '')}".strip(),
                            "Category": title(e.get("subtype")),
                            "Level": LOG_LEVELS.get(e.get("level"), title(e.get("level"))),
                            "Event": e.get("logdesc") or DASH,
                            "Message": (e.get("msg") or e.get("action") or DASH)[:300],
                            "User / source": e.get("user") or e.get("ui") or e.get("remip") or DASH})
        if out:
            break
    if not out and errors and len(errors) == len(LOG_SUBTYPES) * 2:
        raise FortiAPIError("no log device readable (memory/disk): " + errors[0])
    out.sort(key=lambda x: x.pop("_t") or 0, reverse=True)
    return out


def c_arp(ctx: Ctx, vdom):
    return [{"IP address": a.get("ip"), "MAC": a.get("mac"), "Interface": a.get("interface")}
            for a in _list(ctx.client.monitor("network/arp", vdom=vdom))]


def _routes(ctx, vdom, fam):
    out = []
    for r in _list(ctx.client.monitor(f"router/{fam}", vdom=vdom)):
        gw = r.get("gateway") or ""
        out.append({
            "Type": title(r.get("type")),
            "Destination": r.get("ip_mask"),
            "Gateway": gw if gw not in ("0.0.0.0", "::") else DASH,
            "Interface": r.get("interface") or DASH,
            "Distance": r.get("distance", DASH),
            "Metric": r.get("metric", DASH),
            "VRF": r.get("vrf", 0),
        })
    return out


def c_routes4(ctx, vdom):
    return _routes(ctx, vdom, "ipv4")


def c_routes6(ctx, vdom):
    return _routes(ctx, vdom, "ipv6")


def c_bgp(ctx: Ctx, vdom):
    neigh = _list(ctx.client.monitor("router/bgp/neighbors", vdom=vdom))
    try:
        paths = _list(ctx.client.monitor("router/bgp/paths", vdom=vdom))
    except FortiAPIError:
        paths = []
    rx = Counter(p.get("learned_from") for p in paths)
    best = Counter(p.get("learned_from") for p in paths if p.get("is_best"))
    ctx.bgp_neighbors[vdom] = [n.get("neighbor_ip") for n in neigh]
    return [{
        "Neighbour": n.get("neighbor_ip"),
        "Remote AS": n.get("remote_as", DASH),
        "State": title(n.get("state", "")) or DASH,
        "Admin": "Enabled" if n.get("admin_status", True) else "Shut down",
        "Received prefixes": rx.get(n.get("neighbor_ip"), 0),
        "Best paths": best.get(n.get("neighbor_ip"), 0),
    } for n in neigh]


def c_bgp_tx(ctx: Ctx, vdom):
    """Advertised (tx) prefix counts per BGP neighbour, via CLI."""
    if ctx.ssh is None:
        return []
    out = []
    for ip in ctx.bgp_neighbors.get(vdom) or [n["Neighbour"] for n in c_bgp(ctx, vdom)]:
        txt = ctx.ssh.run(f"get router info bgp neighbors {ip} advertised-routes",
                          vdom=vdom, vdom_mode=ctx.vdom_mode)
        m = re.search(r"Total number of prefixes\s+(\d+)", txt)
        n = int(m.group(1)) if m else len(re.findall(r"^\*>?\s", txt, re.M))
        out.append({"Neighbour": ip, "Advertised prefixes": n})
    return out


def c_ospf(ctx, vdom):
    return [{"Neighbour": n.get("neighbor_ip"), "Router ID": n.get("router_id"),
             "State": title(n.get("state"))} for n in _list(ctx.client.monitor("router/ospf/neighbors", vdom=vdom))]


def c_ipsec(ctx, vdom):
    out = []
    for t in _list(ctx.client.monitor("vpn/ipsec", vdom=vdom)):
        for p in t.get("proxyid") or [{}]:
            out.append({
                "Tunnel": t.get("name"),
                "Phase 2": p.get("p2name") or DASH,
                "Remote gateway": t.get("rgwy") or DASH,
                "Status": updown(p.get("status", "down")),
            })
    return out


def c_sslvpn(ctx, vdom):
    users = _list(ctx.client.monitor("vpn/ssl", vdom=vdom))
    return [{"Metric": "Connected SSL VPN users", "Value": len(users)}]


def c_sdwan(ctx, vdom):
    out = []
    hc = ctx.client.monitor("virtual-wan/health-check", vdom=vdom) or {}
    for name, members in (hc.items() if isinstance(hc, dict) else []):
        for m, d in (members or {}).items():
            out.append({"Health check": name, "Member": m, "Status": title((d or {}).get("status", "")) or DASH})
    return out


def c_policy(ctx, vdom):
    pols = _list(ctx.client.cmdb("firewall/policy", vdom=vdom, params={"format": "policyid|status"}))
    st = Counter(p.get("status", "enable") for p in pols)
    return [{"Metric": "Firewall policies", "Value": len(pols)},
            {"Metric": "Enabled policies", "Value": st.get("enable", 0)}]


def c_ha(ctx, _):
    out = []
    try:
        rows = _list(ctx.client.monitor("system/ha-checksums"))
    except FortiAPIError:
        rows = []
    if not rows:
        return out
    primary = next((m for m in rows if m.get("is_root_primary") or m.get("is_manage_primary")
                    or m.get("is_root_master") or m.get("is_manage_master")), rows[0])
    ref = (primary.get("checksum") or {}).get("all", "")
    for m in rows:
        cs = (m.get("checksum") or {}).get("all", "")
        out.append({"Serial number": m.get("serial_no"),
                    "Role": "Primary" if m is primary else "Secondary",
                    "Config in sync": "Yes" if cs == ref else "No",
                    "Checksum": cs or DASH})
    return out


def ha_in_sync(rows: list[dict]) -> bool | None:
    if not rows:
        return None
    return all(r.get("Config in sync") == "Yes" for r in rows)


LICENSE_NAMES = {
    "forticare": "FortiCare support", "forticloud": "FortiCloud account", "security_rating": "Security Rating",
    "antivirus": "Antivirus", "mobile_malware": "Mobile malware", "ai_malware_detection": "AI malware detection",
    "ips": "IPS", "industrial_db": "Industrial (OT) signatures", "appctrl": "Application control",
    "genai_app": "GenAI application control", "internet_service_db": "Internet Service database",
    "device_os_id": "Device / OS identification", "botnet_ip": "Botnet IP", "botnet_domain": "Botnet domains",
    "data_leak_prevention": "Data Leak Prevention", "psirt_security_rating": "PSIRT security rating",
    "timezone_database": "Time zone database", "outbreak_security_rating": "Outbreak security rating",
    "icdb": "Industrial control database", "inline_casb": "Inline CASB",
    "local_in_virtual_patching": "Virtual patching (local-in)", "geoip_db": "GeoIP database",
    "trusted_cert_db": "Trusted CA certificates", "malicious_urls": "Malicious URLs",
    "blacklisted_certificates": "Blocked certificates", "firmware_updates": "Firmware updates",
    "web_filtering": "Web filtering", "outbreak_prevention": "Outbreak prevention", "antispam": "Antispam",
    "sdwan_network_monitor": "SD-WAN network monitor", "iot_detection": "IoT detection",
    "ot_detection": "OT detection", "forticloud_logging": "FortiCloud logging",
    "forticloud_sandbox": "FortiCloud sandbox", "fortianalyzer_cloud": "FortiAnalyzer Cloud",
    "fortianalyzer_cloud_premium": "FortiAnalyzer Cloud Premium", "fortimanager_cloud": "FortiManager Cloud",
    "fortisandbox_cloud": "FortiSandbox Cloud", "fortiguard_ai_based_sandbox": "FortiGuard AI-based sandbox",
    "forticonverter": "FortiConverter", "sdwan_overlay_aas": "SD-WAN overlay as a Service",
    "fortisase_private_access": "FortiSASE private access", "fortisase_lan_extension": "FortiSASE LAN extension",
    "sovereign_sase": "Sovereign SASE", "fortiems_cloud": "FortiClient EMS Cloud",
    "fortimanager_cloud_alci": "FortiManager Cloud (account)", "fortisandbox_cloud_alci": "FortiSandbox Cloud (account)",
    "vdom": "VDOM licence", "vm": "VM licence", "sms": "SMS messaging",
}
LICENSE_STATUS = {"licensed": "Licensed", "registered": "Registered", "no_license": "Not licensed",
                  "free_license": "Free tier", "cloud_na": "Not available", "vm_valid": "Valid",
                  "expired": "Expired", "unregistered": "Not registered", "pending": "Pending",
                  "invalid": "Invalid", "valid": "Valid"}
LICENSE_ACTIVE = {"Licensed", "Registered", "Valid", "Free tier"}


def _date(epoch) -> str:
    try:
        n = int(epoch)
    except (TypeError, ValueError):
        return DASH
    if n <= 0:
        return DASH
    import datetime as _dt
    return _dt.datetime.fromtimestamp(n, _dt.timezone.utc).strftime("%d-%m-%Y")


def c_license(ctx, _):
    lic = ctx.client.monitor("license/status") or {}
    out = []
    for svc, d in lic.items():
        if isinstance(d, dict) and "status" in d:
            st = str(d.get("status") or "")
            out.append({"Service": LICENSE_NAMES.get(svc, title(svc)),
                        "Status": LICENSE_STATUS.get(st.lower(), title(st)),
                        "Expires": _date(d.get("expires"))})
    return out


# ---------------------------------------------------------------- FortiGate-VM series
VM_TIERS = (1, 2, 4, 8, 16, 32)
VM_SERIAL = re.compile(r"^F[A-Z]VM([0-9]{2}|[A-Z]{2})", re.I)
VM_LICENCE_KIND = {"UL": "Unlimited vCPU", "EV": "Evaluation", "SL": "Subscription (VM-S)"}


def is_vm(model: str = "", serial: str = "") -> bool:
    return "VM" in str(model).upper() or bool(VM_SERIAL.match(str(serial or "")))


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().lower()
    if s in ("unlimited", "-1"):
        return float("inf")
    try:
        return float(s)
    except ValueError:
        return None


def _find(d: dict, what: str, kinds) -> Any:
    """First numeric value whose key mentions `what` (cpu/mem) and one of `kinds` (max/allowed/used...)."""
    for k, v in (d or {}).items():
        kl = str(k).lower().replace("-", "_")
        if what in kl and any(x in kl for x in kinds):
            n = _num(v)
            if n is not None:
                return n
    return None


def tier_for_cpus(n) -> str:
    if n is None or n <= 0:
        return ""
    if n == float("inf") or n > VM_TIERS[-1]:
        return "VMUL"
    for t in VM_TIERS:
        if n <= t:
            return f"VM{t:02d}"
    return ""


def vm_series(model: str, serial: str, lic_vm: dict | None = None, vm_info: dict | None = None) -> dict | None:
    """FortiGate-VM licence series (VM01, VM02, VM04 ... VMUL) from the serial number, or, for
    subscription/FortiFlex/evaluation licences whose serial carries no size, from the licensed vCPUs."""
    if not is_vm(model, serial):
        return None
    lic_vm = lic_vm or {}
    vm_info = vm_info or {}
    src = {**lic_vm, **vm_info}
    allowed = _find(src, "cpu", ("max", "allow", "limit", "licen", "total"))
    used = _find(src, "cpu", ("used", "count", "current", "num"))
    mem_allowed = _find(src, "mem", ("max", "allow", "limit", "licen", "total"))
    mem_used = _find(src, "mem", ("used", "current"))
    st = str(lic_vm.get("status") or "")
    out = {"series": "", "basis": "", "licence": "", "cpu_allowed": allowed, "cpu_used": used,
           "mem_allowed": mem_allowed, "mem_used": mem_used,
           "status": LICENSE_STATUS.get(st.lower(), title(st)) if st else ""}
    for k in ("license_model", "license_type", "type"):
        if src.get(k) and isinstance(src[k], str):
            out["licence"] = title(src[k])
            break
    m = VM_SERIAL.match(str(serial or ""))
    code = m.group(1).upper() if m else ""
    if code.isdigit():
        out.update(series=f"VM{code}", basis="serial number")
        out["licence"] = out["licence"] or "Perpetual"
    elif code == "UL":
        out.update(series="VMUL", basis="serial number")
    if code in VM_LICENCE_KIND:
        out["licence"] = VM_LICENCE_KIND[code]
    if not out["series"]:
        t = tier_for_cpus(allowed)
        if t:
            out.update(series=t, basis=("unlimited" if allowed == float("inf") else f"{int(allowed)}")
                       + " licensed vCPUs")
    for k in ("cpu_allowed", "mem_allowed"):
        if out[k] == float("inf"):
            out[k] = "unlimited"
        elif isinstance(out[k], float) and out[k].is_integer():
            out[k] = int(out[k])
    for k in ("cpu_used", "mem_used"):
        if isinstance(out[k], float) and out[k].is_integer():
            out[k] = int(out[k])
    return out


def c_resources(ctx, _):
    res = ctx.client.monitor("system/resource/usage", params={"interval": "1-min"}) or {}
    names = {"cpu": ("CPU usage", pct), "mem": ("Memory usage", pct), "disk": ("Disk usage", pct),
             "session": ("IPv4 sessions", lambda x: f"{int(x):,}"), "session6": ("IPv6 sessions", lambda x: f"{int(x):,}"),
             "setuprate": ("Session setup rate", lambda x: f"{int(x):,}/s")}
    out = []
    for k, (label, fmt) in names.items():
        v = res.get(k)
        if isinstance(v, list) and v:
            v = v[0].get("current")
        if v is not None:
            try:
                out.append({"Metric": label, "Value": fmt(v)})
            except (TypeError, ValueError):
                pass
    return out


def c_dhcp(ctx, vdom):
    return [{"IP address": l.get("ip"), "MAC": l.get("mac"), "Interface": l.get("interface"),
             "Hostname": l.get("hostname") or DASH}
            for l in _list(ctx.client.monitor("system/dhcp", vdom=vdom))]


def _cli(cmd: str, global_ctx: bool):
    def f(ctx: Ctx, vdom):
        if ctx.ssh is None:
            return ""
        return ctx.ssh.run(cmd, vdom=vdom, global_ctx=global_ctx, vdom_mode=ctx.vdom_mode, timeout=900)
    return f


# ---------------------------------------------------------------- summaries
def s_updown(field_="Status", up=("Up",)):
    def s(rows):
        c = Counter("up" if r.get(field_) in up else "down" for r in rows)
        return f"{c['up']} up · {c['down']} down" if rows else "none"
    return s


SINGULAR = {"entries": "entry", "neighbours": "neighbour", "leases": "lease", "routes": "route",
            "tunnels": "tunnel", "users": "user"}


def s_count(noun):
    return lambda rows: f"{len(rows):,} {SINGULAR.get(noun, noun) if len(rows) == 1 else noun}"


def s_routes(rows):
    if not rows:
        return "no routes"
    c = count_by(rows, "Type")
    return f"{len(rows):,} route{'s' if len(rows) != 1 else ''} (" + ", ".join(f"{k} {v:,}" for k, v in c.most_common(4)) + ")"


def s_bgp(rows):
    if not rows:
        return "no neighbours"
    est = sum(1 for r in rows if r.get("State") == "Established")
    rx = sum(int(r.get("Received prefixes") or 0) for r in rows)
    return f"{est}/{len(rows)} established · {rx:,} prefixes received"


def s_ha(rows):
    if not rows:
        return "Standalone (no cluster)"
    return f"{len(rows)} members · " + ("in sync" if ha_in_sync(rows) else "OUT OF SYNC")


def s_sdwan(rows):
    if not rows:
        return "not configured"
    state = rows[0].get("SD-WAN", "")
    zones = len({r["Zone"] for r in rows})
    mem = [r for r in rows if r["Member"] != DASH]
    up = sum(1 for r in mem if r["Link"] == "Up")
    return f"{'SD-WAN ' + state.lower()} · {zones} zone{'s' if zones != 1 else ''} · " + \
        (f"{up}/{len(mem)} members up" if mem else "no members")


def s_logs(rows):
    if not rows:
        return "no events"
    bad = sum(1 for r in rows if r.get("Level") in ("Emergency", "Alert", "Critical", "Error"))
    warn = sum(1 for r in rows if r.get("Level") == "Warning")
    return f"{len(rows):,} events · {bad} error/alert · {warn} warning"


def s_metrics(rows):
    return " · ".join(f"{r['Metric']}: {r['Value']}" for r in rows[:4])


def s_license(rows):
    ok = sum(1 for r in rows if r.get("Status") in LICENSE_ACTIVE)
    bad = sum(1 for r in rows if r.get("Status") in ("Expired", "Invalid"))
    text = f"{ok} active · {len(rows) - ok - bad} not licensed"
    return text + (f" · {bad} expired" if bad else "")


def s_system(rows):
    d = {r["Item"]: r["Value"] for r in rows}
    return f"{d.get('Hostname', '')} · {d.get('Model', '')} · {d.get('FortiOS version', '')}"


# ---------------------------------------------------------------- severities
def sev_interface(ct, key, f, b, a):
    if ct == "removed" or (f == "Status" and b == "Up" and a != "Up") or f == "IP address":
        return CRIT
    if f == "Admin access":
        added = set(str(a).split(", ")) - set(str(b).split(", "))
        return CRIT if added & {"HTTP", "TELNET", "SNMP"} else WARN
    return WARN if ct != "added" else INFO


def sev_routes(ct, key, f, b, a):
    if ct == "removed":
        return CRIT if "0.0.0.0/0" in key or "::/0" in key else WARN
    return INFO


def sev_state_up(up_values: set[str]):
    def s(ct, key, f, b, a):
        if ct == "removed":
            return CRIT
        if f in ("State", "Status") and str(b).lower() in up_values and str(a).lower() not in up_values:
            return CRIT
        if f in ("Received prefixes", "Advertised prefixes", "Best paths") and isinstance(b, int) and isinstance(a, int):
            return WARN if b and a < b * 0.9 else INFO
        return INFO if ct == "added" else WARN
    return s


def sev_log(ct, key, f, b, a):
    if ct == "added" and isinstance(a, dict) and a.get("Level") in ("Emergency", "Alert", "Critical", "Error"):
        return WARN
    return INFO


def sev_ha(ct, key, f, b, a):
    if (f == "Config in sync" and a != "Yes") or ct == "removed":
        return CRIT
    return INFO


def sev_license(ct, key, f, b, a):
    return WARN if ct == "removed" or f == "Status" else INFO


def sev_info(*_):
    return INFO


def sev_warn_removed(ct, *_):
    return WARN if ct == "removed" else INFO


CHECKS: list[Check] = [
    Check("system", "System status", "global", c_system, ("Item",), ("Value",), severity=sev_info, summary=s_system),
    Check("ha", "HA cluster sync", "global", c_ha, ("Serial number",), ("Config in sync", "Role"), severity=sev_ha,
          summary=s_ha),
    Check("license", "FortiGuard licences", "global", c_license, ("Service",), ("Status",), severity=sev_license,
          summary=s_license),
    Check("resources", "CPU, memory and sessions", "global", c_resources, ("Metric",), (), severity=sev_info,
          summary=s_metrics, description="Collected for context; not diffed (values are volatile)."),
    Check("interfaces", "Interfaces", "vdom", c_interfaces, ("Interface",),
          ("Status", "Admin status", "IP address", "Admin access", "Speed", "Duplex"), severity=sev_interface,
          summary=s_updown()),
    Check("sdwan_zones", "SD-WAN zones", "vdom", c_sdwan_zones, ("Zone", "Member"),
          ("Member state", "Link", "Gateway", "SD-WAN"), severity=sev_state_up({"up", "enabled"}), summary=s_sdwan),
    Check("arp", "ARP table", "vdom", c_arp, ("IP address", "Interface"), ("MAC",), severity=sev_warn_removed,
          summary=s_count("entries")),
    Check("routes4", "IPv4 routing table", "vdom", c_routes4, ("Type", "Destination", "Gateway", "Interface"),
          ("Distance", "Metric"), severity=sev_routes, summary=s_routes),
    Check("routes6", "IPv6 routing table", "vdom", c_routes6, ("Type", "Destination", "Gateway", "Interface"),
          ("Distance", "Metric"), severity=sev_routes, summary=s_routes),
    Check("bgp", "BGP neighbours", "vdom", c_bgp, ("Neighbour",),
          ("State", "Remote AS", "Received prefixes", "Best paths"), severity=sev_state_up({"established"}),
          summary=s_bgp),
    Check("bgp_tx", "BGP advertised prefixes (SSH)", "vdom", c_bgp_tx, ("Neighbour",), ("Advertised prefixes",),
          needs_ssh=True, severity=sev_state_up(set()), summary=s_count("neighbours")),
    Check("ospf", "OSPF neighbours", "vdom", c_ospf, ("Neighbour",), ("State", "Router ID"),
          severity=sev_state_up({"full", "2-way"}), summary=lambda r: f"{sum(1 for x in r if x['State'] == 'Full')}/{len(r)} full" if r else "no neighbours"),
    Check("ipsec", "IPsec VPN tunnels", "vdom", c_ipsec, ("Tunnel", "Phase 2"), ("Status", "Remote gateway"),
          severity=sev_state_up({"up"}), summary=s_updown()),
    Check("sslvpn", "SSL VPN", "vdom", c_sslvpn, ("Metric",), (), severity=sev_info, summary=s_metrics),
    Check("sdwan", "SD-WAN health checks", "vdom", c_sdwan, ("Health check", "Member"), ("Status",),
          severity=sev_state_up({"alive", "up"}), summary=s_updown(up=("Alive", "Up"))),
    Check("policy", "Firewall policies", "vdom", c_policy, ("Metric",), ("Value",), severity=sev_warn_removed,
          summary=s_metrics),
    Check("logs", "Event logs", "vdom", c_logs, ("Time", "Event", "Message"), (), severity=sev_log,
          summary=s_logs, description="Recent system, VPN, routing, HA, SD-WAN and user events."),
    Check("dhcp", "DHCP leases", "vdom", c_dhcp, ("IP address", "Interface"), ("MAC",), severity=sev_info,
          default=False, summary=s_count("leases")),
    # CLI (SSH) text checks
    Check("cli_ha_status", "CLI: get system ha status", "global", _cli("get system ha status", True),
          kind="text", needs_ssh=True),
    Check("cli_ha_checksum", "CLI: diagnose sys ha checksum cluster", "global",
          _cli("diagnose sys ha checksum cluster", True), kind="text", needs_ssh=True),
    Check("cli_perf", "CLI: get system performance status", "global",
          _cli("get system performance status", True), kind="text", needs_ssh=True),
    Check("cli_bgp_summary", "CLI: get router info bgp summary", "vdom",
          _cli("get router info bgp summary", False), kind="text", needs_ssh=True),
    Check("cli_crashlog", "CLI: diagnose debug crashlog read", "global",
          _cli("diagnose debug crashlog read", True), kind="text", needs_ssh=True, default=False),
    Check("tac_report", "TAC report (execute tac report)", "global",
          _cli("execute tac report", True), kind="text", needs_ssh=True, default=False,
          description="Full TAC dump over SSH. Takes several minutes and produces a large file."),
]

CHECKS_BY_ID = {c.id: c for c in CHECKS}


def record_key(check: Check, rec: dict) -> str:
    return " | ".join(str(rec.get(k, "")) for k in check.key)


def summarise(check: Check, data) -> str:
    if check.kind == "text":
        n = len((data or "").splitlines())
        return f"{n:,} lines" if n else "no output"
    try:
        return check.summary(data) if check.summary else f"{len(data):,} rows"
    except Exception:  # noqa: BLE001
        return f"{len(data):,} rows"
