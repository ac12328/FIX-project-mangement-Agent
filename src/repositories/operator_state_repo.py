"""
Operator state repository — tracks Telegram-specific operator interaction for inactivity alerts.
Blueprint Section 34: tracks last_telegram_interaction_at and inactivity_alert_dismissed_until.
"""

import sqlite3
from datetime import datetime, timezone


def seconds_since_last_telegram_interaction(conn: sqlite3.Connection) -> float | None:
    """Seconds since last operator Telegram message, or None if never recorded."""
    row = conn.execute(
        "SELECT last_telegram_interaction_at FROM operator_state WHERE id = 1"
    ).fetchone()
    if row is None or not row["last_telegram_interaction_at"]:
        return None
    try:
        ts = datetime.fromisoformat(
            str(row["last_telegram_interaction_at"]).replace("Z", "+00:00")
        )
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    delta = datetime.now(timezone.utc) - ts
    return max(0.0, delta.total_seconds())


def get_operator_state(conn: sqlite3.Connection) -> dict | None:
    """Get operator state. Returns dict with last_telegram_interaction_at and inactivity_alert_dismissed_until."""
    row = conn.execute("SELECT * FROM operator_state WHERE id = 1").fetchone()
    return dict(row) if row else None


def upsert_operator_state(
    conn: sqlite3.Connection,
    *,
    last_telegram_interaction_at: str | None = None,
    inactivity_alert_dismissed_until: str | None = None,
) -> None:
    """Insert or update operator state."""
    now = datetime.now(timezone.utc).isoformat()

    if last_telegram_interaction_at is not None:
        conn.execute(
            """INSERT INTO operator_state (id, last_telegram_interaction_at, updated_at)
               VALUES (1, ?, ?)
               ON CONFLICT(id)
               DO UPDATE SET last_telegram_interaction_at = excluded.last_telegram_interaction_at,
                             updated_at = excluded.updated_at""",
            (last_telegram_interaction_at, now),
        )

    if inactivity_alert_dismissed_until is not None:
        conn.execute(
            """INSERT INTO operator_state (id, inactivity_alert_dismissed_until, updated_at)
               VALUES (1, ?, ?)
               ON CONFLICT(id)
               DO UPDATE SET inactivity_alert_dismissed_until = excluded.inactivity_alert_dismissed_until,
                             updated_at = excluded.updated_at""",
            (inactivity_alert_dismissed_until, now),
        )


def record_telegram_interaction(conn: sqlite3.Connection) -> None:
    """Record that the operator sent a Telegram message (resets inactivity timer)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO operator_state (id, last_telegram_interaction_at, updated_at)
           VALUES (1, ?, ?)
           ON CONFLICT(id)
           DO UPDATE SET last_telegram_interaction_at = excluded.last_telegram_interaction_at,
                         updated_at = excluded.updated_at""",
        (now, now),
    )


def dismiss_inactivity_alert(conn: sqlite3.Connection, dismissed_until: str) -> None:
    """Dismiss the inactivity alert until the given ISO timestamp."""
    upsert_operator_state(conn, inactivity_alert_dismissed_until=dismissed_until)


VALID_NOTIFICATION_MODES = ("silent", "active", "emergency")


def get_notification_mode(conn: sqlite3.Connection) -> str:
    """Return the current notification mode (default: 'active')."""
    row = conn.execute(
        "SELECT notification_mode FROM operator_state WHERE id = 1"
    ).fetchone()
    if row and row[0] in VALID_NOTIFICATION_MODES:
        return row[0]
    return "active"


def set_notification_mode(conn: sqlite3.Connection, mode: str) -> None:
    """Set the notification mode. Raises ValueError for invalid values."""
    if mode not in VALID_NOTIFICATION_MODES:
        raise ValueError(f"Invalid notification_mode '{mode}'. Must be one of {VALID_NOTIFICATION_MODES}")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO operator_state (id, notification_mode, updated_at)
           VALUES (1, ?, ?)
           ON CONFLICT(id)
           DO UPDATE SET notification_mode = excluded.notification_mode,
                         updated_at = excluded.updated_at""",
        (mode, now),
    )
    conn.commit()


_BUDGET_DEFAULTS = {
    "max_daily_notifications": 15,
    "notification_delivery_mode": "realtime",
    "quiet_hours_start": None,
    "quiet_hours_end": None,
    "notifications_sent_today": 0,
    "notifications_reset_date": None,
}

_BUDGET_COLS = ", ".join(_BUDGET_DEFAULTS.keys())


def get_budget_state(conn: sqlite3.Connection) -> dict:
    """Read budget-related columns from operator_state. Returns defaults if no row."""
    row = conn.execute(
        f"SELECT {_BUDGET_COLS} FROM operator_state WHERE id = 1"
    ).fetchone()
    if not row:
        return dict(_BUDGET_DEFAULTS)
    result = dict(row)
    # Apply defaults for any NULL values
    for key, default in _BUDGET_DEFAULTS.items():
        if result.get(key) is None and default is not None:
            result[key] = default
    return result


def increment_notifications_sent(conn: sqlite3.Connection, *, commit: bool = True) -> None:
    """Increment the daily notification counter by 1."""
    conn.execute(
        "UPDATE operator_state SET notifications_sent_today = "
        "COALESCE(notifications_sent_today, 0) + 1 WHERE id = 1"
    )
    if commit:
        conn.commit()


def reset_notifications_counter(conn: sqlite3.Connection, today_str: str, *, commit: bool = True) -> None:
    """Reset daily counter to 0 and stamp the reset date."""
    conn.execute(
        "UPDATE operator_state SET notifications_sent_today = 0, "
        "notifications_reset_date = ? WHERE id = 1",
        (today_str,),
    )
    if commit:
        conn.commit()


# ── Response language ──────────────────────────────────────────────────

VALID_LANGUAGES = ("zh-TW", "en", "zh-CN")


def get_response_language(conn: sqlite3.Connection) -> str:
    """Return the operator's preferred response language (default: 'zh-TW')."""
    row = conn.execute(
        "SELECT response_language FROM operator_state WHERE id = 1"
    ).fetchone()
    if row and row[0] in VALID_LANGUAGES:
        return row[0]
    return "zh-TW"


def set_response_language(conn: sqlite3.Connection, language: str) -> None:
    """Set the response language. Raises ValueError for invalid values."""
    if language not in VALID_LANGUAGES:
        raise ValueError(
            f"Invalid language '{language}'. Must be one of {VALID_LANGUAGES}"
        )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO operator_state (id, response_language, updated_at)
           VALUES (1, ?, ?)
           ON CONFLICT(id)
           DO UPDATE SET response_language = excluded.response_language,
                         updated_at = excluded.updated_at""",
        (language, now),
    )
    conn.commit()
    # Telegram UI pack (t()) caches the active locale — invalidate on change.
    try:
        from src.telegram.i18n import clear_ui_language_cache

        clear_ui_language_cache()
    except Exception:
        pass


# ── Thinking window (Telegram /thinking debug toggle) ─────────────────

def get_thinking_window_enabled(conn: sqlite3.Connection) -> bool:
    """Return whether the operator has enabled the thinking debug window."""
    row = conn.execute(
        "SELECT thinking_window_enabled FROM operator_state WHERE id = 1"
    ).fetchone()
    if row is None or row[0] is None:
        return False
    return bool(row[0])


def set_thinking_window_enabled(conn: sqlite3.Connection, enabled: bool) -> None:
    """Enable or disable the thinking debug window."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO operator_state (id, thinking_window_enabled, updated_at)
           VALUES (1, ?, ?)
           ON CONFLICT(id)
           DO UPDATE SET thinking_window_enabled = excluded.thinking_window_enabled,
                         updated_at = excluded.updated_at""",
        (1 if enabled else 0, now),
    )
    conn.commit()


def toggle_thinking_window(conn: sqlite3.Connection) -> bool:
    """Flip thinking window on/off. Returns the new enabled state."""
    enabled = not get_thinking_window_enabled(conn)
    set_thinking_window_enabled(conn, enabled)
    return enabled


def get_agent_harness_prefs(conn: sqlite3.Connection) -> dict:
    """Effective harness toggles for Dashboard (NULL = use config.json default)."""
    from config.settings import CONFIG
    from src.agent.harness_settings import get_harness_flags

    cfg = CONFIG.agent_harness
    flags = get_harness_flags(conn)
    row = None
    try:
        row = conn.execute(
            """
            SELECT harness_require_wiki_progress, harness_auto_draft_rule58
            FROM operator_state WHERE id = 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    return {
        "require_wiki_on_progress_advisory": flags["require_wiki_on_progress_advisory"],
        "auto_draft_on_rule58": flags["auto_draft_on_rule58"],
        "config_defaults": {
            "require_wiki_on_progress_advisory": cfg.require_wiki_on_progress_advisory,
            "auto_draft_on_rule58": cfg.auto_draft_on_rule58,
        },
        "overrides_set": {
            "require_wiki_on_progress_advisory": (
                row is not None and row["harness_require_wiki_progress"] is not None
            ),
            "auto_draft_on_rule58": (
                row is not None and row["harness_auto_draft_rule58"] is not None
            ),
        },
    }


def set_agent_harness_prefs(
    conn: sqlite3.Connection,
    *,
    require_wiki_on_progress_advisory: bool | None = None,
    auto_draft_on_rule58: bool | None = None,
) -> None:
    """Persist harness toggles. Pass None to clear override (revert to config.json)."""
    now = datetime.now(timezone.utc).isoformat()
    updates: list[str] = []
    values: list[object] = []
    if require_wiki_on_progress_advisory is not None:
        updates.append("harness_require_wiki_progress = ?")
        values.append(1 if require_wiki_on_progress_advisory else 0)
    if auto_draft_on_rule58 is not None:
        updates.append("harness_auto_draft_rule58 = ?")
        values.append(1 if auto_draft_on_rule58 else 0)
    if not updates:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO operator_state (id, updated_at) VALUES (1, ?)",
        (now,),
    )
    updates.append("updated_at = ?")
    values.append(now)
    sets = ", ".join(updates)
    conn.execute(
        f"UPDATE operator_state SET {sets} WHERE id = 1",
        values,
    )
    conn.commit()


def clear_agent_harness_override(
    conn: sqlite3.Connection,
    field: str,
) -> None:
    """Clear one harness override (use config default)."""
    col_map = {
        "require_wiki_on_progress_advisory": "harness_require_wiki_progress",
        "auto_draft_on_rule58": "harness_auto_draft_rule58",
    }
    col = col_map.get(field)
    if not col:
        raise ValueError(f"Unknown harness field: {field}")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        f"UPDATE operator_state SET {col} = NULL, updated_at = ? WHERE id = 1",
        (now,),
    )
    conn.commit()
