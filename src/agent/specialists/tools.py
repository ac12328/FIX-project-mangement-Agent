"""Specialist tool bundles — derived from the skill registry."""

from __future__ import annotations

from src.agent.skills.registry import (
    get_skill,
    get_specialist_tool_names,
    get_tools_for_skill,
)
from src.agent.specialists.contracts import SpecialistType
from src.tools.registry import ALL_TOOLS, _to_openai_schema, get_tools_for_intent

# Never available inside a specialist loop
_EXCLUDED_TOOLS = frozenset({"delegate"})


def get_tools_for_specialist(
    specialist_type: SpecialistType,
    *,
    skill_id: str | None = None,
) -> list[dict]:
    """Return OpenAI tool schemas for a specialist, optionally narrowed by skill."""
    if skill_id:
        skill = get_skill(skill_id)
        if skill is not None:
            return get_tools_for_skill(skill.skill_id)

    if specialist_type == SpecialistType.EMAIL:
        # Keep email lean intent bundle as baseline
        return get_tools_for_intent("email")

    names = get_specialist_tool_names(specialist_type) - _EXCLUDED_TOOLS
    contracts = [
        t for t in ALL_TOOLS
        if t.llm_visible and t.name in names
    ]
    contracts.sort(key=lambda t: t.name)
    return _to_openai_schema(contracts)
