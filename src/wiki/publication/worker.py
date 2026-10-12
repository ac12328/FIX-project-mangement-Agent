"""Policy-free maintenance worker for approved Wiki publication facts."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.db.connection import scoped_read, scoped_write
from src.wiki.publication.models import MaintenanceResult

MAX_ATTEMPTS = 3


def _aware_utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _discover(limit: int, *, now: datetime | None = None) -> list[str]:
    now_text = _aware_utc(now).isoformat()
    with scoped_read() as conn:
        rows = conn.execute("""SELECT f.fact_id FROM memory_facts f
          JOIN memory_decisions d ON d.decision_id=f.decision_id
          JOIN candidate_validation_runs v ON v.validation_id=f.validation_id
          JOIN memory_fact_state_events s ON s.fact_id=f.fact_id
          LEFT JOIN wiki_publications p ON p.fact_id=f.fact_id
          WHERE f.target_type='wiki_publication' AND f.status='active' AND v.passed=1
            AND d.decision_type IN ('approve','edit_and_approve','narrow_scope','supersede')
            AND s.event_id=(SELECT event_id FROM memory_fact_state_events
              WHERE fact_id=f.fact_id ORDER BY effective_at DESC,created_at DESC,event_seq DESC LIMIT 1)
            AND s.state='active' AND (
              p.publication_id IS NULL OR p.status IN ('proposed','failed')
              OR (p.status='publishing' AND p.lease_expires_at IS NOT NULL AND p.lease_expires_at<=?)
            )
          ORDER BY f.valid_from,f.fact_id LIMIT ?""", (now_text, limit)).fetchall()
    return [row["fact_id"] for row in rows]


def run_publication_maintenance(*, wiki_dir: Path, max_jobs: int = 10,
                                now: datetime | None = None) -> MaintenanceResult:
    """Discover, propose, and publish a bounded batch with durable retry state."""
    from src.wiki.publication.proposer import propose_publication
    from src.wiki.publication.publisher import publish_fact

    current_time = _aware_utc(now)
    facts = _discover(max_jobs, now=current_time)
    counts = {"discovered": len(facts), "proposed": 0, "completed": 0,
              "failed": 0, "manual_review": 0, "dead_letter": 0}
    for fact_id in facts:
        try:
            record = propose_publication(fact_id); counts["proposed"] += 1
            result = publish_fact(fact_id, wiki_dir=Path(wiki_dir), now=current_time)
            if result.status in {"active", "revoked"}:
                counts["completed"] += 1
            elif result.status == "manual_review":
                counts["manual_review"] += 1
            else:
                counts["failed"] += 1
        except Exception as exc:
            counts["failed"] += 1
            with scoped_write() as conn:
                row = conn.execute("SELECT publication_id,status,attempts FROM wiki_publications WHERE fact_id=?",
                                   (fact_id,)).fetchone()
                if row and row["status"] == "dead_letter":
                    counts["dead_letter"] += 1
                elif row and row["status"] == "failed" and int(row["attempts"] or 0) >= MAX_ATTEMPTS:
                    changed = conn.execute("""UPDATE wiki_publications SET status='dead_letter',last_error=?
                      WHERE publication_id=? AND status='failed'""", (str(exc), row["publication_id"]))
                    if changed.rowcount == 1:
                        counts["dead_letter"] += 1
    return MaintenanceResult(**counts)
