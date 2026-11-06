"""Local cache of FortiOS release notes downloaded from the Fortinet document library.

<notes_dir>/<version>.txt          full text (also used by the pre-flight scan)
<notes_dir>/<version>.known.json   parsed Known issues table + source URL
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading

from . import release_notes, storage
from .version import FosVersion

log = logging.getLogger(__name__)
_lock = threading.Lock()


def _paths(version: str):
    v = str(FosVersion.parse(version))
    d = storage.notes_dir()
    return d / f"{v}.txt", d / f"{v}.known.json"


def cached(version: str) -> dict | None:
    txt, kj = _paths(version)
    if kj.exists():
        return json.loads(kj.read_text())
    return None


def get(version: str, fetch: bool = True, refresh: bool = False) -> dict | None:
    """Known issues + metadata for `version`, downloading the notes when missing."""
    with _lock:
        have = None if refresh else cached(version)
        if have or not fetch:
            return have
        res = release_notes.fetch_fortinet_notes(version)
        txt, kj = _paths(version)
        txt.write_text(res["text"])
        meta = {"version": str(FosVersion.parse(version)), "url": res["url"],
                "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                "known_issues": res["known_issues"], "chars": len(res["text"])}
        kj.write_text(json.dumps(meta, indent=1))
        return meta


def ensure_text(version: str) -> bool:
    """Make sure the full notes text for `version` is cached. Returns False when offline."""
    txt, _ = _paths(version)
    if txt.exists() and cached(version):
        return True
    if os.environ.get("FORTIUPGRADE_OFFLINE"):
        return txt.exists()
    try:
        get(version, refresh=True)
        return True
    except Exception as e:  # noqa: BLE001
        log.info("release notes %s unavailable: %s", version, e)
        return txt.exists()
