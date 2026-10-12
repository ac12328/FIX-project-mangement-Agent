"""Operator controls for durable learning jobs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from src.db.connection import scoped_write
from src.learning.control_authorization import authorize, resolve_job
from src.learning.jobs.outbox_repo import enqueue_job, get_job
from src.repositories.activity_log_repo import insert_activity


class JobControlError(ValueError):
    pass


def control_job(
    job_id: str,
    *,
    action: str,
    actor_id: str,
    reason: str,
    idempotency_key: str,
    extractor_version: str | None = None,
) -> dict:
    """Apply an idempotent operator transition without bypassing job identity rules."""
    with scoped_write() as conn:
        authorize(resolve_job(conn, job_id), operator_id=actor_id)
        job = get_job(conn, job_id)
        if job is None:
            raise KeyError(job_id)
        marker = f"learning_job_control:{idempotency_key}"
        prior = conn.execute(
            "SELECT metadata_json FROM activity_log WHERE action_type='learning_job_control' AND object_ref=?",
            (marker,),
        ).fetchone()
        if prior:
            return json.loads(prior[0])["result"]
        if action == "retry":
            if job.state.value not in {"dead_letter", "quarantined", "retry"}:
                raise JobControlError("retry requires retry, quarantined, or dead-letter state")
            changed = conn.execute(
                """UPDATE learning_outbox SET state='retry',available_at=datetime('now'),last_error=NULL,
                   lease_token=NULL,leased_at=NULL,lease_expires_at=NULL,completed_at=NULL,updated_at=datetime('now')
                   WHERE job_id=? AND state IN ('dead_letter','quarantined','retry') AND lease_token IS NULL""",
                (job_id,),
            ).rowcount
            if changed != 1:
                raise JobControlError("job state changed before retry")
            result = {"job_id": job_id, "status": "retry"}
        elif action == "quarantine":
            if job.state.value in {"completed", "processing"}:
                raise JobControlError("only non-leased, non-completed jobs can be quarantined")
            changed = conn.execute(
                """UPDATE learning_outbox SET state='quarantined',last_error=?,completed_at=datetime('now'),
                   lease_token=NULL,leased_at=NULL,lease_expires_at=NULL,updated_at=datetime('now')
                   WHERE job_id=? AND state IN ('queued','retry','dead_letter','quarantined')
                     AND lease_token IS NULL""",
                ("quarantined: " + reason, job_id),
            ).rowcount
            if changed != 1:
                raise JobControlError("job state changed before quarantine")
            result = {"job_id": job_id, "status": "quarantined"}
        elif action == "replay":
            if not extractor_version or extractor_version == job.extractor_version:
                raise JobControlError("replay requires a different extractor version")
            replay_id = enqueue_job(
                conn,
                observation_id=job.observation_id,
                job_type=job.job_type,
                extractor_version=extractor_version,
                payload={**job.payload, "replay_of": job.job_id, "requested_by": actor_id},
            )
            result = {"job_id": replay_id, "status": "queued"}
        else:
            raise JobControlError(f"unsupported job action: {action}")
        insert_activity(
            conn,
            actor=actor_id,
            surface="learning_governance",
            action_type="learning_job_control",
            object_ref=marker,
            summary=f"Learning job {action}: {job_id}",
            metadata_json=json.dumps({"reason": reason, "result": result}, sort_keys=True),
        )
        return result
