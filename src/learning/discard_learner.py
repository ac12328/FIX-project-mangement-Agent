"""Discard-vs-sent learning: extract durable rules when a later sent draft corrects a discard."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from src.learning.auto_approve import resolve_rule_status
from src.portfolio.post_send_learning import (
    LearnedRuleExtraction,
    calculate_diff_score,
    should_learn,
)
from src.repositories.activity_log_repo import insert_activity
from src.repositories.learned_rules_repo import insert_learned_rule, merge_into_existing

logger = logging.getLogger(__name__)

DISCARD_LEARN_WINDOW_HOURS = 72


def find_recent_discards(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    before_iso: str | None = None,
    within_hours: int = DISCARD_LEARN_WINDOW_HOURS,
) -> list[dict]:
    """Return discarded drafts for project in the lookback window ending at before_iso."""
    if not project_id:
        return []
    end = before_iso or datetime.now(timezone.utc).isoformat()
    try:
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=within_hours)
    start = start_dt.isoformat()
    rows = conn.execute(
        """SELECT draft_id, project_id, subject, current_draft_text, status, created_at, sent_at
           FROM drafts
           WHERE project_id = ? AND status = 'discarded'
             AND created_at >= ? AND created_at <= ?
           ORDER BY created_at DESC LIMIT 10""",
        (project_id, start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def maybe_learn_against_recent_discards(
    conn: sqlite3.Connection,
    sent_draft: dict,
) -> list[LearnedRuleExtraction]:
    """On successful send, compare against recent discarded drafts on same project.

    Best-effort; never raises to caller.
    """
    results: list[LearnedRuleExtraction] = []
    try:
        project_id = str(sent_draft.get("project_id") or "")
        if not project_id:
            return results
        sent_at = sent_draft.get("sent_at") or sent_draft.get("created_at")
        discards = find_recent_discards(conn, project_id, before_iso=sent_at)
        for discarded in discards:
            if discarded.get("draft_id") == sent_draft.get("draft_id"):
                continue
            extraction = maybe_learn_from_discard_pair(conn, discarded, sent_draft)
            results.append(extraction)
            if extraction.stored:
                break  # one durable rule per send is enough
    except Exception:
        logger.exception("discard learning failed for draft %s", sent_draft.get("draft_id"))
    return results


def maybe_learn_from_discard_pair(
    conn: sqlite3.Connection,
    discarded: dict,
    sent: dict,
    *,
    llm_fn=None,
) -> LearnedRuleExtraction:
    """Extract a durable rule from discarded AI body vs final sent body."""
    discarded_id = str(discarded.get("draft_id") or "")
    sent_id = str(sent.get("draft_id") or "")
    original_text = str(discarded.get("current_draft_text") or "")
    sent_text = str(sent.get("current_draft_text") or "")
    diff_score = calculate_diff_score(original_text, sent_text)

    if not original_text or not sent_text:
        return LearnedRuleExtraction(
            draft_id=sent_id,
            rule_id=None,
            stored=False,
            diff_score=diff_score,
            reason="missing discard or sent body",
        )
    if not should_learn(original_text, sent_text):
        return LearnedRuleExtraction(
            draft_id=sent_id,
            rule_id=None,
            stored=False,
            diff_score=diff_score,
            reason="diff below threshold",
        )

    if llm_fn is None:
        from src.llm.client import llm_call_structured
        llm_fn = llm_call_structured

    prompt = [
        {
            "role": "system",
            "content": (
                "You are reviewing how a human operator discarded an AI draft and later sent a different version.\n"
                "Extract only reusable drafting rules, procedures, corrections, or exceptions.\n"
                "Reject one-off content changes and case-specific facts.\n"
                "Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Project: {sent.get('project_id') or ''}\n"
                f"Discarded draft: {discarded_id}\n"
                f"Sent draft: {sent_id}\n"
                f"Subject: {sent.get('subject') or discarded.get('subject') or ''}\n\n"
                f"Discarded AI draft:\n{original_text[:3000]}\n\n"
                f"Final sent message:\n{sent_text[:3000]}"
            ),
        },
    ]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "should_store_rule": {"type": "boolean"},
            "trigger_type": {"type": "string"},
            "condition": {"type": ["string", "null"]},
            "rule": {"type": ["string", "null"]},
            "scope": {"type": "string"},
            "applicable_to": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "reason": {"type": ["string", "null"]},
        },
        "required": ["should_store_rule", "rule", "confidence"],
    }
    try:
        extracted = llm_fn("learning", prompt, schema, temperature=0.1)
    except Exception:
        logger.warning("LLM failed in discard learner for %s", sent_id, exc_info=True)
        return LearnedRuleExtraction(
            draft_id=sent_id,
            rule_id=None,
            stored=False,
            diff_score=diff_score,
            reason="LLM call failed",
        )

    if not extracted.get("should_store_rule") or not extracted.get("rule"):
        return LearnedRuleExtraction(
            draft_id=sent_id,
            rule_id=None,
            stored=False,
            diff_score=diff_score,
            reason=extracted.get("reason") or "no durable rule",
        )

    rule_text = str(extracted.get("rule") or "")
    confidence = float(extracted.get("confidence") or 0.0)
    status_or_merge, merge_id = resolve_rule_status(
        conn,
        rule=rule_text,
        confidence=confidence,
        trigger_type="discard_learning",
        condition=extracted.get("condition"),
    )
    if status_or_merge == "merge" and merge_id:
        merge_into_existing(conn, merge_id, confidence)
        conn.commit()
        return LearnedRuleExtraction(
            draft_id=sent_id,
            rule_id=merge_id,
            stored=True,
            diff_score=diff_score,
            reason="merged into existing",
        )

    rule_id = f"lr_{uuid.uuid4().hex[:12]}"
    scope = str(extracted.get("scope") or "global")
    applicable_to = extracted.get("applicable_to") or ["*"]
    insert_learned_rule(
        conn,
        {
            "rule_id": rule_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "trigger_type": "discard_learning",
            "source_project_id": sent.get("project_id"),
            "condition": extracted.get("condition"),
            "rule": rule_text,
            "extra_knowledge": json.dumps(
                {
                    "discarded_draft_id": discarded_id,
                    "sent_draft_id": sent_id,
                    "diff_score": round(diff_score, 4),
                },
                ensure_ascii=False,
            ),
            "scope": scope,
            "applicable_to": json.dumps(applicable_to, ensure_ascii=False),
            "status": status_or_merge,
            "confidence": confidence,
        },
    )
    insert_activity(
        conn,
        actor="system",
        action_type="learning_rule_extracted",
        project_id=sent.get("project_id"),
        surface="email",
        object_ref=rule_id,
        summary=f"Discard learning extracted from {discarded_id} → {sent_id}",
        metadata_json=json.dumps(
            {
                "discarded_draft_id": discarded_id,
                "sent_draft_id": sent_id,
                "status": status_or_merge,
                "confidence": confidence,
            },
            ensure_ascii=False,
        ),
    )
    conn.commit()
    return LearnedRuleExtraction(
        draft_id=sent_id,
        rule_id=rule_id,
        stored=True,
        diff_score=diff_score,
        reason=extracted.get("reason"),
    )
