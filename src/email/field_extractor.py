"""
L2 field extractor — parses a free-text suggested_action into a structured
{field, new_value} dict for direct project state writes.

Called by reply_analyser.py when email analysis confidence >= 0.90.
"""

import logging
import sqlite3

from src.llm.client import llm_call_structured

logger = logging.getLogger(__name__)

# Only these fields may be auto-written from email pipeline.
# Structural fields (client_email, broker_code, etc.) require human confirmation.
_AUTO_WRITABLE_FIELDS = frozenset(
    {"current_milestone", "ball_with", "project_status", "next_action"}
)

_FIELD_EXTRACTOR_PROMPT = """\
Given this suggested action for a FIX onboarding project:
"{suggested_action}"

If this implies a direct project field update, return a JSON object:
{"field": "<field_name>", "new_value": "<value>"}

Valid fields and their allowed values:
- current_milestone: M1_NetworkSetup | M2_UATSessionConfiguration | M3_UATEnvironmentAccess |
                     M4_ApplicationTesting | M5_ConformanceTestCases |
                     M6_TestReportAndProductionPreparation | M7_LogReviewAndProductionSessionDelivery |
                     M8_GoLive
- ball_with: Internal | External
- project_status: Active | Waiting | Closed
- next_action: (any short text string)

If the action is advisory only (e.g. "draft an email", "ask the client",
"review inbound reply"), return null.
"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {"type": "string"},
        "new_value": {"type": "string"},
    },
    "required": ["field", "new_value"],
}


def extract_field_updates(
    suggested_action: str,
    project_id: str,
    conn: sqlite3.Connection,
) -> dict | None:
    """
    Parse a free-text suggested_action into a structured {field, new_value} dict.

    Returns None if:
    - The action is advisory (draft email, ask client, etc.)
    - The LLM returns null
    - The returned field is not in the auto-writable whitelist
    - The LLM call fails
    """
    try:
        prompt = _FIELD_EXTRACTOR_PROMPT.replace("{suggested_action}", suggested_action)
        result = llm_call_structured(
            "field_extractor",
            [{"role": "user", "content": prompt}],
            _SCHEMA,
        )
        if not result or not isinstance(result, dict):
            return None
        field = result.get("field")
        new_value = result.get("new_value")
        if not field or not new_value:
            return None
        if field not in _AUTO_WRITABLE_FIELDS:
            logger.debug(
                "field_extractor: field %r not in auto-writable whitelist — skipping",
                field,
            )
            return None
        return {"field": field, "new_value": new_value}
    except Exception:
        logger.exception(
            "field_extractor: LLM call failed for suggested_action=%r project=%s",
            suggested_action[:80],
            project_id,
        )
        return None
