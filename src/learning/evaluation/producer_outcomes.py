"""Exact outcome policies for sent drafts and inbound client replies."""
from __future__ import annotations

import sqlite3

from src.learning.evaluation.attribution import AttributionRequest, AttributionResult, attribute_outcome
from src.memory.output_revision_repo import get_output_revision_for_artifact


def attribute_draft_outcome(
    conn: sqlite3.Connection, *, draft_id: str, draft_version: int,
    outcome_type: str, idempotency_key: str, actor_id: str,
) -> AttributionResult | None:
    """Apply documented all-rendered semantics to the exact draft version."""
    revision = get_output_revision_for_artifact(
        conn, surface="draft", artifact_type="draft", artifact_id=draft_id,
        revision=draft_version,
    )
    if not revision or not revision.get("trace_id"):
        return None
    return attribute_outcome(conn, AttributionRequest(
        idempotency_key=idempotency_key, operator_id=revision["operator_id"],
        surface="draft", trace_id=revision["trace_id"],
        output_revision_id=revision["output_revision_id"],
        outcome_type=outcome_type,
        outcome_value=-1.0 if outcome_type in {"correction", "negative"} else 1.0,
        actor_id=actor_id, feedback_scope="all_rendered",
        expected_artifact_type="draft", expected_artifact_id=draft_id,
    ))


def attribute_client_reply(
    conn: sqlite3.Connection, *, original_draft_id: str, reply_id: str,
    actor_id: str = "client", outcome_type: str = "client_reply",
) -> AttributionResult | None:
    """Resolve the sent version exactly; never guess a latest revision."""
    draft = conn.execute(
        "SELECT sent_version FROM drafts WHERE draft_id=?", (original_draft_id,),
    ).fetchone()
    if not draft or not draft[0]:
        return None
    try:
        version = int(draft[0])
    except (TypeError, ValueError):
        return None
    return attribute_draft_outcome(
        conn, draft_id=original_draft_id, draft_version=version,
        outcome_type=outcome_type, idempotency_key=f"client-reply:{reply_id}",
        actor_id=actor_id,
    )
