"""Preview-only scheduled catch-up for email-backed project truth."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from src.agent.project_resolve import resolve_project_reference
from src.project_reconciliation.contracts import ReconciliationMode
from src.project_reconciliation.service import reconcile_project_from_emails


RECONCILIATION_LOOKBACK_DAYS = 30


def run_email_project_reconciliation_preview(
    conn: sqlite3.Connection,
    *,
    operator_id: str,
    chat_id: str,
    now_hkt: datetime | None = None,
    lookback_days: int = RECONCILIATION_LOOKBACK_DAYS,
) -> dict:
    """Create durable preview checkpoints for projects with recent email evidence."""
    hkt = timezone(timedelta(hours=8))
    now_hkt = now_hkt or datetime.now(hkt)
    if now_hkt.tzinfo is None:
        now_hkt = now_hkt.replace(tzinfo=hkt)
    else:
        now_hkt = now_hkt.astimezone(hkt)
    effective_lookback = max(1, lookback_days)
    threshold = (now_hkt - timedelta(days=effective_lookback)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    run_date = now_hkt.date().isoformat()

    rows = conn.execute(
        """SELECT DISTINCT p.project_id
             FROM projects p
            WHERE p.project_status NOT IN ('Completed','Archived','Cancelled')
              AND (
                    EXISTS (
                        SELECT 1 FROM processed_emails e
                         WHERE e.project_id=p.project_id
                           AND datetime(COALESCE(e.received_at, e.processed_at)) >= datetime(?)
                    )
                 OR EXISTS (
                        SELECT 1 FROM processed_emails e
                         WHERE e.project_id IS NULL
                           AND upper(COALESCE(e.subject, ''))
                               LIKE '%[' || upper(p.project_id) || ']%'
                           AND datetime(COALESCE(e.received_at, e.processed_at)) >= datetime(?)
                    )
                 OR EXISTS (
                        SELECT 1 FROM inbound_replies r
                         WHERE r.project_id=p.project_id
                           AND datetime(r.received_at) >= datetime(?)
                    )
              )
            ORDER BY p.project_id""",
        (threshold, threshold, threshold),
    ).fetchall()
    candidate_ids = {str(row[0]).strip().upper() for row in rows if row[0]}

    existing_ids = {
        str(row[0]).strip().upper()
        for row in conn.execute("SELECT project_id FROM projects").fetchall()
        if row[0]
    }
    tagged_rows = conn.execute(
        """SELECT subject FROM processed_emails
            WHERE project_id IS NULL
              AND subject IS NOT NULL
              AND instr(subject, '[') > 0
              AND datetime(COALESCE(received_at, processed_at)) >= datetime(?)""",
        (threshold,),
    ).fetchall()
    for row in tagged_rows:
        resolved = resolve_project_reference(str(row[0] or ""))
        if (
            resolved.project_id
            and not resolved.ambiguous
            and resolved.project_id not in existing_ids
        ):
            candidate_ids.add(resolved.project_id)

    previews: list[dict] = []
    for project_id in sorted(candidate_ids):
        result = reconcile_project_from_emails(
            conn,
            project_id=project_id,
            mode=ReconciliationMode.PREVIEW,
            operator_id=str(operator_id),
            chat_id=str(chat_id),
            trusted_surface="scheduler",
            trusted_explicit_operator=False,
            request_key=(
                f"scheduler:email-project-reconciliation:{run_date}:{project_id}"
            ),
            create_if_missing=False,
            trigger_kind="scheduler",
        )
        previews.append({
            "project_id": project_id,
            "status": result.status,
            "outcome": result.outcome,
            "operation_id": result.operation_id,
            "project_exists": result.current_project is not None,
            "proposed_fields": dict(result.proposed_fields),
            "committed": result.committed,
        })

    return {
        "run_date_hkt": run_date,
        "lookback_days": effective_lookback,
        "candidate_count": len(candidate_ids),
        "proposal_count": sum(item["outcome"] == "proposed" for item in previews),
        "committed_count": sum(item["committed"] for item in previews),
        "previews": previews,
    }
