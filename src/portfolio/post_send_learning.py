"""
Post-Send Learning (Blueprint Section 25 / Plan Step 3.5).
When an operator edits a draft significantly, extract reusable learning
without polluting the knowledge base with one-off edits.
"""

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

logger = logging.getLogger(__name__)

from config.settings import BASE_DIR
from src.llm.client import llm_call_structured
from src.repositories.activity_log_repo import insert_activity
from src.repositories.learned_rules_repo import insert_learned_rule
from src.repositories.response_pattern_repo import (
    add_avoid_phrase, add_positive_signal, upsert_response_pattern,
)


DIFF_THRESHOLD = 0.30

# Session-scoped learning question tracker: {draft_id: asked_at_datetime}
_learning_asked: dict[str, datetime] = {}
LEARNING_TIMEOUT_SECONDS = 86400  # 24 hours


@dataclass
class LearningQuestion:
    """A question to ask the operator after a significant edit."""
    draft_id: str
    question: str
    question_type: str  # tone | greeting | avoid_phrase | length | general


@dataclass
class LearningResult:
    """Result of processing a learning answer."""
    draft_id: str
    contact_id: str | None
    learned: str  # What was learned
    applied: bool  # Whether it was stored


@dataclass
class LearnedRuleExtraction:
    """Structured result of post-send rule extraction."""
    draft_id: str
    rule_id: str | None
    stored: bool
    diff_score: float
    reason: str | None = None


def calculate_diff_score(original: str, edited: str) -> float:
    """
    Calculate how different the edited version is from the original.
    Simple word-level diff ratio.
    """
    if not original and not edited:
        return 0.0
    if not original or not edited:
        return 1.0

    orig_words = set(original.lower().split())
    edit_words = set(edited.lower().split())

    if not orig_words and not edit_words:
        return 0.0

    union = orig_words | edit_words
    intersection = orig_words & edit_words
    return 1.0 - (len(intersection) / len(union)) if union else 0.0


def should_learn(original_text: str, sent_text: str) -> bool:
    """Check if the edit was significant enough to trigger learning."""
    score = calculate_diff_score(original_text, sent_text)
    if score > DIFF_THRESHOLD:
        return True
    # Soft path: smaller edit but policy keywords present (CM grey-market style)
    blob = f"{original_text}\n{sent_text}".lower()
    policy_keys = (
        "reject", "park", "pre-order", "預放", "port ", "tag ",
        "pct", "grey market", "nnab", "session",
    )
    if score > 0.15 and any(k in blob for k in policy_keys):
        return True
    return False


@lru_cache(maxsize=1)
def _load_learning_prompt() -> str:
    return (BASE_DIR / "prompts" / "learning_agent.md").read_text(encoding="utf-8")


def extract_rule_from_sent_draft(
    conn: sqlite3.Connection,
    draft: dict,
) -> LearnedRuleExtraction:
    """
    Compare the first AI draft with the final sent version and store only
    durable pending-review rules.
    """
    draft_id = str(draft.get("draft_id") or "")
    history = _parse_draft_history(draft.get("draft_history"))
    original_text = str((history[0].get("body") if history else "") or "")
    sent_text = str(draft.get("current_draft_text") or "")
    diff_score = calculate_diff_score(original_text, sent_text)

    if not original_text or not sent_text:
        return LearnedRuleExtraction(
            draft_id=draft_id,
            rule_id=None,
            stored=False,
            diff_score=diff_score,
            reason="missing draft history",
        )
    if not should_learn(original_text, sent_text):
        return LearnedRuleExtraction(
            draft_id=draft_id,
            rule_id=None,
            stored=False,
            diff_score=diff_score,
            reason="diff below threshold",
        )

    prompt = [
        {
            "role": "system",
            "content": (
                (_load_learning_prompt() + "\n\n")
                + "You are reviewing how a human operator edited an AI-generated draft.\n"
                + "Extract only reusable drafting rules, procedures, corrections, or exceptions.\n"
                + "Reject one-off content changes, case-specific facts, and normal wording variation.\n"
                + "\n## Reasoning Protocol (MANDATORY)\n"
                + "You MUST reason in <think>...</think> tags BEFORE producing JSON:\n\n"
                + "<think>\n"
                + "Step 1: DIFF — What exactly changed between the AI draft and the sent version? (added/removed/reworded)\n"
                + "Step 2: WHY — Is this a style preference, factual correction, structural change, or policy enforcement?\n"
                + "Step 3: REUSABLE — Would this same correction apply to future drafts for other projects/clients? "
                + "Or is it specific to this one email's content?\n"
                + "Step 4: RULE — If reusable, formulate a concrete rule with a specific noun (tag, system, field). "
                + "If not reusable → should_store_rule = false\n"
                + "</think>\n\n"
                + "Then output ONLY the JSON result.\n"
                + "If there is no durable rule, return should_store_rule=false."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Project: {draft.get('project_id') or ''}\n"
                f"Draft ID: {draft_id}\n"
                f"Subject: {draft.get('subject') or ''}\n\n"
                f"Previous AI action:\n{_truncate_text(original_text, 3000)}\n\n"
                f"Final sent message:\n{_truncate_text(sent_text, 3000)}"
            ),
        },
    ]
    schema = {
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
        "required": [
            "should_store_rule",
            "trigger_type",
            "condition",
            "rule",
            "scope",
            "applicable_to",
            "confidence",
            "reason",
        ],
    }
    try:
        extracted = llm_call_structured("learning", prompt, schema, temperature=0.1)
    except Exception:
        logger.warning("LLM call failed in extract_rule_from_sent_draft for draft %s", draft_id, exc_info=True)
        return LearnedRuleExtraction(
            draft_id=draft_id,
            rule_id=None,
            stored=False,
            diff_score=diff_score,
            reason="LLM call failed",
        )
    if not extracted.get("should_store_rule") or not extracted.get("rule"):
        return LearnedRuleExtraction(
            draft_id=draft_id,
            rule_id=None,
            stored=False,
            diff_score=diff_score,
            reason=extracted.get("reason") or "no durable rule",
        )

    from src.learning.auto_approve import resolve_rule_status
    from src.repositories.learned_rules_repo import merge_into_existing

    rule_text = str(extracted.get("rule") or "")
    confidence = float(extracted.get("confidence") or 0.0)
    trigger = extracted.get("trigger_type") or "post_send_learning"
    status_or_merge, merge_id = resolve_rule_status(
        conn,
        rule=rule_text,
        confidence=confidence,
        trigger_type=trigger,
        condition=extracted.get("condition"),
    )
    if status_or_merge == "merge" and merge_id:
        merge_into_existing(conn, merge_id, confidence)
        insert_activity(
            conn,
            actor="system",
            action_type="learning_rule_extracted",
            project_id=draft.get("project_id"),
            surface="email",
            object_ref=merge_id,
            summary=f"Post-send learning merged into {merge_id} from {draft_id}",
            metadata_json=json.dumps(
                {"draft_id": draft_id, "merged_into": merge_id, "diff_score": round(diff_score, 4)},
                ensure_ascii=False,
            ),
        )
        conn.commit()
        return LearnedRuleExtraction(
            draft_id=draft_id,
            rule_id=merge_id,
            stored=True,
            diff_score=diff_score,
            reason="merged into existing",
        )

    rule_id = f"lr_{uuid.uuid4().hex[:12]}"
    scope = str(extracted.get("scope") or "project")
    applicable_to = extracted.get("applicable_to") or []
    if not applicable_to:
        applicable_to = [draft["project_id"]] if scope == "project" and draft.get("project_id") else ["*"]

    insert_learned_rule(
        conn,
        {
            "rule_id": rule_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "trigger_type": trigger,
            "source_project_id": draft.get("project_id"),
            "condition": extracted.get("condition"),
            "rule": rule_text,
            "extra_knowledge": json.dumps(
                {
                    "draft_id": draft_id,
                    "subject": draft.get("subject"),
                    "diff_score": round(diff_score, 4),
                    "extraction_reason": extracted.get("reason"),
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
        project_id=draft.get("project_id"),
        surface="email",
        object_ref=rule_id,
        summary=f"Post-send learning extracted from {draft_id}",
        metadata_json=json.dumps(
            {
                "draft_id": draft_id,
                "rule": rule_text,
                "scope": scope,
                "confidence": confidence,
                "status": status_or_merge,
                "diff_score": round(diff_score, 4),
            },
            ensure_ascii=False,
        ),
    )
    conn.commit()
    return LearnedRuleExtraction(
        draft_id=draft_id,
        rule_id=rule_id,
        stored=True,
        diff_score=diff_score,
        reason=extracted.get("reason"),
    )


def generate_learning_question(
    conn: sqlite3.Connection, original_text: str, sent_text: str, draft_id: str
) -> LearningQuestion | None:
    """Generate a learning question based on the diff.

    ONE question per draft, no re-ask, 24-hour timeout.
    """
    if not should_learn(original_text, sent_text):
        return None

    # ── Dedup / timeout check ─────────────────────────────────────
    now = datetime.now(timezone.utc)
    if draft_id in _learning_asked:
        asked_at = _learning_asked[draft_id]
        if (now - asked_at).total_seconds() > LEARNING_TIMEOUT_SECONDS:
            return None  # 24h elapsed — no re-ask
        return None  # Already asked for this draft

    # ── Analyze what changed ──────────────────────────────────────
    orig_lower = original_text.lower()
    sent_lower = sent_text.lower()

    # Check if greeting changed
    orig_first = orig_lower.split("\n")[0] if orig_lower else ""
    sent_first = sent_lower.split("\n")[0] if sent_lower else ""
    if orig_first != sent_first and any(g in sent_first for g in ["hi", "dear", "hello", "hey"]):
        question = LearningQuestion(
            draft_id=draft_id,
            question="You changed the greeting. Should I always use this greeting for this contact?",
            question_type="greeting",
        )
    elif (len_ratio := len(sent_text) / max(len(original_text), 1)) < 0.5:
        question = LearningQuestion(
            draft_id=draft_id,
            question="You significantly shortened the draft. Should I keep responses shorter for this contact?",
            question_type="length",
        )
    elif len_ratio > 2.0:
        question = LearningQuestion(
            draft_id=draft_id,
            question="You significantly expanded the draft. Should I write more detailed responses for this contact?",
            question_type="length",
        )
    else:
        question = LearningQuestion(
            draft_id=draft_id,
            question="You made significant edits to the draft. What should I change about my drafting style for this contact?",
            question_type="general",
        )

    # ── Record that we asked ──────────────────────────────────────
    _learning_asked[draft_id] = now

    return question


def apply_learning(
    conn: sqlite3.Connection,
    contact_id: str | None,
    project_id: str | None,
    question_type: str,
    answer: str,
    draft_id: str,
) -> LearningResult:
    """
    Apply the operator's learning answer to response_pattern_memory.
    """
    if not contact_id:
        return LearningResult(draft_id=draft_id, contact_id=None, learned=answer, applied=False)

    if question_type == "greeting":
        upsert_response_pattern(
            conn, contact_id, project_id,
            preferred_greeting=answer,
            source="post_send_learning",
        )
    elif question_type == "length":
        length_pref = "brief" if "short" in answer.lower() else "detailed"
        upsert_response_pattern(
            conn, contact_id, project_id,
            response_length_preference=length_pref,
            source="post_send_learning",
        )
    elif question_type == "avoid_phrase":
        add_avoid_phrase(conn, contact_id, answer, project_id)
    else:
        # General feedback — store as a positive signal
        add_positive_signal(conn, contact_id, answer, project_id)

    return LearningResult(
        draft_id=draft_id,
        contact_id=contact_id,
        learned=f"{question_type}: {answer}",
        applied=True,
    )


def _parse_draft_history(raw_value: str | None) -> list[dict]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    keep_head = int(limit * 0.7)
    keep_tail = limit - keep_head
    return f"{value[:keep_head]}\n\n[... truncated ...]\n\n{value[-keep_tail:]}"
