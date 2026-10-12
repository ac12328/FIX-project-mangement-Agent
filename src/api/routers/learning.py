"""Learning endpoints — read-only view of email knowledge mining results."""

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.deps import get_read_conn
from src.api.schemas.learning import (
    KnowledgeEntry,
    KnowledgeEntryDetail,
    LearningStats,
    MiningReportDict,
    SourceEmail,
)

router = APIRouter(prefix="/learning", tags=["learning"])


@router.get("/recent", response_model=list[KnowledgeEntry])
def get_recent(
    days: int = Query(7, ge=1, le=365),
    knowledge_type: str | None = Query(None),
    visibility: str | None = Query(None),
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> list[KnowledgeEntry]:
    """List recent knowledge entries with optional filters."""
    where_clauses = ["e.created_at >= datetime('now', ?)"]
    params: list = [f"-{days} days"]
    if knowledge_type:
        where_clauses.append("e.knowledge_type = ?")
        params.append(knowledge_type)
    if visibility:
        where_clauses.append("e.visibility = ?")
        params.append(visibility)
    where = " AND ".join(where_clauses)
    rows = conn.execute(
        f"""SELECT e.*, p.client_name AS project_name
            FROM email_knowledge_log e
            LEFT JOIN projects p ON e.project_id = p.project_id
            WHERE {where}
            ORDER BY e.created_at DESC
            LIMIT 200""",
        params,
    ).fetchall()
    return [
        KnowledgeEntry(
            entry_id=r["entry_id"],
            created_at=r["created_at"],
            thread_id=r["thread_id"],
            project_id=r["project_id"],
            project_name=r["project_name"],
            ticket_id=r["ticket_id"],
            knowledge_type=r["knowledge_type"],
            title=r["title"],
            content=r["content"],
            confidence=r["confidence"],
            visibility=r["visibility"],
            wiki_path=r["wiki_path"],
            source_email_count=len(json.loads(r["source_emails"] or "[]")),
        )
        for r in rows
    ]


@router.get("/stats", response_model=LearningStats)
def get_stats(
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> LearningStats:
    """Aggregated knowledge stats plus last mining run report."""
    total = conn.execute(
        "SELECT COUNT(*) FROM email_knowledge_log",
    ).fetchone()[0]
    by_type = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT knowledge_type, COUNT(*) FROM email_knowledge_log GROUP BY knowledge_type",
        )
    }
    by_vis = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT visibility, COUNT(*) FROM email_knowledge_log GROUP BY visibility",
        )
    }
    by_conf = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT confidence, COUNT(*) FROM email_knowledge_log GROUP BY confidence",
        )
    }
    last_run = None
    try:
        row = conn.execute(
            "SELECT memory_value FROM working_memory "
            "WHERE project_id = '_system' "
            "AND memory_key = 'email_knowledge_mining_last_run'",
        ).fetchone()
        if row and row[0]:
            last_run = MiningReportDict(**json.loads(row[0]))
    except Exception:
        pass
    return LearningStats(
        total_entries=total,
        by_type=by_type,
        by_visibility=by_vis,
        by_confidence=by_conf,
        last_run=last_run,
    )


@router.get("/entry/{entry_id}", response_model=KnowledgeEntryDetail)
def get_entry(
    entry_id: str,
    conn: sqlite3.Connection = Depends(get_read_conn),
) -> KnowledgeEntryDetail:
    """Single knowledge entry with source email details."""
    row = conn.execute(
        """SELECT e.*, p.client_name AS project_name
           FROM email_knowledge_log e
           LEFT JOIN projects p ON e.project_id = p.project_id
           WHERE e.entry_id = ?""",
        (entry_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Entry not found")
    email_ids = json.loads(row["source_emails"] or "[]")
    source_emails: list[SourceEmail] = []
    if email_ids:
        placeholders = ",".join("?" * len(email_ids))
        email_rows = conn.execute(
            f"SELECT email_id, sender_email, subject, direction, processed_at "
            f"FROM processed_emails WHERE email_id IN ({placeholders})",
            email_ids,
        ).fetchall()
        source_emails = [
            SourceEmail(
                email_id=er["email_id"],
                sender_email=er["sender_email"] or "",
                subject=er["subject"] or "",
                direction=er["direction"] or "",
                processed_at=er["processed_at"] or "",
            )
            for er in email_rows
        ]
    return KnowledgeEntryDetail(
        entry_id=row["entry_id"],
        created_at=row["created_at"],
        thread_id=row["thread_id"],
        project_id=row["project_id"],
        project_name=row["project_name"],
        ticket_id=row["ticket_id"],
        knowledge_type=row["knowledge_type"],
        title=row["title"],
        content=row["content"],
        confidence=row["confidence"],
        visibility=row["visibility"],
        wiki_path=row["wiki_path"],
        source_email_count=len(email_ids),
        source_emails=source_emails,
    )
