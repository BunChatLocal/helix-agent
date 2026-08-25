"""Shared desk chat for people who can open the operator panel.

One room. Messages outlive the browser tab so an issue reported at 09:00 is
still there when someone opens the panel at 14:00. Not a pupil or staff /chat
thread — those are private with the agent.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

STORE_PATH = Path.home() / ".helix" / "desk-chat.jsonl"
READ_PATH = Path.home() / ".helix" / "desk-chat-read.json"
_LOCK = threading.Lock()

MAX_CHARS = 2000
HISTORY_LIMIT = 200


def _path() -> Path:
    return STORE_PATH


def identity(email: str = "", role: str = "") -> str:
    """Stable key for who last opened the room."""
    mail = (email or "").strip().lower()
    if mail:
        return mail
    if (role or "") == "operator":
        return "operator"
    return (role or "unknown")


def _reads() -> dict:
    path = READ_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def mark_read(key: str) -> None:
    if not key:
        return
    with _LOCK:
        data = _reads()
        data[key] = time.time()
        READ_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = READ_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(READ_PATH)


def unread_count(key: str) -> int:
    """Messages after this person last opened the room, not counting their own."""
    last = float(_reads().get(key) or 0)
    n = 0
    for m in history():
        if (m.get("ts") or 0) <= last:
            continue
        author = identity(m.get("email") or "", m.get("role") or "")
        if author == key:
            continue
        n += 1
    return n


def history(limit: int = HISTORY_LIMIT) -> list[dict]:
    """Oldest first, most recent `limit` lines."""
    path = _path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("text"):
            out.append(entry)
    return out[-limit:]


def post(text: str, *, name: str, email: str = "", role: str = "") -> dict:
    """Append one message. Empty text is refused by the caller."""
    body = (text or "").strip()
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS]
    entry = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "name": (name or "").strip() or "unknown",
        "email": (email or "").strip(),
        "role": (role or "").strip(),
        "text": body,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    path = _path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    return entry
