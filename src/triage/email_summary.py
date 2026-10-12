"""
LLM-based inbound email thread summarization.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from config.settings import BASE_DIR
from src.llm.client import llm_call_structured

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_analysis_prompt() -> str:
    path = BASE_DIR / "prompts" / "analysis" / "milestone_classification.md"
    return path.read_text(encoding="utf-8")


def summarize_email_thread(
    *,
    project_id: str,
    sender_email: str,
    subject: str,
    body: str,
) -> dict:
    """
    Summarize a noisy inbound email thread into structured PM-ready evidence.
    """
    prompt = [
        {
            "role": "system",
            "content": (
                (_load_analysis_prompt() + "\n\n")
                + "You are analyzing an inbound FIX onboarding email thread for PM use.\n"
                + "Extract the newest actionable content from the thread.\n"
                + "Ignore repeated quoted headers, old reply history, signature boilerplate, "
                + "cid:image placeholders, and separator noise unless they contain a real fact.\n"
                + "Do NOT propose project truth updates.\n"
                + "Do NOT infer milestone regression from a blocker unless the message explicitly says to revert.\n"
                + "Return concise structured output only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Project: {project_id}\n"
                f"From: {sender_email}\n"
                f"Subject: {subject}\n\n"
                "Summarize the newest message in this thread for downstream truth-write reasoning.\n\n"
                f"Raw thread:\n{body[:20000]}"
            ),
        },
    ]
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "newest_message": {"type": "string"},
            "key_facts": {"type": "array", "items": {"type": "string"}},
            "milestone_signals": {"type": "array", "items": {"type": "string"}},
            "action_requested": {"type": ["string", "null"]},
            "blocking_side": {
                "type": "string",
                "enum": ["Internal", "External", "Shared", "Unknown"],
            },
            "confidence": {"type": "number"},
        },
        "required": [
            "summary",
            "newest_message",
            "key_facts",
            "milestone_signals",
            "action_requested",
            "blocking_side",
            "confidence",
        ],
    }
    try:
        return llm_call_structured("analysis", prompt, schema, temperature=0.1)
    except Exception:
        logger.warning(
            "LLM call failed in summarize_email_thread for project %s", project_id, exc_info=True,
        )
        return {
            "summary": f"Email from {sender_email} re: {subject}",
            "newest_message": body[:500] if body else "",
            "key_facts": [],
            "milestone_signals": [],
            "action_requested": None,
            "blocking_side": "Unknown",
            "confidence": 0.0,
        }


def render_email_summary(summary: dict) -> str:
    """
    Render the structured email summary into compact evidence text for truth-write.
    """
    lines = [
        "Email Summary:",
        f"Summary: {summary.get('summary', '')}",
        f"Newest message: {summary.get('newest_message', '')}",
    ]

    key_facts = summary.get("key_facts") or []
    if key_facts:
        lines.append("Key facts:")
        lines.extend(f"- {fact}" for fact in key_facts if fact)

    milestone_signals = summary.get("milestone_signals") or []
    if milestone_signals:
        lines.append("Milestone signals:")
        lines.extend(f"- {signal}" for signal in milestone_signals if signal)

    action_requested = summary.get("action_requested")
    if action_requested:
        lines.append(f"Action requested: {action_requested}")

    lines.append(f"Blocking side: {summary.get('blocking_side', 'Unknown')}")
    lines.append(f"Summary confidence: {summary.get('confidence', 0.0)}")
    return "\n".join(lines)
