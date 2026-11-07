"""Synthetic service probes run from this computer during an upgrade.

Each probe (rapid ICMP ping, TCP connect or HTTP(S) request) runs on its own thread at a fixed
interval. Results are kept compactly: 5-second buckets for the timeline plus exact failure windows,
so an upgrade that waits hours for approval still costs only a few kilobytes. A failure window that
lasts at least ``min_outage_ms`` (and spans 2+ failed probes) is an *outage*; shorter ones are packet
loss. Windows are attributed to the upgrade event (stage, reboot, failover) that preceded them.
"""
from __future__ import annotations

import ipaddress
import os
import platform
import re
import socket
import struct
import subprocess
import threading
import time
from collections import Counter
from urllib.parse import urlparse

BUCKET_MS = 5000
MAX_PROBES = 16
DEFAULT_INTERVAL = {"ping": 200, "tcp": 1000, "http": 2000}
# every HTTP(S) probe is a full new TCP + TLS session; faster rates load the target's web/admin
# daemon (a FortiGate's httpsd) and can make the management plane refuse the upgrade's own API calls
MIN_INTERVAL = {"ping": 100, "tcp": 250, "http": 1000}
MARKER_RE = re.compile(r"image accepted|back online|HA cluster in sync|did not report in sync|fails over", re.I)
STAGE_RE = re.compile(r"Transferring|Installing|HA|Restoring|Post-upgrade", re.I)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ------------------------------------------------------------------ spec handling
def normalise(spec: dict, idx: int = 0) -> dict:
    """Validate one probe definition from the UI/API and fill in defaults."""
    kind = str(spec.get("type") or "ping").lower()
    if kind not in DEFAULT_INTERVAL:
        raise ValueError(f"unknown probe type {kind!r} (ping, tcp or http)")
    out = {"id": spec.get("id") or f"p{idx + 1}", "type": kind}
    if kind == "http":
        url = str(spec.get("url") or spec.get("target") or "").strip()
        if url and "://" not in url:
            url = "https://" + url
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"probe {idx + 1}: enter a URL such as https://intranet.example.com/")
        out.update(url=url, host=u.hostname, verify_tls=bool(spec.get("verify_tls", False)),
                   expect=str(spec.get("expect") or "").strip())
        name = url
    else:
        host = str(spec.get("target") or spec.get("host") or "").strip()
        if not host or " " in host:
            raise ValueError(f"probe {idx + 1}: enter an IP address or hostname")
        out["host"] = host
        name = host
        if kind == "tcp":
            try:
                port = int(spec.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if not 0 < port < 65536:
                raise ValueError(f"probe {idx + 1}: enter a TCP port (1-65535)")
            out["port"] = port
            name = f"{host}:{port}"
    iv = int(spec.get("interval_ms") or DEFAULT_INTERVAL[kind])
    out["interval_ms"] = max(MIN_INTERVAL[kind], min(iv, 60000))
    out["timeout_ms"] = max(100, min(int(spec.get("timeout_ms") or 1000), 10000))
    out["name"] = str(spec.get("name") or "").strip() or f"{ {'ping': 'Ping', 'tcp': 'TCP', 'http': 'HTTP'}[kind]} {name}"
    return out


def normalise_all(specs: list[dict]) -> list[dict]:
    specs = [s for s in (specs or []) if s and (s.get("target") or s.get("host") or s.get("url"))]
    if len(specs) > MAX_PROBES:
        raise ValueError(f"at most {MAX_PROBES} probes")
    out = [normalise(s, i) for i, s in enumerate(specs)]
    seen = Counter(p["id"] for p in out)
    if any(n > 1 for n in seen.values()):
        for i, p in enumerate(out):
            p["id"] = f"p{i + 1}"
    return out


# ------------------------------------------------------------------ probe primitives
def _checksum(b: bytes) -> int:
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f"!{len(b) // 2}H", b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


class Pinger:
    """ICMP echo without root: an unprivileged datagram ICMP socket (macOS, most Linux), falling
    back to the system ping command (Windows, IPv6, locked-down Linux)."""

    def __init__(self, host: str, timeout_ms: int):
        self.host = host
        self.timeout = timeout_ms / 1000
        self.seq = 0
        self.ident = os.getpid() & 0xFFFF
        self.sock = None
        self.addr = None
        self.method = "command"
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET)
            self.addr = infos[0][4][0]
        except (socket.gaierror, IndexError):
            self.addr = None
        if self.addr and platform.system() != "Windows":
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
                self.method = "icmp socket"
            except OSError:
                self.sock = None

    def close(self):
        if self.sock:
            self.sock.close()

    def ping(self) -> tuple[bool, float | None, str]:
        if self.sock:
            return self._ping_socket()
        return self._ping_command()

    def _ping_socket(self):
        self.seq = (self.seq + 1) & 0xFFFF
        token = os.urandom(8)
        hdr = struct.pack("!BBHHH", 8, 0, 0, self.ident, self.seq)
        pkt = struct.pack("!BBHHH", 8, 0, _checksum(hdr + token), self.ident, self.seq) + token
        t0 = time.perf_counter()
        try:
            self.sock.sendto(pkt, (self.addr, 0))
        except OSError as e:
            return False, None, _short_err(e)
        end = t0 + self.timeout
        while True:
            left = end - time.perf_counter()
            if left <= 0:
                return False, None, "timeout"
            self.sock.settimeout(left)
            try:
                data, _ = self.sock.recvfrom(2048)
            except socket.timeout:
                return False, None, "timeout"
            except OSError as e:
                return False, None, _short_err(e)
            if len(data) >= 20 and data[0] >> 4 == 4 and data[9] == 1:  # macOS includes the IP header
                data = data[(data[0] & 0x0F) * 4:]
            if len(data) < 8:
                continue
            typ, _code, _ck, _id, seq = struct.unpack("!BBHHH", data[:8])
            if typ == 0 and seq == self.seq and data[8:16] == token:
                return True, (time.perf_counter() - t0) * 1000, ""
            if typ == 3 and len(data) >= 36:  # destination unreachable for our packet
                return False, None, "unreachable"

    def _ping_command(self):
        system = platform.system()
        ms = int(self.timeout * 1000)
        v6 = False
        try:
            v6 = ipaddress.ip_address(self.host).version == 6
        except ValueError:
            pass
        if system == "Windows":
            cmd = ["ping", "-n", "1", "-w", str(ms)] + (["-6"] if v6 else []) + [self.host]
        elif system == "Darwin":
            cmd = (["ping6", "-c", "1"] if v6 else ["ping", "-n", "-c", "1", "-W", str(ms)]) + [self.host]
        else:
            cmd = ["ping"] + (["-6"] if v6 else []) + ["-n", "-c", "1", "-W", str(max(1, round(ms / 1000)))] + [self.host]
        kw = {}
        if system == "Windows":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        t0 = time.perf_counter()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout + 2, **kw)
        except subprocess.TimeoutExpired:
            return False, None, "timeout"
        except OSError as e:
            return False, None, _short_err(e)
        out = r.stdout or ""
        ok = r.returncode == 0 and ("ttl=" in out.lower() or "time" in out.lower())
        m = re.search(r"time[=<]\s*([\d.]+)\s*ms", out, re.I)
        rtt = float(m.group(1)) if m else (time.perf_counter() - t0) * 1000
        if ok:
            return True, rtt, ""
        return False, None, "unreachable" if "unreachable" in out.lower() else "timeout"


def _short_err(e: Exception) -> str:
    s = str(e)
    for k, v in (("refused", "refused"), ("timed out", "timeout"), ("timeout", "timeout"), ("unreachable", "unreachable"),
                 ("reset", "reset"), ("Name or service", "DNS"), ("nodename", "DNS"), ("getaddrinfo", "DNS"),
                 ("CERTIFICATE", "TLS"), ("SSL", "TLS")):
        if k.lower() in s.lower():
            return v
    return (type(e).__name__ + (": " + s if s else ""))[:60]


def tcp_once(host: str, port: int, timeout_ms: int):
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout_ms / 1000):
            return True, (time.perf_counter() - t0) * 1000, ""
    except OSError as e:
        return False, None, _short_err(e)


def _status_ok(code: int, expect: str) -> bool:
    expect = (expect or "").strip().lower()
    if not expect:
        return code < 500
    for part in re.split(r"[,\s]+", expect):
        if re.fullmatch(r"[1-5]xx", part) and str(code)[0] == part[0]:
            return True
        if part.isdigit() and int(part) == code:
            return True
    return False


class HttpProbe:
    def __init__(self, spec):
        import httpx
        self.spec = spec
        # new TCP/TLS connection per request, so the probe exercises the firewall session setup path
        self.client = httpx.Client(verify=spec.get("verify_tls", False), follow_redirects=False,
                                   timeout=spec["timeout_ms"] / 1000,
                                   limits=httpx.Limits(max_keepalive_connections=0),
                                   headers={"User-Agent": "FortiGate-Upgrader-probe/1", "Connection": "close"})

    def once(self):
        t0 = time.perf_counter()
        try:
            r = self.client.get(self.spec["url"])
        except Exception as e:  # noqa: BLE001
            return False, None, _short_err(e)
        rtt = (time.perf_counter() - t0) * 1000
        if _status_ok(r.status_code, self.spec.get("expect", "")):
            return True, rtt, ""
        return False, None, f"HTTP {r.status_code}"

    def close(self):
        self.client.close()


def make_runner(spec):
    """Returns (callable -> (ok, rtt_ms, error), close, method description)."""
    if spec["type"] == "ping":
        p = Pinger(spec["host"], spec["timeout_ms"])
        return p.ping, p.close, p.method
    if spec["type"] == "tcp":
        return (lambda: tcp_once(spec["host"], spec["port"], spec["timeout_ms"])), (lambda: None), "tcp connect"
    h = HttpProbe(spec)
    return h.once, h.close, "http get"


def test(specs: list[dict], attempts: int = 3) -> list[dict]:
    """Quick reachability check for the wizard: a few attempts per probe, in parallel."""
    specs = normalise_all(specs)
    res: list[dict] = [None] * len(specs)  # type: ignore[list-item]

    def one(i, s):
        fn, close, method = make_runner(s)
        oks, rtts, errs = 0, [], Counter()
        try:
            for _ in range(attempts):
                ok, rtt, err = fn()
                if ok:
                    oks += 1
                    rtts.append(rtt)
                else:
                    errs[err] += 1
                time.sleep(0.1)
        finally:
            close()
        res[i] = {"id": s["id"], "name": s["name"], "ok": oks, "attempts": attempts, "method": method,
                  "rtt_ms": round(sum(rtts) / len(rtts), 1) if rtts else None,
                  "error": errs.most_common(1)[0][0] if errs else ""}

    th = [threading.Thread(target=one, args=(i, s), daemon=True) for i, s in enumerate(specs)]
    for t in th:
        t.start()
    for t in th:
        t.join(timeout=30)
    return [r for r in res if r]


# ------------------------------------------------------------------ live recording
class _Phase:
    __slots__ = ("sent", "lost", "rtts")

    def __init__(self):
        self.sent = 0
        self.lost = 0
        self.rtts: list[float] = []

    def add(self, ok, rtt):
        self.sent += 1
        if ok:
            if len(self.rtts) < 20000:
                self.rtts.append(rtt)
        else:
            self.lost += 1

    def summary(self):
        r = sorted(self.rtts)
        pct = lambda q: round(r[min(len(r) - 1, int(q * len(r)))], 1) if r else None  # noqa: E731
        return {"sent": self.sent, "lost": self.lost,
                "loss_pct": round(100 * self.lost / self.sent, 2) if self.sent else None,
                "rtt_avg": round(sum(r) / len(r), 1) if r else None, "rtt_p95": pct(0.95),
                "rtt_max": round(r[-1], 1) if r else None}


class Probe:
    def __init__(self, spec: dict, session: "ProbeSession"):
        self.spec = spec
        self.s = session
        self.lock = threading.Lock()
        self.buckets: dict[int, list] = {}  # idx -> [sent, lost, rtt_sum, rtt_max]
        self.windows: list[dict] = []
        self.open: dict | None = None
        self.last_ok: int | None = None
        self.last_fail: int | None = None
        self.last: dict = {}
        self.phases: dict[str, _Phase] = {}
        self.method = ""
        self.errors: Counter = Counter()
        self.announced = False

    def record(self, t: int, ok: bool, rtt: float | None, err: str):
        with self.lock:
            ph = self.phases.setdefault(self.s.phase, _Phase())
            ph.add(ok, rtt)
            b = self.buckets.setdefault(t // BUCKET_MS, [0, 0, 0.0, 0.0])
            b[0] += 1
            if ok:
                b[2] += rtt
                b[3] = max(b[3], rtt)
            else:
                b[1] += 1
                self.errors[err] += 1
            self.last = {"t": t, "ok": ok, "rtt": round(rtt, 1) if rtt is not None else None, "err": err}
            if ok:
                if self.open:
                    self._close(t)
                self.last_ok = t
            else:
                self.last_fail = t
                if not self.open:
                    self.open = {"start": t, "last_ok_before": self.last_ok, "fails": 0, "errors": Counter(),
                                 "phase": self.s.phase}
                self.open["fails"] += 1
                self.open["errors"][err] += 1
                if not self.announced and self._is_outage(self.open, t):
                    self.announced = True
                    self.s.say(f"▼ {self.spec['name']} DOWN since {_clock(self.open['start'])} ({err or 'no reply'})",
                               "warn")

    def _is_outage(self, w, end):
        return w["fails"] >= 2 and end - w["start"] >= self.s.min_outage_ms

    def _close(self, end: int, ongoing: bool = False):
        w = self.open
        self.open = None
        dur = end - w["start"]
        kind = "outage" if self._is_outage(w, end) or (ongoing and w["fails"] >= 2) else "loss"
        rec = {"start": w["start"], "end": end, "duration_ms": dur, "fails": w["fails"], "kind": kind,
               "errors": dict(w["errors"]), "phase": w["phase"], "ongoing": ongoing}
        self.windows.append(rec)
        if kind == "outage" and not ongoing:
            self.s.say(f"▲ {self.spec['name']} back after {fmt_ms(dur)} ({w['fails']} probes failed)", "ok")
        self.announced = False

    def finish(self, end: int):
        with self.lock:
            if self.open:
                self._close(end, ongoing=True)
                self.s.say(f"✖ {self.spec['name']} was still DOWN when monitoring stopped", "error")

    def snapshot(self, markers: list[dict]) -> dict:
        with self.lock:
            wins = list(self.windows)
            if self.open:
                o = self.open
                wins.append({"start": o["start"], "end": _now_ms(), "duration_ms": _now_ms() - o["start"],
                             "fails": o["fails"], "kind": "outage" if self._is_outage(o, _now_ms()) else "loss",
                             "errors": dict(o["errors"]), "phase": o["phase"], "ongoing": True, "open": True})
            buckets = [[k * BUCKET_MS, v[0], v[1], round(v[2] / (v[0] - v[1]), 1) if v[0] > v[1] else None,
                        round(v[3], 1)] for k, v in sorted(self.buckets.items())]
            phases = {k: v.summary() for k, v in self.phases.items()}
            total = _Phase()
            for v in self.phases.values():
                total.sent += v.sent
                total.lost += v.lost
                total.rtts.extend(v.rtts[:5000])
            last = dict(self.last)
        for w in wins:
            before = [m for m in markers if m["t"] <= w["start"]]
            w["during"] = before[-1]["label"] if before else ""
            w["during_src"] = before[-1].get("src", "") if before else ""
        outages = [w for w in wins if w["kind"] == "outage"]
        span = (self.s.ended or _now_ms()) - self.s.started if self.s.started else 0
        down = sum(w["duration_ms"] for w in outages)
        base = phases.get("baseline", {})
        return {**{k: self.spec[k] for k in self.spec}, "method": self.method,
                "status": "down" if (self.open and self._is_outage(self.open, _now_ms())) else
                          ("up" if last.get("ok") else "degraded" if last else "pending"),
                "last": last, "buckets": buckets, "windows": wins[-200:], "phases": phases,
                "total": total.summary(), "outages": len(outages), "losses": len(wins) - len(outages),
                "downtime_ms": down, "longest_ms": max((w["duration_ms"] for w in outages), default=0),
                "availability_pct": round(100 * (1 - down / span), 3) if span > 0 else None,
                "baseline_ok": (base.get("sent", 0) > base.get("lost", 0)) if base else None,
                "errors": dict(self.errors.most_common(6))}


def fmt_ms(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f} ms"
    s = ms / 1000
    if s < 60:
        return f"{s:.1f} s"
    m, s = divmod(int(round(s)), 60)
    return f"{m} min {s:02d} s" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def _clock(ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000)) + f".{ms % 1000:03d}"


class ProbeSession:
    """All probes of one upgrade job. Phases: baseline (before install) → upgrade → settle."""

    def __init__(self, specs: list[dict], job=None, min_outage_ms: int = 1000):
        self.specs = normalise_all(specs)
        self.job = job
        self.min_outage_ms = int(min_outage_ms)
        self.phase = "baseline"
        self.phase_times: list[dict] = []
        self.probes = [Probe(s, self) for s in self.specs]
        self.stop_ev = threading.Event()
        self.threads: list[threading.Thread] = []
        self.started: int | None = None
        self.ended: int | None = None

    def say(self, msg, level="info"):
        if self.job is not None:
            self.job.say(msg, level, src="probes")

    def start(self):
        self.started = _now_ms()
        self.phase_times.append({"t": self.started, "phase": "baseline"})
        for p in self.probes:
            th = threading.Thread(target=self._loop, args=(p,), daemon=True, name=f"probe-{p.spec['id']}")
            th.start()
            self.threads.append(th)
        self.say(f"Service probes started from this computer: " +
                 ", ".join(f"{p.spec['name']} every {p.spec['interval_ms']} ms" for p in self.probes))
        return self

    def set_phase(self, phase: str):
        self.phase = phase
        self.phase_times.append({"t": _now_ms(), "phase": phase})

    def _loop(self, p: Probe):
        try:
            fn, close, method = make_runner(p.spec)
        except Exception as e:  # noqa: BLE001
            p.method = "unavailable"
            self.say(f"{p.spec['name']}: cannot run this probe: {e}", "error")
            return
        p.method = method
        iv = p.spec["interval_ms"] / 1000
        nxt = time.monotonic()
        try:
            while not self.stop_ev.is_set():
                t = _now_ms()
                try:
                    ok, rtt, err = fn()
                except Exception as e:  # noqa: BLE001
                    ok, rtt, err = False, None, _short_err(e)
                if self.stop_ev.is_set():
                    break
                p.record(t, ok, rtt, err)
                nxt += iv
                delay = nxt - time.monotonic()
                if delay < 0:  # a slow failure (timeout) pushed us behind; don't burst to catch up
                    nxt = time.monotonic()
                    delay = 0
                self.stop_ev.wait(delay)
        finally:
            close()

    def all_up(self) -> bool:
        return all(not p.open and (p.last.get("ok") or not p.last) for p in self.probes)

    def unstable(self, window_ms: int) -> list[str]:
        """Probes that failed within the last window_ms (or are down now). Instantaneous 'up' is
        not enough: a target that drops for a second every few seconds is up most of the time."""
        now = _now_ms()
        return [p.spec["name"] for p in self.probes
                if p.open or (p.last_fail is not None and now - p.last_fail < window_ms)]

    def stop(self):
        if self.ended:
            return
        self.stop_ev.set()
        for th in self.threads:
            th.join(timeout=12)
        self.ended = _now_ms()
        for p in self.probes:
            p.finish(self.ended)

    def markers(self) -> list[dict]:
        out = []
        if self.job is None:
            return out
        for e in list(self.job.log):
            ts = e.get("ts")
            if not ts or (self.started and ts < self.started - 1000):
                continue
            msg = e.get("msg", "")
            if e.get("level") == "stage":
                label = msg.strip("— ").strip()
                if not STAGE_RE.search(label) or e.get("src") == "probes":
                    continue
            elif e.get("src") != "probes" and MARKER_RE.search(msg):
                label = msg.split(": ", 1)[-1] if e.get("src") and msg.startswith(e["src"] + ": ") else msg
                if re.search(r"fails over", label, re.I):
                    label = "HA failover: the primary fails over to an upgraded secondary"
                elif re.search(r"image accepted", label, re.I):
                    label = re.sub(r"image accepted.*", "image accepted, installing and rebooting", label)
                label = re.sub(r"\s*\(.*$", "", label)[:90]
            else:
                continue
            out.append({"t": ts, "label": label, "src": e.get("src", "")})
        return out

    def summary(self) -> dict:
        mk = self.markers()
        probes = [p.snapshot(mk) for p in self.probes]
        return {"started": self.started, "ended": self.ended, "now": _now_ms(), "phase": self.phase,
                "phases": self.phase_times, "min_outage_ms": self.min_outage_ms, "bucket_ms": BUCKET_MS,
                "markers": mk[-300:], "probes": probes,
                "worst_outage_ms": max((p["longest_ms"] for p in probes), default=0),
                "total_outages": sum(p["outages"] for p in probes),
                "still_down": [p["name"] for p in probes if self.ended and any(
                    w.get("ongoing") and w["kind"] == "outage" for w in p["windows"])]}

    def summary_lines(self) -> list[tuple[str, str]]:
        lines = []
        for p in self.summary()["probes"]:
            if p["outages"]:
                lines.append((f"{p['name']}: {p['outages']} outage(s), longest {fmt_ms(p['longest_ms'])}, total "
                              f"{fmt_ms(p['downtime_ms'])}; {p['total']['lost']}/{p['total']['sent']} probes lost", "warn"))
            elif p["total"]["lost"]:
                lines.append((f"{p['name']}: no outage; {p['total']['lost']}/{p['total']['sent']} probes lost "
                              f"(packet loss only)", "info"))
            else:
                lines.append((f"{p['name']}: no loss ({p['total']['sent']} probes)", "ok"))
            if p["baseline_ok"] is False:
                lines.append((f"{p['name']}: was not reachable before the upgrade started, so its results do not "
                              "reflect the upgrade", "warn"))
        return lines
