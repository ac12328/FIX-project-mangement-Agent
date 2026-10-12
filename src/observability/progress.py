"""Lightweight progress callback helpers for runtime instrumentation."""

from __future__ import annotations

import logging
from typing import Callable, Literal

logger = logging.getLogger(__name__)

ProgressStatus = Literal["running", "done", "error"]
ProgressCallback = Callable[[str, ProgressStatus, str | None], None]


def emit_progress(
    progress_cb: ProgressCallback | None,
    step_name: str,
    status: ProgressStatus,
    detail: str | None = None,
) -> None:
    """Emit one progress update without letting callback failures break the workflow."""
    if progress_cb is None:
        return
    try:
        progress_cb(step_name, status, detail)
    except Exception:
        logger.debug("Progress callback failed for step %s", step_name, exc_info=True)
