"""Detect central management (FortiManager / FortiGate Cloud) and explain the upgrade risks."""
from __future__ import annotations

import re

from .client import FortiAPIError, FortiGateClient

ADVICE = [
    "Confirm the FortiManager and the device's ADOM support the target FortiOS version. Upgrade FortiManager and "
    "the ADOM first if needed; an ADOM older than the device can block installs.",
    "Install or discard pending policy-package and device-settings changes in FortiManager before upgrading.",
    "Check for FortiManager firmware templates or scheduled upgrades for this device; they can conflict with, "
    "or revert, an upgrade done outside FortiManager. Disable or align them first.",
    "After the upgrade FortiManager will see the device as modified / out of sync. Run Retrieve Config (or "
    "refresh the device) in Device Manager before the next install, or FortiManager may overwrite settings "
    "that changed during the upgrade.",
    "Preferably run the upgrade from FortiManager (Device Manager > Firmware) so its records stay consistent.",
]


def _block(config: str, name: str) -> str:
    m = re.search(rf"^config {re.escape(name)}\n(.*?)^end", config or "", re.M | re.S)
    return m.group(1) if m else ""


def _set(block: str, key: str) -> str | None:
    m = re.search(rf"^\s*set {re.escape(key)} (.+)$", block, re.M)
    return m.group(1).strip().strip('"') if m else None


def from_cmdb(cm: dict | None) -> dict:
    cm = cm or {}
    typ = cm.get("type") or "none"
    return _result(typ, cm.get("fmg") or "", cm.get("mode") or "normal", cm.get("serial-number") or "")


def from_config(config: str) -> dict:
    b = _block(config, "system central-management")
    if not b:
        return _result("none", "", "normal", "")
    return _result(_set(b, "type") or "fortimanager" if _set(b, "fmg") else (_set(b, "type") or "none"),
                   _set(b, "fmg") or "", _set(b, "mode") or "normal", _set(b, "serial-number") or "")


def _result(typ, fmg, mode, serial) -> dict:
    fmg = fmg.strip('"')
    managed = typ == "fortimanager" and bool(fmg)
    # 'fortiguard' is the FortiOS default, so it does not prove FortiGate Cloud is actually in use.
    cloud = False
    if managed:
        summary = f"FortiManager {fmg}" + (" (backup mode)" if mode == "backup" else "")
    elif cloud:
        summary = "FortiGate Cloud"
    else:
        summary = "Local (not managed by FortiManager)"
    return {"type": typ, "fmg": fmg, "mode": mode, "serial": serial, "managed": managed, "cloud": cloud,
            "summary": summary}


def live_status(client: FortiGateClient) -> str | None:
    try:
        st = client.monitor("system/central-management/status") or {}
        return st.get("status") or st.get("connection_status")
    except FortiAPIError:
        return None


def findings(mgmt: dict) -> list[dict]:
    if mgmt.get("managed"):
        return [{"severity": "warning",
                 "message": f"Managed by {mgmt['summary']}. Upgrading outside FortiManager: " + a}
                for a in ADVICE]
    if mgmt.get("cloud"):
        return [{"severity": "info", "message": "Managed by FortiGate Cloud. Check for scheduled firmware upgrades "
                                                "there before upgrading manually."}]
    return []
