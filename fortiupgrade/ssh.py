"""Optional SSH access for CLI-only data (TAC report, BGP advertised routes, HA checksums).

The REST API cannot run arbitrary CLI, so these checks need SSH credentials.
They are skipped when no SSH credentials are configured for a device.
"""
from __future__ import annotations

import re
import time

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None

# "fgt #", "fgt (root) #", and in an HA cluster "fgt(Primary) #" / "fgt(Secondary) (root) #"
PROMPT_RX = rb"[\w\-\.]+(\([\w\-\.]+\))?( \([\w\-\.]+\))? [#$] ?$"
_PROMPT = re.compile(PROMPT_RX)
LOGIN_SCREENS = (("new password", "forced password change (password policy or expired password)"),
                 ("password has expired", "forced password change (expired password)"),
                 ("(press 'a' to accept)", "post-login disclaimer banner"),
                 ("press 'a' to accept", "post-login disclaimer banner"),
                 ("do you want to continue", "confirmation question"))
_MORE = b"--More--"


class FortiSSH:
    def __init__(self, host: str, username: str, password: str | None = None, port: int = 22,
                 key_filename: str | None = None, timeout: float = 20):
        if paramiko is None:
            raise RuntimeError("paramiko is not installed")
        self.host = host
        self.cli = paramiko.SSHClient()
        self.cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.cli.connect(host, port=port, username=username, password=password,
                         key_filename=key_filename, timeout=timeout, look_for_keys=False,
                         allow_agent=False)
        self.chan = self.cli.invoke_shell(width=511, height=10000)
        first = self._read_until_prompt(10, quiet=4)
        low = first.lower()
        for pat, why in LOGIN_SCREENS:
            if pat in low:
                self.close()
                raise RuntimeError(f"SSH login as {username} stopped at a {why} instead of the CLI prompt. "
                                   "Log in once interactively to clear it, then retry.")
        if not _PROMPT.search(first.rstrip("\n ").encode() + b" "):
            self.close()
            raise RuntimeError(f"SSH login as {username} did not reach the CLI prompt within 10 s "
                               f"(last output: {first.strip()[-120:]!r})")
        self._send("config system console\nset output standard\nend", timeout=15)

    def close(self):
        try:
            self.cli.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _send(self, text: str, timeout: float = 60) -> str:
        out = []
        for line in text.splitlines():
            self.chan.send(line + "\n")
            out.append(self._read_until_prompt(timeout))
        return "".join(out)

    def _read_until_prompt(self, timeout: float, quiet: float = 30) -> str:
        buf = b""
        end = time.time() + timeout
        quiet_since = time.time()
        while time.time() < end:
            if self.chan.recv_ready():
                chunk = self.chan.recv(65535)
                buf += chunk
                quiet_since = time.time()
                if _MORE in chunk:
                    self.chan.send(" ")
                if _PROMPT.search(buf.rstrip(b"\r\n ") + b" ") or _PROMPT.search(buf):
                    # give the device a moment in case output continues
                    time.sleep(0.3)
                    if not self.chan.recv_ready():
                        break
            else:
                if buf and time.time() - quiet_since > quiet:
                    break
                time.sleep(0.1)
        return buf.decode(errors="replace").replace("\r", "")

    trace = None  # optional callable(str)

    def _wait_for(self, pattern: bytes, timeout: float) -> str:
        buf, end = b"", time.time() + timeout
        rx = re.compile(pattern, re.I)
        while time.time() < end:
            if self.chan.recv_ready():
                buf += self.chan.recv(65535)
                if rx.search(buf):
                    return buf.decode(errors="replace")
            else:
                time.sleep(0.1)
        raise TimeoutError(f"no {pattern!r} within {timeout:.0f} s (got {buf.decode(errors='replace')[-120:]!r})")

    def ha_peer_show(self, username: str, password: str | None, commands: list[str], timeout: float = 25) -> dict:
        """Read-only: log in to each other cluster unit through the HA link (`execute ha manage`),
        run `show` commands there and come back. Returns {serial: {command: output}}."""
        from .hainfo import parse_manage_list
        units = parse_manage_list(self._send("execute ha manage ?", 10))
        self._send("", 5)  # clear the half-typed command FortiOS leaves after '?'
        out = {}
        for u in units:
            t0 = time.time()
            self.chan.send(f"execute ha manage {u['index']} {username}\n")
            got = self._wait_for(rb"password:|\(y/n\)|" + PROMPT_RX, timeout)
            if "(y/n)" in got.lower():
                self.chan.send("y\n")
                got = self._wait_for(rb"password:|" + PROMPT_RX, timeout)
            if "password:" in got.lower():
                self.chan.send((password or "") + "\n")
                got = self._wait_for(PROMPT_RX + rb"|denied|failed", timeout)
                if re.search(r"denied|failed", got, re.I):
                    raise RuntimeError(f"login to cluster unit {u['serial']} as {username} was refused")
            res = {}
            try:
                for c in commands:
                    res[c] = self._send(c, timeout)
            finally:
                self._send("exit", 10)
            out[u["serial"]] = res
            if self.trace:
                self.trace(f"SSH {self.host}: execute ha manage {u['index']} ({u['serial']}) → "
                           f"{', '.join(commands)} · {(time.time() - t0) * 1000:.0f} ms")
        return out

    def run(self, command: str, vdom: str | None = None, global_ctx: bool = False,
            vdom_mode: bool = False, timeout: float = 120) -> str:
        import time as _t
        t0 = _t.time()
        out = self._run(command, vdom, global_ctx, vdom_mode, timeout)
        if self.trace:
            self.trace(f"SSH {self.host}: {command}{f' (vdom {vdom})' if vdom else ''} → "
                       f"{len(out.splitlines())} lines · {(_t.time() - t0) * 1000:.0f} ms")
        return out

    def _run(self, command: str, vdom: str | None = None, global_ctx: bool = False,
             vdom_mode: bool = False, timeout: float = 120) -> str:
        if vdom_mode and (vdom or global_ctx):
            ctx = "config global" if global_ctx else f"config vdom\nedit {vdom}"
            self._send(ctx, 10)
            try:
                return self._send(command, timeout)
            finally:
                self._send("end", 10)
        return self._send(command, timeout)
