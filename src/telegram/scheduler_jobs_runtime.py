"""Runtime maintenance jobs; scheduler_jobs retains public patch points."""
from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

logger = logging.getLogger(__name__)


async def run_backup_database(
    context: Any, *, notify_failure: Callable[..., Any], mark_success: Callable[[str], None],
) -> None:
    """Run the daily backup, reporting failures through the scheduler notifier."""
    chat_id = context.job.data["chat_id"]
    try:
        from src.db.backup import backup_database, verify_backup, rotate_backups
        from config.settings import DB_PATH

        db_path = str(DB_PATH)
        result = backup_database(db_path, "backups")
        if not result["success"]:
            raise RuntimeError("Backup failed")
        if not verify_backup(result["backup_path"]):
            raise RuntimeError(f"Backup integrity check FAILED: {result['backup_path']}")
        rotate_backups("backups")
        mark_success("backup_database")
        logger.info("Daily backup complete: %s", result["backup_path"])
    except Exception as exc:
        logger.exception("Failed to run database backup")
        await notify_failure(context, chat_id, "backup_database", exc)


async def run_bot_heartbeat(
    context: Any, *, write_scope: Callable[[], AbstractContextManager[Any]],
) -> None:
    """Persist a heartbeat on the shared writer connection."""
    try:
        from datetime import datetime, timezone
        from src.repositories.working_memory_repo import upsert_memory

        with write_scope() as conn:
            # Ensure _system sentinel project exists (FK on working_memory.project_id).
            conn.execute(
                "INSERT OR IGNORE INTO projects "
                "(project_id, project_status, ball_with, operator_id) "
                "VALUES ('_system', 'Active', 'Internal', 'system')"
            )
            upsert_memory(conn, "_system", "bot_heartbeat", datetime.now(timezone.utc).isoformat())
    except Exception:
        logger.exception("Failed to write bot heartbeat")


async def run_api_health_check(
    context: Any, *, scheduler_state: Any, send_message: Callable[..., Any],
) -> None:
    """Alert after three consecutive failures, but only after first success."""
    chat_id = context.job.data["chat_id"]
    try:
        import requests

        resp = requests.get("http://127.0.0.1:8317/api/health", timeout=10)
        if resp.status_code == 200:
            scheduler_state._api_fail_count = 0
            scheduler_state._api_seen_healthy = True
            return
        scheduler_state._api_fail_count += 1
    except Exception:
        scheduler_state._api_fail_count += 1

    # Don't alarm if API hasn't been healthy yet (still starting up).
    if not scheduler_state._api_seen_healthy:
        return

    if scheduler_state._api_fail_count >= 3:
        try:
            text = (
                f"\U0001f6a8 **SYSTEM ALERT**\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\u274c API unreachable for {scheduler_state._api_fail_count} consecutive checks\n"
                f"\U0001f517 http://127.0.0.1:8317/api/health\n\n"
                f"\U0001f4a1 Check if the API process is running."
            )
            await send_message(
                context.bot, chat_id=chat_id, text=text, message_kind="system",
            )
        except Exception:
            logger.exception("Failed to send API health alert")
        scheduler_state._api_fail_count = 0  # reset after alerting


async def run_trace_retention(
    context: Any, *, write_scope: Callable[[], AbstractContextManager[Any]],
) -> None:
    """Apply encrypted payload and trace-detail retention policies."""
    from config.settings import CONFIG
    from src.observability.retention import cleanup_traces

    try:
        with write_scope() as conn:
            result = cleanup_traces(
                conn, CONFIG.observability.payload_retention_days,
                CONFIG.observability.detail_retention_days,
            )
        logger.info("Trace retention cleanup completed: %s", result)
    except Exception:
        logger.exception("Trace retention cleanup failed")
