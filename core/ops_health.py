"""Operator-facing health: Claude CLI, token, prompt size, last chat turn."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.cli_backend import _find_claude_bin
from core.config import TIER_STAFF, TIER_STUDENT, get_config
from core.context_engine import build_system_prompt
from security.secrets import get_secret

CREDENTIALS = Path.home() / ".helix" / "claude" / ".credentials.json"


def claude_cli() -> dict:
    try:
        path = _find_claude_bin()
        return {"found": True, "path": path}
    except RuntimeError:
        return {"found": False, "path": ""}


def auth_status() -> dict:
    token = bool((get_secret("CLAUDE_CODE_OAUTH_TOKEN") or "").strip())
    creds = CREDENTIALS.exists()
    return {
        "oauth_token": token,
        "credentials_file": creds,
        "ready": token or creds,
    }


def prompt_sizes() -> dict:
    sizes = {}
    for tier in (TIER_STAFF, TIER_STUDENT):
        try:
            text = build_system_prompt(is_new_session=True, tier=tier, channel="web")
            sizes[tier] = len(text)
        except Exception:
            sizes[tier] = None
    return sizes


def last_chat_ts(events: list[dict]) -> Optional[float]:
    latest = None
    for e in events:
        if not str(e.get("event") or "").startswith("chat_"):
            continue
        ts = e.get("ts")
        if ts and (latest is None or ts > latest):
            latest = ts
    return latest


def report(events: Optional[list[dict]] = None) -> dict:
    """Shape consumed by GET /api/status. Never includes secret values."""
    cfg = get_config()
    events = events if events is not None else []
    return {
        "agent": cfg.agent_name or cfg.agent_id,
        "claude_cli": claude_cli(),
        "auth": auth_status(),
        "prompts": prompt_sizes(),
        "last_chat_ts": last_chat_ts(events),
    }
