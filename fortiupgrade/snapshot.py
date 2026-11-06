"""Take a snapshot: run checks across global + every selected VDOM, back up config."""
from __future__ import annotations

import datetime as dt
import logging
import traceback

from .checks import CHECKS, CHECKS_BY_ID, Ctx, summarise
from .client import FortiGateClient

log = logging.getLogger(__name__)


def config_via_ssh(ssh, vdom_mode: bool) -> str:
    """Running configuration as text via the CLI, in the same format as a backup file."""
    out = ssh.run("show", timeout=600)  # top-level prompt: whole configuration, global and VDOMs
    lines = out.splitlines()
    # drop the echoed command and the trailing prompt
    while lines and not lines[0].lstrip().startswith(("#", "config ")):
        lines.pop(0)
    while lines and (lines[-1].strip().endswith(("#", "$")) or not lines[-1].strip()):
        lines.pop()
    text = "\n".join(lines) + "\n"
    from .client import looks_like_config
    if not looks_like_config(text):
        raise RuntimeError("the CLI output does not look like a configuration")
    return text


def default_check_ids() -> list[str]:
    return [c.id for c in CHECKS if c.default]


def take_snapshot(client: FortiGateClient, label: str, *, check_ids: list[str] | None = None,
                  vdoms: list[str] | None = None, ssh=None, include_config: bool = True,
                  logger=None) -> dict:
    say = logger or (lambda m: log.info(m))
    info = client.discover()
    target_vdoms = vdoms or info.vdoms
    if not info.vdom_mode:
        target_vdoms = ["root"]
    ctx = Ctx(client=client, vdom_mode=info.vdom_mode, ssh=ssh)
    snap = {
        "meta": {"label": label, "taken_at": dt.datetime.now().isoformat(timespec="seconds"),
                 "device": info.to_dict(), "vdoms": target_vdoms, "ssh": ssh is not None},
        "results": {}, "errors": {}, "config": None, "summary": [],
    }
    for cid in check_ids or default_check_ids():
        check = CHECKS_BY_ID.get(cid)
        if check is None:
            continue
        if check.needs_ssh and ssh is None:
            snap["errors"].setdefault(cid, {})["*"] = "skipped: no SSH credentials configured"
            continue
        scopes = ["global"] if check.scope == "global" else target_vdoms
        for scope in scopes:
            say(f"[{label}] {check.name} ({scope})")
            try:
                data = check.collect(ctx, None if scope == "global" else (scope if info.vdom_mode else None))
                snap["results"].setdefault(cid, {})[scope] = data
                snap["summary"].append({"check": cid, "name": check.name, "scope": scope,
                                        "text": summarise(check, data)})
                say(f"   ✓ {check.name} ({scope}): {summarise(check, data)}")
            except Exception as e:  # noqa: BLE001
                log.debug(traceback.format_exc())
                snap["errors"].setdefault(cid, {})[scope] = str(e)
                say(f"   ! {check.name} ({scope}) failed: {e}")
    if include_config:
        say(f"[{label}] Backing up configuration")
        try:
            snap["config"] = client.backup_config("global")
            say(f"   ✓ configuration: {len(snap['config'].splitlines()):,} lines, "
                f"{len(snap['config']) / 1024:.0f} KB")
        except Exception as e:  # noqa: BLE001
            say(f"   ! config backup over the REST API failed: {e}")
            cfg = None
            if ssh is not None:
                say(f"[{label}] Reading the configuration over SSH instead (show)")
                try:
                    cfg = config_via_ssh(ssh, info.vdom_mode)
                    say(f"   ✓ configuration (SSH): {len(cfg.splitlines()):,} lines, {len(cfg) / 1024:.0f} KB")
                except Exception as e2:  # noqa: BLE001
                    say(f"   ! SSH configuration read failed: {e2}")
            if cfg:
                snap["config"] = cfg
                snap["config_source"] = "ssh"
            else:
                snap["errors"]["config"] = {"global": str(e) + ("" if ssh else " (add SSH credentials to read it over SSH)")}
    return snap
