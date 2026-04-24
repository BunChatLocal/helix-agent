"""
Helix Notification Service
Sends completion notifications via Discord DM or Telegram message.

Adapters are registered at startup via register_adapter(). The service
reads the target user IDs from config at notification time so changes
take effect immediately without restart.
"""

import logging
from typing import Optional

from core.config import get_config

logger = logging.getLogger("helix.notifications")


class NotificationService:
    """Routes completion notifications to the configured channel."""

    def __init__(self):
        self._discord_adapter = None
        self._telegram_adapter = None

    # ── Registration ───────────────────────────────────────────────────────

    def register_discord(self, adapter) -> None:
        self._discord_adapter = adapter

    def register_telegram(self, adapter) -> None:
        self._telegram_adapter = adapter

    # ── Notify ─────────────────────────────────────────────────────────────

    async def notify(self, message: str) -> None:
        """Send *message* to all configured notification targets.

        Reads config fresh each call so edits via the web UI take effect
        without a restart.
        """
        cfg = get_config()
        notif = cfg.notifications

        if not notif.enabled:
            return

        sent = False

        if notif.discord_user_id and self._discord_adapter:
            try:
                await self._discord_adapter.send_message(
                    notif.discord_user_id, message
                )
                logger.info(
                    "Notification sent via Discord → %s", notif.discord_user_id
                )
                sent = True
            except Exception as exc:
                logger.warning("Discord notification failed: %s", exc)

        if notif.telegram_user_id and self._telegram_adapter:
            try:
                await self._telegram_adapter.send_message(
                    str(notif.telegram_user_id), message
                )
                logger.info(
                    "Notification sent via Telegram → %s", notif.telegram_user_id
                )
                sent = True
            except Exception as exc:
                logger.warning("Telegram notification failed: %s", exc)

        if not sent:
            logger.debug(
                "Notification not delivered — no enabled targets configured."
            )
