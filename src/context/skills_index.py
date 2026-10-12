"""
Skills index — builds a compact XML block listing all available agent skills.
Injected into the system prompt so the model knows which domain skills to apply.

Inspired by Hermes' skills index pattern (agent/prompt_builder.py:544-572):
the model is told what skills exist and prompted to use them before acting.
"""

from functools import lru_cache

_SKILLS = [
    (
        "fix_onboarding",
        "FIX protocol onboarding domain knowledge: typical blockers per milestone (M1–M8), "
        "expected ball_with, key actions, and realistic duration. Use when diagnosing a stalled "
        "milestone or recommending next steps.",
    ),
    (
        "aging_detection",
        "Canonical aging thresholds for projects and SLAs: EXTERNAL_AGING_YELLOW_DAYS=3, "
        "EXTERNAL_AGING_RED_DAYS=7, SLA_ADVISORY_DAYS=3, SLA_URGENT_DAYS=1. Use when assessing "
        "how overdue or at-risk a project is.",
    ),
    (
        "status_assessment",
        "Deterministic project health check: evaluates status, ball_with, milestone presence, "
        "deadline proximity, open follow-ups, and next_action. Use when giving a full project "
        "health summary.",
    ),
    (
        "communication",
        "Draft quality guard: checks for internal jargon (M1–M8, ProposedUpdate, Write Layer) "
        "and operator avoid-phrases before sending. Use when reviewing or suggesting email tone.",
    ),
]


@lru_cache(maxsize=1)
def build_skills_block() -> str:
    """Return a compact XML block describing all available agent skills.

    Format inspired by the Hermes agent (agent/prompt_builder.py) skills index.
    The model is instructed in the system prompt to consider these before acting.
    """
    lines = ["<available-skills>"]
    for name, description in _SKILLS:
        lines.append(f"  {name}: {description}")
    lines.append("</available-skills>")
    return "\n".join(lines)
