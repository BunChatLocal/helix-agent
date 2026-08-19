"""
Helix Auth + Rate Limiter
AllowList enforcement per channel. Token bucket rate limiting per sender.
All rejections logged to audit.
"""

import time
from collections import defaultdict, deque
from typing import Optional

from core.config import TIER_STAFF, get_config

# How often to drop idle senders. One entry per person is small, but a county's
# worth of pupils over a school year is not, and nothing else ever removes them.
_PRUNE_EVERY_SECONDS = 300


class RateLimiter:
    """Token bucket rate limiter with per-minute and per-hour windows.

    Keyed on whoever is asking, not on the connection they are asking over: a
    limit that resets when a tab is closed is not a limit. The allowance depends
    on their tier — see `RateLimitConfig.for_tier` and ROLES.md.
    """

    def __init__(self):
        # sender_key -> deque of timestamps
        self._minute_windows: dict[str, deque] = defaultdict(deque)
        self._hour_windows: dict[str, deque] = defaultdict(deque)
        self._last_prune = time.time()

    def check(self, sender_key: str, tier: str = TIER_STAFF) -> Optional[str]:
        """
        Returns None if allowed, or an error string if rate limited.
        sender_key = f"{channel}:{sender_id}"
        """
        cfg = get_config()
        per_minute, per_hour = cfg.security.rate_limit.for_tier(tier)
        now = time.time()

        # Clean and check minute window
        mw = self._minute_windows[sender_key]
        while mw and now - mw[0] > 60:
            mw.popleft()
        if len(mw) >= per_minute:
            return f"Rate limited: {per_minute} messages/minute exceeded"

        # Clean and check hour window
        hw = self._hour_windows[sender_key]
        while hw and now - hw[0] > 3600:
            hw.popleft()
        if len(hw) >= per_hour:
            return f"Rate limited: {per_hour} messages/hour exceeded"

        # Record this message
        mw.append(now)
        hw.append(now)

        if now - self._last_prune > _PRUNE_EVERY_SECONDS:
            self._prune(now)
        return None

    def _prune(self, now: float) -> None:
        """Forget senders whose windows have emptied."""
        self._last_prune = now
        for key in [k for k, w in self._hour_windows.items()
                    if not w or now - w[-1] > 3600]:
            self._hour_windows.pop(key, None)
            self._minute_windows.pop(key, None)


class AuthManager:
    def __init__(self, audit_logger=None):
        self.rate_limiter = RateLimiter()
        self.audit_logger = audit_logger

    def _check(self, channel: str, user_id, enabled: bool, allowed: list) -> Optional[str]:
        if not enabled:
            return f"{channel.title()} channel not enabled"
        if allowed and str(user_id) not in [str(u) for u in allowed]:
            self._log_rejection(channel, str(user_id), "not in allowlist")
            return f"User {user_id} not in {channel.title()} allowlist"
        rate_err = self.rate_limiter.check(f"{channel}:{user_id}")
        if rate_err:
            self._log_rejection(channel, str(user_id), rate_err)
            return rate_err
        return None

    def check_discord(self, user_id: str) -> Optional[str]:
        """Returns None if allowed, error string if denied."""
        cfg = get_config()
        return self._check("discord", user_id, cfg.discord.enabled, cfg.discord.allowed_users)

    def check_telegram(self, user_id: int) -> Optional[str]:
        """Returns None if allowed, error string if denied."""
        cfg = get_config()
        return self._check("telegram", user_id, cfg.telegram.enabled, cfg.telegram.allowed_users)

    def _log_rejection(self, channel: str, sender_id: str, reason: str) -> None:
        if self.audit_logger:
            self.audit_logger.log("auth_rejection", channel=channel, sender_id=sender_id, reason=reason)
