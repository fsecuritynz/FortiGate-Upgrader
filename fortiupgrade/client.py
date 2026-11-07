"""Thin FortiOS REST API client.

All endpoint paths used by the app live in this module or are passed in
explicitly, so they are easy to audit against the FortiOS REST API reference
for the version you run (Fortinet Developer Network: fndn.fortinet.net).
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .version import FosVersion

log = logging.getLogger(__name__)


def looks_like_config(text: str) -> bool:
    head = (text or "").lstrip()[:4000]
    return head.startswith("#config-version") or "\nconfig system global" in "\n" + head or head.startswith("config ")


BACKUP_ERRORS = {
    "config_backup_protection_required": "FortiOS requires backups to be password-protected "
                                         "(encrypted), so a readable copy cannot be taken over the REST API",
}


def _config_or_raise(r) -> str:
    """The backup endpoint answers HTTP 200 with a JSON error body when it refuses; never treat
    that as configuration text."""
    text = r.text
    if looks_like_config(text):
        return text
    err = None
    try:
        body = r.json()
        res = body.get("results") if isinstance(body, dict) else None
        err = (res or {}).get("error") if isinstance(res, dict) else None
        err = err or (body.get("error") if isinstance(body, dict) else None)
    except ValueError:
        pass
    msg = BACKUP_ERRORS.get(str(err), f"the FortiGate returned {str(err or text[:120])!r} instead of a configuration")
    raise FortiAPIError(f"Configuration backup refused: {msg}", r.status_code, text[:2000])


# connection-level failures that are usually momentary (TLS handshake refused/reset by a busy
# management daemon, a dropped keep-alive). Timeouts on reads are not retried: the request may
# still be running on the device.
TRANSIENT = (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError, httpx.ConnectTimeout)
RETRY_DELAYS = (2, 5, 10)


class _Counting:
    """File wrapper that reports how much of it has been read (i.e. sent)."""

    def __init__(self, fh, cb):
        self._fh, self._cb, self.sent = fh, cb, 0

    def read(self, n=-1):
        b = self._fh.read(n)
        self.sent += len(b)
        self._cb(self.sent)
        return b

    def __iter__(self):
        while True:
            b = self.read(65536)
            if not b:
                return
            yield b

    def __getattr__(self, name):
        return getattr(self._fh, name)


class FortiAPIError(Exception):
    def __init__(self, msg: str, status: int | None = None, body: Any = None):
        super().__init__(msg)
        self.status = status
        self.body = body


@dataclass
class DeviceInfo:
    hostname: str
    serial: str
    model: str
    version: FosVersion
    vdom_mode: bool
    vdoms: list[str] = field(default_factory=list)
    ha_mode: str = "standalone"  # standalone | a-p | a-a
    fgsp: bool = False
    model_code: str = ""
    management: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "hostname": self.hostname,
            "serial": self.serial,
            "model": self.model,
            "version": str(self.version),
            "build": self.version.build,
            "vdom_mode": self.vdom_mode,
            "vdoms": self.vdoms,
            "ha_mode": self.ha_mode,
            "fgsp": self.fgsp,
            "model_code": self.model_code,
            "management": self.management,
        }


class FortiGateClient:
    def __init__(
        self,
        host: str,
        token: str,
        port: int = 443,
        verify_ssl: bool = False,
        scheme: str = "https",
        timeout: float = 30.0,
    ):
        self.host = host
        self.port = port
        self._args = (host, token, port, verify_ssl, scheme)
        self.trace = None  # optional callable(str) receiving one line per API call
        self.base = f"{scheme}://{host}:{port}"
        self._http = httpx.Client(
            base_url=self.base,
            verify=verify_ssl,
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        self._mk = lambda: httpx.Client(base_url=self.base, verify=verify_ssl, timeout=timeout,
                                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})

    retries = 2  # extra attempts for idempotent GETs on transient connection errors

    def reconnect(self):
        """Drop pooled connections (e.g. after the device closed one mid-upload)."""
        try:
            self._http.close()
        finally:
            self._http = self._mk()

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ------------------------------------------------------------------ core
    def request(
        self,
        method: str,
        path: str,
        *,
        vdom: str | None = None,
        params: dict | None = None,
        json: Any = None,
        timeout: float | None = None,
        raw: bool = False,
        files: Any = None,
        data: Any = None,
    ):
        params = dict(params or {})
        if vdom:
            params["vdom"] = vdom
        url = path if path.startswith("/") else f"/api/v2/{path}"
        t0 = time.time()
        qs = "&".join(f"{k}={v}" for k, v in params.items() if k != "access_token")
        attempts = 1 + (self.retries if method == "GET" and files is None else 0)
        for attempt in range(1, attempts + 1):
            try:
                r = self._http.request(method, url, params=params, json=json, files=files, data=data,
                                       timeout=timeout or self._http.timeout)
                break
            except httpx.HTTPError as e:
                transient = isinstance(e, TRANSIENT)
                if self.trace:
                    self.trace(f"{method} {url}{'?' + qs if qs else ''} → {type(e).__name__}: {e} "
                               f"({(time.time() - t0) * 1000:.0f} ms)"
                               + (f"; retrying in {RETRY_DELAYS[attempt - 1]} s (attempt {attempt + 1} of {attempts})"
                                  if transient and attempt < attempts else ""))
                if not transient or attempt >= attempts:
                    raise FortiAPIError(f"{method} {url}: {e}"
                                        + (f" (failed {attempts} times)" if transient and attempts > 1 else "")) from e
                time.sleep(RETRY_DELAYS[attempt - 1])
                self.reconnect()
                t0 = time.time()
        if self.trace:
            size = len(r.content)
            self.trace(f"{method} {url}{'?' + qs if qs else ''} → HTTP {r.status_code} · "
                       f"{(time.time() - t0) * 1000:.0f} ms · "
                       f"{size / 1024:.1f} KB" if size >= 1024 else
                       f"{method} {url}{'?' + qs if qs else ''} → HTTP {r.status_code} · "
                       f"{(time.time() - t0) * 1000:.0f} ms · {size} B")
        if r.status_code == 401:
            raise FortiAPIError("Authentication failed (401). Check the API token and trusted hosts.", 401)
        if r.status_code == 403:
            raise FortiAPIError(f"Permission denied (403) for {url}. Check the API admin profile.", 403)
        if r.status_code >= 400:
            raise FortiAPIError(f"{method} {url} -> HTTP {r.status_code}", r.status_code, r.text[:2000])
        if raw:
            return r
        try:
            body = r.json()
        except ValueError:
            return r.text
        if isinstance(body, dict) and body.get("status") == "error":
            raise FortiAPIError(f"{method} {url} -> error {body.get('error')}", r.status_code, body)
        return body

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def results(self, path, **kw):
        body = self.get(path, **kw)
        return body.get("results") if isinstance(body, dict) else body

    def monitor(self, path, vdom=None, params=None):
        return self.results(f"monitor/{path}", vdom=vdom, params=params)

    def cmdb(self, path, vdom=None, params=None):
        return self.results(f"cmdb/{path}", vdom=vdom, params=params)

    # ------------------------------------------------------------- discovery
    def status(self) -> dict:
        body = self.get("monitor/system/status")
        res = body.get("results", {}) or {}
        return {
            "hostname": res.get("hostname", ""),
            "model": (res.get("model_name", "") + (("-" + res["model_number"]) if res.get("model_number") else ""))
            or res.get("model", ""),
            "model_code": res.get("model", ""),
            "serial": body.get("serial", ""),
            "version": body.get("version", ""),
            "build": body.get("build"),
        }

    def discover(self) -> DeviceInfo:
        st = self.status()
        ver = FosVersion.parse(st["version"])
        ver = FosVersion(ver.major, ver.minor, ver.patch, st.get("build"))
        g = self.cmdb("system/global") or {}
        vdom_mode = g.get("vdom-mode", "no-vdom") != "no-vdom"
        vdoms = ["root"]
        if vdom_mode:
            try:
                vdoms = [v["name"] for v in self.cmdb("system/vdom") or []]
            except FortiAPIError:
                pass
        ha = self.cmdb("system/ha") or {}
        fgsp = False
        try:
            sc = self.cmdb("system/standalone-cluster") or {}
            fgsp = bool(sc.get("cluster-peer")) or int(sc.get("standalone-group-id", 0) or 0) > 0
        except FortiAPIError:
            pass
        if not fgsp:
            try:
                fgsp = bool(self.cmdb("system/cluster-sync"))
            except FortiAPIError:
                pass
        from . import fmg
        try:
            mgmt = fmg.from_cmdb(self.cmdb("system/central-management"))
        except FortiAPIError:
            mgmt = fmg.from_cmdb(None)
        return DeviceInfo(
            hostname=st["hostname"], serial=st["serial"], model=st["model"], version=ver,
            vdom_mode=vdom_mode, vdoms=vdoms, ha_mode=ha.get("mode", "standalone"), fgsp=fgsp,
            model_code=st.get("model_code", ""), management=mgmt,
        )

    # -------------------------------------------------------------- firmware
    def firmware(self) -> dict:
        """Current + FortiGuard-available images for this model."""
        return self.monitor("system/firmware") or {}

    def upgrade_paths(self):
        """FortiGuard upgrade path data (requires FortiGuard reachability)."""
        return self.monitor("system/firmware/upgrade-paths")

    def upgrade_from_file(self, image: bytes, filename: str, timeout: float = 900):
        """Upload as base64 inside JSON (documented REST form; ~33% larger on the wire)."""
        payload = {
            "source": "upload",
            "filename": filename,
            "file_content": base64.b64encode(image).decode(),
        }
        return self.request("POST", "monitor/system/firmware/upgrade", json=payload, timeout=timeout)

    def clone(self, timeout: float = 10.0) -> "FortiGateClient":
        """A separate client to the same address (used to watch the device while a long request runs)."""
        host, token, port, verify, scheme = self._args
        c = FortiGateClient(host, token, port=port, verify_ssl=verify, scheme=scheme, timeout=timeout)
        c.retries = 0
        return c

    def upgrade_from_file_multipart(self, path, filename: str, timeout: float = 1800, progress=None):
        """Upload the image as a multipart file, the way the FortiOS GUI does (raw bytes, streamed
        from disk, no base64 overhead). progress(bytes_sent) is called as the file is read."""
        with open(path, "rb") as fh:
            body = _Counting(fh, progress) if progress else fh
            return self.request("POST", "monitor/system/firmware/upgrade", timeout=timeout,
                                data={"source": "upload", "filename": filename},
                                files={"file": (filename, body, "application/octet-stream")})

    def upgrade_from_fortiguard(self, image_id: str, timeout: float = 900):
        return self.request("POST", "monitor/system/firmware/upgrade",
                            json={"source": "fortiguard", "filename": image_id}, timeout=timeout)

    # ---------------------------------------------------------------- config
    def backup_config(self, scope: str = "global", vdom: str | None = None) -> str:
        params = {"scope": scope}
        if vdom:
            params["vdom"] = vdom
        try:  # FortiOS <= 7.4 style
            r = self.request("GET", "monitor/system/config/backup", params=params, raw=True, timeout=120)
            return _config_or_raise(r)
        except FortiAPIError as e:
            if e.status not in (404, 405):
                raise
        # FortiOS 7.6+ moved backup to POST
        body = {"destination": "file", "scope": scope}
        if vdom:
            body["vdom"] = vdom
        r = self.request("POST", "monitor/system/config/backup", json=body, raw=True, timeout=120)
        return _config_or_raise(r)

    def set_ha(self, settings: dict):
        return self.request("PUT", "cmdb/system/ha", json=settings)

    # ---------------------------------------------------------------- health
    def wait_until_up(self, timeout: float = 1500, interval: float = 15, expect_version: str | None = None,
                      on_tick=None, initial_delay: float = 60) -> dict:
        start = time.time()
        deadline = start + timeout
        self.retries = 0  # the device is expected to be unreachable while it reboots; we poll ourselves
        el = lambda: f"{int(time.time() - start) // 60}m{int(time.time() - start) % 60:02d}s"  # noqa: E731
        while initial_delay and time.time() - start < initial_delay:
            if on_tick:
                on_tick(f"[{el()}] rebooting: first status check in {int(initial_delay - (time.time() - start))} s")
            time.sleep(min(interval, max(0.5, initial_delay - (time.time() - start))))
        last_err = None
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                st = self.status()
                if expect_version is None or FosVersion.parse(st["version"]) == FosVersion.parse(expect_version):
                    return st
                last_err = f"reachable, still running {st['version']} build {st.get('build')}"
            except Exception as e:  # noqa: BLE001
                last_err = f"not answering yet ({str(e).split(':')[-1].strip() or type(e).__name__})"
            if on_tick:
                on_tick(f"[{el()}] check #{attempt}: {last_err}; next check in {int(interval)} s")
            time.sleep(interval)
        raise TimeoutError(f"{self.host} did not come back on {expect_version or 'any version'}: {last_err}")
