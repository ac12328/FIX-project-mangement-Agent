"""Repository operations for observation-bound durable learning jobs."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from src.learning.jobs.contracts import FailureOutcome, JobResult, LearningJob, LearningJobState
from src.learning.jobs.leases import new_lease_token
from src.learning.jobs.retry import retry_delay_seconds


def _model(row: sqlite3.Row | dict[str, Any]) -> LearningJob:
    value = dict(row)
    value["payload"] = json.loads(value.pop("payload_json") or "{}")
    result_json = value.pop("result_json", None)
    value["result"] = json.loads(result_json) if result_json else None
    value.pop("updated_at", None)
    return LearningJob.model_validate(value)


def enqueue_job(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    job_type: str,
    extractor_version: str,
    payload: dict[str, Any] | None = None,
    available_at: str | None = None,
) -> str:
    """Create one job or return the existing job for the same extraction identity."""
    job_id = f"ljob_{uuid.uuid4().hex[:20]}"
    conn.execute(
        """INSERT OR IGNORE INTO learning_outbox
           (job_id, observation_id, job_type, extractor_version, payload_json, available_at)
           VALUES (?, ?, ?, ?, ?, COALESCE(?, datetime('now')))""",
        (job_id, observation_id, job_type, extractor_version,
         json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str), available_at),
    )
    row = conn.execute(
        """SELECT job_id FROM learning_outbox
           WHERE observation_id=? AND job_type=? AND extractor_version=?""",
        (observation_id, job_type, extractor_version),
    ).fetchone()
    return str(row[0])


def get_job(conn: sqlite3.Connection, job_id: str) -> LearningJob | None:
    row = conn.execute("SELECT * FROM learning_outbox WHERE job_id=?", (job_id,)).fetchone()
    return _model(row) if row else None


def claim_jobs(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    limit: int = 10,
    lease_seconds: int = 300,
) -> list[LearningJob]:
    """Claim bounded due work; caller owns the short write transaction."""
    if limit <= 0:
        return []
    rows = conn.execute(
        """SELECT job_id FROM learning_outbox
           WHERE (state IN ('queued','retry') AND available_at <= datetime('now'))
              OR (state='processing' AND lease_expires_at <= datetime('now'))
           ORDER BY available_at, created_at, job_id LIMIT ?""",
        (limit,),
    ).fetchall()
    claimed: list[LearningJob] = []
    for row in rows:
        token = new_lease_token(worker_id)
        changed = conn.execute(
            """UPDATE learning_outbox
               SET state='processing', attempts=attempts+1, lease_token=?,
                   leased_at=datetime('now'), lease_expires_at=datetime('now', ?),
                   updated_at=datetime('now')
               WHERE job_id=? AND (
                   (state IN ('queued','retry') AND available_at <= datetime('now')) OR
                   (state='processing' AND lease_expires_at <= datetime('now'))
               )""",
            (token, f"+{max(1, lease_seconds)} seconds", str(row[0])),
        ).rowcount
        if changed:
            claimed_row = conn.execute(
                "SELECT * FROM learning_outbox WHERE job_id=? AND lease_token=?",
                (str(row[0]), token),
            ).fetchone()
            if claimed_row:
                claimed.append(_model(claimed_row))
    return claimed


def complete_job(
    conn: sqlite3.Connection,
    *,
    job: LearningJob,
    result: JobResult,
    db_materialize: Any | None = None,
) -> bool:
    """Atomically reserve a canonical effect, run DB-only materialization, and complete."""
    if not job.lease_token:
        return False
    current = conn.execute(
        """SELECT observation_id FROM learning_outbox
           WHERE job_id=? AND state='processing' AND lease_token=?""",
        (job.job_id, job.lease_token),
    ).fetchone()
    if not current:
        return False
    effect_key = result.canonical_effect_key(job)
    existing = conn.execute(
        """SELECT job_id, observation_id, effect_type FROM learning_job_effects
           WHERE effect_key=?""",
        (effect_key,),
    ).fetchone()
    if existing and tuple(existing) != (job.job_id, job.observation_id, result.effect_type):
        raise ValueError(f"learning effect key collision: {effect_key}")
    if not existing:
        conn.execute(
            """INSERT INTO learning_job_effects
               (effect_key, job_id, observation_id, effect_type, result_json)
               VALUES (?, ?, ?, ?, ?)""",
            (effect_key, job.job_id, job.observation_id, result.effect_type, result.model_dump_json()),
        )
        if db_materialize is not None:
            db_materialize(conn, result)
    changed = conn.execute(
        """UPDATE learning_outbox
           SET state='completed', result_json=?, effect_key=?, completed_at=datetime('now'),
               last_error=NULL, lease_token=NULL, leased_at=NULL, lease_expires_at=NULL,
               updated_at=datetime('now')
           WHERE job_id=? AND state='processing' AND lease_token=?""",
        (result.model_dump_json(), effect_key, job.job_id, job.lease_token),
    ).rowcount
    return changed == 1


def quarantine_job(conn: sqlite3.Connection, *, job_id: str, lease_token: str | None, result: JobResult) -> bool:
    if not lease_token:
        return False
    job_row = conn.execute("SELECT * FROM learning_outbox WHERE job_id=?", (job_id,)).fetchone()
    effect_key = result.canonical_effect_key(_model(job_row)) if job_row else None
    changed = conn.execute(
        """UPDATE learning_outbox
           SET state='quarantined', result_json=?, effect_key=?, last_error=?,
               completed_at=datetime('now'), lease_token=NULL, leased_at=NULL,
               lease_expires_at=NULL, updated_at=datetime('now')
           WHERE job_id=? AND state='processing' AND lease_token=?""",
        (result.model_dump_json(), effect_key, result.quarantine_reason, job_id, lease_token),
    ).rowcount
    return changed == 1


def fail_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str | None,
    error: str,
    max_attempts: int = 5,
) -> FailureOutcome:
    row = conn.execute(
        "SELECT attempts FROM learning_outbox WHERE job_id=? AND state='processing' AND lease_token=?",
        (job_id, lease_token),
    ).fetchone()
    if not row:
        return FailureOutcome(state=LearningJobState.PROCESSING)
    attempts = int(row[0])
    terminal = attempts >= max_attempts
    state = LearningJobState.DEAD_LETTER if terminal else LearningJobState.RETRY
    delay = 0 if terminal else retry_delay_seconds(attempts)
    conn.execute(
        """UPDATE learning_outbox
           SET state=?, available_at=datetime('now', ?), last_error=?,
               completed_at=CASE WHEN ? THEN datetime('now') ELSE NULL END,
               lease_token=NULL, leased_at=NULL, lease_expires_at=NULL,
               updated_at=datetime('now')
           WHERE job_id=? AND state='processing' AND lease_token=?""",
        (state.value, f"+{delay} seconds", error[:4000], int(terminal), job_id, lease_token),
    )
    return FailureOutcome(state=state, delay_seconds=delay)
