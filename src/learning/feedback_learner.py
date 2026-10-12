"""Extract learning from operator AI feedback -- close the feedback loop."""
from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.llm.client import llm_call_structured

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "feedback_rule_extraction.md"

_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "rule_type": {"type": "string", "enum": ["response_pattern", "learned_rule", "skip"]},
        "rule": {"type": "string"},
        "condition": {"type": ["string", "null"]},
        "contact_specific": {"type": "boolean"},
        "confidence": {"type": "number"},
        "pattern_field": {"type": ["string", "null"]},
        "pattern_value": {"type": ["string", "null"]},
    },
    "required": ["rule_type", "rule", "confidence"],
}


def process_feedback_for_learning(
    conn: sqlite3.Connection,
    *,
    feedback_id: str,
    item_type: str,
    item_id: str,
    project_id: str | None,
    feedback_kind: str,
    category: str | None,
    operator_note: str | None,
    operator_id: str | None = None,
    surface: str | None = None,
    trace_id: str | None = None,
    output_revision_id: str | None = None,
    fact_id: str | None = None,
    fact_version: int | None = None,
    feedback_scope: str | None = None,
) -> str:
    """Route feedback to exact v2 attribution or the legacy learner."""
    try:
        from config import defaults
        if defaults.EXACT_OUTCOME_ATTRIBUTION_V2:
            if not all((operator_id, surface, trace_id, output_revision_id)):
                return "unattributable"
            from src.learning.evaluation.attribution import AttributionRequest, attribute_outcome
            outcome_type = {"correct": "positive", "wrong": "negative", "edited": "correction"}.get(feedback_kind)
            if not outcome_type:
                return "unattributable"
            result = attribute_outcome(conn, AttributionRequest(
                idempotency_key=f"feedback:{feedback_id}", operator_id=operator_id,
                surface=surface, trace_id=trace_id,
                output_revision_id=output_revision_id, outcome_type=outcome_type,
                outcome_value=1.0 if feedback_kind == "correct" else -1.0,
                reason=operator_note, actor_id=operator_id, feedback_id=feedback_id,
                fact_id=fact_id, fact_version=fact_version,
                feedback_scope=feedback_scope,
                expected_artifact_type=item_type, expected_artifact_id=item_id,
                expected_project_id=project_id,
            ))
            return result.status
        if feedback_kind == "correct":
            _handle_correct(conn, item_type, item_id)
        elif feedback_kind == "wrong" and operator_note:
            _handle_wrong(conn, item_type, item_id, project_id, category, operator_note)
        # edited and unknown kinds -> no-op
        return "legacy"
    except Exception:
        logger.exception("Feedback learning failed for %s", feedback_id)
        return "unattributable"


def _handle_wrong(
    conn: sqlite3.Connection,
    item_type: str,
    item_id: str,
    project_id: str | None,
    category: str | None,
    operator_note: str,
) -> None:
    """Extract a rule from 'wrong' feedback via LLM and persist it."""
    prompt_text = _PROMPT_PATH.read_text(encoding="utf-8")

    # Fetch original AI output for context
    ai_output = _fetch_ai_output(conn, item_type, item_id)

    # Load existing rules to avoid duplicates
    existing_rules_text = _format_existing_rules_for_context(conn)

    user_msg = (
        f"Feedback on: {item_type} ({item_id})\n"
        f"Project: {project_id or 'unknown'}\n"
        f"Category: {category or 'unspecified'}\n"
        f"Operator note: {operator_note}"
    )
    if ai_output:
        user_msg += f"\n\n## Original AI Output\n{ai_output[:1000]}"
    if existing_rules_text:
        user_msg += f"\n\n## Existing Rules (avoid duplicates)\n{existing_rules_text}"

    try:
        result = llm_call_structured(
            "feedback_rule_extraction",
            [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": user_msg},
            ],
            _EXTRACTION_SCHEMA,
            temperature=0.1,
        )
    except Exception:
        logger.warning(
            "LLM call failed in _handle_wrong for %s/%s", item_type, item_id, exc_info=True,
        )
        return

    # Handle skip — vague feedback that doesn't produce a useful rule
    if result.get("rule_type") == "skip" or result.get("rule_type") is None:
        logger.info("Feedback for %s/%s skipped (no actionable rule)", item_type, item_id)
        return

    if result.get("rule_type") == "response_pattern" and project_id:
        _apply_response_pattern(conn, project_id, result)
    elif result.get("rule_type") == "learned_rule":
        _create_learned_rule(conn, project_id, result)


def _fetch_ai_output(
    conn: sqlite3.Connection,
    item_type: str,
    item_id: str,
) -> str | None:
    """Fetch the original AI output text for context in rule extraction."""
    try:
        if item_type == "draft":
            row = conn.execute(
                "SELECT current_draft_text FROM drafts WHERE draft_id = ?", (item_id,)
            ).fetchone()
            if row:
                return row[0] if not isinstance(row, dict) else row.get("current_draft_text")
        # For other item types, try to find in llm_traces via decision_outcome
        row = conn.execute(
            "SELECT decision_outcome FROM llm_traces WHERE trace_id = ? LIMIT 1", (item_id,)
        ).fetchone()
        if row:
            return row[0] if not isinstance(row, dict) else row.get("decision_outcome")
    except Exception:
        logger.debug("Could not fetch AI output for %s/%s", item_type, item_id)
    return None


def _format_existing_rules_for_context(conn: sqlite3.Connection) -> str:
    """Format existing rules for inclusion in extraction prompt to avoid duplicates."""
    try:
        rows = conn.execute(
            """SELECT rule, condition FROM learned_rules
               WHERE status != 'rejected'
               ORDER BY usage_count DESC, confidence DESC
               LIMIT 10""",
        ).fetchall()
        if not rows:
            return ""
        lines = []
        for row in rows:
            d = dict(row)
            cond = d.get("condition")
            rule = d.get("rule", "")
            if cond:
                lines.append(f"- IF {cond} THEN {rule}")
            else:
                lines.append(f"- {rule}")
        return "\n".join(lines)
    except Exception:
        return ""


def _apply_response_pattern(
    conn: sqlite3.Connection,
    project_id: str,
    extraction: dict,
) -> None:
    """Write a contact-specific response pattern from feedback."""
    contact_id = _resolve_contact_for_project(conn, project_id)
    if not contact_id:
        logger.debug("No contact for project %s, falling back to learned_rule", project_id)
        _create_learned_rule(conn, project_id, extraction)
        return

    field = extraction.get("pattern_field")
    value = extraction.get("pattern_value")
    if not field or not value:
        return

    from src.repositories.response_pattern_repo import upsert_response_pattern

    if field == "avoid_phrases":
        from src.repositories.response_pattern_repo import add_avoid_phrase

        add_avoid_phrase(conn, contact_id, value, project_id=project_id)
    else:
        upsert_response_pattern(
            conn,
            contact_id,
            project_id,
            **{
                field: value,
                "source": "operator_feedback",
                "confidence": extraction.get("confidence", 0.7),
            },
        )
    conn.commit()


def _create_learned_rule(
    conn: sqlite3.Connection,
    project_id: str | None,
    extraction: dict,
) -> None:
    """Persist a general learned rule from feedback via shared auto-approve gate."""
    from src.learning.auto_approve import resolve_rule_status
    from src.repositories.learned_rules_repo import (
        insert_learned_rule,
        merge_into_existing,
    )

    rule_text = extraction.get("rule", "")
    new_confidence = float(extraction.get("confidence", 0.7) or 0.7)

    status_or_merge, merge_id = resolve_rule_status(
        conn,
        rule=rule_text,
        confidence=new_confidence,
        trigger_type="operator_feedback",
        condition=extraction.get("condition"),
    )
    if status_or_merge == "merge" and merge_id:
        merge_into_existing(conn, merge_id, new_confidence)
        conn.commit()
        logger.info(
            "Merged duplicate rule into %s (text: %.60s...)",
            merge_id, rule_text,
        )
        return

    rule_id = f"lr_{uuid.uuid4().hex[:12]}"
    insert_learned_rule(
        conn,
        {
            "rule_id": rule_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "trigger_type": "operator_feedback",
            "source_project_id": project_id,
            "condition": extraction.get("condition"),
            "rule": rule_text,
            "scope": "global",
            "applicable_to": '["*"]',
            "status": status_or_merge,
            "confidence": new_confidence,
            "usage_count": 0,
        },
    )
    conn.commit()


def _handle_correct(
    conn: sqlite3.Connection,
    item_type: str,
    item_id: str,
) -> None:
    """Reinforce rules that contributed to a correct output."""
    rule_ids = _find_rules_used_for_item(conn, item_type, item_id)
    if not rule_ids:
        return
    from src.repositories.learned_rules_repo import increment_usage_count

    for rid in rule_ids:
        increment_usage_count(conn, rid)
    conn.commit()


def _find_rules_used_for_item(
    conn: sqlite3.Connection,
    item_type: str,
    item_id: str,
) -> list[str]:
    """Look up llm_traces to find which learned rules were active for an item."""
    try:
        rows = conn.execute(
            """SELECT trace_metadata FROM llm_traces
               WHERE task LIKE '%draft%' AND trace_metadata LIKE ?
               ORDER BY created_at DESC LIMIT 5""",
            (f"%{item_id}%",),
        ).fetchall()
        rule_ids: list[str] = []
        for row in rows:
            meta = row[0] if not isinstance(row, dict) else row.get("trace_metadata", "")
            if meta:
                found = re.findall(r"lr_[a-f0-9]{12}", str(meta))
                rule_ids.extend(found)
        return list(set(rule_ids))
    except Exception:
        logger.debug("Could not look up rules for item %s", item_id, exc_info=True)
        return []


def _resolve_contact_for_project(
    conn: sqlite3.Connection,
    project_id: str,
) -> str | None:
    """Find the primary contact associated with a project via response_pattern_memory."""
    row = conn.execute(
        """SELECT contact_id FROM contacts
           WHERE contact_id IN (
               SELECT contact_id FROM response_pattern_memory WHERE project_id = ?
           ) LIMIT 1""",
        (project_id,),
    ).fetchone()
    if row:
        return row[0] if not isinstance(row, dict) else row["contact_id"]
    return None
