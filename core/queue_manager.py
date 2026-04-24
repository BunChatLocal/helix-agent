"""
Helix Task Queue
SQLite-backed queue for background agent tasks.

Tasks are processed one at a time by a background asyncio loop. Each task
prompt is wrapped with a completion-summary instruction so the agent produces
a structured summary block that can be extracted and sent as a notification.

Summary block format (injected into every queued prompt):
    ---TASK COMPLETE---
    ✅ Done: [bullet points]
    📁 Changed: [files or "none"]
    ⚠️ Issues: [problems or "none"]
    ---END---
"""

import asyncio
import logging
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

from core.config import get_config

logger = logging.getLogger("helix.queue")

QUEUE_DB_PATH = Path.home() / ".helix" / "queue.db"
QUEUE_MAX_PENDING = 50        # cap on queued-but-not-yet-run tasks
QUEUE_POLL_SECONDS = 2        # how often the processor checks for new work
TASK_SUMMARY_FENCE_START = "---TASK COMPLETE---"
TASK_SUMMARY_FENCE_END   = "---END---"
SUMMARY_FALLBACK_CHARS   = 400

_SUMMARY_RE = re.compile(
    r"---TASK COMPLETE---\s*\n(.*?)\n---END---",
    re.DOTALL,
)

# ─── Prompt wrapper ───────────────────────────────────────────────────────────

_SUMMARY_INSTRUCTION = f"""
After you have fully completed every part of this task, append the following \
block at the very end of your response (do NOT omit it):

{TASK_SUMMARY_FENCE_START}
✅ Done:
- [what you accomplished — 2-4 bullet points]
📁 Changed: [comma-separated list of files created/modified, or "none"]
⚠️ Issues: [any problems encountered or things left incomplete, or "none"]
{TASK_SUMMARY_FENCE_END}

---
TASK:
""".lstrip()


def _wrap_prompt(prompt: str) -> str:
    return _SUMMARY_INSTRUCTION + prompt


def extract_summary(response: str) -> str:
    """Pull the structured summary block out of the agent response.

    Returns the raw content between the fences.  Falls back to the first
    SUMMARY_FALLBACK_CHARS characters of the response if no block is found.
    """
    m = _SUMMARY_RE.search(response)
    if m:
        return m.group(1).strip()
    # Fallback: first N chars, trimmed cleanly at a sentence/line boundary
    preview = response[:SUMMARY_FALLBACK_CHARS].strip()
    if len(response) > SUMMARY_FALLBACK_CHARS:
        # Trim to last complete line to avoid mid-sentence cuts
        last_nl = preview.rfind("\n")
        if last_nl > 100:
            preview = preview[:last_nl]
        preview += "\n…"
    return preview


# ─── Schema ───────────────────────────────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS task_queue (
    id           TEXT PRIMARY KEY,
    prompt       TEXT NOT NULL,
    channel      TEXT NOT NULL,
    peer         TEXT NOT NULL,
    model        TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   REAL NOT NULL,
    started_at   REAL,
    completed_at REAL,
    result       TEXT,
    summary      TEXT,
    error        TEXT
);
"""

_CREATE_IDX = """
CREATE INDEX IF NOT EXISTS idx_queue_status ON task_queue (status, created_at);
"""


# ─── Queue Manager ────────────────────────────────────────────────────────────

class QueueManager:
    """Manages the task queue DB and drives the background processor loop."""

    def __init__(self):
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._processor_task: Optional[asyncio.Task] = None
        self._agent_loop = None        # set via set_agent_loop()
        self._notification_svc = None  # set via set_notification_service()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        QUEUE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(QUEUE_DB_PATH))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(_CREATE_TABLE)
        await self._db.execute(_CREATE_IDX)
        await self._db.commit()
        self._processor_task = asyncio.create_task(
            self._process_loop(), name="helix-queue-processor"
        )
        logger.info("Task queue started")

    async def stop(self) -> None:
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        if self._db:
            await self._db.close()

    def set_agent_loop(self, agent_loop) -> None:
        self._agent_loop = agent_loop

    def set_notification_service(self, svc) -> None:
        self._notification_svc = svc

    # ── Public API ─────────────────────────────────────────────────────────

    async def add_task(
        self,
        prompt: str,
        channel: str,
        peer: str,
        model: Optional[str] = None,
    ) -> Optional[dict]:
        """Enqueue a new task. Returns the task dict or None if queue is full."""
        async with self._lock:
            async with self._db.execute(
                "SELECT COUNT(*) AS n FROM task_queue WHERE status='pending'"
            ) as cur:
                row = await cur.fetchone()
            if row["n"] >= QUEUE_MAX_PENDING:
                logger.warning("Task queue full (%d pending)", QUEUE_MAX_PENDING)
                return None

            task_id = uuid.uuid4().hex[:8]
            now = datetime.utcnow().timestamp()
            await self._db.execute(
                """INSERT INTO task_queue
                   (id, prompt, channel, peer, model, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (task_id, prompt, channel, peer, model, now),
            )
            await self._db.commit()

        logger.info("Task queued: %s — %.60s…", task_id, prompt)
        return await self.get_task(task_id)

    async def get_task(self, task_id: str) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM task_queue WHERE id=?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_tasks(self, limit: int = 30) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM task_queue ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a pending task. Running tasks cannot be cancelled."""
        async with self._lock:
            async with self._db.execute(
                "SELECT status FROM task_queue WHERE id=?", (task_id,)
            ) as cur:
                row = await cur.fetchone()
            if not row or row["status"] != "pending":
                return False
            await self._db.execute(
                "DELETE FROM task_queue WHERE id=? AND status='pending'",
                (task_id,),
            )
            await self._db.commit()
        return True

    async def clear_done(self) -> int:
        """Remove all completed/errored tasks. Returns count removed."""
        async with self._lock:
            async with self._db.execute(
                "SELECT COUNT(*) AS n FROM task_queue WHERE status IN ('done','error')"
            ) as cur:
                row = await cur.fetchone()
            n = row["n"]
            await self._db.execute(
                "DELETE FROM task_queue WHERE status IN ('done','error')"
            )
            await self._db.commit()
        return n

    async def queue_depth(self) -> dict:
        """Return counts by status."""
        async with self._db.execute(
            """SELECT status, COUNT(*) AS n FROM task_queue GROUP BY status"""
        ) as cur:
            rows = await cur.fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ── Background processor ───────────────────────────────────────────────

    async def _process_loop(self) -> None:
        """Continuously poll for pending tasks and run them one at a time."""
        logger.debug("Queue processor started")
        while True:
            try:
                await self._run_next()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Queue processor error: %s", exc, exc_info=True)
            await asyncio.sleep(QUEUE_POLL_SECONDS)

    async def _run_next(self) -> None:
        """Pick the oldest pending task and execute it."""
        if not self._agent_loop:
            return

        # Claim the next pending task atomically
        async with self._lock:
            async with self._db.execute(
                """SELECT * FROM task_queue
                   WHERE status='pending'
                   ORDER BY created_at ASC
                   LIMIT 1"""
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return
            task = dict(row)
            now = datetime.utcnow().timestamp()
            await self._db.execute(
                "UPDATE task_queue SET status='running', started_at=? WHERE id=?",
                (now, task["id"]),
            )
            await self._db.commit()

        logger.info("Running queued task %s: %.60s…", task["id"], task["prompt"])

        cfg = get_config()
        model = task["model"] or cfg.models.default_id
        wrapped_prompt = _wrap_prompt(task["prompt"])

        try:
            result = await self._agent_loop._collect(
                "queue", task["id"], wrapped_prompt, model
            )
            if result is None:
                result = "(no response)"

            summary = extract_summary(result)
            now = datetime.utcnow().timestamp()

            async with self._lock:
                await self._db.execute(
                    """UPDATE task_queue
                       SET status='done', completed_at=?, result=?, summary=?
                       WHERE id=?""",
                    (now, result, summary, task["id"]),
                )
                await self._db.commit()

            logger.info("Task %s done. Summary: %.120s", task["id"], summary)
            await self._notify(task, summary, error=False)

        except Exception as exc:
            error_msg = str(exc)
            logger.error("Task %s failed: %s", task["id"], error_msg, exc_info=True)
            now = datetime.utcnow().timestamp()
            async with self._lock:
                await self._db.execute(
                    """UPDATE task_queue
                       SET status='error', completed_at=?, error=?
                       WHERE id=?""",
                    (now, error_msg, task["id"]),
                )
                await self._db.commit()
            await self._notify(task, f"❌ Task failed: {error_msg[:200]}", error=True)

    async def _notify(self, task: dict, summary: str, error: bool) -> None:
        """Fire completion notification if service is available."""
        if not self._notification_svc:
            return
        # Truncate prompt for display
        prompt_preview = task["prompt"][:80].rstrip()
        if len(task["prompt"]) > 80:
            prompt_preview += "…"
        status_icon = "❌" if error else "✅"
        message = (
            f"{status_icon} **Queued task complete**\n"
            f"**Task:** {prompt_preview}\n\n"
            f"{summary}"
        )
        try:
            await self._notification_svc.notify(message)
        except Exception as exc:
            logger.warning("Notification failed: %s", exc)
