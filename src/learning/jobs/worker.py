"""Bounded durable learning worker with short claim/finalization transactions."""
from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from src.learning.jobs.contracts import EffectPhase, JobResult, LearningJob, LearningJobState, WorkerRunResult
from src.learning.jobs.dead_letter import alert_dead_letter
from src.learning.jobs.outbox_repo import claim_jobs, complete_job, fail_job, quarantine_job
from src.repositories.learning_observation_repo import get_observation

logger = logging.getLogger(__name__)
JobHandler = Callable[[LearningJob, Any], JobResult]


class LearningJobWorker:
    """Claim, execute outside the write lock, then finalize using the lease token."""

    def __init__(
        self,
        *,
        handlers: dict[str, JobHandler] | None = None,
        db_materializers: dict[str, Callable[[sqlite3.Connection, JobResult], None]] | None = None,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
        worker_id: str | None = None,
        max_attempts: int = 5,
        dead_letter_notifier: Callable[[LearningJob, str], None] | None = None,
    ) -> None:
        self.handlers = handlers or default_handlers()
        # These callbacks must perform deterministic SQLite writes only: no LLM, network, or filesystem I/O.
        self.db_materializers = db_materializers if db_materializers is not None else default_materializers()
        self.connection_factory = connection_factory
        self.worker_id = worker_id or f"learning-{uuid.uuid4().hex[:10]}"
        self.max_attempts = max_attempts
        self.dead_letter_notifier = dead_letter_notifier

    @contextmanager
    def _write(self):
        if self.connection_factory is None:
            from src.db.connection import scoped_write
            with scoped_write() as conn:
                yield conn
            return
        conn = self.connection_factory()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def run_once(self, *, limit: int = 10, lease_seconds: int = 300) -> WorkerRunResult:
        from src.learning.governance_flags import candidate_only, worker_disabled, writes_disabled
        if worker_disabled() or limit <= 0:
            return WorkerRunResult()
        with self._write() as conn:
            jobs = claim_jobs(conn, worker_id=self.worker_id, limit=limit, lease_seconds=lease_seconds)
        report = WorkerRunResult(claimed=len(jobs))
        for job in jobs:
            result: JobResult | None = None
            with self._write() as conn:
                observation = get_observation(conn, job.observation_id)
            try:
                handler = self.handlers[job.job_type]
                result = handler(job, observation)
                db_materializer = self.db_materializers.get(result.effect_type)
                disallowed_candidate_only = result.effect_phase not in {
                    EffectPhase.CAPTURE_ONLY, EffectPhase.CANDIDATE_PROPOSAL,
                }
                if result.effect_phase is EffectPhase.EXTERNAL_ACTION:
                    result = result.model_copy(update={
                        "quarantine_reason": result.quarantine_reason or "external effect requires reconciliation",
                    })
                elif candidate_only() and disallowed_candidate_only:
                    result = result.model_copy(update={
                        "quarantine_reason": f"candidate-only blocks {result.effect_phase.value}",
                    })
                elif db_materializer is not None and writes_disabled():
                    result = result.model_copy(update={
                        "quarantine_reason": "learning writes disabled before materialization",
                    })
                if result.quarantine_reason:
                    db_materializer = None
                with self._write() as conn:
                    changed = (
                        quarantine_job(conn, job_id=job.job_id, lease_token=job.lease_token, result=result)
                        if result.quarantine_reason
                        else complete_job(conn, job=job, result=result, db_materialize=db_materializer)
                    )
                if not changed:
                    report.stale += 1
                elif result.quarantine_reason:
                    report.quarantined += 1
                else:
                    report.completed += 1
            except ValueError as exc:
                logger.error("Learning job %s quarantined: %s", job.job_id, exc)
                failed_result = result
                if failed_result is None:
                    failed_result = JobResult(
                        effect_type="candidate_proposal",
                        effect_phase=EffectPhase.CANDIDATE_PROPOSAL,
                    )
                collision = failed_result.model_copy(update={"quarantine_reason": str(exc)})
                with self._write() as conn:
                    changed = quarantine_job(
                        conn, job_id=job.job_id, lease_token=job.lease_token, result=collision,
                    )
                report.quarantined += int(changed)
                report.stale += int(not changed)
            except Exception as exc:
                logger.warning("Learning job %s failed: %s", job.job_id, exc)
                with self._write() as conn:
                    failure = fail_job(
                        conn, job_id=job.job_id, lease_token=job.lease_token,
                        error=str(exc), max_attempts=self.max_attempts,
                    )
                if failure.state is LearningJobState.DEAD_LETTER:
                    report.dead_lettered += 1
                    alert_dead_letter(job, str(exc), self.dead_letter_notifier)
                elif failure.state is LearningJobState.RETRY:
                    report.retried += 1
                else:
                    report.stale += 1
        return report


def _placeholder(job: LearningJob, observation: Any) -> JobResult:
    """WP-03 extraction boundary; WP-04 replaces this with typed candidates."""
    return JobResult(
        effect_type="capture_only",
        effect_phase=EffectPhase.CAPTURE_ONLY,
        payload={"capture_only": True, "observation_id": job.observation_id},
    )


def default_handlers() -> dict[str, JobHandler]:
    from src.learning.extraction.base import build_extraction_handler
    from src.learning.extraction.fact_extractor import FactExtractor
    from src.learning.extraction.feedback_router import FeedbackExtractionRouter
    from src.learning.extraction.source_intent_router import SourceIntentExtractionRouter
    from src.learning.ingest.contracts import ObservationSourceType
    from src.learning.extraction.preference_extractor import PreferenceExtractor
    from src.learning.extraction.procedure_extractor import ProcedureExtractor
    from src.learning.extraction.wiki_extractor import WikiPublicationExtractor

    fact = build_extraction_handler(FactExtractor())
    preference = build_extraction_handler(PreferenceExtractor())
    procedure = build_extraction_handler(ProcedureExtractor())
    wiki = build_extraction_handler(WikiPublicationExtractor())
    return {
        "semantic_fact_extraction": fact,
        "preference_extraction": preference,
        "procedure_extraction": procedure,
        "wiki_extraction": wiki,
        "conversation_extraction": SourceIntentExtractionRouter(ObservationSourceType.CONVERSATION_FLUSH),
        "feedback_extraction": FeedbackExtractionRouter(),
        "post_send_extraction": SourceIntentExtractionRouter(ObservationSourceType.DRAFT_EDIT),
    }


def default_materializers() -> dict[str, Callable[[sqlite3.Connection, JobResult], None]]:
    from src.learning.extraction.base import candidate_materializer
    return {"candidate_proposal": candidate_materializer}
