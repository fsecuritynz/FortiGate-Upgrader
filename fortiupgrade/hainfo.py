"""Management addresses and HA layout read from a FortiGate.

* Management addresses: every interface IP with admin access, plus the per-unit ``management-ip``
  (in an FGCP cluster the interface ``ip`` is shared and follows the primary, while
  ``management-ip`` is that one unit's own address). Used to label a device's addresses and to
  propose default service probes: ping only where ``ping`` is allowed, HTTPS only where ``https``
  is allowed, on the admin HTTPS port (``admin-sport``).
* HA layout: mode, group, this unit's priority, heartbeat and monitored interfaces, and every
  cluster member (hostname, serial, priority, role) from the HA monitor endpoints.
"""
from __future__ import annotations

import re

from .client import FortiAPIError

ADMIN_ACCESS = {"https", "ssh", "http", "fgfm", "telnet", "snmp"}


def _list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        return [x]
    return []


def _addr(v) -> str:
    """'10.8.8.21 255.255.255.0' / '10.8.8.21/24' -> '10.8.8.21'; empty or 0.0.0.0 -> ''."""
    s = str(v or "").strip().split()[0] if str(v or "").strip() else ""
    s = s.split("/")[0]
    return "" if s in ("", "0.0.0.0") else s


def _names(v) -> list[str]:
    """hbdev / monitor come back as '"port2" 50 "port3" 0' or a list of {interface-name: ...}."""
    if isinstance(v, list):
        return [str(x.get("interface-name") or x.get("name") or x) for x in v if x]
    s = str(v or "")
    q = re.findall(r'"([^"]+)"', s)
    if q:
        return q
    return [t for t in s.split() if t and not t.isdigit()]


def _hb_priorities(v) -> dict:
    s = str(v or "")
    return {m.group(1): int(m.group(2)) for m in re.finditer(r'"([^"]+)"\s+(\d+)', s)}


def mgmt_info(c) -> dict:
    try:
        g = c.cmdb("system/global") or {}
    except FortiAPIError:
        g = {}
    try:
        rows = _list(c.cmdb("system/interface"))
    except FortiAPIError:
        rows = []
    ifs = []
    for r in rows:
        access = sorted(set(str(r.get("allowaccess") or "").split()))
        ip, mip = _addr(r.get("ip")), _addr(r.get("management-ip"))
        if not ip and not mip:
            continue
        ifs.append({"name": r.get("name"), "ip": ip, "management_ip": mip, "access": access,
                    "status": r.get("status") or "up", "type": r.get("type") or "",
                    "admin": bool(ADMIN_ACCESS & set(access))})
    return {"hostname": g.get("hostname") or "", "admin_sport": int(g.get("admin-sport") or 443),
            "admin_port": int(g.get("admin-port") or 80), "admin_ssh_port": int(g.get("admin-ssh-port") or 22),
            "interfaces": ifs}


def ha_info(c) -> dict:
    try:
        h = c.cmdb("system/ha") or {}
    except FortiAPIError:
        h = {}
    out = {"mode": h.get("mode") or "standalone", "group_name": h.get("group-name") or "",
           "group_id": h.get("group-id"), "priority": h.get("priority"), "override": h.get("override"),
           "hbdev": _names(h.get("hbdev")), "hb_priorities": _hb_priorities(h.get("hbdev")),
           "monitor": _names(h.get("monitor")), "session_pickup": h.get("session-pickup"),
           "ha_mgmt_status": h.get("ha-mgmt-status"),
           "ha_mgmt_interfaces": [{"interface": x.get("interface"), "gateway": x.get("gateway")}
                                  for x in _list(h.get("ha-mgmt-interfaces"))],
           "peers": []}
    if out["mode"] == "standalone":
        return out
    peers: dict[str, dict] = {}
    try:
        for p in _list(c.monitor("system/ha-peer")):
            sn = p.get("serial_no") or p.get("serial")
            if sn:
                peers[sn] = {"serial": sn, "hostname": p.get("hostname") or "", "priority": p.get("priority"),
                             "primary": bool(p.get("primary") or p.get("is_root_primary") or p.get("master"))}
    except FortiAPIError:
        pass
    try:
        for i, p in enumerate(_list(c.monitor("system/ha-checksums"))):
            sn = p.get("serial_no")
            if not sn:
                continue
            e = peers.setdefault(sn, {"serial": sn, "hostname": "", "priority": None, "primary": False})
            if p.get("is_root_master") or p.get("is_root_primary") or p.get("is_manage_master"):
                e["primary"] = True
    except FortiAPIError:
        pass
    try:
        for p in _list(c.monitor("system/ha-statistics")):
            sn = p.get("serial_no")
            if sn and sn in peers:
                peers[sn].update(sessions=p.get("sessions"),
                                 cpu=p.get("cpu_usage"), mem=p.get("mem_usage"),
                                 hostname=peers[sn]["hostname"] or p.get("hostname") or "")
    except FortiAPIError:
        pass
    if peers and not any(p["primary"] for p in peers.values()):
        max(peers.values(), key=lambda p: p.get("priority") or 0)["primary"] = True
    out["peers"] = sorted(peers.values(), key=lambda p: (not p["primary"], -(p.get("priority") or 0)))
    return out


def address_role(host: str, mgmt: dict, ha_mode: str) -> dict:
    """What the address a device is saved with actually is."""
    for i in mgmt.get("interfaces") or []:
        if host and host == i["management_ip"]:
            return {"role": "unit", "interface": i["name"], "cluster_ip": i["ip"]}
        if host and host == i["ip"]:
            return {"role": "cluster" if ha_mode in ("a-p", "a-a") else "device", "interface": i["name"],
                    "unit_ip": i["management_ip"]}
    return {"role": "unknown"}


def unit_addresses(mgmt: dict, ha: bool, dedicated: list[str] | None = None) -> list[dict]:
    """The addresses that reach this one unit.

    HA: the per-interface ``management-ip`` and the IP of a dedicated HA management interface
    (``ha-mgmt-interfaces``) - never the shared interface IP, which follows whichever unit is primary.
    Standalone: every interface IP with admin access."""
    out = []
    ded = set(dedicated or [])
    for i in mgmt.get("interfaces") or []:
        if i.get("status") == "down":
            continue
        if ha:
            if i.get("management_ip"):
                out.append({**i, "addr": i["management_ip"], "kind": f"management-ip on {i['name']}"})
            if i["name"] in ded and i.get("ip"):
                out.append({**i, "addr": i["ip"], "kind": f"dedicated HA management interface {i['name']}"})
        elif i["admin"] and i.get("ip"):
            out.append({**i, "addr": i["ip"], "kind": i["name"]})
    return out


def suggest_probes(units: list[dict]) -> dict:
    """Default probes: the management address of every unit being upgraded.

    ``units``: one per physical unit - {"target", "hostname", "serial", "ha_mode", "mgmt": mgmt_info(),
    "dedicated": [ha-mgmt interface names], "host": saved address or None, "port": saved port}.
    For HA each unit gets its own probes, so the timeline shows each unit's availability separately."""
    probes, notes, seen, units_seen = [], [], set(), set()

    def add(kind, addr, label, why, port=None):
        key = (kind, addr, port)
        if key in seen:
            return
        seen.add(key)
        if kind == "ping":
            probes.append({"type": "ping", "target": addr, "interval_ms": 200, "name": f"Ping {label}",
                           "auto": True, "why": why})
        else:
            url = f"https://{addr}{'' if port in (None, 443) else f':{port}'}/"
            probes.append({"type": "http", "url": url, "interval_ms": 2000, "timeout_ms": 2000, "name": f"HTTPS {label}",
                           "auto": True, "why": why})

    for u in units:
        ukey = u.get("serial") or (u.get("target"), u.get("hostname"))
        if ukey in units_seen:
            continue
        units_seen.add(ukey)
        mg = u.get("mgmt") or {}
        sport = mg.get("admin_sport") or 443
        ha = u.get("ha_mode") in ("a-p", "a-a")
        name = u.get("hostname") or u.get("target")
        addrs = unit_addresses(mg, ha, u.get("dedicated"))
        if not addrs:
            notes.append(f"{name}: no {'per-unit management address (management-ip or dedicated HA management interface)' if ha else 'management address'} is configured, so it is not probed"
                         + (". The shared cluster address is not used: it follows the primary, so it can't show this unit's availability." if ha else ""))
            continue
        for a in addrs:
            label = f"{name} {a['addr']}"
            why = f"{name}'s {a['kind']}, which allows {', '.join(a['access']) or 'nothing'}"
            can_ping, can_https = "ping" in a["access"], "https" in a["access"]
            if can_ping:
                add("ping", a["addr"], label, why)
            if can_https:
                port = int(u.get("port") or sport) if a["addr"] == u.get("host") else sport
                add("http", a["addr"], label, why + (f"; admin HTTPS port {port}" if port != 443 else ""), port)
            if not can_ping and not can_https:
                notes.append(f"{label}: neither ping nor HTTPS is allowed on {a['name']}, so it is not probed")
            elif not can_ping:
                notes.append(f"{label}: ping is not allowed on {a['name']}, so only HTTPS is probed")
    return {"probes": probes, "notes": notes}


# ------------------------------------------------------------------ CLI parsing (peer units over SSH)
def parse_manage_list(text: str) -> list[dict]:
    """`execute ha manage ?` -> [{"index": 1, "serial": "FGVM04..."}]"""
    out = []
    for m in re.finditer(r"<(\d+)>\s+[^\n]*?\b(F[A-Z0-9]{2}[A-Z0-9-]{8,})", text):
        out.append({"index": int(m.group(1)), "serial": m.group(2)})
    return out


def parse_interfaces(show: str) -> list[dict]:
    """`show system interface` -> the same shape as mgmt_info()["interfaces"]."""
    ifs = []
    for m in re.finditer(r'edit "([^"]+)"(.*?)\n\s*next', show, re.S):
        body = m.group(2)
        get = lambda k: (re.search(rf"set {k} ([^\n]+)", body) or [None, ""])[1].strip()  # noqa: E731
        access = sorted(set(get("allowaccess").split()))
        ip, mip = _addr(get("ip")), _addr(get("management-ip"))
        if not ip and not mip:
            continue
        ifs.append({"name": m.group(1), "ip": ip, "management_ip": mip, "access": access,
                    "status": "down" if get("status") == "down" else "up", "type": get("type"),
                    "admin": bool(ADMIN_ACCESS & set(access))})
    return ifs
