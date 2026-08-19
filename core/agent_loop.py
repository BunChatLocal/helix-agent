"""
Helix Agent Loop
Routes all AI calls through `claude -p` CLI (Max subscription, no extra usage).
Session continuity: each (channel, peer) pair maps to a persistent Claude Code
session resumed with --resume. New day = new session (daily reset).
NO_REPLY sentinel suppresses responses (heartbeat use).
"""

import asyncio
import logging
import re
import traceback
import weakref
from pathlib import Path
from typing import AsyncGenerator, Optional

from core.config import get_config, TIER_STAFF, TIER_STUDENT
from core.session_manager import SessionManager
from core.context_engine import build_system_prompt, STUDENT_TURN_MARKER
from core.cli_backend import call_claude

logger = logging.getLogger("helix.agent")

NO_REPLY = "NO_REPLY"

# The web layer prepends one of these per upload so the agent can open the file.
# A pupil's agent has no file tools, so for that tier the path is pulled back out
# and the image is sent inside the message instead. Staff keep the marker and open
# the file themselves, which is also what lets them turn a screenshot into a
# permanent documentation asset.
_ATTACHED_IMAGE = re.compile(r"^\[Attached image:\s*([^\]]+)\]\s*$", re.M)


def _extract_images(message: str) -> tuple[str, list[Path]]:
    """Split attachment markers off a message, returning the text and the paths."""
    paths = [Path(m.group(1).strip()) for m in _ATTACHED_IMAGE.finditer(message)]
    return _ATTACHED_IMAGE.sub("", message).strip(), paths


class AgentLoop:
    def __init__(self, session_manager: SessionManager):
        self.session_manager = session_manager
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def run(
        self,
        channel: str,
        peer: str,
        user_message: str,
        model_override: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Main agent turn. Yields response text.
        Yields NO_REPLY sentinel if response should be suppressed.
        """
        get_config()
        session = await self.session_manager.get_or_create(channel, peer)
        session_id = session["session_id"]
        model_id = model_override or session["model"]
        claude_session_id = session.get("claude_session_id")
        is_new_session = claude_session_id is None

        # Which instructions this conversation gets. The tier is stored on the
        # session by whoever opened it; a web session without one predates the
        # column and falls to the restricted set, while the internal channels —
        # heartbeat, cron, the task queue — have no user behind them and need the
        # full context.
        tier = session.get("tier") or (TIER_STUDENT if channel == "web" else TIER_STAFF)

        async with self._get_lock(session_id):
            try:
                system = build_system_prompt(is_new_session=is_new_session, tier=tier)

                # Inject context from previous session if a model switch just happened
                if is_new_session:
                    pending = await self.session_manager.pop_pending_context(session_id)
                    if pending:
                        user_message = f"{pending}\n\n{user_message}"

                # What the model sees, as opposed to what the person wrote. The
                # transcript below stores the latter, so a pupil's own chat bubble
                # stays their own words rather than our plumbing.
                prompt_message = user_message
                images: list[Path] = []
                if tier == TIER_STUDENT:
                    prompt_message, images = _extract_images(user_message)
                    prompt_message = f"{STUDENT_TURN_MARKER}\n\n{prompt_message}"

                response_text, used_session_id, usage = await call_claude(
                    model=model_id,
                    system=system,
                    user_message=prompt_message,
                    claude_session_id=claude_session_id,
                    is_new_session=is_new_session,
                    tier=tier,
                    images=images,
                )

                # Persist claude_session_id if new or changed (fresh start or resume fallback)
                if used_session_id and used_session_id != claude_session_id:
                    await self.session_manager.set_claude_session_id(session_id, used_session_id)

                # NO_REPLY check — match prefix so "NO_REPLY\n\n(explanation)" is also suppressed
                if response_text.strip().startswith(NO_REPLY):
                    yield NO_REPLY
                    return

                # Audit log
                await self.session_manager.append_message(session_id, "user", user_message)
                await self.session_manager.append_message(session_id, "assistant", response_text)
                await self.session_manager.update_activity(session_id, usage)

                yield response_text

            except Exception as e:
                logger.error(f"Agent loop error [{channel}:{peer}]: {e}\n{traceback.format_exc()}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                # Norwegian, and specific where we can be: staff should know
                # whether to retry or to report it, not just see "went wrong".
                from core.cli_backend import AuthExpiredError
                if isinstance(e, AuthExpiredError):
                    yield ("⚠️ Tjenesten er ikke pålogget akkurat nå, så jeg får "
                           "ikke svart. Dette er ikke noe du har gjort feil — si "
                           "fra til IT, så fikser de det.")
                else:
                    yield ("⚠️ Noe gikk galt her, og jeg fikk ikke svart. Prøv en "
                           "gang til. Skjer det igjen, si fra til IT.")

    async def _collect(self, channel: str, peer: str, message: str, model: str = None) -> Optional[str]:
        """Run agent and collect full response. Returns None for NO_REPLY/errors."""
        try:
            parts = []
            async for chunk in self.run(channel, peer, message, model_override=model):
                parts.append(chunk)
            full = "".join(parts)
            if not full.strip() or full.strip().startswith(NO_REPLY) or "Something went wrong" in full:
                return None
            return full
        except Exception as e:
            logger.warning(f"Agent error [{channel}:{peer}]: {e}")
            return None

    async def run_heartbeat(self, channel: str, peer: str) -> Optional[str]:
        """Silent heartbeat turn. Returns response or None if NO_REPLY."""
        import zoneinfo
        from datetime import datetime as _dt
        cfg = get_config()

        tz = zoneinfo.ZoneInfo(cfg.timezone)
        hour = _dt.now(tz).hour
        if 8 <= hour < 23:
            heartbeat_prompt = (
                "Heartbeat check (daytime). "
                "ONLY reply if there is something genuinely urgent requiring immediate attention. "
                "If nothing is urgent, reply exactly: NO_REPLY"
            )
        else:
            heartbeat_prompt = (
                "Nightly heartbeat. HEARTBEAT.md is already in your context — follow it strictly. "
                "Only run each nightly task once (check heartbeat-state.json). "
                "If all tasks are already done for today, reply exactly: NO_REPLY"
            )
        return await self._collect(channel, "heartbeat", heartbeat_prompt, cfg.models.heartbeat_id)

    async def run_cron(
        self,
        cron_id: str,
        name: str,
        prompt: str,
        model: Optional[str] = None,
    ) -> Optional[str]:
        """Run a user-defined cron job. Returns response or None if NO_REPLY."""
        cfg = get_config()
        model_id = cfg.models.resolve(model) if model else cfg.models.heartbeat_id
        return await self._collect("cron", cron_id, prompt, model_id)
