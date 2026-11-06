"""Firmware file store: local FortiOS images tagged with Version and Hardware."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import threading
import uuid
from pathlib import Path

from . import storage
from .version import FosVersion

_lock = threading.Lock()
# FGT_60F-v7.4.8.F-build2795-FORTINET.out, FWF_40F_3G4G-v7.2.11.M-build1740-FORTINET.out, FGT_VM64-v7.6.4...
_NAME_RE = re.compile(r"^(?P<hw>[A-Z]{2,4}_[A-Z0-9_]+?)-v(?P<maj>\d+)\.(?P<min>\d+)\.(?P<pat>\d+)"
                      r"(?:\.[A-Z])?-build(?P<build>\d+)", re.I)


def _dir() -> Path:
    p = storage.data_dir() / "firmware"
    p.mkdir(exist_ok=True)
    return p


def _index() -> Path:
    return _dir() / "index.json"


# Image file prefixes. Only FGT/FWF/FGR images install on FortiGate-family devices.
PRODUCTS = {"FGT": "FortiGate", "FWF": "FortiWiFi", "FGR": "FortiGate Rugged", "FFW": "FortiFirewall",
            "FPX": "FortiProxy", "FAZ": "FortiAnalyzer", "FMG": "FortiManager", "FSW": "FortiSwitch",
            "FAP": "FortiAP", "FWB": "FortiWeb", "FAD": "FortiADC", "FML": "FortiMail"}
MODEL_PREFIX = {"FORTIGATERUGGED": "FGR", "FORTIGATE": "FGT", "FORTIWIFI": "FWF", "FORTIFIREWALL": "FFW"}


def image_hw_for(model: str) -> str:
    """Hardware part of the image file name for a device model, e.g.
    'FortiGate-VM64-KVM' -> 'FGT_VM64_KVM', 'FortiGate-40F-3G4G' -> 'FGT_40F_3G4G', 'FortiWiFi-60F' -> 'FWF_60F'."""
    m = re.match(r"^\s*(Forti[A-Za-z]+)[- ]?(.*)$", model or "")
    if not m:
        return ""
    prefix = MODEL_PREFIX.get(m.group(1).upper().replace(" ", ""))
    if not prefix:
        return ""
    rest = re.sub(r"[^A-Z0-9]+", "_", m.group(2).upper()).strip("_")
    return f"{prefix}_{rest}" if rest else prefix


def product_of(hw: str) -> str:
    return PRODUCTS.get((hw or "").split("_")[0].upper(), "")


def hw_key(s: str) -> str:
    """Normalise hardware names so 'FGT_60F', 'FGT60F' and 'FortiGate-60F' compare equal."""
    s = (s or "").upper().replace("FORTIGATE-", "FGT").replace("FORTIWIFI-", "FWF")
    s = re.sub(r"[^A-Z0-9]", "", s)
    return s.replace("FGVM", "FGTVM")


def parse_name(filename: str) -> dict:
    m = _NAME_RE.search(Path(filename).name)
    if not m:
        v = re.search(r"v(\d+)\.(\d+)\.(\d+)", filename)
        b = re.search(r"build(\d+)", filename, re.I)
        return {"version": ".".join(v.groups()) if v else "", "build": int(b.group(1)) if b else None,
                "hardware": []}
    return {"version": f"{m['maj']}.{m['min']}.{m['pat']}", "build": int(m["build"]),
            "hardware": [m["hw"].upper()], "product": product_of(m["hw"])}


def list_images() -> list[dict]:
    f = _index()
    items = json.loads(f.read_text()) if f.exists() else []
    for i in items:
        i["product"] = ", ".join(sorted({product_of(h) for h in i.get("hardware", []) if product_of(h)}))
    return sorted(items, key=lambda i: (FosVersion.parse(i["version"]) if i.get("version") else FosVersion(0, 0, 0),
                                        i.get("filename")), reverse=True)


def _save(items):
    _index().write_text(json.dumps(items, indent=1))


def get(image_id: str) -> dict:
    for i in list_images():
        if i["id"] == image_id:
            return i
    raise KeyError(image_id)


def add(src: Path, filename: str | None = None, move: bool = False) -> dict:
    filename = Path(filename or src.name).name
    h = hashlib.sha256()
    with open(src, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    with _lock:
        items = list_images()
        dup = next((i for i in items if i["sha256"] == sha), None)
        if dup:
            return dup
        iid = uuid.uuid4().hex[:10]
        dest = _dir() / f"{iid}-{filename}"
        (shutil.move if move else shutil.copyfile)(str(src), dest)
        meta = parse_name(filename)
        item = {"id": iid, "filename": filename, "path": str(dest), "size": dest.stat().st_size, "sha256": sha,
                "version": meta["version"], "build": meta["build"], "hardware": meta["hardware"], "tags": [],
                "added": dt.datetime.now().isoformat(timespec="seconds"), "notes": ""}
        items.append(item)
        _save(items)
        return item


def update(image_id: str, changes: dict) -> dict:
    with _lock:
        items = list_images()
        for i in items:
            if i["id"] == image_id:
                if "version" in changes:
                    i["version"] = str(FosVersion.parse(changes["version"])) if changes["version"] else ""
                if "build" in changes:
                    i["build"] = int(changes["build"]) if changes["build"] not in (None, "") else None
                if "hardware" in changes:
                    i["hardware"] = sorted({h.strip().upper() for h in changes["hardware"] if h.strip()})
                if "tags" in changes:
                    i["tags"] = sorted({t.strip() for t in changes["tags"] if t.strip()})
                if "notes" in changes:
                    i["notes"] = str(changes["notes"])[:500]
                _save(items)
                return i
    raise KeyError(image_id)


def delete(image_id: str):
    with _lock:
        items = list_images()
        keep = [i for i in items if i["id"] != image_id]
        for i in items:
            if i["id"] == image_id:
                Path(i["path"]).unlink(missing_ok=True)
        _save(keep)


def matches(item: dict, model: str = "", code: str = "") -> bool:
    """True when the image is built for this device (model name such as 'FortiGate-VM64-KVM',
    platform code such as 'FGVMK6' or 'FGT60F' as a fallback)."""
    want = {k for k in (hw_key(image_hw_for(model)), hw_key(code) if code else "") if k}
    return any(hw_key(h) in want for h in item.get("hardware", []))


def find(version: str, model: str, code: str = "") -> dict | None:
    """Best image for a FortiOS version and device. `model` may be a model name
    ('FortiGate-90G') or, for older callers, a platform code ('FGT90G')."""
    want_v = FosVersion.parse(version)
    if not image_hw_for(model) and not code:
        model, code = "", model
    for i in list_images():
        if not i.get("version") or FosVersion.parse(i["version"]) != want_v:
            continue
        if matches(i, model, code):
            return i
    return None


def explain(version: str, model: str, code: str = "") -> dict:
    """Which image a device needs, and why images of the same version in the store do not fit."""
    want_hw = image_hw_for(model)
    want_v = FosVersion.parse(version)
    img = find(version, model, code)
    near = []
    for i in list_images():
        if img or not i.get("version") or FosVersion.parse(i["version"]) != want_v:
            continue
        for h in i.get("hardware", []):
            prod, want_prod = product_of(h), product_of(want_hw)
            same_hw = h.split("_", 1)[-1] == want_hw.split("_", 1)[-1]
            if same_hw and prod != want_prod:
                why = (f"{i['filename']} is a {prod or 'different product'} image; a {want_prod} needs "
                       f"{want_hw} (the {prod or h.split('_')[0]} and {want_prod} images are not interchangeable)")
            else:
                why = f"{i['filename']} is for {h}, not {want_hw}"
            near.append({"filename": i["filename"], "hardware": h, "reason": why, "wrong_product": same_hw})
    near.sort(key=lambda n: not n["wrong_product"])
    return {"image": img, "expected_hw": want_hw,
            "expected_name": f"{want_hw}-v{want_v}.?-build{'####'}-FORTINET.out" if want_hw else "",
            "near": near}
