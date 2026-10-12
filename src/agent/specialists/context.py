from __future__ import annotations

import sqlite3

from config.settings import BASE_DIR
from src.agent.specialists.contracts import SpecialistType
from src.context.engine import ContextEngine
from src.models.context_frame import ContextFrame


def build_specialist_context(
    conn: sqlite3.Connection,
    specialist_type: SpecialistType,
    task: str,
    project_id: str | None = None,
    *,
    skill_id: str | None = None,
    operator_id: str | None = None,
):
    """Build system context for a specialist, preferring skill prompts."""
    skill_system = _load_skill_prompt(skill_id)
    if skill_system:
        system = skill_system
        if project_id:
            system = f"{system.strip()}\n\nProject scope: {project_id}"
        system = f"{system.strip()}\n\nTask: {task}"
        if specialist_type == SpecialistType.SUPPORT:
            return _with_support_frame(conn, system, task, project_id=project_id, operator_id=operator_id)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        return ContextFrame(identity=system), messages

    if specialist_type == SpecialistType.SUPPORT:
        return _build_support_context(conn, task, project_id=project_id, operator_id=operator_id)

    if specialist_type == SpecialistType.RESEARCH:
        return _build_research_context(task, project_id=project_id)

    system = (
        f"You are a {specialist_type.value} specialist. "
        "Use only the tools you are given. Return a clear summary for the host.\n"
        f"Task: {task}"
    )
    if project_id:
        system = f"{system}\nProject scope: {project_id}"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    return ContextFrame(identity=system), messages


def load_skill_system_prompt(
    skill_id: str | None,
    specialist_type: SpecialistType | None = None,
) -> str:
    """Raw skill/specialist system prompt (no task appended) for resume re-render."""
    skill_system = _load_skill_prompt(skill_id)
    if skill_system:
        return skill_system.strip()
    if specialist_type == SpecialistType.RESEARCH:
        path = BASE_DIR / "prompts" / "specialists" / "research.md"
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    if specialist_type == SpecialistType.SUPPORT:
        path = BASE_DIR / "prompts" / "specialists" / "support.md"
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    st = specialist_type.value if specialist_type else "general"
    return (
        f"You are a {st} specialist. "
        "Use only the tools you are given. Return a clear summary for the host."
    )


def _load_skill_prompt(skill_id: str | None) -> str | None:
    if not skill_id:
        return None
    from src.agent.skills.registry import get_skill

    skill = get_skill(skill_id)
    if skill is None or not skill.prompt_file:
        return None
    path = BASE_DIR / "prompts" / skill.prompt_file
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _build_research_context(
    task: str,
    *,
    project_id: str | None = None,
) -> tuple[ContextFrame, list[dict]]:
    prompt_path = BASE_DIR / "prompts" / "specialists" / "research.md"
    system = prompt_path.read_text(encoding="utf-8")
    if project_id:
        system = f"{system.strip()}\n\nProject scope: {project_id}"
    system = f"{system.strip()}\n\nTask: {task}"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    return ContextFrame(identity=system), messages


def _build_support_context(
    conn: sqlite3.Connection,
    task: str,
    *,
    project_id: str | None = None,
    operator_id: str | None = None,
) -> tuple[ContextFrame, list[dict]]:
    prompt_path = BASE_DIR / "prompts" / "specialists" / "support.md"
    system = prompt_path.read_text(encoding="utf-8")
    system = f"{system.strip()}\n\nTask: {task}"
    return _with_support_frame(conn, system, task, project_id=project_id, operator_id=operator_id)


def _with_support_frame(
    conn: sqlite3.Connection,
    system: str,
    task: str,
    *,
    project_id: str | None = None,
    operator_id: str | None = None,
) -> tuple[ContextFrame, list[dict]]:
    engine = ContextEngine(conn)
    knowledge_parts: list[str] = []

    wiki = engine._wiki_search(task, project_id=project_id)
    if wiki:
        knowledge_parts.append(f"<wiki-context>\n{wiki}\n</wiki-context>")

    frame = ContextFrame(identity=system)
    if project_id:
        tool_frame = engine.build_for_tool(
            project_id, f"support_specialist:{task}", operator_id=operator_id,
        )
        if tool_frame is not None:
            frame = tool_frame.model_copy(update={
                "identity": system,
                "knowledge": "\n\n".join(
                    part for part in (tool_frame.knowledge, *knowledge_parts) if part
                ),
            })

    frame = frame.model_copy(update={"identity": system})
    messages = frame.to_messages()
    messages.append({"role": "user", "content": task})
    return frame, messages
