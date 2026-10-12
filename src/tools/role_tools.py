"""Role-based tool bundles for unified loop v2.

Host orchestrator tools come from the skill registry (read + delegate only).
Write primitives are specialist-only.
"""

from __future__ import annotations

from enum import Enum

from src.tools.registry import ALL_TOOLS, _to_openai_schema


class AgentRole(str, Enum):
    HOST = "host"
    RESEARCH = "research"
    WRITE = "write"
    DRAFT = "draft"
    PORTFOLIO = "portfolio"
    SUPPORT = "support"
    EMAIL = "email"
    GENERAL = "general"
    QUERY = "query"


def host_tool_names() -> frozenset[str]:
    """Host may only orchestrate — no write primitives."""
    from src.agent.skills.registry import get_host_tool_names

    return get_host_tool_names()


def get_tools_for_role(role: AgentRole | str) -> list[dict]:
    """Return OpenAI tool schemas for a loop role."""
    if isinstance(role, str):
        try:
            role = AgentRole(role)
        except ValueError:
            role = AgentRole.GENERAL

    if role == AgentRole.HOST:
        names = host_tool_names()
        filtered = [t for t in ALL_TOOLS if t.llm_visible and t.name in names]
        return _to_openai_schema(filtered)

    # Map role → specialist skill tools via registry
    from src.agent.specialists.contracts import SpecialistType
    from src.agent.specialists.tools import get_tools_for_specialist

    role_to_specialist = {
        AgentRole.RESEARCH: SpecialistType.RESEARCH,
        AgentRole.WRITE: SpecialistType.WRITE,
        AgentRole.DRAFT: SpecialistType.DRAFT,
        AgentRole.PORTFOLIO: SpecialistType.PORTFOLIO,
        AgentRole.SUPPORT: SpecialistType.SUPPORT,
        AgentRole.EMAIL: SpecialistType.EMAIL,
    }
    if role in role_to_specialist:
        return get_tools_for_specialist(role_to_specialist[role])

    if role == AgentRole.QUERY:
        from src.tools.registry import get_tools_for_intent
        return get_tools_for_intent("query")

    # GENERAL — still no free-form write surface when orchestrator is the product default;
    # fall back to host tools so writes go through delegate.
    names = host_tool_names()
    filtered = [t for t in ALL_TOOLS if t.llm_visible and t.name in names]
    return _to_openai_schema(filtered)
