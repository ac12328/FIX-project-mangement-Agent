"""
Notification budget — daily caps, quiet hours, and budget-aware push decisions.

Layers on top of push_guard.should_push() to add:
  - Daily notification cap (max_daily_notifications; 0 = unlimited)
  - Quiet hours (HKT-based window where non-emergency is suppressed)
  - Automatic counter reset on new HKT day
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta

from src.utils.push_guard import should_push, URGENCY_EMERGENCY
from src.repositories.operator_state_repo import (
    get_budget_state,
    get_notification_mode,
    increment_notifications_sent,
    reset_notifications_counter,
)

_HKT = timedelta(hours=8)


def _now_utc() -> datetime:
    """Return current UTC time. Mockable for tests."""
    return datetime.now(timezone.utc)


def _hkt_today_str() -> str:
    """Return today's date string in HKT (YYYY-MM-DD)."""
    return (_now_utc() + _HKT).strftime("%Y-%m-%d")


def _hkt_hour_now() -> int:
    """Return current hour in HKT (0-23)."""
    return (_now_utc() + _HKT).hour


def _in_quiet_hours(hour: int, start: str | None, end: str | None) -> bool:
    """Check if *hour* (0-23) falls within quiet window [start, end).

    Handles overnight ranges (e.g. 22:00-08:00) and same-day ranges (12:00-14:00).
    Returns False if start/end are None.
    """
    if not start or not end:
        return False
    start_h = int(start.split(":")[0])
    end_h = int(end.split(":")[0])
    if start_h <= end_h:
        # same-day range, e.g. 12:00–14:00
        return start_h <= hour < end_h
    else:
        # overnight range, e.g. 22:00–08:00
        return hour >= start_h or hour < end_h


# ── Public API ───────────────────────────────────────────────────────


def reset_budget_if_needed(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    """Reset the daily counter if the HKT date has changed since last reset."""
    state = get_budget_state(conn)
    today = _hkt_today_str()
    if state["notifications_reset_date"] != today:
        reset_notifications_counter(conn, today, commit=commit)


def should_push_with_budget(
    conn: sqlite3.Connection,
    urgency: str,
    *,
    now_hkt_hour: int | None = None,
) -> bool:
    """Determine if a notification should be sent, considering mode, quiet hours, and budget.

    Order of checks:
      1. EMERGENCY always returns True.
      2. Base push_guard.should_push(mode, urgency) — respects silent/emergency mode.
      3. Quiet hours — blocks non-emergency during configured window.
      4. Budget cap — blocks when sent_today >= max_daily (0 = unlimited).
    """
    # 1. Emergency always passes
    if urgency == URGENCY_EMERGENCY:
        return True

    # 2. Base mode check
    mode = get_notification_mode(conn)
    if not should_push(mode, urgency):
        return False

    # 3. Quiet hours
    hour = now_hkt_hour if now_hkt_hour is not None else _hkt_hour_now()
    state = get_budget_state(conn)
    if _in_quiet_hours(hour, state["quiet_hours_start"], state["quiet_hours_end"]):
        return False

    # 4. Budget cap (0 = unlimited)
    max_daily = state["max_daily_notifications"]
    if max_daily > 0 and state["notifications_sent_today"] >= max_daily:
        return False

    return True


def consume_notification(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    """Record one notification; borrowed transactions must pass commit=False.

    The legacy committing call makes its two writes one atomic operation. A
    failed increment does not commit the daily reset or prior caller edits.
    """
    # A savepoint only isolates this helper from caller work. Without an outer
    # transaction, releasing one would commit immediately; keep the borrowed
    # commit=False path open for its caller to finish instead.
    if not commit and not conn.in_transaction:
        conn.execute("BEGIN")
    savepoint = "notification_budget_consume"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        reset_budget_if_needed(conn, commit=False)
        increment_notifications_sent(conn, commit=False)
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    if commit:
        conn.commit()


def reserve_notification_attempt(conn: sqlite3.Connection, urgency: str) -> bool:
    """Reserve one budget slot in a caller-owned short transaction, before I/O.

    This is deliberately conservative: an unconfirmed Telegram send retains
    its slot rather than permitting another attempt that could exceed the cap.
    The caller must commit this transaction before starting the remote send.
    """
    now_hkt = _now_utc() + _HKT
    today = now_hkt.strftime("%Y-%m-%d")
    conn.execute("INSERT OR IGNORE INTO operator_state (id) VALUES (1)")
    if get_budget_state(conn)["notifications_reset_date"] != today:
        reset_notifications_counter(conn, today, commit=False)
    if not should_push_with_budget(conn, urgency, now_hkt_hour=now_hkt.hour):
        return False
    increment_notifications_sent(conn, commit=False)
    return True


def get_budget_status(conn: sqlite3.Connection) -> dict:
    """Return a human-friendly budget snapshot.

    Keys: max_daily, sent_today, remaining (None if unlimited),
          delivery_mode, quiet_hours_start, quiet_hours_end.
    """
    state = get_budget_state(conn)
    max_daily = state["max_daily_notifications"]
    sent = state["notifications_sent_today"]
    remaining = None if max_daily == 0 else max(0, max_daily - sent)
    return {
        "max_daily": max_daily,
        "sent_today": sent,
        "remaining": remaining,
        "delivery_mode": state["notification_delivery_mode"],
        "quiet_hours_start": state["quiet_hours_start"],
        "quiet_hours_end": state["quiet_hours_end"],
    }
