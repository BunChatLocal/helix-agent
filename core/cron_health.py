"""Whether a scheduled job actually fired when it was supposed to.

`last_status` only describes the most recent execution. A daily 07:00 job
that succeeded yesterday still reads as success this afternoon if Helix was
down at 07:00 — so the UI also needs "did it run for the latest due slot?"
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
import zoneinfo

# Don't flag a job in the first minutes after it is due — the scheduler may
# still be starting it. After this, a missing last_run means it did not fire.
MISS_GRACE = timedelta(minutes=15)

# How late a sleep/restart may be and still run the slot. The 07:00 job this
# morning was skipped because the PC woke at 07:55 and APScheduler's default
# grace is one second. Six hours covers a typical commute start without
# firing yesterday's probe at 15:00.
CATCH_UP_WINDOW = timedelta(hours=6)
MISFIRE_GRACE_SECONDS = int(CATCH_UP_WINDOW.total_seconds())

_DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MON = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _token(tok: str, names: dict[str, int]) -> int:
    tok = tok.strip().lower()
    if tok in names:
        return names[tok]
    return int(tok)


def _parse_field(expr: str, lo: int, hi: int, names: dict[str, int] | None = None):
    """Allowed values and whether the field was `*` (unrestricted)."""
    expr = (expr or "*").strip()
    names = names or {}
    if expr == "*":
        return set(range(lo, hi + 1)), True
    out: set[int] = set()
    for part in expr.split(","):
        part = part.strip().lower()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            part = base or "*"
        if part in ("*", ""):
            out.update(range(lo, hi + 1, step))
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = _token(a, names), _token(b, names)
            out.update(range(start, end + 1, step))
            continue
        val = _token(part, names)
        if step != 1:
            out.update(range(val, hi + 1, step))
        else:
            out.add(val)
    if names is _DOW and 7 in out:
        out.add(0)
        out.discard(7)
    return out, False


def _parse_cron(schedule: str):
    parts = (schedule or "").split()
    if len(parts) != 5:
        return None
    minutes, min_star = _parse_field(parts[0], 0, 59)
    hours, hour_star = _parse_field(parts[1], 0, 23)
    doms, dom_star = _parse_field(parts[2], 1, 31)
    months, _ = _parse_field(parts[3], 1, 12, _MON)
    dows, dow_star = _parse_field(parts[4], 0, 7, _DOW)
    if 7 in dows:
        dows.add(0)
        dows.discard(7)
    if not minutes or not hours or not months:
        return None
    return minutes, hours, doms, months, dows, dom_star, dow_star


def _day_ok(t: datetime, doms, dows, dom_star, dow_star) -> bool:
    cron_dow = (t.weekday() + 1) % 7  # Sunday = 0
    dom_ok = t.day in doms
    dow_ok = cron_dow in dows
    if dom_star and dow_star:
        return True
    if not dom_star and not dow_star:
        return dom_ok or dow_ok
    if dom_star:
        return dow_ok
    return dom_ok


def previous_fire_time(schedule: str, now: datetime, timezone: str) -> Optional[datetime]:
    """Latest scheduled fire at or before `now`, in `timezone`."""
    parsed = _parse_cron(schedule)
    if not parsed:
        return None
    minutes, hours, doms, months, dows, dom_star, dow_star = parsed
    tz = zoneinfo.ZoneInfo(timezone)
    now = now.astimezone(tz)
    t = now.replace(second=0, microsecond=0)
    max_minute = max(minutes)
    max_hour = max(hours)

    def hit(candidate: datetime) -> bool:
        return (
            candidate.month in months
            and candidate.hour in hours
            and candidate.minute in minutes
            and _day_ok(candidate, doms, dows, dom_star, dow_star)
        )

    if hit(t) and t <= now:
        return t

    for _ in range(5000):
        prev_mins = [m for m in minutes if m < t.minute]
        if prev_mins and t.hour in hours and t.month in months and _day_ok(t, doms, dows, dom_star, dow_star):
            t = t.replace(minute=max(prev_mins), second=0, microsecond=0)
            if hit(t) and t <= now:
                return t
            continue
        prev_hours = [h for h in hours if h < t.hour]
        if prev_hours and t.month in months:
            t = t.replace(hour=max(prev_hours), minute=max_minute, second=0, microsecond=0)
            if hit(t) and t <= now:
                return t
            continue
        t = (t.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(minutes=1))
        t = t.replace(hour=max_hour, minute=max_minute, second=0, microsecond=0)
        if hit(t) and t <= now:
            return t
    return None


def _as_dt(value, tz) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def schedule_state(cron, *, now: Optional[datetime] = None, timezone: str) -> dict:
    """`missed` is true when the latest due slot has no run covering it."""
    tz = zoneinfo.ZoneInfo(timezone)
    now = now or datetime.now(tz)
    now = now.astimezone(tz)
    expected = previous_fire_time(getattr(cron, "schedule", ""), now, timezone)
    last = _as_dt(getattr(cron, "last_run", None), tz)
    enabled = bool(getattr(cron, "enabled", True))
    running = getattr(cron, "last_status", None) == "running"
    missed = False
    if enabled and expected and not running and now >= expected + MISS_GRACE:
        missed = last is None or last < expected
    return {
        "expected_at": expected.isoformat() if expected else None,
        "missed": missed,
    }


def should_catch_up(cron, *, now: Optional[datetime] = None, timezone: str) -> bool:
    """True when startup should run this job because its latest slot was skipped.

    Used after a sleep/restart. A 15:00 start does not fire the 07:00 probe —
    that is reported as missed instead.
    """
    if not bool(getattr(cron, "enabled", True)):
        return False
    if getattr(cron, "last_status", None) == "running":
        return False
    tz = zoneinfo.ZoneInfo(timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    expected = previous_fire_time(getattr(cron, "schedule", ""), now, timezone)
    if not expected or now < expected:
        return False
    if now - expected > CATCH_UP_WINDOW:
        return False
    last = _as_dt(getattr(cron, "last_run", None), tz)
    return last is None or last < expected


def crons_due_for_catch_up(cfg, *, now: Optional[datetime] = None) -> list:
    timezone = getattr(cfg, "timezone", None) or "Europe/Oslo"
    return [
        cron for cron in (getattr(cfg, "crons", None) or [])
        if should_catch_up(cron, now=now, timezone=timezone)
    ]
