import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FORTIUPGRADE_HOME", str(tmp_path))
    monkeypatch.setenv("FORTIUPGRADE_NO_KEYRING", "1")
    monkeypatch.setenv("FORTIUPGRADE_OFFLINE", "1")


import uvicorn  # noqa: E402

import mock_fortigate as mock  # noqa: E402
from fortiupgrade import fmg, images, jobs, orchestrator, release_notes, storage, upgrade_path  # noqa: E402
from fortiupgrade.client import FortiGateClient  # noqa: E402
from fortiupgrade.diff import diff_config, normalise_config  # noqa: E402
from fortiupgrade.jobs import Job  # noqa: E402
from fortiupgrade.version import FosVersion  # noqa: E402


def serve(state):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(mock.make_app(state), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    return port, srv


def add_target(ttype, ports, name="t"):
    members = [{"name": f"m{i}", "host": "127.0.0.1", "port": p, "scheme": "http"} for i, p in enumerate(ports)]
    secrets = {str(i): {"token": mock.TOKEN} for i in range(len(ports))}
    return storage.save_target({"name": name, "type": ttype, "members": members}, secrets)


FAST = dict(initial_delay=0, poll_interval=0.3, wait_timeout=30, approve_before_upgrade=False,
            approve_between_hops=False, approve_between_members=False, stop_on_critical=False)


def test_versions():
    assert FosVersion.parse("v7.4.8 build2795") == FosVersion.parse("7.4.8")
    assert FosVersion.parse("7.2.11") < "7.4.0"
    assert orchestrator.image_version("FGT_60F-v7.4.8.F-build2795-FORTINET.out") == "7.4.8"


def test_path_from_fortiguard():
    edges = upgrade_path.parse_edges([{"from": {"major": 7, "minor": 2, "patch": 10}, "to": {"major": 7, "minor": 4, "patch": 8}},
                                      {"from": "7.4.8", "to": "7.6.4"}, {"from": "7.2.10", "to": "7.2.11"},
                                      {"from": "7.2.11", "to": "7.4.8"}])
    p = upgrade_path.shortest_path(edges, FosVersion.parse("7.2.10"), FosVersion.parse("7.6.4"))
    assert [str(v) for v in p] == ["7.2.10", "7.4.8", "7.6.4"]


def test_fallback_path():
    r = upgrade_path.plan(None, "7.0.12", "7.4.8", available=["7.2.9", "7.2.11", "7.4.8"])
    assert r["steps"] == ["7.0.12", "7.2.11", "7.4.8"] and not r["verified"]


def test_rules():
    cfg = mock.CONFIG.format(ver="7.4.8", build=1, name="x", ha="a-p", vd=0, nonce=1, fmgblock="")
    ids = {h["id"] for h in release_notes.evaluate_rules(cfg, "7.4.8", "7.6.4")}
    assert "sslvpn-tunnel-removed" in ids and "ha-upgrade-mode" not in ids
    ids = {h["id"] for h in release_notes.evaluate_rules(cfg, "7.2.10", "7.4.8")}
    assert "ha-upgrade-mode" in ids and "sslvpn-tunnel-removed" not in ids
    notes = "Special notices\nSSL VPN tunnel mode is no longer supported.\nKnown issues\n1012345 BGP neighbor flaps after upgrade.\n"
    hits = release_notes.scan_release_notes(notes, cfg, "7.6.4")
    assert {h["term"] for h in hits} >= {"BGP", "SSL VPN"}


def test_config_diff_masks_secrets():
    a = mock.CONFIG.format(ver="7.2.10", build=1, name="x", ha="a-p", vd=0, nonce=1, fmgblock="")
    b = mock.CONFIG.format(ver="7.4.8", build=2, name="x", ha="a-p", vd=0, nonce=2, fmgblock="")
    assert not any("abcdef1" in line for line in normalise_config(a))
    assert diff_config(a, b)["summary"] == {"added": 0, "removed": 0, "changed": 0}


def test_standalone_upgrade_with_diff():
    state = mock.new_state("7.2.10", reboot=1, brk=True)
    port, srv = serve(state)
    t = add_target("standalone", [port])
    job = Job("upgrade", "t")
    res = orchestrator.run_upgrade(job, t, dict(FAST, target_version="7.4.8", source="fortiguard", dry_run=False))
    assert state["version"] == "7.4.8" and res["path"] == ["7.2.10", "7.4.8"]
    tot = res["reports"][0]["totals"]
    assert tot["critical"] >= 2  # bgp down + port3 link down
    # snapshot naming: <hostname>-<day>-<dd-mm-yyyy-hh-mm-ss>.log
    pre = res["reports"][0]["pre"]
    assert re.fullmatch(r"FGT-LAB-(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)-"
                        r"\d{2}-\d{2}-\d{4}-\d{2}-\d{2}-\d{2}", pre)
    log = storage.snapshot_log_path(t["id"], pre).read_text()
    assert "BGP neighbours" in log and "Established" in log and "_" not in log.split("SUMMARY")[1].split("\n")[1]
    srv.should_exit = True


def test_dry_run_changes_nothing():
    state = mock.new_state("7.2.10", ha="a-p", reboot=1)
    port, srv = serve(state)
    t = add_target("fgcp", [port])
    orchestrator.run_upgrade(Job("u", "t"), t, dict(FAST, target_version="7.6.4", source="fortiguard",
                                                    ha_mode="simultaneous", dry_run=True))
    assert state["version"] == "7.2.10" and not state.get("ha_puts") and not state.get("upgrades")
    srv.should_exit = True


def test_fgcp_multi_hop_mode_and_restore():
    state = mock.new_state("7.2.10", ha="a-p", reboot=1)
    port, srv = serve(state)
    t = add_target("fgcp", [port])
    orchestrator.run_upgrade(Job("u", "t"), t, dict(FAST, target_version="7.6.4", source="fortiguard",
                                                    ha_mode="simultaneous", dry_run=False))
    assert [u["version"] for u in state["upgrades"]] == ["7.4.8", "7.6.4"]
    # 7.2 uses uninterruptible-upgrade; restored at the end
    assert state["ha_puts"][0] == {"uninterruptible-upgrade": "disable"}
    assert state["ha_puts"][-1] == {"uninterruptible-upgrade": "enable"}
    srv.should_exit = True


def test_fgcp_local_only_needs_741():
    state = mock.new_state("7.2.10", ha="a-p", reboot=1)
    port, srv = serve(state)
    t = add_target("fgcp", [port])
    with pytest.raises(RuntimeError, match="7.4.1"):
        orchestrator.run_upgrade(Job("u", "t"), t, dict(FAST, target_version="7.4.8", source="fortiguard",
                                                        ha_mode="local-only", member_ids=[t["members"][0]["id"]],
                                                        dry_run=False, run_checks=False))
    srv.should_exit = True


def test_fgsp_rolling_store_image(tmp_path):
    s1, s2 = mock.new_state("7.4.7", fgsp=True, reboot=1, name="a"), mock.new_state("7.4.7", fgsp=True, reboot=1, name="b")
    (p1, srv1), (p2, srv2) = serve(s1), serve(s2)
    img = tmp_path / "FGT_60F-v7.4.8.F-build2795-FORTINET.out"
    img.write_bytes(b"\0" * 1024)
    item = images.add(img)
    assert item["version"] == "7.4.8" and item["hardware"] == ["FGT_60F"] and item["build"] == 2795
    t = add_target("fgsp", [p1, p2])
    res = orchestrator.run_upgrade(Job("u", "t"), t, dict(FAST, target_version="7.4.8", source="store",
                                                          ha_mode="uninterruptible", dry_run=False))
    assert s1["version"] == s2["version"] == "7.4.8" and len(res["reports"]) == 2
    assert s1["upgrades"][0]["source"] == "upload"
    srv1.should_exit = srv2.should_exit = True


def test_vdom_snapshot():
    state = mock.new_state("7.4.8", vdom=True)
    port, srv = serve(state)
    from fortiupgrade.snapshot import take_snapshot
    snap = take_snapshot(FortiGateClient("127.0.0.1", mock.TOKEN, port=port, scheme="http"), "pre")
    assert set(snap["results"]["interfaces"]) == {"root", "cust1"}
    assert "global" in snap["results"]["ha"] and snap["config"]
    port1 = snap["results"]["interfaces"]["root"][0]
    assert port1["Status"] == "Up" and port1["Speed"] == "1 Gbps" and port1["Duplex"] == "Full"
    srv.should_exit = True


def test_server_auth():
    from fastapi.testclient import TestClient
    from fortiupgrade import server
    c = TestClient(server.app)
    assert c.get("/api/meta").status_code == 401
    assert c.get("/api/meta", headers={"x-auth": server.TOKEN}).status_code == 200
    assert c.get("/").status_code == 200


def test_image_store_matching(tmp_path):
    f = tmp_path / "FGT_100F-v7.4.8.F-build2795-FORTINET.out"
    f.write_bytes(b"x")
    images.add(f)
    assert images.find("7.4.8", "FGT100F") and not images.find("7.4.8", "FGT60F")
    assert images.hw_key("FGT_VM64") == images.hw_key("FGVM64")


def test_fmg_detection_and_gate():
    cfg = 'config system central-management\n    set type fortimanager\n    set fmg "10.0.0.5"\nend\n'
    m = fmg.from_config(cfg)
    assert m["managed"] and m["fmg"] == "10.0.0.5" and len(fmg.findings(m)) == 5
    assert not fmg.from_config("config system global\nend\n")["managed"]
    state = mock.new_state("7.4.7", reboot=1, fmg="10.0.0.5")
    port, srv = serve(state)
    t = add_target("standalone", [port])
    with pytest.raises(RuntimeError, match="FortiManager"):
        orchestrator.run_upgrade(Job("u", "t"), t, dict(FAST, target_version="7.4.8", source="fortiguard"))
    orchestrator.run_upgrade(Job("u", "t"), t, dict(FAST, target_version="7.4.8", source="fortiguard",
                                                    fmg_acknowledged=True, run_checks=False))
    assert state["version"] == "7.4.8"
    srv.should_exit = True


def test_batch_simultaneous():
    sts = [mock.new_state("7.4.7", reboot=1, name=f"fw{i}") for i in range(3)]
    srvs = [serve(s) for s in sts]
    ts = [add_target("standalone", [p], name=f"fw{i}") for i, (p, _) in enumerate(srvs)]
    batch = jobs.start("batch", "b", orchestrator.run_batch, ts,
                       dict(FAST, target_version="7.4.8", source="fortiguard", run_checks=False))
    for _ in range(100):
        if batch.status == "waiting":
            break
        time.sleep(0.1)
    assert batch.status == "waiting"
    batch.answer(True)
    for _ in range(300):
        if batch.status not in ("running", "waiting"):
            break
        time.sleep(0.1)
    assert batch.status == "done", batch.log
    assert all(s["version"] == "7.4.8" for s in sts)
    for _, srv in srvs:
        srv.should_exit = True


KI_HTML = """<h2>New known issues</h2><h3>IPsec VPN</h3><table><tr><th>Bug ID</th><th>Description</th></tr>
<tr><td><p>1111111</p></td><td><p>IPsec tunnel flaps when DPD is on-idle.</p></td></tr></table>
<h2>Existing known issues</h2><h3>HA</h3><table><tr><td>2222222</td><td>HA cluster goes out of sync.</td></tr></table>
<h3>FortiGate 6000/7000 Platform</h3><table><tr><td>3333333</td><td>FIM link fails with RS-FEC.</td></tr></table>
<h3>System</h3><table><tr><td>4444444</td><td>FG-100F/101F reboot when NP6 is busy.</td></tr></table>"""


def test_known_issues_parse_and_rate():
    ki = release_notes.parse_known_issues(KI_HTML)
    assert [k["bug_id"] for k in ki] == ["1111111", "2222222", "3333333", "4444444"]
    assert ki[0]["category"] == "IPsec VPN" and ki[0]["group"] == "New known issues"
    cfg = mock.CONFIG.format(ver="7.4.7", build=1, vd=0, name="x", ha="standalone", nonce=1, fmgblock="")
    rated = {k["bug_id"]: k for k in release_notes.rate_known_issues(ki, cfg, "FortiGate-60F")}
    assert rated["1111111"]["relevant"]              # config uses IPsec
    assert not rated["2222222"]["relevant"]          # HA is standalone
    assert not rated["3333333"]["relevant"]          # 6000/7000 only
    assert not rated["4444444"]["relevant"]          # another model
    assert release_notes.rate_known_issues(ki, cfg.replace("set mode standalone", "set mode a-p"),
                                           "FortiGate-60F")[1]["relevant"]


def test_job_history_persisted():
    j = jobs.start("snapshot", "history test", lambda job: {"ok": 1}, target_id="t1", meta={"label": "x"})
    for _ in range(50):
        if j.status != "running":
            break
        time.sleep(0.05)
    time.sleep(0.1)
    d = storage.load_job(j.id)
    assert d["status"] == "done" and d["target_ids"] == ["t1"] and d["finished"] and d["result"] == {"ok": 1}
    assert any(x["id"] == j.id for x in storage.list_jobs())


def test_new_checks_and_log_compare():
    state = mock.new_state("7.4.7", brk=True)
    state["model"] = "100F"
    port, _srv = serve(state)
    c = FortiGateClient("127.0.0.1", "mocktoken", port=port, scheme="http")
    from fortiupgrade import checks
    from fortiupgrade.diff import diff_records
    ctx = checks.Ctx(client=c, vdom_mode=False)
    ifs = {r["Interface"]: r for r in checks.c_interfaces(ctx, None)}
    assert ifs["port1"]["Admin access"] == "PING, HTTPS, SSH" and ifs["wan1"]["Role"] == "WAN"
    sd = checks.c_sdwan_zones(ctx, None)
    assert {r["Zone"] for r in sd} == {"virtual-wan-link", "overlay"} and all(r["Link"] == "Up" for r in sd)
    before = checks.c_logs(ctx, None)
    state["extra_events"] = [("router", "error", "BGP neighbor status changed", "neighbor 10.0.0.2 Down")]
    after = checks.c_logs(ctx, None)
    ch = diff_records(checks.CHECKS_BY_ID["logs"], before, after)
    assert len(ch) == 1 and ch[0]["type"] == "added" and ch[0]["severity"] == "warning"


def test_vault_file_store_migration_and_switch(tmp_path):
    import json as _json
    from fortiupgrade import vault
    vault.reset_cache()
    # an older install kept secrets in secrets.json
    (tmp_path / "secrets.json").write_text(_json.dumps({"m1:token": "abc"}))
    assert storage.get_secret("m1:token") == "abc"           # migrated into the vault
    raw = (tmp_path / "secrets.vault").read_bytes()
    assert b"abc" not in raw                                   # encrypted on disk
    assert (tmp_path / "vault.key").stat().st_mode & 0o077 == 0
    storage.set_secret("m1:ssh", "pw")
    vault.reset_cache()
    assert storage.get_secret("m1:ssh") == "pw" and storage.get_secret("m1:token") == "abc"
    storage.set_secret("m1:ssh", None)
    assert storage.get_secret("m1:ssh") is None
    assert vault.settings()["secret_store"] == "file"


@pytest.mark.parametrize("model,expect,absent", [
    ("90G", {"x1", "x2", "port1", "port6", "a", "b"}, {"npu0_vlink0", "npu0_vlink1", "modem"}),
    ("60F", {"wan1", "dmz", "internal1", "internal5"}, {"modem", "internal"}),
    ("200F", {"mgmt", "ha", "port24", "x4"}, {"npu0_vlink0"}),
])
def test_physical_ports_only(model, expect, absent):
    from fortiupgrade import checks
    state = mock.new_state("7.4.7")
    state["model"] = model
    port, _srv = serve(state)
    c = FortiGateClient("127.0.0.1", "mocktoken", port=port, scheme="http")
    ps = {p["name"]: p for p in checks.physical_ports(checks.Ctx(client=c, vdom_mode=False))}
    assert expect <= set(ps) and not (absent & set(ps))
    if model == "60F":
        assert ps["internal1"]["member_of"] == "internal" and ps["internal1"]["status"] == "Up"
        assert ps["internal2"]["status"] == "Down"


def test_batch_no_pause_local_push_streams_verbose_log(tmp_path):
    sts = [mock.new_state("7.4.7", reboot=1, name=f"fw{i}") for i in range(2)]
    for s in sts:
        s["model"] = "60F"
    srvs = [serve(s) for s in sts]
    ts = [add_target("standalone", [p], name=f"fw{i}") for i, (p, _) in enumerate(srvs)]
    img = tmp_path / "FGT_60F-v7.4.8.M-build1259-FORTINET.out"
    img.write_bytes(b"x" * 4096)
    images.add(img)
    batch = jobs.start("batch", "b", orchestrator.run_batch, ts,
                       dict(FAST, target_version="7.4.8", source="store", run_checks=True,
                            pause_after_prechecks=False))
    for _ in range(400):
        if batch.status not in ("running", "waiting"):
            break
        assert batch.status != "waiting" or batch.prompt is None, "should not stop for approval"
        time.sleep(0.1)
    assert batch.status == "done", [l["msg"] for l in batch.log][-20:]
    assert all(s["version"] == "7.4.8" for s in sts)
    msgs = [(l["src"], l["level"], l["msg"]) for l in batch.log]
    srcs = {s for s, _, _ in msgs}
    assert {"fw0", "fw1", "all devices"} <= srcs                       # one stream, tagged per device
    assert any("install method: push local image FGT_60F-v7.4.8" in m for _, _, m in msgs)
    assert any("upload complete" in m for _, _, m in msgs)
    assert any(lv == "detail" and "POST /api/v2/monitor/system/firmware/upgrade" in m for _, lv, m in msgs)
    assert any("✓ Interfaces" in m for _, _, m in msgs)
    assert not any("Waiting for approval" in m for _, _, m in msgs)
    for _, srv in srvs:
        srv.should_exit = True


def test_image_matching_by_model_name_and_product(tmp_path):
    for name in ("FFW_VM64_KVM-v8.0.2.F-build0251-FORTINET.out", "FGT_90G-v8.0.2.F-build0251-FORTINET.out"):
        f = tmp_path / name
        f.write_bytes(name.encode())
        images.add(f)
    assert images.image_hw_for("FortiGate-VM64-KVM") == "FGT_VM64_KVM"
    assert images.image_hw_for("FortiGate-40F-3G4G") == "FGT_40F_3G4G"
    # FortiFirewall image must not be offered to a FortiGate-VM
    ex = images.explain("8.0.2", "FortiGate-VM64-KVM", "FGVMK6")
    assert ex["image"] is None and ex["expected_hw"] == "FGT_VM64_KVM"
    assert "FortiFirewall" in ex["near"][0]["reason"]
    assert images.find("8.0.2", "FortiGate-90G", "FGT90G")["filename"].startswith("FGT_90G")
    assert images.find("8.0.2", "FGT90G")["filename"].startswith("FGT_90G")      # legacy code-only call
    f = tmp_path / "FGT_VM64_KVM-v8.0.2.F-build0251-FORTINET.out"
    f.write_bytes(b"kvm")
    images.add(f)
    assert images.find("8.0.2", "FortiGate-VM64-KVM", "FGVMK6")["filename"].startswith("FGT_VM64_KVM")


def test_config_diff_with_removed_settings():
    d = diff_config("config system global\n    set hostname a\n    set x 1\nend\n",
                    "config system global\n    set hostname b\nend\n")
    assert d["summary"] == {"added": 0, "removed": 1, "changed": 1}


def test_config_history_fetch_view_and_diff():
    from fastapi.testclient import TestClient
    from fortiupgrade import server
    state = mock.new_state("7.4.7", reboot=0)
    port, srv = serve(state)
    t = add_target("standalone", [port], name="cfgdev")
    c = TestClient(server.app)
    H = {"x-auth": server.TOKEN}
    r1 = c.post(f"/api/targets/{t['id']}/configs/fetch", headers=H, json={}).json()
    assert r1["kind"] == "live" and not r1["unchanged"] and r1["file"].endswith(".conf")
    again = c.post(f"/api/targets/{t['id']}/configs/fetch", headers=H, json={}).json()
    assert again["unchanged"] and again["ref"] == r1["ref"]          # identical config is not stored twice
    state["upgrades"] = [{"version": "7.4.8"}]
    state["break"] = True
    state["version"] = "7.4.8"
    r2 = c.post(f"/api/targets/{t['id']}/configs/fetch", headers=H, json={}).json()
    assert not r2["unchanged"]
    refs = [x["ref"] for x in c.get(f"/api/targets/{t['id']}/configs", headers=H).json()]
    assert refs[:2] == [r2["ref"], r1["ref"]]
    view = c.get(f"/api/targets/{t['id']}/configs/view", headers=H, params={"ref": r1["ref"]}).text
    assert "ENC <redacted>" in view and "ENC xyz" not in view
    raw = c.get(f"/api/targets/{t['id']}/configs/view", headers=H, params={"ref": r1["ref"], "raw": True}).text
    assert "ENC xyz" in raw
    d = c.get(f"/api/targets/{t['id']}/configs/diff", headers=H, params={"a": r1["ref"], "b": r2["ref"]}).json()
    assert d["summary"]["added"] >= 1 and d["summary"]["changed"] >= 1
    assert any(row["t"] in ("chg", "add") for row in d["rows"])
    assert any("gui-firmware-upgrade-warning" in ch["path"] for ch in d["changes"])
    srv.should_exit = True


@pytest.mark.parametrize("flags,expect_mode,ok", [
    ({}, "multipart", True),                                  # GUI-style file upload
    ({"no_multipart": True}, "base64", True),                 # older build: falls back to base64 JSON
    ({"no_multipart": True, "max_json": 1000}, None, False),  # base64 too large -> clear 413 message
])
def test_local_image_upload_modes(tmp_path, flags, expect_mode, ok):
    state = mock.new_state("7.4.7", reboot=1)
    state.update(flags)
    port, srv = serve(state)
    t = add_target("standalone", [port], name="up")
    img = tmp_path / "FGT_60F-v7.4.8.M-build1259-FORTINET.out"
    img.write_bytes(b"y" * 5000)
    images.add(img)
    job = Job("upgrade", "t")
    opts = dict(FAST, target_version="7.4.8", source="store", run_checks=False)
    if ok:
        orchestrator.run_upgrade(job, t, opts)
        assert state["last_upload"]["mode"] == expect_mode and state["version"] == "7.4.8"
    else:
        with pytest.raises(RuntimeError, match="too large \\(HTTP 413\\)"):
            orchestrator.run_upgrade(job, t, opts)
    srv.should_exit = True


def test_upgrade_pdf_report(tmp_path):
    from pypdf import PdfReader
    from fortiupgrade import report
    sts = [mock.new_state("7.4.7", reboot=1, name=f"fw{i}", brk=(i == 1)) for i in range(2)]
    srvs = [serve(s) for s in sts]
    ts = [add_target("standalone", [p], name=f"fw{i}") for i, (p, _) in enumerate(srvs)]
    pf = jobs.start("preflight", "pf", lambda job: {"results": [
        {"target": t["name"], "target_id": t["id"], "status": "done", "result": orchestrator.preflight(job, t, "7.4.8")}
        for t in ts]})
    from fortiupgrade import review
    rv = jobs.start("review", "rv", lambda job: {"results": [
        {"target": t["name"], "target_id": t["id"], "status": "done", "result": review.review_target(job, t, "7.4.8")}
        for t in ts]})
    for _ in range(100):
        if pf.status != "running" and rv.status != "running":
            break
        time.sleep(0.1)
    batch = jobs.start("batch", "Upgrade 2 device(s) to 7.4.8", orchestrator.run_batch, ts,
                       dict(FAST, target_version="7.4.8", source="fortiguard", run_checks=True, pause_after_prechecks=False),
                       target_ids=[t["id"] for t in ts], meta={"target_version": "7.4.8", "source": "fortiguard",
                                                              "preflight_job": pf.id, "review_job": rv.id, "run_checks": True})
    for _ in range(400):
        if batch.status not in ("running", "waiting"):
            break
        time.sleep(0.1)
    assert batch.status == "done"
    time.sleep(0.3)
    out = report.build(batch.id, tmp_path / "r.pdf")
    assert out.read_bytes()[:5] == b"%PDF-"
    pdf = PdfReader(str(out))
    text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    assert len(pdf.pages) >= 5
    for want in ("FortiGate Upgrade Report", "Download from FortiGuard", "Pre-flight", "fw0", "fw1", "Timeline",
                 "Pre / post checks", "Check differences", "Configuration differences", "Activity log",
                 "BGP neighbours", "Established", "Idle", "Configuration and log review", "Logs: m0"):
        assert want in text, want
    for _, srv in srvs:
        srv.should_exit = True


def test_protected_backup_is_not_treated_as_config():
    from fortiupgrade.client import FortiAPIError
    from fortiupgrade.snapshot import take_snapshot
    state = mock.new_state("8.0.2")
    state["backup_protected"] = True
    port, srv = serve(state)
    c = FortiGateClient("127.0.0.1", "mocktoken", port=port, scheme="http")
    with pytest.raises(FortiAPIError, match="password-protected"):
        c.backup_config("global")
    snap = take_snapshot(c, "post-upgrade", check_ids=["system"])
    assert snap["config"] is None and "password-protected" in snap["errors"]["config"]["global"]
    good = take_snapshot(c, "x", check_ids=["system"], include_config=False)
    good["config"] = mock.CONFIG.format(ver="7.6.8", build=1, vd=0, name="x", ha="standalone", nonce=1, fmgblock="")
    bad = dict(good, config='{"results":{"error":"config_backup_protection_required"}}')
    d = diff_snapshots_for_test(good, bad)
    assert d["config"] is None and "not compared" in d["config_note"]
    srv.should_exit = True


def diff_snapshots_for_test(a, b):
    from fortiupgrade.diff import diff_snapshots
    return diff_snapshots(a, b)


def test_vm_series():
    from fortiupgrade.checks import vm_series
    assert vm_series("FortiGate-60F", "FGT60FTK00000001") is None
    assert vm_series("FortiGate-VM64-KVM", "FGVM04TM24000001")["series"] == "VM04"
    assert vm_series("FortiGate-VM64", "FGVMULTM24000001")["series"] == "VMUL"
    v = vm_series("FortiGate-VM64-KVM", "FGVMSLTM00000001", {"status": "vm_valid"}, {"cpu_max": 2, "cpu_used": 1})
    assert v["series"] == "VM02" and v["licence"] == "Subscription (VM-S)" and "2 licensed vCPUs" in v["basis"]
    assert vm_series("FortiGate-VM64", "FGVMSLTM1", {}, {"cpu_max": 3})["series"] == "VM04"
    assert vm_series("FortiGate-VM64", "FGVMSLTM1", {}, {"cpu_max": "unlimited"})["series"] == "VMUL"
    assert vm_series("FortiGate-VM64", "FGVMSLTM1")["series"] == ""


def test_vm_series_in_overview():
    from fastapi.testclient import TestClient
    from fortiupgrade import server
    state = mock.new_state("7.6.8", serial="FGVMSLTM00000001")
    state["model"] = "VM64-KVM"
    state["vm_cpus"] = 8
    port, _ = serve(state)
    tid = add_target("standalone", [port], name="vm")["id"]
    cl = TestClient(server.app)
    r = cl.get(f"/api/targets/{tid}/overview", headers={"X-Auth": server.TOKEN}).json()
    vm = r["members"][0]["vm"]
    assert vm["series"] == "VM08" and vm["cpu_allowed"] == 8 and vm["mem_allowed"] == "unlimited"


def test_probes_normalise_and_status_rules():
    from fortiupgrade import probes
    ps = probes.normalise_all([{"type": "tcp", "target": "10.0.0.5", "port": "443"},
                               {"type": "http", "url": "intranet.local/health", "expect": "2xx"},
                               {"type": "ping", "target": "8.8.8.8", "interval_ms": 10}, {"type": "ping", "target": ""}])
    assert [p["name"] for p in ps] == ["TCP 10.0.0.5:443", "HTTP https://intranet.local/health", "Ping 8.8.8.8"]
    assert ps[2]["interval_ms"] == 100 and ps[1]["url"].startswith("https://")
    with pytest.raises(ValueError):
        probes.normalise({"type": "tcp", "target": "h"})
    assert probes._status_ok(401, "") and not probes._status_ok(503, "")
    assert probes._status_ok(204, "2xx") and not probes._status_ok(302, "200")


def test_probes_measure_outage_during_batch_upgrade():
    from fortiupgrade import probes
    st = mock.new_state("7.4.7", reboot=4.5, name="pfw")
    port, srv = serve(st)
    t = add_target("standalone", [port], name="pfw")
    specs = [{"type": "http", "url": f"http://127.0.0.1:{port}/api/v2/monitor/system/status", "interval_ms": 1000,
              "timeout_ms": 500},
             {"type": "ping", "target": "127.0.0.1", "interval_ms": 100}]
    batch = jobs.start("batch", "b", orchestrator.run_batch, [t],
                       dict(FAST, target_version="7.4.8", source="fortiguard", run_checks=False,
                            pause_after_prechecks=False, probes=probes.normalise_all(specs),
                            probe_baseline_s=2, probe_settle_s=1.5, probe_min_outage_ms=1500))
    for _ in range(300):
        if batch.status not in ("running", "waiting"):
            break
        time.sleep(0.1)
    assert batch.status == "done", batch.log
    s = batch.to_dict()["probes"]
    http, ping = s["probes"]
    assert http["outages"] >= 1 and http["longest_ms"] >= 2000, http["windows"]
    w = next(x for x in http["windows"] if x["kind"] == "outage")
    assert w["phase"] == "upgrade" and "HTTP 503" in w["errors"] and w["during"]
    assert ping["outages"] == 0 and ping["total"]["sent"] > 10
    assert http["phases"]["baseline"]["lost"] == 0 and http["baseline_ok"]
    assert any("DOWN since" in e["msg"] for e in batch.log) and any("back after" in e["msg"] for e in batch.log)
    assert not s["still_down"]
    # persisted with the job and drawn in the PDF report
    from fortiupgrade import report
    pdf = report.build(batch.id)
    assert pdf.exists() and pdf.stat().st_size > 2000
    srv.should_exit = True


def test_probes_on_fgcp_cluster_attribute_failover():
    from fortiupgrade import probes
    st = mock.new_state("7.4.7", ha="a-p", reboot=4.5, name="ha")
    port, srv = serve(st)
    t = add_target("fgcp", [port], name="ha")
    specs = probes.normalise_all([{"type": "http", "url": f"http://127.0.0.1:{port}/", "interval_ms": 1000,
                                   "timeout_ms": 400}])
    batch = jobs.start("batch", "b", orchestrator.run_batch, [t],
                       dict(FAST, target_version="7.4.8", source="fortiguard", run_checks=False,
                            pause_after_prechecks=False, ha_modes={"fgcp": "uninterruptible"}, probes=specs,
                            probe_baseline_s=1.5, probe_settle_s=1.5, probe_min_outage_ms=1500))
    for _ in range(300):
        if batch.status not in ("running", "waiting"):
            break
        time.sleep(0.1)
    assert batch.status == "done", batch.log
    s = batch.to_dict()["probes"]
    labels = [m["label"] for m in s["markers"]]
    assert any("fails over" in x for x in labels) and any("rebooting" in x for x in labels), labels
    p = s["probes"][0]
    assert p["outages"] >= 1 and all(w["during"] for w in p["windows"])
    srv.should_exit = True


def test_cluster_saved_as_standalone_gets_chosen_ha_mode():
    st = mock.new_state("7.4.7", ha="a-p", reboot=1, name="hsa")
    port, srv = serve(st)
    t = add_target("standalone", [port], name="hsa")      # user saved the cluster address as a standalone device
    batch = jobs.start("batch", "b", orchestrator.run_batch, [t],
                       dict(FAST, target_version="7.4.8", source="fortiguard", run_checks=False,
                            pause_after_prechecks=False, ha_modes={"fgcp": "simultaneous", "fgsp": "uninterruptible"}))
    for _ in range(300):
        if batch.status not in ("running", "waiting"):
            break
        time.sleep(0.1)
    assert batch.status == "done", batch.log
    assert st["version"] == "7.4.8"
    assert st["ha_puts"][0] == {"upgrade-mode": "simultaneous"}            # the chosen mode was applied
    assert any("upgrading it as an FGCP cluster" in e["msg"] for e in batch.log)
    srv.should_exit = True


VM04_IFS = {"interfaces": [
    {"name": "port1", "ip": "10.8.8.21", "management_ip": "10.8.8.19", "access": ["fabric", "fgfm", "ftm", "http", "https", "ping", "ssh"], "status": "up", "admin": True},
    {"name": "port3", "ip": "10.30.30.200", "management_ip": "", "access": ["ping"], "status": "up", "admin": False},
    {"name": "fortilink", "ip": "10.255.1.1", "management_ip": "", "access": ["fabric", "ping"], "status": "up", "admin": True}],
    "admin_sport": 443}


def test_default_probes_use_each_units_own_address():
    from fortiupgrade import hainfo
    vm_b = {"interfaces": [dict(VM04_IFS["interfaces"][0], management_ip="10.8.8.20"), *VM04_IFS["interfaces"][1:]],
            "admin_sport": 443}
    units = [{"target": "76", "hostname": "fgt-vm04-a", "serial": "A", "ha_mode": "a-p", "host": "10.8.8.19", "mgmt": VM04_IFS},
             {"target": "76", "hostname": "fgt-vm04-b", "serial": "B", "ha_mode": "a-p", "host": None, "mgmt": vm_b},
             {"target": "76", "hostname": "fgt-vm04-a", "serial": "A", "ha_mode": "a-p", "host": "10.8.8.21", "mgmt": VM04_IFS}]
    r = hainfo.suggest_probes(units)
    got = sorted((p["type"], p.get("target") or p.get("url")) for p in r["probes"])
    assert got == [("http", "https://10.8.8.19/"), ("http", "https://10.8.8.20/"), ("ping", "10.8.8.19"), ("ping", "10.8.8.20")]
    assert not any("10.8.8.21" in str(x) for x in got)          # the shared cluster address follows the primary
    assert not any("10.30.30.200" in str(x) or "10.255.1.1" in str(x) for x in got)
    assert all(p["interval_ms"] >= 1000 for p in r["probes"] if p["type"] == "http")
    assert {p["name"].split()[1] for p in r["probes"]} == {"fgt-vm04-a", "fgt-vm04-b"}
    # a unit without a per-unit address is reported, not probed through the cluster address
    bare = {"interfaces": [dict(VM04_IFS["interfaces"][0], management_ip="")], "admin_sport": 443}
    r = hainfo.suggest_probes([{"target": "x", "hostname": "solo", "serial": "S", "ha_mode": "a-p", "mgmt": bare}])
    assert not r["probes"] and "cluster address is not used" in r["notes"][0]
    # dedicated HA management interface: its own IP is per unit
    ded = {"interfaces": [{"name": "port4", "ip": "192.0.2.4", "management_ip": "", "access": ["https", "ping"],
                           "status": "up", "admin": True}], "admin_sport": 8443}
    r = hainfo.suggest_probes([{"target": "x", "hostname": "u1", "serial": "U", "ha_mode": "a-p", "mgmt": ded,
                                "dedicated": ["port4"]}])
    assert [p.get("url") or p.get("target") for p in r["probes"]] == ["192.0.2.4", "https://192.0.2.4:8443/"]
    # standalone, HTTPS on a non-default admin port, ping not allowed
    ifs = {"interfaces": [{"name": "mgmt", "ip": "192.0.2.1", "management_ip": "", "access": ["https", "ssh"],
                           "status": "up", "admin": True}], "admin_sport": 8443}
    r = hainfo.suggest_probes([{"target": "x", "hostname": "x", "ha_mode": "standalone", "host": "192.0.2.1", "mgmt": ifs}])
    assert [p.get("url") or p.get("target") for p in r["probes"]] == ["https://192.0.2.1:8443/"]
    assert any("ping is not allowed" in n for n in r["notes"])
    assert hainfo.address_role("10.8.8.19", VM04_IFS, "a-p") == {"role": "unit", "interface": "port1", "cluster_ip": "10.8.8.21"}


def test_parse_peer_cli_output():
    from fortiupgrade import hainfo
    assert hainfo.parse_manage_list("execute ha manage ?\n<id>    please input peer box index.\n"
                                    "<1>     Subsidiary unit FGVM04TM00000002\n") == [{"index": 1, "serial": "FGVM04TM00000002"}]
    show = """config system interface
    edit "port1"
        set vdom "root"
        set management-ip 10.8.8.20 255.255.255.0
        set ip 10.8.8.21 255.255.255.0
        set allowaccess ping https ssh http fgfm fabric ftm
        set type physical
    next
    edit "port2"
        set vdom "root"
        set type physical
    next
end"""
    ifs = hainfo.parse_interfaces(show)
    assert ifs == [{"name": "port1", "ip": "10.8.8.21", "management_ip": "10.8.8.20",
                    "access": ["fabric", "fgfm", "ftm", "http", "https", "ping", "ssh"], "status": "up",
                    "type": "physical", "admin": True}]


def test_probe_http_minimum_interval():
    from fortiupgrade import probes
    assert probes.normalise({"type": "http", "url": "https://10.8.8.19", "interval_ms": 100})["interval_ms"] == 1000


def test_get_retries_transient_connection_errors(monkeypatch):
    import httpx
    from fortiupgrade import client as cl
    monkeypatch.setattr(cl, "RETRY_DELAYS", (0, 0, 0))
    c = FortiGateClient("127.0.0.1", "t", port=1, scheme="http")
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
        return httpx.Response(200, json={"results": {"ok": 1}}, request=httpx.Request("GET", "http://x"))
    monkeypatch.setattr(cl.FortiGateClient, "reconnect", lambda self: None)
    c._http.request = flaky
    assert c.get("monitor/system/status")["results"] == {"ok": 1} and calls["n"] == 3
    calls["n"] = -10
    with pytest.raises(cl.FortiAPIError, match="failed 3 times"):
        c.get("monitor/system/status")


def test_ha_addresses_fix_and_overview_ha_pair():
    from fastapi.testclient import TestClient
    from fortiupgrade import server
    st = mock.new_state("7.6.8", ha="a-p", reboot=1, name="fgt-vm04-a", serial="FGVM04TM26000001")
    st["model"] = "VM64-KVM"
    port, srv = serve(st)
    t = add_target("standalone", [port], name="76")
    cl, H = TestClient(server.app), {"x-auth": server.TOKEN}
    ov = cl.get(f"/api/targets/{t['id']}/overview", headers=H).json()["members"][0]
    assert [p["hostname"] for p in ov["ha_info"]["peers"]] == ["fgt-vm04-a", "fgt-vm04-a-b"]
    assert ov["ha_info"]["hbdev"] == ["ha"] and ov["ha_info"]["priority"] == 200 and ov["ha_info"]["peers"][0]["primary"] and isinstance(ov["ha"], list)
    r = cl.post(f"/api/targets/{t['id']}/ha-addresses", headers=H,
                json={"cluster_host": "127.0.0.1", "units": [{"host": "10.8.8.20", "name": "fgt-vm04-b"}]})
    assert r.status_code == 200, r.text
    saved = storage.get_target(t["id"])
    assert saved["type"] == "fgcp" and [m["host"] for m in saved["members"]] == ["127.0.0.1", "10.8.8.20"]
    assert all(storage.get_secret(f"{m['id']}:token") == mock.TOKEN for m in saved["members"])
    srv.should_exit = True


def test_units_endpoint_and_suggest_skip_cluster_address():
    from fastapi.testclient import TestClient
    from fortiupgrade import server
    st = mock.new_state("7.6.8", ha="a-p", reboot=1, name="fgt-vm04-a", serial="FGVM04TM26000001")
    st["model"] = "VM64-KVM"
    st["if_extra"] = {"port1": {"management-ip": "127.0.0.1 255.255.255.0", "ip": "10.8.8.21 255.255.255.0",
                                "allowaccess": "ping https ssh http"}}
    port, srv = serve(st)
    t = add_target("standalone", [port], name="76")
    cl, H = TestClient(server.app), {"x-auth": server.TOKEN}
    u = cl.get(f"/api/targets/{t['id']}/units", headers=H).json()
    assert [x["serial"] for x in u["units"]] == ["FGVM04TM26000001"]
    assert u["units"][0]["own"][0]["addr"] == "127.0.0.1"
    assert any("fgt-vm04-a-b" in n and "add its own management address" in n for n in u["notes"])
    r = cl.get(f"/api/probes/suggest?target_ids={t['id']}", headers=H).json()
    targets = [p.get("target") or p.get("url") for p in r["probes"]]
    assert "127.0.0.1" in targets and f"https://127.0.0.1:{port}/" in targets
    assert not any("10.8.8.21" in x for x in targets)
    srv.should_exit = True


def test_version_matches_changelog():
    """Every release bumps __version__/__build__ and adds a change-log entry on top."""
    import json as _json
    from fastapi.testclient import TestClient
    import fortiupgrade
    from fortiupgrade import server
    log = _json.loads((Path(fortiupgrade.__file__).parent / "changelog.json").read_text())
    assert log[0]["version"] == fortiupgrade.__version__ and log[0]["build"] == fortiupgrade.__build__
    builds = [r["build"] for r in log]
    assert builds == sorted(builds, reverse=True) and len(set(builds)) == len(builds)
    vers = [tuple(map(int, r["version"].split("."))) for r in log]
    assert vers == sorted(vers, reverse=True) and len(set(vers)) == len(vers)
    assert all(r["changes"] and all(c["type"] in ("new", "improved", "changed", "fixed") for c in r["changes"]) for r in log)
    assert fortiupgrade.version_string() == f"v{fortiupgrade.__version__} build{fortiupgrade.__build__:04d}"
    cl, H = TestClient(server.app), {"x-auth": server.TOKEN}
    m = cl.get("/api/meta", headers=H).json()
    assert m["version"] == fortiupgrade.__version__ and m["build"] == fortiupgrade.__build__
    assert cl.get("/api/changelog", headers=H).json()[0]["build"] == fortiupgrade.__build__


def _fake_release(ver="9.9.0", build=99, payload=b"new-app-bytes", sums=None, key="macOS-arm64"):
    import hashlib
    name = f"FortiGate-Upgrader-{ver}-{key}.{'dmg' if key.startswith('macOS') else 'exe'}"
    base = "https://github.com/fsecuritynz/FortiGate-Upgrader/releases/download/v" + ver
    sums = sums if sums is not None else f"{hashlib.sha256(payload).hexdigest()}  {name}\n"
    rel = {"tag_name": f"v{ver}", "name": f"FortiGate Upgrader v{ver} build{build:04d}", "body": "## Notes\n- thing",
           "published_at": "2026-11-07T00:00:00Z", "html_url": base,
           "assets": [{"name": name, "size": len(payload), "browser_download_url": f"{base}/{name}"},
                      {"name": "FortiGate-Upgrader-x-Windows-x64-Setup.exe", "size": 1, "browser_download_url": f"{base}/w.exe"},
                      {"name": "SHA256SUMS.txt", "size": 10, "browser_download_url": f"{base}/SHA256SUMS.txt"}]}
    return rel, {f"{base}/{name}": payload, f"{base}/SHA256SUMS.txt": sums.encode()}


def test_updater_check_download_verify(monkeypatch):
    import httpx
    from fortiupgrade import updater
    assert updater.vtuple("v0.11.0") > updater.vtuple("0.10.9") and updater.vtuple("1.0.0") > updater.vtuple("0.99.0")
    rel, files = _fake_release()
    assets = rel["assets"]
    assert updater.pick_asset(assets, "macOS-arm64")["name"].endswith("macOS-arm64.dmg")
    assert updater.pick_asset(assets, "Windows-x64")["name"].endswith("Setup.exe")
    assert updater.pick_asset(assets, "macOS-x86_64") is None

    def handler(req):
        if req.url.host == "api.github.com":
            return httpx.Response(200, json=rel)
        body = files.get(str(req.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)
    monkeypatch.setattr(updater, "_client", lambda timeout=20: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(updater, "platform_key", lambda: "macOS-arm64")
    updater.STATE.update(checked_at=None, latest=None, download=None, error=None)
    st = updater.check(force=True)
    assert st["available"] and st["latest"]["version"] == "9.9.0" and st["latest"]["build"] == 99 and st["can_install"]
    updater.download()
    for _ in range(50):
        if updater.STATE["download"]["state"] != "downloading":
            break
        time.sleep(0.05)
    d = updater.STATE["download"]
    assert d["state"] == "ready", d
    assert Path(d["path"]).read_bytes() == b"new-app-bytes"
    # skipping a version hides the prompt but keeps it available in Settings
    updater.set_prefs(skip="9.9.0")
    assert updater.status()["skipped"] and updater.status()["available"]
    # a tampered download is refused and deleted
    rel2, files2 = _fake_release(ver="9.9.1", payload=b"evil", sums=f"{'0' * 64}  FortiGate-Upgrader-9.9.1-macOS-arm64.dmg\n")
    rel.clear(); rel.update(rel2); files.clear(); files.update(files2)
    updater.check(force=True)
    updater.download()
    for _ in range(50):
        if updater.STATE["download"]["state"] != "downloading":
            break
        time.sleep(0.05)
    assert updater.STATE["download"]["state"] == "failed" and "doesn't match" in updater.STATE["download"]["error"]
    assert not list((storage.data_dir() / "updates").glob("*9.9.1*"))
    with pytest.raises(RuntimeError):
        updater.install()
    updater.STATE.update(checked_at=None, latest=None, download=None, error=None)


def test_release_notes_tool_matches_version():
    import subprocess as sp
    root = Path(__file__).resolve().parents[1]
    import fortiupgrade
    r = sp.run([sys.executable, str(root / "tools" / "release_notes.py"), "--check", f"v{fortiupgrade.__version__}"])
    assert r.returncode == 0
    title = sp.run([sys.executable, str(root / "tools" / "release_notes.py"), "--title"], capture_output=True, text=True).stdout
    assert title.strip() == f"FortiGate Upgrader {fortiupgrade.version_string()}"


def test_snapshot_list_pairs_and_side_by_side_compare():
    from fastapi.testclient import TestClient

    from fortiupgrade import server
    from fortiupgrade.diff import side_by_side_text
    state = mock.new_state("7.2.10", reboot=1, brk=True)
    port, srv = serve(state)
    t = add_target("standalone", [port], name="lab")
    res = orchestrator.run_upgrade(Job("upgrade", "t"), t, dict(FAST, target_version="7.4.8", source="fortiguard", dry_run=False))
    pre, post = res["reports"][0]["pre"], res["reports"][0]["post"]
    srv.should_exit = True
    c = TestClient(server.app, headers={"x-auth": server.TOKEN})
    lst = c.get("/api/snapshots").json()
    by = {s["id"]: s for s in lst}
    assert by[pre]["pair"] == post and by[post]["pair"] == pre
    assert by[pre]["hostname"] == "FGT-LAB" and by[pre]["target"] == "lab" and by[post]["version"] == "7.4.8"
    assert [s["taken_at"] for s in lst] == sorted((s["taken_at"] for s in lst), reverse=True)
    d = c.get(f"/api/targets/{t['id']}/compare", params={"a": pre, "b": post}).json()
    bgp = next(r for r in d["summary"] if r["check"] == "bgp")
    assert bgp["critical"] >= 1 and bgp["status"] == "changed"
    assert d["summary"][0]["worst"] == "critical"           # worst first
    assert d["changed_lines"] > 0 and {"gap", "chg"} & {r["t"] for r in d["rows"]}
    full = c.get(f"/api/targets/{t['id']}/compare", params={"a": pre, "b": post, "full": True}).json()
    assert "gap" not in {r["t"] for r in full["rows"]} and len(full["rows"]) > len(d["rows"])
    assert not any("ENC " in (r.get("a") or [0, ""])[1] and "<redacted>" not in r["a"][1] for r in full["rows"])
    # clocks and uptimes don't count as changes
    rows = side_by_side_text("up 1d 2h 3m\nA 10:00:00\nx", "up 4d 5h 6m\nA 11:11:11\ny")
    assert [r["t"] for r in rows] == ["same", "same", "chg"]


def test_review_finds_ha_licence_and_log_problems():
    from fortiupgrade import review
    state = mock.new_state("7.4.8", ha="a-p")
    state["unlicensed"] = ["antivirus", "forticare"]
    state["extra_events"] = mock.BAD_EVENTS
    state["ha_cfg"]["session-pickup"] = "disable"
    state["ha_cfg"]["override"] = "enable"
    port, srv = serve(state)
    t = add_target("fgcp", [port], name="cl")
    res = review.review_target(Job("review", "t"), t, "7.6.4", "fortiguard")
    srv.should_exit = True
    ids = {f["id"]: f for f in res["findings"]}
    assert ids["lic-antivirus-av-profile"]["severity"] == "warning" and "policy 1" in ids["lic-antivirus-av-profile"]["evidence"]
    assert "lic-web_filtering-webfilter-profile" not in ids               # web filtering is licensed
    assert ids["lic-forticare"]["severity"] == "critical"                  # FortiGuard download needs FortiCare
    assert {"ha-session-pickup", "ha-override", "ha-hbdev"} <= ids.keys()
    assert "rule-sslvpn-tunnel-removed" in ids                             # 7.6.3 removed SSL VPN tunnel mode
    assert ids["log-conserve"]["severity"] == "critical" and ids["log-crash"]["severity"] == "critical"
    assert "log-ha-hb" in ids and "log-disk" in ids
    assert res["logs"][0]["read"] >= 10
    assert [f["severity"] for f in res["findings"]] == sorted((f["severity"] for f in res["findings"]),
                                                               key=review.SEV_ORDER.get)
    assert res["totals"]["critical"] >= 3


def test_review_clean_standalone_and_parsers():
    from fortiupgrade import review
    state = mock.new_state("7.4.8")
    port, srv = serve(state)
    t = add_target("standalone", [port])
    res = review.review_target(Job("review", "t"), t, "7.4.8", "store")
    srv.should_exit = True
    assert res["totals"]["critical"] == 0 and not any(f["category"] == "HA" for f in res["findings"])
    cfg = 'config firewall policy\n    edit 7\n        set status disable\n        set av-profile "x"\n    next\n    edit 8\n' \
          '        set ips-sensor "y"\n        config x\n            set a b\n        end\n    next\nend\n' \
          'config system password-policy\n    set status enable\n    set expire-status enable\nend\n'
    es = review.entries(cfg, "firewall policy")
    assert [e["_name"] for e in es] == ["7", "8"] and es[1]["ips-sensor"] == '"y"' and "set a b" in es[1]["_sub"]
    assert review.settings(cfg, "system password-policy")["expire-status"] == "enable"
    f = review.review_config(cfg, {"ips": {"status": "expired"}, "antivirus": {"status": "no_license"}}, {}, "FortiGate-60F",
                             "7.4.8", "7.4.8", [], "standalone", "store")
    ids = {x["id"] for x in f}
    assert "lic-ips-ips-sensor" in ids and "lic-antivirus-av-profile" not in ids  # the AV policy is disabled
    assert "cfg-password" in ids
    assert review.config_error_lines('>>> "set foo bar" @ root.firewall.policy.3:command parse error before \'foo\'\nFGT # ') \
        == ['>>> "set foo bar" @ root.firewall.policy.3:command parse error before \'foo\'']
    import datetime as _dt
    d = (_dt.datetime.now() - _dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    crash = f"1: {d} the killed daemon is /bin/ipsengine: status=0x0\n2: 2001-01-01 00:00:00 application wad crashed\n3: {d} firmware upgraded by admin"
    assert len(review.parse_crashlog(crash)) == 1
    la = review.analyse_logs([{"level": "warning", "logdesc": "Link monitor", "msg": "Link monitor: interface port5 was turned down", "date": "2026-11-07", "time": f"10:0{i}:00"} for i in range(4)])
    assert la["groups"][0]["title"].startswith("Interface flapping: port5")


def test_reveal_stored_api_token():
    from fastapi.testclient import TestClient

    from fortiupgrade import server
    t = storage.save_target({"name": "x", "type": "fgcp", "members": [{"host": "10.0.0.1"}, {"host": "10.0.0.2"}]},
                            {"0": {"token": "tok-abc123"}})
    c = TestClient(server.app, headers={"x-auth": server.TOKEN})
    m0, m1 = t["members"]
    r = c.post(f"/api/targets/{t['id']}/members/{m0['id']}/token")
    assert r.status_code == 200 and r.json() == {"token": "tok-abc123"} and r.headers["cache-control"] == "no-store"
    assert c.post(f"/api/targets/{t['id']}/members/{m1['id']}/token").status_code == 404   # none stored
    assert c.post(f"/api/targets/{t['id']}/members/nope/token").status_code == 404
    assert c.get(f"/api/targets/{t['id']}/members/{m0['id']}/token").status_code == 405    # POST only
    assert TestClient(server.app).post(f"/api/targets/{t['id']}/members/{m0['id']}/token").status_code == 401
    assert "tok-abc123" not in c.get("/api/targets").text
