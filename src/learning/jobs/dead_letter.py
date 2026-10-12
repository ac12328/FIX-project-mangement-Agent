"""Dead-letter alert boundary for durable learning jobs."""
from __future__ import annotations

import logging
from collections.abc import Callable

from src.learning.jobs.contracts import LearningJob

logger = logging.getLogger(__name__)


def alert_dead_letter(job: LearningJob, error: str, notifier: Callable[[LearningJob, str], None] | None = None) -> None:
    """Emit an operational alert without changing the durable terminal state."""
    logger.error("Learning job %s dead-lettered after %d attempts: %s", job.job_id, job.attempts, error)
    if notifier is not None:
        notifier(job, error)
