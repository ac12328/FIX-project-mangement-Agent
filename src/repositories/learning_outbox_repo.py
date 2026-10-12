"""Compatibility adapters for the observation-bound durable learning outbox."""
from __future__ import annotations

import sqlite3
from typing import Any

from src.learning.ingest.conversation import capture_conversation_flush
from src.learning.jobs.contracts import JobResult
from src.learning.jobs.outbox_repo import claim_jobs, complete_job, enqueue_job, fail_job


def enqueue_learning(
    conn: sqlite3.Connection,
    *,
    idempotency_key: str,
    surface: str,
    conversation_id: str,
    messages: list[dict[str, Any]],
    round_count: int,
    tool_call_count: int,
    notify_chat_id: int | None = None,
    enqueue: bool = True,
    operator_id: str = "default_operator",
    project_id: str | None = None,
) -> str:
    observation = capture_conversation_flush(
        conn,
        source_id=idempotency_key,
        operator_id=operator_id,
        project_id=project_id,
        messages=messages,
    )
    if not enqueue:
        return observation.observation_id
    return enqueue_job(
        conn,
        observation_id=observation.observation_id,
        job_type="conversation_extraction",
        extractor_version="conversation-v2",
        payload={
            "surface": surface,
            "conversation_id": conversation_id,
            "round_count": round_count,
            "tool_call_count": tool_call_count,
            "notify_chat_id": notify_chat_id,
        },
    )


def claim_due_jobs(conn: sqlite3.Connection, *, limit: int = 10, lease_minutes: int = 15):
    jobs = claim_jobs(conn, worker_id="legacy-conversation-worker", limit=limit, lease_seconds=lease_minutes * 60)
    token = jobs[0].lease_token if jobs else ""
    values = []
    for job in jobs:
        item = job.model_dump(mode="json")
        item.update(item.pop("payload"))
        item["outbox_id"] = item["job_id"]
        item["lease_owner"] = item["lease_token"]
        values.append(item)
    return token, values


def mark_completed(conn: sqlite3.Connection, outbox_id: str, lease_owner: str, result: dict[str, Any]) -> bool:
    from src.learning.jobs.outbox_repo import get_job
    job = get_job(conn, outbox_id)
    if job is None or job.lease_token != lease_owner:
        return False
    return complete_job(
        conn, job=job,
        result=JobResult(effect_type="legacy_capture", payload=result),
    )


def mark_failed(conn: sqlite3.Connection, outbox_id: str, lease_owner: str, error: str, *, max_attempts: int = 5) -> bool:
    outcome = fail_job(
        conn, job_id=outbox_id, lease_token=lease_owner,
        error=error, max_attempts=max_attempts,
    )
    return outcome.state.value in {"retry", "dead_letter"}
