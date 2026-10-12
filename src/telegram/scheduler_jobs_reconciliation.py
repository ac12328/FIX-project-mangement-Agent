"""Preview-only email-to-project reconciliation job."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ReconciliationPorts:
    scoped_write: Callable[..., Any]
    _mark_job_success: Callable[..., Any]
    logger: Any


async def run_email_project_reconciliation(context, ports: ReconciliationPorts) -> None:
    """Weekday preview-only email-to-project catch-up; never mutates project rows."""
    scoped_write = ports.scoped_write
    _mark_job_success = ports._mark_job_success
    logger = ports.logger
    chat_id = context.job.data["chat_id"]
    try:
        from config.settings import CONFIG

        def _core() -> dict:
            from src.project_reconciliation.scheduler import (
                run_email_project_reconciliation_preview,
            )

            with scoped_write() as conn:
                return run_email_project_reconciliation_preview(
                    conn,
                    operator_id=CONFIG.operator_id,
                    chat_id=str(chat_id),
                )

        summary = await asyncio.to_thread(_core)
        logger.info(
            "Email project reconciliation preview: candidates=%d proposals=%d committed=%d",
            summary["candidate_count"],
            summary["proposal_count"],
            summary["committed_count"],
        )
        _mark_job_success("email_project_reconciliation")
    except Exception:
        logger.exception("Email project reconciliation preview job failed")
