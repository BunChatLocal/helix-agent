"""
Helix Secrets Manager
Fernet-encrypted credential store at ~/.helix/secrets.enc
Master key derived via argon2id from stable entropy + optional passphrase.

On Railway the container's /etc/machine-id changes every deploy. Entropy is
therefore persisted on the Helix volume (~/.helix/.machine-entropy) so a new
container can still open the vault. HELIX_SECRETS_ENTROPY, if set, wins.
"""

import os
import json
import hashlib
import base64
import logging
import time
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from argon2.low_level import hash_secret_raw, Type


logger = logging.getLogger("helix.secrets")

ENV_ENTROPY = "HELIX_SECRETS_ENTROPY"


class VaultUnreadable(RuntimeError):
    """secrets.enc exists but cannot be decrypted with this host's key."""


def _helix_home() -> Path:
    return Path.home() / ".helix"


def _secrets_path() -> Path:
    return _helix_home() / "secrets.enc"


def _salt_path() -> Path:
    return _helix_home() / ".salt"


def _entropy_path() -> Path:
    return _helix_home() / ".machine-entropy"


# Kept for callers/tests that patched the old module-level constants.
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


def _platform_machine_entropy() -> bytes:
    """Host-local entropy. Unstable across Railway deploys — do not use alone."""
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


def _persist_entropy(data: bytes) -> None:
    path = _entropy_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        logger.warning("could not persist machine entropy to %s", path)


def _machine_entropy() -> bytes:
    """Stable entropy: env var, then the volume file, then this host (saved)."""
    env = (os.environ.get(ENV_ENTROPY) or "").strip()
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()

    path = _entropy_path()
    try:
        if path.exists():
            data = path.read_bytes().strip()
            if data:
                return data
    except OSError:
        logger.warning("could not read %s", path)

    data = _platform_machine_entropy()
    _persist_entropy(data)
    return data


def _get_or_create_salt() -> bytes:
    path = _salt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return base64.b64decode(path.read_bytes())
    salt = os.urandom(16)
    path.write_bytes(base64.b64encode(salt))
    try:
        path.chmod(0o600)
    except OSError:
        pass
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
_unreadable_logged = False


def reset_runtime_state() -> None:
    """Drop cached key material. Tests call this after changing entropy."""
    global _fernet_empty, _secrets_cache, _secrets_mtime, _unreadable_logged
    _fernet_empty = None
    _secrets_cache = None
    _secrets_mtime = -1.0
    _unreadable_logged = False


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
    path = _secrets_path()
    if not path.exists():
        if not passphrase:
            _secrets_cache = {}
            _secrets_mtime = 0.0
        return {}
    mtime = path.stat().st_mtime
    if not passphrase and _secrets_cache is not None and mtime == _secrets_mtime:
        return _secrets_cache
    fernet = _get_fernet(passphrase)
    encrypted = path.read_bytes()
    try:
        decrypted = fernet.decrypt(encrypted)
        data = json.loads(decrypted)
    except (InvalidToken, json.JSONDecodeError, ValueError) as exc:
        raise VaultUnreadable(
            f"cannot decrypt {path} with this host's key"
        ) from exc
    if not isinstance(data, dict):
        raise VaultUnreadable(f"{path} decrypted to a non-object")
    if not passphrase:
        _secrets_cache = data
        _secrets_mtime = mtime
    return data


def _save_secrets(data: dict, passphrase: str = "") -> None:
    global _secrets_cache, _secrets_mtime
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fernet = _get_fernet(passphrase)
    encrypted = fernet.encrypt(json.dumps(data).encode())
    path.write_bytes(encrypted)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if not passphrase:
        _secrets_cache = data
        _secrets_mtime = path.stat().st_mtime


def rotate_unreadable_vault() -> Optional[Path]:
    """Move an unreadable secrets.enc aside so a new vault can be written."""
    path = _secrets_path()
    if not path.exists():
        reset_runtime_state()
        return None
    dest = path.with_name(f"secrets.enc.unreadable.{int(time.time())}")
    try:
        path.replace(dest)
    except OSError:
        dest = path.with_name(f"secrets.enc.unreadable.{int(time.time())}.{os.getpid()}")
        path.replace(dest)
    reset_runtime_state()
    logger.error("moved unreadable vault aside to %s", dest)
    return dest


def vault_is_readable(passphrase: str = "") -> bool:
    if not _secrets_path().exists():
        return False
    try:
        _load_secrets(passphrase)
        return True
    except VaultUnreadable:
        return False


def _log_unreadable(exc: VaultUnreadable) -> None:
    global _unreadable_logged
    if not _unreadable_logged:
        logger.error("%s", exc)
        _unreadable_logged = True


def set_secret(key: str, value: str, passphrase: str = "") -> None:
    """Store a secret by key."""
    try:
        data = _load_secrets(passphrase)
    except VaultUnreadable as exc:
        _log_unreadable(exc)
        rotate_unreadable_vault()
        data = {}
    data[key] = value
    _save_secrets(data, passphrase)


def get_secret(key: str, passphrase: str = "", default: Optional[str] = None) -> Optional[str]:
    """Retrieve a secret by key."""
    try:
        data = _load_secrets(passphrase)
    except VaultUnreadable as exc:
        _log_unreadable(exc)
        return default
    return data.get(key, default)


def delete_secret(key: str, passphrase: str = "") -> bool:
    """Delete a secret. Returns True if it existed."""
    try:
        data = _load_secrets(passphrase)
    except VaultUnreadable as exc:
        _log_unreadable(exc)
        return False
    if key in data:
        del data[key]
        _save_secrets(data, passphrase)
        return True
    return False


def list_keys(passphrase: str = "") -> list[str]:
    """List all secret keys (not values)."""
    try:
        return list(_load_secrets(passphrase).keys())
    except VaultUnreadable as exc:
        _log_unreadable(exc)
        return []
