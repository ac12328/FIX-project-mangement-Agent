"""Ordered, restartable migration dispatch without migration-body policy."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class MigrationStep:
    version: int
    apply: Callable[[sqlite3.Connection], None]
    description: str | None = None
    reconcile: bool = False


def apply_migration_steps(
    conn: sqlite3.Connection,
    *,
    current: int,
    steps: Iterable[MigrationStep],
    record: Callable[[sqlite3.Connection, int], None],
    logger: logging.Logger,
) -> None:
    """Apply missing versions in order; rerun only marked reconciliation steps.

    The version record remains the caller's responsibility because historical
    migration bodies and record_migration each own their existing commits.
    """
    ordered = tuple(steps)
    versions = (1, *(step.version for step in ordered))
    if any(left >= right for left, right in zip(versions, versions[1:])):
        raise ValueError("Migration steps must have unique ascending versions")
    for step in ordered:
        if current < step.version:
            step.apply(conn)
            record(conn, step.version)
            if step.description is not None:
                logger.info("Migration v%s applied (%s)", step.version, step.description)
        elif step.reconcile:
            step.apply(conn)
