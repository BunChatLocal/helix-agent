"""Suggestions and issue reports from the operator-panel Team chat.

Anyone who can open the room can file one and reply on their own reports.
Operators and Users-tab admins see every report, reply on any of them, and
set status. Moderators cannot change status.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

STORE_PATH = Path.home() / ".helix" / "desk-tickets.json"
ASSETS_DIR = Path.home() / ".helix" / "desk-ticket-assets"
_LOCK = threading.Lock()

KINDS = ("suggestion", "issue")
STATUSES = (
    "new", "waiting_response", "in_progress", "needs_context", "finished", "refused",
)
SUBJECT_MAX = 120
BODY_MAX = 4000
REPLY_MAX = 2000
IMAGES_MAX = 4
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

_STATUS_RANK = {
    "new": 0,
    "waiting_response": 1,
    "needs_context": 2,
    "in_progress": 3,
    "finished": 4,
    "refused": 5,
}
_STATUS_ALIAS = {
    "done": "finished",
    "completed": "finished",
}


def normalize_status(status: str) -> str:
    raw = (status or "").strip().lower()
    mapped = _STATUS_ALIAS.get(raw, raw)
    if mapped not in STATUSES:
        return "new"
    return mapped


def _path() -> Path:
    return STORE_PATH


def _load() -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        status = normalize_status(row.get("status") or "")
        row["status"] = status
        if row.get("reporter_seen") is None:
            row["reporter_seen"] = row.get("updated") or row.get("ts") or 0
        out.append(row)
    return out


def _save(rows: list[dict]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _clip(text: str, limit: int) -> str:
    body = (text or "").strip()
    if len(body) > limit:
        return body[:limit]
    return body


def _author(name: str, email: str, role: str) -> dict:
    return {
        "name": (name or "").strip() or "unknown",
        "email": (email or "").strip(),
        "role": (role or "").strip(),
    }


def save_image(data: bytes, suffix: str) -> str:
    suffix = (suffix or "").lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    if suffix not in IMAGE_SUFFIXES:
        raise ValueError("unsupported image type")
    if not data:
        raise ValueError("empty image")
    name = f"{uuid.uuid4().hex}{suffix}"
    root = ASSETS_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(data)
    return name


def image_path(name: str) -> Path | None:
    if not _IMAGE_NAME_RE.match(name or ""):
        return None
    root = ASSETS_DIR.resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = (root / Path(name).name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    return path


def _image_names(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = str(item or "").strip()
        if not name or name in seen or not _IMAGE_NAME_RE.match(name):
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= IMAGES_MAX:
            break
    return out


def _existing_images(value) -> list[str]:
    return [name for name in _image_names(value) if image_path(name) is not None]


def ticket_for_image(name: str) -> dict | None:
    name = (name or "").strip()
    if not _IMAGE_NAME_RE.match(name):
        return None
    for row in _load():
        if name in _image_names(row.get("images")):
            return row
        for reply in row.get("replies") or []:
            if isinstance(reply, dict) and name in _image_names(reply.get("images")):
                return row
    return None


def create(
    kind: str,
    subject: str,
    description: str,
    *,
    name: str,
    email: str = "",
    role: str = "",
    images=None,
) -> dict:
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError("kind must be suggestion or issue")
    subject = _clip(subject, SUBJECT_MAX)
    description = _clip(description, BODY_MAX)
    if not subject:
        raise ValueError("subject is required")
    if not description:
        raise ValueError("description is required")
    now = time.time()
    entry = {
        "id": str(uuid.uuid4()),
        "ts": now,
        "updated": now,
        "kind": kind,
        "subject": subject,
        "description": description,
        "status": "new",
        "reporter_seen": now,
        **_author(name, email, role),
        "images": _existing_images(images),
        "replies": [],
    }
    with _LOCK:
        rows = _load()
        rows.append(entry)
        _save(rows)
    return entry


def list_all() -> list[dict]:
    rows = _load()
    rows.sort(key=lambda t: (
        _STATUS_RANK.get(t.get("status") or "", 9),
        -(t.get("updated") or t.get("ts") or 0),
    ))
    return rows


def owner_key(email: str = "", role: str = "") -> str:
    from core.desk_chat import identity
    return identity(email, role)


def is_owner(ticket: dict, email: str = "", role: str = "") -> bool:
    return owner_key(ticket.get("email") or "", ticket.get("role") or "") == owner_key(email, role)


def list_for(email: str = "", role: str = "", *, inbox: bool) -> list[dict]:
    rows = list_all()
    if inbox:
        return rows
    key = owner_key(email, role)
    return [t for t in rows if owner_key(t.get("email") or "", t.get("role") or "") == key]


def new_count_for(email: str = "", role: str = "", *, inbox: bool) -> int:
    return sum(1 for row in list_for(email, role, inbox=inbox) if row.get("status") == "new")


def get(ticket_id: str) -> dict | None:
    if not ticket_id:
        return None
    for row in _load():
        if row.get("id") == ticket_id:
            return row
    return None


def set_status(ticket_id: str, status: str) -> dict | None:
    raw = (status or "").strip().lower()
    status = _STATUS_ALIAS.get(raw, raw)
    if status not in STATUSES:
        raise ValueError("unknown status")
    with _LOCK:
        rows = _load()
        found = None
        for row in rows:
            if row.get("id") == ticket_id:
                row["status"] = status
                row["updated"] = time.time()
                found = row
                break
        if found is None:
            return None
        _save(rows)
        return found


def reply(
    ticket_id: str,
    text: str,
    *,
    name: str,
    email: str = "",
    role: str = "",
    staff: bool = False,
    images=None,
) -> dict | None:
    body = _clip(text, REPLY_MAX)
    shots = _existing_images(images)
    if not body and not shots:
        raise ValueError("text or image is required")
    with _LOCK:
        rows = _load()
        found = None
        for row in rows:
            if row.get("id") == ticket_id:
                item = {
                    "id": str(uuid.uuid4()),
                    "ts": time.time(),
                    "text": body,
                    "images": shots,
                    **_author(name, email, role),
                }
                replies = list(row.get("replies") or [])
                replies.append(item)
                row["replies"] = replies
                row["updated"] = item["ts"]
                if staff and row.get("status") == "waiting_response":
                    row["status"] = "in_progress"
                if is_owner(row, email, role):
                    row["reporter_seen"] = item["ts"]
                found = row
                break
        if found is None:
            return None
        _save(rows)
        return found


def is_unseen_for_reporter(row: dict) -> bool:
    updated = float(row.get("updated") or row.get("ts") or 0)
    seen = float(row.get("reporter_seen") or 0)
    return updated > seen + 0.01


def mark_reporter_seen(ticket_id: str, email: str = "", role: str = "") -> dict | None:
    if not ticket_id:
        return None
    with _LOCK:
        rows = _load()
        found = None
        for row in rows:
            if row.get("id") != ticket_id:
                continue
            if not is_owner(row, email, role):
                return None
            row["reporter_seen"] = time.time()
            found = row
            break
        if found is None:
            return None
        _save(rows)
        return found


def new_count() -> int:
    return sum(1 for row in _load() if row.get("status") == "new")
