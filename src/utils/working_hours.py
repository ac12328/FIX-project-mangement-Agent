"""
Working hours gate — determines whether the current time falls within
the operator's configured working hours.

URGENCY_EMERGENCY always bypasses the gate so genuinely critical alerts
are never silenced by a time restriction.
"""
from __future__ import annotations

import zoneinfo
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.settings import AppConfig

from src.utils.push_guard import URGENCY_EMERGENCY


def is_within_working_hours(config: "AppConfig", now: datetime | None = None) -> bool:
    """Return True if *now* falls inside the configured working window.

    Always returns True when working_hours.enabled is False.
    """
    wh = config.working_hours
    if not wh.enabled:
        return True

    tz = zoneinfo.ZoneInfo(wh.timezone)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    if wh.weekdays_only and now.weekday() >= 5:   # 5=Sat, 6=Sun
        return False

    return wh.start_hour <= now.hour < wh.end_hour


def should_notify(
    config: "AppConfig",
    mode: str,
    urgency: str,
    score: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Combined push check: notification_mode gate + working-hours gate.

    URGENCY_EMERGENCY bypasses the working-hours gate so critical alerts
    always reach the operator regardless of time or day.
    """
    from src.utils.push_guard import should_push

    if not should_push(mode, urgency, score):
        return False

    if urgency == URGENCY_EMERGENCY:
        return True

    return is_within_working_hours(config, now)
