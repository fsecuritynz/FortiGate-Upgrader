"""In-app updates from GitHub Releases.

* check():    asks the GitHub API for the latest release of REPO (at start-up, then every 6 hours
              unless switched off in Settings) and compares its version with this build.
* download(): fetches this platform's asset (macOS .dmg / Windows setup .exe) into the data
              folder and verifies its SHA-256 against the release's SHA256SUMS.txt. An asset
              without a matching checksum is refused.
* install():  macOS - a small helper script waits for the app to quit, copies the new .app out
              of the .dmg over the running one and reopens it.
              Windows - runs the installer silently; it closes the app, upgrades it and reopens it.
              Running from source (not a packaged app): the download is shown instead; use git pull.

Only https://api.github.com and the release's own download URLs are contacted; nothing about
the user or their FortiGates is sent.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

from . import __build__, __version__

REPO = "fsecuritynz/FortiGate-Upgrader"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE = f"https://github.com/{REPO}/releases"
CHECK_EVERY = 6 * 3600
LOG = logging.getLogger("fortiupgrade.updater")

_lock = threading.Lock()
STATE: dict = {"checked_at": None, "error": None, "latest": None, "download": None}


def vtuple(v: str) -> tuple:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def platform_key() -> str | None:
    if sys.platform == "darwin":
        return "macOS-" + ("arm64" if platform.machine() in ("arm64", "aarch64") else "x86_64")
    if sys.platform.startswith("win"):
        return "Windows-x64"
    return None


def pick_asset(assets: list[dict], key: str | None = None) -> dict | None:
    key = key or platform_key()
    if not key:
        return None
    ext = ".dmg" if key.startswith("macOS") else ".exe"
    cands = [a for a in assets if a.get("name", "").endswith(ext)]
    for want in (key, "macOS-universal" if key.startswith("macOS") else None):
        if want:
            hit = next((a for a in cands if want in a["name"]), None)
            if hit:
                return hit
    return None


def _settings() -> dict:
    from . import vault
    s = vault.settings()
    s.setdefault("update_auto", True)
    return s


def set_prefs(auto: bool | None = None, skip: str | None = None):
    from . import vault
    s = vault.settings()
    if auto is not None:
        s["update_auto"] = bool(auto)
    if skip is not None:
        s["update_skip"] = skip
    vault.save_settings(s)


def _client(timeout=20) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True,
                        headers={"User-Agent": f"FortiGate-Upgrader/{__version__}",
                                 "Accept": "application/vnd.github+json"})


def fetch_latest() -> dict:
    with _client() as c:
        r = c.get(API)
    if r.status_code == 404:
        raise RuntimeError("no published release found (or the repository is private)")
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise RuntimeError("GitHub's API rate limit was reached; the app will try again later")
    r.raise_for_status()
    return r.json()


def check(force: bool = False) -> dict:
    """Refresh STATE from GitHub (at most once a minute unless forced) and return status()."""
    if os.environ.get("FORTIUPGRADE_OFFLINE") == "1" and not force:
        return status()
    with _lock:
        if not force and STATE["checked_at"] and time.time() - STATE["checked_at"] < 60:
            return status()
        try:
            rel = fetch_latest()
            asset = pick_asset(rel.get("assets") or [])
            sums = next((a for a in rel.get("assets") or [] if a.get("name") == "SHA256SUMS.txt"), None)
            build = re.search(r"build\s*0*(\d+)", rel.get("name") or "")
            STATE["latest"] = {
                "version": str(rel.get("tag_name") or "").lstrip("v"), "build": int(build.group(1)) if build else None,
                "name": rel.get("name") or rel.get("tag_name"), "notes": rel.get("body") or "",
                "published": rel.get("published_at"), "url": rel.get("html_url") or PAGE,
                "asset": {k: asset[k] for k in ("name", "size", "browser_download_url")} if asset else None,
                "sums_url": sums["browser_download_url"] if sums else None,
            }
            STATE["error"] = None
        except Exception as e:  # noqa: BLE001
            STATE["error"] = str(e)
            LOG.info("update check failed: %s", e)
        STATE["checked_at"] = time.time()
    return status()


def status() -> dict:
    s = _settings()
    lt = STATE["latest"]
    newer = bool(lt and vtuple(lt["version"]) > vtuple(__version__))
    return {"current": {"version": __version__, "build": __build__}, "latest": lt, "available": newer,
            "skipped": bool(newer and s.get("update_skip") == lt["version"]), "auto": s.get("update_auto", True),
            "checked_at": STATE["checked_at"], "error": STATE["error"], "download": STATE["download"],
            "can_install": newer and bool(lt and lt.get("asset")), "packaged": bool(getattr(sys, "frozen", False)),
            "platform": platform_key(), "releases_url": PAGE}


def _updates_dir() -> Path:
    from .storage import data_dir
    d = data_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _expected_sha(sums_text: str, name: str) -> str | None:
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def _download(lt: dict):
    a = lt["asset"]
    dest = _updates_dir() / a["name"]
    part = dest.with_suffix(dest.suffix + ".part")
    d = STATE["download"]
    try:
        if not lt.get("sums_url"):
            raise RuntimeError("the release has no SHA256SUMS.txt, so the download can't be verified")
        with _client(60) as c:
            sums = c.get(lt["sums_url"])
            sums.raise_for_status()
            want = _expected_sha(sums.text, a["name"])
            if not want:
                raise RuntimeError(f"SHA256SUMS.txt has no checksum for {a['name']}")
            h = hashlib.sha256()
            with c.stream("GET", a["browser_download_url"]) as r:
                r.raise_for_status()
                d["total"] = int(r.headers.get("content-length") or a.get("size") or 0)
                with open(part, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
                        h.update(chunk)
                        d["done"] += len(chunk)
        if h.hexdigest() != want:
            part.unlink(missing_ok=True)
            raise RuntimeError("the download's SHA-256 doesn't match the release checksum; it was deleted")
        part.replace(dest)
        d.update(state="ready", path=str(dest), sha256=want)
    except Exception as e:  # noqa: BLE001
        d.update(state="failed", error=str(e))
        LOG.warning("update download failed: %s", e)


def download() -> dict:
    st = check()
    lt = st["latest"]
    if not st["available"] or not (lt and lt.get("asset")):
        raise RuntimeError("no newer release with a download for this platform")
    cur = STATE["download"]
    if cur and cur.get("version") == lt["version"] and cur["state"] in ("downloading", "ready"):
        return status()
    STATE["download"] = {"version": lt["version"], "name": lt["asset"]["name"], "state": "downloading",
                         "done": 0, "total": lt["asset"].get("size") or 0, "error": None, "path": None}
    threading.Thread(target=_download, args=(lt,), daemon=True, name="update-download").start()
    return status()


def current_app_bundle() -> Path | None:
    """…/FortiGate Upgrader.app when running as a packaged macOS app."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == ".app":
            return parent
    return None


MAC_SCRIPT = r'''#!/bin/sh
# FortiGate Upgrader self-update: wait for the app to quit, replace it, reopen it.
PID="$1"; DMG="$2"; APP="$3"
exec >>"$4" 2>&1
echo "$(date) updating $APP from $DMG"
while kill -0 "$PID" 2>/dev/null; do sleep 0.5; done
MNT=$(mktemp -d /tmp/fgu-update.XXXXXX)
hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$MNT" "$DMG" || { open "$APP"; exit 1; }
SRC=$(find "$MNT" -maxdepth 1 -name "*.app" | head -1)
if [ -z "$SRC" ]; then echo "no .app in the disk image"; hdiutil detach "$MNT" -quiet; open "$APP"; exit 1; fi
rm -rf "$APP.update-new" "$APP.update-old"
if ditto "$SRC" "$APP.update-new" && mv "$APP" "$APP.update-old" && mv "$APP.update-new" "$APP"; then
  rm -rf "$APP.update-old"
  echo "updated"
else
  echo "update failed, keeping the current version"
  [ -d "$APP" ] || mv "$APP.update-old" "$APP"
  rm -rf "$APP.update-new"
fi
hdiutil detach "$MNT" -quiet
xattr -dr com.apple.quarantine "$APP" 2>/dev/null
open "$APP"
'''


def install(quit_app=None) -> dict:
    """Start installing the downloaded update. Returns what happens next; quits the app if needed."""
    d = STATE["download"]
    if not d or d.get("state") != "ready" or not d.get("path") or not Path(d["path"]).exists():
        raise RuntimeError("download the update first")
    path = Path(d["path"])
    if sys.platform == "darwin":
        app = current_app_bundle()
        if not app:
            subprocess.Popen(["open", str(path)])
            return {"action": "opened", "message": "Running from source: the disk image was opened. "
                    "Update the source with git pull, or drag the new app to Applications."}
        if not os.access(app.parent, os.W_OK):
            subprocess.Popen(["open", str(path)])
            return {"action": "opened", "message": f"No permission to replace {app}. The disk image was opened: "
                    "drag the new app over the old one."}
        script = Path(tempfile.gettempdir()) / "fortigate-upgrader-update.sh"
        script.write_text(MAC_SCRIPT)
        log = _updates_dir() / "update.log"
        subprocess.Popen(["/bin/sh", str(script), str(os.getpid()), str(path), str(app), str(log)],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    elif sys.platform.startswith("win"):
        if not getattr(sys, "frozen", False):
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
            return {"action": "opened", "message": "Running from source: the installer was started."}
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                         creationflags=flags, close_fds=True)
    else:
        raise RuntimeError("automatic install is only supported on macOS and Windows")
    threading.Timer(1.0, quit_app or (lambda: os._exit(0))).start()
    return {"action": "restarting", "message": "Installing the update. The app will close and reopen in a few seconds."}


def start_background_checks():
    """Check shortly after start-up, then every CHECK_EVERY seconds while automatic checks are on."""
    if os.environ.get("FORTIUPGRADE_OFFLINE") == "1":
        return

    def loop():
        time.sleep(8)
        while True:
            try:
                if _settings().get("update_auto", True):
                    check(force=True)
            except Exception:  # noqa: BLE001
                LOG.exception("background update check")
            time.sleep(CHECK_EVERY)

    threading.Thread(target=loop, daemon=True, name="update-check").start()
