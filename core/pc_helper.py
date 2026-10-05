"""Short-lived PC helper sessions for chat and the operator panel.

A colleague clicks Check this PC. If Zork Helper is already listening on
this PC, the page talks to it on loopback with no prompt. If it is
installed but not running, the page opens ``zorkhelper://`` to wake the
tray. Only the first visit downloads ``ZorkHelper.cmd``.

The helper authenticates with a session or device secret. It can post
progress, one result payload, and run an allowlisted install job after
the person clicks Yes on the card. There is no endpoint that runs
caller-supplied commands.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import time
from pathlib import Path

SESSION_TTL_SEC = 15 * 60
DEVICE_TTL_SEC = 90
# Chat pings this loopback port to see if the tray is on THIS PC.
LOCAL_HELPER_PORT = 18792
MAX_LIVE = 40
MAX_DEVICES = 20
ID_RE = re.compile(r"^[a-f0-9]{32}$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PROGRESS_MAX = 200
LOG_MAX = 40

INSTALLABLE = frozenset({
    "teams",
    "winupdate",
    "appsanywhere",
    "office",
    "word",
    "excel",
    "powerpoint",
    "outlook",
    "onenote",
    "onedrive",
    "sketchup",
    "fridnett",
    "skoleprogrammer",
    "printer",
})

BOOTSTRAP_SCRIPT_PATH = Path(__file__).resolve().parent / "pc_helper_bootstrap.ps1"
SCAN_SCRIPT_PATH = Path(__file__).resolve().parent / "pc_helper_scan.ps1"
SCAN_MAC_SCRIPT_PATH = Path(__file__).resolve().parent / "pc_helper_scan_mac.sh"
TRAY_SCRIPT_PATH = Path(__file__).resolve().parent / "pc_helper_tray.ps1"
OPEN_SCRIPT_PATH = Path(__file__).resolve().parent / "pc_helper_open.ps1"
INSTALL_SCRIPT_PATH = Path(__file__).resolve().parent / "pc_helper_install.ps1"
LAUNCH_SCRIPT_PATH = Path(__file__).resolve().parent / "pc_helper_launch.ps1"
LAUNCH_MAC_SCRIPT_PATH = Path(__file__).resolve().parent / "pc_helper_launch_mac.sh"
HELPER_PROTOCOL = "zorkhelper"

_LOCK = threading.Lock()
_SESSIONS: dict[str, dict] = {}
_DEVICES: dict[str, dict] = {}


def _now() -> float:
    return time.time()


def _prune_locked(now: float) -> None:
    dead = [sid for sid, row in _SESSIONS.items() if row["expires"] <= now]
    for sid in dead:
        _SESSIONS.pop(sid, None)
    if len(_SESSIONS) <= MAX_LIVE:
        return
    extra = sorted(_SESSIONS.values(), key=lambda r: r["created"])[: len(_SESSIONS) - MAX_LIVE]
    for row in extra:
        _SESSIONS.pop(row["id"], None)


def _prune_devices_locked(now: float) -> None:
    dead = [
        did for did, row in _DEVICES.items()
        if (row.get("last_seen") or 0) + DEVICE_TTL_SEC <= now
    ]
    for did in dead:
        _DEVICES.pop(did, None)
    if len(_DEVICES) <= MAX_DEVICES:
        return
    extra = sorted(_DEVICES.values(), key=lambda r: r.get("last_seen") or 0)[
        : len(_DEVICES) - MAX_DEVICES
    ]
    for row in extra:
        _DEVICES.pop(row["id"], None)


def create_session(*, owner: str, base_url: str) -> dict:
    owner = (owner or "").strip()
    if not owner:
        raise ValueError("owner is required")
    base = (base_url or "").strip().rstrip("/")
    if not base.startswith("http://") and not base.startswith("https://"):
        raise ValueError("base_url must be http(s)")
    if any(c in base for c in "'\"\\\n\r"):
        raise ValueError("base_url must be http(s)")
    now = _now()
    with _LOCK:
        _prune_locked(now)
        sid = secrets.token_hex(16)
        secret = secrets.token_urlsafe(18)
        row = {
            "id": sid,
            "secret": secret,
            "owner": owner,
            "base_url": base,
            "created": now,
            "expires": now + SESSION_TTL_SEC,
            "status": "waiting",
            "activity": "",
            "percent": None,
            "log": [],
            "result": None,
            "error": "",
            "pending_job": None,
            "intent": None,
        }
        _SESSIONS[sid] = row
        return dict(row)


def _get_locked(sid: str) -> dict | None:
    if not ID_RE.match(sid or ""):
        return None
    row = _SESSIONS.get(sid)
    if not row:
        return None
    if row["expires"] <= _now():
        _SESSIONS.pop(sid, None)
        return None
    return row


def get(sid: str) -> dict | None:
    with _LOCK:
        row = _get_locked(sid)
        return dict(row) if row else None


def public_view(row: dict) -> dict:
    return {
        "id": row.get("id") or "",
        "status": row.get("status") or "waiting",
        "activity": row.get("activity") or "",
        "percent": row.get("percent"),
        "created": row.get("created") or 0,
        "expires": row.get("expires") or 0,
        "log": list(row.get("log") or []),
        "result": row.get("result"),
        "error": row.get("error") or "",
    }


def owned_by(row: dict, owner: str) -> bool:
    return bool(owner) and row.get("owner") == owner


def check_secret(sid: str, secret: str) -> dict | None:
    if not SECRET_RE.match(secret or ""):
        return None
    with _LOCK:
        row = _get_locked(sid)
        if not row or not secrets.compare_digest(row["secret"], secret):
            return None
        return dict(row)


def _append_log_locked(row: dict, text: str) -> None:
    msg = (text or "").strip()[:PROGRESS_MAX]
    if not msg:
        return
    log = list(row.get("log") or [])
    log.append({"ts": _now(), "text": msg})
    row["log"] = log[-LOG_MAX:]


def mark_connected(sid: str, secret: str) -> dict | None:
    with _LOCK:
        row = _get_locked(sid)
        if not row or not secrets.compare_digest(row["secret"], secret):
            return None
        if row["status"] in ("done", "error"):
            return dict(row)
        row["status"] = "scanning"
        row["pending_job"] = None
        _append_log_locked(row, "Hjelperen er i gang.")
        return dict(row)


_CLEAR_ACTIVITY_LOG = frozenset({
    "installasjon ferdig",
    "installasjon avbrutt",
    "ingenting a installere",
})


def _clean_percent(raw) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return max(0, min(100, int(raw)))
    except (TypeError, ValueError):
        return None


def add_progress(sid: str, secret: str, text: str, activity=None, percent=None) -> dict | None:
    """``percent`` belongs to this step only; a step without one clears the bar."""
    with _LOCK:
        row = _get_locked(sid)
        if not row or not secrets.compare_digest(row["secret"], secret):
            return None
        if row["status"] not in ("done", "error"):
            row["status"] = "scanning"
        _append_log_locked(row, text)
        row["percent"] = _clean_percent(percent)
        if activity is not None:
            act = str(activity).strip().lower()
            if act in ("", "scanning", "installing"):
                row["activity"] = act
        elif (text or "").strip().rstrip(".").lower() in _CLEAR_ACTIVITY_LOG:
            row["activity"] = ""
        return dict(row)


def set_result(sid: str, secret: str, data: dict) -> dict | None:
    if not isinstance(data, dict):
        return None
    items = data.get("items")
    if isinstance(items, dict):
        keyed = []
        for key in sorted(items, key=lambda k: int(k) if str(k).isdigit() else 0):
            val = items.get(key)
            if isinstance(val, dict):
                keyed.append(val)
        items = keyed
    if not isinstance(items, list):
        return None
    clean_items = []
    for raw in items[:40]:
        if not isinstance(raw, dict):
            continue
        clean_items.append({
            "id": str(raw.get("id") or "")[:40],
            "name": str(raw.get("name") or "")[:80],
            "found": bool(raw.get("found")),
            "version": str(raw.get("version") or "")[:80],
            "ok": bool(raw.get("ok")),
            "note": str(raw.get("note") or "")[:400],
            "reboot": bool(raw.get("reboot")),
        })
    payload = {
        "host": str(data.get("host") or "")[:80],
        "os": str(data.get("os") or "")[:160],
        "admin": bool(data.get("admin")),
        "user": str(data.get("user") or "")[:120],
        "scanned_at": str(data.get("scanned_at") or "")[:40],
        "items": clean_items,
    }
    with _LOCK:
        row = _get_locked(sid)
        if not row or not secrets.compare_digest(row["secret"], secret):
            return None
        row["result"] = payload
        row["status"] = "done"
        row["activity"] = ""
        row["percent"] = None
        row["error"] = ""
        _append_log_locked(row, "Sjekken er ferdig.")
        return dict(row)


def set_error(sid: str, secret: str, message: str) -> dict | None:
    with _LOCK:
        row = _get_locked(sid)
        if not row or not secrets.compare_digest(row["secret"], secret):
            return None
        row["status"] = "error"
        row["activity"] = ""
        row["percent"] = None
        row["error"] = (message or "Helper failed").strip()[:300]
        _append_log_locked(row, row["error"])
        return dict(row)


def _with_bom(text: str) -> str:
    if not text.startswith("\ufeff"):
        return "\ufeff" + text
    return text


def scan_script() -> str:
    text = SCAN_SCRIPT_PATH.read_text(encoding="utf-8-sig")
    return _with_bom(text)


def scan_mac_script() -> str:
    return SCAN_MAC_SCRIPT_PATH.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def tray_script() -> str:
    text = TRAY_SCRIPT_PATH.read_text(encoding="utf-8-sig")
    return _with_bom(text)


def tray_version() -> str:
    """Hash of the tray exactly as served. The tray hashes its own file on disk
    the same way, so the two match only when the PC runs this build's tray."""
    return hashlib.sha256(tray_script().encode("utf-8")).hexdigest()[:16]


def open_script() -> str:
    text = OPEN_SCRIPT_PATH.read_text(encoding="utf-8-sig")
    return _with_bom(text)


def bootstrap_script(job_kind: str = "scan", job_items=None) -> str:
    kind = "install" if str(job_kind or "").strip().lower() == "install" else "scan"
    items = ",".join(normalize_install_items(job_items)) if kind == "install" else ""
    text = (
        BOOTSTRAP_SCRIPT_PATH.read_text(encoding="utf-8-sig")
        .replace("__DEFAULT_JOB_KIND__", kind)
        .replace("__DEFAULT_JOB_ITEMS__", items)
    )
    return _with_bom(text)


def install_script() -> str:
    text = INSTALL_SCRIPT_PATH.read_text(encoding="utf-8-sig")
    return _with_bom(text)


def _encoded_ps(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def set_session_intent(sid: str, kind: str, items=None) -> dict | None:
    """Remember whether this session is a PC-check or an install-only job."""
    kind = (kind or "").strip().lower()
    if kind not in {"scan", "install"}:
        return None
    wanted = normalize_install_items(items) if kind == "install" else []
    with _LOCK:
        row = _get_locked(sid)
        if not row:
            return None
        row["intent"] = {"kind": kind, "items": wanted}
        return dict(row)


def session_intent(row: dict | None) -> tuple[str, list[str]]:
    blob = (row or {}).get("intent")
    if not isinstance(blob, dict):
        return "scan", []
    kind = str(blob.get("kind") or "scan").strip().lower()
    if kind not in {"scan", "install"}:
        kind = "scan"
    items = normalize_install_items(blob.get("items")) if kind == "install" else []
    return kind, items


def launcher_mac(base_url: str, sid: str, secret: str) -> str:
    """Tiny .command: download the Mac scan and run it. Scan only."""
    if not ID_RE.match(sid) or not SECRET_RE.match(secret):
        raise ValueError("invalid session")
    base = base_url.rstrip("/")
    if any(ch in base for ch in "'\"\\\n\r;|&`$"):
        raise ValueError("invalid session")
    return (
        LAUNCH_MAC_SCRIPT_PATH.read_text(encoding="utf-8")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("__BASE_URL__", base)
        .replace("__SESSION_ID__", sid)
        .replace("__SECRET__", secret)
    )


def launcher_cmd(base_url: str, sid: str, secret: str, job_kind: str = "scan", job_items=None) -> str:
    """Tiny cmd: hand the bootstrap to a hidden PowerShell and close at once.

    Progress shows in the chat, not in a console. The work runs in a new
    process started hidden because ``-WindowStyle Hidden`` on the cmd's own
    console does not hide it when Windows Terminal is the default console.
    """
    if not ID_RE.match(sid) or not SECRET_RE.match(secret):
        raise ValueError("invalid session")
    kind = "install" if str(job_kind or "").strip().lower() == "install" else "scan"
    items = ",".join(normalize_install_items(job_items)) if kind == "install" else ""
    base = base_url.rstrip("/")
    bootstrap = (
        LAUNCH_SCRIPT_PATH.read_text(encoding="utf-8-sig")
        .replace("__BASE_URL__", base)
        .replace("__SESSION_ID__", sid)
        .replace("__SECRET__", secret)
        .replace("__JOB_KIND__", kind)
        .replace("__JOB_ITEMS__", items)
    )
    encoded = _encoded_ps(bootstrap)
    return (
        "@echo off\r\n"
        "rem Zork Helper tray launcher v3\r\n"
        "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command "
        "\"Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList "
        "'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand "
        + encoded
        + "'\"\r\n"
    )


def normalize_install_items(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        key = str(item or "").strip().lower()
        if key in INSTALLABLE and key not in out:
            out.append(key)
    return out


def _online_device_locked(owner: str, now: float) -> dict | None:
    live = [
        d for d in _DEVICES.values()
        if d.get("owner") == owner and (d.get("last_seen") or 0) + DEVICE_TTL_SEC > now
    ]
    if not live:
        return None
    live.sort(key=lambda d: d.get("last_seen") or 0, reverse=True)
    return live[0]


def _flush_pending_jobs_locked(owner: str, dev: dict) -> None:
    if dev.get("pending_job"):
        return
    candidates = [
        s for s in _SESSIONS.values()
        if s.get("owner") == owner and s.get("pending_job")
    ]
    if not candidates:
        return
    candidates.sort(key=lambda s: s.get("created") or 0, reverse=True)
    session = candidates[0]
    dev["pending_job"] = session["pending_job"]
    session["pending_job"] = None


def _job_payload(session: dict, kind: str, items: list[str]) -> dict:
    return {
        "kind": kind,
        "session_id": session["id"],
        "session_secret": session["secret"],
        "items": list(items),
    }


def presence_for(owner: str) -> dict:
    owner = (owner or "").strip()
    now = _now()
    with _LOCK:
        _prune_devices_locked(now)
        if not owner:
            return {"online": False, "last_seen": 0, "hostname": ""}
        dev = _online_device_locked(owner, now)
        if not dev:
            return {"online": False, "last_seen": 0, "hostname": ""}
        return {
            "online": True,
            "last_seen": dev.get("last_seen") or 0,
            "hostname": str(dev.get("hostname") or "")[:64],
        }


def queue_job(*, owner: str, session_id: str, kind: str, items=None) -> dict | None:
    owner = (owner or "").strip()
    kind = (kind or "").strip().lower()
    if kind not in {"scan", "install"}:
        return None
    wanted = normalize_install_items(items) if kind == "install" else []
    if kind == "install" and not wanted:
        return None
    now = _now()
    with _LOCK:
        row = _get_locked(session_id)
        if not row or row.get("owner") != owner:
            return None
        job = _job_payload(row, kind, wanted)
        _prune_devices_locked(now)
        dev = _online_device_locked(owner, now)
        if kind == "install" and dev:
            row["activity"] = "installing"
            _append_log_locked(row, "Zork Helper installerer det som mangler.")
        elif kind != "install" and row["status"] not in ("done", "error"):
            row["activity"] = "scanning"
        if dev:
            dev["pending_job"] = job
            row["pending_job"] = None
            return {"queued": True, "via": "tray"}
        row["pending_job"] = job
        return {"queued": True, "via": "waiting"}


def attach_session(sid: str, secret: str) -> dict | None:
    """If a tray is already up for this owner, hand it this session's job."""
    if not SECRET_RE.match(secret or ""):
        return None
    now = _now()
    with _LOCK:
        row = _get_locked(sid)
        if not row or not secrets.compare_digest(row["secret"], secret):
            return None
        _prune_devices_locked(now)
        dev = _online_device_locked(row["owner"], now)
        if not dev:
            return {"attached": False}
        job = row.get("pending_job") or _job_payload(row, "scan", [])
        row["pending_job"] = None
        if not dev.get("pending_job"):
            dev["pending_job"] = job
        if row["status"] not in ("done", "error"):
            row["status"] = "scanning"
            row["activity"] = "scanning"
        return {"attached": True}


def register_device(sid: str, secret: str, hostname: str) -> dict | None:
    if not SECRET_RE.match(secret or ""):
        return None
    host = (hostname or "").strip()
    if not HOSTNAME_RE.match(host):
        host = "pc"
    now = _now()
    with _LOCK:
        row = _get_locked(sid)
        if not row or not secrets.compare_digest(row["secret"], secret):
            return None
        _prune_devices_locked(now)
        owner = row["owner"]
        existing = None
        for dev in _DEVICES.values():
            if dev.get("owner") == owner and dev.get("hostname") == host:
                existing = dev
                break
        if existing:
            existing["secret"] = secrets.token_urlsafe(18)
            existing["last_seen"] = now
            existing["session_id"] = sid
            dev = existing
        else:
            did = secrets.token_hex(16)
            dev = {
                "id": did,
                "secret": secrets.token_urlsafe(18),
                "owner": owner,
                "hostname": host,
                "created": now,
                "last_seen": now,
                "session_id": sid,
                "pending_job": None,
            }
            _DEVICES[did] = dev
        _flush_pending_jobs_locked(owner, dev)
        return {"id": dev["id"], "secret": dev["secret"]}


def _device_locked(did: str, secret: str) -> dict | None:
    if not ID_RE.match(did or "") or not SECRET_RE.match(secret or ""):
        return None
    now = _now()
    _prune_devices_locked(now)
    row = _DEVICES.get(did)
    if not row or not secrets.compare_digest(row["secret"], secret):
        return None
    return row


def heartbeat(did: str, secret: str) -> dict | None:
    now = _now()
    with _LOCK:
        row = _device_locked(did, secret)
        if not row:
            return None
        row["last_seen"] = now
        _flush_pending_jobs_locked(row["owner"], row)
        return {"ok": True, "has_job": bool(row.get("pending_job"))}


def take_job(did: str, secret: str) -> dict | None:
    now = _now()
    with _LOCK:
        row = _device_locked(did, secret)
        if not row:
            return None
        row["last_seen"] = now
        job = row.get("pending_job")
        row["pending_job"] = None
        if not isinstance(job, dict):
            return {"job": None}
        kind = str(job.get("kind") or "")
        if kind not in {"scan", "install"}:
            return {"job": None}
        items = normalize_install_items(job.get("items")) if kind == "install" else []
        sid = str(job.get("session_id") or "")
        session_secret = str(job.get("session_secret") or "")
        if not ID_RE.match(sid) or not SECRET_RE.match(session_secret):
            return {"job": None}
        return {
            "job": {
                "kind": kind,
                "session_id": sid,
                "session_secret": session_secret,
                "items": items,
            }
        }


def _app_file_by_names(*needles: str):
    """Return (catalog row, path) for the first Apps-shelf match."""
    from core import apps as helix_apps
    wanted = tuple(n.lower().replace(" ", "") for n in needles if n)
    for item in helix_apps.list_apps():
        blob = f"{item.get('id') or ''} {item.get('name') or ''} {item.get('filename') or ''}".lower().replace(" ", "")
        if not any(n in blob for n in wanted):
            continue
        row = helix_apps.get_app(item["id"])
        if not row:
            continue
        path = helix_apps.file_path(row["id"], row.get("filename") or "")
        if path is None or not path.is_file():
            continue
        return row, path
    return None


def skoleprogrammer_file():
    """Return (catalog row, path) if Skoleprogrammer is on the Apps shelf."""
    return _app_file_by_names("skoleprogrammer")


def printer_file():
    """Return (catalog row, path) for Viken Print / SafeQ on the Apps shelf."""
    found = _app_file_by_names("printer", "vikenprint", "viken-print")
    if found:
        return found
    from core import apps as helix_apps
    helix_apps.ensure_seeds()
    for name in ("VikenPrint.msi", "vikenprint.msi"):
        path = helix_apps.file_path("printer", name)
        if path is not None:
            return {"id": "printer", "name": "printer", "filename": path.name}, path
    return None


def reset_store() -> None:
    """Tests only."""
    with _LOCK:
        _SESSIONS.clear()
        _DEVICES.clear()
