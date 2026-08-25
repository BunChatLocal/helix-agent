"""
Helix Secrets Manager
Fernet-encrypted credential store at ~/.helix/secrets.enc
Master key derived via argon2id from machine entropy + optional passphrase.
"""

import os
import json
import hashlib
import base64
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet
from argon2.low_level import hash_secret_raw, Type


SECRETS_PATH = Path.home() / ".helix" / "secrets.enc"
SALT_PATH = Path.home() / ".helix" / ".salt"


def _windows_machine_guid() -> Optional[str]:
    """Stable per-machine GUID from the registry — the Windows analogue of
    /etc/machine-id. Survives reboots and upgrades."""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            return guid.strip() or None
    except Exception:
        return None


def _machine_entropy() -> bytes:
    """Stable per-machine entropy from machine-id."""
    sources = [
        Path("/etc/machine-id"),
        Path("/var/lib/dbus/machine-id"),
    ]
    for src in sources:
        if src.exists():
            return src.read_bytes().strip()

    if os.name == "nt":
        guid = _windows_machine_guid()
        if guid:
            return hashlib.sha256(guid.encode()).digest()

    # Fallback: hash of hostname + user identity. os.getuid() is POSIX-only,
    # so fall back to the account name on Windows.
    import socket
    try:
        user_id = str(os.getuid())
    except AttributeError:
        user_id = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    return hashlib.sha256(f"{socket.gethostname()}:{user_id}".encode()).digest()


def _get_or_create_salt() -> bytes:
    SALT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SALT_PATH.exists():
        return base64.b64decode(SALT_PATH.read_bytes())
    salt = os.urandom(16)
    SALT_PATH.write_bytes(base64.b64encode(salt))
    SALT_PATH.chmod(0o600)
    return salt


def _derive_key(passphrase: str = "") -> bytes:
    """Derive 32-byte key via argon2id from machine entropy + passphrase."""
    salt = _get_or_create_salt()
    password = _machine_entropy() + passphrase.encode()
    raw = hash_secret_raw(
        secret=password,
        salt=salt,
        time_cost=2,
        memory_cost=65536,
        parallelism=2,
        hash_len=32,
        type=Type.ID,
    )
    return base64.urlsafe_b64encode(raw)


_fernet_empty: Optional[Fernet] = None
_secrets_cache: Optional[dict] = None
_secrets_mtime: float = -1.0


def _get_fernet(passphrase: str = "") -> Fernet:
    # Argon2id is the expensive part of every secrets read. The empty-passphrase
    # key is derived from machine entropy and the salt file, so it is stable
    # for the life of the process — cache it rather than spending tens-to-
    # hundreds of milliseconds on every chat turn.
    global _fernet_empty
    if not passphrase:
        if _fernet_empty is None:
            _fernet_empty = Fernet(_derive_key(""))
        return _fernet_empty
    return Fernet(_derive_key(passphrase))


def _load_secrets(passphrase: str = "") -> dict:
    global _secrets_cache, _secrets_mtime
    if not SECRETS_PATH.exists():
        if not passphrase:
            _secrets_cache = {}
            _secrets_mtime = 0.0
        return {}
    mtime = SECRETS_PATH.stat().st_mtime
    if not passphrase and _secrets_cache is not None and mtime == _secrets_mtime:
        return _secrets_cache
    fernet = _get_fernet(passphrase)
    encrypted = SECRETS_PATH.read_bytes()
    decrypted = fernet.decrypt(encrypted)
    data = json.loads(decrypted)
    if not passphrase:
        _secrets_cache = data
        _secrets_mtime = mtime
    return data


def _save_secrets(data: dict, passphrase: str = "") -> None:
    global _secrets_cache, _secrets_mtime
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fernet = _get_fernet(passphrase)
    encrypted = fernet.encrypt(json.dumps(data).encode())
    SECRETS_PATH.write_bytes(encrypted)
    SECRETS_PATH.chmod(0o600)
    if not passphrase:
        _secrets_cache = data
        _secrets_mtime = SECRETS_PATH.stat().st_mtime


def set_secret(key: str, value: str, passphrase: str = "") -> None:
    """Store a secret by key."""
    data = _load_secrets(passphrase)
    data[key] = value
    _save_secrets(data, passphrase)


def get_secret(key: str, passphrase: str = "", default: Optional[str] = None) -> Optional[str]:
    """Retrieve a secret by key."""
    data = _load_secrets(passphrase)
    return data.get(key, default)


def delete_secret(key: str, passphrase: str = "") -> bool:
    """Delete a secret. Returns True if it existed."""
    data = _load_secrets(passphrase)
    if key in data:
        del data[key]
        _save_secrets(data, passphrase)
        return True
    return False


def list_keys(passphrase: str = "") -> list[str]:
    """List all secret keys (not values)."""
    return list(_load_secrets(passphrase).keys())
