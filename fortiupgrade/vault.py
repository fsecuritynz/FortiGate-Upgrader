"""Encrypted secret store.

All API tokens and SSH passwords live in one encrypted file (<data>/secrets.vault).
Only its key is stored outside it, either:

  * "keychain" (default): ONE OS keychain / Credential Manager item, read once per launch,
    so macOS asks for access at most once per app build instead of once per secret per call.
  * "file": <data>/vault.key, readable only by the current user (no keychain prompts at all;
    protection is the file permissions of your user account).

Secrets from earlier versions (one keychain item per secret, or secrets.json) are migrated
into the vault the first time it is opened.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

try:
    import keyring
except ImportError:  # pragma: no cover
    keyring = None

SERVICE = "FortiGate Upgrader"
LEGACY = "FortiUpgrade"
KEY_ACCOUNT = "vault-key"

_lock = threading.RLock()
_cache: dict | None = None
_key: bytes | None = None
_cache_dir: Path | None = None


def _dir() -> Path:
    from .storage import data_dir
    return data_dir()


def _settings_file() -> Path:
    return _dir() / "settings.json"


def settings() -> dict:
    f = _settings_file()
    d = json.loads(f.read_text()) if f.exists() else {}
    d.setdefault("secret_store", "file" if os.environ.get("FORTIUPGRADE_NO_KEYRING") == "1" or keyring is None
                 else "keychain")
    return d


def save_settings(d: dict):
    _settings_file().write_text(json.dumps(d, indent=1))


def _private_write(path: Path, data: bytes):
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------- key
def _key_file() -> Path:
    return _dir() / "vault.key"


def _load_key(store: str, create: bool) -> bytes | None:
    if store == "keychain":
        v = keyring.get_password(SERVICE, KEY_ACCOUNT)
        if v:
            return v.encode()
        if create:
            k = Fernet.generate_key()
            keyring.set_password(SERVICE, KEY_ACCOUNT, k.decode())
            return k
        return None
    f = _key_file()
    if f.exists():
        return f.read_bytes().strip()
    if create:
        k = Fernet.generate_key()
        _private_write(f, k)
        return k
    return None


def _vault_file() -> Path:
    return _dir() / "secrets.vault"


def _open() -> dict:
    """Decrypt the vault once per process; later calls use the in-memory copy."""
    global _cache, _key, _cache_dir
    if _cache is not None and _cache_dir == _dir():
        return _cache
    _cache_dir = _dir()
    store = settings()["secret_store"]
    vf = _vault_file()
    _key = _load_key(store, create=True)
    if vf.exists():
        try:
            _cache = json.loads(Fernet(_key).decrypt(vf.read_bytes()))
        except InvalidToken as e:
            raise RuntimeError("The secret vault cannot be decrypted with the stored key. Re-enter the API tokens "
                               "in the device editor, or restore vault.key / the keychain item.") from e
    else:
        _cache = {}
        _migrate(_cache)
        _write()
    return _cache


def _write():
    _private_write(_vault_file(), Fernet(_key).encrypt(json.dumps(_cache).encode()))


def _migrate(d: dict):
    """Pull secrets saved by earlier versions (one keychain item each, or secrets.json)."""
    from .storage import list_targets
    old = _dir() / "secrets.json"
    if old.exists():
        try:
            d.update(json.loads(old.read_text()))
        except ValueError:
            pass
    if keyring is None or os.environ.get("FORTIUPGRADE_NO_KEYRING") == "1":
        return
    for t in list_targets():
        for m in t.get("members", []):
            for kind in ("token", "ssh"):
                name = f"{m['id']}:{kind}"
                if name in d:
                    continue
                for svc in (SERVICE, LEGACY):
                    try:
                        v = keyring.get_password(svc, name)
                    except Exception:  # noqa: BLE001
                        v = None
                    if v:
                        d[name] = v
                        break


# ------------------------------------------------------------------- API
def get(name: str) -> str | None:
    with _lock:
        return _open().get(name)


def has(name: str) -> bool:
    return bool(get(name))


def set(name: str, value: str | None):  # noqa: A001
    with _lock:
        d = _open()
        if value:
            d[name] = value
        else:
            d.pop(name, None)
        _write()


def set_store(store: str):
    """Move the vault key between the OS keychain and a local file."""
    global _key
    if store not in ("keychain", "file"):
        raise ValueError("store must be 'keychain' or 'file'")
    with _lock:
        _open()
        cur = settings()
        if cur["secret_store"] == store:
            return
        if store == "keychain":
            if keyring is None:
                raise RuntimeError("No OS keychain is available on this system.")
            keyring.set_password(SERVICE, KEY_ACCOUNT, _key.decode())
            try:
                _key_file().unlink()
            except FileNotFoundError:
                pass
        else:
            _private_write(_key_file(), _key)
        cur["secret_store"] = store
        save_settings(cur)


def reset_cache():
    global _cache, _key
    with _lock:
        _cache, _key = None, None
