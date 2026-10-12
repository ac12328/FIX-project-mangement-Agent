"""
Shared helper functions for bot.py and mixin handler files.
Extracted to avoid circular imports between bot.py and mixin files.
"""

from __future__ import annotations

from src.telegram.cards import InteractionCard, build_card
from src.utils.prompt_loader import load_prompt


def _query_session_payload(evaluation) -> dict:
    return {
        "query_text": evaluation.profile.query_text,
        "filters": evaluation.suggested_filters,
        "search_text": evaluation.search_text,
        "limit": evaluation.profile.limit,
        "clarification_question": evaluation.clarification_question,
    }


def _build_query_clarification_card(evaluation) -> InteractionCard:
    body_lines = [
        evaluation.clarification_question or "Please confirm how I should run this aggregate query.",
        "",
        f"🔎 Query: {evaluation.profile.query_text}",
    ]
    if evaluation.suggested_filters:
        rendered_filters = ", ".join(
            f"{key}={value}" for key, value in sorted(evaluation.suggested_filters.items())
        )
        body_lines.append(f"📋 Suggested filters: {rendered_filters}")
    if evaluation.profile.escalation_reason:
        body_lines.append(f"⚠️ {evaluation.profile.escalation_reason}")
    return build_card(
        "K",  # Profile K: Query Clarification Card
        "Query Clarification",
        "\n".join(body_lines),
        extra_buttons=[["CONFIRM RUN", "MODIFY FILTERS"], ["CANCEL"]],
    )


def _llm_parse_project_query(free_text: str) -> dict:
    import logging
    from src.llm.client import llm_call_structured

    prompt = [
        {
            "role": "system",
            "content": load_prompt("routing/query_parser.md"),
        },
        {"role": "user", "content": free_text},
    ]
    schema = {
        "type": "object",
        "properties": {
            "ball_with": {"type": "string"},
            "project_status": {"type": "string"},
            "current_milestone": {"type": "string"},
            "market": {"type": "string"},
            "client_name": {"type": "string"},
            "search_text": {"type": "string"},
        },
    }
    try:
        return llm_call_structured("status", prompt, schema, temperature=0.0)
    except Exception:
        logging.getLogger(__name__).exception("LLM query parse failed — returning empty filters")
        return {}
