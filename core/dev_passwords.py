"""Hashed passwords for test sign-in (web.dev_login).

Plaintext is never written to disk or logs. Each password is stored as a
salted PBKDF2-SHA256 hash inside the encrypted secrets vault
(~/.helix/secrets.enc), the same store as WEB_PASSWORD.

Tests may point STORE_PATH at a temp file so they do not touch the vault.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from pathlib import Path

from security.secrets import get_secret, set_secret

logger = logging.getLogger("helix.dev_passwords")

SECRET_KEY = "DEV_LOGIN_PASSWORDS"
LEGACY_PATH = Path.home() / ".helix" / "dev-passwords.json"
# Tests set this to a temp file. None = encrypted vault.
STORE_PATH: Path | None = None
MIN_LENGTH = 8
_ITERATIONS = 210_000
_LOCK = threading.Lock()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2$sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, algo, iters, salt_hex, hash_hex = stored.split("$")
        if kind != "pbkdf2" or algo != "sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iters),
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _parse(raw) -> dict[str, str]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except Exception:
            return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in data.items() if isinstance(v, str)}


def _read_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return _parse(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_file(data: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _migrate_legacy() -> dict[str, str]:
    """Move hashes out of the old plaintext JSON file into the vault once."""
    data = _parse(get_secret(SECRET_KEY, default="") or "")
    legacy = _read_file(LEGACY_PATH)
    if not legacy:
        return data
    data.update(legacy)
    set_secret(SECRET_KEY, json.dumps(data, sort_keys=True))
    try:
        LEGACY_PATH.unlink()
    except OSError:
        logger.warning("Could not remove leftover %s after migrating hashes", LEGACY_PATH)
    else:
        logger.info("Moved test-login password hashes into the encrypted secret store")
    return data


def load(path: Path | None = None) -> dict[str, str]:
    p = path if path is not None else STORE_PATH
    if p is not None:
        return _read_file(p)
    return _migrate_legacy()


def _save(data: dict[str, str], path: Path | None) -> None:
    if path is not None:
        _write_file(data, path)
        return
    set_secret(SECRET_KEY, json.dumps(data, sort_keys=True))


def get_hash(email: str, path: Path | None = None) -> str | None:
    return load(path).get((email or "").strip().lower())


def set_password(email: str, password: str, path: Path | None = None) -> None:
    key = (email or "").strip().lower()
    p = path if path is not None else STORE_PATH
    with _LOCK:
        data = load(p)
        data[key] = hash_password(password)
        _save(data, p)


def clear_password(email: str, path: Path | None = None) -> bool:
    key = (email or "").strip().lower()
    p = path if path is not None else STORE_PATH
    with _LOCK:
        data = load(p)
        if key not in data:
            return False
        del data[key]
        _save(data, p)
        return True
