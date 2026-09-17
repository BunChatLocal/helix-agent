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
from typing import AsyncGenerator, Optional, Union

from core.config import get_config, TIER_STAFF, TIER_STUDENT, is_staff_like
from core.session_manager import SessionManager
from core.context_engine import build_system_prompt, turn_marker
from core.cli_backend import call_claude, redact_internal_paths
from core.knowledge import (
    FOLLOW_UP_MIN_TOKENS,
    classify_topic,
    with_matching_skill,
    _tokens as _knowledge_tokens,
)
from core.client_os import normalize as normalize_client_os, prompt_note as client_os_note

logger = logging.getLogger("helix.agent")

NO_REPLY = "NO_REPLY"

# /chat wait-bubble phases. Read/Skill is using the guide; Glob/Grep is
# still looking for which one.
_TOOL_STATUS_KIND = {
    "read": "rules",
    "skill": "rules",
    "glob": "think",
    "grep": "think",
    "webfetch": "web",
    "websearch": "web",
    "edit": "write",
    "write": "write",
}


def _tool_status_kind(name: str) -> str:
    return _TOOL_STATUS_KIND.get((name or "").lower(), "working")


def _topic_for_turn(message: str, tier: str, is_new_session: bool) -> Optional[str]:
    """Which skill this turn is about, or None when we should not re-pick."""
    if not is_new_session and len(_knowledge_tokens(message)) < FOLLOW_UP_MIN_TOKENS:
        return None
    return classify_topic(message, tier)

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
        # Last skill body prepended for this Helix session. Re-sending it on
        # every follow-up costs TTFT; --resume already holds the file.
        self._injected_skill: dict[str, str] = {}

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
        caller_name: str = "",
        caller_email: str = "",
        client_os: str = "",
    ) -> AsyncGenerator[Union[str, dict], None]:
        """
        Main agent turn. Yields response text, or a status dict the web
        socket forwards without saving (queue place-in-line).
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
                if channel == "web":
                    yield {"type": "status", "kind": "read"}

                system = build_system_prompt(
                    is_new_session=is_new_session,
                    tier=tier,
                    channel=channel,
                    caller_name=caller_name,
                    caller_email=caller_email,
                )

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
                os_note = client_os_note(normalize_client_os(client_os))
                if tier == TIER_STUDENT:
                    prompt_message, images = _extract_images(user_message)

                if channel == "web":
                    yield {"type": "status", "kind": "think"}
                picked = _topic_for_turn(user_message, tier, is_new_session)

                if tier == TIER_STUDENT:
                    prompt_message = f"{os_note}{prompt_message}"
                elif is_staff_like(tier):
                    # Skip the "list skills/ then Read SKILL.md" round-trip.
                    # The transcript below still stores what the person wrote.
                    # On a follow-up, skip injection unless the message is long
                    # enough to be a new case — "en elev" otherwise attaches
                    # visma-inschool and the bot asks for bruker@afk.no.
                    # Same skill as last turn: --resume already has it.
                    prev = self._injected_skill.get(session_id)
                    if picked and picked == prev:
                        prompt_message = os_note + user_message
                    else:
                        prompt_message = os_note + with_matching_skill(
                            user_message,
                            tier,
                            min_tokens=0 if is_new_session else FOLLOW_UP_MIN_TOKENS,
                        )
                        if picked:
                            self._injected_skill[session_id] = picked

                marker = turn_marker(tier, channel)
                if marker:
                    prompt_message = f"{marker}\n\n{prompt_message}"

                if channel == "web" and picked:
                    yield {"type": "status", "kind": "rules"}

                # Overflow waits for a slot rather than launching another
                # claude.exe. Web gets live place-in-line events (not saved);
                # heartbeat/cron just wait.
                claude_kwargs = dict(
                    model=model_id,
                    system=system,
                    user_message=prompt_message,
                    claude_session_id=claude_session_id,
                    is_new_session=is_new_session,
                    tier=tier,
                    images=images,
                    channel=channel,
                )
                if channel == "web":
                    progress: asyncio.Queue = asyncio.Queue()
                    streamed: list[str] = []
                    responded = False

                    async def on_wait(position: int, waiting: int) -> None:
                        await progress.put({
                            "type": "queue",
                            "position": position,
                            "waiting": waiting,
                        })

                    displayed = ""

                    async def on_text(delta: str) -> None:
                        nonlocal responded, displayed
                        if not responded:
                            responded = True
                            await progress.put({"type": "status", "kind": "respond"})
                        displayed += delta
                        redacted = redact_internal_paths(displayed)
                        if redacted != displayed:
                            displayed = redacted
                            await progress.put({"replace": True, "chunk": redacted})
                        else:
                            streamed.append(delta)
                            await progress.put(delta)

                    async def on_reset() -> None:
                        nonlocal responded, displayed
                        streamed.clear()
                        displayed = ""
                        responded = False
                        await progress.put({"replace": True})

                    async def on_tool(name: str) -> None:
                        await progress.put({
                            "type": "status",
                            "kind": _tool_status_kind(name),
                        })

                    work = asyncio.create_task(
                        call_claude(
                            on_wait=on_wait, on_text=on_text, on_reset=on_reset,
                            on_tool=on_tool, **claude_kwargs,
                        )
                    )
                    try:
                        while not work.done():
                            getter = asyncio.create_task(progress.get())
                            done, _pending = await asyncio.wait(
                                {work, getter},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if getter in done:
                                yield getter.result()
                            else:
                                getter.cancel()
                                try:
                                    await getter
                                except (asyncio.CancelledError, Exception):
                                    pass
                        while True:
                            try:
                                yield progress.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                        response_text, used_session_id, usage = work.result()
                        response_text = redact_internal_paths(response_text)
                        if displayed:
                            streamed = [displayed]
                    except BaseException:
                        if not work.done():
                            work.cancel()
                            try:
                                await work
                            except (asyncio.CancelledError, Exception):
                                pass
                        raise
                else:
                    response_text, used_session_id, usage = await call_claude(
                        **claude_kwargs
                    )
                    streamed = []

                # Persist claude_session_id if new or changed (fresh start or resume fallback)
                if used_session_id and used_session_id != claude_session_id:
                    await self.session_manager.set_claude_session_id(session_id, used_session_id)

                # NO_REPLY check — match prefix so "NO_REPLY\n\n(explanation)" is also suppressed
                if response_text.strip().startswith(NO_REPLY):
                    if streamed:
                        yield {"replace": True}
                    yield NO_REPLY
                    return

                # Audit log
                await self.session_manager.append_message(session_id, "user", user_message)
                await self.session_manager.append_message(session_id, "assistant", response_text)
                await self.session_manager.update_activity(session_id, usage)

                # /chat already received tokens as they arrived. Only send what
                # the UI does not yet have, so the bubble does not double.
                shown = "".join(streamed)
                if not shown:
                    yield response_text
                elif response_text.startswith(shown):
                    rest = response_text[len(shown):]
                    if rest:
                        yield rest
                elif shown != response_text:
                    yield {"replace": True, "chunk": response_text}

            except Exception as e:
                logger.error(f"Agent loop error [{channel}:{peer}]: {e}\n{traceback.format_exc()}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                # Norwegian, and specific where we can be: staff should know
                # whether to retry or to report it, not just see "went wrong".
                from core.cli_backend import (
                    AuthExpiredError, QuotaExhaustedError, quota_user_message,
                )
                if isinstance(e, AuthExpiredError):
                    yield ("⚠️ Tjenesten er ikke pålogget akkurat nå, så jeg får "
                           "ikke svart. Dette er ikke noe du har gjort feil — si "
                           "fra til IT, så fikser de det.")
                elif isinstance(e, QuotaExhaustedError):
                    yield quota_user_message(e.resets_at)
                else:
                    yield ("⚠️ Noe gikk galt her, og jeg fikk ikke svart. Prøv en "
                           "gang til. Skjer det igjen, si fra til IT.")

    async def _collect(self, channel: str, peer: str, message: str, model: str = None) -> Optional[str]:
        """Run agent and collect full response. Returns None for NO_REPLY/errors."""
        try:
            parts = []
            async for chunk in self.run(channel, peer, message, model_override=model):
                if isinstance(chunk, str):
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
        """Run a user-defined cron job. Returns the full reply, including errors.

        Unlike heartbeat, a cron is a debug/ops tool: silence and failures must
        both be visible, not collapsed into None.
        """
        cfg = get_config()
        model_id = cfg.models.resolve(model) if model else cfg.models.heartbeat_id
        parts = []
        async for chunk in self.run("cron", cron_id, prompt, model_override=model_id):
            if isinstance(chunk, str):
                parts.append(chunk)
        return "".join(parts) or None
