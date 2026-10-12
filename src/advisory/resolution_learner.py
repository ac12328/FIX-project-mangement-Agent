"""
Resolution pattern learner — extracts reusable patterns from operator handling descriptions.
"""

import logging
import re
import sqlite3
import uuid

from src.models.resolution_pattern import ResolutionPattern
from src.repositories.resolution_pattern_repo import insert_pattern
from src.repositories.ticket_repo import update_ticket_fields
from src.utils.enum_helpers import enum_val

from src.telegram.i18n import t

logger = logging.getLogger(__name__)

_MIN_RESOLUTION_TEXT_LEN = 10
_MIN_PATTERN_CONFIDENCE = 0.15
_MIN_RESOLUTION_ACTION_LEN = 10

_SKIP_PHRASES = frozenset({
    "skip", "later", "no", "cancel", "/cancel", "stop",
    "略過", "跳過", "稍後", "唔記錄", "不用", "取消",
})

_CONFUSION_RE = re.compile(
    r"^(what|huh|eh|啊|咩|什麼|啥)[\s'?!.]*$",
    re.IGNORECASE,
)


def is_resolution_skip(text: str) -> bool:
    """True when the operator declines to record a resolution pattern."""
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if normalized in _SKIP_PHRASES:
        return True
    return normalized.startswith(("skip ", "略過", "跳過"))


def is_resolution_bypass(text: str) -> bool:
    """True when free text should not be treated as a resolution follow-up reply."""
    from src.agent.intent import is_casual_message

    normalized = (text or "").strip()
    if not normalized:
        return True
    if is_casual_message(normalized):
        return True
    if _CONFUSION_RE.match(normalized):
        return True
    return False


def _is_valid_extracted_pattern(result: dict) -> bool:
    action = (result.get("resolution_action") or "").strip()
    if len(action) < _MIN_RESOLUTION_ACTION_LEN:
        return False
    try:
        confidence = float(result.get("confidence") or 0)
    except (TypeError, ValueError):
        return False
    return confidence >= _MIN_PATTERN_CONFIDENCE


def default_resolution_llm_fn(task, messages, schema, temperature=0.1):
    """LLM callable for resolution pattern extraction (learning task route)."""
    from src.llm.client import llm_call_structured
    return llm_call_structured(task, messages, schema, temperature=temperature)

_EXTRACTION_SCHEMA = {
    "category": "string",
    "subcategory": "string or null",
    "trigger_keywords": ["string"],
    "diagnosis_steps": ["string"],
    "resolution_action": "string",
    "escalation_target": "string or null",
    "confidence": 0.5,
}


def extract_resolution_pattern(
    operator_text: str,
    ticket_context: dict | None = None,
    llm_fn=None,
) -> dict | None:
    """Extract a resolution pattern from operator's description using LLM.

    Args:
        operator_text: The operator's free-text description of how they resolved the issue.
        ticket_context: Optional dict with ticket info (category, severity, title, description).
        llm_fn: Optional LLM callable. If None, uses deterministic fallback.

    Returns:
        Extracted pattern dict, or None if extraction failed.
    """
    if llm_fn:
        return _extract_with_llm(operator_text, ticket_context, llm_fn)
    return _extract_deterministic(operator_text, ticket_context)


def _extract_with_llm(
    operator_text: str,
    ticket_context: dict | None,
    llm_fn,
) -> dict | None:
    """Use LLM to extract structured resolution pattern."""
    try:
        from src.utils.prompt_loader import load_prompt
        prompt_template = load_prompt("advisory/resolution_extractor.md")

        ctx_lines = ""
        if ticket_context:
            ctx_lines = (
                f"Category: {ticket_context.get('category', 'unknown')}\n"
                f"Severity: {ticket_context.get('severity', 'unknown')}\n"
                f"Title: {ticket_context.get('title', '')[:200]}\n"
                f"Description: {ticket_context.get('description', '')[:500]}\n"
            )

        prompt = (
            f"{prompt_template}\n\n"
            f"## Ticket Context\n{ctx_lines}\n"
            f"## Operator Resolution\n{operator_text[:2000]}"
        )
        result = llm_fn(
            "learning",
            [{"role": "user", "content": prompt}],
            _EXTRACTION_SCHEMA,
            temperature=0.1,
        )
        if isinstance(result, dict) and _is_valid_extracted_pattern(result):
            return result
        return None
    except Exception:
        logger.debug("LLM resolution extraction failed", exc_info=True)
        return None


def _extract_deterministic(
    operator_text: str,
    ticket_context: dict | None,
) -> dict | None:
    """Fallback: extract basic pattern from keywords in operator text."""
    if not operator_text or len(operator_text.strip()) < _MIN_RESOLUTION_TEXT_LEN:
        return None
    words = [w.lower() for w in operator_text.split() if len(w) > 3]
    if not words:
        return None
    category = (ticket_context or {}).get("category", "other")
    return {
        "category": category,
        "subcategory": None,
        "trigger_keywords": words[:5],
        "diagnosis_steps": [],
        "resolution_action": operator_text.strip()[:500],
        "escalation_target": None,
        "confidence": 0.4,
    }


def store_resolution(
    conn: sqlite3.Connection,
    pattern_data: dict,
    ticket_id: str | None = None,
) -> ResolutionPattern:
    """Store an extracted resolution pattern and optionally link to a ticket.

    Returns the created ResolutionPattern.
    """
    pattern_id = f"rp_{uuid.uuid4().hex[:10]}"
    pattern = insert_pattern(
        conn,
        pattern_id,
        category=pattern_data.get("category"),
        subcategory=pattern_data.get("subcategory"),
        trigger_keywords=pattern_data.get("trigger_keywords"),
        diagnosis_steps=pattern_data.get("diagnosis_steps"),
        resolution_action=pattern_data.get("resolution_action"),
        escalation_target=pattern_data.get("escalation_target"),
        source="operator",
        confidence=pattern_data.get("confidence", 0.5),
    )

    if ticket_id:
        update_ticket_fields(conn, ticket_id, resolution_pattern_id=pattern_id)

    return pattern


def handle_resolution_followup_reply(
    conn: sqlite3.Connection,
    session_data: dict,
    operator_text: str,
    llm_fn=None,
) -> dict:
    """Process the operator's reply to 'How did you handle this?'

    Args:
        conn: Database connection.
        session_data: The session's intermediate_data dict.
        operator_text: The operator's free-text reply.
        llm_fn: Optional LLM callable.

    Returns:
        Response dict with 'text' key.
    """
    ticket_id = session_data.get("ticket_id")
    ticket_context = None
    if ticket_id:
        try:
            from src.repositories.ticket_repo import get_ticket
            ticket = get_ticket(conn, ticket_id)
            if ticket:
                ticket_context = {
                    "category": enum_val(ticket.category),
                    "severity": enum_val(ticket.severity),
                    "title": ticket.title,
                    "description": ticket.description or "",
                }
        except Exception:
            logger.debug("Could not load ticket context for %s", ticket_id, exc_info=True)

    if is_resolution_skip(operator_text):
        return {"text": t("advisory.resolution_skipped")}

    if is_resolution_bypass(operator_text):
        return {"bypass": True}

    if len((operator_text or "").strip()) < _MIN_RESOLUTION_TEXT_LEN:
        return {"text": t("advisory.resolution_no_pattern")}

    extract_fn = llm_fn or default_resolution_llm_fn
    pattern_data = extract_resolution_pattern(operator_text, ticket_context, extract_fn)
    if not pattern_data:
        pattern_data = extract_resolution_pattern(operator_text, ticket_context)
    if not pattern_data:
        return {"text": t("advisory.resolution_no_pattern")}

    pattern = store_resolution(conn, pattern_data, ticket_id)

    # Update ticket status — store_resolution already committed,
    # so this update needs its own commit
    if ticket_id:
        try:
            update_ticket_fields(conn, ticket_id, status="resolved", resolution=operator_text[:2000])
            conn.commit()
        except Exception:
            logger.debug("Could not update ticket status for %s", ticket_id, exc_info=True)

    return {
        "text": t("support.resolution_stored").format(
            pattern_id=pattern.pattern_id,
            category=pattern.category or "unknown",
            confidence=f"{pattern.confidence:.0%}",
        ),
    }
