"""
Helix Session Manager
aiosqlite-backed session index + JSONL transcripts per session.
Session routing: per-channel-peer (channel + sender_id = session key).
"""

import asyncio
import json
import logging
import sqlite3
import uuid
import zoneinfo
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger("helix.session")

from core.config import TIER_STUDENT, get_config

SESSIONS_DIR = Path.home() / ".helix" / "sessions"


def _tz():
    return zoneinfo.ZoneInfo(get_config().timezone)

# ─── Schema ───────────────────────────────────────────────────────────────────

CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    agent_id          TEXT NOT NULL,
    channel           TEXT NOT NULL,
    peer              TEXT NOT NULL,
    created_at        REAL NOT NULL,
    last_active       REAL NOT NULL,
    model             TEXT NOT NULL,
    compacted         INTEGER NOT NULL DEFAULT 0,
    token_count       INTEGER NOT NULL DEFAULT 0,
    claude_session_id TEXT
);
"""

MIGRATE_ADD_CLAUDE_SESSION_ID = """
ALTER TABLE sessions ADD COLUMN claude_session_id TEXT;
"""

MIGRATE_ADD_PENDING_CONTEXT = """
ALTER TABLE sessions ADD COLUMN pending_context TEXT;
"""

MIGRATE_ADD_CONTEXT_WINDOW = """
ALTER TABLE sessions ADD COLUMN context_window INTEGER NOT NULL DEFAULT 0;
"""

MIGRATE_ADD_MAX_OUTPUT_TOKENS = """
ALTER TABLE sessions ADD COLUMN max_output_tokens INTEGER NOT NULL DEFAULT 0;
"""

# How much context the MOST RECENT turn consumed. token_count is cumulative
# across the session's lifetime, so it can't answer "how full is the window" —
# this can.
MIGRATE_ADD_LAST_CONTEXT_TOKENS = """
ALTER TABLE sessions ADD COLUMN last_context_tokens INTEGER NOT NULL DEFAULT 0;
"""

# Permission tier of whoever owns this conversation — see ROLES.md. Held on the
# session rather than derived from the peer key, so retention can treat a pupil's
# conversation differently without changing the key format and orphaning every
# conversation that already exists. NULL means "signed in before tiers existed";
# it is filled in the next time they connect.
MIGRATE_ADD_TIER = """
ALTER TABLE sessions ADD COLUMN tier TEXT;
"""

# Number of user/assistant exchange pairs to carry forward on model switch
CONTEXT_EXCHANGES = 30

CREATE_IDX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_channel_peer
    ON sessions (agent_id, channel, peer);
"""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _db_path(agent_id: str) -> Path:
    p = SESSIONS_DIR / agent_id
    p.mkdir(parents=True, exist_ok=True)
    return p / "sessions.db"


def _transcript_path(agent_id: str, session_id: str) -> Path:
    p = SESSIONS_DIR / agent_id
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{session_id}.jsonl"


def _now_ts() -> float:
    return datetime.now(_tz()).timestamp()


def _remove_transcripts(agent_id: str, session_id: str) -> None:
    """Delete the live transcript and any leftover archive. Never raises."""
    live = _transcript_path(agent_id, session_id)
    for path in (live, live.with_suffix(".jsonl.archived")):
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning(f"Could not delete transcript {path}: {exc}")


def _reset_due(last_active_ts: float, cfg) -> bool:
    """True if the daily reset has passed since last activity (uses configured timezone)."""
    cfg_session = cfg.session
    tz = _tz()
    last = datetime.fromtimestamp(last_active_ts, tz)
    now = datetime.now(tz)
    # Build today's reset time
    reset_today = now.replace(
        hour=cfg_session.daily_reset_hour,
        minute=0, second=0, microsecond=0,
    )
    # If now is before today's reset, check yesterday's reset
    if now < reset_today:
        reset_today -= timedelta(days=1)
    return last < reset_today


# ─── Session Manager ──────────────────────────────────────────────────────────

class SessionManager:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    def _backup_db(self, keep: int = 7) -> None:
        """Snapshot sessions.db before migrations run.

        Conversations must survive code updates. The data already lives outside
        the repo (~/.helix), so `git pull` can't touch it — the real risk is a
        schema migration or a crash mid-write corrupting the file. This keeps
        the last `keep` daily snapshots next to it.
        """
        src = _db_path(self.agent_id)
        if not src.exists() or src.stat().st_size == 0:
            return
        try:
            backup_dir = src.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(_tz()).strftime("%Y%m%d")
            dest = backup_dir / f"sessions-{stamp}.db"
            if not dest.exists():
                # sqlite3's backup API copies a consistent snapshot even if
                # another connection is mid-write; a file copy would not.
                with sqlite3.connect(src) as s, sqlite3.connect(dest) as d:
                    s.backup(d)
                logger.info(f"Session database backed up to {dest.name}")
            snaps = sorted(backup_dir.glob("sessions-*.db"))
            for old in snaps[:-keep]:
                old.unlink(missing_ok=True)
        except Exception as exc:
            # Never let a backup problem stop the agent from starting.
            logger.warning(f"Could not back up session database: {exc}")

    async def start(self) -> None:
        self._backup_db()
        self._db = await aiosqlite.connect(_db_path(self.agent_id))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(CREATE_SESSIONS)
        await self._db.execute(CREATE_IDX)
        # Migrations — add columns if they don't exist yet.
        # Catch only OperationalError (SQLite's "duplicate column" signal) and
        # log everything else so real schema problems are not silently swallowed.
        for migration in (MIGRATE_ADD_CLAUDE_SESSION_ID, MIGRATE_ADD_PENDING_CONTEXT,
                          MIGRATE_ADD_CONTEXT_WINDOW, MIGRATE_ADD_MAX_OUTPUT_TOKENS,
                          MIGRATE_ADD_LAST_CONTEXT_TOKENS, MIGRATE_ADD_TIER):
            try:
                await self._db.execute(migration)
            except sqlite3.OperationalError:
                pass  # Column already exists — expected on subsequent starts
            except Exception as exc:
                logger.error(f"Unexpected migration error: {exc}", exc_info=True)
                raise
        await self._db.commit()

    async def stop(self) -> None:
        if self._db:
            await self._db.close()

    async def get_or_create(
        self,
        channel: str,
        peer: str,
        model: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> dict:
        """Find or open the session for (channel, peer).

        `tier` is the caller's permission tier, where the caller is a signed-in
        person. Pass None from anywhere that has no notion of one — heartbeats,
        cron jobs, the admin session endpoints — and the stored value is left
        alone rather than cleared.
        """
        cfg = get_config()
        if model is None:
            # Pupil /chat uses the fast model. Staff /chat stays on the
            # default (sonnet): MFA reset and similar procedures go wrong on
            # haiku. Discord and the queue keep the general default.
            if channel == "web" and tier == TIER_STUDENT:
                model = cfg.models.chat_id
            else:
                model = cfg.models.default_id

        async with self._lock:
            # Try to find existing
            async with self._db.execute(
                "SELECT * FROM sessions WHERE agent_id=? AND channel=? AND peer=?",
                (self.agent_id, channel, peer),
            ) as cur:
                row = await cur.fetchone()

            if row:
                session = dict(row)
                # Daily reset exists so a single always-on personal session
                # doesn't accumulate context forever. It must NOT apply to the
                # staff web chat, where peers are "<user>|<conversation-id>":
                # those are conversations the user creates, revisits and deletes
                # deliberately, and are governed by retention_days instead.
                # Without this guard, simply opening yesterday's conversation
                # deletes it and archives the transcript.
                explicit_conversation = "|" in str(session.get("peer", ""))
                if not explicit_conversation and _reset_due(session["last_active"], cfg):
                    await self._db.execute(
                        "DELETE FROM sessions WHERE session_id=?",
                        (session["session_id"],),
                    )
                    await self._db.commit()
                    # Archive old transcript
                    old_path = _transcript_path(self.agent_id, session["session_id"])
                    if old_path.exists():
                        archive = old_path.with_suffix(".jsonl.archived")
                        old_path.rename(archive)
                    session = None

            else:
                session = None

            if session is None:
                session_id = str(uuid.uuid4())
                now = _now_ts()
                await self._db.execute(
                    """INSERT INTO sessions
                       (session_id, agent_id, channel, peer, created_at, last_active, model, claude_session_id, tier)
                       VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                    (session_id, self.agent_id, channel, peer, now, now, model, tier),
                )
                await self._db.commit()
                session = {
                    "session_id": session_id,
                    "agent_id": self.agent_id,
                    "channel": channel,
                    "peer": peer,
                    "created_at": now,
                    "last_active": now,
                    "model": model,
                    "compacted": 0,
                    "token_count": 0,
                    "claude_session_id": None,
                    "tier": tier,
                }

            elif tier and session.get("tier") != tier:
                # Labels conversations that predate the tier column, and follows
                # anyone whose tier legitimately changes — a pupil who leaves and
                # comes back as an employee keeps their conversations but not
                # their old permissions.
                await self._db.execute(
                    "UPDATE sessions SET tier=? WHERE session_id=?",
                    (tier, session["session_id"]),
                )
                await self._db.commit()
                session["tier"] = tier

            return session

    async def update_activity(self, session_id: str, usage: Optional[dict] = None) -> None:
        """Update last_active and accumulate token usage stats from a CLI response."""
        token_delta = 0
        context_window = 0
        max_output_tokens = 0
        last_context = 0
        if usage:
            token_delta = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            context_window = usage.get("context_window", 0)
            max_output_tokens = usage.get("max_output_tokens", 0)
            # Everything the model had to read this turn — fresh input plus what
            # came back from cache. This is the session's current occupancy of
            # the context window.
            last_context = (
                usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )

        async with self._lock:
            if context_window:
                await self._db.execute(
                    """UPDATE sessions
                       SET last_active=?, token_count=token_count+?,
                           context_window=?, max_output_tokens=?,
                           last_context_tokens=?
                       WHERE session_id=?""",
                    (_now_ts(), token_delta, context_window, max_output_tokens,
                     last_context, session_id),
                )
            else:
                await self._db.execute(
                    "UPDATE sessions SET last_active=?, token_count=token_count+? WHERE session_id=?",
                    (_now_ts(), token_delta, session_id),
                )
            await self._db.commit()

    async def _build_context_block(self, session_id: str) -> Optional[str]:
        """Format the last CONTEXT_EXCHANGES user/assistant pairs as a context block.

        Uses async_read_transcript so the file read is offloaded to a thread
        executor and does not block the event loop while the session lock is held.
        """
        messages = await self.async_read_transcript(session_id)
        if not messages:
            return None
        # Take last N exchange pairs (user + assistant = 2 messages per exchange)
        tail = messages[-(CONTEXT_EXCHANGES * 2):]
        lines = [
            "[Context from previous session — carried forward after model switch]",
            "",
        ]
        for msg in tail:
            role = "User" if msg["role"] == "user" else "Assistant"
            content = msg["content"]
            # Truncate very long messages to keep token cost reasonable
            if len(content) > 2000:
                content = content[:2000] + "... [truncated]"
            lines.append(f"{role}: {content}")
        lines += ["", "[End of previous context. Continue from here with the new model.]"]
        return "\n".join(lines)

    async def set_model(self, session_id: str, model_id: str) -> None:
        """Switch model for a session.

        Saves the last CONTEXT_EXCHANGES exchanges as pending_context so the
        first turn on the new session can pick up where the old one left off.
        Clears claude_session_id so the next turn starts a fresh Claude Code session.

        If pending_context is already set (e.g. from a prior /compact), it is
        preserved as-is rather than overwritten with a transcript-derived block.
        """
        # Build the context block BEFORE acquiring the lock: the async file read
        # must not block the event loop while we hold the session lock.
        async with self._db.execute(
            "SELECT pending_context FROM sessions WHERE session_id=?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()

        existing = row["pending_context"] if row else None
        # Build context outside the lock (async-safe file read)
        context_block = existing if existing else await self._build_context_block(session_id)

        async with self._lock:
            await self._db.execute(
                "UPDATE sessions SET model=?, claude_session_id=NULL, pending_context=? WHERE session_id=?",
                (model_id, context_block, session_id),
            )
            await self._db.commit()

    async def pop_pending_context(self, session_id: str) -> Optional[str]:
        """Atomically read and clear pending_context. Returns None if not set."""
        async with self._lock:
            async with self._db.execute(
                "SELECT pending_context FROM sessions WHERE session_id=?",
                (session_id,),
            ) as cur:
                row = await cur.fetchone()
            if not row or not row["pending_context"]:
                return None
            context = row["pending_context"]
            await self._db.execute(
                "UPDATE sessions SET pending_context=NULL WHERE session_id=?",
                (session_id,),
            )
            await self._db.commit()
            return context

    async def compact_session(self, session_id: str, summary: str) -> None:
        """Store compaction summary as pending_context, clear claude_session_id, mark compacted."""
        context_block = (
            "[Compacted session summary — loaded on session resume]\n\n"
            f"{summary}\n\n"
            "[End of compacted context. Continue the conversation from here.]"
        )
        async with self._lock:
            # last_active is bumped too: retention deletes by inactivity, and
            # compacting is how a user says "keep this conversation". Without
            # this the UI would offer to save a chat and then delete it anyway.
            # last_context_tokens resets because the window starts fresh from
            # the summary — otherwise the user stays blocked after compacting.
            await self._db.execute(
                """UPDATE sessions
                   SET pending_context=?, claude_session_id=NULL,
                       compacted=compacted+1, last_active=?, last_context_tokens=0
                   WHERE session_id=?""",
                (context_block, _now_ts(), session_id),
            )
            await self._db.commit()

    async def touch(self, session_id: str) -> None:
        """Refresh last_active without changing anything else.

        Used when a user asks to keep a conversation that is too short to be
        worth compacting — the retention clock still needs resetting.
        """
        async with self._lock:
            await self._db.execute(
                "UPDATE sessions SET last_active=? WHERE session_id=?",
                (_now_ts(), session_id),
            )
            await self._db.commit()

    async def set_claude_session_id(self, session_id: str, claude_session_id: str) -> None:
        """Persist the Claude Code session ID for --resume on future turns."""
        async with self._lock:
            await self._db.execute(
                "UPDATE sessions SET claude_session_id=? WHERE session_id=?",
                (claude_session_id, session_id),
            )
            await self._db.commit()

    async def forget_model_sessions(self, channel: str) -> int:
        """Drop Claude session ids for a channel so the next turn is a new context.

        Transcripts stay. Used when an old model session must not keep carrying
        operator identity that leaked into it.
        """
        async with self._lock:
            cur = await self._db.execute(
                "UPDATE sessions SET claude_session_id=NULL "
                "WHERE agent_id=? AND channel=? AND claude_session_id IS NOT NULL",
                (self.agent_id, channel),
            )
            await self._db.commit()
            return cur.rowcount or 0

    async def rekey_peer(self, session_id: str, new_peer: str) -> bool:
        """Move a conversation to another peer key without touching the transcript.

        Used when sign-in changes (Entra object-id keys → email keys) so the
        same person still sees their chats. False if the new key is already taken.
        """
        async with self._lock:
            try:
                cur = await self._db.execute(
                    "UPDATE sessions SET peer=? WHERE session_id=? AND agent_id=?",
                    (new_peer, session_id, self.agent_id),
                )
                await self._db.commit()
            except (aiosqlite.IntegrityError, sqlite3.IntegrityError):
                return False
            return (cur.rowcount or 0) > 0

    async def reset_session(self, channel: str, peer: str) -> None:
        """Force-reset: delete the session row, transcript, and Claude resume id.

        Nothing is left to send back to the model on the next turn — the
        conversation no longer occupies context. Callers that need a paper
        trail (the /chat delete path) write it before calling this.
        """
        async with self._lock:
            async with self._db.execute(
                "SELECT session_id FROM sessions WHERE agent_id=? AND channel=? AND peer=?",
                (self.agent_id, channel, peer),
            ) as cur:
                row = await cur.fetchone()
            if row:
                sid = row["session_id"]
                await self._db.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
                await self._db.commit()
                _remove_transcripts(self.agent_id, sid)

    async def purge_older_than(self, days: int, student_days: int = 0) -> int:
        """Delete sessions untouched past their retention window.

        Data-retention control: conversations hold staff and pupil details, so
        they must not accumulate indefinitely. Pupils get the shorter
        `student_days` window; everyone else, including sessions predating tiers,
        gets `days`. Returns the number removed.

        A pupil conversation is kept alive by being used, and `keep_summary`
        rewrites one down to its solution and touches it — so the clock resets
        rather than the record being exempted.
        """
        if days <= 0:
            return 0
        now = _now_ts()
        if student_days and student_days > 0:
            windows = [
                ("pupil", student_days, "tier = ?", (TIER_STUDENT,)),
                ("other", days, "IFNULL(tier,'') != ?", (TIER_STUDENT,)),
            ]
        else:
            windows = [("all", days, "1=1", ())]

        async with self._lock:
            doomed: list[tuple[str, str]] = []
            for label, window, clause, params in windows:
                async with self._db.execute(
                    f"SELECT session_id FROM sessions "
                    f"WHERE agent_id=? AND last_active < ? AND {clause}",
                    (self.agent_id, now - window * 86400, *params),
                ) as cur:
                    for row in await cur.fetchall():
                        sid = row["session_id"] if not isinstance(row, tuple) else row[0]
                        doomed.append((sid, label))

            removed = 0
            per_label: dict[str, int] = {}
            for sid, label in doomed:
                _remove_transcripts(self.agent_id, sid)
                await self._db.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
                removed += 1
                per_label[label] = per_label.get(label, 0) + 1

            await self._db.commit()

        if removed:
            detail = ", ".join(f"{n} {label}" for label, n in sorted(per_label.items()))
            logger.info(f"Retention: purged {removed} session(s) — {detail}")
        return removed

    async def context_status(self, channel: str, peer: str, limit_pct: float) -> dict:
        """How full this conversation's context window is.

        Returns used/limit/percent and whether the user must compact before
        sending again. Before the first reply there are no usage figures yet,
        so nothing is ever blocked on an empty conversation.
        """
        async with self._db.execute(
            "SELECT * FROM sessions WHERE agent_id=? AND channel=? AND peer=?",
            (self.agent_id, channel, peer),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return {"used": 0, "window": 0, "percent": 0.0, "blocked": False}

        s = dict(row)
        window = s.get("context_window") or 0
        used = s.get("last_context_tokens") or 0
        if not window or not used:
            return {"used": used, "window": window, "percent": 0.0, "blocked": False}

        # Reserve room for the reply itself, so we block before a turn would
        # overflow rather than after it already has.
        headroom = s.get("max_output_tokens") or 0
        usable = max(window - headroom, 1)
        pct = min(used / usable, 1.0)
        return {
            "used": used,
            "window": window,
            "percent": round(pct * 100, 1),
            "blocked": pct >= limit_pct,
        }

    async def list_sessions(self) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM sessions WHERE agent_id=? ORDER BY last_active DESC",
            (self.agent_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── Transcript ──────────────────────────────────────────────────────────

    def _append_message_sync(self, session_id: str, role: str, content) -> None:
        """Synchronous implementation of append_message (runs in thread executor)."""
        path = _transcript_path(self.agent_id, session_id)
        entry = {
            "ts": _now_ts(),
            "role": role,
            "content": content,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    async def append_message(self, session_id: str, role: str, content) -> None:
        """Append a message to the JSONL transcript.

        Awaits the executor write so callers get ordering guarantees and
        exceptions propagate correctly instead of being silently swallowed.
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._append_message_sync, session_id, role, content)

    def _read_transcript_sync(self, session_id: str) -> list[dict]:
        """Synchronous implementation of read_transcript (runs in thread executor)."""
        path = _transcript_path(self.agent_id, session_id)
        if not path.exists():
            return []
        messages = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                messages.append({"role": entry["role"], "content": entry["content"]})
        return messages

    def read_transcript(self, session_id: str) -> list[dict]:
        """Read the full transcript as a list of {role, content} dicts.

        This method keeps a synchronous signature for compatibility with callers
        that cannot await (e.g. snapshot route, context builder).  For the
        /compact path the heavy read is offloaded via async_read_transcript.
        """
        return self._read_transcript_sync(session_id)

    async def async_read_transcript(self, session_id: str) -> list[dict]:
        """Non-blocking version of read_transcript for use in async coroutines."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._read_transcript_sync, session_id)

    def overwrite_transcript(self, session_id: str, messages: list[dict]) -> None:
        """Replace the transcript with a new set of messages (used after compaction)."""
        path = _transcript_path(self.agent_id, session_id)
        now = _now_ts()
        with open(path, "w", encoding="utf-8") as f:
            for msg in messages:
                entry = {"ts": now, "role": msg["role"], "content": msg["content"]}
                f.write(json.dumps(entry) + "\n")

