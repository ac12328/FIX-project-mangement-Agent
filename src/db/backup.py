"""Automated SQLite backup with integrity verification and rotation."""

import logging
import os
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

BACKUP_KEEP_COUNT = 7
_HKT = timedelta(hours=8)


def backup_database(
    db_path: str,
    backup_dir: str,
    *,
    label: str | None = None,
) -> dict:
    """Create a backup of an existing SQLite database using the backup API."""
    source_path = Path(db_path).resolve(strict=True)
    if not source_path.is_file() or source_path.stat().st_size == 0:
        raise ValueError(f"Database source is not a nonempty file: {db_path}")
    os.makedirs(backup_dir, exist_ok=True)

    now_hkt = datetime.now(timezone.utc) + _HKT
    if label is None:
        label = now_hkt.strftime("%Y-%m-%d_%H%M%S")
    backup_filename = f"agentfixbpro_{label}.db"
    backup_path = os.path.join(backup_dir, backup_filename)

    start = time.time()

    # Checkpoint WAL before backup — retry up to 3 times
    source = sqlite3.connect(f"{source_path.as_uri()}?mode=rw", uri=True, timeout=30)
    source.execute("PRAGMA busy_timeout = 30000")
    for attempt in range(1, 4):
        try:
            source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            break
        except sqlite3.OperationalError as e:
            logger.warning(
                "WAL checkpoint attempt %d/3 failed: %s", attempt, e,
            )
            if attempt < 3:
                time.sleep(5)
            else:
                logger.error("WAL checkpoint failed after 3 attempts — proceeding with backup anyway")

    # Use sqlite3 backup API for consistency
    dest = sqlite3.connect(backup_path)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    duration = round(time.time() - start, 2)
    size = os.path.getsize(backup_path)

    logger.info(
        "Backup complete: %s (%.1f KB, %.2fs)",
        backup_filename, size / 1024, duration,
    )

    return {
        "success": True,
        "backup_path": backup_path,
        "size_bytes": size,
        "duration_seconds": duration,
    }


def verify_backup(backup_path: str) -> bool:
    """Check an existing, nonempty backup without creating or modifying it."""
    conn = None
    try:
        path = Path(backup_path).resolve(strict=True)
        if not path.is_file() or path.stat().st_size == 0:
            logger.error("Backup verification rejected empty or non-file path: %s", backup_path)
            return False
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        if conn.execute("PRAGMA page_count").fetchone()[0] == 0:
            logger.error("Backup verification rejected empty database: %s", backup_path)
            return False
        result = conn.execute("PRAGMA integrity_check").fetchone()
        ok = result is not None and result[0] == "ok"
        if not ok:
            logger.error("Backup integrity FAILED: %s - %s", backup_path, result)
        return ok
    except Exception as exc:
        logger.error("Backup verification error: %s - %s", backup_path, exc)
        return False
    finally:
        if conn is not None:
            conn.close()


def rotate_backups(backup_dir: str) -> int:
    """Retain the newest verified copies; preserve invalid files for investigation."""
    candidates = sorted(
        f for f in os.listdir(backup_dir)
        if f.startswith("agentfixbpro_") and f.endswith(".db")
    )
    valid = []
    for filename in candidates:
        path = os.path.join(backup_dir, filename)
        if verify_backup(path):
            valid.append(filename)
        else:
            logger.warning("Invalid backup excluded from retention count: %s", filename)
    deleted = 0
    for filename in valid[:max(0, len(valid) - BACKUP_KEEP_COUNT)]:
        os.remove(os.path.join(backup_dir, filename))
        logger.info("Rotated old backup: %s", filename)
        deleted += 1
    return deleted
