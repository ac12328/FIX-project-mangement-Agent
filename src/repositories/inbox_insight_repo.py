"""Persistence for actionable inbox intelligence insights."""

from __future__ import annotations

import json
import sqlite3

from src.models.inbox_insight import InboxInsight


def upsert_inbox_insight(conn: sqlite3.Connection, insight: InboxInsight) -> None:
    conn.execute(
        """INSERT INTO inbox_insights (
               insight_id, email_id, operator_id, priority, priority_reasons_json,
               action_required, request_summary, suggested_action, due_at,
               proposed_owner, related_entities_json, evidence_spans_json,
               confidence, review_status, category, suppressed,
               suppression_reason, dedup_key
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(operator_id, dedup_key) DO UPDATE SET
               priority = excluded.priority,
               priority_reasons_json = excluded.priority_reasons_json,
               action_required = excluded.action_required,
               request_summary = excluded.request_summary,
               suggested_action = excluded.suggested_action,
               due_at = excluded.due_at,
               proposed_owner = excluded.proposed_owner,
               related_entities_json = excluded.related_entities_json,
               evidence_spans_json = excluded.evidence_spans_json,
               confidence = excluded.confidence,
               review_status = excluded.review_status,
               category = excluded.category,
               suppressed = excluded.suppressed,
               suppression_reason = excluded.suppression_reason,
               updated_at = datetime('now')""",
        (
            insight.insight_id,
            insight.email_id,
            insight.operator_id,
            insight.priority,
            json.dumps(insight.priority_reasons, ensure_ascii=False),
            1 if insight.action_required else 0,
            insight.request_summary,
            insight.suggested_action,
            insight.due_at,
            insight.proposed_owner,
            json.dumps(insight.related_entities, ensure_ascii=False),
            json.dumps(insight.evidence_spans, ensure_ascii=False),
            insight.confidence,
            insight.review_status,
            insight.category,
            1 if insight.suppressed else 0,
            insight.suppression_reason,
            insight.dedup_key,
        ),
    )


def get_inbox_insight(conn: sqlite3.Connection, insight_id: str) -> InboxInsight | None:
    row = conn.execute(
        "SELECT * FROM inbox_insights WHERE insight_id = ?", (insight_id,)
    ).fetchone()
    return _row(row) if row else None


def list_inbox_insights(
    conn: sqlite3.Connection,
    operator_id: str,
    *,
    priority: str | None = None,
    action_required: bool | None = None,
    include_suppressed: bool = False,
    limit: int = 100,
) -> list[InboxInsight]:
    clauses = ["operator_id = ?"]
    params: list = [operator_id]
    if priority:
        clauses.append("priority = ?")
        params.append(priority)
    if action_required is not None:
        clauses.append("action_required = ?")
        params.append(1 if action_required else 0)
    if not include_suppressed:
        clauses.append("suppressed = 0")
    params.append(limit)
    rows = conn.execute(
        f"""SELECT * FROM inbox_insights WHERE {' AND '.join(clauses)}
            ORDER BY CASE priority
                WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                WHEN 'normal' THEN 2 ELSE 3 END,
                created_at DESC LIMIT ?""",
        params,
    ).fetchall()
    return [_row(row) for row in rows]


def _row(row: sqlite3.Row) -> InboxInsight:
    data = dict(row)
    data["priority_reasons"] = json.loads(data.pop("priority_reasons_json") or "[]")
    data["related_entities"] = json.loads(data.pop("related_entities_json") or "[]")
    data["evidence_spans"] = json.loads(data.pop("evidence_spans_json") or "[]")
    data["action_required"] = bool(data["action_required"])
    data["suppressed"] = bool(data["suppressed"])
    return InboxInsight(**data)
