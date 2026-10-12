"""Business tasks vs LLM route keys — two layers of task taxonomy.

``TaskType`` (in enums.py) is the business/audit taxonomy stored on drafts
and proposed updates. ``LlmRoute`` is keys into ``TASK_MODEL_MAP`` only.

Do not put pure routing keys (e.g. wiki_curator) into ``TaskType``.
"""
from __future__ import annotations

from enum import Enum

from src.models.enums import TaskType


class LlmRoute(str, Enum):
    """Keys into TASK_MODEL_MAP only — not stored as business task_type."""

    TRIAGE = "triage"
    STATUS = "status"
    FOLLOW_UP = "follow_up"
    ANALYSIS = "analysis"
    LEARNING = "learning"
    MORNING_BRIEF = "morning_brief"
    OPERATIONAL = "operational"
    DRAFT = "draft"
    EMAIL_DRAFT = "email_draft"
    REASONING = "reasoning"
    LEAD_REASONING = "lead_reasoning"
    TRUTH_WRITE_REASONING = "truth_write_reasoning"
    FLUSH = "flush"
    IMAGE_DESCRIPTION = "image_description"
    INBOUND_REPLY_ANALYSIS = "inbound_reply_analysis"
    FEEDBACK_RULE_EXTRACTION = "feedback_rule_extraction"
    KNOWLEDGE_MINING = "knowledge_mining"
    INSIGHT_MINING = "insight_mining"
    WIKI_CURATOR = "wiki_curator"


# String keys match SpecialistType values (avoid importing contracts → circular risk).
SPECIALIST_TO_BUSINESS_TASK: dict[str, TaskType] = {
    "status": TaskType.ANSWER_STATUS,
    "research": TaskType.RESEARCH,
    "write": TaskType.TRUTH_WRITE_REASONING,
    "draft": TaskType.REPLY_TO_CLIENT,
    "portfolio": TaskType.PORTFOLIO_SCAN,
    "support": TaskType.SUPPORT_REPLY,
    "email": TaskType.REPLY_TO_CLIENT,
}

BUSINESS_TASK_TO_LLM_ROUTE: dict[TaskType, LlmRoute] = {
    TaskType.TRUTH_WRITE_REASONING: LlmRoute.TRUTH_WRITE_REASONING,
    TaskType.REPLY_TO_CLIENT: LlmRoute.DRAFT,
    TaskType.DRAFT_FOLLOW_UP: LlmRoute.DRAFT,
    TaskType.ANSWER_STATUS: LlmRoute.STATUS,
    TaskType.INTAKE_TRIAGE: LlmRoute.TRIAGE,
    TaskType.INTERNAL_FOLLOW_UP: LlmRoute.OPERATIONAL,
    TaskType.MORNING_BRIEF: LlmRoute.MORNING_BRIEF,
    TaskType.AGGREGATE_QUERY: LlmRoute.REASONING,
    TaskType.SUPPORT_REPLY: LlmRoute.DRAFT,
    TaskType.RESEARCH: LlmRoute.ANALYSIS,
    TaskType.PORTFOLIO_SCAN: LlmRoute.REASONING,
}


def specialist_to_business_task(specialist: str | object) -> TaskType:
    """Map a SpecialistType (or its value string) to a business TaskType."""
    key = specialist.value if hasattr(specialist, "value") else specialist
    return SPECIALIST_TO_BUSINESS_TASK[str(key)]


def resolve_llm_route(task: str | TaskType | LlmRoute) -> str:
    """Normalize any task-like input to a TASK_MODEL_MAP key string."""
    if isinstance(task, LlmRoute):
        return task.value
    if isinstance(task, TaskType):
        route = BUSINESS_TASK_TO_LLM_ROUTE.get(task, LlmRoute.REASONING)
        return route.value
    # string: if already a known LlmRoute value, use it
    try:
        return LlmRoute(task).value
    except ValueError:
        pass
    try:
        bt = TaskType(task)
        return BUSINESS_TASK_TO_LLM_ROUTE.get(bt, LlmRoute.REASONING).value
    except ValueError:
        return task  # pass-through unknown for backward compat (get_model falls back)
