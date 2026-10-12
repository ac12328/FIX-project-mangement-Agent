"""
Runtime agent harness flags — operator_state overrides config.json defaults.
"""

from __future__ import annotations

import sqlite3

from config.settings import CONFIG


def _row_bool(value, default: bool) -> bool:
    if value is None:
        return default
    return bool(int(value))


def get_harness_flags(conn: sqlite3.Connection | None) -> dict[str, bool]:
    """Return effective harness toggles for this process/connection."""
    cfg = CONFIG.agent_harness
    defaults = {
        "require_wiki_on_progress_advisory": cfg.require_wiki_on_progress_advisory,
        "auto_draft_on_rule58": cfg.auto_draft_on_rule58,
    }
    if conn is None:
        return defaults
    try:
        row = conn.execute(
            """
            SELECT harness_require_wiki_progress, harness_auto_draft_rule58
            FROM operator_state WHERE id = 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return defaults
    if not row:
        return defaults
    return {
        "require_wiki_on_progress_advisory": _row_bool(
            row["harness_require_wiki_progress"],
            defaults["require_wiki_on_progress_advisory"],
        ),
        "auto_draft_on_rule58": _row_bool(
            row["harness_auto_draft_rule58"],
            defaults["auto_draft_on_rule58"],
        ),
    }


def require_wiki_on_progress(conn: sqlite3.Connection | None) -> bool:
    return get_harness_flags(conn)["require_wiki_on_progress_advisory"]


def auto_draft_on_rule58(conn: sqlite3.Connection | None) -> bool:
    return get_harness_flags(conn)["auto_draft_on_rule58"]