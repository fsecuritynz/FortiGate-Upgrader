"""A small FortiGate REST API simulator for demos and tests. NOT a faithful emulator.

  python tools/mock_fortigate.py --port 8443 --version 7.2.10 --ha a-p --vdom
  token: "mocktoken"   scheme: http

After an upgrade request the device is "down" for --reboot-seconds and then reports
the new version. --break makes BGP neighbour 10.0.0.2 drop and port3 go down after
upgrade, so the post-check diff has something to show.
"""
from __future__ import annotations

import argparse
import time

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

TOKEN = "mocktoken"
CATALOG = ["7.0.15", "7.0.17", "7.2.8", "7.2.10", "7.2.11", "7.4.4", "7.4.7", "7.4.8", "7.6.3", "7.6.4"]
PATHS = [("7.0.15", "7.0.17"), ("7.0.17", "7.2.11"), ("7.2.8", "7.2.11"), ("7.2.10", "7.2.11"),
         ("7.2.10", "7.4.8"), ("7.2.11", "7.4.8"), ("7.4.4", "7.4.8"), ("7.4.7", "7.4.8"), ("7.4.8", "7.6.4"),
         ("7.4.8", "7.6.3")]

CONFIG = """#config-version=FGT60F-{ver}-FW-build{build}-240101:opmode=0:vdom={vd}:user=api
#conf_file_ver=1234567890
#buildno={build}
#global_vdom=1
config system global
    set hostname "{name}"
    set admin-https-ssl-versions tlsv1-1 tlsv1-2 tlsv1-3
end
config system ha
    set group-name "lab"
    set mode {ha}
    set password ENC abcdef{nonce}
end
{fmgblock}config system interface
    edit "port1"
        set ip 192.0.2.1 255.255.255.0
        set allowaccess ping https ssh
    next
    edit "port3"
        set ip 198.51.100.1 255.255.255.0
    next
end
config vpn ssl settings
    set tunnel-ip-pools "SSLVPN_TUNNEL_ADDR1"
    set source-interface "port1"
end
config router bgp
    set as 65001
    config neighbor
        edit "10.0.0.2"
            set remote-as 65002
        next
    end
end
config vpn ipsec phase1-interface
    edit "to-dc"
        set interface "port1"
        set remote-gw 203.0.113.9
        set psksecret ENC xyz{nonce}
    next
end
"""


def build(ver):
    return 1000 + CATALOG.index(ver) * 37 if ver in CATALOG else 999


def make_app(state: dict) -> FastAPI:
    app = FastAPI()

    def up():
        if state.get("down_until", 0) > time.time():
            raise HTTPException(503, "rebooting")
        if state.get("pending"):
            state["version"] = state.pop("pending")
            state["upgraded"] = True

    @app.middleware("http")
    async def mw(request: Request, call_next):
        try:  # a rebooting unit answers nothing, authenticated or not
            up()
        except HTTPException:
            return JSONResponse({"detail": "rebooting"}, 503)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return JSONResponse({"status": "error", "http_status": 401}, 401)
        return await call_next(request)

    def wrap(results, vdom="root"):
        v = state["version"]
        return {"http_method": "GET", "results": results, "vdom": vdom, "path": "x", "name": "x",
                "status": "success", "serial": state["serial"], "version": f"v{v}", "build": build(v)}

    broken = lambda: state.get("break") and state.get("upgraded")  # noqa: E731

    @app.get("/api/v2/monitor/system/status")
    def status():
        mdl = state.get("model") or "60F"
        return wrap({"model_name": "FortiGate", "model_number": mdl, "model": f"FGT{mdl}", "hostname": state["name"],
                     "log_disk_status": "available"})

    @app.get("/api/v2/cmdb/system/global")
    def glob():
        return wrap({"hostname": state["name"], "vdom-mode": "multi-vdom" if state["vdom"] else "no-vdom",
                     "admin-sport": state.get("admin_sport", 443)})

    @app.get("/api/v2/monitor/system/ha-peer")
    def ha_peer():
        if state["ha_cfg"]["mode"] == "standalone":
            return wrap([])
        return wrap([{"serial_no": state["serial"], "hostname": state["name"], "priority": state["ha_cfg"].get("priority", 200),
                      "vcluster_id": 0, "primary": True},
                     {"serial_no": state["serial"][:-1] + "2", "hostname": state["name"] + "-b", "priority": 100,
                      "vcluster_id": 0, "primary": False}])

    @app.get("/api/v2/cmdb/system/vdom")
    def vdoms():
        return wrap([{"name": "root"}, {"name": "cust1"}] if state["vdom"] else [{"name": "root"}])

    @app.get("/api/v2/cmdb/system/ha")
    def ha():
        return wrap(state["ha_cfg"])

    @app.put("/api/v2/cmdb/system/ha")
    def ha_put(body: dict = Body(...)):
        state["ha_cfg"].update(body)
        state.setdefault("ha_puts", []).append(body)
        return wrap({})

    @app.get("/api/v2/cmdb/system/standalone-cluster")
    def sc():
        return wrap({"standalone-group-id": 1 if state["fgsp"] else 0, "cluster-peer": []})

    @app.get("/api/v2/cmdb/system/central-management")
    def cm():
        if state.get("fmg"):
            return wrap({"mode": "normal", "type": "fortimanager", "fmg": f'"{state["fmg"]}"'})
        return wrap({"mode": "normal", "type": "fortiguard", "fmg": ""})

    @app.get("/api/v2/monitor/system/central-management/status")
    def cm_status():
        return wrap({"status": "up" if state.get("fmg") else "down"})

    def phys():
        """Physical interfaces as FortiOS lists them for each model (switch members excluded)."""
        model = str(state.get("model") or "").upper()
        if not model:  # legacy generic layout used by the tests
            return ["port1", "port2", "port3", "port4", "x1", "x2", "mgmt", "ha"]
        if model.startswith("VM"):
            return ["port1", "port2", "port3", "port4"]
        if model in ("60F", "61F", "70F", "71F"):
            return ["wan1", "wan2", "dmz", "a", "b", "modem"]          # internal1-5 sit in the "internal" switch
        if model in ("90G", "91G"):
            return ["x1", "x2", "port1", "port2", "port3", "port4", "port5", "port6", "a", "b",
                    "modem", "npu0_vlink0", "npu0_vlink1"]
        if model in ("100F", "101F"):
            return (["mgmt", "dmz", "ha1", "ha2", "wan1", "wan2"] + [f"port{i}" for i in range(1, 21)] +
                    ["x1", "x2", "npu0_vlink0", "npu0_vlink1"])
        return ["mgmt", "ha"] + [f"port{i}" for i in range(1, 25)] + [f"x{i}" for i in range(1, 5)] + \
            ["npu0_vlink0", "npu0_vlink1"]

    def switch_members():
        model = str(state.get("model") or "").upper()
        if model in ("60F", "61F", "70F", "71F"):
            return {"internal": [f"internal{i}" for i in range(1, 6)]}
        return {}

    def iface_state():
        up = {"port1": True, "port2": True, "port3": not broken(), "wan1": True, "mgmt": True, "ha1": True, "x1": True,
              "x2": True, "a": True, "internal": True, "npu0_vlink0": True, "npu0_vlink1": True}
        ips = {"port1": ("192.0.2.1", 24), "port3": ("198.51.100.1", 24), "wan1": ("203.0.113.2", 29),
               "mgmt": ("10.10.0.11", 24)}
        out = {}
        names = phys() + list(switch_members())
        for n, name in enumerate(names):
            ip, mask = ips.get(name, ("0.0.0.0", 0))
            sp = 10000 if name.startswith("x") else 1000
            out[name] = {"name": name, "ip": ip, "mask": mask, "link": up.get(name, False), "speed": sp if up.get(name) else 0,
                         "duplex": 1 if up.get(name) else 0, "mac": f"00:09:0f:00:00:{n + 1:02x}"}
        return out

    @app.get("/api/v2/monitor/system/interface")
    def ifaces(vdom: str = "root"):
        d = iface_state()
        if vdom == "cust1":
            d = {"vlan100": {"name": "vlan100", "ip": "10.100.0.1", "mask": 24, "link": True, "speed": 1000, "duplex": 1}}
        return wrap(d, vdom)

    @app.get("/api/v2/cmdb/system/interface")
    def ifcfg():
        access = {"port1": "ping https ssh", "mgmt": "ping https ssh fgfm", "wan1": "ping", "port3": "ping"}
        roles = {"wan1": "wan", "port1": "lan", "port3": "lan", "mgmt": "undefined"}
        rows = [{"name": n, "type": "physical", "vdom": "root", "allowaccess": access.get(n, ""), "role": roles.get(n, "undefined"),
                 "status": "up", "alias": {"wan1": "ISP-A", "port1": "LAN"}.get(n, ""),
                 "ip": f"{d['ip']} 255.255.255.0"} for n, d in iface_state().items()]
        for r in rows:  # per-test overrides, e.g. an HA unit's own management-ip on port1
            r.update((state.get("if_extra") or {}).get(r["name"], {}))
        for sw, mem in switch_members().items():
            for r in rows:
                if r["name"] == sw:
                    r["type"] = "hard-switch"
        rows.append({"name": "vlan100", "type": "vlan", "vdom": "cust1", "allowaccess": "ping", "role": "lan", "status": "up"})
        rows.append({"name": "to-dc", "type": "tunnel", "vdom": "root", "allowaccess": "ping", "role": "undefined", "status": "up"})
        return wrap(rows)

    @app.get("/api/v2/cmdb/system/virtual-switch")
    def vsw():
        return wrap([{"name": sw, "port": [{"name": m} for m in mem]} for sw, mem in switch_members().items()])

    @app.get("/api/v2/monitor/system/available-interfaces")
    def avail():
        out = []
        for sw, mem in switch_members().items():
            for i, m in enumerate(mem):
                out.append({"name": m, "link": i in (0, 2), "speed": 1000 if i in (0, 2) else 0})
        st = iface_state()
        for n in ("x1", "x2"):
            if n in st:
                out.append({"name": n, "media": "copper" if n == "x1" else "sfp", "link": st[n]["link"]})
        return wrap(out)

    @app.get("/api/v2/cmdb/system/sdwan")
    def sdwan_cfg(vdom: str = "root"):
        return wrap({"status": "enable", "zone": [{"name": "virtual-wan-link"}, {"name": "overlay"}],
                     "members": [{"seq-num": 1, "interface": "wan1", "zone": "virtual-wan-link", "gateway": "203.0.113.1",
                                  "cost": 0, "priority": 1, "status": "enable"},
                                 {"seq-num": 2, "interface": "port3", "zone": "virtual-wan-link", "gateway": "198.51.100.254",
                                  "cost": 10, "priority": 1, "status": "enable"},
                                 {"seq-num": 3, "interface": "to-dc", "zone": "overlay", "gateway": "",
                                  "cost": 0, "priority": 1, "status": "enable"}]}, vdom)

    @app.get("/api/v2/monitor/virtual-wan/members")
    def sdwan_members(vdom: str = "root"):
        return wrap({"wan1": {"interface": "wan1", "link_status": "up"},
                     "port3": {"interface": "port3", "link_status": "down" if broken() else "up"},
                     "to-dc": {"interface": "to-dc", "link_status": "up"}}, vdom)

    @app.get("/api/v2/log/{dev}/event/{sub}")
    def logs(dev: str, sub: str, vdom: str = "root", rows: int = 100):
        if dev != "memory":
            raise HTTPException(404)
        base = [
            ("system", "information", "Admin login successful", "Administrator admin logged in successfully from https(10.0.0.10)"),
            ("vpn", "notice", "IPsec phase 1 status changed", "IPsec phase 1 to-dc up"),
            ("router", "notice", "BGP neighbor status changed", "BGP: %BGP-5-ADJCHANGE: neighbor 10.0.0.2 Up"),
            ("system", "warning", "Disk log full", "Memory log usage is over 75%"),
        ] + state.get("extra_events", [])
        t0 = state.setdefault("t0", time.time() - 3600)
        out = []
        for i, (subtype, lvl, desc, msg) in enumerate(base):
            ts = t0 + i * 600 if i < 4 else time.time() - 60 + i
            out.append({"date": time.strftime("%Y-%m-%d", time.localtime(ts)), "time": time.strftime("%H:%M:%S", time.localtime(ts)),
                        "eventtime": int(ts * 1e9), "logid": f"01000{i:05d}", "type": "event", "subtype": subtype,
                        "level": lvl, "logdesc": desc, "msg": msg, "user": "admin" if "Admin" in desc else None})
        return {"results": list(reversed(out))[:rows], "total_lines": len(out), "status": "success"}

    @app.get("/api/v2/monitor/network/arp")
    def arp(vdom: str = "root"):
        return wrap([{"ip": "192.0.2.10", "mac": "aa:bb:cc:00:00:10", "interface": "port1"},
                     {"ip": "198.51.100.20", "mac": "aa:bb:cc:00:00:20", "interface": "port3"}][: 1 if broken() else 2], vdom)

    @app.get("/api/v2/monitor/router/ipv4")
    def r4(vdom: str = "root"):
        rs = [{"type": "static", "ip_mask": "0.0.0.0/0", "gateway": "192.0.2.254", "interface": "port1", "distance": 10, "metric": 0},
              {"type": "connect", "ip_mask": "192.0.2.0/24", "gateway": "0.0.0.0", "interface": "port1", "distance": 0, "metric": 0}]
        if not broken():
            rs += [{"type": "bgp", "ip_mask": f"172.16.{i}.0/24", "gateway": "10.0.0.2", "interface": "port3", "distance": 20, "metric": 0} for i in range(5)]
        return wrap(rs, vdom)

    @app.get("/api/v2/monitor/router/ipv6")
    def r6(vdom: str = "root"):
        return wrap([], vdom)

    @app.get("/api/v2/monitor/router/bgp/neighbors")
    def bgp(vdom: str = "root"):
        return wrap([{"neighbor_ip": "10.0.0.2", "remote_as": 65002, "admin_status": True,
                      "state": "Idle" if broken() else "Established"}], vdom)

    @app.get("/api/v2/monitor/router/bgp/paths")
    def paths(vdom: str = "root"):
        return wrap([] if broken() else [{"nlri_prefix": f"172.16.{i}.0", "learned_from": "10.0.0.2", "is_best": True} for i in range(5)], vdom)

    @app.get("/api/v2/monitor/router/ospf/neighbors")
    def ospf(vdom: str = "root"):
        return wrap([], vdom)

    @app.get("/api/v2/monitor/vpn/ipsec")
    def ipsec(vdom: str = "root"):
        return wrap([{"name": "to-dc", "rgwy": "203.0.113.9", "proxyid": [{"p2name": "to-dc", "status": "up"}]}], vdom)

    @app.get("/api/v2/monitor/vpn/ssl")
    def ssl(vdom: str = "root"):
        return wrap([], vdom)

    @app.get("/api/v2/monitor/virtual-wan/health-check")
    def sdwan(vdom: str = "root"):
        return wrap({"ping-google": {"port1": {"status": "alive", "latency": 12.3}}}, vdom)

    @app.get("/api/v2/cmdb/firewall/policy")
    def pol(vdom: str = "root"):
        return wrap([{"policyid": i, "status": "enable"} for i in range(1, 12)], vdom)

    @app.get("/api/v2/monitor/system/ha-checksums")
    def hacs():
        if state["ha_cfg"]["mode"] == "standalone":
            return wrap([])
        return wrap([{"serial_no": state["serial"], "is_root_master": 1, "checksum": {"all": "abc", "global": "g1"}},
                     {"serial_no": state["serial"][:-1] + "2", "is_root_master": 0, "checksum": {"all": "abc", "global": "g1"}}])

    @app.get("/api/v2/monitor/license/status")
    def lic():
        exp = int(time.time()) + 200 * 86400
        return wrap({"forticare": {"status": "registered"}, "antivirus": {"status": "licensed", "expires": exp},
                     "ips": {"status": "licensed", "expires": exp}, "web_filtering": {"status": "licensed", "expires": exp},
                     "appctrl": {"status": "licensed", "expires": exp}, "sdwan_network_monitor": {"status": "no_license"},
                     "forticloud_logging": {"status": "free_license"},
                     **({"vm": {"status": "vm_valid", "type": "subscription"}}
                        if str(state.get("model") or "").upper().startswith("VM") else {})})

    @app.get("/api/v2/monitor/system/vm-information")
    def vminfo():
        if not str(state.get("model") or "").upper().startswith("VM"):
            raise HTTPException(404)
        return wrap({"cpu_used": 2, "cpu_max": state.get("vm_cpus", 4), "mem_used": 3946, "mem_max": "unlimited"})

    @app.get("/api/v2/monitor/system/resource/usage")
    def res():
        return wrap({"cpu": [{"current": 7}], "mem": [{"current": 46}], "session": [{"current": 1234}]})

    @app.get("/api/v2/monitor/system/dhcp")
    def dhcp(vdom: str = "root"):
        return wrap([], vdom)

    @app.get("/api/v2/monitor/system/firmware")
    def fw():
        v = state["version"]
        maj, mi, pa = map(int, v.split("."))
        return wrap({"current": {"version": f"v{v}", "major": maj, "minor": mi, "patch": pa, "build": build(v)},
                     "available": [{"id": f"07{c.replace('.', '')}F{build(c)}", "version": f"v{c}",
                                    "major": int(c.split('.')[0]), "minor": int(c.split('.')[1]),
                                    "patch": int(c.split('.')[2]), "build": build(c), "maturity": "M"}
                                   for c in CATALOG if tuple(map(int, c.split("."))) > (maj, mi, pa)]})

    @app.get("/api/v2/monitor/system/firmware/upgrade-paths")
    def ups():
        def p(v):
            a, b, c = map(int, v.split("."))
            return {"major": a, "minor": b, "patch": c, "build": build(v)}
        return wrap([{"from": p(a), "to": p(b)} for a, b in PATHS])

    @app.post("/api/v2/monitor/system/firmware/upgrade")
    async def upgrade(request: Request):
        ctype = request.headers.get("content-type", "")
        raw_len = int(request.headers.get("content-length") or 0)
        if ctype.startswith("multipart/"):
            if state.get("no_multipart"):
                return JSONResponse({"status": "error", "error": -651}, 400)
            form = await request.form()
            f = form.get("file")
            body = {"source": form.get("source"), "filename": form.get("filename") or getattr(f, "filename", "")}
            state["last_upload"] = {"mode": "multipart", "bytes": len(await f.read()) if f else 0}
        else:
            if state.get("max_json") and raw_len > state["max_json"]:
                return PlainTextResponse("Request Entity Too Large", 413)
            body = await request.json()
            if body.get("file_content"):
                state["last_upload"] = {"mode": "base64", "bytes": len(body["file_content"])}
        if body.get("source") == "fortiguard":
            ver = next((c for c in CATALOG if f"07{c.replace('.', '')}F{build(c)}" == body.get("filename")), None)
        else:
            import re
            m = re.search(r"v(\d+\.\d+\.\d+)", body.get("filename", ""))
            ver = m.group(1) if m else None
        if not ver:
            return JSONResponse({"status": "error", "error": -1}, 400)
        state["pending"] = ver
        state["down_until"] = time.time() + state["reboot"]
        state.setdefault("upgrades", []).append({"source": body.get("source"), "version": ver})
        ev = state.setdefault("extra_events", [])
        ev.append(("system", "notice", "Firmware upgraded", f"Image upgraded from v{state['version']} to v{ver}"))
        if state.get("break"):
            ev.append(("router", "error", "BGP neighbor status changed",
                       "BGP: %BGP-3-NOTIFICATION: neighbor 10.0.0.2 Down - hold timer expired"))
        return wrap({"status": "success"})

    @app.get("/api/v2/monitor/system/config/backup")
    def backup(scope: str = "global"):
        v = state["version"]
        if state.get("backup_protected"):  # FortiOS 8.0 behaviour seen on a real unit
            return JSONResponse({"http_method": "POST", "results": {"error": "config_backup_protection_required"},
                                 "vdom": "root", "path": "system", "name": "config", "action": "backup",
                                 "status": "success", "version": f"v{v}"})
        text_ = CONFIG.format(ver=v, build=build(v), name=state["name"],
                                               ha=state["ha_cfg"]["mode"], vd=1 if state["vdom"] else 0,
                                               nonce=int(time.time() * 1000) % 9999,
                                               fmgblock=(f'config system central-management\n    set type fortimanager\n'
                                                         f'    set fmg "{state["fmg"]}"\nend\n') if state.get("fmg") else "")
        txt = text_
        if state.get("upgrades"):  # upgrades add new defaults, as real FortiOS does
            txt = txt.replace("    set admin-https-ssl-versions tlsv1-1 tlsv1-2 tlsv1-3\n",
                              "    set admin-https-ssl-versions tlsv1-2 tlsv1-3\n    set gui-firmware-upgrade-warning enable\n")
        if broken():
            txt = txt.replace('        edit "10.0.0.2"\n            set remote-as 65002\n        next\n', "")
        return PlainTextResponse(txt)

    return app


def new_state(version="7.2.10", ha="standalone", vdom=False, fgsp=False, name="FGT-LAB", serial="FGT60FTK00000001",
              reboot=5.0, brk=False, fmg=""):
    ha_cfg = {"mode": ha, "group-name": "lab", "group-id": 21, "override": "disable", "priority": 200,
              "hbdev": '"ha" 50 ', "monitor": '"port1" ', "session-pickup": "enable"}
    maj, mi, pa = map(int, version.split("."))
    if (maj, mi, pa) >= (7, 4, 1):
        ha_cfg["upgrade-mode"] = "uninterruptible"
    else:
        ha_cfg["uninterruptible-upgrade"] = "enable"
    return {"version": version, "ha_cfg": ha_cfg, "vdom": vdom, "fgsp": fgsp, "name": name, "serial": serial,
            "reboot": reboot, "break": brk, "fmg": fmg}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--version", default="7.2.10")
    ap.add_argument("--ha", default="standalone", choices=["standalone", "a-p", "a-a"])
    ap.add_argument("--vdom", action="store_true")
    ap.add_argument("--fgsp", action="store_true")
    ap.add_argument("--name", default="FGT-LAB")
    ap.add_argument("--serial", default="FGT60FTK00000001")
    ap.add_argument("--reboot-seconds", type=float, default=8)
    ap.add_argument("--break", dest="brk", action="store_true")
    ap.add_argument("--fmg", default="", help="pretend to be managed by this FortiManager IP")
    ap.add_argument("--model", default=None, help="e.g. 60F, 90G, 100F, 200F, VM64")
    ap.add_argument("--if-extra", default="", help='JSON per-interface overrides, e.g. {"port1": {"management-ip": "..."}}')
    ap.add_argument("--priority", type=int, default=200)
    a = ap.parse_args()
    st = new_state(a.version, a.ha, a.vdom, a.fgsp, a.name, a.serial, a.reboot_seconds, a.brk, a.fmg)
    st["model"] = a.model
    if a.if_extra:
        import json as _json
        st["if_extra"] = _json.loads(a.if_extra)
    st["ha_cfg"]["priority"] = a.priority
    if (a.model or "").upper().startswith("VM"):
        st["ha_cfg"]["hbdev"] = '"port2" 50 '
    uvicorn.run(make_app(st), host="127.0.0.1", port=a.port, log_level="warning")
